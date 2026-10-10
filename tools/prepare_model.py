"""Download the public small model for an offline distribution, without Hub credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

REPO = "Systran/faster-whisper-small"
REVISION = "536b0662742c02347bc0e980a01041f333bce120"
MODEL_SHA256 = "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"
FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "README.md")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="https://huggingface.co")
    args = parser.parse_args()
    endpoint = args.endpoint.rstrip("/")
    response = subprocess.run(["curl.exe", "--fail", "--silent", "--show-error", "--location", "--max-time", "30",
                               f"{endpoint}/api/models/{REPO}?blobs=true"], check=True, capture_output=True)
    repository = json.loads(response.stdout)
    if repository["sha"] != REVISION or repository["cardData"]["license"] != "mit":
        raise RuntimeError("Unexpected model revision or license")
    metadata = {item["rfilename"]: item for item in repository["siblings"]}
    target = Path(__file__).resolve().parents[1] / "models" / "small"
    target.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in FILES:
        path = target / name
        expected_size = metadata[name]["size"]
        if not path.exists() or path.stat().st_size != expected_size:
            temporary = path.with_suffix(path.suffix + ".part")
            print(f"Downloading {name} ({expected_size:,} bytes)", flush=True)
            subprocess.run([
                "curl.exe", "--fail", "--location", "--retry", "3", "--connect-timeout", "15",
                "--max-time", "1200", "--output", str(temporary),
                f"{endpoint}/{REPO}/resolve/{REVISION}/{name}",
            ], check=True)
            if temporary.stat().st_size != expected_size:
                raise RuntimeError(f"Wrong file size: {name}")
            temporary.replace(path)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if name == "model.bin":
            if digest != MODEL_SHA256:
                raise RuntimeError("Model SHA256 mismatch")
        else:
            content = path.read_bytes()
            blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
            if blob != metadata[name]["blobId"]:
                raise RuntimeError(f"Git blob mismatch: {name}")
        hashes[name] = digest
        print(f"Verified {name}", flush=True)
    license_path = target / "LICENSE-Whisper.txt"
    response = subprocess.run(["curl.exe", "--fail", "--silent", "--show-error", "--location", "--max-time", "30",
                               "https://raw.githubusercontent.com/openai/whisper/main/LICENSE"], check=True, capture_output=True)
    license_text = response.stdout
    if b"MIT License" not in license_text or b"OpenAI" not in license_text:
        raise RuntimeError("Unexpected upstream license")
    license_path.write_bytes(license_text)
    hashes[license_path.name] = hashlib.sha256(license_text).hexdigest()
    manifest = {"repository": REPO, "revision": REVISION, "download_endpoint": endpoint,
                "license": "MIT", "sha256": hashes}
    (target / "MODEL_MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Offline model prepared and verified", flush=True)


if __name__ == "__main__":
    main()
