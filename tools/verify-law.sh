#!/usr/bin/env bash
# 국가법령정보 공동활용 API로 이 저장소의 가이드가 인용한 조문을 직접 확인한다.
#
# 사용법:
#   1) https://open.law.go.kr 에서 오픈API 활용 신청 (무료)
#   2) 승인된 ID(이메일 앞부분)를 OC 값으로 사용
#   3) ./tools/verify-law.sh <OC>
#
# 예: ./tools/verify-law.sh hong
set -euo pipefail

OC="${1:-}"
if [ -z "$OC" ]; then
  echo "사용법: $0 <OC>   (OC = law.go.kr 오픈API 신청 시 발급받은 ID)" >&2
  exit 1
fi

BASE="https://www.law.go.kr/DRF"
OUT="${OUT_DIR:-./law-out}"
mkdir -p "$OUT"

# 확인 대상: 법령명 | 조문번호(6자리: 조4 + 가지2) | 설명
TARGETS=(
  "관세법|008100|제81조 간이세율의 적용"
  "관세법|009600|제96조 여행자 휴대품·이사물품 등의 감면"
  "관세법 시행령|009600|제96조 간이세율의 적용 (배제 대상)"
  "관세법 시행규칙|004800|제48조 관세가 면제되는 여행자 휴대품 등"
  "부가가치세법|003500|제35조 수입세금계산서"
  "부가가치세법|003800|제38조 매입세액의 공제"
)

fetch() {  # fetch <법령명> <조문번호>
  local name="$1" jo="$2"
  # 1) 법령 일련번호(MST) 조회
  local mst
  mst=$(curl -sS --get "$BASE/lawSearch.do" \
          --data-urlencode "OC=$OC" \
          --data-urlencode "target=law" \
          --data-urlencode "type=XML" \
          --data-urlencode "query=$name" \
        | grep -o '<법령일련번호>[0-9]*</법령일련번호>' | head -1 \
        | grep -o '[0-9]*') || true
  [ -z "${mst:-}" ] && { echo "  ✗ '$name' 법령 일련번호를 찾지 못했습니다 (OC 승인 여부 확인)"; return 1; }

  # 2) 해당 조문 본문 조회
  curl -sS --get "$BASE/lawService.do" \
    --data-urlencode "OC=$OC" \
    --data-urlencode "target=law" \
    --data-urlencode "type=XML" \
    --data-urlencode "MST=$mst" \
    --data-urlencode "JO=$jo"
}

for t in "${TARGETS[@]}"; do
  IFS='|' read -r name jo desc <<< "$t"
  echo "══════════════════════════════════════════════════"
  echo "  $name $desc"
  echo "══════════════════════════════════════════════════"
  f="$OUT/$(echo "$name$jo" | tr -d ' ').xml"
  if fetch "$name" "$jo" > "$f" 2>/dev/null && [ -s "$f" ]; then
    # 태그를 걷어내고 본문만 읽기 좋게 출력
    python3 - "$f" <<'PY'
import re, sys, html
raw = open(sys.argv[1], encoding='utf-8', errors='replace').read()
raw = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', raw, flags=re.S)
txt = html.unescape(re.sub(r'<[^>]+>', '\n', raw))
lines = [l.strip() for l in txt.splitlines() if l.strip()]
print('\n'.join(lines[:80]) if lines else '(본문 없음 — OC 승인 또는 조문번호 확인)')
PY
    echo "  → 원본 저장: $f"
  else
    echo "  ✗ 조회 실패"
  fi
  echo
done

echo "완료. XML 원본은 $OUT/ 에 있습니다."
echo "결과를 Claude에게 붙여넣으면 가이드 내용과 대조해 드립니다."
