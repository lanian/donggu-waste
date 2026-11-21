"""휴대폰 화면 개선 2단계 "다음 행동 하나로" 회귀 (docs/reviews/2026-11-15-mobile-ui-expert-review.md P1 5·6·7·8, P2 15).

실행: python tests/test_mobile_ui_phase2.py [시험 이름 ...] (CHROME=Chromium 실행 파일, 이름 없으면 전부)
실제 PIN·암호화 IndexedDB를 쓰고, 자료는 모두 가상 자료다. 보낼 기록/확인 대기는 화면 표시용 계산만 바꾼다
(releasedSeq·exports 저장 형식과 결과 파일에 담는 기록은 그대로)인지까지 확인한다.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server, unlock
from test_workflow_mobile import daylist, event, open_mobile, route, seed, stop

passed = 0
SHOTS = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-mobile-ui-phase2-")) / "shots"
OLD_WORDS = ("보내지 않은 기록", "중간 결과 보내기", "미반영 기록", "결과 전달·반영 확인", "배정 목록 수신", "PC 연결 등록하기", "미전송")


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message, flush=True)


def today_of(page):
    return page.evaluate("async () => (await import('./core.js')).todayKST()")


def ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def aged(ev, hours):
    return {**ev, "ts": ago(hours)}


def events(page, kind=None):
    return page.evaluate("""async kind => (await import('./core.js')).st.events
        .filter(e => !kind || e.kind === kind).map(e => ({ id: e.event_id, rid: e.request_id, kind: e.kind, body: e.body }))""", kind)


def capture_external(page):
    page.evaluate("() => { window.__opened = []; window.__dgExternal = (u) => { window.__opened.push(u); }; }")


def visible_primaries(page):
    return page.evaluate("""() => [...document.querySelectorAll('main .btn.primary')]
        .filter(b => { const r = b.getBoundingClientRect(), s = getComputedStyle(b); return r.width && r.height && s.visibility !== 'hidden' && !b.closest('[hidden]'); })
        .map(b => ({ text: b.textContent.trim(), inNext: !!b.closest('.next-action') }))""")


def before(page, a, b):
    """a 가 문서 순서에서 b 보다 앞이면 True (둘 다 있어야 함)."""
    return page.evaluate("""([a, b]) => { const x = document.querySelector(a), y = document.querySelector(b);
        return !!x && !!y && !!(x.compareDocumentPosition(y) & Node.DOCUMENT_POSITION_FOLLOWING); }""", [a, b])


def probe(page, var):
    return page.evaluate("""v => { const e = document.createElement('i'); e.style.color = `var(${v})`; document.body.append(e);
        const c = getComputedStyle(e).color; e.remove(); return c; }""", var)


def rgb(text):
    return tuple(float(x) for x in re.findall(r"[\d.]+", text)[:3])


def contrast(a, b):
    def lum(c):
        def ch(v):
            v /= 255
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, bl = (ch(x) for x in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def set_theme(page, theme, hc=False):
    page.evaluate("async p => { const C = await import('./core.js'); await C.setPrefs(p); await C.render(); }", {"theme": theme, "hc": hc})


# ------------------------------------------------------------ A. 홈 순서와 주 행동 하나
def home_one_action(browser, url):
    print("\n— A. 홈: 머리글 → 지금 할 일(주 행동 하나) → 경고 띠 → 자주 쓰는 기능 → 진행 → 거르기 → 목록 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        records = [stop(1, today), stop(2, today, phone="01000000002"), stop(3, today, lat=35.1, lon=126.9, phone=""), stop(4, today)]
        evs = [aged(event(1, "ATTEMPT", outcome="DONE"), 5), aged(event(4, "ATTEMPT", outcome="ON_HOLD"), 5)]
        seed(page, {"daylist": daylist(today, records), "events": evs})
        capture_external(page)
        prim = visible_primaries(page)
        ok(len(prim) == 1 and prim[0]["inNext"] and prim[0]["text"] == "2번 수거 이어가기",
           f"홈의 보이는 주 단추(.btn.primary)는 '지금 할 일' 하나 ({prim})")
        ok(page.locator(".list-jump").count() == 0 and page.get_by_role("button", name="수거 목록 바로 보기").count() == 0,
           "[수거 목록 바로 보기] 단추 없음")
        ok(before(page, ".next-action", ".unsent-banner") and before(page, ".unsent-banner", ".home-tools")
           and before(page, ".home-tools", ".summary") and before(page, ".summary", ".pc-list-filters")
           and before(page, ".pc-list-filters", "ul.stops"), "순서: 지금 할 일 → 경고 띠 → 자주 쓰는 기능 → 진행 요약 → 거르기 → 목록")
        ok(before(page, "main > .bar", ".next-action"), "머리글 다음에 바로 지금 할 일")
        quick = page.locator(".next-quick")
        expect(quick).to_have_count(1)
        names = [b.inner_text().replace("\n", " ").strip() for b in quick.get_by_role("button").all()]
        ok(len(names) == 2 and names[0].startswith("2번 길안내") and names[1] == "2번 전화", f"지금 할 일 아래 [2번 길안내][2번 전화] ({names})")
        nav = quick.get_by_role("button").nth(0)
        ok(nav.is_disabled() and "위치 없음" in nav.inner_text(), "위치 없는 지점은 [2번 길안내] 비활성 · '위치 없음'")
        quick.get_by_role("button", name="2번 전화", exact=True).click()
        page.wait_for_function("() => window.__opened.length > 0")
        ok(page.evaluate("window.__opened")[-1] == "tel:01000000002" and events(page, "DIAL_INTENT")[-1]["rid"] == "workflow-2",
           "[2번 전화]는 기존 dial()로 2번 주민에게")
        ok(page.locator(".summary .sum-sub", has_text="재방문·보류").count() == 0
           and page.get_by_role("button", name="재방문·보류 1", exact=True).count() == 1,
           "진행 요약의 '재방문·보류' 칩은 빼고 거르기 탭만 남김")
        band = page.locator(".unsent-banner")
        ok(band.locator("button").count() == 0 and band.get_by_role("link", name="보내기 ›", exact=True).get_attribute("href") == "#/export",
           "미전송 경고는 단추 없는 띠 + 링크형 [보내기 ›]")

        # 360×640: 지금 할 일 주 단추가 첫 화면에 완전히 보임
        page.set_viewport_size({"width": 360, "height": 640})
        route(page, "#/home")
        page.evaluate("() => scrollTo(0, 0)")
        box = page.locator(".next-action .btn.primary").bounding_box()
        bar = page.locator("main > .bar").bounding_box()
        tab = page.locator(".tabbar").bounding_box()
        ok(box["y"] >= bar["y"] + bar["height"] - 0.5 and box["y"] + box["height"] <= tab["y"] + 0.5,
           f"360×640: 지금 할 일 주 단추가 앱 바 아래·탭바 위에 완전히 보임 (단추 {box['y']:.0f}~{box['y'] + box['height']:.0f}, 바 {bar['y'] + bar['height']:.0f}, 탭 {tab['y']:.0f})")
        page.screenshot(path=str(SHOTS / "phase2-home-360x640.png"))
        page.set_viewport_size({"width": 390, "height": 844})

        # 다음 지점에 위치가 있고 연락처가 없으면: 길안내 사용, 전화 비활성
        seed(page, {"daylist": daylist(today, records), "events": [event(1, "ATTEMPT", outcome="DONE"), event(2, "ATTEMPT", outcome="DONE")]})
        capture_external(page)
        quick = page.locator(".next-quick")
        ok(quick.get_by_role("button", name="3번 전화", exact=True).is_disabled(), "연락처 없는 지점은 [3번 전화] 비활성")
        quick.get_by_role("button", name=re.compile(r"^3번 길안내")).click()
        page.wait_for_function("async () => (await import('./core.js')).st.events.some(e => e.kind === 'NAV_INTENT' && e.request_id === 'workflow-3')")
        ok(True, "[3번 길안내]는 기존 navBtn으로 3번 좌표")

        # 남은 수거가 없으면 지금 할 일이 보내기를 맡고, 자주 쓰는 기능은 자리 그대로(주 단추가 아님)
        done = [event(n, "ATTEMPT", outcome="DONE") for n in (1, 2, 3, 4)]
        seed(page, {"daylist": daylist(today, records), "events": done})
        expect(page.locator(".next-action .btn.primary")).to_have_text("결과 보내기")
        tool = page.locator(".home-tools").get_by_role("button", name="결과 보내기 · 보낼 기록 4건", exact=True)
        ok(tool.count() == 1 and "primary" not in tool.get_attribute("class").split() and len(visible_primaries(page)) == 1,
           "남은 수거가 없으면 지금 할 일 = [결과 보내기], 자주 쓰는 기능은 같은 자리에 보통 단추로")
        ok(page.locator(".next-quick").count() == 0, "다음 지점이 아니면 길안내·전화 두 칸 없음")

        # 기기 미등록: 같은 내용을 되풀이하는 등록 카드 없음
        seed(page, {"daylist": daylist(today, records)}, registered=False)
        expect(page.locator(".next-action .btn.primary")).to_have_text("기기 등록")
        ok(page.get_by_text("먼저 관리자와 함께 기기 등록을 해 주세요.").count() == 0 and len(visible_primaries(page)) == 1,
           "기기 미등록: 지금 할 일 [기기 등록] 하나, 별도 등록 카드 없음")

        # 지난 날짜 목록 띠·대기 사진 띠도 주 단추를 늘리지 않음
        yesterday = page.evaluate("async () => (await import('./core.js')).todayKST(-1)")
        seed(page, {"daylist": daylist(yesterday, [stop(1, yesterday)]), "events": [event(1)]})
        ok(page.locator(".day-banner").count() == 1 and len(visible_primaries(page)) == 1, f"지난 목록 띠가 있어도 주 단추 하나 ({visible_primaries(page)})")
        page.evaluate("""async () => { const C = await import('./core.js');
            C.pendingPhotos.push({ stopId: 'workflow-1', phase: 'BEFORE', file: null, at: new Date().toISOString() }); await C.render(); }""")
        prim = visible_primaries(page)
        ok(len(prim) == 1 and prim[0]["inNext"], f"대기 사진 띠의 [사진 다시 저장]도 주 단추가 아님 ({prim})")
        page.evaluate("async () => { const C = await import('./core.js'); C.pendingPhotos.length = 0; }")

        # 단독 방식도 같은 원칙
        seed(page, {"mode": "SOLO", "requests": [stop(1, today, lat=35.1, lon=126.9, phone="01000000001"), stop(2, today)], "lastBackupAt": None}, registered=False)
        prim = visible_primaries(page)
        ok(len(prim) == 1 and prim[0]["text"] == "1번 수거 이어가기", f"단독: 주 단추는 지금 할 일 하나 ({prim})")
        ok(before(page, ".next-action", ".backup-warn") and before(page, ".backup-warn", ".home-tools") and before(page, ".home-tools", "ul.stops"),
           "단독: 지금 할 일 → 백업 경고 띠 → 자주 쓰는 기능 → 목록")
        ok(page.locator(".list-jump").count() == 0 and page.locator(".next-quick").get_by_role("button").count() == 2,
           "단독: [수거 목록 바로 보기] 없음, 지금 할 일 아래 [1번 길안내][1번 전화]")
        page.screenshot(path=str(SHOTS / "phase2-solo-home.png"), full_page=True)
    finally:
        ctx.close()


# ------------------------------------------------------------ B. 보낼 기록 / 확인 대기
def outbox_terms(browser, url):
    print("\n— B. '보낼 기록'(어느 파일에도 없음)과 '확인 대기'(보냈지만 D-코드 전)를 나눠 보임 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        records = [stop(1, today), stop(2, today), stop(3, today)]
        evs = [aged(event(1, "CONTACT", result="REACHED"), 10), aged(event(2, "CONTACT", result="REACHED"), 10), aged(event(3, "CONTACT", result="REACHED"), 4)]
        exp = [{"from": 1, "to": 2, "code": "ABCDEFGH", "name": "DG_R_가상_1-2.dgr", "at": ago(9), "confirmed": False}]
        seed(page, {"daylist": daylist(today, records), "events": evs, "exports": exp})
        info = page.evaluate("""async () => { const C = await import('./core.js'), F = await import('./field.js');
            return { o: F.outboxInfo(C.st.events, C.st.releasedSeq, C.st.exports), u: C.unsent().length, r: C.st.releasedSeq }; }""")
        ok(info["o"]["toSend"] == 1 and info["o"]["waiting"] == 2 and info["u"] == 3 and info["r"] == 0,
           f"순수 함수: 보낼 기록 1·확인 대기 2, 기존 unsent()는 그대로 3, releasedSeq 그대로 0 ({info})")
        band = page.locator(".unsent-banner")
        expect(band).to_contain_text("보낼 기록 1건")
        ok("4시간 전" in band.inner_text() and "10시간" not in band.inner_text(), f"경고 띠의 시간은 보낼 기록 중 가장 오래된 것 ({band.inner_text()})")
        ok(page.locator(".home-tools").get_by_role("button", name="결과 보내기 · 보낼 기록 1건", exact=True).count() == 1,
           "자주 쓰는 기능: [결과 보내기 · 보낼 기록 1건]")
        text = page.locator("main").inner_text()
        ok(not [w for w in OLD_WORDS if w in text], f"홈에 옛 문구 없음 ({[w for w in OLD_WORDS if w in text]})")
        page.screenshot(path=str(SHOTS / "phase2-home-outbox.png"), full_page=True)

        route(page, "#/export")
        head = page.locator("h2", has_text="결과 보내기").first.inner_text()
        ok("보낼 기록 1건" in head and "확인 대기 2건" in head and "미전송" not in page.locator("main").inner_text(),
           f"결과 보내기 화면 제목: '보낼 기록 1건 · 확인 대기 2건' ({head})")
        ok(page.get_by_role("button", name="결과 파일 만들어 보내기 (3건)", exact=True).count() == 1,
           "결과 파일에는 지금처럼 D-코드 전 기록 전부(3건)가 들어감 — 전송 형식 그대로")
        page.locator("details.send-card > summary").click()  # 3단계: D-코드 대기면 ① 카드는 접혀 있음
        page.locator("details.send-more summary").click()  # 3단계: 긴 설명은 [자세히] 안
        ok("지금까지 결과" in page.locator("main").inner_text() and "중간 결과" not in page.locator("main").inner_text(), "'중간 결과' → '지금까지 결과' ([자세히] 안)")

        route(page, "#/settings")
        dl = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('dt')].map(dt => [dt.textContent.trim(), dt.nextElementSibling?.textContent.trim()]))""")
        ok(dl.get("보낼 기록") == "1건" and dl.get("확인 대기") == "2건" and "미전송 기록" not in dl, f"설정 정보표: '보낼 기록 1건' / '확인 대기 2건' 두 줄 ({dl})")
        page.locator("details.set-group.admin > summary").click()  # 4단계: 기기 등록은 관리자 설정(기본 접힘) 안
        ok(page.get_by_role("button", name="기기 등록", exact=True).count() == 1 and page.get_by_role("button", name="기기 등록 화면").count() == 0,
           "설정: [기기 등록 화면] → [기기 등록]")
        text = page.locator("main").inner_text()
        ok("미전송" not in text and "보내지 않은 기록" not in text, "설정에도 '미전송'·'보내지 않은 기록' 없음")

        # 보낼 기록이 0이고 확인 대기만: 경고 띠 대신 조용한 안내
        exp_all = [{**exp[0], "to": 3}]
        seed(page, {"daylist": daylist(today, records), "events": evs, "exports": exp_all})
        ok(page.locator(".unsent-banner").count() == 0, "보낼 기록 0 → 경고 띠 없음")
        note = page.locator(".waiting-note")
        expect(note).to_contain_text("확인 대기 3건 — PC의 D-코드를 넣으세요")
        ok("info" in note.get_attribute("class").split() and note.locator("button").count() == 0, "확인 대기만 있으면 조용한 안내(.callout.info, 단추 없음)")
        ok(page.locator(".home-tools").get_by_role("button", name="결과 보내기 · 확인 대기 3건", exact=True).count() == 1,
           "자주 쓰는 기능: [결과 보내기 · 확인 대기 3건]")

        # 지금 할 일 문구: 받기·보내기·코드 넣기
        done = [event(n, "ATTEMPT", outcome="DONE") for n in (1, 2, 3)]
        seed(page, {"daylist": daylist(today, records), "events": done, "exports": [{**exp[0], "to": 3}]})
        expect(page.locator(".next-action .btn.primary")).to_have_text("결과 반영 코드 넣기")
        ok(True, "모두 보냈고 D-코드만 남음 → 지금 할 일 [결과 반영 코드 넣기]")
        seed(page, {"daylist": daylist(today, records), "events": done})
        expect(page.locator(".next-action .btn.primary")).to_have_text("결과 보내기")
        expect(page.locator(".next-action .next-detail")).to_contain_text("보낼 기록 3건")
        ok(True, "보낼 기록이 있으면 [결과 보내기], 설명은 '보낼 기록 3건'")
        seed(page)
        expect(page.locator(".next-action .btn.primary")).to_have_text("오늘 목록 받기")
        ok(True, "목록이 없으면 [배정 목록 수신] → [오늘 목록 받기]")
        seed(page, registered=False)
        expect(page.locator(".next-action .btn.primary")).to_have_text("기기 등록")
        ok(True, "미등록이면 [PC 연결 등록하기] → [기기 등록]")

        # 지난 날짜 목록: 목록 받기 전 알림도 같은 두 용어
        yesterday = page.evaluate("async () => (await import('./core.js')).todayKST(-1)")
        seed(page, {"daylist": daylist(yesterday, [stop(n, yesterday) for n in (1, 2, 3)]), "events": evs, "exports": exp})
        expect(page.locator(".next-action .btn.primary")).to_have_text("결과 보내기")
        route(page, "#/scan")
        owed = page.locator(".owed-card")
        expect(owed).to_contain_text("보낼 기록 1건 · 확인 대기 2건")
        ok("callout" in owed.get_attribute("class").split() and "warn" in owed.get_attribute("class").split(), "지난 목록 결과 알림: '보낼 기록 1건 · 확인 대기 2건' (.callout.warn)")
    finally:
        ctx.close()


# ------------------------------------------------------------ C. 안내 상자 네 종류
KINDS = ("info", "ok", "warn", "bad")


def callout_colors(page):
    return page.evaluate("""kinds => { const out = {};
        for (const k of [...kinds, 'notice']) {
            const e = document.createElement('p'); e.className = k === 'notice' ? 'notice' : 'callout ' + k; e.textContent = '가상 안내';
            document.querySelector('main').append(e); const s = getComputedStyle(e);
            out[k] = { bg: s.backgroundColor, ink: s.color, shadow: s.boxShadow }; e.remove();
        } return out; }""", list(KINDS))


def callouts(browser, url):
    print("\n— C. .callout 네 종류(info·ok·warn·bad)와 사용처 분류 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today), stop(2, today)]), "events": [aged(event(1, "CONTACT", result="REACHED"), 5)]})
        for theme, hc in (("light", False), ("dark", False), ("light", True), ("dark", True)):
            set_theme(page, theme, hc)
            c = callout_colors(page)
            bgs = {k: c[k]["bg"] for k in KINDS}
            ok(len(set(bgs.values())) == 4, f"{theme}{' 고대비' if hc else ''}: 네 종류의 배경색이 모두 다름 ({bgs})")
            worst = min(contrast(rgb(c[k]["ink"]), rgb(c[k]["bg"])) for k in KINDS)
            ok(worst >= 4.5, f"{theme}{' 고대비' if hc else ''}: 네 종류 모두 글자 대비 4.5:1 이상 (최저 {worst:.2f})")
            ok(c["notice"]["bg"] == c["warn"]["bg"] and c["notice"]["shadow"] == c["warn"]["shadow"], f"{theme}: 예전 .notice 는 .callout.warn 과 같은 모양")
        set_theme(page, "light")
        c = callout_colors(page)
        ok(c["info"]["bg"] == probe(page, "--field-bg") and "4px" in c["info"]["shadow"] and c["ok"]["bg"] == probe(page, "--ok-bg")
           and c["bad"]["bg"] == probe(page, "--bad-bg") and c["warn"]["bg"] == probe(page, "--warn-bg"),
           "토큰: info=--field-bg(왼쪽 4px 띠), ok=--ok-bg, warn=--warn-bg, bad=--bad-bg")

        reps = {}
        band = page.locator(".unsent-banner")
        ok({"callout", "warn"} <= set(band.get_attribute("class").split()), "홈 보낼 기록 경고 → .callout.warn")
        reps["warn"] = band.evaluate("e => getComputedStyle(e).backgroundColor")

        route(page, "#/intake")
        intro = page.get_by_text("수거 중 주민이 새로 요청했거나", exact=False)
        cls = intro.evaluate("e => (e.closest('.callout') || e).className")
        ok("info" in cls.split() and "warn" not in cls.split() and page.locator("main .notice").count() == 0,
           f"접수 첫머리 설명은 경고가 아닌 .callout.info ({cls})")
        reps["info"] = intro.evaluate("e => getComputedStyle(e.closest('.callout') || e).backgroundColor")
        seed(page, {"daylist": daylist(today, [stop(1, today)])}, registered=False)
        route(page, "#/intake")
        warn = page.locator(".callout.warn", has_text="아직 PC에 등록되지 않았습니다")
        ok(warn.count() == 1 and page.locator(".card.notice.warn").count() == 0, "기기 미등록 안내는 .callout.warn (규칙 없는 'card notice warn' 정리)")

        seed(page, {"daylist": daylist(today, [stop(1, today)])})
        page.evaluate("""async () => { const C = await import('./core.js');
            C.ui.receipt = { code: 'ABCD EFGH', count: 1, ver: 2, diff: '가상 1곳 시간 바뀜', rel: { events: 2, photos: 1 } };
            location.hash = '#/received'; await C.render(); }""")
        rel, diff = page.locator(".rel-note"), page.locator(".diff-summary")
        ok({"callout", "ok"} <= set(rel.get_attribute("class").split()) and {"callout", "info"} <= set(diff.get_attribute("class").split()),
           "받기 완료: 'PC 반영 정보도 함께 받았습니다' → .callout.ok, '바뀐 내용' → .callout.info")
        reps["ok"] = rel.evaluate("e => getComputedStyle(e).backgroundColor")
        page.screenshot(path=str(SHOTS / "phase2-received.png"))

        seed(page, {"daylist": {**daylist(today, [stop(1, today)]), "fee_table": {"version_id": "가상-다른-수수료표"}}})
        route(page, "#/add/workflow-1")
        bad = page.locator(".callout.bad")
        expect(bad).to_contain_text("수수료표 버전이 다릅니다")
        ok(bad.get_attribute("role") == "alert" and bad.get_by_role("button", name="← 접수로 돌아가기", exact=True).count() == 1,
           "수수료표 버전 불일치(막힘 오류) → .callout.bad(role=alert) + 돌아갈 단추")
        reps["bad"] = bad.evaluate("e => getComputedStyle(e).backgroundColor")
        page.screenshot(path=str(SHOTS / "phase2-fee-mismatch.png"))
        ok(len(set(reps.values())) == 4, f"대표 화면 네 곳(홈 경고·접수 첫머리·받기 완료·버전 불일치)의 배경색이 서로 다름 ({reps})")

        route(page, "#/calculator")
        ok(page.locator(".callout.bad", has_text="요금표 업데이트 필요").count() == 1, "빠른 계산의 요금표 업데이트 필요도 .callout.bad")

        # 정적 점검: 화면 모듈에 옛 .notice 사용처가 남지 않음
        root = Path(__file__).resolve().parents[1] / "web"
        left = [f"{p.name}:{i}" for p in [*root.glob("*.js"), *(root / "screens").glob("*.js")] if p.name not in ("labels-data.js", "fees-data.js")
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if re.search(r"class: '(?:[^']*\s)?notice(?:\s[^']*)?'", line)]
        ok(not left, f"web/ 화면 코드에 class 'notice' 사용처가 남지 않음 ({left})")
    finally:
        ctx.close()


# ------------------------------------------------------------ D. 현장 화면 문구와 빈 상태
def field_wording(browser, url):
    print("\n— D. 현장 화면에서 PC·관리자 설명을 빼고, 빈 상태에 다음 행동 —")
    ctx, page = open_mobile(browser, url)
    try:
        today = today_of(page)
        seed(page, {"daylist": daylist(today, [stop(1, today)])})
        route(page, "#/intake")
        expect(page.locator(".addr-status")).to_have_text("주소를 직접 적어 주세요. 정확한 위치는 사무실이 PC에서 정합니다.")
        page.get_by_text("휴대폰 저장 후 PC에 보내는 방법", exact=True).click()
        text = page.locator("main").inner_text()
        ok("PC 경유" not in text and "[+ 현장 품목 추가]" in text and "[품목 추가]" not in text,
           "접수: 주소 검색 PC 경유 설명 → '주소를 직접 적어 주세요…', 안내의 [품목 추가] → [+ 현장 품목 추가]")
        page.evaluate("async () => { const C = await import('./core.js'); C.st.addrRoute = 'direct'; C.st.jusoKey = ''; await C.render(); }")
        expect(page.locator(".addr-status")).to_have_text("주소 검색을 쓸 수 없습니다. 주소를 직접 적어 주세요.")
        ok("승인키" not in page.locator("main").inner_text(), "승인키 안내는 현장 화면에서 빠짐 (설정의 주소 검색 칸에만)")

        route(page, "#/find")
        ok(page.get_by_text("날짜 기준", exact=True).count() == 1 and page.get_by_role("button", name="조건 지우기", exact=True).count() == 1
           and "조회조건" not in page.locator("main").inner_text(), "찾기: '목록일·예정일 기준' → '날짜 기준', '조회조건 초기화' → '조건 지우기'")

        # 빈 상태
        seed(page)
        empty = page.locator(".empty")
        expect(empty).to_have_text("오늘 목록이 없습니다. PC 담당자의 QR을 받으세요.")
        # 3단계: 지금 할 일이 같은 [오늘 목록 받기]면 빈 상태에는 문구만(단추 중복 없음)
        ok(page.locator(".empty-actions").count() == 0, "빈 상태에는 같은 단추를 되풀이하지 않음")
        page.locator(".next-action").get_by_role("button", name="오늘 목록 받기", exact=True).click()
        expect(page).to_have_url(url + "#/scan")
        ok(True, "홈 빈 상태: '오늘 목록이 없습니다. PC 담당자의 QR을 받으세요.' + 지금 할 일 [오늘 목록 받기]")

        seed(page, {"daylist": daylist(today, [stop(1, today), stop(2, today)])})
        page.get_by_role("button", name="재방문·보류 0", exact=True).click()
        expect(page.locator(".empty")).to_have_text("'재방문·보류'에 해당하는 곳이 없습니다.")
        page.locator(".empty-actions").get_by_role("button", name="전체 보기", exact=True).click()
        expect(page.locator(".stops .stop")).to_have_count(2)
        ok(page.evaluate("async () => (await import('./core.js')).ui.filter") == "ALL", "거르기 결과 없음: \"'재방문·보류'에 해당하는 곳이 없습니다.\" + [전체 보기]")

        seed(page, {"daylist": daylist(today, [stop(1, today)]), "mapEnabled": False})
        route(page, "#/map")
        expect(page.locator(".empty")).to_have_text("지도가 꺼져 있습니다.")
        page.get_by_role("button", name="지도 켜기", exact=True).click()
        page.wait_for_function("async () => (await import('./core.js')).st.mapEnabled === true")
        expect(page.locator("#map")).to_have_count(1)
        ok(True, "지도 꺼짐: '지도가 꺼져 있습니다.' + [지도 켜기](설정과 같은 동작)로 바로 지도")
        page.evaluate("async () => { const C = await import('./core.js'); C.setMapEnabled ? await C.setMapEnabled(false) : (C.st.mapEnabled = false); }")

        seed(page, {"mode": "SOLO", "requests": [stop(1, today)], "lastBackupAt": None}, registered=False)
        ok(page.get_by_text("아직 백업하지 않았습니다. 휴대폰을 잃어버리면 접수 원본이 사라집니다.").count() == 1
           and page.get_by_text("마지막 백업: 없음").count() == 0, "단독: '마지막 백업: 없음.' → '아직 백업하지 않았습니다…'")
    finally:
        ctx.close()


# ------------------------------------------------------------ E. 스크린리더 알림과 초점
def focused(page):
    return page.evaluate("""() => { const a = document.activeElement;
        return { fk: a?.dataset?.fk || null, name: a?.getAttribute('aria-label') || a?.textContent?.trim() || '', tag: a?.tagName, cls: a?.className || '',
                 connected: !!a?.isConnected, disabled: !!a?.disabled }; }""")


def tab_to(page, name, presses=80):
    page.evaluate("() => { scrollTo(0, 0); document.activeElement?.blur(); }")
    for _ in range(presses):
        page.keyboard.press("Tab")
        if page.evaluate("n => document.activeElement?.getAttribute('aria-label') === n", name):
            return True
    return False


def screen_reader(browser, url):
    print("\n— E. 항상 있는 알림 영역, PIN·검증 문구 role, 수량 단추 이름, 다시 그린 뒤 초점 복원 —")
    ctx, page = open_mobile(browser, url)
    try:
        html = page.evaluate("() => fetch('index.html', { cache: 'no-store' }).then(r => r.text())")
        ok('<div id="sr-polite" class="toast-host" aria-live="polite" aria-atomic="true"></div>' in html
           and '<div id="sr-alert" class="toast-host"></div>' in html, "index.html 에 항상 있는 알림 영역 #sr-polite(polite)·오류 토스트 자리 #sr-alert")
        ok("connect-src 'self' https://tiles.openfreemap.org https://business.juso.go.kr;" in html, "CSP 허용 서버는 그대로")
        layout = page.evaluate("""() => ['sr-polite', 'sr-alert'].map(id => { const e = document.getElementById(id), r = e.getBoundingClientRect();
            return { inApp: !!e.closest('#app'), w: r.width, h: r.height, pos: getComputedStyle(e).position }; })""")
        ok(all(not x["inApp"] and x["w"] == 0 and x["h"] == 0 and x["pos"] == "fixed" for x in layout), f"알림 영역은 자리를 차지하지 않고 #app 밖 ({layout})")

        today = today_of(page)
        s1 = stop(1, today)
        s1["items"][0].update(qty=3, amount=3000)
        seed(page, {"daylist": daylist(today, [s1, stop(2, today)])})
        msg = "가상 안내 문구입니다. 사십 자 정도가 되도록 길게 적은 안내 문구를 보여 줍니다"
        page.evaluate("async m => (await import('./core.js')).toast(m)", msg)
        page.wait_for_function("m => document.getElementById('sr-polite').textContent === m", arg=msg)
        role = page.locator(".toast").last.get_attribute("role")
        ok(role is None and page.locator("#sr-polite > .toast").count() == 1 and page.get_by_text(msg).count() == 1,
           f"안내 토스트는 #sr-polite 안에 하나만(같은 글을 두 곳에 두지 않음, 토스트 자신은 role 없음: {role})")
        page.wait_for_timeout(4300)
        ok(page.locator(".toast", has_text="가상 안내 문구").count() == 1, f"안내 토스트는 글자 수만큼 오래 남음 (4.3초 뒤에도 보임, {len(msg)}자)")
        page.wait_for_timeout(1500 + len(msg) * 90 - 4300 + 500)
        ok(page.locator(".toast", has_text="가상 안내 문구").count() == 0, f"Math.max(4000, 1500 + {len(msg)}×90)ms 뒤 사라짐")
        page.evaluate("async () => (await import('./core.js')).toast('가상 오류 알림', 'bad')")
        page.wait_for_function("() => document.querySelector('#sr-alert > .toast.bad')?.firstChild?.textContent === '가상 오류 알림'")
        ok(page.locator("#sr-alert > .toast.bad").get_attribute("role") == "alert" and page.locator("#sr-alert").get_attribute("role") is None
           and page.locator("#sr-alert .tx").get_attribute("aria-hidden") == "true", "오류 토스트는 자신이 role=alert(담는 자리는 role 없음, 겹쳐 읽지 않음), '닫기 ✕'는 읽지 않음")
        page.locator(".toast.bad").click()

        # 수량 + 를 Tab 으로 → Enter 두 번 → 다시 그린 뒤에도 같은 + 에 초점, 값 단추 이름에 새 숫자
        route(page, "#/stop/workflow-1")
        group = page.get_by_role("group", name="가상 의자 실은 수량", exact=True)
        ok(group.count() == 1 and group.get_by_role("button", name="가상 의자 실은 수량 0개, 눌러서 직접 입력", exact=True).count() == 1,
           "수량 묶음 role=group, 값 단추 이름 '가상 의자 실은 수량 0개, 눌러서 직접 입력'")
        ok(tab_to(page, "가상 의자 실은 수량 더하기"), "Tab 으로 [실은 수량 +]에 초점")
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")
        page.wait_for_function("async () => (await import('./core.js')).st.events.some(e => e.kind === 'ITEM_RESULT' && e.body.qty_collected === 2)")
        page.wait_for_function("() => document.activeElement?.dataset?.fk === 'line-1:collected:+' && document.activeElement.isConnected")
        f = focused(page)
        ok(f["fk"] == "line-1:collected:+" and not f["disabled"], f"다시 그린 뒤 초점이 같은 [+]에 돌아옴 ({f})")
        ok(page.get_by_role("button", name="가상 의자 실은 수량 2개, 눌러서 직접 입력", exact=True).count() == 1, "값 단추 이름에 새 숫자 '2개'")
        page.keyboard.press("Enter")  # 현장 수량을 다 채우면 같은 + 로 수량을 더 적을 수 있다.
        page.wait_for_function("async () => (await import('./core.js')).st.events.some(e => e.kind === 'ITEM_RESULT' && e.body.qty_collected === 3)")
        more = page.get_by_role("button", name="가상 의자 현장·실은 수량 늘리기", exact=True)
        expect(more).to_be_focused()
        page.keyboard.press("Enter")
        increase = page.get_by_role("dialog", name="수량 늘려 수거", exact=True)
        expect(increase).to_be_visible()
        increase.get_by_role("button", name="취소", exact=True).click()
        expect(more).to_be_focused()
        ok(page.get_by_role("button", name="가상 의자 실은 수량 3개, 눌러서 직접 입력", exact=True).count() == 1,
           "다 채운 뒤에도 [+] 초점을 유지하고 수량 늘리기 취소 뒤 이전 수량을 보존")

        # 연락 결과·결과 단추도 같은 키로 초점 복원
        page.get_by_role("button", name="통화 완료", exact=True).focus()
        page.keyboard.press("Enter")
        page.wait_for_function("async () => (await import('./core.js')).st.events.some(e => e.kind === 'CONTACT')")
        page.wait_for_function("() => document.activeElement?.dataset?.fk === 'workflow-1:contact:REACHED'")
        ok(True, "연락 결과 단추: 다시 그린 뒤 같은 단추에 초점")
        page.locator("#st-result").get_by_role("button", name="전부 실음 · 수거 완료", exact=True).focus()
        page.keyboard.press("Enter")
        page.wait_for_function("async () => (await import('./core.js')).st.events.some(e => e.kind === 'ATTEMPT')")
        page.wait_for_function("() => document.activeElement?.dataset?.fk === 'workflow-1:result:DONE'")
        ok(True, "수거 결과 단추: 저장 뒤 같은 결과 단추(선택형)에 초점")
        sticker = page.locator('.chk[data-line="line-1"] [data-result="OK"]')
        ok(sticker.get_attribute("data-fk") == "line-1:sticker:OK", "스티커 확인 단추에도 고정 키")

        # 처리 단계 링크: 스크롤한 뒤 그 구역 제목으로 초점, 단계 이름에 완료/남음
        route(page, "#/stop/workflow-2")
        labels = [a.get_attribute("aria-label") for a in page.locator(".steps .step").all()]
        ok(labels == ["1단계 연락, 남음", "2단계 품목, 남음", "3단계 사진, 남음", "4단계 결과, 남음"], f"단계 이름 ({labels})")
        page.locator(".steps .step").nth(3).click()
        page.wait_for_function("() => document.activeElement?.tagName === 'H2' && !!document.activeElement.closest('#st-result')")
        ok(True, "[결과] 단계 링크 → 결과 구역 제목(h2)에 초점")
        page.locator(".steps .step").nth(0).click()
        page.wait_for_function("() => document.activeElement?.tagName === 'H2' && !!document.activeElement.closest('#st-contact')")
        ok(True, "[연락] 단계 링크 → 연락 구역 제목에 초점")
        page.locator(".dock-next").click()
        page.wait_for_function("() => document.activeElement?.tagName === 'H2' && !!document.activeElement.closest('#st-result')")
        ok(True, "[수거 결과 선택 ↓] → 결과 구역 제목에 초점")

        # 검증 문구 role
        route(page, "#/fix/workflow-2")
        page.get_by_role("button", name="정정 요청 남기기", exact=True).click()
        expect(page.get_by_role("alert").filter(has_text="바뀐 내용이 없습니다.")).to_have_count(1)
        ok(True, "정정 요청 검증 문구는 role=alert")
        route(page, "#/backup")
        page.get_by_role("button", name="암호화 백업 파일 만들기", exact=True).click()
        expect(page.get_by_role("alert").filter(has_text="12자 이상")).to_have_count(1)
        ok(True, "백업 비밀번호 검증 문구는 role=alert")
        route(page, "#/export")
        ok(page.locator("main .msg:not(.err)").evaluate_all("els => els.every(e => e.getAttribute('role') === 'status')"),
           "결과 보내기 화면의 안내 .msg 는 role=status")

        # PIN 오류
        page.evaluate("async () => { await (await import('./core.js')).lockNow(); }")
        pin = page.get_by_label("PIN", exact=True)
        ok(pin.count() == 1 and pin.get_attribute("aria-invalid") is None, "PIN 칸 aria-label='PIN'")
        pin.fill("000000")
        page.get_by_role("button", name="잠금 해제", exact=True).click()
        alert = page.get_by_role("alert").filter(has_text="PIN이 맞지 않습니다")
        expect(alert).to_have_count(1)
        pin = page.get_by_label("PIN", exact=True)
        described = pin.get_attribute("aria-describedby")
        ok(pin.get_attribute("aria-invalid") == "true" and described and page.locator("#" + described).inner_text().startswith("PIN이 맞지 않습니다"),
           "PIN 오류: role=alert 문구 + aria-invalid=true + aria-describedby")
        page.screenshot(path=str(SHOTS / "phase2-pin-error.png"))
        unlock(page)
    finally:
        ctx.close()


CASES = (home_one_action, outbox_terms, callouts, field_wording, screen_reader)


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True)
        try:
            for case in CASES:
                if not sys.argv[1:] or case.__name__ in sys.argv[1:]:
                    case(browser, url)
        finally:
            browser.close()
    print(f"\n통과 {passed}개 · 화면: {SHOTS}")


if __name__ == "__main__":
    main()
