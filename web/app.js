const $ = (selector) => document.querySelector(selector);

const state = {
  status: "待唤醒",
  runtime: "idle",
  processing: false,
  recording: false,
  ttsEnabled: true,
  messages: [],
  progress: [],
};

const messagesEl = $("#messages");
const statusEl = $("#status");
const statusHintEl = $("#status-hint");
const waveformEl = $("#waveform");
const voiceButton = $("#voice-button");
const avatarPicker = $("#avatar-picker");
const avatarFile = $("#avatar-file");
const ttsToggle = $("#tts-toggle");
const stopButton = $("#stop-button");
const textInput = $("#text-input");
const conversationCard = $("#conversation-card");
const conversationMain = $(".conversation-main");
const conversationControls = [
  ...document.querySelectorAll(".conversation-controls button"),
];
const liveAssistant = new Map();
let liveTranscriptEl = null;
let unlockTimer = null;
let compositionActive = false;
let compositionEndedAt = 0;

for (let i = 0; i < 18; i += 1) {
  waveformEl.appendChild(document.createElement("i"));
}

function setStatus(status, runtime = "idle") {
  const hints = {
    idle: "说“Lisa”，或双击 Enter 手动说话",
    listening: "正在收集语音，停顿 3 秒后发送",
    recognizing: "正在转写语音内容",
    processing: "正在整理语音内容",
    thinking: "正在请求 Codex",
    speaking: "直接说“Lisa”可打断",
    warning: "请检查语音输入设置",
    error: "请稍后重试",
  };
  state.status = status;
  state.runtime = runtime;
  statusEl.textContent = status;
  statusHintEl.textContent = hints[runtime] || "说“Lisa”开始";
  waveformEl.dataset.state = runtime;
  voiceButton.classList.toggle("recording", runtime === "recording");
  voiceButton.classList.toggle(
    "listening",
    runtime === "listening" || runtime === "recognizing"
  );
  voiceButton.classList.toggle("ready", runtime === "idle");
  const locked = runtime === "idle";
  const wasLocked = conversationCard.classList.contains("idle-locked");
  if (locked) {
    if (unlockTimer) clearTimeout(unlockTimer);
    conversationMain.classList.remove("unlocking");
    conversationCard.classList.add("idle-locked");
  } else if (wasLocked) {
    conversationCard.classList.remove("idle-locked");
    conversationMain.classList.add("unlocking");
    unlockTimer = setTimeout(() => {
      conversationMain.classList.remove("unlocking");
    }, 620);
  }
  conversationControls.forEach((control) => {
    control.disabled = locked;
  });
}

function setTts(enabled) {
  state.ttsEnabled = enabled;
  ttsToggle.classList.toggle("active", enabled);
  ttsToggle.classList.toggle("muted", !enabled);
}

function scrollMessages() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function addMessage(speaker, text, time = "") {
  state.progress = [];
  const row = document.createElement("article");
  row.className = `message ${speaker === "you" ? "user" : "lisa"}`;

  const head = document.createElement("div");
  head.className = "message-head";
  head.textContent = `${speaker === "you" ? "你" : "Lisa"}${time ? ` · ${time}` : ""}`;

  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = text;

  row.append(head, body);
  messagesEl.appendChild(row);
  state.messages.push({ speaker, text, time });
  scrollMessages();
}

function addProgress(text) {
  const line = document.createElement("div");
  line.className = "progress-line";
  line.textContent = `· ${text}`;
  const liveRows = [...liveAssistant.values()].map((entry) => entry.row);
  const liveRow = liveRows[liveRows.length - 1];
  const anchor = liveTranscriptEl || liveRow;
  if (anchor && anchor.parentNode === messagesEl) {
    messagesEl.insertBefore(line, anchor);
  } else {
    messagesEl.appendChild(line);
  }
  state.progress.push(text);
  scrollMessages();
}

function startTranscript() {
  if (liveTranscriptEl) liveTranscriptEl.remove();
  liveTranscriptEl = document.createElement("div");
  liveTranscriptEl.className = "progress-line live-transcript";
  liveTranscriptEl.textContent = "识别中…";
  messagesEl.appendChild(liveTranscriptEl);
  scrollMessages();
}

function updateTranscript(text) {
  if (!liveTranscriptEl) startTranscript();
  liveTranscriptEl.textContent = `识别：${text || "…"}`;
  scrollMessages();
}

function endTranscript() {
  liveTranscriptEl?.remove();
  liveTranscriptEl = null;
}

function startAssistant(id) {
  if (liveAssistant.has(id)) return liveAssistant.get(id);
  const row = document.createElement("article");
  row.className = "message lisa";
  const head = document.createElement("div");
  head.className = "message-head";
  head.textContent = "Lisa · 正在输出";
  const body = document.createElement("div");
  body.className = "message-body";
  row.append(head, body);
  messagesEl.appendChild(row);
  const entry = { row, body, head };
  liveAssistant.set(id, entry);
  scrollMessages();
  return entry;
}

function appendAssistantDelta(id, text) {
  const entry = startAssistant(id);
  entry.body.textContent += text;
  scrollMessages();
}

function finishAssistant(id, text) {
  const entry = startAssistant(id);
  entry.head.textContent = `Lisa · ${new Date().toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })}`;
  entry.body.textContent = text;
  liveAssistant.delete(id);
  messagesEl.appendChild(entry.row);
  state.messages.push({ speaker: "lisa", text, time: "" });
  scrollMessages();
}

function clearConversation() {
  state.messages = [];
  state.progress = [];
  liveAssistant.clear();
  liveTranscriptEl = null;
  messagesEl.innerHTML = "";
}

function handleEvent(event) {
  if (event.type === "snapshot") {
    setStatus(event.status, event.state);
    setTts(Boolean(event.tts_enabled));
    clearConversation();
    for (const item of event.history || []) handleEvent({ ...item, replay: true });
    return;
  }
  if (event.type === "status") {
    setStatus(event.status, event.state);
  } else if (event.type === "message") {
    if (event.speaker === "you") endTranscript();
    addMessage(event.speaker, event.text, event.time);
  } else if (event.type === "progress") {
    if (/^(VAD|声量校准)/.test(event.text || "")) return;
    addProgress(event.text);
  } else if (event.type === "tts") {
    setTts(Boolean(event.enabled));
  } else if (event.type === "tts_state") {
    if (event.state === "speaking" && !state.recording) setStatus("正在朗读", "speaking");
    if (event.state === "idle" && state.runtime === "speaking") setStatus("待唤醒", "idle");
  } else if (event.type === "assistant_start") {
    startAssistant(event.id);
  } else if (event.type === "assistant_delta") {
    appendAssistantDelta(event.id, event.text || "");
  } else if (event.type === "assistant_done") {
    finishAssistant(event.id, event.text || "");
  } else if (event.type === "stt_start") {
    startTranscript();
  } else if (event.type === "stt_partial") {
    updateTranscript(event.text);
  } else if (event.type === "stt_done") {
    endTranscript();
  } else if (event.type === "clear") {
    clearConversation();
  }
}

async function api(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

async function sendText(text) {
  const clean = text.trim();
  if (!clean) return;
  textInput.value = "";
  try {
    await api("/api/text", { text: clean });
  } catch (error) {
    addProgress(`发送失败：${error.message}`);
  }
}

$("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  sendText(textInput.value);
});

textInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey) return;
  const recentlyComposed =
    performance.now() - compositionEndedAt < 120;
  if (
    compositionActive ||
    event.isComposing ||
    event.keyCode === 229 ||
    recentlyComposed
  ) {
    return;
  }
  event.preventDefault();
  sendText(textInput.value);
});

textInput.addEventListener("compositionstart", () => {
  compositionActive = true;
});

textInput.addEventListener("compositionend", () => {
  compositionActive = false;
  compositionEndedAt = performance.now();
});

voiceButton.addEventListener("click", () => api("/api/record").catch(() => {}));
avatarPicker.addEventListener("click", () => avatarFile.click());
avatarFile.addEventListener("change", async () => {
  const file = avatarFile.files?.[0];
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    addProgress("请选择图片文件");
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    addProgress("头像图片不能超过 10 MB");
    return;
  }
  try {
    const image = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
    const result = await api("/api/avatar", { image });
    document.querySelectorAll(".avatar-image").forEach((element) => {
      element.src = `avatar.png?v=${result.version}`;
    });
    addProgress(
      result.desktop_icon
        ? "头像和桌面图标已更新"
        : "头像已更新，但桌面图标同步失败"
    );
  } catch (error) {
    addProgress(`头像更新失败：${error.message}`);
  } finally {
    avatarFile.value = "";
  }
});
stopButton.addEventListener("click", () => api("/api/stop").catch(() => {}));
ttsToggle.addEventListener("click", () => api("/api/tts").catch(() => {}));

document.querySelectorAll(".action-button").forEach((button) => {
  button.addEventListener("click", async () => {
    const action = button.dataset.action;
    if (action === "clear") {
      clearConversation();
      await api("/api/clear").catch(() => {});
      return;
    }
    const handler = window.webkit?.messageHandlers?.lisaShell;
    if (handler) handler.postMessage(action);
    if (action === "exit" && !handler) window.close();
  });
});

function connectEvents() {
  const events = new EventSource("/api/events");
  events.onmessage = (message) => {
    try {
      handleEvent(JSON.parse(message.data));
    } catch (_error) {
      // Ignore malformed events and keep the stream alive.
    }
  };
  events.onerror = () => {};
}

async function initialize() {
  window.webkit?.messageHandlers?.lisaShell?.postMessage("ready");
  const preview = new URLSearchParams(location.search).has("preview");
  if (preview) {
    setStatus("待唤醒", "idle");
    addMessage("you", "Lisa，帮我检查今天的任务。", "20:48:12");
    addProgress("正在分析");
    addMessage(
      "lisa",
      "今天的任务已经整理好。我会先检查关键项目，再把需要处理的事项逐条告诉你。",
      "20:48:18"
    );
    return;
  }
  try {
    const snapshot = await fetch("/api/state").then((response) => response.json());
    handleEvent({ type: "snapshot", ...snapshot });
    connectEvents();
  } catch (_error) {
    initializePreview();
  }
}

function initializePreview() {
  setStatus("待唤醒", "idle");
  addMessage("you", "Lisa，帮我检查一下目前的任务。", "20:48");
  addMessage("lisa", "好的，我正在检查。完成后会在这里显示结果。", "20:48");
}

initialize();
