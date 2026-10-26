# -*- coding: utf-8 -*-
"""Codex app-server stream client and sentence-level streaming TTS."""
from __future__ import annotations

import json
import os
import queue
import selectors
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any


class CodexStreamError(RuntimeError):
    pass


class CodexAppServer:
    """Minimal JSON-RPC client for Codex's local app-server protocol."""

    def __init__(self, codex_bin: str, cwd: Path, timeout: float = 30) -> None:
        self.cwd = cwd
        self.timeout = timeout
        self.next_id = 1
        self.thread_id = ""
        self.line_buffer = bytearray()
        self.selector = selectors.DefaultSelector()
        self.proc = subprocess.Popen(
            [codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self._initialize()

    def _send(self, payload: dict[str, Any]) -> None:
        if self.proc.poll() is not None:
            raise CodexStreamError("Codex app-server 已退出")
        assert self.proc.stdin is not None
        self.proc.stdin.write(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        )
        self.proc.stdin.flush()

    def _read(self, timeout: float | None = None) -> dict[str, Any] | None:
        while b"\n" not in self.line_buffer:
            if not self.selector.select(self.timeout if timeout is None else timeout):
                return None
            assert self.proc.stdout is not None
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                return None
            self.line_buffer.extend(chunk)
        line, _, rest = self.line_buffer.partition(b"\n")
        self.line_buffer = bytearray(rest)
        try:
            return json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

    def _request(
        self, method: str, params: dict[str, Any], timeout: float = 30
    ) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = self._read(min(2.0, deadline - time.monotonic()))
            if message is None:
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise CodexStreamError(str(message["error"]))
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexStreamError(f"{method} 返回格式异常")
            return result
        raise CodexStreamError(f"{method} 超时")

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "LisaVoice",
                    "title": "Lisa Voice Bridge",
                    "version": "1.0",
                },
                "capabilities": None,
            },
        )
        self._send({"jsonrpc": "2.0", "method": "initialized"})

    def ensure_thread(self, thread_id: str = "") -> str:
        if thread_id:
            try:
                result = self._request(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "cwd": str(self.cwd),
                        "excludeTurns": True,
                    },
                    timeout=30,
                )
                thread = result.get("thread") or {}
                self.thread_id = str(thread.get("id") or thread_id)
                return self.thread_id
            except CodexStreamError:
                pass
        result = self._request(
            "thread/start",
            {
                "cwd": str(self.cwd),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": False,
            },
            timeout=30,
        )
        thread = result.get("thread") or {}
        self.thread_id = str(thread.get("id") or "")
        if not self.thread_id:
            raise CodexStreamError("thread/start 未返回线程 ID")
        return self.thread_id

    def stream_turn(
        self, text: str, thread_id: str = "", timeout: float = 600
    ) -> Iterator[tuple[str, str]]:
        thread_id = self.ensure_thread(thread_id)
        yield "thread", thread_id
        request_id = self.next_id
        self.next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "turn/start",
                "params": {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text}],
                },
            }
        )
        item_phase: dict[str, str] = {}
        delta_seen: set[str] = set()
        final_sent: set[str] = set()
        progress_seen: set[str] = set()
        progress_labels = {
            "reasoning": "正在分析…",
            "commandexecution": "正在执行命令…",
            "mcptoolcall": "正在调用工具…",
            "dynamictoolcall": "正在调用工具…",
            "filechange": "正在修改文件…",
            "websearch": "正在搜索资料…",
            "imagegeneration": "正在生成图片…",
            "collabagenttoolcall": "正在协调子任务…",
        }
        deadline = time.monotonic() + timeout
        while self.proc.poll() is None:
            if time.monotonic() >= deadline:
                raise CodexStreamError("Codex 流式回合超时")
            message = self._read(1.0)
            if message is None:
                continue
            if message.get("id") == request_id and message.get("error"):
                raise CodexStreamError(str(message["error"]))
            method = message.get("method")
            params = message.get("params") or {}
            if method == "item/started":
                item = params.get("item") or {}
                item_type = str(item.get("type") or "")
                item_id = str(item.get("id") or "")
                if item_type == "agentMessage" and item_id:
                    item_phase[str(item["id"])] = str(item.get("phase") or "")
                label = progress_labels.get(item_type.lower())
                if label and item_id not in progress_seen:
                    progress_seen.add(item_id)
                    yield "progress", label
            elif method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or "")
                phase = item_phase.get(item_id, "")
                delta = str(params.get("delta") or "")
                if delta and phase in {"", "final_answer"}:
                    delta_seen.add(item_id)
                    yield "delta", delta
            elif method == "item/completed":
                item = params.get("item") or {}
                item_id = str(item.get("id") or "")
                if (
                    item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"
                    and item_id not in delta_seen
                    and item_id not in final_sent
                ):
                    text_value = str(item.get("text") or "")
                    if text_value:
                        final_sent.add(item_id)
                        yield "final", text_value
            elif method == "error":
                raise CodexStreamError(str(params.get("message") or params))
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                for item in turn.get("items") or []:
                    item_id = str(item.get("id") or "")
                    if (
                        item.get("type") == "agentMessage"
                        and item.get("phase") == "final_answer"
                        and item_id not in delta_seen
                        and item_id not in final_sent
                    ):
                        text_value = str(item.get("text") or "")
                        if text_value:
                            final_sent.add(item_id)
                            yield "final", text_value
                return

    def close(self) -> None:
        try:
            self.selector.close()
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def __enter__(self) -> "CodexAppServer":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class StreamingSpeech:
    """Turn streaming text deltas into ordered sentence-level TTS playback."""

    STRONG = set("。！？!?；;\n")
    SOFT = set("，,、：:")

    def __init__(
        self,
        speak_fn: Callable[[str], None],
        cancel_file: Path,
        stream_file: Path,
        min_soft_chars: int = 26,
        max_chars: int = 90,
    ) -> None:
        self.speak_fn = speak_fn
        self.cancel_file = cancel_file
        self.stream_file = stream_file
        self.min_soft_chars = min_soft_chars
        self.max_chars = max_chars
        self.buffer = ""
        self.text = ""
        self.items: queue.Queue[str | None] = queue.Queue()
        self.worker = threading.Thread(target=self._run, daemon=True)
        try:
            self.cancel_file.unlink(missing_ok=True)
        except OSError:
            pass
        self.stream_file.touch()
        self.worker.start()

    def feed(self, delta: str) -> None:
        if not delta:
            return
        self.text += delta
        self.buffer += delta
        while True:
            segment, rest = self._take_segment(self.buffer)
            if not segment:
                break
            self.buffer = rest
            self.items.put(segment)

    def _take_segment(self, value: str) -> tuple[str, str]:
        for index, char in enumerate(value):
            if char in self.STRONG and index + 1 >= 3:
                segment = value[: index + 1].strip()
                return segment, value[index + 1 :]
        for index, char in enumerate(value):
            if char in self.SOFT and index + 1 >= self.min_soft_chars:
                segment = value[: index + 1].strip()
                return segment, value[index + 1 :]
        if len(value) >= self.max_chars:
            segment = value[: self.max_chars].strip()
            return segment, value[self.max_chars :]
        return "", value

    def _run(self) -> None:
        try:
            while True:
                item = self.items.get()
                if item is None:
                    return
                if self.cancel_file.exists():
                    continue
                try:
                    self.speak_fn(item)
                except Exception:
                    continue
                if self.cancel_file.exists():
                    continue
        finally:
            try:
                self.stream_file.unlink(missing_ok=True)
            except OSError:
                pass

    def close(self) -> str:
        tail = self.buffer.strip()
        if tail and not self.cancel_file.exists():
            self.items.put(tail)
        self.buffer = ""
        self.items.put(None)
        self.worker.join(timeout=max(30, min(300, len(self.text) // 2 + 20)))
        return self.text
