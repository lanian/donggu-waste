// 단독 모드용 내장 수수료표. pc/sample_data.py 의 FEE_TABLE 과 같은 행이다.
// 운영 전 공식 원문(조례 별표) 전체로 바꾸고 2인 대조해야 한다.
export const BUILTIN_FEES = {
  "version_id": "builtin-2026-11-02",
  "label": "내장 수수료표: 확인된 일부 행 (원문 대조 전)",
  "source_url": "https://www.donggu.kr/menu.es?mid=a10408010500",
  "source_page_updated": "2025-03-25",
  "source_checked_on": "2026-10-28",
  "effective_from": null,
  "rows": [
    {
      "id": "r-chair-fixed",
      "category": "가구류",
      "name": "의자",
      "spec": "고정식",
      "unit": "EA",
      "unit_label": "개",
      "price": 2000,
      "status": "CONFIRMED"
    },
    {
      "id": "r-chair-swivel",
      "category": "가구류",
      "name": "의자",
      "spec": "회전식",
      "unit": "EA",
      "unit_label": "개",
      "price": 3000,
      "status": "CONFIRMED"
    },
    {
      "id": "r-mattress-1",
      "category": "가구류",
      "name": "매트리스",
      "spec": "1인용",
      "unit": "EA",
      "unit_label": "개",
      "price": 8000,
      "status": "CONFIRMED"
    },
    {
      "id": "r-mattress-2",
      "category": "가구류",
      "name": "매트리스",
      "spec": "2인용",
      "unit": "EA",
      "unit_label": "개",
      "price": 11000,
      "status": "CONFIRMED"
    },
    {
      "id": "r-purifier-1m",
      "category": "가전류",
      "name": "정수기",
      "spec": "높이 1m 이상",
      "unit": "EA",
      "unit_label": "개",
      "price": 5000,
      "status": "CONFIRMED",
      "note": "검색 요약으로 확인, 원문 대조 필요"
    },
    {
      "id": "r-bag20l",
      "category": "기타",
      "name": "봉투에 담기 어려운 폐기물",
      "spec": "20ℓ 기준",
      "unit": "L20",
      "unit_label": "20ℓ당",
      "price": 3000,
      "status": "CONFIRMED",
      "note": "검색 요약으로 확인, 원문 대조 필요"
    }
  ],
  "aliases": {
    "쇼파": "소파",
    "장농": "장롱",
    "메트리스": "매트리스",
    "매트": "매트리스",
    "회전의자": "의자 회전식",
    "사무용의자": "의자 회전식",
    "물정수기": "정수기"
  }
};
