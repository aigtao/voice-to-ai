from __future__ import annotations

import json
import math
import os
import queue
import re
import sys
import threading
import time
import atexit
import textwrap
import urllib.error
import urllib.request
import ctypes
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pyperclip
import sounddevice as sd
import soundfile as sf
import tkinter as tk
from tkinter import messagebox, ttk

from faster_whisper import WhisperModel
from pynput import keyboard

try:
    from opencc import OpenCC
except ImportError:
    OpenCC = None


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VoiceToAI-MVP"
RECORD_DIR = APP_DIR / "records"
HISTORY_FILE = APP_DIR / "history.jsonl"
SAMPLE_RATE = 16_000
CHANNELS = 1
HOTKEY = "Ctrl+Alt+Space"
LOCK_FILE = APP_DIR / "app.lock"
PAUSE_BREAK_SECONDS = 0.65
PARAGRAPH_BREAK_SECONDS = 1.35
# Keep a pasted paragraph scannable when Whisper returns one long segment.
# This is a formatting limit, not a claim about where the speaker paused.
MAX_SENTENCE_CHARS = 48
# The client cannot see provider-side queue/cold-start state.  Use a small
# elapsed-time threshold to make an unusually slow API response visible.
API_WAITING_THRESHOLD_SECONDS = 8
LOCAL_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")


class LocalModelUnavailable(RuntimeError):
    pass


def portable_model_directory() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "models" / "small"


def complete_local_model(path: Path) -> bool:
    return (
        all((path / name).is_file() and (path / name).stat().st_size > 0 for name in LOCAL_MODEL_FILES)
        and (path / "model.bin").stat().st_size > 1_000_000
    )


@dataclass
class Record:
    created_at: str
    audio_path: str
    raw_text: str
    task_text: str
    language: str = ""


def normalize_transcript(text: str) -> str:
    """Normalize spacing while preserving the words produced by speech recognition."""
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def normalize_recognized_terms(text: str, language: str) -> str:
    """Apply narrow, meaning-preserving spelling fixes to recognized text."""
    text = re.sub(
        r"(?<![A-Za-z0-9_\\/])(?:verse(?:2|\s+to\s+)|worst\s+to\s+)ai(?![A-Za-z0-9_\\/])",
        "Voice to AI",
        text,
        flags=re.IGNORECASE,
    )
    if language.startswith("zh"):
        text = re.sub(r"(?<=[\u3400-\u9fff]),\s*(?=[\u3400-\u9fff])", "，", text)
        text = re.sub(
            r"(?<=[\u3400-\u9fff])Voice to AI(?=[\u3400-\u9fff])",
            " Voice to AI ",
            text,
        )
    return text


def to_simplified_chinese(text: str, language: str = "") -> str:
    """Convert Chinese output to Simplified Chinese when OpenCC is installed."""
    if not text or not (language.startswith("zh") or _has_cjk(text)):
        return text
    if OpenCC is None:
        return text
    try:
        return OpenCC("t2s").convert(text)
    except Exception:
        return text


def build_task_prompt(raw_text: str) -> str:
    """Backward-compatible helper; AI mode no longer uses a fixed template."""
    return normalize_transcript(raw_text)


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", text))


def _join_segment_text(previous: str, current: str) -> str:
    """Join Whisper chunks without inserting spaces into Chinese text."""
    if not previous:
        return current
    if not current:
        return previous
    if _has_cjk(previous[-1:]) or _has_cjk(current[:1]):
        return previous + current
    if previous.endswith((" ", "\n", "(", "[", "{", "/", "\\", ".", ":", "_", "-")):
        return previous + current
    if current.startswith(tuple(",.!?;:)]}%/\\_")):
        return previous + current
    return previous + " " + current


def _is_cjk_char(value: str) -> bool:
    return bool(value and re.search(r"[\u3400-\u9fff]", value))


def _normalize_punctuation(text: str, language: str = "") -> str:
    """Normalize punctuation without rewriting the words recognized by Whisper."""
    text = normalize_transcript(text)
    if not text:
        return ""
    # Whisper may emit ASCII punctuation even for Chinese speech.
    if language.startswith("zh") or _has_cjk(text):
        replacements = {
            ",": "，", ";": "；", ":": "：", "?": "？", "!": "！",
        }
        text = "".join(replacements.get(char, char) for char in text)
        text = re.sub(r"\s*([，。！？；：])\s*", r"\1", text)
        text = re.sub(r"([。！？；]){2,}", r"\1", text)
    else:
        text = re.sub(r"\s*([,.!?;:])\s*", r"\1 ", text).strip()
        text = re.sub(r"([.!?]){2,}", r"\1", text)
    return text.strip()


def _split_long_clause(text: str, limit: int, language: str = "") -> list[str]:
    """Split an overlong clause at a word/CJK boundary and add readable punctuation."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    remaining = text
    cjk = language.startswith("zh") or _has_cjk(remaining)
    while len(remaining) > limit:
        cut = limit
        # Prefer an existing separator near the limit.
        separators = "，,；;：:、 " if cjk else " ,;:"
        candidates = [remaining.rfind(char, max(1, limit - 18), limit + 1) for char in separators]
        candidates = [candidate for candidate in candidates if candidate > 0]
        if candidates:
            cut = max(candidates)
        elif cjk:
            # Whisper often omits punctuation in Chinese. Prefer a clause
            # boundary before a connective instead of cutting through a word.
            connectives = ("以及", "如果", "然后", "但是", "并且", "所以", "因此", "同时", "另外", "目前")
            connective_candidates = [
                remaining.find(word, max(1, limit - 18), limit + 1)
                for word in connectives
            ]
            connective_candidates = [candidate for candidate in connective_candidates if candidate > 0]
            if connective_candidates:
                cut = min(connective_candidates)
        elif not cjk:
            next_space = remaining.find(" ", limit)
            if next_space > 0 and next_space <= limit + 18:
                cut = next_space
        piece = remaining[:cut].strip(" ,，;；:：")
        if not piece:
            cut = limit
            piece = remaining[:cut].strip()
        parts.append(piece + ("，" if cjk else ","))
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _format_clause(text: str, language: str = "", limit: int = MAX_SENTENCE_CHARS) -> list[str]:
    """Add conservative sentence punctuation and wrap long clauses."""
    text = _normalize_punctuation(text, language)
    if not text:
        return []
    cjk = language.startswith("zh") or _has_cjk(text)
    chunks = re.split(r"(?<=[。！？!?；;])", text)
    output: list[str] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        # A chunk ending in a clause separator is already complete. Otherwise
        # add a period so pasted text does not become one unpunctuated wall.
        if not re.search(r"[。！？!?；;]$", chunk):
            chunk += "。" if cjk else "."
        output.extend(_split_long_clause(chunk, limit, language))
    return output


def format_transcript_segments(segments: list[object], language: str = "") -> str:
    """Format Whisper's timed chunks into readable paragraphs.

    Whisper already detects speech regions. Keeping the gaps lets the raw
    transcript resemble a dictation tool without asking a cloud model to
    rewrite the user's words.
    """
    output: list[str] = []
    current = ""
    current_start: Optional[float] = None
    current_end: Optional[float] = None
    for segment in segments:
        text = normalize_transcript(str(getattr(segment, "text", "")))
        if not text:
            continue
        start = float(getattr(segment, "start", 0.0) or 0.0)
        end = float(getattr(segment, "end", start) or start)
        gap = max(0.0, start - current_end) if current_end is not None else 0.0
        if current and gap >= PARAGRAPH_BREAK_SECONDS:
            output.extend(_format_clause(current.strip(), language))
            output.append("")
            current = ""
            current_start = None
        elif current and gap >= PAUSE_BREAK_SECONDS:
            output.extend(_format_clause(current.strip(), language))
            current = ""
            current_start = None

        current = _join_segment_text(current, text)
        if current_start is None:
            current_start = start
        current_end = end

    if current.strip():
        output.extend(_format_clause(current.strip(), language))
    return "\n".join(output).replace("\n\n\n", "\n\n").strip()


def build_audio_api_request(endpoint: str, model: str, api_key: str, audio_path: Path) -> urllib.request.Request:
    """Build an OpenAI-compatible multipart audio transcription request."""
    boundary = "----VoiceToAI" + uuid.uuid4().hex
    audio_bytes = audio_path.read_bytes()
    filename = audio_path.name
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode("utf-8"),
            b"\r\n",
        ])

    field("model", model)
    field("response_format", "json")
    field(
        "prompt",
        "请直接把音频转写成简体中文或原语言文字，保留原意，修正明显识别错误，补充自然标点并按语义分段。只返回最终转写正文。",
    )
    parts.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: audio/wav\r\n\r\n",
        audio_bytes,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    request = urllib.request.Request(
        endpoint.strip(),
        data=b"".join(parts),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {api_key.strip()}",
        },
        method="POST",
    )
    return request


def parse_api_response(data: dict) -> str:
    """Read the common OpenAI-compatible chat completion response shape."""
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("API 没有返回 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("API 没有返回可读文本")
    return content.strip()


def parse_audio_api_response(data: dict) -> str:
    text = data.get("text") if isinstance(data, dict) else None
    if isinstance(text, str) and text.strip():
        return text.strip()
    # Some audio-capable gateways wrap the generated text in a chat-style
    # response even when the request itself is multipart audio.
    try:
        return parse_api_response(data)
    except ValueError as exc:
        raise ValueError("音频 API 没有返回可读转写文本") from exc


def set_windows_app_identity() -> None:
    """Set a stable Windows identity before Tk creates the first window."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VoiceToAI.MVP")
    except (AttributeError, OSError):
        pass


class WindowsShortcutGate:
    MODIFIERS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C}

    def __init__(self, keys, activate):
        generic = {
            keyboard.Key.ctrl: {0x11, 0xA2, 0xA3},
            keyboard.Key.shift: {0x10, 0xA0, 0xA1},
            keyboard.Key.alt: {0x12, 0xA4, 0xA5},
        }
        self.groups = []
        for key in keys:
            if key in generic:
                group = generic[key]
            elif isinstance(key, keyboard.Key):
                group = {key.value.vk}
            elif key.char:
                mapped = ctypes.windll.user32.VkKeyScanW(key.char)
                if mapped == -1:
                    raise ValueError(f"当前键盘布局不支持此快捷键：{key.char}")
                group = {mapped & 0xFF}
            else:
                group = {key.vk}
            self.groups.append(frozenset(group))
        self.activate = activate
        self.allowed_modifiers = set().union(*self.groups) & self.MODIFIERS
        self.pressed = set()
        self.blocked = set()
        self.latched = False

    def handle(self, vk, is_down):
        """Consume the chord's ordinary key, including repeat and key-up.

        Modifiers continue to reach the destination so normal typing and
        unrelated shortcuts work before and after our chord.
        """
        if not is_down:
            consumed = vk in self.blocked
            self.blocked.discard(vk)
            self.pressed.discard(vk)
            if not all(group & self.pressed for group in self.groups):
                self.latched = False
            return consumed
        repeated = vk in self.pressed
        self.pressed.add(vk)
        consumed = vk in self.blocked
        matched = (all(group & self.pressed for group in self.groups)
                   and not (self.pressed & self.MODIFIERS) - self.allowed_modifiers)
        if matched:
            if vk not in self.MODIFIERS:
                self.blocked.add(vk)
                consumed = True
            if not self.latched and not repeated:
                self.latched = True
                self.activate()
        return consumed


class Recorder:
    def __init__(self) -> None:
        self._frames: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._level = 0.0
        self.started_at = 0.0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    @property
    def level(self) -> float:
        with self._lock:
            return self._level

    def _callback(self, indata, frames, time_info, status) -> None:
        if status and sys.stdout is not None:
            print(f"audio status: {status}")
        with self._lock:
            self._frames.append(indata.copy())
            if indata.size:
                rms = float(np.sqrt(np.mean(np.square(indata))))
                self._level = min(1.0, rms * 10.0)

    def start(self, device: Optional[int]) -> None:
        with self._lock:
            self._frames = []
            self._level = 0.0
        self.started_at = time.time()
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            device=device,
            callback=self._callback,
        )
        try:
            stream.start()
        except Exception:
            stream.close()
            raise
        self._stream = stream

    def stop(self) -> Optional[Path]:
        stream, self._stream = self._stream, None
        if stream is None:
            return None
        stream.stop()
        stream.close()
        with self._lock:
            frames = list(self._frames)
            self._level = 0.0
        if not frames:
            return None
        audio = np.concatenate(frames, axis=0)
        RECORD_DIR.mkdir(parents=True, exist_ok=True)
        filename = RECORD_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{int(time.time() * 1000) % 1000:03d}.wav"
        sf.write(filename, audio, SAMPLE_RATE, subtype="PCM_16")
        return filename

    def cancel(self) -> None:
        """Stop recording and discard the captured frames without saving audio."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()
        with self._lock:
            self._frames = []
            self._level = 0.0


def rounded_shape(canvas, x1, y1, x2, y2, radius, **options):
    points = [x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
              x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
              x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


class RoundButton(tk.Canvas):
    def __init__(self, parent, text, command, primary=False, width=124):
        super().__init__(parent, width=width, height=42, bd=0, highlightthickness=0,
                         bg=parent.cget("background"), takefocus=True, cursor="hand2")
        self.text, self.command, self.primary = text, command, primary
        self.hovered, self.enabled = False, True
        self.bind("<Configure>", lambda _: self._draw())
        self.bind("<Enter>", lambda _: self._hover(True))
        self.bind("<Leave>", lambda _: self._hover(False))
        self.bind("<Button-1>", lambda _: self._activate())
        self.bind("<Return>", lambda _: self._activate())
        self.bind("<space>", lambda _: self._activate())
        self.bind("<FocusIn>", lambda _: self._draw())
        self.bind("<FocusOut>", lambda _: self._draw())

    def _activate(self):
        if self.enabled:
            self.command()

    def _hover(self, value):
        self.hovered = value
        self._draw()

    def configure(self, cnf=None, **kwargs):
        if "text" in kwargs:
            self.text = kwargs.pop("text")
        if "state" in kwargs:
            self.enabled = kwargs.pop("state") != "disabled"
        super().configure(cnf, **kwargs)
        if hasattr(self, "text"):
            self._draw()

    config = configure

    def _draw(self):
        self.delete("all")
        width, height = self.winfo_width(), self.winfo_height()
        fill = ("#0e6c5a" if self.hovered else "#147d68") if self.primary else ("#e4ece9" if self.hovered else "#edf2ef")
        if not self.enabled:
            fill = "#e4e9e6"
        rounded_shape(self, 2, 2, width-2, height-2, 14, fill=fill,
                      outline="#147d68" if self.focus_get() == self else fill, width=2)
        self.create_text(width/2, height/2, text=self.text, font=("Microsoft YaHei UI", 10),
                         fill=("#ffffff" if self.primary else "#213a34") if self.enabled else "#6b7974")


class RoundPanel(tk.Canvas):
    def __init__(self, parent):
        super().__init__(parent, bg=parent.cget("background"), bd=0, highlightthickness=0)
        self.body = tk.Frame(self, bg="#ffffff")
        self.body_window = self.create_window(20, 20, window=self.body, anchor="nw")
        self.bind("<Configure>", self._resize)

    def _resize(self, event):
        self.delete("surface")
        shape = rounded_shape(self, 1, 1, event.width-1, event.height-1, 22,
                              fill="#ffffff", outline="#e2e9e5", tags="surface")
        self.tag_lower(shape)
        self.itemconfigure(self.body_window, width=max(1, event.width-40), height=max(1, event.height-40))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Voice to AI · 本地语音输入")
        self.geometry("600x126")
        self.minsize(420, 96)
        self.attributes("-topmost", True)
        self.configure(bg="#f3f6f5")
        self._compact_width = 280
        self._compact_height = 56
        self._collapsed = True
        self._busy = False
        self._job_id = 0
        self._compact_note = ""
        self._compact_anim_after = None
        self._wave_phase = 0.0
        self._compact_is_recording = False
        self._transcription_started_at = 0.0
        self._position_bottom_center()
        icon_path = Path(__file__).with_name("voice-to-ai.ico")
        if icon_path.exists():
            try:
                self.iconbitmap(default=str(icon_path))
            except tk.TclError:
                pass
        self._configure_window_identity(icon_path)

        self.recorder = Recorder()
        self.model: Optional[WhisperModel] = None
        self.model_lock = threading.Lock()
        self.hotkey_listener: Optional[keyboard.Listener] = None
        # A single physical shortcut can produce duplicate activate callbacks
        # on Windows (especially with Ctrl/Alt combinations).  Without a
        # small gate, the duplicate callback starts and immediately stops a
        # recording, which looks like an instant jump to “正在转写…”.
        self._last_hotkey_activation = 0.0
        self.hotkey_var = tk.StringVar(value=self._read_hotkey())
        self.hotkey_key_data = self._read_hotkey_key_data()
        self.target_window = None
        self.target_control = None
        self.target_text_input_ready: Optional[bool] = None
        self.target_window_title = ""
        self._pending_target_window = None
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self.current_audio: Optional[Path] = None
        self.raw_text = tk.StringVar()
        self.status_text = tk.StringVar(value="就绪。按一次开始，再按一次结束")
        self.device_var = tk.StringVar()
        self.model_var = tk.StringVar(value="small · 本地 CPU")
        self.language_var = tk.StringVar(value="语言：自动识别")
        self._device_map: dict[str, int] = {}
        self._lock_handle = None
        self.api_config = self._read_api_config()
        self.tray_icon = None
        self._hidden_to_tray = False
        self._exiting = False
        self._tray_stopping = False
        self.result_popup: Optional[tk.Toplevel] = None
        self._result_popup_escape_binding = None
        self.result_popup_pending = False
        self.last_transcript = ""

        self._build_ui()
        self._own_hwnd = self.winfo_id()
        self.status_text.trace_add("write", self._status_changed)
        self.collapse_window()
        self._load_devices()
        self._start_hotkey()
        self.after(50, self._drain_ui_queue)
        self.after(100, self._drain_worker_queue)
        self.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self.after(200, self._start_tray_icon)

    def _configure_window_identity(self, icon_path: Path) -> None:
        """Bind the branded icon to Tk and the native Windows taskbar window."""
        png_path = icon_path.with_name("voice-to-ai-icon.png")
        if png_path.exists():
            try:
                self._icon_image = tk.PhotoImage(file=str(png_path))
                self.iconphoto(True, self._icon_image)
            except tk.TclError:
                self._icon_image = None
        if os.name != "nt":
            return
        try:
            shell32 = ctypes.windll.shell32
            shell32.SetCurrentProcessExplicitAppUserModelID("VoiceToAI.MVP")
            user32 = ctypes.windll.user32
            user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
            user32.SendMessageW.restype = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            hwnd = user32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            # WM_SETICON, ICON_SMALL and ICON_BIG. Tk's iconbitmap handles
            # loading, while this explicit message updates the taskbar entry.
            wm_seticon = 0x0080
            icon_small, icon_big = 0, 1
            if icon_path.exists():
                user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                user32.LoadImageW.restype = ctypes.c_void_p
                hicon = user32.LoadImageW(None, str(icon_path), 1, 0, 0, 0x10 | 0x40)
                if hicon:
                    user32.SendMessageW(hwnd, wm_seticon, icon_small, hicon)
                    user32.SendMessageW(hwnd, wm_seticon, icon_big, hicon)
        except (AttributeError, OSError, tk.TclError):
            pass

    def _read_hotkey(self) -> str:
        return self._read_settings().get("hotkey", HOTKEY)

    @staticmethod
    def _read_settings() -> dict:
        path = APP_DIR / "settings.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _read_api_config(self) -> dict[str, str]:
        data = self._read_settings().get("api", {})
        if not isinstance(data, dict):
            data = {}
        endpoint = str(data.get("endpoint", "")).strip()
        model = str(data.get("model", "")).strip()
        # Migrate the earlier text-analysis defaults to the audio contract.
        if not endpoint or endpoint.endswith("/v1/chat/completions"):
            endpoint = "https://api.openai.com/v1/audio/transcriptions"
        if not model or model == "gpt-4o-mini":
            model = "gpt-4o-mini-transcribe"
        return {
            "endpoint": endpoint,
            "model": model,
            "api_key": str(data.get("api_key", "")),
        }

    def _read_hotkey_key_data(self) -> Optional[list[dict[str, str]]]:
        return self._read_settings().get("hotkey_keys")

    def _write_hotkey(self, value: str, keys: Optional[list[dict[str, str]]] = None) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = self._read_settings()
        payload["hotkey"] = value
        if keys:
            payload["hotkey_keys"] = keys
        (APP_DIR / "settings.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _write_api_config(self, config: dict[str, str]) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = self._read_settings()
        payload["api"] = config
        (APP_DIR / "settings.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def configure_api(self) -> None:
        """Configure an optional OpenAI-compatible audio transcription endpoint."""
        dialog = tk.Toplevel(self)
        dialog.title("设置 AI 音频转写 API")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("640x300")
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="音频转写 API 地址").grid(row=0, column=0, sticky="w", pady=6)
        endpoint = tk.StringVar(value=self.api_config["endpoint"])
        ttk.Entry(frame, textvariable=endpoint, width=58).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="模型名称").grid(row=1, column=0, sticky="w", pady=6)
        model = tk.StringVar(value=self.api_config["model"])
        ttk.Entry(frame, textvariable=model, width=58).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(frame, text="API Key").grid(row=2, column=0, sticky="w", pady=6)
        api_key = tk.StringVar(value=self.api_config["api_key"])
        ttk.Entry(frame, textvariable=api_key, show="*", width=58).grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Label(
            frame,
            text="可留空：留空时使用本机 Whisper。填写后，录音音频会直接发送给该模型完成识别和整理。",
            foreground="#647572",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 16))

        def save():
            values = {"endpoint": endpoint.get().strip(), "model": model.get().strip(), "api_key": api_key.get().strip()}
            if values["api_key"] and (not values["endpoint"] or not values["model"]):
                messagebox.showwarning("配置不完整", "填写 API Key 时，API 地址和模型名称不能为空。", parent=dialog)
                return
            self.api_config = values
            self._write_api_config(values)
            self.status_text.set("AI 音频转写 API 设置已保存")
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="保存", command=save).pack(side="left", padx=5)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=5)

    def _position_bottom_center(self) -> None:
        self.update_idletasks()
        left, top, right, bottom = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()
        try:
            import ctypes
            from ctypes import wintypes
            area = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):
                left, top, right, bottom = area.left, area.top, area.right, area.bottom
        except (AttributeError, OSError):
            pass
        width, height = self._compact_width, self._compact_height
        x, y = max(left, left+(right-left-width)//2), max(top, bottom-height-16)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.after_idle(lambda: self._set_native_position(x, y))

    def _set_native_position(self, x: int, y: int) -> None:
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, wintypes.UINT]
            hwnd = user32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            user32.SetWindowPos(hwnd, None, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)
        except (AttributeError, OSError, tk.TclError):
            pass

    def _build_ui(self) -> None:
        bg, ink, muted = "#eef2f1", "#152a27", "#5c716c"
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=ink, font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(12, 9), font=("Microsoft YaHei UI", 10))
        style.configure(
            "Voice.TCombobox",
            padding=(12, 9),
            fieldbackground="#f8fbfa",
            background="#f8fbfa",
            foreground="#173832",
            bordercolor="#c8d9d3",
            lightcolor="#c8d9d3",
            darkcolor="#c8d9d3",
            arrowsize=16,
            font=("Microsoft YaHei UI", 10),
        )
        style.map(
            "Voice.TCombobox",
            fieldbackground=[("readonly", "#f8fbfa"), ("focus", "#ffffff")],
            bordercolor=[("focus", "#147d68"), ("readonly", "#c8d9d3")],
            lightcolor=[("focus", "#147d68"), ("readonly", "#c8d9d3")],
            darkcolor=[("focus", "#147d68"), ("readonly", "#c8d9d3")],
        )

        def label(parent, text=None, variable=None, size=10, color=ink, bold=False):
            return tk.Label(parent, text=text, textvariable=variable, bg=parent.cget("background"),
                            fg=color, font=("Microsoft YaHei UI", size, "bold" if bold else "normal"), anchor="w")

        self.ui_root = tk.Frame(self, bg=bg, padx=28, pady=26)
        root = self.ui_root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # Dark utility header gives the workspace a clear starting point and
        # separates product identity from the denser transcription surface.
        self.header = tk.Frame(root, bg="#173832", padx=20, pady=18)
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        self.header.columnconfigure(1, weight=1)
        brand_mark = tk.Label(self.header, bg="#173832")
        if getattr(self, "_icon_image", None):
            self._brand_image = self._icon_image.subsample(8, 8)
            brand_mark.configure(image=self._brand_image)
        brand_mark.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        brand = tk.Frame(self.header, bg="#173832")
        brand.grid(row=0, column=1, rowspan=2, sticky="w")
        tk.Label(brand, text="Voice to AI", bg="#173832", fg="#f5fbf8",
                 font=("Microsoft YaHei UI", 20, "bold"), anchor="w").pack(anchor="w")
        tk.Label(brand, text="本地语音输入工作台", bg="#173832", fg="#b7d0c8",
                 font=("Microsoft YaHei UI", 10), anchor="w").pack(anchor="w", pady=(4, 0))
        header_actions = tk.Frame(self.header, bg="#173832")
        header_actions.grid(row=0, column=2, rowspan=2, sticky="e")
        RoundButton(header_actions, "设置快捷键", self.configure_hotkey, width=126).pack(side="left", padx=4)
        RoundButton(header_actions, "设置 API", self.configure_api, width=100).pack(side="left", padx=4)
        RoundButton(header_actions, "收起浮条", self.collapse_window, width=110).pack(side="left", padx=(4, 0))

        self.controls = RoundPanel(root)
        self.controls.grid(row=1, column=0, sticky="ew", pady=(0, 18))
        self.controls.configure(height=156)
        body = self.controls.body
        body.columnconfigure(1, weight=1)
        body.columnconfigure(3, weight=1)
        tk.Label(body, text="录音控制", bg="#ffffff", fg="#173832",
                 font=("Microsoft YaHei UI", 13, "bold"), anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 22))
        self.record_button = RoundButton(body, "开始录音", self.toggle_recording, primary=True, width=132)
        self.record_button.grid(row=0, column=1, sticky="w")
        self.cancel_button = RoundButton(body, "取消录音", self.cancel_recording, width=104)
        self.cancel_button.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.cancel_button.configure(state="disabled")
        tk.Label(body, text="快捷键", bg="#ffffff", fg=muted,
                 font=("Microsoft YaHei UI", 9), anchor="w").grid(row=0, column=3, sticky="e", padx=(20, 8))
        tk.Label(body, textvariable=self.hotkey_var, bg="#e9f2ee", fg="#17624e",
                 font=("Microsoft YaHei UI", 10, "bold"), padx=12, pady=6).grid(row=0, column=4, sticky="e")
        tk.Label(body, text="麦克风", bg="#ffffff", fg=muted,
                 font=("Microsoft YaHei UI", 9), anchor="w").grid(row=1, column=0, sticky="w", pady=(16, 0))
        self.device_combo = ttk.Combobox(body, textvariable=self.device_var, state="readonly", width=34, style="Voice.TCombobox")
        self.device_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(16, 0))
        tk.Label(body, textvariable=self.model_var, bg="#ffffff", fg=muted,
                 font=("Microsoft YaHei UI", 9), anchor="w").grid(row=1, column=3, sticky="e", pady=(16, 0))
        tk.Label(body, textvariable=self.language_var, bg="#ffffff", fg=muted,
                 font=("Microsoft YaHei UI", 9), anchor="e").grid(row=1, column=4, sticky="e", pady=(16, 0))

        self.panes = tk.Frame(root, bg=bg)
        self.panes.grid(row=2, column=0, sticky="nsew")
        self.panes.columnconfigure(0, weight=1, uniform="text")
        self.panes.columnconfigure(1, weight=1, uniform="text")
        self.panes.rowconfigure(0, weight=1)
        for column, title, subtitle, is_raw in (
            (0, "转写结果", "本地转写后自动分段、补标点，并输入录音前窗口。", True),
            (1, "音频模型结果", "绑定 API 后直接识别录音，结果可复制或自动输入。", False),
        ):
            card = RoundPanel(self.panes)
            card.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if is_raw else (8, 0))
            content = card.body
            heading = tk.Frame(content, bg="#ffffff")
            heading.pack(fill="x")
            label(heading, title, size=14, bold=True).pack(side="left", anchor="w")
            label(heading, "原始" if is_raw else "可选", color="#147d68" if is_raw else muted, size=9, bold=True).pack(side="right", anchor="e")
            label(content, subtitle, color=muted, size=9).pack(anchor="w", pady=(7, 14))
            actions = tk.Frame(content, bg="#ffffff")
            actions.pack(side="bottom", fill="x", pady=(14, 0))
            text_area = tk.Frame(content, bg="#ffffff")
            text_area.pack(fill="both", expand=True)
            box = tk.Text(text_area, wrap="word", undo=True, font=("Microsoft YaHei UI", 11),
                          bg="#ffffff", fg=ink, insertbackground="#147d68", relief="flat", bd=0,
                          padx=0, pady=4, spacing1=4, spacing3=7, highlightthickness=0, width=24, height=8)
            scroll = ttk.Scrollbar(text_area, orient="vertical", command=box.yview)
            box.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            box.pack(side="left", fill="both", expand=True)
            if is_raw:
                self.raw_box = box
                RoundButton(actions, "一键复制", lambda: self.copy_text(self.raw_box.get("1.0", "end-1c")), primary=True, width=110).pack(side="left")
                RoundButton(actions, "输入当前窗口", lambda: self.insert_into_current_window(self.raw_box.get("1.0", "end-1c")), width=126).pack(side="right")
            else:
                self.task_box = box
                RoundButton(actions, "复制 AI 结果", lambda: self.copy_text(self.task_box.get("1.0", "end-1c")), width=124).pack(side="left")

        footer = tk.Frame(root, bg=bg)
        footer.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        footer.columnconfigure(0, weight=1)
        self.status_label = label(footer, variable=self.status_text, color="#3d5b54", size=9)
        self.status_label.configure(wraplength=610)
        self.status_label.grid(row=0, column=0, sticky="w")
        RoundButton(footer, "清理本地记录", self.clear_records, width=126).grid(row=0, column=1, padx=(8, 0))

        self.compact_canvas = tk.Canvas(self, width=self._compact_width, height=self._compact_height,
                                       bg="#ff00ff", highlightthickness=0, bd=0, cursor="hand2", takefocus=True)
        self.compact_canvas.bind("<Button-1>", self._compact_click)
        self.compact_canvas.bind("<Button-3>", self._show_context_menu)
        self.compact_canvas.bind("<Return>", lambda _: self.expand_window())
        self.compact_canvas.bind("<space>", lambda _: self.toggle_recording())
        self.compact_canvas.bind("<Escape>", lambda _: self.cancel_recording())
        self.compact_canvas.bind("<Motion>", self._compact_hover)
        self.compact_canvas.bind("<Leave>", lambda _: self._draw_compact())

    def _load_devices(self) -> None:
        try:
            devices = sd.query_devices()
            names: list[str] = []
            for idx, device in enumerate(devices):
                if int(device.get("max_input_channels", 0)) > 0:
                    label = f"{idx}: {device['name']}"
                    names.append(label)
                    self._device_map[label] = idx
            self.device_combo["values"] = names
            default_index = sd.default.device[0]
            default_label = next((name for name in names if name.startswith(f"{default_index}:")), names[0] if names else "")
            self.device_var.set(default_label)
        except Exception as exc:
            self.status_text.set(f"无法读取麦克风：{exc}")

    def _start_hotkey(self) -> None:
        try:
            keys = self._deserialize_hotkey_keys(self.hotkey_key_data) if self.hotkey_key_data else self._parse_hotkey_keys(self.hotkey_var.get())
            def activate():
                now = time.monotonic()
                if now - self._last_hotkey_activation < 0.30:
                    return
                self._last_hotkey_activation = now
                # Read the foreground window while the user's AI input still
                # owns focus. The floating pill is intentionally topmost and
                # may otherwise become the foreground window by the time the
                # Tk callback runs.
                target = self._foreground_window_handle()
                self._post_ui(lambda: self._toggle_from_hotkey(target))

            hotkey = keyboard.HotKey(keys, activate)

            def on_press(key):
                hotkey.press(self._canonical_hotkey_key(key, self.hotkey_listener))

            def on_release(key):
                hotkey.release(self._canonical_hotkey_key(key, self.hotkey_listener))

            if os.name == "nt":
                gate = WindowsShortcutGate(keys, activate)
                listener = None

                def event_filter(msg, data):
                    # Our synthetic paste keys must not alter the physical
                    # shortcut state or trigger a user-bound Ctrl+V chord.
                    if data.flags & 0x10:
                        return False
                    if msg in (0x100, 0x104, 0x101, 0x105):
                        if gate.handle(data.vkCode, msg in (0x100, 0x104)):
                            # Returning False only hides the event from pynput;
                            # suppress_event also stops delivery to other apps.
                            listener.suppress_event()
                    return False

                listener = keyboard.Listener(win32_event_filter=event_filter)
                self.hotkey_listener = listener
            else:
                self.hotkey_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
            self.after(80, self._check_hotkey_listener)
        except Exception as exc:
            self.hotkey_listener = None
            self.status_text.set(f"快捷键无效：{self.hotkey_var.get()}（{exc}）")
            raise

    def _post_ui(self, callback: Callable[[], None]) -> None:
        """Queue work from pynput/pystray threads for the Tk main thread."""
        if not self._exiting:
            self.ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                callback = self.ui_queue.get_nowait()
                try:
                    callback()
                except (tk.TclError, RuntimeError) as exc:
                    if not self._exiting:
                        self.status_text.set(f"界面恢复失败：{exc}")
        except queue.Empty:
            pass
        if not self._exiting:
            self.after(50, self._drain_ui_queue)

    @staticmethod
    def _foreground_window_handle():
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            return user32.GetForegroundWindow()
        except (AttributeError, OSError):
            return None

    def _toggle_from_hotkey(self, target_window) -> None:
        if self.recorder.recording:
            self.stop_recording()
            return
        if self._busy:
            # The same shortcut is a true toggle throughout the whole job:
            # while an API/local transcription is running, pressing it again
            # cancels the job instead of leaving the pill flashing forever.
            self.cancel_recording()
            return
        if target_window:
            self._pending_target_window = target_window

        was_unavailable = self.state() in ("iconic", "withdrawn") or self._hidden_to_tray
        if was_unavailable:
            self._restore_hidden_window(show_main=False)
        elif not self._collapsed:
            self.collapse_window()
        self.start_recording()

    def _restore_hidden_window(self, show_main: bool) -> None:
        """Restore a withdrawn tray window before changing its Tk layout."""
        self._hidden_to_tray = False
        if show_main:
            try:
                if self.state() == "withdrawn":
                    self.deiconify()
            except tk.TclError:
                return
            self.expand_window()
            self.lift()
            try:
                self.focus_force()
            except tk.TclError:
                pass
        else:
            # Set the pill layout and non-activating window style before it is
            # shown, so the withdrawn main-window geometry never flashes.
            self.collapse_window()
            try:
                if self.state() == "withdrawn":
                    self.deiconify()
                self._make_compact_nonactivating()
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
                ctypes.windll.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            except (AttributeError, OSError, tk.TclError):
                pass

    def _check_hotkey_listener(self) -> None:
        if self.hotkey_listener is None or not self.hotkey_listener.is_alive():
            self.status_text.set(f"快捷键监听失败：{self.hotkey_var.get()}，请重新设置")
        else:
            self.status_text.set(f"快捷键已生效：{self.hotkey_var.get()}")

    @staticmethod
    def _pynput_hotkey(value: str) -> str:
        parts = [part.strip().lower() for part in value.split("+") if part.strip()]
        mapped = {
            "left ctrl": "<ctrl_l>", "right ctrl": "<ctrl_r>",
            "left alt": "<alt_l>", "right alt": "<alt_r>", "alt_gr": "<alt_r>", "alt gr": "<alt_r>",
            "left shift": "<shift_l>", "right shift": "<shift_r>",
            "ctrl": "<ctrl>", "control": "<ctrl>", "alt": "<alt>",
            "shift": "<shift>", "space": "<space>",
        }
        return "+".join(mapped.get(part, part) for part in parts)

    @staticmethod
    def _validate_hotkey(value: str) -> str:
        App._parse_hotkey_keys(value)
        return App._pynput_hotkey(value)

    @staticmethod
    def _serialize_hotkey_key(key) -> dict[str, str]:
        if isinstance(key, keyboard.Key):
            return {"kind": "special", "value": key.name}
        if isinstance(key, keyboard.KeyCode):
            if key.char:
                return {"kind": "char", "value": key.char.lower()}
            if key.vk is not None:
                return {"kind": "vk", "value": str(key.vk)}
        raise ValueError(f"无法保存此键位：{key!r}")

    @staticmethod
    def _deserialize_hotkey_keys(data: list[dict[str, str]]) -> list[object]:
        keys = []
        for item in data:
            kind, value = item.get("kind"), item.get("value", "")
            if kind == "special":
                key = getattr(keyboard.Key, value, None)
                if key is None:
                    raise ValueError(f"此电脑不支持保存的按键：{value}")
                keys.append(key)
            elif kind == "char" and value:
                keys.append(keyboard.KeyCode.from_char(value))
            elif kind == "vk":
                keys.append(keyboard.KeyCode.from_vk(int(value)))
            else:
                raise ValueError(f"快捷键配置无效：{item}")
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("快捷键为空或包含重复按键")
        return keys

    @staticmethod
    def _parse_hotkey_keys(value: str) -> list[object]:
        special = {
            "ctrl": keyboard.Key.ctrl, "control": keyboard.Key.ctrl,
            "left ctrl": keyboard.Key.ctrl_l, "right ctrl": keyboard.Key.ctrl_r,
            "alt": keyboard.Key.alt, "left alt": keyboard.Key.alt_l,
            "right alt": keyboard.Key.alt_gr, "alt gr": keyboard.Key.alt_gr,
            "alt_gr": keyboard.Key.alt_gr,
            "shift": keyboard.Key.shift, "left shift": keyboard.Key.shift_l,
            "right shift": keyboard.Key.shift_r, "space": keyboard.Key.space,
            "enter": keyboard.Key.enter, "tab": keyboard.Key.tab,
            "esc": keyboard.Key.esc, "escape": keyboard.Key.esc,
            "backspace": keyboard.Key.backspace, "delete": keyboard.Key.delete,
        }
        keys = []
        for part in (part.strip().lower() for part in value.split("+") if part.strip()):
            if part in special:
                keys.append(special[part])
            elif len(part) == 1:
                keys.append(keyboard.KeyCode.from_char(part))
            elif part.startswith("f") and part[1:].isdigit():
                key = getattr(keyboard.Key, part, None)
                if key is None:
                    raise ValueError(f"不支持的按键：{part}")
                keys.append(key)
            else:
                key = getattr(keyboard.Key, part, None)
                if key is None:
                    raise ValueError(f"不支持的按键：{part}")
                keys.append(key)
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("快捷键为空或包含重复按键")
        return keys

    @staticmethod
    def _canonical_hotkey_key(key, listener):
        if key == keyboard.Key.alt_gr:
            return keyboard.Key.alt_gr
        if listener is not None:
            return listener.canonical(key)
        return key

    def configure_hotkey(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("设置快捷键")
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("520x250")
        dialog.resizable(False, False)
        ttk.Label(dialog, text="点击‘开始录入’，然后按下你想使用的组合键").pack(padx=18, pady=(16, 6))
        value = tk.StringVar(value=self.hotkey_var.get())
        entry = ttk.Entry(dialog, textvariable=value, width=28, state="readonly")
        entry.pack(padx=18, pady=6)
        recording = {"active": False, "keys": {}, "listener": None}

        def format_key(key):
            if isinstance(key, keyboard.KeyCode):
                if key.char:
                    return key.char.upper()
                if key.vk is not None:
                    return f"VK {key.vk:02X}"
                return "未知按键"
            names = {
                keyboard.Key.ctrl_l: "Left Ctrl", keyboard.Key.ctrl_r: "Right Ctrl",
                keyboard.Key.alt_l: "Left Alt", keyboard.Key.alt_r: "Right Alt",
                keyboard.Key.shift_l: "Left Shift", keyboard.Key.shift_r: "Right Shift",
                keyboard.Key.space: "Space",
            }
            value = names.get(key, str(key).replace("Key.", "").title())
            return "Right Alt" if value.lower() in {"alt gr", "alt_gr"} else value

        def on_press(key):
            if not recording["active"]:
                return
            key = capture_listener.canonical(key)
            name = format_key(key)
            if name:
                recording["keys"][key] = name
                display = " + ".join(recording["keys"].values())
                dialog.after(0, lambda: value.set(display))

        def on_release(key):
            if not recording["active"] or not recording["keys"]:
                return
            # Once a captured chord starts releasing, freeze it for review.
            recording["active"] = False
            dialog.after(0, lambda: dialog.status_label.configure(text="已捕获组合键；如需重录请点击开始录入"))

        def begin_capture():
            recording["active"] = True
            recording["keys"].clear()
            value.set("请按键…")
            dialog.status_label.configure(text="正在监听按键，按下组合键后点击保存")

        dialog.status_label = ttk.Label(dialog, text="当前快捷键可区分左右 Ctrl / Alt / Shift")
        dialog.status_label.pack(padx=18, pady=(4, 8))
        capture_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        capture_listener.daemon = True
        capture_listener.start()

        def save():
            raw = value.get().strip()
            recording["active"] = False
            if not raw or raw == "请按键…":
                messagebox.showwarning("快捷键无效", "请先点击开始录入，再按下快捷键。", parent=dialog)
                return
            names = {part.strip().lower() for part in raw.split("+")}
            modifiers = {"left ctrl", "right ctrl", "ctrl", "control", "left alt", "right alt", "alt", "alt gr", "left shift", "right shift", "shift"}
            if names.issubset(modifiers):
                messagebox.showwarning("快捷键无效", "不能只使用 Alt、Ctrl 或 Shift，至少再按一个普通键，例如 Right Alt+Space。", parent=dialog)
                return
            try:
                key_data = [self._serialize_hotkey_key(key) for key in recording["keys"]]
                candidate_keys = self._deserialize_hotkey_keys(key_data)
                keyboard.HotKey(candidate_keys, lambda: None)
                old_raw = self.hotkey_var.get()
                old_key_data = self.hotkey_key_data
                if self.hotkey_listener:
                    self.hotkey_listener.stop()
                captured_keys = list(recording["keys"].keys())
                if not captured_keys:
                    raise ValueError("没有捕获到按键，请重新录入")
                self.hotkey_var.set(raw)
                self.hotkey_key_data = key_data
                self._start_hotkey()
                if self.hotkey_listener is None or not self.hotkey_listener.is_alive():
                    raise RuntimeError(f"系统未能注册快捷键：{raw}")
                self._write_hotkey(raw, key_data)
                self.status_text.set(f"快捷键已保存：{raw}")
                capture_listener.stop()
                dialog.destroy()
            except Exception as exc:
                self.hotkey_var.set(locals().get("old_raw", self._read_hotkey()))
                self.hotkey_key_data = locals().get("old_key_data", self._read_hotkey_key_data())
                try:
                    self._start_hotkey()
                except Exception:
                    pass
                messagebox.showerror("快捷键设置失败", str(exc), parent=dialog)

        buttons = ttk.Frame(dialog)
        buttons.pack(pady=(6, 16))
        ttk.Button(buttons, text="开始录入", width=12, command=begin_capture).pack(side="left", padx=5)
        ttk.Button(buttons, text="保存", width=12, command=save).pack(side="left", padx=5)
        ttk.Button(buttons, text="取消", width=12, command=lambda: (capture_listener.stop(), dialog.destroy())).pack(side="left", padx=5)

    def toggle_recording(self) -> None:
        self.stop_recording() if self.recorder.recording else self.start_recording()

    def start_recording(self) -> None:
        if self.recorder.recording or self._busy:
            return
        self._close_result_popup()
        label = self.device_var.get()
        device = self._device_map.get(label)
        try:
            self.raw_box.delete("1.0", "end")
            self.task_box.delete("1.0", "end")
            pending_target = self._pending_target_window
            self._pending_target_window = None
            self.target_window = None
            self.target_control = None
            self.target_text_input_ready = None
            self.target_window_title = ""
            if pending_target:
                self._remember_target_window(pending_target)
            else:
                self.capture_target_window()
            self.recorder.start(device)
            self._job_id += 1
            self.current_audio = None
            self._compact_note = ""
            self.record_button.configure(text="停止录音")
            self.cancel_button.configure(text="取消录音", state="normal")
            self.status_text.set("正在录音… 再按一次快捷键结束")
            self._compact_is_recording = True
            self._draw_compact()
            self._start_compact_animation()
        except Exception as exc:
            self.status_text.set(f"录音失败：{exc}")
            messagebox.showerror("无法录音", str(exc))

    def stop_recording(self) -> None:
        if not self.recorder.recording:
            return
        try:
            audio_path = self.recorder.stop()
            self.record_button.configure(text="开始录音")
            self.cancel_button.configure(state="disabled")
            self._compact_is_recording = False
            self._stop_compact_animation()
            self._draw_compact()
            if not audio_path:
                self.record_button.configure(text="开始录音", state="normal")
                self.cancel_button.configure(state="disabled")
                self.status_text.set("没有采集到音频")
                return
            self.current_audio = audio_path
            self._busy = True
            self._transcription_started_at = time.monotonic()
            self.record_button.configure(text="正在转写…", state="disabled")
            self.cancel_button.configure(text="取消输入", state="normal")
            self.status_text.set("正在本地转写… 再按快捷键可取消")
            self._start_compact_animation()
            self._start_transcription(audio_path, self._job_id)
        except Exception as exc:
            self._compact_is_recording = False
            self._stop_compact_animation()
            self.record_button.configure(text="开始录音", state="normal")
            self.cancel_button.configure(state="disabled")
            self._draw_compact()
            self.status_text.set(f"停止录音失败：{exc}")

    def cancel_recording(self) -> None:
        """Cancel the current capture without transcribing or inserting it."""
        if not self.recorder.recording and not self._busy:
            return
        try:
            self._job_id += 1
            self.recorder.cancel()
            if self._busy and self.current_audio:
                try:
                    self.current_audio.unlink(missing_ok=True)
                except OSError:
                    pass
            self._busy = False
            self.current_audio = None
            self._transcription_started_at = 0.0
            self.record_button.configure(text="开始录音", state="normal")
            self.cancel_button.configure(text="取消录音", state="disabled")
            self._compact_is_recording = False
            self._stop_compact_animation()
            self._draw_compact()
            self.status_text.set("已取消录音，没有输入到 AI")
        except Exception as exc:
            self.status_text.set(f"取消录音失败：{exc}")

    def _load_model(self, progress: Optional[Callable[[str], None]] = None) -> WhisperModel:
        with self.model_lock:
            if self.model is None:
                if progress:
                    progress("正在加载本地模型… 首次加载可能需要稍等")
                candidates = (portable_model_directory(), APP_DIR / "models" / "small")
                local_model = next((path for path in candidates if complete_local_model(path)), None)
                if local_model is not None:
                    self.model = WhisperModel(str(local_model), device="cpu", compute_type="int8", local_files_only=True)
                else:
                    from faster_whisper.utils import download_model
                    # Resolve only the cache first; a local run should not need a Hub request.
                    try:
                        cached = Path(download_model("small", local_files_only=True))
                        if not complete_local_model(cached):
                            cached = None
                    except Exception:
                        cached = None
                    if cached is None:
                        if progress:
                            progress("首次准备模型：正在下载 small（约 0.5 GB）… 可按快捷键取消本次输入")
                        try:
                            cached = Path(download_model("small"))
                            if not complete_local_model(cached):
                                raise LocalModelUnavailable("下载的模型文件不完整")
                        except Exception as exc:
                            raise LocalModelUnavailable(
                                "本地模型尚未准备好，首次下载失败。\n\n"
                                "本地转写不需要 API，但需要 Whisper 模型文件。\n"
                                "请使用含模型的“离线完整包”，完整解压后打开 VoiceToAI.exe；\n"
                                "或将 small 模型的完整文件放到程序旁的 models\\small 文件夹。\n\n"
                                "本次录音已保留在本机。使用本地模式不需要音频 API。"
                            ) from exc
                    self.model = WhisperModel(str(cached), device="cpu", compute_type="int8", local_files_only=True)
            return self.model

    def _transcribe_with_api(self, audio_path: Path, job_id: int) -> None:
        config = dict(self.api_config)
        if not config.get("api_key"):
            return
        try:
            request = build_audio_api_request(config["endpoint"], config["model"], config["api_key"], audio_path)
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
            text = parse_audio_api_response(data)
            language = "zh" if _has_cjk(text) else "unknown"
            text = to_simplified_chinese(text, language)
            text = normalize_recognized_terms(text, language)
            self.worker_queue.put(("api_transcribed", (job_id, text, language)))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            self.worker_queue.put(("api_error", (job_id, str(exc))))

    def _transcribe_worker(self, audio_path: Path, job_id: int) -> None:
        try:
            progress = lambda status: self.worker_queue.put(("model_progress", (job_id, status)))
            model = self._load_model(progress)
            progress("正在本地转写… 再按快捷键可取消")
            # Read WAV ourselves so this MVP does not depend on PyAV's optional
            # metadata argument compatibility. Whisper accepts a float array.
            audio, _ = sf.read(str(audio_path), dtype="float32")
            segments, info = model.transcribe(
                audio, language=None, vad_filter=True, beam_size=5
            )
            # Materialize the generator before formatting so Whisper's timing
            # gaps are retained for readable pauses and paragraph breaks.
            segment_list = list(segments)
            language = info.language or ""
            text = format_transcript_segments(segment_list, language)
            text = to_simplified_chinese(text, language)
            text = normalize_recognized_terms(text, language)
            self.worker_queue.put(("transcribed", (job_id, text, language or "unknown")))
        except Exception as exc:
            self.worker_queue.put(("error", (job_id, str(exc))))

    def _start_transcription(self, audio_path: Path, job_id: int) -> None:
        if not audio_path or not audio_path.exists():
            self.worker_queue.put(("error", (job_id, "录音文件不存在，无法转写")))
            return
        if self.api_config.get("api_key"):
            self.status_text.set("正在将音频发送到 AI 模型转写… 再按快捷键可取消")
            threading.Thread(target=self._transcribe_with_api, args=(audio_path, job_id), daemon=True).start()
        else:
            threading.Thread(target=self._transcribe_worker, args=(audio_path, job_id), daemon=True).start()

    def _drain_worker_queue(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if payload[0] != self._job_id:
                    continue
                if kind == "model_progress":
                    self.status_text.set(str(payload[1]))
                    self._draw_compact()
                    continue
                self._busy = False
                self._transcription_started_at = 0.0
                self._stop_compact_animation()
                self.record_button.configure(text="开始录音", state="normal")
                self.cancel_button.configure(text="取消录音", state="disabled")
                self._draw_compact()
                if kind == "transcribed":
                    _, raw, language = payload
                    raw = str(raw)
                    self.language_var.set(f"语言：{language}")
                    if not raw.strip():
                        self.status_text.set("没有检测到有效语音，没有输入到 AI")
                        continue
                    self.raw_box.delete("1.0", "end")
                    self.raw_box.insert("1.0", raw)
                    self.task_box.delete("1.0", "end")
                    self._save_history(raw, "", language)
                    inserted = self.insert_into_target(raw)
                    if inserted:
                        self.status_text.set("完成。已输入本地转写；未自动发送")
                    else:
                        self.status_text.set("完成。未检测到可输入光标；已复制并打开一键复制窗口")
                        self.show_transcript_popup(raw)
                elif kind == "api_transcribed":
                    _, raw, language = payload
                    raw = str(raw)
                    self.language_var.set(f"语言：{language}")
                    self.raw_box.delete("1.0", "end")
                    self.raw_box.insert("1.0", raw)
                    self.task_box.delete("1.0", "end")
                    self.task_box.insert("1.0", raw)
                    self._save_history(raw, raw, language)
                    inserted = self.insert_into_target(raw)
                    if inserted:
                        self.status_text.set("AI 模型转写完成，已输入当前 AI 对话框；未自动发送")
                    else:
                        self.status_text.set("AI 模型转写完成；未检测到可输入光标，已打开复制窗口")
                        self.show_transcript_popup(raw, ai_result=True)
                elif kind == "api_error":
                    self.status_text.set(f"AI 音频转写失败，正在回退到本地转写：{payload[1]}")
                    self._busy = True
                    self._transcription_started_at = time.monotonic()
                    self.record_button.configure(text="正在本地转写…", state="disabled")
                    self.cancel_button.configure(text="取消输入", state="normal")
                    self._start_compact_animation()
                    threading.Thread(target=self._transcribe_worker,
                                     args=(self.current_audio, self._job_id), daemon=True).start()
                else:
                    self.status_text.set(f"转写失败：{payload[1]}")
                    messagebox.showerror("转写失败", str(payload[1]))
        except queue.Empty:
            pass
        self.after(100, self._drain_worker_queue)

    def _save_history(self, raw: str, task: str, language: str = "") -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        record = Record(datetime.now().isoformat(timespec="seconds"), str(self.current_audio or ""), raw, task, language)
        with HISTORY_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.__dict__, ensure_ascii=False) + "\n")

    def copy_text(self, text: str) -> None:
        if not text.strip():
            self.status_text.set("没有可复制的内容")
            return
        pyperclip.copy(text)
        self.status_text.set("已复制到剪贴板")

    def _close_result_popup(self) -> None:
        popup = self.result_popup
        self.result_popup = None
        self.result_popup_pending = False
        if self._result_popup_escape_binding:
            try:
                self.unbind_all("<Escape>")
            except tk.TclError:
                pass
            self._result_popup_escape_binding = None
        if popup is not None:
            try:
                popup.destroy()
            except tk.TclError:
                pass

    def _copy_result_popup(self, text: str) -> None:
        if self.result_popup_pending:
            self.status_text.set("AI 整理仍在进行，请稍候")
            return
        self.copy_text(text)
        self.status_text.set("已复制结果；切回输入框后按 Ctrl+V")
        self._close_result_popup()

    def show_transcript_popup(self, text: str, pending: bool = False, ai_result: bool = False) -> None:
        """Show Voice to AI's copy fallback when automatic paste is unavailable."""
        text = str(text or "").strip()
        if not text:
            return
        self._close_result_popup()
        self.last_transcript = text
        self.result_popup_pending = pending

        from tkinter import font as tkfont
        width = 460
        transcript_font = tkfont.Font(family="Microsoft YaHei UI", size=10)
        max_line_width = width - 48
        lines: list[str] = []
        for paragraph in text.splitlines() or [text]:
            line = ""
            for char in paragraph:
                candidate = line + char
                if line and transcript_font.measure(candidate) > max_line_width:
                    lines.append(line)
                    line = char
                else:
                    line = candidate
            lines.append(line)
        if len(lines) > 7:
            lines = lines[:7]
            lines[-1] = lines[-1].rstrip("。！？!?，, ") + "…"
        display_text = "\n".join(lines)
        height = max(148, 112 + len(lines) * 20)

        popup = tk.Toplevel(self)
        self.result_popup = popup
        popup.overrideredirect(True)
        popup.configure(bg="#ff00ff")
        popup.attributes("-topmost", True)
        try:
            popup.attributes("-transparentcolor", "#ff00ff")
        except tk.TclError:
            pass
        canvas = tk.Canvas(popup, width=width, height=height, bg="#ff00ff",
                           highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)
        rounded_shape(canvas, 2, 2, width - 2, height - 2, 18,
                      fill="#173832", outline="#173832")
        canvas.create_oval(20, 20, 38, 38, fill="#39b98a", outline="#39b98a")
        canvas.create_text(29, 29, text="✓", fill="#173832",
                           font=("Segoe UI", 10, "bold"))
        title = "Voice to AI · AI 结果已就绪" if ai_result else "Voice to AI · 转写已保留"
        canvas.create_text(52, 29, text=title, anchor="w",
                           fill="#f5fbf8", font=("Microsoft YaHei UI", 11, "bold"))
        canvas.create_text(width - 32, 29, text="×", fill="#b7d0c8",
                           font=("Segoe UI", 18), tags="close")
        canvas.create_text(24, 56, text=display_text, anchor="nw",
                           width=max_line_width, fill="#f5fbf8",
                           font=transcript_font)
        footer = "AI 整理完成后可复制。" if pending else "复制后切回 AI 输入框粘贴。"
        canvas.create_text(24, height - 25,
                           text=footer,
                           anchor="w", width=width - 158, fill="#b7d0c8",
                           font=("Microsoft YaHei UI", 8))
        copy_button = tk.Button(
            popup,
            text="整理中…" if pending else ("复制 AI 结果" if ai_result else "复制转写"),
            command=lambda: self._copy_result_popup(text),
            relief="flat",
            bd=0,
            bg="#39b98a",
            activebackground="#59c99f",
            fg="#173832",
            activeforeground="#173832",
            font=("Microsoft YaHei UI", 9, "bold"),
            cursor="hand2",
            padx=10,
            pady=3,
        )
        if pending:
            copy_button.configure(state="disabled", bg="#2b6658", fg="#b7d0c8", cursor="arrow")
        copy_button.place(x=width - 110, y=height - 45, width=86, height=32)

        def close_on_canvas(event):
            if event.x >= width - 58 and event.y <= 52:
                self._close_result_popup()

        canvas.bind("<Button-1>", close_on_canvas)
        # Use a temporary application-wide binding so Esc still closes the
        # fallback when a child canvas/button owns focus.
        self._result_popup_escape_binding = self.bind_all(
            "<Escape>", lambda _event: (self._close_result_popup(), "break")[1], add="+"
        )
        popup.bind("<Escape>", lambda _event: self._close_result_popup())
        popup.protocol("WM_DELETE_WINDOW", self._close_result_popup)
        popup.update_idletasks()
        left, top, right, bottom = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()
        try:
            from ctypes import wintypes
            area = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):
                left, top, right, bottom = area.left, area.top, area.right, area.bottom
        except (AttributeError, OSError):
            pass
        x = max(left + 8, left + (right - left - width) // 2)
        y = max(top + 8, bottom - height - 86)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.lift()

    def paste_to_active_window(self) -> None:
        text = self.raw_box.get("1.0", "end-1c")
        if not text.strip():
            self.status_text.set("没有可粘贴的内容")
            return
        self.copy_text(text)
        self.status_text.set("已复制；请切回 AI 输入框并按 Ctrl+V")

    def insert_into_current_window(self, text: Optional[str] = None) -> bool:
        """Use the currently focused external window for a deliberate retry."""
        try:
            hwnd = self._foreground_window_handle()
            if hwnd:
                self._remember_target_window(hwnd)
        except Exception:
            pass
        return self.insert_into_target(text)

    def capture_target_window(self) -> None:
        """Remember the foreground window before this app takes focus."""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            self._remember_target_window(user32.GetForegroundWindow())
        except Exception:
            self.target_window = None
            self.target_control = None
            self.target_text_input_ready = None
            self.target_window_title = ""

    def _remember_target_window(self, hwnd) -> None:
        """Remember a top-level target and its currently focused child control."""
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        own_window = user32.GetParent(self._own_hwnd) or self._own_hwnd
        internal_windows = {self._own_hwnd, own_window, self.winfo_id()}
        if self.result_popup is not None:
            try:
                internal_windows.add(self.result_popup.winfo_id())
            except tk.TclError:
                pass
        internal_windows.update(
            user32.GetAncestor(candidate, 2) or candidate
            for candidate in tuple(internal_windows)
            if candidate
        )
        if not hwnd or hwnd in internal_windows or (user32.GetAncestor(hwnd, 2) or hwnd) in internal_windows:
            return
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        if not user32.IsWindow(hwnd):
            return
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        target_control = None
        gui_info = None

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]

        gui_info = GUITHREADINFO()
        gui_info.cbSize = ctypes.sizeof(gui_info)
        if thread_id and user32.GetGUIThreadInfo(thread_id, ctypes.byref(gui_info)):
            target_control = gui_info.hwndFocus
        self.target_window = hwnd
        self.target_control = target_control
        self.target_text_input_ready = self._detect_text_input_focus(user32, target_control, gui_info)
        self.target_window_title = title.value

    @staticmethod
    def _detect_text_input_focus(user32, focus_hwnd, gui_info) -> Optional[bool]:
        """Return whether the foreground window has a focused text caret.

        Native edit controls are direct evidence. Browser content controls often
        share a renderer HWND, so the system caret reported by GUITHREADINFO is
        also used. Unknown controls remain uncertain and trigger the copy UI.
        """
        import ctypes
        from ctypes import wintypes
        if not focus_hwnd:
            return None
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(focus_hwnd, class_name, len(class_name))
        name = class_name.value.lower()
        if name == "edit" or name.startswith("richedit"):
            return True
        if gui_info is not None and gui_info.hwndCaret and (gui_info.flags & 0x0001):
            return True
        non_editable = {
            "button", "static", "combobox", "listbox", "syslistview32",
            "systreeview32", "toolbarwindow32",
        }
        if name in non_editable:
            return False
        return None

    def insert_into_target(self, text: Optional[str] = None) -> bool:
        """Paste into a focused text field; uncertain focus uses the copy popup."""
        text = text if text is not None else self.raw_box.get("1.0", "end-1c")
        if not text.strip():
            self.status_text.set("没有可输入的内容")
            return False
        self.copy_text(text)
        if not self.target_window:
            self.status_text.set("已复制；未找到录音前窗口")
            return False
        if self.target_text_input_ready is False:
            self.status_text.set("已复制；录音前窗口没有输入光标")
            return False
        attached_threads = []
        inserted = False
        try:
            import ctypes
            from ctypes import wintypes
            import time as _time
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.IsWindow.argtypes = [wintypes.HWND]
            user32.IsWindow.restype = wintypes.BOOL
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
            user32.AttachThreadInput.restype = wintypes.BOOL
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.BringWindowToTop.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetFocus.argtypes = [wintypes.HWND]
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.IsIconic.restype = wintypes.BOOL
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            if not user32.IsWindow(self.target_window):
                raise RuntimeError("录音前的目标窗口已经关闭")
            foreground = user32.GetForegroundWindow()
            current_thread = kernel32.GetCurrentThreadId()
            foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
            target_thread = user32.GetWindowThreadProcessId(self.target_window, None)
            if foreground_thread:
                if user32.AttachThreadInput(current_thread, foreground_thread, True):
                    attached_threads.append(foreground_thread)
            if target_thread and target_thread != foreground_thread:
                if user32.AttachThreadInput(current_thread, target_thread, True):
                    attached_threads.append(target_thread)
            user32.GetFocus.restype = wintypes.HWND
            focused_control = user32.GetFocus()
            if focused_control and user32.IsWindow(focused_control):
                self.target_control = focused_control
            # SW_RESTORE also unmaximizes a maximized window. Restore only if
            # the user actually minimized the destination to the taskbar.
            if user32.IsIconic(self.target_window):
                user32.ShowWindow(self.target_window, 9)
            user32.BringWindowToTop(self.target_window)
            user32.SetForegroundWindow(self.target_window)
            deadline = _time.time() + 1.0
            actual_foreground = user32.GetForegroundWindow()
            while actual_foreground != self.target_window and _time.time() < deadline:
                _time.sleep(0.04)
                actual_foreground = user32.GetForegroundWindow()
            if actual_foreground != self.target_window:
                raise RuntimeError("Windows 没有将原对话窗口切到前台")
            if self.target_control and user32.IsWindow(self.target_control):
                user32.SetFocus(self.target_control)
            # AttachThreadInput resets keyboard state. Detach before injecting
            # a paste chord so destination input is processed independently.
            for thread_id in attached_threads:
                user32.AttachThreadInput(current_thread, thread_id, False)
            attached_threads.clear()
            deadline = _time.monotonic() + 0.8
            while any(user32.GetAsyncKeyState(vk) & 0x8000
                      for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
                if _time.monotonic() >= deadline:
                    raise RuntimeError("请松开快捷键后手动粘贴")
                _time.sleep(0.02)
            # Native edit controls can paste synchronously through WM_PASTE;
            # browser/Electron renderers need keyboard input instead.
            class_name = ctypes.create_unicode_buffer(256)
            if self.target_control and user32.IsWindow(self.target_control):
                user32.GetClassNameW(self.target_control, class_name, len(class_name))
            if class_name.value.lower() in {"edit", "richedit20w", "richedit50w"}:
                user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT,
                    wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                    ctypes.POINTER(ctypes.c_size_t)]
                user32.SendMessageTimeoutW.restype = wintypes.LPARAM
                result = ctypes.c_size_t()
                if not user32.SendMessageTimeoutW(self.target_control, 0x0302,
                        0, 0, 0x0002, 500, ctypes.byref(result)):
                    raise RuntimeError("输入框未响应粘贴请求")
            else:
                self._send_ctrl_v()
            self.status_text.set(f"已输入到：{self.target_window_title or '录音前窗口'}；未自动发送")
            # Browser and Electron editors often expose an unknown renderer
            # control even when the caret is active. Once Windows accepted
            # the foreground switch and Ctrl+V was sent, treat that attempt
            # as successful; explicit non-editable controls were rejected
            # before reaching this path.
            inserted = self.target_text_input_ready is not False
        except Exception as exc:
            self.status_text.set(f"已复制；自动输入失败，请按 Ctrl+V（{exc}）")
        finally:
            try:
                import ctypes
                from ctypes import wintypes
                ctypes.windll.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
                current_thread = ctypes.windll.kernel32.GetCurrentThreadId()
                for thread_id in attached_threads:
                    ctypes.windll.user32.AttachThreadInput(current_thread, thread_id, False)
            except Exception:
                pass
        return inserted

    @staticmethod
    def _send_ctrl_v() -> None:
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

        keyup = 0x0002
        send_input = ctypes.windll.user32.SendInput
        send_input.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        send_input.restype = wintypes.UINT

        def send(vk, flags=0):
            event = INPUT(type=1, ki=KEYBDINPUT(vk, 0, flags, 0, 0))
            array = (INPUT * 1)(event)
            if send_input(1, array, ctypes.sizeof(INPUT)) != 1:
                raise ctypes.WinError()

        # Separate events and a short interval are more reliable in Chromium
        # and Electron than a zero-delay four-event batch. Use generic
        # VK_CONTROL so both left/right physical Ctrl states are understood.
        ctrl_down = v_down = False
        try:
            send(0x11)
            ctrl_down = True
            time.sleep(0.035)
            send(0x56)
            v_down = True
            time.sleep(0.035)
        finally:
            try:
                if v_down:
                    send(0x56, keyup)
            finally:
                if ctrl_down:
                    time.sleep(0.02)
                    send(0x11, keyup)

    def _status_changed(self, *_):
        status = self.status_text.get()
        if "失败" in status or "无效" in status:
            self._compact_note = "需要检查 · 展开查看"
        elif "没有检测" in status or "没有采集" in status:
            self._compact_note = "未检测到语音"
        elif "取消" in status:
            self._compact_note = "已取消"
        elif "已粘贴" in status or "已输入" in status:
            self._compact_note = "已输入 · 未发送"
        elif status.startswith("已复制"):
            self._compact_note = "已复制 · 可手动粘贴"
        elif "快捷键" in status:
            self._compact_note = ""
        self._draw_compact()

    def _draw_compact(self, hover=None) -> None:
        canvas = self.compact_canvas
        canvas.delete("all")
        active = self.recorder.recording or self._busy
        width = 280 if active else 220
        canvas.configure(width=width)
        if self._collapsed and self.winfo_width() != width:
            self._compact_width = width
            self._position_bottom_center()
        self._compact_width = width
        rounded_shape(canvas, 2, 2, width-2, 54, 26, fill="#ffffff", outline="#d5e1dc", width=1)
        if active:
            canvas.create_oval(9, 8, 49, 48, fill="#e8f4ee" if hover != "record" else "#d7ece1", outline="")
            if self.recorder.recording:
                rounded_shape(canvas, 22, 21, 36, 35, 4, fill="#147d68", outline="")
            else:
                canvas.create_arc(21, 20, 37, 36, start=(self._wave_phase*45)%360, extent=250,
                                  style="arc", outline="#147d68", width=2)
            for i in range(7):
                amplitude = self.recorder.level if self.recorder.recording else 0.0
                height = 4 + amplitude * 25 * (0.4 + 0.6*abs(math.sin(self._wave_phase+i*0.75)))
                x = 62+i*6
                canvas.create_line(x, 28-height/2, x, 28+height/2, fill="#147d68", width=3, capstyle="round")
            elapsed = max(0, int(time.time()-self.recorder.started_at))
            api_waiting = False
            if self.recorder.recording:
                text = f"录音中 {elapsed//60:02d}:{elapsed%60:02d}"
            else:
                status = self.status_text.get()
                # Keep the compact pill useful during tests: distinguish the
                # configured remote audio model from the local fallback.
                if "首次准备模型" in status:
                    text = "下载模型中…"
                elif "正在加载本地模型" in status:
                    text = "加载模型中…"
                elif "回退到本地" in status or "本地转写" in status:
                    text = "本地转写中…"
                elif self.api_config.get("api_key"):
                    elapsed = max(0, int(time.monotonic() - self._transcription_started_at))
                    api_waiting = elapsed >= API_WAITING_THRESHOLD_SECONDS
                    text = (
                        f"等待中 {elapsed:02d}s"
                        if api_waiting
                        else f"AI 转写中 {elapsed:02d}s"
                    )
                else:
                    text = "正在转写…"
            canvas.create_text(115, 20, text=text, fill="#213a34", anchor="w", font=("Microsoft YaHei UI", 9))
            if self.recorder.recording:
                compact_hint = "左侧结束 · × 取消"
            elif "首次准备模型" in self.status_text.get():
                compact_hint = "首次下载约 0.5 GB"
            elif api_waiting:
                compact_hint = "等待服务响应"
            else:
                compact_hint = "快捷键或 × 取消"
            canvas.create_text(115, 38, text=compact_hint,
                               fill="#647572", anchor="w", font=("Microsoft YaHei UI", 8))
            canvas.create_oval(width-48, 8, width-8, 48, fill="#e1f1ea" if hover == "cancel" else "#f1f4f2", outline="")
            for a, b in ((-4, 4), (4, -4)):
                canvas.create_line(width-28+a, 24, width-28+b, 32, fill="#6e7c75", width=2, capstyle="round")
        else:
            canvas.create_oval(9, 8, 49, 48, fill="#d7ece1" if hover == "record" else "#e8f4ee", outline="")
            rounded_shape(canvas, 24, 16, 34, 31, 5, fill="#147d68", outline="")
            canvas.create_arc(20, 23, 38, 36, start=180, extent=180, style="arc", outline="#147d68", width=2)
            canvas.create_line(29, 36, 29, 40, fill="#147d68", width=2)
            canvas.create_text(59, 20, text=self._compact_note or "点击麦克风说话", anchor="w",
                               fill="#213a34", font=("Microsoft YaHei UI", 9))
            canvas.create_text(59, 37, text=self.hotkey_var.get().replace("Right ", "右 ").replace("Left ", "左 "),
                               anchor="w", fill="#647572", font=("Microsoft YaHei UI", 8))
            canvas.create_oval(width-46, 8, width-6, 48, fill="#e4ece9" if hover == "expand" else "#f1f4f2", outline="")
            canvas.create_line(width-29, 30, width-25, 26, width-21, 30, fill="#6e7c75", width=2)

    def _compact_hover(self, event):
        area = "record" if event.x < 53 else ("cancel" if self.recorder.recording or self._busy else "expand") if event.x > self._compact_width-50 else None
        self._draw_compact(area)

    def _compact_click(self, event):
        active = self.recorder.recording or self._busy
        if event.x > self._compact_width-50:
            self.cancel_recording() if active else self.expand_window()
        elif event.x < 53:
            self.toggle_recording()
        elif not active:
            self.expand_window()

    def _show_context_menu(self, event):
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="展开主界面", command=self.expand_window)
        menu.add_command(label="设置快捷键", command=lambda: (self.expand_window(), self.configure_hotkey()))
        menu.add_command(label="复制原始转写", command=lambda: self.copy_text(self.raw_box.get("1.0", "end-1c")))
        menu.add_separator()
        menu.add_command(label="退出 Voice to AI", command=self._on_close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
            menu.destroy()

    def _start_compact_animation(self):
        if self._compact_anim_after is None:
            self._animate_compact()

    def _animate_compact(self):
        self._compact_anim_after = None
        if self.recorder.recording or self._busy:
            self._wave_phase += 0.25
            self._draw_compact()
            self._compact_anim_after = self.after(60, self._animate_compact)

    def _stop_compact_animation(self):
        if self._compact_anim_after is not None:
            self.after_cancel(self._compact_anim_after)
            self._compact_anim_after = None

    def collapse_window(self) -> None:
        switch_visible_window = not self._collapsed and self.state() == "normal"
        if switch_visible_window:
            # Hide the native window before changing decorations. Without the
            # alpha guard Windows can paint the old main surface once, causing
            # a black flash before the pill appears.
            try:
                self.attributes("-alpha", 0.0)
            except tk.TclError:
                pass
            self.withdraw()
        self._collapsed = True
        self.attributes("-topmost", True)
        self.ui_root.pack_forget()
        self.overrideredirect(True)
        self.attributes("-transparentcolor", "#ff00ff")
        self.minsize(1, 1)
        self.resizable(False, False)
        self.compact_canvas.pack(fill="both", expand=True)
        self._compact_width = 280 if self.recorder.recording or self._busy else 220
        self._position_bottom_center()
        self._draw_compact()
        self._make_compact_nonactivating()
        if switch_visible_window:
            try:
                self.deiconify()
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
                ctypes.windll.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            except (AttributeError, OSError, tk.TclError):
                pass
            self.after(40, self._show_compact_after_switch)
        self.after_idle(self._make_compact_nonactivating)

    def _show_compact_after_switch(self) -> None:
        if not self._collapsed or self._exiting:
            return
        try:
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass

    def _make_compact_nonactivating(self):
        if not self._collapsed:
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            ex_style = user32.GetWindowLongW(hwnd, -20)
            ex_style = (ex_style | 0x08000000 | 0x00000080) & ~0x00040000
            user32.SetWindowLongW(hwnd, -20, ex_style)
        except (AttributeError, OSError):
            pass

    def expand_window(self) -> None:
        self._collapsed = False
        self._hidden_to_tray = False
        self.attributes("-topmost", False)
        self.compact_canvas.pack_forget()
        self.overrideredirect(False)
        self.attributes("-transparentcolor", "")
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            ex_style = user32.GetWindowLongW(hwnd, -20)
            ex_style = (ex_style | 0x00040000) & ~(0x08000000 | 0x00000080)
            user32.SetWindowLongW(hwnd, -20, ex_style)
        except (AttributeError, OSError):
            pass
        self.ui_root.pack(fill="both", expand=True)
        self.resizable(True, True)
        self.minsize(900, 640)
        width, height = 1000, 720
        left, top, right, bottom = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()
        try:
            import ctypes
            from ctypes import wintypes
            area = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):
                left, top, right, bottom = area.left, area.top, area.right, area.bottom
        except (AttributeError, OSError):
            pass
        x = max(left, left+(right-left-width)//2)
        y = max(top, top+(bottom-top-height)//2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.update_idletasks()
        self._configure_window_identity(Path(__file__).with_name("voice-to-ai.ico"))
        self._set_native_position(x, y)
        self.after(120, lambda: self._set_native_position(x, y))
        self.lift()
        self.focus_force()

    def clear_records(self) -> None:
        if not messagebox.askyesno("确认清理", "删除本地音频、转写历史和当前文本？"):
            return
        if RECORD_DIR.exists():
            for path in RECORD_DIR.glob("*.wav"):
                try:
                    path.unlink()
                except OSError:
                    pass
        if HISTORY_FILE.exists():
            HISTORY_FILE.unlink()
        self.raw_box.delete("1.0", "end")
        self.task_box.delete("1.0", "end")
        self.status_text.set("本地记录已清理")

    def _start_tray_icon(self) -> None:
        if self._exiting or self.tray_icon is not None:
            return
        try:
            import pystray
            from PIL import Image

            icon_path = Path(__file__).with_name("voice-to-ai.ico")
            image = Image.open(icon_path).convert("RGBA")
            menu = pystray.Menu(
                pystray.MenuItem("打开主窗口", self._tray_open, default=True),
                pystray.MenuItem("显示悬浮条", self._tray_show_pill),
                pystray.MenuItem("开始 / 停止录音", self._tray_toggle_recording),
                pystray.MenuItem("取消本次录音", self._tray_cancel_recording),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出 Voice to AI", self._tray_exit),
            )
            self.tray_icon = pystray.Icon("voice-to-ai", image, "Voice to AI", menu)
            self.tray_icon.run_detached()
        except Exception as exc:
            self.tray_icon = None
            self.status_text.set(f"系统托盘启动失败：{exc}")

    def _tray_open(self, icon=None, item=None) -> None:
        self._post_ui(lambda: self._restore_hidden_window(show_main=True))

    def _tray_show_pill(self, icon=None, item=None) -> None:
        self._post_ui(lambda: self._restore_hidden_window(show_main=False))

    def _tray_toggle_recording(self, icon=None, item=None) -> None:
        self._post_ui(self.toggle_recording)

    def _tray_cancel_recording(self, icon=None, item=None) -> None:
        self._post_ui(self.cancel_recording)

    def _tray_exit(self, icon=None, item=None) -> None:
        # pystray invokes menu callbacks on its own thread. Queue shutdown on
        # Tk first; the Tk thread owns both the window and tray lifecycle.
        if self._exiting or self._tray_stopping:
            return
        self._tray_stopping = True
        self._post_ui(self._exit_application)

    def _hide_to_tray(self) -> None:
        if self.tray_icon is None:
            self.iconify()
            self.status_text.set("托盘图标尚未就绪；窗口已最小化到任务栏")
            return
        self._hidden_to_tray = True
        self.withdraw()

    def _exit_application(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        self._job_id += 1
        self._stop_compact_animation()
        if self.recorder.recording or self._busy:
            self.recorder.cancel()
        self._busy = False
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self._tray_stopping = True
        self._close_result_popup()
        self.destroy()

    def _on_close(self) -> None:
        """Keep the old close callback as an explicit full-exit alias."""
        self._exit_application()


def acquire_single_instance() -> object:
    """Use a named Windows mutex so every launch shares one reliable lock."""
    if os.name != "nt":
        return object()
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel32.CreateMutexW(None, False, "Local\\VoiceToAI-MVP-single-instance")
    if not mutex:
        raise RuntimeError("无法创建语音工具单实例锁。")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(mutex)
        raise RuntimeError("语音工具已经运行，请使用已有窗口。")
    atexit.register(lambda: kernel32.CloseHandle(mutex))
    return mutex


def run_release_self_test(report_path: Path, model_path: Optional[Path] = None,
                          audio_path: Optional[Path] = None) -> None:
    """Exercise the frozen runtime using temporary data and no API requests."""
    import tempfile
    from unittest.mock import patch
    global APP_DIR, RECORD_DIR, HISTORY_FILE, LOCK_FILE
    result = {"ok": False, "checks": [], "errors": []}
    with tempfile.TemporaryDirectory(prefix="voice-to-ai-test-") as data_dir:
        APP_DIR = Path(data_dir)
        RECORD_DIR = APP_DIR / "records"
        HISTORY_FILE = APP_DIR / "history.jsonl"
        LOCK_FILE = APP_DIR / "app.lock"
        application = None
        try:
            assert to_simplified_chinese("語音轉寫", "zh") == "语音转写"
            result["checks"].append("simplified-chinese")
            application = App()
            application.report_callback_exception = lambda *args: result["errors"].append(str(args))
            if application.hotkey_listener:
                application.hotkey_listener.stop()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                application.update()
                time.sleep(0.02)
            assert application.tray_icon is not None, "Tray did not start"
            result["checks"].append("startup-and-tray")
            application.expand_window()
            application.update()
            assert application.state() == "normal"
            application._hide_to_tray()
            application.update()
            assert application.state() == "withdrawn"
            application._tray_open()
            deadline = time.monotonic() + 0.4
            while time.monotonic() < deadline:
                application.update()
                time.sleep(0.02)
            assert application.state() == "normal"
            result["checks"].append("tray-hide-reopen")
            application.collapse_window()
            application.update()
            assert application.compact_canvas.winfo_ismapped()
            result["checks"].append("compact-window")
            if model_path is None and complete_local_model(portable_model_directory()):
                model_path = portable_model_directory()
            if model_path is not None:
                assert complete_local_model(model_path), "Incomplete offline model"
                application.model = None
                with patch(
                    __name__ + ".portable_model_directory", return_value=model_path
                ), patch(
                    "huggingface_hub.snapshot_download", side_effect=AssertionError("Offline model must not use Hub")
                ), patch(
                    "socket.socket.connect", side_effect=AssertionError("Network forbidden during offline inference")
                ):
                    model = application._load_model()
                    segments, _ = model.transcribe(np.zeros(SAMPLE_RATE * 2, dtype=np.float32), vad_filter=True)
                    assert not list(segments)
                    result["checks"].append("local-model-and-vad")
                    segments, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en",
                                                   vad_filter=False, beam_size=1, temperature=0)
                    list(segments)
                    result["checks"].append("local-inference-runtime")
                    if audio_path is not None:
                        audio, rate = sf.read(str(audio_path), dtype="float32")
                        assert rate == SAMPLE_RATE, "Test speech must use 16kHz audio"
                        segments, info = model.transcribe(audio, vad_filter=True, beam_size=5)
                        transcript = format_transcript_segments(list(segments), info.language)
                        assert transcript.strip(), "Test speech was not recognized"
                        result["transcript"] = transcript
                        result["checks"].append("offline-speech-transcription")
            application._exit_application()
            application = None
            result["checks"].append("exit")
            result["ok"] = not result["errors"]
        except Exception as exc:
            result["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            if application is not None:
                application._exit_application()
            report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        run_release_self_test(Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else None,
                              Path(sys.argv[4]) if len(sys.argv) > 4 else None)
        raise SystemExit(0)
    set_windows_app_identity()
    try:
        _instance_handle = acquire_single_instance()
    except RuntimeError as exc:
        # A duplicate launch must exit immediately. A modal Tk message box
        # can leave hidden pythonw.exe processes behind when the app is
        # started repeatedly from a shortcut or tray helper.
        if sys.stdout is not None:
            try:
                print(str(exc), file=sys.stdout)
            except OSError:
                pass
        raise SystemExit(0)
    App().mainloop()
