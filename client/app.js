// State
let callObject = null;
let localStream = null;
let ws = null;
let connected = false;
let startTime = null;
let durationInterval = null;
let micEnabled = true;
let camEnabled = true;

// Transcript segment merging
const MERGE_WINDOW_MS = 3000;
let lastRow = null;
let mergeTimer = null;

// DOM elements
const preConnect = document.getElementById("pre-connect");
const meetingEl = document.getElementById("meeting");
const startBtn = document.getElementById("start-btn");
const localVideo = document.getElementById("local-video");
const userPlaceholder = document.getElementById("user-placeholder");
const agentOrb = document.getElementById("agent-orb");
const captionSpeaker = document.getElementById("caption-speaker");
const captionText = document.getElementById("caption-text");
const transcriptPanel = document.getElementById("transcript-panel");
const micBtn = document.getElementById("mic-btn");
const camBtn = document.getElementById("cam-btn");
const endBtn = document.getElementById("end-btn");
const durationEl = document.getElementById("duration");
const notesPanel = document.getElementById("notes-panel");
const notesEl = document.getElementById("notes");
const remoteAudio = document.getElementById("remote-audio");

// ── Connect ────────────────────────────────────────────────

async function connect() {
  startBtn.disabled = true;
  startBtn.textContent = "Connecting...";

  try {
    // Get local camera stream for preview (Daily handles mic audio separately)
    localStream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480 },
    });
    localVideo.srcObject = localStream;

    // Create Daily room via our server
    const res = await fetch("/api/create-room", { method: "POST" });
    const { room_url, token, error } = await res.json();
    if (error) throw new Error(error);

    // Create Daily call object and join
    callObject = DailyIframe.createCallObject({
      audioSource: true,
      videoSource: false,
    });

    callObject.on("joined-meeting", () => {
      console.log("Joined Daily room");
    });

    callObject.on("error", (e) => {
      console.error("Daily error:", e);
    });

    callObject.on("left-meeting", () => {
      console.log("Left Daily room");
    });

    await callObject.join({ url: room_url, token });

    // Connect WebSocket for transcript streaming
    const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${wsProtocol}//${location.host}/ws/transcripts`);
    ws.onmessage = handleMessage;
    ws.onclose = () => console.log("WebSocket closed");

    // Switch to meeting view
    connected = true;
    preConnect.classList.add("hidden");
    meetingEl.classList.remove("hidden");
    notesPanel.classList.add("hidden");
    startTimer();
    agentOrb.className = "orb orb--small orb--listening";
  } catch (err) {
    console.error("Connection failed:", err);
    startBtn.disabled = false;
    startBtn.textContent = "Start Meeting";
    cleanup();
  }
}

// ── WebSocket message handler ──────────────────────────────

function handleMessage(event) {
  const msg = JSON.parse(event.data);

  switch (msg.type) {
    case "transcript":
      removePartialRow();
      addTranscriptRow(msg.data);
      updateCaption(msg.data.speaker, msg.data.text);
      if (msg.data.speaker === "user") {
        agentOrb.className = "orb orb--small orb--thinking";
      }
      break;
    case "partial":
      updateCaption("user", msg.data.text);
      updatePartialRow(msg.data.text);
      break;
    case "notes":
      showNotes(msg.data);
      break;
  }
}

// ── Transcript panel ───────────────────────────────────────

function addTranscriptRow(data) {
  const now = Date.now();
  const isAgent = data.speaker === "agent";

  if (
    lastRow &&
    lastRow.speaker === data.speaker &&
    now - lastRow.ts < MERGE_WINDOW_MS
  ) {
    const textEl = lastRow.el.querySelector(".transcript-row__text");
    textEl.textContent += " " + data.text;
    lastRow.ts = now;

    clearTimeout(mergeTimer);
    mergeTimer = setTimeout(() => { lastRow = null; }, MERGE_WINDOW_MS);

    transcriptPanel.scrollTop = transcriptPanel.scrollHeight;
    return;
  }

  const row = document.createElement("div");
  row.className = "transcript-row";
  row.innerHTML = `
    <span class="transcript-row__time">${data.timestamp}</span>
    <span class="transcript-row__speaker ${
      isAgent ? "transcript-row__speaker--agent" : "transcript-row__speaker--user"
    }">
      ${isAgent ? "AI Facilitator" : "You"}
    </span>
    <span class="transcript-row__text">${escapeHtml(data.text)}</span>
  `;
  transcriptPanel.appendChild(row);
  transcriptPanel.scrollTop = transcriptPanel.scrollHeight;

  clearTimeout(mergeTimer);
  lastRow = { el: row, speaker: data.speaker, ts: now };
  mergeTimer = setTimeout(() => { lastRow = null; }, MERGE_WINDOW_MS);
}

// ── Partial transcript ─────────────────────────────────────

function updateCaption(speaker, text) {
  captionSpeaker.textContent = speaker === "agent" ? "AI:" : "You:";
  captionText.textContent = text;
}

function updatePartialRow(text) {
  let row = document.getElementById("partial-row");
  if (!row) {
    row = document.createElement("div");
    row.id = "partial-row";
    row.className = "transcript-row transcript-row--partial";
    transcriptPanel.appendChild(row);
  }
  row.innerHTML = `
    <span class="transcript-row__time"></span>
    <span class="transcript-row__speaker transcript-row__speaker--user">You</span>
    <span class="transcript-row__text">${escapeHtml(text)}</span>
  `;
  transcriptPanel.scrollTop = transcriptPanel.scrollHeight;
}

function removePartialRow() {
  const row = document.getElementById("partial-row");
  if (row) row.remove();
}

// ── Controls ───────────────────────────────────────────────

function toggleMic() {
  if (!callObject) return;
  micEnabled = !micEnabled;
  callObject.setLocalAudio(micEnabled);
  micBtn.className = `control-btn ${
    micEnabled ? "control-btn--default" : "control-btn--muted"
  }`;
}

function toggleCamera() {
  if (!localStream) return;
  camEnabled = !camEnabled;
  localStream.getVideoTracks().forEach((t) => (t.enabled = camEnabled));
  camBtn.className = `control-btn ${
    camEnabled ? "control-btn--default" : "control-btn--muted"
  }`;
  if (camEnabled) {
    localVideo.classList.remove("hidden");
    userPlaceholder.classList.add("hidden");
  } else {
    localVideo.classList.add("hidden");
    userPlaceholder.classList.remove("hidden");
  }
}

// ── End meeting ────────────────────────────────────────────

function endMeeting() {
  endBtn.disabled = true;
  endBtn.textContent = "Generating notes...";
  stopTimer();

  if (callObject) {
    callObject.leave();
    callObject.destroy();
    callObject = null;
  }
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
  }

  connected = false;

  meetingEl.classList.add("hidden");
  notesPanel.classList.remove("hidden");
  notesEl.innerHTML = '<div class="loading">Generating meeting notes...</div>';
}

// ── Notes display ──────────────────────────────────────────

function showNotes(markdown) {
  notesEl.innerHTML = marked.parse(markdown);
  if (ws) {
    ws.close();
    ws = null;
  }
}

// ── Reset to start ─────────────────────────────────────────

function resetToStart() {
  cleanup();
  preConnect.classList.remove("hidden");
  meetingEl.classList.add("hidden");
  notesPanel.classList.add("hidden");
  startBtn.disabled = false;
  startBtn.textContent = "Start Meeting";
  transcriptPanel.innerHTML = "";
  captionText.textContent = "Waiting for speech...";
  captionSpeaker.textContent = "";
  durationEl.textContent = "";
  endBtn.disabled = false;
  endBtn.textContent = "End Meeting";
  lastRow = null;
  clearTimeout(mergeTimer);
}

// ── Timer ──────────────────────────────────────────────────

function startTimer() {
  startTime = Date.now();
  durationInterval = setInterval(() => {
    const elapsed = Math.floor((Date.now() - startTime) / 1000);
    const mins = String(Math.floor(elapsed / 60)).padStart(2, "0");
    const secs = String(elapsed % 60).padStart(2, "0");
    durationEl.textContent = `${mins}:${secs}`;
  }, 1000);
}

function stopTimer() {
  if (durationInterval) {
    clearInterval(durationInterval);
    durationInterval = null;
  }
}

// ── Cleanup ────────────────────────────────────────────────

function cleanup() {
  if (callObject) {
    callObject.leave();
    callObject.destroy();
    callObject = null;
  }
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
  }
  if (ws) {
    ws.close();
    ws = null;
  }
  connected = false;
  stopTimer();
}

function escapeHtml(text) {
  const el = document.createElement("div");
  el.textContent = text;
  return el.innerHTML;
}
