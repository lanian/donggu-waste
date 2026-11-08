"""프로그램에 포함된 읽기 전용 자산의 위치. 업무 자료 위치(DG_STATE)와 분리한다."""
from pathlib import Path
import sys


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        bundled = getattr(sys, "_MEIPASS", None)
        if not bundled:
            raise RuntimeError("실행 앱의 자산 폴더를 찾지 못했습니다. 배포 폴더 전체를 다시 풀어 주세요.")
        return Path(bundled).resolve()
    return Path(__file__).resolve().parents[2]
