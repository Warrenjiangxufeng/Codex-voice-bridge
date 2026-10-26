#!/usr/bin/env python3
"""Create polished white app icons for Lisa Voice and the finance report."""
from __future__ import annotations

import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"


def _font(path: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size, index=index)


def _white_tile(size: int = 1024) -> Image.Image:
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (54, 54, size - 54, size - 54), radius=220, fill=255
    )

    base = Image.new("RGB", (size, size))
    px = base.load()
    for y in range(size):
        for x in range(size):
            radial = ((x - size * 0.34) ** 2 + (y - size * 0.18) ** 2) ** 0.5
            glow = max(0.0, 1 - radial / (size * 0.95))
            shade = y / (size - 1)
            value = 250 - int(18 * shade) + int(10 * glow)
            px[x, y] = (value, min(255, value + 2), min(255, value + 4))
    tile.alpha_composite(Image.composite(base.convert("RGBA"), tile, mask))

    # Soft inner edge, like a ceramic tile catching light from the top left.
    edge = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(edge)
    d.rounded_rectangle(
        (54, 54, size - 54, size - 54),
        radius=220,
        outline=(173, 190, 201, 190),
        width=10,
    )
    d.rounded_rectangle(
        (80, 80, size - 80, size - 80),
        radius=196,
        outline=(255, 255, 255, 235),
        width=5,
    )
    d.line((120, 82, size - 120, 82), fill=(255, 255, 255, 170), width=6)
    tile.alpha_composite(edge)
    return tile


def _portrait_medallion(source: Image.Image, diameter: int = 604) -> Image.Image:
    # Keep the face and full headdress, with the face slightly right of center.
    portrait = ImageOps.fit(
        source.convert("RGB"),
        (diameter, diameter),
        method=Image.Resampling.LANCZOS,
        centering=(0.53, 0.26),
    ).convert("RGBA")
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=255)
    portrait.putalpha(mask)
    return portrait


def _mic_badge(diameter: int = 146) -> Image.Image:
    badge = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    d = ImageDraw.Draw(badge)
    d.ellipse((0, 0, diameter - 1, diameter - 1), fill=(15, 118, 110, 255))
    d.ellipse((8, 8, diameter - 9, diameter - 9), outline=(94, 234, 212, 180), width=4)
    c = diameter / 2
    d.rounded_rectangle(
        (c - 20, c - 42, c + 20, c + 8), radius=20, fill=(249, 251, 252, 255)
    )
    d.arc((c - 40, c - 18, c + 40, c + 38), start=0, end=180, fill=(249, 251, 252, 255), width=10)
    d.line((c, c + 38, c, c + 54), fill=(249, 251, 252, 255), width=10)
    d.line((c - 20, c + 56, c + 20, c + 56), fill=(249, 251, 252, 255), width=10)
    return badge


def _voice_icon(size: int = 1024) -> Image.Image:
    tile = _white_tile(size)
    source = Image.open(BUILD / "lisa_avatar_source.jpg")
    medallion = _portrait_medallion(source)

    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        (202, 148, 822, 768), fill=(30, 54, 63, 70)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    tile.alpha_composite(shadow)
    tile.alpha_composite(medallion, (210, 120))

    d = ImageDraw.Draw(tile)
    d.ellipse((204, 114, 820, 730), outline=(249, 251, 252, 255), width=14)
    d.ellipse((198, 108, 826, 736), outline=(15, 118, 110, 255), width=7)
    d.ellipse((190, 100, 834, 744), outline=(255, 255, 255, 120), width=3)
    tile.alpha_composite(_mic_badge(), (706, 614))

    font = _font("/System/Library/Fonts/Avenir Next.ttc", 78, index=1)
    label = "LISA"
    widths = [d.textlength(c, font=font) for c in label]
    tracking = 24
    total = sum(widths) + tracking * (len(label) - 1)
    x = (size - total) / 2
    for c, w in zip(label, widths):
        d.text((x, 838), c, font=font, fill=(20, 72, 72, 255))
        x += w + tracking
    return tile


def _finance_glyph() -> Image.Image:
    old = Image.open(BUILD / "FinReport_dark_backup.png").convert("RGB")
    crop = old.crop((250, 220, 790, 720))
    mask = Image.new("L", crop.size, 0)
    src = crop.load()
    dst = mask.load()
    for y in range(crop.height):
        for x in range(crop.width):
            r, g, b = src[x, y]
            if r > 115 and g > 85 and r > b * 1.35 and g > b * 1.15:
                dst[x, y] = min(255, int((r - 90) * 2.2))
    mask = mask.filter(ImageFilter.GaussianBlur(1.25))
    bbox = mask.getbbox()
    if bbox:
        mask = mask.crop(bbox)
    glyph = Image.new("RGBA", mask.size, (31, 58, 63, 255))
    glyph.putalpha(mask)
    return glyph


def _finance_icon(size: int = 1024) -> Image.Image:
    tile = _white_tile(size)
    glyph = _finance_glyph()
    target_h = 430
    scale = target_h / glyph.height
    glyph = glyph.resize(
        (round(glyph.width * scale), target_h), Image.Resampling.LANCZOS
    )
    x = (size - glyph.width) // 2
    y = 250
    tile.alpha_composite(glyph, (x, y))

    d = ImageDraw.Draw(tile)
    d.rounded_rectangle((334, 728, 690, 735), radius=4, fill=(15, 118, 110, 255))
    font = _font("/System/Library/Fonts/Avenir Next.ttc", 54, index=1)
    label = "SOY REPORT"
    widths = [d.textlength(c, font=font) for c in label]
    tracking = 12
    total = sum(widths) + tracking * (len(label) - 1)
    tx = (size - total) / 2
    for c, w in zip(label, widths):
        d.text((tx, 800), c, font=font, fill=(84, 106, 121, 255))
        tx += w + tracking
    return tile


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
    chunks = []
    for icon_type, edge in variants.items():
        temp = icns.with_name(f".{icns.stem}-{edge}.png")
        source.resize((edge, edge), Image.Resampling.LANCZOS).save(temp)
        payload = temp.read_bytes()
        temp.unlink()
        chunks.append(icon_type + struct.pack(">I", len(payload) + 8) + payload)
    body = b"".join(chunks)
    icns.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main() -> None:
    voice_png = BUILD / "VoiceLisa_design_1024.png"
    finance_png = BUILD / "FinReport_design_1024.png"
    _voice_icon().save(voice_png)
    _finance_icon().save(finance_png)
    _write_icns(voice_png, BUILD / "VoiceLisa_design.icns")
    _write_icns(finance_png, BUILD / "FinReport_design.icns")
    print(voice_png)
    print(finance_png)


if __name__ == "__main__":
    main()
