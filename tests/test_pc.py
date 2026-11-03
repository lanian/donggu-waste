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


if __name__ == "__main__":
    main()
