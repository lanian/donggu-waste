"""동구 기본 행정동과 동별 기본 수거팀. DB 이전 단계에서 누락분만 등록한다.

출처: https://app.donggu.kr/index.es?sid=a1 (우리동네 행정복지센터, 2026-11-09 확인).
담당팀은 동마다 한 팀을 기본 연결한다. 요일·한도·코드·판별 낱말은 기관 설정을 따른다.
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


def seed_default_dong_crews(con) -> int:
    """이전 트랜잭션 안에서 사용 중인 기본 동의 미지정 담당팀만 한 번 보충한다."""
    names = {row[0] for row in con.execute("SELECT name FROM crew").fetchall()}
    dongs = con.execute("""SELECT id, name FROM dong
        WHERE active=1 AND (crew_id IS NULL OR crew_id='') ORDER BY sort, name""").fetchall()
    added = 0
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for did, name in dongs:
        canonical = "".join(name.split())
        if canonical not in DONGGU_DONGS:
            continue
        base = canonical + " 수거팀"
        crew_name, suffix = base, 2
        while crew_name in names:
            crew_name = f"{base} ({suffix})"
            suffix += 1
        cid = str(uuid.uuid4())
        con.execute("INSERT INTO crew (id, name) VALUES (?,?)", (cid, crew_name))
        con.execute("UPDATE dong SET crew_id=?, updated_at=? WHERE id=?", (cid, now, did))
        names.add(crew_name)
        added += 1
    return added
