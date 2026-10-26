#!/usr/bin/env python3
"""Generate the white Lisa Voice and finance-report macOS icons."""
from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
WHITE = (249, 251, 252, 255)
INK = (30, 54, 63, 255)
TEAL = (15, 118, 110, 255)
SLATE = (100, 116, 139, 255)


def _background(size: int) -> Image.Image:
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            t = min(1.0, max(0.0, (x * 0.35 + y * 0.65) / (size - 1)))
            top = (255, 255, 255)
            bottom = (226, 235, 240)
            pixels[x, y] = tuple(
                round(top[c] * (1 - t) + bottom[c] * t) for c in range(3)
            )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (56, 56, size - 56, size - 56), radius=218, fill=255
    )
    canvas.alpha_composite(Image.composite(image.convert("RGBA"), canvas, mask))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (56, 56, size - 56, size - 56),
        radius=218,
        outline=(196, 209, 218, 230),
        width=9,
    )
    draw.rounded_rectangle(
        (79, 79, size - 79, size - 79),
        radius=198,
        outline=(255, 255, 255, 220),
        width=4,
    )
    return canvas


def _tracked_text(
    draw: ImageDraw.ImageDraw,
    center_x: int,
    y: int,
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    tracking: int,
) -> None:
    pieces = []
    for char in text:
        box = draw.textbbox((0, 0), char, font=font)
        pieces.append((char, box[2] - box[0]))
    total = sum(width for _, width in pieces) + tracking * (len(pieces) - 1)
    x = center_x - total / 2
    for char, width in pieces:
        draw.text((x, y), char, font=font, fill=fill)
        x += width + tracking


def _voice_icon(size: int = 1024) -> Image.Image:
    canvas = _background(size)
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        (250, 140, 774, 710), fill=(94, 234, 212, 36)
    )
    glow = glow.filter(ImageFilter.GaussianBlur(82))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (56, 56, size - 56, size - 56), radius=218, fill=255
    )
    canvas.alpha_composite(Image.composite(glow, Image.new("RGBA", (size, size)), mask))
    draw = ImageDraw.Draw(canvas)

    width = 34
    draw.rounded_rectangle((430, 226, 594, 590), radius=82, fill=TEAL)
    draw.rounded_rectangle((458, 266, 566, 520), radius=54, fill=WHITE)
    draw.arc((350, 430, 674, 730), start=0, end=180, fill=TEAL, width=width)
    draw.line((512, 730, 512, 820), fill=TEAL, width=width)
    draw.line((412, 830, 612, 830), fill=TEAL, width=width)

    font = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", 49, index=0)
    _tracked_text(draw, 512, 874, "VOICE LISA", font, SLATE, 10)
    return canvas


def _finance_icon(size: int = 1024) -> Image.Image:
    canvas = _background(size)
    draw = ImageDraw.Draw(canvas)
    font_path = "/System/Library/Fonts/PingFang.ttc"
    glyph_font = ImageFont.truetype(font_path, 430, index=1)
    box = draw.textbbox((0, 0), "研", font=glyph_font)
    x = (size - (box[2] - box[0])) / 2 - box[0]
    y = 260 - box[1]
    draw.text((x, y), "研", font=glyph_font, fill=INK)
    draw.rounded_rectangle((332, 715, 692, 721), radius=3, fill=TEAL)

    font = ImageFont.truetype("/System/Library/Fonts/Avenir Next.ttc", 50, index=0)
    _tracked_text(draw, 512, 790, "SOY REPORT", font, SLATE, 9)
    return canvas


def _write_icns(png: Path, icns: Path) -> None:
    source = Image.open(png).convert("RGBA")
    variants = {
        b"icp4": 16,
        b"icp5": 32,
        b"icp6": 64,
        b"ic07": 128,
        b"ic08": 256,
        b"ic09": 512,
        b"ic10": 1024,
    }
    entries = []
    for icon_type, edge in variants.items():
        temp = icns.with_name(f".{icns.stem}-{edge}.png")
        source.resize((edge, edge), Image.Resampling.LANCZOS).save(temp)
        payload = temp.read_bytes()
        temp.unlink()
        entries.append(icon_type + struct.pack(">I", len(payload) + 8) + payload)
    body = b"".join(entries)
    icns.parent.mkdir(parents=True, exist_ok=True)
    icns.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=BUILD)
    args = parser.parse_args()
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)

    voice_png = output / "VoiceLisa_white_1024.png"
    voice_icns = output / "VoiceLisa_white.icns"
    _voice_icon().save(voice_png)
    _write_icns(voice_png, voice_icns)

    finance_png = output / "FinReport_white_1024.png"
    finance_icns = output / "FinReport_white.icns"
    _finance_icon().save(finance_png)
    _write_icns(finance_png, finance_icns)
    print(f"{voice_png} {voice_icns}")
    print(f"{finance_png} {finance_icns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
