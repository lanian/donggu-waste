"""재발급 QR과 이전 QR 결과의 PC 수거 완료 보호를 가상 자료로 확인한다."""
import base64
import hashlib
import http.client
import json
import os
import unittest
import uuid

import test_label_usage_api as fixture
import dgproto as P
from dgpc import assign as AS, core as C, service as S


class CompletedPickupQRTest(fixture.UsageHTTPCase):
    def setUp(self):
        super().setUp()
        self.day = C.today_kst()
        self.kex, self.sign, self.device = P.gen_key(), P.gen_key(), uuid.uuid4().bytes
        code = self.device + P.pub_raw(self.kex) + P.pub_raw(self.sign)
        code += hashlib.sha256(code).digest()[:4]
        registered = S.register_device(self.con, "DGD1." + base64.urlsafe_b64encode(code).decode().rstrip("="), confirm=True)
        self.crew = S.save_crew(self.con, {"name": "가상 QR 수거팀", "device_id": registered["device_id"]})
        self.garage = {"label": "가상 차고지", "lat": 35.147, "lon": 126.92}
        self.unload = {"label": "가상 반입지", "lat": 35.152, "lon": 126.926}
        S.save_settings(self.con, {"depot": self.garage, "unload_site": self.unload})

    def assign(self, *requests, load=None):
        stops = [{"request_id": r["id"]} for r in requests]
        stops[-1]["unload_after"] = {"label": self.unload["label"], "eta": "10:00", "minutes": 10, "volume_m3": 1, "final": True}
        histories = None if load is None else {self.crew: {"depot": self.garage, "end": self.garage, "unload_site": self.unload,
                                                          "path": {}, "initial_load": load}}
        batch, = AS.confirm_plan(self.con, self.day, {self.crew: stops}, histories=histories)
        return batch["batch_id"]

    def collect_pc(self, request, quantity):
        current = S.get_request(self.con, request["id"])
        status, result, _ = self.post("requests/" + request["id"] + "/collection", {
            "req_ver": current["req_ver"], "basis": current["collection"]["basis"], "collected_on": self.day,
            "items": [{"line_id": current["items"][0]["line_id"], "qty": quantity}], "reason": "가상 수거 확인",
            "client_req_id": str(uuid.uuid4()),
        }, self.worker_cookie)
        self.assertEqual(status, 200, result)
        return result

    def receive(self, frames):
        collector = P.Collector()
        for frame in frames:
            bid, _, _ = collector.add(frame)
        return P.open_envelope(collector.assemble(bid), S.pc_key(self.con, "sign").public_key(), self.kex, self.device)[1]

    def full_qr(self, batch):
        return self.receive(AS.make_bundle(self.con, batch_id=batch)["frames"])

    def print_page(self, batch, mode):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request("GET", f"/print/batch/{batch[:8]}?mode={mode}", headers={"Cookie": self.admin_cookie})
            response = connection.getresponse()
            body = response.read().decode()
            self.assertEqual(response.status, 200, body)
            return body
        finally:
            connection.close()

    def test_reissued_full_qr_uses_existing_schema_and_keeps_remaining_pickup(self):
        partial, _ = self.request()
        done, _ = self.request()
        batch = self.assign(partial, done)
        before = P.expand_daylist(self.full_qr(batch), S.FEE_ROWS)
        self.assertEqual([s["request_id"] for s in before["stops"]], [partial["id"], done["id"]])
        self.assertEqual(self.collect_pc(partial, 1)["status"], "PARTIAL")
        self.assertEqual(self.collect_pc(done, 3)["status"], "DONE")

        reissued = self.full_qr(batch)
        self.assertEqual(reissued["schema"], "dg.daylist/2")
        visits = P.expand_daylist(reissued, S.FEE_ROWS)["stops"]
        self.assertEqual([s["request_id"] for s in visits], [partial["id"]])
        visit = visits[0]
        self.assertEqual((visit["request_id"], visit["seq"]), (partial["id"], 1))
        self.assertEqual(visit["items"][0]["prior_collected"], 1)
        self.assertEqual(visit["unload_after"]["stop_count"], 1)
        self.assertEqual(visit["unload_after"]["depot"], {k: self.garage[k] for k in ("lat", "lon")})
        audit = C.one(self.con, "SELECT detail FROM audit_log WHERE entity='bundle' AND action='DISPLAY' ORDER BY id DESC LIMIT 1")
        self.assertEqual(json.loads(audit["detail"])["stop_versions"], {partial["id"]: visit["req_ver"]})

    def test_protected_reprint_keeps_completed_row_without_a_visit_qr(self):
        remaining, _ = self.request()
        done, _ = self.request()
        batch = self.assign(remaining, done)
        self.collect_pc(done, 3)

        sheet = AS.dispatch_sheet(self.con, batch, protected=True)
        first, last = sheet["stops"]
        self.assertEqual([first["request_id"], last["request_id"]], [remaining["id"], done["id"]])
        self.assertEqual(last["status"], "DONE")
        self.assertTrue(last["unload_after"]["final"], "인쇄 위치는 확정 당시 최종 하차 지점을 유지한다")
        self.assertFalse(last["qr_frames"], "수거 완료 행에는 읽을 수 있는 QR이 없어야 한다")
        visit_payload = self.receive(first["qr_frames"])
        self.assertEqual(visit_payload["schema"], "dg.visit/1")
        visit, = P.expand_daylist({**visit_payload, "schema": "dg.daylist/2"}, S.FEE_ROWS)["stops"]
        self.assertEqual(visit["request_id"], remaining["id"])
        self.assertEqual(visit["unload_after"]["stop_count"], 1)
        protected = self.print_page(batch, "protected")
        self.assertIn("수거 완료", protected)
        self.assertNotIn('alt="2번 수거 건 QR', protected)
        self.assertNotIn(done["name"], protected)
        plain = self.print_page(batch, "plain")
        self.assertIn(done["name"], plain)
        self.assertIn("최종 하차 후 차고지 복귀", plain)

    def test_all_pc_done_reissues_a_valid_empty_list(self):
        done, _ = self.request()
        batch = self.assign(done)
        self.collect_pc(done, 3)
        bundled = AS.make_bundle(self.con, batch_id=batch)
        self.assertEqual(bundled["count"], 0)
        payload = self.receive(bundled["frames"])
        self.assertEqual(payload["schema"], "dg.daylist/2")
        self.assertEqual(P.expand_daylist(payload, S.FEE_ROWS)["stops"], [])

    def test_all_pc_done_keeps_explicit_vehicle_load_and_prints_one_final_unload(self):
        done, _ = self.request()
        load = {"volume_m3": 1, "weight_kg": 0}
        batch = self.assign(done, load=load)
        self.collect_pc(done, 3)

        payload = P.expand_daylist(self.full_qr(batch), S.FEE_ROWS)
        self.assertEqual((payload["stops"], payload["carry_load"], payload["final_unload"]["stop_count"]), ([], load, 0))
        protected = self.print_page(batch, "protected")
        self.assertIn("수거 완료", protected)
        self.assertEqual(protected.count("최종 하차 후 차고지 복귀"), 1)
        self.assertIn('alt="최종 하차 QR', protected)
        self.assertNotIn('alt="1번 수거 건 QR', protected)

    def test_phone_keeps_its_previous_results_and_can_send_them_after_reissue(self):
        from playwright.sync_api import sync_playwright
        from pc_tool import qr_png_data_uri
        from test_review_ui import mobile_server
        from test_workflow_mobile import open_mobile, seed

        done, line = self.request()
        batch = self.assign(done)
        before = P.expand_daylist(self.full_qr(batch), S.FEE_ROWS)
        self.collect_pc(done, 3)
        with mobile_server() as url, sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
            _, app = open_mobile(browser, url)
            code = app.evaluate("""async (registration) => {
                const P = await import('./proto.js');
                window.__workflowPc = P.parseRegistration(registration);
                const d = window.__workflowDevice;
                return P.deviceCode(d.id, await P.exportRaw(d.kex.publicKey), await P.exportRaw(d.sign.publicKey));
            }""", S.registration_text(self.con))
            device = S.register_device(self.con, code, confirm=True)["device_id"]
            S.save_crew(self.con, {"id": self.crew, "name": "가상 QR 수거팀", "device_id": device})
            seed(app, {"daylist": before, "events": [
                {"request_id": done["id"], "base_req_ver": done["req_ver"], "kind": "ITEM_RESULT",
                 "body": {"line_id": line, "qty_confirmed": 3, "qty_collected": 1}},
                {"request_id": done["id"], "base_req_ver": done["req_ver"], "kind": "ATTEMPT", "body": {"outcome": "PARTIAL"}},
            ]})
            frames = AS.make_bundle(self.con, batch_id=batch)["frames"]
            app.evaluate("""async (sources) => {
                const canvas = document.createElement('canvas'); canvas.width = canvas.height = 640;
                const ctx = canvas.getContext('2d'); ctx.imageSmoothingEnabled = false;
                const images = await Promise.all(sources.map(async (source) => {
                    const img = new Image(); img.src = source; await img.decode(); return img;
                }));
                let frame = 0;
                const draw = () => {
                    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 640, 640);
                    ctx.drawImage(images[Math.floor(frame++ / 8) % images.length], 20, 20, 600, 600);
                };
                navigator.mediaDevices.getUserMedia = async () => canvas.captureStream(15);
                draw(); setInterval(draw, 70);
                location.hash = '#/scan';
            }""", [qr_png_data_uri(frame)[0] for frame in frames])
            app.wait_for_url('**/#/received', timeout=20000)
            persisted = app.evaluate("""async () => {
                const st = await (await import('./store.js')).getSecure('state');
                return {removed: st.daylist.stops.map(s => [s.request_id, s.removed]), kinds: st.events.map(e => e.kind)};
            }""")
            self.assertEqual(persisted, {"removed": [[done["id"], True]], "kinds": ["ITEM_RESULT", "ATTEMPT"]})
            app.evaluate("location.hash = '#/export'")
            with app.expect_download() as download:
                app.get_by_role('button', name='결과 파일 만들어 보내기 (2건)', exact=True).click()
            imported = S.import_result(self.con, download.value.path().read_bytes())
            self.assertEqual(imported["conflict"], 2)
            self.assertEqual(S.get_request(self.con, done["id"])["status"], "DONE")
            browser.close()

    def test_even_a_current_pc_version_cannot_overwrite_done_on_result_import(self):
        done, line = self.request()
        batch = self.assign(done)
        old_stop, = P.expand_daylist(self.full_qr(batch), S.FEE_ROWS)["stops"]
        confirmed = self.collect_pc(done, 3)
        version = confirmed["req_ver"]
        bodies = [
            ("ITEM_RESULT", {"line_id": line, "qty_confirmed": 3, "qty_collected": 1, "pc_collection_ver": version}),
            ("ATTEMPT", {"outcome": "ON_HOLD", "pc_collection_ver": version}),
            ("CONTACT", {"result": "CALLBACK"}),
        ]
        events = [{"event_id": str(uuid.uuid4()), "seq": i, "request_id": done["id"], "assign_ver": old_stop["assign_ver"],
                   "base_req_ver": version, "kind": kind, "body": body, "device_ts": C.now()}
                  for i, (kind, body) in enumerate(bodies, 1)]
        header = P.Header(P.TYPE_RESULT, uuid.uuid4().bytes, self.device, old_stop["assign_ver"], 1, P.key_id(P.pub_raw(self.sign)))
        sealed, _ = P.seal({"schema": "dg.result/1", "work_date": self.day, "events": events}, header,
                           S.pc_key(self.con, "kex").public_key(), self.sign)
        imported = S.import_result(self.con, sealed)
        self.assertEqual((imported["conflict"], imported["applied"]), (2, 1))
        again = S.resolve_conflicts_bulk(self.con, "APPLY", "가상 충돌 확인", request_id=done["id"])
        self.assertEqual((again["resolved"], len(again["failed"])), (0, 2))
        remaining = S.get_request(self.con, done["id"])
        self.assertEqual((remaining["status"], remaining["state"]["lines"][0]["qty_collected"], remaining["state"]["contact"]), ("DONE", 3, "CALLBACK"))


if __name__ == "__main__":
    unittest.main()
