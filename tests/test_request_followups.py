"""가상 접수와 실제 PC HTTP·SQLCipher로 동별 후속조치 검증."""
import unittest

from test_label_usage_api import UsageHTTPCase
from dgpc import area, close, core as C, privacy, service as S, users


QUEUE = "dong-work/followups"


class RequestFollowupsTest(UsageHTTPCase):
    def setUp(self):
        super().setUp()
        self.a = area.get_dong(self.con, "충장동")["id"]
        self.b = area.get_dong(self.con, "동명동")["id"]
        self.owner = self.staff("followup_owner", self.a)
        self.second = self.staff("followup_second", self.a)
        self.outside = self.staff("followup_outside", self.b)
        self.cookie = self.login("followup_owner", "followup-test-only-123")

    def staff(self, username, dong):
        return S.create_user(self.con, username, username, "DONG", "followup-test-only-123", dong_ids=[dong])

    def get_ok(self, path, cookie=None):
        status, value = self.get(path, cookie or self.cookie)
        self.assertEqual(status, 200, value)
        return value

    def post_ok(self, path, body, cookie=None):
        status, value, _ = self.post(path, body, cookie or self.cookie)
        self.assertEqual(status, 200, value)
        return value

    def contact(self, r, kind):
        self.post_ok(f"requests/{r['id']}/contacts", {"kind": kind, "content": "가상 통화 내용"}, self.admin_cookie)

    def task(self, path, assignee=None, **fields):
        current = self.get_ok(path)
        return {"version": current["version"], "signal": current["signal"], "status": "OPEN",
                "assignee_id": assignee or self.owner, "due_date": "2099-01-10", "next_action": "가상 조치 내용", **fields}

    def partial(self, r, line):
        seal, event = self.phone(r)
        package = seal([event("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 1, "reason": "NOT_PUT_OUT"}),
                        event("ATTEMPT", {"outcome": "PARTIAL"})])
        self.assertEqual(self.post_ok("import/result", package, self.admin_cookie)["applied"], 2)

    def test_partial_revisit_and_close_carryover(self):
        r, line = self.request()
        self.partial(r, line)
        rid = r["id"]
        day, revisit = C.today_kst(), "2098-12-19"
        S.reschedule_requests(self.con, [rid], day, "OTHER", "가상 예정일")
        path = f"{QUEUE}/{rid}"
        detail = self.get_ok(path)
        self.assertEqual(detail["status"], "NEEDS_ACTION")
        self.assertEqual([(x["remaining"], x["reason"]) for x in detail["remaining"]], [(2, "NOT_PUT_OUT")])
        self.assertFalse(detail["appointment"]["confirmed"])
        paid = self.post_ok(f"requests/{rid}/payments", {"amount": 1000, "method": "기타"}, self.admin_cookie)
        self.assertEqual(paid["paid"], 1000)
        self.post_ok(path, self.task(path))
        other, other_line = self.request()
        self.partial(other, other_line)
        S.reschedule_requests(self.con, [other["id"]], day, "OTHER", "가상 예정일")
        before = S.get_request(self.con, rid)
        appointment = {"req_ver": before["req_ver"], "date": revisit, "start": "10:00", "end": "11:00"}
        scheduled = self.post_ok(path + "/revisit", appointment)
        self.assertEqual(self.post_ok(path + "/revisit", appointment)["req_ver"], scheduled["req_ver"])
        self.assertEqual(self.get_ok(path)["appointment"], {"date": revisit, "start": "10:00", "end": "11:00", "confirmed": True})
        close.close_day(self.con, day)
        after = S.get_request(self.con, rid)
        moved = S.get_request(self.con, other["id"])
        self.assertEqual(after["planned_date"], revisit)
        self.assertEqual(moved["planned_date"], area.next_working_day(self.con, day, moved["dong"]))
        self.assertTrue(any("마감 이월" in v["reason"] for v in moved["versions"]))
        other_path = f"{QUEUE}/{other['id']}"
        self.assertEqual(self.get_ok(other_path)["status"], "NEEDS_ACTION")
        S.confirm_plan(self.con, moved["planned_date"], {area.get_dong(self.con, moved["dong"])["crew_id"]: [{"request_id": other["id"]}]})
        assigned = self.get_ok(other_path)
        self.assertEqual((assigned["status"], assigned["request_status"], assigned["outcome"]), ("NEEDS_ACTION", "ASSIGNED", "PARTIAL"))
        self.assertIn(other["id"], {x["id"] for x in self.get_ok(QUEUE)["rows"]})
        self.post_ok(path, self.task(path, status="DONE"))
        done = S.get_request(self.con, rid)
        self.assertEqual((done["status"], done["payment"], done["refunds"], done["state"]["lines"], done["req_ver"]),
                         ("PARTIAL", before["payment"], before["refunds"], before["state"]["lines"], after["req_ver"]))
        self.assertEqual(self.get_ok(path)["status"], "DONE")
        self.assertEqual(self.get_ok(QUEUE + "?status=done")["rows"][0]["id"], rid)

    def test_existing_mobile_appointment_and_new_collection_outcome(self):
        r, line = self.request()
        rid = r["id"]
        path = f"{QUEUE}/{rid}"
        seal, event = self.phone(r)
        first = [event("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 0, "reason": "NOT_PUT_OUT"}),
                 event("ATTEMPT", {"outcome": "NOT_COLLECTED"}),
                 event("REVISIT_APPT", {"date": "2098-12-18", "start": "09:00", "end": None})]
        self.assertEqual(self.post_ok("import/result", seal(first), self.admin_cookie)["applied"], 3)
        self.assertEqual(self.get_ok(path)["request_status"], "NOT_COLLECTED")
        self.assertEqual(self.get_ok(path)["appointment"], {"date": "2098-12-18", "start": "09:00", "end": None, "confirmed": True})
        self.post_ok(path, self.task(path, status="DONE"))
        self.assertEqual(self.get_ok(path)["status"], "DONE")
        current = self.get_ok(path)
        second = event("ATTEMPT", {"outcome": "ON_HOLD"})
        second["base_req_ver"] = current["req_ver"]
        self.assertEqual(self.post_ok("import/result", seal([second]), self.admin_cookie)["applied"], 1)
        detail = self.get_ok(path)
        self.assertEqual((detail["status"], detail["request_status"]), ("NEEDS_ACTION", "ON_HOLD"))
        self.assertEqual([h["action"] for h in detail["history"]], ["COMPLETE"])

    def test_contact_reassignment_overdue_and_retries(self):
        r, _ = self.request()
        rid = r["id"]
        path = f"{QUEUE}/{rid}"
        self.contact(r, "문의")
        self.assertEqual(self.get_ok(path)["status"], None)
        self.assertEqual(self.get_ok(QUEUE)["rows"], [])
        self.contact(r, "독촉")
        self.assertEqual(self.get_ok(path)["contacts"][0]["kind"], "독촉")
        first = self.task(path, due_date="2020-01-01")
        self.assertEqual(self.post_ok(path, first), self.post_ok(path, first))
        self.assertEqual(len(self.get_ok(path)["history"]), 1)
        self.assertEqual(self.get_ok(f"{QUEUE}?dong={self.a}&assignee={self.owner}&overdue=1")["rows"][0]["id"], rid)
        reassigned = self.task(path, self.second, status="IN_PROGRESS")
        self.post_ok(path, reassigned)
        self.assertEqual(self.get_ok(f"{QUEUE}?assignee={self.owner}&overdue=1")["rows"], [])
        self.assertEqual(self.post(path, first, self.cookie)[0], 409)
        completion = self.task(path, self.second, status="DONE")
        self.contact(r, "변경요청")
        self.assertEqual(self.post(path, completion, self.cookie)[0], 409)
        completion = self.task(path, self.second, status="DONE")
        self.assertEqual(self.post_ok(path, completion), self.post_ok(path, completion))
        detail = self.get_ok(path)
        self.assertEqual([h["action"] for h in detail["history"]], ["COMPLETE", "UPDATE", "CREATE"])
        self.assertEqual(detail["history"][1]["before"]["assignee"], "followup_owner")
        self.assertEqual(detail["history"][1]["after"]["assignee"], "followup_second")
        self.assertEqual(detail["history"][0]["actor"], "followup_owner")
        self.assertEqual(self.get_ok(QUEUE)["rows"], [])
        self.contact(r, "문의")
        self.assertEqual(self.get_ok(path)["status"], "DONE")
        self.contact(r, "독촉")
        self.post_ok(path, completion)
        self.assertEqual(self.get_ok(path)["status"], "NEEDS_ACTION")
        self.post_ok(path, self.task(path, self.second))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM request_followup").fetchone()[0], 1)

    def test_current_scope_and_assignment_validation(self):
        allowed, _ = self.request()
        other, _ = self.request(dong="동명동")
        self.contact(allowed, "독촉")
        self.contact(other, "변경요청")
        path = f"{QUEUE}/{allowed['id']}"
        foreign = f"{QUEUE}/{other['id']}"
        data = self.task(path)
        self.assertEqual({x["id"] for x in self.get_ok(QUEUE)["rows"]}, {allowed["id"]})
        self.assertNotIn(self.outside, {x["id"] for x in self.get_ok(path)["assignees"]})
        self.assertEqual(self.get(f"{QUEUE}?dong={self.b}", self.cookie)[0], 403)
        self.assertEqual(self.get(foreign, self.cookie)[0], 403)
        self.assertEqual(self.post(foreign, data, self.cookie)[0], 403)
        self.assertEqual(self.post(foreign + "/revisit", {}, self.cookie)[0], 403)
        self.assertEqual(self.post(path, {**data, "assignee_id": self.outside}, self.cookie)[0], 400)
        self.post_ok(path, data)
        users.set_user_dongs(self.con, self.owner, [self.b])
        self.assertEqual(self.get(path, self.cookie)[0], 403)
        self.assertEqual(self.post(path, data, self.cookie)[0], 403)

    def test_privacy_removes_next_action_and_keeps_assignment_history(self):
        r, _ = self.request()
        self.contact(r, "독촉")
        path = f"{QUEUE}/{r['id']}"
        self.post_ok(path, self.task(path, next_action="가상 민감 문구"))
        with C.tx(self.con):
            privacy.pseudonymize(self.con, r["id"], C.now())
        detail = self.get_ok(path)
        self.assertIsNone(detail["next_action"])
        self.assertFalse(detail["editable"])
        self.assertEqual(detail["history"][0]["after"]["assignee"], "followup_owner")
        self.assertNotIn("가상 민감 문구", self.con.execute(
            "SELECT detail FROM audit_log WHERE entity='request_followup'").fetchone()[0])
        self.assertEqual(self.post(path, self.task(path), self.cookie)[0], 400)
        with C.tx(self.con):
            self.con.execute("UPDATE request_followup SET next_action='가상 잔존 문구' WHERE request_id=?", (r["id"],))
        self.assertEqual(privacy.purge_pii(self.con)["repaired_requests"], 1)
        self.assertIsNone(self.con.execute("SELECT next_action FROM request_followup").fetchone()[0])
        self.assertTrue(privacy.verify_audit(self.con)["ok"])


if __name__ == "__main__":
    unittest.main()
