"""휴대폰 정리 경합 회귀: 임시 Chromium·실제 PIN/IndexedDB와 가상 자료만 사용."""
from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile
import unittest
import urllib.request
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server
from test_workflow_mobile import daylist, event, open_mobile, seed, stop


class MobileCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mobile_server()
        cls.url = cls.server.__enter__()
        cls.playwright = sync_playwright().start()
        options = {"headless": True}
        if os.environ.get("CHROME"):
            options["executable_path"] = os.environ["CHROME"]
        cls.browser = cls.playwright.chromium.launch(**options)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.__exit__(None, None, None)

    def setUp(self):
        self.context = self.page = None
        self.initial_events = []
        owner = self

        class ObservedBrowser:
            def new_context(self, *args, **kwargs):
                context = owner.browser.new_context(*args, **kwargs)
                owner.context = context
                owner.addCleanup(context.close)

                def observe(page):
                    owner.page = page
                    page.on("pageerror", lambda error: owner.initial_events.append({"kind": "pageerror", "message": str(error)}))
                    page.on("console", lambda message: owner.initial_events.append({"kind": "console", "message": message.text}) if message.type == "error" else None)
                    page.on("requestfailed", lambda request: owner.initial_events.append({
                        "kind": "requestfailed", "path": urlsplit(request.url).path, "error": request.failure}))
                    page.on("response", lambda response: owner.initial_events.append({
                        "kind": "http", "path": urlsplit(response.url).path, "status": response.status}) if response.status >= 400 else None)

                context.on("page", observe)
                return context

        try:
            self.context, self.page = open_mobile(ObservedBrowser(), self.url)
        except BaseException:
            self.capture_startup_failure()
            raise
        self.today = self.page.evaluate("async () => (await import('./core.js')).todayKST()")
        self.prepare()

    def capture_startup_failure(self):
        if os.environ.get("DG_TEST_DIR"):
            shots = Path(os.environ["DG_TEST_DIR"]) / "shots"
            shots.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="mobile-cleanup-startup-", dir=shots))
        else:
            directory = Path(tempfile.mkdtemp(prefix="dg-mobile-cleanup-startup-"))
        report = {"test": self.id(), "url": self.url, "events": self.initial_events, "probes": []}
        if self.page:
            try:
                report["page"] = self.page.evaluate("""() => ({url:location.href,readyState:document.readyState,
                    body:document.body?.innerText.slice(0,8000),scripts:[...document.scripts].map(script=>script.src),
                    resources:performance.getEntriesByType('resource').map(item=>({name:new URL(item.name).pathname,duration:item.duration,transferSize:item.transferSize}))})""")
                self.page.screenshot(path=str(directory / "startup.png"), full_page=True)
            except Exception as error:
                report["capture_error"] = repr(error)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path in ("", "app.js"):
            try:
                with opener.open(self.url + path, timeout=1) as response:
                    report["probes"].append({"path": path or "/", "status": response.status, "bytes": len(response.read())})
            except Exception as error:
                report["probes"].append({"path": path or "/", "error": repr(error)})
        (directory / "startup.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("초기 화면 실패 증거: " + str(directory), flush=True)

    def prepare(self, **extra):
        seed(self.page, {
            "daylist": daylist(self.today, [stop(1, self.today)]),
            "events": [event(1)], "releasedSeq": 1,
            "exports": [{"from": 1, "to": 1, "code": "TEST0001", "confirmed": True}],
            "workers": ["가상 작업자"], "worker": "가상 작업자", **extra,
        })
        self.page.evaluate("document.querySelectorAll('.toast').forEach(node => node.remove())")

    def open_export(self):
        self.page.evaluate("async () => { location.hash = '#/export'; await (await import('./core.js')).render(); }")
        expect(self.page.get_by_role("button", name="오늘 자료 정리", exact=True)).to_be_visible()
        return self.page.get_by_role("button", name="오늘 자료 정리", exact=True)

    def open_confirmation(self):
        button = self.open_export()
        expect(button).to_be_enabled()
        button.click()
        dialog = self.page.get_by_role("dialog", name="확인해 주세요", exact=True)
        expect(dialog).to_be_visible()
        return dialog

    def confirm(self, dialog):
        dialog.get_by_role("button", name="정리하기", exact=True).click()
        expect(dialog).to_have_count(0)
        # 대화상자 promise의 후속 작업과 화면 교체까지 처리한다. 고정 시간 대기는 쓰지 않는다.
        self.page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def state(self):
        return self.page.evaluate("""async () => {
            const C = await import('./core.js'), S = await import('./store.js');
            return { current: C.st, stored: await S.getSecure('state'), pending: C.pendingPhotos.length };
        }""")

    def queue_photo(self):
        self.page.evaluate("""async () => {
            const C = await import('./core.js');
            C.pendingPhotos.push({ stopId: 'workflow-1', phase: 'BEFORE', file: new File(['fixture'], '가상.jpg') });
        }""")

    def hold_storage_completion(self, operation, key):
        self.page.evaluate("""({operation, key}) => {
            const original = IDBObjectStore.prototype[operation];
            const completion = Object.getOwnPropertyDescriptor(IDBTransaction.prototype, 'oncomplete');
            const gate = new Promise(resolve => { window.__releaseCleanupStorage = resolve; });
            window.__cleanupStorageReached = false;
            IDBObjectStore.prototype[operation] = function(...args) {
                if ((operation === 'put' ? args[1] : args[0]) === key) {
                    IDBObjectStore.prototype[operation] = original;
                    const transaction = this.transaction;
                    const hold = callback => completion.set.call(transaction, event => {
                        window.__cleanupStorageReached = true;
                        gate.then(() => callback.call(transaction, event));
                    });
                    const callback = completion.get.call(transaction);
                    if (callback) hold(callback);
                    else Object.defineProperty(transaction, 'oncomplete', { configurable: true, set: hold });
                }
                return original.apply(this, args);
            };
        }""", {"operation": operation, "key": key})

    def test_pending_photo_blocks_cleanup_until_ready(self):
        self.queue_photo()
        expect(self.open_export()).to_be_disabled()
        self.assertEqual(self.state()["pending"], 1)
        self.assertIsNotNone(self.state()["current"]["daylist"])
        self.page.evaluate("async () => { const C = await import('./core.js'); C.pendingPhotos.length = 0; await C.render(); }")
        expect(self.page.get_by_role("button", name="오늘 자료 정리", exact=True)).to_be_enabled()

    def test_new_unsent_event_during_confirmation_is_not_erased(self):
        dialog = self.open_confirmation()
        self.page.evaluate("""async () => {
            const C = await import('./core.js');
            await C.addEvent(C.st.daylist.stops[0], 'CONTACT', {result: 'NO_ANSWER'});
        }""")
        self.confirm(dialog)
        state = self.state()
        for location in ("current", "stored"):
            self.assertIsNotNone(state[location]["daylist"])
            self.assertEqual(len(state[location]["events"]), 2)
            self.assertEqual((state[location]["seq"], state[location]["releasedSeq"]), (2, 1))

    def test_photo_queued_during_confirmation_keeps_its_target(self):
        dialog = self.open_confirmation()
        self.queue_photo()
        self.confirm(dialog)
        state = self.state()
        self.assertEqual(state["pending"], 1)
        self.assertIsNotNone(state["current"]["daylist"])
        self.assertEqual(state["current"]["daylist"]["stops"][0]["request_id"], "workflow-1")

    def test_leaving_export_before_confirmation_does_not_clear_data(self):
        dialog = self.open_confirmation()
        self.page.evaluate("async () => { location.hash = '#/home'; await (await import('./core.js')).render(); }")
        self.confirm(dialog)
        self.assertIsNotNone(self.state()["current"]["daylist"])
        self.assertEqual(self.state()["current"]["seq"], 1)

    def test_replaced_state_is_not_cleared_by_old_confirmation(self):
        dialog = self.open_confirmation()
        self.page.evaluate("""async () => {
            const C = await import('./core.js');
            C.setSt({...C.st, daylist: {...C.st.daylist, crew: '다음 가상 수거팀'}});
            await C.commit();
        }""")
        self.confirm(dialog)
        state = self.state()
        self.assertIsNotNone(state["current"]["daylist"])
        self.assertEqual(state["current"]["daylist"]["crew"], "다음 가상 수거팀")

    def test_new_event_while_deleting_recovered_photo_is_not_erased(self):
        photo = {"photo_id": "recovered-photo", "request_id": "workflow-1", "phase": "AFTER", "sha256": "fixture", "bytes": 1}
        self.prepare(keptPhotos=[photo], recoveredPhotos=[photo["photo_id"]])
        self.page.evaluate("async () => (await import('./store.js')).putSecureBytes('ph:recovered-photo', new Uint8Array([7]))")
        dialog = self.open_confirmation()
        self.hold_storage_completion("delete", "b:ph:recovered-photo")
        dialog.get_by_role("button", name="정리하기", exact=True).click()
        self.page.wait_for_function("() => window.__cleanupStorageReached")
        self.page.evaluate("""async () => {
            const C = await import('./core.js');
            await C.addEvent(C.st.daylist.stops[0], 'CONTACT', {result: 'NO_ANSWER'});
            window.__releaseCleanupStorage();
        }""")
        expect(self.page.locator(".toast")).to_be_visible()
        state = self.state()
        for location in ("current", "stored"):
            self.assertIsNotNone(state[location]["daylist"])
            self.assertEqual(len(state[location]["events"]), 2)
            self.assertEqual((state[location]["seq"], state[location]["releasedSeq"]), (2, 1))

    def test_cleanup_completion_does_not_redirect_a_new_screen(self):
        dialog = self.open_confirmation()
        self.hold_storage_completion("put", "s:state")
        dialog.get_by_role("button", name="정리하기", exact=True).click()
        self.page.wait_for_function("() => window.__cleanupStorageReached")
        self.page.evaluate("""async () => {
            const C = await import('./core.js');
            location.hash = '#/settings'; await C.render();
            window.__releaseCleanupStorage();
            await C.saving;
        }""")
        self.assertEqual(self.page.evaluate("location.hash"), "#/settings")

    def test_storage_recovery_reenables_cleanup_on_the_same_screen(self):
        self.page.evaluate("""async () => {
            const C = await import('./core.js');
            const original = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function(value, key) {
                if (key === 's:state') throw new DOMException('가상 저장 공간 부족', 'QuotaExceededError');
                return original.apply(this, arguments);
            };
            try { await C.commit(); } finally { IDBObjectStore.prototype.put = original; }
        }""")
        button = self.open_export()
        expect(button).to_be_disabled()
        self.assertTrue(self.page.evaluate("async () => { const C = await import('./core.js'), S = await import('./store.js'); return C.saveFailed && S.hasPendingSecure('state'); }"))
        self.page.evaluate("async () => (await import('./core.js')).commit()")
        self.assertTrue(self.page.evaluate("async () => { const C = await import('./core.js'), S = await import('./store.js'); return !C.saveFailed && !S.hasPendingSecure('state'); }"))
        expect(button).to_be_enabled()
        button.click()
        self.confirm(self.page.get_by_role("dialog", name="확인해 주세요", exact=True))
        self.assertIsNone(self.state()["current"]["daylist"])

    def test_failed_cleanup_save_does_not_report_success_or_leave_export(self):
        dialog = self.open_confirmation()
        self.page.evaluate("""() => {
            const original = IDBObjectStore.prototype.put;
            IDBObjectStore.prototype.put = function(value, key) {
                if (key === 's:state') {
                    IDBObjectStore.prototype.put = original;
                    throw new DOMException('가상 저장 공간 부족', 'QuotaExceededError');
                }
                return original.apply(this, arguments);
            };
        }""")
        self.confirm(dialog)
        expect(self.page.locator('#save-fail')).to_be_visible()
        self.assertEqual(self.page.evaluate('location.hash'), '#/export')
        expect(self.page.get_by_text('정리했습니다', exact=True)).to_have_count(0)

    def test_cancel_preserves_data_and_allows_retry(self):
        dialog = self.open_confirmation()
        dialog.get_by_role("button", name="취소", exact=True).click()
        expect(dialog).to_have_count(0)
        expect(self.page.get_by_role("button", name="오늘 자료 정리", exact=True)).to_be_enabled()
        self.assertEqual(len(self.state()["current"]["events"]), 1)

    def test_normal_cleanup_without_photos_preserves_sequence_and_generation(self):
        before = self.state()["current"]
        self.confirm(self.open_confirmation())
        state = self.state()
        for location in ("current", "stored"):
            self.assertIsNone(state[location]["daylist"])
            self.assertEqual(state[location]["events"], [])
            self.assertEqual(state[location]["keptPhotos"], [])
            self.assertEqual((state[location]["seq"], state[location]["releasedSeq"]), (1, 1))
            self.assertEqual(state[location]["gen"], before["gen"])
            self.assertEqual(state[location]["workers"], before["workers"])

    def test_normal_cleanup_keeps_unrecovered_photos_and_settings(self):
        photo = {"photo_id": "keep-photo", "request_id": "workflow-1", "phase": "BEFORE", "sha256": "fixture", "bytes": 3}
        self.prepare(keptPhotos=[photo])
        self.page.evaluate("async () => (await import('./store.js')).putSecureBytes('ph:keep-photo', new Uint8Array([1,2,3]))")
        self.confirm(self.open_confirmation())
        state = self.state()
        for location in ("current", "stored"):
            self.assertIsNone(state[location]["daylist"])
            self.assertEqual(state[location]["events"], [])
            self.assertEqual(state[location]["keptPhotos"], [photo])
            self.assertEqual(state[location]["workers"], ["가상 작업자"])
            self.assertEqual((state[location]["seq"], state[location]["releasedSeq"]), (1, 1))
        self.assertEqual(self.page.evaluate("async () => [...await (await import('./store.js')).getSecureBytes('ph:keep-photo')]"), [1, 2, 3])

    def test_normal_cleanup_deletes_duplicate_recovered_photo_metadata(self):
        photo = {"photo_id": "recovered-photo", "request_id": "workflow-1", "phase": "AFTER", "sha256": "fixture", "bytes": 1}
        self.prepare(keptPhotos=[photo, photo], recoveredPhotos=[photo["photo_id"]])
        self.page.evaluate("async () => (await import('./store.js')).putSecureBytes('ph:recovered-photo', new Uint8Array([7]))")
        self.confirm(self.open_confirmation())
        state = self.state()
        self.assertIsNone(state["current"]["daylist"])
        self.assertEqual(state["current"]["keptPhotos"], [])
        self.assertIsNone(self.page.evaluate("async () => (await import('./store.js')).getSecureBytes('ph:recovered-photo')"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
