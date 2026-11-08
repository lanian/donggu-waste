"""소스·Windows 배포판의 읽기 전용 자산 경로 회귀. 업무 DB를 열거나 만들지 않는다."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
passed = 0


def ok(condition, message):
    global passed
    assert condition, message
    passed += 1
    print("  ✓", message, flush=True)


def build_output_preflight():
    """가상 출력 폴더와 빌드 호출 대체만 사용한다. PyInstaller·삭제는 실행하지 않는다."""
    import build_desktop as builder
    from PIL import Image
    with tempfile.TemporaryDirectory(prefix="dg desktop output preflight ") as temporary:
        repository = Path(temporary).resolve()
        distribution = repository / "dist" / "desktop"
        selected = distribution / "v0.13.4"
        app = selected / "Donggu-PC"
        (repository / "pc").mkdir()
        (repository / "pc" / "desktop.py").write_text("# 가상 진입점\n", encoding="utf-8")
        with patch.multiple(builder, ROOT=repository, DIST=distribution, BUILD=repository / "build" / "desktop"), \
                patch.object(sys, "platform", "win32"), \
                patch.object(sys, "argv", ["build_desktop.py", "--dist-dir", "dist/desktop/v0.13.4"]), \
                patch.object(builder.metadata, "version", return_value="6.22.3"), \
                patch.object(Image, "open"), patch.object(builder.subprocess, "run") as run, \
                patch.object(builder, "write_release_info") as release, \
                patch.object(builder, "zip_distribution", side_effect=lambda directory, destination: destination.write_bytes(b"fixture zip")) as zipped:
            builder.main()
            command = run.call_args.args[0]
            ok(command[command.index("--distpath") + 1] == str(selected)
               and release.call_args.args == (app,)
               and zipped.call_args.args == (app, selected / "Donggu-PC-windows-x64.zip"),
               "상대 --dist-dir는 저장소 기준 버전 폴더의 PyInstaller·앱·ZIP 경로로 전달")
            # 기본 출력 계약과 빌드 외부 경로 거부도 폴더 변경 없이 확인한다.
            ok(builder.resolve_dist_dir() == distribution, "빌드 기본 출력 경로 계약 유지")
            rejected = 0
            for outside in (Path("dist/other"), repository.parent / "outside-build", Path("dist/desktop/../escaped")):
                try:
                    builder.resolve_dist_dir(outside)
                except ValueError:
                    rejected += 1
            ok(rejected == 3, "빌드 출력은 실제 dist/desktop 내부 경로만 허용")
            app.mkdir(parents=True)
            original = app / "Donggu-PC.exe"
            original.write_bytes(b"original executable fixture")
            private = app / "pc.db"
            private.write_bytes(b"fictitious database fixture")
            run.reset_mock()
            protected = False
            try:
                builder.main()
            except ValueError:
                protected = True
            ok(protected and not run.called and original.read_bytes() == b"original executable fixture"
               and private.read_bytes() == b"fictitious database fixture",
               "기존 출력에 DB가 있으면 PyInstaller 호출 전에 거부하고 원본 파일 보존")


def main():
    ok((ROOT / "pc/dgpc/paths.py").is_file(), "공통 배포 자산 경로 helper가 있음")
    sys.path.insert(0, str(ROOT / "pc"))
    from dgpc.paths import resource_root
    ok(resource_root() == ROOT, "원본 실행의 기존 저장소 자산 위치 보존")
    with tempfile.TemporaryDirectory(prefix="dg desktop paths ") as temporary:
        work = Path(temporary).resolve()
        old_cwd = Path.cwd()
        try:
            os.chdir(work)
            ok(resource_root() == ROOT, "시작 작업 폴더와 무관하게 자산 탐색")
        finally:
            os.chdir(old_cwd)
        assets = work / "임시 bundle 자산"
        (assets / "data").mkdir(parents=True)
        for name in ("labels.json", "fee_table.json", "item_handling.json"):
            data = json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))
            if name == "fee_table.json":
                data["label"] = "가상 배포판 수수료표"
            if name == "item_handling.json":
                data["default"][0] = 19.75
            (assets / "data" / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        script = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from dgpc.paths import resource_root
sys.frozen = True
sys._MEIPASS = sys.argv[2]
expected_root = Path(sys.argv[2]).resolve()
assert resource_root() == expected_root
from dgpc import core, fees, routing
import sample_data
assert core.ROOT == expected_root
assert fees.FEE_TABLE['label'] == '가상 배포판 수수료표'
assert sample_data.FEE_TABLE['label'] == '가상 배포판 수수료표'
assert routing.DEFAULT_VOLUME == 19.75
assert not Path(os.environ['DG_STATE']).exists()
assert not Path(os.environ['DG_OUT']).exists()
del sys._MEIPASS
try:
    resource_root()
except RuntimeError:
    pass
else:
    raise AssertionError('frozen 자산 위치가 없는데 원본 위치로 조용히 대체함')
"""
        env = {**os.environ, "DG_STATE": str(work / "업무 자료"), "DG_OUT": str(work / "출력")}
        # CI 임시 경로의 별칭·정규화 차이도 통과해야 한다.
        asset_argument = assets / ".." / assets.name
        result = subprocess.run([sys.executable, "-c", script, str(ROOT / "pc"), str(asset_argument)], cwd=work,
                                env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
        ok(result.returncode == 0, "frozen 자산 위치를 core·fees·routing·QR용 sample_data가 함께 사용: " + result.stderr)
        ok(not (work / "업무 자료").exists() and not (work / "출력").exists(), "자산 조회가 업무 자료·출력 폴더를 만들지 않음")
    ok((ROOT / "tools/build_desktop.py").is_file(), "화이트리스트 배포 빌더가 있음")
    sys.path.insert(0, str(ROOT / "tools"))
    from build_desktop import asset_files, zip_distribution
    included = {path.relative_to(ROOT).as_posix() for path in asset_files()}
    ok({"data/labels.json", "data/fee_table.json", "data/item_handling.json", "pc/ui/start.js",
        "web/vendor/maplibre/maplibre-gl-worker.mjs", "web/icons/icon-512.png"} <= included,
       "PC 시작 화면·공유 worker·JSON·아이콘을 배포 자산에 포함")
    ok(not any(name.startswith(("pc/state/", "pc/out/", "tests/", "data/source/")) or name.endswith("derive_vectors.json")
               for name in included), "업무 자료·출력·시험 벡터·HWP 원문은 자산 목록에서 제외")
    with tempfile.TemporaryDirectory(prefix="dg desktop zip ") as temporary:
        work = Path(temporary).resolve()
        app = work / "Donggu-PC"
        app.mkdir()
        (app / "Donggu-PC.exe").write_bytes(b"MZ-test-fixture")
        private = app / "pc.db-wal"
        private.write_bytes(b"fictitious private data")
        blocked = False
        try:
            zip_distribution(app, work / "blocked.zip")
        except ValueError:
            blocked = True
        ok(blocked and not (work / "blocked.zip").exists(), "배포 폴더에 DB 곁 파일이 섞이면 ZIP 생성 전에 거부")
        private.unlink()
        zip_distribution(app, work / "clean.zip")
        with zipfile.ZipFile(work / "clean.zip") as archive:
            ok(archive.namelist() == ["Donggu-PC/Donggu-PC.exe"], "ZIP은 실행 폴더 전체를 한 최상위 폴더로 묶음")
    build_output_preflight()
    print(f"데스크톱 자산 경로 {passed}개 통과", flush=True)


if __name__ == "__main__":
    main()
