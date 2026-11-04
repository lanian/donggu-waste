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
    reg = S.register_device(con, phone.code(), "가상 1팀 휴대폰")
    dev = reg["device_id"]
    ok(len(reg["fingerprint"]) == 6, f"휴대폰 등록, 지문 {reg['fingerprint']}")
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
    S.update_request(con, r1["id"], {**base, "name": "가상일", "phone": "01000001111", "address": "가상시 동구 예시로 1, 101호",
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
    cf = S.list_conflicts(con)
    ok(rep3["conflict"] == 1 and len(cf) == 1 and "오래된 목록" in cf[0]["conflict_reason"], "배정 변경 후 옛 목록 결과 → 충돌함 (자동 덮어쓰기 없음)")
    raises(lambda: S.resolve_conflict(con, cf[0]["event_id"], "APPLY", ""), "사유", "충돌 처리에 사유 필수")
    S.resolve_conflict(con, cf[0]["event_id"], "APPLY", "팀장 확인: 실제 수거함")
    ok(S.get_request(con, r2["id"])["state"]["lines"][0]["qty_collected"] == 1, "담당자 확인 후 반영")

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
    sys.path.insert(0, str(ROOT / "pc" / "dgpc"))
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
    ok(rr["kept"] and Path(rr["kept"]).exists() and S.get_setting(con, "after_backup") is None and S.one(con, "SELECT COUNT(*) AS n FROM request")["n"] == n_before,
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
    cl = S.close_day(con, day, "가상 마감")
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
    upd = S.update_request(con, t["done"], {**base, "name": "가상이월done", "address": "가상시 동구 시험길 5, 2층", "planned_date": day,
                                            "items": [{"line_id": gd["items"][0]["line_id"], "fee_rate_id": chair, "qty": 1}]}, "주소 보완(가상)")
    raises(lambda: S.add_payment(con, t["done"], {"amount": 2000, "method": "현장 납부", "paid_on": day}), "사유", "마감한 날 수납 기록은 사유 필수")
    S.add_payment(con, t["done"], {"amount": 2000, "method": "현장 납부", "paid_on": day, "reason": "늦게 확인한 납부(가상)"})
    edits = S.day_summary(con, day)["post_close_edits"]
    ok(upd["after_close"] and len(edits) == 2 and {e["entity"] for e in edits} == {"request", "payment"} and edits[0]["receipt_no"],
       "마감 후 수정 2건(접수 수정·수납)이 '마감 후 수정'으로 표시·마감 화면 목록에")
    # 다시 마감: 회차가 쌓임
    S.close_day(con, day, "다시 마감")
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
    S.add_payment(con, rp, {"kind": "REFUND", "amount": 2000, "method": "무통장 입금", "paid_on": "2026-11-04"})
    ok(S.payment_status(con, rp)["status"] == "NONE" and S.search_requests(con, {"quick": "REFUND"})["total"] == 0, "환불 기록하면 목록에서 빠짐")

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
    dev = S.register_device(con, phone.code(), "가상폰")["device_id"]
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
    ok(st_r["totals"]["requests"] >= 7 and st_c["totals"]["requests"] == 3 and st_r["basis_label"] == "접수일", "통계 기준: 접수일·완료일(오늘 끝난 3건: 일부 수거 1·취소 2)")
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
    S.update_request(con, ra["id"], {**base, "name": "가상감면", "memo": "메모 가상감면", "items": [{"line_id": S.get_request(con, ra["id"])["items"][0]["line_id"], "fee_rate_id": wardrobe, "qty": 2}]}, "가상 수정")
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
    from dgpc import area as A, external as X, finance as FN, geo, privacy as PV
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
                 "zipNo": "61400", "emdNm": "가상동", "hemdNm": "가상9동", "admCd": "2911010800", "rnMgtSn": "291103166047", "udrtYn": "0", "buldMnnm": "1", "buldSlno": "0"},
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
        cache = routing.TravelCache(con)
        mat, info = routing.kakao_matrix(pts, "KAKAOKEY-0000-1111", cache, min_interval_s=0)
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
        kc.clear()
        mat2, info2 = routing.kakao_matrix(pts, "KAKAOKEY-0000-1111", cache, min_interval_s=0)
        ok(not kc and info2["cache_hits"] == 35 * 34 and mat2 == mat, "두 번째 계산: DB 캐시(좌표 소수 5자리 쌍)로 API 호출 없음")
        ok(S.one(con, "SELECT COUNT(*) AS n FROM travel_cache")["n"] == 35 * 34 and not S.rows(con, "SELECT origin FROM travel_cache WHERE origin GLOB '*[가-힣]*'"),
           "캐시 표에는 좌표 쌍과 초만 (이름·주소 없음)")
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=31)).isoformat(timespec="seconds")
        con.execute("UPDATE travel_cache SET fetched_at=? WHERE origin=?", (old, routing._ck(pts[0])))
        kc.clear()
        routing.kakao_matrix(pts[:3], "KAKAOKEY-0000-1111", cache, min_interval_s=0)
        ok(len(kc) == 1 and kc[0]["body"]["origin"]["y"] == "35.14620", "30일 지난 캐시는 쓰지 않고 다시 물음 (지난 것은 정리)")
        # 일부 실패 → 그 쌍만 직선 추정
        kc.clear()
        fail_keys.add(f"{pts[2][0]:.5f},{pts[2][1]:.5f}")
        sub = [(35.15, 126.93), pts[2], (35.151, 126.931), (35.152, 126.935)]
        mat3, info3 = routing.kakao_matrix(sub, "KAKAOKEY-0000-1111", routing.TravelCache(con), min_interval_s=0)
        est = routing._estimate_min(sub[0], sub[1])
        ok(info3["fallback_pairs"] == 3 and mat3[0][1] == est and routing.matrix_note("KAKAO", info3) == "도로 시간: 카카오모빌리티 (일부 직선 추정 3쌍)",
           "경로를 못 받은 쌍만 직선거리 추정 → '도로 시간: 카카오모빌리티 (일부 직선 추정 3쌍)'")
        # 시간 초과: 더 묻지 않고 전부 추정
        X.urlopen = lambda req, timeout: (_ for _ in ()).throw(urllib.error.URLError(socket.timeout("timed out")))
        far = [(35.16, 126.94), (35.161, 126.941), (35.163, 126.945)]
        m4, s4, i4 = routing.build_matrix(far, method="KAKAO", kakao_key="KAKAOKEY-0000-1111", cache=routing.TravelCache(con))
        ok(s4 == "ESTIMATE" and i4["api_calls"] == 1 and m4 == routing.estimate_matrix(far) and "카카오모빌리티로 구하지 못함" in routing.matrix_note(s4, i4),
           "시간 초과 → 첫 실패에서 멈추고 전부 직선거리 추정, 화면 문구에 표시")
        X.urlopen = kakao_fake
        kc.clear()
        _m, s5, i5 = routing.build_matrix(far, method="KAKAO", kakao_key="WRONGKEY-000000", cache=None)
        ok(s5 == "ESTIMATE" and len(kc) == 1 and "인증" in i5["error"], "틀린 키(401) → 한 번만 묻고 직선거리 추정")
        _m, s6, i6 = routing.build_matrix(far, method="KAKAO", kakao_key="", cache=None)
        ok(s6 == "ESTIMATE" and "REST 키" in routing.matrix_note(s6, i6), "키 없이 카카오 방식 → 직선거리 추정과 이유 표시")
        kc.clear()
        mm, _ = routing.kakao_matrix([(35.15 + i * 1e-3, 126.93) for i in range(6)], "KAKAOKEY-0000-1111", None, max_calls=2, min_interval_s=0)
        ok(len(kc) == 2, "호출 수 한도(max_calls)를 넘으면 더 묻지 않음 (나머지 직선 추정)")
        t0 = time.monotonic()
        routing.kakao_matrix([(35.17 + i * 1e-3, 126.93) for i in range(3)], "KAKAOKEY-0000-1111", None, min_interval_s=0.15)
        ok(time.monotonic() - t0 >= 0.3, "호출 사이 최소 간격(초당 호출 수 제한)")
        # 노선 계산 전체: 카카오 방식
        S.save_settings(con, {"external": {"keys": {"kakao_rest_key": "KAKAOKEY-0000-1111"}, "travel_method": "KAKAO"}})
        ok(S.travel_method(con) == "KAKAO" and S.settings(con)["external"]["keys"]["kakao_rest_key"]["mask"] == "••••1111", "설정: 이동시간 계산 방식 카카오모빌리티 + REST 키(가린 값)")
        crew = {"id": "c1", "name": "가상", "capacity_m3": 20, "shift_start": "09:00", "shift_end": "17:00"}
        reqs = [{"id": f"r{i}", "lat": 35.145 + i * 2e-3, "lon": 126.92 + i * 1e-3, "items": [{"name": "의자", "qty": 1}]} for i in range(4)]
        pl = routing.plan(reqs, [crew], {"lat": 35.1462, "lon": 126.9233}, travel_method="KAKAO", kakao_key=S.secret(con, "kakao_rest_key"),
                          travel_cache=routing.TravelCache(con), time_limit_s=1)
        ok(pl["matrix_source"] == "KAKAO" and pl["matrix_note"].startswith("도로 시간: 카카오모빌리티") and len(pl["routes"][0]["stops"]) == 4, f"노선 계산: {pl['matrix_note']}")
        S.save_settings(con, {"external": {"travel_method": "ESTIMATE"}})
        ok(routing.plan(reqs, [crew], {"lat": 35.1462, "lon": 126.9233}, travel_method=S.travel_method(con), time_limit_s=1)["matrix_source"] == "ESTIMATE", "기본(직선거리 추정)으로 되돌리면 외부 호출 없음")
    finally:
        X.urlopen = real_urlopen

    # 휴대폰 목록: 감면율(rd)·부과번호(lv)
    reg = S.registration_text(con)
    ph = Phone(reg)
    dev = S.register_device(con, ph.code(), "감면폰")["device_id"]
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


if __name__ == "__main__":
    main()
    v10()
    v11()
    v11_external()
