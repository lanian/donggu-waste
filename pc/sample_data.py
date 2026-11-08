"""가상 자료와 시제품용 수수료표.

수수료표: data/fee_table.json ([별표 1] 원문 HWP에서 tools/convert_fee_hwp.py 로 변환).
시행일은 원문에 없어 비어 있으며, 운영 전 담당자 2인 대조가 필요하다.
- 가상 접수 자료는 실제 인물·주소가 아니다.
"""
from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from dgpc.paths import resource_root

FEE_TABLE = json.loads((resource_root() / "data" / "fee_table.json").read_text(encoding="utf8"))


_SURNAMES = "김이박최정강조윤장임"
_GIVEN = ["가상", "예시", "시험", "샘플", "모의", "연습", "보기", "견본"]
_ROADS = ["예시로", "가상대로", "시험길", "샘플로", "모의로"]

# 시험용 가상 행정동 (실제 동 이름이 아님). 판별 낱말은 위 가상 도로명과 맞춘다.
SAMPLE_DONGS = [
    {"name": "가상1동", "keywords": ["예시로"], "weekdays": [0, 2, 4], "daily_limit": 20},
    {"name": "가상2동", "keywords": ["가상대로", "시험길"], "weekdays": [1, 3], "daily_limit": 15},
    {"name": "가상3동", "keywords": ["샘플로", "모의로"], "weekdays": [], "daily_limit": None},
]


def dong_of(address: str) -> str:
    """가상 주소 → 가상 행정동 (make_daylist 용)."""
    for d in SAMPLE_DONGS:
        if any(k in address for k in d["keywords"]):
            return d["name"]
    return ""


def seed_dongs(con, crew_ids: list[str] | None = None) -> list[str]:
    """가상 행정동을 넣는다(이미 있는 이름은 건너뜀). crew_ids 를 주면 차례로 기본 담당 팀으로 연결."""
    from dgpc import area
    out = []
    for i, d in enumerate(SAMPLE_DONGS):
        if area.get_dong(con, d["name"]):
            continue
        crew = crew_ids[i % len(crew_ids)] if crew_ids else None
        out.append(area.save_dong(con, {**d, "crew_id": crew, "sort": i}))
    return out


ADDR_STYLES = {"mixed": None, "short": 0, "medium": 1, "long": 2}


def _addr(rng: random.Random, i: int, style: str = "mixed") -> str:
    base = f"가상시 동구 {rng.choice(_ROADS)} {rng.randint(1, 300)}"
    kind = i % 3 if ADDR_STYLES.get(style) is None else ADDR_STYLES[style]
    if kind == 0:
        return base
    if kind == 1:
        return f"{base}, {rng.randint(101, 115)}동 {rng.randint(1, 25)}{rng.randint(1, 4):02d}호"
    return f"{base} 가상아파트 {rng.randint(101, 115)}동 {rng.randint(1, 25)}{rng.randint(1, 4):02d}호 (후문 쪽 경비실 옆 출입구)"


def make_daylist(count: int, work_date: str, assign_ver: int = 1, seed: int = 7, addr_style: str = "mixed") -> dict:
    rng = random.Random(seed)
    rows = [r for r in FEE_TABLE["rows"] if r["unit"] == "EA" and r["status"] == "CONFIRMED"]
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
            "address": _addr(rng, i, addr_style),
            "spot_note": rng.choice(["", "분리수거장 옆", "1층 현관 앞", "주차장 입구 왼쪽"]),
            "lat": round(35.146 + rng.uniform(-0.01, 0.01), 5),
            "lon": round(126.923 + rng.uniform(-0.01, 0.01), 5),
            "eta": f"{9 + i * 15 // 60:02d}:{i * 15 % 60:02d}",
            "memo": rng.choice(["", "오전 방문 희망", "도착 전 전화 요청"]),
            "items": items,
        })
        stops[-1]["dong"] = dong_of(stops[-1]["address"])
    return {
        "schema": "dg.daylist/1",
        "work_date": work_date,
        "crew": "가상 1팀",
        "assign_ver": assign_ver,
        "fee_table": {"version_id": FEE_TABLE["version_id"], "label": FEE_TABLE["label"]},  # 표 전체는 앱에 내장, 버전만 전달
        "stops": stops,
    }
