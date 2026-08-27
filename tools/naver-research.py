#!/usr/bin/env python3
"""
네이버 오픈API 수요조사 도구
============================

쿠팡 파트너스 API는 "얼마에 팔리는지"만 알려줍니다.
"몇 명이나 찾는지 / 언제 팔리는지 / 누가 사는지"는 알려주지 않습니다.
이 스크립트가 그 절반을 채웁니다.

  쿠팡 API              네이버 API
  ────────────          ────────────────────────
  경쟁 가격 ○           경쟁 가격 ○ (100건, 하루 25,000회)
  로켓 비율 ○           등록 상품 수 ○  ← 경쟁 강도
  판매량   ✕            검색량 추이 ○  ← 수요 크기·계절성
  리뷰수   ✕            연령·성별 ○   ← 누구한테 팔지

  ※ 두 API 어디에도 "판매 개수"는 없습니다. 그건 공개 API로 안 나옵니다.
    검색량이 판매량의 대리지표입니다.

  준비
  ----
  1. https://developers.naver.com/apps/#/register 에서 애플리케이션 등록 (무료·즉시)
     - 사용 API: [검색] + [데이터랩(검색어트렌드)] + [데이터랩(쇼핑인사이트)] 체크
  2. .env 파일에 추가:

        NAVER_CLIENT_ID=발급받은_Client_ID
        NAVER_CLIENT_SECRET=발급받은_Client_Secret

  실행
  ----
      python naver-research.py "에코백"
      python naver-research.py "에코백" "캔버스백" --cat 50015340
      python naver-research.py --file keywords.txt --out 수요조사.csv

  파이썬 표준 라이브러리만 씁니다. pip install 필요 없습니다.
"""

import argparse
import csv
import json
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

API = os.environ.get("NAVER_API_BASE", "https://openapi.naver.com")  # 테스트용 오버라이드
SHOP = "/v1/search/shop.json"
TREND = "/v1/datalab/search"
SHOP_AGE = "/v1/datalab/shopping/category/keyword/age"

# 마진 역산 상수 — coupang-research.py 와 동일하게 유지할 것
FEE_RATE, AD_RATE, OVERHEAD = 0.105, 0.15, 1.38
LOGI_GROWTH = 2800


# ─────────────────────────────────────────────────────────── 인증
def load_keys(env_path: Path):
    cid = os.environ.get("NAVER_CLIENT_ID")
    sec = os.environ.get("NAVER_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    if not env_path.exists():
        sys.exit(f"""
[에러] {env_path} 를 찾을 수 없습니다.

  1) https://developers.naver.com/apps/#/register 에서 앱 등록 (무료)
     사용 API 로 [검색], [데이터랩(검색어트렌드)], [데이터랩(쇼핑인사이트)] 를 모두 체크
  2) {env_path} 파일을 만들고 아래 두 줄을 넣으세요:

     NAVER_CLIENT_ID=발급받은_Client_ID
     NAVER_CLIENT_SECRET=발급받은_Client_Secret
""")
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k == "NAVER_CLIENT_ID":
            cid = cid or v
        elif k == "NAVER_CLIENT_SECRET":
            sec = sec or v
    if not cid or not sec:
        sys.exit(f"[에러] {env_path} 에 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 가 둘 다 필요합니다.")
    return cid, sec


def call(path, cid, sec, query=None, body=None):
    url = f"{API}{path}" + (f"?{query}" if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("X-Naver-Client-Id", cid)
    req.add_header("X-Naver-Client-Secret", sec)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            sys.exit(f"[401] 인증 실패. Client ID/Secret 을 확인하세요.\n{detail}")
        if e.code == 403:
            sys.exit(f"[403] 이 API가 앱에 등록되어 있지 않습니다.\n"
                     f"      developers.naver.com → 내 애플리케이션 → API 설정에서\n"
                     f"      [검색]과 [데이터랩]을 모두 추가하세요.\n{detail}")
        if e.code == 429:
            sys.exit(f"[429] 일일 한도(25,000회)를 초과했습니다.\n{detail}")
        sys.exit(f"[{e.code}] {path}\n{detail}")


# ─────────────────────────────────────────────────────────── 조회
def strip_tags(s):
    return s.replace("<b>", "").replace("</b>", "").replace("&amp;", "&") \
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')


def shop_search(keyword, cid, sec, n=100):
    q = urllib.parse.urlencode({"query": keyword, "display": min(n, 100), "sort": "sim"},
                               quote_via=urllib.parse.quote)
    d = call(SHOP, cid, sec, query=q)
    rows = []
    for it in d.get("items", []):
        try:
            price = int(it.get("lprice") or 0)
        except ValueError:
            price = 0
        rows.append({
            "상품명": strip_tags(it.get("title", "")),
            "가격": price,
            "쇼핑몰": it.get("mallName", ""),
            "브랜드": it.get("brand", "") or it.get("maker", ""),
            "카테고리": " > ".join(filter(None, [it.get(f"category{i}") for i in range(1, 5)])),
            "상품유형": it.get("productType", ""),
            "링크": it.get("link", ""),
        })
    return d.get("total", 0), rows


def trend(keyword, cid, sec, months=13):
    end = date.today().replace(day=1) - timedelta(days=1)
    start = (end.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    body = {
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "timeUnit": "month",
        "keywordGroups": [{"groupName": keyword, "keywords": [keyword]}],
    }
    d = call(TREND, cid, sec, body=body)
    res = d.get("results") or []
    return [(p["period"][:7], p["ratio"]) for p in (res[0]["data"] if res else [])]


def by_age(keyword, category, cid, sec, months=4):
    """쇼핑인사이트 연령별. 카테고리 코드가 있어야 하고, 없으면 건너뛴다."""
    if not category:
        return None
    end = date.today().replace(day=1) - timedelta(days=1)
    start = (end.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    body = {
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "timeUnit": "month", "category": category, "keyword": keyword,
        "ages": ["10", "20", "30", "40", "50", "60"],
    }
    try:
        d = call(SHOP_AGE, cid, sec, body=body)
    except SystemExit:
        return None
    res = d.get("results") or []
    agg = Counter()
    for p in (res[0]["data"] if res else []):
        agg[p["group"]] += p["ratio"]
    return agg or None


# ─────────────────────────────────────────────────────────── 분석
def won(n):
    return f"{round(n):,}원"


def max_source_price(sell_price):
    """이 가격에 팔려면 사입 원가(관세·통관 포함)가 얼마 이하여야 하는지 역산."""
    net = sell_price * (1 - FEE_RATE - AD_RATE) - LOGI_GROWTH - sell_price / 11
    return max(net / OVERHEAD, 0)


def sparkline(series):
    if not series:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(series), max(series)
    span = (hi - lo) or 1
    return "".join(blocks[min(int((v - lo) / span * 7.99), 7)] for v in series)


def season_text(pts):
    """계절성을 문장으로. pts = [(YYYY-MM, ratio), ...]"""
    if len(pts) < 6:
        return None, None, None
    peak = max(pts, key=lambda x: x[1])
    trough = min(pts, key=lambda x: x[1])
    now = pts[-1]
    swing = peak[1] / trough[1] if trough[1] else 0
    return peak, trough, (now, swing)


def report(keyword, total, rows, pts, ages, cat_name=None):
    print()
    print("═" * 66)
    print(f"  📊 {keyword}")
    print("═" * 66)

    # ── 수요 크기와 계절성 ──
    if pts:
        vals = [v for _, v in pts]
        print("\n  【 검색량 추이 】 12개월  (네이버 검색어트렌드, 최대치=100)")
        print(f"    {pts[0][0]} {sparkline(vals)} {pts[-1][0]}")
        peak, trough, (now, swing) = season_text(pts)
        print(f"    최고 {peak[0]} ({peak[1]:.0f})  ·  최저 {trough[0]} ({trough[1]:.0f})"
              f"  ·  현재 {now[0]} ({now[1]:.0f})")
        if swing >= 2.0:
            print(f"    ⚠️  성수기/비수기 차이가 {swing:.1f}배입니다. 계절 상품입니다.")
            print(f"       → {peak[0][5:]}월에 팔려면 2~3개월 전에 사입·통관을 끝내야 합니다.")
        pos = now[1] / peak[1] * 100 if peak[1] else 0
        if pos < 60:
            print(f"    ⚠️  지금은 성수기의 {pos:.0f}% 수준. 재고를 안고 비수기로 들어갑니다.")
        elif pos > 85:
            print(f"    ⭕ 지금이 성수기({pos:.0f}%)입니다. 다만 지금 사입하면 도착이 늦습니다.")

    # ── 경쟁 강도 ──
    print(f"\n  【 경쟁 강도 】 네이버 등록 상품 {total:,}건")
    if total > 500_000:
        print("    ✕ 레드오션입니다. 이 키워드 자체로는 절대 상위노출 안 됩니다.")
        print("      → 반드시 세부 키워드(소재·용도·사이즈)로 좁히세요.")
    elif total > 100_000:
        print("    ⚠️  포화 시장. 대형 키워드는 포기하고 중·소형 키워드로 진입하세요.")
    elif total > 10_000:
        print("    △ 경쟁은 있지만 세부 키워드로 자리를 잡을 수 있습니다.")
    else:
        print("    ⭕ 경쟁 상품이 적습니다. 다만 수요도 같이 확인하세요.")

    # ── 가격 ──
    prices = sorted(r["가격"] for r in rows if r["가격"] > 0)
    lo = mid = None
    if prices:
        lo, mid = prices[0], statistics.median(prices)
        p10 = prices[len(prices) // 10] if len(prices) >= 10 else lo
        print(f"\n  【 가격 】 {len(prices)}건 기준")
        print(f"    최저 {won(lo)} · 하위10% {won(p10)} · 중간 {won(mid)} · 최고 {won(prices[-1])}")
        src_mid = max_source_price(mid)
        src_p10 = max_source_price(p10)
        print(f"\n  【 진입 가능 사입가 】 (로켓그로스·광고 15% 기준)")
        print(f"    중간가 {won(mid)}에 팔려면 → 사입가 {won(src_mid)} 이하 (약 ¥{src_mid/9.5:,.0f})")
        print(f"    하위10% {won(p10)}에 맞추려면 → 사입가 {won(src_p10)} 이하 (약 ¥{src_p10/9.5:,.0f})")

    # ── 누가 사는가 ──
    if ages:
        tot = sum(ages.values()) or 1
        top = sorted(ages.items(), key=lambda x: -x[1])[:3]
        print(f"\n  【 구매 연령 】{' (' + cat_name + ')' if cat_name else ''}")
        print("    " + " · ".join(f"{a}대 {v/tot*100:.0f}%" for a, v in top))
        main_age = top[0][0]
        if main_age in ("40", "50"):
            print(f"    → 주 수요층이 {main_age}대입니다. 상세페이지·상품명을 그 연령대에 맞추세요.")
            print("      (실용성·용량·내구성 소구. '감성'·'트렌디' 소구는 헛돕니다)")
        elif main_age in ("10", "20"):
            print(f"    → 주 수요층이 {main_age}대입니다. 가격 민감도가 높습니다.")

    # ── 판매처 집중도 ──
    malls = Counter(r["쇼핑몰"] for r in rows if r["쇼핑몰"])
    brands = Counter(r["브랜드"] for r in rows if r["브랜드"])
    if malls:
        top_mall, top_n = malls.most_common(1)[0]
        print(f"\n  【 판매처 】 상위 {len(rows)}건에 판매처 {len(malls)}곳")
        if top_n >= len(rows) * 0.3:
            print(f"    ⚠️  '{top_mall}' 한 곳이 {top_n}건을 차지합니다. 대형 셀러가 장악 중.")
    if brands:
        b_share = sum(brands.values()) / max(len(rows), 1)
        print(f"    브랜드 표기 상품 {b_share*100:.0f}% "
              f"({', '.join(b for b, _ in brands.most_common(3))})")
        if b_share > 0.6:
            print("    ⚠️  브랜드 상품 위주입니다. 노브랜드 사입품은 신뢰도에서 밀립니다.")

    # ── 경쟁 상품 ──
    if rows:
        print("\n  【 상위 노출 상품 】")
        for r in rows[:5]:
            print(f"    {won(r['가격']):>10}  {r['상품명'][:36]}  [{r['쇼핑몰'][:10]}]")
            print(f"       {r['링크'][:72]}")

    # ── 최종 판정 ──
    score, reasons = 0, []
    if total > 500_000:
        score -= 2; reasons.append("등록 상품이 50만 건을 넘습니다(대형 키워드 진입 불가)")
    elif total > 100_000:
        score -= 1; reasons.append("포화 시장입니다")
    elif total < 10_000:
        score += 1; reasons.append("경쟁 상품이 적습니다")

    if pts:
        peak, trough, (now, swing) = season_text(pts)
        pos = now[1] / peak[1] * 100 if peak[1] else 100
        if swing >= 2.0 and pos < 60:
            score -= 2; reasons.append(f"계절 상품인데 지금은 비수기 진입 구간({pos:.0f}%)입니다")
        elif swing >= 2.0:
            score -= 1; reasons.append(f"성수기/비수기 차이가 {swing:.1f}배입니다")

    if mid:
        src = max_source_price(mid)
        if src < 3000:
            score -= 2; reasons.append(f"중간가로 팔아도 사입가가 {won(src)}(¥{src/9.5:,.0f}) 이하여야 합니다")
        elif src < 6000:
            score -= 1; reasons.append(f"사입가 한도가 {won(src)}(¥{src/9.5:,.0f})로 빠듯합니다")
        else:
            score += 1; reasons.append(f"사입가 한도 {won(src)}(¥{src/9.5:,.0f})는 여유가 있습니다")

    if brands and sum(brands.values()) / max(len(rows), 1) > 0.6:
        score -= 1; reasons.append("브랜드 상품 위주라 노브랜드 사입품은 신뢰도에서 밀립니다")

    verdict = "○ 해볼 만함" if score >= 1 else ("△ 조건부" if score >= -2 else "✕ 비추천")
    print("\n" + "─" * 66)
    print(f"  판정: {verdict}")
    for r in reasons:
        print(f"    · {r}")
    if verdict.startswith("✕"):
        print("    → 이 키워드 자체로는 들어가지 마세요. 세부 키워드로 다시 조회하세요.")
    elif verdict.startswith("△"):
        print("    → 대형 키워드는 포기하고, 위 '세부 키워드'로 좁혀 다시 조회하세요.")
    print("─" * 66)

    return {"등록상품수": total, "최저가": lo, "중간가": mid, "판정": verdict,
            "진입사입가": round(max_source_price(mid)) if mid else None,
            "현재검색지수": pts[-1][1] if pts else None}


# ─────────────────────────────────────────────────────────── main
def main():
    p = argparse.ArgumentParser(
        description="네이버 오픈API로 사입 후보의 수요·경쟁·연령을 조사합니다.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("keywords", nargs="*", help="조회할 검색어")
    p.add_argument("--file", help="검색어가 한 줄에 하나씩 든 텍스트 파일")
    p.add_argument("--cat", help="쇼핑 카테고리 코드 (연령 분석용, 예: 50015340=여성가방>에코백)")
    p.add_argument("--env", default=".env", help=".env 경로 (기본: 현재 폴더)")
    p.add_argument("--out", default="naver-research.csv", help="CSV 저장 경로")
    p.add_argument("--display", type=int, default=100, help="상품 조회 건수 (최대 100)")
    args = p.parse_args()

    keywords = list(args.keywords)
    if args.file:
        keywords += [l.strip() for l in Path(args.file).read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
    if not keywords:
        p.print_help()
        sys.exit(0)

    cid, sec = load_keys(Path(args.env).expanduser())

    all_rows, summary = [], []
    for kw in keywords:
        total, rows = shop_search(kw, cid, sec, args.display)
        pts = trend(kw, cid, sec)
        ages = by_age(kw, args.cat, cid, sec)
        stat = report(kw, total, rows, pts, ages)
        for r in rows:
            all_rows.append({"검색어": kw, **r})
        summary.append({"검색어": kw, **stat})

    if all_rows:
        out = Path(args.out)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader()
            w.writerows(all_rows)
        print(f"\n✅ 상품 {len(all_rows)}건을 {out} 에 저장했습니다.")

    if len(summary) > 1:
        print("\n" + "═" * 66)
        print("  📋 키워드별 요약")
        print("═" * 66)
        print(f"  {'검색어':<16}{'등록상품':>10}{'중간가':>11}{'진입사입가':>11}  판정")
        for s in summary:
            print(f"  {s['검색어'][:14]:<16}{s['등록상품수']:>10,}"
                  f"{won(s['중간가']) if s['중간가'] else '-':>11}"
                  f"{won(s['진입사입가']) if s['진입사입가'] else '-':>11}  {s['판정']}")

    print("\n  ※ 검색량은 '수요의 대리지표'입니다. 실제 판매 개수는 어떤 공개 API에도 없습니다.")
    print("    개별 상품의 판매 규모는 쿠팡/네이버 상품 페이지의 리뷰 수로 추정하세요.\n")


if __name__ == "__main__":
    main()
