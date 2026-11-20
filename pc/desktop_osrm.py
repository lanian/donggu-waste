"""Windows 실행 앱이 소유하는 동봉 OSRM 프로세스."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as ct
from ctypes import wintypes as W
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time

from dgpc import osrm


def bundled_pack_dir():
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / osrm.PACK_DIR
    return None


def _free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


LOOPBACK_ADDRESS = int.from_bytes(socket.inet_aton('127.0.0.1'), 'little')


def _listeners(port):
    # 같은 포트의 모든 로컬 리스너를 검사한다. Windows에서는 주소 재사용으로 둘 다 열릴 수 있다.
    tcp_table = ct.WinDLL('iphlpapi').GetExtendedTcpTable
    tcp_table.argtypes = [ct.c_void_p, ct.POINTER(W.DWORD), W.BOOL, W.ULONG, ct.c_int, W.ULONG]
    tcp_table.restype = W.DWORD
    size = W.DWORD()
    tcp_table(None, ct.byref(size), False, socket.AF_INET, 3, 0)  # TCP_TABLE_OWNER_PID_LISTENER
    buf = ct.create_string_buffer(size.value)
    if tcp_table(buf, ct.byref(size), False, socket.AF_INET, 3, 0) != 0:
        return set()
    (count,) = struct.unpack_from('=I', buf)
    return {(addr, owner) for _, addr, local_port, _, _, owner in struct.iter_unpack('=6I', buf.raw[4:4 + count * 24])
            if addr in (0, LOOPBACK_ADDRESS) and socket.ntohs(local_port & 0xffff) == port}


class _BasicLimits(ct.Structure):
    _fields_ = [('per_process', ct.c_int64), ('per_job', ct.c_int64), ('flags', W.DWORD),
                ('min_working_set', ct.c_size_t), ('max_working_set', ct.c_size_t), ('active_processes', W.DWORD),
                ('affinity', ct.c_size_t), ('priority', W.DWORD), ('scheduling', W.DWORD)]


class _ExtendedLimits(ct.Structure):
    _fields_ = [('basic', _BasicLimits), ('io_counters', ct.c_uint64 * 6),
                ('process_memory', ct.c_size_t), ('job_memory', ct.c_size_t),
                ('peak_process_memory', ct.c_size_t), ('peak_job_memory', ct.c_size_t)]


@contextmanager
def _process_job():
    kernel = ct.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ct.c_void_p, W.LPCWSTR]
    kernel.CreateJobObjectW.restype = W.HANDLE
    kernel.SetInformationJobObject.argtypes = [W.HANDLE, ct.c_int, ct.c_void_p, W.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ct.WinError(ct.get_last_error())
    try:
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: 업무 서버가 비정상 종료되어도 정리
        if not kernel.SetInformationJobObject(job, 9, ct.byref(limits), ct.sizeof(limits)):
            raise ct.WinError(ct.get_last_error())

        def own(process):
            if not kernel.AssignProcessToJobObject(job, int(process._handle)):
                raise ct.WinError(ct.get_last_error())

        yield own
    finally:
        kernel.CloseHandle(job)


def _spawn(argv, *, cwd=None):
    # PyInstaller가 넣은 DLL 검색 경로를 별도 공식 Windows 프로그램에 물려주지 않는다.
    bundled_dlls = getattr(sys, '_MEIPASS', None)
    if bundled_dlls:
        dll_directory = ct.WinDLL('kernel32', use_last_error=True).SetDllDirectoryW
        dll_directory.argtypes = [W.LPCWSTR]
        dll_directory(None)
    try:
        return subprocess.Popen(argv, cwd=cwd or Path(argv[0]).parent, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    finally:
        if bundled_dlls:
            dll_directory(bundled_dlls)


class BundledOSRM:
    def __init__(self, directory: Path, dataset: Path | None = None):
        self.directory = directory.resolve()
        self.dataset = dataset.resolve() if dataset is not None else self.directory / osrm.DATASET
        self.edited_data = dataset is not None
        self.status = 'MISSING'
        self.endpoint = ''
        self.thread = None
        self.cancel = threading.Event()
        self.settled = threading.Event()
        self.lock = threading.Lock()
        if directory.exists():
            try:
                osrm.pack_files(directory)
                if dataset is not None and not all(self.dataset.with_suffix(suffix).is_file() for suffix in
                                                   ('.osrm.partition', '.osrm.cells', '.osrm.cell_metrics', '.osrm.mldgr')):
                    raise ValueError('도로망 파일이 없습니다.')
                self.status = 'AVAILABLE'
            except ValueError:
                self.status = 'INVALID_PACK'

    def local_endpoint(self, *, retry=False):
        with self.lock:
            if (self.status == 'AVAILABLE' or (self.status == 'FAILED' and retry)) and not self.cancel.is_set():
                self.status = 'STARTING'
                self.settled.clear()
                self.thread = threading.Thread(target=self._run, name='bundled-osrm', daemon=True)
                self.thread.start()
            return self.endpoint, self.status

    def wait_ready(self, timeout):
        self.settled.wait(timeout)
        with self.lock:
            return self.endpoint, self.status

    def _ready(self, process, port, url):
        deadline = time.monotonic() + 25
        owned = {(LOOPBACK_ADDRESS, process.pid)}
        while not self.cancel.is_set() and process.poll() is None and time.monotonic() < deadline:
            listeners = _listeners(port)
            if listeners and listeners != owned:
                return False
            if listeners == owned:
                # 기동 시험은 광주의 고정 좌표 하나. 자료에 해당 위치가 없어도 서버 응답은 확인할 수 있다.
                response = osrm.fetch(url, '/nearest/v1/driving/126.923300,35.146200?number=1', timeout=.5)
                if response.get('code') in ('Ok', 'NoSegment'):
                    return process.poll() is None and _listeners(port) == owned
            self.cancel.wait(.1)
        return False

    def _run(self):
        try:
            with _process_job() as own:
                for _ in range(2):  # 포트를 찾고 osrm-routed가 열기 사이의 충돌은 새 포트로 한 번 재시도
                    if self.cancel.is_set():
                        return
                    port = _free_port()
                    url = f'http://127.0.0.1:{port}'
                    command = [str(self.directory / osrm.EXECUTABLE), '--ip', '127.0.0.1', '--port', str(port),
                               '--algorithm', 'mld', '--max-table-size', str(osrm.MAX_PACK_TABLE_POINTS)]
                    if self.edited_data:  # Windows OSRM은 한글이 든 인수 대신 작업 폴더와 데이터 이름을 따로 받는다.
                        process = _spawn([*command, self.dataset.name], cwd=self.dataset.parent)
                    else:
                        process = _spawn([*command, str(self.dataset)])
                    try:
                        own(process)
                        if self._ready(process, port, url):
                            with self.lock:
                                self.status, self.endpoint = 'READY', url
                                self.settled.set()
                            while process.poll() is None and not self.cancel.wait(.2):
                                pass
                            return
                    finally:
                        if process.poll() is None:
                            process.terminate()
                        process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass  # 앱은 계속 열고 배정 결과에서 직선 대체와 원인을 안내한다.
        finally:
            with self.lock:
                self.endpoint, self.status = '', 'FAILED'
                self.settled.set()

    def close(self):
        self.cancel.set()
        if self.thread:
            self.thread.join()


@contextmanager
def bundled_osrm(directory, state=None):
    if directory is None:
        yield None
        return
    if state is None:
        bundle = BundledOSRM(directory)
    else:
        from desktop_road_rules import RoadRulesManager
        bundle = RoadRulesManager(directory, state, lambda dataset: BundledOSRM(directory, dataset))
    osrm.bundled = bundle
    try:
        yield bundle
    finally:
        bundle.close()
        osrm.bundled = None
