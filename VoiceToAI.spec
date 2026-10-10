from PyInstaller.utils.hooks import collect_data_files, copy_metadata
import os
from pathlib import Path
import ctypes

# Driver folders on PATH can contain an older incompatible VC runtime.
# Keep one matching set from the Windows x64 runtime instead of that PATH copy.
runtime_dir = Path(os.environ["WINDIR"]) / "System32"
runtime_names = (
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
    "msvcp140_codecvt_ids.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll",
)
for name in runtime_names:
    path = runtime_dir / name
    if not path.is_file():
        raise RuntimeError("Install the current Microsoft Visual C++ x64 Redistributable before building")
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    buffer = ctypes.create_string_buffer(size)
    ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer)
    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length))
    fixed = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
    if fixed[2] < (14 << 16 | 42):
        raise RuntimeError("Build requires Microsoft VC runtime 14.42 or newer")

datas = [("voice-to-ai.ico", "."), ("voice-to-ai-icon.png", ".")]
for package in ("faster_whisper", "opencc"):
    datas += collect_data_files(package)
for distribution in ("faster-whisper", "huggingface-hub", "tokenizers", "onnxruntime"):
    datas += copy_metadata(distribution)

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["pynput.keyboard._win32", "pynput.mouse._win32", "pystray._win32"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow", "transformers", "matplotlib", "pandas", "IPython", "pytest"],
    noarchive=False,
)
a.binaries = [entry for entry in a.binaries if entry[0].lower() not in runtime_names]
a.binaries += [(name, str(runtime_dir / name), "BINARY") for name in runtime_names]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="VoiceToAI", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, icon="voice-to-ai.ico",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="VoiceToAI")
