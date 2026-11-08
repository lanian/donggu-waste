"""브라우저 첫 화면의 필요한 모듈·조회 제한·지연 복구를 독립 실행한다."""
from pathlib import Path
import subprocess
import sys


def main():
    for name in ("test_pc_legacy_startup.py", "test_pc_startup_navigation.py", "test_pc_lazy_startup.py",
                 "test_pc_startup_reads.py", "test_pc_startup_ui.py"):
        subprocess.run([sys.executable, str(Path(__file__).with_name(name))], check=True)


if __name__ == "__main__":
    main()
