(function () {
  const originalLog = console.log.bind(console);
  console.log = (...args) => {
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    const ss = String(now.getSeconds()).padStart(2, "0");
    const ms = String(now.getMilliseconds()).padStart(3, "0");
    originalLog(`[${hh}:${mm}:${ss}.${ms}]`, ...args);
  };
})();

const statusDiv = document.getElementById("status");
const messagesDiv = document.getElementById("messages");
const speedSlider = document.getElementById("speedSlider");
const characterSelect = document.getElementById("characterSelect");
const characterBadge = document.getElementById("characterBadge");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const clearBtn = document.getElementById("clearBtn");
const copyBtn = document.getElementById("copyBtn");

speedSlider.disabled = true;
startBtn.disabled = true;

const sockets = new Map(); // id -> entry
const chatHistories = new Map(); // id -> Array<{role, content, type}>
const typingStates = new Map(); // id -> { user, assistant }
let availableCharacters = [];
let activeCharacterId = null;

let audioContext = null;
let mediaStream = null;
let micWorkletNode = null;
let ttsWorkletNode = null;
let micActive = false;

const BATCH_SAMPLES = 2048;
const HEADER_BYTES = 8;
const FRAME_BYTES = BATCH_SAMPLES * 2;
const MESSAGE_BYTES = HEADER_BYTES + FRAME_BYTES;

const bufferPool = [];
let batchBuffer = null;
let batchView = null;
let batchInt16 = null;
let batchOffset = 0;

const urlParams = new URLSearchParams(window.location.search);

function initBatch() {
  if (!batchBuffer) {
    batchBuffer = bufferPool.pop() || new ArrayBuffer(MESSAGE_BYTES);
    batchView = new DataView(batchBuffer);
    batchInt16 = new Int16Array(batchBuffer, HEADER_BYTES);
    batchOffset = 0;
  }
}

function recycleBatch() {
  if (batchBuffer) {
    bufferPool.push(batchBuffer);
    batchBuffer = null;
  }
}

function getActiveEntry() {
  return activeCharacterId ? sockets.get(activeCharacterId) || null : null;
}

function getHistory(id) {
  if (!chatHistories.has(id)) {
    chatHistories.set(id, []);
  }
  return chatHistories.get(id);
}

function getTyping(id) {
  if (!typingStates.has(id)) {
    typingStates.set(id, { user: "", assistant: "" });
  }
  return typingStates.get(id);
}

function updateStatus(message) {
  statusDiv.textContent = message;
}

function updateCharacterBadge() {
  const entry = getActiveEntry();
  characterBadge.textContent = entry ? entry.name : "";
}

function renderMessages() {
  messagesDiv.innerHTML = "";
  if (!activeCharacterId) return;
  const history = getHistory(activeCharacterId);
  const typing = getTyping(activeCharacterId);

  history.forEach((msg) => {
    const bubble = document.createElement("div");
    bubble.className = `bubble ${msg.role}`;
    bubble.textContent = msg.content;
    messagesDiv.appendChild(bubble);
  });

  if (typing.user) {
    const typingBubble = document.createElement("div");
    typingBubble.className = "bubble user typing";
    typingBubble.innerHTML = typing.user + '<span style="opacity:.6;">✏️</span>';
    messagesDiv.appendChild(typingBubble);
  }

  if (typing.assistant) {
    const typingBubble = document.createElement("div");
    typingBubble.className = "bubble assistant typing";
    typingBubble.innerHTML = typing.assistant + '<span style="opacity:.6;">✏️</span>';
    messagesDiv.appendChild(typingBubble);
  }

  messagesDiv.scrollTop = messagesDiv.scrollHeight;
}

function escapeHtml(str) {
  return (str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function initCharacters() {
  updateStatus("Loading characters...");
  try {
    const resp = await fetch("/characters");
    const data = await resp.json();
    if (Array.isArray(data) && data.length > 0) {
      availableCharacters = data.map((entry) => ({
        id: entry.id,
        name: entry.name || entry.id,
      }));
    }
  } catch (err) {
    console.error("Failed to fetch character list", err);
  }

  if (!Array.isArray(availableCharacters) || availableCharacters.length === 0) {
    availableCharacters = [{ id: "LisaParker", name: "Lisa Parker" }];
  }

  characterSelect.innerHTML = "";
  availableCharacters.forEach(({ id, name }) => {
    const option = document.createElement("option");
    option.value = id;
    option.textContent = name;
    characterSelect.appendChild(option);

    chatHistories.set(id, []);
    typingStates.set(id, { user: "", assistant: "" });

    const entry = {
      id,
      name,
      ws: null,
      isOpen: false,
      isTtsPlaying: false,
      ignoreIncomingTTS: false,
    };
    sockets.set(id, entry);
    openSocket(entry);
  });

  const stored = localStorage.getItem("rtvcCharacterId");
  const urlId = urlParams.get("characterId");
  const fallback = availableCharacters[0]?.id;
  const chosen = urlId || stored || fallback;
  setActiveCharacter(chosen);

  startBtn.disabled = false;
  updateStatus("Ready");
}

function openSocket(entry) {
  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${wsProto}//${location.host}/ws?characterId=${encodeURIComponent(entry.id)}`;
  const ws = new WebSocket(url);
  entry.ws = ws;

  ws.onopen = () => {
    entry.isOpen = true;
    console.log(`[${entry.id}] socket open`);
    if (entry.id === activeCharacterId) {
      updateStatus(micActive ? `Streaming to ${entry.name}` : `Connected to ${entry.name}`);
    }
  };

  ws.onclose = () => {
    entry.isOpen = false;
    console.log(`[${entry.id}] socket closed`);
    if (entry.id === activeCharacterId) {
      updateStatus(`Connection closed for ${entry.name}. Reconnecting...`);
    }
    setTimeout(() => {
      if (sockets.get(entry.id) === entry) {
        openSocket(entry);
      }
    }, 1000);
  };

  ws.onerror = (err) => {
    console.error(`[${entry.id}] socket error`, err);
  };

  ws.onmessage = (evt) => handleSocketMessage(entry, evt);
}

function handleSocketMessage(entry, evt) {
  if (typeof evt.data !== "string") {
    console.warn(`[${entry.id}] received non-text frame`);
    return;
  }
  try {
    const payload = JSON.parse(evt.data);
    handleJSONMessage(entry, payload);
  } catch (err) {
    console.error(`[${entry.id}] failed to parse message`, err, evt.data);
  }
}

function handleJSONMessage(entry, { type, content }) {
  const history = getHistory(entry.id);
  const typing = getTyping(entry.id);

  if (type === "partial_user_request") {
    typing.user = content?.trim() ? escapeHtml(content) : "";
    if (entry.id === activeCharacterId) renderMessages();
    return;
  }

  if (type === "final_user_request") {
    if (content?.trim()) {
      history.push({ role: "user", content, type: "final" });
    }
    typing.user = "";
    if (entry.id === activeCharacterId) renderMessages();
    return;
  }

  if (type === "partial_assistant_answer") {
    typing.assistant = content?.trim() ? escapeHtml(content) : "";
    if (entry.id === activeCharacterId) renderMessages();
    return;
  }

  if (type === "final_assistant_answer") {
    if (content?.trim()) {
      history.push({ role: "assistant", content, type: "final" });
    }
    typing.assistant = "";
    if (entry.id === activeCharacterId) renderMessages();
    return;
  }

  if (type === "tts_chunk") {
    if (entry.ignoreIncomingTTS) return;
    if (entry.id !== activeCharacterId) return;
    if (!ttsWorkletNode) return;
    const int16Data = base64ToInt16Array(content);
    ttsWorkletNode.port.postMessage(int16Data);
    return;
  }

  if (type === "tts_interruption") {
    entry.isTtsPlaying = false;
    entry.ignoreIncomingTTS = false;
    if (entry.id === activeCharacterId && ttsWorkletNode) {
      ttsWorkletNode.port.postMessage({ type: "clear" });
    }
    return;
  }

  if (type === "stop_tts") {
    entry.isTtsPlaying = false;
    entry.ignoreIncomingTTS = true;
    if (entry.id === activeCharacterId && ttsWorkletNode) {
      ttsWorkletNode.port.postMessage({ type: "clear" });
    }
    sendJsonMessage({ type: "tts_stop" }, entry.id);
    return;
  }

  console.log(`[${entry.id}] Unhandled message type`, type);
}

function base64ToInt16Array(b64) {
  const raw = atob(b64);
  const buf = new ArrayBuffer(raw.length);
  const view = new Uint8Array(buf);
  for (let i = 0; i < raw.length; i++) {
    view[i] = raw.charCodeAt(i);
  }
  return new Int16Array(buf);
}

async function startRawPcmCapture() {
  if (micActive) {
    updateStatus(`Already streaming to ${getActiveEntry()?.name ?? "character"}.`);
    return;
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: { ideal: 24000 },
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
      },
    });
    mediaStream = stream;
    if (!audioContext) {
      audioContext = new AudioContext();
    }
    await audioContext.audioWorklet.addModule("/static/pcmWorkletProcessor.js");
    micWorkletNode = new AudioWorkletNode(audioContext, "pcm-worklet-processor");

    micWorkletNode.port.onmessage = ({ data }) => {
      const incoming = new Int16Array(data);
      let read = 0;
      while (read < incoming.length) {
        initBatch();
        const toCopy = Math.min(incoming.length - read, BATCH_SAMPLES - batchOffset);
        batchInt16.set(incoming.subarray(read, read + toCopy), batchOffset);
        batchOffset += toCopy;
        read += toCopy;
        if (batchOffset === BATCH_SAMPLES) {
          flushBatch();
        }
      }
    };

    const source = audioContext.createMediaStreamSource(stream);
    source.connect(micWorkletNode);
    micActive = true;
    updateStatus(`Streaming to ${getActiveEntry()?.name ?? "character"}...`);
  } catch (err) {
    updateStatus("Mic access denied.");
    console.error(err);
  }
}

async function setupTTSPlayback() {
  if (ttsWorkletNode) return;
  if (!audioContext) {
    audioContext = new AudioContext();
  }
  await audioContext.audioWorklet.addModule("/static/ttsPlaybackProcessor.js");
  ttsWorkletNode = new AudioWorkletNode(audioContext, "tts-playback-processor");

  ttsWorkletNode.port.onmessage = (event) => {
    const { type } = event.data;
    const entry = getActiveEntry();
    if (!entry) return;

    if (type === "ttsPlaybackStarted") {
      if (!entry.isTtsPlaying && entry.isOpen) {
        entry.isTtsPlaying = true;
        console.log(`[${entry.id}] TTS playback started.`);
        sendJsonMessage({ type: "tts_start" }, entry.id);
      }
    } else if (type === "ttsPlaybackStopped") {
      if (entry.isTtsPlaying && entry.isOpen) {
        entry.isTtsPlaying = false;
        console.log(`[${entry.id}] TTS playback stopped.`);
        sendJsonMessage({ type: "tts_stop" }, entry.id);
      }
    }
  };

  ttsWorkletNode.connect(audioContext.destination);
}

function cleanupAudio() {
  if (micWorkletNode) {
    micWorkletNode.disconnect();
    micWorkletNode = null;
  }
  if (ttsWorkletNode) {
    ttsWorkletNode.disconnect();
    ttsWorkletNode = null;
  }
  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }
  if (mediaStream) {
    mediaStream.getAudioTracks().forEach((track) => track.stop());
    mediaStream = null;
  }
  micActive = false;
  sockets.forEach((entry) => {
    entry.isTtsPlaying = false;
    entry.ignoreIncomingTTS = false;
  });
  speedSlider.disabled = true;
}

function flushBatch() {
  const entry = getActiveEntry();
  if (!entry || !entry.isOpen || entry.ws.readyState !== WebSocket.OPEN) {
    recycleBatch();
    return;
  }

  const timestamp = Date.now() & 0xffffffff;
  batchView.setUint32(0, timestamp, false);
  const flags = entry.isTtsPlaying ? 1 : 0;
  batchView.setUint32(4, flags, false);

  entry.ws.send(batchBuffer);
  bufferPool.push(batchBuffer);
  batchBuffer = null;
}

function flushRemainder() {
  if (batchOffset > 0) {
    for (let i = batchOffset; i < BATCH_SAMPLES; i++) {
      batchInt16[i] = 0;
    }
    flushBatch();
  }
}

function sendJsonMessage(payload, targetId) {
  const id = targetId || activeCharacterId;
  if (!id) return;
  const entry = sockets.get(id);
  if (!entry || !entry.isOpen || entry.ws.readyState !== WebSocket.OPEN) return;

  const message = {
    character_id: id,
    ...payload,
  };
  entry.ws.send(JSON.stringify(message));
}

function setActiveCharacter(newId) {
  if (!newId) return;
  if (!sockets.has(newId)) {
    console.warn("Unknown character", newId);
    return;
  }

  const previousId = activeCharacterId;
  if (micActive && previousId && previousId !== newId) {
    flushRemainder();
  }

  activeCharacterId = newId;
  characterSelect.value = newId;
  localStorage.setItem("rtvcCharacterId", newId);
  updateCharacterBadge();
  renderMessages();

  const entry = getActiveEntry();
  if (entry && entry.isOpen) {
    updateStatus(micActive ? `Streaming to ${entry.name}` : `Connected to ${entry.name}`);
  } else {
    updateStatus(`Waiting for connection to ${entry?.name ?? newId}...`);
  }
}

async function waitForSocket(entry) {
  if (entry.isOpen && entry.ws.readyState === WebSocket.OPEN) return;
  updateStatus(`Connecting to ${entry.name}...`);
  await new Promise((resolve) => {
    const interval = setInterval(() => {
      if (entry.isOpen && entry.ws.readyState === WebSocket.OPEN) {
        clearInterval(interval);
        resolve();
      }
    }, 100);
  });
}

clearBtn.onclick = () => {
  const entry = getActiveEntry();
  if (!entry) return;
  chatHistories.set(entry.id, []);
  const typing = getTyping(entry.id);
  typing.user = "";
  typing.assistant = "";
  renderMessages();
  sendJsonMessage({ type: "clear_history" }, entry.id);
};

speedSlider.addEventListener("input", (e) => {
  const speedValue = parseInt(e.target.value, 10);
  const entry = getActiveEntry();
  if (!entry || !entry.isOpen) return;
  sendJsonMessage({ type: "set_speed", speed: speedValue }, entry.id);
  console.log(`[${entry.id}] Speed changed to`, speedValue);
});

startBtn.onclick = async () => {
  const entry = getActiveEntry();
  if (!entry) {
    updateStatus("Select a character first.");
    return;
  }

  await waitForSocket(entry);
  await setupTTSPlayback();
  await startRawPcmCapture();
  speedSlider.disabled = false;
};

stopBtn.onclick = () => {
  const entry = getActiveEntry();
  flushRemainder();
  cleanupAudio();
  if (entry) {
    sendJsonMessage({ type: "tts_stop" }, entry.id);
  }
  updateStatus("Stopped.");
};

copyBtn.onclick = () => {
  const history = activeCharacterId ? getHistory(activeCharacterId) : [];
  const typing = activeCharacterId ? getTyping(activeCharacterId) : { user: "", assistant: "" };
  const combined = [...history];
  if (typing.user) combined.push({ role: "user", content: typing.user });
  if (typing.assistant) combined.push({ role: "assistant", content: typing.assistant });

  const text = combined
    .map((msg) => `${msg.role.charAt(0).toUpperCase() + msg.role.slice(1)}: ${msg.content}`)
    .join("\n");

  navigator.clipboard.writeText(text)
    .then(() => console.log("Conversation copied to clipboard"))
    .catch((err) => console.error("Copy failed", err));
};

characterSelect.addEventListener("change", (event) => {
  setActiveCharacter(event.target.value);
});

renderMessages();
initCharacters();
