"""PC 표 정렬: 접수 목록 서버 정렬(새 키·2차 정렬·빈 값 뒤로·잘못된 키 무시·SQL 끼워 넣기 차단)과
나머지 표의 화면 안 정렬(열 머리 단추·형식별 비교·원래 순서 복귀·다시 그려도 유지)을 본다. 가상 자료만 쓴다.

실행: python tests/test_pc_table_sort.py  (화면 절은 Playwright Chromium, CHROME=브라우저 경로)
DG_SORT_SERVER_ONLY=1 이면 서버 단위 절만 본다.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
sys.path.insert(0, str(ROOT / "tests"))

PASSED = 0


def ok(value, message):
    global PASSED
    if not value:
        raise AssertionError("실패: " + message)
    PASSED += 1
    print("  ✓", message, flush=True)


# ------------------------------------------------------------------ 1. 서버: search_requests 정렬
def server_unit():
    print("[서버 정렬]", flush=True)
    work = tempfile.TemporaryDirectory(prefix="dg-table-sort-")
    old = {k: os.environ.get(k) for k in ("DG_STATE", "DG_OUT", "DG_DB_KEY")}
    os.environ.update(DG_STATE=work.name, DG_OUT=str(Path(work.name) / "out"), DG_DB_KEY="test-db-key-not-for-production")
    from dgpc import area as A, core as C, intake as I, service as S, store
    con = store.connect(create=True)
    actor = C.ACTOR.set("table_sort_fixture")
    try:
        for name in ("가상가동", "가상나동"):
            A.save_dong(con, {"name": name})
        crew_b = S.save_crew(con, {"name": "가상 나팀", "capacity_m3": 8})
        crew_a = S.save_crew(con, {"name": "가상 가팀", "capacity_m3": 8})
        fee = {r["name"] + "|" + r["spec"]: r["id"] for r in S.FEE_TABLE["rows"]}
        chair, wardrobe = fee["의자|고정식 의자"], fee["장롱|120cm 이상 1쪽당"]

        def make(tag, planned, dong, items, lat=None, received="2026-11-01T00:00:00Z"):
            data = {"name": f"가상정렬{tag}", "phone": f"010000077{len(made):02d}", "address": f"가상시 동구 정렬로 {len(made) + 1}",
                    "dong": dong, "items": items}
            if planned:
                data["planned_date"] = planned
            if lat is not None:
                data.update(lat=lat, lon=126.9)
            rid = S.create_request(con, data)["id"]
            con.execute("UPDATE request SET received_at=? WHERE id=?", (received, rid))
            made[rid] = tag
            return rid

        made: dict[str, str] = {}
        # 가: 11-20 가상나동, 의자 1개(확정), 위치 있음, 나팀 배정, 납부 확인 / 나: 11-20 가상가동, 표에 없는 품목(금액 미정), 위치 없음, 미배정
        # 다: 11-21 가상가동, 장롱 3쪽(가장 비쌈), 위치 있음, 가팀 배정, 미납 / 라: 예정일 없음, 동 없음, 의자 2개, 위치 없음, 미배정, 미납
        ra = make("가", "2026-11-20", "가상나동", [{"fee_rate_id": chair, "qty": 1}], lat=35.1, received="2026-11-03T01:00:00Z")
        make("나", "2026-11-20", "가상가동", [{"name": "가상 미등록 품목", "qty": 1}], received="2026-11-05T01:00:00Z")
        rc = make("다", "2026-11-21", "가상가동", [{"fee_rate_id": wardrobe, "qty": 3}], lat=35.2, received="2026-11-01T01:00:00Z")
        make("라", None, None, [{"fee_rate_id": chair, "qty": 2}], received="2026-11-04T01:00:00Z")
        S.confirm_plan(con, "2026-11-20", {crew_b: [{"request_id": ra, "eta": "09:30"}]})
        S.confirm_plan(con, "2026-11-21", {crew_a: [{"request_id": rc, "eta": "10:00"}]})
        S.add_payment(con, ra, {"amount": S.get_request(con, ra)["state"]["fee"]["final"], "method": S.PAY_METHODS[0]})

        def order(**f):
            return "".join(made[r["id"]] for r in I.search_requests(con, {"status": "", **f}, with_counts=False)["rows"])

        ok(order() == "나가다라", f"기본 정렬: 예정일(없음은 뒤) → 같은 날은 접수 최신 먼저 ({order()})")
        ok(order(sort="received_at", dir="desc") == "나라가다" and order(sort="received_at") == "다가라나", "접수일 오름·내림")
        ok(order(sort="fee", dir="desc") == "다라가나" and order(sort="fee") == "가라다나", "금액: 큰 순·작은 순, 금액 미정은 방향과 무관하게 맨 뒤")
        ok(order(sort="pay") == "나라다가" and order(sort="pay", dir="desc") == "가라다나",
           "수납 상태: 화면 수납 상태 순서(금액 확인 대기 → 미납 → 납부 확인), 같은 상태는 접수 최신 먼저")
        ok(order(sort="crew") == "다가나라" and order(sort="crew", dir="desc") == "가다나라", "배정 팀 이름 오름·내림(가팀 → 나팀), 미배정은 방향과 무관하게 뒤")
        ok(order(sort="loc") == "나라가다" and order(sort="loc", dir="desc") == "가다나라", "위치: 오름 = 위치 없음 먼저, 내림 = 위치 있음 먼저")
        ok(order(sort="planned_date", dir="desc") == "다나가라" and order(sort="dong") == "나다가라" and order(sort="dong", dir="desc") == "가나다라",
           "예정일·동이 비어 있으면 방향과 무관하게 뒤")
        # 2차 정렬
        ok(order(sort="planned_date", sort2="dong") == "나가다라", "예정일 → 동 (11-20 가상가동 → 가상나동)")
        ok(order(sort="planned_date", sort2="dong", dir2="desc") == "가나다라", "2차 정렬 내림")
        ok(order(sort="loc", sort2="received_at") == "라나다가", "위치 없음 먼저 → 접수 오래된 순")
        ok(order(sort="planned_date", sort2="planned_date", dir2="desc") == order(sort="planned_date"), "2차가 1차와 같은 열이면 무시")
        ok(order(sort2="dong") == order(), "1차 없이 2차만 주면 기본 정렬")
        # 잘못된 키·SQL 끼워 넣기
        for b in ["name; DROP TABLE request", "received_at DESC, (SELECT 1)", "1", "status--", "rowid", "__proto__", "phone"]:
            ok(order(sort=b) == order() and order(sort="dong", sort2=b) == order(sort="dong"), f"허용 목록에 없는 정렬 키 무시: {b!r}")
        ok(order(sort="dong", dir="DESC; DROP TABLE request") == order(sort="dong"), "방향 값도 desc 외에는 오름차순(문자열을 SQL에 넣지 않음)")
        ok(C.one(con, "SELECT COUNT(*) AS n FROM request")["n"] == 4, "끼워 넣기 시도 뒤에도 접수 표가 그대로")
        ok(all(isinstance(v, tuple) and len(v) == 2 for v in I.SORTABLE.values()) and "phone" not in I.SORTABLE, "정렬 허용 목록(식, 빈 값 뒤로)에 연락처는 없음")
        # 쪽 나눔: 같은 값끼리도 쪽 사이 순서가 흔들리지 않음 (마지막 기준 id)
        pages = [r["id"] for p in (1, 2, 3, 4) for r in I.search_requests(con, {"status": "", "sort": "dong", "size": 1, "page": p}, with_counts=False)["rows"]]
        ok(len(set(pages)) == 4, "한 건씩 쪽을 넘겨도 빠지거나 겹치는 접수 없음")
    finally:
        C.ACTOR.reset(actor)
        con.close()
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        work.cleanup()


def main():
    server_unit()
    if os.environ.get("DG_SORT_SERVER_ONLY") != "1":
        ui_main()
    print(f"PC 표 정렬 {PASSED}개 통과")


# ------------------------------------------------------------------ 2. 화면: 접수 목록 정렬 상자·Shift 2차 정렬, 공통 표 정렬
def api(ctx, url, path, body=None):
    options = {"headers": {"X-DG-CSRF": "1"}, "timeout": 30000}
    r = ctx.request.get(url + "api/" + path, **options) if body is None else ctx.request.post(url + "api/" + path, data=body, **options)
    assert r.status == 200, (path, r.status, r.text())
    return r.json()


def wait_js(page, expr, arg=None, timeout=10000):
    """PC 화면 CSP(script-src 'self')는 wait_for_function 의 문자열 평가를 막으므로 evaluate 로 직접 기다린다."""
    for _ in range(max(1, timeout // 50)):
        if page.evaluate(expr, arg):
            return
        page.wait_for_timeout(50)
    raise AssertionError("기다린 조건이 시간 안에 참이 되지 않음: " + expr)


def open_screen(page, url, hash_):
    page.goto("about:blank")
    page.goto(url + hash_)
    page.locator(".shell main h1").first.wait_for()


ROWS_JS = "() => [...document.querySelectorAll('.tablecard table.grid tbody tr')].map((tr) => tr.querySelector('td.code a')?.title || '')"
LAST_LIST = "() => window.__lists.at(-1) || ''"
COUNT = "() => window.__lists.length"


def wait_list(page, before):
    """접수 목록 조회가 한 번 더 끝나고 표가 다 그려질 때까지."""
    wait_js(page, "(n) => window.__lists.length > n && !document.querySelector('.tablecard tbody[aria-busy]')"
                  " && !!document.querySelector('.tablecard tbody tr td.code a')", before)


def requests_ui(ctx, page, url):
    print("[접수 목록 정렬]", flush=True)
    open_screen(page, url, "#/requests")
    wait_list(page, 0)
    head = page.locator(".tablecard thead")
    for name in ("금액", "위치", "배정"):
        ok(head.get_by_role("button", name=name, exact=False).count() == 1, f"'{name}' 열 머리가 정렬 단추")
    ok(page.locator("#rq-sort1").count() == 1 and page.locator("#rq-sort2").count() == 1, "정렬 상자: 1차·2차 기준 고르기가 따로 있음(빠른 조회와 분리)")
    ok(page.locator("#rq-sort1 option[value=received_at]").count() == 1 and page.locator("#rq-sort1 option[value=pay]").count() == 1
       and page.locator("#rq-sort1 option[value=phone]").count() == 0, "표에 없는 접수일·수납 상태는 정렬 상자에서 고름(연락처 정렬 없음)")

    def displayed():
        return page.evaluate(ROWS_JS)

    def server_order(qs):
        return [r["receipt_no"] for r in api(ctx, url, qs)["rows"]]

    def th_of(name):
        return page.locator(".tablecard thead th").filter(has=page.get_by_role("button", name=name, exact=False))

    # 머리 단추: 금액 오름 → 내림
    n = page.evaluate(COUNT)
    head.get_by_role("button", name="금액", exact=False).click()
    wait_list(page, n)
    last = page.evaluate(LAST_LIST)
    ok("sort=fee" in last and "dir=&" in last + "&", "금액 머리를 누르면 서버에 sort=fee 오름차순으로 다시 조회")
    ok(displayed() == server_order(last), "표 순서 = 서버가 정렬한 순서")
    ok(th_of("금액").get_attribute("aria-sort") == "ascending", "1차 정렬 열 머리에 aria-sort=ascending")
    n = page.evaluate(COUNT)
    head.get_by_role("button", name="금액", exact=False).click()
    wait_list(page, n)
    ok("dir=desc" in page.evaluate(LAST_LIST) and th_of("금액").get_attribute("aria-sort") == "descending", "다시 누르면 내림차순")
    page.get_by_label("쪽당 건수").select_option("100")
    wait_js(page, "() => (window.__lists.at(-1) || '').includes('size=100') && !document.querySelector('.tablecard tbody[aria-busy]')")
    amounts = page.evaluate("() => [...document.querySelectorAll('.tablecard tbody tr')].map((tr) => tr.children[10].textContent)")
    ok(amounts[0].replace(",", "").startswith("45000") and amounts[-1] == "금액 확인 대기", f"금액 큰 순, 금액 미정은 맨 뒤 ({amounts[0]} … {amounts[-1]})")

    # Shift+클릭: 2차 기준
    n = page.evaluate(COUNT)
    head.get_by_role("button", name="위치", exact=False).click(modifiers=["Shift"])
    wait_list(page, n)
    last = page.evaluate(LAST_LIST)
    ok("sort=fee" in last and "sort2=loc" in last, "Shift+클릭은 1차(금액)를 두고 2차 기준(위치)을 더함")
    loc_th = th_of("위치")
    ok(loc_th.get_attribute("aria-sort") in (None, "none") and "2차" in (loc_th.get_by_role("button").get_attribute("aria-label") or ""),
       "aria-sort 는 1차 열에만, 2차 열은 단추 이름으로 '2차 정렬'을 알림")
    ok(loc_th.locator(".arr2").inner_text().strip() == "2", "2차 기준 머리 화살표 옆 작은 '2'")
    ok(page.locator("#rq-sort2").input_value() == "loc" and page.locator("#rq-sort1").input_value() == "fee", "정렬 상자도 같은 상태를 보여 줌")
    summary = page.get_by_role("status", name="접수 조회 범위").inner_text()
    ok("정렬" in summary and "금액" in summary and "위치" in summary, f"조회 범위 요약에 정렬 기준이 나옴 ({summary})")
    ok(displayed() == server_order(page.evaluate(LAST_LIST)), "2차 정렬 결과도 서버 순서와 같음")

    # 정렬 상자(키보드): 2차 방향 바꾸기
    n = page.evaluate(COUNT)
    page.locator("#rq-dir2").select_option("desc")
    wait_list(page, n)
    ok("dir2=desc" in page.evaluate(LAST_LIST), "정렬 상자에서 2차 방향을 바꾸면 dir2=desc 로 조회")

    # 선택 유지
    page.locator(".tablecard tbody tr input[type=checkbox]").first.check()
    ok("1건 선택" in page.locator(".selbar").inner_text(), "한 건 고름")
    n = page.evaluate(COUNT)
    head.get_by_role("button", name="배정", exact=False).click()
    wait_list(page, n)
    ok("1건 선택" in page.locator(".selbar").inner_text(), "정렬을 바꿔도 일괄 선택은 그대로")
    last = page.evaluate(LAST_LIST)
    ok("sort=crew" in last and "sort2=&" in last + "&", "그냥 누르면 그 열 하나로 정렬(2차 해제)")
    first_two = page.evaluate("() => [...document.querySelectorAll('.tablecard tbody tr')].slice(0, 3).map((tr) => tr.children[12].textContent)")
    ok(first_two[0].startswith("가상 정렬팀") and first_two[1].startswith("가상 정렬팀") and first_two[2] == "", "배정 정렬: 배정된 건이 앞, 미배정은 뒤")

    # 프리셋
    presets = page.get_by_role("group", name="정렬 빠른 고르기")
    for label in ("예정일 → 동", "접수일 최신", "금액 큰 순", "위치 없음 먼저"):
        ok(presets.get_by_role("button", name=label, exact=True).count() == 1, f"정렬 프리셋 '{label}'")
    n = page.evaluate(COUNT)
    presets.get_by_role("button", name="접수일 최신", exact=True).click()
    wait_list(page, n)
    last = page.evaluate(LAST_LIST)
    ok("sort=received_at" in last and "dir=desc" in last and "sort2=&" in last + "&", "'접수일 최신' = 접수일 내림차순 하나")
    ok(presets.get_by_role("button", name="접수일 최신", exact=True).get_attribute("aria-pressed") == "true", "고른 프리셋은 눌림 표시")
    ok(page.locator(".tablecard tbody tr").first.locator(".rcv").count() == 1, "접수일로 정렬하면 예정일 칸 아래 접수일을 작게 보여 줌(열을 늘리지 않음)")
    if os.environ.get("DG_TEST_DIR"):
        shots = Path(os.environ["DG_TEST_DIR"]) / "shots"
        shots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots / "pc-requests-sort.png"), full_page=False)
    n = page.evaluate(COUNT)
    presets.get_by_role("button", name="예정일 → 동", exact=True).click()
    wait_list(page, n)
    last = page.evaluate(LAST_LIST)
    ok("sort=planned_date" in last and "sort2=dong" in last, "'예정일 → 동' = 예정일 → 동 2단계")
    ok(page.locator(".tablecard tbody tr").first.locator(".rcv").count() == 0, "접수일 정렬이 아니면 접수일 줄 없음")

    # 쪽 넘김·필터 변경·다른 화면 다녀오기에도 정렬 유지
    page.get_by_label("쪽당 건수").select_option("20")
    wait_js(page, "() => (window.__lists.at(-1) || '').includes('size=20') && !document.querySelector('.tablecard tbody[aria-busy]')")
    n = page.evaluate(COUNT)
    page.get_by_role("button", name="다음 ›").click()
    wait_list(page, n)
    last = page.evaluate(LAST_LIST)
    ok("page=2" in last and "sort=planned_date" in last and "sort2=dong" in last, "다음 쪽에도 같은 정렬")
    n = page.evaluate(COUNT)
    page.locator("#rq-status").select_option("")
    wait_list(page, n)
    ok("sort2=dong" in page.evaluate(LAST_LIST), "조회조건을 바꿔도 정렬 유지")
    page.goto(url + "#/home")
    page.locator(".shell main h1").first.wait_for()
    n = page.evaluate(COUNT)
    page.goto(url + "#/requests")
    wait_list(page, n)
    ok("sort=planned_date" in page.evaluate(LAST_LIST) and page.locator("#rq-sort2").input_value() == "dong", "다른 화면에 다녀와도(같은 세션) 정렬 유지")
    ok("1건 선택" in page.locator(".selbar").inner_text(), "다녀와도 선택 유지")
    page.get_by_role("button", name="선택 해제").click()

    # 접속기록: 같은 쪽 번호라도 정렬에 따라 본 행이 달라지므로 정렬 조건(2차 포함)도 남긴다. 검색어 내용은 남기지 않는다.
    rows = api(ctx, url, "access?action=LIST&size=500")["rows"]
    lists = [r["filter"] for r in rows if (r["filter"] or "").startswith("전체")]
    ok(lists and any("page=2" in f for f in lists) and any("sort=" in f for f in lists) and any("sort2=" in f for f in lists),
       "접수 목록 접속기록: 건수·조건·쪽 번호와 정렬 조건(1·2차)을 남김")


HELPER_JS = r"""async () => {
  const C = await import('/core.js');
  const box = document.createElement('div'); box.id = 'sort-fixture'; document.querySelector('.shell main').prepend(box);
  window.__clicked = [];
  const data = [['가상10동', '1,200원', '2026-11-03T01:00:00Z', 'a'], ['가상2동', '300원', '2025-12-30T01:00:00Z', 'b'],
    ['', '', '', 'c'], ['가상2동', '1,200원', '2026-01-01T00:00:00Z', 'd']];
  const rows = data.map(([d, w, at, id]) => C.h('tr', { 'data-id': id, onclick: () => window.__clicked.push(id) },
    C.h('td', {}, d), C.h('td', { class: 'num' }, w), C.cell(at ? C.kst(at) : '', at), C.h('td', {}, C.h('input', { type: 'checkbox', 'aria-label': id + ' 고르기' }))));
  rows.push(C.h('tr', { class: 'sum', 'data-id': 'sum' }, C.h('td', {}, '합계'), C.h('td', { class: 'num' }, '2,700원'), C.h('td', {}), C.h('td', {})));
  box.append(C.grid(['동', '금액', '시각', '고르기'], rows, { sort: { key: 'fixture-helper', cols: ['text', 'num', 'date', null] } }));
}"""
IDS = "() => [...document.querySelectorAll('#sort-fixture tbody tr')].map((tr) => tr.dataset.id).join('')"


def helper_ui(page, url):
    print("[공통 표 정렬 도우미]", flush=True)
    open_screen(page, url, "#/home")
    page.evaluate(HELPER_JS)
    box = page.locator("#sort-fixture")
    ok(box.locator("thead th button.sort").count() == 3, "정렬할 열(동·금액·시각)만 머리 단추, 고르기 칸은 그대로")
    ok(page.evaluate(IDS) == "abcdsum", "처음은 원래 순서")
    dong = box.get_by_role("button", name="동", exact=False)
    dong.click()
    ok(page.evaluate(IDS) == "bdacsum", "글자 오름: 숫자 섞인 이름은 숫자 크기대로(가상2동 < 가상10동), 같은 값은 원래 순서, 빈 값은 뒤, 합계 행은 맨 아래")
    ok(box.locator("thead th").first.get_attribute("aria-sort") == "ascending", "aria-sort=ascending")
    dong.click()
    ok(page.evaluate(IDS) == "abdcsum", "글자 내림: 같은 값은 원래 순서 유지(안정 정렬), 빈 값은 여전히 뒤")
    dong.click()
    ok(page.evaluate(IDS) == "abcdsum" and box.locator("thead th").first.get_attribute("aria-sort") == "none", "세 번째 누르면 원래 순서")
    money = box.get_by_role("button", name="금액", exact=False)
    money.click()
    ok(page.evaluate(IDS) == "badcsum", "원화 금액은 숫자로 비교(300원 < 1,200원)")
    money.click()
    ok(page.evaluate(IDS) == "adbcsum", "금액 내림, 빈 값은 뒤")
    money.click()
    when = box.get_by_role("button", name="시각", exact=False)
    when.click()
    ok(page.evaluate(IDS) == "bdacsum", "날짜·시각은 화면 글자(연도 없음)가 아니라 원래 시각 값으로 비교(해를 넘겨도 맞음)")
    box.get_by_label("b 고르기").check()
    when.click()
    ok(page.evaluate(IDS) == "adbcsum", "시각 내림")
    page.evaluate("() => { window.__clicked = []; }")
    box.locator("tr[data-id=b] td").first.click()
    ok(box.get_by_label("b 고르기").is_checked() and page.evaluate("() => window.__clicked.join('')") == "b", "행을 옮겨도 고름 상태·행 클릭 처리가 그대로")
    # tbody 를 새로 채워도 같은 정렬을 다시 적용
    page.evaluate("""async () => { const C = await import('/core.js'); const tb = document.querySelector('#sort-fixture tbody');
      tb.replaceChildren(...[['x', '2026-01-02T00:00:00Z'], ['y', '2026-03-01T00:00:00Z'], ['z', '2025-01-01T00:00:00Z']].map(([id, at]) =>
        C.h('tr', { 'data-id': id }, C.h('td', {}, id), C.h('td', {}), C.cell(C.kst(at), at), C.h('td', {})))); }""")
    wait_js(page, "() => [...document.querySelectorAll('#sort-fixture tbody tr')].map((tr) => tr.dataset.id).join('') === 'yxz'")
    ok(True, "다시 그린 행에도 같은 정렬(시각 내림)을 곧바로 적용")
    # 같은 표를 다시 만들면(화면 다시 그리기) 정렬 상태 유지
    page.evaluate("() => document.getElementById('sort-fixture').remove()")
    page.evaluate(HELPER_JS)
    ok(page.evaluate(IDS) == "adbcsum" and page.locator("#sort-fixture thead th").nth(2).get_attribute("aria-sort") == "descending",
       "같은 화면을 다시 그려도(같은 세션) 정렬 상태 유지")
    page.evaluate("() => document.getElementById('sort-fixture').remove()")


def screens_ui(page, url):
    print("[화면별 적용]", flush=True)
    open_screen(page, url, "#/users")
    card = page.locator(".card").filter(has=page.get_by_role("heading", name="사용자", exact=True))
    users = card.locator("table").first
    users.locator("tbody tr").first.wait_for()
    users.get_by_role("button", name="아이디", exact=True).click()
    names = users.locator("tbody tr td:first-child").all_inner_texts()
    ok(names == sorted(names) and len(names) >= 3, f"사용자 표: 아이디 머리로 정렬 ({names})")
    users.get_by_role("button", name="아이디", exact=True).click()
    names_desc = users.locator("tbody tr td:first-child").all_inner_texts()
    ok(names_desc == sorted(names, reverse=True), "사용자 표: 다시 누르면 내림")
    users.locator("tbody tr").first.get_by_role("button", name="비밀번호 재설정").click()
    ok(users.locator("tbody tr").first.locator("input[type=password]").is_visible(), "정렬 뒤에도 행 안 단추가 그대로 동작")
    page.goto(url + "#/home")
    page.locator(".shell main h1").first.wait_for()
    page.goto(url + "#/users")
    card = page.locator(".card").filter(has=page.get_by_role("heading", name="사용자", exact=True))
    users = card.locator("table").first
    users.locator("tbody tr").first.wait_for()
    ok(users.locator("tbody tr td:first-child").all_inner_texts() == names_desc
       and users.locator("thead th").first.get_attribute("aria-sort") == "descending", "다른 화면에 다녀와도 사용자 표 정렬 유지")
    audit = page.locator("table.audit")
    ok("이 쪽" in (audit.locator("thead button.sort").first.get_attribute("title") or "") and "이 쪽" in audit.locator("caption").inner_text(),
       "서버가 쪽 나눔하는 변경 이력 표는 '이 쪽 안에서만' 정렬함을 머리 설명·작은 글씨로 알림")
    open_screen(page, url, "#/access")
    acc = page.locator("table.audit")
    wait_js(page, "() => document.querySelectorAll('table.audit tbody tr td.rn').length > 1")
    ok("이 쪽" in (acc.locator("thead button.sort").first.get_attribute("title") or ""), "접속기록 표도 '이 쪽만' 정렬 안내")
    acc.get_by_role("button", name="번호", exact=True).click()
    nums = [int(x) for x in acc.locator("tbody tr td.rn").all_inner_texts()]
    ok(nums == sorted(nums), "접속기록: 번호 오름(원래는 최신 먼저)")
    open_screen(page, url, "#/settings/dongs")
    page.locator("table.dongedit").wait_for()
    ok(page.locator("table.dongedit thead button.sort").count() == 0, "동 순서 편집 표는 정렬 제외(순서 자체가 설정)")
    open_screen(page, url, "#/home")
    page.locator("table.dongtbl").wait_for()
    ok(page.locator("table.dongtbl thead button.sort").count() == 5, "오늘 현황 동별 표: 동·담당 팀·예정·완료·미처리 정렬")
    open_screen(page, url, "#/stats")
    page.locator("main table.grid thead button.sort").first.wait_for(state="attached")
    ok(page.locator("main table.grid thead button.sort").count() > 10, "통계 표들 열 머리 정렬")
    open_screen(page, url, "#/fees")
    full = page.locator(".card").filter(has=page.get_by_role("heading", name="전체 표", exact=True)).locator("table")
    full.locator("tbody tr").first.wait_for()
    first = full.locator("tbody tr").first.inner_text()
    full.get_by_role("button", name="단가", exact=True).click()
    full.get_by_role("button", name="단가", exact=True).click()
    prices = [int("".join(ch for ch in t if ch.isdigit()) or 0) for t in full.locator("tbody tr td:nth-child(5)").all_inner_texts()[:5]]
    ok(prices == sorted(prices, reverse=True) and prices[0] > 0, f"수수료 전체 표: 단가 큰 순 ({prices})")
    full.get_by_role("button", name="단가", exact=True).click()
    ok(full.locator("tbody tr").first.inner_text() == first and "원문 순서" in full.locator("caption").inner_text(), "세 번째 누르면 원문 순서로 돌아감(원문 대조용 기본 순서 유지)")
    ok(page.locator(".card").filter(has=page.get_by_role("heading", name="확인 필요 행", exact=False)).locator("thead button.sort").count() == 0,
       "확인 필요 행(확정 단가 입력 양식)은 정렬 제외")


def screens_smoke(page, url, rid):
    """정렬을 붙인 화면들: 그리기 오류 없음, 정렬 옵션이 칸 내용으로 새지 않음([object Object] 없음), 제외한 표는 정렬 단추 없음."""
    print("[화면 그리기 점검]", flush=True)
    for hash_ in ("#/home", "#/dongs", "#/requests", f"#/request/{rid}", "#/labels", "#/dispatch", "#/send", "#/results", "#/close", "#/revenue",
                  "#/refunds", "#/qrtest", "#/stats", "#/reports", "#/pilot", "#/fees", "#/settings/basic", "#/settings/crews", "#/settings/devices",
                  "#/settings/dongs", "#/settings/capacity", "#/settings/holidays", "#/settings/fees", "#/settings/backup", "#/settings/privacy",
                  "#/users", "#/access", "#/help"):
        open_screen(page, url, hash_)
        page.wait_for_timeout(400)
        text = page.locator(".shell main").inner_text()
        ok("[object Object]" not in text and "undefined" not in text.split("\n")[0], f"{hash_} 그리기 정상")
        clash = page.evaluate("""() => { const sorts = new Set([...document.querySelectorAll('thead button.sort')].map((b) => b.childNodes[0]?.textContent?.trim()));
          return [...document.querySelectorAll('button:not(.sort)')].map((b) => b.textContent.trim()).filter((t) => sorts.has(t)); }""")
        ok(not clash, f"{hash_} 정렬 머리 단추 이름이 업무 단추와 겹치지 않음 {clash}")
    open_screen(page, url, f"#/request/{rid}")
    page.locator("main table.grid").first.wait_for(state="attached")
    ok(page.locator("main table.grid").first.locator("thead button.sort").count() == 0, "접수 상세 품목 표(현장 기록과 줄 대조)는 정렬 제외")


def ui_main():
    from playwright.sync_api import sync_playwright
    from test_review_ui import pc_server
    import dgproto as P
    PW = "table-sort-fixture-password-123"
    with pc_server() as url, sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROME") or pw.chromium.executable_path, headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        ctx.add_init_script("""(() => { window.__lists = []; const real = window.fetch.bind(window);
          window.fetch = (path, opts) => { const s = String(path); if (s.startsWith('/api/requests?')) window.__lists.push(s.slice(5)); return real(path, opts); }; })()""")
        ctx.route("https://tiles.openfreemap.org/**", lambda route: route.fulfill(json={
            "version": 8, "sources": {}, "layers": [{"id": "background", "type": "background", "paint": {"background-color": "#dde6e8"}}]}))
        errors: list[str] = []
        try:
            api(ctx, url, "setup", {"username": "sort_admin", "display_name": "가상 정렬관리자", "password": PW})
            boot = api(ctx, url, "bootstrap")
            today = boot["today"]
            raw = os.urandom(16) + P.pub_raw(P.gen_key()) + P.pub_raw(P.gen_key())
            code = "DGD1." + base64.urlsafe_b64encode(raw + hashlib.sha256(raw).digest()[:4]).decode().rstrip("=")
            dev = api(ctx, url, "devices/register", {"code": code, "label": "가상 정렬폰"})["device_id"]
            api(ctx, url, "devices/" + dev + "/confirm", {})
            crew = api(ctx, url, "crews", {"name": "가상 정렬팀", "device_id": dev})["id"]
            for d in ("가상정렬가동", "가상정렬나동"):
                api(ctx, url, "dongs", {"name": d})
            for u in ("sort_zeta", "sort_alpha"):
                api(ctx, url, "users", {"username": u, "display_name": "가상 " + u, "role": "INTAKE", "password": PW})
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url + "#/home")
            page.locator(".shell main h1").first.wait_for()
            fees = page.evaluate("""async () => { const rows = (await import('/web/fees-data.js')).BUILTIN_FEES.rows;
              const id = (n, s) => rows.find((r) => r.name === n && r.spec === s).id; return [id('의자', '고정식 의자'), id('장롱', '120cm 이상 1쪽당')]; }""")
            made = []
            for i in range(24):
                item = [{"name": "가상 미등록 품목", "qty": 1}] if i % 7 == 3 else [{"fee_rate_id": fees[1] if i == 5 else fees[0], "qty": 3 if i == 5 else 1 + i % 3}]
                body = {"name": f"가상목록{i:02d}", "phone": f"0100000{8000 + i:04d}", "address": f"가상구 정렬로 {i + 1}", "planned_date": today,
                        "dong": "가상정렬가동" if i % 2 else "가상정렬나동", "items": item}
                if i % 3:
                    body.update(lat=35.1 + i / 1000, lon=126.9)
                made.append(api(ctx, url, "requests", body)["id"])
            api(ctx, url, "assign", {"date": today, "plan": {crew: [{"request_id": made[1], "eta": "09:30"}, {"request_id": made[4], "eta": "10:00"}]}})
            requests_ui(ctx, page, url)
            helper_ui(page, url)
            screens_ui(page, url)
            screens_smoke(page, url, made[0])
            ok(not errors, "화면 스크립트 오류 없음: " + "; ".join(errors))
        except BaseException:
            out = Path(os.environ.get("DG_TEST_DIR") or tempfile.mkdtemp(prefix="dg-table-sort-"))
            out.mkdir(parents=True, exist_ok=True)
            for i, p in enumerate(ctx.pages):
                try:
                    p.screenshot(path=str(out / f"table-sort-{i}.png"), full_page=True)
                except Exception:
                    pass
            print("화면 캡처:", out, flush=True)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
