"""설정 등록표(pc/dgpc/settings.py) → docs/settings.md 만들기.

실행: python3 tools/gen_settings_doc.py          (파일을 새로 씀)
      python3 tools/gen_settings_doc.py --check  (파일이 등록표와 같은지만 확인, 다르면 종료 코드 1)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

from dgpc import service  # noqa: E402,F401 — 설정 묶음에 값을 더하는 모듈(contribute)을 모두 불러 둔다
from dgpc import settings as SR  # noqa: E402

OUT = ROOT / "docs" / "settings.md"


def _val(v) -> str:
    s = json.dumps(v, ensure_ascii=False)
    s = s if len(s) <= 70 else s[:67] + "…"
    return "`" + s.replace("|", "\\|") + "`"


def render() -> str:
    lines = ["# 설정 키 목록 (PC)", "",
             "> 이 파일은 `python3 tools/gen_settings_doc.py` 가 `pc/dgpc/settings.py` 의 등록표에서 만든다. 손으로 고치지 말 것.", "",
             "- 값은 암호화 DB 의 `meta` 표에 JSON 으로 저장된다. 등록되지 않은 키를 읽거나 쓰면 오류(KeyError)가 난다.",
             "- **화면 묶음**: 표시된 키는 설정 묶음(`GET /api/bootstrap` 의 `settings`)에 같은 이름으로 들어간다.",
             "- **비밀**: 값을 화면·로그·문서에 내지 않는다(화면에는 가린 값만).",
             "- 새 설정 추가: `settings.py` 에 `register(...)` 한 줄 → 저장은 `config.save_settings`(또는 담당 모듈의 save_settings)에서 검사 후 `set_setting`.",
             ""]
    for kind, title in (("setting", "설정"), ("state", "운영 기록 (프로그램이 남김)")):
        lines += [f"## {title}", "", "| 키 | 기본값 | 묶음 | 저장 모듈 | 화면 묶음 | 설명 |", "|---|---|---|---|---|---|"]
        for k, s in SR.SETTINGS.items():
            if s.kind != kind:
                continue
            d = "(비밀)" if s.secret else _val(s.default)
            lines.append(f"| `{k}` | {d} | {s.group} | {s.owner} | {'예' if s.expose else ''} | {s.doc.replace('|', '/')} |")
        lines.append("")
    lines += ["## 설정 묶음에 계산해 더하는 값 (contribute)", "", "| 이름 | 모듈 | 함수 |", "|---|---|---|"]
    for name, fn in SR.CONTRIBUTORS:
        lines.append(f"| {name} | {fn.__module__} | `{fn.__qualname__}` |")
    return "\n".join(lines) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur != text:
            print(f"{OUT.relative_to(ROOT)} 이(가) 설정 등록표와 다릅니다. python3 tools/gen_settings_doc.py 로 다시 만드세요.")
            return 1
        print(f"✓ {OUT.relative_to(ROOT)} = 설정 등록표")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"썼습니다: {OUT.relative_to(ROOT)} ({sum(1 for _ in SR.SETTINGS)}개 키)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
