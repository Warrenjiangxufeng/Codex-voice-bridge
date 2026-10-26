Codex Voice Bridge 技术设计

版本：1.0
整理日期：2026-10-22
项目目录：Codex-voice-bridge
运行状态：语音桥 GUI 与 Codex 透明语音终端当前均在运行

一、方案目标

语音桥的目标是把“说话、识别、Codex 推理、结果朗读”整合为一个连续交互回路，同时尽量复用现有 Codex 会话和本地模型，避免打开多个界面。

核心原则：

1. 语音识别优先本地处理，减少隐私外传。
2. 默认保持同一个 Codex 会话，避免每次说话都丢失上下文。
3. 可以语音输入，也可以直接输入文字。
4. 朗读失败不能影响 Codex 对话本身。
5. TTS 按在线高质量、本地可用、系统兜底的顺序降级。
6. 随系统登录自动驻留，用户不必每次手动启动。
7. 长文本可以随时打断，避免无法停止的语音播放。

二、整体架构

用户麦克风
→ 双击 Enter 或 GUI 录音按钮
→ ffmpeg 录制 last_input.wav
→ faster-whisper 本地语音识别
→ 热词和常见错字修正
→ 当前 Codex 会话
→ Codex app-server 输出 item/agentMessage/delta 增量文字
→ 按标点组成短句队列
→ TTS 路由按句合成和播放
→ Edge TTS / Piper / macOS 语音
→ 扬声器播放

辅助控制链：

全局快捷键 → GUI 状态机 → 录音、停止、朗读打断
Codex PTY → 将语音文字注入当前 Codex 会话 → 保持单界面
LaunchAgent → 登录后自动打开语音桥 App

三、入口与进程结构

1. voice_lisa_gui.py

语音桥主 GUI，负责状态显示、录音按钮、送入 Codex、朗读开关、操作日志和 TTS 播放状态。

2. voice_lisa.py

语音核心模块，负责 Whisper 模型加载、录音结束、语音转写、Codex 调用和 TTS 合成。

3. codex_voice_pty.py

透明 PTY 层。它包装真正的 Codex 终端，让 Codex 仍然只有一个界面，同时接收语音桥送到当前会话的文字。

4. voice_hotkey.m 与 build/voice_hotkey

macOS 原生全局快捷键辅助程序。当前主要交互是双击 Enter，备用快捷键为 Ctrl + Option + Space。

5. Codex Voice Bridge.app

桌面入口。内部启动语音桥 GUI，不会另开一个普通终端窗口。

6. LaunchAgent

com.codexvoicebridge.launchagent
作用：用户登录后自动在后台启动语音桥。

四、录音链路

录音使用 ffmpeg 的 avfoundation 后端，默认音频设备编号为 0，即内建麦克风。

流程：

1. 开始录音时启动 ffmpeg 子进程。
2. 录音数据写入项目根目录 last_input.wav。
3. 再次触发快捷键或点击“说完了”时停止录音。
4. 文件大于最小阈值后进入识别。
5. 录音失败时给出麦克风权限或设备提示。

设计取舍：

保留原生采样格式，让 faster-whisper 内部完成重采样，避免强制转换成系统不支持的 16k 单声道。

五、语音识别模型

当前实际模型：Systran/faster-whisper-small

本地目录：
model/faster-whisper-small

模型大小：约 484 MB

运行参数：

1. 设备：CPU
2. 计算精度：INT8
3. 强制本地文件：local_files_only=True
4. 语言：中文
5. VAD：开启
6. beam_size：5
7. condition_on_previous_text：关闭，降低长句串话和重复幻觉

识别增强：

1. 注入中文领域热词：Lisa、豆粕、美豆、期货、基差、库存、压榨、巴西豆等。
2. 对“莉莎、丽莎、美斗、都破”等常见误识别做固定修正。
3. 识别结果为空时提示重新说一次，不进入 Codex。

回退策略：

如果 faster-whisper-small 缺失，自动回退到 faster-whisper-base。
也可以通过环境变量 LISA_WHISPER_MODEL 临时指定其他本地模型。

六、Codex 会话交互

语音桥支持两类模式。

模式一：送入当前 Codex 界面

GUI 默认勾选“送入 Codex”。识别文字通过 PTY 写入当前 Codex 语音终端，思考和回复直接显示在同一个 Codex 界面。

模式二：语音桥独立调用

语音桥调用 codex exec，使用 JSON 事件流读取最终 agent_message。首次运行会创建线程 ID，并保存到 .voice_thread_id；后续通过 codex exec resume 续接同一个线程。

安全边界：

语音自动调用默认使用 read-only 沙箱。语音识别存在误识别风险，因此不自动执行写文件、修改系统或高风险操作；需要修改文件时使用文字输入确认。

超时：

单次 Codex 调用最长约 10 分钟。

七、语音朗读是否使用模型

使用。当前 TTS 不是简单的波形拼接，也会使用神经语音模型，只是模型运行位置不同。

当前第一优先级：
Edge TTS zh-CN-XiaoxiaoNeural

配置名称：edge-xiaoxiao

这是微软云端神经 TTS 模型。语音桥只把待朗读文本发送给 Edge TTS，再由 edge-tts 命令行合成 MP3。优点是音色自然、中文表现较好；缺点是需要网络，并且待朗读文本会离开本机。

第二优先级：
Piper zh_CN-xiao_ya-medium

本地模型文件：
model/piper-xiao_ya/zh_CN-xiao_ya-medium.onnx

这是完全本地运行的神经网络 TTS 模型。中英混排时，可切换到本地 Piper 英文模型 en_US-amy-medium，再合并为一条语音。

第三优先级：
macOS 原生 lisa_tts

使用系统 Siri 李牧 Premium 声线，对应语音标识：
com.apple.speech.synthesis.voice.custom.siri.li-mu.premium

第四优先级：
macOS say 命令

默认系统中文声线 Ting-Ting，语速 175。

因此，完整回退顺序是：

Edge 晓晓在线神经模型
→ Piper 小雅本地模型
→ Siri 李牧 Premium
→ macOS Ting-Ting

当前的 .voice_tts_engine 内容为：
edge-xiaoxiao

八、流式朗读机制

默认语音模式通过 Codex app-server 的 item/agentMessage/delta 事件获取实时增量文字，
不再等待整段回复完成。

处理流程：

1. 收到正文 token 或小片段后写入流式缓冲区。
2. 遇到句号、问号、叹号、分号或换行时切成一个短句。
3. 较长文本也会在逗号、顿号、冒号或达到最大长度时提前切段。
4. 切句立即进入 TTS 队列，由后台线程按顺序合成并播放。
5. 后续文本继续生成，当前句可以先朗读，形成边生成边播放。
6. 回复完成后刷新剩余尾部文本并正常收尾。
7. Edge 路径使用 edge_tts 的音频分片流，把 MP3 分片实时写入 ffplay，
   不再等待整句 MP3 完整落盘后才开始播放。
8. Edge 流式播放失败时回退到完整 MP3、Piper 和系统语音。

并发控制：

1. TTS 队列保持严格顺序，不会因为并发而打乱语序。
2. 双击 Enter 会终止当前 TTS 进程，并阻止后续排队短句继续播放。
3. .tts_streaming 标记整个生成和播放周期，GUI 能看到“正在朗读”状态。
4. 新请求开始时会清理上次取消标记。

兼容模式：

勾选 GUI 的“送入 Codex”后，语音文字会发送到当前透明 Codex 终端。
该模式保持单界面交互，但当前 Codex 会话文件没有 token delta，
因此在最终回复完成后才整段朗读。默认不勾选，优先使用流式语音。

路径边界：

1. 流式朗读适用于从“语音 Lisa”窗口发起的语音或文字输入。
2. 直接在 Codex TUI 中打字时，当前会话 JSONL 只保存完整消息，未保存 token delta。
3. 默认流式模式下，PTY 会跳过最终的整段回复朗读，避免新流式路径和旧监听同时工作。
4. PTY 跳过逻辑在下次重新运行 codex 时生效。

九、播放控制和打断

朗读使用独立子进程，不阻塞 Codex 推理界面。

状态文件：

.tts_state：speaking 或 idle
.tts_active.pid：当前朗读进程 ID
.tts_cancel：取消标志

双击 Enter 的行为：

1. 正在朗读时，立即打断朗读。
2. 正在录音时，结束录音并进入识别。
3. 其他状态下，开始录音。

朗读线程和 GUI 都有播放状态检测，避免重复朗读或进程失控。

十、GUI 交互设计

GUI 主要能力：

1. 开始说话、说完了。
2. 实时显示录音、识别、Codex、朗读状态。
3. 文字输入，作为无麦克风或识别失败时的替代。
4. “送入 Codex”开关。
5. “朗读回复”开关。
6. 打开操作日志。
7. 隐藏后台运行。
8. 再次启动 App 时恢复前台窗口。

运行日志：

~/Desktop/Lisa输出/语音对话/gui.log
~/Desktop/Lisa输出/语音对话/operations.log
~/Desktop/Lisa输出/语音对话/run.log

对话记录：

~/Desktop/Lisa输出/语音对话/YYYY-MM-DD.md

十一、配置与状态文件

.voice_thread_id
保存语音桥复用的 Codex 线程 ID。

.voice_tts_enabled
1 表示朗读开启，0 表示关闭。

.voice_tts_engine
当前 TTS 引擎名称，例如 edge-xiaoxiao、piper-xiao_ya、piper-huayan、piper-chaowen、macos。

.codex_voice_pty_active
标记透明 Codex PTY 是否在线。

.codex_voice_pty_heartbeat
PTY 心跳文件，用于判断终端是否仍然可用。

.codex_voice_inbox
GUI 向当前 PTY 提交识别文字的原子收件目录。

.tts_state
当前朗读状态。

.tts_active.pid
当前朗读进程。

.tts_cancel
中断朗读信号文件。

.tts_streaming
表示一轮 Codex 流式输出和语音队列仍在进行。

十二、性能设计与实测对比

1. Whisper 使用 INT8 CPU 推理，避免依赖显卡。
2. 小模型优先，缺失时才回退 base。
3. VAD 过滤静音段，减少无效转写。
4. 关闭 previous_text 条件，降低重复生成。
5. TTS 单独进程播放，Codex 输出可先显示，不等待长时间朗读。
6. 上下文由 Codex 会话负责，语音桥不重复保存大段对话上下文。

主要耗时：

录音时长
+ Whisper 转写时间
+ Codex 思考时间
+ TTS 合成与播放时间

测试环境与口径（2026-10-22）：

设备：Intel MacBook Pro，macOS 12.3.1。
识别模型：faster-whisper-small，CPU INT8。
当前 TTS：Edge zh-CN-XiaoxiaoNeural。
Codex 文本测试使用 app-server 临时线程，不写入正式语音线程。
Edge 音频分片测试只记录网络首片和合成耗时；真实扬声器测试使用无声音频驱动，
避免自动播放干扰。

修改前的组件实测：

1. Codex 完整回复结束：约 6.124 秒。
2. Edge 完整 MP3 合成：两次约 2.372 秒、1.632 秒。
3. Piper 小雅本地合成：两次约 5.650 秒、4.002 秒。
4. 旧流程必须等完整回复结束，再等待完整 MP3 合成，之后才开始播放。

修改前的首声时间估算：

6.124 秒（完整回复）
+ 1.632～2.372 秒（完整 MP3 合成）
= 约 7.76～8.50 秒。

修改后的组件实测：

1. Codex 第一段正文 delta：约 2.546 秒。
2. 第一完整短句结束：约 2.631 秒。
3. Codex 后续正文批次：约 5.931 秒继续到达，完整回合约 6.124 秒结束。
4. Edge 首个音频分片到达：两次约 1.271 秒、1.652 秒。
5. Edge 完整音频流结束：两次约 1.754 秒、2.339 秒。
6. Edge 分片直接送入 ffplay 后，无声音频驱动的流式播放启动测试约 1.0～1.4 秒。

修改后的首声时间估算：

2.631 秒（第一句正文完成）
+ 1.271～1.652 秒（Edge 首片音频到达）
= 约 3.90～4.28 秒。

修改前后对比：

旧流程首声估算：约 7.76～8.50 秒。
新流程首声估算：约 3.90～4.28 秒。
预计首声提前：约 3.5～4.6 秒。

测试结论：

1. 云端 Edge TTS 确实是重要延迟来源，完整合成需要约 1.6～2.4 秒。
2. Codex 首句生成同样重要，第一段正文在约 2.5 秒才到达。
3. 流式优化同时利用了文本 delta、句级切分、Edge 音频分片和 ffplay 直接播放，
   不是只缩短 TTS 合成本身。
4. 较短回答中，模型可能在很短时间内一次输出多个 delta，体感提升会小于长回答。
5. 真实网络、TTS 服务和音频设备会有波动，上述为首声准备时间估算，不是固定 SLA。

十三、隐私与安全

1. 语音识别默认完全本地，录音不会上传给 Whisper 服务。
2. 当前 Edge TTS 需要把待朗读文字发送给在线语音服务。
3. 如果要求完全离线，应将 .voice_tts_engine 改为 piper-xiao_ya。
4. Codex 是否联网取决于当前 Codex 模型和任务本身。
5. 语音输入默认走 read-only 沙箱，避免误识别触发文件修改。
6. 密码、密钥、令牌不应通过语音朗读或语音输入。

十四、故障处理

没有识别结果：
检查麦克风权限、输入设备和 last_input.wav 是否包含有效声音。

模型加载失败：
检查 model/faster-whisper-small/model.bin 是否存在，或切换到 base。

Edge TTS 失败：
系统会自动回退 Piper；如需完全离线，直接配置 piper-xiao_ya。

语音桥点开没有窗口：
通过桌面“Codex Voice Bridge.app”恢复前台窗口。

送入 Codex 失败：
确认当前 codex 是通过透明语音终端启动，并检查 PTY 心跳和收件目录。

朗读进程未结束：
双击 Enter 触发停止，语音桥会清理 TTS 进程和状态文件。

十五、后续升级路线

1. 全离线高质量 TTS：增加更自然的中文本地模型，同时保留低延迟回退。
2. 流式识别：说话过程中逐步转写，减少停止说话后的等待。
3. 自动 VAD 断句：静音自动结束录音，减少双击 Enter 操作。
4. 唤醒词：增加“Lisa”唤醒，但需要常驻低功耗监听。
5. 降噪与回声消除：改善远场输入和扬声器播放时的识别率。
6. 声线管理界面：在 GUI 中切换 Edge、Piper、Siri 等声线，而不是手动修改配置文件。
7. 更严格的状态机：统一录音、识别、推理、朗读和取消状态，降低并发冲突。
8. 语音风险评估：对写文件、删文件、支付、账号等高风险语句强制二次文字确认。

十六、当前运行摘要

语音识别：faster-whisper-small，本地 CPU INT8。
语音朗读：Codex app-server 流式文本 + Edge TTS zh-CN-XiaoxiaoNeural。
本地回退：Piper zh_CN-xiao_ya-medium。
系统回退：Siri 李牧 Premium / Ting-Ting。
主要入口：Codex Voice Bridge.app。
单界面模式：codex 透明语音终端。
自动启动：com.codexvoicebridge.launchagent LaunchAgent。

结论：

语音桥已经形成“本地识别 + 复用 Codex 会话 + 多级 TTS 回退”的可用闭环。当前最明显的优化空间不是识别模型本身，而是让 TTS 更适合日常使用：优先降低在线依赖、增加可切换声线，并完善流式识别和自动断句。
