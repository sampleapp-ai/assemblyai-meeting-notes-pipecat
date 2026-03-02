// State
let pc = null;
let localStream = null;
let ws = null;
let sessionId = null;
let connected = false;
let startTime = null;
let durationInterval = null;
let micEnabled = true;
let camEnabled = true;

// Audio analysis for orb
let audioCtx = null;
let analyser = null;
let animFrame = null;

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
    // Get mic + camera
    localStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000 },
      video: { width: 640, height: 480 },
    });

    // Show local video
    localVideo.srcObject = localStream;

    // Fetch ICE servers (includes TURN if configured on server)
    const iceRes = await fetch("/api/ice-servers");
    const iceServers = await iceRes.json();

    // Create WebRTC peer connection
    pc = new RTCPeerConnection({ iceServers });

    // Only add audio track to peer connection (video stays local-only)
    const audioTrack = localStream.getAudioTracks()[0];
    if (audioTrack) {
      pc.addTrack(audioTrack, localStream);
    }

    // Handle remote audio from server (TTS output)
    pc.ontrack = (event) => {
      const remoteStream = event.streams[0] || new MediaStream([event.track]);
      remoteAudio.srcObject = remoteStream;
      setupAudioAnalysis(remoteStream);
    };

    // Create SDP offer
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // Wait for ICE gathering
    await new Promise((resolve) => {
      if (pc.iceGatheringState === "complete") resolve();
      else {
        pc.addEventListener("icegatheringstatechange", () => {
          if (pc.iceGatheringState === "complete") resolve();
        });
      }
    });

    // Start session
    const startRes = await fetch("/start", { method: "POST" });
    const startData = await startRes.json();
    sessionId = startData.session_id;

    // Send offer to server
    const offerRes = await fetch(`/sessions/${sessionId}/api/offer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: pc.localDescription.sdp,
        type: pc.localDescription.type,
      }),
    });
    const answer = await offerRes.json();

    // Set remote description
    await pc.setRemoteDescription(
      new RTCSessionDescription({ sdp: answer.sdp, type: answer.type })
    );

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

// ── Audio analysis for orb ─────────────────────────────────

function setupAudioAnalysis(stream) {
  audioCtx = new AudioContext();
  const source = audioCtx.createMediaStreamSource(stream);
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.6;
  source.connect(analyser);

  const data = new Uint8Array(analyser.frequencyBinCount);

  function tick() {
    analyser.getByteFrequencyData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) sum += data[i];
    const level = sum / data.length / 255; // 0..1

    // Drive orb scale and glow from audio level
    const scale = 1 + level * 0.25;
    const glow = 40 + level * 80;
    agentOrb.style.transform = `scale(${scale})`;
    agentOrb.style.boxShadow = `0 0 ${glow}px rgba(94, 200, 242, ${
      0.25 + level * 0.4
    }), 0 0 ${glow * 2}px rgba(94, 200, 242, ${
      0.1 + level * 0.2
    }), inset 0 0 30px rgba(0, 0, 0, 0.4)`;

    // Update orb class based on audio activity
    if (level > 0.02) {
      if (!agentOrb.classList.contains("orb--speaking")) {
        agentOrb.className = "orb orb--small orb--speaking";
      }
    } else {
      if (agentOrb.classList.contains("orb--speaking")) {
        agentOrb.className = "orb orb--small orb--listening";
        agentOrb.style.transform = "";
        agentOrb.style.boxShadow = "";
      }
    }

    animFrame = requestAnimationFrame(tick);
  }
  tick();
}

// ── WebSocket message handler ──────────────────────────────

function handleMessage(event) {
  const msg = JSON.parse(event.data);

  switch (msg.type) {
    case "transcript":
      addTranscriptRow(msg.data);
      updateCaption(msg.data.speaker, msg.data.text);
      if (msg.data.speaker === "user") {
        agentOrb.className = "orb orb--small orb--thinking";
      }
      break;
    case "partial":
      updateCaption("user", msg.data.text);
      break;
    case "notes":
      showNotes(msg.data);
      break;
  }
}

// ── Transcript panel ───────────────────────────────────────

function addTranscriptRow(data) {
  const row = document.createElement("div");
  row.className = "transcript-row";
  const isAgent = data.speaker === "agent";
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
}

// ── Caption bar ────────────────────────────────────────────

function updateCaption(speaker, text) {
  captionSpeaker.textContent = speaker === "agent" ? "AI:" : "You:";
  captionText.textContent = text;
}

// ── Controls ───────────────────────────────────────────────

function toggleMic() {
  if (!localStream) return;
  micEnabled = !micEnabled;
  localStream.getAudioTracks().forEach((t) => (t.enabled = micEnabled));
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

  // Stop local tracks (triggers on_client_disconnected → note generation)
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
  }
  if (pc) {
    pc.close();
    pc = null;
  }
  if (animFrame) {
    cancelAnimationFrame(animFrame);
    animFrame = null;
  }
  if (audioCtx) {
    audioCtx.close();
    audioCtx = null;
  }

  connected = false;

  // Show notes panel with loading state
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
  if (localStream) {
    localStream.getTracks().forEach((t) => t.stop());
    localStream = null;
  }
  if (pc) {
    pc.close();
    pc = null;
  }
  if (ws) {
    ws.close();
    ws = null;
  }
  if (animFrame) {
    cancelAnimationFrame(animFrame);
    animFrame = null;
  }
  if (audioCtx) {
    audioCtx.close();
    audioCtx = null;
  }
  connected = false;
  stopTimer();
}

function escapeHtml(text) {
  const el = document.createElement("div");
  el.textContent = text;
  return el.innerHTML;
}
