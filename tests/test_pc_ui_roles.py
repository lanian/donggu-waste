"""PC 화면의 역할별 기능 표(pc/ui/core.js 의 CAN)가 서버 경로 권한(server.py ROUTES)과 같은지 대조한다.

화면은 쓸 수 없는 단추·카드를 숨기는 데 CAN 을 쓰고 서버가 다시 막는다. 둘이 어긋나면 단추는 보이는데 403 이 나거나
쓸 수 있는 기능이 숨는다. CAN 에 항목을 더하면 아래 CAN_ROUTES 에 대응 경로를 적는다(없으면 실패).
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
os.environ.setdefault("DG_STATE", tempfile.mkdtemp(prefix="dg-ui-roles-"))
os.environ.setdefault("DG_DB_KEY", "test-db-key-not-for-production")

from dgpc import server as SV  # noqa: E402

# 화면 기능 → 그 기능이 부르는 서버 경로(모두 같은 역할이어야 한다)
CAN_ROUTES = {
    "work": [("POST", "requests"), ("POST", "requests/{id}"), ("POST", "requests/bulk")],
    "calendar": [("GET", "calendar")],
    "dongDetect": [("GET", "dong-detect")],
    "unloadWrite": [("POST", "unloads")],
    "feeReview": [("POST", "fee-revisions/{id}/approve"), ("POST", "fee-revisions/{id}/reject")],
    "soloImport": [("POST", "import/solo")],
}
UI_ROLES = {"ADMIN", "INTAKE", "FIELD", "LEAD"}  # 업무 화면을 쓰는 역할(지정 동 DONG 은 동별 화면만 쓴다)

passed = 0


def ok(cond, msg):
    global passed
    if not cond:
        raise SystemExit(f"  ✗ {msg}")
    passed += 1
    print(f"  ✓ {msg}")


src = (ROOT / "pc" / "ui" / "core.js").read_text(encoding="utf-8")
block = re.search(r"const CAN = \{(.*?)\n\};", src, re.S)
ok(block is not None, "core.js 에 CAN 표가 있음")
can = {k: set(v.split()) for k, v in re.findall(r"^\s*(\w+):\s*'([A-Z ]+)'", block.group(1), re.M)}
ok(can and set(can) == set(CAN_ROUTES), f"CAN 항목과 대조 표가 같음 (화면 {sorted(can)} / 시험 {sorted(CAN_ROUTES)})")

routes = {(r.method, r.pattern): set(r.roles) for r in SV.ROUTES}
for action, targets in CAN_ROUTES.items():
    for target in targets:
        ok(target in routes, f"{action}: 서버 경로 {target[0]} {target[1]} 있음")
        ok(can[action] == routes[target] & UI_ROLES,
           f"{action}: 화면 {sorted(can[action])} = 서버 {target[0]} {target[1]} {sorted(routes[target] & UI_ROLES)}")

print(f"\n통과 {passed}개")
