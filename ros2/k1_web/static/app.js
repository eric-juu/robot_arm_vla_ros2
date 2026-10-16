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
document.addEventListener("keydown", (e) => {
  // the page has no text fields, so these keys always mean STOP (even with a slider focused)
  if (e.key === "Escape" || e.key === " ") { e.preventDefault(); stop(); }
});

$("save-pose").addEventListener("click", () => {
  const name = prompt("Pose name (letters, digits, _ or -):");
  if (name) send({type: "save_pose", name});
});

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
  ws.onopen = () => { reconnectDelay = 500; setBadge("conn", "connected", "on"); };
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
  else if (msg.type === "error") showMessage(msg.message, true);
  else if (msg.type === "enable_result") showMessage(msg.message, !msg.success);
  else if (msg.type === "info") showMessage(msg.message, false);
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
