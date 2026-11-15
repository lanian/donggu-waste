"""실행 앱의 읽기 전용 운영 점검과 허용 목록 기반 지원 보고서."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sys

from dgpc import __version__

PROBE_MESSAGES = {
    "ready": "이 PC에서 업무 서버와 화면 파일 응답을 확인했습니다.",
    "invalid_url": "접속 주소를 확인하세요. 기관망은 HTTPS 주소가 필요합니다.",
    "tls_error": "인증서의 신뢰·유효기간·접속 이름을 확인하세요.",
    "access_denied": "접속 허용 IP 대역과 서버 이름을 확인하세요.",
    "unreachable": "서버 실행 여부·주소·포트·네트워크 연결을 확인하세요.",
    "wrong_service": "이 주소에서 동구 업무 서버를 확인하지 못했습니다. 포트 충돌도 확인하세요.",
    "version_mismatch": "서버와 점검 프로그램의 버전이 다릅니다.",
    "assets_unavailable": "업무 화면 파일을 불러오지 못했습니다. 프로그램 폴더를 확인하세요.",
    "check_failed": "점검을 완료하지 못했습니다. 잠시 뒤 다시 점검하세요.",
}
SERVER_STATES = {"idle", "ready", "existing", "stopped", "error", "starting", "waiting"}


@dataclass(frozen=True)
class DiagnosticSnapshot:
    mode: str
    port: int
    url: str
    state_dir: Path
    status: str


def _safe_url(value):
    from desktop_connection import normalize_url
    try:
        return normalize_url(value)
    except (TypeError, ValueError):
        return ""


def _timestamp(value=None):
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            raise ValueError("timezone required")
        return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _size(value):
    return value if type(value) is int and 0 <= value < 2 ** 64 else None


def _storage_info(directory):
    folder = Path(directory)
    result = {"folder_exists": False, "folder_access": "unavailable", "disk_total_bytes": None,
              "disk_free_bytes": None, "disk_state": "unknown"}
    try:
        exists = folder.exists()
        result["folder_exists"] = exists
        if not exists:
            result["folder_access"] = "not_created"
        elif folder.is_dir():
            result["folder_access"] = ("read_write" if os.access(folder, os.R_OK | os.W_OK) else
                                       "read_only" if os.access(folder, os.R_OK) else "unavailable")
        volume = folder
        while not volume.exists() and volume != volume.parent:
            volume = volume.parent
        total, _, free = shutil.disk_usage(volume)
        result.update(disk_total_bytes=total, disk_free_bytes=free,
                      disk_state="low" if free < max(1024 ** 3, total // 20) else "ok")
    except (OSError, ValueError):
        pass
    return result


def collect_diagnostics(snapshot, *, probe=None):
    """선택한 폴더의 메타데이터만 확인한다. DB·키·로그 파일은 열지 않는다."""
    if probe is None:
        from desktop_connection import probe_url
        probe = probe_url
    try:
        checked = probe(snapshot.url)
        code = checked.get("code", "check_failed")
        if code not in PROBE_MESSAGES:
            code = "check_failed"
    except Exception:
        code = "check_failed"
    return _report_metadata({
        "checked_at": _timestamp(),
        "connection": {"mode": snapshot.mode, "port": snapshot.port, "url": snapshot.url},
        "server": {"launch_state": snapshot.status},
        "probe": {"code": code},
        "storage": _storage_info(snapshot.state_dir),
    })


def _report_metadata(result):
    """입력 전체를 직렬화하지 않는다. 지원 보고서의 필드를 여기에서 제한한다."""
    connection, server = result.get("connection", {}), result.get("server", {})
    storage, probe = result.get("storage", {}), result.get("probe", {})
    mode = connection.get("mode") if connection.get("mode") in {"local", "lan"} else "local"
    port = connection.get("port")
    code = probe.get("code")
    folder_access, disk_state = storage.get("folder_access"), storage.get("disk_state")
    launch_state = server.get("launch_state")
    return {
        "schema": 1, "application": "Donggu-PC", "version": __version__,
        "checked_at": _timestamp(result.get("checked_at")),
        "platform": sys.platform, "architecture": platform.machine(),
        "connection": {"mode": mode, "port": port if type(port) is int and 1 <= port <= 65535 else None,
                       "url": _safe_url(connection.get("url", "")), "scope": "this_device", "peer_confirmed": False},
        "server": {"launch_state": launch_state if launch_state in SERVER_STATES else "unknown",
                   "configuration_verified": False},
        "probe": {"code": code if code in PROBE_MESSAGES else "check_failed"},
        "storage": {"folder_exists": storage.get("folder_exists") is True,
                    "folder_access": folder_access if folder_access in {"read_write", "read_only", "unavailable", "not_created"} else "unavailable",
                    "disk_total_bytes": _size(storage.get("disk_total_bytes")),
                    "disk_free_bytes": _size(storage.get("disk_free_bytes")),
                    "disk_state": disk_state if disk_state in {"ok", "low", "unknown"} else "unknown"},
    }


def report_text(result):
    return json.dumps(_report_metadata(result), ensure_ascii=False, indent=2) + "\n"


def save_report(destination, result):
    destination = Path(destination)
    temporary = destination.with_name(destination.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        temporary.write_text(report_text(result), encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def diagnostics_text(result):
    info = _report_metadata(result)
    storage = info["storage"]
    free = storage["disk_free_bytes"]
    capacity = "확인 못함" if free is None else f"{free / 1024 ** 3:,.1f} GB 여유"
    access = {"read_write": "읽기·쓰기 권한 표시 확인", "read_only": "읽기 권한만 확인", "unavailable": "접근 확인 필요",
              "not_created": "아직 생성되지 않음"}[storage["folder_access"]]
    lines = [f"점검 시각: {info['checked_at']}", PROBE_MESSAGES[info["probe"]["code"]],
             f"선택 자료 폴더: {access}", f"저장공간: {capacity}"]
    if storage["disk_state"] == "low":
        lines.append("남은 저장공간이 적습니다. 백업과 여유 공간을 확인하세요.")
    if info["server"]["launch_state"] == "existing":
        lines.append("기존 서버의 실제 자료 폴더와 접속 설정은 이 실행창에서 확인할 수 없습니다.")
    lines.append("다른 PC 접속: 미확인. 해당 PC에서 ‘PC 접속 점검’을 실행하세요.")
    return "\n".join(lines)
