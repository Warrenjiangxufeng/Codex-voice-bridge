#!/usr/bin/env python3
"""Compare local faster-whisper models on the same audio file."""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from faster_whisper import WhisperModel  # noqa: E402

from voice_lisa import FIXES, HOTWORDS  # noqa: E402


def normalize(text: str) -> str:
    """Keep only characters that matter for Chinese transcription accuracy."""
    return re.sub(r"[\s，。！？、,.!?：:；;\"'“”‘’（）()《》<>-]", "", text)


def edit_distance(reference: str, hypothesis: str) -> int:
    previous = list(range(len(hypothesis) + 1))
    for i, ref_char in enumerate(reference, 1):
        current = [i]
        for j, hyp_char in enumerate(hypothesis, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (ref_char != hyp_char),
                )
            )
        previous = current
    return previous[-1]


def transcribe(
    model_path: Path, audio: Path, beam_size: int, vad_filter: bool
) -> tuple[str, float]:
    started = time.perf_counter()
    model = WhisperModel(
        str(model_path), device="cpu", compute_type="int8", local_files_only=True
    )
    segments, _ = model.transcribe(
        str(audio),
        language="zh",
        vad_filter=vad_filter,
        beam_size=beam_size,
        condition_on_previous_text=False,
        initial_prompt=HOTWORDS,
    )
    text = "".join(segment.text for segment in segments).strip()
    for wrong, right in FIXES.items():
        text = text.replace(wrong, right)
    return text, time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--reference", default="")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--no-vad", action="store_true")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Model to test; defaults to base and small.",
    )
    args = parser.parse_args()

    candidates = args.model or [
        f"base={ROOT / 'model' / 'faster-whisper-base'}",
        f"small={ROOT / 'model' / 'faster-whisper-small'}",
    ]
    reference = normalize(args.reference)
    for candidate in candidates:
        name, raw_path = candidate.split("=", 1)
        model_path = Path(raw_path).expanduser()
        if not (model_path / "model.bin").is_file():
            print(f"{name}: SKIP (missing {model_path / 'model.bin'})")
            continue
        text, elapsed = transcribe(
            model_path, args.audio, args.beam_size, not args.no_vad
        )
        print(f"{name}: {elapsed:.2f}s")
        print(f"  {text}")
        if reference:
            normalized = normalize(text)
            distance = edit_distance(reference, normalized)
            rate = distance / max(1, len(reference))
            print(f"  CER: {rate:.2%} ({distance}/{len(reference)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
