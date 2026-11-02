#!/usr/bin/env python3
"""data/fee_table.json → web/fees-data.js (휴대폰 앱 내장 수수료표). 수수료표를 바꾸면 이 스크립트를 다시 실행한다."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
t = json.loads((root / "data/fee_table.json").read_text(encoding="utf8"))
(root / "web/fees-data.js").write_text(
    "// 자동 생성 파일: tools/build_fee_js.py 로 data/fee_table.json 에서 만든다. 직접 고치지 않는다.\n"
    f"// 원문: {t['label']} ({t['source_file']}, sha256 {t['source_sha256'][:12]}…), 자료 확인일 {t['source_checked_on']}, 시행일 미확인\n"
    "export const BUILTIN_FEES = " + json.dumps(t, ensure_ascii=False) + ";\n", encoding="utf8")
print("web/fees-data.js:", len(t["rows"]), "행")
