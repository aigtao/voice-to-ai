# Third-Party Notices

Voice to AI application source is distributed under MIT. Bundled dependencies retain their own licenses; this document does not replace those terms.

The portable package includes Python and Tcl/Tk, NumPy, CTranslate2, faster-whisper (including its VAD asset), ONNX Runtime, PyAV and its codec libraries, sounddevice/PortAudio, soundfile/libsndfile, pynput, pyperclip, pystray, Pillow, OpenCC, Hugging Face client libraries and their dependencies. The build uses PyInstaller with its bootloader exception.

Complete license and notice files provided by installed distributions are copied into `third-party-licenses`, along with Python/Tcl/Tk licenses. `DEPENDENCIES.json` records distribution versions in the build environment; build-only packages may also be listed. Native libraries may have additional notices included by their wheel distributors.

pystray is LGPLv3. Its unmodified Python modules are shipped as part of the PyInstaller archive. The corresponding upstream source for 0.19.5 is available at https://github.com/moses-palmer/pystray/tree/v0.19.5 and https://pypi.org/project/pystray/0.19.5/ . The Voice to AI source package and build script permit rebuilding with a modified pystray version; no restriction on modifying or debugging these components is added. Include the LGPL text and its referenced GPL text when redistributing.

soundfile bundles libsndfile (LGPL); PyAV bundles FFmpeg libraries with their own licensing terms. Retain the notices distributed with their wheels. The offline edition includes Whisper small converted weights from Systran/faster-whisper-small (MIT, as declared in its model card). Preserve the model README, upstream Whisper MIT license, and MODEL_MANIFEST.json inside models/small. The smaller portable edition does not include weights.

Please preserve this document and the complete third-party license directory when sharing the portable package.

The Windows package also includes Microsoft Visual C++ runtime DLLs under Microsoft's redistribution terms, rather than the MIT license for Voice to AI. Build machines need a current Microsoft Visual C++ x64 runtime (14.42 or newer). Official runtime information and installer: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist . Retain applicable Microsoft license terms when redistributing these DLLs.
