#!/bin/bash
# 启动 Codex Voice Bridge（本地识别 + Codex 会话 + 语音朗读）
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)" || exit 1
cd "$ROOT" || exit 1
LOG="$HOME/Desktop/Lisa输出/语音对话/run.log"
mkdir -p "$(dirname "$LOG")"
PYTHONUNBUFFERED=1 ./venv/bin/python -u voice_lisa.py "$@" 2>&1 | tee -a "$LOG"
