# -*- coding: utf-8 -*-
"""Always-on voice activity detection with barge-in support."""
from __future__ import annotations

import audioop
import array
import json
import os
import subprocess
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUDIO_HELPER = ROOT / "build" / "lisa_audio"
PROFILE_FILE = ROOT / ".voice_vad_profile.json"
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
FRAME_SECONDS = 0.02
FRAME_BYTES = int(SAMPLE_RATE * FRAME_SECONDS * SAMPLE_WIDTH)
PREROLL_FRAMES = 20
END_SILENCE_FRAMES = 150
MIN_SPEECH_FRAMES = 12
MAX_CAPTURE_SECONDS = 30


class VoiceActivityDetector:
    """Capture utterances automatically and report possible user barge-in."""

    def __init__(
        self,
        wav_path: Path,
        *,
        is_speaking: Callable[[], bool],
        can_capture: Callable[[], bool],
        on_speech_start: Callable[[], None],
        on_utterance: Callable[[Path | None], None],
        on_barge_in: Callable[[], None],
        on_error: Callable[[str], None],
        on_debug: Callable[[str], None] | None = None,
        on_checkpoint: Callable[[bytes], None] | None = None,
        is_wake_armed: Callable[[], bool] | None = None,
    ) -> None:
        self.wav_path = wav_path
        self.is_speaking = is_speaking
        self.is_wake_armed = is_wake_armed or (lambda: False)
        self.can_capture = can_capture
        self.on_speech_start = on_speech_start
        self.on_utterance = on_utterance
        self.on_barge_in = on_barge_in
        self.on_error = on_error
        self.on_debug = on_debug
        self.on_checkpoint = on_checkpoint

        self.proc: subprocess.Popen[bytes] | None = None
        self.error_lines: deque[str] = deque(maxlen=12)
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.preroll: deque[bytes] = deque(maxlen=PREROLL_FRAMES)
        self.capture = bytearray()
        self.capturing = False
        self.voiced_run = 0
        self.silence_frames = 0
        self.speech_frames = 0
        self.checkpoint_count = 0
        self.next_checkpoint_seconds = 0.6
        self.noise_floor = 110.0
        self.speech_level = 2500.0
        self.voiced_levels: deque[float] = deque(maxlen=200)
        self.started_at = 0.0
        self.frame_count = 0
        self.peak_rms = 0.0
        self.last_debug = time.monotonic()
        self.next_listen_at = 0.0
        self.source_rate = 0
        self.source_channels = 0
        self.rate_state = None
        self.float_pending = bytearray()
        self.pcm_pending = bytearray()
        self._load_profile()

    def start(self) -> None:
        if not AUDIO_HELPER.is_file():
            raise RuntimeError(f"缺少音频采集器：{AUDIO_HELPER}")
        self.proc = subprocess.Popen(
            [str(AUDIO_HELPER)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        threading.Thread(target=self._read_audio, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def force_start(self) -> None:
        with self.lock:
            if self.capturing:
                return
            speaking = self.is_speaking()
            self.capture = bytearray(b"".join(self.preroll))
            self.capturing = True
            self.voiced_run = 0
            self.silence_frames = 0
            self.speech_frames = 0
            self.voiced_levels.clear()
            self.checkpoint_count = 0
            self.next_checkpoint_seconds = 0.6
            self.started_at = time.monotonic()
        if speaking:
            self.on_barge_in()
        self.on_speech_start()

    def force_stop(self) -> None:
        capture = self._finish_capture()
        self._emit_utterance(capture)

    def stop(self) -> None:
        self.stopping.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _read_audio(self) -> None:
        assert self.proc and self.proc.stdout
        fd = self.proc.stdout.fileno()
        while self.source_rate <= 0 and not self.stopping.wait(0.1):
            pass
        while not self.stopping.is_set():
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            if self.source_rate <= 0:
                continue
            self.float_pending.extend(chunk)
            sample_count = len(self.float_pending) // 4
            if sample_count <= 0:
                continue
            raw = bytes(self.float_pending[: sample_count * 4])
            del self.float_pending[: sample_count * 4]

            floats = array.array("f")
            floats.frombytes(raw)
            pcm = array.array(
                "h",
                (
                    max(-32768, min(32767, int(value * 32767.0)))
                    for value in floats
                ),
            )
            converted, self.rate_state = audioop.ratecv(
                pcm.tobytes(),
                SAMPLE_WIDTH,
                1,
                self.source_rate,
                SAMPLE_RATE,
                self.rate_state,
            )
            self.pcm_pending.extend(converted)
            while len(self.pcm_pending) >= FRAME_BYTES:
                frame = bytes(self.pcm_pending[:FRAME_BYTES])
                del self.pcm_pending[:FRAME_BYTES]
                self._process_frame(frame)

        if not self.stopping.is_set():
            detail = self.error_lines[-1] if self.error_lines else "音频采集器已退出"
            self.on_error(detail)

    def _read_errors(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self.error_lines.append(line)
                if line.startswith("FORMAT "):
                    parts = line.split()
                    try:
                        self.source_rate = int(float(parts[1]))
                        self.source_channels = int(parts[2])
                    except (IndexError, ValueError):
                        pass

    def _watchdog(self) -> None:
        started = time.monotonic()
        while not self.stopping.wait(2.0):
            if self.frame_count > 0:
                return
            if time.monotonic() - started >= 8.0:
                detail = "音频采集器 8 秒内没有音频帧"
                if self.error_lines:
                    detail += f"（{self.error_lines[-1]}）"
                self.on_error(detail)
                self.stop()
                return

    def _process_frame(self, frame: bytes) -> None:
        rms = float(audioop.rms(frame, SAMPLE_WIDTH))
        self.frame_count += 1
        self.peak_rms = max(self.peak_rms, rms)
        speaking = self.is_speaking()
        wake_armed = self.is_wake_armed()
        allowed = self.can_capture()
        if speaking:
            user_threshold = max(500.0, self.speech_level * 0.90)
            threshold = max(user_threshold, self.noise_floor * 3.5)
        elif wake_armed:
            user_threshold = max(
                300.0,
                min(5000.0, self.speech_level * 0.40),
            )
            threshold = max(user_threshold, self.noise_floor * 3.2)
        else:
            user_threshold = max(
                220.0,
                min(5000.0, self.speech_level * 0.22),
            )
            threshold = max(user_threshold, self.noise_floor * 2.8)
        voiced = allowed and rms >= threshold

        checkpoint: bytes | None = None
        now = time.monotonic()
        if self.on_debug and now - self.last_debug >= 5.0:
            self.on_debug(
                "VAD "
                f"frames={self.frame_count} "
                f"peak={self.peak_rms:.0f} "
                f"floor={self.noise_floor:.0f} "
                f"threshold={threshold:.0f} "
                f"capturing={self.capturing}"
            )
            self.peak_rms = 0.0
            self.last_debug = now

        with self.lock:
            if not allowed:
                self._reset_locked()
                return
            if not self.capturing and now < self.next_listen_at:
                self.preroll.clear()
                if not speaking and not voiced:
                    self.noise_floor = (
                        self.noise_floor * 0.97 + rms * 0.03
                    )
                return

            if not self.capturing:
                self.preroll.append(frame)
                if not speaking and not voiced:
                    self.noise_floor = (
                        self.noise_floor * 0.97 + rms * 0.03
                    )
                if voiced:
                    self.voiced_levels.append(rms)
                    self.voiced_run += 1
                else:
                    self.voiced_run = max(0, self.voiced_run - 1)
                if speaking:
                    required_run = 6
                elif wake_armed:
                    required_run = 5
                else:
                    required_run = 4
                if self.voiced_run < required_run:
                    return

                self.capture = bytearray(b"".join(self.preroll))
                self.capturing = True
                self.silence_frames = 0
                self.speech_frames = self.voiced_run
                self.started_at = time.monotonic()
                self.checkpoint_count = 0
                self.next_checkpoint_seconds = 0.6
                barge_in = speaking
            else:
                self.capture.extend(frame)
                if voiced:
                    self.silence_frames = 0
                    self.speech_frames += 1
                    self.voiced_levels.append(rms)
                else:
                    self.silence_frames += 1
                barge_in = False

                capture_seconds = len(self.capture) / (
                    SAMPLE_RATE * SAMPLE_WIDTH
                )
                if (
                    self.checkpoint_count < 8
                    and self.speech_frames >= 4
                    and (
                        capture_seconds >= self.next_checkpoint_seconds
                        or self.silence_frames >= 20
                    )
                ):
                    self.checkpoint_count += 1
                    self.next_checkpoint_seconds += 0.8
                    checkpoint = bytes(self.capture)

            duration = time.monotonic() - self.started_at
            should_finish = (
                (
                    self.silence_frames >= END_SILENCE_FRAMES
                    and self.speech_frames >= MIN_SPEECH_FRAMES
                )
                or duration >= MAX_CAPTURE_SECONDS
            )
            if should_finish:
                capture = self._finish_capture_locked()
            else:
                capture = None

        if barge_in:
            self.on_barge_in()
        if self.capturing and self.speech_frames == self.voiced_run:
            self.on_speech_start()
        if capture is not None:
            self._emit_utterance(capture)
        elif checkpoint and self.on_checkpoint:
            self.on_checkpoint(checkpoint)

    def _finish_capture(self) -> bytes:
        with self.lock:
            return self._finish_capture_locked()

    def _finish_capture_locked(self) -> bytes:
        capture = bytes(self.capture)
        self._update_voice_level()
        self._reset_locked()
        return capture

    def _reset_locked(self) -> None:
        self.capture = bytearray()
        self.capturing = False
        self.voiced_run = 0
        self.silence_frames = 0
        self.speech_frames = 0
        self.voiced_levels.clear()
        self.checkpoint_count = 0
        self.next_checkpoint_seconds = 0.6
        self.started_at = 0.0

    def _load_profile(self) -> None:
        try:
            data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
            level = float(data.get("speech_level", self.speech_level))
            if 200.0 <= level <= 20000.0:
                self.speech_level = level
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    def _update_voice_level(self) -> None:
        levels = sorted(
            value for value in self.voiced_levels if value >= 200.0
        )
        if len(levels) < 6:
            return
        median = levels[len(levels) // 2]
        self.speech_level = max(
            500.0,
            min(12000.0, self.speech_level * 0.72 + median * 0.28),
        )
        try:
            PROFILE_FILE.write_text(
                json.dumps(
                    {
                        "speech_level": round(self.speech_level, 1),
                        "updated_at": int(time.time()),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        if self.on_debug:
            self.on_debug(
                f"声量校准 speech_level={self.speech_level:.0f}"
            )

    def _emit_utterance(self, capture: bytes) -> None:
        self.next_listen_at = time.monotonic() + 0.8
        if len(capture) < SAMPLE_RATE * SAMPLE_WIDTH // 5:
            self.on_utterance(None)
            return
        temporary = self.wav_path.with_suffix(".tmp.wav")
        with wave.open(str(temporary), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(capture)
        os.replace(temporary, self.wav_path)
        self.on_utterance(self.wav_path)
