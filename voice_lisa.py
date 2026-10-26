# -*- coding: utf-8 -*-
"""
Voice Lisa：本地语音对话循环（免费、离线识别）

流程：按 Enter 开始录音 -> 再按 Enter 结束 -> faster-whisper 本地转文字
      -> codex exec（同一 thread）-> say 朗读回复

用法：
    ./venv/bin/python voice_lisa.py            # 语音对话
    ./venv/bin/python voice_lisa.py --text "你好"   # 只测文字链路
    ./venv/bin/python voice_lisa.py --wav test.wav  # 只测录音识别链路
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
from voice_stream import CodexAppServer, CodexStreamError, StreamingSpeech  # noqa: E402


def _select_model_dir() -> Path:
    """Prefer a complete small model, with base as a safe fallback."""
    override = os.environ.get("LISA_WHISPER_MODEL", "").strip()
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() else ROOT / path
    for name in ("faster-whisper-small", "faster-whisper-base"):
        path = ROOT / "model" / name
        if (path / "model.bin").is_file():
            return path
    return ROOT / "model" / "faster-whisper-base"


MODEL_DIR = _select_model_dir()
WHISPER_MODEL_NAME = MODEL_DIR.name
THREAD_FILE = ROOT / ".voice_thread_id"
ASR_MODE_FILE = ROOT / ".voice_asr_mode"
OUT_DIR = Path.home() / "Desktop" / "Lisa输出" / "语音对话"
OUT_DIR.mkdir(parents=True, exist_ok=True)

MIC_INDEX = "0"          # ffmpeg avfoundation 音频设备编号
MIC_NAME = "内建麦克风"
TTS_VOICE = "Ting-Ting"  # zh_CN
TTS_LOUDNORM_FILTER = "loudnorm=I=-14:LRA=7:TP=-1.5"
TTS_FALLBACK_FILTER = "alimiter=limit=0.98"
TTS_VOICE_ID = (
    "com.apple.speech.synthesis.voice.custom.siri.li-mu.premium"
)
TTS_HELPER = ROOT / "build" / "lisa_tts"
TTS_ENABLED_FILE = ROOT / ".voice_tts_enabled"
TTS_ENGINE_FILE = ROOT / ".voice_tts_engine"
TTS_PID_FILE = ROOT / ".tts_active.pid"
TTS_CANCEL_FILE = ROOT / ".tts_cancel"
TTS_STATE_FILE = ROOT / ".tts_state"
TTS_STREAM_FILE = ROOT / ".tts_streaming"
VOICE_QUEUE_MODE_FILE = ROOT / ".voice_queue_to_codex"
MAX_RECORD_SECONDS = 30
_TTS_LOCK = threading.Lock()
_TTS_PROCESSES: set[subprocess.Popen] = set()
_TTS_PROCESS_LOCK = threading.Lock()
_TTS_CANCEL = threading.Event()
_TTS_ACTIVE = threading.Event()
_FAST_MODEL = None


def _find_tool(name: str, *fallbacks: str) -> str:
    """Resolve tools even when launched by LaunchAgent with a minimal PATH."""
    for candidate in (shutil.which(name), *fallbacks):
        if candidate and Path(candidate).is_file():
            return candidate
    return name


@contextmanager
def _tts_session():
    TTS_CANCEL_FILE.unlink(missing_ok=True)
    TTS_STATE_FILE.write_text("speaking", encoding="utf-8")
    _TTS_CANCEL.clear()
    _TTS_ACTIVE.set()
    try:
        yield
    finally:
        _TTS_ACTIVE.clear()
        _TTS_CANCEL.clear()
        TTS_STATE_FILE.write_text("idle", encoding="utf-8")


def tts_is_active() -> bool:
    return _TTS_ACTIVE.is_set() or TTS_STREAM_FILE.exists()


def tts_state() -> str:
    """Shared speaking/idle state for the GUI and background TTS process."""
    if TTS_STREAM_FILE.exists():
        try:
            if time.time() - TTS_STREAM_FILE.stat().st_mtime <= 900:
                return "speaking"
            TTS_STREAM_FILE.unlink(missing_ok=True)
        except OSError:
            return "speaking"
    try:
        state = TTS_STATE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "idle"
    if state != "speaking":
        return "idle"
    age = 999.0
    try:
        age = time.time() - TTS_STATE_FILE.stat().st_mtime
    except OSError:
        return "idle"
    if age <= 5:
        return "speaking"
    try:
        pid = int(TTS_PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return "speaking"
    except ProcessLookupError:
        return "idle"
    except (OSError, ValueError):
        return "speaking" if TTS_PID_FILE.exists() else "idle"


def _tts_cancelled() -> bool:
    return _TTS_CANCEL.is_set() or TTS_CANCEL_FILE.exists()


def _is_tts_pid(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if result.returncode != 0:
        return True
    command = result.stdout.lower()
    return any(
        token in command
        for token in ("afplay", "piper", "lisa_tts", "/say ")
    )


def stop_speaking() -> bool:
    """Interrupt the current Piper/AVSpeech/say playback."""
    _TTS_CANCEL.set()
    TTS_CANCEL_FILE.touch()
    streaming = TTS_STREAM_FILE.exists()
    with _TTS_PROCESS_LOCK:
        processes = list(_TTS_PROCESSES)
    external_pid = 0
    try:
        external_pid = int(TTS_PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    if not processes and not _TTS_ACTIVE.is_set() and not external_pid and not streaming:
        return False
    for proc in processes:
        if proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
    if external_pid and _is_tts_pid(external_pid):
        try:
            os.kill(external_pid, signal.SIGTERM)
        except OSError:
            pass
    try:
        TTS_STATE_FILE.write_text("idle", encoding="utf-8")
    except OSError:
        pass
    return True


def _register_tts_process(proc: subprocess.Popen) -> None:
    with _TTS_PROCESS_LOCK:
        _TTS_PROCESSES.add(proc)
    try:
        TTS_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass


def _unregister_tts_process(proc: subprocess.Popen) -> None:
    with _TTS_PROCESS_LOCK:
        _TTS_PROCESSES.discard(proc)
    try:
        if TTS_PID_FILE.read_text(encoding="utf-8").strip() == str(proc.pid):
            TTS_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _tts_run(
    args: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> tuple[int, str, str]:
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    _register_tts_process(proc)
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise
    finally:
        _unregister_tts_process(proc)
    return proc.returncode or 0, out or "", err or ""


def _play_audio(path: Path, timeout: float = 60) -> None:
    normalized: Path | None = None
    try:
        if Path(FFMPEG_BIN).is_file():
            fd, name = tempfile.mkstemp(
                prefix="lisa_tts_norm_", suffix=".wav"
            )
            os.close(fd)
            normalized = Path(name)
            code, _, _ = _tts_run(
                [
                    FFMPEG_BIN,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-af",
                    TTS_LOUDNORM_FILTER,
                    "-ar",
                    "48000",
                    str(normalized),
                ],
                timeout=min(45, timeout),
            )
            if code != 0 or normalized.stat().st_size <= 2000:
                normalized.unlink(missing_ok=True)
                normalized = None

        playback_path = normalized or path
        if Path(FFPLAY_BIN).is_file():
            command = [
                FFPLAY_BIN,
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "quiet",
                str(playback_path),
            ]
        else:
            command = [AFPLAY_BIN, str(playback_path)]
        proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _register_tts_process(proc)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        finally:
            _unregister_tts_process(proc)
    finally:
        if normalized:
            normalized.unlink(missing_ok=True)


FFMPEG_BIN = _find_tool(
    "ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg"
)
CODEX_BIN = _find_tool(
    "codex", "/usr/local/bin/codex", "/opt/homebrew/bin/codex"
)
SAY_BIN = _find_tool("say", "/usr/bin/say")
AFPLAY_BIN = _find_tool("afplay", "/usr/bin/afplay")
FFPLAY_BIN = _find_tool(
    "ffplay", "/usr/local/bin/ffplay", "/opt/homebrew/bin/ffplay"
)
PIPER_BIN = _find_tool("piper", str(ROOT / "venv" / "bin" / "piper"))
EDGE_TTS_BIN = _find_tool(
    "edge-tts", str(ROOT / "venv" / "bin" / "edge-tts")
)
PIPER_MODEL = ROOT / "model" / "piper-huayan" / "zh_CN-huayan-medium.onnx"
PIPER_CONFIG = PIPER_MODEL.with_suffix(PIPER_MODEL.suffix + ".json")
PIPER_VOICES = {
    "piper": PIPER_MODEL,
    "piper-huayan": PIPER_MODEL,
    "piper-xiao_ya": ROOT / "model" / "piper-xiao_ya" / "zh_CN-xiao_ya-medium.onnx",
    "piper-chaowen": ROOT / "model" / "piper-chaowen" / "zh_CN-chaowen-medium.onnx",
}
PIPER_EN_MODEL = ROOT / "model" / "piper-en-amy" / "en_US-amy-medium.onnx"
PIPER_EN_CONFIG = PIPER_EN_MODEL.with_suffix(PIPER_EN_MODEL.suffix + ".json")
EDGE_VOICES = {
    "edge-xiaoxiao": "zh-CN-XiaoxiaoNeural",
    "edge-ava": "en-US-AvaMultilingualNeural",
    "edge-emma": "en-US-EmmaMultilingualNeural",
}
ENGLISH_SPAN = re.compile(
    r"[A-Za-z][A-Za-z0-9&+._/-]*(?:\s+[A-Za-z][A-Za-z0-9&+._/-]*)*"
)

HOTWORDS = (
    "以下是中文语音对话。常见词：Lisa、豆粕、美豆、巴西豆、大豆、期货、压榨、库存、"
    "基差、采购、价格、上涨、下跌、成本、进口、天气、播种、收割。"
)
FIXES = {
    "莉莎": "Lisa", "丽萨": "Lisa", "丽莎": "Lisa", "莉萨": "Lisa",
    "李三": "Lisa", "李撒": "Lisa", "李萨": "Lisa",
    "都破": "豆粕", "豆破": "豆粕", "斗破": "豆粕",
    "都迫": "豆粕", "都珀": "豆粕", "都粕": "豆粕", "豆迫": "豆粕",
    "美斗": "美豆", "美都": "美豆", "巴西斗": "巴西豆",
}

FIRST_PROMPT = (
    "[语音模式] 你是 Lisa，正在和主人进行语音对话。"
    "请用自然、简短的中文口语回答，通常不超过 3 句话；需要步骤时先给结论。"
    "不要复述记忆加载内容（如“已加载记忆”），直接回答。\n"
    "主人说：{text}"
)


def log(speaker: str, text: str) -> None:
    day = dt.date.today().isoformat()
    path = OUT_DIR / f"{day}.md"
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- {stamp} **{speaker}**：{text}\n")


def tts_enabled() -> bool:
    try:
        return TTS_ENABLED_FILE.read_text(encoding="utf-8").strip() != "0"
    except OSError:
        return True


def set_tts_enabled(enabled: bool) -> None:
    TTS_ENABLED_FILE.write_text("1" if enabled else "0", encoding="utf-8")


def tts_engine() -> str:
    override = os.environ.get("LISA_TTS_ENGINE", "").strip().lower()
    if override:
        return override
    try:
        value = TTS_ENGINE_FILE.read_text(encoding="utf-8").strip().lower()
    except OSError:
        value = ""
    return value or "piper"


def _piper_paths() -> tuple[Path, Path] | None:
    model = PIPER_VOICES.get(tts_engine())
    if model is None:
        model = PIPER_VOICES["piper-xiao_ya"]
    config = model.with_suffix(model.suffix + ".json")
    return model, config


def _piper_available() -> bool:
    paths = _piper_paths()
    return bool(
        paths
        and Path(PIPER_BIN).is_file()
        and paths[0].is_file()
        and paths[1].is_file()
    )


def record_wav(path: Path) -> bool:
    """录一段音频，按 Enter 结束。"""
    cmd = [
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "avfoundation", "-i", f":{MIC_INDEX}",
        "-t", str(MAX_RECORD_SECONDS), str(path),
    ]
    print(f"🎙  正在录音（{MIC_NAME}）…… 按 Enter 结束（最长 {MAX_RECORD_SECONDS}s 自动结束）")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    while proc.poll() is None:
        try:
            ready, _, _ = select.select([sys.stdin], [], [], 0.2)
        except (OSError, ValueError):
            ready = []
        if ready:
            try:
                sys.stdin.readline()
            except (EOFError, KeyboardInterrupt):
                pass
            break
    try:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    ok = path.exists() and path.stat().st_size > 2000
    print("⏹  录音结束" if ok else "⚠️  没录到声音（检查麦克风权限）")
    return ok


_MODEL = None


def _get_model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        if not (MODEL_DIR / "model.bin").is_file():
            raise FileNotFoundError(f"Whisper 模型缺少 model.bin：{MODEL_DIR}")
        print(f"🧠 Whisper 模型：{WHISPER_MODEL_NAME}", flush=True)
        _MODEL = WhisperModel(str(MODEL_DIR), device="cpu", compute_type="int8", local_files_only=True)
    return _MODEL


def _get_fast_model():
    global _FAST_MODEL
    if _FAST_MODEL is None:
        from faster_whisper import WhisperModel

        path = ROOT / "model" / "faster-whisper-base"
        if not (path / "model.bin").is_file():
            return _get_model()
        _FAST_MODEL = WhisperModel(
            str(path),
            device="cpu",
            compute_type="int8",
            local_files_only=True,
        )
    return _FAST_MODEL


def _audio_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / max(1, handle.getframerate())
    except (OSError, wave.Error):
        return 0.0


def transcribe(path: Path, on_partial=None) -> str:
    model = _get_model()
    segments, _ = model.transcribe(
        str(path),
        language="zh",
        vad_filter=True,
        beam_size=5,
        best_of=5,
        without_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=HOTWORDS,
        hotwords=HOTWORDS,
    )
    parts: list[str] = []
    for segment in segments:
        parts.append(segment.text)
        if on_partial:
            partial = "".join(parts).strip()
            for wrong, right in FIXES.items():
                partial = partial.replace(wrong, right)
            on_partial(partial)
    text = "".join(parts).strip()
    for wrong, right in FIXES.items():
        text = text.replace(wrong, right)
    return text


def _run_codex(args: list[str]) -> tuple[str, str | None]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, cwd=str(Path.home()),
                              stdin=subprocess.DEVNULL, timeout=600)
    except subprocess.TimeoutExpired:
        return "Codex 响应超时（10 分钟），请稍后重试。", None
    text_parts: list[str] = []
    thread_id = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        elif event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text_parts.append(item.get("text", ""))
    if proc.returncode != 0 and not text_parts:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        text_parts.append("Codex 调用失败：" + (err[-1] if err else f"exit {proc.returncode}"))
    return "\n".join(p for p in text_parts if p).strip(), thread_id


def ask_codex(text: str, timeout: int = 600) -> str:
    base = [
        CODEX_BIN, "exec", "--json", "--skip-git-repo-check", "-s", "read-only"
    ]
    thread_id = THREAD_FILE.read_text().strip() if THREAD_FILE.exists() else ""
    if thread_id:
        args = [
            CODEX_BIN,
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            thread_id,
            text,
        ]
        answer, _ = _run_codex(args)
        return answer or "（没有拿到回复）"
    else:
        args = base + [FIRST_PROMPT.format(text=text)]
    answer, new_thread = _run_codex(args)
    if new_thread:
        THREAD_FILE.write_text(new_thread)
    return answer or "（没有拿到回复）"


def ask_codex_stream(
    text: str,
    timeout: int = 600,
    stream_audio: bool = True,
    on_first_delta=None,
    on_progress=None,
    on_text_delta=None,
    client: CodexAppServer | None = None,
) -> str:
    """Ask Codex through app-server and speak final-answer deltas while they arrive."""
    thread_id = THREAD_FILE.read_text().strip() if THREAD_FILE.exists() else ""
    prompt = FIRST_PROMPT.format(text=text) if not thread_id else text
    speaker = (
        StreamingSpeech(speak, TTS_CANCEL_FILE, TTS_STREAM_FILE)
        if stream_audio and tts_enabled()
        else None
    )
    answer_parts: list[str] = []
    owns_client = client is None
    stream_error: Exception | None = None
    stream_started = time.monotonic()
    first_delta_seen = False
    try:
        if client is None:
            client = CodexAppServer(
                CODEX_BIN, Path.home(), timeout=min(timeout, 60)
            )
        for kind, payload in client.stream_turn(prompt, thread_id, timeout=timeout):
            if kind == "thread":
                thread_id = payload
                THREAD_FILE.write_text(thread_id, encoding="utf-8")
            elif kind == "progress":
                if on_progress:
                    on_progress(payload)
            elif kind in {"delta", "final"}:
                if not first_delta_seen:
                    first_delta_seen = True
                    if on_first_delta:
                        on_first_delta(time.monotonic() - stream_started)
                answer_parts.append(payload)
                if on_text_delta:
                    on_text_delta(payload)
                if speaker:
                    speaker.feed(payload)
    except CodexStreamError as exc:
        stream_error = exc
    finally:
        if owns_client and client:
            client.close()
        if speaker:
            speaker.close()
    answer = "".join(answer_parts).strip()
    if answer:
        return answer
    if stream_error:
        # The experimental app-server can be unavailable on older Codex builds.
        # Preserve voice functionality by falling back to the regular exec path.
        answer = ask_codex(text, timeout=timeout)
        if stream_audio and tts_enabled():
            speak(answer)
        return answer
    return "（没有拿到回复）"


def queue_to_codex(text: str, timeout: int = 30) -> str:
    """把识别文字排入当前 Codex 会话，让回复显示在 Codex 界面。"""
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id and THREAD_FILE.exists():
        thread_id = THREAD_FILE.read_text().strip()
    if not thread_id:
        raise RuntimeError("没有可用的 Codex 会话 ID")

    proc = subprocess.run(
        [CODEX_BIN, "queue", "--thread", thread_id, "--message", text],
        capture_output=True,
        text=True,
        cwd=str(Path.home()),
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"exit {proc.returncode}")
    return thread_id


def speak(text: str) -> None:
    if not tts_enabled():
        return

    clean = text
    clean = re.sub(r"```.*?```", " 代码略。", clean, flags=re.S)
    clean = re.sub(r"https?://\S+", " 链接略。", clean)
    clean = re.sub(r"^[#>\-*\s]+", "", clean, flags=re.M)
    clean = re.sub(r"[`*_\[\]()]", "", clean)
    clean = clean.replace("…", "，").replace("——", "，")
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return

    with _TTS_LOCK, _tts_session():
        edge_voice = EDGE_VOICES.get(tts_engine())
        if edge_voice and Path(EDGE_TTS_BIN).is_file():
            edge_path: Path | None = None
            try:
                edge_path = _edge_synthesize_to(clean, edge_voice)
                _play_audio(
                    edge_path,
                    timeout=max(
                        30, min(300, len(clean) // 3 + 15)
                    ),
                )
                return
            except (OSError, RuntimeError, subprocess.SubprocessError):
                if _tts_cancelled():
                    return
            finally:
                if edge_path:
                    edge_path.unlink(missing_ok=True)

        if tts_engine() != "macos" and _piper_available():
            paths = _piper_paths()
            assert paths is not None
            piper_model, piper_config = paths
            env = os.environ.copy()
            env["HF_HUB_OFFLINE"] = "1"
            temp_paths: list[Path] = []
            try:
                segments = [
                    (kind, chunk.strip())
                    for kind, chunk in _bilingual_segments(clean)
                    if chunk.strip()
                ]
                can_mix = (
                    PIPER_EN_MODEL.is_file()
                    and PIPER_EN_CONFIG.is_file()
                    and any(kind == "en" for kind, _ in segments)
                )
                if can_mix:
                    final_path = _piper_bilingual_to(
                        clean, piper_model, piper_config, env
                    )
                    temp_paths.append(final_path)
                    _play_audio(
                        final_path,
                        timeout=max(30, min(300, len(clean) // 3 + 15)),
                    )
                    return

                final_path = _piper_synthesize_to(
                    clean, piper_model, piper_config, env
                )
                temp_paths.append(final_path)
                _play_audio(
                    final_path,
                    timeout=max(30, min(300, len(clean) // 3 + 15)),
                )
                return
            except (OSError, RuntimeError, subprocess.SubprocessError):
                if _tts_cancelled():
                    return
            finally:
                for path in temp_paths:
                    path.unlink(missing_ok=True)

        if _tts_cancelled():
            return
        if TTS_HELPER.is_file() and os.access(TTS_HELPER, os.X_OK):
            code, _, _ = _tts_run(
                [str(TTS_HELPER), "--voice", TTS_VOICE_ID, clean],
                timeout=max(30, min(300, len(clean) // 4 + 10)),
            )
            if code == 0:
                return
        if _tts_cancelled():
            return
        _tts_run(
            [SAY_BIN, "-v", TTS_VOICE, "-r", "175", clean],
            timeout=max(30, min(300, len(clean) // 4 + 10)),
        )


def _bilingual_segments(text: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    position = 0
    for match in ENGLISH_SPAN.finditer(text):
        if match.start() > position:
            segments.append(("zh", text[position : match.start()]))
        segments.append(("en", match.group()))
        position = match.end()
    if position < len(text):
        segments.append(("zh", text[position:]))
    return segments


def _piper_synthesize_to(
    text: str, model: Path, config: Path, env: dict[str, str]
) -> Path:
    fd, name = tempfile.mkstemp(prefix="lisa_piper_", suffix=".wav")
    os.close(fd)
    path = Path(name)
    try:
        code, out, err = _tts_run(
            [
                PIPER_BIN,
                "-m",
                str(model),
                "-c",
                str(config),
                "--data-dir",
                str(ROOT),
                "-f",
                str(path),
                "--sentence-silence",
                "0.12",
            ],
            input_text=text + "\n",
            env=env,
            timeout=max(30, min(300, len(text) // 4 + 15)),
        )
        if code != 0 or path.stat().st_size <= 2000:
            raise RuntimeError(
                (err or out or "piper failed").strip()
            )
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _piper_bilingual_to(
    text: str, zh_model: Path, zh_config: Path, env: dict[str, str]
) -> Path:
    fd, name = tempfile.mkstemp(prefix="lisa_mixed_", suffix=".wav")
    os.close(fd)
    path = Path(name)
    try:
        code, out, err = _tts_run(
            [
                sys.executable,
                str(ROOT / "tools" / "piper_bilingual.py"),
                "--zh-model",
                str(zh_model),
                "--zh-config",
                str(zh_config),
                "--en-model",
                str(PIPER_EN_MODEL),
                "--en-config",
                str(PIPER_EN_CONFIG),
                "--data-dir",
                str(ROOT),
                "--out",
                str(path),
            ],
            input_text=text + "\n",
            env=env,
            timeout=max(60, min(300, len(text) // 4 + 30)),
        )
        if code != 0 or path.stat().st_size <= 2000:
            raise RuntimeError(
                (err or out or "bilingual piper failed").strip()
            )
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _edge_synthesize_to(text: str, voice: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="lisa_edge_", suffix=".mp3")
    os.close(fd)
    path = Path(name)
    try:
        code, out, err = _tts_run(
            [
                EDGE_TTS_BIN,
                "--voice",
                voice,
                "--text",
                text,
                "--write-media",
                str(path),
            ],
            timeout=max(30, min(300, len(text) // 4 + 20)),
        )
        if code != 0 or path.stat().st_size <= 2000:
            raise RuntimeError(
                (err or out or "edge-tts failed").strip()
            )
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _edge_stream_play(text: str, voice: str, timeout: float = 60) -> None:
    """Stream Edge MP3 chunks into ffplay to reduce first-audio latency."""
    if not Path(FFPLAY_BIN).is_file():
        raise RuntimeError("ffplay 不可用")

    proc = subprocess.Popen(
        [
            FFPLAY_BIN,
            "-nodisp",
            "-autoexit",
            "-loglevel",
            "quiet",
            "-af",
            TTS_FALLBACK_FILTER,
            "-fflags",
            "nobuffer",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _register_tts_process(proc)
    assert proc.stdin is not None

    async def pump() -> None:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice)
        stream = communicate.stream()
        try:
            async for chunk in stream:
                if _tts_cancelled():
                    break
                if chunk.get("type") != "audio":
                    continue
                data = chunk.get("data") or b""
                if not data:
                    continue
                try:
                    proc.stdin.write(data)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    break
        finally:
            await stream.aclose()
            await asyncio.sleep(0)

    try:
        asyncio.run(pump())
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=timeout)
        if proc.returncode not in (0, None) and not _tts_cancelled():
            raise RuntimeError(f"ffplay 退出码 {proc.returncode}")
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise
    finally:
        _unregister_tts_process(proc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="直接发一句话（不录音）")
    ap.add_argument("--wav", help="识别指定 wav（不录音）")
    ap.add_argument("--no-speak", action="store_true")
    args = ap.parse_args()

    # 单实例锁：防止重复双击导致两个进程抢麦克风
    lock_fp = (ROOT / ".voice.lock").open("w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("语音 Lisa 已经在运行（请切到已有的控制台窗口）。")
        return 0

    if not MODEL_DIR.exists():
        print(f"缺少模型目录：{MODEL_DIR}")
        return 1

    if args.text:
        answer = ask_codex_stream(args.text, stream_audio=not args.no_speak)
        print(answer)
        log("你", args.text)
        log("Lisa", answer)
        return 0

    if args.wav:
        text = transcribe(Path(args.wav))
        print("识别：", text)
        return 0 if text else 1

    print("🎧 Voice Lisa 已启动（本地识别 / Ting-Ting 朗读）")
    print("按 Enter 开始说话，再按 Enter 结束；直接输入文字也可发送；输入 q 退出。\n")
    while True:
        try:
            typed = input("▶ 回车说话（或直接输入文字 / q 退出）：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if typed.lower() in ("q", "quit", "exit", "退出"):
            break
        if typed:
            text = typed
        else:
            wav = ROOT / "last_input.wav"
            if not record_wav(wav):
                continue
            print("🧠 识别中……")
            t0 = time.time()
            text = transcribe(wav)
            print(f"你：{text}（{time.time()-t0:.1f}s）")
            if not text:
                print("⚠️ 没听清，再说一次。")
                continue
        log("你", text)
        print("💭 Lisa 思考中……")
        answer = ask_codex_stream(text, stream_audio=not args.no_speak)
        print(f"Lisa：{answer}\n")
        log("Lisa", answer)
    print("👋 退出语音模式")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
