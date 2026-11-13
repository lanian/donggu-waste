"""Windows GUI 배포판 생성: python tools/build_desktop.py

사전 설치: python -m pip install --require-hashes -r tools/requirements-desktop.txt
실행 의존성은 pc/requirements.txt를 사용한다. 빌드 결과만 build/desktop, dist/desktop에 쓴다.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "desktop"
DIST = ROOT / "dist" / "desktop"
DATA_JSON = ("fee_table.json", "labels.json", "item_handling.json", "dong_centers.json")
PUBLIC_ICONS = ("icon.svg", "icon-180.png", "icon-192.png", "icon-512.png")
PRIVATE_DIRS = {"state", "out", "logs", "backups", "pre-migration", "tests", "shots", ".git", ".venv", "__pycache__"}
PRIVATE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".dgr", ".dgp", ".dgb", ".dgbak", ".dgabk",
                    ".pem", ".key", ".pfx", ".p12", ".y4m", ".log"}


def allowed_web_files() -> set[str]:
    """서버 코드를 실행하지 않고 현재 정적 파일 허용 목록만 읽는다."""
    tree = ast.parse((ROOT / "pc/dgpc/server.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "WEB_ALLOWED" for t in node.targets):
            names = ast.literal_eval(node.value)
            if isinstance(names, set) and all(isinstance(name, str) for name in names):
                return names
    raise ValueError("서버의 WEB_ALLOWED 정적 자산 목록을 확인하지 못했습니다.")


def asset_files() -> list[Path]:
    """업무 자료가 있는 저장소 전체가 아니라 실행에 필요한 공개 자산만 선택한다."""
    files = [path for path in (ROOT / "pc/ui").rglob("*") if path.is_file()
             and (path.suffix in {".js", ".css", ".html", ".woff2"} or path.name == "LICENSE-PretendardGOV.txt")]
    files += [ROOT / "web" / name for name in sorted(allowed_web_files())]
    files += [ROOT / "web/icons" / name for name in PUBLIC_ICONS]
    files += [ROOT / "data" / name for name in DATA_JSON]
    files += [ROOT / "data/report_templates" / name for name in ("monthly.xlsx", "ledger.odt")]
    for path in files:
        if not path.resolve().is_relative_to(ROOT) or not path.is_file():
            raise ValueError("배포 자산이 없거나 저장소 바깥을 가리킵니다: " + str(path))
    # 공유 지도 라이브러리의 배포 고지도 함께 보관한다.
    files += [ROOT / "web/vendor/maplibre/LICENSE.txt"]
    return sorted(set(files))


def asset_datas() -> list[tuple[str, str]]:
    return [(str(path), path.relative_to(ROOT).parent.as_posix()) for path in asset_files()]


def runtime_distributions() -> list[str]:
    from packaging.requirements import Requirement
    names = []
    for line in (ROOT / "pc/requirements.txt").read_text(encoding="utf-8").splitlines():
        if re.match(r"^[A-Za-z0-9_.-]+==", line):
            requirement = Requirement(line.rstrip(" \\"))
            if not requirement.marker or requirement.marker.evaluate():
                names.append(requirement.name)
    return sorted(set(names))


def assert_public_files(directory: Path) -> list[Path]:
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(directory)
        lower = path.name.lower()
        if (any(part.lower() in PRIVATE_DIRS for part in relative.parts)
                or path.suffix.lower() in PRIVATE_SUFFIXES or ".db-" in lower
                or lower.startswith((".env", "pc.db.")) or lower == "derive_vectors.json"):
            raise ValueError("배포 폴더에 업무 자료·키·시험 파일이 포함되어 ZIP을 만들지 않습니다: " + relative.as_posix())
    return files


def zip_distribution(directory: Path, destination: Path) -> None:
    files = assert_public_files(directory)  # 파일을 검사한 뒤에만 ZIP을 연다.
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, (Path(directory.name) / path.relative_to(directory)).as_posix())


def write_release_info(directory: Path) -> None:
    distributions = {}
    licenses = ["동구 대형폐기물 PC — 포함된 외부 구성 요소", ""]
    for name in runtime_distributions():
        distribution = metadata.distribution(name)
        distributions[name] = distribution.version
        license_name = distribution.metadata.get("License-Expression") or distribution.metadata.get("License") or "포함된 dist-info 고지 참조"
        licenses.append(f"{name} {distribution.version}\n{license_name}\n")
    (directory / "THIRD-PARTY.txt").write_text("\n".join(licenses), encoding="utf-8")
    (directory / "사용안내.txt").write_text(
        "Donggu-PC.exe를 실행하세요. Python이나 터미널 명령은 필요하지 않습니다.\n"
        "_internal 폴더를 포함한 압축 해제 폴더 전체를 함께 보관하세요.\n"
        "업무 자료 위치는 실행 앱에 표시됩니다. 기존 자료는 자료 폴더 선택으로 연결하세요.\n"
        "새 버전을 받을 때 프로그램 폴더만 교체하고 업무 자료 폴더는 삭제하지 마세요.\n",
        encoding="utf-8-sig")
    manifest = {"python": sys.version.split()[0], "pyinstaller": metadata.version("pyinstaller"),
                "runtime_distributions": distributions, "files": {}}
    for path in assert_public_files(directory):
        manifest["files"][path.relative_to(directory).as_posix()] = {
            "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_dist_dir(directory: Path | None = None) -> Path:
    """상대 경로는 저장소 기준이며 실제 출력은 dist/desktop 내부로 제한한다."""
    selected = Path(directory) if directory is not None else DIST
    if not selected.is_absolute():
        selected = ROOT / selected
    selected = selected.resolve()
    boundary = ROOT.resolve() / "dist" / "desktop"
    if not selected.is_relative_to(boundary):
        raise ValueError("배포 출력 경로는 저장소의 dist/desktop 안이어야 합니다.")
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, help="dist/desktop 내부의 배포 출력 폴더 (상대 경로는 저장소 기준)")
    args = parser.parse_args()
    distribution = resolve_dist_dir(args.dist_dir)
    app = distribution / "Donggu-PC"
    if not app.resolve().is_relative_to(distribution):
        raise ValueError("기존 앱 폴더가 선택한 배포 출력 바깥을 가리킵니다.")
    if app.exists():
        assert_public_files(app)  # --noconfirm이 기존 폴더를 정리하기 전에 업무 자료를 보호한다.
    if sys.platform != "win32":
        raise SystemExit("Windows 배포판은 Windows에서 빌드하세요.")
    try:
        version = metadata.version("pyinstaller")
    except metadata.PackageNotFoundError:
        raise SystemExit("먼저 고정 빌드 도구를 설치하세요: python -m pip install --require-hashes -r tools/requirements-desktop.txt")
    if version != "6.22.3":
        raise SystemExit("고정된 PyInstaller 6.22.3이 필요합니다. tools/requirements-desktop.txt를 사용하세요.")
    if not (ROOT / "pc/desktop.py").is_file():
        raise SystemExit("pc/desktop.py 실행 앱 진입점이 없습니다.")
    for target in (BUILD, distribution):
        if not target.resolve().is_relative_to(ROOT):
            raise SystemExit("빌드 결과 경로가 저장소 바깥입니다.")
        target.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    with Image.open(ROOT / "web/icons/icon-512.png") as icon:
        icon.save(BUILD / "Donggu-PC.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(distribution),
                    "--workpath", str(BUILD / "work"), str(ROOT / "tools/desktop.spec")], cwd=ROOT, check=True)
    write_release_info(app)
    archive = distribution / "Donggu-PC-windows-x64.zip"
    zip_distribution(app, archive)
    print("실행 앱: " + str(app / "Donggu-PC.exe"))
    print("배포 ZIP: " + str(archive))
    print("ZIP SHA256: " + hashlib.sha256(archive.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
