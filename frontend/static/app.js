// Capture mic + system audio, downsample via the worklet, stream 16 kHz PCM to
// the backend over two WebSockets (one per source), and render transcripts.

const $ = (id) => document.getElementById(id);
const transcriptEl = $("transcript");
const statusEl = $("status");

let audioCtx = null;
const sources = {}; // name -> { stream, node, ws, source }
const sourceEpochs = {}; // name -> wall-clock ms when that source's stream started
const entries = []; // accumulated transcript lines for export: {source, text, at, dur}
const meetingId = crypto.randomUUID(); // one meeting per page load
const lineEls = {}; // transcript line id -> DOM element (for echo retraction)
let suggWs = null;
const meetingCfg = { mode: "online", party: "one" }; // segmented toggles
let enrolled = false;
let appCfg = { public_url: "", deep_dive: true }; // from /api/config

function httpsHint() {
  return appCfg.public_url
    ? `open this page via HTTPS — use ${appCfg.public_url}`
    : "open this page via HTTPS (see README: tailscale serve / reverse proxy)";
}

async function loadAppConfig() {
  try {
    const r = await fetch("/api/config");
    appCfg = await r.json();
    // Deep dive works on every provider (web search only where supported).
    $("deepDiveBtn").textContent = appCfg.deep_dive ? "🌐 Deep dive" : "🔍 Deep dive";
    $("deepDiveBtn").title = appCfg.deep_dive
      ? "Slower run using the deep model; may search the web for current facts"
      : "Slower, more thorough run using the deep model (this provider has no web search)";
    const m = appCfg.models || {};
    $("modelInfo").textContent =
      `Currently selected models · ${appCfg.provider} — quick: ${m.fast || "—"} · deep: ${m.deep || "—"}`;
  } catch { /* defaults stand */ }
}

// Settings may change in another tab — refresh the indicator on return.
window.addEventListener("focus", loadAppConfig);

function setStatus(msg) {
  statusEl.textContent = msg;
}

function wsUrl(path, params) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({
    ...params, meeting: meetingId, mode: meetingCfg.mode, party: meetingCfg.party,
  });
  return `${proto}://${location.host}${path}?${q}`;
}

// --- mode / party segmented toggles ---
function bindSeg(id, key, onChange) {
  const seg = $(id);
  seg.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      meetingCfg[key] = b.dataset.v;
      seg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
      if (onChange) onChange();
    });
  });
}

function applyModeUi() {
  const inPerson = meetingCfg.mode === "inperson";
  // In-person: one mic hears the room; system-audio capture is meaningless.
  $("sysLabel").style.display = inPerson ? "none" : "";
  $("micLabel").style.display = inPerson ? "none" : "";
  if (inPerson && !enrolled) setStatus("in-person mode needs your voice enrolled — click 🎙 Enroll voice");
  else if (statusEl.textContent.startsWith("in-person mode needs")) setStatus("idle");
}

async function ensureContext() {
  if (audioCtx) return audioCtx;
  audioCtx = new AudioContext();
  await audioCtx.audioWorklet.addModule("/static/pcm-worklet.js");
  return audioCtx;
}

function makeAudioWs(name) {
  const ws = new WebSocket(wsUrl("/ws/audio", { source: name }));
  ws.binaryType = "arraybuffer";
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "transcript") addLine(msg);
  };
  return ws;
}

// Wire one MediaStream's audio into the worklet -> WS pipeline.
async function startSource(name, stream) {
  const ctx = await ensureContext();
  if (ctx.state === "suspended") await ctx.resume();
  sourceEpochs[name] = Date.now(); // aligns this stream's t0-relative times to the wall clock

  const ws = makeAudioWs(name);
  ws.onopen = () => setStatus(`streaming: ${activeNames().join(" + ")}`);
  ws.onclose = () => scheduleReconnect(name);
  ws.onerror = () => setStatus(`websocket error (${name})`);

  const srcNode = ctx.createMediaStreamSource(stream);
  const worklet = new AudioWorkletNode(ctx, "pcm-worklet");
  worklet.port.onmessage = (ev) => {
    // Look the socket up each time: reconnects swap it out under us.
    const s = sources[name];
    if (s && s.ws.readyState === WebSocket.OPEN) s.ws.send(ev.data);
  };
  srcNode.connect(worklet);
  // Do NOT connect to destination — we don't want to play the audio back.

  sources[name] = { stream, node: worklet, srcNode, ws, userStopped: false, reconnecting: false };
}

// The backend dropped (e.g. watchdog self-restart). The media stream is still
// alive in this page, so keep it and re-attach when the server comes back.
function scheduleReconnect(name) {
  const s = sources[name];
  if (!s || s.userStopped || s.reconnecting) return;
  s.reconnecting = true;
  setStatus("backend connection lost — reconnecting… (capture is still running)");
  const attempt = () => {
    if (!sources[name] || s.userStopped) return;
    const nw = makeAudioWs(name);
    nw.onopen = () => {
      s.ws = nw;
      s.reconnecting = false;
      // The server restarted its per-connection clock; re-anchor exports.
      sourceEpochs[name] = Date.now();
      nw.onclose = () => scheduleReconnect(name);
      setStatus(`reconnected — streaming: ${activeNames().join(" + ")}`);
      connectSuggestions();
    };
    nw.onclose = () => { if (s.reconnecting) setTimeout(attempt, 3000); };
    nw.onerror = () => {};
  };
  attempt();
}

function activeNames() {
  return Object.keys(sources);
}

function stopSource(name) {
  const s = sources[name];
  if (!s) return;
  s.userStopped = true;
  try { s.srcNode.disconnect(); } catch {}
  try { s.node.disconnect(); } catch {}
  try { s.stream.getTracks().forEach((t) => t.stop()); } catch {}
  try { if (s.ws.readyState === WebSocket.OPEN) s.ws.close(); } catch {}
  delete sources[name];
  if (activeNames().length === 0) {
    setStatus("stopped");
    $("startBtn").disabled = false;
    $("stopBtn").disabled = true;
  }
}

async function start() {
  // Browsers expose mic/screen capture only in secure contexts (HTTPS or localhost).
  if (!navigator.mediaDevices) {
    setStatus("capture blocked: " + httpsHint());
    return;
  }
  const inPerson = meetingCfg.mode === "inperson";
  if (inPerson && !enrolled) {
    setStatus("in-person mode needs your voice enrolled first — click 🎙 Enroll voice");
    return;
  }
  $("startBtn").disabled = true;
  const wantMic = inPerson || $("micToggle").checked;
  const wantSys = !inPerson && $("sysToggle").checked;
  if (!wantMic && !wantSys) {
    setStatus("enable at least one source");
    $("startBtn").disabled = false;
    return;
  }

  connectSuggestions();
  try {
    if (wantMic) {
      const mic = await navigator.mediaDevices.getUserMedia({
        // In-person: raw room audio — browser noise suppression eats far-field
        // voices, so keep processing minimal and let whisper cope.
        audio: inPerson
          ? { echoCancellation: false, noiseSuppression: false, channelCount: 1 }
          : { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
      });
      await startSource("mic", mic);
    }
    if (wantSys) {
      // Browsers require a video track to grant system/tab audio; we capture it
      // and immediately drop the video, keeping only the audio.
      setStatus("pick a tab/window/screen and CHECK 'share audio'…");
      const disp = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      disp.getVideoTracks().forEach((t) => t.stop());
      if (disp.getAudioTracks().length === 0) {
        setStatus("no system audio captured — re-share and check 'share audio'");
        disp.getTracks().forEach((t) => t.stop());
      } else {
        await startSource("system", disp);
      }
    }
    $("stopBtn").disabled = false;
  } catch (err) {
    console.error(err);
    setStatus("capture error: " + err.message);
    $("startBtn").disabled = false;
  }
}

function stop() {
  activeNames().forEach((n) => stopSource(n));
}

// --- transcript rendering ---
function addLine(msg) {
  // Drop placeholder/empty whisper output.
  const text = (msg.text || "").trim();
  if (!text) return;
  // Record for export. Mic and system streams start at different wall-clock
  // moments (the share dialog adds seconds), so anchor each line via its own
  // source's epoch to put both on one timeline.
  const epoch = sourceEpochs[msg.source] || Date.now();
  entries.push({
    id: msg.id,
    source: msg.source,
    text,
    at: epoch + (msg.t0 || 0) * 1000,
    dur: Math.max(0.5, (msg.t1 || 0) - (msg.t0 || 0)),
  });
  $("exportVttBtn").disabled = false;
  $("exportMdBtn").disabled = false;
  const line = document.createElement("div");
  line.className = "line " + (msg.source === "mic" ? "me" : "other");
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = speakerLabel(msg.source);
  const body = document.createElement("span");
  body.className = "body";
  body.textContent = text;
  line.appendChild(who);
  line.appendChild(body);
  if (msg.id !== undefined) lineEls[msg.id] = line;
  transcriptEl.appendChild(line);
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
}

// The server retracts a mic line when it turns out to be speaker echo of a
// system line that transcribed slightly later.
function retractLine(id) {
  const el = lineEls[id];
  if (el) { el.remove(); delete lineEls[id]; }
  const i = entries.findIndex((e) => e.id === id);
  if (i >= 0) entries.splice(i, 1);
}

// --- suggestions ---
function connectSuggestions() {
  if (suggWs && (suggWs.readyState === WebSocket.OPEN || suggWs.readyState === WebSocket.CONNECTING)) return;
  suggWs = new WebSocket(wsUrl("/ws/suggestions", {}));
  suggWs.onopen = () => { $("suggestNowBtn").disabled = false; $("deepDiveBtn").disabled = false; };
  suggWs.onclose = () => {
    $("suggestNowBtn").disabled = true;
    $("deepDiveBtn").disabled = true;
    setSuggStatus("");
    // Auto-reconnect while a capture is active (backend restart mid-meeting).
    if (activeNames().length > 0) setTimeout(connectSuggestions, 3000);
  };
  suggWs.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "suggestions") renderCards(msg);
    else if (msg.type === "retract") retractLine(msg.id);
    else if (msg.type === "suggest_status") {
      if (msg.state === "thinking") setSuggStatus("thinking…", true);
      else if (msg.state === "researching") setSuggStatus("researching the web…", true);
      else if (msg.state === "error") setSuggStatus("error: " + (msg.msg || "suggestion failed"));
      else setSuggStatus(msg.last_ms ? `ready · last run ${(msg.last_ms / 1000).toFixed(1)}s` : "ready");
    }
  };
}

function setSuggStatus(text, pulsing = false) {
  const el = $("suggStatus");
  el.textContent = text;
  el.classList.toggle("pulsing", pulsing);
}

const KIND_LABELS = { talking_point: "SAY", question: "ASK", fact: "FACT", idea: "IDEA" };

function renderCards(msg) {
  const panel = $("cards");
  const ph = panel.querySelector(".placeholder");
  if (ph) ph.remove();
  const group = document.createElement("div");
  group.className = "card-group" + (msg.deep ? " deep" : "");
  if (msg.at) {
    const when = document.createElement("div");
    when.className = "card-time";
    when.textContent = (msg.deep ? "🌐 deep dive · " : "") + msg.at;
    group.appendChild(when);
  }
  for (const c of msg.cards || []) {
    const card = document.createElement("div");
    card.className = `card kind-${c.kind}`;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = KIND_LABELS[c.kind] || "IDEA";
    const title = document.createElement("div");
    title.className = "card-title";
    title.appendChild(badge);
    title.appendChild(document.createTextNode(" " + c.title));
    const detail = document.createElement("div");
    detail.className = "card-detail";
    detail.textContent = c.detail || "";
    card.appendChild(title);
    card.appendChild(detail);
    const pin = document.createElement("button");
    pin.className = "pin-btn";
    pin.title = "Pin this suggestion";
    pin.textContent = "📌";
    pin.onclick = () => pinCard(card, group);
    card.appendChild(pin);
    group.appendChild(card);
  }
  panel.prepend(group);
  // keep the panel bounded
  while (panel.children.length > 12) panel.removeChild(panel.lastChild);
}

// Move a card into the pinned area; it stays until explicitly removed.
function pinCard(card, group) {
  const pinned = $("pinned");
  const ph = pinned.querySelector(".placeholder");
  if (ph) ph.remove();
  const pinBtn = card.querySelector(".pin-btn");
  if (pinBtn) pinBtn.remove();
  const x = document.createElement("button");
  x.className = "pin-btn";
  x.title = "Remove from pinned";
  x.textContent = "✕";
  x.onclick = () => {
    card.remove();
    if (!pinned.querySelector(".card")) {
      const p = document.createElement("div");
      p.className = "placeholder";
      p.textContent = "📌 Pin a suggestion to keep it here until you've used it.";
      pinned.appendChild(p);
    }
  };
  card.appendChild(x);
  card.classList.add("pinned-card");
  pinned.appendChild(card);
  // tidy up the source group if this was its last card
  if (group && !group.querySelector(".card")) group.remove();
}

function suggestNow() {
  if (suggWs && suggWs.readyState === WebSocket.OPEN) {
    suggWs.send(JSON.stringify({ type: "suggest_now" }));
    setSuggStatus("thinking…", true);
  }
}

function deepDive() {
  if (suggWs && suggWs.readyState === WebSocket.OPEN) {
    suggWs.send(JSON.stringify({ type: "deep_dive" }));
    setSuggStatus("researching the web…", true);
  }
}

// --- labels ---
function speakerLabel(source) {
  if (source === "mic") return "You";
  return meetingCfg.party === "multi" ? "Others" : "Them";
}

function vttClock(ms) {
  // VTT cue time: HH:MM:SS.mmm
  const s = Math.max(0, ms) / 1000;
  const h = String(Math.floor(s / 3600)).padStart(2, "0");
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const sec = (s % 60).toFixed(3).padStart(6, "0");
  return `${h}:${m}:${sec}`;
}

function download(filename, mime, content) {
  const blob = new Blob([content], { type: mime });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}

function sortedEntries() {
  return entries.slice().sort((a, b) => a.at - b.at);
}

function exportVtt() {
  const list = sortedEntries();
  if (!list.length) return;
  const zero = list[0].at;
  const cues = list.map((e) => {
    const start = vttClock(e.at - zero);
    const end = vttClock(e.at - zero + e.dur * 1000);
    return `${start} --> ${end}\n<v ${speakerLabel(e.source)}>${e.text}`;
  });
  download(`meeting-${stamp()}.vtt`, "text/vtt", "WEBVTT\n\n" + cues.join("\n\n") + "\n");
}

function exportMd() {
  const list = sortedEntries();
  if (!list.length) return;
  const started = new Date(list[0].at);
  const lines = list.map((e) => {
    const t = new Date(e.at);
    const p = (n) => String(n).padStart(2, "0");
    const clock = `${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
    return `**${speakerLabel(e.source)}** [${clock}]: ${e.text}`;
  });
  const md = `# Meeting transcript — ${started.toLocaleString()}\n\n` + lines.join("\n\n") + "\n";
  download(`meeting-${stamp()}.md`, "text/markdown", md);
}

// --- voice enrollment ---
async function refreshEnrollment() {
  try {
    const r = await fetch("/api/enrollment");
    const s = await r.json();
    enrolled = !!s.enrolled;
    $("enrollStatus").textContent = enrolled
      ? `voice enrolled ✓ (${s.speech_seconds || "?"}s${s.updated_at ? ", " + s.updated_at.slice(0, 10) : ""})`
      : "no voice enrolled";
    $("enrollStatus").classList.toggle("ok", enrolled);
    $("enrollBtn").textContent = enrolled ? "🎙 Re-enroll voice" : "🎙 Enroll voice";
  } catch {
    $("enrollStatus").textContent = "";
  }
}

async function enroll() {
  if (!navigator.mediaDevices) {
    setStatus("enrollment blocked: " + httpsHint());
    return;
  }
  $("enrollBtn").disabled = true;
  let mic = null;
  let node = null;
  let srcNode = null;
  const cleanup = () => {
    try { srcNode && srcNode.disconnect(); } catch {}
    try { node && node.disconnect(); } catch {}
    try { mic && mic.getTracks().forEach((t) => t.stop()); } catch {}
    $("enrollBtn").disabled = false;
  };
  try {
    const ctx = await ensureContext();
    if (ctx.state === "suspended") await ctx.resume();
    mic = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: false, channelCount: 1 },
    });
    const ws = new WebSocket(wsUrl("/ws/enroll", {}));
    ws.binaryType = "arraybuffer";
    ws.onopen = () => setStatus("enrolling — talk naturally (read anything aloud)…");
    ws.onerror = () => { setStatus("enrollment connection error"); cleanup(); };
    ws.onclose = () => cleanup();
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "enroll_progress") {
        setStatus(`enrolling — ${msg.speech_s}s / ${msg.target_s}s of speech captured…`);
      } else if (msg.type === "enroll_done") {
        setStatus(`voice enrolled (${msg.seconds}s) ✓`);
        refreshEnrollment().then(applyModeUi);
        ws.close();
      } else if (msg.type === "enroll_error") {
        setStatus("enrollment failed: " + msg.msg);
        ws.close();
      }
    };
    srcNode = ctx.createMediaStreamSource(mic);
    node = new AudioWorkletNode(ctx, "pcm-worklet");
    node.port.onmessage = (ev) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(ev.data);
    };
    srcNode.connect(node);
  } catch (err) {
    setStatus("enrollment error: " + err.message);
    cleanup();
  }
}

$("startBtn").addEventListener("click", start);
$("stopBtn").addEventListener("click", stop);
$("exportVttBtn").addEventListener("click", exportVtt);
$("exportMdBtn").addEventListener("click", exportMd);
$("suggestNowBtn").addEventListener("click", suggestNow);
$("deepDiveBtn").addEventListener("click", deepDive);
$("enrollBtn").addEventListener("click", enroll);
bindSeg("modeSeg", "mode", applyModeUi);
bindSeg("partySeg", "party");
loadAppConfig();
refreshEnrollment().then(applyModeUi);
setStatus("idle");
