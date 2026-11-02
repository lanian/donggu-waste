#!/usr/bin/env python3
"""동구 대형폐기물 시제품 - PC 도구 (명령줄)

  python pc_tool.py init                         PC 키와 로컬 DB 생성
  python pc_tool.py reg-qr                       기기 등록용 QR 화면(HTML) 생성
  python pc_tool.py register-device DGD1.xxxx    휴대폰이 보여 준 기기 코드 등록
  python pc_tool.py make-bundle --device <앞8자> --count 20
                                                 가상 당일 목록 → 암호화 분할 QR 화면(HTML)
  python pc_tool.py confirm-receipt <묶음 앞8자> <수신 확인 코드>
  python pc_tool.py import-result <파일.dgr>      휴대폰 결과 파일 가져오기
  python pc_tool.py status                       기기·묶음·가져오기 현황

시제품 한계: PC 개인키를 state/ 폴더의 PEM 파일로 둔다. 운영에서는 운영체제 키 저장소와
SQLCipher를 사용한다. 이 도구는 네트워크를 사용하지 않는다.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import io
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import qrcode
from cryptography.hazmat.primitives import serialization
from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q

import dgproto as P
from sample_data import make_daylist

STATE = Path(os.environ.get("DG_STATE", Path(__file__).parent / "state"))
OUT = Path(os.environ.get("DG_OUT", Path(__file__).parent / "out"))
KST = dt.timezone(dt.timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS device (
  id TEXT PRIMARY KEY, kex_pub BLOB NOT NULL, sign_pub BLOB NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ACTIVE','LOST','RETIRED')),
  registered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transfer_bundle (
  id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES device(id),
  work_date TEXT NOT NULL, assign_ver INTEGER NOT NULL, chunk_count INTEGER NOT NULL,
  envelope_sha256 TEXT NOT NULL, receipt_code TEXT NOT NULL,
  displayed_at TEXT, receipt_confirmed_at TEXT);
CREATE TABLE IF NOT EXISTS result_import (
  id TEXT PRIMARY KEY, channel TEXT NOT NULL CHECK (channel IN ('MAIL','QR','USB')),
  device_id TEXT NOT NULL, package_sha256 TEXT NOT NULL UNIQUE,
  imported_at TEXT NOT NULL, summary_json TEXT);
CREATE TABLE IF NOT EXISTS result_event (
  event_id TEXT PRIMARY KEY, import_id TEXT NOT NULL REFERENCES result_import(id),
  device_id TEXT NOT NULL, device_seq INTEGER NOT NULL,
  request_id TEXT NOT NULL, assign_ver INTEGER NOT NULL, kind TEXT NOT NULL,
  body_json TEXT NOT NULL, apply_status TEXT NOT NULL,
  UNIQUE (device_id, device_seq));
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db() -> sqlite3.Connection:
    STATE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(STATE / "pc.db")
    con.executescript(SCHEMA)
    return con


def load_key(name: str):
    path = STATE / f"{name}.pem"
    if not path.exists():
        sys.exit("PC 키가 없습니다. 먼저 'python pc_tool.py init'을 실행하세요.")
    return serialization.load_pem_private_key(path.read_bytes(), None)


def save_key(name: str, key) -> None:
    path = STATE / f"{name}.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    os.chmod(path, 0o600)


def find_one(con, table: str, prefix: str) -> str:
    rows = con.execute(f"SELECT id FROM {table} WHERE id LIKE ?", (prefix + "%",)).fetchall()
    if len(rows) != 1:
        sys.exit(f"'{prefix}'에 해당하는 항목이 {len(rows)}개입니다. 더 길게 입력하세요.")
    return rows[0][0]


def qr_png_data_uri(text: str, ec: str = "M") -> tuple[str, int]:
    level = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q}[ec]
    q = qrcode.QRCode(error_correction=level, box_size=8, border=4)
    q.add_data(qrcode.util.QRData(text, mode=qrcode.util.MODE_ALPHA_NUM))
    q.make(fit=True)
    buf = io.BytesIO()
    q.make_image().save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), q.version


# ------------------------------------------------------------------ 명령
def cmd_init(_):
    STATE.mkdir(parents=True, exist_ok=True)
    if (STATE / "pc_sign.pem").exists():
        print("이미 초기화되어 있습니다.")
        return
    save_key("pc_sign", P.gen_key())
    save_key("pc_kex", P.gen_key())
    db().close()
    print("PC 키와 DB를 만들었습니다:", STATE)


def registration_text() -> str:
    sign_pub = P.pub_raw(load_key("pc_sign"))
    kex_pub = P.pub_raw(load_key("pc_kex"))
    raw = P.MAGIC + bytes([P.VERSION, 0x09]) + sign_pub + kex_pub
    return P.b45encode(raw)


def cmd_reg_qr(_):
    text = registration_text()
    uri, ver = qr_png_data_uri(text, "M")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "register.html"
    path.write_text(f"""<!doctype html><meta charset="utf-8"><title>기기 등록 QR</title>
<style>body{{font-family:sans-serif;text-align:center;margin:40px}}img{{width:min(80vmin,520px);image-rendering:pixelated}}</style>
<h1>기기 등록 QR</h1><p>휴대폰 앱 → [기기 등록] → 이 QR을 비추세요. (공개키만 들어 있습니다)</p>
<img src="{uri}" alt="등록 QR"><p>QR 버전 {ver}</p>""", encoding="utf-8")
    print("등록 화면:", path)


def cmd_register_device(a):
    dev, kex, sig = P.parse_device_code(a.code)
    dev_id = str(uuid.UUID(bytes=dev))
    con = db()
    con.execute("INSERT OR REPLACE INTO device VALUES (?,?,?, 'ACTIVE', ?)", (dev_id, kex, sig, now()))
    con.commit()
    pc_sign_pub = P.pub_raw(load_key("pc_sign"))
    print("등록 완료:", dev_id)
    print("지문:", P.fingerprint(pc_sign_pub, kex, sig), "← 휴대폰 화면의 숫자와 같은지 확인하세요")


def cmd_make_bundle(a):
    con = db()
    dev_id = find_one(con, "device", a.device)
    kex_raw, status = con.execute("SELECT kex_pub, status FROM device WHERE id=?", (dev_id,)).fetchone()
    if status != "ACTIVE":
        sys.exit(f"기기 상태가 {status}라서 목록을 만들 수 없습니다.")
    work_date = a.date or dt.datetime.now(KST).date().isoformat()
    prev = con.execute("SELECT MAX(assign_ver) FROM transfer_bundle WHERE device_id=? AND work_date=?",
                       (dev_id, work_date)).fetchone()[0] or 0
    assign_ver = prev + 1
    payload = make_daylist(a.count, work_date, assign_ver, seed=a.seed)
    bundle_id = uuid.uuid4().bytes
    head = P.Header(P.TYPE_DAYLIST, bundle_id, uuid.UUID(dev_id).bytes, assign_ver, 1,
                    P.key_id(P.pub_raw(load_key("pc_sign"))))
    env, code = P.seal(payload, head, P.pub_from_raw(kex_raw), load_key("pc_sign"))
    frames = P.split_frames(env, bundle_id, a.chunk)
    bid = str(uuid.UUID(bytes=bundle_id))
    import hashlib
    con.execute("INSERT INTO transfer_bundle VALUES (?,?,?,?,?,?,?,?,NULL)",
                (bid, dev_id, work_date, assign_ver, len(frames),
                 hashlib.sha256(env).hexdigest(), code, now()))
    con.commit()
    imgs, vers = [], set()
    for f in frames:
        uri, v = qr_png_data_uri(f, a.ec)
        imgs.append(uri)
        vers.add(v)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"bundle_{bid[:8]}.html"
    path.write_text(display_html(imgs, bid[:8], work_date, assign_ver, a.count, a.fps), encoding="utf-8")
    (OUT / f"bundle_{bid[:8]}.frames.json").write_text(json.dumps(frames))
    print(f"묶음 {bid[:8]}: 접수 {a.count}건, 암호문 {len(env):,}B, QR {len(frames)}장 "
          f"(조각 {a.chunk}B, 오류복원 {a.ec}, QR 버전 {min(vers)}~{max(vers)})")
    print("표시 화면:", path)
    print("휴대폰 수신 후 표시되는 확인 코드를 'confirm-receipt'로 입력하세요.")


def display_html(imgs, short, work_date, ver, count, fps) -> str:
    data = json.dumps(imgs)
    return f"""<!doctype html><html lang="ko"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<title>목록 전송 {short}</title>
<style>
body{{margin:0;font-family:system-ui,sans-serif;background:#fff;color:#111;display:flex;flex-direction:column;align-items:center}}
header{{padding:12px;text-align:center}} img{{width:min(78vmin,640px);image-rendering:pixelated;margin:8px}}
.n{{font-size:42px;font-weight:700}} .ctl button{{font-size:18px;padding:8px 16px;margin:4px}}
</style>
<header><div>목록 {html.escape(short)} · {html.escape(work_date)} · 갱신본 {ver} · {count}건</div>
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


def cmd_confirm_receipt(a):
    con = db()
    bid = find_one(con, "transfer_bundle", a.bundle)
    expect = con.execute("SELECT receipt_code FROM transfer_bundle WHERE id=?", (bid,)).fetchone()[0]
    if a.code.strip().upper() != expect:
        sys.exit("확인 코드가 맞지 않습니다. 수신이 확인되지 않았습니다.")
    con.execute("UPDATE transfer_bundle SET receipt_confirmed_at=? WHERE id=?", (now(), bid))
    con.commit()
    print(f"묶음 {bid[:8]} 모바일 수신 확인 완료")


def cmd_import_result(a):
    import hashlib
    data = Path(a.file).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    con = db()
    if con.execute("SELECT 1 FROM result_import WHERE package_sha256=?", (sha,)).fetchone():
        print("이미 가져온 파일입니다. 변경 없음.")
        return
    head = P.Header.unpack(data)
    dev_id = str(uuid.UUID(bytes=head.device_id))
    row = con.execute("SELECT sign_pub, status FROM device WHERE id=?", (dev_id,)).fetchone()
    if not row:
        sys.exit("등록되지 않은 기기의 파일입니다. 거부했습니다.")
    if row[1] != "ACTIVE":
        sys.exit(f"기기 상태가 {row[1]}입니다. 충돌 검토가 필요해 반영하지 않았습니다.")
    try:
        head, payload, release = P.open_envelope(data, P.pub_from_raw(row[0]), load_key("pc_kex"))
    except P.ProtocolError as e:
        sys.exit(f"결과 파일 검증 실패({e.code}). 아무것도 반영하지 않았습니다.")
    if head.btype != P.TYPE_RESULT or payload.get("schema") != "dg.result/1":
        sys.exit("결과 파일 형식이 아닙니다.")
    imp_id = str(uuid.uuid4())
    counts = {"applied": 0, "skipped_duplicate": 0, "conflict": 0}
    with con:  # 한 트랜잭션
        con.execute("INSERT INTO result_import VALUES (?,?,?,?,?,NULL)", (imp_id, a.channel, dev_id, sha, now()))
        latest_ver = {}
        for (wd, av) in con.execute("SELECT work_date, MAX(assign_ver) FROM transfer_bundle WHERE device_id=? GROUP BY work_date", (dev_id,)):
            latest_ver[wd] = av
        cur_ver = latest_ver.get(payload.get("work_date"))
        for ev in payload["events"]:
            if con.execute("SELECT 1 FROM result_event WHERE event_id=?", (ev["event_id"],)).fetchone():
                counts["skipped_duplicate"] += 1
                continue
            status = "APPLIED" if cur_ver is None or ev["assign_ver"] == cur_ver else "CONFLICT"
            counts["applied" if status == "APPLIED" else "conflict"] += 1
            con.execute("INSERT INTO result_event VALUES (?,?,?,?,?,?,?,?,?)",
                        (ev["event_id"], imp_id, dev_id, ev["seq"], ev["request_id"], ev["assign_ver"],
                         ev["kind"], json.dumps(ev.get("body", {}), ensure_ascii=False), status))
        con.execute("UPDATE result_import SET summary_json=? WHERE id=?", (json.dumps(counts), imp_id))
    print("가져오기 결과:", json.dumps(counts, ensure_ascii=False))
    print("사진 해시 목록:", len(payload.get("photos", [])), "건 (사진 파일은 보안 USB로 별도 회수)")
    print("보관 종료 확인 코드:", release, "← 휴대폰 [결과 보내기] 화면에 입력하면 반영됨으로 표시됩니다")


def cmd_status(_):
    con = db()
    print("[기기]")
    for r in con.execute("SELECT substr(id,1,8), status, registered_at FROM device"):
        print("  ", *r)
    print("[목록 묶음]")
    for r in con.execute("SELECT substr(id,1,8), substr(device_id,1,8), work_date, assign_ver, chunk_count, "
                         "CASE WHEN receipt_confirmed_at IS NULL THEN '수신 미확인' ELSE '수신 확인' END FROM transfer_bundle"):
        print("  ", *r)
    print("[결과 가져오기]")
    for r in con.execute("SELECT substr(id,1,8), channel, imported_at, summary_json FROM result_import"):
        print("  ", *r)


def main(argv=None):
    p = argparse.ArgumentParser(description="동구 대형폐기물 시제품 PC 도구")
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("init").set_defaults(fn=cmd_init)
    s.add_parser("reg-qr").set_defaults(fn=cmd_reg_qr)
    r = s.add_parser("register-device"); r.add_argument("code"); r.set_defaults(fn=cmd_register_device)
    m = s.add_parser("make-bundle")
    m.add_argument("--device", required=True); m.add_argument("--count", type=int, default=20)
    m.add_argument("--date"); m.add_argument("--chunk", type=int, default=700)
    m.add_argument("--ec", choices=["L", "M", "Q"], default="M"); m.add_argument("--fps", type=int, default=3)
    m.add_argument("--seed", type=int, default=7)
    m.set_defaults(fn=cmd_make_bundle)
    c = s.add_parser("confirm-receipt"); c.add_argument("bundle"); c.add_argument("code"); c.set_defaults(fn=cmd_confirm_receipt)
    i = s.add_parser("import-result"); i.add_argument("file")
    i.add_argument("--channel", choices=["MAIL", "QR", "USB"], default="MAIL"); i.set_defaults(fn=cmd_import_result)
    s.add_parser("status").set_defaults(fn=cmd_status)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
