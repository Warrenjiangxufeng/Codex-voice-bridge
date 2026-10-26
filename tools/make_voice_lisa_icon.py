#!/usr/bin/env python3
"""Generate the teal Lisa Voice app icon and its macOS iconset."""
from __future__ import annotations

import argparse
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def _gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            t = min(1.0, max(0.0, (x + y) / (size * 2 - 2)))
            pixels[x, y] = tuple(
                round(top[channel] * (1 - t) + bottom[channel] * t)
                for channel in range(3)
            )
    return image


def _tracked_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int, int],
    tracking: int,
) -> None:
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        box = draw.textbbox((x, y), char, font=font)
        x += box[2] - box[0] + tracking


def build_icon(output: Path, size: int = 1024) -> Path:
    scale = size / 1024
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    background = _gradient(size, (10, 72, 68), (20, 184, 166)).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (round(56 * scale), round(56 * scale), round(968 * scale), round(968 * scale)),
        radius=round(218 * scale),
        fill=255,
    )
    canvas.alpha_composite(Image.composite(background, Image.new("RGBA", (size, size)), mask))

    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        (round(260 * scale), round(150 * scale), round(764 * scale), round(700 * scale)),
        fill=(153, 246, 228, 34),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(round(80 * scale)))
    canvas.alpha_composite(Image.composite(glow, Image.new("RGBA", (size, size)), mask))

    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (round(56 * scale), round(56 * scale), round(968 * scale), round(968 * scale)),
        radius=round(218 * scale),
        outline=(204, 251, 241, 210),
        width=max(2, round(10 * scale)),
    )

    white = (248, 250, 252, 255)
    inner = (10, 91, 85, 255)
    line_width = max(2, round(34 * scale))

    draw.rounded_rectangle(
        (round(430 * scale), round(226 * scale), round(594 * scale), round(590 * scale)),
        radius=round(82 * scale),
        fill=white,
    )
    draw.rounded_rectangle(
        (round(458 * scale), round(266 * scale), round(566 * scale), round(520 * scale)),
        radius=round(54 * scale),
        fill=inner,
    )
    draw.arc(
        (round(350 * scale), round(430 * scale), round(674 * scale), round(730 * scale)),
        start=0,
        end=180,
        fill=white,
        width=line_width,
    )
    draw.line(
        (round(512 * scale), round(730 * scale), round(512 * scale), round(820 * scale)),
        fill=white,
        width=line_width,
    )
    draw.line(
        (round(412 * scale), round(830 * scale), round(612 * scale), round(830 * scale)),
        fill=white,
        width=line_width,
    )

    font_path = Path("/System/Library/Fonts/Avenir Next.ttc")
    font = ImageFont.truetype(str(font_path), round(49 * scale), index=0)
    label = "VOICE LISA"
    tracking = round(10 * scale)
    widths = []
    for char in label:
        box = draw.textbbox((0, 0), char, font=font)
        widths.append(box[2] - box[0])
    total = sum(widths) + tracking * (len(label) - 1)
    _tracked_text(
        draw,
        (round((size - total) / 2), round(874 * scale)),
        label,
        font,
        (236, 254, 255, 228),
        tracking,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output)
    return output


def build_icns(png: Path, icns: Path) -> None:
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
    parser.add_argument("--output", type=Path, default=Path("build/VoiceLisa_teal_1024.png"))
    parser.add_argument("--icns", type=Path, default=Path("build/VoiceLisa_teal.icns"))
    args = parser.parse_args()
    png = build_icon(args.output)
    build_icns(png, args.icns)
    print(f"{png} {args.icns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
