"""가상 자료와 시제품용 수수료표.

수수료표 주의
- 아래 행은 요구 문서(2026-10-28 공식 페이지 확인)와 2026-11-02 검색 요약에서 확인한 일부 행뿐이다.
- 운영 전 공식 원문(조례 별표) 전체를 입력하고 2인 대조해야 한다.
- 가상 접수 자료는 실제 인물·주소가 아니다.
"""
from __future__ import annotations

import random
import uuid

FEE_TABLE = {
    "version_id": "proto-2026-11-02",
    "label": "시제품용 확인 행 (원문 대조 전)",
    "source_url": "https://www.donggu.kr/menu.es?mid=a10408010500",
    "source_page_updated": "2025-03-25",
    "source_checked_on": "2026-10-28",
    "effective_from": None,
    "rows": [
        {"id": "r-chair-fixed", "category": "가구류", "name": "의자", "spec": "고정식",
         "unit": "EA", "unit_label": "개", "price": 2000, "status": "CONFIRMED"},
        {"id": "r-chair-swivel", "category": "가구류", "name": "의자", "spec": "회전식",
         "unit": "EA", "unit_label": "개", "price": 3000, "status": "CONFIRMED"},
        {"id": "r-mattress-1", "category": "가구류", "name": "매트리스", "spec": "1인용",
         "unit": "EA", "unit_label": "개", "price": 8000, "status": "CONFIRMED"},
        {"id": "r-mattress-2", "category": "가구류", "name": "매트리스", "spec": "2인용",
         "unit": "EA", "unit_label": "개", "price": 11000, "status": "CONFIRMED"},
        {"id": "r-purifier-1m", "category": "가전류", "name": "정수기", "spec": "높이 1m 이상",
         "unit": "EA", "unit_label": "개", "price": 5000, "status": "CONFIRMED",
         "note": "검색 요약으로 확인, 원문 대조 필요"},
        {"id": "r-bag20l", "category": "기타", "name": "봉투에 담기 어려운 폐기물", "spec": "20ℓ 기준",
         "unit": "L20", "unit_label": "20ℓ당", "price": 3000, "status": "CONFIRMED",
         "note": "검색 요약으로 확인, 원문 대조 필요"},
    ],
    # 현장 표현 → 공식 명칭
    "aliases": {"쇼파": "소파", "장농": "장롱", "메트리스": "매트리스", "매트": "매트리스",
                "회전의자": "의자 회전식", "사무용의자": "의자 회전식", "물정수기": "정수기"},
}

_SURNAMES = "김이박최정강조윤장임"
_GIVEN = ["가상", "예시", "시험", "샘플", "모의", "연습", "보기", "견본"]
_ROADS = ["예시로", "가상대로", "시험길", "샘플로", "모의로"]


def _addr(rng: random.Random, i: int) -> str:
    base = f"가상시 동구 {rng.choice(_ROADS)} {rng.randint(1, 300)}"
    kind = i % 3
    if kind == 0:
        return base
    if kind == 1:
        return f"{base}, {rng.randint(101, 115)}동 {rng.randint(1, 25)}{rng.randint(1, 4):02d}호"
    return f"{base} 가상아파트 {rng.randint(101, 115)}동 {rng.randint(1, 25)}{rng.randint(1, 4):02d}호 (후문 쪽 경비실 옆 출입구)"


def make_daylist(count: int, work_date: str, assign_ver: int = 1, seed: int = 7) -> dict:
    rng = random.Random(seed)
    rows = [r for r in FEE_TABLE["rows"] if r["unit"] == "EA"]
    stops = []
    for i in range(count):
        items = []
        for r in rng.sample(rows, rng.randint(1, 3)):
            qty = rng.randint(1, 3)
            items.append({
                "line_id": str(uuid.UUID(int=rng.getrandbits(128))),
                "fee_rate_id": r["id"], "name": r["name"], "spec": r["spec"],
                "unit": r["unit"], "unit_label": r["unit_label"], "qty": qty,
                "unit_price": r["price"], "amount": r["price"] * qty, "price_status": "CONFIRMED",
            })
        stops.append({
            "seq": i + 1,
            "request_id": str(uuid.UUID(int=rng.getrandbits(128))),
            "receipt_no": f"DG-{work_date.replace('-', '')}-{i + 1:04d}",
            "req_ver": 1,
            "name": rng.choice(_SURNAMES) + rng.choice(_GIVEN),
            "phone": f"0100000{rng.randint(0, 9999):04d}",
            "address": _addr(rng, i),
            "spot_note": rng.choice(["", "분리수거장 옆", "1층 현관 앞", "주차장 입구 왼쪽"]),
            "lat": round(35.146 + rng.uniform(-0.01, 0.01), 5),
            "lon": round(126.923 + rng.uniform(-0.01, 0.01), 5),
            "eta": f"{9 + i * 15 // 60:02d}:{i * 15 % 60:02d}",
            "memo": rng.choice(["", "오전 방문 희망", "도착 전 전화 요청"]),
            "items": items,
        })
    return {
        "schema": "dg.daylist/1",
        "work_date": work_date,
        "crew": "가상 1팀",
        "assign_ver": assign_ver,
        "fee_table": FEE_TABLE,
        "stops": stops,
    }
