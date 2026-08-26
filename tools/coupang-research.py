#!/usr/bin/env python3
"""
쿠팡 파트너스 API 시장조사 도구
================================

일본 사입 후보 상품이 쿠팡에서 얼마에, 몇 개나 팔리고 있는지 자동으로 조회합니다.
사입 판정기의 "한국 최저가" 칸을 채우는 데 쓰세요.

  준비
  ----
  1. 쿠팡 파트너스 로그인 → 상단 [Tools] → 파트너스 API 발급
  2. 같은 폴더(또는 --env 로 지정한 경로)에 .env 파일 생성:

        COUPANG_ACCESS_KEY=발급받은_액세스_키
        COUPANG_SECRET_KEY=발급받은_시크릿_키

  실행
  ----
      python coupang-research.py "일본 캔버스 에코백"
      python coupang-research.py "시세이도 퍼펙트휩" "로토 안약" --limit 30
      python coupang-research.py --file keywords.txt --out 조사결과.csv

  주의
  ----
  * Search API는 시간당 10회 제한입니다. 이 스크립트가 호출 수를 세어 막아줍니다.
  * 한 번 조회한 키워드는 캐시되어 쿼터를 소모하지 않습니다(기본 24시간).
  * 파이썬 표준 라이브러리만 씁니다. pip install 필요 없습니다.
"""

import argparse
import csv
import hashlib
import hmac
import json
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DOMAIN = "https://api-gateway.coupang.com"
SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/products/search"

HOURLY_LIMIT = 10          # 쿠팡 Search API 시간당 호출 한도
CACHE_HOURS = 24
STATE_DIR = Path.home() / ".coupang-research"


# ─────────────────────────────────────────────────────────── 인증
def load_keys(env_path: Path):
    """.env 에서 키를 읽는다. 환경변수가 이미 있으면 그쪽을 우선한다."""
    ak = os.environ.get("COUPANG_ACCESS_KEY")
    sk = os.environ.get("COUPANG_SECRET_KEY")
    if ak and sk:
        return ak, sk

    if not env_path.exists():
        sys.exit(
            f"[오류] .env 를 찾을 수 없습니다: {env_path}\n"
            "       --env 로 경로를 지정하거나, 아래 형식으로 파일을 만드세요.\n\n"
            "       COUPANG_ACCESS_KEY=액세스키\n"
            "       COUPANG_SECRET_KEY=시크릿키"
        )

    values = {}
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")

    # 키 이름은 사람마다 다르게 쓰므로 흔한 변형을 모두 받아들인다
    def pick(*names):
        for n in names:
            for k, v in values.items():
                if k.upper().replace("-", "_") == n and v:
                    return v
        return None

    ak = ak or pick("COUPANG_ACCESS_KEY", "ACCESS_KEY", "CP_ACCESS_KEY",
                    "COUPANG_ACCESSKEY", "ACCESSKEY", "PARTNERS_ACCESS_KEY")
    sk = sk or pick("COUPANG_SECRET_KEY", "SECRET_KEY", "CP_SECRET_KEY",
                    "COUPANG_SECRETKEY", "SECRETKEY", "PARTNERS_SECRET_KEY")

    if not ak or not sk:
        found = ", ".join(values.keys()) or "(비어 있음)"
        missing = []
        if not ak: missing.append("ACCESS KEY")
        if not sk: missing.append("SECRET KEY")
        sys.exit(
            f"[오류] {env_path} 에서 {' / '.join(missing)} 를 찾지 못했습니다.\n"
            f"       파일에 있는 항목: {found}\n\n"
            "       아래 이름 중 하나로 바꿔주세요:\n"
            "         COUPANG_ACCESS_KEY=...   (또는 ACCESS_KEY)\n"
            "         COUPANG_SECRET_KEY=...   (또는 SECRET_KEY)"
        )
    return ak, sk


def build_auth(method: str, path: str, query: str, access_key: str, secret_key: str) -> str:
    """CEA HmacSHA256 인증 헤더를 만든다. 서명 시각은 반드시 GMT."""
    signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
    message = signed_date + method + path + query
    signature = hmac.new(
        secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return (
        f"CEA algorithm=HmacSHA256, access-key={access_key}, "
        f"signed-date={signed_date}, signature={signature}"
    )


# ─────────────────────────────────────────────────────────── 쿼터 · 캐시
def _quota_file():
    return STATE_DIR / "quota.json"


def quota_used() -> int:
    """최근 1시간 내 호출 수."""
    f = _quota_file()
    if not f.exists():
        return 0
    try:
        calls = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    cutoff = time.time() - 3600
    return len([t for t in calls if t > cutoff])


def quota_record():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    f = _quota_file()
    try:
        calls = json.loads(f.read_text()) if f.exists() else []
    except (json.JSONDecodeError, OSError):
        calls = []
    cutoff = time.time() - 3600
    calls = [t for t in calls if t > cutoff] + [time.time()]
    f.write_text(json.dumps(calls))


def cache_path(keyword: str, limit: int) -> Path:
    key = hashlib.sha256(f"{keyword}|{limit}".encode()).hexdigest()[:16]
    return STATE_DIR / "cache" / f"{key}.json"


def cache_get(keyword: str, limit: int, max_age_h: int):
    f = cache_path(keyword, limit)
    if not f.exists():
        return None
    age_h = (time.time() - f.stat().st_mtime) / 3600
    if age_h > max_age_h:
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8")), age_h
    except (json.JSONDecodeError, OSError):
        return None


def cache_put(keyword: str, limit: int, data):
    f = cache_path(keyword, limit)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────── 조회
def search(keyword: str, limit: int, access_key: str, secret_key: str):
    query = urllib.parse.urlencode({"keyword": keyword, "limit": limit})
    url = f"{DOMAIN}{SEARCH_PATH}?{query}"
    auth = build_auth("GET", SEARCH_PATH, query, access_key, secret_key)

    req = urllib.request.Request(url, headers={
        "Authorization": auth,
        "Content-Type": "application/json;charset=UTF-8",
    })
    try:
        with urllib.request.urlopen(req, timeout=30,
                                    context=ssl.create_default_context()) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 401:
            hint = "키가 잘못됐거나 PC 시계가 어긋났을 수 있습니다(서명에 시각이 들어갑니다)."
        elif e.code == 429:
            hint = "시간당 호출 한도를 넘었습니다. 한 시간 뒤에 다시 시도하세요."
        else:
            hint = ""
        sys.exit(f"[오류] HTTP {e.code} — {hint}\n{body}")
    except urllib.error.URLError as e:
        sys.exit(f"[오류] 접속 실패: {e.reason}")


def extract(payload):
    """응답에서 상품 목록을 꺼낸다. 응답 구조가 바뀌어도 최대한 견디게."""
    data = payload.get("data")
    if isinstance(data, dict):
        items = data.get("productData") or data.get("products") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append({
            "상품명": it.get("productName", ""),
            "가격": it.get("productPrice") or 0,
            "로켓배송": "O" if it.get("isRocket") else "",
            "무료배송": "O" if it.get("isFreeShipping") else "",
            "카테고리": it.get("categoryName", ""),
            "상품ID": it.get("productId", ""),
            "링크": it.get("productUrl", ""),
        })
    return out


# ─────────────────────────────────────────────────────────── 출력
def won(n):
    return f"{int(n):,}원"


def report(keyword, rows, from_cache=None):
    print()
    print("═" * 62)
    src = f"  (캐시 {from_cache:.1f}시간 전)" if from_cache is not None else ""
    print(f"  🔍 {keyword}{src}")
    print("═" * 62)

    if not rows:
        print("  검색 결과 없음 — 국내에 아직 없는 상품일 수 있습니다.")
        print("  ⚠️  기회일 수도 있지만, 인증 문제로 아무도 못 파는 물건일 확률도 높습니다.")
        return None

    prices = sorted(r["가격"] for r in rows if r["가격"])
    rocket = sum(1 for r in rows if r["로켓배송"])

    print(f"  검색 결과 {len(rows)}건 · 로켓배송 {rocket}건 ({rocket*100//len(rows)}%)")
    if prices:
        print(f"  최저 {won(prices[0])} · 중간 {won(statistics.median(prices))} · 최고 {won(prices[-1])}")
    print("─" * 62)
    for r in rows[:8]:
        tag = "🚀" if r["로켓배송"] else "  "
        name = r["상품명"][:34]
        print(f"  {tag} {won(r['가격']):>12}  {name}")
    if len(rows) > 8:
        print(f"     … 외 {len(rows)-8}건 (CSV에 전부 저장됩니다)")

    if prices:
        print("─" * 62)
        low = prices[0]
        print(f"  💡 사입 판단: 사입 원가가 {won(low/2.5)} 이하여야 마진이 납니다.")
        print(f"     (한국 최저가 {won(low)} ÷ 2.5 — 부대비용·수수료·광고비 감안)")
    return {"최저가": prices[0] if prices else 0,
            "중간가": statistics.median(prices) if prices else 0,
            "최고가": prices[-1] if prices else 0,
            "결과수": len(rows), "로켓배송수": rocket}


# ─────────────────────────────────────────────────────────── main
def main():
    p = argparse.ArgumentParser(
        description="쿠팡 파트너스 API로 사입 후보 상품의 국내 시세를 조사합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("keywords", nargs="*", help="조회할 검색어 (여러 개 가능)")
    p.add_argument("--file", help="검색어가 한 줄에 하나씩 든 텍스트 파일")
    p.add_argument("--limit", type=int, default=30, help="키워드당 조회 건수 (기본 30)")
    p.add_argument("--env", default=".env", help=".env 경로 (기본: 현재 폴더)")
    p.add_argument("--out", default="coupang-research.csv", help="CSV 저장 경로")
    p.add_argument("--cache-hours", type=int, default=CACHE_HOURS, help="캐시 유효시간 (기본 24)")
    p.add_argument("--no-cache", action="store_true", help="캐시를 무시하고 새로 조회")
    args = p.parse_args()

    keywords = list(args.keywords)
    if args.file:
        keywords += [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
    if not keywords:
        p.print_help()
        sys.exit(0)

    ak, sk = load_keys(Path(args.env).expanduser())

    used = quota_used()
    print(f"\n시간당 호출 한도 {HOURLY_LIMIT}회 중 {used}회 사용됨 · 남음 {HOURLY_LIMIT - used}회")

    all_rows, summary = [], []
    for kw in keywords:
        cached = None if args.no_cache else cache_get(kw, args.limit, args.cache_hours)
        if cached:
            rows, age = cached[0], cached[1]
            stat = report(kw, rows, from_cache=age)
        else:
            if quota_used() >= HOURLY_LIMIT:
                print(f"\n⛔ 시간당 한도({HOURLY_LIMIT}회)에 도달해 '{kw}' 부터 중단합니다.")
                print("   한 시간 뒤에 다시 실행하면 이어서 조회합니다.")
                print("   (이미 조회한 키워드는 캐시되어 쿼터를 쓰지 않습니다)")
                break
            payload = search(kw, args.limit, ak, sk)
            quota_record()
            rows = extract(payload)
            cache_put(kw, args.limit, rows)
            stat = report(kw, rows)
            time.sleep(1)          # 연속 호출 완충

        for r in rows:
            all_rows.append({"검색어": kw, **r})
        if stat:
            summary.append({"검색어": kw, **stat})

    if all_rows:
        out = Path(args.out)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\n✅ 상품 {len(all_rows)}건을 {out} 에 저장했습니다.")

    if len(summary) > 1:
        print("\n" + "═" * 62)
        print("  📊 키워드별 요약")
        print("═" * 62)
        print(f"  {'검색어':<22}{'결과':>5}{'최저가':>12}{'중간가':>12}")
        for s in summary:
            print(f"  {s['검색어'][:20]:<22}{s['결과수']:>5}{won(s['최저가']):>12}{won(s['중간가']):>12}")

    print(f"\n남은 호출 {HOURLY_LIMIT - quota_used()}회\n")


if __name__ == "__main__":
    main()
