# -*- coding: utf-8 -*-
"""生成语音 Lisa 的桌面图标（Lisa 头像 + 麦克风徽章）。"""
from pathlib import Path

from make_designed_app_icons import _voice_icon, _write_icns

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "VoiceLisa_1024.png"
ICNS = ROOT / "build" / "VoiceLisa.icns"


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    _voice_icon().save(OUT)
    _write_icns(OUT, ICNS)
    print(OUT)


if __name__ == "__main__":
    main()
