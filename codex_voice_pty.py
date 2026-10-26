# -*- coding: utf-8 -*-
"""在 Codex 终端前面加一层透明语音输入，不显示第二个窗口。"""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
from datetime import datetime
from pathlib import Path

from companion_trigger import (
    mark_user_activity,
    read_base_interval,
    read_proactive_state,
    should_trigger,
)
from voice_lisa import (
    FFMPEG_BIN,
    MIC_INDEX,
    OUT_DIR,
    ROOT,
    speak,
    stop_speaking,
    transcribe,
    tts_enabled,
)

CODEX_BIN = os.environ.get("CODEX_VOICE_UNDERLYING") or shutil.which("codex")
WAV_PATH = ROOT / "last_input.wav"
PTY_ACTIVE = ROOT / ".codex_voice_pty_active"
PTY_HEARTBEAT = ROOT / ".codex_voice_pty_heartbeat"
BRIDGE_HEARTBEAT = ROOT / ".voice_bridge_heartbeat"
INBOX_DIR = ROOT / ".codex_voice_inbox"
VOICE_QUEUE_MODE_FILE = ROOT / ".voice_queue_to_codex"
SESSIONS_DIR = Path.home() / ".codex" / "sessions"
OPERATIONS_LOG = OUT_DIR / "operations.log"
CODEX_LIVE_HOME = Path(
    os.environ.get(
        "CODEX_LIVE_DATA_DIR",
        str(Path.home() / ".codex-live"),
    )
).expanduser()
COMPANION_DATABASE = CODEX_LIVE_HOME / "memory.db"
COMPANION_SESSION_ID = "codex-live"
COMPANION_BASE_INTERVAL_SECONDS = read_base_interval(
    CODEX_LIVE_HOME / "config.json"
)
COMPANION_IDLE_SECONDS = max(
    1,
    int(
        os.environ.get(
            "LISA_COMPANION_IDLE_SECONDS",
            str(COMPANION_BASE_INTERVAL_SECONDS),
        )
    ),
)
COMPANION_OUTPUT_QUIET_SECONDS = 15.0
COMPANION_RETRY_SECONDS = 300.0
COMPANION_CHECK_INTERVAL_SECONDS = 1.0

NON_INTERACTIVE = {
    "agents",
    "app-server",
    "apply",
    "archive",
    "cloud",
    "completion",
    "debug",
    "delete",
    "doctor",
    "e",
    "exec",
    "exec-server",
    "features",
    "help",
    "login",
    "logout",
    "mcp",
    "mcp-server",
    "migrate-rollouts",
    "queue",
    "remote-control",
    "review",
    "sandbox",
    "unarchive",
    "update",
}


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} [PTY] {message}"
    OPERATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with OPERATIONS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class CodexVoicePTY:
    def __init__(self, args: list[str]) -> None:
        if not CODEX_BIN:
            raise RuntimeError("找不到 codex 可执行文件")

        self.args = args
        self.stdin_fd = sys.stdin.fileno()
        self.stdout_fd = sys.stdout.fileno()
        self.master_fd = -1
        self.child: subprocess.Popen[bytes] | None = None
        self.old_termios = termios.tcgetattr(self.stdin_fd)
        self.pending_enter = 0.0
        self.pending_byte = b"\r"
        self.recording: subprocess.Popen[bytes] | None = None
        self.processing = False
        self.write_lock = threading.Lock()
        self.resize_pending = True
        self.spoken_ids: set[str] = set()
        self.last_user_activity = time.time()
        self.last_output_activity = time.time()
        self.last_companion_trigger = 0.0
        self.next_companion_check = 0.0
        self.last_companion_state_write = 0.0

    def run(self) -> int:
        INBOX_DIR.mkdir(exist_ok=True)
        for stale in INBOX_DIR.glob("*.txt"):
            stale.unlink(missing_ok=True)

        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        self._resize(slave_fd)
        started_at = time.time()
        self.child = subprocess.Popen(
            [CODEX_BIN, *self.args],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)

        PTY_ACTIVE.touch()
        threading.Thread(
            target=self._watch_final_answers,
            args=(started_at,),
            daemon=True,
        ).start()
        tty.setraw(self.stdin_fd)
        signal.signal(signal.SIGWINCH, self._on_resize)
        log("语音终端代理启动；双击 Enter 开始或结束录音")
        log(
            "主动陪伴已接入当前会话；"
            f"无输入 {COMPANION_IDLE_SECONDS:.0f}s 且 Codex 空闲时检查到期"
        )

        try:
            while True:
                PTY_HEARTBEAT.touch()
                if self.resize_pending:
                    self._resize(master_fd)
                    self.resize_pending = False
                self._finish_if_recording_ended()
                self._drain_inbox()
                self._maybe_trigger_companion()

                timeout = 0.2
                if self.pending_enter:
                    timeout = min(
                        timeout,
                        max(0.0, self.pending_enter - time.monotonic()),
                    )
                try:
                    ready, _, _ = select.select(
                        [self.stdin_fd, master_fd], [], [], timeout
                    )
                except InterruptedError:
                    continue

                if self.stdin_fd in ready:
                    try:
                        data = os.read(self.stdin_fd, 4096)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    self._record_user_activity()
                    self._handle_input(data)

                if master_fd in ready:
                    try:
                        output = os.read(master_fd, 65536)
                    except OSError:
                        output = b""
                    if output:
                        self.last_output_activity = time.time()
                        try:
                            os.write(self.stdout_fd, output)
                        except OSError:
                            break
                    elif self.child.poll() is not None:
                        break

                if (
                    self.pending_enter
                    and time.monotonic() >= self.pending_enter
                ):
                    self.pending_enter = 0.0
                    self._write_child(self.pending_byte)

                if self.child.poll() is not None and not ready:
                    break
        finally:
            self._cleanup()

        return self.child.wait() if self.child else 1

    def _handle_input(self, data: bytes) -> None:
        # 后台语音桥在线时由它统一录音；否则保留终端内双击 Enter 作为兜底。
        if self._bridge_is_alive():
            self._write_child(data)
            return

        buffered = bytearray()
        for value in data:
            current = bytes([value])
            if current != b"\r":
                if self.pending_enter:
                    self.pending_enter = 0.0
                    self._write_child(self.pending_byte)
                buffered.extend(current)
                continue

            if self.pending_enter:
                self.pending_enter = 0.0
                if buffered:
                    self._write_child(bytes(buffered))
                    buffered.clear()
                self._toggle_recording()
            else:
                if buffered:
                    self._write_child(bytes(buffered))
                    buffered.clear()
                self.pending_byte = current
                self.pending_enter = time.monotonic() + 0.45

        if buffered:
            self._write_child(bytes(buffered))

    def _toggle_recording(self) -> None:
        if self.recording:
            self._stop_recording()
            return
        if stop_speaking():
            log("双击 Enter：已打断朗读")
            self._beep("Pop")
            return
        if self.processing:
            self._beep("Pop")
            return
        self._start_recording()

    def _start_recording(self) -> None:
        try:
            WAV_PATH.unlink(missing_ok=True)
        except OSError as exc:
            log(f"无法清理旧录音：{exc}")
            return
        command = [
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
            self.recording = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:
            log(f"麦克风启动失败：{exc}")
            return
        log("录音开始")
        self._beep("Tink")

    def _stop_recording(self) -> None:
        proc = self.recording
        self.recording = None
        if not proc:
            return
        if proc.poll() is None:
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
        log(f"录音结束 · {size / 1024:.0f} KB")
        if size < 2000:
            log("录音文件为空或过小")
            return
        self.processing = True
        threading.Thread(target=self._transcribe_and_inject, daemon=True).start()

    def _finish_if_recording_ended(self) -> None:
        if self.recording and self.recording.poll() is not None:
            log("录音达到时长上限，自动结束")
            self._stop_recording()

    def _transcribe_and_inject(self) -> None:
        try:
            started = time.monotonic()
            log("本地 Whisper 识别开始")
            text = transcribe(WAV_PATH).strip()
            elapsed = time.monotonic() - started
            if not text:
                log("识别结果为空")
                self._beep("Pop")
                return
            log(f"识别完成 · {elapsed:.1f}s · {text}")
            clean = re.sub(r"[\x00-\x1f\x7f]", " ", text)
            safe = " ".join(clean.split())
            self._record_user_activity()
            self._submit_text(safe)
            log("已写入 Codex 并提交")
        except Exception as exc:
            log(f"语音注入失败：{exc}")
            self._beep("Pop")
        finally:
            self.processing = False

    def _write_child(self, data: bytes) -> None:
        if not data or self.master_fd < 0:
            return
        with self.write_lock:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass

    def _submit_text(self, text: str) -> None:
        self._write_child(text.encode("utf-8"))
        time.sleep(0.15)
        self._write_child(b"\r")

    def _record_user_activity(self) -> None:
        now = time.time()
        self.last_user_activity = now
        if now - self.last_companion_state_write < 1.0:
            return
        self.last_companion_state_write = now
        mark_user_activity(
            COMPANION_DATABASE,
            COMPANION_SESSION_ID,
            now=now,
            base_interval_sec=COMPANION_BASE_INTERVAL_SECONDS,
        )

    def _maybe_trigger_companion(self) -> None:
        now = time.time()
        if now < self.next_companion_check:
            return
        self.next_companion_check = now + COMPANION_CHECK_INTERVAL_SECONDS
        if self.recording or self.processing or self.pending_enter:
            return
        state = read_proactive_state(
            COMPANION_DATABASE,
            COMPANION_SESSION_ID,
        )
        allowed, reason = should_trigger(
            state=state,
            now=now,
            last_input_at=self.last_user_activity,
            last_output_at=self.last_output_activity,
            last_trigger_at=self.last_companion_trigger,
            idle_seconds=COMPANION_IDLE_SECONDS,
            output_quiet_seconds=COMPANION_OUTPUT_QUIET_SECONDS,
            retry_seconds=COMPANION_RETRY_SECONDS,
        )
        if not allowed:
            return
        self.last_companion_trigger = now
        self._submit_text("/companion")
        log(
            "主动陪伴到期，已在当前 Codex 会话提交；"
            f"原因={reason}，静默={now - self.last_user_activity:.0f}s"
        )

    def _drain_inbox(self) -> None:
        if not INBOX_DIR.exists():
            return
        for path in sorted(INBOX_DIR.glob("*.txt")):
            try:
                text = path.read_text(encoding="utf-8").strip()
                path.unlink(missing_ok=True)
            except OSError as exc:
                log(f"读取语音输入失败：{exc}")
                continue
            if not text:
                continue
            clean = re.sub(r"[\x00-\x1f\x7f]", " ", text)
            safe = " ".join(clean.split())
            if not safe:
                continue
            self._record_user_activity()
            self._submit_text(safe)
            log(f"已从后台语音桥送入 Codex · {safe}")

    @staticmethod
    def _bridge_is_alive() -> bool:
        try:
            return time.time() - BRIDGE_HEARTBEAT.stat().st_mtime < 5
        except OSError:
            return False

    def _watch_final_answers(self, started_at: float) -> None:
        session_path = None
        while self.child and self.child.poll() is None:
            session_path = self._find_session_file(started_at, timeout=2.0)
            if session_path:
                break
            time.sleep(1.0)
        if not session_path:
            log("未定位当前 Codex 会话，回复朗读不可用")
            return
        log(f"回复朗读已连接 · {session_path.name}")

        try:
            offset = session_path.stat().st_size
        except OSError:
            return
        buffer = ""

        while self.child and self.child.poll() is None:
            try:
                size = session_path.stat().st_size
            except OSError:
                time.sleep(0.5)
                continue
            if size < offset:
                offset = 0
                buffer = ""
            if size == offset:
                time.sleep(0.25)
                continue

            try:
                with session_path.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                offset += len(chunk)
            except OSError:
                time.sleep(0.5)
                continue

            buffer += chunk.decode("utf-8", "replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                self._speak_final_answer(line)

    def _speak_final_answer(self, line: str) -> None:
        # Default GUI mode already speaks app-server deltas. The TUI session
        # file only stores completed messages, so reading it here would add a
        # delayed duplicate. Keep final-message TTS only for explicit queue mode.
        if self._bridge_is_alive():
            try:
                queue_mode = (
                    VOICE_QUEUE_MODE_FILE.read_text(encoding="utf-8").strip()
                    == "1"
                )
            except OSError:
                queue_mode = False
            if not queue_mode:
                return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        payload = event.get("payload", {})
        if (
            event.get("type") != "response_item"
            or payload.get("type") != "message"
            or payload.get("role") != "assistant"
            or payload.get("phase") != "final_answer"
        ):
            return

        message_id = str(payload.get("id", ""))
        if message_id and message_id in self.spoken_ids:
            return
        text = "\n".join(
            str(part.get("text", ""))
            for part in payload.get("content", [])
            if isinstance(part, dict) and part.get("text")
        ).strip()
        if not text:
            return
        if not tts_enabled():
            return
        self.spoken_ids.add(message_id)
        log(f"朗读最终回复 · {len(text)} 字")
        try:
            speak(text)
        except Exception as exc:
            log(f"朗读失败：{exc}")

    @staticmethod
    def _find_session_file(started_at: float, timeout: float = 15.0) -> Path | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = time.localtime()
            day_dir = (
                SESSIONS_DIR
                / time.strftime("%Y", current)
                / time.strftime("%m", current)
                / time.strftime("%d", current)
            )
            try:
                candidates = sorted(
                    day_dir.glob("*.jsonl"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                candidates = []

            for path in candidates[:20]:
                try:
                    if path.stat().st_mtime < started_at - 3:
                        continue
                    with path.open(encoding="utf-8", errors="replace") as handle:
                        first = json.loads(handle.readline())
                    payload = first.get("payload", {})
                    if first.get("type") != "session_meta":
                        continue
                    cwd = Path(str(payload.get("cwd", ""))).resolve()
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if cwd != Path.cwd().resolve():
                    continue
                try:
                    session_started = datetime.fromisoformat(
                        str(payload.get("timestamp", "")).replace("Z", "+00:00")
                    )
                except ValueError:
                    session_started = None
                if session_started is not None:
                    if session_started.timestamp() >= started_at - 5:
                        return path
                    continue
                try:
                    if path.stat().st_birthtime >= started_at - 3:
                        return path
                except OSError:
                    pass
            time.sleep(0.5)
        return None

    def _resize(self, fd: int) -> None:
        try:
            size = os.get_terminal_size(self.stdout_fd)
            packed = struct.pack("HHHH", size.lines, size.columns, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
        except OSError:
            pass

    def _on_resize(self, _signum, _frame) -> None:
        self.resize_pending = True

    @staticmethod
    def _beep(name: str) -> None:
        subprocess.Popen(
            ["/usr/bin/afplay", f"/System/Library/Sounds/{name}.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _cleanup(self) -> None:
        if self.recording and self.recording.poll() is None:
            try:
                self.recording.terminate()
            except OSError:
                pass
        try:
            PTY_ACTIVE.unlink()
        except OSError:
            pass
        try:
            PTY_HEARTBEAT.unlink()
        except OSError:
            pass
        try:
            termios.tcsetattr(
                self.stdin_fd, termios.TCSADRAIN, self.old_termios
            )
        except OSError:
            pass


def main() -> int:
    if not CODEX_BIN:
        print("找不到 codex 命令", file=sys.stderr)
        return 1
    args = sys.argv[1:]
    if not sys.stdin.isatty() or (args and args[0] in NON_INTERACTIVE):
        os.execv(CODEX_BIN, [CODEX_BIN, *args])
    try:
        return CodexVoicePTY(args).run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
