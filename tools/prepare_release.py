"""Assemble shareable artifacts from an explicit source allowlist."""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.1-preview"
SOURCE_FILES = (
    "app.py", "build_icon.py", "requirements.txt", "requirements-build.txt",
    "build-constraints.txt", "VoiceToAI.spec", "build_release.ps1", "run.bat",
    "启动语音工具.vbs", "启动说明.txt", "settings.example.json", ".gitignore",
    "README.md", "PRODUCT.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE",
    "PUBLISH_CHECKLIST.md",
    "QUICKSTART.md", "RELEASE_NOTES.md", "SHARING.md", "THIRD_PARTY_NOTICES.md",
    "voice-to-ai.ico", "voice-to-ai-icon.png", "tools/prepare_release.py", "tools/prepare_model.py",
)
LICENSE_DISTRIBUTIONS = {
    "faster-whisper", "numpy", "sounddevice", "soundfile", "pynput", "pyperclip",
    "pystray", "opencc-python-reimplemented", "ctranslate2", "huggingface-hub",
    "tokenizers", "onnxruntime", "av", "pillow", "cffi", "pycparser", "six",
    "pyyaml", "tqdm", "typing-extensions", "filelock", "fsspec", "hf-xet",
    "certifi", "httpx", "httpcore", "h11", "anyio", "idna", "click", "flatbuffers",
    "protobuf", "colorama", "packaging", "setuptools", "pyinstaller", "altgraph",
    "pefile", "pywin32-ctypes", "pyinstaller-hooks-contrib", "truststore",
}


def archive_tree(directory: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, Path(directory.name) / path.relative_to(directory))


def main() -> None:
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    portable = ROOT / "dist" / "VoiceToAI"
    if not (portable / "VoiceToAI.exe").exists():
        raise SystemExit("Build VoiceToAI.exe first")
    for name in ("LICENSE", "QUICKSTART.md", "RELEASE_NOTES.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, portable / name)
    licenses = portable / "third-party-licenses"
    if licenses.exists():
        shutil.rmtree(licenses)
    licenses.mkdir(exist_ok=True)
    dependencies = []
    for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        if name.lower().replace("_", "-") not in LICENSE_DISTRIBUTIONS:
            continue
        dependencies.append({"name": name, "version": distribution.version})
        for file in distribution.files or ():
            if not any(word in file.name.lower() for word in ("license", "copying", "notice", "copyright")):
                continue
            source = Path(distribution.locate_file(file))
            if source.is_file() and source.suffix.lower() not in (".py", ".pyc", ".pyd", ".dll"):
                relative = Path(*(part for part in file.parts if part not in ("..", ".", "/", "\\")))
                target = licenses / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    for name in ("LICENSE.txt", "tcl/tcl8.6/license.terms", "tcl/tk8.6/license.terms"):
        origin = Path(sys.base_prefix) / name
        if origin.is_file():
            target = licenses / "python-runtime" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    (portable / "DEPENDENCIES.json").write_text(json.dumps(dependencies, indent=2), encoding="utf-8")
    source_root = release / f"VoiceToAI-{VERSION}-source"
    source_root.mkdir(exist_ok=True)
    for name in SOURCE_FILES:
        target = source_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    artifacts = [release / f"VoiceToAI-{VERSION}-windows-x64.zip", release / f"VoiceToAI-{VERSION}-source.zip"]
    archive_tree(portable, artifacts[0])
    archive_tree(source_root, artifacts[1])
    model_dir = ROOT / "models" / "small"
    if model_dir.exists():
        manifest = json.loads((model_dir / "MODEL_MANIFEST.json").read_text(encoding="utf-8"))
        if not set(("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")).issubset(manifest["sha256"]):
            raise RuntimeError("Incomplete model manifest")
        for filename, expected in manifest["sha256"].items():
            with (model_dir / filename).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise RuntimeError(f"Model hash mismatch: {filename}")
        offline = release / f"VoiceToAI-{VERSION}-windows-x64-offline.zip"
        with zipfile.ZipFile(offline, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(portable.rglob("*")):
                if path.is_file():
                    archive.write(path, Path("VoiceToAI") / path.relative_to(portable))
            for name in (*manifest["sha256"].keys(), "MODEL_MANIFEST.json"):
                archive.write(model_dir / name, Path("VoiceToAI/models/small") / name)
        artifacts.append(offline)
    hashes = []
    for artifact in artifacts:
        with artifact.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        hashes.append(f"{digest}  {artifact.name}")
        print(f"{artifact.name}: {artifact.stat().st_size:,} bytes")
    (release / "SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
