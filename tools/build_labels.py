#!/usr/bin/env python3
"""data/labels.json → web/labels-data.js (PC 화면·휴대폰이 함께 쓰는 표시 이름: 접수 상태·연락·미수거 사유·스티커 확인).
표시 이름을 바꾸면 이 스크립트를 다시 실행한다. 파이썬 쪽은 pc/dgpc/core.py 의 LABELS 가 같은 파일을 읽는다.

실행: python3 tools/build_labels.py          (파일을 새로 씀)
      python3 tools/build_labels.py --check  (파일이 원본과 같은지만 확인, 다르면 종료 코드 1)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "labels.json"
OUT = ROOT / "web" / "labels-data.js"


def render() -> str:
    d = json.loads(SRC.read_text(encoding="utf8"))
    d.pop("about", None)
    return ("// 자동 생성 파일: tools/build_labels.py 로 data/labels.json 에서 만든다. 직접 고치지 않는다.\n"
            "export const LABELS = " + json.dumps(d, ensure_ascii=False) + ";\n")


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf8") != text:
            print("web/labels-data.js 가 data/labels.json 과 다릅니다. python3 tools/build_labels.py 로 다시 만드세요.")
            return 1
        print("✓ web/labels-data.js = data/labels.json")
        return 0
    OUT.write_text(text, encoding="utf8")
    print("web/labels-data.js:", ", ".join(k for k in json.loads(SRC.read_text(encoding="utf8")) if k != "about"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
