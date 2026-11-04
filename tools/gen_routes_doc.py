"""PC 서버 경로 표(pc/dgpc/server.py 의 ROUTES) → docs/routes.md (역할 × 경로 표) 만들기.

실행: python3 tools/gen_routes_doc.py          (파일을 새로 씀)
      python3 tools/gen_routes_doc.py --check  (파일이 경로 표와 같은지만 확인, 다르면 종료 코드 1)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))
os.environ.setdefault("DG_STATE", tempfile.mkdtemp(prefix="dg-doc-"))  # 서버 모듈만 불러온다(DB 열지 않음)

from dgpc import server as SV  # noqa: E402

OUT = ROOT / "docs" / "routes.md"
ROLES = (("ADMIN", "관리자"), ("INTAKE", "접수"), ("FIELD", "수거팀"), ("LEAD", "팀장"))
PUBLIC = (("GET", "state", "설정 필요 여부·로그인 여부"), ("POST", "setup", "첫 관리자 만들기(사용자가 없을 때만)"),
          ("POST", "login", "로그인(아이디별·PC별 실패 잠금)"), ("POST", "logout", "로그아웃(세션만 있으면)"))


def render() -> str:
    lines = ["# PC 서버 API 경로 (역할 × 경로)", "",
             "> 이 파일은 `python3 tools/gen_routes_doc.py` 가 `pc/dgpc/server.py` 의 경로 표(`@endpoint`)에서 만든다. 손으로 고치지 말 것.", "",
             "- 주소 앞에 `/api/` 가 붙는다. 모든 API 요청에는 `X-DG-CSRF: 1` 머리글이 있어야 한다.",
             "- **잠금**: " + " · ".join(f"`{k}` = {v}" for k, v in SV.LOCKS.items()) + ".",
             "- **임시 비번**: ✓ 인 경로만 임시 비밀번호 상태(비밀번호 변경 전)에서 쓸 수 있다.",
             "- **접속기록**: 경로에서 남기는 접속기록 동작(건수·메모만). 내보내기·인쇄·배차표·주소 검색은 업무 함수 안에서 남긴다.",
             "- 경로 표에 없는 주소: 관리자, 또는 같은 방법·같은 첫 마디의 경로를 쓸 수 있는 역할은 404, 그 밖은 403.",
             "- 새 경로 추가: `server.py` 의 `Handler` 안에 `@endpoint(방법, 경로, 역할 묶음, lock=…, access=…)` 처리 함수 하나 → 이 문서를 다시 만든다.",
             "  역할 묶음: " + ", ".join(f"`{n}`={'·'.join(sorted(getattr(SV, n)))}" for n in ("ALL", "VIEW", "WORK", "CREW", "REVIEW", "ADMIN")) + ".",
             "", "## 로그인 전", "", "| 방법 | 경로 | 설명 |", "|---|---|---|"]
    lines += [f"| {m} | `{p}` | {d} |" for m, p, d in PUBLIC]
    for method in ("GET", "POST"):
        lines += ["", f"## {method}", "", "| 경로 | " + " | ".join(lab for _, lab in ROLES) + " | 잠금 | 임시 비번 | 접속기록 | 설명 |",
                  "|---|" + "---|" * (len(ROLES) + 4)]
        for r in SV.ROUTES:
            if r.method != method:
                continue
            doc = (r.fn.__doc__ or "").strip().splitlines()[0] if r.fn.__doc__ else ""
            lines.append(f"| `{r.pattern}` | " + " | ".join("✓" if code in r.roles else "" for code, _ in ROLES)
                         + f" | {r.lock} | {'✓' if r.must_change_ok else ''} | {r.access} | {doc.replace('|', '/')} |")
    lines += ["", f"경로 {len(SV.ROUTES)}개 (로그인 전 {len(PUBLIC)}개 제외)."]
    return "\n".join(lines) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur != text:
            print(f"{OUT.relative_to(ROOT)} 이(가) 서버 경로 표와 다릅니다. python3 tools/gen_routes_doc.py 로 다시 만드세요.")
            return 1
        print(f"✓ {OUT.relative_to(ROOT)} = 서버 경로 표")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"썼습니다: {OUT.relative_to(ROOT)} (경로 {len(SV.ROUTES)}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
