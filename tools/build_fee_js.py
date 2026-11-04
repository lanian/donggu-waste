#!/usr/bin/env python3
"""data/fee_table.json + data/item_handling.json → web/fees-data.js (휴대폰 앱 내장 수수료표·품목별 적재 기준).
수수료표나 적재 기준을 바꾸면 이 스크립트를 다시 실행한다.

실행: python3 tools/build_fee_js.py          (파일을 새로 씀)
      python3 tools/build_fee_js.py --check  (파일이 원본과 같은지만 확인, 다르면 종료 코드 1)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
OUT = root / "web/fees-data.js"


def render() -> str:
    t = json.loads((root / "data/fee_table.json").read_text(encoding="utf8"))
    hd = json.loads((root / "data/item_handling.json").read_text(encoding="utf8"))
    return ("// 자동 생성 파일: tools/build_fee_js.py 로 data/fee_table.json 에서 만든다. 직접 고치지 않는다.\n"
            f"// 원문: {t['label']} ({t['source_file']}, sha256 {t['source_sha256'][:12]}…), 자료 확인일 {t['source_checked_on']}, 시행일 미확인\n"
            "export const BUILTIN_FEES = " + json.dumps(t, ensure_ascii=False) + ";\n"
            "// 품목별 적재 기준(추정치): data/item_handling.json. PC 노선 계산(pc/dgpc/routing.py)과 같은 표.\n"
            "export const ITEM_HANDLING = " + json.dumps(hd, ensure_ascii=False) + ";\n")


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if OUT.read_text(encoding="utf8") != text:
            print("web/fees-data.js 가 data/fee_table.json·item_handling.json 과 다릅니다. python3 tools/build_fee_js.py 로 다시 만드세요.")
            return 1
        print("✓ web/fees-data.js = data/fee_table.json + item_handling.json")
        return 0
    OUT.write_text(text, encoding="utf8")
    t = json.loads((root / "data/fee_table.json").read_text(encoding="utf8"))
    hd = json.loads((root / "data/item_handling.json").read_text(encoding="utf8"))
    print("web/fees-data.js:", len(t["rows"]), "행, 적재 기준", len(hd["rules"]), "규칙")
    return 0


if __name__ == "__main__":
    sys.exit(main())
