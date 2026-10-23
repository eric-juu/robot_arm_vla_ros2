// K1 web client. Talks to the k1_web node over a WebSocket; the server owns all safety logic
// (limits, smoothing, deadman), this page only sends goals.

const JOINT_LABELS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_roll", "wrist_flex", "gripper_roll"];
const SEND_INTERVAL_MS = 50;      // throttle for slider updates
const PING_INTERVAL_MS = 250;     // keeps the server's deadman alive
const USER_EDIT_HOLD_MS = 800;    // don't overwrite a slider the user just moved

const $ = (id) => document.getElementById(id);
let ws = null;
let state = null;
let reconnectDelay = 500;
let lastUserEdit = 0;
let posesKey = "";
let pendingGoal = false;
let pendingGripper = false;

// ---- joint sliders ----

const sliders = JOINT_LABELS.map((name, i) => {
  const row = document.createElement("div");
  row.className = "joint";
  row.innerHTML = `<label for="j${i}">${name}</label>
    <input id="j${i}" type="range" step="0.005" min="-1" max="1" value="0" disabled>
    <output id="j${i}-value">–</output>`;
  $("joints").appendChild(row);
  const input = row.querySelector("input");
  input.addEventListener("input", () => { lastUserEdit = Date.now(); pendingGoal = true; });
  return input;
});

$("gripper").addEventListener("input", () => { lastUserEdit = Date.now(); pendingGripper = true; });

setInterval(() => {
  if (pendingGoal) {
    pendingGoal = false;
    send({type: "goal", positions: sliders.map((s) => parseFloat(s.value))});
  }
  if (pendingGripper) {
    pendingGripper = false;
    send({type: "gripper", value: parseFloat($("gripper").value)});
  }
}, SEND_INTERVAL_MS);

// ---- buttons ----

$("enable").addEventListener("click", () => {
  if (!state) return;
  if (state.enabled) {
    if (!confirm("Release the motors? The arm will go limp and fall. Support it first.")) return;
    send({type: "enable", value: false});
  } else {
    send({type: "enable", value: true});
  }
});

const stop = () => { pendingGoal = false; lastUserEdit = 0; send({type: "stop"}); };
$("stop").addEventListener("click", stop);
const isTyping = (el) => el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && el.type === "text"));
document.addEventListener("keydown", (e) => {
  // Esc always means STOP; Space too, except while typing in the prompt or an answer
  if (e.key === "Escape" || (e.key === " " && !isTyping(e.target))) { e.preventDefault(); stop(); }
});

$("save-pose").addEventListener("click", () => {
  const name = prompt("Pose name (letters, digits, _ or -):");
  if (name) send({type: "save_pose", name});
});

// ---- agent: prompts and questions ----

$("prompt-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("prompt").value.trim();
  if (!text) return;
  send({type: "agent_prompt", text, mode: $("prompt-mode").value, id: "prompt"});
});
$("prompt").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("prompt-form").requestSubmit(); }
});

let question = null;    // the agent's open question: {qid, text, kind}

function showQuestion(q) {
  question = q;
  $("agent-question").hidden = !q;
  if (!q) return;
  $("agent-question-text").textContent = q.text;
  $("agent-answer-form").hidden = q.kind !== "ask";
  $("agent-confirm").hidden = q.kind !== "confirm";
  $("agent-reset").hidden = q.kind !== "reset";
  $("agent-answer").value = "";
  if (q.kind === "ask") $("agent-answer").focus();
}

function answer(text) {
  if (question) send({type: "agent_answer", qid: question.qid, text});
  showQuestion(null);
}
$("agent-answer-form").addEventListener("submit", (e) => { e.preventDefault(); answer($("agent-answer").value); });
$("agent-yes").addEventListener("click", () => answer("yes"));
$("agent-no").addEventListener("click", () => answer("no"));
$("agent-ready").addEventListener("click", () => answer("ready"));

$("logout").addEventListener("click", async () => {
  await fetch("/logout", {method: "POST"});
  location.reload();
});

// ---- connection ----

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function showMessage(text, isError) {
  $("message").textContent = text;
  $("message").className = isError ? "error" : "muted";
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => {
    reconnectDelay = 500;
    setBadge("conn", "connected", "on");
    send({type: "hello", role: "human"});
  };
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = (e) => {
    setBadge("conn", "disconnected", "off");
    state = null;
    render();
    if (e.code === 1008) { location.reload(); return; }   // session no longer valid
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 5000);
  };
}
setInterval(() => send({type: "ping"}), PING_INTERVAL_MS);

function handle(msg) {
  if (msg.type === "state") { state = msg; render(); }
  else if (msg.id === "prompt") {         // reply to "Send to agent"
    if (msg.type === "error") logLine(`not sent: ${msg.message}`);
    else $("prompt").value = "";
  }
  else if (msg.type === "agent_question") { showQuestion(msg); logLine(`agent asks: ${msg.text}`); }
  else if (msg.type === "agent_answer") {
    if (question && question.qid === msg.qid) showQuestion(null);     // answered here or on another page
    logLine(`answer: ${msg.text || "(empty)"}`);
  }
  else if (msg.type === "error") showMessage(msg.message, true);
  else if (msg.type === "enable_result") showMessage(msg.message, !msg.success);
  else if (msg.type === "info") showMessage(msg.message, false);
  else if (msg.type === "agent_note") logLine(msg.text);
  else if (msg.type === "event" && msg.name === "stop") logLine(`STOP pressed (${msg.by})`);
}

const MAX_LOG_LINES = 200;
let logEmpty = true;

function logLine(text) {
  const log = $("agent-log");
  if (logEmpty) { log.replaceChildren(); logEmpty = false; }
  const li = document.createElement("li");
  li.textContent = `${new Date().toLocaleTimeString()}  ${text}`;
  log.prepend(li);
  while (log.children.length > MAX_LOG_LINES) log.lastChild.remove();
}

// ---- rendering ----

function setBadge(id, text, cls) {
  $(id).textContent = text;
  $(id).className = `badge ${cls}`;
}

function render() {
  const ready = !!(state && state.ready);
  if (!state) setBadge("arm", "no state", "off");
  else if (state.stale) setBadge("arm", "arm silent", "warn");
  else setBadge("arm", state.enabled ? (ready ? "enabled" : "enabling…") : "motors off", state.enabled ? "on" : "off");

  $("agent-banner").hidden = !(state && state.clients && state.clients.agent > 0);

  const agent = state ? state.agent : null;
  const idle = !!(agent && agent.state === "idle");
  $("agent-status").textContent = !agent ? "· no agent service (start k1-agent serve on the PC)"
    : `· ${agent.provider}:${agent.model} · ${agent.state === "running" ? `running: ${agent.task}` : "ready"}` +
      (agent.allow_motion === false ? " · dry run (no motion)" : "");
  $("prompt").disabled = !agent;
  $("prompt-send").disabled = !idle;
  if (!agent && question) showQuestion(null);

  $("enable").textContent = state && state.enabled ? "Disable motors" : "Enable motors";
  $("enable").disabled = !state;
  $("stop").disabled = !ready;
  $("save-pose").disabled = !state || state.stale;
  $("gripper").disabled = !ready;

  const editing = Date.now() - lastUserEdit < USER_EDIT_HOLD_MS;
  sliders.forEach((s, i) => {
    s.disabled = !ready;
    if (state) { s.min = state.limits_min[i]; s.max = state.limits_max[i]; }
    // follow the server's goal (pose buttons, STOP, deadman) unless the user is dragging
    if (ready && state.goal && !editing) s.value = state.goal[i];
    const measured = state && state.positions ? state.positions[i] : null;
    $(`j${i}-value`).textContent = measured === null ? "–" : measured.toFixed(3);
  });
  if (state && state.gripper !== null && !editing) $("gripper").value = state.gripper;
  $("gripper-value").textContent = state && state.gripper !== null ? state.gripper.toFixed(2) : "–";

  const names = state ? state.poses : [];
  if (names.join(",") !== posesKey) {
    posesKey = names.join(",");
    $("poses").replaceChildren(...names.map((name) => {
      const b = document.createElement("button");
      b.textContent = name;
      b.addEventListener("click", () => send({type: "pose", name}));
      return b;
    }));
  }
  $("poses").querySelectorAll("button").forEach((b) => { b.disabled = !ready; });
}

// ---- camera ----

function startVideo() {
  const img = $("video");
  img.onload = () => { $("video-status").hidden = true; };
  img.onerror = () => {
    $("video-status").hidden = false;
    $("video-status").textContent = "camera unavailable, retrying…";
    setTimeout(() => { img.src = `/video.mjpeg?t=${Date.now()}`; }, 2000);
  };
  img.src = "/video.mjpeg";
}

render();
connect();
startVideo();
