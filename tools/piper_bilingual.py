#!/usr/bin/env python3
"""Synthesize Chinese/English mixed text with two Piper voices in one process."""
from __future__ import annotations

import argparse
import re
import sys
import wave
from pathlib import Path

from piper import PiperVoice, SynthesisConfig

ENGLISH_SPAN = re.compile(
    r"[A-Za-z][A-Za-z0-9&+._/-]*(?:\s+[A-Za-z][A-Za-z0-9&+._/-]*)*"
)


def segments(text: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    position = 0
    for match in ENGLISH_SPAN.finditer(text):
        if match.start() > position:
            result.append(("zh", text[position : match.start()]))
        result.append(("en", match.group()))
        position = match.end()
    if position < len(text):
        result.append(("zh", text[position:]))
    return [(kind, chunk.strip()) for kind, chunk in result if chunk.strip()]


def synthesis_config(text: str) -> SynthesisConfig:
    length = 1.0
    last = text.rstrip()[-1:] if text.strip() else ""
    if last in "？?":
        length = 1.08
    elif last in "！!":
        length = 0.95
    elif last == "…":
        length = 1.15
    return SynthesisConfig(length_scale=length, normalize_audio=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zh-model", type=Path, required=True)
    parser.add_argument("--zh-config", type=Path, required=True)
    parser.add_argument("--en-model", type=Path, required=True)
    parser.add_argument("--en-config", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--text", default="")
    args = parser.parse_args()

    text = args.text.strip() or sys.stdin.read().strip()
    parts = segments(text)
    if not parts:
        return 2

    zh_voice = PiperVoice.load(
        args.zh_model,
        args.zh_config,
        download_dir=args.data_dir,
    )
    en_voice = PiperVoice.load(args.en_model, args.en_config)

    sample_rate = 22050
    gap = b"\x00" * int(sample_rate * 2 * 0.08)
    with wave.open(str(args.out), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        for kind, chunk in parts:
            voice = en_voice if kind == "en" else zh_voice
            for audio in voice.synthesize(chunk, synthesis_config(chunk)):
                writer.writeframes(audio.audio_int16_bytes)
            writer.writeframes(gap)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
