#!/usr/bin/env python3
"""Local web UI for Lisa Voice with the existing Codex/audio backend."""
from __future__ import annotations

import argparse
import base64
import io
import json
import mimetypes
import os
import queue
import re
import struct
import subprocess
import tempfile
import threading
import time
import wave
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image, ImageOps

from voice_activity import VoiceActivityDetector
from voice_lisa import (
    CODEX_BIN,
    OUT_DIR,
    _get_model,
    ask_codex_stream,
    log,
    set_tts_enabled,
    stop_speaking,
    transcribe,
    tts_enabled,
    tts_state,
)
from voice_stream import CodexAppServer

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
WAV_PATH = ROOT / "last_input.wav"
WEBVIEW_BIN = ROOT / "build" / "lisa_webview"
HOTKEY_HELPER = ROOT / "build" / "voice_hotkey"
LOCK_FILE = ROOT / ".voice_web.lock"
APP_BUNDLE = Path.home() / "Desktop" / "Codex Voice Bridge.app"
APP_ICON = APP_BUNDLE / "Contents" / "Resources" / "AppIcon.icns"


def _write_icns(image: Image.Image, target: Path) -> None:
    icon_types = {
        b"icp4": 16,
        b"icp5": 32,
        b"icp6": 64,
        b"ic07": 128,
        b"ic08": 256,
        b"ic09": 512,
        b"ic10": 1024,
    }
    chunks: list[bytes] = []
    for icon_type, size in icon_types.items():
        buffer = io.BytesIO()
        image.resize(
            (size, size),
            Image.Resampling.LANCZOS,
        ).save(buffer, format="PNG")
        payload = buffer.getvalue()
        chunks.append(
            icon_type + struct.pack(">I", len(payload) + 8) + payload
        )
    body = b"".join(chunks)
    content = b"icns" + struct.pack(">I", len(body) + 8) + body
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, target)


def _sync_desktop_icon(image: Image.Image) -> bool:
    if not APP_ICON.parent.is_dir():
        return False
    try:
        _write_icns(image, APP_ICON)
        os.utime(APP_BUNDLE, None)
        registrar = Path(
            "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
            "LaunchServices.framework/Support/lsregister"
        )
        if registrar.is_file():
            subprocess.run(
                [str(registrar), "-f", str(APP_BUNDLE)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
        return True
    except Exception:
        return False


class VoiceService:
    def __init__(self, preview: bool = False) -> None:
        self.preview = preview
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue[dict[str, Any]]] = []
        self.history: list[dict[str, Any]] = []
        self.sequence = 0
        self.status = "待唤醒"
        self.state = "idle"
        self.processing = False
        self.recording = False
        self.wake_armed = False
        self.manual_capture = False
        self.pending_prompt: str | None = None
        self.codex_client: CodexAppServer | None = None
        self.codex_ready = threading.Event()
        self.vad: VoiceActivityDetector | None = None
        self.last_tts_state = "idle"
        self.wake_check_lock = threading.Lock()
        self.wake_check_active = False
        self.last_wake_ack_at = 0.0
        self.hotkey_process: subprocess.Popen[bytes] | None = None
        if not preview:
            threading.Thread(target=self._prewarm_codex, daemon=True).start()
            threading.Thread(target=self._preload_wake_model, daemon=True).start()
            self._start_vad()
            self._start_hotkey()
            threading.Thread(target=self._watch_tts, daemon=True).start()

    def _publish(
        self,
        kind: str,
        payload: dict[str, Any],
        remember: bool = False,
        broadcast: bool = True,
    ) -> None:
        with self.lock:
            self.sequence += 1
            event = {
                "type": kind,
                "seq": self.sequence,
                "time": time.strftime("%H:%M:%S"),
                **payload,
            }
            if remember:
                self.history.append(event)
                self.history = self.history[-100:]
            subscribers = list(self.subscribers)
        if broadcast:
            for target in subscribers:
                try:
                    target.put_nowait(event)
                except queue.Full:
                    pass

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "status": self.status,
                "state": self.state,
                "processing": self.processing,
                "recording": self.recording,
                "tts_enabled": tts_enabled(),
                "history": list(self.history),
            }

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        target: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=300)
        with self.lock:
            self.subscribers.append(target)
        return target

    def unsubscribe(self, target: queue.Queue[dict[str, Any]]) -> None:
        with self.lock:
            if target in self.subscribers:
                self.subscribers.remove(target)

    def _set_status(self, status: str, state: str) -> None:
        self.status = status
        self.state = state
        self._publish("status", {"status": status, "state": state})

    def message(self, speaker: str, text: str) -> None:
        self._publish(
            "message",
            {"speaker": speaker, "text": text.strip()},
            remember=True,
        )

    def progress(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] [WEB] {text}"
        print(line, flush=True)
        try:
            with (OUT_DIR / "operations.log").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(f"{time.strftime('%Y-%m-%d')} {line}\n")
        except OSError:
            pass
        self._publish("progress", {"text": text})

    def debug(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] [VAD] {text}"
        print(line, flush=True)
        try:
            with (OUT_DIR / "operations.log").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(f"{time.strftime('%Y-%m-%d')} {line}\n")
        except OSError:
            pass

    def _prewarm_codex(self) -> None:
        self.progress("Codex 后端启动中")
        try:
            client = CodexAppServer(CODEX_BIN, Path.home(), timeout=30)
            with self.lock:
                self.codex_client = client
            self.progress("Codex 后端已就绪")
        except Exception as exc:
            self.progress(f"Codex 启动失败：{exc}")
        finally:
            self.codex_ready.set()

    def _get_codex_client(self) -> CodexAppServer:
        self.codex_ready.wait(timeout=15)
        with self.lock:
            client = self.codex_client
            if client and client.proc.poll() is None:
                return client
            if client:
                client.close()
            client = CodexAppServer(CODEX_BIN, Path.home(), timeout=30)
            self.codex_client = client
            return client

    def _start_vad(self) -> None:
        try:
            self.vad = VoiceActivityDetector(
                WAV_PATH,
                is_speaking=lambda: tts_state() == "speaking",
                is_wake_armed=lambda: self.wake_armed,
                can_capture=lambda: True,
                on_speech_start=self._vad_start,
                on_utterance=self._vad_utterance,
                on_barge_in=self._vad_barge_candidate,
                on_error=self._vad_error,
                on_debug=self.debug,
                on_checkpoint=self._wake_checkpoint,
            )
            self.vad.start()
            self.progress("免按键唤醒已开启")
        except Exception as exc:
            self.vad = None
            self.progress(f"免按键唤醒不可用：{exc}")

    def _start_hotkey(self) -> None:
        if not HOTKEY_HELPER.is_file():
            return
        try:
            self.hotkey_process = subprocess.Popen(
                [str(HOTKEY_HELPER), str(os.getpid())],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
            threading.Thread(
                target=self._read_hotkey, daemon=True
            ).start()
        except Exception as exc:
            self.progress(f"Enter 快键不可用：{exc}")

    def _read_hotkey(self) -> None:
        proc = self.hotkey_process
        if not proc or not proc.stdout:
            return
        for raw in proc.stdout:
            if raw.strip() == "TOGGLE":
                self.toggle_record()

    def _vad_start(self) -> None:
        self.recording = True
        if self.wake_armed or self.manual_capture:
            self._set_status("正在聆听", "listening")

    def _vad_barge_candidate(self) -> None:
        self.progress("检测到语音，等待 Lisa 唤醒词")

    def _preload_wake_model(self) -> None:
        try:
            self._get_wake_model()
        except Exception as exc:
            self.progress(f"快速唤醒模型不可用：{exc}")

    def _get_wake_model(self):
        return _get_model()

    def _wake_checkpoint(self, capture: bytes) -> None:
        with self.wake_check_lock:
            if self.wake_check_active:
                return
            if time.monotonic() - self.last_wake_ack_at < 10.0:
                return
            self.wake_check_active = True
        threading.Thread(
            target=self._check_wake_checkpoint,
            args=(capture,),
            daemon=True,
        ).start()

    def _check_wake_checkpoint(self, capture: bytes) -> None:
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="lisa_wake_",
                suffix=".wav",
                dir=str(ROOT),
                delete=False,
            ) as handle:
                path = Path(handle.name)
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(capture)
            segments, _ = self._get_wake_model().transcribe(
                str(path),
                language="zh",
                beam_size=3,
                best_of=3,
                temperature=0.0,
                vad_filter=False,
                no_speech_threshold=0.85,
                initial_prompt=(
                    "语音唤醒词是 Lisa，也可能识别为丽莎、莉莎、"
                    "丽萨、莉萨、李三、李撒、李萨。"
                ),
                hotwords="Lisa 丽莎 莉莎 丽萨 莉萨 李三 李撒 李萨",
                condition_on_previous_text=False,
            )
            text = "".join(segment.text for segment in segments).strip()
            normalized = text.lower()
            if not any(
                token in normalized
                for token in (
                    "lisa",
                    "丽莎",
                    "莉莎",
                    "丽萨",
                    "莉萨",
                    "李三",
                    "李撒",
                    "李萨",
                )
            ):
                return
            self._acknowledge_wake(text)
        except Exception as exc:
            self.progress(f"快速唤醒检查失败：{exc}")
        finally:
            if path:
                path.unlink(missing_ok=True)
            with self.wake_check_lock:
                self.wake_check_active = False

    def _acknowledge_wake(self, text: str) -> None:
        now = time.monotonic()
        if now - self.last_wake_ack_at < 10.0:
            return
        self.last_wake_ack_at = now
        self.wake_armed = True
        self._set_status("正在聆听", "listening")
        self.progress(f"快速唤醒：{text[:40]}")
        if tts_state() == "speaking":
            stop_speaking()
        self.message("lisa", "收到！")
        self._set_status("正在聆听", "listening")

    def _vad_error(self, message: str) -> None:
        self.vad = None
        self._set_status(f"自动聆听不可用：{message}", "warning")

    def _vad_utterance(self, path: Path | None) -> None:
        manual = self.manual_capture
        self.manual_capture = False
        self.recording = False
        if not path:
            self._set_status("待唤醒", "idle")
            return
        if manual:
            self.progress("Enter 手动结束，立即识别")
        threading.Thread(
            target=self._process_audio,
            args=(path, manual),
            daemon=True,
        ).start()

    def _process_audio(self, path: Path, manual: bool) -> None:
        try:
            started = time.monotonic()
            self._publish("stt_start", {})
            text = transcribe(
                path,
                on_partial=lambda partial: self._publish(
                    "stt_partial", {"text": partial}
                ),
            ).strip()
            if not text:
                self._set_status("待唤醒", "idle")
                return
            wake, command = self._extract_wake(text)
            if not wake and not self.wake_armed:
                self.progress(f"未检测到 Lisa，忽略：{text[:50]}")
                self._set_status("待唤醒", "idle")
                return
            recent_ack = time.monotonic() - self.last_wake_ack_at < 10.0
            if wake and tts_state() == "speaking" and not recent_ack:
                stop_speaking()
                self.progress("检测到 Lisa，已停止朗读")
            if wake:
                if not recent_ack:
                    self._acknowledge_wake(text)
                else:
                    self.wake_armed = True
            if wake and not command:
                self.wake_armed = True
                self._set_status("正在聆听", "listening")
                self.progress("收到，等待指令")
                return
            self.wake_armed = False
            self._set_status("正在识别", "recognizing")
            self.progress(
                f"语音识别 · {time.monotonic() - started:.1f}s"
            )
            log("你", command)
            self.submit(command)
        except Exception as exc:
            self.progress(f"语音识别失败：{exc}")
            self._set_status("待唤醒", "idle")
        finally:
            self._publish("stt_done", {})

    @staticmethod
    def _extract_wake(text: str) -> tuple[bool, str]:
        match = re.search(r"lisa", text, flags=re.IGNORECASE)
        if not match:
            return False, text.strip()
        return (
            True,
            text[match.end() :].lstrip(" ,，。.!！?？:：-").strip(),
        )

    def submit(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.processing:
            self.pending_prompt = text
            self.progress("已收到，等待上一轮结束")
            return
        self.processing = True
        self.message("you", text)
        self._set_status("正在思考", "thinking")
        threading.Thread(
            target=self._run_codex,
            args=(text,),
            daemon=True,
        ).start()

    def _run_codex(self, text: str) -> None:
        message_id = f"lisa-{time.time_ns()}"
        assistant_started = False

        def start_assistant() -> None:
            nonlocal assistant_started
            if not assistant_started:
                assistant_started = True
                self._publish("assistant_start", {"id": message_id})

        def emit_delta(delta: str) -> None:
            start_assistant()
            self._publish(
                "assistant_delta",
                {"id": message_id, "text": delta},
            )

        try:
            self.progress("正在分析")
            client = self._get_codex_client()
            answer = ask_codex_stream(
                text,
                client=client,
                on_progress=self.progress,
                on_first_delta=lambda _seconds: self.progress("正在生成回复"),
                on_text_delta=emit_delta,
            )
            log("Lisa", answer)
            start_assistant()
            self._publish(
                "assistant_done",
                {"id": message_id, "text": answer},
            )
            self._publish(
                "message",
                {"speaker": "lisa", "text": answer.strip()},
                remember=True,
                broadcast=False,
            )
            self.debug("回复完成")
        except Exception as exc:
            error_text = f"请求失败：{exc}"
            start_assistant()
            self._publish(
                "assistant_done",
                {"id": message_id, "text": error_text},
            )
            self._publish(
                "message",
                {"speaker": "lisa", "text": error_text},
                remember=True,
                broadcast=False,
            )
            self.debug(f"请求失败：{exc}")
        finally:
            self.processing = False
            if self.pending_prompt:
                pending = self.pending_prompt
                self.pending_prompt = None
                self.submit(pending)
            else:
                if tts_state() == "speaking":
                    self._set_status("正在朗读", "speaking")
                else:
                    self._set_status("待唤醒", "idle")

    def toggle_record(self) -> None:
        if self.processing:
            self.progress("当前正在处理，请稍后")
            return
        if not self.vad:
            self.progress("自动聆听不可用")
            return
        if self.recording:
            self.vad.force_stop()
            return
        if tts_state() == "speaking":
            stop_speaking()
        self.manual_capture = True
        self._set_status("正在聆听", "listening")
        self.vad.force_start()

    def toggle_tts(self) -> bool:
        enabled = not tts_enabled()
        set_tts_enabled(enabled)
        self._publish("tts", {"enabled": enabled})
        return enabled

    def clear(self) -> None:
        with self.lock:
            self.history.clear()
        self._publish("clear", {})

    def _watch_tts(self) -> None:
        while True:
            state = "speaking" if tts_state() == "speaking" else "idle"
            if state != self.last_tts_state:
                self.last_tts_state = state
                self._publish("tts_state", {"state": state})
            time.sleep(0.2)

    def close(self) -> None:
        if self.hotkey_process and self.hotkey_process.poll() is None:
            self.hotkey_process.terminate()
        if self.vad:
            self.vad.stop()
        with self.lock:
            if self.codex_client:
                self.codex_client.close()
                self.codex_client = None


class LisaHandler(BaseHTTPRequestHandler):
    server: "LisaHTTPServer"

    def log_message(self, *_args) -> None:
        return

    def _send_json(
        self, payload: dict[str, Any], status: int = HTTPStatus.OK
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            self._send_json(self.server.service.snapshot())
            return
        if path == "/api/events":
            target = self.server.service.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                snapshot = self.server.service.snapshot()
                data = json.dumps(
                    {"type": "snapshot", **snapshot},
                    ensure_ascii=False,
                )
                self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        event = target.get(timeout=15)
                        data = json.dumps(event, ensure_ascii=False)
                        self.wfile.write(
                            f"data: {data}\n\n".encode("utf-8")
                        )
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self.server.service.unsubscribe(target)
            return
        if path == "/":
            path = "/index.html"
        safe = Path(path.lstrip("/"))
        target = (WEB_ROOT / safe).resolve()
        if WEB_ROOT not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        )
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        service = self.server.service
        try:
            if path == "/api/text":
                service.submit(str(body.get("text", "")))
            elif path == "/api/avatar":
                encoded = str(body.get("image", ""))
                if not encoded.startswith("data:image/"):
                    self._send_json(
                        {"error": "请选择图片文件"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                try:
                    _, payload = encoded.split(",", 1)
                    raw = base64.b64decode(payload, validate=True)
                    if len(raw) > 10 * 1024 * 1024:
                        raise ValueError("头像图片不能超过 10 MB")
                    image = Image.open(io.BytesIO(raw))
                    image.load()
                    image = ImageOps.fit(
                        image.convert("RGB"),
                        (1024, 1024),
                        method=Image.Resampling.LANCZOS,
                    )
                    image.save(
                        WEB_ROOT / "avatar.png",
                        format="PNG",
                        optimize=True,
                    )
                    desktop_icon = _sync_desktop_icon(image)
                except Exception as exc:
                    self._send_json(
                        {"error": f"头像处理失败：{exc}"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                self._send_json(
                    {
                        "ok": True,
                        "version": time.time_ns(),
                        "desktop_icon": desktop_icon,
                    }
                )
                return
            elif path == "/api/record":
                service.toggle_record()
            elif path == "/api/tts":
                service.toggle_tts()
            elif path == "/api/stop":
                stop_speaking()
            elif path == "/api/clear":
                service.clear()
            else:
                self._send_json(
                    {"error": "not found"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True})
        except Exception as exc:
            self._send_json(
                {"error": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )


class LisaHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, service: VoiceService) -> None:
        super().__init__(address, LisaHandler)
        self.service = service


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--serve-only", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()

    lock = LOCK_FILE.open("w")
    import fcntl

    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0

    service = VoiceService(preview=args.preview)
    server = LisaHTTPServer(("127.0.0.1", args.port), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"WEB_URL {url}", flush=True)
    if args.serve_only:
        print(url, flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        proc = subprocess.Popen([str(WEBVIEW_BIN), url])
        proc.wait()
    server.shutdown()
    service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
