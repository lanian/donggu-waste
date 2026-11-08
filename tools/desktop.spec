# Windows GUI onedir 배포. 실행 파일과 _internal 폴더를 함께 배포한다.
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

TOOLS = Path(SPECPATH)
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from build_desktop import asset_datas, runtime_distributions

datas = asset_datas()
for name in runtime_distributions():
    datas += copy_metadata(name)
binaries = collect_dynamic_libs('ortools') + collect_dynamic_libs('sqlcipher3')
hiddenimports = ['desktop_runtime', 'pc_tool', 'sample_data', 'sqlcipher3._sqlite3',
                 'keyring.backends.Windows', 'keyring.backends.chainer', 'keyring.backends.fail',
                 'tkinter', 'tkinter.ttk', '_tkinter',
                 # SWIG 네이티브 함수가 protobuf 모듈을 동적으로 불러온다.
                 'ortools.sat.sat_parameters_pb2', 'ortools.util.optional_boolean_pb2',
                 'google.protobuf.duration_pb2'] + collect_submodules('win32ctypes') + collect_submodules('ortools.constraint_solver')

a = Analysis([str(ROOT / 'pc/desktop.py')], pathex=[str(ROOT / 'pc')],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['pytest', 'playwright'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Donggu-PC', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, icon=str(ROOT / 'build/desktop/Donggu-PC.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Donggu-PC')
