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
const injectInput = document.getElementById("injectInput");
const injectBtn = document.getElementById("injectBtn");

// Game Manager UI elements
const gmPanel = document.getElementById("gmPanel");
const gmStatus = document.getElementById("gmStatus");
const gmTimer = document.getElementById("gmTimer");
const gmTriggerBtn = document.getElementById("gmTriggerBtn");
const gmThinking = document.getElementById("gmThinking");
const gmActions = document.getElementById("gmActions");
const gmHistory = document.getElementById("gmHistory");
const gmDisabledOverlay = document.getElementById("gmDisabledOverlay");
const gmContent = document.getElementById("gmContent");
const gmInjectInput = document.getElementById("gmInjectInput");
const gmInjectBtn = document.getElementById("gmInjectBtn");
const gmClues = document.getElementById("gmClues");

// Loading overlay UI elements
const loadingOverlay = document.getElementById("loadingOverlay");
const loadingProgressBar = document.getElementById("loadingProgressBar");
const loadingStatus = document.getElementById("loadingStatus");
const loadingCharacters = document.getElementById("loadingCharacters");
const loadingSteps = document.getElementById("loadingSteps");

// Loading state tracking
const loadingStates = new Map(); // id -> { name, ready }
let allCharactersReady = false;

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
    // Handle injection messages with special styling
    if (msg.type === "injection") {
      bubble.className = "bubble injection";
    } else {
      bubble.className = `bubble ${msg.role}`;
    }
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

// ============================================
// Loading Overlay Functions
// ============================================
function initLoadingState(characterId, characterName) {
  loadingStates.set(characterId, {
    id: characterId,
    name: characterName,
    ready: false
  });
  renderLoadingUI();
}

function markCharacterReady(characterId) {
  const state = loadingStates.get(characterId);
  if (state) {
    state.ready = true;
    console.log(`[Loading] ${characterId} is ready`);
  }
  renderLoadingUI();
  checkAllReady();
}

function renderLoadingUI() {
  const states = Array.from(loadingStates.values());
  const readyCount = states.filter(s => s.ready).length;
  const totalCount = states.length;
  
  // Update progress bar
  if (loadingProgressBar && totalCount > 0) {
    const progress = (readyCount / totalCount) * 100;
    loadingProgressBar.style.width = `${progress}%`;
  }
  
  // Update status text
  if (loadingStatus) {
    const loadingChars = states.filter(s => !s.ready);
    if (loadingChars.length > 0) {
      loadingStatus.textContent = `Initializing ${loadingChars[0].name}...`;
    } else if (totalCount > 0) {
      loadingStatus.textContent = "All characters ready!";
    }
  }
  
  // Update character cards
  if (loadingCharacters) {
    const html = states.map(state => {
      const statusClass = state.ready ? "ready" : "loading";
      const icon = state.ready ? "✓" : "◌";
      const statusText = state.ready ? "Ready" : "Initializing...";
      return `
        <div class="loading-character ${statusClass}">
          <span class="loading-character-icon">${icon}</span>
          <span class="loading-character-name">${escapeHtml(state.name)}</span>
          <span class="loading-character-status">${escapeHtml(statusText)}</span>
        </div>
      `;
    }).join("");
    loadingCharacters.innerHTML = html;
  }
  
  // Update steps indicator
  if (loadingSteps) {
    loadingSteps.textContent = `${readyCount}/${totalCount} characters loaded`;
  }
}

function checkAllReady() {
  const states = Array.from(loadingStates.values());
  const allReady = states.length > 0 && states.every(s => s.ready);
  
  if (allReady && !allCharactersReady) {
    allCharactersReady = true;
    console.log("[Loading] All characters ready!");
    
    // Update UI status
    updateStatus("Ready - click Start to begin");
    
    // Hide loading overlay - user can now click Start
    setTimeout(() => {
      hideLoadingOverlay();
    }, 300);
  }
}

function hideLoadingOverlay() {
  if (loadingOverlay) {
    loadingOverlay.classList.add("hidden");
  }
}

function showLoadingOverlay() {
  if (loadingOverlay) {
    loadingOverlay.classList.remove("hidden");
  }
}

async function initCharacters() {
  console.log("[initCharacters] Starting...");
  updateStatus("Loading characters...");
  if (loadingStatus) loadingStatus.textContent = "Fetching character list...";
  
  try {
    const resp = await fetch("/characters");
    console.log("[initCharacters] Fetch response:", resp.status);
    const data = await resp.json();
    console.log("[initCharacters] Characters data:", data);
    if (Array.isArray(data) && data.length > 0) {
      availableCharacters = data.map((entry) => ({
        id: entry.id,
        name: entry.name || entry.id,
      }));
      console.log("[initCharacters] Parsed characters:", availableCharacters);
    }
  } catch (err) {
    console.error("[initCharacters] Failed to fetch character list", err);
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
    
    // Initialize loading state for this character
    initLoadingState(id, name);

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
  console.log(`[openSocket] Opening socket for ${entry.id}...`);
  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${wsProto}//${location.host}/ws?characterId=${encodeURIComponent(entry.id)}`;
  console.log(`[openSocket] URL: ${url}`);
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

  if (type === "inject_confirmed") {
    // Add injection to chat history as a special message type
    if (content?.trim()) {
      history.push({ role: "system", content: content, type: "injection" });
    }
    if (entry.id === activeCharacterId) renderMessages();
    console.log(`[${entry.id}] Injection confirmed:`, content);
    return;
  }

  if (type === "character_ready") {
    // Character initialization is complete
    console.log(`[${entry.id}] Character ready!`);
    markCharacterReady(entry.id);
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

// Injection functionality
injectBtn.onclick = () => {
  const content = injectInput.value.trim();
  if (!content) {
    console.log("Inject: empty content, skipping");
    return;
  }
  const entry = getActiveEntry();
  if (!entry || !entry.isOpen) {
    console.warn("Inject: no active connection");
    return;
  }
  sendJsonMessage({ type: "inject", content: content }, entry.id);
  injectInput.value = "";
  console.log(`[${entry.id}] Sent injection:`, content);
};

// Allow Enter key to inject
injectInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    injectBtn.click();
  }
});

renderMessages();
initCharacters();

// ==================== GAME MANAGER ====================

let gmWebSocket = null;
let gmState = {
  enabled: false,
  is_processing: false,
  seconds_until_tick: 0,
  tick_interval: 30,
  last_thinking: "",
  last_actions: [],
  history: []
};

function initGameManager() {
  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const gmUrl = `${wsProto}//${location.host}/ws/game_manager`;
  
  gmWebSocket = new WebSocket(gmUrl);
  
  gmWebSocket.onopen = () => {
    console.log("[GameManager] WebSocket connected");
    updateGmStatus("connected");
  };
  
  gmWebSocket.onclose = () => {
    console.log("[GameManager] WebSocket disconnected, reconnecting...");
    updateGmStatus("disconnected");
    setTimeout(initGameManager, 2000);
  };
  
  gmWebSocket.onerror = (err) => {
    console.error("[GameManager] WebSocket error", err);
  };
  
  gmWebSocket.onmessage = (evt) => {
    try {
      const data = JSON.parse(evt.data);
      if (data.type === "game_manager_state") {
        handleGmStateUpdate(data);
      }
    } catch (e) {
      console.error("[GameManager] Failed to parse message", e);
    }
  };
}

function handleGmStateUpdate(data) {
  gmState = {
    enabled: data.enabled,
    is_processing: data.is_processing,
    seconds_until_tick: data.seconds_until_tick,
    tick_interval: data.tick_interval,
    last_thinking: data.last_thinking || "",
    last_actions: data.last_actions || [],
    history: data.history || []
  };
  
  renderGameManager();
}

function updateGmStatus(status) {
  if (status === "connected") {
    // Will be updated by state
  } else if (status === "disconnected") {
    gmStatus.textContent = "● Disconnected";
    gmStatus.className = "gm-status disabled";
  }
}

function renderGameManager() {
  // Update enabled/disabled overlay
  if (!gmState.enabled) {
    gmDisabledOverlay.classList.add("visible");
    gmStatus.textContent = "● Disabled";
    gmStatus.className = "gm-status disabled";
    return;
  } else {
    gmDisabledOverlay.classList.remove("visible");
  }
  
  // Update status
  if (gmState.is_processing) {
    gmStatus.textContent = "● Processing...";
    gmStatus.className = "gm-status processing";
  } else {
    gmStatus.textContent = "● Active";
    gmStatus.className = "gm-status active";
  }
  
  // Update timer
  gmTimer.textContent = `${gmState.seconds_until_tick}s`;
  
  // Update thinking
  if (gmState.last_thinking) {
    gmThinking.innerHTML = escapeHtml(gmState.last_thinking);
  } else {
    gmThinking.innerHTML = "<em>Waiting for first tick...</em>";
  }
  
  // Update actions
  if (gmState.last_actions && gmState.last_actions.length > 0) {
    gmActions.innerHTML = gmState.last_actions.map(action => `
      <div class="gm-action-item">
        <div class="gm-action-target">→ ${escapeHtml(action.target)}</div>
        <div class="gm-action-text">${escapeHtml(action.instruction)}</div>
      </div>
    `).join("");
  } else {
    gmActions.innerHTML = "<em>No actions</em>";
  }
  
  // Update history
  if (gmState.history && gmState.history.length > 0) {
    gmHistory.innerHTML = gmState.history.slice().reverse().map(entry => {
      const time = new Date(entry.timestamp * 1000);
      const timeStr = time.toLocaleTimeString();
      const actionCount = entry.actions ? entry.actions.length : 0;
      const summary = actionCount > 0 
        ? `${actionCount} injection(s)` 
        : "No changes";
      return `
        <div class="gm-history-item">
          <span class="gm-history-time">${timeStr}</span>
          <span class="gm-history-summary"> - ${summary}</span>
        </div>
      `;
    }).join("");
  } else {
    gmHistory.innerHTML = "<em>No history</em>";
  }
  
  // Update clues display
  if (gmClues) {
    if (gmState.clues && gmState.clues.length > 0) {
      gmClues.innerHTML = gmState.clues.map((clue, idx) => `
        <div class="gm-clue-item">
          <span class="gm-clue-text">${escapeHtml(clue)}</span>
          <button class="gm-clue-remove" onclick="removeGmClue(${idx})" title="Remove clue">✕</button>
        </div>
      `).join("");
    } else {
      gmClues.innerHTML = "<em>No active clues</em>";
    }
  }
}

// Remove a clue from Game Manager
function removeGmClue(index) {
  if (gmWebSocket && gmWebSocket.readyState === WebSocket.OPEN) {
    gmWebSocket.send(JSON.stringify({ type: "remove_clue", index: index }));
    console.log("[GameManager] Remove clue at index:", index);
  }
}

// Trigger button
if (gmTriggerBtn) {
  gmTriggerBtn.onclick = () => {
    if (gmWebSocket && gmWebSocket.readyState === WebSocket.OPEN) {
      gmWebSocket.send(JSON.stringify({ type: "trigger" }));
      console.log("[GameManager] Manual trigger sent");
    }
  };
}

// Game Manager Inject button
if (gmInjectBtn && gmInjectInput) {
  gmInjectBtn.onclick = () => {
    const clue = gmInjectInput.value.trim();
    if (!clue) return;
    
    if (gmWebSocket && gmWebSocket.readyState === WebSocket.OPEN) {
      gmWebSocket.send(JSON.stringify({ type: "inject_clue", content: clue }));
      console.log("[GameManager] Clue injected:", clue);
      gmInjectInput.value = "";
    }
  };
  
  // Enter key to submit
  gmInjectInput.addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
      gmInjectBtn.click();
    }
  });
}

// Initialize Game Manager connection
initGameManager();

// ==================== NPC CONVERSATION ====================

// NPC Conversation UI elements
const npcConvPanel = document.getElementById("npcConvPanel");
const npcConvStatus = document.getElementById("npcConvStatus");
const npcConvNpc1 = document.getElementById("npcConvNpc1");
const npcConvNpc2 = document.getElementById("npcConvNpc2");
const npcConvTurns = document.getElementById("npcConvTurns");
const npcConvContext = document.getElementById("npcConvContext");
const npcConvStartBtn = document.getElementById("npcConvStartBtn");
const npcConvStopBtn = document.getElementById("npcConvStopBtn");
const npcConvMessages = document.getElementById("npcConvMessages");

let npcConvWebSocket = null;
let npcConvState = {
  state: "idle",
  config: null,
  current_turn: 0,
  turns_remaining: 0,
  current_speaker: null,
  conversation_history: [],
  error: null
};
let npcConvAvailableCharacters = [];

// NPC Conversation Audio
let npcConvAudioContext = null;
let npcConvAudioQueue = [];
let npcConvIsPlaying = false;
let npcConvCurrentSource = null; // Track current playing audio source for interruption

function initNpcConversation() {
  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const npcConvUrl = `${wsProto}//${location.host}/ws/npc_conversation`;
  
  npcConvWebSocket = new WebSocket(npcConvUrl);
  
  npcConvWebSocket.onopen = () => {
    console.log("[NPC Conv] WebSocket connected");
    updateNpcConvStatus("idle");
  };
  
  npcConvWebSocket.onclose = () => {
    console.log("[NPC Conv] WebSocket disconnected, reconnecting...");
    updateNpcConvStatus("disconnected");
    setTimeout(initNpcConversation, 2000);
  };
  
  npcConvWebSocket.onerror = (err) => {
    console.error("[NPC Conv] WebSocket error", err);
  };
  
  npcConvWebSocket.onmessage = async (evt) => {
    try {
      if (evt.data instanceof Blob) {
        // Audio data
        const arrayBuffer = await evt.data.arrayBuffer();
        handleNpcConvAudio(arrayBuffer);
      } else {
        const data = JSON.parse(evt.data);
        if (data.type === "npc_conversation_state") {
          handleNpcConvStateUpdate(data);
        } else if (data.type === "available_characters") {
          handleNpcConvAvailableCharacters(data);
        } else if (data.type === "npc_conversation_turn") {
          handleNpcConvTurn(data);
        } else if (data.type === "npc_conversation_interrupted") {
          handleNpcConvInterrupted(data);
        } else if (data.type === "error") {
          console.error("[NPC Conv] Error:", data.message);
          alert("NPC Conversation Error: " + data.message);
        }
      }
    } catch (e) {
      console.error("[NPC Conv] Failed to parse message", e);
    }
  };
}

let npcConvConnectedCharacters = [];

function handleNpcConvAvailableCharacters(data) {
  // data can be array (old format) or object with characters and connected
  let characters, connected;
  if (Array.isArray(data)) {
    characters = data;
    connected = data;
  } else {
    characters = data.characters || [];
    connected = data.connected || [];
  }
  
  npcConvAvailableCharacters = characters;
  npcConvConnectedCharacters = connected;
  
  // Populate dropdowns - show connection status
  const npc1Options = characters.map(c => {
    const isConnected = connected.includes(c);
    return `<option value="${c}" ${!isConnected ? 'disabled' : ''}>${c}${isConnected ? ' ✓' : ' (not connected)'}</option>`;
  }).join("");
  
  const npc2Options = `<option value="">(None - Monologue)</option>` + 
    characters.map(c => {
      const isConnected = connected.includes(c);
      return `<option value="${c}" ${!isConnected ? 'disabled' : ''}>${c}${isConnected ? ' ✓' : ' (not connected)'}</option>`;
    }).join("");
  
  if (npcConvNpc1) {
    npcConvNpc1.innerHTML = `<option value="">Select character...</option>` + npc1Options;
  }
  if (npcConvNpc2) {
    npcConvNpc2.innerHTML = npc2Options;
  }
  
  console.log("[NPC Conv] Characters:", characters, "Connected:", connected);
}

function handleNpcConvStateUpdate(data) {
  npcConvState = {
    state: data.state || "idle",
    config: data.config,
    current_turn: data.current_turn || 0,
    turns_remaining: data.turns_remaining || 0,
    current_speaker: data.current_speaker,
    conversation_history: data.conversation_history || [],
    error: data.error
  };
  
  renderNpcConversation();
}

function handleNpcConvTurn(data) {
  // Add turn to local state for immediate rendering
  const turn = {
    speaker_id: data.speaker_id,
    message: data.message,
    turn_number: data.turn_number,
    is_last: data.is_last
  };
  
  // Check if this turn already exists in history
  const exists = npcConvState.conversation_history.some(t => 
    t.turn_number === turn.turn_number && t.speaker_id === turn.speaker_id
  );
  
  if (!exists) {
    npcConvState.conversation_history.push(turn);
    renderNpcConvMessages();
  }
  
  console.log(`[NPC Conv] Turn ${turn.turn_number}: ${turn.speaker_id} says: ${turn.message.substring(0, 50)}...`);
}

function handleNpcConvInterrupted(data) {
  console.log(`[NPC Conv] 🛑 Conversation interrupted: ${data.reason || 'player speaking'}`);
  
  // Clear the audio queue immediately
  npcConvAudioQueue = [];
  
  // Stop any currently playing audio
  if (npcConvCurrentSource) {
    try {
      npcConvCurrentSource.stop();
      console.log("[NPC Conv] Stopped current audio playback");
    } catch (e) {
      // Source might already be stopped
    }
    npcConvCurrentSource = null;
  }
  
  npcConvIsPlaying = false;
  
  // Update state
  npcConvState.state = "stopping";
  updateNpcConvStatus("idle");
  
  console.log("[NPC Conv] Audio playback interrupted, queue cleared");
}

function handleNpcConvAudio(arrayBuffer) {
  // First 32 bytes are speaker ID (null-padded)
  const headerBytes = new Uint8Array(arrayBuffer, 0, 32);
  let speakerId = "";
  for (let i = 0; i < 32 && headerBytes[i] !== 0; i++) {
    speakerId += String.fromCharCode(headerBytes[i]);
  }
  
  // Rest is audio data
  const audioData = arrayBuffer.slice(32);
  
  console.log(`[NPC Conv] Received audio for ${speakerId}, ${audioData.byteLength} bytes`);
  
  if (audioData.byteLength < 100) {
    console.warn(`[NPC Conv] Audio data too small for ${speakerId}, skipping`);
    return;
  }
  
  // Queue audio for playback
  npcConvAudioQueue.push({ speakerId, audioData });
  playNextNpcConvAudio();
}

async function playNextNpcConvAudio() {
  if (npcConvIsPlaying || npcConvAudioQueue.length === 0) return;
  
  npcConvIsPlaying = true;
  const { speakerId, audioData } = npcConvAudioQueue.shift();
  
  try {
    // Create or resume audio context
    if (!npcConvAudioContext) {
      npcConvAudioContext = new (window.AudioContext || window.webkitAudioContext)();
      console.log(`[NPC Conv] Created AudioContext, state: ${npcConvAudioContext.state}`);
    }
    
    // Resume if suspended (browser autoplay policy)
    if (npcConvAudioContext.state === 'suspended') {
      console.log('[NPC Conv] Resuming suspended AudioContext...');
      await npcConvAudioContext.resume();
    }
    
    // Decode the audio - 24kHz 16-bit PCM
    const int16Array = new Int16Array(audioData);
    if (int16Array.length === 0) {
      console.warn(`[NPC Conv] Empty audio data for ${speakerId}`);
      npcConvIsPlaying = false;
      playNextNpcConvAudio();
      return;
    }
    
    const float32Array = new Float32Array(int16Array.length);
    for (let i = 0; i < int16Array.length; i++) {
      float32Array[i] = int16Array[i] / 32768.0;
    }
    
    const audioBuffer = npcConvAudioContext.createBuffer(1, float32Array.length, 24000);
    audioBuffer.getChannelData(0).set(float32Array);
    
    const source = npcConvAudioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(npcConvAudioContext.destination);
    
    // Track current source for interruption
    npcConvCurrentSource = source;
    
    source.onended = () => {
      console.log(`[NPC Conv] Finished playing audio for ${speakerId}`);
      npcConvCurrentSource = null;
      npcConvIsPlaying = false;
      playNextNpcConvAudio();
    };
    
    source.start();
    const durationSec = (float32Array.length / 24000).toFixed(1);
    console.log(`[NPC Conv] Playing ${durationSec}s audio for ${speakerId}`);
    
  } catch (e) {
    console.error("[NPC Conv] Audio playback error:", e);
    npcConvIsPlaying = false;
    playNextNpcConvAudio();
  }
}

function updateNpcConvStatus(status) {
  if (!npcConvStatus) return;
  
  if (status === "idle") {
    npcConvStatus.textContent = "● Idle";
    npcConvStatus.className = "npc-conv-status idle";
  } else if (status === "running") {
    npcConvStatus.textContent = "● Running...";
    npcConvStatus.className = "npc-conv-status running";
  } else if (status === "finished") {
    npcConvStatus.textContent = "● Finished";
    npcConvStatus.className = "npc-conv-status finished";
  } else if (status === "stopping" || status === "interrupted") {
    npcConvStatus.textContent = "● Interrupted";
    npcConvStatus.className = "npc-conv-status idle";
  } else if (status === "disconnected") {
    npcConvStatus.textContent = "● Disconnected";
    npcConvStatus.className = "npc-conv-status idle";
  }
}

function renderNpcConversation() {
  // Update status
  updateNpcConvStatus(npcConvState.state);
  
  // Show/hide buttons
  if (npcConvStartBtn && npcConvStopBtn) {
    if (npcConvState.state === "running") {
      npcConvStartBtn.style.display = "none";
      npcConvStopBtn.style.display = "block";
    } else {
      npcConvStartBtn.style.display = "block";
      npcConvStopBtn.style.display = "none";
    }
  }
  
  // Render messages
  renderNpcConvMessages();
}

function renderNpcConvMessages() {
  if (!npcConvMessages) return;
  
  if (npcConvState.conversation_history.length === 0) {
    npcConvMessages.innerHTML = `<div class="npc-conv-empty">Select characters and start a conversation</div>`;
    return;
  }
  
  npcConvMessages.innerHTML = npcConvState.conversation_history.map(turn => `
    <div class="npc-conv-turn ${turn.is_last ? 'last' : ''}">
      <div class="npc-conv-turn-header">
        <span class="npc-conv-speaker">${turn.speaker_id}</span>
        <span class="npc-conv-turn-num">#${turn.turn_number}</span>
      </div>
      <div class="npc-conv-message">${escapeHtml(turn.message)}</div>
    </div>
  `).join("");
  
  // Scroll to bottom
  npcConvMessages.scrollTop = npcConvMessages.scrollHeight;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Start conversation button
if (npcConvStartBtn) {
  npcConvStartBtn.onclick = () => {
    const npc1 = npcConvNpc1 ? npcConvNpc1.value : "";
    const npc2 = npcConvNpc2 ? npcConvNpc2.value : "";
    const turns = npcConvTurns ? parseInt(npcConvTurns.value) || 4 : 4;
    const context = npcConvContext ? npcConvContext.value.trim() : "";
    
    if (!npc1) {
      alert("Please select NPC 1");
      return;
    }
    
    if (npcConvWebSocket && npcConvWebSocket.readyState === WebSocket.OPEN) {
      // Clear previous conversation
      npcConvState.conversation_history = [];
      npcConvAudioQueue = [];
      renderNpcConvMessages();
      
      npcConvWebSocket.send(JSON.stringify({
        type: "start_conversation",
        npc1_id: npc1,
        npc2_id: npc2,
        turns: turns,
        context: context
      }));
      console.log(`[NPC Conv] Starting conversation: ${npc1} <-> ${npc2 || '(monologue)'}, ${turns} turns`);
    }
  };
}

// Stop conversation button
if (npcConvStopBtn) {
  npcConvStopBtn.onclick = () => {
    if (npcConvWebSocket && npcConvWebSocket.readyState === WebSocket.OPEN) {
      npcConvWebSocket.send(JSON.stringify({ type: "stop_conversation" }));
      console.log("[NPC Conv] Stop requested");
    }
  };
}

// Initialize NPC Conversation connection
initNpcConversation();
