"""PC·휴대폰 업무 이어하기 화면 회귀를 독립 프로세스에서 실행한다."""
from pathlib import Path
import subprocess
import sys


def main():
    for name in ("test_workflow_startup.py", "test_workflow_routes.py", "test_workflow_pc.py", "test_workflow_mobile.py"):
        subprocess.run([sys.executable, str(Path(__file__).with_name(name))], check=True)


if __name__ == "__main__":
    main()
