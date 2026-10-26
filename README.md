# Codex Voice Bridge

Codex Voice Bridge 是一个面向 macOS 的本地语音桥，把语音识别、Codex 会话和语音朗读串成一个连续交互回路：

```text
麦克风 -> faster-whisper -> 当前 Codex 会话 -> 流式文本 -> TTS -> 扬声器
```

项目包含 Web UI、透明 Codex PTY、本地 VAD、全局快捷键，以及 Edge TTS、Piper 和 macOS 原生语音三级回退。

## 功能

- 本地 `faster-whisper` 语音识别
- 复用当前 Codex 会话，保持上下文连续
- WebView 单窗口交互与透明 PTY 语音输入
- 双击 Enter 或快捷键录音，朗读可随时打断
- Edge TTS、Piper 和 macOS AVSpeech 自动降级
- 内置 VAD、唤醒词和录音状态监测

## 环境要求

- macOS，建议 Python 3.10–3.12
- Codex CLI 已安装并完成登录
- Xcode Command Line Tools
- `ffmpeg` 与 `ffplay`

## 安装

```bash
git clone git@github.com:Warrenjiangxufeng/Codex-voice-bridge.git
cd Codex-voice-bridge
python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt
```

将 Whisper 模型放入以下任一目录：

```text
model/faster-whisper-small
model/faster-whisper-base
```

默认优先使用 `small`，缺失时回退到 `base`。也可通过 `LISA_WHISPER_MODEL` 指定模型路径。

编译 macOS 原生组件：

```bash
mkdir -p build
clang -fobjc-arc -framework AVFoundation -framework Foundation \
  tools/lisa_tts.m -o build/lisa_tts
clang -fobjc-arc -framework Cocoa -framework WebKit \
  lisa_webview.m -o build/lisa_webview
clang -fobjc-arc -framework Cocoa -framework Carbon -framework ApplicationServices \
  voice_hotkey.m -o build/voice_hotkey
```

## 运行

启动 Web UI：

```bash
./venv/bin/python lisa_web.py
```

启动命令行语音循环：

```bash
./run.sh
```

通过透明 PTY 启动 Codex：

```bash
./codex-voice
```

首次运行需要允许终端或 Python 使用麦克风。录音设备默认为 ffmpeg `avfoundation` 的 `0` 号设备。

## 隐私说明

- 语音识别由本地 faster-whisper 完成。
- 默认 Edge TTS 需要联网，会将待朗读文本发送给在线语音服务；可在 `.voice_tts_engine` 中切换为 `piper` 或 `macos` 以避免该在线请求。
- 仓库不包含模型、录音、对话记录、运行日志、虚拟环境或任何凭据。
- 本地 HTTP 服务只监听 `127.0.0.1`。

## 目录

```text
codex_voice_pty.py      透明 Codex PTY
voice_lisa.py           录音、识别、Codex 调用与 TTS 核心
lisa_web.py             本地 Web 服务
voice_activity.py       VAD
voice_stream.py         Codex app-server 流式接口
web/                    Web UI
tools/                  TTS、Piper 和图标工具
tests/                  单元测试
docs/architecture.md    架构与状态文件说明
```

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  ./venv/bin/python -m unittest tests.test_companion_trigger tests.test_codex_voice_pty_trigger
```
