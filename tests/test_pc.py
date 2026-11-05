"""PC 업무 로직 테스트 (암호화 DB, 접수·이력·배정·노선·묶음·결과 병합·충돌·사진·마감). 휴대폰은 Python으로 흉내 낸다.
실행: python3 tests/test_pc.py
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import math
import os
import time
import re
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-")
os.environ["DG_DB_KEY"] = "test-db-key-not-for-production"
sys.path.insert(0, str(ROOT / "pc"))

import dgproto as P  # noqa: E402
from dgpc import routing, service as S, store  # noqa: E402

passed = 0


def ok(c, m):
    global passed
    if not c:
        raise AssertionError("실패: " + m)
    passed += 1
    print("  ✓", m)


def RV(con, rid):
    """화면이 연 접수 버전(req_ver) 흉내."""
    return S.one(con, "SELECT req_ver FROM request WHERE id=?", (rid,))["req_ver"]


def raises(fn, text, m):
    try:
        fn()
    except S.UserError as e:
        return ok(text in str(e), f"{m} → '{e}'")
    raise AssertionError("실패(오류 없음): " + m)


class Phone:
    """휴대폰 흉내: 등록 코드, 목록 열기, 결과·사진 파일 만들기."""

    def __init__(self, reg_text):
        raw = P.b45decode(reg_text)
        self.pc_sign, self.pc_kex = P.pub_from_raw(raw[4:69]), P.pub_from_raw(raw[69:134])
        self.kex, self.sign, self.id = P.gen_key(), P.gen_key(), uuid.uuid4().bytes
        self.seq = 0

    def code(self):
        data = self.id + P.pub_raw(self.kex) + P.pub_raw(self.sign)
        return "DGD1." + base64.urlsafe_b64encode(data + hashlib.sha256(data).digest()[:4]).decode().rstrip("=")

    def receive(self, frames):
        c = P.Collector()
        for f in frames:
            bid, *_ = c.add(f)
        head, payload, receipt = P.open_envelope(c.assemble(bid), self.pc_sign, self.kex, self.id)
        return head, P.expand_daylist(payload, S.FEE_ROWS), receipt

    def ev(self, stop, kind, body, assign_ver=None):
        self.seq += 1
        return {"event_id": str(uuid.uuid4()), "seq": self.seq, "request_id": stop["request_id"],
                "assign_ver": assign_ver or stop["assign_ver"], "base_req_ver": stop["req_ver"], "kind": kind, "body": body,
                "device_ts": f"2026-11-03T01:{self.seq:02d}:00Z"}

    def seal(self, payload, btype):
        head = P.Header(btype, uuid.uuid4().bytes, self.id, 1, 1, P.key_id(P.pub_raw(self.sign)))
        return P.seal(payload, head, self.pc_kex, self.sign)[0]


def main():
    print("작업 폴더:", os.environ["DG_STATE"])
    con = store.connect(create=True)
    S.ensure_pc_keys(con)
    db_file = Path(os.environ["DG_STATE"]) / "pc.db"
    ok(b"SQLite format 3" not in db_file.read_bytes()[:64] and store.ENCRYPTED, "PC DB 파일 전체 암호화(SQLCipher), PC 개인키도 DB 안에 보관")

    # 기기·팀
    phone = Phone(S.registration_text(con))
    reg = S.register_device(con, phone.code(), "가상 1팀 휴대폰", confirm=True)
    dev = reg["device_id"]
    ok(len(reg["fingerprint"]) == 9 and reg["confirmed"], f"휴대폰 등록, 지문 9자리 {reg['fingerprint']}")
    c1 = S.save_crew(con, {"name": "가상 1팀", "device_id": dev, "capacity_m3": 6, "shift_start": "09:00", "shift_end": "13:00"})
    c2 = S.save_crew(con, {"name": "가상 2팀", "capacity_m3": 6})

    # 접수
    fee = {r["name"] + "|" + r["spec"]: r["id"] for r in S.FEE_TABLE["rows"]}
    day = "2026-11-03"
    base = dict(planned_date=day, spot_note="1층 현관 앞")
    r1 = S.create_request(con, {**base, "name": "가상일", "phone": "010-0000-1111", "address": "가상시 동구 예시로 1",
                               "lat": 35.1470, "lon": 126.9250, "items": [{"fee_rate_id": fee["의자|고정식 의자"], "qty": 3}]})
    ok(r1["receipt_no"].startswith("DG-"), f"전화 접수 등록 {r1['receipt_no']}")
    raises(lambda: S.create_request(con, {**base, "name": "x", "phone": "1234", "address": "a", "items": [{"fee_rate_id": fee["의자|고정식 의자"], "qty": 1}]}),
           "연락처", "잘못된 연락처 거부")
    dups = S.find_duplicates(con, "01000001111", "다른 주소")
    ok(len(dups) == 1, "같은 연락처 → 중복 후보 1건 (자동 병합 없음)")
    r2 = S.create_request(con, {**base, "name": "가상이", "phone": "01000002222", "address": "가상시 동구 예시로 20",
                               "lat": 35.1440, "lon": 126.9300, "appt_start": "09:00", "appt_end": "10:00",
                               "items": [{"fee_rate_id": fee["침대|2인용 매트리스"], "qty": 1}, {"name": "운동기구 부속", "qty": 2}]})
    r3 = S.create_request(con, {**base, "name": "가상삼", "phone": "01000003333", "address": "가상시 동구 예시로 33",
                               "items": [{"fee_rate_id": fee["장롱|120cm 이상 1쪽당"], "qty": 2}]})
    r4 = S.create_request(con, {**base, "name": "가상사", "phone": "01000004444", "address": "가상시 동구 예시로 44",
                               "lat": 35.1500, "lon": 126.9200, "must_today": True,
                               "items": [{"fee_rate_id": fee["식탁|6인용 이상(대리석)"], "qty": 1}]})
    g2 = S.get_request(con, r2["id"])
    ok(g2["state"]["fee"]["final"] is None and g2["state"]["fee"]["confirmed_subtotal"] == 11000, "목록에 없는 품목 → 최종 금액 미확정, 확인된 소계 11,000원")
    g4 = S.get_request(con, r4["id"])
    ok(g4["items"][0]["price_status"] == "PENDING", "대리석 식탁(규격 확인 필요) → 단가 확인 대기")
    g3 = S.get_request(con, r3["id"])
    ok(g3["state"]["fee"]["qty_by_unit"] == {"쪽": 2} and g3["state"]["fee"]["final"] == 30000, "장롱 2쪽 = 30,000원, 부과단위 '쪽' 보존")

    # 수정 이력
    raises(lambda: S.update_request(con, r1["id"], {**base, "name": "가상일", "phone": "01000001111", "address": "가상시 동구 예시로 1, 101호",
                                                     "items": [{"fee_rate_id": fee["의자|고정식 의자"], "qty": 3}]}, ""), "수정 사유", "사유 없는 수정 거부")
    items1 = S.get_request(con, r1["id"])["items"]
    S.update_request(con, r1["id"], {**base, "req_ver": RV(con, r1["id"]), "name": "가상일", "phone": "01000001111", "address": "가상시 동구 예시로 1, 101호",
                                     "lat": 35.1470, "lon": 126.9250, "items": [{"line_id": items1[0]["line_id"], "fee_rate_id": fee["의자|고정식 의자"], "qty": 3}]},
                     "주민 요청: 호수 추가")
    g1 = S.get_request(con, r1["id"])
    ok(g1["req_ver"] == 2 and g1["versions"][0]["reason"] == "주민 요청: 호수 추가", "수정 시 이전 원본을 버전 1로 보존, 사유 기록")

    # 노선
    reqs = [S.get_request(con, r["id"]) for r in (r1, r2, r3, r4)]
    crews = [c for c in S.list_crews(con) if c["id"] in (c1, c2)]
    res = routing.plan(reqs, crews, S.settings(con)["depot"], time_limit_s=2)
    ok(res["matrix_source"] == "ESTIMATE" and "도로 아님" in res["matrix_note"], "로컬 OSRM 없음 → 직선거리 추정으로 표시")
    un = {u["request_id"]: u["reason"] for u in res["unassigned"]}
    ok(r3["id"] in un and "좌표" in un[r3["id"]], "좌표 없는 접수 → 미배정 사유 '위치 지정 필요'")
    routed = {s["request_id"]: (rt["crew_id"], s["eta"]) for rt in res["routes"] for s in rt["stops"]}
    ok(r4["id"] in routed, "오늘 필수 포함 건은 반드시 배정")
    ok(r2["id"] in routed and "09:00" <= routed[r2["id"]][1] <= "10:00", f"방문 약속 9~10시 → 예상 도착 {routed[r2['id']][1]}")

    # 배정 확정 (1팀에 r1, r2, r4)
    out = S.confirm_plan(con, day, {c1: [{"request_id": r1["id"], "eta": "09:20"}, {"request_id": r2["id"], "eta": "09:40", "unload_after": {"eta": "10:00", "label": "가상 반입지", "minutes": 20, "volume_m3": 5.2}}, {"request_id": r4["id"], "eta": "10:10"}]})
    b1 = out[0]["batch_id"]
    raises(lambda: S.confirm_plan(con, day, {c2: [{"request_id": r1["id"]}]}), "이미 다른 배정", "같은 접수를 다른 팀에 동시 배정 거부")

    # 목록 묶음 → 휴대폰 수신
    bun = S.make_bundle(con, b1)
    head, payload, receipt = phone.receive(bun["frames"])
    s1 = next(s for s in payload["stops"] if s["request_id"] == r1["id"])
    ok(s1["name"] == "가상일" and s1["phone"] == "01000001111" and "101호" in s1["address"], "휴대폰에 이름·연락처·주소(수정본) 전달")
    s2u = next(s for s in payload["stops"] if s["request_id"] == r2["id"])
    ok(s2u.get("unload_after", {}).get("label") == "가상 반입지" and s2u["unload_after"]["eta"] == "10:00" and not s1.get("unload_after"),
       "배정 확정한 반입지 경유(하차) 위치가 휴대폰 목록에 전달")
    S.confirm_receipt(con, bun["bundle_id"][:8], receipt)
    ok(S.list_batches(con, day)[0]["bundles"][0]["receipt_confirmed_at"], f"수신 확인 코드 대조 ({receipt})")

    # 현장 결과: r1 3개 중 2개 수거 + 현장 추가 회전의자 2개 모두 수거 → 일부 수거, 남은 1개
    s2 = next(s for s in payload["stops"] if s["request_id"] == r2["id"])
    s4 = next(s for s in payload["stops"] if s["request_id"] == r4["id"])
    add_line = str(uuid.uuid4())
    photo_raw = b"\xff\xd8fake-jpeg-bytes"
    photo_id = str(uuid.uuid4())
    events = [
        phone.ev(s1, "CONTACT", {"result": "REACHED"}),
        phone.ev(s1, "FIELD_ITEM_ADD", {"line_id": add_line, "fee_rate_id": fee["의자|회전식 의자"], "name": "의자", "spec": "회전식 의자",
                                        "unit": "EA", "unit_label": "개", "unit_price": 3000, "price_status": "CONFIRMED", "qty": 2}),
        phone.ev(s1, "ITEM_RESULT", {"line_id": s1["items"][0]["line_id"], "line_kind": "RECEIVED", "qty_confirmed": 3, "qty_collected": 2, "reason": "NOT_PUT_OUT"}),
        phone.ev(s1, "ITEM_RESULT", {"line_id": add_line, "line_kind": "FIELD", "qty_confirmed": 2, "qty_collected": 2, "reason": ""}),
        phone.ev(s1, "PHOTO_META", {"photo_id": photo_id, "phase": "AFTER", "sha256": hashlib.sha256(photo_raw).hexdigest(), "bytes": len(photo_raw)}),
        phone.ev(s1, "ATTEMPT", {"outcome": "PARTIAL"}),
        phone.ev(s1, "REVISIT_APPT", {"date": "2026-11-05", "start": "14:00", "end": "16:00"}),
        phone.ev(s2, "CONTACT", {"result": "NO_ANSWER"}),
        phone.ev(s4, "ITEM_RESULT", {"line_id": s4["items"][0]["line_id"], "line_kind": "RECEIVED", "qty_confirmed": 1, "qty_collected": 1, "reason": ""}),
        phone.ev(s4, "ATTEMPT", {"outcome": "DONE"}),
    ]
    file1 = phone.seal({"schema": "dg.result/1", "work_date": day, "events": events, "photos": []}, P.TYPE_RESULT)
    rep = S.import_result(con, file1)
    ok(rep["applied"] == 10 and rep["conflict"] == 0 and rep["release_code"], f"결과 10건 반영, 보관 종료 코드 {rep['release_code']}")
    g1 = S.get_request(con, r1["id"])
    lines = {l["line_id"]: l for l in g1["state"]["lines"]}
    ok(g1["status"] == "PARTIAL" and lines[s1["items"][0]["line_id"]]["remaining"] == 1 and lines[add_line]["remaining"] == 0,
       "접수 3 + 현장 추가 2 중 4개 수거 → 일부 수거, 원접수 행 남은 대상 1 (추가 품목으로 상쇄 안 됨)")
    ok(g1["planned_date"] == "2026-11-05" and g1["appt_start"] == "14:00" and g1["versions"][0]["reason"].startswith("현장 통화로 재방문"),
       "통화로 약속한 재방문(11/5 14~16시) → 접수 일정·방문 시간창 반영, 이전 일정은 버전 보존")
    ok(S.fee_usage(con)[0] in (fee["의자|고정식 의자"], fee["침대|2인용 매트리스"], fee["장롱|120cm 이상 1쪽당"], fee["식탁|6인용 이상(대리석)"]), "자주 쓰는 품목 집계")
    ok(g1["state"]["fee"]["final"] == 12000, "수수료: 고정식 의자 3개 6,000 + 회전식 의자 2개 6,000 = 12,000원")
    ok(S.get_request(con, r4["id"])["status"] == "DONE", "필수 포함 건 수거 완료")
    ok(S.get_request(con, r2["id"])["status"] == "ASSIGNED" and S.get_request(con, r2["id"])["state"]["contact"] == "NO_ANSWER",
       "통화 결과(부재)는 수거 상태를 바꾸지 않음")
    ok(S.import_result(con, file1).get("duplicate_file"), "같은 결과 파일 재가져오기 → 변경 없음")
    resend = phone.seal({"schema": "dg.result/1", "work_date": day, "events": events}, P.TYPE_RESULT)
    ok(S.import_result(con, resend)["skipped_duplicate"] == 10, "같은 이벤트를 새 파일로 재전송 → 10건 모두 중복 건너뜀")
    bad = bytearray(file1); bad[150] ^= 1
    raises(lambda: S.import_result(con, bytes(bad)), "검증에 실패", "변조된 결과 파일 거부")

    # 재방문: r1을 다음 날 다시 배정 → 남은 1개만 전달, 이전 수거량 이어서
    day2 = "2026-11-04"
    out2 = S.confirm_plan(con, day2, {c1: [{"request_id": r1["id"], "eta": "09:10"}]})
    bun2 = S.make_bundle(con, out2[0]["batch_id"])
    _, pl2, _ = phone.receive(bun2["frames"])
    rv = pl2["stops"][0]
    ok(rv["revisit"] and len(rv["items"]) == 1 and rv["items"][0]["qty"] - rv["items"][0]["prior_collected"] == 1,
       "재방문 목록: 남은 품목만, 이전 수거량을 이어서 전달 (남은 1개)")

    # 충돌: 1팀 11/3 배정을 새 버전으로 바꾼 뒤(r2만 남김) 옛 버전 결과가 오면
    S.confirm_plan(con, day, {c1: [{"request_id": r2["id"], "eta": "09:30"}]})
    late = phone.seal({"schema": "dg.result/1", "work_date": day, "events": [
        phone.ev(s2, "ITEM_RESULT", {"line_id": s2["items"][0]["line_id"], "line_kind": "RECEIVED", "qty_confirmed": 1, "qty_collected": 1, "reason": ""}, assign_ver=1)]},
        P.TYPE_RESULT)
    rep3 = S.import_result(con, late)
    ok(rep3["applied"] == 1 and rep3["conflict"] == 0 and S.get_request(con, r2["id"])["state"]["lines"][0]["qty_collected"] == 1
       and "같은 팀" in (S.one(con, "SELECT conflict_reason FROM result_event WHERE import_id=?", (rep3["import_id"],))["conflict_reason"] or ""),
       "v0.13: 지금도 같은 팀 배정이면 옛 갱신본 결과도 반영(오래된 목록이라고 충돌로 보내지 않음, 메모만)")
    # 다른 팀으로 옮긴 뒤 옛 목록 결과가 오면 충돌
    S.confirm_plan(con, day, {c1: [], c2: [{"request_id": r2["id"]}]})
    late_b = phone.seal({"schema": "dg.result/1", "work_date": day, "events": [
        phone.ev(s2, "ITEM_RESULT", {"line_id": s2["items"][0]["line_id"], "line_kind": "RECEIVED", "qty_confirmed": 1, "qty_collected": 0, "reason": "다시"}, assign_ver=2)]},
        P.TYPE_RESULT)
    rep3 = S.import_result(con, late_b)
    cf = S.list_conflicts(con)
    ok(rep3["conflict"] == 1 and len(cf) == 1 and "다른 팀" in cf[0]["conflict_reason"], "다른 팀으로 옮긴 접수의 옛 목록 결과 → 충돌함 (자동 덮어쓰기 없음)")
    raises(lambda: S.resolve_conflict(con, cf[0]["event_id"], "APPLY", ""), "사유", "충돌 처리에 사유 필수")
    S.resolve_conflict(con, cf[0]["event_id"], "APPLY", "팀장 확인: 실제 수거 못 함")
    ok(S.get_request(con, r2["id"])["state"]["lines"][0]["qty_collected"] == 0, "담당자 확인 후 반영")
    S.confirm_plan(con, day, {c2: []})

    # 취소 후 들어온 결과
    S.confirm_plan(con, day, {c1: [{"request_id": r2["id"]}]})
    S.cancel_request(con, r2["id"], "주민 직접 처리")
    late2 = phone.seal({"schema": "dg.result/1", "work_date": day, "events": [phone.ev(s2, "ATTEMPT", {"outcome": "DONE"}, assign_ver=3)]}, P.TYPE_RESULT)
    ok(S.import_result(con, late2)["conflict"] == 1 and S.get_request(con, r2["id"])["status"] == "CANCELLED", "취소된 접수의 결과 → 충돌함, 취소 상태 유지")

    # 사진 회수
    pfile = phone.seal({"schema": "dg.photos/1", "photos": [{"photo_id": photo_id, "request_id": r1["id"], "phase": "AFTER",
                                                             "sha256": hashlib.sha256(photo_raw).hexdigest(), "data": base64.b64encode(photo_raw).decode()}]}, S.TYPE_PHOTOS)
    prep = S.import_photos(con, pfile)
    ok(prep["recovered"] == 1 and prep["release_code"] and S.photo_bytes(con, photo_id) == photo_raw, f"사진 파일 회수·해시 대조, 사진 회수 확인 코드 {prep['release_code']}")
    raises(lambda: S.import_photos(con, file1), "파일 종류", "결과 파일을 사진 파일로 넣으면 거부")
    wrong = phone.seal({"schema": "dg.photos/1", "photos": [{"photo_id": str(uuid.uuid4()), "request_id": r1["id"], "phase": "AFTER",
                                                             "sha256": "0" * 64, "data": base64.b64encode(b"x").decode()}]}, S.TYPE_PHOTOS)
    wr = S.import_photos(con, wrong)
    ok(wr["hash_mismatch"] == 1 and wr["release_code"] is None, "해시가 맞지 않는 사진 → 확인 코드 미발급(휴대폰 삭제 금지)")

    # 분실 기기
    S.set_device_status(con, dev, "LOST")
    raises(lambda: S.import_result(con, phone.seal({"schema": "dg.result/1", "events": []}, P.TYPE_RESULT)), "LOST", "분실 처리된 기기의 파일 거부")
    raises(lambda: S.make_bundle(con, out2[0]["batch_id"]), "사용 중지", "분실 기기로는 목록 전송 안 됨")
    S.set_device_status(con, dev, "ACTIVE")

    # 휴대폰에서 기록한 하차 → 결과 파일 → PC 하차 기록
    phone.seq += 1
    uev = {"event_id": str(uuid.uuid4()), "seq": phone.seq, "request_id": "", "assign_ver": 1, "base_req_ver": None, "kind": "UNLOAD",
           "body": {"place": "가상 반입지", "at_time": "11:40", "volume_m3": 4.5, "weight_kg": 380, "note": "", "work_date": day}, "device_ts": "2026-11-03T02:40:00Z"}
    ur = S.import_result(con, phone.seal({"schema": "dg.result/1", "work_date": day, "events": [uev]}, P.TYPE_RESULT))
    ul = [u for u in S.list_unloads(con, day) if u["id"] == uev["event_id"]]
    ok(ur.get("unloads") == 1 and ul and ul[0]["volume_m3"] == 4.5 and "휴대폰" in ul[0]["note"], "휴대폰 하차 기록 → PC 하차 기록에 반영 (작업팀 자동 연결)")
    ur2 = S.import_result(con, phone.seal({"schema": "dg.result/1", "work_date": day, "events": [uev]}, P.TYPE_RESULT))
    ok(ur2["skipped_duplicate"] == 1 and len([u for u in S.list_unloads(con, day) if u["id"] == uev["event_id"]]) == 1, "같은 하차 기록은 한 번만")

    # 수납: 계산 금액·납부 금액·상태 분리, 정정은 지우지 않고 사유 기록
    ps = S.payment_status(con, r4["id"])
    ok(ps["status"] == "AMOUNT_PENDING" and ps["calculated"] is None, "금액 미확정 건은 '금액 확인 대기' (미납으로 단정하지 않음)")
    p1 = S.get_request(con, r1["id"])["payment"]
    ok(p1["calculated"] == 12000 and p1["status"] == "UNPAID", "수거 후 계산 12,000원 · 납부 기록 없음 → 미납")
    S.add_payment(con, r1["id"], {"amount": 10000, "method": "현장 납부", "paid_on": day})
    ok(S.payment_status(con, r1["id"])["status"] == "UNDERPAID", "10,000원 납부 → 부족")
    p2 = S.add_payment(con, r1["id"], {"amount": 5000, "method": "무통장 입금", "paid_on": day})
    ok(p2["status"] == "OVERPAID_REVIEW", "추가 5,000원 → 과납 확인 필요 (자동 환불 확정 안 함)")
    raises(lambda: S.void_payment(con, p2["records"][-1]["id"], ""), "사유", "수납 정정에 사유 필수")
    p3 = S.void_payment(con, p2["records"][-1]["id"], "중복 입력")
    S.add_payment(con, r1["id"], {"amount": 2000, "method": "무통장 입금", "paid_on": day})
    p4 = S.payment_status(con, r1["id"])
    ok(p4["status"] == "PAID" and len(p4["records"]) == 3 and p4["records"][1]["void_reason"] == "중복 입력", "정정 기록 보존, 합계 12,000원 → 납부 확인")
    raises(lambda: S.add_payment(con, r1["id"], {"amount": 1000, "method": "카드"}), "납부 방법", "정해진 납부 방법만")

    # 같은 장소 묶음 + 적재량 보정
    sp = [S.create_request(con, {**base, "planned_date": "2026-11-06", "name": n, "phone": ph, "address": "가상시 동구 예시로 77", "lat": 35.1480 + dx, "lon": 126.9260,
                                  "items": [{"fee_rate_id": fee["장롱|120cm 이상 1쪽당"], "qty": 2}]}) for n, ph, dx in (("가상오", "01000005555", 0), ("가상육", "01000006666", 0.0001))]
    reqs2 = [S.get_request(con, x["id"]) for x in sp]
    res2 = routing.plan(reqs2, crews[:1], S.settings(con)["depot"], time_limit_s=1)
    st2 = res2["routes"][0]["stops"]
    ok(len(st2) == 2 and all(x["same_place"] == 2 for x in st2) and st2[0]["eta"] < st2[1]["eta"], "11m 떨어진 두 접수 → 같은 장소로 묶어 연속 방문, 접수는 각각 유지")
    S.save_volume_override(con, fee["장롱|120cm 이상 1쪽당"], 4.0)
    res3 = routing.plan(reqs2, crews[:1], S.settings(con)["depot"], time_limit_s=1, volume_overrides=S.volume_overrides(con))
    ok(len(res3["routes"][0]["stops"]) == 0 and len(res3["unassigned"]) == 2 and "적재" in res3["unassigned"][0]["reason"],
       "장롱 1쪽 부피 보정 4m³ → 16m³로 6m³ 차량 초과, 미배정 사유 '적재'")

    # 하차 기록
    S.add_unload(con, {"crew_id": c1, "work_date": day, "at_time": "12:10", "place": "가상 반입지", "volume_m3": 5.5, "weight_kg": 420})
    raises(lambda: S.add_unload(con, {"crew_id": c1, "place": "가상 반입지"}), "부피나 중량", "하차 기록에 부피 또는 중량 필수")

    # 마감
    sm = S.day_summary(con, day)
    ok(sm["by_status"] == {"CANCELLED": 1, "RECEIVED": 1, "DONE": 1}, f"마감 요약 {day}: 완료 1·취소 1·미배정 1 {sm['by_status']}")
    ok(sm["unload_total"] == {"volume_m3": 10.0, "weight_kg": 800.0}, "마감: 하차 합계 10m³ · 800kg (PC 입력 5.5 + 휴대폰 기록 4.5)")
    lc = S.load_check(con, day, day)
    ok(len(lc["days"]) == 1 and lc["days"][0]["rec_m3"] == 10.0 and lc["days"][0]["rec_kg"] == 800 and lc["days"][0]["est_m3"] > 0
       and lc["kg_pct"] == round(100 * 800 / lc["days"][0]["est_kg"]), f"추정 ↔ 하차 기록 비교: 추정 {lc['days'][0]['est_m3']}m³·{lc['days'][0]['est_kg']}kg ↔ 기록 10m³·800kg (중량 {lc['kg_pct']}%)")
    ok(any(x["status"] == "AMOUNT_PENDING" for x in sm["payments"]), "마감: 금액 확인 대기 수납 건 표시")
    sm2 = S.day_summary(con, day2)
    ok([x["receipt_no"] for x in sm2["no_result"]] == [g1["receipt_no"]], f"마감 {day2}: 재배정했으나 결과 미회수 건 표시")
    S.close_day(con, day, "시험 마감")
    ok(S.day_summary(con, day)["closed"], "일일 마감 기록")

    # 유사 품목 적용 (표에 없는 품목 → 근거·확인자와 함께 확정 단가 행에 연결)
    fr = S.create_request(con, {**base, "name": "가상유사", "phone": "01000004545", "address": "가상시 동구 유사로 1",
                                "items": [{"name": "안마의자", "unit": "EA", "unit_label": "개", "qty": 1}]})
    g = S.get_request(con, fr["id"]); ln = g["state"]["lines"][0]
    ok(g["state"]["fee"]["final"] is None, "표에 없는 품목 → 금액 확인 대기")
    raises(lambda: S.link_similar(con, fr["id"], ln["line_id"], fee["장롱|120cm 이상 1쪽당"], "근거", "가상"), "부과단위", "부과단위가 다른 행은 적용 거부 (개 ↔ 쪽)")
    raises(lambda: S.link_similar(con, fr["id"], ln["line_id"], fee["의자|회전식 의자"], "", "가상"), "근거", "적용 근거 없으면 거부")
    raises(lambda: S.link_similar(con, fr["id"], ln["line_id"], fee["식탁|6인용 이하(대리석)"], "근거", "가상"), "확정된", "단가 미확정 행은 적용 거부")
    lk = S.link_similar(con, fr["id"], ln["line_id"], fee["의자|회전식 의자"], "같은 크기·재질로 담당 확인(가상)", "가상 확인자")
    g = S.get_request(con, fr["id"])
    ok(g["state"]["fee"]["final"] == 3000 and g["state"]["lines"][0]["link"]["confirmed_by"] == "가상 확인자" and g["items"][0]["price_status"] == "PENDING",
       "유사 품목 적용 → 계산 3,000원 확정, 원래 품목 행은 그대로, 근거·확인자 기록")
    raises(lambda: S.unlink_similar(con, lk, ""), "사유", "해제 사유 필수")
    S.unlink_similar(con, lk, "잘못 고름(가상)")
    ok(S.get_request(con, fr["id"])["state"]["fee"]["final"] is None and S.list_audit(con, entity="request")[0]["action"] == "UNLINK_SIMILAR", "해제하면 다시 확인 대기, 이력 보존")
    S.link_similar(con, fr["id"], ln["line_id"], fee["의자|회전식 의자"], "다시 확인(가상)", "가상 확인자")

    # 관할 확인·배정 조정 사유
    ok(S.jurisdiction_warnings(con, "광주광역시 북구 가상로 12") and not S.jurisdiction_warnings(con, "광주광역시 동구 가상로 12")
       and not S.jurisdiction_warnings(con, "가상구길 5") and not S.jurisdiction_warnings(con, "동구 서구로 7"), "관할 밖 주소(북구) 경고, 동구·도로명 속 '구'는 경고 안 함")
    wr = S.create_request(con, {**base, "name": "가상관할", "phone": "01000003434", "address": "가상광역시 북구 시험로 3", "items": [{"fee_rate_id": fee["의자|고정식 의자"], "qty": 1}]})
    ok(wr["warnings"] and "북구" in wr["warnings"][0], "관할 밖 접수도 막지 않고 경고와 함께 등록")
    raises(lambda: S.confirm_plan(con, "2026-11-08", {c2: [{"request_id": wr["id"]}]}, "", adjusted=True), "조정 사유", "추천을 바꾼 배정은 조정 사유 필수")
    S.confirm_plan(con, "2026-11-08", {c2: [{"request_id": wr["id"]}]}, "주민 요청으로 다른 팀(가상)", adjusted=True)
    ok(S.list_audit(con, entity="batch")[0]["detail"].find("주민 요청") > 0, "조정 사유를 배정 이력에 기록")

    # 통계·보고서
    st_ = S.stats(con, day, day2)
    ok(st_["totals"]["by_status"].get("DONE", 0) >= 1 and any(d["date"] == day and d["done"] >= 1 for d in st_["days"]) and st_["items"], f"기간 통계: 상태·날짜별·품목 집계 ({st_['totals']['requests']}건)")
    ok(st_["totals"]["unload_m3"] == 10.0, "통계: 하차 합계 반영")
    raises(lambda: S.stats(con, day2, day), "늦습니다", "기간 거꾸로 입력 거부")
    csv_b = S.stats_csv(con, day, day2).decode("utf-8-sig")
    names = [r["name"] for r in S.rows(con, "SELECT name, phone, address FROM request")]
    phones = [r["phone"] for r in S.rows(con, "SELECT phone FROM request")]
    ok(csv_b.startswith("접수번호,") and not any(n in csv_b for n in names) and not any(p_ in csv_b for p_ in phones) and "시험로" not in csv_b,
       "접수별 CSV: 이름·연락처·주소 없이 내보내기 (엑셀용 BOM)")
    ok(S.stats_csv(con, day, day2, "days").decode("utf-8-sig").splitlines()[0].startswith("날짜,접수"), "날짜별 CSV")
    sheet = S.dispatch_sheet(con, out2[0]["batch_id"])
    ok(sheet["stops"] and sheet["stops"][0]["name"] and S.list_audit(con, entity="batch")[0]["action"] == "PRINT", "인쇄용 배차표 자료 + 출력 이력 기록")

    # 휴대폰 단독 방식 백업(.dgb) → PC로 옮기기
    import json as _j, os as _os, zlib as _z
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    def make_dgb(obj, pw):
        salt, iv = _os.urandom(16), _os.urandom(12)
        co = _z.compressobj(9, _z.DEFLATED, -15); plain = co.compress(_j.dumps(obj).encode()) + co.flush()
        key = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 600000, 32)
        return b"DGB1" + salt + iv + AESGCM(key).encrypt(iv, plain, b"DGB1")
    sid, sid2, lid, pid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    pic = b"\xff\xd8solo-photo"
    solo_req = {"request_id": sid, "receipt_no": S.rows(con, "SELECT receipt_no FROM request ORDER BY received_at LIMIT 1")[0]["receipt_no"], "req_ver": 2, "name": "단독가상", "phone": "01000009191", "address": "가상시 동구 단독로 3",
                "spot_note": "", "memo": "", "planned_date": day, "items": [{"line_id": lid, "fee_rate_id": fee["장롱|120cm 이상 1쪽당"], "name": "장롱", "spec": "120cm 이상 1쪽당",
                "unit": "PIECE", "unit_label": "쪽", "qty": 1, "unit_price": 15000, "amount": 15000, "price_status": "CONFIRMED"}], "status": "ACTIVE",
                "received_at": "2026-11-02T23:00:00Z", "updated_at": "2026-11-02T23:10:00Z", "versions": [{"req_ver": 1, "at": "2026-11-02T23:05:00Z", "reason": "주민 요청", "snapshot": {"name": "단독가상"}}]}
    solo_c = {**solo_req, "request_id": sid2, "receipt_no": "DG-29991231-0002", "phone": "01000009292", "items": [], "status": "CANCELLED", "cancel_reason": "주민 취소", "versions": []}
    sev = [{"event_id": str(uuid.uuid4()), "seq": i + 1, "request_id": sid, "assign_ver": 1, "base_req_ver": 2, "kind": k, "body": b, "ts": "2026-11-03T01:00:00Z"} for i, (k, b) in enumerate([
        ("CONTACT", {"result": "REACHED"}), ("ITEM_RESULT", {"line_id": lid, "line_kind": "RECEIVED", "qty_confirmed": 1, "qty_collected": 1, "reason": ""}),
        ("PHOTO_META", {"photo_id": pid, "phase": "AFTER", "sha256": hashlib.sha256(pic).hexdigest(), "bytes": len(pic), "line_id": lid}),
        ("FIELD_MEMO", {"text": "지하 주차장 입구에 배출(가상)"}), ("ATTEMPT", {"outcome": "DONE"})])]
    sev.append({"event_id": str(uuid.uuid4()), "seq": 9, "request_id": "", "assign_ver": 0, "base_req_ver": None, "kind": "UNLOAD",
                "body": {"place": "가상 반입지", "at_time": "12:00", "volume_m3": 1.5, "work_date": day}, "ts": "2026-11-03T03:00:00Z"})
    dgb = make_dgb({"format": "dg.backup/1", "created_at": "2026-11-03T04:00:00Z", "state": {"mode": "SOLO", "requests": [solo_req, solo_c], "events": sev},
                    "photos": {pid: base64.b64encode(pic).decode()}}, "solo-pass-1")
    raises(lambda: S.import_solo(con, dgb, "wrong-pass"), "비밀번호", "단독 백업: 틀린 비밀번호 거부")
    pv = S.import_solo(con, dgb, "solo-pass-1", preview=True)
    ok(pv["new"] == 2 and pv["unloads"] == 1 and pv["photos"] == 1, "단독 백업 미리보기: 새 접수 2건·하차 1회·사진 1장")
    mv = S.import_solo(con, dgb, "solo-pass-1", c1)
    g = S.get_request(con, sid)
    ok(mv["requests"] == 2 and g["status"] == "DONE" and g["receipt_no"].endswith("S0001") and g["versions"] and S.photo_bytes(con, pid) == pic
       and S.get_request(con, sid2)["status"] == "CANCELLED" and mv["unloads_applied"] == 1,
       "단독 → PC: 접수·수정 이력·현장 결과(완료)·사진·취소·하차가 PC 원본으로, 겹치는 접수번호는 S 표시")
    ok(S.import_solo(con, dgb, "solo-pass-1", c1).get("duplicate_file"), "같은 단독 백업 재가져오기 → 중복 없음")
    g = S.get_request(con, sid)
    ok(g["state"]["memos"] == ["지하 주차장 입구에 배출(가상)"] and g["photos"][0]["line_id"] == lid, "현장 메모와 품목에 묶은 사진이 PC 접수 상세로")

    # 사용자·역할·변경 이력
    from dgpc import server as SV
    S.set_setting(con, "login", {"salt": "00" * 16, "hash": S._pw_hash("legacy-pass-123", b"\x00" * 16)})
    con.commit()
    S.migrate_legacy_login(con)
    ok(S.check_login(con, "admin", "legacy-pass-123") and not S.get_setting(con, "login"), "이전 단일 업무 비밀번호 → 관리자 계정(admin)으로 이전")
    raises(lambda: S.create_user(con, "kim", "김접수", "INTAKE", "short"), "10자", "비밀번호 10자 미만 거부")
    raises(lambda: S.create_user(con, "Kim Lee", "김접수", "INTAKE", "long-enough-pw"), "아이디", "아이디 형식 검사")
    S.ACTOR.set("admin")
    uid = S.create_user(con, "kim", "김접수", "INTAKE", "temp-pass-0001", must_change=True)
    raises(lambda: S.create_user(con, "kim", "김접수2", "INTAKE", "temp-pass-0001"), "이미", "같은 아이디 중복 거부")
    u = S.check_login(con, "kim", "temp-pass-0001")
    ok(u and u["role"] == "INTAKE" and u["must_change"], "접수 담당자 로그인, 임시 비밀번호 변경 요구")
    ok(S.check_login(con, "kim", "wrong-password") is None and S.check_login(con, "nobody", "x") is None, "틀린 비밀번호·없는 아이디 거부")
    raises(lambda: S.set_password(con, uid, "new-pass-0002", old="bad-old-pass"), "현재 비밀번호", "본인 변경 시 현재 비밀번호 확인")
    S.set_password(con, uid, "new-pass-0002", old="temp-pass-0001")
    ok(not S.check_login(con, "kim", "new-pass-0002")["must_change"], "비밀번호 변경 후 변경 요구 해제")
    admin_id = S.one(con, "SELECT id FROM app_user WHERE username='admin'")["id"]
    raises(lambda: S.set_user_active(con, admin_id, False, uid), "마지막 운영 관리자", "마지막 관리자 중지 거부")
    S.set_user_active(con, uid, False, admin_id)
    ok(S.check_login(con, "kim", "new-pass-0002") is None, "중지된 계정 로그인 거부")
    ok(SV.allowed("INTAKE", "POST", "requests") and not SV.allowed("INTAKE", "POST", "users") and not SV.allowed("INTAKE", "POST", "settings")
       and not SV.allowed("INTAKE", "GET", "audit") and not SV.allowed("INTAKE", "POST", "devices/register"), "접수 담당자: 접수 가능, 사용자·설정·기기·이력 불가")
    ok(SV.allowed("FIELD", "POST", "import/result") and SV.allowed("FIELD", "POST", "unloads") and not SV.allowed("FIELD", "POST", "requests/x/cancel")
       and not SV.allowed("FIELD", "POST", "assign") and not SV.allowed("FIELD", "GET", "duplicates"), "수거팀: 결과·하차 가능, 접수 수정·배정 불가")
    # 수거팀 계정 ↔ 팀: 자기 팀 배정만
    raises(lambda: S.create_user(con, "crewx", "가상팀원", "FIELD", "field-pass-001", crew_id="nope"), "없는 수거팀", "없는 팀 연결 거부")
    fu = S.create_user(con, "crew1", "가상팀원", "FIELD", "field-pass-001", crew_id=c1)
    ok(S.check_login(con, "crew1", "field-pass-001")["crew_id"] == c1, "수거팀 계정 로그인 정보에 팀 포함")
    mine = S.crew_request_ids(con, c1)
    ok(r1["id"] in mine and not S.request_in_crew(con, r3["id"], c1), "팀에 배정된 접수만 범위에 포함 (미배정 건 제외)")
    full, scoped = S.day_summary(con, day), S.day_summary(con, day, c2)
    ok(scoped["total"] <= full["total"] and all(u["crew_id"] == c2 for u in scoped["unloads"]), "마감 요약도 팀 범위로 거름")
    raises(lambda: S.set_user_crew(con, uid, c1), "수거팀 계정만", "수거팀이 아닌 계정에는 팀 연결 안 함")
    S.set_user_crew(con, fu, c2)
    ok(S.check_login(con, "crew1", "field-pass-001")["crew_id"] == c2, "팀 변경")
    ok(SV.allowed("INTAKE", "POST", "links/x/void") and not SV.allowed("FIELD", "POST", "links/x/void") and not SV.allowed("FIELD", "POST", "requests/x/link"), "유사 품목 적용은 접수 담당자·관리자만")

    S.ACTOR.set("kim")
    S.cancel_request(con, sp[0]["id"], "주민 취소 요청(시험)")
    a = S.list_audit(con, entity="request", actor="kim")
    ok(a and a[0]["action"] == "CANCEL", "변경 이력에 작업자(kim) 기록")
    ok(not any("가상오" in (x["detail"] or "") or "01000005555" in (x["detail"] or "") for x in S.list_audit(con, limit=1000)), "변경 이력 내용에 이름·연락처 없음")

    # 운영 수수료표 개정 (확인 필요 행 확정 → 다른 담당자 승인 → 시행일부터 적용)
    marble = fee["식탁|6인용 이상(대리석)"]
    S.ACTOR.set("admin")
    before = S.create_request(con, {**base, "name": "가상대리석", "phone": "01000007777", "address": "가상시 동구 대리석로 1", "items": [{"fee_rate_id": marble, "qty": 1}]})
    raises(lambda: S.save_fee_draft(con, {"effective_from": S.today_kst(), "confirmed_by": "가상 확인자", "overrides": {marble: {"price": 0, "reason": "x"}}}), "0원보다", "확정 단가 0원 거부")
    raises(lambda: S.save_fee_draft(con, {"effective_from": S.today_kst(), "confirmed_by": "가상 확인자", "overrides": {marble: {"price": 15000, "reason": ""}}}), "근거", "확정 근거 없으면 거부")
    raises(lambda: S.save_fee_draft(con, {"effective_from": "", "confirmed_by": "가상 확인자"}), "시행일", "시행일 필수")
    dr = S.save_fee_draft(con, {"effective_from": S.today_kst(), "confirmed_by": "가상 확인자(청소행정과)", "overrides": {marble: {"price": 15000, "reason": "6인용은 '이상'으로 적용 (가상 회신)"}}})
    raises(lambda: S.approve_fee_revision(con, dr), "작성한 사람", "작성자 본인 승인 거부 (2인 대조)")
    ok(S.fee_rows(con)[marble]["status"] == "REVIEW", "승인 전에는 적용 안 됨")
    S.ACTOR.set("kim")
    S.approve_fee_revision(con, dr)
    stt = S.fee_state(con)
    ok(stt["version_id"].endswith("-r1") and stt["confirmed_by"].startswith("가상 확인자") and stt["approved_by"] == "kim", f"다른 담당자 승인 → 적용 버전 {stt['version_id']}")
    after = S.create_request(con, {**base, "name": "가상대리석2", "phone": "01000007778", "address": "가상시 동구 대리석로 2", "items": [{"fee_rate_id": marble, "qty": 1}]})
    it_b, it_a = S.get_request(con, before["id"])["items"][0], S.get_request(con, after["id"])["items"][0]
    ok(it_b["price_status"] == "PENDING" and it_a["price_status"] == "CONFIRMED" and it_a["amount"] == 15000 and it_a["fee_version_id"] == stt["version_id"],
       "개정 뒤 새 접수만 확정 단가·새 버전, 이전 접수 원본은 그대로")
    ph = S.fee_table_for_phone(con)
    comp = P.compact_daylist({"work_date": day, "crew": "x", "assign_ver": 1, "fee_table": ph, "stops": [
        {"seq": 1, "request_id": after["id"], "receipt_no": "R", "req_ver": 1, "name": "n", "phone": "010", "address": "a", "items": [{**it_a, "line_kind": "RECEIVED"}]}]}, S.FEE_ROWS)
    exp = P.expand_daylist(comp, S.FEE_ROWS)
    ok(comp["fo"] == {marble: 15000} and exp["stops"][0]["items"][0]["price_status"] == "CONFIRMED" and exp["fee_table"]["overrides"] == {marble: 15000},
       "휴대폰 목록에 개정 버전·확정 단가 전달 (내장 원문 표 위에 덧씌움)")
    S.ACTOR.set("admin")
    future = (dt.date.fromisoformat(S.today_kst()) + dt.timedelta(days=10)).isoformat()
    dr2 = S.save_fee_draft(con, {"effective_from": future, "confirmed_by": "가상 확인자", "overrides": {fee["물탱크, 정화조(FRP)|용적 1.0 ㎡ 당"]: {"price": 5000, "reason": "가상 근거"}}})
    S.ACTOR.set("kim"); S.approve_fee_revision(con, dr2)
    ok(S.fee_state(con)["rev_no"] == 1 and S.fee_state(con, future)["rev_no"] == 2 and marble in S.fee_state(con, future)["overrides"], "시행일 전 개정은 그날부터 적용, 이전 확정값은 이어받음")
    S.ACTOR.set("admin")

    # 로컬 주소 → 좌표 (위치정보요약DB 형식 가상 파일: 파이프 구분·CP949·머리글 없음·UTM-K)
    from dgpc import geo
    def line(sg, road, main, sub, lat, lon, under="0"):
        x, y = geo.wgs84_to_utmk(lat, lon)
        return "|".join(["29110", "1", "2911010100", "가상광역시", sg, "가상동", "291104000001", road, under, str(main), str(sub), "", "61400", "주택", "0", "가상동", f"{x:.6f}", f"{y:.6f}"])
    lines = [line("동구", "시험로", 1, 0, 35.1470, 126.9240), line("동구", "시험로", 2, 0, 35.1475, 126.9245), line("동구", "시험로12번길", 5, 3, 35.1480, 126.9250),
             line("서구", "공통로", 10, 0, 35.1500, 126.8900), line("남구", "공통로", 10, 0, 35.1300, 126.9000), line("동구", "예시로", 77, 0, 35.1481, 126.9261)]
    lines += [line("동구", f"가상{i}길", i, 0, 35.14 + i * 1e-4, 126.92) for i in range(1, 40)]
    raw = "\n".join(lines).encode("cp949")
    pv = S.import_addr_points(con, raw, preview=True)
    ok(pv["columns"]["road"] == 7 and pv["columns"]["main"] == 9 and pv["columns"]["sub"] == 10 and pv["columns"]["x"] == 16 and pv["columns"]["y"] == 17 and pv["columns"].get("sigungu") == 4,
       f"머리글 없는 파일의 열 자동 인식 (도로명 7·본번 9·부번 10·X 16·Y 17·시군구 4)")
    ok(abs(pv["sample"][0]["lat"] - 35.147) < 1e-6 and abs(pv["sample"][0]["lon"] - 126.924) < 1e-6, "UTM-K → 위도·경도 변환 (오차 1e-6도 미만)")
    imp = S.import_addr_points(con, raw)
    ok(imp["count"] == 45 and S.addr_stats(con)["count"] == 45, "주소 좌표 45건 가져오기 (암호화 DB 안)")
    g = S.geocode(con, "가상광역시 동구 시험로 2, 3층")
    ok(g and abs(g["lat"] - 35.1475) < 1e-6 and g["exact"], "주소 '시험로 2' → 좌표")
    g2 = S.geocode(con, "가상시 동구 시험로12번길 5-9")
    ok(g2 and not g2["exact"] and abs(g2["lat"] - 35.1480) < 1e-6, "부번이 없으면 본번으로 찾고 '본번 기준' 표시")
    ok(S.geocode(con, "공통로 10")["ambiguous"] and abs(S.geocode(con, "가상광역시 서구 공통로 10")["lon"] - 126.89) < 1e-6, "여러 구에 같은 도로명 → 구 이름으로 구분, 없으면 모호 표시")
    ok(S.geocode(con, "없는로 1") is None, "자료에 없는 주소는 찾지 못함으로 표시")
    hdr = ("시군구명,도로명,건물본번,건물부번,위도,경도\n동구,헤더로,3,0,35.15,126.93\n").encode("utf-8")
    ok(S.import_addr_points(con, hdr, preview=True)["sample"][0]["road"] == "헤더로", "머리글 있는 파일(위도·경도)도 가져오기")
    rq = S.create_request(con, {**base, "planned_date": "2026-11-07", "name": "가상위치", "phone": "01000008888", "address": "가상시 동구 예시로 77",
                                "lat": None, "lon": None, "items": [{"fee_rate_id": fee["장롱|120cm 이상 1쪽당"], "qty": 1}]})
    gm = S.geocode_missing(con, "2026-11-07")
    r7 = S.get_request(con, rq["id"])
    ok(gm["found"] >= 1 and abs(r7["lat"] - 35.1481) < 1e-6 and r7["geo_source"] == "ADDR", "위치 없는 접수 일괄 찾기 → 출처 '주소 자료'로 기록")

    # 초성 검색 + 반입지 경유
    names = {r["name"] for r in S.list_requests(con, "ㄱㅅㅇ")}
    ok({"가상오", "가상육"} <= names and not any(n.startswith("가상하") for n in names), f"접수 초성 검색 'ㄱㅅㅇ' → 가상오·가상육 ({len(names)}건)")
    ok(all("예시로" in r["address"] for r in S.list_requests(con, "ㅇㅅㄹ 77")), "초성+일반 낱말 섞어 검색")
    many = [{"id": f"u{i}", "lat": 35.14 + 0.004 * i, "lon": 126.92 + 0.003 * i, "items": [{"name": "장롱", "spec": "", "qty": 1, "fee_rate_id": "X"}]} for i in range(4)]
    one_crew = [{"id": "c", "name": "1팀", "capacity_m3": 6, "shift_start": "09:00", "shift_end": "17:00"}]
    dep = {"lat": 35.1462, "lon": 126.9233}
    no_u = routing.plan(many, one_crew, dep, time_limit_s=1, volume_overrides={"X": 4})
    with_u = routing.plan(many, one_crew, dep, time_limit_s=2, volume_overrides={"X": 4}, unload_site={"lat": 35.16, "lon": 126.95, "label": "가상 반입지"}, max_unloads=3)
    rt = with_u["routes"][0]
    ok(len(no_u["routes"][0]["stops"]) == 1 and len(rt["stops"]) == 4 and rt["unloads"] == 3 and all(s["load_m3"] <= 6 for s in rt["stops"]),
       f"반입지 경유: 6m³ 차량으로 4m³×4건 → 하차 3회 끼워 4곳 모두 방문 (경유 없으면 1곳)")
    raises(lambda: S.save_settings(con, {"unload_site": {"lat": 10, "lon": 10}}), "국내", "반입지 좌표 범위 검사")
    far_end = {"lat": 35.20, "lon": 127.00, "label": "가상 종료지"}
    e1 = routing.plan(many[:2], one_crew, dep, time_limit_s=1, volume_overrides={"X": 1})
    e2 = routing.plan(many[:2], one_crew, dep, time_limit_s=1, volume_overrides={"X": 1}, end_site=far_end)
    ok(e2["end_site"] and e2["routes"][0]["end"] > e1["routes"][0]["end"] and len(e2["routes"][0]["stops"]) == 2, f"종료지를 따로 두면 그곳까지 계산 (종료 {e1['routes'][0]['end']} → {e2['routes'][0]['end']})")

    # 사진 보관 기간 정리
    S.ACTOR.set("admin")
    raises(lambda: S.save_settings(con, {"photo_retention_days": 3}), "7일", "사진 보관 기간 범위 검사")
    S.save_settings(con, {"photo_retention_days": 30})
    rec = S.rows(con, "SELECT photo_id, request_id FROM photo WHERE recovery_status='RECOVERED'")
    ok(rec and not S.purge_candidates(con), "회수 직후 사진은 정리 대상 아님")
    ok(not [c for c in S.purge_candidates(con, (dt.date.fromisoformat(S.today_kst()) + dt.timedelta(days=40)).isoformat()) if c["status"] not in ("DONE", "CANCELLED")],
       "처리 중(배정 상태)인 접수의 사진은 기간이 지나도 정리하지 않음")
    S.close_day(con, day2, "시험 마감 2")
    far = (dt.date.fromisoformat(S.today_kst()) + dt.timedelta(days=40)).isoformat()
    cand = S.purge_candidates(con, far)
    ok(len(cand) >= 1, f"40일 뒤 기준 처리 끝난 접수 사진 {len(cand)}장 정리 대상")
    pr = S.purge_photos(con, far)
    ok(pr["count"] == len(cand) and S.photo_bytes(con, cand[0]["photo_id"]) is None
       and S.one(con, "SELECT sha256, purged_at FROM photo WHERE photo_id=?", (cand[0]["photo_id"],))["purged_at"], "사진 원본 삭제, 해시·정리 시각은 남김")
    ok(S.list_audit(con, entity="photo")[0]["action"] == "PURGE", "사진 정리 이력 기록")

    # 암호화 백업·복원
    raises(lambda: S.make_backup(con, "short"), "12자", "백업 비밀번호 12자 미만 거부")
    b = S.make_backup(con, "backup-pass-for-test-01")
    raw = Path(b["file"]).read_bytes()
    ok(b"SQLite format 3" not in raw[:100] and "가상".encode() not in raw, "백업 파일은 암호화됨 (평문 머리·한글 자료 없음)")
    raises(lambda: S.verify_backup(con, b["name"], "wrong-backup-pass-00"), "열 수 없습니다", "다른 백업 비밀번호로 열기 거부")
    n_before = S.one(con, "SELECT COUNT(*) AS n FROM request")["n"]
    ok(S.verify_backup(con, b["name"], "backup-pass-for-test-01")["requests"] == n_before, f"백업 열어 확인: 접수 {n_before}건")
    raises(lambda: S.verify_backup(con, "../pc.db", "x" * 12), "이름", "백업 이름에 경로 넣기 거부")
    con.execute("INSERT INTO meta (key, value) VALUES ('after_backup', '1')")
    con.close()
    rr = store.restore_from(Path(b["file"]), "backup-pass-for-test-01")
    con = store.connect()
    ok(rr["kept"] and Path(rr["kept"]).exists() and not S.one(con, "SELECT 1 AS x FROM meta WHERE key='after_backup'") and S.one(con, "SELECT COUNT(*) AS n FROM request")["n"] == n_before,
       "복원: 백업 시점으로 되돌리고 기존 DB는 옆에 보관")
    ok(S.check_login(con, "admin", "legacy-pass-123") is not None, "복원 뒤 현재 DB 키로 열리고 계정 유지")
    print(f"\n통과 {passed}개")


def v10():
    """v0.10: 행정동·달력·한도·마감 이월·마감 통제·팀장·일괄 처리·사유 코드·접수 경로·일괄 등록·쪽 나눔·민원 기록·이력 거르기."""
    print("\n— v0.10 —")
    old_state = os.environ["DG_STATE"]
    # 이전 DB 올리기: 팀장 역할이 없는 사용자 표·날짜별 1행 마감 표·새 열이 없는 접수 표
    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-old-")
    import sqlcipher3
    oldp = Path(os.environ["DG_STATE"]) / "pc.db"
    oc = sqlcipher3.connect(str(oldp))
    oc.execute("PRAGMA key = 'test-db-key-not-for-production'")
    legacy = store.SCHEMA.replace(",'LEAD'", "").replace(", last_login_at TEXT, crew_id TEXT);", ", last_login_at TEXT);")
    legacy = legacy.split("-- (구) 날짜별 마감")[0] + "CREATE TABLE day_close (work_date TEXT PRIMARY KEY, closed_at TEXT NOT NULL, summary_json TEXT NOT NULL, note TEXT);"
    oc.executescript(legacy)
    oc.execute("INSERT INTO app_user (id, username, display_name, role, pw_salt, pw_hash, created_at) VALUES ('u1','old','옛 관리자','ADMIN','00','00','2026-01-01T00:00:00Z')")
    oc.execute("INSERT INTO day_close VALUES ('2026-10-01','2026-10-01T09:00:00Z','{}','옛 마감')")
    oc.commit(); oc.close()
    mc = store.connect()
    cols = {r[1] for r in mc.execute("PRAGMA table_info(request)").fetchall()}
    S.ACTOR.set("old")
    lead_ok = S.create_user(mc, "lead0", "가상 팀장", "LEAD", "lead-pass-0001")
    ok({"dong", "channel", "cancel_code", "cancelled_at"} <= cols and lead_ok and S.close_info(mc, "2026-10-01")["note"] == "옛 마감"
       and S.check_login(mc, "lead0", "lead-pass-0001")["role"] == "LEAD" and {"dong_h", "dong_b"} <= store._cols(mc, "addr_point"),
       "이전 DB 올리기: 접수 새 열(동·경로·취소 코드), 팀장 역할, 옛 마감 → 1회차, 주소 자료 동 열")
    mc.close()
    store.connect().close()
    ok(True, "같은 DB를 다시 열어도 이전(migrate) 반복 안전")

    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-v10-")
    con = store.connect(create=True)
    S.ensure_pc_keys(con)
    S.ACTOR.set("admin")
    S.create_user(con, "admin", "관리자", "ADMIN", "admin-pass-001")
    S.create_user(con, "kim", "김접수", "INTAKE", "intake-pass-01")
    S.create_user(con, "boss", "가상 팀장", "LEAD", "lead-pass-0001")
    fee = {r["name"] + "|" + r["spec"]: r["id"] for r in S.FEE_TABLE["rows"]}
    chair = fee["의자|고정식 의자"]
    c1 = S.save_crew(con, {"name": "가상 1팀", "capacity_m3": 8})
    c2 = S.save_crew(con, {"name": "가상 2팀", "capacity_m3": 8})
    import sample_data
    from dgpc import area as A, server as SV

    # 행정동 관리
    ids = sample_data.seed_dongs(con, [c1, c2, c1])
    ok(len(ids) == 3 and [d["name"] for d in A.list_dongs(con)] == ["가상1동", "가상2동", "가상3동"] and A.get_dong(con, "가상1동")["weekdays_label"] == "월·수·금",
       "가상 행정동 3개(가상1동~3동) 넣기: 수거 요일·판별 낱말·기본 담당 팀")
    ok(sample_data.seed_dongs(con) == [], "가상 동을 다시 넣어도 중복 없음")
    raises(lambda: A.save_dong(con, {"name": "가상1동"}), "같은 이름", "같은 이름 동 거부")
    raises(lambda: A.save_dong(con, {"name": "가상9동", "weekdays": [7]}), "요일", "잘못된 요일 거부")
    raises(lambda: A.save_dong(con, {"name": "가상9동", "daily_limit": "많이"}), "숫자", "한도는 숫자만")
    tmp = A.save_dong(con, {"name": "가상임시동", "keywords": "임시로, 임시길"})
    ok(A.get_dong(con, "가상임시동")["keywords"] == ["임시로", "임시길"], "판별 낱말 쉼표로 입력")
    A.delete_dong(con, tmp)
    ok(A.get_dong(con, "가상임시동") is None and S.list_audit(con, entity="dong")[0]["action"] == "DELETE", "쓰이지 않은 동 지우기, 이력 기록")

    # 주소 → 동 자동 판별
    ok(A.detect_dong(con, "가상시 동구 예시로 5")["dong"] == "가상1동" and A.detect_dong(con, "가상시 동구 예시로 5")["source"] == "KEYWORD", "주소 낱말(도로명)로 동 판별: 예시로 → 가상1동")
    ok(A.detect_dong(con, "가상시 동구 없는로 3")["dong"] is None, "맞는 낱말이 없으면 동 미정")
    A.save_dong(con, {**A.get_dong(con, "가상3동"), "keywords": ["샘플로", "모의로", "겹침로"]})
    A.save_dong(con, {**A.get_dong(con, "가상2동"), "keywords": ["가상대로", "시험길", "겹침로"]})
    amb = A.detect_dong(con, "가상시 동구 겹침로 1")
    ok(amb["dong"] is None and amb.get("ambiguous") and set(amb["candidates"]) == {"가상2동", "가상3동"}, "두 동에 같은 낱말 → 고르지 않고 후보만")
    from dgpc import geo
    x, y = geo.wgs84_to_utmk(35.15, 126.92)
    addr_raw = ("시군구명,읍면동명,도로명,건물본번,건물부번,관할행정동,X좌표,Y좌표\n" + f"동구,가상법정동,경계로,10,0,가상3동,{x:.3f},{y:.3f}\n").encode()
    pv = S.import_addr_points(con, addr_raw, preview=True)
    S.import_addr_points(con, addr_raw)
    ok(pv["has_dong"] and pv["sample"][0]["dong_h"] == "가상3동" and A.detect_dong(con, "가상시 동구 경계로 10")["dong"] == "가상3동"
       and A.detect_dong(con, "가상시 동구 경계로 10")["source"] == "ADDR", "주소 위치 자료의 관할 행정동 열로 동 판별 (낱말 없이도)")

    # 접수: 동 자동·직접, 접수 경로
    base = {"phone": "01000000001", "items": [{"fee_rate_id": chair, "qty": 1}]}
    ra = S.create_request(con, {**base, "name": "가상가", "address": "가상시 동구 예시로 1"})
    ok(ra["dong"] == "가상1동" and S.get_request(con, ra["id"])["channel"] == "전화", "접수 등록 시 동 자동 판별(가상1동), 접수 경로 기본 '전화'")
    rb = S.create_request(con, {**base, "phone": "01000000002", "name": "가상나", "address": "가상시 동구 예시로 2", "dong": "가상2동", "channel": "방문"})
    ok(rb["dong"] == "가상2동" and S.get_request(con, rb["id"])["channel"] == "방문", "화면에서 고른 동·접수 경로(방문)가 우선")
    rc = S.create_request(con, {**base, "phone": "01000000003", "name": "가상다", "address": "가상시 동구 없는로 3", "dong": ""})
    ok(rc["dong"] is None and any("행정동을 정하지" in w for w in rc["warnings"]), "동을 못 정하면 경고(막지 않음)")
    raises(lambda: S.create_request(con, {**base, "phone": "01000000004", "name": "x", "address": "a", "dong": "없는동"}), "등록되지 않은 행정동", "없는 동 거부")
    raises(lambda: S.create_request(con, {**base, "phone": "01000000004", "name": "x", "address": "a", "channel": "팩스"}), "접수 경로", "정해진 접수 경로만")

    # 휴무일·쉬는 요일·동 수거 요일·한도
    mon = "2026-11-09"  # 월
    ok(A.working_reason(con, "2026-11-07") and "토요일" in A.working_reason(con, "2026-11-07") and A.working_reason(con, mon) is None, "기본: 토·일 수거 안 함")
    A.add_holiday(con, "2026-11-10", "가상 휴무")
    ok("휴무일(가상 휴무)" == A.working_reason(con, "2026-11-10") and A.next_working_day(con, mon) == "2026-11-11", "휴무일 등록 → 다음 업무일은 휴무일 건너뜀")
    ok(A.next_working_day(con, mon, "가상1동") == "2026-11-11" and A.next_working_day(con, "2026-11-11", "가상2동") == "2026-11-12"
       and A.next_working_day(con, "2026-11-13", "가상1동") == "2026-11-16", "동 수거 요일(가상1동 월·수·금, 가상2동 화·목) 반영")
    with S.tx(con):
        A.save_capacity(con, {"daily_count": 2, "closed_weekdays": [6]})
    ok(A.working_reason(con, "2026-11-07") is None and A.capacity_settings(con)["daily_count"] == 2, "쉬는 요일·전체 한도 설정 (일요일만 쉼, 하루 2건)")
    for i in range(2):
        S.create_request(con, {**base, "phone": f"0100000010{i}", "name": f"가상한도{i}", "address": "가상시 동구 예시로 9", "planned_date": mon})
    w = S.create_request(con, {**base, "phone": "01000000109", "name": "가상초과", "address": "가상시 동구 예시로 9", "planned_date": mon})["warnings"]
    ok(any("한도" in x and "2건" in x for x in w), "전체 한도 넘는 예정일 → 경고 (등록은 됨)")
    w2 = S.create_request(con, {**base, "phone": "01000000110", "name": "가상휴무", "address": "가상시 동구 시험길 1", "planned_date": "2026-11-10"})["warnings"]
    ok(any("휴무일" in x for x in w2), "휴무일 예정일 → 경고")
    cal = A.calendar(con, mon, 5, "가상1동")
    ok([d["date"] for d in cal] == ["2026-11-09", "2026-11-11", "2026-11-13", "2026-11-16", "2026-11-18"] and cal[0]["booked"] == 3 and cal[0]["over"]
       and cal[0]["dong_booked"] == 3, "예약 달력: 가상1동 수거 요일 업무일 5개, 날짜별 예약/한도")
    ok(A.earliest_available(con, mon, "가상1동") == "2026-11-11" and A.earliest_available(con, mon) == "2026-11-11", "가장 빠른 가능일: 꽉 찬 날·휴무일 건너뜀")
    A.save_dong(con, {**A.get_dong(con, "가상1동"), "daily_limit": 1})
    S.create_request(con, {**base, "phone": "01000000111", "name": "가상동한도", "address": "가상시 동구 예시로 8", "planned_date": "2026-11-11"})
    ok(A.earliest_available(con, mon, "가상1동") == "2026-11-13" and A.earliest_available(con, mon, "가상2동") == "2026-11-12", "동 한도가 찬 날은 그 동만 건너뜀")
    with S.tx(con):
        A.save_capacity(con, {"daily_count": None, "closed_weekdays": [5, 6]})

    # 마감 이월 (B1)
    day = "2026-11-12"  # 목
    t = {}
    for k, addr in (("un", "가상시 동구 시험길 3"), ("as", "가상시 동구 시험길 4"), ("done", "가상시 동구 시험길 5"), ("nc", "가상시 동구 시험길 6")):
        t[k] = S.create_request(con, {**base, "phone": "0100000020" + str(len(t)), "name": "가상이월" + k, "address": addr, "planned_date": day, "lat": 35.15, "lon": 126.92})["id"]
    S.confirm_plan(con, day, {c1: [{"request_id": t["as"]}, {"request_id": t["done"]}, {"request_id": t["nc"]}]})
    with S.tx(con):  # 현장 결과 흉내: 완료 1, 미수거(재방문 약속 없음) 1
        for rid_, st_ in ((t["done"], "DONE"), (t["nc"], "NOT_COLLECTED")):
            con.execute("UPDATE request SET status=? WHERE id=?", (st_, rid_))
            con.execute("DELETE FROM active_assignment WHERE request_id=?", (rid_,))
    A.add_holiday(con, "2026-11-13", "가상 휴무2")
    sm = S.day_summary(con, day)
    carry = {c["id"]: c for c in sm["carry"]}
    ok(set(carry) == {t["un"], t["as"], t["nc"]} and all(c["to"] == "2026-11-17" for c in carry.values())
       and carry[t["as"]]["kind"] == "결과 없음" and carry[t["un"]]["kind"] == "미배정",
       "마감 화면 이월 대상: 미배정·결과 없음·재방문 약속 없는 미수거 → 다음 업무일(휴무일·주말·가상2동 화목 → 11/17)")
    S.ACTOR.set("kim")
    from dgpc import close as CL  # 마감은 지난 날만: 시험 날짜(11-12)를 지난 것으로 (close 모듈의 '오늘'만 바꿈)
    _today = CL.today_kst; CL.today_kst = lambda: "2026-12-31"
    cl = S.close_day(con, day, "가상 마감")
    CL.today_kst = _today
    g_as = S.get_request(con, t["as"])
    ok(len(cl["carried"]) == 3 and g_as["planned_date"] == "2026-11-17" and g_as["status"] == "RECEIVED" and g_as["versions"][0]["reason"].startswith("마감 이월")
       and not S.one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (t["as"],)) and S.get_request(con, t["done"])["planned_date"] == day,
       "마감 기록 → 미처리 3건 예정일 이월(버전 사유 '마감 이월'), 결과 없는 배정은 풀림, 완료 건은 그대로")
    ok(S.list_audit(con, entity="request", action="CARRY") and S.close_info(con, day)["status"] == "RECORDED" and S.close_info(con, day)["closed_by"] == "kim",
       "이월 건별 이력(CARRY), 마감 1회차 = 담당자 기록")
    # 팀장 확인(2단계)
    raises(lambda: S.approve_close(con, day), "기록한 사람", "마감을 기록한 사람은 확인 불가")
    S.ACTOR.set("boss")
    S.approve_close(con, day, "확인")
    ok(S.close_info(con, day)["status"] == "APPROVED" and S.close_info(con, day)["approved_by"] == "boss", "팀장(검토자)이 마감 확인")
    raises(lambda: S.approve_close(con, day), "이미 확인", "같은 회차 두 번 확인 거부")
    raises(lambda: S.approve_close(con, "2026-11-20"), "아직 마감", "마감 기록 없는 날 확인 거부")
    # 마감 후 수정
    S.ACTOR.set("kim")
    gd = S.get_request(con, t["done"])
    ok(gd["closed"] and gd["closed"]["status"] == "APPROVED", "접수 상세에 '마감한 날' 표시")
    raises(lambda: S.update_request(con, t["done"], {**base, "name": "가상이월done", "address": "가상시 동구 시험길 5", "planned_date": day,
                                                       "items": [{"line_id": gd["items"][0]["line_id"], "fee_rate_id": chair, "qty": 1}]}, ""), "마감한 날", "마감한 날 접수 수정은 사유 필수")
    upd = S.update_request(con, t["done"], {**base, "req_ver": RV(con, t["done"]), "name": "가상이월done", "address": "가상시 동구 시험길 5, 2층", "planned_date": day,
                                            "items": [{"line_id": gd["items"][0]["line_id"], "fee_rate_id": chair, "qty": 1}]}, "주소 보완(가상)")
    raises(lambda: S.add_payment(con, t["done"], {"amount": 2000, "method": "현장 납부", "paid_on": day}), "사유", "마감한 날 수납 기록은 사유 필수")
    S.add_payment(con, t["done"], {"amount": 2000, "method": "현장 납부", "paid_on": day, "reason": "늦게 확인한 납부(가상)"})
    edits = S.day_summary(con, day)["post_close_edits"]
    ok(upd["after_close"] and len(edits) == 2 and {e["entity"] for e in edits} == {"request", "payment"} and edits[0]["receipt_no"],
       "마감 후 수정 2건(접수 수정·수납)이 '마감 후 수정'으로 표시·마감 화면 목록에")
    # 다시 마감: 회차가 쌓임
    _today = CL.today_kst; CL.today_kst = lambda: "2026-12-31"  # 마감은 지난 날만
    S.close_day(con, day, "다시 마감")
    CL.today_kst = _today
    hist = S.day_summary(con, day)["closes"]
    ok([h["seq"] for h in hist] == [1, 2] and hist[0]["status"] == "APPROVED" and hist[1]["status"] == "RECORDED" and hist[0]["carried"] == 3 and hist[1]["carried"] == 0,
       "다시 마감 → 새 회차(덮어쓰지 않음), 새 회차는 다시 팀장 확인 필요")

    # 납부 뒤 취소 → 환불 필요 (B4)
    rp = S.create_request(con, {**base, "phone": "01000000301", "name": "가상환불", "address": "가상시 동구 예시로 30", "planned_date": "2026-11-18"})["id"]
    S.add_payment(con, rp, {"amount": 2000, "method": "무통장 입금", "paid_on": "2026-11-03"})
    raises(lambda: S.cancel_request(con, rp, "", "OTHER"), "직접", "기타 사유는 글 필수")
    raises(lambda: S.cancel_request(con, rp, "x", "NOPE"), "코드", "없는 사유 코드 거부")
    cr = S.cancel_request(con, rp, "", "RESIDENT")
    g = S.get_request(con, rp)
    ok(cr["refund_needed"] and cr["paid"] == 2000 and g["payment"]["status"] == "REFUND_NEEDED" and g["cancel_code"] == "RESIDENT" and g["cancel_label"] == "주민 취소",
       "납부 2,000원 뒤 취소 → 환불 필요 경고, 사유 코드 '주민 취소' 저장")
    ok(any(x["id"] == rp and x["status"] == "REFUND_NEEDED" for x in S.day_summary(con, "2026-11-18")["payments"]), "환불 필요 건이 마감 수납 확인 목록에")
    ok(S.search_requests(con, {"status": "", "quick": "REFUND"})["total"] == 1, "'환불 필요' 빠른 조회")
    raises(lambda: S.add_payment(con, rp, {"kind": "REFUND", "amount": 2000, "method": "무통장 입금", "paid_on": "2026-11-04"}), "결재", "환불은 수납 기록으로 바로 못 넣음(결재선 우회 막음)")
    from dgpc import finance as FN
    case = S.one(con, "SELECT id FROM refund_case WHERE request_id=? AND status='DRAFT'", (rp,))["id"]
    FN.request_refund(con, {"case_id": case})
    _act = S.ACTOR.get(); S.ACTOR.set("checker-b"); FN.approve_refund(con, case, True); S.ACTOR.set(_act)
    FN.pay_refund(con, case, {"method": "계좌 이체", "account_checked": True, "paid_on": "2026-11-04"})
    ok(S.payment_status(con, rp)["status"] == "NONE" and S.search_requests(con, {"quick": "REFUND"})["total"] == 0, "환불 신청·결재·지급하면 목록에서 빠짐")

    # 일괄 처리
    bl = [S.create_request(con, {**base, "phone": f"0100000040{i}", "name": f"가상일괄{i}", "address": f"가상시 동구 샘플로 {i}", "planned_date": "2026-11-18",
                                 "lat": 35.15, "lon": 126.92})["id"] for i in range(3)]
    S.confirm_plan(con, "2026-11-18", {c1: [{"request_id": bl[0]}]})
    raises(lambda: S.reschedule_requests(con, bl, "2026-11-19"), "사유", "예정일 일괄 변경은 사유 필수")
    raises(lambda: S.reschedule_requests(con, bl + [t["done"]], "2026-11-19", "RESIDENT"), "수거 완료", "완료 건이 섞이면 전체 거부")
    ok(all(S.get_request(con, i)["planned_date"] == "2026-11-18" for i in bl), "한 트랜잭션: 실패하면 아무것도 바뀌지 않음")
    rs = S.reschedule_requests(con, bl, "2026-11-19", "RESIDENT", "단지 요청(가상)")
    g0 = S.get_request(con, bl[0])
    ok(rs["changed"] == 3 and g0["planned_date"] == "2026-11-19" and g0["status"] == "RECEIVED" and g0["versions"][0]["reason"].startswith("예정일 변경 · 주민 요청")
       and len(S.list_audit(con, entity="request", action="RESCHEDULE")) == 3, "예정일 일괄 변경 3건: 건별 버전·이력, 다른 날 배정은 풀림")
    sd = S.set_requests_dong(con, bl[:2], "가상2동")
    ok(sd["changed"] == 2 and S.get_request(con, bl[0])["dong"] == "가상2동", "동 일괄 지정")
    csv_np = S.export_requests(con, bl).decode("utf-8-sig")
    csv_p = S.export_requests(con, bl, include_pii=True).decode("utf-8-sig")
    ok("가상일괄0" not in csv_np and "01000000400" not in csv_np and "행정동" in csv_np.splitlines()[0] and "가상일괄0" in csv_p and "샘플로" in csv_p
       and len([a for a in S.list_audit(con, entity="request", action="EXPORT") if '"pii": true' in a["detail"]]) == 3,
       "고른 접수 CSV: 기본은 개인정보 없음, 개인정보 포함은 건마다 내보낸 기록")
    pr = S.print_requests(con, bl)
    ok(len(pr["rows"]) == 3 and S.list_audit(con, entity="request", action="PRINT"), "고른 접수 목록 인쇄 자료 + 건별 출력 기록")
    bc = S.cancel_requests(con, bl[1:], "DUPLICATE")
    ok(bc["cancelled"] == 2 and S.get_request(con, bl[1])["cancel_code"] == "DUPLICATE", "일괄 취소(사유 코드 '중복 접수')")
    ok(S.cancel_requests(con, bl[1:], "DUPLICATE")["skipped"] == 2, "이미 취소된 건은 건너뜀")
    ok(SV.allowed("INTAKE", "POST", "requests/bulk") and SV.allowed("INTAKE", "POST", "requests/export") and not SV.allowed("FIELD", "POST", "requests/export")
       and not SV.allowed("LEAD", "POST", "requests/export"), "일괄 처리·내보내기: 접수 담당자·관리자만")

    # 접수 일괄 등록 (CSV)
    tpl = S.import_template().decode("utf-8-sig")
    ok(tpl.startswith("이름,연락처,배출 주소") and "품목5" in tpl, "일괄 등록 서식 내려받기")
    head = tpl.splitlines()[0]
    lines_ = [head,
              "가상등록1,010-0000-0501,가상시 동구 모의로 1,,2026-11-19,,단체(공동주택),,의자,고정식 의자,2,,,,,,,,,,,,",
              "가상등록2,123,가상시 동구 모의로 2,,2026-11-19,,,,의자,고정식 의자,1,,,,,,,,,,,,",
              "가상등록3,010-0000-0503,가상시 동구 모의로 3,,2026-11-19,없는동,,,의자,고정식 의자,x,,,,,,,,,,,,",
              "가상등록4,010-0000-0501,가상시 동구 모의로 4,,,가상3동,전화,,안마의자,,1,,,,,,,,,,,,",
              "가상등록5,010-0000-0505,가상시 동구 예시로 1,,,,,,의자,고정식 의자,1,,,,,,,,,,,,"]
    raw = "\n".join(lines_).encode("cp949")
    pv = S.import_requests(con, raw, preview=True)
    L = {x["line"]: x for x in pv["lines"]}
    ok(pv["rows"] == 5 and pv["errors"] == 2 and L[3]["errors"] and any("연락처" in e for e in L[3]["errors"]) and any("없는동" in e for e in L[4]["errors"])
       and any("수량" in e for e in L[4]["errors"]), "미리보기: 연락처·없는 동·수량 오류 표시 (저장 안 함)")
    ok("파일 2행" in L[5]["duplicates"] and L[6]["duplicates"] and L[2]["summary"]["dong"] == "가상3동" and any("금액 확인 대기" in w_ for w_ in L[5]["warnings"]),
       "중복 후보: 파일 안 같은 연락처·기존 접수 같은 주소, 동 자동 판별, 표에 없는 품목은 금액 확인 대기")
    n0 = S.one(con, "SELECT COUNT(*) AS n FROM request")["n"]
    im = S.import_requests(con, raw, preview=False)
    ok(im["created"] == 3 and im["skipped_lines"] == [3, 4] and S.one(con, "SELECT COUNT(*) AS n FROM request")["n"] == n0 + 3
       and S.search_requests(con, {"channel": "단체(공동주택)", "status": ""})["total"] == 2 and S.list_audit(con, entity="request_import")[0]["action"] == "IMPORT",
       "등록: 오류 행은 건너뛰고 3건 등록, 접수 경로·일괄 등록 이력")
    ok(S.import_requests(con, raw, preview=False, skip_duplicates=True)["created"] == 0, "중복 후보 건너뛰기 선택 시 이미 들어간 행은 다시 안 들어감")

    # 접수 목록: 서버 쪽 쪽 나눔·전체 건수·정렬·기간·팀·동·수납 상태
    for i in range(55):
        S.create_request(con, {**base, "phone": f"010000{6000 + i:05d}", "name": f"가상쪽{i:02d}", "address": f"가상시 동구 모의로 {100 + i}", "planned_date": "2026-11-24"})
    pg = S.search_requests(con, {"date": "2026-11-24", "size": 20, "page": 3, "sort": "name", "dir": "desc"})
    ok(pg["total"] == 55 and pg["pages"] == 3 and len(pg["rows"]) == 15 and pg["rows"][0]["name"] == "가상쪽14" and pg["rows"][-1]["name"] == "가상쪽00",
       "쪽 나눔: 55건 중 3쪽 15건, 이름 내림차순 (500건 상한 없음)")
    ok(S.search_requests(con, {"date_from": "2026-11-18", "date_to": "2026-11-19", "status": ""})["total"] >= 5
       and S.search_requests(con, {"date_from": "2026-11-24", "date_to": "2026-11-24"})["total"] == 55, "예정일 기간으로 찾기")
    ok(S.search_requests(con, {"date_field": "received", "date_from": S.today_kst(), "date_to": S.today_kst(), "status": ""})["total"] >= 55, "접수일 기간으로 찾기")
    ok(S.search_requests(con, {"dong": "가상3동", "date": "2026-11-24"})["total"] == 55 and S.search_requests(con, {"dong": "-", "status": ""})["total"] >= 1, "동으로 찾기·동 미지정")
    ok(S.search_requests(con, {"crew": c1, "status": ""})["total"] == 0 and S.search_requests(con, {"pay": "PAID", "status": ""})["total"] >= 1, "팀·수납 상태로 찾기")
    sc = S.search_requests(con, {"status": ""}, scope_ids={ra["id"]})
    ok(sc["total"] == 1, "수거팀 범위 거르기도 쪽 나눔 안에서")
    raises(lambda: S.search_requests(con, {"date_from": "11/24"}), "날짜", "잘못된 기간 거부")

    # 민원·통화 기록
    raises(lambda: S.add_contact(con, ra["id"], {"kind": "칭찬", "content": "x"}), "유형", "정해진 유형만")
    S.add_contact(con, ra["id"], {"kind": "독촉", "content": "언제 오는지 문의(가상)", "at": "2026-11-03T10:20"})
    gc = S.get_request(con, ra["id"])
    ok(gc["contacts"][0]["kind"] == "독촉" and gc["contacts"][0]["actor"] == "kim" and "언제" not in (S.list_audit(con, entity="contact")[0]["detail"] or ""),
       "민원·통화 기록: 일시·유형·내용·담당자, 이력에는 내용 없음")

    # 접수 시 안내(설정)
    S.save_settings(con, {"intake_notices": [{"keywords": "냉장고, 세탁기", "text": "폐가전은 무상 방문수거 안내(가상)"}]})
    ok(S.settings(con)["intake_notices"][0]["keywords"] == ["냉장고", "세탁기"], "접수 시 안내 설정 (품목 낱말 → 안내문)")
    raises(lambda: S.save_settings(con, {"intake_notices": [{"keywords": "", "text": "x"}]}), "낱말", "안내문에 품목 낱말 필수")

    # 휴대폰 목록·배차표에 동
    bat = S.confirm_plan(con, "2026-11-19", {c2: [{"request_id": bl[0]}]})[0]["batch_id"]
    pl, _ = S.build_daylist(con, bat)
    ok(pl["stops"][0]["dong"] == "가상2동" and S.dispatch_sheet(con, bat)["stops"][0]["dong"] == "가상2동", "휴대폰 목록 묶음·배차표 지점에 'dong'(문자열)")
    comp = P.compact_daylist({**pl, "fee_table": S.fee_table_for_phone(con)}, S.FEE_ROWS)
    ok(comp["s"][0]["i"], "목록 압축 형식은 그대로 동작 (동 칸은 형식이 지원할 때 전달)")

    # 노선: 동 기본 담당 팀(약한 선호)
    pts = [{"id": f"p{i}", "lat": 35.1462 + 0.002 * (i % 3), "lon": 126.9233 + 0.002 * (i // 3), "items": [{"name": "의자", "spec": "", "qty": 1}]} for i in range(6)]
    crews_ = [{"id": "A", "name": "A", "capacity_m3": 8, "shift_start": "09:00", "shift_end": "17:00"}, {"id": "B", "name": "B", "capacity_m3": 8, "shift_start": "09:00", "shift_end": "17:00"}]
    pref = {"p0": "B", "p1": "A", "p2": "B", "p3": "A", "p4": "B", "p5": "A"}
    rp_ = routing.plan(pts, crews_, {"lat": 35.1462, "lon": 126.9233}, time_limit_s=2, crew_pref=pref, pref_penalty=60)
    got = {s_["request_id"]: rt["crew_id"] for rt in rp_["routes"] for s_ in rt["stops"]}
    ok(len(got) == 6 and all(got[k] == v for k, v in pref.items()) and not any(s_.get("off_pref") for rt in rp_["routes"] for s_ in rt["stops"]),
       "노선 계산: 동 기본 담당 팀 선호 반영 (6곳 모두 담당 팀으로)")
    full = routing.plan(pts, crews_[:1], {"lat": 35.1462, "lon": 126.9233}, time_limit_s=1, crew_pref=pref)
    ok(len(full["routes"][0]["stops"]) == 6, "선호는 약하게: 담당 팀이 없으면 다른 팀이 모두 가져감")

    # 통계: 동별·경로별, CSV에 동·경로
    st = S.stats(con, "2026-11-01", "2026-11-30")
    ok(any(d["dong"] == "가상3동" and d["planned"] >= 55 for d in st["dongs"]) and any(c["channel"] == "단체(공동주택)" for c in st["channels"]), "통계: 동별·접수 경로별 집계")
    hdr = S.stats_csv(con, "2026-11-01", "2026-11-30").decode("utf-8-sig").splitlines()[0]
    ok(hdr.startswith("접수번호,") and "행정동" in hdr and "접수 경로" in hdr, "접수별 CSV에 행정동·접수 경로 열")
    ds = S.day_summary(con, "2026-11-24")
    ok(any(d["dong"] == "가상3동" and d["total"] == 55 for d in ds["dongs"]), "오늘 현황(마감 요약) 동별 건수")

    # 변경 이력: 대상 이름·기간·쪽 나눔·CSV
    ok(set(S.AUDIT_ENTITIES) >= {r["entity"] for r in S.rows(con, "SELECT DISTINCT entity FROM audit_log")}, "이력 대상 목록이 실제 기록 대상을 모두 포함")
    ap = S.audit_page(con, {"entity": "request", "size": 50, "page": 2, "date_from": S.today_kst(), "date_to": S.today_kst()})
    ok(ap["total"] > 50 and len(ap["rows"]) == min(50, ap["total"] - 50) and ap["page"] == 2, f"변경 이력 기간·대상·쪽 나눔 (전체 {ap['total']}건, 300건 상한 없음)")
    ok(S.audit_page(con, {"date_from": "2020-01-01", "date_to": "2020-01-02"})["total"] == 0, "기간 밖은 0건")
    acsv = S.audit_csv(con, {"entity": "request"}).decode("utf-8-sig")
    names_ = [r["name"] for r in S.rows(con, "SELECT name FROM request")]
    ok(acsv.startswith("번호,시각") and acsv.count("\n") > 50 and not any(n_ in acsv for n_ in names_) and "0100000" not in acsv
       and S.list_audit(con, entity="report")[0]["detail"].find('"rows"') > 0, "거른 변경 이력 CSV (개인정보 없음, 내보낸 기록)")

    # 로그인 실패 잠금: 아이디별 (B3)
    SV.FAILS.clear()
    for _ in range(5):
        SV.login_failed("kim")
    ok(SV.login_blocked("kim") > 0 and SV.login_blocked("KIM ") > 0 and SV.login_blocked("boss") == 0, "로그인 5번 실패 → 그 아이디만 잠김 (다른 아이디는 영향 없음)")
    SV.login_failed("kim")
    ok(SV.login_blocked("kim") > 30, "계속 틀리면 잠금 시간 늘어남")
    SV.login_succeeded("kim")
    ok(SV.login_blocked("kim") == 0, "로그인 성공하면 실패 횟수 초기화")
    # 팀장 역할
    ok(SV.allowed("LEAD", "GET", "audit") and SV.allowed("LEAD", "GET", "audit.csv") and SV.allowed("LEAD", "GET", "requests") and SV.allowed("LEAD", "POST", "close/approve")
       and not SV.allowed("LEAD", "POST", "close") and not SV.allowed("LEAD", "POST", "users") and not SV.allowed("LEAD", "POST", "settings")
       and not SV.allowed("LEAD", "POST", "requests") and not SV.allowed("LEAD", "GET", "backups"), "팀장: 전체 조회·이력·마감 확인, 사용자·설정·업무 변경 불가")
    ok(not SV.allowed("INTAKE", "POST", "close/approve") and SV.allowed("INTAKE", "POST", "close") and not SV.allowed("INTAKE", "POST", "dongs")
       and not SV.allowed("INTAKE", "POST", "holidays") and SV.allowed("INTAKE", "GET", "calendar"), "접수 담당자: 마감 기록 가능·확인 불가, 동·휴무일 관리 불가, 달력 조회 가능")
    raises(lambda: S.set_user_crew(con, S.one(con, "SELECT id FROM app_user WHERE username='boss'")["id"], c1), "수거팀 계정만", "팀장 계정에는 팀 연결 안 함")
    con.close()
    os.environ["DG_STATE"] = old_state
    print(f"\n통과 {passed}개 (v0.10 포함)")


def v11():
    """v0.11: 개인정보 파기·접속기록(해시 사슬)·감면·부과번호·수납·세입·대사·환불·출력·자동 백업·주소 찾기·통계·휴대폰 새 기록·로그·LAN 모드."""
    print("\n— v0.11 —")
    import csv as _csv
    import io as _io
    import json as _json
    from dgpc import backup as BK, finance as FN, privacy as PV, server as SV, geo
    # 이전(v0.10) DB 올리기: 새 열 없음, 변경 이력은 사슬 없음 → '이전 기록' 표시, 부과번호 채움
    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-v10db-")
    import sqlcipher3
    oc = sqlcipher3.connect(str(Path(os.environ["DG_STATE"]) / "pc.db"))
    oc.execute("PRAGMA key = 'test-db-key-not-for-production'")
    old_schema = store.SCHEMA.split("-- 개인정보 파기 대장")[0]
    oc.executescript(old_schema)
    for col, decl in (("dong", "TEXT"), ("channel", "TEXT NOT NULL DEFAULT '전화'"), ("cancel_code", "TEXT"), ("cancelled_at", "TEXT")):
        oc.execute(f"ALTER TABLE request ADD COLUMN {col} {decl}")
    oc.execute("ALTER TABLE audit_log ADD COLUMN actor TEXT")
    oc.execute("INSERT INTO audit_log (entity, entity_id, action, detail, at, actor) VALUES ('request','x','CREATE',NULL,'2026-10-01T00:00:00Z','old')")
    oc.execute("""INSERT INTO request (id, receipt_no, name, phone, address, status, received_at, updated_at) VALUES
        ('r-old','DG-20261001-0001','옛신청','01000009999','가상시 동구 옛길 1','DONE','2026-10-01T00:00:00Z','2026-10-02T00:00:00Z')""")
    oc.commit(); oc.close()
    mc = store.connect()
    r_old = S.one(mc, "SELECT * FROM request WHERE id='r-old'")
    ok(r_old["levy_no"] == "2026-00-00001" and r_old["completed_at"] == "2026-10-02T00:00:00Z" and S.one(mc, "SELECT legacy FROM audit_log")["legacy"] == 1
       and {"reduction_code", "levy_no", "pii_purged_at"} <= store._cols(mc, "request") and {"receipt_ref", "source"} <= store._cols(mc, "payment"),
       "v0.10 DB 올리기: 감면·부과번호·완료일 열, 기존 접수에 부과번호, 이전 변경 이력은 '이전 기록'")
    mc.close(); store.connect().close()
    ok(True, "v0.11 이전(migrate) 반복 안전")

    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-v11-")
    con = store.connect(create=True)
    S.ensure_pc_keys(con)
    S.ACTOR.set("admin")
    for u, n_, r_, pw in (("admin", "관리자", "ADMIN", "admin-pass-001"), ("kim", "김접수", "INTAKE", "intake-pass-01"), ("boss", "가상 팀장", "LEAD", "lead-pass-0001")):
        S.create_user(con, u, n_, r_, pw)
    fee = {r["name"] + "|" + r["spec"]: r["id"] for r in S.FEE_TABLE["rows"]}
    chair, wardrobe = fee["의자|고정식 의자"], fee["장롱|120cm 이상 1쪽당"]
    from dgpc import area as A
    d1 = A.save_dong(con, {"name": "가상1동", "code": "11", "keywords": "예시로"})
    raises(lambda: A.save_dong(con, {"id": d1, "name": "가상1동", "code": "1-1"}), "동 코드", "동 코드 형식 검사")
    base = {"phone": "01000007001", "address": "가상시 동구 예시로 7", "planned_date": "2026-11-05", "items": [{"fee_rate_id": wardrobe, "qty": 2}]}

    # 부과번호
    ra = S.create_request(con, {**base, "name": "가상감면"})
    rb = S.create_request(con, {**base, "name": "가상둘", "phone": "01000007002", "dong": ""})
    ok(re.fullmatch(r"\d{4}-11-00001", ra["levy_no"]) and rb["levy_no"].endswith("-00-00001"), f"부과번호 연도-동코드-일련번호 ({ra['levy_no']}, 동 없음 {rb['levy_no']})")

    # 감면
    reds = S.settings(con)["reductions"]
    ok(len(reds) == 4 and all(x["note"] == "조례 확인 필요" for x in reds) and reds[0]["rate"] == 100, "감면 사유 기본값 4종 ('조례 확인 필요' 표시)")
    raises(lambda: FN.set_reduction(con, ra["id"], {"reduction_code": "NEAR_POOR", "reduction_proof": "서류 확인", "reduction_checker": "900101-1234567"}), "식별번호", "확인자 칸에 주민번호 거부")
    raises(lambda: FN.set_reduction(con, ra["id"], {"reduction_code": "NEAR_POOR", "reduction_proof": "", "reduction_checker": "김접수"}), "증빙", "증빙 확인 방법 필수")
    ps = FN.set_reduction(con, ra["id"], {"reduction_code": "NEAR_POOR", "reduction_proof": "행정정보 공동이용", "reduction_checker": "김접수"})
    ok(ps["calculated"] == 30000 and ps["reduction"] == 15000 and ps["levied"] == 15000 and ps["reduction_label"] == "차상위계층",
       "감면 50%: 계산 30,000원 → 감면 15,000원 → 부과 15,000원")
    rx = S.create_request(con, {**base, "name": "가상전액", "phone": "01000007003", "reduction_code": "BASIC", "reduction_proof": "서류 확인", "reduction_checker": "김접수"})
    ok(S.payment_status(con, rx["id"])["levied"] == 0 and S.get_request(con, rx["id"])["reduction_proof"] == "서류 확인", "접수 등록 때 감면(기초 100%) → 부과 0원")
    with S.tx(con):
        FN.save_reductions(con, reds + [{"code": "TEST", "label": "가상 감면", "rate": 30, "note": "시험"}])
    raises(lambda: FN.save_reductions(con, [{"code": "A", "label": "x", "rate": 120}]), "1~100", "감면율 범위 검사")

    # 수납 방법·영수번호
    raises(lambda: S.add_payment(con, ra["id"], {"amount": 1000, "method": "외상"}), "납부 방법", "정해진 납부 방법만")
    S.add_payment(con, ra["id"], {"amount": 15000, "method": "가상계좌", "paid_on": "2026-11-04", "receipt_ref": "VA-0001"})
    p_ = S.payment_status(con, ra["id"])
    ok(p_["status"] == "PAID" and p_["records"][0]["receipt_ref"] == "VA-0001" and p_["records"][0]["source"] == "수기", "감면 뒤 부과 금액만큼 납부 → 납부 확인 (영수번호·출처 기록)")
    S.add_payment(con, rb["id"], {"amount": 10000, "method": "현장 카드", "paid_on": "2026-11-04"})
    S.add_payment(con, rb["id"], {"amount": 5000, "method": "현장 현금", "paid_on": "2026-11-05"})

    # 세입 일계표·월계표
    rv = FN.revenue(con, "2026-11-01", "2026-11-30")
    ok(rv["totals"]["pay"] == 30000 and {m["method"] for m in rv["methods"]} == {"가상계좌", "현장 카드", "현장 현금"} and any(x["dong"] == "가상1동" for x in rv["by_dong"]),
       "세입 일계표: 납부 방법별·동별 합계 30,000원")
    rvm = FN.revenue(con, "2026-11-01", "2026-11-30", "month")
    ok(len({x["period"] for x in rvm["by_method"]}) == 1 and rvm["by_method"][0]["period"] == "2026-11", "세입 월계표(월 단위)")
    rcsv = FN.revenue_csv(con, "2026-11-01", "2026-11-30", "day", "detail").decode("utf-8-sig")
    ok(ra["levy_no"] in rcsv and "가상감면" not in rcsv and "010" not in rcsv and rcsv.startswith("납부일,부과번호"), "세입 명세 CSV: 부과번호·방법·금액 (개인정보 없음)")

    # 대사
    rb_ps = S.payment_status(con, rb["id"])
    rc = S.create_request(con, {**base, "name": "가상대사", "phone": "01000007004"})
    rd = S.create_request(con, {**base, "name": "가상다름", "phone": "01000007005"})
    lv = {k: S.one(con, "SELECT levy_no FROM request WHERE id=?", (v["id"],))["levy_no"] for k, v in (("c", rc), ("d", rd))}
    raw = ("부과번호,금액,납부일,영수번호\n" + f"{lv['c']},30000,2026-11-06,R-1\n{lv['d']},20000,20261106,R-2\n2099-99-99999,1000,2026-11-06,R-3\n{lv['c']},30000,2026-11-06,R-4\n"
           f"{rb_ps['levy_no']},1,2026-11-06,R-5\nx,,\n").encode()
    pv = FN.import_recon(con, raw, preview=True)
    st_ = {x["line"]: x["status"] for x in pv["rows"]}
    ok(st_ == {2: "MATCH", 3: "AMOUNT_DIFF", 4: "UNMATCHED", 5: "DUPLICATE", 6: "AMOUNT_DIFF", 7: "BAD"}, f"대사 미리보기: 일치·금액 다름·부과번호 없음·파일 안 중복·형식 오류 {pv['counts']}")
    ok(S.payment_status(con, rc["id"])["paid"] == 0, "미리보기는 기록하지 않음")
    done_ = FN.import_recon(con, raw, preview=False)
    pc_ = S.payment_status(con, rc["id"])
    ok(done_["recorded"] == 1 and pc_["status"] == "PAID" and pc_["records"][0]["source"] == "대사" and pc_["records"][0]["receipt_ref"] == "R-1", "확인 뒤 일치 1건만 '대사' 출처 납부로 기록")
    raises(lambda: FN.import_recon(con, raw, preview=False), "이미 반영", "같은 대사 파일 두 번 반영 거부")
    again = FN.import_recon(con, ("부과번호,금액,납부일,영수번호\n" + f"{lv['c']},30000,2026-11-06,R-1\n").encode(), preview=True)
    ok(again["rows"][0]["status"] == "ALREADY", "다른 파일에 같은 납부 → 이미 반영")
    ok(FN.recon_template().decode("utf-8-sig").startswith("부과번호,금액,납부일"), "대사 서식")

    # 환불: 납부 뒤 취소 → 자동 환불 건 → 신청 → 결재(신청자 아닌 사람) → 지급(계좌번호 저장 안 함)
    S.cancel_request(con, rc["id"], "", "RESIDENT")
    lr = FN.list_refunds(con, "OPEN")
    case = next(x for x in lr["rows"] if x["request_id"] == rc["id"])
    ok(case["status"] == "DRAFT" and case["auto"] == 1 and case["amount"] == 30000 and case["cause"] == "CANCELLED", "납부 뒤 취소 → 환불 건 자동 생성(신청 전) 30,000원")
    S.ACTOR.set("kim")
    FN.request_refund(con, {"case_id": case["id"], "note": "주민 요청"})
    raises(lambda: FN.approve_refund(con, case["id"], True), "신청한 사람", "신청자는 결재 불가")
    ok(not SV.allowed("INTAKE", "POST", f"refunds/{case['id']}/approve") and SV.allowed("LEAD", "POST", f"refunds/{case['id']}/approve")
       and SV.allowed("INTAKE", "POST", f"refunds/{case['id']}/pay") and not SV.allowed("LEAD", "POST", f"refunds/{case['id']}/pay"), "환불 결재는 팀장·관리자만 (서버 권한)")
    S.ACTOR.set("boss")
    raises(lambda: FN.pay_refund(con, case["id"], {"method": "계좌 이체"}), "결재가 끝난", "결재 전 지급 기록 거부")
    FN.approve_refund(con, case["id"], True, "확인")
    S.ACTOR.set("kim")
    raises(lambda: FN.pay_refund(con, case["id"], {"method": "계좌 이체", "paid_on": "2026-11-07"}), "계좌 확인함", "계좌 이체는 '계좌 확인함' 필수")
    raises(lambda: FN.pay_refund(con, case["id"], {"method": "계좌 이체", "account_checked": True, "account_last4": "110-123-456789"}), "끝 4자리", "전체 계좌번호 거부")
    ps2 = FN.pay_refund(con, case["id"], {"method": "계좌 이체", "account_checked": True, "account_last4": "6789", "paid_on": "2026-11-07"})
    cr = S.one(con, "SELECT * FROM refund_case WHERE id=?", (case["id"],))
    ok(ps2["status"] == "NONE" and cr["status"] == "PAID" and cr["account_last4"] == "6789" and "account_no" not in cr, "지급 기록 → 환불 완료, 납부 상태 정리, 계좌는 끝 4자리만")
    # 과납 → 자동 건, 정정하면 해소
    S.ACTOR.set("admin")
    pd_ = S.add_payment(con, rd["id"], {"amount": 40000, "method": "무통장 입금", "paid_on": "2026-11-06"})
    oc_ = [x for x in FN.list_refunds(con)["rows"] if x["request_id"] == rd["id"]]
    ok(pd_["status"] == "OVERPAID_REVIEW" and oc_ and oc_[0]["cause"] == "OVERPAID" and oc_[0]["amount"] == 10000, "과납 10,000원 → 자동 환불 건")
    S.void_payment(con, pd_["records"][-1]["id"], "금액 잘못 입력")
    ok(S.one(con, "SELECT status FROM refund_case WHERE id=?", (oc_[0]["id"],))["status"] == "VOID", "수납 정정으로 과납이 없어지면 자동 건은 '해소'")

    # 휴대폰 새 기록 (ARRIVE·STICKER_CHECK·FEE_NOTICE·SMS/NAV·작업자·사진 촬영 정보·알 수 없는 기록)
    phone = Phone(S.registration_text(con))
    dev = S.register_device(con, phone.code(), "가상폰", confirm=True)["device_id"]
    c1 = S.save_crew(con, {"name": "가상 1팀", "device_id": dev, "capacity_m3": 8})
    rp = S.create_request(con, {**base, "name": "가상현장", "phone": "01000007010", "planned_date": "2026-11-05",
                                 "items": [{"fee_rate_id": chair, "qty": 2}, {"fee_rate_id": wardrobe, "qty": 1}]})
    rq = S.create_request(con, {**base, "name": "가상취소현장", "phone": "01000007011", "planned_date": "2026-11-05"})
    S.confirm_plan(con, "2026-11-05", {c1: [{"request_id": rp["id"]}, {"request_id": rq["id"]}]})
    b = S.make_bundle(con, S.list_batches(con, "2026-11-05")[0]["id"])
    _h, pl, _r = phone.receive(b["frames"])
    sp, sq = pl["stops"]
    S.cancel_request(con, rq["id"], "", "RESIDENT")
    l0, l1 = sp["items"][0]["line_id"], sp["items"][1]["line_id"]
    evs = [phone.ev(sp, "ARRIVE", {"lat": 35.1, "lon": 126.9, "acc": 12, "worker": "김수거"}),
           phone.ev(sp, "STICKER_CHECK", {"line_id": l0, "result": "OK", "worker": "김수거"}),
           phone.ev(sp, "STICKER_CHECK", {"line_id": l1, "result": "MISMATCH", "number": "2611-0042", "decision": "LEAVE", "worker": "김수거"}),
           phone.ev(sp, "ITEM_RESULT", {"line_id": l1, "qty_confirmed": 1, "qty_collected": 0, "reason": "NO_STICKER"}),
           phone.ev(sp, "FEE_NOTICE", {"received": 15000, "total": 26000, "extra": 11000, "pending": 0, "response": "REFUSED", "lines": [l0], "worker": "이수거"}),
           phone.ev(sp, "ITEM_RESULT", {"line_id": l0, "qty_confirmed": 2, "qty_collected": 2, "reason": ""}),
           phone.ev(sp, "SMS_INTENT", {"template": "SOON"}), phone.ev(sp, "SMS_INTENT", {"template": "SOON"}), phone.ev(sp, "NAV_INTENT", {"app": "geo"}),
           phone.ev(sp, "PHOTO_META", {"photo_id": str(uuid.uuid4()), "phase": "AFTER", "sha256": "0" * 64, "bytes": 10, "taken_at": "2026-11-05T01:00:00Z", "lat": 35.1, "lon": 126.9, "acc": 8}),
           phone.ev(sp, "FUTURE_KIND", {"x": 1}),
           phone.ev(sp, "ATTEMPT", {"outcome": "PARTIAL", "worker": "김수거"}),
           phone.ev(sq, "ARRIVE", {}), phone.ev(sq, "SMS_INTENT", {"template": "ABSENT"}), phone.ev(sq, "NAV_INTENT", {"app": "geo"})]
    team_ev = {"event_id": str(uuid.uuid4()), "seq": 999, "request_id": "", "assign_ver": 1, "kind": "CREW_SHIFT", "body": {"worker": "교대"}, "device_ts": "2026-11-05T02:00:00Z"}
    res = S.import_result(con, phone.seal({"schema": "dg.result/1", "work_date": "2026-11-05", "events": evs + [team_ev]}, P.TYPE_RESULT))
    ok(res["conflict"] == 0 and res.get("stored_other") == 1, "새 기록 종류 병합: 취소 건의 도착·문자·길안내는 충돌 아님, 팀 단위 모르는 기록도 보관 (충돌 0)")
    g = S.get_request(con, rp["id"])
    fx = g["state"]["field"]
    ok(fx["arrive"]["acc"] == 12 and fx["stickers"][l1]["result"] == "MISMATCH" and fx["fee_notices"][0]["response"] == "REFUSED" and fx["sms"] == {"SOON": 2}
       and fx["nav"] == 1 and fx["workers"] == ["김수거", "이수거"] and fx["other_kinds"] == {"FUTURE_KIND": 1} and g["status"] == "PARTIAL",
       "접수 상세 현장 기록: 도착(정확도)·스티커 불일치·고지 거부·문자 2·길안내 1·작업자·알 수 없는 기록 수")
    ok(g["photos"][0]["taken_at"] == "2026-11-05T01:00:00Z" and g["photos"][0]["has_gps"] and g["photos"][0]["acc"] == 8, "사진 촬영 시각·위치 정확도 저장")
    ok(S.one(con, "SELECT COUNT(*) AS n FROM result_event WHERE kind='FUTURE_KIND'")["n"] == 1, "모르는 기록 종류도 저장")
    ds = S.day_summary(con, "2026-11-05")
    ok([x["receipt_no"] for x in ds["sticker_issues"]] == [rp["receipt_no"]] and ds["sticker_issues"][0]["left"] == 1 and ds["fee_refused"][0]["extra"] == 11000,
       "마감 확인: 스티커 불일치 건·추가 금액 고지 거부 건")
    # 일부 수거 차액 환불 후보
    S.add_payment(con, rp["id"], {"amount": 18000, "method": "현장 카드", "paid_on": "2026-11-05"})
    cand = [x for x in FN.partial_candidates(con) if x["request_id"] == rp["id"]]
    cp = S.FEE_ROWS[chair]["price"]
    ok(cand and cand[0]["due"] == 18000 - 2 * cp, f"일부 수거 차액 환불 후보: 실은 수량(의자 2개 {2 * cp:,}원) 기준 → 차액 {cand[0]['due'] if cand else '-'}원")
    rid_ = FN.request_refund(con, {"request_id": rp["id"], "cause": "PARTIAL", "amount": cand[0]["due"], "note": "재방문 안 함(주민)"})
    ok(S.one(con, "SELECT status FROM refund_case WHERE id=?", (rid_,))["status"] == "REQUESTED" and not [x for x in FN.partial_candidates(con) if x["request_id"] == rp["id"]],
       "차액 환불 신청 → 결재 대기, 후보에서 빠짐")

    # 통계 확장
    st = S.stats(con, "2026-11-01", "2026-11-30")
    ok(st["totals"]["reduction_count"] >= 2 and st["categories"] and st["months"][0]["month"] == "2026-11" and st["revenue"]["totals"]["pay"] > 0
       and {x["category"] for x in st["categories"]} >= {"가구류"} if any(x["category"] == "가구류" for x in st["categories"]) else st["categories"], "통계: 감면 건수·품목 분류·월별·세입")
    st_r = S.stats(con, S.today_kst(), S.today_kst(), "received")
    st_c = S.stats(con, S.today_kst(), S.today_kst(), "completed")
    rp_done = S.one(con, "SELECT completed_at FROM request WHERE id=?", (rp["id"],))["completed_at"]
    rp_ts = S.one(con, "SELECT device_ts FROM result_event WHERE request_id=? AND kind='ATTEMPT' ORDER BY device_ts DESC LIMIT 1", (rp["id"],))["device_ts"]
    ok(st_r["totals"]["requests"] >= 7 and st_c["totals"]["requests"] == 2 and st_r["basis_label"] == "접수일" and rp_done == rp_ts,
       f"통계 기준: 접수일·완료일(완료일 = 휴대폰 최종 결과 시각 {rp_done} → 오늘 끝난 건은 취소 2건)")
    raises(lambda: S.stats(con, "2026-11-01", "2026-11-02", "x"), "기준", "없는 집계 기준 거부")
    for i in range(25):
        S.create_request(con, {**base, "name": f"가상품목{i}", "phone": f"010000072{i:02d}", "items": [{"name": f"가상 품목 {i}", "qty": 1}]})
    ok(len(S.stats(con, "2026-11-01", "2026-11-30")["items"]) > 15, "많이 나온 품목: 15개 제한 없이 전체")
    cs = list(_csv.reader(_io.StringIO(S.stats_csv(con, "2026-11-01", "2026-11-30").decode("utf-8-sig"))))
    hd = cs[0]
    row = next(r for r in cs[1:] if r[0] == ra["receipt_no"])
    ok(row[hd.index("상태")] == "접수" and row[hd.index("상태 코드")] == "RECEIVED" and row[hd.index("수납 상태")] == "납부 확인" and row[hd.index("수납 상태 코드")] == "PAID"
       and row[hd.index("감면액(원)")] == "15000" and row[hd.index("부과 금액(원)")] == "15000" and row[hd.index("부과번호")] == ra["levy_no"],
       "접수별 CSV: 상태 한글+코드, 감면액·부과 금액·부과번호")
    for k in ("dongs", "items", "categories", "months", "reductions", "channels"):
        assert S.stats_csv(con, "2026-11-01", "2026-11-30", k).decode("utf-8-sig").count("\n") >= 1
    ok(True, "통계 CSV 6종(동·품목·분류·월·감면·경로)")

    # 접속기록·해시 사슬·무결성 검사
    S.ACTOR.set("kim")
    PV.record_access(con, "VIEW", ra["id"], 1, "접수 상세")
    PV.record_access(con, "LIST", "", 20, PV.filter_summary({"q": "가상감면", "status": "OPEN"}))
    acc = PV.access_page(con, {})
    ok(acc["total"] == 2 and acc["rows"][0]["filter"] == "status=OPEN&q=(검색어 있음)" and "가상감면" not in _json.dumps(acc["rows"], ensure_ascii=False)
       and acc["rows"][1]["receipt_no"] == ra["receipt_no"] and acc["rows"][1]["actor"] == "kim", "접속기록: 상세·목록(건수·조건만, 검색어 값은 남기지 않음)")
    ok(PV.verify_access(con)["ok"] and PV.verify_audit(con)["ok"], "접속기록·변경 이력 해시 사슬 검사 정상")
    mid = S.one(con, "SELECT id FROM access_log ORDER BY id LIMIT 1 OFFSET 1")["id"]
    con.execute("UPDATE access_log SET actor='someone' WHERE id=?", (mid,))
    bad = PV.verify_access(con)
    ok(not bad["ok"] and bad["broken_id"] == mid, f"접속기록 한 행을 고치면 그 행에서 검사 실패 (#{mid})")
    con.execute("UPDATE access_log SET actor='kim' WHERE id=?", (mid,))
    aid = S.one(con, "SELECT id FROM audit_log WHERE legacy=0 ORDER BY id LIMIT 1 OFFSET 3")["id"]
    saved = S.one(con, "SELECT * FROM audit_log WHERE id=?", (aid,))
    con.execute("DELETE FROM audit_log WHERE id=?", (aid,))
    ok(not PV.verify_audit(con)["ok"], "변경 이력 한 행을 지우면 검사 실패")
    con.execute("INSERT INTO audit_log (id, entity, entity_id, action, detail, at, actor, prev_hash, hash, legacy) VALUES (?,?,?,?,?,?,?,?,?,?)",
                tuple(saved[k] for k in ("id", "entity", "entity_id", "action", "detail", "at", "actor", "prev_hash", "hash", "legacy")))
    ok(PV.verify_all(con)["ok"], "되돌리면 다시 정상")
    # 월 점검
    S.ACTOR.set("boss")
    ok(PV.check_status(con)["done"] is None, "이번 달 접속기록 점검 전")
    PV.record_check(con, "가상 점검")
    ok(PV.check_status(con)["done"]["result"]["ok"] and PV.list_checks(con)[0]["ok"], "점검 완료 기록(무결성 검사 결과 함께)")
    last_id = S.one(con, "SELECT MAX(id) AS n FROM access_log")["n"]
    con.execute("DELETE FROM access_log WHERE id=?", (last_id,))
    ok(not PV.verify_all(con)["ok"], "지난 점검 때의 마지막 행을 지우면(끝 지우기) 검사 실패")
    con.execute("DELETE FROM access_check")
    for _ in range(3):
        PV.record_access(con, "LIST", "", 5, "")
    # 보관 기간(2년) 지난 접속기록 정리: 시작점(anchor)으로 사슬 유지
    con.execute("UPDATE access_log SET at='2020-01-01T00:00:00Z' WHERE id IN (SELECT id FROM access_log ORDER BY id LIMIT 2)")
    # 시각을 바꾸면 해시가 달라지므로 다시 사슬을 만들어 둔다(시험용)
    prev = ""
    for r in S.rows(con, "SELECT * FROM access_log ORDER BY id"):
        h = S.chain_hash(prev, PV._access_fields(r))
        con.execute("UPDATE access_log SET prev_hash=?, hash=? WHERE id=?", (prev, h, r["id"]))
        prev = h
    n_old = PV.purge_old_access(con)
    PV.record_access(con, "VIEW", ra["id"], 1, "")
    ok(n_old == 2 and PV.verify_access(con)["ok"] and S.get_setting(con, "access_chain_anchor")["id"] >= 2, "2년 지난 접속기록 정리 뒤에도 사슬 검사 정상(시작점 보관)")
    raises(lambda: PV.save_settings(con, {"access_log_retention_years": 1}), "2년 이상", "접속기록 보관 2년 미만 거부")
    acsv = PV.access_csv(con, {}).decode("utf-8-sig")
    ok(acsv.startswith("번호,시각") and ra["receipt_no"] in acsv, "접속기록 CSV(대상은 접수번호)")

    # 개인정보 보유기간·파기
    S.ACTOR.set("admin")
    S.add_contact(con, ra["id"], {"kind": "문의", "content": "가상감면 010-0000-7001 문의"})
    S.update_request(con, ra["id"], {**base, "req_ver": RV(con, ra["id"]), "name": "가상감면", "memo": "메모 가상감면", "items": [{"line_id": S.get_request(con, ra["id"])["items"][0]["line_id"], "fee_rate_id": wardrobe, "qty": 2}]}, "가상 수정")
    con.execute("UPDATE request SET status='DONE', completed_at='2022-01-10T00:00:00Z' WHERE id=?", (ra["id"],))
    con.execute("UPDATE request SET cancelled_at='2022-02-01T00:00:00Z' WHERE id=?", (rc["id"],))
    pvw = PV.purge_preview(con)
    ok(pvw["count"] == 2 and pvw["retention_years"] == 3, f"파기 미리보기: 보유기간 3년 지난 완료·취소 {pvw['count']}건 (기준일 {pvw['cutoff']})")
    raises(lambda: PV.save_settings(con, {"pii_retention_years": 0}), "1~10년", "보유기간 범위 검사")
    stats_before = S.stats(con, "2026-11-01", "2026-11-30")["totals"]["requests"]
    pr_ = PV.purge_pii(con)
    g = S.get_request(con, ra["id"])
    snap = S.one(con, "SELECT snapshot_json FROM request_version WHERE request_id=?", (ra["id"],))["snapshot_json"]
    ok(pr_["count"] == 2 and g["name"] == "(파기)" and g["address"] == "(파기)" and g["memo"] == "" and g["receipt_no"] == ra["receipt_no"] and g["levy_no"] == ra["levy_no"]
       and g["dong"] == "가상1동" and g["items"] and g["contacts"][0]["content"] == "(파기)" and "가상감면" not in snap and g["pii_purged_at"],
       "파기: 이름·연락처·주소·메모·민원 내용·이전 버전 가명 처리, 접수번호·부과번호·동·품목 유지")
    ok(S.stats(con, "2026-11-01", "2026-11-30")["totals"]["requests"] == stats_before and "VA-0001" in FN.revenue_csv(con, "2026-11-01", "2026-11-30", "day", "detail").decode("utf-8-sig"),
       "파기 뒤에도 통계·세입 명세 그대로")
    lg = PV.purge_log(con)
    ok(lg[0]["count"] == 2 and lg[0]["actor"] == "admin" and lg[0]["criteria"]["retention_years"] == 3 and PV.purge_pii(con)["count"] == 0, "파기 대장(일시·건수·작업자·기준), 다시 해도 0건")

    # 자동 백업: 공개키 암호화·두 번째 위치·세대 보관·복원
    raises(lambda: BK.generate_key(con, "short"), "12자", "백업 개인키 비밀번호 12자 이상")
    ok(not BK.make_auto(con)["ok"], "백업 키가 없으면 자동 백업 안 함(상태에 기록)")
    pem, info = BK.generate_key(con, "backup-key-pass-01")
    ok(b"ENCRYPTED PRIVATE KEY" in pem and "pub" in S.get_setting(con, "backup_pubkey") and "pem" not in _json.dumps(S.get_setting(con, "backup_pubkey")),
       "백업 키: 개인키는 비밀번호로 암호화한 PEM으로만 내보내고 PC에는 공개키만")
    dest2 = Path(tempfile.mkdtemp(prefix="dg-bk2-"))
    with S.tx(con):
        BK.save_settings(con, {"backup_dest2": str(dest2), "backup_time": "00:00"})
    raises(lambda: BK.save_settings(con, {"backup_time": "25:00"}), "HH:MM", "자동 백업 시각 형식")
    ok(BK.due(con), "정한 시각이 지났고 오늘 자동 백업이 없으면 실행 대상")
    class TrackLock:  # 서버 잠금 흉내: 잡은 횟수와 잡은 채로 파일을 다루는지 본다
        def __init__(self): self.n = 0; self.held = False
        def __enter__(self): self.n += 1; self.held = True
        def __exit__(self, *a): self.held = False
    tl = TrackLock()
    real_sha = BK._sha
    held_during_hash = []
    BK._sha = lambda p_: (held_during_hash.append(tl.held), real_sha(p_))[1]
    ab0 = BK.make_auto(con, "TEST-LOCK", lock=tl)
    BK._sha = real_sha
    ok(ab0["ok"] and tl.n == 3 and held_during_hash and not any(held_during_hash),
       "자동 백업: DB 잠금은 설정 읽기·내보내기·결과 기록 때만(3번), 해시·복사 같은 파일 작업은 잠금 밖 → 백업 중 화면이 멈추지 않음")
    ab = BK.make_auto(con, "TEST")
    f_ = S.backup_dir(con) / ab["file"]
    rawb = f_.read_bytes()
    ok(ab["ok"] and rawb[:5] == b"DGAB1" and b"SQLite format 3" not in rawb[:200] and "가상".encode() not in rawb and ab["dest2"]["ok"] and (dest2 / ab["file"]).exists(),
       "자동 백업(.dgabk): 암호화됨, 두 번째 위치 복사·해시 대조")
    ok(not BK.due(con) and BK.status(con)["stale"] is False, "자동 백업 뒤 오늘은 다시 실행 안 함, 최근 백업 26시간 이내")
    raises(lambda: BK.verify_auto(con, ab["file"], pem, "wrong-pass-000"), "비밀번호", "틀린 개인키 비밀번호 거부")
    ok(BK.verify_auto(con, ab["file"], pem, "backup-key-pass-01")["requests"] > 30, "개인키 + 비밀번호로 자동 백업 열어 확인")
    pem2, _ = BK.generate_key(con, "other-key-pass-02")
    raises(lambda: BK.verify_auto(con, ab["file"], pem2, "other-key-pass-02"), "다른 백업 키", "다른 키로 만든 백업은 그 개인키가 필요")
    now_ = dt.datetime(2026, 11, 4, 18, 0, tzinfo=S.KST)
    fs = [Path(f"donggu-auto-202611{d:02d}-180000.dgabk") for d in range(1, 4)] + [Path(f"donggu-auto-2026{m:02d}01-180000.dgabk") for m in range(6, 11)] \
         + [Path("donggu-auto-20261103-190000.dgabk"), Path("donggu-backup-20260101-090000.dgbak"), Path("donggu-backup-20261020-090000.dgbak")]
    drop = {f.name for f in BK.plan_retention(fs, 2, 3, 90, now_)}
    # 90일 기준(8/6) 이전: 6·7·8월 자동, 1월 수동 → 삭제. 일별 2일(11/3 19시·11/2) + 월별 3개월(9/1·10/1·11/1) 남김 → 11/3 18시 삭제
    ok(drop == {"donggu-auto-20261103-180000.dgabk", "donggu-auto-20260601-180000.dgabk", "donggu-auto-20260701-180000.dgabk", "donggu-auto-20260801-180000.dgabk",
                "donggu-backup-20260101-090000.dgbak"}, "백업 보관: 일별 최근 2일·월별 3개월, 90일 지난 백업 삭제 (수동 백업은 기간만)")
    bcount = S.one(con, "SELECT COUNT(*) AS n FROM request")["n"]
    con.close()
    rr = BK.restore_auto(f_, pem, "backup-key-pass-01")
    con = store.connect()
    ok(rr["requests"] == bcount and S.one(con, "SELECT COUNT(*) AS n FROM request")["n"] == bcount, "자동 백업 복원(개인키 파일 + 비밀번호)")

    # 주소 찾기: 도로명·지번·건물명(초성)
    x, y = geo.wgs84_to_utmk(35.15, 126.92)
    araw = ("시군구명,읍면동명,도로명,건물본번,건물부번,건물명,지번본번,지번부번,관할행정동,X좌표,Y좌표\n"
            f"동구,가상법정동,예시로,12,0,가상아파트,345,6,가상1동,{x:.3f},{y:.3f}\n"
            f"동구,가상법정동,시험길,3,1,,77,0,,{x + 30:.3f},{y + 30:.3f}\n").encode()
    pa = S.import_addr_points(con, araw, preview=True)
    S.import_addr_points(con, araw)
    ok(pa["has_bname"] and pa["has_jibun"] and pa["sample"][0]["bname"] == "가상아파트" and pa["sample"][0]["jibun"] == "345-6", "주소 자료 가져오기: 건물명·지번 열 보관")
    h1 = S.addr_search(con, "가상아파트")
    h2 = S.addr_search(con, "ㄱㅅㅇㅍㅌ")
    h3 = S.addr_search(con, "가상법정동 345-6")
    h4 = S.addr_search(con, "예시로 12")
    h5 = S.addr_search(con, "시험길")
    ok(h1 and h1[0]["kind"] == "건물명" and h1[0]["address"] == "동구 예시로 12 (가상법정동, 가상아파트)" and h1[0]["dong"] == "가상1동" and abs(h1[0]["lat"] - 35.15) < 1e-5,
       "건물명 찾기 → 표준 주소·좌표·동")
    ok(h2 and h2[0]["bname"] == "가상아파트" and h3 and h3[0]["kind"] == "지번" and h4 and h4[0]["kind"] == "도로명" and h5 and h5[0]["address"].startswith("동구 시험길 3-1"),
       "초성(ㄱㅅㅇㅍㅌ)·지번(법정동 번지)·도로명·도로명만으로 찾기")
    h6 = S.addr_search(con, "예시")
    ok(any(x.get("hint") and x["road"] == "예시로" and "12~12" in x["label"] for x in h6) and not any(x.get("hint") for x in h4),
       "도로명 앞부분만('예시') → '예시로 — 건물번호를 이어서 적으세요' 제안 (번호까지 적으면 제안 없음)")
    # 이름 없는 위치정보요약DB: 건물명 열 자동 인식
    def aline(road, main, bname, lat, lon):
        xx, yy = geo.wgs84_to_utmk(lat, lon)
        return "|".join(["29110", "1", "2911010100", "가상광역시", "동구", "가상동", "1", road, "0", str(main), "0", bname, "61400", "주택", "0", "가상동", f"{xx:.6f}", f"{yy:.6f}"])
    lines = [aline(f"가상{i}길", i, "가상빌라" if i % 3 == 0 else "", 35.14 + i * 1e-4, 126.92) for i in range(1, 30)]
    cols = geo.detect_columns([l.split("|") for l in lines])
    ok(cols.get("bname") == 11, "머리글 없는 위치정보요약DB에서 건물명 열 찾기")

    # 휴대폰 단독 방식: 개인정보를 지운 접수도 옮기기
    import zlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", b"solo-pass-1", salt, 600000, 32)
    rid_s = str(uuid.uuid4())
    obj = {"format": "dg.backup/1", "created_at": "2026-11-04T00:00:00Z", "state": {"mode": "SOLO", "requests": [
        {"request_id": rid_s, "receipt_no": "DG-20260801-0001", "name": "", "phone": "", "address": "", "pii_purged": "2026-11-01T00:00:00Z", "dong": "가상1동",
         "status": "RECEIVED", "items": [{"line_id": str(uuid.uuid4()), "fee_rate_id": chair, "name": "의자", "spec": "고정식 의자", "qty": 1}], "versions": [{"req_ver": 1, "snapshot": {"name": "옛이름"}}]}], "events": []}, "photos": {}}
    comp = zlib.compressobj(wbits=-15)
    plain = comp.compress(_json.dumps(obj).encode()) + comp.flush()
    dgb = b"DGB1" + salt + iv + AESGCM(key).encrypt(iv, plain, b"DGB1")
    rid_g = str(uuid.uuid4())
    obj["state"]["requests"].append({"request_id": rid_g, "receipt_no": "DG-20260801-0002", "name": "가상위치", "phone": "01000007401", "address": "가상시 동구 예시로 9",
                                      "lat": 35.15, "lon": 126.92, "geo_source": "GPS", "status": "RECEIVED", "items": [{"line_id": str(uuid.uuid4()), "fee_rate_id": chair, "name": "의자", "spec": "고정식 의자", "qty": 1}]})
    comp = zlib.compressobj(wbits=-15)
    plain = comp.compress(_json.dumps(obj).encode()) + comp.flush()
    dgb = b"DGB1" + salt + iv + AESGCM(key).encrypt(iv, plain, b"DGB1")
    sr = S.import_solo(con, dgb, "solo-pass-1")
    ok(S.get_request(con, rid_g)["geo_source"] == "FIELD", "단독 방식 위치 출처 GPS → 현장 위치로 저장")
    gs = S.get_request(con, rid_s)
    ok(sr["requests"] == 2 and gs["name"] == "(정리됨)" and gs["dong"] == "가상1동" and gs["pii_purged_at"] and gs["levy_no"] and "옛이름" not in S.one(con, "SELECT snapshot_json FROM request_version WHERE request_id=?", (rid_s,))["snapshot_json"],
       "단독 방식 백업: 개인정보 지운 접수(빈 이름·연락처·주소)도 가명으로 옮김, 동 받음")

    # 단독 백업의 품목 행 ID가 PC에 이미 있으면(CI에서 실제로 난 UNIQUE 실패) 새 ID로 바꾸고 현장 기록·사진도 따라 바꾼다
    used = S.one(con, "SELECT line_id FROM request_item WHERE request_id=?", (rid_g,))["line_id"]
    rid_c, ev_c = str(uuid.uuid4()), str(uuid.uuid4())
    obj2 = {"format": "dg.backup/1", "created_at": "2026-11-04T00:00:00Z", "state": {"mode": "SOLO", "requests": [
        {"request_id": rid_c, "receipt_no": "DG-20260802-0001", "name": "가상겹침", "phone": "01000007402", "address": "가상시 동구 예시로 10", "status": "RECEIVED",
         "items": [{"line_id": used, "fee_rate_id": chair, "name": "의자", "spec": "고정식 의자", "qty": 2}]}],
        "events": [{"event_id": ev_c, "seq": 1, "request_id": rid_c, "kind": "ITEM_RESULT", "body": {"line_id": used, "line_kind": "RECEIVED", "qty_confirmed": 2, "qty_collected": 2, "reason": ""}, "ts": "2026-11-04T01:00:00Z"}]}, "photos": {}}
    comp = zlib.compressobj(wbits=-15)
    plain = comp.compress(_json.dumps(obj2).encode()) + comp.flush()
    dgb2 = b"DGB1" + salt + iv + AESGCM(key).encrypt(iv, plain, b"DGB1")
    r2 = S.import_solo(con, dgb2, "solo-pass-1")
    new_lid = S.one(con, "SELECT line_id FROM request_item WHERE request_id=?", (rid_c,))["line_id"]
    ev_body = _json.loads(S.one(con, "SELECT body_json FROM result_event WHERE event_id=?", (ev_c,))["body_json"])
    ok(r2["requests"] == 1 and r2.get("lines_renumbered") == 1 and new_lid != used and ev_body["line_id"] == new_lid
       and S.one(con, "SELECT request_id FROM request_item WHERE line_id=?", (used,))["request_id"] == rid_g,
       "단독 백업 품목 행 ID가 PC에 이미 있으면 새 ID로 바꿔 옮김(현장 기록도 따라감, 기존 행은 그대로)")

    # 휴대폰 v0.10.1: 반입지 좌표를 목록에 싣기, 길안내 앱 종류, 단독 방식 위치 출처
    S.save_settings(con, {"unload_site": {"lat": 35.13, "lon": 126.95, "label": "가상 반입지"}})
    ru = S.create_request(con, {**base, "name": "가상반입", "phone": "01000007301", "planned_date": "2026-11-20", "lat": 35.14, "lon": 126.92})
    bu = S.confirm_plan(con, "2026-11-20", {c1: [{"request_id": ru["id"], "eta": "09:00", "unload_after": {"eta": "09:40", "label": "가상 반입지", "minutes": 20, "volume_m3": 2}}]})[0]["batch_id"]
    dl, _b = S.build_daylist(con, bu)
    cu = P.compact_daylist(dl, S.FEE_ROWS)["s"][0]["u"]
    ok(dl["stops"][0]["unload_after"]["lat"] == 35.13 and cu[4:] == [35.13, 126.95] and P.expand_daylist(P.compact_daylist(dl, S.FEE_ROWS), S.FEE_ROWS)["stops"][0]["unload_after"]["lon"] == 126.95,
       "목록 묶음: 반입지 경유에 반입지 좌표(u 5·6번째 값)")
    bu_ = S.make_bundle(con, bu)
    _h2, pl2, _r2 = phone.receive(bu_["frames"])
    su = pl2["stops"][0]
    S.import_result(con, phone.seal({"schema": "dg.result/1", "work_date": "2026-11-20", "events": [phone.ev(su, "NAV_INTENT", {"app": "kakao"}), phone.ev(su, "NAV_INTENT", {"app": "tmap"}),
        phone.ev(su, "LOCATION_FIX", {"lat": 35.141, "lon": 126.921, "source": "ADDR"})]}, P.TYPE_RESULT))
    gu = S.get_request(con, ru["id"])
    ok(gu["state"]["field"]["nav_apps"] == {"kakao": 1, "tmap": 1} and gu["geo_source"] == "ADDR" and abs(gu["lat"] - 35.141) < 1e-9, "길안내 앱 종류별 횟수, 휴대폰 주소 검색 위치(LOCATION_FIX source ADDR) → 출처 '주소 자료'")

    # 로그·LAN 모드
    ok(SV.path_template("/api/requests/0b7c1e7a-1111-2222-3333-444455556666/payments") == "/api/requests/:id/payments" and SV.path_template("/api/registration-qr") == "/api/registration-qr"
       and SV.path_template("/print/doc/Ab3_x9KkLmNo12") == "/print/doc/:id" and SV.path_template("/api/requests?q=가상") == "/api/requests", "로그 경로: ID·토큰 가림, 조건(query) 버림")
    ok(SV.scrub("ValueError: '가상하나 010-1234-5678 가상시 동구 예시로 12-3'") .count("<전화>") == 1 and "예시로 12" not in SV.scrub("가상시 동구 예시로 12-3"), "로그 오류 기록: 전화번호·주소 모양 값 가림")
    lf = SV.setup_logging(Path(os.environ["DG_STATE"]))
    SV.LOG.info("시험 %s", SV.path_template("/api/requests/0b7c1e7a-1111-2222-3333-444455556666"))
    ok(lf.exists() and ":id" in lf.read_text(encoding="utf-8") and any(getattr(h, "maxBytes", 0) == 5 * 1024 * 1024 and h.backupCount == 10 for h in SV.LOG.handlers),
       "서버 로그 state/logs/server.log (5MB × 10개 돌려 쓰기)")
    for args, msg in (((None, None, "10.0.0.0/24"), "HTTPS 전용"), (("c.pem", "k.pem", ""), "IP 대역")):
        try:
            SV.lan_config(*args)
            raise AssertionError("LAN 설정 오류 없음")
        except ValueError as e:
            ok(msg in str(e), f"LAN 모드: 인증서·키·허용 대역 없으면 시작 안 함 → '{e}'")
    certf = Path(os.environ["DG_STATE"]) / "c.pem"; certf.write_text("x")
    try:
        SV.lan_config(str(certf), str(certf), "0.0.0.0/0")
        raise AssertionError("전체 허용 통과")
    except ValueError as e:
        ok("0.0.0.0/0" in str(e), "LAN 모드: 모든 주소 허용 거부")
    lan = SV.lan_config(str(certf), str(certf), "10.0.0.0/24, 192.168.5.7", "dong-pc")
    ok(SV.ip_allowed("10.0.0.15", lan["nets"]) and SV.ip_allowed("192.168.5.7", lan["nets"]) and not SV.ip_allowed("10.0.1.15", lan["nets"])
       and not SV.ip_allowed("192.168.5.8", lan["nets"]) and SV.ip_allowed("127.0.0.1", lan["nets"]) and not SV.ip_allowed("bogus", lan["nets"]) and lan["hostname"] == "dong-pc",
       "LAN 모드 접속 허용 검사: 허용 대역·서버 자신만, 그 밖 IP 거부")
    import subprocess
    out = subprocess.run([sys.executable, str(ROOT / "pc" / "pc_tool.py"), "serve", "--lan", "--allow", "10.0.0.0/24"], capture_output=True, text=True, env=os.environ, timeout=60)
    ok(out.returncode != 0 and "HTTPS 전용" in (out.stderr + out.stdout), "pc_tool serve --lan: 인증서 없이 시작 거부")
    out = subprocess.run([sys.executable, str(ROOT / "pc" / "pc_tool.py"), "purge-pii", "--preview"], capture_output=True, text=True, env=os.environ, timeout=60)
    ok(out.returncode == 0 and "보유기간 3년" in out.stdout, "pc_tool purge-pii --preview")
    ok(set(S.AUDIT_ENTITIES) >= {r["entity"] for r in S.rows(con, "SELECT DISTINCT entity FROM audit_log")}, "이력 대상 목록이 새 기록 대상(파기·접속기록·환불·세입)을 포함")
    con.close()
    print(f"\n통과 {passed}개 (v0.11 포함)")


class FakeResp:
    def __init__(self, body):
        self.body = json.dumps(body, ensure_ascii=False).encode() if not isinstance(body, bytes) else body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def v11_external():
    """v0.11 외부 서비스: PC 서버가 대신 묻는 행안부 주소 API, 카카오모빌리티 이동시간 행렬(가짜 HTTP), 휴대폰 목록 감면·부과번호."""
    print("\n— v0.11 외부 서비스 (네트워크 없이 가짜 HTTP) —")
    import socket
    import urllib.error
    import urllib.parse
    import urllib.request
    from dgpc import area as A, external as X, finance as FN, geo
    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-ext-")
    con = store.connect(create=True)
    S.ensure_pc_keys(con)
    calls: list[dict] = []
    real_urlopen = X.urlopen
    utm = geo.wgs84_to_utmk(35.1462, 126.9233)

    def juso_fake(req, timeout):
        u = urllib.parse.urlsplit(req.full_url)
        q = dict(urllib.parse.parse_qsl(u.query))
        calls.append({"url": req.full_url, "path": u.path, "q": q, "timeout": timeout, "headers": dict(req.header_items()), "body": req.data})
        if q.get("confmKey") == "BADKEY-0000":
            return FakeResp({"results": {"common": {"errorCode": "E0001", "errorMessage": "승인되지 않은 KEY 입니다."}}})
        if u.path.endswith("addrLinkApi.do"):
            return FakeResp({"results": {"common": {"errorCode": "0", "totalCount": "2"}, "juso": [
                {"roadAddr": "광주광역시 동구 서남로 1 (가상동)", "roadAddrPart1": "광주광역시 동구 서남로 1", "jibunAddr": "광주광역시 동구 가상동 1-1", "bdNm": "가상빌딩",
                 "zipNo": "61400", "emdNm": "가상동", "hemdNm": "가상9동", "admCd": "2911010800", "rnMgtSn": "291103166047", "udrtYn": "0", "buldMnnm": "1", "buldSlno": "0",
                 "rn": "서남로", "sggNm": "동구", "siNm": "광주광역시", "lnbrMnnm": "1", "lnbrSlno": "1", "mtYn": "0"},
                {"roadAddr": "광주광역시 동구 서남로 3", "roadAddrPart1": "광주광역시 동구 서남로 3", "jibunAddr": "", "bdNm": "", "zipNo": "61401", "emdNm": "모르는동", "hemdNm": "",
                 "admCd": "2911010800", "rnMgtSn": "291103166047", "udrtYn": "0", "buldMnnm": "3", "buldSlno": "0"}]}})
        return FakeResp({"results": {"common": {"errorCode": "0", "totalCount": "1"}, "juso": [{"entX": f"{utm[0]:.6f}", "entY": f"{utm[1]:.6f}"}]}})

    X.urlopen = juso_fake
    try:
        # 설정: 키는 DB에만, 화면에는 가린 값
        ok(S.juso_search(con, "서남로 1")["configured"] is False and not calls, "승인키 없으면 외부로 묻지 않음 (configured=false)")
        raises(lambda: S.save_settings(con, {"external": {"keys": {"juso_search_key": "짧음"}}}), "형식", "승인키 형식 검사")
        raises(lambda: S.save_settings(con, {"external": {"travel_method": "KAKAO"}}), "REST 키", "카카오 방식은 REST 키가 있어야 고를 수 있음")
        S.save_settings(con, {"external": {"keys": {"juso_search_key": "JUSOSEARCHKEY-123456", "juso_coord_key": "JUSOCOORDKEY-654321"}}})
        st_txt = json.dumps(S.settings(con), ensure_ascii=False)
        ex = S.settings(con)["external"]
        ok("JUSOSEARCHKEY-123456" not in st_txt and "JUSOCOORDKEY" not in st_txt and ex["keys"]["juso_search_key"]["set"] and ex["keys"]["juso_search_key"]["mask"] == "••••3456"
           and not ex["keys"]["kakao_rest_key"]["set"] and ex["travel_method"] == "ESTIMATE", "승인키는 암호화 DB에만 저장, 설정 응답(브라우저)에는 가린 값(••••3456)만")
        ok(not [r for r in S.rows(con, "SELECT detail FROM audit_log") if "JUSOSEARCHKEY" in (r["detail"] or "")], "변경 이력에 키 값 없음 (키 이름만)")
        # 검색: 검색어·승인키·쪽수·json만, 동은 행정동으로
        A.save_dong(con, {"name": "가상9동", "keywords": []})
        res = S.juso_search(con, "서남로 1 <script>", name="가상주민")
        c0 = calls[-1]
        ok(c0["path"] == "/addrlink/addrLinkApi.do" and sorted(c0["q"]) == ["confmKey", "countPerPage", "currentPage", "keyword", "resultType"] and c0["q"]["keyword"] == "서남로 1 script"
           and c0["q"]["resultType"] == "json" and c0["url"].startswith("https://business.juso.go.kr/") and c0["timeout"] == 5 and c0["body"] is None
           and not any(k.lower() in ("cookie", "referer") for k in c0["headers"]), "행안부 검색 요청: business.juso.go.kr, 검색어(특수문자 제거)·승인키·쪽수·json만, 5초 제한, 쿠키·Referer 없음")
        ok(res["items"][0]["kind"] == "행안부" and res["items"][0]["dong"] == "가상9동" and res["items"][0]["address"] == "광주광역시 동구 서남로 1 (가상동, 가상빌딩)"
           and res["items"][1]["dong"] is None and res["coord"], "검색 결과: '행안부' 표시, 행정동(hemdNm) → 동 자동 선택, 모르는 동은 비움")
        acc = S.rows(con, "SELECT * FROM access_log ORDER BY id DESC LIMIT 1")[0]
        ok(acc["action"] == "SEARCH" and acc["count"] == 2 and "서남로" not in json.dumps(acc, ensure_ascii=False) and "행안부" in acc["filter"], "접속기록: 행안부 검색 1줄(건수만, 검색어 없음)")
        co = S.juso_coord(con, res["items"][0]["ref"])
        c1 = calls[-1]
        ok(c1["path"] == "/addrlink/addrCoordApi.do" and sorted(c1["q"]) == ["admCd", "buldMnnm", "buldSlno", "confmKey", "resultType", "rnMgtSn", "udrtYn"]
           and c1["q"]["confmKey"] == "JUSOCOORDKEY-654321" and abs(co["lat"] - 35.1462) < 1e-6 and abs(co["lon"] - 126.9233) < 1e-6,
           f"좌표 요청: 건물 식별 번호·좌표 승인키만, UTM-K → WGS84 ({co['lat']}, {co['lon']})")
        n = len(calls)
        ok(S.juso_coord(con, {**res["items"][0]["ref"], "admCd": "가상하나"})["none"] and len(calls) == n, "건물 식별 번호가 숫자 형식이 아니면 보내지 않음")
        ok(co["source"] == "JUSO" and res["items"][0]["ref"]["rn"] == "서남로" and res["items"][0]["ref"]["lnbrMnnm"] == "1", "이 PC 주소 위치 자료가 없으면 좌표 승인키로 행안부 좌표 API (source JUSO)")

        # 행안부 검색 결과 → 이 PC 주소 위치 자료(위치정보요약DB)로 좌표 채우기 (좌표 API 없이)
        def yline(road, main, sub, lat, lon, rcd="291103166047", under="0", entr="1", bjd="2911010800", sg="동구", bname=""):
            x_, y_ = geo.wgs84_to_utmk(lat, lon)
            return "|".join(["29110", entr, bjd, "광주광역시", sg, "가상동", rcd, road, under, str(main), str(sub), bname, "61400", "주택", "0", "가상9동", f"{x_:.6f}", f"{y_:.6f}"])
        ylines = [yline("서남로", 1, 0, 35.1521, 126.9311, entr="7"), yline("서남로", 1, 0, 35.1501, 126.9301, entr="2"),  # 출입구 둘: 일련번호 작은 것
                  yline("서남로", 1, 0, 35.1491, 126.9291, under="1", entr="3"), yline("서남로", 5, 2, 35.1511, 126.9321)]
        ylines += [yline(f"가상{i}길", i, 0, 35.14 + i * 1e-4, 126.92, rcd=f"2911031{i:05d}") for i in range(1, 30)]
        yraw = "\n".join(ylines).encode("cp949")
        ypv = S.import_addr_points(con, yraw, preview=True)
        ok(ypv["columns"].get("road_cd") == 6 and ypv["columns"].get("bjd_cd") == 2 and ypv["columns"].get("entr_sn") == 1 and ypv["columns"].get("underground") == 8
           and ypv["has_codes"], f"머리글 없는 위치정보요약DB: 도로명코드 6·법정동코드 2·출입구일련번호 1·지하여부 8 열 찾기 ({ypv['columns']})")
        S.import_addr_points(con, yraw)
        pt = S.rows(con, "SELECT * FROM addr_point WHERE road='서남로' AND main=1 ORDER BY underground")
        ok(len(pt) == 2 and pt[0]["road_cd"] == "291103166047" and pt[0]["bjd_cd"] == "2911010800" and pt[0]["entr_sn"] == 2 and abs(pt[0]["lat"] - 35.1501) < 1e-6
           and pt[1]["underground"] == 1, "가져오기: 도로명코드·법정동코드·지하여부 보관, 출입구가 여럿이면 출입구일련번호가 가장 작은 것(2) 하나, 지하는 따로")
        S.import_addr_points(con, "\n".join([yline("서남로", 1, 0, 35.1600, 126.9400, entr="9")] + ylines[4:]).encode("cp949"))
        ok(abs(S.one(con, "SELECT lat FROM addr_point WHERE road='서남로' AND main=1 AND underground=0")["lat"] - 35.1501) < 1e-6, "다시 가져와도 같은 출입구(일련번호 작은 것)를 유지 — 정해진 하나")
        n = len(calls)
        lc = S.juso_coord(con, res["items"][0]["ref"])
        ok(lc["source"] == "LOCAL" and lc["how"] == "CODE" and abs(lc["lat"] - 35.1501) < 1e-6 and len(calls) == n,
           "좌표 승인키가 있어도 이 PC 자료를 먼저: 도로명코드·지하여부·본번·부번으로 정확히 (외부 호출 없음, source LOCAL)")
        lu = S.juso_coord(con, {**res["items"][0]["ref"], "udrtYn": "1"})
        ok(lu["source"] == "LOCAL" and abs(lu["lat"] - 35.1491) < 1e-6, "지하여부가 1이면 지하 건물 좌표")
        ok(S.juso_search(con, "서남로 1")["coord"], "검색 응답 coord: 주소 위치 자료나 좌표 승인키가 있으면 true")
        n = len(calls)
        # 예전 자료(식별 번호 없음): 도로명·건물번호로 (관할 시군구 안)
        x0, y0 = geo.wgs84_to_utmk(35.1531, 126.9331)
        S.import_addr_points(con, ("시군구명,읍면동명,도로명,건물본번,건물부번,지번본번,지번부번,X좌표,Y좌표\n"
                                   f"동구,가상동,서남로,3,0,,,{x0:.3f},{y0:.3f}\n동구,가상동,옛길,8,0,77,2,{x0 + 50:.3f},{y0:.3f}\n서구,가상동,서남로,9,0,,,{x0:.3f},{y0:.3f}\n").encode())
        r3 = S.juso_coord(con, res["items"][1]["ref"] | {"rn": "서남로", "sggNm": "동구"})
        ok(r3["source"] == "LOCAL" and r3["how"] == "ROAD" and abs(r3["lat"] - 35.1531) < 1e-6 and len(calls) == n,
           "도로명코드로 못 찾으면 도로명·건물번호로 (관할 '동구' 안, 식별 번호 없는 예전 자료)")
        # 리뷰 반영: 지하 주소는 같은 번호의 지상 건물 좌표로 채우지 않음(도로명 대체에서도 지하여부가 같아야 함)
        rug = S.juso_coord(con, res["items"][1]["ref"] | {"rn": "서남로", "sggNm": "동구", "udrtYn": "1", "rnMgtSn": "291104000098"})
        ok(rug.get("source") != "LOCAL" and rug.get("how") != "ROAD",
           "지하 주소(udrtYn=1)에 지상 '서남로 3'만 있으면 도로명 대체로 고르지 않음 → 좌표 API·지도 지정으로")
        n = len(calls)
        # 리뷰 반영: 산 번지 행은 지번 대체 일치에서 같은 번호의 일반 필지로 잡히지 않음(다시 가져오면 예전 지번도 지움)
        xs, ys = geo.wgs84_to_utmk(35.1601, 126.9401)
        S.import_addr_points(con, ("시군구명,읍면동명,도로명,건물본번,건물부번,지번본번,지번부번,산여부,X좌표,Y좌표\n"
                                   f"동구,가상동,산길,5,0,88,3,1,{xs:.3f},{ys:.3f}\n").encode())
        ok(S.one(con, "SELECT jibun_main FROM addr_point WHERE road='산길' AND main=5")["jibun_main"] is None, "산 번지 행은 지번을 저장하지 않음(전에 저장된 지번도 지움)")
        rsan = S.juso_coord(con, {**res["items"][1]["ref"], "rnMgtSn": "291104000097", "rn": "없는길", "buldMnnm": "1", "sggNm": "동구",
                                  "emdNm": "가상동", "lnbrMnnm": "88", "lnbrSlno": "3", "mtYn": "0"})
        ok(not (rsan.get("source") == "LOCAL" and rsan.get("how") == "JIBUN"), "일반 필지 88-3을 산 88-3 좌표로 채우지 않음 → 좌표 API·지도 지정으로")
        n = len(calls)
        ref9 = {**res["items"][1]["ref"], "rnMgtSn": "291104000099", "rn": "서남로", "buldMnnm": "9", "sggNm": "서구"}
        S.save_settings(con, {"external": {"keys": {"juso_coord_key": ""}}})
        r9 = S.juso_coord(con, ref9)
        ok(r9.get("none") and r9["code"] == "NOT_LOCAL" and "이 PC 주소 위치 자료에 없는 주소" in r9["reason"] and "최신" in r9["reason"] and len(calls) == n,
           "관할(동구) 밖 시군구 결과는 이름으로 맞추지 않음 → 좌표 승인키도 없으면 '이 PC 주소 위치 자료에 없는 주소 — 지도에서 지정' (외부 호출 없음)")
        rj = S.juso_coord(con, {"admCd": "2911010800", "rnMgtSn": "291104000098", "udrtYn": "0", "buldMnnm": "4", "buldSlno": "0", "rn": "새길", "sggNm": "동구",
                                "emdNm": "가상동", "lnbrMnnm": "77", "lnbrSlno": "2", "mtYn": "0"})
        ok(rj["source"] == "LOCAL" and rj["how"] == "JIBUN" and abs(rj["lon"] - S.one(con, "SELECT lon FROM addr_point WHERE road='옛길'")["lon"]) < 1e-9,
           "도로명으로도 못 찾으면 지번(법정동 + 번지)으로")
        ok(S.juso_coord(con, {"admCd": "2911010800", "rnMgtSn": "291104000098", "udrtYn": "0", "buldMnnm": "4", "buldSlno": "0", "rn": "새길", "sggNm": "동구",
                              "emdNm": "가상동", "lnbrMnnm": "77", "lnbrSlno": "2", "mtYn": "1"}).get("none"), "산 번지는 지번으로 맞추지 않음")
        ok(S.juso_search(con, "서남로 1")["coord"] and S.addr_stats(con)["imported_at"], "좌표 승인키 없이도 주소 위치 자료가 있으면 coord true")
        S.save_settings(con, {"external": {"keys": {"juso_coord_key": "JUSOCOORDKEY-654321"}}})
        n = len(calls)
        rk = S.juso_coord(con, ref9)
        ok(rk["source"] == "JUSO" and len(calls) == n + 1 and calls[-1]["path"] == "/addrlink/addrCoordApi.do"
           and sorted(calls[-1]["q"]) == ["admCd", "buldMnnm", "buldSlno", "confmKey", "resultType", "rnMgtSn", "udrtYn"],
           "이 PC 자료에 없고 좌표 승인키가 있으면 그때만 행안부 좌표 API (도로명·시군구·지번은 보내지 않음)")
        # 주소 위치 자료 날짜: 45일 넘으면 대시보드 경고·설정 화면 안내
        from dgpc import ops as OPS_
        ok(not S.addr_stats(con)["stale"] and not OPS_.ops_status(con)["addr_warning"], "방금 가져온 주소 위치 자료는 경고 없음")
        S.set_setting(con, "addr_import", {**S.get_setting(con, "addr_import"), "at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=46)).isoformat(timespec="seconds")})
        ast = S.addr_stats(con)
        rdy = {x["key"]: x for x in OPS_.readiness(con)["items"]}["address"]
        ok(ast["stale"] and ast["age_days"] >= 46 and "46일 전" in OPS_.ops_status(con)["addr_warning"] and rdy["ok"] and rdy["label"] == "주소 위치 자료 또는 승인키"
           and "45일 넘음" in rdy["note"], "주소 위치 자료가 45일 넘으면 오늘 현황 경고(운영 준비 점검은 통과·메모로 안내)")
        # 검색어 안전장치: 전화번호·이름 칸 값·저장된 주민 이름
        S.create_request(con, {"name": "홍길가상", "phone": "01000007777", "address": "가상시 동구 예시로 7", "planned_date": "2026-11-05",
                               "items": [{"fee_rate_id": S.FEE_TABLE["rows"][0]["id"], "qty": 1}]})
        n = len(calls)
        b1 = S.juso_search(con, "010-1234-5678")
        b2 = S.juso_search(con, "서남로 김가상", name="김가상")
        b3 = S.juso_search(con, "홍길가상 서남로")
        b4 = S.juso_search(con, "서남로 01012345678")
        ok(all(b.get("blocked") for b in (b1, b2, b3, b4)) and len(calls) == n and "전화번호" in b1["blocked"] and "이름" in b2["blocked"] and "주민 이름" in b3["blocked"],
           "검색어에 전화번호·신청인 이름(이름 칸)·저장된 주민 이름이 있으면 외부로 보내지 않음")
        ok(X.keyword_guard("서남로 12-3") is None and X.keyword_guard("지산1동 123-45") is None and X.keyword_guard("동명로 14 101동 1203호") is None, "보통 주소(번지·동·호)는 막지 않음")
        S.save_settings(con, {"external": {"keys": {"juso_search_key": "BADKEY-0000"}}})
        e = S.juso_search(con, "서남로 1")
        ok(e["code"] == "E0001" and "승인키" in e["error"] and "BADKEY" not in e["error"], "틀린 승인키 → 안내 문구(키 값 없음)")
        S.save_settings(con, {"external": {"keys": {"juso_search_key": "JUSOSEARCHKEY-123456"}}})
        ok(S.external_test(con, "juso")["ok"] and "동구청" in calls[-2]["q"]["keyword"], "설정 [연결 시험]: 개인정보 없는 고정 검색어(동구청)로 검색·좌표 확인")

        # 오류: 시간 초과·HTTP 오류는 일반 문구 (검색어·주소 없음)
        def boom(exc):
            def f(req, timeout):
                raise exc
            return f
        X.urlopen = boom(urllib.error.URLError(socket.timeout("timed out")))
        e = S.juso_search(con, "서남로 99")
        ok(e["code"] == "TIMEOUT" and "서남로" not in e["error"], f"시간 초과 → '{e['error']}'")
        X.urlopen = boom(urllib.error.URLError(ConnectionRefusedError("refused")))
        ok(S.juso_search(con, "서남로 99")["code"] == "NETWORK", "연결 실패 → NETWORK (인터넷·프록시 확인 안내)")
        X.urlopen = boom(urllib.error.HTTPError("https://x/?keyword=서남로", 500, "err", {}, None))
        e = S.juso_search(con, "서남로 99")
        ok(e["code"] == "HTTP_500" and "서남로" not in e["error"] and "keyword" not in e["error"], "HTTP 오류 문구에 요청 주소·검색어 없음")
    finally:
        X.urlopen = real_urlopen

    # 프록시·받는 곳 바꾸기
    saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy")}
    os.environ["HTTPS_PROXY"] = os.environ["https_proxy"] = "http://proxy.example:3128"
    try:
        ph = [hd for hd in X._opener("https://business.juso.go.kr/addrlink/addrLinkApi.do").handlers if isinstance(hd, urllib.request.ProxyHandler)]
        pl = [hd for hd in X._opener("http://127.0.0.1:9/x").handlers if isinstance(hd, urllib.request.ProxyHandler)]
        ok(ph and ph[0].proxies.get("https") == "http://proxy.example:3128" and not any(x.proxies for x in pl), "기관망 프록시(HTTPS_PROXY) 따름, 이 PC 안 주소(시험용 가짜 서버)는 프록시 안 거침")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    os.environ["DG_JUSO_BASE"], os.environ["DG_KAKAO_NAVI_BASE"] = "http://127.0.0.1:1/", "http://127.0.0.1:2"
    ok(X.juso_base() == "http://127.0.0.1:1" and X.kakao_base() == "http://127.0.0.1:2", "DG_JUSO_BASE·DG_KAKAO_NAVI_BASE 로 받는 곳 바꾸기(자동 시험용)")
    del os.environ["DG_JUSO_BASE"], os.environ["DG_KAKAO_NAVI_BASE"]

    # 카카오모빌리티 이동시간 행렬
    kc: list[dict] = []
    fail_keys: set[str] = set()

    def kakao_fake(req, timeout):
        u = urllib.parse.urlsplit(req.full_url)
        rec = {"path": u.path, "q": dict(urllib.parse.parse_qsl(u.query)), "auth": req.get_header("Authorization"), "method": req.get_method(),
               "body": json.loads(req.data) if req.data else None, "url": req.full_url}
        kc.append(rec)
        if rec["auth"] != "KakaoAK KAKAOKEY-0000-1111":
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
        if u.path == "/v1/destinations/directions":
            b = rec["body"]
            ox, oy = float(b["origin"]["x"]), float(b["origin"]["y"])
            routes = []
            for d in b["destinations"]:
                km = routing.haversine_km((oy, ox), (float(d["y"]), float(d["x"])))
                if f'{d["y"]},{d["x"]}' in fail_keys:
                    routes.append({"result_code": 104, "result_msg": "출발지와 도착지가 너무 가까움", "key": d["key"]})
                else:
                    routes.append({"result_code": 0, "key": d["key"], "summary": {"distance": int(km * 1000), "duration": int(km * 100) + 60}})
            return FakeResp({"trans_id": "t", "routes": routes})
        if u.path == "/v1/directions":
            (ox, oy), (dx, dy) = [map(float, rec["q"][k].split(",")) for k in ("origin", "destination")]
            km = routing.haversine_km((oy, ox), (dy, dx))
            return FakeResp({"routes": [{"result_code": 0, "summary": {"duration": int(km * 100) + 60}}]})
        raise AssertionError(u.path)

    X.urlopen = kakao_fake
    try:
        import random as _r
        rng = _r.Random(3)
        pts = [(35.1462, 126.9233)] + [(round(35.14 + rng.uniform(-0.02, 0.02), 6), round(126.92 + rng.uniform(-0.02, 0.02), 6)) for _ in range(33)] + [(35.30, 126.92)]  # 마지막: 17km 밖
        db_before = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("audit_log", "meta")}
        mat, info = routing.kakao_matrix(pts, "KAKAOKEY-0000-1111", min_interval_s=0)
        posts = [c for c in kc if c["path"] == "/v1/destinations/directions"]
        gets = [c for c in kc if c["path"] == "/v1/directions"]
        ok(all(len(c["body"]["destinations"]) <= 30 and c["body"]["radius"] <= 10000 for c in posts) and len(posts) == 34 * 2 and all(c["method"] == "POST" for c in posts),
           f"다중 목적지 API: 출발지마다 30곳씩 나눠 묻고(반경 10km), 행 단위로 채움 (POST {len(posts)}회)")
        ok(len(gets) == 34 * 2 and all(set(c["q"]) == {"origin", "destination", "priority"} for c in gets), f"반경 10km 밖 지점은 1:1 길찾기(GET {len(gets)}회)")
        body_txt = json.dumps([c["body"] for c in posts], ensure_ascii=False)
        ok(set(posts[0]["body"]) == {"origin", "destinations", "radius", "priority"} and all(set(d) == {"x", "y", "key"} for c in posts for d in c["body"]["destinations"])
           and all(c["auth"] == "KakaoAK KAKAOKEY-0000-1111" for c in kc) and not re.search(r"[가-힣]", body_txt), "카카오모빌리티에는 좌표(경도 x·위도 y)와 'KakaoAK 키' 머리글만, 이름·주소 없음")
        ok(info["fallback_pairs"] == 0 and info["road_pairs"] == 35 * 34 and mat[0][1] == math.ceil((int(routing.haversine_km(pts[0], pts[1]) * 100) + 60) / 60) and mat[3][3] == 0,
           f"N×N 행렬(35×35) 모두 도로 시간, 분 단위 올림 (API {info['api_calls']}회)")
        n1 = len(kc)
        kc.clear()
        mat2, info2 = routing.kakao_matrix(pts, "KAKAOKEY-0000-1111", min_interval_s=0)
        ok(len(kc) == n1 and info2["api_calls"] == info["api_calls"] and "cache_hits" not in info2 and mat2 == mat,
           f"두 번째 계산도 카카오모빌리티에 새로 물음(보관한 이동시간 없음, API {len(kc)}회)")
        ok(not con.execute("SELECT 1 FROM sqlite_master WHERE name='travel_cache'").fetchone() and not hasattr(routing, "TravelCache")
           and {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("audit_log", "meta")} == db_before,
           "카카오 이동시간은 DB에 쓰지 않음 (travel_cache 표 없음, 계산 전후 DB 그대로)")
        # 일부 실패 → 그 쌍만 직선 추정
        kc.clear()
        fail_keys.add(f"{pts[2][0]:.5f},{pts[2][1]:.5f}")
        sub = [(35.15, 126.93), pts[2], (35.151, 126.931), (35.152, 126.935)]
        mat3, info3 = routing.kakao_matrix(sub, "KAKAOKEY-0000-1111", min_interval_s=0)
        est = routing._estimate_min(sub[0], sub[1])
        ok(info3["fallback_pairs"] == 3 and mat3[0][1] == est and routing.matrix_note("KAKAO", info3) == "도로 시간: 카카오모빌리티 (일부 직선 추정 3쌍)",
           "경로를 못 받은 쌍만 직선거리 추정 → '도로 시간: 카카오모빌리티 (일부 직선 추정 3쌍)'")
        # 시간 초과: 더 묻지 않고 전부 추정
        X.urlopen = lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError(socket.timeout("timed out")))
        far = [(35.16, 126.94), (35.161, 126.941), (35.163, 126.945)]
        m4, s4, i4 = routing.build_matrix(far, method="KAKAO", kakao_key="KAKAOKEY-0000-1111")
        ok(s4 == "ESTIMATE" and i4["api_calls"] == 1 and m4 == routing.estimate_matrix(far) and "카카오모빌리티로 구하지 못함" in routing.matrix_note(s4, i4),
           "시간 초과 → 첫 실패에서 멈추고 전부 직선거리 추정, 화면 문구에 표시")
        X.urlopen = kakao_fake
        kc.clear()
        _m, s5, i5 = routing.build_matrix(far, method="KAKAO", kakao_key="WRONGKEY-000000")
        ok(s5 == "ESTIMATE" and len(kc) == 1 and "인증" in i5["error"], "틀린 키(401) → 한 번만 묻고 직선거리 추정")
        _m, s6, i6 = routing.build_matrix(far, method="KAKAO", kakao_key="")
        ok(s6 == "ESTIMATE" and "REST 키" in routing.matrix_note(s6, i6), "키 없이 카카오 방식 → 직선거리 추정과 이유 표시")
        kc.clear()
        mm, _ = routing.kakao_matrix([(35.15 + i * 1e-3, 126.93) for i in range(6)], "KAKAOKEY-0000-1111", max_calls=2, min_interval_s=0)
        ok(len(kc) == 2, "호출 수 한도(max_calls)를 넘으면 더 묻지 않음 (나머지 직선 추정)")
        t0 = time.monotonic()
        routing.kakao_matrix([(35.17 + i * 1e-3, 126.93) for i in range(3)], "KAKAOKEY-0000-1111", min_interval_s=0.15)
        ok(time.monotonic() - t0 >= 0.3, "호출 사이 최소 간격(초당 호출 수 제한)")
        # 노선 계산 전체: 카카오 방식
        S.save_settings(con, {"external": {"keys": {"kakao_rest_key": "KAKAOKEY-0000-1111"}, "travel_method": "KAKAO"}})
        ok(S.travel_method(con) == "KAKAO" and S.settings(con)["external"]["keys"]["kakao_rest_key"]["mask"] == "••••1111", "설정: 이동시간 계산 방식 카카오모빌리티 + REST 키(가린 값)")
        crew = {"id": "c1", "name": "가상", "capacity_m3": 20, "shift_start": "09:00", "shift_end": "17:00"}
        reqs = [{"id": f"r{i}", "lat": 35.145 + i * 2e-3, "lon": 126.92 + i * 1e-3, "items": [{"name": "의자", "qty": 1}]} for i in range(4)]
        pl = routing.plan(reqs, [crew], {"lat": 35.1462, "lon": 126.9233}, travel_method="KAKAO", kakao_key=S.secret(con, "kakao_rest_key"),
                          time_limit_s=1)
        ok(pl["matrix_source"] == "KAKAO" and pl["matrix_note"].startswith("도로 시간: 카카오모빌리티") and len(pl["routes"][0]["stops"]) == 4, f"노선 계산: {pl['matrix_note']}")
        S.save_settings(con, {"external": {"travel_method": "ESTIMATE"}})
        ok(routing.plan(reqs, [crew], {"lat": 35.1462, "lon": 126.9233}, travel_method=S.travel_method(con), time_limit_s=1)["matrix_source"] == "ESTIMATE", "기본(직선거리 추정)으로 되돌리면 외부 호출 없음")
    finally:
        X.urlopen = real_urlopen

    # 휴대폰 목록: 감면율(rd)·부과번호(lv)
    reg = S.registration_text(con)
    ph = Phone(reg)
    dev = S.register_device(con, ph.code(), "감면폰", confirm=True)["device_id"]
    cid = S.save_crew(con, {"name": "감면 시험팀", "device_id": dev})
    base = {"phone": "01000008888", "address": "가상시 동구 예시로 8", "planned_date": "2026-11-06", "lat": 35.15, "lon": 126.92,
            "items": [{"fee_rate_id": next(r["id"] for r in S.FEE_TABLE["rows"] if r["status"] == "CONFIRMED"), "qty": 1}]}
    ra = S.create_request(con, {**base, "name": "감면가상", "reduction_code": "NEAR_POOR", "reduction_proof": "서류 확인", "reduction_checker": "확인자"})
    rb = S.create_request(con, {**base, "name": "보통가상"})
    bt = S.confirm_plan(con, "2026-11-06", {cid: [{"request_id": ra["id"], "eta": "09:00"}, {"request_id": rb["id"], "eta": "09:30"}]})[0]
    payload, _ = S.build_daylist(con, bt["batch_id"])
    comp = P.compact_daylist(payload, S.FEE_ROWS)
    ok(comp["s"][0]["rd"] == 50 and comp["s"][0]["lv"] == ra["levy_no"] and "rd" not in comp["s"][1] and comp["s"][1]["lv"] == rb["levy_no"]
       and "차상위" not in json.dumps(comp, ensure_ascii=False), f"목록 압축: 감면 건 'rd'=50·'lv'={ra['levy_no']}, 감면 없는 건은 'rd' 없음, 감면 사유 이름은 보내지 않음")
    bun = S.make_bundle(con, bt["batch_id"])
    _h, got, _r = ph.receive(bun["frames"])
    ok(got["stops"][0]["reduction"] == {"rate": 50} and got["stops"][0]["levy_no"] == ra["levy_no"] and "reduction" not in got["stops"][1], "휴대폰(흉내)이 감면율·부과번호를 받음")
    old = {**comp, "s": [{k: v for k, v in x.items() if k not in ("rd", "lv")} for x in comp["s"]]}
    eo = P.expand_daylist(old, S.FEE_ROWS)
    ok(all("reduction" not in x and x["levy_no"] == "" for x in eo["stops"]), "이전 형식(rd·lv 없음)도 그대로 읽음 → 감면 없음")
    calc = sum(i["amount"] or 0 for i in payload["stops"][0]["items"])
    fs = FN.fee_summary(S.one(con, "SELECT * FROM request WHERE id=?", (ra["id"],)), calc)
    ok(fs["reduction"] == calc * 50 // 100 and fs["levied"] == calc - fs["reduction"], f"PC 감면 규칙(감면액 원 단위 버림): 계산 {calc} → 감면 {fs['reduction']} → 부과 {fs['levied']}")
    con.close()
    print(f"\n통과 {passed}개 (v0.11 외부 서비스 포함)")


def v12_field():
    """v0.12 PC 연결 방식 휴대폰의 현장 접수·정정 요청·취소 요청: PC가 원본, 사무실 확인을 거친다."""
    print("\n— v0.12 현장 접수·정정·취소 요청 —")
    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-v12-")
    con = store.connect(create=True)
    S.ensure_pc_keys(con)
    ph = Phone(S.registration_text(con))
    dev = S.register_device(con, ph.code(), "현장폰", confirm=True)["device_id"]
    cid = S.save_crew(con, {"name": "현장 시험팀", "device_id": dev})
    other = Phone(S.registration_text(con))
    S.register_device(con, other.code(), "다른폰", confirm=True)
    fee = {r["name"] + "|" + r["spec"]: r for r in S.FEE_TABLE["rows"]}
    chair = fee["의자|고정식 의자"]
    day = "2026-11-10"
    # 원래 접수 + 배정
    r0 = S.create_request(con, {"name": "원래가상", "phone": "010-0000-7001", "address": "가상시 동구 예시로 70", "planned_date": day,
                                "lat": 35.15, "lon": 126.92, "items": [{"fee_rate_id": chair["id"], "qty": 1}]})
    bt = S.confirm_plan(con, day, {cid: [{"request_id": r0["id"], "eta": "09:00"}]})[0]
    payload, _ = S.build_daylist(con, bt["batch_id"])
    s0 = payload["stops"][0]

    # 1) 현장 접수: 휴대폰 단가(0원)는 믿지 않고 PC 표로 다시 계산, 표에 없는 품목은 확인 대기
    rid, l1, l2 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    fstop = {"request_id": rid, "assign_ver": 0, "req_ver": 1}
    intake = ph.ev(fstop, "FIELD_INTAKE", {"temp_no": "현장-1", "name": "현장가상", "phone": "010-0000-7002", "address": "가상시 동구 예시로 72",
                                            "spot_note": "대문 옆", "memo": "", "planned_date": day, "eta": "10:30", "dong": "",
                                            "items": [{"line_id": l1, "fee_rate_id": chair["id"], "qty": 2, "unit_price": 0, "price_status": "CONFIRMED"},
                                                      {"line_id": l2, "fee_rate_id": None, "name": "표에없는물건", "unit": "EA", "unit_label": "개", "qty": 1}]}, assign_ver=0)
    intake["assign_ver"] = 0
    res_ev = [ph.ev(fstop, "ITEM_RESULT", {"line_id": l1, "qty_confirmed": 2, "qty_collected": 2}, assign_ver=0),
              ph.ev(fstop, "ITEM_RESULT", {"line_id": l2, "qty_confirmed": 1, "qty_collected": 1}, assign_ver=0),
              ph.ev(fstop, "ATTEMPT", {"outcome": "DONE"}, assign_ver=0)]
    for e in res_ev:
        e["assign_ver"] = 0
    out = S.import_result(con, ph.seal({"schema": "dg.result/1", "work_date": day, "events": [intake] + res_ev}, P.TYPE_RESULT))
    req = S.one(con, "SELECT * FROM request WHERE id=?", (rid,))
    items = {i["line_id"]: i for i in S.rows(con, "SELECT * FROM request_item WHERE request_id=?", (rid,))}
    ok(out.get("field_intakes") == 1 and out["conflict"] == 0 and req and req["channel"] == "현장" and req["origin_device"] == dev and req["receipt_no"].startswith("DG-"),
       f"현장 접수 → PC 원본 접수 {req and req['receipt_no']} (경로 '현장', 출처 기기 기록), 결과도 충돌 없이 반영")
    ok(items[l1]["unit_price"] == chair["price"] and items[l1]["amount"] == chair["price"] * 2 and items[l2]["price_status"] == "PENDING" and items[l2]["amount"] is None,
       "현장 접수 금액: 휴대폰 단가를 믿지 않고 PC 표로 다시 계산, 표에 없는 품목은 확인 대기(0원 아님)")
    ok(req["status"] == "DONE" and req["appt_start"] == "10:30", "현장 접수 건의 수거 결과 반영(수거 완료) · 방문 시각")
    ok(req["received_at"] == intake["device_ts"], "현장 접수의 접수 시각 = 휴대폰에서 받은 때(PC로 가져온 때가 아님)")
    stored = json.loads(S.one(con, "SELECT body_json FROM result_event WHERE event_id=?", (intake["event_id"],))["body_json"])
    ok("현장가상" not in json.dumps(stored, ensure_ascii=False) and "0000-7002" not in json.dumps(stored) and stored.get("pii_purged"),
       "결과 기록에는 현장 접수의 이름·연락처·주소를 남기지 않음(원본은 접수에)")
    ok([x["id"] for x in S.field_intakes(con)] == [rid] and S.search_requests(con, {"quick": "FIELD", "status": ""})["total"] == 1, "현장 접수 확인 목록·빠른 조회 '현장 접수 확인'")
    ok(S.day_summary(con, day)["field_intakes"] == 1, "오늘 현황의 처리할 일: 현장 접수 확인 1건")
    S.check_field_intake(con, rid)
    ok(not S.field_intakes(con) and S.one(con, "SELECT field_checked_at FROM request WHERE id=?", (rid,))["field_checked_at"], "사무실 확인 완료 표시")
    raises(lambda: S.check_field_intake(con, rid), "이미", "두 번 확인 거부")
    raises(lambda: S.check_field_intake(con, r0["id"]), "현장 접수가 아닙니다", "일반 접수는 현장 확인 대상 아님")
    # 같은 파일 다시: 중복 / 같은 ID 다시 보내도 접수를 두 번 만들지 않음
    ph.seq += 1
    again = dict(intake, event_id=str(uuid.uuid4()), seq=ph.seq)
    S.import_result(con, ph.seal({"schema": "dg.result/1", "work_date": day, "events": [again]}, P.TYPE_RESULT))
    ok(S.one(con, "SELECT COUNT(*) AS n FROM request WHERE id=?", (rid,))["n"] == 1, "같은 현장 접수가 다시 와도 접수는 하나")
    # 다른 휴대폰이 이 현장 접수 건의 결과를 보내면: 배정된 적 없는 접수 → 충돌
    rid2 = str(uuid.uuid4())
    i2 = ph.ev({"request_id": rid2, "assign_ver": 0, "req_ver": 1}, "FIELD_INTAKE", {"channel": "전화", "name": "현장둘", "phone": "010-0000-7003", "address": "가상시 동구 예시로 74",
                                                                                 "planned_date": day, "items": [{"line_id": str(uuid.uuid4()), "fee_rate_id": chair["id"], "qty": 1}]})
    S.import_result(con, ph.seal({"schema": "dg.result/1", "work_date": day, "events": [i2]}, P.TYPE_RESULT))
    ok(S.one(con, "SELECT channel FROM request WHERE id=?", (rid2,))["channel"] == "전화", "휴대폰에서 고른 접수 경로(전화)가 PC 원본 접수 경로로")
    oth = other.ev({"request_id": rid2, "assign_ver": 0, "req_ver": 1}, "ATTEMPT", {"outcome": "NOT_COLLECTED"}, assign_ver=0)
    oth["assign_ver"] = 0
    S.import_result(con, other.seal({"schema": "dg.result/1", "work_date": day, "events": [oth]}, P.TYPE_RESULT))
    ok(S.one(con, "SELECT apply_status FROM result_event WHERE event_id=?", (oth["event_id"],))["apply_status"] == "CONFLICT", "다른 휴대폰의 결과는 충돌함으로(출처 기기만 예외)")

    # 2) 잘못된 현장 접수(연락처 틀림): 충돌함에서 확인, 반영하려면 고쳐서 다시 접수해야 함
    bad_id = str(uuid.uuid4())
    bad = ph.ev({"request_id": bad_id, "assign_ver": 0, "req_ver": 1}, "FIELD_INTAKE", {"name": "틀린가상", "phone": "12", "address": "가상시 동구 예시로 73",
                                                                                    "planned_date": day, "items": [{"line_id": str(uuid.uuid4()), "fee_rate_id": chair["id"], "qty": 1}]})
    o2 = S.import_result(con, ph.seal({"schema": "dg.result/1", "work_date": day, "events": [bad]}, P.TYPE_RESULT))
    cf = [c for c in S.list_conflicts(con) if c["event_id"] == bad["event_id"]]
    ok(o2.get("review") == 1 and cf and "현장 접수 확인 필요" in cf[0]["conflict_reason"] and not S.one(con, "SELECT 1 AS x FROM request WHERE id=?", (bad_id,)),
       "형식이 틀린 현장 접수는 만들지 않고 충돌함으로")
    raises(lambda: S.resolve_conflict(con, bad["event_id"], "APPLY", "확인"), "연락처", "그대로 반영하면 오류(아무것도 바뀌지 않음)")
    S.resolve_conflict(con, bad["event_id"], "REJECT", "주민에게 전화해 PC에서 다시 접수")
    ok("틀린가상" not in S.one(con, "SELECT body_json FROM result_event WHERE event_id=?", (bad["event_id"],))["body_json"], "거부한 현장 접수 기록에서도 개인정보를 지움")

    # 3) 정정 요청: 바로 바뀌지 않고 확인 대상 → 반영하면 버전 보존 + 동·위치 다시
    s0c = {**s0, "assign_ver": s0["assign_ver"]}
    corr = ph.ev(s0c, "CORRECTION", {"fields": {"phone": "010-0000-7009", "address": "가상시 동구 예시로 79", "planned_date": "2026-11-12"}, "reason": "주민이 번호 바뀜 알려 줌"})
    o3 = S.import_result(con, ph.seal({"schema": "dg.result/1", "work_date": day, "events": [corr]}, P.TYPE_RESULT))
    cur = S.one(con, "SELECT phone, req_ver FROM request WHERE id=?", (r0["id"],))
    lc = [c for c in S.list_conflicts(con) if c["event_id"] == corr["event_id"]]
    ok(o3.get("review") == 1 and o3["conflict"] == 0 and cur["phone"] == "01000007001" and lc and lc[0]["current"]["phone"] == "01000007001" and "정정 요청" in lc[0]["conflict_reason"],
       "정정 요청은 원본을 바로 바꾸지 않고 확인 대상(지금 값과 나란히)")
    S.resolve_conflict(con, corr["event_id"], "APPLY", "주민 통화로 확인")
    now_ = S.one(con, "SELECT * FROM request WHERE id=?", (r0["id"],))
    ver = S.rows(con, "SELECT * FROM request_version WHERE request_id=?", (r0["id"],))
    ok(now_["phone"] == "01000007009" and now_["address"] == "가상시 동구 예시로 79" and now_["planned_date"] == "2026-11-12" and now_["req_ver"] == cur["req_ver"] + 1
       and now_["lat"] is None and any("현장 정정 요청 반영" in v["reason"] for v in ver), "정정 반영: 연락처·주소·예정일 변경, 이전 내용은 버전으로, 주소가 바뀌어 위치는 다시 지정")
    ok(not S.one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (r0["id"],)) and now_["status"] == "RECEIVED", "예정일이 바뀌면 그날 배정을 풀어 다시 배정")
    ok("0000-7009" not in S.one(con, "SELECT body_json FROM result_event WHERE event_id=?", (corr["event_id"],))["body_json"], "반영한 정정 기록에는 바꾼 칸 이름만 남김")
    same = ph.ev(s0c, "CORRECTION", {"fields": {"phone": "010-0000-7009"}, "reason": "중복"})
    S.import_result(con, ph.seal({"schema": "dg.result/1", "work_date": day, "events": [same]}, P.TYPE_RESULT))
    raises(lambda: S.resolve_conflict(con, same["event_id"], "APPLY", "확인"), "같은 내용", "원본과 같은 정정은 반영하지 않음(거부로 정리)")
    S.resolve_conflict(con, same["event_id"], "REJECT", "이미 반영됨")

    # 4) 취소 요청: 확인 후 취소(사유 코드 + 현장 요청 표시)
    canc = ph.ev(s0c, "CANCEL_REQUEST", {"code": "RESIDENT", "text": "주민이 직접 처리함"})
    S.import_result(con, ph.seal({"schema": "dg.result/1", "work_date": day, "events": [canc]}, P.TYPE_RESULT))
    ok(S.one(con, "SELECT status FROM request WHERE id=?", (r0["id"],))["status"] != "CANCELLED", "취소 요청도 바로 취소하지 않음")
    S.resolve_conflict(con, canc["event_id"], "APPLY", "주민 통화 확인")
    cr = S.one(con, "SELECT status, cancel_code FROM request WHERE id=?", (r0["id"],))
    au = [S._with_note(con, a) for a in S.rows(con, "SELECT id, detail FROM audit_log WHERE entity='request' AND entity_id=? AND action='CANCEL'", (r0["id"],))]
    ok(cr["status"] == "CANCELLED" and cr["cancel_code"] == "RESIDENT" and au and "현장 요청" in au[-1]["detail"], "취소 요청 반영: 사유 코드(주민 취소) + '현장 요청' 기록")
    d = S.derive(con, S.one(con, "SELECT * FROM request WHERE id=?", (rid,)))
    ok(not d["field"]["other_kinds"], "PC 화면의 '알 수 없는 기록'에 새 종류가 섞이지 않음")
    # 보유기간 파기: 처리하지 않은 정정 요청·접수를 만들지 못한 현장 접수의 개인정보도 지운다
    from dgpc import privacy as PV
    pend = ph.ev(fstop, "CORRECTION", {"fields": {"phone": "010-0000-7777", "address": "가상시 동구 남은로 1"}, "reason": "남은 요청"}, assign_ver=0)
    orphan_id = str(uuid.uuid4())
    orphan = ph.ev({"request_id": orphan_id, "assign_ver": 0, "req_ver": 1}, "FIELD_INTAKE", {"name": "고아가상", "phone": "1", "address": "가상시 동구 고아로 1",
                                                                                       "planned_date": day, "items": [{"line_id": str(uuid.uuid4()), "fee_rate_id": chair["id"], "qty": 1}]})
    S.import_result(con, ph.seal({"schema": "dg.result/1", "work_date": day, "events": [pend, orphan]}, P.TYPE_RESULT))
    pr = PV.purge_pii(con, today="2031-12-31")
    bodies = {e["event_id"]: e for e in S.rows(con, "SELECT event_id, body_json, apply_status FROM result_event WHERE event_id IN (?,?)", (pend["event_id"], orphan["event_id"]))}
    ok(pr["count"] >= 1 and pr.get("orphan_requests") == 1 and "7777" not in bodies[pend["event_id"]]["body_json"] and "남은로" not in bodies[pend["event_id"]]["body_json"]
       and "고아가상" not in bodies[orphan["event_id"]]["body_json"] and {b["apply_status"] for b in bodies.values()} == {"REJECTED"},
       "보유기간 파기: 미처리 정정 요청·접수 없는 현장 접수 기록의 이름·연락처·주소도 지우고 처리 끝으로")
    aw = lambda a: " ".join(S.address_warnings(con, a))
    ok(aw("가상시 동구 예시로 12") == "" and "건물번호" in aw("예시아파트 앞") and "형식" in aw("동구 12번지 근처") and "전화번호" in aw("예시로 5 010-1234-5678")
       and "관할" in aw("광주 서구 상무대로 1") and aw("동구 충장로1가 12") == "", "PC 배출 주소 확인: 건물번호 없음·형식·전화번호 섞임·관할 밖(막지 않고 알림), '충장로1가 12'는 지번")
    con.close()
    print(f"\n통과 {passed}개 (v0.12 포함)")


def v12_pilot():
    """시범 운영 비교: 이 시스템 기록으로 계산한 지표 ↔ 담당자가 적은 기존 방식 수치(100건당으로 맞춤)."""
    print("\n— v0.12 시범 운영 비교 —")
    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-pilot-")
    con = store.connect(create=True)
    S.ensure_pc_keys(con)
    today = S.today_kst()
    fid = next(r["id"] for r in S.FEE_TABLE["rows"] if r["status"] == "CONFIRMED")
    ids = [S.create_request(con, {"name": f"시범{i}", "phone": f"010000060{i:02d}", "address": f"가상시 동구 예시로 {i}", "planned_date": today,
                                  "items": [{"fee_rate_id": fid, "qty": 1}], "intake_secs": 60 * (i + 1)})["id"] for i in range(4)]
    S.create_request(con, {"name": "자리비움", "phone": "01000006099", "address": "가상시 동구 예시로 99", "planned_date": today, "items": [{"fee_rate_id": fid, "qty": 1}], "intake_secs": 99999})
    con.execute("UPDATE request SET status='DONE', completed_at=received_at WHERE id=?", (ids[0],))
    S.add_contact(con, ids[1], {"kind": "독촉", "content": "언제 오나요"})
    raises(lambda: S.pilot_report(con), "기간", "시범 운영 기간을 정하기 전에는 비교표 없음")
    raises(lambda: S.save_pilot(con, {"start": today, "end": today, "base": {"missed": "열"}}), "숫자", "기존 방식 수치는 숫자만")
    raises(lambda: S.save_pilot(con, {"start": today, "end": ""}), "함께", "시범 운영 기간은 시작·끝을 함께(한쪽만 저장해 화면이 막히지 않게)")
    S.save_pilot(con, {"start": today, "end": today, "base_period": "2026년 9월 종이 대장",
                       "base": {"requests": 100, "missed": 10, "complaints": 8, "lead_days": 5, "intake_min": 6, "close_min": 40}})
    S.close_day(con, today, "", close_secs=1800)
    r = {x["key"]: x for x in S.pilot_report(con)["rows"]}
    ok(r["requests"]["pilot"] == 5 and r["missed"]["base"] == 10.0 and r["missed"]["count"] == 4 and r["missed"]["pilot"] == 80.0 and r["missed"]["verdict"] == "나빠짐",
       f"누락: 마감 때 '미배정'으로 넘어간 건 → 100건당 {r['missed']['pilot']} (기존 10 → 나빠짐)")
    ok(r["complaints"]["pilot"] == 20.0 and r["complaints"]["base"] == 8.0 and r["lead_days"]["pilot"] == 0 and r["lead_days"]["verdict"] == "좋아짐",
       "독촉·불만 100건당·평균 처리 일수 비교")
    ok(r["intake_min"]["pilot"] == 2.5 and r["intake_min"]["count"] == 4 and r["intake_min"]["verdict"] == "좋아짐" and r["close_min"]["pilot"] == 30.0,
       "접수 1건 처리 시간(화면에서 잰 값, 자리 비움 같은 이상값 제외) 2.5분 · 하루 마감 30분")
    csvb = S.pilot_csv(con).decode("utf-8-sig")
    ok("누락(약속한 날 처리 못 함)" in csvb and not re.search(r"시범\d|자리비움", csvb) and "010" not in csvb, "시범 운영 비교 CSV(이름·연락처 없음)")
    con.close()
    print(f"\n통과 {passed}개 (시범 운영 비교 포함)")


def v12_load():
    """품목별 적재 기준(부피·무게·작업 시간·취급 표시)과 노선 계산: 무게 한도, 큰 짐 먼저, 필수 건 대안, 반입지 시간·쉬는 시간."""
    print("\n— v0.12 적재 기준·노선 —")
    H = routing.handling_of
    rows = list(S.FEE_ROWS.values())
    ok(all(H(r)["rule"] for r in rows), f"수수료표 {len(rows)}행 모두 품목별 적재 기준이 있음 (기본값으로 빠지는 행 없음)")
    tv = H({"name": "TV 받침대", "spec": "모든 규격"})
    ok(tv["m3"] == 0.3 and "BASE" not in tv["tags"], "'TV 받침대'를 침대로 잘못 읽지 않음 (이전: 1.5m³)")
    big, small = H({"name": "냉장고 (업소용 제외)", "spec": "800ℓ 이상"}), H({"name": "냉장고 (업소용 제외)", "spec": "300ℓ 미만"})
    ok(big["m3"] > small["m3"] * 3 and big["kg"] > small["kg"] * 3 and "UPRIGHT" in big["tags"], "규격별 부피·무게: 냉장고 800ℓ 이상 ≫ 300ℓ 미만, 세워서 싣기")
    ok(H({"name": "흙, 깨진 유리, 사기", "spec": "20ℓ 당"})["m3"] == 0.02 and H({"name": "목재류", "spec": "50ℓ(1 마대) 당"})["m3"] == 0.05,
       "마대·리터 단위 품목은 그 부피(20ℓ = 0.02m³, 50ℓ = 0.05m³)")
    ok(H({"name": "책상 (책장, 책꽂이 별도)", "spec": "양수, 대형"})["rule"].startswith("책상") and H({"name": "책상의자", "spec": ""})["rule"] == "의자",
       "이름에 다른 품목 낱말이 섞여도 바른 규칙 (책상(책장 별도)·책상의자)")
    ok(routing.tags_of([{"name": "피아노", "spec": "업라이트", "qty": 1}, {"name": "의자", "spec": "", "qty": 2}]) == ["EQUIP", "BASE"],
       "취급 표시 합치기: 피아노 → 4인·장비(2인은 생략)·먼저·바닥")
    ok(routing.work_min([{"name": "피아노", "spec": "그랜드", "qty": 1}]) > routing.work_min([{"name": "의자", "spec": "", "qty": 3}]),
       "작업 시간: 그랜드 피아노 1대 > 의자 3개 (품목 수가 아니라 품목별)")
    js = (ROOT / "web" / "fees-data.js").read_text(encoding="utf8")
    ok("export const ITEM_HANDLING = " + json.dumps(routing.HANDLING, ensure_ascii=False) + ";" in js,
       "휴대폰 내장 적재 기준이 data/item_handling.json 과 같음 (tools/build_fee_js.py 로 생성)")

    dep = {"lat": 35.1462, "lon": 126.9233}
    crew = lambda **k: {"id": k.get("id", "c"), "name": k.get("id", "c"), "capacity_m3": k.get("m3", 8), "capacity_kg": k.get("kg", 0),
                        "shift_start": k.get("s", "09:00"), "shift_end": k.get("e", "17:00")}
    stone = [{"id": f"s{i}", "lat": 35.14 + 0.003 * i, "lon": 126.92, "items": [{"name": "침대(돌, 옥, 흙)", "spec": "2인용", "qty": 1}]} for i in range(4)]
    no_kg = routing.plan(stone, [crew()], dep, time_limit_s=1)
    with_kg = routing.plan(stone, [crew(kg=1000)], dep, time_limit_s=1)
    ok(len(no_kg["routes"][0]["stops"]) == 4 and len(with_kg["routes"][0]["stops"]) == 3 and with_kg["routes"][0]["end_load_kg"] <= 1000 and with_kg["uses_weight"],
       f"차량 최대 적재 중량 1,000kg → 돌침대(약 280kg) 4개 중 3개만 (부피로는 4개 다 들어감)")
    wk = routing.plan(stone, [crew(kg=1000)], dep, time_limit_s=2, unload_site={"lat": 35.16, "lon": 126.95, "label": "가상 반입지"})
    rt = wk["routes"][0]
    ok(len(rt["stops"]) == 4 and rt["unloads"] >= 1 and all(x["load_kg"] <= 1000 for x in rt["stops"]) and any(x.get("unload_kg") for x in rt["stops"]),
       f"반입지가 있으면 무게가 찰 때도 들러 비우고 4곳 모두 (하차 {rt['unloads']}회)")
    sm = wk["summary"]
    ok(sm["requests"] == 4 and sm["kg"] == 1120 and sm["tags"].get("EQUIP") == 4 and any("4인·장비" in w for w in sm["warnings"]),
       "하루 물량 점검: 무게 합 1,120kg · 4인·장비 품목 알림")

    # 큰 짐 먼저: 한 줄로 늘어선 곳(가는 길·오는 길 이동시간이 같음)이면 장롱(먼저·바닥)을 빈 차에 싣는 순서를 고른다
    mix = [{"id": "w", "lat": 35.162, "lon": 126.9233, "items": [{"name": "장롱", "spec": "120cm 이상 1쪽당", "qty": 2}]}] + \
          [{"id": f"b{i}", "lat": 35.150 + 0.004 * i, "lon": 126.9233, "items": [{"name": "목재류", "spec": "50ℓ(1 마대) 당", "qty": 30}]} for i in range(3)]
    def before(res):
        return next(round(x["load_m3"] - x["m3"], 2) for x in res["routes"][0]["stops"] if x["request_id"] == "w")
    on, off = routing.plan(mix, [crew()], dep, time_limit_s=1), routing.plan(mix, [crew()], dep, time_limit_s=1, base_first=False)
    ok(before(on) == 0 and before(off) > 3.2 and on["routes"][0]["end"] <= off["routes"][0]["end"] and all("tags" in x for x in on["routes"][0]["stops"]),
       f"큰 짐 먼저 선호: 장롱을 빈 차에 먼저 (선호 끄면 마대 {before(off)}m³ 위에) · 끝나는 시각 같음 {on['routes'][0]['end']}")

    # 오늘 필수가 다 안 들어가면 전체 실패 대신 넣을 수 있는 것은 넣고 못 넣은 필수를 알림
    must = [{"id": f"m{i}", "lat": 35.14 + 0.004 * i, "lon": 126.92, "must_today": True, "items": [{"name": "장롱", "spec": "120cm 이상 1쪽당", "qty": 2}]} for i in range(4)]
    rm = routing.plan(must, [crew(m=6)], dep, time_limit_s=1)
    miss = [u for u in rm["unassigned"] if u.get("must_today")]
    ok(rm["must_relaxed"] and not rm.get("infeasible") and len(rm["routes"][0]["stops"]) == 2 and len(miss) == 2 and "오늘 필수였지만" in miss[0]["reason"],
       "오늘 필수 4건(각 2.8m³)이 6m³ 차량에 다 안 들어감 → 2건은 노선에, 2건은 '오늘 필수였지만 넣지 못함'")

    # 쉬는 시간·반입지 받는 시간
    pts = [{"id": f"p{i}", "lat": 35.14 + 0.002 * (i % 4), "lon": 126.92 + 0.002 * (i // 4), "items": [{"name": "의자", "spec": "", "qty": 1}]} for i in range(12)]
    lr = routing.plan(pts, [crew()], dep, time_limit_s=1, lunch={"start": "11:30", "end": "13:00", "minutes": 60})
    lu = lr["routes"][0]["lunch"]
    gap = lu and not any(lu["start"] < x["eta"] < routing.hhmm(routing.to_min(lu["start"], 0) + 60) for x in lr["routes"][0]["stops"])
    ok(lu and "11:30" <= lu["start"] <= "12:00" and gap and len(lr["routes"][0]["stops"]) == 12, f"쉬는 시간 {lu and lu['start']}부터 60분: 그동안 방문 없음")
    late = routing.plan(stone, [crew(kg=1000)], dep, time_limit_s=1, unload_site={"lat": 35.16, "lon": 126.95, "label": "가상 반입지"},
                        unload_hours={"start": "13:00", "end": "16:00"})
    ue = [x["unload_after"]["eta"] for x in late["routes"][0]["stops"] if x.get("unload_after")]
    ok(ue and all("13:00" <= e <= "15:40" for e in ue), f"반입지 받는 시간 13:00~16:00 → 하차 도착 {', '.join(ue)}")

    # 설정·팀 저장
    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-load-")
    con = store.connect(create=True)
    raises(lambda: S.save_settings(con, {"lunch": {"start": "12:00", "end": "13:00", "minutes": 90}}), "쉬는 시간", "쉬는 시간이 창보다 길면 거부")
    raises(lambda: S.save_settings(con, {"unload_hours": {"start": "17:00", "end": "09:00"}}), "반입지", "반입지 받는 시간 순서 검사")
    S.save_settings(con, {"lunch": {"start": "11:30", "end": "13:30", "minutes": 60}, "unload_hours": {"start": "09:00", "end": "17:00"}, "base_first": False})
    st = S.settings(con)
    ok(st["lunch"] == {"start": "11:30", "end": "13:30", "minutes": 60} and st["unload_hours"]["end"] == "17:00" and st["base_first"] is False, "노선 설정 저장: 쉬는 시간·반입지 받는 시간·큰 짐 먼저 끔")
    S.save_settings(con, {"lunch": {"start": "", "end": ""}})
    ok(S.settings(con)["lunch"] is None, "쉬는 시간 비우면 따지지 않음")
    raises(lambda: S.save_crew(con, {"name": "1팀", "capacity_kg": -5}), "중량", "최대 적재 중량 범위 검사")
    cid = S.save_crew(con, {"name": "1팀", "capacity_m3": 8, "capacity_kg": 1000})
    ok(next(c for c in S.list_crews(con) if c["id"] == cid)["capacity_kg"] == 1000, "팀 최대 적재 중량 저장")
    con.close()
    print(f"\n통과 {passed}개 (적재 기준·노선 포함)")


def v12_review():
    """전체 검토(2026-11-04)에서 나온 결함의 재발 방지."""
    print("\n— v0.12 전체 검토 반영 —")
    import io
    from dgpc import finance as FN, privacy as PV
    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-review-")
    con = store.connect(create=True)
    S.ensure_pc_keys(con)
    S.ACTOR.set("admin")
    fee = {r["name"] + "|" + r["spec"]: r for r in S.FEE_TABLE["rows"]}
    chair, ward = fee["의자|고정식 의자"], fee["장롱|120cm 이상 1쪽당"]
    base = {"phone": "010-0000-9100", "address": "가상시 동구 예시로 91", "planned_date": "2026-11-20", "lat": 35.15, "lon": 126.92}

    # 기기 등록: 지문 확인 전에는 목록을 못 보냄, 분실 기기는 다시 등록되지 않음, 지문은 PC 키 합의 키까지 덮음
    ph = Phone(S.registration_text(con))
    reg = S.register_device(con, ph.code(), "검토폰")
    cid = S.save_crew(con, {"name": "검토팀", "device_id": reg["device_id"]})
    r1 = S.create_request(con, {**base, "name": "검토하나", "items": [{"fee_rate_id": chair["id"], "qty": 1}]})
    bt = S.confirm_plan(con, base["planned_date"], {cid: [{"request_id": r1["id"]}]})[0]
    raises(lambda: S.make_bundle(con, bt["batch_id"]), "지문 확인 전", "지문 확인 전인 휴대폰으로는 목록 QR을 만들지 않음")
    S.confirm_device(con, reg["device_id"])
    ok(S.make_bundle(con, bt["batch_id"]), "지문 같음 확인 뒤 목록 QR 만들기")
    fp_other_kex = P.fingerprint(P.pub_raw(ph.pc_sign), P.pub_raw(P.gen_key()), ph.id, P.pub_raw(ph.kex), P.pub_raw(ph.sign))
    ok(len(reg["fingerprint"]) == 9 and fp_other_kex != reg["fingerprint"], "지문 9자리, 등록 QR의 키 합의 키를 바꾸면 지문이 달라짐")
    S.set_device_status(con, reg["device_id"], "LOST")
    raises(lambda: S.register_device(con, ph.code(), "되살리기"), "분실", "분실 처리한 기기는 같은 코드로 다시 등록되지 않음")
    S.set_device_status(con, reg["device_id"], "ACTIVE")

    # 접수번호: 단독 휴대폰에서 옮긴 번호와 겹치지 않게(그날 가장 큰 번호 + 1)
    day = S.today_kst().replace("-", "")
    con.execute("UPDATE request SET receipt_no=? WHERE id=?", (f"DG-{day}-0007", r1["id"]))
    r2 = S.create_request(con, {**base, "name": "검토둘", "phone": "010-0000-9101", "items": [{"fee_rate_id": chair["id"], "qty": 1}]})
    ok(S.get_request(con, r2["id"])["receipt_no"] == f"DG-{day}-0008", "접수번호: 그날 가장 큰 번호 다음 (건수+1이면 겹침)")
    raises(lambda: S.create_request(con, {**base, "name": "x", "phone": "010-0000-9102", "items": [{"fee_rate_id": chair["id"], "qty": "1.5"}]}), "정수", "수량 1.5 → 업무 오류(처리 오류 아님)")

    # 접수 수정: 이미 받은 품목은 접수 때 단가를 그대로(개정 단가로 바뀌지 않음)
    it = S.get_request(con, r2["id"])["items"][0]
    con.execute("UPDATE request_item SET unit_price=4321, amount=4321 WHERE line_id=?", (it["line_id"],))
    S.update_request(con, r2["id"], {**base, "req_ver": RV(con, r2["id"]), "name": "검토둘", "phone": "010-0000-9199", "items": [{"line_id": it["line_id"], "fee_rate_id": chair["id"], "qty": 2}]}, "연락처 변경")
    it2 = S.get_request(con, r2["id"])["items"][0]
    ok(it2["unit_price"] == 4321 and it2["amount"] == 8642, "접수 수정: 받은 품목 단가는 그대로, 수량만 바뀌면 같은 단가로 다시 셈")

    # 수수료 개정 시행일 뒤섞임 막기
    S.ACTOR.set("writer")
    d1 = S.save_fee_draft(con, {"effective_from": "2027-01-01", "confirmed_by": "확인자", "overrides": {chair["id"]: {"price": chair["price"] + 1000, "reason": "가상"}}})
    S.ACTOR.set("checker"); S.approve_fee_revision(con, d1)
    S.ACTOR.set("writer")
    raises(lambda: S.save_fee_draft(con, {"effective_from": "2026-12-01", "confirmed_by": "확인자", "overrides": {ward["id"]: {"price": 99999, "reason": "가상"}}}), "나중 시행",
           "더 나중 시행 개정이 승인돼 있으면 앞선 날짜 개정은 만들 수 없음(단가가 한 달 일찍 적용되는 일 막음)")
    raises(lambda: S.save_fee_draft(con, {"effective_from": "2026-02-30", "confirmed_by": "확인자", "overrides": {}}), "날짜", "없는 날짜(2월 30일) 시행일 거부")
    ok(S.fee_state(con, "2026-12-15")["rev_no"] == 0 and S.fee_state(con, "2027-01-02")["rev_no"] == 1, "시행일 전에는 옛 표, 시행일부터 개정 표")
    S.ACTOR.set("admin")

    # 환불: 수납 기록으로 바로 못 넣음, 결재 뒤 원 납부가 정정되면 지급 막음, 일부 수거 차액 환불 뒤 '부족'으로 안 남음
    S.add_payment(con, r1["id"], {"amount": chair["price"], "method": S.PAY_METHODS[0], "paid_on": "2026-11-04"})
    S.cancel_request(con, r1["id"], "", "RESIDENT")
    case = S.one(con, "SELECT id FROM refund_case WHERE request_id=? AND status='DRAFT'", (r1["id"],))["id"]
    FN.request_refund(con, {"case_id": case})
    S.ACTOR.set("checker"); FN.approve_refund(con, case, True); S.ACTOR.set("admin")
    pay = S.one(con, "SELECT id FROM payment WHERE request_id=? AND kind='PAY'", (r1["id"],))["id"]
    S.void_payment(con, pay, "잘못 기록")
    raises(lambda: FN.pay_refund(con, case, {"method": "현금", "paid_on": "2026-11-05"}), "남은 납부", "결재된 환불이라도 원 납부가 정정되면 지급 막음(초과 환불 방지)")

    # 결과 병합: 팀 없는 기기·옛 갱신본·현장 추가 단가와 음수 수량
    lone = Phone(S.registration_text(con))
    S.register_device(con, lone.code(), "예비폰", confirm=True)
    r3 = S.create_request(con, {**base, "name": "검토셋", "phone": "010-0000-9103", "items": [{"fee_rate_id": chair["id"], "qty": 1}]})
    b3 = S.confirm_plan(con, base["planned_date"], {cid: [{"request_id": r3["id"]}]})[0]
    stop3 = {"request_id": r3["id"], "assign_ver": b3["assign_ver"], "req_ver": 1}
    rr = S.import_result(con, lone.seal({"schema": "dg.result/1", "events": [lone.ev(stop3, "ATTEMPT", {"outcome": "DONE"})]}, P.TYPE_RESULT))
    ok(rr["conflict"] == 1 and S.get_request(con, r3["id"])["status"] != "DONE", "팀에 연결되지 않은 휴대폰의 완료 결과 → 충돌함(반영 안 함)")
    lid = str(uuid.uuid4())
    evs = [ph.ev(stop3, "FIELD_ITEM_ADD", {"line_id": lid, "fee_rate_id": ward["id"], "name": "장롱", "qty": 1, "unit_price": 1, "price_status": "CONFIRMED"}),
           ph.ev(stop3, "ITEM_RESULT", {"line_id": lid, "line_kind": "FIELD", "qty_confirmed": -3, "qty_collected": -3})]
    S.import_result(con, ph.seal({"schema": "dg.result/1", "events": evs}, P.TYPE_RESULT))
    fl = [l for l in S.derive(con, S.one(con, "SELECT * FROM request WHERE id=?", (r3["id"],)))["lines"] if l["line_id"] == lid][0]
    ok(fl["price"] == ward["price"] and fl["qty_confirmed"] == 0, f"현장 추가 품목 단가는 PC 표로({ward['price']:,}원), 음수 수량은 0으로")
    # 같은 파일 안 기록 하나가 DB 제약에 걸려도 나머지는 반영
    r4 = S.create_request(con, {**base, "name": "검토넷", "phone": "010-0000-9104", "items": [{"fee_rate_id": chair["id"], "qty": 1}]})
    b4 = S.confirm_plan(con, base["planned_date"], {cid: [{"request_id": r3["id"]}, {"request_id": r4["id"]}]})[0]
    stop4 = {"request_id": r4["id"], "assign_ver": b4["assign_ver"], "req_ver": 1}
    good = ph.ev(stop4, "ATTEMPT", {"outcome": "NOT_COLLECTED"})
    bad = ph.ev(stop4, "CONTACT", {"result": "NO_ANSWER"}); bad["seq"] = evs[0]["seq"]  # 이미 받은 순번과 겹침(휴대폰 복원)
    broken = ph.ev(stop4, "CONTACT", {"result": "NO_ANSWER"}); broken["seq"] = "망가진 순번"
    rr2 = S.import_result(con, ph.seal({"schema": "dg.result/1", "events": [bad, broken, good]}, P.TYPE_RESULT))
    ok(S.get_request(con, r4["id"])["status"] == "NOT_COLLECTED" and rr2["conflict"] == 1 and rr2.get("release_code"),
       "기록 하나가 실패해도 같은 파일의 다른 결과는 반영, 실패한 기록은 충돌함")
    bev = S.one(con, "SELECT apply_status, warning FROM result_event WHERE event_id=?", (bad["event_id"],))
    ok(rr2.get("seq_reused") == 1 and bev["apply_status"] == "APPLIED" and "재사용" in (bev["warning"] or ""),
       "v0.13: 휴대폰 복원으로 순번이 겹쳐도 event_id가 새것이면 반영하고 경고만(충돌 아님)")
    ok(S.import_result(con, ph.seal({"schema": "dg.result/1", "events": [bad]}, P.TYPE_RESULT))["skipped_duplicate"] == 1, "같은 event_id는 여전히 한 번만")

    # 재방문 약속으로 예정일이 바뀌면 옛 날짜 배정이 풀림
    appt = ph.ev(stop4, "REVISIT_APPT", {"date": "2026-11-27", "start": "10:00", "end": "11:00"}, assign_ver=b4["assign_ver"])
    S.import_result(con, ph.seal({"schema": "dg.result/1", "events": [appt]}, P.TYPE_RESULT))
    ok(not S.one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (r4["id"],)) and S.get_request(con, r4["id"])["planned_date"] == "2026-11-27",
       "재방문 약속으로 예정일이 바뀌면 옛 날짜 배정 해제(새 날짜 배정이 막히지 않게)")

    # 마감: 아직 오지 않은 날은 마감 못 함
    raises(lambda: S.close_day(con, "2099-01-02"), "오지 않은", "미래 날짜 마감 거부")

    # 노선: 같은 건물 묶음 합이 차량보다 크면 묶지 않고 반입지 경유로 나눠 싣기
    apts = [{"id": f"a{i}", "lat": 35.1500, "lon": 126.9300 + 0.00001 * i, "items": [{"name": "장롱", "spec": "120cm 이상 1쪽당", "qty": 2}]} for i in range(5)]
    crew8 = [{"id": "c", "name": "c", "capacity_m3": 8, "shift_start": "09:00", "shift_end": "17:00"}]
    rp = routing.plan(apts, crew8, {"lat": 35.1462, "lon": 126.9233}, time_limit_s=2, unload_site={"lat": 35.16, "lon": 126.95, "label": "가상 반입지"})
    ok(len(rp["routes"][0]["stops"]) == 5 and rp["routes"][0]["unloads"] >= 1, f"공동주택 5건(각 2.8m³, 합 14m³) → 8m³ 차량이 반입지 {rp['routes'][0]['unloads']}회 들러 모두 방문")
    raises(lambda: S.save_crew(con, {"name": "음수팀", "capacity_m3": -5}), "적재 용량", "차량 용량 음수 거부(노선 계산기가 멈추는 일 막음)")

    # CSV 수식 주입 막기
    buf = io.StringIO(); S.safe_csv_writer(buf).writerow(["=HYPERLINK(1)", "-", "-5", "@x", "보통"])
    ok(buf.getvalue().strip() == "'=HYPERLINK(1),-,-5,'@x,보통", "CSV: = + - @ 로 시작하는 글 앞에 ' (숫자·'-'는 그대로)")

    # 변경 이력의 사유 글: 보유기간 파기 때 지워지고, 해시 사슬 검사는 그대로
    S.cancel_request(con, r2["id"], "가상둘 본인 010-1234-5678 요청", "OTHER")
    note = S.one(con, "SELECT note FROM audit_note WHERE request_id=? ORDER BY audit_id DESC LIMIT 1", (r2["id"],))["note"]
    raw = S.one(con, "SELECT detail FROM audit_log WHERE entity='request' AND entity_id=? AND action='CANCEL'", (r2["id"],))["detail"]
    ok("010-1234-5678" in note and "010-1234-5678" not in raw, "사유 글은 변경 이력 본문이 아니라 따로(해시만 사슬에)")
    PV.pseudonymize(con, r2["id"], S.now())
    va = PV.verify_audit(con)
    ok(all(x["note"] is None for x in S.rows(con, "SELECT note FROM audit_note WHERE request_id=?", (r2["id"],))) and va.get("ok", va.get("valid", True)) is not False,
       f"파기하면 사유 글을 지움(변경 이력 사슬 검사는 그대로: {va})")
    con.close()
    print(f"\n통과 {passed}개 (전체 검토 반영 포함)")


def v13():
    """v0.13 PC 서버: 무결성·동시성·재전송 방지·성능(요약 열·색인)·잠금 정책·이전 단계·운영 API."""
    print("\n— v0.13 PC 서버 —")
    import socket
    import threading
    import urllib.error
    import urllib.request
    import zlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from dgpc import finance as FN, ops as OPS, privacy as PV, server as SV
    os.environ["DG_STATE"] = tempfile.mkdtemp(prefix="dg-pc-v13-")
    con = store.connect(create=True)
    S.ensure_pc_keys(con)
    S.ACTOR.set("admin")
    today = S.today_kst()
    fee = {r["name"] + "|" + r["spec"]: r for r in S.FEE_TABLE["rows"]}
    chair, ward = fee["의자|고정식 의자"], fee["장롱|120cm 이상 1쪽당"]
    base = {"phone": "010-0000-9300", "address": "가상시 동구 예시로 13", "planned_date": today, "lat": 35.15, "lon": 126.92}

    # 17. 이전 단계
    ok(con.execute("PRAGMA user_version").fetchone()[0] == store.LATEST and con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2,
       f"새 DB: 구조 버전 {store.LATEST}(PRAGMA user_version), auto_vacuum=INCREMENTAL")
    import sqlcipher3
    mp = Path(tempfile.mkdtemp(prefix="dg-mig-")) / "m.db"
    mc = sqlcipher3.connect(str(mp), isolation_level=None)
    mc.execute("PRAGMA key = 'test-db-key-not-for-production'")
    store._TEST_FAIL_STEP = 2
    try:
        store.run_migrations(mc)
        ok(False, "중간 실패가 나야 함")
    except RuntimeError:
        pass
    store._TEST_FAIL_STEP = None
    ok(mc.execute("PRAGMA user_version").fetchone()[0] == 1 and "fee_final" not in store._cols(mc, "request")
       and not mc.execute("SELECT 1 FROM sqlite_master WHERE name='idem'").fetchone(), "이전 단계 중간에 멈춤 → 그 단계 전체 되돌림(1단계까지만 기록)")
    ok(store.run_migrations(mc) == [2, 3, 4, 5, 6] and "fee_final" in store._cols(mc, "request") and "reduction_round_unit" in store._cols(mc, "request")
       and mc.execute("PRAGMA user_version").fetchone()[0] == store.LATEST == 6, "다시 열면 멈춘 단계부터 다시 실행(2~6단계)")
    # 5단계(주소 위치 자료 식별 번호)·6단계(카카오 이동시간 보관 중단): 4단계까지의 DB에 옛 자료를 넣고 올린다
    m4 = sqlcipher3.connect(str(mp.parent / "m4.db"), isolation_level=None)
    m4.execute("PRAGMA key = 'test-db-key-not-for-production'")
    saved_m = store.MIGRATIONS, store.LATEST
    store.MIGRATIONS, store.LATEST = saved_m[0][:4], 4
    try:
        store.run_migrations(m4)
    finally:
        store.MIGRATIONS, store.LATEST = saved_m
    m4.execute("INSERT INTO addr_point (sigungu, road, main, sub, lat, lon, dong_h, bname, jibun_main) VALUES ('동구','옛로',3,0,35.15,126.92,'가상1동','옛빌라',12)")
    m4.execute("CREATE TABLE travel_cache (origin TEXT NOT NULL, dest TEXT NOT NULL, seconds INTEGER NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (origin, dest))")
    m4.execute("INSERT INTO travel_cache VALUES ('35.15000,126.92000','35.15100,126.92100',120,'2026-11-01T00:00:00+00:00')")
    old_pk = [r[1] for r in m4.execute("PRAGMA table_info(addr_point)").fetchall() if r[5]]
    ok(store.run_migrations(m4) == [5, 6], "4단계 DB → 5·6단계 실행")
    a4 = m4.execute("SELECT sigungu, road, main, lat, dong_h, bname, jibun_main, underground, road_cd, bjd_cd, entr_sn FROM addr_point").fetchall()
    new_pk = [r[1] for r in m4.execute("PRAGMA table_info(addr_point)").fetchall() if r[5]]
    ok(a4 == [("동구", "옛로", 3, 35.15, "가상1동", "옛빌라", 12, 0, None, None, None)] and "underground" in new_pk and "underground" not in old_pk
       and {"ix_addr_point_rcd", "ix_addr_point_bjd"} <= {r[0] for r in m4.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()},
       "이전 단계 5: 주소 위치 자료에 지하여부(기본 키)·도로명코드·법정동코드·출입구일련번호 — 기존 좌표·동·건물명·지번 그대로(지상, 식별 번호는 다시 가져오면 채움)")
    ok(not m4.execute("SELECT 1 FROM sqlite_master WHERE name='travel_cache'").fetchone(), "이전 단계 6: 카카오 이동시간 보관 표(travel_cache)와 남은 좌표 쌍 삭제")
    m4.execute("PRAGMA user_version = 4")
    ok(store.run_migrations(m4) == [5, 6] and m4.execute("SELECT COUNT(*) FROM addr_point").fetchone()[0] == 1, "5·6단계는 여러 번 실행해도 같음")
    m4.close()
    mc.execute("PRAGMA user_version = 99")
    try:
        store.run_migrations(mc)
        ok(False, "새 DB 거부")
    except SystemExit as e:
        ok("새것" in str(e), "DB가 프로그램보다 새 버전이면 열지 않음")
    mc.close()

    # 기기·팀·접수
    ph = Phone(S.registration_text(con))
    reg = S.register_device(con, ph.code(), "v13폰", confirm=True)
    dev = reg["device_id"]
    c1 = S.save_crew(con, {"name": "십삼1팀", "device_id": dev})
    c2 = S.save_crew(con, {"name": "십삼2팀"})
    mk = lambda name, **kw: S.create_request(con, {**base, "name": name, "items": [{"fee_rate_id": chair["id"], "qty": 1}], **kw})["id"]
    ra, rb, rc_ = mk("십삼가"), mk("십삼나", phone="010-0000-9301"), mk("십삼다", phone="010-0000-9302")

    # 5. 겹친 트랜잭션(저장점)
    with S.tx(con):
        con.execute("UPDATE request SET memo='바깥' WHERE id=?", (ra,))
        try:
            with S.tx(con):
                con.execute("UPDATE request SET memo='안쪽' WHERE id=?", (ra,))
                raise S.UserError("안쪽 실패")
        except S.UserError:
            pass
    ok(S.one(con, "SELECT memo FROM request WHERE id=?", (ra,))["memo"] == "바깥", "tx 겹쳐 쓰기: 안쪽 실패는 그 부분만 되돌림(SAVEPOINT)")

    # 3. 낙관적 동시성
    upd = lambda rid, **kw: S.update_request(con, rid, {**base, "name": "십삼가", "items": [{"line_id": S.get_request(con, rid)["items"][0]["line_id"],
                                                                                          "fee_rate_id": chair["id"], "qty": 1}], **kw}, "주민 요청")
    raises(lambda: upd(ra), "req_ver", "수정에 req_ver 없으면 거부")
    r0 = RV(con, ra)
    ok(upd(ra, req_ver=r0)["req_ver"] == r0 + 1, "열었던 버전과 같으면 수정, 새 버전 돌려줌")
    try:
        upd(ra, req_ver=r0)
        ok(False, "옛 버전 수정 거부")
    except S.StaleError as e:
        ok("다른 곳에서 바뀌었습니다" in str(e) and e.info["req_ver"] == r0 + 1, f"옛 버전으로 수정 → StaleError(409), 지금 버전 {e.info['req_ver']}")
    raises(lambda: S.cancel_request(con, rb, "", "RESIDENT", req_ver=RV(con, rb) - 1 or 99), "다른 곳에서", "취소도 옛 버전이면 거부")
    raises(lambda: FN.set_reduction(con, rb, {"reduction_code": "BASIC", "reduction_proof": "서류 확인", "reduction_checker": "가상", "req_ver": 77}),
           "다른 곳에서", "감면 변경도 옛 버전이면 거부")
    ok(S.one(con, "SELECT status FROM request WHERE id=?", (rb,))["status"] != "CANCELLED", "거부된 취소는 반영 안 됨")

    # 4. 재전송 방지
    n0 = S.one(con, "SELECT COUNT(*) AS n FROM request")["n"]
    a1 = S.idempotent(con, "kim", "requests", "k-1", lambda: S.create_request(con, {**base, "name": "두번누름", "items": [{"fee_rate_id": chair["id"], "qty": 1}]}))
    a2 = S.idempotent(con, "kim", "requests", "k-1", lambda: S.create_request(con, {**base, "name": "두번누름", "items": [{"fee_rate_id": chair["id"], "qty": 1}]}))
    ok(a1["id"] == a2["id"] and S.one(con, "SELECT COUNT(*) AS n FROM request")["n"] == n0 + 1, "같은 client_req_id 두 번 → 접수 1건, 같은 응답")
    try:
        S.idempotent(con, "kim", "payments", "k-2", lambda: S.add_payment(con, ra, {"amount": "abc", "method": "현장 카드"}))
    except S.UserError:
        pass
    ok(not S.one(con, "SELECT 1 AS x FROM idem WHERE key='k-2'"), "처리 중 오류면 재전송 기록도 남지 않음")

    # 26. 수납 입력 검사·감면 끝수
    raises(lambda: S.add_payment(con, ra, {"amount": "만원", "method": "현장 카드"}), "숫자", "납부 금액이 숫자가 아니면 업무 오류(500 아님)")
    raises(lambda: S.add_payment(con, ra, {"amount": 1000, "method": "현장 카드", "paid_on": "2026-13-45"}), "날짜", "없는 날짜 거부")
    ok(S.add_payment(con, ra, {"amount": "1,000원", "method": "현장 카드"})["paid"] == 1000, "'1,000원'처럼 적어도 금액으로 받음")
    ok(FN.fee_summary({"reduction_code": "X", "reduction_rate": 50}, 3015)["reduction"] == 1507
       and FN.fee_summary({"reduction_code": "X", "reduction_rate": 50}, 3015, 10)["reduction"] == 1500, "감면액 끝수: 기본 1원 미만 버림(이전과 같음), 10원 설정 시 10원 미만 버림")
    raises(lambda: S.save_settings(con, {"reduction_round_unit": 7}), "1원 또는 10원", "끝수 단위는 1·10원만")
    S.save_settings(con, {"reduction_round_unit": 10})
    ok(S.settings(con)["reduction_round_unit"] == 10 and S.settings(con)["reduction_round_units"] == [1, 10], "끝수 단위 설정·선택지(bootstrap settings)")
    S.save_settings(con, {"reduction_round_unit": 1})
    # 끝수 단위는 감면을 적용할 때 접수에 저장: 나중에 설정을 바꿔도 이미 감면한 접수의 부과 금액·수납 상태는 그대로
    S.save_settings(con, {"reductions": FN.reductions(con) + [{"code": "T25", "label": "시험25", "rate": 25, "note": "", "active": True}]})
    wall = next(r for r in S.FEE_TABLE["rows"] if r["status"] == "CONFIRMED" and r["price"] % 1000 == 300)  # 3,300원 × 25% = 825
    red25 = {"reduction_code": "T25", "reduction_proof": "서류 확인", "reduction_checker": "가상"}
    S.save_settings(con, {"reduction_round_unit": 10})
    ru1 = S.create_request(con, {**base, "name": "끝수가", "phone": "010-0000-9311", "items": [{"fee_rate_id": wall["id"], "qty": 1}], **red25})["id"]
    lev = lambda rid: (S.payment_status(con, rid)["levied"], S.one(con, "SELECT levied FROM request WHERE id=?", (rid,))["levied"])
    ok(S.one(con, "SELECT reduction_round_unit AS u FROM request WHERE id=?", (ru1,))["u"] == 10 and lev(ru1) == (2480, 2480),
       "끝수 10원일 때 감면 접수: 접수에 단위 10 저장, 3,300원 25% → 감면 820 → 부과 2,480(요약 열 같음)")
    S.add_payment(con, ru1, {"amount": 2480, "method": "현장 카드"})
    S.save_settings(con, {"reduction_round_unit": 1})
    S.refresh_request_summary(con, [ru1])
    ok(lev(ru1) == (2480, 2480) and S.payment_status(con, ru1)["status"] == "PAID" and S.payment_status(con, ru1)["reduction_round_unit"] == 10,
       "설정을 1원으로 바꿔도 이미 감면한 접수는 그대로(부과 2,480, 납부 확인 유지)")
    ru2 = S.create_request(con, {**base, "name": "끝수나", "phone": "010-0000-9312", "items": [{"fee_rate_id": wall["id"], "qty": 1}], **red25})["id"]
    ok(S.one(con, "SELECT reduction_round_unit AS u FROM request WHERE id=?", (ru2,))["u"] == 1 and lev(ru2) == (2475, 2475), "새 감면 접수는 새 단위(1원: 감면 825 → 부과 2,475)")
    from dgpc import assign as AS
    ok(AS.request_round_unit(S.one(con, "SELECT * FROM request WHERE id=?", (ru1,))) == 10, "휴대폰 목록에 실을 단위도 접수에 저장된 값")
    from dgpc import reports as RP_
    csv_ = [line for _r, line in RP_._export_rows(con, [S.one(con, "SELECT * FROM request WHERE id=?", (x,)) for x in (ru1, ru2)])]
    ok(any(str(c_) in ("2480", "2,480") for c_ in csv_[0]) and any(str(c_) in ("2475", "2,475") for c_ in csv_[1]), "CSV(내보내기)도 접수에 저장된 단위로 부과 금액")
    S.save_settings(con, {"reduction_round_unit": 10})
    FN.set_reduction(con, ru2, {**red25, "req_ver": RV(con, ru2)})
    ok(lev(ru2) == (2480, 2480), "감면을 다시 적용·바꾸면 그때의 설정값으로 저장")
    FN.set_reduction(con, ru2, {"reduction_code": "", "req_ver": RV(con, ru2)})
    ok(S.one(con, "SELECT reduction_round_unit AS u FROM request WHERE id=?", (ru2,))["u"] is None and lev(ru2) == (3300, 3300), "감면 해제 → 단위도 지움")
    # 3단계 이전: 기존 감면 접수는 그때의 설정값으로 채움(지금까지와 같은 금액)
    con.execute("UPDATE request SET reduction_round_unit=NULL WHERE id=?", (ru1,))
    con.execute("PRAGMA user_version = 2")
    ok(store.run_migrations(con) == [3, 4, 5, 6] and S.one(con, "SELECT reduction_round_unit AS u FROM request WHERE id=?", (ru1,))["u"] == 10
       and S.one(con, "SELECT reduction_round_unit AS u FROM request WHERE id=?", (ru2,))["u"] is None and lev(ru1) == (2480, 2480),
       "이전 단계 3: 기존 감면 접수에 지금 설정값(10) 채움, 감면 없는 접수는 비움")
    S.save_settings(con, {"reduction_round_unit": 1})
    raises(lambda: FN.request_refund(con, {"request_id": ra, "cause": "MANUAL", "amount": "x", "note": "가상"}), "환불 금액", "환불 금액 숫자 아님 → 업무 오류")

    # 5·6. 취소 → 같은 트랜잭션 안에서 환불 건
    try:
        with S.tx(con):
            S.cancel_request(con, ra, "", "RESIDENT")
            raise S.UserError("되돌리기")
    except S.UserError:
        pass
    ok(S.one(con, "SELECT status FROM request WHERE id=?", (ra,))["status"] != "CANCELLED" and not S.one(con, "SELECT 1 AS x FROM refund_case WHERE request_id=?", (ra,)),
       "취소와 자동 환불 건은 한 트랜잭션(되돌리면 둘 다 없음)")
    S.cancel_request(con, ra, "", "RESIDENT", req_ver=RV(con, ra))
    rf = S.one(con, "SELECT status, amount, cause FROM refund_case WHERE request_id=?", (ra,))
    ok(rf and rf["status"] == "DRAFT" and rf["amount"] == 1000 and rf["cause"] == "CANCELLED", "납부 뒤 취소 → 별도 호출 없이 자동 환불 건(신청 전) 생김")
    con.execute("DELETE FROM refund_case WHERE request_id=?", (ra,))  # 어긋난 상태 흉내
    ok(FN.sync_refunds(con, [rb])["created"] == 0 and not S.one(con, "SELECT 1 AS x FROM refund_case WHERE request_id=?", (ra,)), "sync_refunds(rids): 준 접수만 본다")
    job = OPS.JOBS.run_now({"actor": "admin", "client": "local", "kind": "refund_sync", "progress": 0}, OPS.job_fn("refund_sync", con, None))
    ok(job["status"] == "DONE" and job["result"]["created"] == 1 and S.one(con, "SELECT 1 AS x FROM refund_case WHERE request_id=?", (ra,)),
       "전체 점검은 관리자 단추·야간 작업(작업 실행기)에서만 → 빠진 환불 건 다시 생김")

    # 12. 요약 열 = derive()
    def summary_matches():
        bad = []
        for r in S.rows(con, "SELECT * FROM request"):
            ps = S.payment_status(con, r["id"])
            act = S.one(con, "SELECT b.crew_id FROM active_assignment a JOIN assignment_batch b ON b.id=a.batch_id WHERE a.request_id=?", (r["id"],))
            want = (S.derive(con, r)["fee"]["final"], ps["levied"], ps["paid"], ps["status"], act["crew_id"] if act else None, S.chosung_key(r["name"], r["address"]))
            if (r["fee_final"], r["levied"], r["paid_total"], r["pay_status"], r["active_crew_id"], r["chosung"]) != want:
                bad.append((r["receipt_no"], want))
        return bad
    ok(not summary_matches(), "요약 열(fee_final·levied·paid_total·pay_status·active_crew_id·chosung) = derive()·수납 계산")

    # 23. 배정 부분 수정
    rd_ = mk("십삼라", phone="010-0000-9303")
    S.confirm_plan(con, today, {c1: [{"request_id": rb, "eta": "09:00"}, {"request_id": rc_, "eta": "09:30"}]})
    cur = {b["crew_id"]: b for b in S.current_assignment(con, today)}
    ok([s_["request_id"] for s_ in cur[c1]["stops"]] == [rb, rc_] and cur[c1]["assign_ver"] == 1, "확정 배정 불러오기(팀·순서·갱신본)")
    e1 = S.edit_assignment(con, today, "insert", {"crew_id": c1, "request_id": rd_, "assign_ver": {c1: 1}})
    cur = {b["crew_id"]: b for b in S.current_assignment(con, today)}
    ok([s_["request_id"] for s_ in cur[c1]["stops"]] == [rb, rc_, rd_] and cur[c1]["assign_ver"] == 2 and cur[c1]["stops"][0]["eta"] == "09:00"
       and e1["resend_needed"] == [c1], "팀 끝에 넣기: 다시 계산 없이 새 갱신본(2), 기존 순서·도착 예정 유지")
    try:
        S.edit_assignment(con, today, "remove", {"request_id": rd_, "assign_ver": {c1: 1}})
        ok(False, "옛 갱신본 거부")
    except S.StaleError as e:
        ok(e.info["assign_ver"] == 2, "옛 갱신본을 보고 고치면 거부(409, 지금 갱신본 알려 줌)")
    S.edit_assignment(con, today, "reorder", {"crew_id": c1, "order": [rd_, rb, rc_]})
    raises(lambda: S.edit_assignment(con, today, "reorder", {"crew_id": c1, "order": [rd_, rb]}), "순서 목록", "순서 바꾸기는 같은 접수 모두여야 함")
    S.edit_assignment(con, today, "move", {"request_id": rc_, "to_crew_id": c2, "position": 1})
    S.edit_assignment(con, today, "remove", {"request_id": rd_})
    cur = {b["crew_id"]: b for b in S.current_assignment(con, today)}
    ok([s_["request_id"] for s_ in cur[c1]["stops"]] == [rb] and [s_["request_id"] for s_ in cur[c2]["stops"]] == [rc_]
       and S.one(con, "SELECT status FROM request WHERE id=?", (rd_,))["status"] == "RECEIVED" and not S.one(con, "SELECT 1 AS x FROM active_assignment WHERE request_id=?", (rd_,))
       and S.one(con, "SELECT active_crew_id FROM request WHERE id=?", (rc_,))["active_crew_id"] == c2,
       "순서 바꾸기·다른 팀으로 옮기기·빼기(뺀 건은 배정 풀림), 요약 열의 현재 팀도 따라감")
    raises(lambda: S.edit_assignment(con, today, "insert", {"crew_id": c2, "request_id": rb}), "이미 이날", "이미 배정된 접수 넣기는 거부(옮기기를 쓰라고)")

    # 8. 휴대폰에 '받은 것' 알리기 + 1·24. 충돌함 정리
    b1 = cur[c1]
    stop_b = {"request_id": rb, "assign_ver": b1["assign_ver"], "req_ver": RV(con, rb)}
    stop_c = {"request_id": rc_, "assign_ver": 1, "req_ver": RV(con, rc_)}  # 2팀으로 옮긴 접수(1팀 휴대폰의 옛 목록)
    lid = str(uuid.uuid4())
    pid_ = str(uuid.uuid4())
    pic = b"\xff\xd8v13-photo"
    evs = [ph.ev(stop_b, "PHOTO_META", {"photo_id": pid_, "phase": "AFTER", "sha256": hashlib.sha256(pic).hexdigest(), "bytes": len(pic)}),
           ph.ev(stop_c, "FIELD_ITEM_ADD", {"line_id": lid, "fee_rate_id": ward["id"], "name": "장롱", "qty": 1, "unit_price": 1, "price_status": "CONFIRMED"}),
           ph.ev(stop_c, "ITEM_RESULT", {"line_id": lid, "line_kind": "FIELD", "qty_confirmed": "abc", "qty_collected": "1"})]
    imp = S.import_result(con, ph.seal({"schema": "dg.result/1", "events": evs}, P.TYPE_RESULT))
    stored = {e["kind"]: json.loads(e["body_json"]) for e in S.rows(con, "SELECT kind, body_json FROM result_event WHERE import_id=? AND apply_status='CONFLICT'", (imp["import_id"],))}
    ok(imp["conflict"] == 2 and stored["FIELD_ITEM_ADD"]["unit_price"] == ward["price"] and stored["ITEM_RESULT"]["qty_confirmed"] == 0,
       "충돌함에 들어가는 현장 품목도 정리해서 저장(단가 = PC 표, 수량 0~9999)")
    con.execute("UPDATE result_event SET body_json=? WHERE event_id=?", (json.dumps({"line_id": lid, "fee_rate_id": ward["id"], "name": "장롱", "qty": 1,
                                                                                     "unit_price": 1, "price_status": "CONFIRMED"}), evs[1]["event_id"]))  # 이전 버전이 저장한 정리 안 된 기록 흉내
    lc13 = [c for c in S.list_conflicts(con) if c["request_id"] == rc_]
    ok(len(lc13) == 2 and all(c["import_id"] == imp["import_id"] and "warning" in c for c in lc13),
       "충돌함 목록에 가져오기 번호(import_id)·경고(warning) — 화면이 가져오기별 [모두 반영/거부]")
    bulk = S.resolve_conflicts_bulk(con, "APPLY", "팀장 확인: 실제 수거", request_id=rc_)
    fl = [l for l in S.get_request(con, rc_)["state"]["lines"] if l["line_id"] == lid][0]
    ok(bulk["resolved"] == 2 and fl["price"] == ward["price"] and fl["qty_confirmed"] == 0, f"충돌함 [반영]도 직접 가져오기와 같게 정리: 휴대폰 단가 1원 무시 → PC 표 {ward['price']:,}원")
    raises(lambda: S.resolve_conflicts_bulk(con, "APPLY", "", request_id=rc_), "사유", "일괄 처리도 사유 필수")
    con.execute("""INSERT INTO result_event (event_id, import_id, device_id, device_seq, request_id, assign_ver, kind, body_json, apply_status)
        VALUES (?,?,?,?,?,?,?,?,'APPLIED')""", (str(uuid.uuid4()), imp["import_id"], dev, 9999, rc_, 1, "ITEM_RESULT", json.dumps({"line_id": lid, "qty_confirmed": "x", "qty_collected": None})))
    g = S.get_request(con, rc_)
    ok(g["state"]["lines"][-1]["qty_confirmed"] == 0 and FN.sync_refunds(con) is not None and S.payment_status(con, rc_), "숫자 아닌 수량이 들어 있어도 상세·환불 점검이 멈추지 않음")
    rel = S.release_info(con, dev)
    ok(rel["seq"] == ph.seq and rel["ph"] == [], f"release_info: 1..{rel['seq']} 빠짐없이 받음(중간이 빈 9999번은 넘지 않음), 아직 회수 사진 없음")
    con.execute("DELETE FROM result_event WHERE device_seq=9999")
    s0 = ph.seq
    gap = ph.ev(stop_b, "CONTACT", {"result": "REACHED"}); gap["seq"] = s0 + 2  # s0+1 이 빠진 채 더 큰 번호만 옴
    S.import_result(con, ph.seal({"schema": "dg.result/1", "events": [gap]}, P.TYPE_RESULT))
    ok(S.release_info(con, dev)["seq"] == s0, "중간 번호가 빠지면 rel.seq는 빠진 번호 앞에서 멈춤(휴대폰이 못 받은 기록을 지우지 않게)")
    fill = ph.ev(stop_b, "CONTACT", {"result": "REACHED"}); fill["seq"] = s0 + 1
    S.import_result(con, ph.seal({"schema": "dg.result/1", "events": [fill]}, P.TYPE_RESULT))
    ok(S.release_info(con, dev)["seq"] == s0 + 2 == ph.seq, f"빠진 번호가 들어오면 이어서 {s0 + 2}까지")
    pr = S.import_photos(con, ph.seal({"schema": "dg.photos/1", "photos": [{"photo_id": pid_, "request_id": rb, "phase": "AFTER", "sha256": hashlib.sha256(pic).hexdigest(),
                                                                         "data": base64.b64encode(pic).decode()}]}, S.TYPE_PHOTOS))
    ok(S.release_info(con, dev)["ph"] == [pid_], "회수한 사진 ID가 다음 목록의 rel.ph에 실림")
    recent = OPS.recent_imports(con)
    rr = {x["id"]: x for x in recent}
    ok(rr[imp["import_id"]]["release_code"] == imp["release_code"] and rr[pr["import_id"]]["photo_code"] == pr["release_code"]
       and not rr[imp["import_id"]]["phone_confirmed"] and rr[imp["import_id"]]["crew"] == "십삼1팀", "최근 가져오기: 팀·확인 코드(결과·사진)·휴대폰 확인 전")
    bun = S.make_bundle(con, b1["batch_id"])
    tb = S.one(con, "SELECT rel_seq, rel_json FROM transfer_bundle WHERE id=?", (bun["bundle_id"],))
    ok(tb["rel_seq"] == ph.seq and json.loads(tb["rel_json"])["ph"] == [pid_], "목록 묶음에 rel(받은 순번·사진) 저장")
    _, plr, receipt = ph.receive(bun["frames"])
    ok(plr.get("rel") == {"seq": ph.seq, "ph": [pid_]}, "휴대폰이 받은 목록(서명된 봉투)에 rel이 실려 옴")
    S.confirm_receipt(con, bun["bundle_id"], "r-" + receipt[:4] + " " + receipt[4:])
    ok(S.one(con, "SELECT receipt_confirmed_at FROM transfer_bundle WHERE id=?", (bun["bundle_id"],))["receipt_confirmed_at"], "수신 코드는 화면 앞 글자 'R-'·띄어쓰기를 붙여 넣어도 받음")
    rr = {x["id"]: x for x in OPS.recent_imports(con)}
    ok(S.release_info(con, dev)["ph"] == [] and rr[imp["import_id"]]["phone_confirmed"] and rr[imp["import_id"]]["phone_confirmed_by"] == "DAYLIST",
       "휴대폰이 그 목록을 받았다고 확인 → 사진은 알린 것으로, 결과 가져오기는 '휴대폰 확인'으로")
    OPS.confirm_import(con, pr["import_id"])
    ok({x["id"]: x for x in OPS.recent_imports(con)}[pr["import_id"]]["phone_confirmed_by"] == "MANUAL", "담당자가 '휴대폰에 입력함' 표시")

    # 8-1. 기록 세대(gen): 오래된 백업으로 복원한 휴대폰이 PC가 본 번호를 다시 써도 '받음'으로 알리지 않음 (검토 지적)
    gA, gB, gC, gD, gE = "aaaa0001", "bbbb0002", "cccc0003", "dddd0004", "eeee0005"
    n0 = ph.seq
    legacy0 = S.release_info(con, dev)["seq"]

    def gev(seq, gen, **kw):
        e = ph.ev(stop_b, "CONTACT", {"result": "REACHED"}); e["seq"] = seq
        if gen:
            e["gen"] = gen
        return {**e, **kw}

    def send(events, gens=None):
        body = {"schema": "dg.result/1", "events": events, **({"gens": gens} if gens is not None else {})}
        return S.import_result(con, ph.seal(body, P.TYPE_RESULT))
    a_evs = [gev(n0 + i, gA) for i in range(1, 21)]  # 세대 A: n0+1 .. n0+20 (PC가 모두 받음)
    send(a_evs[:10], [[gA, n0 + 1]]); send(a_evs[10:], [[gA, n0 + 1]])
    r_ = S.release_info(con, dev)
    ok(r_["seq"] == legacy0 and r_.get("g") == [[gA, n0 + 20]] and S.one(con, "SELECT gen FROM result_event WHERE event_id=?", (a_evs[0]["event_id"],))["gen"] == gA,
       "세대 A 기록 20건: rel.g = [[A, 끝 번호]], 세대 없는(이전 앱) rel.seq는 그대로, 기록에 세대 저장(result_event.gen)")
    pre_bun = S.make_bundle(con, b1["batch_id"])  # 복원 전 목록(A까지): 이 목록 수신 확인이 뒤 가져오기의 '휴대폰 확인'이 되면 안 됨
    # 휴대폰: n0+10까지 든 백업으로 복원 → 새 세대 B(첫 번호 n0+11) → n0+11에 새 기록. 위 rel(A: n0+20)로는 반영되면 안 됨(휴대폰 쪽 시험은 interop.mjs)
    b_ev = gev(n0 + 11, gB)
    rb_ = send([b_ev], [[gB, n0 + 11]])
    r_ = S.release_info(con, dev)
    ok(rb_.get("seq_reused") == 1 and r_.get("g") == [[gB, n0 + 11], [gA, n0 + 20]],
       "복원 뒤 세대 B의 n0+11(PC가 A로 본 번호): 받고 순번 재사용 경고, rel.g에 [B, n0+11] 추가(최근 세대 먼저) — A 번호와 섞이지 않음")
    S.confirm_receipt(con, pre_bun["bundle_id"], ph.receive(pre_bun["frames"])[2])
    ok(not {x["id"]: x for x in OPS.recent_imports(con)}[rb_["import_id"]]["phone_confirmed"],
       "가져오기 전에 만든 목록(rel에 A만)의 수신 확인은 세대 B 가져오기의 '휴대폰 확인'이 아님")
    post_bun = S.make_bundle(con, b1["batch_id"])
    _, plg, rcg = ph.receive(post_bun["frames"])
    ok(plg["rel"]["g"] == [[gB, n0 + 11], [gA, n0 + 20]], "휴대폰이 받은 목록(서명된 봉투)에 rel.g가 실려 옴")
    S.confirm_receipt(con, post_bun["bundle_id"], rcg)
    ok({x["id"]: x for x in OPS.recent_imports(con)}[rb_["import_id"]]["phone_confirmed_by"] == "DAYLIST", "그 뒤 목록(rel.g에 B)을 받았다고 확인 → 세대 B 가져오기는 '휴대폰 확인'")
    # 첫 번호가 빠지면 그 세대는 싣지 않음(머리 gens로 첫 번호를 앎) → 첫 번호가 들어오면 이어서
    c1_, c2_ = gev(n0 + 30, gC), gev(n0 + 31, gC)
    send([c2_], [[gC, n0 + 30]])
    ok(gC not in [x[0] for x in S.release_info(con, dev).get("g", [])], "세대 C 첫 번호(n0+30)를 못 받으면 C는 rel.g에 없음(빠진 첫 기록을 받은 것으로 오해하지 않음)")
    send([c1_], [[gC, n0 + 30]])
    ok([gC, n0 + 31] in S.release_info(con, dev)["g"], "첫 번호가 들어오면 C: n0+30..n0+31")
    # 머리(gens)가 없으면 받은 가장 작은 번호부터, 이미 받은 기록(event_id 같음)을 새 세대·번호로 옮겨 보내도 '받음'
    send([gev(n0 + 40, gD)])
    moved_ = {**a_evs[19], "seq": n0 + 41, "gen": gE}
    rm_ = send([moved_, gev(n0 + 42, gE)], [[gE, n0 + 41]])
    gl = S.release_info(con, dev)["g"]
    ok(rm_["skipped_duplicate"] == 1 and [gE, n0 + 42] in gl and len(gl) == 4 and gA not in [x[0] for x in gl],
       "이미 받은 기록을 복원 때 옮긴 번호(세대 E 첫 번호)도 받은 번호로 셈 → E: n0+41..n0+42, rel.g는 최근 세대 4개만")
    ok(P.norm_rel({"seq": 1, "ph": [], "g": [[gA, 3], [gB, 0], [gA, 5]]}) == {"seq": 1, "ph": [], "g": [[gA, 5], [gB, 0]]}
       and all("g" not in P.norm_rel({"seq": 1, "g": g_}) for g_ in ([["AAAA0001", 1]], [[gA, -1]], [[gA, True]], [[gA]], "x", [[gA, 1]] * 17, {"a": 1}))
       and P.norm_rel({"seq": 1, "g": [[gA, "1"]]}) == {"seq": 1, "ph": []} and P.norm_gen("abcd") is None and P.norm_gen(gA) == gA,
       "dgproto rel.g 검사: 같은 세대는 큰 번호, 틀린 형식(대문자·음수·참거짓·짝 아님·16개 초과)이면 g만 뺌")
    # 4단계 이전: 이전 기록의 받은 번호를 result_seq(세대 '')로 채움 → rel.seq 그대로
    legacy_now = S.release_info(con, dev)["seq"]
    con.execute("DELETE FROM result_seq WHERE gen=''")
    con.execute("PRAGMA user_version = 3")
    ok(store.run_migrations(con) == [4, 5, 6] and S.release_info(con, dev)["seq"] == legacy_now == legacy0, "이전 단계 4: 이전 기록 순번을 세대 ''로 채움(rel.seq 같음)")

    # 7. 완료일 = 휴대폰 최종 결과 시각
    fut = ph.ev(stop_b, "ATTEMPT", {"outcome": "DONE"})
    fut["device_ts"] = "2099-01-01T00:00:00Z"
    S.import_result(con, ph.seal({"schema": "dg.result/1", "events": [fut]}, P.TYPE_RESULT))
    done_at = S.one(con, "SELECT completed_at FROM request WHERE id=?", (rb,))["completed_at"]
    ok(done_at[:10] in (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"), (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)).strftime("%Y-%m-%d"))
       and done_at < "2099", f"미래 시각(휴대폰 시계 오류)은 무시하고 지금으로 → {done_at}")
    re_ = mk("십삼마", phone="010-0000-9304")
    S.edit_assignment(con, today, "insert", {"crew_id": c1, "request_id": re_})
    v_ = S.current_assignment(con, today)
    st_e = {"request_id": re_, "assign_ver": [b for b in v_ if b["crew_id"] == c1][0]["assign_ver"], "req_ver": 1}
    S.import_result(con, ph.seal({"schema": "dg.result/1", "events": [ph.ev(st_e, "ATTEMPT", {"outcome": "NOT_COLLECTED"})]}, P.TYPE_RESULT))
    ev_ts = S.one(con, "SELECT device_ts FROM result_event WHERE request_id=? AND kind='ATTEMPT'", (re_,))["device_ts"]
    ok(S.one(con, "SELECT completed_at FROM request WHERE id=?", (re_,))["completed_at"] == ev_ts, f"완료일 = 최종 결과(ATTEMPT) 기기 시각 {ev_ts}")

    # 9. 마감: 목록 보냈는데 결과 안 온 팀 경고(막지 않음)
    rf_ = mk("십삼바", phone="010-0000-9305")
    S.edit_assignment(con, today, "insert", {"crew_id": c1, "request_id": rf_})
    bun2 = S.make_bundle(con, [b for b in S.current_assignment(con, today) if b["crew_id"] == c1][0]["batch_id"])
    cs = S.day_summary(con, today)
    ok([x["crew"] for x in cs["crews_no_result"]] == ["십삼1팀"], "마감 점검: 목록을 보냈는데 그 뒤 결과 파일이 없는 팀")
    cl = S.close_day(con, today, "가상 마감")
    ok(cl["seq"] == 1 and any("십삼1팀" in w for w in cl["warnings"]), "마감은 막지 않고 경고만")
    _, _, rcpt2 = ph.receive(bun2["frames"])

    # 21. 처리할 일 · 빠른 조회
    ro = mk("지난일", phone="010-0000-9306", planned_date="2026-10-01")
    rn = mk("날짜없음", phone="010-0000-9307", planned_date="")
    pend = {x["key"]: x for x in OPS.pending(con)["items"]}
    ok(pend["close_approve"]["count"] == 1 and pend["close_approve"]["oldest"] == today and pend["overdue"]["count"] >= 1 and pend["overdue"]["oldest"] == "2026-10-01"
       and pend["nodate"]["count"] >= 1 and pend["no_result_crews"]["count"] == 1 and pend["unclosed_days"]["oldest"] == "2026-10-01",
       "처리할 일: 마감 확인 대기·지난 예정일 미처리·예정일 없음·결과 미회수 팀·마감 안 한 지난 날짜(가장 오래된 날)")
    sr = S.search_requests(con, {"quick": "OVERDUE"})
    ok([x["id"] for x in sr["rows"]] == [ro] and sr["counts"]["NODATE"] >= 1 and rn in [x["id"] for x in S.search_requests(con, {"quick": "NODATE"})["rows"]],
       "빠른 조회 OVERDUE(지난 예정일·최종 결과 없음)·NODATE(예정일 없음)")
    ok([x["receipt_no"] for x in S.search_requests(con, {"q": "ㅈㄴㅇ"})["rows"]] == [S.one(con, "SELECT receipt_no FROM request WHERE id=?", (ro,))["receipt_no"]],
       "초성 검색은 저장한 초성 열로(SQL)")
    for _ in range(3):
        mk("쪽나눔", phone="010-0000-9308", planned_date="2026-12-01")
    pg = S.search_requests(con, {"date": "2026-12-01", "size": 2, "page": 2})
    ok(pg["total"] == 3 and pg["pages"] == 2 and len(pg["rows"]) == 1 and pg["counts"]["ALL"] == 3, "목록 쪽 나눔·건수는 SQL(LIMIT/OFFSET·COUNT)")

    # 22. 운영 준비 점검
    rd1 = {x["key"]: x for x in OPS.readiness(con)["items"]}
    ok(not rd1["depot"]["ok"] and not rd1["devices"]["ok"] and not rd1["backup_key"]["ok"] and not rd1["contact"]["ok"] and rd1["admin_password"]["ok"] is False
       and len(rd1) == 10 and not rd1["fee"]["ok"], "운영 준비: 차고지 기본값·팀2 휴대폰 없음·백업 키 없음·문의처 기본 문구 → 고칠 곳 표시")
    S.save_settings(con, {"depot": {"lat": 35.14, "lon": 126.92, "label": "가상 차고지"}, "contact_info": "가상 청소과 000-0000"})
    rd2 = {x["key"]: x for x in OPS.readiness(con)["items"]}
    ok(rd2["depot"]["ok"] and rd2["contact"]["ok"] and rd2["depot"]["fix"] == "settings/route", "설정하면 그 항목 통과")

    # 25. 주민 안내 문구
    nt = S.notice_text(con, rb)
    ok(S.one(con, "SELECT receipt_no FROM request WHERE id=?", (rb,))["receipt_no"] in nt and "부과 금액" in nt and "납부 방법" in nt and "가상 청소과" in nt
       and "십삼나" not in nt and "9301" not in nt, "주민 안내 문구: 접수번호·배출일·부과 금액·납부 방법·문의처, 이름·연락처 없음")

    # 11. 색인이 실제 조회에 쓰임
    def plan_uses(sql, args, ix):
        return ix in " ".join(str(r[-1]) for r in con.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall())
    checks = [("SELECT * FROM photo WHERE request_id=?", ("x",), "ix_photo_req"),
              ("SELECT 1 FROM assignment_stop s WHERE s.request_id=?", ("x",), "ix_stop_req"),
              ("SELECT * FROM payment p WHERE p.void_reason IS NULL AND p.paid_on BETWEEN ? AND ?", ("a", "b"), "ix_payment_paid_on"),
              ("SELECT id FROM audit_log WHERE after_close=? AND entity IN ('request','payment') ORDER BY id", ("2026-11-01",), "ix_audit_after_close"),
              ("SELECT entity_id FROM audit_log WHERE entity='request' AND action='CARRY' AND at >= ?", ("a",), "ix_audit_eai"),
              ("SELECT MAX(device_seq) FROM result_event WHERE device_id=?", ("x",), "ix_event_dev_seq"),
              ("SELECT * FROM request WHERE received_at >= ? AND received_at < ?", ("a", "b"), "ix_request_received"),
              ("SELECT 1 FROM transfer_bundle WHERE batch_id=?", ("x",), "ix_bundle_batch"),
              ("SELECT * FROM access_log WHERE actor=? ORDER BY id DESC", ("x",), "ix_access_actor"),
              ("SELECT * FROM unload_log WHERE work_date=?", ("x",), "ix_unload_date"),
              ("SELECT * FROM result_event WHERE import_id=?", ("x",), "ix_event_import")]
    miss = [ix for sql, a, ix in checks if not plan_uses(sql, a, ix)]
    ok(not miss, f"색인 {len(checks)}개가 실제 조회 계획에 쓰임 {miss}")

    # 10은 v12_review. 2. 단독 휴대폰 두 대(같은 날)
    def make_dgb(obj, pw):
        salt, iv = os.urandom(16), os.urandom(12)
        co = zlib.compressobj(9, zlib.DEFLATED, -15); plain = co.compress(json.dumps(obj).encode()) + co.flush()
        key = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 600000, 32)
        return b"DGB1" + salt + iv + AESGCM(key).encrypt(iv, plain, b"DGB1")
    first_no = S.one(con, "SELECT receipt_no FROM request ORDER BY receipt_no LIMIT 1")["receipt_no"]
    def solo(name, extra=()):
        rid, l_ = str(uuid.uuid4()), str(uuid.uuid4())
        r = {"request_id": rid, "receipt_no": first_no, "name": name, "phone": "01000009390", "address": "가상시 동구 단독로 13", "planned_date": today,
             "items": [{"line_id": l_, "fee_rate_id": chair["id"], "name": "의자", "spec": "", "unit": "EA", "unit_label": "개", "qty": 2, "unit_price": 1, "price_status": "CONFIRMED"}],
             "status": "ACTIVE", "received_at": "2026-11-03T23:00:00Z"}
        evs_ = [{"event_id": str(uuid.uuid4()), "seq": 1, "request_id": rid, "kind": "ITEM_RESULT", "body": {"line_id": l_, "qty_confirmed": 2, "qty_collected": 99}, "ts": "2026-11-04T01:00:00Z"},
                {"event_id": str(uuid.uuid4()), "seq": 2, "request_id": rid, "kind": "ATTEMPT", "body": {"outcome": "DONE"}, "ts": "2026-11-04T01:01:00Z"}]
        return rid, make_dgb({"format": "dg.backup/1", "state": {"mode": "SOLO", "requests": [r, *extra], "events": evs_}, "photos": {}}, "solo-pass-13")
    bad_q = {"request_id": str(uuid.uuid4()), "receipt_no": "DG-29990101-0009", "name": "수량영", "phone": "01000009391", "address": "가상시 동구 단독로 14",
             "items": [{"line_id": str(uuid.uuid4()), "name": "의자", "qty": 0}], "status": "ACTIVE"}
    no_rec = {"request_id": str(uuid.uuid4()), "name": "번호없음", "phone": "01000009392", "address": "가상시 동구 단독로 15", "items": [], "status": "ACTIVE"}
    sa, fa = solo("단독가")
    sb, fb = solo("단독나", (bad_q, no_rec))
    pv = S.import_solo(con, fb, "solo-pass-13", preview=True)
    ok(pv["new"] == 1 and pv["invalid"] == 2 and {e["receipt_no"] for e in pv["errors"]} == {"DG-29990101-0009", ""} and any("수량" in e["error"] for e in pv["errors"])
       and any("접수번호" in e["error"] for e in pv["errors"]), "단독 미리보기: 수량 0·접수번호 없음은 행별 오류(500 아님)")
    S.import_solo(con, fa, "solo-pass-13", c1)
    mv = S.import_solo(con, fb, "solo-pass-13", c1)
    ga, gb = S.get_request(con, sa), S.get_request(con, sb)
    pre_ = first_no.rsplit("-", 1)
    ok(ga["receipt_no"] == f"{pre_[0]}-S{pre_[1]}" and gb["receipt_no"] == f"{pre_[0]}-S{pre_[1]}-2" and mv["requests"] == 1 and mv["invalid"] == 2,
       f"단독 휴대폰 두 대 같은 날: {ga['receipt_no']}, {gb['receipt_no']} (빈 번호까지), 틀린 접수는 건너뜀")
    ok(ga["items"][0]["unit_price"] == chair["price"] and ga["state"]["lines"][0]["qty_collected"] == 2 and ga["status"] == "DONE",
       "단독 품목 단가는 PC 수수료표로(휴대폰 1원 무시), 수거 수량은 확인 수량을 넘지 않게 정리")

    # 15. 예약 작업: 개인정보 자동 파기(묶음)·사진 정리·상태
    st0 = {x["key"]: x for x in OPS.ops_status(con)["items"]}
    ok(st0["pii_purge"]["stale"] and st0["photo_purge"]["stale"] and OPS.ops_status(con)["warnings"], "예약 작업이 돈 적 없으면 대시보드 경고")
    olds = [mk(f"오래된{i}", phone=f"010-0000-94{i:02d}") for i in range(3)]
    for o in olds:
        con.execute("UPDATE request SET status='DONE', completed_at='2020-01-01T00:00:00Z' WHERE id=?", (o,))
    lk = threading.Lock()
    res = PV.purge_pii(con, trigger="SCHEDULE", lock=lk, batch=2)
    logs = S.rows(con, "SELECT count, trigger FROM pii_purge_log ORDER BY at")
    ok(res["count"] == 3 and [x["count"] for x in logs] == [2, 1] and all(x["trigger"] == "SCHEDULE" for x in logs) and not lk.locked(),
       "개인정보 파기: 묶음마다 한 트랜잭션·대장 한 줄, 묶음 사이 잠금 풀림(SCHEDULE)")
    # 마감 기록 요약 속 이름: 묶음 트랜잭션 안에서 지움(중간에 멈춰도 파기된 접수 이름이 남지 않게) + 매번 남은 것 복구 (검토 지적)
    olds2 = [mk(f"요약이름{i}", phone=f"010-0000-95{i:02d}") for i in range(4)]
    for o in olds2:
        con.execute("UPDATE request SET status='DONE', completed_at='2020-01-02T00:00:00Z' WHERE id=?", (o,))
    rno2 = {o: S.one(con, "SELECT receipt_no FROM request WHERE id=?", (o,))["receipt_no"] for o in olds2}
    summ = {"no_result": [{"id": o, "receipt_no": rno2[o], "name": f"요약이름{i}"} for i, o in enumerate(olds2)],
            "unpaid": [{"receipt_no": rno2[olds2[3]], "name": "요약이름3", "phone": "010-0000-9503"}]}
    con.execute("INSERT INTO day_close_log (id, work_date, seq, closed_at, closed_by, summary_json) VALUES (?,?,?,?,?,?)",
                ("close-pii-1", "2019-12-31", 1, "2020-01-01T00:00:00Z", "admin", json.dumps(summ, ensure_ascii=False)))
    real_ps = PV.pseudonymize
    calls = {"n": 0}

    def flaky(con_, rid, ts):
        calls["n"] += 1
        if calls["n"] == 3:  # 두 번째 묶음에서 멈춤(첫 묶음은 이미 기록됨)
            raise RuntimeError("시험용 중단")
        real_ps(con_, rid, ts)
    PV.pseudonymize = flaky
    try:
        PV.purge_pii(con, trigger="SCHEDULE", batch=2)
        ok(False, "시험용 중단이 일어나야 함")
    except RuntimeError:
        pass
    finally:
        PV.pseudonymize = real_ps
    sj = lambda: S.one(con, "SELECT summary_json FROM day_close_log WHERE id='close-pii-1'")["summary_json"]
    gone2 = [o for o in olds2 if S.one(con, "SELECT pii_purged_at FROM request WHERE id=?", (o,))["pii_purged_at"]]
    ok(len(gone2) == 2 and "요약이름0" not in sj() and "요약이름1" not in sj() and "요약이름2" in sj() and "요약이름3" in sj(),
       "파기 도중 멈춤: 이미 파기된 첫 묶음 2건의 이름은 같은 트랜잭션에서 요약에서도 지워짐, 아직 파기 안 된 건은 그대로")
    # 예전 버전에서 멈춘 경우 흉내: 파기됐는데(pii_purged_at) 요약에 이름이 남음 → 다음 실행(대상 0건이어도)이 찾아 지움
    con.execute("UPDATE day_close_log SET summary_json=? WHERE id='close-pii-1'", (json.dumps(summ, ensure_ascii=False),))
    for o in olds2[2:]:
        PV.pseudonymize(con, o, S.now())
    con.commit()
    rep_ = PV.purge_pii(con, trigger="SCHEDULE")
    ok(rep_["count"] == 0 and rep_.get("repaired_close_logs") == 1 and not any(f"요약이름{i}" in sj() for i in range(4)) and "010-0000-9503" not in sj()
       and json.loads(sj())["no_result"][0]["receipt_no"] == rno2[olds2[0]],
       "다음 파기 실행: 대상이 없어도 파기된 접수의 이름·연락처가 남은 마감 요약을 찾아 지움(id·접수번호로 찾음, 접수번호는 남김)")
    ok(PV.purge_pii(con, trigger="SCHEDULE").get("repaired_close_logs") is None, "다시 돌리면 고칠 것 없음")
    # 업데이트 사전 사본(state/pre-migration, 개인정보 포함) 보관: 가장 새 1개는 늘 남김, 나머지는 새것부터 3개 안·30일 이내만
    import datetime as _dt
    pmd = store.pre_migration_dir(); pmd.mkdir(parents=True, exist_ok=True)
    mkc = lambda days, tag="": (pmd / f"pc-v1-to-v3-{(_dt.datetime.now() - _dt.timedelta(days=days)).strftime('%Y%m%d-%H%M%S')}{tag}.db").write_bytes(b"x" * 100)
    for days_ in (1, 2, 3, 5, 40):
        mkc(days_)
    (pmd / "메모.txt").write_text("사본 아님")
    dr = OPS.daily_tasks(con, None, None)
    left = sorted(f.name for f in pmd.iterdir())
    ok(dr["pre_migration_retention"] == 2 and len(left) == 4 and "메모.txt" in left
       and S.one(con, "SELECT 1 AS x FROM audit_log WHERE entity_id='pre-migration' AND action='PURGE'"),
       "매일 예약 작업: 사전 사본 4번째(5일)·30일 넘은 것 삭제, 새것 3개 남김(다른 파일은 그대로), 변경 이력")
    info = store.pre_migration_info()
    ok(info["count"] == 3 and info["bytes"] == 300, "사전 사본 개수·크기(진단용)")
    for f in pmd.glob("pc-v*.db"):
        f.unlink()
    mkc(50); mkc(60)
    oldest_ = sorted(f.name for f in pmd.glob("pc-v*.db"))[0]
    ok(store.pre_migration_retention() == [oldest_] and len(list(pmd.glob("pc-v*.db"))) == 1,
       "30일 넘은 사본만 있어도 가장 새 1개는 남김")
    for f in pmd.glob("pc-v*.db"):
        f.unlink()
    mkc(1); mkc(0, "-1")
    old_ = (_dt.datetime.now() - _dt.timedelta(days=50)).timestamp()
    for f in pmd.glob("pc-v*.db"):
        os.utime(f, (old_, old_))  # copy2 는 원본 DB 수정 시각을 옮기므로 파일 시각은 쓰지 않음
    ok(store.pre_migration_retention() == [] and store.pre_migration_info()["newest"].endswith("-1.db") and store.pre_migration_info()["count"] == 2,
       "나이는 파일 이름의 만든 시각으로(파일 수정 시각 아님, 같은 초 '-1' 이름 포함)")
    st1 = {x["key"]: x for x in OPS.ops_status(con)["items"]}
    ok(not st1["pii_purge"]["stale"] and not st1["photo_purge"]["stale"] and not st1["daily"]["stale"] and "verify_quick" in dr and dr["verify_full"] is True,
       "하루 예약 작업(파기·사진 정리·환불 점검·재전송 기록 정리·무결성 검사) 뒤 경고 사라짐")

    # 14. 무결성: 검사점부터 빠른 검사, 전체 검사는 따로
    PV.record_access(con, "VIEW", ra, 1, "가상")
    q1 = PV.verify_all(con, full=False)
    ck = S.get_setting(con, PV.CKPT_KEYS["access"])
    PV.record_access(con, "VIEW", ra, 1, "가상2")
    q2 = PV.verify_all(con, full=False)
    ok(q1["ok"] and q2["ok"] and q2["access"]["incremental"] and q2["access"]["from_id"] == ck["id"] and q2["access"]["checked"] == 1,
       "빠른 검사: 검사점 다음 새 행만 확인(1행)")
    first = S.one(con, "SELECT id, filter FROM access_log ORDER BY id LIMIT 1")
    con.execute("UPDATE access_log SET filter='고침' WHERE id=?", (first["id"],))
    ok(PV.verify_all(con, full=False, save=False)["ok"] and not PV.verify_all(con, full=True, save=False)["ok"], "검사점 앞 행을 고치면 빠른 검사는 못 보고 전체 검사가 찾음")
    jf = OPS.JOBS.run_now({"actor": "admin", "client": "local", "kind": "verify_full", "progress": 0}, OPS.job_fn("verify_full", con, None))
    ok(jf["status"] == "DONE" and jf["result"]["ok"] is False and jf["progress"] > 0, "전체 검사는 백그라운드 작업(진행률)")
    con.execute("UPDATE access_log SET filter=? WHERE id=?", (first["filter"], first["id"]))
    ck2 = S.get_setting(con, PV.CKPT_KEYS["access"])
    con.execute("UPDATE access_log SET filter='고침' WHERE id=?", (ck2["id"],))
    ok(not PV.verify_all(con, full=False, save=False)["ok"], "검사점 행 자체를 고치면 빠른 검사도 찾음")
    con.execute("UPDATE access_log SET filter='가상2' WHERE id=?", (ck2["id"],))

    # 16·19. 백업: 조각 해시·이름 검사·두 번째 위치 변경 이력
    bk = S.make_backup(con, "backup-pass-13x")
    ok(bk["sha256"] == hashlib.sha256(Path(bk["file"]).read_bytes()).hexdigest(), "수동 백업 해시는 조각으로 읽어 계산(같은 값)")
    for bad in ("../pc.db", "donggu-backup-x.dgbak", "/etc/passwd", "donggu-backup-20261104-120000.dgbak/../../x.dgbak"):
        raises(lambda b=bad: S.verify_backup(con, b, "backup-pass-13x"), "이름", f"백업 이름 형식·폴더 밖 거부: {bad}")
    ok(S.verify_backup(con, bk["name"], "backup-pass-13x")["ok"], "정한 형식의 백업 이름은 검사")
    d2 = tempfile.mkdtemp(prefix="dg-dest2-")
    S.save_settings(con, {"backup_dest2": d2})
    a_ = S.list_audit(con, entity="backup", action="DEST2")
    ok(a_ and json.loads(a_[0]["detail"])["to"] == d2, "두 번째 보관 위치 변경은 경로와 함께 변경 이력")

    # 13·18·20. 서버: 조회는 잠금 없이, 409, 재전송 방지, 작업 번호, 세션 절대 만료, 로그
    S.create_user(con, "admin13", "관리자", "ADMIN", "admin-pass-013")
    S.create_user(con, "kim13", "김접수", "INTAKE", "intake-pass-13")
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    SV.CON, SV.PORT, SV.LAN = con, port, None
    SV.setup_logging(Path(os.environ["DG_STATE"]))
    srv = SV.Server(("127.0.0.1", port), SV.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def http(method, path, body=None, cookie=None, timeout=20):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                     headers={"X-DG-CSRF": "1", "Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
        try:
            r = opener.open(req, timeout=timeout)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def login(u, pw):
        st_, hd, _ = http("POST", "/api/login", {"username": u, "password": pw})
        return re.search(r"dg_s=[^;]+", hd.get("Set-Cookie") or "").group(0) if st_ == 200 else None
    adm, kim = login("admin13", "admin-pass-013"), login("kim13", "intake-pass-13")
    ok(adm and kim, "서버 로그인(비밀번호 확인은 잠금 밖)")
    with SV.LOCK:  # 쓰기 잠금을 잡고 있어도 조회는 된다
        t0 = time.time()
        st_, _, body = http("GET", "/api/pending", cookie=kim, timeout=5)
    ok(st_ == 200 and json.loads(body)["items"] and time.time() - t0 < 5, "조회(GET)는 쓰기 잠금 없이 조회 전용 연결(WAL)로")
    st_, _, body = http("GET", "/api/requests?quick=OVERDUE", cookie=kim)
    ok(st_ == 200 and json.loads(body)["total"] >= 1 and S.one(con, "SELECT action FROM access_log ORDER BY id DESC LIMIT 1")["action"] == "LIST",
       "조회 전용 연결에서도 접속기록은 잠금 안에서 쓰기 연결로 남김")
    st_, _, body = http("POST", f"/api/requests/{rn}", {**base, "name": "날짜없음", "planned_date": "", "req_ver": RV(con, rn) + 5, "reason": "가상",
                                                         "items": [{"line_id": S.get_request(con, rn)["items"][0]["line_id"], "fee_rate_id": chair["id"], "qty": 1}]}, cookie=kim)
    jb = json.loads(body)
    ok(st_ == 409 and jb["stale"] and jb["req_ver"] == RV(con, rn) and "다시 열어" in jb["error"], "옛 버전 수정 → HTTP 409 + 지금 req_ver")
    n0 = S.one(con, "SELECT COUNT(*) AS n FROM request")["n"]
    body_ = {**base, "name": "서버두번", "items": [{"fee_rate_id": chair["id"], "qty": 1}], "client_req_id": "c-" + uuid.uuid4().hex}
    r1_, r2_ = json.loads(http("POST", "/api/requests", body_, cookie=kim)[2]), json.loads(http("POST", "/api/requests", body_, cookie=kim)[2])
    ok(r1_["id"] == r2_["id"] and S.one(con, "SELECT COUNT(*) AS n FROM request")["n"] == n0 + 1, "POST /api/requests 두 번(같은 client_req_id) → 1건")
    pay_b = {"amount": 500, "method": "현장 현금", "reason": "마감 뒤 납부(가상)", "client_req_id": "p-" + uuid.uuid4().hex}
    http("POST", f"/api/requests/{r1_['id']}/payments", pay_b, cookie=kim); http("POST", f"/api/requests/{r1_['id']}/payments", pay_b, cookie=kim)
    ok(S.one(con, "SELECT COUNT(*) AS n FROM payment WHERE request_id=?", (r1_["id"],))["n"] == 1, "수납 기록 두 번 → 1건")
    st_, _, body = http("POST", "/api/photo-purge", {}, cookie=adm)
    jid = json.loads(body)["job_id"]
    for _ in range(100):
        j = json.loads(http("GET", f"/api/jobs/{jid}", cookie=adm)[2])
        if j["status"] in ("DONE", "FAILED"):
            break
        time.sleep(0.05)
    ok(j["status"] == "DONE" and "count" in j["result"] and http("GET", f"/api/jobs/{jid}", cookie=kim)[0] == 404, "사진 정리 → 작업 번호, GET /api/jobs/{id}로 결과(다른 사용자는 못 봄)")
    st_, _, body = http("GET", "/api/diag", cookie=adm)
    dg = json.loads(body)
    ok(st_ == 200 and dg["tables"]["request"] >= 10 and dg["db_bytes"] > 0 and dg["schema_version"] == store.LATEST and http("GET", "/api/diag", cookie=kim)[0] == 403
       and dg["pre_migration"]["count"] == 2 and dg["pre_migration"]["bytes"] == 200 and dg["pre_migration"]["keep"] == 3 and dg["pre_migration"]["days"] == 30,
       "진단(관리자만): DB·WAL·사진 크기, 표별 건수, 마지막 백업·검사·파기, 업데이트 사전 사본 개수·크기")
    st_, hd, body = http("GET", f"/api/requests/{rb}/notice", cookie=kim)
    ok(st_ == 200 and hd.get("Content-Type", "").startswith("text/plain") and "접수번호" in body.decode(), "GET /api/requests/{id}/notice → 안내 글(text/plain)")
    tkn = json.loads(http("POST", f"/api/requests/{rb}/receipt-print", {}, cookie=kim)[2])["token"]
    ok("납부 안내" in http("GET", f"/print/doc/{tkn}", cookie=kim)[2].decode(), "접수증에 납부 안내 칸(설정 payment_guide)")
    st_, hd, _ = http("GET", f"/bundle/{bun2['bundle_id'][:8]}", cookie=kim)
    ok(st_ == 200 and "frame-ancestors 'none'" in (hd.get("Content-Security-Policy") or "") and hd.get("X-Frame-Options") == "DENY", "QR 표시 화면에도 보안 머리(CSP·틀 막기)")
    SV.SLOW_MS = -1
    http("GET", "/api/readiness", cookie=kim)
    # 서버는 응답을 보낸 뒤 로그를 쓴다: 로그가 남을 때까지(최대 3초) 기다린 뒤 기준을 되돌린다
    logf = Path(os.environ["DG_STATE"]) / "logs" / "server.log"
    for _ in range(60):
        if "느린 요청 GET /api/readiness" in logf.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    SV.SLOW_MS = 1000
    ok("느린 요청 GET /api/readiness" in logf.read_text(encoding="utf-8"), "1초 넘는 요청은 WARNING(경로 틀·ms)")
    tok_ = kim.split("=", 1)[1]
    SV.SESSIONS[tok_]["created"] -= SV.ABS_S + 1
    ok(http("GET", "/api/me", cookie=kim)[0] == 401, "세션 절대 만료(조작이 이어져도 10시간)")
    SV.SESSIONS["dead"] = {"exp": time.time() - 1, "created": time.time(), "user_id": "x", "username": "x", "role": "ADMIN"}
    ok(SV.cleanup_sessions() >= 1 and "dead" not in SV.SESSIONS, "만료 세션 정기 정리")
    for _ in range(3):
        http("POST", "/api/login", {"username": "kim13", "password": "wrong-password"})
    SV.FAILS.clear()
    with SV.LOCK:
        SV.flush_login_fails(con, force=True)
    lf = S.rows(con, "SELECT detail, actor FROM audit_log WHERE action='LOGIN_FAIL'")
    ok(len(lf) == 1 and json.loads(lf[0]["detail"])["count"] == 3 and lf[0]["actor"] == "kim13", "로그인 실패 이력은 분마다 모아 한 줄(횟수)")
    # 접속 위치(IP)별 제한은 LAN 모드에서만. 기본 방식(이 PC에서만, 모두 127.0.0.1)은 아이디별 제한만
    SV.FAILS.clear(); SV.IP_FAILS.clear()
    codes = [http("POST", "/api/login", {"username": f"nobody{i}", "password": "wrong-password"})[0] for i in range(SV.IP_FAIL_MAX + 3)]
    ok(all(c_ == 400 for c_ in codes) and not SV.IP_FAILS and login("kim13", "intake-pass-13"),
       f"기본 방식: 여러 아이디로 {SV.IP_FAIL_MAX + 3}번 틀려도 이 PC의 다른 사용자 로그인은 막히지 않음(위치별 제한 없음)")
    for _ in range(5):
        http("POST", "/api/login", {"username": "kim13", "password": "wrong-password"})
    st_, _, body = http("POST", "/api/login", {"username": "kim13", "password": "intake-pass-13"})
    ok(st_ == 400 and "초 뒤" in json.loads(body)["error"] and login("admin13", "admin-pass-013"), "기본 방식: 아이디별 제한은 그대로(5번 틀린 아이디만 잠김)")
    SV.FAILS.clear(); SV.IP_FAILS.clear()
    import ipaddress
    SV.LAN = {"hostname": "127.0.0.1", "nets": [ipaddress.ip_network("10.0.0.0/24")]}
    for i in range(SV.IP_FAIL_MAX):
        http("POST", "/api/login", {"username": f"nobody{i}", "password": "wrong-password"})
    st_, _, body = http("POST", "/api/login", {"username": "kim13", "password": "intake-pass-13"})
    ok(st_ == 400 and "초 뒤" in json.loads(body)["error"] and SV.ip_blocked("127.0.0.1") > 0 and SV.ip_blocked("10.0.0.8") == 0,
       "LAN 모드: 한 PC(IP)에서 10분에 20번 틀리면 그 PC 잠금(맞는 비밀번호도), 다른 PC는 영향 없음")
    SV.IP_FAILS.clear()
    for _ in range(5):
        http("POST", "/api/login", {"username": "kim13", "password": "wrong-password"})
    st_, _, body = http("POST", "/api/login", {"username": "kim13", "password": "intake-pass-13"})
    ok(st_ == 400 and "초 뒤" in json.loads(body)["error"] and not SV.ip_blocked("127.0.0.1"), "LAN 모드: 아이디별 제한도 그대로")
    SV.LAN = None
    ok(SV.login_ip("10.0.0.9") is None and SV.ip_blocked(None) == 0, "기본 방식에서는 위치별 제한에 쓸 IP 없음")
    SV.FAILS.clear(); SV.IP_FAILS.clear()
    srv.shutdown(); srv.server_close()

    # 리뷰 반영: 다른 날·다른 팀으로 옮겨 간 접수는 오늘 배정 부분 수정에서 다시 쓰지 않는다
    tmr = (dt.date.fromisoformat(today) + dt.timedelta(days=1)).isoformat()
    rm_, rn_ = mk("십삼마", phone="010-0000-9305"), mk("십삼바", phone="010-0000-9306")
    S.edit_assignment(con, today, "insert", {"crew_id": c1, "request_id": rm_})
    S.reschedule_requests(con, [rm_], tmr, "OTHER", "주민 요청 내일로")
    S.edit_assignment(con, tmr, "insert", {"crew_id": c2, "request_id": rm_})
    ok(rm_ not in [x["request_id"] for b in S.current_assignment(con, today) for x in b["stops"]], "옮겨 간 접수는 오늘 확정 배정 목록에 없음")
    S.edit_assignment(con, today, "insert", {"crew_id": c1, "request_id": rn_})
    mv = S.one(con, "SELECT r.planned_date, b.crew_id, b.work_date FROM request r JOIN active_assignment a ON a.request_id=r.id JOIN assignment_batch b ON b.id=a.batch_id WHERE r.id=?", (rm_,))
    ok(mv and mv["planned_date"] == tmr and mv["crew_id"] == c2 and mv["work_date"] == tmr,
       "오늘 배정에 다른 접수를 넣어도 내일·2팀으로 옮긴 접수의 일정·팀은 그대로(되돌리지 않음)")

    # 리뷰 반영: 콘솔에서 입력한 DB 비밀번호는 맞는 것으로 확인된 뒤 기억 → 조회 연결이 다시 묻지 않음
    saved_env, saved_kr, saved_gp, saved_pk = os.environ.pop("DG_DB_KEY"), store._keyring_get, store.getpass.getpass, store._PROMPTED_KEY
    asked = []
    try:
        store._keyring_get = lambda: None
        store._PROMPTED_KEY = None
        store.getpass.getpass = lambda prompt="": asked.append(prompt) or saved_env
        c_a = store.connect(readonly=True); c_b = store.connect(readonly=True)
        c_a.close(); c_b.close()
        ok(len(asked) == 1, "DB 비밀번호는 한 번만 물음(조회용 연결·예약 작업은 기억한 값 사용)")
        store._PROMPTED_KEY = None; asked.clear()
        store.getpass.getpass = lambda prompt="": asked.append(prompt) or "wrong-key-xxxxxxxx"
        try:
            store.connect(readonly=True)
        except SystemExit:
            pass
        ok(store._PROMPTED_KEY is None, "틀린 비밀번호는 기억하지 않음")
    finally:
        os.environ["DG_DB_KEY"] = saved_env
        store._keyring_get, store.getpass.getpass, store._PROMPTED_KEY = saved_kr, saved_gp, saved_pk

    ok(not summary_matches(), "여러 쓰기 경로(취소·수납·배정 수정·충돌 반영·단독 옮기기·파기) 뒤에도 요약 열 = derive()")
    con.close()
    print(f"\n통과 {passed}개 (v0.13 포함)")

def v13_refactor():
    """v0.13 구조 정리(행동 그대로): 경로 표 = 정리 전 권한, 설정 등록표, PC·휴대폰 공통 규칙(시험 벡터·표시 이름)."""
    print("\n— v0.13 구조 정리 —")
    from dgpc import server as SV, settings as SR
    # 경로 표(역할 × 경로)가 정리 전 server.allowed() 결과와 같은지 (tests/data/route_matrix_v013.json: 정리 전에 떠 둔 표)
    snap = json.loads((ROOT / "tests" / "data" / "route_matrix_v013.json").read_text(encoding="utf-8"))["routes"]
    cur = {(r.method, r.pattern): r for r in SV.ROUTES}
    ok(set(cur) == {(x["method"], x["path"]) for x in snap}, f"경로 표의 경로 {len(cur)}개 = 정리 전 경로(빠지거나 늘어난 것 없음)")
    diff = [(x["method"], x["path"]) for x in snap if sorted(cur[(x["method"], x["path"])].roles) != sorted(x["roles"])]
    ok(not diff, f"역할 × 경로 권한이 정리 전과 같음 (다른 곳: {diff[:5]})")
    sample = lambda pat: pat.replace("{id}", "a1b2c3d4")
    ok(all(SV.allowed(role, x["method"], sample(x["path"])) == (role in x["roles"]) for x in snap for role in ("ADMIN", "INTAKE", "FIELD", "LEAD")),
       "server.allowed() 가 경로 표에서 나온 판단과 같음(4역할 × 경로)")
    ok(all(cur[(x["method"], x["path"])].must_change_ok == x["must_change_ok"] for x in snap), "임시 비밀번호 상태에서 쓸 수 있는 경로(me·password·bootstrap·prefs) 그대로")
    ok(all(SV.match(x["method"], sample(x["path"]))[0] is cur[(x["method"], x["path"])] for x in snap),
       "견본 주소가 모두 자기 경로로 맞춰짐(앞 경로에 가려지는 것 없음)")
    ok(all(r.lock == ("read" if r.method == "GET" else r.lock) for r in SV.ROUTES) and
       {r.pattern for r in SV.ROUTES if r.lock == "none"} == {"import/result", "import/photos", "import/solo", "requests/import", "recon", "addr-import",
                                                             "external/test", "route", "backup", "backup/auto", "backup/retention", "privacy/purge",
                                                             "photo-purge", "refunds/sync", "integrity/full"},
       "잠금 정책: GET = 조회 전용 연결, 잠금 밖 검증 POST 15개 = none, 나머지 POST = write")
    ok(not SV.allowed("FIELD", "GET", "nothing-here") and SV.allowed("ADMIN", "GET", "nothing-here") and SV.allowed("INTAKE", "GET", "requests/a/b/c"),
       "경로 표에 없는 주소: 관리자·같은 첫 마디를 쓸 수 있는 역할은 404, 그 밖은 403")
    # 설정 등록표
    con = store.connect()
    def err(fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            return type(e).__name__
        return None
    ok(err(lambda: S.get_setting(con, "no_such_key")) == "KeyError" and err(lambda: S.set_setting(con, "no_such_key", 1)) == "KeyError",
       "등록되지 않은 설정 키 읽기·쓰기 → KeyError")
    ok(err(lambda: S.set_setting(con, "unload_min", "많이")) == "ValueError", "등록표 형식 검사: 숫자 설정에 글 저장 거부(ValueError)")
    st = S.settings(con)
    ok({k for k, v in SR.SETTINGS.items() if v.expose} <= set(st) and {"capacity", "reductions", "external", "fee_version", "backup_auto", "pay_methods",
                                                                         "reduction_round_units", "pii_retention_years"} <= set(st),
       "설정 묶음 = 등록표(expose) + 모듈이 더한 값(수용량·감면·외부 키·수수료표·백업·수납 방법)")
    ok(not any(SR.SETTINGS[k].secret and k in st for k in SR.SETTINGS), "비밀 설정(외부 승인키)은 설정 묶음에 그대로 나가지 않음")
    # PC·휴대폰 공통 계산 규칙: data/derive_vectors.json (tests/fees.test.mjs 도 같은 파일로 휴대폰 deriveStop·applyReduction 을 확인)
    from dgpc import derive as D
    vec = json.loads((ROOT / "data" / "derive_vectors.json").read_text(encoding="utf-8"))["vectors"]
    for v in vec:
        d = D.derive_pure(v["items"], [{"kind": e["kind"], "body_json": json.dumps(e["body"]), "device_ts": f"2026-11-12T00:00:{i:02d}Z"}
                                       for i, e in enumerate(v["events"])], [])
        fs = D.fee_summary({"reduction_code": "X" if v["reduction_rate"] else None, "reduction_rate": v["reduction_rate"],
                            "reduction_round_unit": v.get("reduction_round_unit")}, d["fee"]["final"])
        x = v["expect"]
        ok([{k: l[k] for k in ("line_id", "kind", "price", "qty_confirmed", "qty_collected", "remaining")} for l in d["lines"]] == x["lines"]
           and {k: d["fee"][k] for k in x["fee"]} == x["fee"] and d["contact"] == x["contact"] and d["outcome"] == x["outcome"]
           and {"rate": fs["reduction_rate"], "reduction": fs["reduction"], "levied": fs["levied"]} == x["reduction"], f"공통 벡터: {v['name']}")
    labels = json.loads((ROOT / "data" / "labels.json").read_text(encoding="utf-8"))
    from dgpc import reports as RP
    ok(RP.STATUS_LABEL == labels["status"] and S.STATUS_LABEL["DONE"] == "수거 완료", "CSV 상태 이름 = data/labels.json (PC 화면·휴대폰과 같은 표)")
    import subprocess
    gen = [subprocess.run([sys.executable, str(ROOT / "tools" / t), "--check"], capture_output=True, text=True).returncode
           for t in ("build_fee_js.py", "build_labels.py", "gen_settings_doc.py", "gen_routes_doc.py")]
    ok(gen == [0, 0, 0, 0], f"만든 파일이 원본과 같음: fees-data.js·labels-data.js·docs/settings.md·docs/routes.md {gen}")


if __name__ == "__main__":
    main()
    v10()
    v11()
    v11_external()
    v12_field()
    v12_pilot()
    v12_load()
    v12_review()
    v13()
    v13_refactor()
    print(f"\n통과 {passed}개 (구조 정리 포함)")
