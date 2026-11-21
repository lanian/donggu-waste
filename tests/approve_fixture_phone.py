"""임시 E2E 작업 폴더에서 시험 관리자 인증 앱을 연결한 뒤 실제 CLI로 가상 수거폰을 승인한다."""
import base64
from contextlib import closing
import getpass
import os
from pathlib import Path
import secrets
import sys
import time
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc"))
import pc_tool
from device_pairing import totp_number
from dgpc import device_approval, users


def approve_fixture_phone(work, code):
    work = Path(work).resolve()
    if (not os.environ.get("DG_DB_KEY") or Path(os.environ.get("DG_STATE", "")).resolve() != work / "state"
            or Path(os.environ.get("DG_OUT", "")).resolve() != work / "out"):
        raise RuntimeError("가상 시험용 state/out 작업 폴더가 필요합니다.")
    username = "e2e_approval_admin"
    password, owner = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    now = int(time.time())
    with closing(pc_tool.con()) as con:
        if users.users_exist(con):
            raise RuntimeError("새로 생성한 시험 DB에서만 시험 관리자를 만듭니다.")
        uid = users.create_user(con, username, "가상 승인 담당자", "ADMIN", password)
        with patch.object(device_approval, "time", return_value=now - 30):
            uri = device_approval.start_setup(con, uid, owner, password)["uri"]
            key = base64.b32decode(parse_qs(urlsplit(uri).query)["secret"][0])
            device_approval.finish_setup(con, uid, owner, totp_number(key, now // 30 - 1))
    with (patch.object(device_approval, "time", return_value=now),
          patch.object(getpass, "getpass", side_effect=[password, totp_number(key, now // 30)])):
        pc_tool.main(["register-device", code, "--admin", username, "--confirm"])


if __name__ == "__main__":
    approve_fixture_phone(*sys.argv[1:])
