"""생성된 Windows EXE에서 동별 신규 접수·배정·권한 변경을 검증한다."""
import argparse
from pathlib import Path

import test_dong_work_e2e as flow
from test_reports_labels_bundle import frozen_server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    exe = parser.parse_args().exe.resolve()
    assert exe.is_file(), exe
    flow.pc_server = lambda: frozen_server(exe)
    flow.main()
    print("실제 EXE 동별 업무: Python 없는 PATH·암호화 DB·신규 위치·등록·배정·권한 취소·정상 종료 통과")
