"""서버·DB 없이 실행 앱의 가상 상태를 미리 본다.

실행: python tools/preview_desktop.py --state ready --size 680x520
F1 준비 완료 / F2 시작 중 / F3 오류 / F4 기존 서버 / F5 연결 끊김
F6 종료 대기 / F7 종료 완료 / F8 설정·상세 접기·펼치기.
모든 버튼은 가상 상태나 문구만 바꾸며 실제 업무 자료를 읽거나 쓰지 않는다.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tkinter as tk

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pc"))

from desktop_view import DesktopView, STATES  # noqa: E402

FAKE_PATH = r"C:\가상 업무 자료\동구\state"
LONG_FAKE_PATH = FAKE_PATH + r"\길이가 긴 가상 자료 폴더\디자인 검토용 하위 폴더" * 3
ERROR_DETAIL = (
    "가상 오류: 서버의 준비 응답을 확인하지 못했습니다. 실제 서버에는 연결하지 않았습니다.\n"
    "긴 안내 문장이 줄바꿈되어 표시되고, 오류 내용 전체를 선택하거나 복사할 수 있는지 확인하세요. "
    "이 문구는 화면 검토를 위해 만든 예시이며 실제 자료나 비밀번호를 포함하지 않습니다.\n"
    "가상 경로: " + LONG_FAKE_PATH
)


def size_argument(value):
    if not re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("창 크기는 680x520 형식으로 입력하세요.")
    return value


def scaling_argument(value):
    try:
        scaling = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("배율은 1.333 또는 2.667 같은 양수입니다.") from error
    if not 0 < scaling <= 8:
        raise argparse.ArgumentTypeError("배율은 0보다 크고 8 이하여야 합니다.")
    return scaling


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", choices=tuple(STATES), default="ready")
    parser.add_argument("--size", type=size_argument, default="680x480")
    parser.add_argument("--scaling", type=scaling_argument, help="선택적 Tk 글꼴 배율")
    parser.add_argument("--version", default="0.13.7", help="화면에만 표시할 가상 버전")
    args = parser.parse_args()

    root = tk.Tk()
    root.title("동구 PC 디자인 검토 · 가상 화면")
    if args.scaling is not None:
        root.tk.call("tk", "scaling", args.scaling)
    root.geometry(args.size)

    def show(state):
        detail = ERROR_DETAIL if state == "error" else (
            "가상 종료 대기: 진행 중인 작업이 끝나는 상황을 표시합니다. 실제 서버는 실행되지 않았습니다."
            if state == "stop_waiting" else ""
        )
        view.set_path(LONG_FAKE_PATH if state == "error" else FAKE_PATH)
        view.show_state(state, detail=detail, owned=state in {"ready", "stop_waiting", "stopping"})

    def note(message):
        view.description_var.set(message)

    callbacks = {
        "open": lambda: note("가상 미리보기입니다. 실제 브라우저나 업무 화면을 열지 않습니다."),
        "retry": lambda: show("ready"),
        "reconnect": lambda: show("existing"),
        "password": lambda: show("password"),
        "stop": lambda: show("stopped"),
        "folder": lambda: note("가상 미리보기입니다. 자료 폴더를 읽거나 변경하지 않습니다."),
        "shortcut": lambda: note("가상 미리보기입니다. 바로가기나 다른 파일을 만들지 않습니다."),
    }
    view = DesktopView(root, args.version, callbacks=callbacks)
    view.pack(fill="both", expand=True)
    show(args.state)

    for key, state in (("F1", "ready"), ("F2", "starting"), ("F3", "error"), ("F4", "existing"),
                       ("F5", "connection_lost"), ("F6", "stop_waiting"), ("F7", "stopped")):
        root.bind("<" + key + ">", lambda event, target=state: (show(target), "break")[1])
    root.bind("<F8>", lambda event: (view.toggle_details(), "break")[1])

    def close():
        view.destroy()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
