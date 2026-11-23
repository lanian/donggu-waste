"""미등록폰 결과 파일을 실제 PC API에서 검토하고 한 번만 반영한다."""

import base64
import json
import os
import unittest
import uuid
from unittest.mock import patch

import test_guest_list as lists
import test_label_usage_api as fixtures
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dgpc import close, core, ops, server, service


def encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class GuestWorkTests(fixtures.UsageHTTPCase):
    assign = lists.GuestListTests.assign
    open = lists.GuestListTests.open

    def setUp(self):
        super().setUp()
        self.day = core.today_kst()
        self.team = service.save_crew(self.con, {"name": "가상 현장팀"})
        self.req, self.line = self.request()
        self.batch = self.assign(self.req)

    def post_ok(self, path, body, cookie=None):
        status, data, _ = self.post(path, body, cookie or self.worker_cookie)
        self.assertEqual(status, 200, data)
        return data

    def work_list(self):
        opened = self.open(self.post_ok("guest-work-lists", {"batch_id": self.batch}))
        self.assertEqual(opened["schema"], "dg.guest-work/1")
        self.assertNotIn(self.req["id"], json.dumps(opened))
        return opened

    def file(self, pack, entries):
        key = base64.urlsafe_b64decode(pack["return_key"] + "=")
        iv = os.urandom(12)
        data = AESGCM(key).encrypt(iv, json.dumps({"entries": entries}).encode(), pack["ticket"].encode())
        return {
            "file": {
                "schema": "dg.guest-return/1",
                "ticket": pack["ticket"],
                "iv": encode(iv),
                "ciphertext": encode(data),
            }
        }

    def entry(self, kind, **fields):
        return {"id": str(uuid.uuid4()), "kind": kind, **fields}

    def test_review_apply_idempotence_and_fields(self):
        pack = self.work_list()
        entries = [
            self.entry(
                "collection",
                seq=1,
                qty=[1],
                confirmed=[2],
                additions=[{"name": "가상 의자", "qty": 1, "collected": 1}],
                reason="현장 수량 확인",
            ),
            self.entry(
                "intake",
                name="가상 접수인",
                phone="01012345678",
                address="가상로 17",
                dong="",
                items=[{"name": "가상 물건", "qty": 2}],
            ),
            self.entry("unload", site="가상 반입지", weight_kg=21.5),
        ]
        body = self.file(pack, entries)
        reviewed = self.post_ok("guest-work-preview", body)
        self.assertEqual(len(reviewed["rows"]), 3)
        self.assertTrue(all(not r["duplicate"] for r in reviewed["rows"]))
        self.assertIn("가상 의자", reviewed["rows"][0]["detail"])
        self.assertIn("가상로 17", reviewed["rows"][1]["detail"])

        def counts():
            return [
                self.con.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                for table in ("request", "unload_log", "result_event", "device")
            ]

        self.assertEqual(counts(), [1, 0, 0, 0])
        self.assertEqual(self.post("guest-work-apply", body, self.worker_cookie)[0], 400)
        data = {**body, "preview_token": reviewed["preview_token"]}
        self.assertNotEqual(self.post("guest-work-apply", data, self.admin_cookie)[0], 200)
        self.assertEqual(self.post_ok("guest-work-apply", data), {"applied": 3, "duplicates": 0})
        expected = counts()
        self.assertEqual(expected[:2], [2, 1])
        self.assertGreater(expected[2], 0)
        self.assertEqual(expected[3], 0)
        self.assertEqual(service.get_request(self.con, self.req["id"])["status"], "PARTIAL")
        intake = core.one(self.con, "SELECT id,origin_device FROM request WHERE address=?", ("가상로 17",))
        self.assertTrue(intake["origin_device"])
        self.assertIsNone(service.get_request(self.con, intake["id"])["items"][0]["unit_price"])
        self.assertEqual(self.post_ok("guest-work-apply", data), {"applied": 0, "duplicates": 3})
        new = self.file(self.work_list(), entries)
        again = self.post_ok("guest-work-preview", new, self.admin_cookie)
        self.assertTrue(all(r["duplicate"] for r in again["rows"]))
        self.assertEqual(
            self.post_ok("guest-work-apply", {**new, "preview_token": again["preview_token"]}, self.admin_cookie),
            {"applied": 0, "duplicates": 3},
        )
        self.assertEqual(counts(), expected)
        for scope in (None, self.team):
            history = [row for row in ops.recent_imports(self.con, crew_id=scope) if row["counts"].get("guest_entry")]
            self.assertEqual({r["counts"]["guest_entry"] for r in history}, {e["id"] for e in entries})
            self.assertEqual(len(history), 3)
            self.assertTrue(all(r["crew"] == "가상 현장팀" for r in history))
        self.assertFalse(ops.recent_imports(self.con, crew_id="다른 팀"))
        entries[2]["weight_kg"] = 999
        self.assertNotEqual(self.post("guest-work-preview", self.file(pack, entries), self.worker_cookie)[0], 200)

    def test_changed_assignment_stale_collection_bad_file_and_closed_day(self):
        pack = self.work_list()
        unload = self.entry("unload", site="가상 반입지", weight_kg=2)
        collection = self.entry("collection", seq=1, qty=[3], reason="", additions=[])
        body = self.file(pack, [unload, collection])
        review = self.post_ok("guest-work-preview", body)
        for bad in ({"iv": "----"}, {"ticket": "bad"}, {"ciphertext": "AA"}):
            self.assertEqual(
                self.post("guest-work-preview", {"file": {**body["file"], **bad}}, self.worker_cookie)[0], 400
            )
        for entry in ({**collection, "qty": [4]}, {**collection, "seq": 99}, {**unload, "weight_kg": -1}):
            self.assertNotEqual(self.post("guest-work-preview", self.file(pack, [entry]), self.worker_cookie)[0], 200)
        with core.tx(self.con):
            self.con.execute("UPDATE request SET req_ver=req_ver+1 WHERE id=?", (self.req["id"],))
        self.assertNotEqual(
            self.post("guest-work-apply", {**body, "preview_token": review["preview_token"]}, self.worker_cookie)[0],
            200,
        )
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM unload_log").fetchone()[0], 0)
        body = self.file(pack, [unload])
        self.assign(self.req)
        self.assertNotEqual(self.post("guest-work-preview", body, self.worker_cookie)[0], 200)
        self.batch = self.assign(self.req)
        body = self.file(self.work_list(), [unload])
        review = self.post_ok("guest-work-preview", body)
        close.close_day(self.con, self.day, "합성 자료 마감")
        self.assertNotEqual(
            self.post("guest-work-apply", {**body, "preview_token": review["preview_token"]}, self.worker_cookie)[0],
            200,
        )
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM unload_log").fetchone()[0], 0)

    def test_work_permissions(self):
        for path in ("guest-work-lists", "guest-work-preview", "guest-work-apply"):
            for role in ("ADMIN", "INTAKE", "FIELD", "DONG", "LEAD"):
                self.assertEqual(server.allowed(role, "POST", path), role in ("ADMIN", "INTAKE"))

    def test_administrative_dong_review_matches_final_intake(self):
        pack = self.work_list()
        intake = self.entry(
            "intake",
            name="가상 신청인",
            phone="01012345678",
            address="가상로 55",
            dong="충장동",
            items=[{"name": "가상 소파", "qty": 1}],
        )
        body = self.file(pack, [intake])
        with patch("dgpc.area.detect_dong", return_value={"source": "ADDR", "dong": "동명동"}):
            review = self.post_ok("guest-work-preview", body)
            self.assertIn("동명동", review["rows"][0]["detail"])
        with patch("dgpc.area.detect_dong", return_value={"source": "ADDR", "dong": "충장동"}):
            status, _, _ = self.post(
                "guest-work-apply", {**body, "preview_token": review["preview_token"]}, self.worker_cookie
            )
            self.assertEqual(status, 409)
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM request").fetchone()[0], 1)
            review = self.post_ok("guest-work-preview", body)
            self.post_ok("guest-work-apply", {**body, "preview_token": review["preview_token"]})
        self.assertEqual(core.one(self.con, "SELECT dong FROM request WHERE address='가상로 55'")["dong"], "충장동")

    def test_file_binding_expiry_partial_replay_and_atomic_domain_failure(self):
        pack = self.work_list()
        unload = self.entry("unload", site="첫 반입지", weight_kg=15)
        body = self.file(pack, [unload])
        preview = self.post_ok("guest-work-preview", body)
        with patch("dgpc.guest_work.REVIEW_SECONDS", -1):
            status, _, _ = self.post(
                "guest-work-apply", {**body, "preview_token": preview["preview_token"]}, self.worker_cookie
            )
            self.assertNotEqual(status, 200)
        altered = self.file(pack, [{**unload, "weight_kg": 16}])
        self.assertNotEqual(
            self.post("guest-work-apply", {**altered, "preview_token": preview["preview_token"]}, self.worker_cookie)[
                0
            ],
            200,
        )
        self.post_ok("guest-work-apply", {**body, "preview_token": preview["preview_token"]})
        second = self.entry("unload", site="다른 반입지", weight_kg=20)
        intake = self.entry(
            "intake",
            name="가상 신청인",
            phone="01012345678",
            address="가상로 56",
            dong="",
            items=[{"name": "가상 소파", "qty": 1}],
        )
        mixed = self.file(pack, [unload, intake, second])
        preview = self.post_ok("guest-work-preview", mixed)
        self.assertEqual([r["duplicate"] for r in preview["rows"]], [True, False, False])
        with patch("dgpc.assign.add_unload", side_effect=core.UserError("합성 하차 저장 실패")):
            self.assertNotEqual(
                self.post(
                    "guest-work-apply", {**mixed, "preview_token": preview["preview_token"]}, self.worker_cookie
                )[0],
                200,
            )
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM request").fetchone()[0], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM audit_log WHERE entity='guest_work'").fetchone()[0], 1)
        self.assertEqual(
            self.post_ok("guest-work-apply", {**mixed, "preview_token": preview["preview_token"]}),
            {"applied": 2, "duplicates": 1},
        )
        close.close_day(self.con, self.day, "합성 자료 마감")
        self.assertEqual(
            self.post_ok("guest-work-apply", {**mixed, "preview_token": preview["preview_token"]}),
            {"applied": 0, "duplicates": 3},
        )

    def test_team_close_distinguishes_issue_from_receipt_and_recovers_guest_results(self):
        row = lambda: close.day_summary(self.con, self.day, self.team)["result_recovery"][0]
        self.assertEqual(row()["receipt_status"], "UNSENT")
        self.post_ok("guest-lists", {"batch_id": self.batch})
        self.assertEqual(row()["receipt_status"], "UNSENT")
        pack = self.work_list()
        self.assertEqual(row()["receipt_status"], "GUEST_ISSUED")
        self.assertEqual(row()["stage"], "WAITING")
        self.assertIsNone(row()["receipt_confirmed_at"])
        self.assertEqual(close.crews_without_result(self.con, self.day)[0]["crew_id"], self.team)

        def pending_count():
            return next(i["count"] for i in ops.pending(self.con, self.day)["items"] if i["key"] == "no_result_crews")

        self.assertEqual(pending_count(), 1)
        self.assertEqual(close.day_summary(self.con, self.day, "other-team")["result_recovery"], [])
        body = self.file(pack, [self.entry("collection", seq=1, qty=[3], reason="", additions=[])])
        review = self.post_ok("guest-work-preview", body)
        self.assertEqual(row()["missing"], 1)
        self.post_ok("guest-work-apply", {**body, "preview_token": review["preview_token"]})
        result = row()
        self.assertEqual(
            (result["receipt_status"], result["stage"], result["applied"], result["pc"], result["missing"]),
            ("GUEST_ISSUED", "COMPLETE", 1, 0, 0),
        )
        self.assertEqual(close.crews_without_result(self.con, self.day), [])
        self.assertEqual(pending_count(), 0)


if __name__ == "__main__":
    unittest.main()
