#!/usr/bin/env python3
"""Create a circular Lisa avatar from a source portrait."""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--size", type=int, default=256)
    parser.add_argument(
        "--crop",
        default="",
        help="Optional crop box: left,top,right,bottom",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=0.0,
        help="Fraction of the diameter kept clear around the portrait",
    )
    args = parser.parse_args()

    image = Image.open(args.source).convert("RGB")
    if args.crop:
        box = tuple(int(value) for value in args.crop.split(","))
        if len(box) != 4:
            raise ValueError("crop must contain four comma-separated numbers")
        image = image.crop(box)
    if args.padding > 0:
        inner = max(1, round(args.size * (1 - args.padding * 2)))
        portrait = ImageOps.contain(
            image,
            (inner, inner),
            method=Image.Resampling.LANCZOS,
        )
        backdrop = ImageOps.fit(
            image,
            (args.size, args.size),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        backdrop = backdrop.filter(ImageFilter.GaussianBlur(args.size / 24))
        backdrop = ImageEnhance.Brightness(backdrop).enhance(0.5)
        rounded = Image.new("L", portrait.size, 0)
        ImageDraw.Draw(rounded).rounded_rectangle(
            (0, 0, portrait.width - 1, portrait.height - 1),
            radius=max(6, inner // 14),
            fill=255,
        )
        backdrop.paste(
            portrait,
            (
                (args.size - portrait.width) // 2,
                (args.size - portrait.height) // 2,
            ),
            rounded,
        )
        image = backdrop
    else:
        image = ImageOps.fit(
            image,
            (args.size, args.size),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    mask = Image.new("L", (args.size, args.size), 0)
    ImageDraw.Draw(mask).ellipse(
        (0, 0, args.size - 1, args.size - 1), fill=255
    )
    image.putalpha(mask)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output)
    print(f"{args.output} {args.size}x{args.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
