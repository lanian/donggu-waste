"""휴대폰 화면 개선 4단계 "디자인 정리" 회귀 (docs/reviews/2026-11-15-mobile-ui-expert-review.md P2 14·16·17, P3 18~23).

실행: python tests/test_mobile_ui_phase4.py [시험 이름 ...] (CHROME=Chromium 실행 파일, 이름 없으면 전부)
실제 PIN·암호화 IndexedDB를 쓰고, 자료는 모두 가상 자료다. 디자인만 바꾸는 단계라 이벤트·전송 규격은 그대로다.

- common_checks: 주요 화면(홈·상세·접수·결과 보내기·설정·단독 홈)을 기본 360×780·다크·아주 큰 글씨·360×640·작게(14px)에서
  가로 넘침 없음, 보이는 글자 12px 이상, 보이는 단추·링크·summary 44px 이상, 화면당 보이는 주 단추 하나(하단 바가 있으면 그 단추만),
  페이지 오류 0을 본다.
- shots: 같은 화면을 캡처만 한다(DG_SHOTS_DIR 폴더, 없으면 시험 폴더의 shots/phase4). 바꾸기 전·후 비교용.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from test_review_ui import mobile_server
from test_workflow_mobile import daylist, event, open_mobile, seed, stop

passed = 0
ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
SHOTS = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-mobile-ui-phase4-")) / "shots"
CSS = (WEB / "style.css").read_text(encoding="utf-8")
CALC_CSS = (WEB / "fee-calculator.css").read_text(encoding="utf-8")


def ok(condition, message):
    global passed
    if not condition:
        raise AssertionError("실패: " + message)
    passed += 1
    print("  ✓", message, flush=True)


def today_of(page):
    return page.evaluate("async () => (await import('./core.js')).todayKST()")


def settled(page, hash_):
    """그 주소의 화면을 다 그릴 때까지 기다린다(주소 바뀜 render와 직접 부른 render가 겹치면 먼저 부른 쪽은 그리지 않고 끝난다)."""
    page.evaluate("""async h => { const C = await import('./core.js');
        for (let i = 0; i < 200 && (C.ui.lastRoute !== h || location.hash !== h); i++) await new Promise(r => setTimeout(r, 25)); }""", hash_)


def route(page, hash_):
    page.evaluate("async h => { location.hash = h; await (await import('./core.js')).render(); }", hash_)
    settled(page, hash_)


def probe(page, var, prop="color"):
    """토큰 값을 계산된 값으로 (색은 rgb(...), 길이는 px)."""
    return page.evaluate("""([v, p]) => { const e = document.createElement('i'); e.style.display = 'block'; e.style[p] = `var(${v})`; document.body.append(e);
        const c = getComputedStyle(e)[p]; e.remove(); return c; }""", [var, prop])


def style_of(page, selector, *props, pseudo=None):
    return page.evaluate("""([s, props, pseudo]) => { const e = document.querySelector(s); if (!e) return null;
        const c = getComputedStyle(e, pseudo); return Object.fromEntries(props.map(p => [p, c[p]])); }""", [selector, list(props), pseudo])


def set_theme(page, theme, hc=False):
    page.evaluate("async p => { const C = await import('./core.js'); await C.setPrefs(p); await C.render(); }", {"theme": theme, "hc": hc})


def text_size(page, size):
    page.evaluate("async t => { const C = await import('./core.js'); C.st.textSize = t; C.applyTextSize(); await C.commit(); await C.render(); }", size)


# ------------------------------------------------------------ 가상 시나리오: 6곳(완료·일부·부재·미수거·방문 전 둘), 두 동, 2인·장비 품목
def line(lid, name, spec="시험용", qty=1, price=1000):
    return {"line_id": lid, "name": name, "spec": spec, "unit": "EA", "unit_label": "개", "qty": qty,
            "price_status": "CONFIRMED", "unit_price": price, "amount": price * qty}


def scenario_stops(today):
    s = [stop(n, today, dong="가상동" if n <= 3 else "시험동", address=f"가상시 {'가상동' if n <= 3 else '시험동'} 시험길 {n}", eta=f"{8 + n:02d}:00")
         for n in range(1, 7)]
    s[0]["items"] = [line("line-1", "장롱", "120cm 이상", price=8000)]
    s[1]["items"] = [line("line-2a", "가상 의자"), line("line-2b", "가상 탁자")]
    s[2]["unload_after"] = {"label": "가상 반입지", "eta": "11:30", "volume_m3": 3, "minutes": 20, "lat": 35.1, "lon": 126.9}
    s[4]["items"] = [line("line-5", "피아노", "", price=20000), line("line-5b", "가상 의자")]
    s[5]["items"] = [line("line-6", "장롱", "90cm 이상", price=6000)]
    return s


def scenario_events():
    return [
        event(1, "CONTACT", result="REACHED"),
        event(1, "ITEM_RESULT", line_id="line-1", qty_confirmed=1, qty_collected=1),
        event(1, "ATTEMPT", outcome="DONE"),
        event(2, "ITEM_RESULT", line_id="line-2a", qty_confirmed=1, qty_collected=1),
        event(2, "ITEM_RESULT", line_id="line-2b", qty_confirmed=1, qty_collected=0, reason="CANNOT_LOAD"),
        event(2, "ATTEMPT", outcome="PARTIAL"),
        event(3, "CONTACT", result="NO_ANSWER"),
        event(4, "ITEM_RESULT", line_id="line-4", qty_confirmed=1, qty_collected=0, reason="POSTPONED"),
        event(4, "ATTEMPT", outcome="NOT_COLLECTED"),
    ]


def seed_pc(page, **patch):
    today = today_of(page)
    seed(page, {"daylist": daylist(today, scenario_stops(today)), "events": scenario_events(), **patch})
    settled(page, "#/home")
    return today


def seed_solo(page, **patch):
    today = today_of(page)
    reqs = scenario_stops(today)
    for r in reqs:
        r["status"] = "RECEIVED"
    seed(page, {"mode": "SOLO", "requests": reqs, "events": scenario_events(), "lastBackupAt": None, **patch}, registered=False)
    settled(page, "#/home")
    return today


# 화면: (이름, 준비 함수). 준비 함수는 page를 받아 그 화면을 그린다.
def screen_home(page):
    seed_pc(page)


def screen_stop(page):
    seed_pc(page)
    route(page, "#/stop/workflow-5")


def screen_intake(page):
    seed_pc(page)
    page.evaluate("async () => { const C = await import('./core.js'); C.ui.form = null; }")
    route(page, "#/intake")


def screen_export(page):
    seed_pc(page)
    route(page, "#/export")


def screen_settings(page):
    seed_pc(page)
    route(page, "#/settings")


def screen_solo(page):
    seed_solo(page)


SCREENS = [("home", screen_home), ("stop", screen_stop), ("intake", screen_intake), ("export", screen_export),
           ("settings", screen_settings), ("solo-home", screen_solo)]
# (이름, 화면 크기, 테마, 글자 크기)
VARIANTS = [("base", (360, 780), "light", ""), ("dark", (360, 780), "dark", ""), ("xl", (360, 780), "light", "xl"),
            ("short", (360, 640), "light", ""), ("small", (360, 780), "light", "s")]


def apply_variant(page, size, theme, fs):
    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.evaluate("""async ([theme, fs]) => { const C = await import('./core.js'); await C.setPrefs({ theme, hc: false });
        C.st.textSize = fs; C.applyTextSize(); await C.commit(); await C.render(); scrollTo(0, 0); }""", [theme, fs])
    page.wait_for_timeout(50)


AUDIT_JS = """() => {
  const W = document.documentElement.clientWidth, H = innerHeight;
  const shown = (el) => { const r = el.getBoundingClientRect(); if (r.width < 1 || r.height < 1) return false;
    for (let x = el; x && x.nodeType === 1; x = x.parentElement) { const s = getComputedStyle(x);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false; if (x.classList.contains('sr-only')) return false; }
    return true; };
  const inView = (r) => r.bottom > 1 && r.top < H - 1 && r.right > 1 && r.left < W - 1;
  const label = (el) => (el.getAttribute('aria-label') || el.textContent || el.className || el.tagName).trim().replace(/\\s+/g, ' ').slice(0, 40);
  const small = [], tiny = [], prim = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement; if (!el || el.closest('script,style,noscript,.maplibregl-ctrl-attrib')) continue;
    if (!shown(el)) continue;
    const fs = parseFloat(getComputedStyle(el).fontSize);
    if (fs < 11.95) small.push(`${label(el)} ${fs}px`);
  }
  for (const el of document.querySelectorAll('button, a[href], summary, [role="button"]')) {
    if (!shown(el) || el.closest('.maplibregl-ctrl')) continue;
    const r = el.getBoundingClientRect();
    if (r.height < 43.5) tiny.push(`${label(el)} ${Math.round(r.height)}px`);
  }
  const bars = [...document.querySelectorAll('.dock, .footbar, .cta')].filter(shown);
  for (const b of document.querySelectorAll('.btn.primary')) { if (shown(b) && inView(b.getBoundingClientRect())) prim.push({ text: label(b), inBar: !!b.closest('.dock, .footbar, .cta') }); }
  const barHasPrimary = bars.some((b) => [...b.querySelectorAll('.btn.primary')].some(shown));
  return { overflow: document.documentElement.scrollWidth - W, small, tiny, prim, barHasPrimary };
}"""


def audit(page, where):
    out = {}
    for pos in ("top", "bottom"):
        page.evaluate("p => scrollTo(0, p === 'top' ? 0 : document.documentElement.scrollHeight)", pos)
        page.wait_for_timeout(30)
        out[pos] = page.evaluate(AUDIT_JS)
    top = out["top"]
    ok(top["overflow"] <= 0, f"[{where}] 가로 넘침 없음 (scrollWidth - 너비 = {top['overflow']})")
    small = sorted(set(out["top"]["small"] + out["bottom"]["small"]))
    ok(not small, f"[{where}] 보이는 글자 계산 크기 12px 이상 {small[:6]}")
    tiny = sorted(set(out["top"]["tiny"] + out["bottom"]["tiny"]))
    ok(not tiny, f"[{where}] 보이는 단추·링크·summary 높이 44px 이상 {tiny[:6]}")
    for pos, r in out.items():
        prim = r["prim"]
        ok(len(prim) <= 1 and (not r["barHasPrimary"] or all(p["inBar"] for p in prim)),
           f"[{where}·{pos}] 보이는 주 단추 하나(하단 바가 있으면 그 단추만) {prim}")


# ------------------------------------------------------------ J. 주요 화면 공통 검사
def common_checks(browser, url):
    print("\n— J. 주요 화면 공통: 넘침·글자 12px·누르는 곳 44px·주 단추 하나·오류 0 —")
    for vname, size, theme, fs in VARIANTS:
        ctx, page = open_mobile(browser, url)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            for sname, prepare in SCREENS:
                prepare(page)
                apply_variant(page, size, theme, fs)
                audit(page, f"{sname}·{vname}")
            ok(not errors, f"[{vname}] 페이지 오류 0 {errors[:3]}")
        finally:
            ctx.close()


# ------------------------------------------------------------ K. 캡처(바꾸기 전·후 비교용)
def shots(browser, url):
    out = Path(os.environ.get("DG_SHOTS_DIR") or (SHOTS / "phase4"))
    out.mkdir(parents=True, exist_ok=True)
    print(f"\n— K. 캡처 → {out} —")
    for vname, size, theme, fs in VARIANTS[:4]:
        ctx, page = open_mobile(browser, url)
        try:
            for sname, prepare in SCREENS:
                prepare(page)
                apply_variant(page, size, theme, fs)
                page.screenshot(path=str(out / f"{sname}-{vname}.png"), full_page=True)
                page.screenshot(path=str(out / f"{sname}-{vname}-first.png"))
        finally:
            ctx.close()
    ok(True, f"캡처 {len(list(out.glob('*.png')))}장")


# ------------------------------------------------------------ A. 글자·굵기 토큰 (P3 18)
def decls(css, prop):
    return [m.group(1).strip() for m in re.finditer(r"(?<![-\w])" + prop + r"\s*:\s*([^;}]+)", css)]


def type_tokens(browser, url):
    print("\n— A. 글자 크기 토큰 6단계·굵기 400/700/800, 소제목 .section-label 하나, 12px 미만 없음 —")
    for name, css in (("style.css", CSS), ("fee-calculator.css", CALC_CSS)):
        sizes = [v for v in decls(css, "font-size") if "var(--t-" not in v and v not in ("inherit", "var(--fs)")]
        ok(not sizes, f"{name}: font-size는 모두 --t-* 토큰 ({sizes[:8]})")
        shorthand = [v for v in decls(css, "font") if v not in ("inherit",) and "var(--t-" not in v]
        ok(not shorthand, f"{name}: font 줄임 속성도 --t-* 토큰 ({shorthand[:4]})")
        weights = sorted({v for v in decls(css, "font-weight")} | {m.group(1) for m in re.finditer(r"(?<![-\w])font\s*:\s*(\d{3})\b", css)})
        ok(set(weights) <= {"400", "700", "800", "inherit"}, f"{name}: 굵기는 400·700·800만 ({weights})")
    toks = re.findall(r"--t-(xs|sm|md|lg|xl|2xl)\s*:", CSS)
    ok(sorted(set(toks)) == sorted(["xs", "sm", "md", "lg", "xl", "2xl"]) and len(toks) == 6, f"크기 토큰 6단계를 한 곳에서 정의 ({toks})")
    ok(not re.search(r"\.(fld-label|chk-label|pick-h)\b", CSS), "옛 소제목 클래스(.fld-label·.chk-label·.pick-h) 규칙 없음 — .section-label 하나")
    ctx, page = open_mobile(browser, url)
    try:
        seed_pc(page)
        want = {"": [12, 14, 15, 17, 21, 30], "s": [12, 13.07, 14, 15.87, 19.6, 28], "xl": [15.2, 17.73, 19, 21.53, 26.6, 38]}
        for fs, sizes in want.items():
            text_size(page, fs)
            got = [float(probe(page, f"--t-{t}", "fontSize")[:-2]) for t in ("xs", "sm", "md", "lg", "xl", "2xl")]
            ok(all(abs(a - b) < 0.6 for a, b in zip(got, sizes)), f"글자 크기 '{fs or '보통'}': 토큰이 --fs 따라 바뀜, 가장 작은 것도 12px 이상 ({got})")
        text_size(page, "s")
        chip = style_of(page, ".stop .badge", "fontSize", "fontWeight")
        ok(chip and float(chip["fontSize"][:-2]) >= 12 and chip["fontWeight"] == "700", f"작게(14px)에서도 상태 뱃지 12px 이상·700 ({chip})")
        tab = style_of(page, ".tabbar a", "fontSize")
        ok(float(tab["fontSize"][:-2]) >= 12, f"작게(14px)에서도 탭 막대 글자 12px 이상 ({tab})")
        ok(float(style_of(page, ".now-tag", "fontSize")["fontSize"][:-2]) >= 12, "작게(14px)에서도 '지금' 표시 12px 이상")
        text_size(page, "")
        route(page, "#/settings")
        ok(page.locator(".section-label").count() >= 4 and page.locator(".fld-label").count() == 0, "설정 소제목은 .section-label")
        sl = style_of(page, ".section-label", "fontSize", "fontWeight")
        ok(sl["fontWeight"] == "700" and abs(float(sl["fontSize"][:-2]) - 14) < 0.6, f".section-label = --t-sm·700 ({sl})")
        route(page, "#/export")
        ok(float(style_of(page, ".flow li", "fontSize")["fontSize"][:-2]) >= 12, "결과 보내기 단계 표시(.flow) 글자 12px 이상")
    finally:
        ctx.close()


# ------------------------------------------------------------ B. 모서리·단추 모양 (P3 19)
def shape_tokens(browser, url):
    print("\n— B. 모서리 토큰, 모든 단추 12px(알약은 칩·탭만), 초록 그림자 없음, 단추 하나인 하단 바는 한 칸 전체 —")
    ctx, page = open_mobile(browser, url)
    try:
        seed_pc(page)
        for t, v in (("--r-control", "12px"), ("--r-card", "16px"), ("--r-sheet", "22px"), ("--r-pill", "999px")):
            ok(probe(page, t, "borderTopLeftRadius") == v, f"{t} = {v}")
        for hash_ in ("#/home", "#/stop/workflow-5", "#/export", "#/settings", "#/unload", "#/intake"):
            route(page, hash_)
            bad = page.evaluate("""() => [...document.querySelectorAll('.btn')].filter(b => b.getBoundingClientRect().width && !b.matches('.tab, .chipbtn'))
                .map(b => [b.textContent.trim().slice(0, 16), getComputedStyle(b).borderTopLeftRadius]).filter(([, r]) => r !== '12px')""")
            ok(not bad, f"{hash_}: 단추 모서리는 모두 12px ({bad[:5]})")
        for theme in ("light", "dark"):
            set_theme(page, theme)
            route(page, "#/home")
            sh = page.evaluate("() => [...document.querySelectorAll('.btn.primary')].map(b => getComputedStyle(b).boxShadow)")
            ok(sh and all(s == "none" for s in sh), f"[{theme}] 주 단추에 초록 그림자 없음 ({sh})")
        set_theme(page, "light")
        route(page, "#/unload")
        dock = page.locator(".dock")
        expect(dock.locator(".btn")).to_have_count(1)
        w = page.evaluate("""() => { const d = document.querySelector('.dock'), b = d.querySelector('.btn'), s = getComputedStyle(d);
            return [b.getBoundingClientRect().width, d.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight)]; }""")
        ok(abs(w[0] - w[1]) < 1, f"하차 기록: 단추 하나인 하단 바는 한 칸 전체 ({w[0]:.0f}/{w[1]:.0f}px)")
        ok(".dock:has(> .btn:only-child)" in CSS, "규칙: .dock:has(> .btn:only-child)")
    finally:
        ctx.close()


# ------------------------------------------------------------ C. 칩·뱃지 정리 (P2 14)
def badges_chips(browser, url):
    print("\n— C. 상태는 .badge(연한 바탕), 속성은 .chip(테두리), 진한 노랑은 번호 원·띠에만, 2인·장비 ⚠, 선택 표시 하나 —")
    ctx, page = open_mobile(browser, url)
    try:
        seed_pc(page)
        cards = page.locator(".stops .stop")
        ok(cards.count() == 6 and all(cards.nth(i).locator(".badge").count() >= 1 for i in range(6)), "목록 카드마다 상태 .badge")
        warn = probe(page, "--warn-bg", "backgroundColor")
        b3 = page.locator('.stop[href="#/stop/workflow-3"] .badge', has_text="부재").first
        ok(b3.count() == 1, "부재(연락 결과)는 .badge")
        st3 = b3.evaluate("e => { const c = getComputedStyle(e); return [c.backgroundColor, c.color, c.paddingTop + ' ' + c.paddingRight, c.fontWeight]; }")
        ok(st3[0] == warn and st3[1] == probe(page, "--warn-ink") and st3[2] == "3px 10px" and st3[3] == "700",
           f"경고 뱃지: --warn-bg 바탕·--warn-ink 글자·3px 10px·700 ({st3})")
        for kind in ("ok", "bad", "field", "neutral"):
            ok(probe(page, f"--{kind}-bg", "backgroundColor") != "rgba(0, 0, 0, 0)" and probe(page, f"--{kind}-ink") != "rgba(0, 0, 0, 0)", f"--{kind}-bg / --{kind}-ink 토큰")
        chip = page.locator(".stop .chip").first.evaluate("e => { const c = getComputedStyle(e); return [c.backgroundColor, c.boxShadow, c.borderTopWidth, c.borderTopStyle]; }")
        ok(chip[0] in ("rgba(0, 0, 0, 0)", probe(page, "--card", "backgroundColor")) and (chip[1] != "none" or chip[3] != "none"),
           f"속성 .chip은 테두리만(바탕 없음) ({chip})")
        sticker = probe(page, "--sticker", "backgroundColor")
        for hash_ in ("#/home", "#/stop/workflow-2", "#/stop/workflow-5", "#/export"):
            route(page, hash_)
            yellow = page.evaluate("""s => [...document.querySelectorAll('body *')].filter(e => getComputedStyle(e).backgroundColor === s && e.textContent.trim())
                .filter(e => !e.matches('.seq, .seq *, .n, .n *')).map(e => e.className + ':' + e.textContent.trim().slice(0, 12))""", sticker)
            ok(not yellow, f"{hash_}: 진한 노랑 바탕 글자는 번호 원 말고 없음 ({yellow[:4]})")
        route(page, "#/home")
        two = page.locator(".chip.ld-TWO").first
        equip = page.locator(".chip.ld-EQUIP").first
        ok(two.inner_text().startswith("⚠") and "두 사람 이상이 들기" in (two.get_attribute("aria-label") or ""),
           f"2인 칩: 앞에 ⚠, aria-label에 설명 ({two.inner_text()} / {two.get_attribute('aria-label')})")
        ok(equip.inner_text().startswith("⚠") and "운반 장비" in (equip.get_attribute("aria-label") or ""), "장비 칩: 앞에 ⚠, aria-label에 설명")
        base = page.locator(".chip.ld-BASE").first
        ok(not base.inner_text().startswith("⚠"), "그 밖의 싣기 칩에는 ⚠ 없음")
        ok(page.locator(".dong-head").count() == 2 and page.locator(".stop .chip.dong").evaluate_all("els => els.filter(e => e.getBoundingClientRect().width).length") == 0,
           "동 머리글 아래 카드에서는 동 칩 숨김")
        route(page, "#/find")
        ok(page.locator(".stop .chip.dong").evaluate_all("els => els.filter(e => e.getBoundingClientRect().width).length") >= 6, "동 머리글 없는 찾기 목록에는 동 칩 보임")
        # 선택 표시 하나: --sel-bg/--sel-ink/--sel-ring + ✓(스크린리더 이름에는 없음)
        route(page, "#/home")
        sel = [probe(page, "--sel-bg", "backgroundColor"), probe(page, "--sel-ink"), probe(page, "--sel-ring", "borderTopColor")]
        tab = page.get_by_role("button", name="전체 6", exact=True)
        ok(tab.count() == 1, "선택된 거르기 탭 이름에 ✓ 없음 ('전체 6')")

        def sel_style(loc):
            return loc.evaluate("""e => { const c = getComputedStyle(e), b = getComputedStyle(e, '::before');
                return [c.backgroundColor, c.color, c.borderTopColor, b.content]; }""")
        for what, loc in (("거르기 탭", tab),):
            s = sel_style(loc)
            ok(s[0] == sel[0] and s[1] == sel[1] and "✓" in s[3], f"{what}.on: --sel-bg·--sel-ink·✓ ({s})")
        route(page, "#/settings")
        theme_on = page.locator(".seg-opt.on").first
        s = sel_style(theme_on)
        ok(s[0] == sel[0] and s[1] == sel[1] and "✓" in s[3], f"설정 .seg-opt.on 같은 선택 표시 ({s})")
        lock_on = page.get_by_role("button", name="10분", exact=True)
        s = sel_style(lock_on)
        ok(s[0] == sel[0] and s[1] == sel[1] and s[2] == sel[2] and "✓" in s[3], f"설정 .opt.on 같은 선택 표시 ({s})")
        stray = page.evaluate("() => [...document.querySelectorAll('.btn.opt[aria-pressed]')].filter(b => !b.closest('.seg')).map(b => b.textContent.trim())")
        ok(not stray, f"설정: 고르는 단추(opt)는 모두 .seg 틀 안 ({stray})")
        route(page, "#/stop/workflow-2")
        done = page.locator("#st-result .btn.opt.on")
        ok(done.count() == 1 and done.text_content().startswith("✓ ") and "✓" not in sel_style(done)[3],
           "저장된 결과 단추는 기존 ✓ 글자 하나(겹쳐 그리지 않음)")
        # 품목 고르기: '담은 것 있음'은 테두리 대신 개수 뱃지
        route(page, "#/intake")
        page.get_by_role("button", name="+ 품목 담기", exact=True).click()
        page.get_by_role("group", name="품목 분류").get_by_role("button", name="가구", exact=True).click()
        page.locator(".name-chip").first.click()
        if page.get_by_role("dialog").count():
            page.get_by_role("dialog").locator(".pick-add").first.click()
            page.get_by_role("dialog").get_by_role("button", name="완료", exact=True).click()
        picked = page.get_by_role("group", name="품목 분류").locator(".tab").last
        ok(picked.locator(".badge").count() == 1 and picked.locator(".badge").inner_text().strip() == "1" and "has" not in (picked.get_attribute("class") or ""),
           f"담음 탭: 테두리 대신 개수 뱃지 ({picked.inner_text()})")
        nc = page.locator(".name-chip.on").first
        s = sel_style(nc)
        ok(s[0] == sel[0] and s[2] == sel[2], f"담은 이름 단추(.name-chip.on)도 같은 선택 표시 ({s})")
    finally:
        ctx.close()


# ------------------------------------------------------------ D. 설정 재구성 (P2 16)
GROUPS = ["도움말·점검", "화면", "나와 이 휴대폰", "기록 지키기", "관리자 설정", "앱 정보"]


def settings_groups(browser, url):
    print("\n— D. 설정 묶음: 도움말·점검 / 화면 / 나와 이 휴대폰 / 기록 지키기 / 관리자 설정(접힘) / 앱 정보, 위험 동작은 맨 아래 —")
    ctx, page = open_mobile(browser, url)
    try:
        for mode in ("PC", "SOLO"):
            seed_pc(page) if mode == "PC" else seed_solo(page)
            page.evaluate("async () => { (await import('./core.js')).ui.adminOpen = false; }")  # 앞 경우에서 펼친 것(화면 상태)을 지움
            route(page, "#/settings")
            expect(page.get_by_role("heading", name="설정", exact=True, level=1)).to_be_visible()
            ok(page.locator(".tabbar .t-set").inner_text().strip() == "설정", f"[{mode}] 탭 이름 '설정'")
            names = page.locator(".set-group > .group-title, .set-group > summary .group-title").all_inner_texts()
            ok([n.strip() for n in names] == GROUPS, f"[{mode}] 묶음 순서 {names}")
            admin = page.locator("details.set-group.admin")
            ok(admin.count() == 1 and admin.get_attribute("open") is None, f"[{mode}] 관리자 설정은 기본 접힘")
            hidden = ["주소 검색 승인키", "문자 문구", "사용 방식 바꾸기"] + (["기기 등록"] if mode == "PC" else ["확인 필요 품목 단가"])
            for t in hidden:
                inside = admin.locator(".card").filter(has_text=t)
                ok(inside.count() >= 1 and not inside.first.is_visible(), f"[{mode}] '{t}'은 관리자 설정 안(접힘)")
            ok(admin.locator(".card h3").filter(has_text=re.compile("^지도$")).count() == 1, f"[{mode}] 지도 서버 설정은 관리자 설정 안")
            danger = page.locator("main > .danger-zone")
            ok(danger.count() == 1 and danger.get_by_role("button", name="이 휴대폰의 모든 자료 지우기", exact=True).count() == 1
               and page.evaluate("() => document.querySelector('main > .danger-zone').nextElementSibling?.matches('.tabbar') ?? true"),
               f"[{mode}] 모든 자료 지우기는 맨 아래 따로")
            info = page.locator(".set-group.info dl").inner_text()
            ok(all(t in info for t in ("앱 버전", "기기 번호", "저장소")), f"[{mode}] 앱 정보: 버전·기기 번호·저장소")
            keep = page.locator(".set-group.keep")
            ok(keep.locator(".card.backup-card").count() == 1, f"[{mode}] 기록 지키기: 백업은 별도 카드")
            me = page.locator(".set-group.me")
            ok(all(me.get_by_role("heading", name=t, exact=True).count() == 1 for t in ("작업자", "잠금", "길안내 앱")), f"[{mode}] 나와 이 휴대폰: 작업자·잠금·길안내 앱")
            admin.locator("summary").click()
            ok(admin.get_by_label("주소 검색 승인키").is_visible(), f"[{mode}] 관리자 설정을 펴면 승인키 칸이 보임")
            page.get_by_role("button", name="5분", exact=True).click()
            expect(page.get_by_role("button", name="5분", exact=True)).to_have_attribute("aria-pressed", "true")
            ok(page.locator("details.set-group.admin").get_attribute("open") is not None, f"[{mode}] 다른 설정을 바꿔 다시 그려도 관리자 설정은 펼친 채")
            ok(page.evaluate("async () => (await import('./core.js')).st.lockMin") == 5, f"[{mode}] 기존 설정 저장 그대로(무조작 5분)")
            page.get_by_label("주소 검색 승인키").fill("TEST-KEY-1234")
            page.get_by_role("button", name="승인키 저장", exact=True).click()
            page.wait_for_function("async () => (await import('./core.js')).st.jusoKey === 'TEST-KEY-1234'")
            ok(True, f"[{mode}] 관리자 설정 안 승인키 저장 그대로")
            page.screenshot(path=str(SHOTS / f"phase4-settings-{mode.lower()}.png"), full_page=True)
    finally:
        ctx.close()


# ------------------------------------------------------------ E. 뒤로 단추·탭바 (P2 17)
def back_and_tabs(browser, url):
    print("\n— E. 뒤로 단추 이름은 목적지에서, 탭바 [목록 · 보내기/백업 · 설정] + 보낼 기록 배지 —")
    ctx, page = open_mobile(browser, url)
    try:
        seed_pc(page)
        tabs = page.locator(".tabbar a")
        ok([t.strip() for t in tabs.all_inner_texts()][:1] == ["목록"] and tabs.count() == 3, f"PC: 탭 세 칸 ({tabs.all_inner_texts()})")
        send = page.locator(".tabbar .t-send")
        to_send = page.evaluate("async () => (await import('./core.js')).outbox().toSend")
        ok(send.get_attribute("href") == "#/export" and send.locator(".tab-badge").inner_text().strip() == str(to_send) and to_send > 0,
           f"보내기 칸에 보낼 기록 수 배지 ({to_send})")
        ok(re.search(r"보내기.*보낼 기록 %d건" % to_send, send.get_attribute("aria-label") or "") is not None, f"보내기 칸 이름에 건수 ({send.get_attribute('aria-label')})")
        hs = tabs.evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
        ok(min(hs) >= 56, f"탭 칸 누르는 곳 56px 이상 ({hs})")
        ok(page.locator(".tabbar .t-map").count() == 0, "지도는 탭에서 빠짐")
        seed_pc(page, events=[], mapEnabled=True)
        ok(page.locator(".tabbar .t-send .tab-badge").count() == 0, "보낼 기록 0이면 배지 없음")
        page.locator(".home-tools").get_by_role("button", name="지도", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/map$"))
        page.get_by_role("button", name="← 목록", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/home$"))
        ok(True, "지도는 자주 쓰는 기능 [지도] → 지도 화면 [← 목록]")
        # 탭으로 연 결과 보내기
        page.locator(".tabbar .t-send").click()
        expect(page).to_have_url(re.compile(r"#/export$"))
        page.get_by_role("button", name="← 목록", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/home$"))
        # 설정에서 연 화면은 ← 설정
        for open_name, hash_ in (("현장 처리 순서", "#/help"), ("현장 점검", None), ("안전용 암호화 백업", "#/backup")):
            route(page, "#/settings")
            page.get_by_role("button", name=re.compile("^" + open_name)).first.click()
            if hash_:
                expect(page).to_have_url(re.compile(re.escape(hash_) + "$"))
            back = page.locator("main > .bar").get_by_role("button", name="← 설정", exact=True)
            expect(back).to_have_count(1)
            ok(True, f"설정 → {open_name}: [← 설정]")
            back.click()
            expect(page).to_have_url(re.compile(r"#/settings$"))
        route(page, "#/settings")
        page.locator("details.set-group.admin summary").click()
        page.get_by_role("button", name="기기 등록", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/register$"))
        page.get_by_role("button", name="← 설정", exact=True).click()
        expect(page).to_have_url(re.compile(r"#/settings$"))
        ok(True, "설정 → 기기 등록: [← 설정]으로 설정에 돌아감")
        seed_pc(page)
        seed(page, registered=False)
        page.locator(".next-action").get_by_role("button", name="배차 QR로 업무 시작", exact=True).click()
        expect(page.locator("main > .bar").get_by_role("button", name="← 목록", exact=True)).to_have_count(1)
        ok(True, "목록 → 배차와 현장 기록: [← 목록]")
        # 찾기 → 상세 → ← 찾기
        seed_pc(page)
        route(page, "#/find")
        page.locator('.stops .stop[href="#/stop/workflow-2"]').click()
        back = page.locator("main > .bar").get_by_role("button", name="← 찾기", exact=True)
        expect(back).to_have_count(1)
        ok(back.count() == 1 and page.locator(".dock-back").get_attribute("aria-label") == "찾기로 돌아가기", "찾기에서 연 상세: [← 찾기], 하단 [←]도 '찾기로 돌아가기'")
        back.click()
        expect(page).to_have_url(re.compile(r"#/find$"))
        route(page, "#/home")
        page.locator('.stops .stop[href="#/stop/workflow-2"]').click()
        expect(page.locator("main > .bar").get_by_role("button", name="← 목록", exact=True)).to_have_count(1)
        ok(True, "목록에서 연 상세: [← 목록]")
        # 단독: [목록 · 백업 · 설정], 백업 경고 때 점
        seed_solo(page)
        tabs = page.locator(".tabbar a")
        ok([t.strip() for t in tabs.all_inner_texts()] == ["목록", "백업", "설정"], f"단독: 탭 [목록 · 백업 · 설정] ({tabs.all_inner_texts()})")
        bk = page.locator(".tabbar .t-backup")
        ok(bk.get_attribute("href") == "#/backup" and bk.locator(".tab-dot").count() == 1 and "백업 필요" in (bk.get_attribute("aria-label") or ""),
           "단독: 백업 경고 때 백업 칸에 점 + 이름 '백업 필요'")
        seed_solo(page, lastBackupAt=page.evaluate("() => new Date().toISOString()"))
        ok(page.locator(".tabbar .t-backup .tab-dot").count() == 0, "백업한 지 하루 안이면 점 없음")
    finally:
        ctx.close()


# ------------------------------------------------------------ F. 링크·토큰 정리 (P3 20)
def css_rules(css):
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def links_tokens(browser, url):
    print("\n— F. 링크 색, 다크 토큰 한 벌, 쓰지 않는 토큰·클래스 없음, 지도 핀 고정색 없음 —")
    # 다크 토큰 묶음은 [data-scheme=dark] 한 벌. 예외는 스크립트 전 첫 화면용 바탕·글자 세 값뿐(boot_dark 참고)
    boot = re.search(r":root:not\(\[data-scheme\]\)\s*\{([^{}]*)\}", CSS)
    boot_vars = set(re.findall(r"(--[\w-]+)\s*:", boot.group(1))) if boot else set()
    ok(CSS.count("--brand: #3fcf8e") == 1 and boot_vars <= {"--bg", "--ink", "--muted"},
       f"다크 토큰 묶음은 한 벌, 스크립트 전 첫 화면 규칙은 바탕·글자만 ({sorted(boot_vars)})")
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in list(WEB.glob("*.js")) + list(WEB.glob("screens/*.js")) + [WEB / "index.html"])
    defined = sorted(set(re.findall(r"(--[\w-]+)\s*:", CSS)))
    uses = CSS + CALC_CSS + corpus
    unused = [t for t in defined if not re.search(r"var\(" + re.escape(t) + r"[,)]", uses)]
    ok(not unused, f"정의만 하고 쓰지 않는 토큰 없음 ({unused})")
    ok(not re.search(r"--band-(a|b|ink|sub)|--asphalt|--chip|--radius", CSS), "--band-*·--asphalt·--chip·--radius 토큰 삭제")
    classes = sorted({c for sel, _ in css_rules(CSS) for c in re.findall(r"\.([A-Za-z][\w-]*)", sel) if not sel.strip().startswith("@")})
    dyn = lambda c: "-" in c and re.search(r"['\"`\s]" + re.escape(c.rsplit("-", 1)[0] + "-"), corpus)
    dead = [c for c in classes if not re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", corpus) and not dyn(c)
            and not c.startswith("maplibregl")]
    ok(not dead, f"쓰는 곳 없는 클래스 규칙 없음 ({dead})")
    pin_rules = [(s.strip(), b) for s, b in css_rules(CSS) if ".pin" in s or ".crosshair" in s]
    fixed = [s for s, b in pin_rules if re.search(r"#[0-9a-fA-F]{3,6}\b|rgba?\(", b)]
    ok(pin_rules and not fixed, f"지도 핀 규칙에 고정색 없음 ({fixed})")
    ctx, page = open_mobile(browser, url)
    try:
        seed_pc(page)
        for theme in ("light", "dark"):
            set_theme(page, theme)
            a = page.evaluate("""() => { const a = document.createElement('a'); a.href = '#/home'; a.textContent = '시험 링크'; document.querySelector('main').append(a);
                const c = getComputedStyle(a); const r = [c.color, c.textUnderlineOffset]; a.remove(); return r; }""")
            ok(a[0] == probe(page, "--brand-soft-ink") and a[1] == "3px", f"[{theme}] 링크 색 --brand-soft-ink, 밑줄 간격 3px ({a})")
        set_theme(page, "light")
    finally:
        ctx.close()


# ------------------------------------------------------------ G. 지도 핀 접근성 (P3 21)
def map_pins(browser, url):
    print("\n— G. 지도 핀: 이름에 번호·진행 상태, 내 위치, 완료 ✓·테두리, 미수거·보류 점선, 선택은 진한 윤곽 —")
    ctx, page = open_mobile(browser, url)
    try:
        seed_pc(page)
        got = page.evaluate("""async () => {
            const M = await import('./map.js'), V = await import('./screens/mapview.js'), C = await import('./core.js'), F = await import('./fees.js');
            const ml = { Marker: class { constructor({ element }) { this.el = element; } setLngLat() { return this; } addTo() { document.querySelector('main').append(this.el); return this; } } };
            const out = [];
            for (const id of ['workflow-1', 'workflow-2', 'workflow-3', 'workflow-4', 'workflow-5']) {
                const s = C.findStop(id), d = F.deriveStop(s, C.st.events);
                const o = V.pinFor(s, d, id === 'workflow-5' ? id : null);
                const m = M.pin(ml, null, { lng: 1, lat: 1, ...o });
                const c = getComputedStyle(m.el), b = getComputedStyle(m.el, '::before');
                out.push({ id, name: m.el.getAttribute('aria-label'), cls: m.el.className, opacity: c.opacity, bg: c.backgroundColor, border: c.borderTopColor,
                    style: c.borderTopStyle, before: b.content, outline: c.outlineColor + ' ' + c.outlineStyle, shadow: c.boxShadow });
            }
            const me = M.pin(ml, null, { lng: 1, lat: 1, label: '나', cls: 'me' });
            out.push({ id: 'me', name: me.el.getAttribute('aria-label') });
            const pc = M.pin(ml, null, { lng: 1, lat: 1, label: '1-2', cls: 'c1' });
            out.push({ id: 'pc', name: pc.el.getAttribute('aria-label') });
            return out;
        }""")
        by = {x["id"]: x for x in got}
        ok(by["workflow-1"]["name"] == "1번 · 수거 완료" and by["workflow-4"]["name"] == "4번 · 미수거" and by["workflow-5"]["name"].startswith("5번 · "),
           f"핀 이름 '{{n}}번 · {{진행 상태}}' ({[x['name'] for x in got]})")
        ok(by["me"]["name"] == "내 위치", "내 위치 핀 이름 '내 위치'")
        ok(by["pc"]["name"] == "1-2번 위치", "PC 지도 핀(이름을 넘기지 않음)은 전과 같은 이름")
        card, okc = probe(page, "--card", "backgroundColor"), probe(page, "--ok", "borderTopColor")
        d1 = by["workflow-1"]
        ok(d1["opacity"] == "1" and d1["bg"] == card and d1["border"] == okc and "✓" in d1["before"],
           f"완료 핀: 흐리게 하지 않고 카드색 바탕 + --ok 테두리 + ✓ ({d1})")
        ok(by["workflow-4"]["style"] == "dashed", "미수거 핀은 점선 테두리")
        page.evaluate("""async () => { const M = await import('./map.js');
            const ml = { Marker: class { constructor({ element }) { this.el = element; } setLngLat() { return this; } addTo() { document.querySelector('main').append(this.el); return this; } } };
            M.pin(ml, null, { lng: 1, lat: 1, label: '9', name: '9번 · 보류', cls: 'p-ON_HOLD', }).el.id = 'hold-pin'; }""")
        ok(style_of(page, "#hold-pin", "borderTopStyle")["borderTopStyle"] == "dashed", "보류 핀도 점선 테두리")
        f5 = by["workflow-5"]
        ink = probe(page, "--ink")
        ok("focus" in f5["cls"] and f5["bg"] != "rgb(180, 35, 24)" and (ink in f5["outline"] or ink in f5["shadow"]) and probe(page, "--focus-halo") in f5["shadow"],
           f"선택 핀: 빨강 대신 진한 윤곽(--ink) + --focus-halo ({f5})")
    finally:
        ctx.close()


# ------------------------------------------------------------ H. PC/단독 이름·도움말 (P3 22)
def pc_solo_names(browser, url):
    print("\n— H. 같은 일은 같은 이름·자리: [+ 접수 등록], 거르기 '남음', 하차 기록은 도구 묶음, 도움말 PC/단독 —")
    ctx, page = open_mobile(browser, url)
    try:
        for mode in ("PC", "SOLO"):
            seed_pc(page) if mode == "PC" else seed_solo(page)
            tools = page.locator(".home-tools")
            ok(tools.get_by_role("button", name="+ 접수 등록", exact=True).count() == 1, f"[{mode}] 도구 묶음에 [+ 접수 등록]")
            ok(tools.get_by_role("button", name=re.compile(r"^하차 기록")).count() == 1, f"[{mode}] 하차 기록은 도구 묶음 안")
            ok(page.get_by_role("group", name="목록 거르기").get_by_role("button", name=re.compile(r"^남음 \d+$")).count() == 1, f"[{mode}] 거르기 '남음'")
            ok(page.get_by_role("button", name="+ 전화 접수 등록").count() == 0 and page.get_by_role("button", name="+ 접수 등록 (현장·전화)").count() == 0, f"[{mode}] 옛 이름 없음")
            if mode == "PC":
                ok(page.locator(".unload-row").get_by_role("button", name="가상 반입지 하차 기록", exact=True).count() == 1, "PC 목록의 하차 줄 단추는 그대로")
            route(page, "#/help")
            heads = [x.strip() for x in page.locator(".help .card h2").all_inner_texts()]
            ok("접수 받기" in heads, f"[{mode}] 도움말에 '접수 받기' ({heads})")
            text = page.locator("main").inner_text()
            if mode == "SOLO":
                morning = page.locator(".help .card", has=page.get_by_role("heading", name="아침", exact=True)).inner_text()
                end = page.locator(".help .card", has=page.get_by_role("heading", name="하루 끝", exact=True)).inner_text()
                ok("전화 접수" in morning and "오늘" in morning and "백업" in end and "QR" not in text and "D-" not in text,
                   f"단독 도움말: 아침(전화 접수 → 오늘 탭)·하루 끝(백업), PC 전용 설명 없음")
            else:
                ok("QR" in text and "D-" in text, "PC 도움말은 목록 받기·결과 반영 코드")
        mods = page.evaluate("async () => { const H = await import('./screens/help.js'); return [Array.isArray(H.HELP_PC), Array.isArray(H.HELP_SOLO)]; }")
        ok(mods == [True, True], "help.js: HELP_PC·HELP_SOLO")
    finally:
        ctx.close()


# ------------------------------------------------------------ I. 작은 누르는 곳 (P3 23)
def small_targets(browser, url):
    print("\n— I. summary·'이름 변경'·그 밖 작은 누르는 곳 —")
    ctx, page = open_mobile(browser, url)
    try:
        seed_pc(page)
        change = page.get_by_role("group", name="현재 작업자").get_by_role("button", name="이름 변경", exact=True)
        ok(change.count() == 1 and "ghost" in change.get_attribute("class") and change.evaluate("e => e.getBoundingClientRect().height") >= 44,
           "작업자 '이름 변경'은 ghost 단추, 44px 이상")
        change.click()
        expect(page).to_have_url(re.compile(r"#/settings$"))
        expect(page.get_by_label("작업자 이름")).to_be_visible()
        ok(True, "[이름 변경] → 설정의 작업자 칸")
        route(page, "#/export")
        hs = page.locator("summary").evaluate_all("els => els.filter(e => e.getBoundingClientRect().width).map(e => e.getBoundingClientRect().height)")
        ok(hs and min(hs) >= 56, f"결과 보내기: 모든 summary 높이 var(--tap) 이상 ({hs})")
        route(page, "#/register")
        hs = page.locator("summary").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
        ok(hs and min(hs) >= 56, f"기기 등록: '코드 복사' summary 높이 ({hs})")
        route(page, "#/stop/workflow-3")
        tel = page.locator(".who .phone a")
        ok(tel.evaluate("e => e.getBoundingClientRect().height") >= 44, "상세 전화번호 링크 44px 이상")
        ok("details > summary" in CSS.replace("details>summary", "details > summary"), "규칙: details > summary { min-height: var(--tap) }")
    finally:
        ctx.close()


def boot_dark(browser, url):
    print("\n— 스크립트가 돌기 전 첫 화면도 휴대폰 다크 설정을 따름(밤에 밝게 번쩍이지 않음) —")
    for scheme, want_dark in (("dark", True), ("light", False)):
        ctx = browser.new_context(service_workers="block", viewport={"width": 360, "height": 640}, color_scheme=scheme, java_script_enabled=False)
        try:
            page = ctx.new_page()
            page.goto(url)
            bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
            nums = [int(x) for x in bg[bg.index("(") + 1:bg.index(")")].split(",")[:3]]
            dark = sum(nums) / 3 < 80
            ok(dark == want_dark, f"스크립트 전 첫 화면({scheme}): 배경 {bg} → {'어두움' if dark else '밝음'}")
        finally:
            ctx.close()


CASES = (type_tokens, shape_tokens, badges_chips, settings_groups, back_and_tabs, links_tokens, map_pins, pc_solo_names, small_targets, common_checks, boot_dark)


def main():
    SHOTS.mkdir(parents=True, exist_ok=True)
    names = sys.argv[1:]
    with mobile_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), headless=True)
        try:
            for case in CASES + (shots,):
                if (not names and case is not shots) or case.__name__ in names:
                    case(browser, url)
        finally:
            browser.close()
    print(f"\n통과 {passed}개 · 화면: {SHOTS}")


if __name__ == "__main__":
    main()
