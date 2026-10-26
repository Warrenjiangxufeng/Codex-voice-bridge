#!/usr/bin/env python3
"""Speak new Codex final answers from an existing session file."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voice_lisa import OUT_DIR, speak, tts_enabled  # noqa: E402

SESSIONS_DIR = Path.home() / ".codex" / "sessions"
OPERATIONS_LOG = OUT_DIR / "operations.log"
PID_FILE = ROOT / ".session_tts_watch.pid"


def log(message: str) -> None:
    OPERATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with OPERATIONS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} [TTS-WATCH] {message}\n")


def session_started_at(path: Path) -> float | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = json.loads(handle.readline())
        if first.get("type") != "session_meta":
            return None
        payload = first.get("payload", {})
        if Path(str(payload.get("cwd", ""))).resolve() != ROOT:
            return None
        stamp = str(payload.get("timestamp", "")).replace("Z", "+00:00")
        return datetime.fromisoformat(stamp).timestamp()
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def find_session(since: float) -> Path | None:
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
        return None
    for path in candidates[:20]:
        started = session_started_at(path)
        if started is not None and started >= since - 10:
            return path
    return None


def speak_line(line: str, spoken: set[str]) -> None:
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
    if message_id and message_id in spoken:
        return
    text = "\n".join(
        str(part.get("text", ""))
        for part in payload.get("content", [])
        if isinstance(part, dict) and part.get("text")
    ).strip()
    if not text:
        return
    if message_id:
        spoken.add(message_id)
    if not tts_enabled():
        return
    log(f"朗读最终回复 · {len(text)} 字")
    try:
        speak(text)
    except Exception as exc:
        log(f"朗读失败：{exc}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", type=Path)
    parser.add_argument("--since", type=float, default=time.time() - 300)
    parser.add_argument("--max-idle", type=float, default=1800)
    args = parser.parse_args()

    session = args.session or find_session(args.since)
    if not session or not session.is_file():
        log("没有找到可监听的 Codex 会话")
        return 1

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

    def stop(_signum=None, _frame=None) -> None:
        PID_FILE.unlink(missing_ok=True)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    log(f"开始监听回复朗读 · {session.name}")
    offset = session.stat().st_size
    buffer = ""
    spoken: set[str] = set()
    last_growth = time.monotonic()
    try:
        while True:
            try:
                size = session.stat().st_size
            except OSError:
                time.sleep(0.5)
                continue
            if size < offset:
                offset = 0
                buffer = ""
            if size == offset:
                if time.monotonic() - last_growth > args.max_idle:
                    log("会话长时间无新内容，退出回复朗读监听")
                    return 0
                time.sleep(0.3)
                continue
            with session.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
            offset += len(chunk)
            last_growth = time.monotonic()
            buffer += chunk.decode("utf-8", "replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                speak_line(line, spoken)
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
