"""PC에서 개인 휴대폰으로 건네는 배차 조회용 QR. 발급마다 새 열쇠를 메모리에만 둔다."""
import base64
import json
import os
import uuid
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import dgproto
from dgpc import assign, privacy
from dgpc.core import UserError, audit, require_current_crew, today_kst, tx


CHUNK_SIZE = 700
MAX_FRAMES = 256
MAX_PLAINTEXT = 1024 * 1024


def _visits(daylist):
    visits = []
    for stop in daylist["stops"]:
        items = []
        for item in stop["items"]:
            qty = item["qty"] - item["prior_collected"]
            if qty > 0:
                items.append({"name": item["name"], "spec": item["spec"] or "", "qty": qty,
                              "unit": item["unit_label"] or "개"})
        if items:
            visits.append({"seq": stop["seq"], "dong": stop["dong"], "address": stop["address"] or "", "items": items})
    return visits


def issue(con, batch_id: str):
    if not isinstance(batch_id, str) or not 1 <= len(batch_id) <= 36:
        raise UserError("배차 목록을 선택해 주세요.")
    with tx(con):
        daylist, batch = assign.build_daylist(con, batch_id)
        require_current_crew(con, batch["crew_id"])
        if daylist["work_date"] < today_kst():
            raise UserError("지난 날짜의 배차 목록입니다. 오늘 이후의 배차를 선택해 주세요.")
        info = {"work_date": daylist["work_date"], "crew": daylist["crew"], "assign_ver": daylist["assign_ver"]}
        visits = _visits(daylist)
        detail = {"crew_id": batch["crew_id"], "work_date": info["work_date"], "count": len(visits)}
        audit(con, "guest_list", batch_id, "ISSUE", detail)
        # 같은 배정 차수 안에서도 주소와 잔여 수량이 바뀐다. 이번 발급 이력의 순번으로 이전 QR을 구분한다.
        issue_no = con.execute(
            "SELECT id FROM audit_log WHERE entity='guest_list' AND entity_id=? AND action='ISSUE' ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()[0]
        projection = {"schema": "dg.guest-list/1", **info, "crew_id": batch["crew_id"], "issue_no": issue_no, "stops": visits}
        plain = json.dumps(projection, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        size_error = "조회용 QR로 보내기에는 목록이 너무 큽니다. 배차표 인쇄를 이용해 주세요."
        if len(plain) > MAX_PLAINTEXT:
            raise UserError(size_error)
        bundle_id = uuid.uuid4()
        context = f"DGVIEW1:{bundle_id.hex}"
        key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        envelope = nonce + AESGCM(key).encrypt(nonce, zlib.compress(plain), context.encode("ascii"))
        if len(envelope) > CHUNK_SIZE * MAX_FRAMES:
            raise UserError(size_error)
        frames = ["DGV1." + frame for frame in dgproto.split_frames(envelope, bundle_id.bytes, CHUNK_SIZE)]
        privacy.record_access(con, "EXPORT", batch_id, len(visits), "등록 없이 배차 목록 조회용 QR 발급")
    encoded_key = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    return {**info, "key_qr": context + ":" + encoded_key, "frames": frames, "count": len(visits)}
