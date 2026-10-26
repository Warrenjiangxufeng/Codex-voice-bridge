# -*- coding: utf-8 -*-
"""Codex 语音桥：无终端窗口的 Lisa 语音入口。"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import fcntl
import os
import random
import re
import signal
import subprocess
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from queue import Empty, Queue
from tkinter import ttk

from voice_lisa import (
    CODEX_BIN,
    FFMPEG_BIN,
    MIC_INDEX,
    OUT_DIR,
    ROOT,
    ask_codex_stream,
    log,
    queue_to_codex,
    set_tts_enabled,
    stop_speaking,
    transcribe,
    tts_enabled,
    tts_state,
)
from voice_activity import VoiceActivityDetector
from voice_stream import CodexAppServer

WAV_PATH = ROOT / "last_input.wav"
HOTKEY_LABEL = "双击 Enter"
HOTKEY_HELPER = ROOT / "build" / "voice_hotkey"
HOTKEY_LOG = ROOT / "build" / "hotkey.log"
AVATAR_FILE = ROOT / "build" / "lisa_avatar_172.png"
OPERATIONS_LOG = OUT_DIR / "operations.log"
TRANSCRIPT_LIMIT = 24
SHOW_REQUEST = ROOT / ".voice_show_request"
PTY_ACTIVE = ROOT / ".codex_voice_pty_active"
QUEUE_MODE_FILE = ROOT / ".voice_queue_to_codex"
PTY_HEARTBEAT = ROOT / ".codex_voice_pty_heartbeat"
BRIDGE_HEARTBEAT = ROOT / ".voice_bridge_heartbeat"
INBOX_DIR = ROOT / ".codex_voice_inbox"

BG = "#F3F6FA"
SURFACE = "#FFFFFF"
SURFACE_RAISED = "#F8FAFC"
SURFACE_SOFT = "#EEF2F7"
BORDER = "#D9E2EC"
TEXT = "#172033"
MUTED = "#64748B"
SUBTLE = "#94A3B8"
ACCENT = "#0F766E"
ACCENT_DARK = "#CCFBF1"
RECORDING = "#DC2626"
WARNING = "#B45309"
SPEAKING = "#2563EB"
ERROR = "#DC2626"
CONSOLE_BG = "#F8FAFC"

STATUS_COLORS = {
    "ready": ACCENT,
    "recording": RECORDING,
    "processing": WARNING,
    "speaking": SPEAKING,
    "warning": WARNING,
    "error": ERROR,
}


def _flat_button(
    parent: tk.Misc,
    text: str,
    command,
    *,
    accent: bool = False,
    compact: bool = False,
) -> tk.Label:
    background = ACCENT if accent else SURFACE_RAISED
    foreground = "#FFFFFF" if accent else "#334155"
    button = tk.Label(
        parent,
        text=text,
        font=(
            "PingFang SC",
            10 if compact else 11,
            "bold" if accent else "normal",
        ),
        fg=foreground,
        bg=background,
        highlightthickness=1,
        highlightbackground=ACCENT_DARK if accent else BORDER,
        padx=11 if compact else 16,
        pady=5 if compact else 8,
        cursor="pointinghand",
    )
    button.bind("<Button-1>", lambda _event: command())
    button.bind(
        "<Enter>",
        lambda _event: button.configure(
            bg="#115E59" if accent else SURFACE_SOFT
        ),
    )
    button.bind(
        "<Leave>",
        lambda _event: button.configure(bg=background),
    )
    return button


def _tool_button(
    parent: tk.Misc,
    icon: str,
    text: str,
    command,
    *,
    danger: bool = False,
) -> tk.Label:
    button = tk.Label(
        parent,
        text=f"{icon}  {text}",
        font=("PingFang SC", 9),
        fg=MUTED,
        bg=SURFACE,
        highlightthickness=1,
        highlightbackground=BORDER,
        padx=9,
        pady=5,
        cursor="pointinghand",
    )
    button.bind("<Button-1>", lambda _event: command())
    button.bind(
        "<Enter>",
        lambda _event: button.configure(
            bg=SURFACE_SOFT,
            fg=ERROR if danger else TEXT,
            highlightbackground="#FCA5A5" if danger else "#B8C5D3",
        ),
    )
    button.bind(
        "<Leave>",
        lambda _event: button.configure(
            bg=SURFACE,
            fg=MUTED,
            highlightbackground=BORDER,
        ),
    )
    return button


def _pill_toggle(
    parent: tk.Misc,
    text: str,
    variable: tk.BooleanVar,
    command=None,
) -> tk.Label:
    label = tk.Label(
        parent,
        text=text,
        font=("PingFang SC", 9, "bold"),
        padx=10,
        pady=4,
        cursor="pointinghand",
        highlightthickness=1,
    )

    def refresh() -> None:
        enabled = variable.get()
        label.configure(
            text=("● " if enabled else "○ ") + text,
            bg=ACCENT_DARK if enabled else SURFACE_RAISED,
            fg=ACCENT if enabled else MUTED,
            highlightbackground=ACCENT_DARK if enabled else BORDER,
        )

    def toggle(_event=None) -> None:
        variable.set(not variable.get())
        refresh()
        if command:
            command()

    label.bind("<Button-1>", toggle)
    label.bind("<Enter>", lambda _event: label.configure(bg=SURFACE_SOFT))
    label.bind("<Leave>", lambda _event: refresh())
    refresh()
    return label


def _pty_is_active() -> bool:
    try:
        return time.time() - PTY_HEARTBEAT.stat().st_mtime < 3
    except OSError:
        return False


def _recent_pty_submission(text: str, max_age: float = 8.0) -> bool:
    """识别旧透明终端已提交同一句时，跳过语音桥的重复提交。"""
    try:
        lines = OPERATIONS_LOG.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[-100:]
    except OSError:
        return False

    now = dt.datetime.now()
    marker = " [PTY] 识别完成 · "
    for line in reversed(lines):
        if marker not in line or not line.endswith(text):
            continue
        try:
            stamp = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if (now - stamp).total_seconds() <= max_age:
            return True
    return False


def inject_to_codex_terminal(text: str) -> None:
    """把转写原子地交给当前 codex 语音终端。"""
    if not _pty_is_active():
        raise RuntimeError("当前没有正在运行的 codex 语音终端")
    INBOX_DIR.mkdir(exist_ok=True)
    stamp = time.time_ns()
    target = INBOX_DIR / f"{stamp}.txt"
    temporary = INBOX_DIR / f".{stamp}.tmp"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)


class VoiceLisaApp:
    def __init__(self, root: tk.Tk, headless: bool = False) -> None:
        self.root = root
        self.headless = headless
        self.root.title("Lisa Voice")
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(920, max(720, int(screen_width * 0.48)))
        height = min(720, max(520, int(screen_height * 0.58)))
        left = max(24, (screen_width - width) // 2)
        top = max(20, (screen_height - height) // 2 - 16)
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self.root.minsize(720, 500)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)

        self.events: Queue[tuple[str, object]] = Queue()
        self.record_process: subprocess.Popen[bytes] | None = None
        self.recording = False
        self.processing = False
        self.started_at = 0.0
        self._hotkey_ref = ctypes.c_void_p()
        self._handler_ref = ctypes.c_void_p()
        self._hotkey_callback = None
        self.hotkey_process: subprocess.Popen[bytes] | None = None
        self.codex_client: CodexAppServer | None = None
        self.codex_client_lock = threading.Lock()
        self.codex_ready = threading.Event()
        self.vad: VoiceActivityDetector | None = None
        self.pending_prompt: str | None = None
        self.wake_armed = False
        self.barge_candidate = False
        self.manual_capture = False
        self.log_lock = threading.Lock()
        self.status_tone = "ready"
        self.voice_state = "idle"
        self.wave_phase = 0
        self.tts_enabled_var = tk.BooleanVar(value=tts_enabled())
        self.avatar_photo = None
        if AVATAR_FILE.is_file():
            try:
                self.avatar_photo = tk.PhotoImage(
                    file=str(AVATAR_FILE)
                ).subsample(2)
            except tk.TclError:
                self.avatar_photo = None

        self._build_ui()
        self._load_recent_transcript()
        self._operation("语音桥启动")
        self._heartbeat()
        self._start_hotkey_helper()
        threading.Thread(target=self._prewarm_codex, daemon=True).start()
        self._start_hands_free()
        self.root.after(100, self._poll_events)
        self.root.after(180, self._poll_tts_state)
        self.root.after(300, self._poll_show_request)
        self.root.createcommand("::tk::mac::ReopenApplication", self.show)
        if self.headless:
            self.root.withdraw()

    def _heartbeat(self) -> None:
        BRIDGE_HEARTBEAT.touch()
        self.root.after(2000, self._heartbeat)

    def _prewarm_codex(self) -> None:
        self._operation("Codex app-server 启动中")
        try:
            client = CodexAppServer(
                CODEX_BIN,
                Path.home(),
                timeout=30,
            )
            with self.codex_client_lock:
                self.codex_client = client
            self._operation("Codex app-server 已就绪")
        except Exception as exc:
            self._operation(f"Codex app-server 启动失败：{exc}")
        finally:
            self.codex_ready.set()

    def _get_codex_client(self) -> CodexAppServer:
        self.codex_ready.wait(timeout=15)
        with self.codex_client_lock:
            client = self.codex_client
            if client and client.proc.poll() is None:
                return client
            if client:
                client.close()
            client = CodexAppServer(
                CODEX_BIN,
                Path.home(),
                timeout=30,
            )
            self.codex_client = client
            self._operation("Codex app-server 已重新就绪")
            return client

    def _start_hands_free(self) -> None:
        try:
            self.vad = VoiceActivityDetector(
                WAV_PATH,
                is_speaking=lambda: tts_state() == "speaking",
                is_wake_armed=lambda: self.wake_armed,
                can_capture=lambda: (
                    not _pty_is_active()
                    and (
                        not self.processing
                        or tts_state() == "speaking"
                    )
                ),
                on_speech_start=lambda: self.root.after(
                    0, self._hands_free_start
                ),
                on_utterance=lambda path: self.root.after(
                    0, self._hands_free_utterance, path
                ),
                on_barge_in=lambda: self.root.after(
                    0, self._hands_free_barge_in
                ),
                on_error=lambda message: self.root.after(
                    0, self._hands_free_error, message
                ),
                on_debug=lambda message: self.root.after(
                    0, self._operation, message
                ),
            )
            self.vad.start()
            self._operation("免按键聆听已开启")
        except Exception as exc:
            self.vad = None
            self._operation(f"免按键聆听不可用：{exc}")

    def _hands_free_start(self) -> None:
        if self.recording:
            return
        self.recording = True
        self.started_at = time.monotonic()
        self.timer_label.configure(text="00:00")
        if self.wake_armed:
            self._set_status("正在聆听…", "recording")
            self._set_mic_state("recording")
        self.root.after(200, self._tick_recording)

    def _hands_free_barge_in(self) -> None:
        self.barge_candidate = True
        self._operation("检测到可能的插话，等待 Lisa 唤醒词")

    def _hands_free_utterance(self, path: Path | None) -> None:
        self.recording = False
        self.started_at = 0.0
        manual_capture = self.manual_capture
        self.manual_capture = False
        if not path:
            self._set_status("待唤醒 · 说 Lisa", "ready")
            self._set_mic_state("idle")
            self.barge_candidate = False
            return
        if manual_capture:
            self._operation("手动录音结束")
        self._set_status("正在识别…", "processing")
        self._set_mic_state("processing")
        threading.Thread(
            target=self._process_hands_free,
            args=(path,),
            daemon=True,
        ).start()

    def _hands_free_error(self, message: str) -> None:
        self.vad = None
        self._operation(f"免按键聆听停止：{message}")
        self._set_status("自动聆听不可用，可用双击 Enter", "warning")

    def _process_hands_free(self, path: Path) -> None:
        try:
            started = time.monotonic()
            text = transcribe(path).strip()
            if not text:
                self._set_status("待唤醒 · 说 Lisa", "ready")
                self._set_mic_state("idle")
                self.barge_candidate = False
                return
            _wake, command = self._extract_wake_word(text)
            if not _wake and not self.wake_armed:
                self._operation(
                    f"未检测到 Lisa，忽略语音 · {text[:60]}"
                )
                self._set_status("待唤醒 · 说 Lisa", "ready")
                self._set_mic_state("idle")
                self.barge_candidate = False
                return

            if _wake and tts_state() == "speaking":
                stop_speaking()
                self._operation("检测到 Lisa 唤醒词，已停止朗读")
                self._beep("Pop")

            if _wake and not command:
                self.wake_armed = True
                self._set_status("我在听", "speaking")
                self._set_mic_state("idle")
                self._beep("Tink")
                self._operation("已唤醒，等待下一条语音指令")
                self.barge_candidate = False
                return

            self.wake_armed = False
            self.barge_candidate = False
            self._operation(
                f"自动识别 · {time.monotonic() - started:.1f}s · {command}"
            )
            log("你", command)
            self._queue_or_start_prompt(command)
        except Exception as exc:
            self._operation(f"自动识别失败：{exc}")
            self._show_error(f"自动识别失败：{exc}")

    def _queue_or_start_prompt(self, text: str) -> None:
        if self.processing:
            self.pending_prompt = text
            self.events.put(("progress", "已听到，等待上一轮结束…"))
            return
        self._start_prompt_text(text)

    def _start_prompt_text(self, text: str) -> None:
        if self.processing:
            self.pending_prompt = text
            return
        self.processing = True
        self.events.put(("user", text))
        self._set_status("Lisa 正在思考…", "processing")
        self._set_mic_state("processing")
        threading.Thread(
            target=self._process_gui_text,
            args=(text,),
            daemon=True,
        ).start()

    @staticmethod
    def _extract_wake_word(text: str) -> tuple[bool, str]:
        match = re.search(r"\blisa\b", text, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"lisa", text, flags=re.IGNORECASE)
        if not match:
            return False, text.strip()
        command = text[match.end() :].lstrip(" ,，。.!！?？:：-")
        return True, command.strip()

    def _toggle_tts(self) -> None:
        enabled = self.tts_enabled_var.get()
        set_tts_enabled(enabled)
        self._operation(f"朗读回复：{'开启' if enabled else '关闭'}")

    def _toggle_queue_mode(self) -> None:
        enabled = self.queue_to_codex_var.get()
        QUEUE_MODE_FILE.write_text("1" if enabled else "0", encoding="utf-8")
        self._operation(
            "语音模式：送入当前 Codex（整段朗读）"
            if enabled
            else "语音模式：流式语音（默认）"
        )

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Dark.Vertical.TScrollbar",
            gripcount=0,
            width=7,
            background="#CBD5E1",
            darkcolor="#CBD5E1",
            lightcolor="#CBD5E1",
            troughcolor=SURFACE,
            bordercolor=SURFACE,
            arrowcolor=SURFACE,
            relief="flat",
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", "#94A3B8")],
        )

        page = tk.Frame(self.root, bg=BG, padx=18, pady=12)
        page.pack(fill="both", expand=True)

        header = tk.Frame(page, bg=BG)
        header.pack(fill="x", pady=(0, 9))

        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left")
        title_row = tk.Frame(brand, bg=BG)
        title_row.pack(anchor="w")
        tk.Label(
            title_row,
            text="Lisa",
            bg=BG,
            fg=TEXT,
            font=("SF Pro Display", 22, "bold"),
        ).pack(side="left")
        tk.Label(
            title_row,
            text="VOICE",
            bg=SURFACE_RAISED,
            fg=ACCENT,
            font=("SF Mono", 8, "bold"),
            padx=7,
            pady=2,
        ).pack(side="left", padx=(8, 0), pady=(4, 0))
        tk.Label(
            brand,
            text="Codex 语音助手",
            bg=BG,
            fg=SUBTLE,
            font=("PingFang SC", 9),
        ).pack(anchor="w", pady=(1, 0))

        header_tools = tk.Frame(header, bg=BG)
        header_tools.pack(side="right", pady=(3, 0))
        _tool_button(
            header_tools, "⌫", "清空", self.clear
        ).pack(side="left")
        _tool_button(
            header_tools, "—", "后台", self.hide
        ).pack(side="left", padx=(7, 0))
        _tool_button(
            header_tools, "×", "退出", self.quit, danger=True
        ).pack(side="left", padx=(7, 0))

        voice_dock = tk.Frame(
            page,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        voice_dock.pack(fill="x", pady=(0, 10))
        tk.Frame(voice_dock, bg=ACCENT_DARK, height=2).pack(fill="x")
        dock_inner = tk.Frame(
            voice_dock, bg=SURFACE, padx=13, pady=7
        )
        dock_inner.pack(fill="x")

        mic_column = tk.Frame(dock_inner, bg=SURFACE)
        mic_column.pack(side="left", padx=(0, 12))
        self.mic_canvas = tk.Canvas(
            mic_column,
            width=82,
            height=82,
            bg=SURFACE,
            highlightthickness=0,
            cursor="pointinghand",
        )
        self.mic_canvas.pack()
        self.mic_canvas.bind("<Button-1>", self._on_mic_click)
        self.mic_canvas.bind("<Enter>", self._on_mic_hover)
        self.mic_canvas.bind("<Leave>", lambda _event: self._draw_mic())
        self.mic_hint = tk.Label(
            mic_column,
            text="点击说话",
            bg=SURFACE,
            fg=SUBTLE,
            font=("PingFang SC", 8),
        )
        self.mic_hint.pack()

        voice_info = tk.Frame(dock_inner, bg=SURFACE)
        voice_info.pack(side="left", fill="both", expand=True)
        self.status = tk.Label(
            voice_info,
            text="待唤醒 · 说 Lisa",
            anchor="w",
            bg=SURFACE,
            fg=ACCENT,
            font=("PingFang SC", 17, "bold"),
        )
        self.status.pack(fill="x", pady=(8, 0))
        tk.Label(
            voice_info,
            text="先说 Lisa，再说指令",
            anchor="w",
            bg=SURFACE,
            fg=MUTED,
            font=("PingFang SC", 9),
        ).pack(fill="x", pady=(1, 0))

        voice_visual = tk.Frame(dock_inner, bg=SURFACE)
        voice_visual.pack(side="right", anchor="center", padx=(12, 0))
        self.timer_label = tk.Label(
            voice_visual,
            text="00:00",
            bg=SURFACE,
            fg=SUBTLE,
            font=("SF Mono", 9),
        )
        self.timer_label.pack(anchor="e")
        self.wave_canvas = tk.Canvas(
            voice_visual,
            width=170,
            height=24,
            bg=SURFACE,
            highlightthickness=0,
        )
        self.wave_canvas.pack(pady=(3, 0))
        self.wave_canvas.bind(
            "<Configure>", lambda _event: self._draw_waveform()
        )

        self.queue_to_codex_var = tk.BooleanVar(value=False)
        QUEUE_MODE_FILE.write_text("0", encoding="utf-8")

        workspace = tk.Frame(page, bg=BG)
        workspace.pack(fill="both", expand=True)

        transcript_card = tk.Frame(
            workspace,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        transcript_card.pack(fill="both", expand=True)

        transcript_head = tk.Frame(
            transcript_card, bg=SURFACE, padx=12, pady=8
        )
        transcript_head.pack(fill="x")
        tk.Label(
            transcript_head,
            text="最近对话",
            bg=SURFACE,
            fg=TEXT,
            font=("PingFang SC", 11, "bold"),
        ).pack(side="left")
        tk.Label(
            transcript_head,
            text="● LIVE",
            bg=SURFACE,
            fg=ACCENT,
            font=("SF Mono", 8, "bold"),
        ).pack(side="left", padx=(9, 0))

        toggles = tk.Frame(transcript_head, bg=SURFACE)
        toggles.pack(side="right")
        queue_toggle = _pill_toggle(
            toggles,
            "送入 Codex",
            self.queue_to_codex_var,
            command=self._toggle_queue_mode,
        )
        queue_toggle.pack(side="left")
        tts_toggle = _pill_toggle(
            toggles,
            "朗读",
            self.tts_enabled_var,
            command=self._toggle_tts,
        )
        tts_toggle.pack(side="left", padx=(7, 0))

        tk.Frame(transcript_card, bg=BORDER, height=1).pack(fill="x")

        transcript_body = tk.Frame(
            transcript_card, bg=SURFACE, padx=8, pady=6
        )
        transcript_body.pack(fill="both", expand=True)
        transcript_scroll = ttk.Scrollbar(
            transcript_body,
            orient="vertical",
            style="Dark.Vertical.TScrollbar",
        )
        transcript_scroll.pack(side="right", fill="y")
        self.transcript = tk.Text(
            transcript_body,
            wrap="word",
            height=8,
            font=("PingFang SC", 10),
            padx=8,
            pady=5,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=SURFACE,
            fg="#334155",
            insertbackground=TEXT,
            selectbackground=ACCENT_DARK,
            yscrollcommand=transcript_scroll.set,
        )
        self.transcript.pack(side="left", fill="both", expand=True)
        transcript_scroll.configure(command=self.transcript.yview)
        self.transcript.tag_configure(
            "time", foreground=SUBTLE, font=("SF Mono", 8)
        )
        self.transcript.tag_configure(
            "you", foreground=ACCENT, font=("PingFang SC", 10, "bold")
        )
        self.transcript.tag_configure(
            "lisa", foreground=TEXT, font=("PingFang SC", 10, "bold")
        )
        self.transcript.tag_configure(
            "body",
            foreground="#334155",
            font=("PingFang SC", 10),
            spacing3=5,
        )
        self.transcript.tag_configure(
            "status",
            foreground=SUBTLE,
            font=("PingFang SC", 9, "italic"),
            spacing3=4,
        )
        self.transcript.configure(state="disabled")

        composer_shell = tk.Frame(
            transcript_card,
            bg=SURFACE_RAISED,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        composer_shell.pack(fill="x", padx=9, pady=(0, 9))
        composer = tk.Frame(
            composer_shell, bg=SURFACE_RAISED, padx=8, pady=7
        )
        composer.pack(fill="x")
        self.text_input = tk.Text(
            composer,
            height=3,
            width=1,
            wrap="word",
            font=("PingFang SC", 11),
            bg=SURFACE_RAISED,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            padx=2,
            pady=2,
            highlightthickness=0,
            selectbackground=ACCENT_DARK,
        )
        self.text_input.pack(
            side="left", fill="both", expand=True
        )
        self.text_input.bind("<Return>", self._submit_text_input)
        self.text_input.bind("<FocusIn>", self._focus_text_input)
        self.text_input.bind("<FocusOut>", self._blur_text_input)
        self.text_input.bind("<KeyPress>", self._clear_placeholder_on_key)
        self.text_input.tag_configure(
            "placeholder",
            foreground=SUBTLE,
        )

        send_column = tk.Frame(composer, bg=SURFACE_RAISED)
        send_column.pack(side="right", fill="y", padx=(9, 0))
        _flat_button(
            send_column,
            "发送  ↵",
            self._submit_text_input,
            accent=True,
            compact=True,
        ).pack(side="bottom", anchor="e", pady=(0, 2))

        self.input_placeholder = "输入指令，Enter 发送"
        self.input_placeholder_active = False
        self._show_input_placeholder()

        self._draw_mic()
        self._draw_waveform()

    def _build_ui_legacy(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Dark.Vertical.TScrollbar",
            gripcount=0,
            width=8,
            background="#2B394C",
            darkcolor="#2B394C",
            lightcolor="#2B394C",
            troughcolor=CONSOLE_BG,
            bordercolor=CONSOLE_BG,
            arrowcolor=CONSOLE_BG,
            relief="flat",
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", "#43546A")],
        )

        page = tk.Frame(self.root, bg=BG, padx=16, pady=8)
        page.pack(fill="both", expand=True)

        header = tk.Frame(page, bg=BG)
        header.pack(fill="x", pady=(0, 6))
        brand = tk.Frame(header, bg=BG)
        brand.pack(side="left")

        title_box = tk.Frame(brand, bg=BG)
        title_box.pack(side="left")
        tk.Label(
            title_box,
            text="Lisa Voice",
            bg=BG,
            fg=TEXT,
            font=("SF Pro Display", 18, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="本地识别  ·  中英朗读  ·  Codex 直连",
            bg=BG,
            fg=MUTED,
            font=("PingFang SC", 9),
        ).pack(anchor="w", pady=(1, 0))

        header_tools = tk.Frame(header, bg=BG)
        header_tools.pack(side="right")
        _flat_button(
            header_tools, "清空", self.clear, compact=True
        ).pack(side="left", padx=(9, 0))
        _flat_button(
            header_tools, "后台", self.hide, compact=True
        ).pack(side="left", padx=(9, 0))
        _flat_button(
            header_tools, "退出", self.quit, compact=True
        ).pack(side="left", padx=(9, 0))

        hero = tk.Frame(
            page,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        hero.pack(fill="x", pady=(0, 7))
        tk.Frame(hero, bg=ACCENT_DARK, height=2).pack(fill="x")
        hero_inner = tk.Frame(hero, bg=SURFACE, padx=14, pady=8)
        hero_inner.pack(fill="both", expand=True)

        mic_column = tk.Frame(hero_inner, bg=SURFACE)
        mic_column.pack(side="left", padx=(0, 14))
        self.mic_canvas = tk.Canvas(
            mic_column,
            width=86,
            height=86,
            bg=SURFACE,
            highlightthickness=0,
            cursor="pointinghand",
        )
        self.mic_canvas.pack()
        self.mic_canvas.bind("<Button-1>", self._on_mic_click)
        self.mic_canvas.bind("<Enter>", self._on_mic_hover)
        self.mic_canvas.bind("<Leave>", lambda _event: self._draw_mic())
        self.mic_hint = tk.Label(
            mic_column,
            text="点击头像开始",
            bg=SURFACE,
            fg=SUBTLE,
            font=("PingFang SC", 8),
        )
        self.mic_hint.pack(pady=(0, 0))

        voice_panel = tk.Frame(hero_inner, bg=SURFACE)
        voice_panel.pack(side="left", fill="both", expand=True)

        state_row = tk.Frame(voice_panel, bg=SURFACE)
        state_row.pack(fill="x")
        tk.Label(
            state_row,
            text="●  VOICE INPUT",
            bg=SURFACE_RAISED,
            fg=ACCENT,
            font=("SF Mono", 8, "bold"),
            padx=8,
            pady=3,
        ).pack(side="left")
        self.timer_label = tk.Label(
            state_row,
            text="00:00",
            bg=SURFACE,
            fg=MUTED,
            font=("SF Mono", 10),
        )
        self.timer_label.pack(side="right")

        self.status = tk.Label(
            voice_panel,
            text="准备聆听",
            anchor="w",
            bg=SURFACE,
            fg=ACCENT,
            font=("PingFang SC", 18, "bold"),
        )
        self.status.pack(fill="x", pady=(3, 0))
        tk.Label(
            voice_panel,
            text=f"{HOTKEY_LABEL} 或点击头像开始",
            anchor="w",
            bg=SURFACE,
            fg=MUTED,
            font=("PingFang SC", 10),
        ).pack(fill="x")

        self.wave_canvas = tk.Canvas(
            voice_panel,
            height=26,
            bg=SURFACE,
            highlightthickness=0,
        )
        self.wave_canvas.pack(fill="x", pady=(3, 3))
        self.wave_canvas.bind("<Configure>", lambda _event: self._draw_waveform())

        # Default to app-server streaming: audio starts at the first stable phrase.
        # Checking this keeps the legacy "send to current Codex" full-answer mode.
        self.queue_to_codex_var = tk.BooleanVar(value=False)
        QUEUE_MODE_FILE.write_text("0", encoding="utf-8")
        toggle_row = tk.Frame(state_row, bg=SURFACE)
        toggle_row.pack(side="right", padx=(0, 10))
        queue_toggle = _pill_toggle(
            toggle_row,
            "送入 Codex",
            self.queue_to_codex_var,
            command=self._toggle_queue_mode,
        )
        queue_toggle.pack(side="left")
        tts_toggle = _pill_toggle(
            toggle_row,
            "朗读回复",
            self.tts_enabled_var,
            command=self._toggle_tts,
        )
        tts_toggle.pack(side="left", padx=(8, 0))

        workspace = tk.Frame(page, bg=BG)
        workspace.pack(fill="both", expand=True)
        workspace.rowconfigure(0, weight=1)
        workspace.columnconfigure(0, weight=1)

        transcript_card = tk.Frame(
            workspace,
            bg=SURFACE,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        transcript_card.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=0,
        )
        transcript_head = tk.Frame(transcript_card, bg=SURFACE)
        transcript_head.pack(fill="x", padx=11, pady=(7, 3))
        tk.Label(
            transcript_head,
            text="最近对话",
            bg=SURFACE,
            fg=TEXT,
            font=("PingFang SC", 11, "bold"),
        ).pack(side="left")
        tk.Label(
            transcript_head,
            text="WHISPER · LOCAL",
            bg=SURFACE,
            fg=SUBTLE,
            font=("SF Mono", 8),
        ).pack(side="right")

        transcript_body = tk.Frame(transcript_card, bg=SURFACE)
        transcript_body.pack(fill="both", expand=True, padx=7, pady=(0, 6))
        transcript_scroll = ttk.Scrollbar(
            transcript_body,
            orient="vertical",
            style="Dark.Vertical.TScrollbar",
        )
        transcript_scroll.pack(side="right", fill="y")
        self.transcript = tk.Text(
            transcript_body,
            wrap="word",
            height=6,
            font=("PingFang SC", 10),
            padx=9,
            pady=6,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=SURFACE,
            fg="#C9D4E2",
            insertbackground=TEXT,
            selectbackground=ACCENT_DARK,
            yscrollcommand=transcript_scroll.set,
        )
        self.transcript.pack(side="left", fill="both", expand=True)
        transcript_scroll.configure(command=self.transcript.yview)
        self.transcript.tag_configure(
            "time", foreground=SUBTLE, font=("SF Mono", 8)
        )
        self.transcript.tag_configure(
            "you", foreground=ACCENT, font=("PingFang SC", 10, "bold")
        )
        self.transcript.tag_configure(
            "lisa", foreground=TEXT, font=("PingFang SC", 10, "bold")
        )
        self.transcript.tag_configure(
            "body",
            foreground="#C9D4E2",
            font=("PingFang SC", 10),
            spacing3=5,
        )
        self.transcript.tag_configure(
            "status",
            foreground=SUBTLE,
            font=("PingFang SC", 9, "italic"),
            spacing3=4,
        )
        self.transcript.configure(state="disabled")

        composer = tk.Frame(transcript_card, bg=SURFACE)
        composer.pack(fill="x", padx=9, pady=(0, 8))
        self.text_input = tk.Text(
            composer,
            height=3,
            width=1,
            wrap="word",
            font=("PingFang SC", 12),
            bg=SURFACE_RAISED,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            bd=0,
            padx=10,
            pady=8,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        self.text_input.pack(side="left", fill="both", expand=True)
        self.text_input.bind("<Return>", self._submit_text_input)
        _flat_button(
            composer,
            "发送",
            self._submit_text_input,
            compact=True,
        ).pack(side="left", padx=(7, 0))

        self._draw_mic()
        self._draw_waveform()

    def _draw_mic(self, hover: bool = False) -> None:
        canvas = self.mic_canvas
        canvas.delete("all")
        color = {
            "idle": ACCENT,
            "recording": RECORDING,
            "processing": WARNING,
            "speaking": SPEAKING,
        }.get(self.voice_state, ACCENT)
        if self.avatar_photo is not None:
            width = max(canvas.winfo_width(), 86)
            height = max(canvas.winfo_height(), 86)
            cx = width / 2
            cy = height / 2
            canvas.create_oval(
                cx - 45,
                cy - 45,
                cx + 45,
                cy + 45,
                outline="#D5E6E2",
                width=1,
            )
            canvas.create_image(cx, cy, image=self.avatar_photo)
            canvas.create_oval(
                1,
                1,
                width - 1,
                height - 1,
                outline=ACCENT if hover else color,
                width=2,
            )
            if self.voice_state in {"recording", "speaking"}:
                canvas.create_oval(
                    width - 18,
                    height - 18,
                    width - 8,
                    height - 8,
                    fill=color,
                    outline=SURFACE,
                    width=2,
                )
            return
        width = max(canvas.winfo_width(), 86)
        height = max(canvas.winfo_height(), 86)
        cx = width / 2
        cy = height / 2
        outer = min(width, height) / 2 - 4
        middle = outer - 8
        inner = outer - 15

        canvas.create_oval(
            cx - outer - 3,
            cy - outer - 3,
            cx + outer + 3,
            cy + outer + 3,
            outline="#D5E6E2",
            width=1,
        )
        canvas.create_oval(
            cx - outer,
            cy - outer,
            cx + outer,
            cy + outer,
            fill="#F8FAFC",
            outline="",
        )
        canvas.create_oval(
            cx - middle,
            cy - middle,
            cx + middle,
            cy + middle,
            fill=SURFACE_RAISED,
            outline="#94A3B8" if hover else BORDER,
            width=1,
        )
        canvas.create_oval(
            cx - inner,
            cy - inner,
            cx + inner,
            cy + inner,
            fill=color,
            outline="",
        )

        head_half_width = inner * 0.25
        head_top = cy - inner * 0.56
        head_bottom = cy + inner * 0.05
        canvas.create_oval(
            cx - head_half_width,
            head_top,
            cx + head_half_width,
            head_bottom,
            fill="#FFFFFF",
            outline="#FFFFFF",
        )
        canvas.create_rectangle(
            cx - head_half_width,
            cy - inner * 0.12,
            cx + head_half_width,
            cy + inner * 0.0,
            fill="#FFFFFF",
            outline="#FFFFFF",
        )
        canvas.create_arc(
            cx - inner * 0.48,
            cy - inner * 0.38,
            cx + inner * 0.48,
            cy + inner * 0.54,
            start=180,
            extent=180,
            style="arc",
            outline="#FFFFFF",
            width=2,
        )
        canvas.create_line(
            cx,
            cy + inner * 0.52,
            cx,
            cy + inner * 0.76,
            fill="#FFFFFF",
            width=2,
        )
        canvas.create_line(
            cx - inner * 0.25,
            cy + inner * 0.76,
            cx + inner * 0.25,
            cy + inner * 0.76,
            fill="#FFFFFF",
            width=2,
        )

        if self.voice_state == "recording":
            canvas.create_oval(
                cx - outer + 2,
                cy - outer + 2,
                cx + outer - 2,
                cy + outer - 2,
                outline=RECORDING,
                width=2,
            )

    def _draw_waveform(self) -> None:
        canvas = self.wave_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 120)
        height = max(canvas.winfo_height(), 24)
        count = max(14, min(24, int(width / 8)))
        gap = width / count
        center = height / 2

        if self.voice_state == "recording":
            color = RECORDING
            amplitudes = [
                3 + random.random() * (height * 0.36)
                for _ in range(count)
            ]
        elif self.voice_state == "processing":
            color = WARNING
            amplitudes = [
                4 + abs((i % 6) - 3) * 2 for i in range(count)
            ]
        elif self.voice_state == "speaking":
            color = SPEAKING
            self.wave_phase = (self.wave_phase + 1) % 8
            amplitudes = [
                3 + abs(((i + self.wave_phase) % 8) - 4) * 1.6
                for i in range(count)
            ]
        else:
            color = "#94A3B8"
            amplitudes = [
                3 + abs((i % 7) - 3) * 1.6 for i in range(count)
            ]

        for index, amplitude in enumerate(amplitudes):
            x = gap * index + gap / 2
            canvas.create_line(
                x,
                center - amplitude,
                x,
                center + amplitude,
                fill=color,
                width=2,
                capstyle="round",
            )

    def _set_mic_state(self, state: str) -> None:
        self.voice_state = state
        self._draw_mic()
        self._draw_waveform()
        if state == "recording":
            self.mic_hint.configure(text="正在聆听", fg=RECORDING)
        elif state == "speaking":
            self.mic_hint.configure(text="正在朗读", fg=SPEAKING)
        elif state == "processing":
            self.mic_hint.configure(text="正在识别", fg=WARNING)
        else:
            self.mic_hint.configure(text="点击说话", fg=SUBTLE)

    def _set_status(self, text: str, tone: str = "ready") -> None:
        self.status_tone = tone
        self.status.configure(
            text=text,
            fg=STATUS_COLORS.get(tone, TEXT),
        )

    def _on_mic_click(self, _event=None) -> None:
        self.toggle_recording()

    def _on_mic_hover(self, _event=None) -> None:
        if not self.processing and not self.recording:
            self._draw_mic(hover=True)

    def _start_hotkey_helper(self) -> None:
        if not HOTKEY_HELPER.exists():
            self._register_hotkey()
            return
        try:
            self.hotkey_process = subprocess.Popen(
                [str(HOTKEY_HELPER), str(os.getpid())],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self._log_hotkey(f"helper launch failed: {exc}")
            self._register_hotkey()
            return
        threading.Thread(target=self._read_hotkey_output, daemon=True).start()

    def _read_hotkey_output(self) -> None:
        proc = self.hotkey_process
        if not proc or not proc.stdout:
            self.root.after(0, self._register_hotkey)
            return
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            self._log_hotkey(line)
            self._operation(f"快捷键监听：{line}", persist=False)
            if line == "TOGGLE":
                self.root.after(0, self.toggle_recording)
            elif line.startswith("NEEDS_PERMISSION"):
                extra = "；可先用 ⌃⌥ Space" if line.endswith("FALLBACK_OK") else ""
                self.root.after(
                    0,
                    lambda value=extra: self._set_status(
                        text=f"双击 Enter 需要键盘监听权限{value}",
                        tone="warning",
                    ),
                )
            elif line.startswith("READY:"):
                mode = line.partition(":")[2]
                if mode == "DOUBLE_ENTER":
                    text = "待唤醒 · 说 Lisa"
                elif mode == "FALLBACK":
                    text = "待唤醒 · 说 Lisa"
                else:
                    text = f"待唤醒 · 说 Lisa"
                self.root.after(
                    0,
                    lambda value=text: self._set_status(value, "ready"),
                )
            elif line.startswith("ERROR:"):
                self.root.after(
                    0,
                    lambda value=line: self._set_status(
                        f"快捷键不可用：{value}", "error"
                    ),
                )
        self.root.after(0, self._register_hotkey)

    @staticmethod
    def _log_hotkey(text: str) -> None:
        HOTKEY_LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with HOTKEY_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {text}\n")

    def _operation(self, text: str, persist: bool = True) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        print(line, flush=True)
        if persist:
            with self.log_lock:
                OPERATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
                with OPERATIONS_LOG.open("a", encoding="utf-8") as handle:
                    handle.write(f"{dt.date.today()} {line}\n")
        self.events.put(("op", line))

    def _append_operation(self, line: str) -> None:
        # Detailed operations remain in operations.log, not in the main UI.
        return

    def _register_hotkey(self) -> None:
        """注册系统级热键，不要求 Codex 窗口获得焦点。"""
        try:
            class EventTypeSpec(ctypes.Structure):
                _fields_ = [
                    ("eventClass", ctypes.c_uint32),
                    ("eventKind", ctypes.c_uint32),
                ]

            class EventHotKeyID(ctypes.Structure):
                _fields_ = [
                    ("signature", ctypes.c_uint32),
                    ("id", ctypes.c_uint32),
                ]

            callback_type = ctypes.CFUNCTYPE(
                ctypes.c_int32,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            )

            def on_hotkey(_next_handler, _event, _user_data):
                self.root.after(0, self.toggle_recording)
                return 0

            self._hotkey_callback = callback_type(on_hotkey)
            carbon = ctypes.CDLL(
                "/System/Library/Frameworks/Carbon.framework/Carbon"
            )
            carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
            target = carbon.GetApplicationEventTarget()

            spec = EventTypeSpec(0x6B657962, 5)  # keyb / kEventHotKeyPressed
            install = carbon.InstallEventHandler
            install.argtypes = [
                ctypes.c_void_p,
                callback_type,
                ctypes.c_ulong,
                ctypes.POINTER(EventTypeSpec),
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            install.restype = ctypes.c_int32
            status = install(
                target,
                self._hotkey_callback,
                1,
                ctypes.byref(spec),
                None,
                ctypes.byref(self._handler_ref),
            )
            if status != 0:
                raise OSError(status, "InstallEventHandler failed")

            hotkey_id = EventHotKeyID(0x4C495341, 1)  # LISA
            register = carbon.RegisterEventHotKey
            register.argtypes = [
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(EventHotKeyID),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            register.restype = ctypes.c_int32
            no_modifier = 0
            status = register(
                100,  # F8
                no_modifier,
                ctypes.byref(hotkey_id),
                target,
                0,
                ctypes.byref(self._hotkey_ref),
            )
            if status != 0:
                raise OSError(status, "RegisterEventHotKey failed")
        except Exception as exc:
            self._set_status(
                f"快捷键不可用，可点击头像（{exc}）",
                "warning",
            )

    def toggle_recording(self) -> None:
        if self.vad:
            if self.recording:
                self.vad.force_stop()
                return
            if tts_state() == "speaking":
                stop_speaking()
                self._operation("双击 Enter：已打断朗读")
                self.manual_capture = True
                self._set_status("正在聆听…", "recording")
                self._set_mic_state("recording")
                self.vad.force_start()
                return
            if self.processing:
                self._beep("Pop")
                return
            self.manual_capture = True
            self._set_status("正在聆听…", "recording")
            self._set_mic_state("recording")
            self.vad.force_start()
            return
        if self.recording:
            self.stop_recording()
            return
        if stop_speaking():
            self._operation("双击 Enter：已打断朗读")
            self._set_status("朗读已停止", "ready")
            self._beep("Pop")
            return
        if self.processing:
            self._beep("Pop")
            return
        self.start_recording()

    def start_recording(self) -> None:
        if self.vad:
            self.vad.force_start()
            return
        if self.record_process and self.record_process.poll() is None:
            self.record_process.terminate()

        try:
            WAV_PATH.unlink(missing_ok=True)
        except OSError as exc:
            self._show_error(f"无法清理上次录音：{exc}")
            return

        cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "avfoundation",
            "-i",
            f":{MIC_INDEX}",
            "-t",
            "60",
            str(WAV_PATH),
        ]
        try:
            self.record_process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:
            self._operation(f"麦克风启动失败：{exc}")
            self._show_error(f"无法启动麦克风：{exc}")
            return

        self.recording = True
        self.started_at = time.monotonic()
        self._operation(f"录音开始 · 设备 {MIC_INDEX}")
        self._set_status("正在聆听", "recording")
        self._set_mic_state("recording")
        self.timer_label.configure(text="00:00")
        self._beep("Tink")
        self.root.after(200, self._tick_recording)

    def _tick_recording(self) -> None:
        if not self.recording:
            return
        if self.record_process and self.record_process.poll() is not None:
            proc = self.record_process
            self.recording = False
            self.record_process = None
            self._set_mic_state("idle")
            self._beep("Pop")
            if proc.returncode:
                try:
                    stderr = proc.stderr.read().decode("utf-8", "replace").strip()
                except Exception:
                    stderr = ""
                detail = stderr.splitlines()[-1] if stderr else f"exit {proc.returncode}"
                self._operation(f"录音进程失败：{detail}")
                self._show_error(f"录音失败：{detail}")
                return
            self._operation("录音进程自行结束")
            self._begin_processing()
            return
        if time.monotonic() - self.started_at >= 59:
            self.stop_recording()
            return
        elapsed = int(time.monotonic() - self.started_at)
        if self.manual_capture or self.wake_armed:
            self._set_status("正在聆听", "recording")
            minutes, seconds = divmod(elapsed, 60)
            self.timer_label.configure(text=f"{minutes:02d}:{seconds:02d}")
            self._draw_waveform()
        self.root.after(250, self._tick_recording)

    def stop_recording(self) -> None:
        if self.vad and self.recording:
            self.vad.force_stop()
            return
        self.recording = False
        duration = (
            time.monotonic() - self.started_at if self.started_at else 0.0
        )
        proc = self.record_process
        self.record_process = None
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()

        self._beep("Pop")
        size = WAV_PATH.stat().st_size if WAV_PATH.exists() else 0
        self._operation(f"录音结束 · {duration:.1f}s · {size / 1024:.0f} KB")
        self._begin_processing()

    def _begin_processing(self) -> None:
        if not WAV_PATH.exists() or WAV_PATH.stat().st_size < 2000:
            self._operation("录音文件为空或过小")
            self._show_error("没有录到声音，请检查麦克风权限")
            return

        self.processing = True
        self._operation(f"录音文件就绪 · {WAV_PATH}")
        self._set_status("正在识别…", "processing")
        self._set_mic_state("processing")
        queue_mode = self.queue_to_codex_var.get()
        threading.Thread(
            target=self._process_turn,
            args=(WAV_PATH, queue_mode),
            daemon=True,
        ).start()

    def _process_turn(self, wav: str, queue_mode: bool) -> None:
        text = ""
        try:
            started = time.monotonic()
            self._operation("本地 Whisper 识别开始")
            text = transcribe(wav)
            elapsed = time.monotonic() - started
            if not text:
                self._operation("识别结果为空")
                self.events.put(("error", "没听清，再说一次"))
                return
            self._operation(f"识别完成 · {elapsed:.1f}s · {text}")
            self._operation(f"语音指令直接送入 Codex · {text}")
            log("你", text)
            self.events.put(("user", text))
            self._dispatch_text(text, queue_mode)
        except Exception as exc:
            self._operation(f"本轮异常：{exc}")
            with self.log_lock:
                with OPERATIONS_LOG.open("a", encoding="utf-8") as handle:
                    handle.write(traceback.format_exc())
            self.events.put(("error", f"本轮失败：{exc}"))
        finally:
            self.events.put(("done", None))

    def _submit_text_input(self, _event=None):
        if self.processing or self.recording:
            return "break"
        text = self.text_input.get("1.0", "end-1c").strip()
        if not text or self.input_placeholder_active:
            return "break"
        self.text_input.delete("1.0", "end")
        self.input_placeholder_active = False
        self._show_input_placeholder()
        self._operation(f"GUI 文字输入 · {text[:80]}")
        log("你", text)
        self._start_prompt_text(text)
        return "break"

    def _focus_text_input(self, _event=None) -> None:
        if self.input_placeholder_active:
            self.text_input.delete("1.0", "end")
            self.input_placeholder_active = False

    def _clear_placeholder_on_key(self, _event=None) -> None:
        if self.input_placeholder_active:
            self.text_input.delete("1.0", "end")
            self.input_placeholder_active = False

    def _blur_text_input(self, _event=None) -> None:
        if not self.text_input.get("1.0", "end-1c").strip():
            self._show_input_placeholder()

    def _show_input_placeholder(self) -> None:
        if self.text_input.get("1.0", "end-1c").strip():
            return
        self.text_input.configure(state="normal")
        self.text_input.insert(
            "1.0",
            self.input_placeholder,
            ("placeholder",),
        )
        self.input_placeholder_active = True

    def _process_gui_text(self, text: str) -> None:
        try:
            self._dispatch_text(text, False)
        except Exception as exc:
            self._operation(f"GUI 文字请求失败：{exc}")
            self.events.put(("error", f"本轮失败：{exc}"))
        finally:
            self.events.put(("done", None))

    def _dispatch_text(self, text: str, queue_mode: bool) -> None:
        if queue_mode:
            if _recent_pty_submission(text):
                self._operation("终端语音层已提交同一句，跳过重复")
                return
            if _pty_is_active():
                self._operation("送入当前 Codex 终端")
                inject_to_codex_terminal(text)
                self._operation("已送入当前 Codex 终端")
            elif PTY_ACTIVE.exists():
                raise RuntimeError(
                    "当前 Codex 仍是旧语音层，请关闭后重新运行 codex；"
                    "本次没有执行任何操作"
                )
            else:
                self._operation("送入 Codex 当前会话开始")
                thread_id = queue_to_codex(text)
                self._operation(f"已送入 Codex 会话 {thread_id}")
            return

        self._operation("流式语音请求开始")
        started = time.monotonic()
        client = self._get_codex_client()
        self.events.put(("progress", "正在分析…"))
        answer = ask_codex_stream(
            text,
            client=client,
            on_first_delta=self._on_first_delta,
            on_progress=lambda message: self.events.put(
                ("progress", message)
            ),
        )
        elapsed = time.monotonic() - started
        self._operation(
            f"Codex 回复完成 · {elapsed:.1f}s · {len(answer)} 字"
        )
        log("Lisa", answer)
        self.events.put(("answer", answer))
        self._operation("流式语音已结束")

    def _on_first_delta(self, seconds: float) -> None:
        self.events.put(("op", f"首个文本增量 · {seconds:.2f}s"))
        self.events.put(("progress", "正在生成回复…"))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "user":
                    self._append("你", str(payload))
                    self._set_status("Lisa 正在思考…", "processing")
                elif kind == "answer":
                    self._append("Lisa", str(payload))
                elif kind == "progress":
                    self._append_status(str(payload))
                elif kind == "error":
                    self._show_error(str(payload))
                elif kind == "op":
                    self._append_operation(str(payload))
                elif kind == "done":
                    self.processing = False
                    self._set_mic_state("idle")
                    if self.pending_prompt:
                        text = self.pending_prompt
                        self.pending_prompt = None
                        self._start_prompt_text(text)
                    elif self.status_tone not in {"warning", "error"}:
                        self._set_status("待唤醒 · 说 Lisa", "ready")
        except Empty:
            pass
        self.root.after(100, self._poll_events)

    def _poll_tts_state(self) -> None:
        state = tts_state()
        if not self.recording:
            if state == "speaking":
                if self.voice_state != "speaking":
                    self._set_status("正在朗读", "speaking")
                    self._set_mic_state("speaking")
                else:
                    self.wave_phase += 1
                    self._draw_waveform()
            elif self.voice_state == "speaking":
                if self.processing:
                    self._set_status("Lisa 正在思考…", "processing")
                    self._set_mic_state("processing")
                else:
                    self._set_status("待唤醒 · 说 Lisa", "ready")
                    self._set_mic_state("idle")
        self.root.after(180, self._poll_tts_state)

    def _load_recent_transcript(self) -> None:
        """Load the most recent persisted voice messages, including multiline ones."""
        pattern = re.compile(
            r"^- (\d{2}:\d{2}:\d{2}) \*\*(.+?)\*\*：(.*)$"
        )
        entries: list[tuple[str, str, str]] = []
        for path in sorted(OUT_DIR.glob("20??-??-??.md"))[-2:]:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            current: list[str] | None = None
            for line in lines:
                match = pattern.match(line)
                if match:
                    if current:
                        entries.append(
                            (
                                current[0],
                                current[1],
                                "\n".join(current[2:]),
                            )
                        )
                    current = [
                        f"{path.stem[5:]} {match.group(1)[:5]}",
                        match.group(2),
                        match.group(3),
                    ]
                elif current is not None:
                    current.append(line)
            if current:
                entries.append(
                    (current[0], current[1], "\n".join(current[2:]))
                )

        entries = [
            entry
            for entry in entries
            if not entry[1].startswith("Lisa · 理解确认")
        ]
        if not entries:
            self.transcript.configure(state="normal")
            self.transcript.insert(
                "end", "还没有语音对话记录。\n", ("body",)
            )
            self.transcript.configure(state="disabled")
            return
        for stamp, speaker, text in entries[-TRANSCRIPT_LIMIT:]:
            self._insert_transcript(
                speaker, text, time_str=stamp, scroll=False
            )
        self.transcript.see("end")

    def _append(self, speaker: str, text: str) -> None:
        self._insert_transcript(speaker, text)

    def _append_status(self, text: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        self.transcript.configure(state="normal")
        self.transcript.insert(
            "end",
            f"{stamp}  · {text}\n",
            ("status",),
        )
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _insert_transcript(
        self,
        speaker: str,
        text: str,
        time_str: str = "",
        scroll: bool = True,
    ) -> None:
        clean = text.strip().replace("**", "").replace("`", "")
        if not clean:
            return
        stamp = time_str or dt.datetime.now().strftime("%H:%M:%S")
        speaker_tag = "lisa" if speaker.startswith("Lisa") else "you"
        self.transcript.configure(state="normal")
        self.transcript.insert("end", f"{stamp}  ", ("time",))
        self.transcript.insert("end", f"{speaker}\n", (speaker_tag,))
        self.transcript.insert("end", f"{clean}\n\n", ("body",))
        if scroll:
            self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _show_error(self, text: str) -> None:
        self._operation(f"错误：{text}")
        self.processing = False
        self._set_mic_state("idle")
        self._set_status(text, "error")
        with (ROOT / "build" / "voice_gui.log").open("a", encoding="utf-8") as handle:
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            handle.write(f"{stamp} {text}\n")

    @staticmethod
    def _beep(name: str) -> None:
        subprocess.Popen(
            ["/usr/bin/afplay", f"/System/Library/Sounds/{name}.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def hide(self) -> None:
        if self.recording:
            self.stop_recording()
        self.root.withdraw()

    def _poll_show_request(self) -> None:
        if SHOW_REQUEST.exists():
            try:
                SHOW_REQUEST.unlink()
            except OSError:
                pass
            self.show()
        self.root.after(300, self._poll_show_request)

    def clear(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def open_log(self) -> None:
        OPERATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
        OPERATIONS_LOG.touch(exist_ok=True)
        subprocess.Popen(["open", str(OPERATIONS_LOG)])

    def quit(self) -> None:
        if self.vad:
            self.vad.stop()
            self.vad = None
            self.recording = False
        elif self.recording:
            self.stop_recording()
        BRIDGE_HEARTBEAT.unlink(missing_ok=True)
        self._operation("语音桥退出")
        with self.codex_client_lock:
            if self.codex_client:
                self.codex_client.close()
                self.codex_client = None
        if self.hotkey_process and self.hotkey_process.poll() is None:
            self.hotkey_process.terminate()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--headless",
        action="store_true",
        help="后台运行，不显示语音桥窗口",
    )
    args = parser.parse_args()

    lock_fp = (ROOT / ".voice_gui.lock").open("w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if not args.headless:
            SHOW_REQUEST.touch()
        print("Lisa 语音已在运行")
        return
    root = tk.Tk()
    VoiceLisaApp(root, headless=args.headless)
    root.mainloop()


if __name__ == "__main__":
    main()
