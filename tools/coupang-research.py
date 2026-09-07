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
MAX_LIMIT = 10             # 한 번에 받을 수 있는 최대 상품 수 (초과 시 400 "limit is out of range")
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
    # 공백은 반드시 %20 으로. urlencode 는 '+' 를 쓰는데, 쿠팡이 이를 공백으로
    # 풀지 않아 검색어가 '일본+캔버스+에코백' 이라는 문자열이 되어 0건이 된다.
    query = f"keyword={urllib.parse.quote(keyword)}&limit={limit}"
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


def check_rcode(payload, keyword):
    """쿠팡은 HTTP 200이어도 본문에 오류 코드를 담아 보낸다."""
    rcode = str(payload.get("rCode", "0"))
    rmsg = payload.get("rMessage", "")
    if rcode not in ("0", "00", ""):
        print(f"  ⚠️  쿠팡 응답 코드 {rcode}: {rmsg}")
        if "limit" in str(rmsg).lower():
            print(f"      → --limit 은 1~{MAX_LIMIT} 만 허용됩니다.")
        return False
    return True


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


def deep_find_products(obj, depth=0):
    """구조가 예상과 달라도 상품처럼 생긴 리스트를 찾아본다."""
    if depth > 5:
        return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
        if any(k in obj[0] for k in ("productName", "productId", "productPrice")):
            return obj
    if isinstance(obj, dict):
        for v in obj.values():
            found = deep_find_products(v, depth + 1)
            if found:
                return found
    return None


# ─────────────────────────────────────────────────────────── 경쟁 분석
# 판매가에서 원가까지 역산할 때 쓰는 계수.
#   판매수수료 10.5%(VAT포함) + 물류비 + 광고비 15% + 실납부 부가세를 빼고 나면
#   수입원가로 쓸 수 있는 돈이 남고, 거기서 부대비용 38%를 다시 빼야 사입가가 된다.
FEE_RATE, AD_RATE, OVERHEAD = 0.105, 0.15, 1.38
LOGI_GROWTH, LOGI_SELF = 2800, 3000


def max_source_price(sell_price, logi=LOGI_GROWTH):
    """이 가격에 팔려면 사입가가 얼마 이하여야 하는지."""
    fee = sell_price * FEE_RATE * 1.1
    ad = sell_price * AD_RATE
    lo, hi = 0.0, float(sell_price)
    for _ in range(50):
        c = (lo + hi) / 2
        vat_due = sell_price / 11 - (c / 11 + fee / 11 + logi / 11)
        if sell_price - c - fee - logi - ad - vat_due > 0:
            lo = c
        else:
            hi = c
    return lo / OVERHEAD


def min_sell_price(source_price, logi=LOGI_GROWTH):
    """이 사입가면 최소 얼마에 팔아야 본전인지."""
    lo, hi = 0.0, source_price * 20 + 100000
    for _ in range(60):
        p = (lo + hi) / 2
        if max_source_price(p, logi) < source_price:
            lo = p
        else:
            hi = p
    return hi


STOP = {"개", "세트", "무료배송", "당일발송", "정품", "신상", "특가", "할인", "+",
        "및", "와", "과", "the", "for", "of"}


def keyword_freq(rows):
    """경쟁 상품명을 토큰화해 빈출 키워드를 센다."""
    from collections import Counter
    c = Counter()
    for r in rows:
        seen = set()
        for tok in str(r["상품명"]).replace("/", " ").replace(",", " ").split():
            tok = tok.strip("()[]·+")
            if len(tok) < 2 or tok.lower() in STOP:
                continue
            if any(ch.isdigit() for ch in tok) and not any(
                    "\uac00" <= ch <= "\ud7a3" for ch in tok):
                continue          # 숫자·모델번호만인 토큰 제외
            if tok not in seen:
                c[tok] += 1
                seen.add(tok)
    return c


def price_histogram(prices):
    """가격대 분포를 막대로."""
    if not prices:
        return []
    bands = [(0, 10000), (10000, 20000), (20000, 30000), (30000, 50000),
             (50000, 10**9)]
    labels = ["~1만원", "1~2만원", "2~3만원", "3~5만원", "5만원~"]
    out = []
    for (lo, hi), lab in zip(bands, labels):
        n = len([p for p in prices if lo <= p < hi])
        if n:
            out.append((lab, n))
    return out


def report(keyword, rows, from_cache=None, raw=None):
    print()
    print("═" * 64)
    src = f"  (캐시 {from_cache:.1f}시간 전)" if from_cache is not None else ""
    print(f"  🔍 {keyword}{src}")
    print("═" * 64)

    if not rows:
        salvaged = deep_find_products(raw) if raw else None
        if salvaged:
            print(f"  ⚠️  응답에 상품 {len(salvaged)}건이 있는데 해석하지 못했습니다.")
            print("      --debug 로 원본을 저장해 알려주시면 파서를 고치겠습니다.")
            return None
        print("  검색 결과 0건.")
        print("  ├ 검색어가 너무 구체적일 수 있습니다 → 단어를 줄여 다시 조회")
        print("  └ 넓은 검색어도 0건이면 API 응답 문제일 수 있습니다")
        return None

    prices = sorted(r["가격"] for r in rows if r["가격"])
    rocket = sum(1 for r in rows if r["로켓배송"])
    lo, mid = prices[0], statistics.median(prices)

    # ── 가격 분포 ──
    print("\n  【 가격 분포 】")
    hist = price_histogram(prices)
    top_band = max(hist, key=lambda x: x[1]) if hist else None
    for lab, n in hist:
        bar = "█" * n
        mark = "  ← 주력 구간" if top_band and lab == top_band[0] else ""
        print(f"    {lab:<8} {bar} {n}건{mark}")
    print(f"    최저 {won(lo)} · 중간 {won(mid)} · 최고 {won(prices[-1])}")

    # ── 채널 판단 ──
    pct = rocket * 100 // len(rows)
    print(f"\n  【 채널 】 로켓배송 {rocket}/{len(rows)}건 ({pct}%)")
    if pct >= 70:
        print("    ⚠️  상위권이 대부분 로켓입니다. 판매자배송으로는 노출에서 밀립니다.")
        print("       → 로켓그로스를 전제로 원가를 계산하세요.")
    elif pct <= 30:
        print("    ⭕ 판매자배송 상품이 많습니다. 마켓플레이스로 시작해도 됩니다.")

    # ── 진입 가능 사입가 ──
    src_lo, src_mid = max_source_price(lo), max_source_price(mid)
    print("\n  【 진입 가능 사입가 】 (로켓그로스·광고 15% 기준)")
    print(f"    최저가 {won(lo)}에 맞추려면 → 사입가 {won(src_lo)} 이하 (약 ¥{src_lo/9.5:,.0f})")
    print(f"    중간가 {won(mid)}에 팔려면 → 사입가 {won(src_mid)} 이하 (약 ¥{src_mid/9.5:,.0f})")

    # ── 경쟁자 키워드 ──
    freq = keyword_freq(rows)
    common = [(w, c) for w, c in freq.most_common(8) if c >= 2]
    rare = [w for w, c in freq.items() if c == 1][:10]
    if common:
        print("\n  【 경쟁자가 쓰는 키워드 】 상품명에 넣을 것")
        print("    " + " · ".join(f"{w}({c})" for w, c in common))
    if rare:
        print("\n  【 아무도 안 쓰는 단어 】 롱테일 기회")
        print("    " + " · ".join(rare[:8]))

    # ── 상위 상품 + 링크 ──
    print("\n  【 경쟁 상품 】")
    for r in rows[:5]:
        tag = "🚀" if r["로켓배송"] else "  "
        print(f"    {tag} {won(r['가격']):>10}  {r['상품명'][:38]}")
        if r["링크"]:
            print(f"        {r['링크'][:70]}")

    # ── 최종 판정 ──
    print("\n" + "─" * 64)
    if src_lo < 1500:
        verdict, msg = "✕ 비추천", "최저가 경쟁이 극심합니다. 이 사입가로는 일본에서 못 구합니다."
    elif src_mid < 3000:
        verdict, msg = "△ 조건부", "최저가 경쟁은 포기하고, 디자인·브랜드로 중간가대를 노려야 합니다."
    else:
        verdict, msg = "○ 검토 가치", "사입가 여유가 있습니다. 인증 요건만 확인하세요."
    if pct >= 70:
        msg += " 로켓그로스 필수."
    print(f"  판정: {verdict}")
    print(f"  {msg}")
    print("─" * 64)
    print("  ※ 이 API는 판매량·리뷰수를 제공하지 않습니다. 실제 판매 규모는")
    print("    위 링크를 열어 리뷰 수를 보거나, 네이버 데이터랩으로 검색량을 확인하세요.")

    return {"최저가": lo, "중간가": mid, "최고가": prices[-1],
            "결과수": len(rows), "로켓배송수": rocket,
            "진입사입가": round(src_mid), "판정": verdict}


# ─────────────────────────────────────────────────────────── main
def main():
    p = argparse.ArgumentParser(
        description="쿠팡 파트너스 API로 사입 후보 상품의 국내 시세를 조사합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("keywords", nargs="*", help="조회할 검색어 (여러 개 가능)")
    p.add_argument("--file", help="검색어가 한 줄에 하나씩 든 텍스트 파일")
    p.add_argument("--limit", type=int, default=MAX_LIMIT,
                   help=f"키워드당 조회 건수 (1~{MAX_LIMIT}, 기본 {MAX_LIMIT})")
    p.add_argument("--env", default=".env", help=".env 경로 (기본: 현재 폴더)")
    p.add_argument("--out", default="coupang-research.csv", help="CSV 저장 경로")
    p.add_argument("--cache-hours", type=int, default=CACHE_HOURS, help="캐시 유효시간 (기본 24)")
    p.add_argument("--no-cache", action="store_true", help="캐시를 무시하고 새로 조회")
    p.add_argument("--debug", action="store_true", help="API 원본 응답을 raw-응답.json 으로 저장")
    args = p.parse_args()

    keywords = list(args.keywords)
    if args.file:
        keywords += [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
    if not keywords:
        p.print_help()
        sys.exit(0)

    if args.limit > MAX_LIMIT or args.limit < 1:
        print(f"\n[알림] --limit 은 1~{MAX_LIMIT} 만 됩니다 (쿠팡 제한). "
              f"{args.limit} → {MAX_LIMIT} 으로 조정합니다.")
        args.limit = MAX_LIMIT

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
            if args.debug:
                f = Path(f"raw-{kw.replace(' ', '_')[:20]}.json")
                f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  [debug] 원본 응답 저장: {f}")
            check_rcode(payload, kw)
            rows = extract(payload)
            cache_put(kw, args.limit, rows)
            stat = report(kw, rows, raw=payload)
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
        print(f"  {'검색어':<18}{'결과':>4}{'최저가':>11}{'진입사입가':>11}  판정")
        for s in summary:
            print(f"  {s['검색어'][:16]:<18}{s['결과수']:>4}{won(s['최저가']):>11}"
                  f"{won(s['진입사입가']):>11}  {s['판정']}")

    print(f"\n남은 호출 {HOURLY_LIMIT - quota_used()}회\n")


if __name__ == "__main__":
    main()
