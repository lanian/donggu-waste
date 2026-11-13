"""동구 기본 행정동 기준 자료. DB 이전 단계에서 누락된 이름만 등록한다.

출처: https://app.donggu.kr/index.es?sid=a1 (우리동네 행정복지센터, 2026-11-09 확인).
담당 팀·요일·한도·부과번호용 코드와 법정동 판별 낱말은 기관 설정을 따른다.
"""
from __future__ import annotations

import datetime as dt
import uuid

DONGGU_DONGS = ("충장동", "동명동", "계림1동", "계림2동", "산수1동", "산수2동", "지산1동", "지산2동",
                "서남동", "학동", "학운동", "지원1동", "지원2동")


def seed_default_dongs(con) -> int:
    """호출자의 이전 트랜잭션 안에서 보충한다. 기존 행·연결·운영 설정은 건드리지 않는다."""
    existing = {"".join(row[0].split()) for row in con.execute("SELECT name FROM dong").fetchall()}
    added = 0
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for order, name in enumerate(DONGGU_DONGS):
        if name in existing:
            continue
        added += con.execute("""INSERT INTO dong (id, name, sort, updated_at) VALUES (?,?,?,?)
            ON CONFLICT(name) DO NOTHING""", (str(uuid.uuid4()), name, order, now)).rowcount
    return added
