#!/usr/bin/env python3
"""동구 대형폐기물 - PC 프로그램

  python pc_tool.py init                 암호화 DB와 PC 키 만들기 (처음 한 번)
  python pc_tool.py serve                업무 화면 열기 → 브라우저에서 http://127.0.0.1:8765
  python pc_tool.py status               기기·묶음·가져오기 현황

시험·자동화용 명령
  python pc_tool.py reg-qr               기기 등록 QR 화면(HTML) 만들기
  python pc_tool.py register-device DGD1.xxxx
  python pc_tool.py make-bundle --device <앞8자> --count 20   가상 접수를 만들어 배정하고 목록 QR 화면 생성
  python pc_tool.py confirm-receipt <묶음 앞8자> <코드>
  python pc_tool.py import-result <파일.dgr>
  python pc_tool.py import-photos <파일.dgp>
  python pc_tool.py backup | restore <파일.dgbak> | reset-admin | purge-photos
  python pc_tool.py import-addr <위치정보요약DB 파일·폴더> --region 동구 [--preview]
  python pc_tool.py purge-pii [--preview]        보유기간 지난 접수 개인정보 파기(가명 처리) · 파기 대장 기록
  python pc_tool.py backup-auto                  백업 공개키로 자동 백업 1회 (비밀번호 불필요)
  python pc_tool.py restore-auto <파일.dgabk> --key <개인키.pem>   자동 백업 복원 (업무 화면을 끄고)
  python pc_tool.py serve --lan --cert cert.pem --key key.pem --allow 10.0.0.0/24 [--host 0.0.0.0] [--hostname 서버이름]
                                                 (선택) 여러 접수 PC용 LAN 모드 — 정보화부서 승인 필요, HTTPS 전용

자료는 SQLCipher로 암호화된 pc/state/pc.db 에 있고, PC 개인키도 그 안에 있다.
DB 키는 운영체제 키 저장소(또는 DG_DB_KEY, 또는 시작할 때 입력)에서 가져온다.
이 프로그램은 업무 자료를 외부로 보내지 않는다. 화면 서버는 이 PC(127.0.0.1)에서만 열린다.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import io
import json
import os
import sys
import uuid
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dgproto as P  # noqa: E402
from dgpc import service as S, store  # noqa: E402
from sample_data import make_daylist  # noqa: E402

OUT = Path(os.environ.get("DG_OUT", Path(__file__).parent / "out"))
KST = dt.timezone(dt.timedelta(hours=9))


def qr_png_data_uri(text: str, ec: str = "M") -> tuple[str, int]:
    level = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q}[ec]
    q = qrcode.QRCode(error_correction=level, box_size=8, border=4)
    q.add_data(qrcode.util.QRData(text, mode=qrcode.util.MODE_ALPHA_NUM))
    q.make(fit=True)
    buf = io.BytesIO()
    q.make_image().save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), q.version


def con():
    return store.connect()


def registration_text() -> str:
    return S.registration_text(con())


def display_html(imgs, short, work_date, ver, count, fps, crew="") -> str:
    data = json.dumps(imgs)
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>목록 전송 {short}</title>
<style>
body{{margin:0;font-family:system-ui,sans-serif;background:#fff;color:#111;display:flex;flex-direction:column;align-items:center}}
header{{padding:12px;text-align:center}} img{{width:min(78vmin,640px);image-rendering:pixelated;margin:8px}}
.n{{font-size:42px;font-weight:700}} .ctl button{{font-size:18px;padding:8px 16px;margin:4px}}
</style>
<header><div>{html.escape(crew)} · {html.escape(work_date)}{f" · {ver}차 목록" if str(ver).isdigit() and int(ver) > 1 else ""}{f" · {count}곳" if count != "" else ""}</div>
<div class="n" id="n"></div></header>
<img id="q" alt="전송 QR">
<div class="ctl"><button id="slow">느리게</button><button id="pause">일시정지</button><button id="fast">빠르게</button>
<span id="fps"></span></div>
<p>휴대폰에서 [오늘 목록 받기]를 누르고 이 화면을 비추세요. 다 받을 때까지 계속 반복됩니다.</p>
<script>
const imgs={data}; let i=0, fps={fps}, run=true, t;
const q=document.getElementById('q'), n=document.getElementById('n'), f=document.getElementById('fps');
function show(){{ q.src=imgs[i]; n.textContent=(i+1)+' / '+imgs.length; f.textContent=fps+'장/초'; }}
function tick(){{ if(run){{ i=(i+1)%imgs.length; show(); }} t=setTimeout(tick, 1000/fps); }}
document.getElementById('slow').onclick=()=>fps=Math.max(1,fps-1);
document.getElementById('fast').onclick=()=>fps=Math.min(10,fps+1);
document.getElementById('pause').onclick=e=>{{run=!run; e.target.textContent=run?'일시정지':'계속';}};
show(); t=setTimeout(tick,1000/fps);
</script></html>"""


# ------------------------------------------------------------------ 명령
def cmd_init(_):
    c = store.connect(create=True)
    made = S.ensure_pc_keys(c)
    print(("PC 키와 암호화 DB를 만들었습니다: " if made else "이미 초기화되어 있습니다: ") + str(store.state_dir() / "pc.db"))


def cmd_reg_qr(_):
    uri, ver = qr_png_data_uri(registration_text(), "M")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "register.html"
    path.write_text(f"""<!doctype html><meta charset="utf-8"><title>기기 등록 QR</title>
<style>body{{font-family:sans-serif;text-align:center;margin:40px}}img{{width:min(80vmin,520px);image-rendering:pixelated}}</style>
<h1>기기 등록 QR</h1><p>휴대폰 앱 → [기기 등록] → 이 QR을 비추세요. (공개키만 들어 있습니다)</p>
<img src="{uri}" alt="등록 QR"><p>QR 버전 {ver}</p>""", encoding="utf-8")
    print("등록 화면:", path)


def cmd_register_device(a):
    try:
        r = S.register_device(con(), a.code, a.label or "")
    except S.UserError as e:
        sys.exit(str(e))
    print("등록 완료:", r["device_id"])
    print("지문:", r["fingerprint"], "← 휴대폰 화면의 숫자와 같은지 확인하세요")


def cmd_make_bundle(a):
    """가상 접수를 PC에 실제로 등록·배정한 뒤 그 배정으로 목록 QR을 만든다 (화면 없이 하는 시험용)."""
    c = con()
    devs = [d for d in S.list_devices(c) if d["id"].startswith(a.device)]
    if len(devs) != 1:
        sys.exit(f"'{a.device}'에 해당하는 기기가 {len(devs)}개입니다.")
    dev = devs[0]["id"]
    crew = next((x for x in S.list_crews(c) if x["device_id"] == dev), None)
    cid = crew["id"] if crew else S.save_crew(c, {"name": f"가상 시험팀 {dev[:4]}", "device_id": dev, "capacity_m3": 30, "shift_end": "18:00"})
    work_date = a.date or dt.datetime.now(KST).date().isoformat()
    sample = make_daylist(a.count, work_date, 1, seed=a.seed)
    stops = []
    for s in sample["stops"]:
        r = S.create_request(c, {"name": s["name"], "phone": s["phone"], "address": s["address"], "spot_note": s["spot_note"],
                                 "memo": s["memo"], "planned_date": work_date, "lat": s["lat"], "lon": s["lon"],
                                 "items": [{"fee_rate_id": i["fee_rate_id"], "qty": i["qty"]} for i in s["items"]]})
        stops.append({"request_id": r["id"], "eta": s["eta"]})
    batch = S.confirm_plan(c, work_date, {cid: stops})[0]
    try:
        b = S.make_bundle(c, batch["batch_id"], chunk=a.chunk)
    except S.UserError as e:
        sys.exit(str(e))
    short = b["bundle_id"][:8]
    imgs, vers = [], set()
    for f in b["frames"]:
        uri, v = qr_png_data_uri(f, a.ec)
        imgs.append(uri)
        vers.add(v)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"bundle_{short}.html"
    path.write_text(display_html(imgs, short, work_date, b["assign_ver"], b["count"], a.fps), encoding="utf-8")
    (OUT / f"bundle_{short}.frames.json").write_text(json.dumps(b["frames"]))
    print(f"묶음 {short}: 접수 {b['count']}건, 암호문 {b['bytes']:,}B, QR {len(b['frames'])}장 "
          f"(조각 {a.chunk}B, 오류복원 {a.ec}, QR 버전 {min(vers)}~{max(vers)})")
    print("표시 화면:", path)


def cmd_confirm_receipt(a):
    try:
        bid = S.confirm_receipt(con(), a.bundle, a.code)
    except S.UserError as e:
        sys.exit(str(e))
    print(f"묶음 {bid[:8]} 모바일 수신 확인 완료")


def cmd_import_result(a):
    try:
        r = S.import_result(con(), Path(a.file).read_bytes(), a.channel)
    except S.UserError as e:
        sys.exit(f"결과 파일 검증 실패: {e}" if "검증" in str(e) else str(e))
    if r.get("duplicate_file"):
        print("이미 가져온 파일입니다. 변경 없음.")
        return
    print("가져오기 결과:", json.dumps({k: r[k] for k in ("applied", "skipped_duplicate", "conflict", "unloads") if k in r}, ensure_ascii=False))
    print("사진 해시 목록:", r["photos_expected"], "건 (사진 파일은 보안 매체로 별도 회수)")
    print("보관 종료 확인 코드:", r["release_code"], "← 휴대폰 [결과 보내기] 화면에 입력하면 반영됨으로 표시됩니다")


def cmd_import_photos(a):
    try:
        r = S.import_photos(con(), Path(a.file).read_bytes())
    except S.UserError as e:
        sys.exit(str(e))
    print("사진 회수:", json.dumps({k: r.get(k) for k in ("recovered", "already", "hash_mismatch", "unexpected")}, ensure_ascii=False))
    print("사진 회수 확인 코드:", r["release_code"] or "발급 안 함(해시 불일치 사진이 있어 휴대폰에서 지우면 안 됨)")


def cmd_status(_):
    c = con()
    print("[기기]")
    for r in S.list_devices(c):
        print("  ", r["id"][:8], r["status"], r["registered_at"], r["label"] or "")
    print("[목록 묶음]")
    for r in S.rows(c, "SELECT id, device_id, work_date, assign_ver, chunk_count, receipt_confirmed_at FROM transfer_bundle ORDER BY displayed_at"):
        print("  ", r["id"][:8], r["device_id"][:8], r["work_date"], r["assign_ver"], r["chunk_count"], "수신 확인" if r["receipt_confirmed_at"] else "수신 미확인")
    print("[결과 가져오기]")
    for r in S.rows(c, "SELECT id, channel, imported_at, summary_json FROM result_import ORDER BY imported_at"):
        print("  ", r["id"][:8], r["channel"], r["imported_at"], r["summary_json"])


def _backup_pass(confirm: bool) -> str:
    import getpass
    v = os.environ.get("DG_BACKUP_PASS")
    if v:
        return v
    a = getpass.getpass("백업 비밀번호: ")
    if confirm and getpass.getpass("한 번 더: ") != a:
        sys.exit("두 번 입력한 비밀번호가 다릅니다.")
    return a


def cmd_backup(_):
    c = con()
    S.ACTOR.set("cli")
    try:
        r = S.make_backup(c, _backup_pass(True))
    except S.UserError as e:
        sys.exit(str(e))
    print(f"백업 완료: {r['file']}  ({r['bytes']:,}B, sha256 {r['sha256'][:16]}…)")


def cmd_restore(a):
    from dgpc import store
    print("※ PC 업무 화면(serve)을 끈 상태에서 실행하세요. 기존 DB는 지우지 않고 옆에 보관합니다.")
    if not a.yes and input("계속할까요? (yes 입력): ").strip() != "yes":
        sys.exit("취소했습니다.")
    try:
        r = store.restore_from(a.file, _backup_pass(False))
    except ValueError as e:
        sys.exit(str(e))
    c = store.connect()
    S.ACTOR.set("cli")
    with S.tx(c):
        S.audit(c, "backup", os.path.basename(a.file), "RESTORE", {"file": os.path.basename(a.file), "requests": r["requests"]})
    print(f"복원 완료: 접수 {r['requests']}건." + (f" 이전 DB 보관: {r['kept']}" if r["kept"] else ""))


def cmd_reset_admin(a):
    """관리자 비밀번호를 잊었을 때: DB 키를 아는 사람만 이 PC에서 실행할 수 있다."""
    import getpass
    c = con()
    S.migrate_legacy_login(c)
    u = S.one(c, "SELECT id FROM app_user WHERE username=? AND role='ADMIN'", (a.username,))
    if not u:
        sys.exit("해당 관리자 아이디가 없습니다.")
    pw = os.environ.get("DG_NEW_PASS") or getpass.getpass("새 임시 비밀번호(10자 이상): ")
    S.ACTOR.set("cli")
    try:
        S.set_password(c, u["id"], pw, must_change=True)
        c.execute("UPDATE app_user SET active=1 WHERE id=?", (u["id"],))
    except S.UserError as e:
        sys.exit(str(e))
    print("재설정했습니다. 다음 로그인 때 새 비밀번호로 바꾸게 됩니다.")


def cmd_purge_photos(_):
    c = con()
    S.ACTOR.set("cli")
    r = S.purge_photos(c)
    print(f"사진 {r['count']}장 정리 ({r['bytes']:,}B)")


def cmd_import_addr(a):
    """행안부 도로명주소 위치정보요약DB → 이 PC의 암호화 DB (외부 전송 없음)"""
    from pathlib import Path
    from dgpc import geo
    c = con()
    S.ACTOR.set("cli")
    cols = geo.parse_columns_arg(a.columns) if a.columns else None
    files = sorted(Path(a.path).glob("*.txt")) if Path(a.path).is_dir() else [Path(a.path)]
    total = 0
    for f in files:
        try:
            info = S.import_addr_points(c, f.read_bytes(), a.region, cols, preview=a.preview)
        except S.UserError as e:
            print(f"{f.name}: {e}")
            continue
        print(f"{f.name}: 열 배치 {info['columns']} · 구분자 '{info['delimiter']}' · {info['count']:,}건")
        for x in info["sample"][:3]:
            print(f"   예) {x['sigungu']} {x['road']} {x['main']}{'-' + str(x['sub']) if x['sub'] else ''} → {x['lat']:.6f}, {x['lon']:.6f}")
        total += info["count"]
    print(("미리보기 " if a.preview else "가져오기 ") + f"합계 {total:,}건" + ("" if a.preview else f" · DB 전체 {S.addr_stats(c)['count']:,}건"))


def cmd_import_solo(a):
    """휴대폰 단독 방식 백업(.dgb) → PC 원본"""
    import getpass
    pw = os.environ.get("DG_SOLO_PASS") or getpass.getpass("휴대폰 백업 비밀번호: ")
    c = con()
    S.ACTOR.set("cli")
    crew = None
    if a.crew:
        row = S.one(c, "SELECT id FROM crew WHERE name=? OR id=?", (a.crew, a.crew))
        crew = row["id"] if row else sys.exit("없는 수거팀입니다.")
    try:
        r = S.import_solo(c, Path(a.file).read_bytes(), pw, crew, preview=a.preview)
    except S.UserError as e:
        sys.exit(str(e))
    print(("미리보기: " if a.preview else "옮김: ") + json.dumps(r, ensure_ascii=False))


def cmd_seed_dongs(_):
    """시험용 가상 행정동(가상1동~가상3동)을 넣는다. 실제 운영 DB에는 쓰지 말 것."""
    from sample_data import seed_dongs
    c = con()
    ids = seed_dongs(c)
    print(f"가상 행정동 {len(ids)}개를 넣었습니다 (이미 있던 이름은 건너뜀).")


def cmd_serve(a):
    from dgpc import server
    lan = None
    if a.lan:
        try:
            lan = server.lan_config(a.cert, a.key, a.allow, a.hostname)
        except ValueError as e:
            sys.exit(f"LAN 모드를 시작하지 않았습니다: {e}")
    elif a.cert or a.key or a.allow:
        sys.exit("--cert/--key/--allow 는 --lan 과 함께만 씁니다. (기본은 이 PC에서만 열림)")
    server.serve(a.port, lan, a.host)


def cmd_purge_pii(a):
    from dgpc import privacy
    c = con()
    S.ACTOR.set("cli")
    if a.preview:
        p = privacy.purge_preview(c)
        print(f"보유기간 {p['retention_years']}년 · 기준일 {p['cutoff']} 이전 {p['basis']} · 대상 {p['count']}건 (미리보기, 바꾸지 않음)")
        return
    r = privacy.purge_pii(c, trigger="CLI")
    print(f"개인정보 파기(가명 처리) {r['count']}건 · 기준일 {r['cutoff']} (보유기간 {r['retention_years']}년). 파기 대장에 기록했습니다.")
    print("※ 이전 백업 파일에는 그때의 개인정보가 남아 있습니다. 백업 보관 기간에 따라 지우세요.")


def cmd_backup_auto(_):
    from dgpc import backup
    c = con()
    S.ACTOR.set("cli")
    r = backup.make_auto(c, "CLI")
    if not r["ok"]:
        sys.exit(r.get("error") or "자동 백업 실패")
    print(f"자동 백업: {r['file']} ({r['bytes']:,}B, 키 지문 {r['fpr']})" + (f" · 두 번째 위치 {'확인' if r['dest2']['ok'] else '실패'}" if r.get("dest2") else ""))


def cmd_restore_auto(a):
    import getpass
    from dgpc import backup
    print("※ PC 업무 화면(serve)을 끈 상태에서 실행하세요. 기존 DB는 지우지 않고 옆에 보관합니다.")
    if not a.yes and input("계속할까요? (yes 입력): ").strip() != "yes":
        sys.exit("취소했습니다.")
    pw = os.environ.get("DG_BACKUP_PASS") or getpass.getpass("백업 개인키 보호 비밀번호: ")
    try:
        r = backup.restore_auto(Path(a.file), Path(a.key).read_bytes(), pw)
    except (S.UserError, ValueError) as e:
        sys.exit(str(e))
    c = store.connect()
    S.ACTOR.set("cli")
    with S.tx(c):
        S.audit(c, "backup", os.path.basename(a.file), "RESTORE", {"file": os.path.basename(a.file), "requests": r["requests"], "auto": True})
    print(f"복원 완료: 접수 {r['requests']}건." + (f" 이전 DB 보관: {r['kept']}" if r["kept"] else ""))


def main(argv=None):
    p = argparse.ArgumentParser(description="동구 대형폐기물 PC 프로그램")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("init").set_defaults(fn=cmd_init)
    sv = s.add_parser("serve"); sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--lan", action="store_true", help="(선택) 같은 망의 다른 접수 PC에 HTTPS로 열기 — 정보화부서 승인 필요")
    sv.add_argument("--cert"); sv.add_argument("--key"); sv.add_argument("--allow", help="허용 IP 대역, 쉼표로 (예: 10.0.0.0/24,10.0.1.15/32)")
    sv.add_argument("--host", help="LAN 모드에서 묶을 주소 (기본 0.0.0.0)"); sv.add_argument("--hostname", help="Host 헤더로 허용할 서버 이름 (기본: 이 PC 이름)")
    sv.set_defaults(fn=cmd_serve)
    s.add_parser("reg-qr").set_defaults(fn=cmd_reg_qr)
    r = s.add_parser("register-device"); r.add_argument("code"); r.add_argument("--label"); r.set_defaults(fn=cmd_register_device)
    m = s.add_parser("make-bundle")
    m.add_argument("--device", required=True); m.add_argument("--count", type=int, default=20)
    m.add_argument("--date"); m.add_argument("--chunk", type=int, default=700)
    m.add_argument("--ec", choices=["L", "M", "Q"], default="M"); m.add_argument("--fps", type=int, default=3)
    m.add_argument("--seed", type=int, default=7)
    m.set_defaults(fn=cmd_make_bundle)
    c = s.add_parser("confirm-receipt"); c.add_argument("bundle"); c.add_argument("code"); c.set_defaults(fn=cmd_confirm_receipt)
    i = s.add_parser("import-result"); i.add_argument("file")
    i.add_argument("--channel", choices=["MAIL", "QR", "USB"], default="MAIL"); i.set_defaults(fn=cmd_import_result)
    ip = s.add_parser("import-photos"); ip.add_argument("file"); ip.set_defaults(fn=cmd_import_photos)
    s.add_parser("status").set_defaults(fn=cmd_status)
    s.add_parser("backup").set_defaults(fn=cmd_backup)
    rs = s.add_parser("restore"); rs.add_argument("file"); rs.add_argument("--yes", action="store_true"); rs.set_defaults(fn=cmd_restore)
    ra = s.add_parser("reset-admin"); ra.add_argument("--username", default="admin"); ra.set_defaults(fn=cmd_reset_admin)
    s.add_parser("purge-photos").set_defaults(fn=cmd_purge_photos)
    pp = s.add_parser("purge-pii"); pp.add_argument("--preview", action="store_true"); pp.set_defaults(fn=cmd_purge_pii)
    s.add_parser("backup-auto").set_defaults(fn=cmd_backup_auto)
    rsa = s.add_parser("restore-auto"); rsa.add_argument("file"); rsa.add_argument("--key", required=True); rsa.add_argument("--yes", action="store_true")
    rsa.set_defaults(fn=cmd_restore_auto)
    ia = s.add_parser("import-addr"); ia.add_argument("path"); ia.add_argument("--region", default="", help="시군구명 거르기 (예: 동구)")
    ia.add_argument("--columns", help="열 번호 직접 지정 (예: road=7,main=9,sub=10,x=16,y=17,sigungu=4)")
    ia.add_argument("--preview", action="store_true"); ia.set_defaults(fn=cmd_import_addr)
    s.add_parser("seed-sample-dongs", help="시험용 가상 행정동 넣기").set_defaults(fn=cmd_seed_dongs)
    so = s.add_parser("import-solo"); so.add_argument("file"); so.add_argument("--crew"); so.add_argument("--preview", action="store_true"); so.set_defaults(fn=cmd_import_solo)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
