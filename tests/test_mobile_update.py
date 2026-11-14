"""실제 SW/PIN/IndexedDB: 최초 설치 문서의 명시 업데이트와 저장 보호.

현재 web 파일에서 app/SW 버전만 바꾸는 로컬 fixture다. 과거 Git 객체나 공개
서비스를 사용하지 않으며, 실제 50개 파일 캐시·설치·활성화·재시작을 거친다.
"""
import base64
import contextlib
import http.server
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import unquote, urlsplit

from PIL import Image
from playwright.sync_api import expect, sync_playwright
from test_review_ui import PIN
from test_workflow_mobile import seed, stop


WEB = Path(__file__).resolve().parent.parent / 'web'
OLD, NEW = '99.0.1', '99.0.2'  # fixture versions, not public-release proof
PROBE = """window.__updateProbe={documentId:crypto.randomUUID(),initialController:!!navigator.serviceWorker.controller,changes:0};
navigator.serviceWorker.addEventListener('controllerchange',()=>window.__updateProbe.changes++);"""


@contextlib.contextmanager
def update_server():
    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, '.js': 'text/javascript', '.mjs': 'text/javascript'}

        def do_GET(self):
            rel = unquote(urlsplit(self.path).path).lstrip('/') or 'index.html'
            path = (WEB / rel).resolve()
            if not path.is_relative_to(WEB.resolve()) or not path.is_file():
                self.send_error(404)
                return
            # Hold a precache-only icon so the first claim occurs after the PIN input.
            if rel == 'icons/icon-512.png' and not self.server.install_gate.wait(20):
                self.send_error(503)
                return
            data = path.read_bytes()
            if rel == 'app.js':
                data = re.sub(rb"APP_VERSION = '[^']+'", f"APP_VERSION = '{self.server.version}'".encode(), data)
            elif rel == 'sw.js':
                data = re.sub(rb"const VERSION = '[^']+'", f"const VERSION = '{self.server.version}'".encode(), data)
            self.send_response(200)
            self.send_header('Content-Type', self.guess_type(str(path)))
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        request_queue_size = 128
        daemon_threads = True

    server = Server(('127.0.0.1', 0), Handler)
    server.version, server.install_gate = OLD, threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f'http://127.0.0.1:{server.server_address[1]}/'
    finally:
        server.install_gate.set()
        server.shutdown()
        server.server_close()


def unlock(page):
    page.get_by_placeholder('PIN', exact=True).fill(PIN)
    page.get_by_role('button', name='잠금 해제', exact=True).click()
    expect(page.get_by_role('region', name='자주 쓰는 기능', exact=True)).to_be_visible()


def prepare(browser, server, url):
    context = browser.new_context(service_workers='allow', viewport={'width': 390, 'height': 844})
    context.add_init_script(PROBE)
    page = context.new_page()
    page.goto(url)
    entry = page.get_by_placeholder('숫자 6자리 이상')
    entry.fill(PIN)
    doc = page.evaluate('window.__updateProbe.documentId')
    assert page.evaluate('window.__updateProbe.initialController') is False
    assert page.evaluate('!!navigator.serviceWorker.controller') is False
    server.install_gate.set()
    page.wait_for_function('()=>!!navigator.serviceWorker.controller && window.__updateProbe.changes >= 1')
    assert page.evaluate('window.__updateProbe.documentId') == doc
    expect(entry).to_have_value(PIN)  # automatic first claim must preserve an input document
    page.get_by_placeholder('한 번 더', exact=True).fill(PIN)
    page.get_by_role('button', name='PIN 설정', exact=True).click()
    expect(page.get_by_role('button', name='이 휴대폰만으로 사용', exact=True)).to_be_visible()
    today = page.evaluate("import('./core.js').then(C=>C.todayKST())")
    seed(page, {'mode': 'SOLO', 'requests': [stop(1, today, receipt_no='UPDATE-FIXTURE')], 'textSize': 'xl'}, registered=False)
    page.evaluate("""async()=>{const C=await import('./core.js');await C.setPrefs({theme:'dark',hc:true});
      await C.setAutoLockEnabled(false);await C.addEvent(C.st.requests[0],'FIELD_MEMO',{text:'가상 보존 기록'});}""")
    return context, page, doc


def offer(page, server):
    server.version = NEW
    page.evaluate('navigator.serviceWorker.getRegistration().then(r=>r.update())')
    button = page.get_by_role('button', name='업데이트', exact=True)
    expect(button).to_be_visible(timeout=20000)
    return button


def snapshot(page):
    return page.evaluate("""async()=>{const C=await import('./core.js'),S=await import('./store.js');
      return{state:C.st,prefs:await S.getRaw('prefs'),autoLock:await S.getRaw('auto-lock'),
        photos:(await S.keysRaw()).filter(k=>k.startsWith('b:ph:')).sort()};}""")


def still_waiting(page, doc):
    assert page.evaluate('window.__updateProbe.documentId') == doc
    assert page.evaluate("import('./app.js').then(A=>A.APP_VERSION)") == OLD
    assert page.evaluate('navigator.serviceWorker.getRegistration().then(r=>!!r.waiting)')


def verify_updated(page, before, doc):
    expect(page.get_by_placeholder('PIN', exact=True)).to_be_visible(timeout=10000)
    assert page.evaluate('window.__updateProbe.documentId') != doc
    assert page.evaluate("import('./app.js').then(A=>A.APP_VERSION)") == NEW
    unlock(page)
    assert snapshot(page) == before, '업데이트 전 저장된 접수·기록·사진·설정을 보존한다'
    assert page.evaluate("import('./core.js').then(C=>C.autoLockStatus().enabled)") is False
    assert page.evaluate('caches.keys()').count('dg-app-' + NEW) == 1


def first_document_case(browser, shots):
    with update_server() as (server, url):
        context, page, doc = prepare(browser, server, url)
        try:
            update = offer(page, server)
            page.evaluate("import('./core.js').then(C=>C.lockNow())")
            update.click()
            expect(page.get_by_role('alert').filter(has_text='잠금을 푼 뒤 업데이트하세요')).to_be_visible()
            still_waiting(page, doc)
            unlock(page)
            image = io.BytesIO()
            Image.new('RGB', (8, 8), '#0b7a55').save(image, 'JPEG')
            page.evaluate("""async data=>{const C=await import('./core.js');C.pendingPhotos.push({stopId:'workflow-1',phase:'BEFORE',lineId:null,
              file:new File([Uint8Array.from(atob(data),c=>c.charCodeAt(0))],'fixture.jpg',{type:'image/jpeg'}),at:new Date().toISOString(),fileTs:null,geo:null});}""", base64.b64encode(image.getvalue()).decode())
            update.click()
            expect(page.get_by_role('alert').filter(has_text='사진·기록 저장이 끝난 뒤')).to_be_visible()
            still_waiting(page, doc)
            assert page.evaluate("import('./core.js').then(C=>C.pendingPhotos.length)") == 1
            page.evaluate("import('./core.js').then(C=>C.flushPhotos())")
            assert page.evaluate("import('./core.js').then(C=>C.pendingPhotos.length)") == 0
            # Delay completion of an actual encrypted state write, not a fabricated controller event.
            page.evaluate("""async()=>{const C=await import('./core.js');const put=IDBObjectStore.prototype.put,
              complete=Object.getOwnPropertyDescriptor(IDBTransaction.prototype,'oncomplete');window.__holdUpdateSave=true;
              IDBObjectStore.prototype.put=function(value,key){if(key==='s:state'&&window.__holdUpdateSave)this.transaction.__holdUpdateSave=true;return put.call(this,value,key)};
              Object.defineProperty(IDBTransaction.prototype,'oncomplete',{...complete,set(fn){complete.set.call(this,typeof fn!=='function'?fn:function(event){
                if(this.__holdUpdateSave)window.__releaseUpdateSave=()=>fn.call(this,event);else fn.call(this,event);})}});
              window.__updateSave=C.addEvent(C.st.requests[0],'FIELD_MEMO',{text:'가상 저장 대기 기록'});}""")
            page.wait_for_function("()=>typeof window.__releaseUpdateSave==='function'")
            update.click()
            expect(page.locator('.update')).to_contain_text('저장을 마치는 중')
            still_waiting(page, doc)
            before = snapshot(page)
            assert len(before['photos']) == 1 and len(before['state']['events']) == 3
            page.evaluate('async()=>{window.__holdUpdateSave=false;window.__releaseUpdateSave();await window.__updateSave}')
            verify_updated(page, before, doc)
            page.screenshot(path=str(shots / 'first-document-updated.png'), full_page=True)
            print('PASS: 첫 설치 claim은 입력 보존, 같은 문서 명시 업데이트→실제 재시작/PIN/새버전; 잠금·사진·지연저장 보호와 자료/설정 보존', flush=True)
        finally:
            context.close()


def controlled_document_case(browser, shots):
    with update_server() as (server, url):
        context, page, _ = prepare(browser, server, url)
        try:
            page.reload()
            unlock(page)
            assert page.evaluate('window.__updateProbe.initialController') is True
            doc, before = page.evaluate('window.__updateProbe.documentId'), snapshot(page)
            offer(page, server).click()
            verify_updated(page, before, doc)
            page.screenshot(path=str(shots / 'controlled-document-updated.png'), full_page=True)
            print('PASS: 기존 controller 문서의 명시 업데이트/PIN/자료보존', flush=True)
        finally:
            context.close()


def main():
    output = Path(os.environ.get('DG_TEST_DIR') or tempfile.mkdtemp(prefix='dg-mobile-update-')) / 'shots'
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get('CHROME') or pw.chromium.executable_path, headless=True)
        try:
            first_document_case(browser, output)
            controlled_document_case(browser, output)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
