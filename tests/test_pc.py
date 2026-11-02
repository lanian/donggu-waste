"""PC 업무 로직 테스트 (암호화 DB, 접수·이력·배정·노선·묶음·결과 병합·충돌·사진·마감). 휴대폰은 Python으로 흉내 낸다.
실행: python3 tests/test_pc.py
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
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
    out = S.confirm_plan(con, day, {c1: [{"request_id": r1["id"], "eta": "09:20"}, {"request_id": r2["id"], "eta": "09:40"}, {"request_id": r4["id"], "eta": "10:10"}]})
    b1 = out[0]["batch_id"]
    raises(lambda: S.confirm_plan(con, day, {c2: [{"request_id": r1["id"]}]}), "이미 다른 배정", "같은 접수를 다른 팀에 동시 배정 거부")

    # 목록 묶음 → 휴대폰 수신
    bun = S.make_bundle(con, b1)
    head, payload, receipt = phone.receive(bun["frames"])
    s1 = next(s for s in payload["stops"] if s["request_id"] == r1["id"])
    ok(s1["name"] == "가상일" and s1["phone"] == "01000001111" and "101호" in s1["address"], "휴대폰에 이름·연락처·주소(수정본) 전달")
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
    ok(sm["unload_total"] == {"volume_m3": 5.5, "weight_kg": 420.0}, "마감: 하차 합계 5.5m³ · 420kg")
    ok(any(x["status"] == "AMOUNT_PENDING" for x in sm["payments"]), "마감: 금액 확인 대기 수납 건 표시")
    sm2 = S.day_summary(con, day2)
    ok([x["receipt_no"] for x in sm2["no_result"]] == [g1["receipt_no"]], f"마감 {day2}: 재배정했으나 결과 미회수 건 표시")
    S.close_day(con, day, "시험 마감")
    ok(S.day_summary(con, day)["closed"], "일일 마감 기록")

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
    S.ACTOR.set("kim")
    S.cancel_request(con, sp[0]["id"], "주민 취소 요청(시험)")
    a = S.list_audit(con, entity="request", actor="kim")
    ok(a and a[0]["action"] == "CANCEL", "변경 이력에 작업자(kim) 기록")
    ok(not any("가상오" in (x["detail"] or "") or "01000005555" in (x["detail"] or "") for x in S.list_audit(con, limit=1000)), "변경 이력 내용에 이름·연락처 없음")

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

    # 사진 보관 기간 정리
    S.ACTOR.set("admin")
    raises(lambda: S.save_settings(con, {"photo_retention_days": 3}), "7일", "사진 보관 기간 범위 검사")
    S.save_settings(con, {"photo_retention_days": 30})
    rec = S.rows(con, "SELECT photo_id, request_id FROM photo WHERE recovery_status='RECOVERED'")
    ok(rec and not S.purge_candidates(con), "회수 직후 사진은 정리 대상 아님")
    ok(not S.purge_candidates(con, (dt.date.fromisoformat(S.today_kst()) + dt.timedelta(days=40)).isoformat()), "처리 중(배정 상태)인 접수의 사진은 기간이 지나도 정리하지 않음")
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


if __name__ == "__main__":
    main()
