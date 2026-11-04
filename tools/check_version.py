#!/usr/bin/env python3
"""휴대폰 앱 버전 일치 검사 (CI 배포 전 단계).

확인하는 것
  1. web/app.js 의 APP_VERSION 과 web/sw.js 의 VERSION 이 같은지
     (다르면 휴대폰의 [업데이트] 안내·캐시 이름이 어긋나 옛 앱이 남는다)
  2. 태그로 실행할 때(예: v0.9.1) 태그 버전이 위 버전과 같은지
  3. web/sw.js 가 미리 저장(precache)하는 파일이 모두 web/ 에 있는지
     (하나라도 없으면 서비스 워커 설치가 실패해 오프라인 실행이 안 된다)

실행
  python tools/check_version.py               # 1·3
  python tools/check_version.py --tag v0.9.1  # 1·2·3
  CI(GitHub Actions)에서 태그 push 이면 GITHUB_REF_TYPE=tag, GITHUB_REF_NAME 을 읽어 --tag 없이도 2를 검사한다.
끝 코드: 0 = 통과, 1 = 불일치, 2 = 파일에서 버전을 찾지 못함
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def find(path: Path, pattern: str) -> str:
    m = re.search(pattern, path.read_text(encoding="utf8"), re.M)
    if not m:
        print(f"::error file={path.relative_to(ROOT)}::버전 선언을 찾지 못했습니다 (형식: {pattern})")
        sys.exit(2)
    return m.group(1)


def precache_files(sw: Path) -> list[str]:
    m = re.search(r"const\s+FILES\s*=\s*\[(.*?)\];", sw.read_text(encoding="utf8"), re.S)
    return re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)) if m else []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", help="배포 태그 (예: v0.9.1). 생략하면 GitHub Actions 태그 실행일 때 자동으로 읽음")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    root = a.root.resolve()
    web = root / "web"

    app_v = find(web / "app.js", r"^export\s+const\s+APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]")
    sw_v = find(web / "sw.js", r"^const\s+VERSION\s*=\s*['\"]([^'\"]+)['\"]")
    tag = a.tag
    if tag is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        tag = os.environ.get("GITHUB_REF_NAME")

    errors: list[str] = []
    print(f"web/app.js APP_VERSION = {app_v}")
    print(f"web/sw.js  VERSION     = {sw_v}")
    for name, v in (("APP_VERSION", app_v), ("sw.js VERSION", sw_v)):
        if not SEMVER.match(v):
            errors.append(f"{name} '{v}' 가 x.y.z 형식이 아닙니다")
    pc_init = (ROOT / "pc" / "dgpc" / "__init__.py").read_text(encoding="utf8") if (ROOT / "pc" / "dgpc" / "__init__.py").exists() else ""
    m_pc = re.search(r'__version__\s*=\s*"([^"]+)"', pc_init)
    pc_v = m_pc.group(1) if m_pc else ""
    print(f"pc/dgpc  __version__ = {pc_v}")
    if pc_v and pc_v != app_v:
        errors.append(f"PC __version__({pc_v}) 과 APP_VERSION({app_v}) 이 다릅니다 — 같은 번호로 맞추세요")
    if app_v != sw_v:
        errors.append(f"APP_VERSION({app_v}) 과 sw.js VERSION({sw_v}) 이 다릅니다 — 두 파일을 같은 버전으로 맞추세요")
    if tag:
        print(f"태그                   = {tag}")
        tv = tag[1:] if tag.startswith("v") else None
        if tv is None:
            errors.append(f"배포 태그 '{tag}' 는 v 로 시작해야 합니다 (예: v{app_v})")
        elif tv != app_v or tv != sw_v:
            errors.append(f"태그 {tag} 와 앱 버전(APP_VERSION {app_v}, sw.js {sw_v})이 다릅니다 — 버전을 올려 커밋한 뒤 그 커밋에 태그를 다세요")

    missing = [f for f in precache_files(web / "sw.js") if not (web / f.removeprefix("./")).exists() and f != "./"]
    if missing:
        errors.append("sw.js 미리 저장 목록에 있지만 web/ 에 없는 파일: " + ", ".join(missing))

    # 거꾸로: 앱이 불러오는 파일(import·script src·link href·manifest·아이콘)이 미리 저장 목록에 다 있는가 — 빠지면 오프라인에서 멈춤
    import re as _re
    pre = {f.removeprefix("./") for f in precache_files(web / "sw.js")}
    used = set()
    # 화면 모듈(web/screens/*.js)까지: 상대 경로는 그 파일 기준으로 풀어 web/ 기준 경로로 비교한다
    for src in [web / "index.html", *web.glob("*.js"), *web.glob("screens/*.js")]:
        if src.name == "sw.js":
            continue
        t = src.read_text(encoding="utf8")
        for m in _re.finditer(r"""(?:import\s[^'"]*?from\s*|import\(\s*|src=|href=)['"](\./)?([\w./-]+\.(?:m?js|css|webmanifest|svg|png))['"]""", t):
            target = (src.parent / m.group(2)).resolve()
            try:
                used.add(target.relative_to(web.resolve()).as_posix())
            except ValueError:
                used.add(m.group(2))
    lack = sorted(u for u in used if u not in pre and not u.startswith(("http", "/")) and (web / u).exists())
    if lack:
        errors.append("앱이 쓰지만 sw.js 미리 저장 목록에 없는 파일(오프라인에서 멈춤): " + ", ".join(lack))

    for e in errors:
        print("::error::" + e)
    if errors:
        return 1
    print("통과: 앱 버전 일치" + (f", 태그 {tag} 일치" if tag else "") + ", 미리 저장 파일 모두 있음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
