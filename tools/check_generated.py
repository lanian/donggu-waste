#!/usr/bin/env python3
"""만든 파일(생성물)이 원본과 같은지 한 번에 확인 (CI용).

  web/fees-data.js    ← data/fee_table.json + data/item_handling.json   (tools/build_fee_js.py)
  web/labels-data.js  ← data/labels.json                                (tools/build_labels.py)
  docs/settings.md    ← pc/dgpc/settings.py 등록표                       (tools/gen_settings_doc.py)
  docs/routes.md      ← pc/dgpc/server.py 경로 표                        (tools/gen_routes_doc.py)

실행: python3 tools/check_generated.py   (다르면 종료 코드 1 — 해당 도구를 다시 실행해 커밋하세요)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("build_fee_js.py", "build_labels.py", "gen_settings_doc.py", "gen_routes_doc.py")


def main() -> int:
    bad = 0
    for t in TOOLS:
        r = subprocess.run([sys.executable, str(ROOT / "tools" / t), "--check"], cwd=ROOT, capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        if r.returncode:
            print(f"::error::{t} --check 실패")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
