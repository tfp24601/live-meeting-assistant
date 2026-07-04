// Settings screen: runtime settings (grouped form driven by /api/settings),
// knowledge sources CRUD, and an ingest trigger with live log.

const $ = (id) => document.getElementById(id);
const dirty = {}; // env name -> new value

// Curated placement; anything not listed lands under Advanced.
const GROUPS = [
  ["Profile", ["USER_NAME", "USER_CONTEXT", "LMA_PUBLIC_URL"]],
  ["LLM provider", ["LLM_PROVIDER",
    "SUGGEST_MODEL", "SUGGEST_EFFORT", "DEEP_MODEL", "DEEP_EFFORT",
    "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
    "OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL",
    "CUSTOM_LLM_CMD"]],
  ["Suggestions", ["SUGGEST_MIN_INTERVAL_S", "SUGGEST_MIN_NEW_CHARS"]],
  ["Knowledge retrieval", ["RAG_ENABLED", "RAG_TOP_K", "RAG_MIN_SCORE", "QDRANT_URL"]],
  ["Speaker & echo", ["SPEAKER_THRESHOLD", "ECHO_SUPPRESS", "ECHO_SIMILARITY"]],
];

const PROVIDER_FIELDS = {
  "claude-cli": ["SUGGEST_MODEL", "SUGGEST_EFFORT", "DEEP_MODEL", "DEEP_EFFORT"],
  "anthropic-api": ["ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"],
  "openai-compatible": ["OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL"],
  "custom-command": ["CUSTOM_LLM_CMD"],
};
const ALL_PROVIDER_FIELDS = [...new Set(Object.values(PROVIDER_FIELDS).flat())];

let fields = [];
let claudeCliAvailable = false;

function fieldInput(f) {
  let input;
  if (f.type === "bool") {
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!f.value;
    input.addEventListener("change", () => markDirty(f.env, input.checked));
  } else {
    input = document.createElement("input");
    input.type = f.secret ? "password" : "text";
    if (f.secret && f.value === "__SET__") {
      input.value = "__SET__";
      input.addEventListener("focus", () => { if (input.value === "__SET__") input.value = ""; });
    } else {
      input.value = f.value ?? "";
    }
    input.placeholder = String(f.default ?? "");
    input.addEventListener("input", () => markDirty(f.env, input.value));
  }
  input.id = "f_" + f.env;
  return input;
}

function renderField(f) {
  const label = document.createElement("label");
  label.dataset.env = f.env;
  const caption = document.createElement("span");
  caption.textContent = f.env.toLowerCase().replaceAll("_", " ");
  if (f.restart) {
    const badge = document.createElement("em");
    badge.className = "restart-badge";
    badge.title = "takes effect after a service restart";
    badge.textContent = "restart";
    caption.appendChild(badge);
  }
  label.appendChild(caption);

  if (f.env === "LLM_PROVIDER") {
    const sel = document.createElement("select");
    for (const p of Object.keys(PROVIDER_FIELDS)) {
      const o = document.createElement("option");
      o.value = p;
      o.textContent = p + (p === "claude-cli" && !claudeCliAvailable ? " (binary not found!)" : "");
      if (p === f.value) o.selected = true;
      sel.appendChild(o);
    }
    sel.id = "f_" + f.env;
    sel.addEventListener("change", () => { markDirty(f.env, sel.value); applyProviderVisibility(sel.value); });
    label.appendChild(sel);
  } else {
    label.appendChild(fieldInput(f));
  }
  return label;
}

function applyProviderVisibility(provider) {
  const visible = new Set(PROVIDER_FIELDS[provider] || []);
  for (const env of ALL_PROVIDER_FIELDS) {
    const el = document.querySelector(`label[data-env="${env}"]`);
    if (el) el.hidden = !visible.has(env);
  }
}

function markDirty(env, value) {
  dirty[env] = value;
  $("saveBtn").disabled = false;
  $("saveStatus").textContent = Object.keys(dirty).length + " unsaved change(s)";
}

async function loadSettings() {
  const data = await (await fetch("/api/settings")).json();
  fields = data.fields;
  claudeCliAvailable = data.claude_cli_available;
  const byEnv = Object.fromEntries(fields.map((f) => [f.env, f]));
  const placed = new Set();
  const mount = $("groupsMount");
  mount.innerHTML = "";
  for (const [title, envs] of GROUPS) {
    const h = document.createElement("h3");
    h.textContent = title;
    const grid = document.createElement("div");
    grid.className = "form-grid";
    for (const env of envs) {
      if (!byEnv[env]) continue;
      grid.appendChild(renderField(byEnv[env]));
      placed.add(env);
    }
    mount.appendChild(h);
    mount.appendChild(grid);
  }
  const adv = $("advancedMount");
  adv.innerHTML = "";
  const grid = document.createElement("div");
  grid.className = "form-grid";
  for (const f of fields) if (!placed.has(f.env)) grid.appendChild(renderField(f));
  adv.appendChild(grid);
  applyProviderVisibility(byEnv["LLM_PROVIDER"]?.value || "claude-cli");
}

async function save() {
  $("saveBtn").disabled = true;
  const r = await fetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(dirty),
  });
  if (!r.ok) {
    $("saveStatus").textContent = "save failed: " + (await r.text()).slice(0, 120);
    $("saveBtn").disabled = false;
    return;
  }
  const res = await r.json();
  Object.keys(dirty).forEach((k) => delete dirty[k]);
  $("saveStatus").textContent = "saved ✓";
  const banner = $("restartBanner");
  if (res.restart_needed && res.restart_needed.length) {
    banner.hidden = false;
    banner.textContent = "Restart the service to apply: " + res.restart_needed.join(", ") +
      "  (on the host: systemctl --user restart lma)";
  }
  loadSettings();
}

// ---- sources ----
async function loadSources() {
  const data = await (await fetch("/api/sources")).json();
  const tbody = $("sourcesTable").querySelector("tbody");
  tbody.innerHTML = "";
  for (const s of data.sources) {
    const tr = document.createElement("tr");
    const onTd = document.createElement("td");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = s.enabled !== false;
    cb.title = "uncheck to ignore this source in suggestions";
    cb.addEventListener("change", async () => {
      await fetch(`/api/sources/${encodeURIComponent(s.id)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: cb.checked }),
      });
      loadSources();
    });
    onTd.appendChild(cb);
    const del = document.createElement("button");
    del.className = "ghost";
    del.textContent = "✕";
    del.title = "remove source (indexed data is pruned on next ingest)";
    del.addEventListener("click", async () => {
      if (!confirm(`Remove source "${s.label}"?`)) return;
      await fetch(`/api/sources/${encodeURIComponent(s.id)}`, { method: "DELETE" });
      loadSources();
    });
    const re = document.createElement("button");
    re.className = "ghost";
    re.textContent = "⟳";
    re.title = "re-ingest just this source (on this host)";
    re.addEventListener("click", () => runIngest(s.id));
    for (const [i, cell] of [onTd, s.label, s.type, s.url || s.path || "", re, del].entries()) {
      const td = document.createElement("td");
      if (cell instanceof HTMLElement) td.appendChild(cell);
      else td.textContent = cell;
      if (i === 3) td.className = "mono";
      tr.appendChild(td);
    }
    if (s.enabled === false) tr.className = "disabled-row";
    tbody.appendChild(tr);
  }
}

$("srcType").addEventListener("change", () => {
  const isSite = $("srcType").value === "ghost-site";
  $("srcUrlWrap").hidden = !isSite;
  $("srcPathWrap").hidden = isSite;
});

$("srcAddBtn").addEventListener("click", async () => {
  const entry = {
    id: $("srcId").value.trim(),
    type: $("srcType").value,
    label: $("srcLabel").value.trim(),
    url: $("srcUrl").value.trim(),
    path: $("srcPath").value.trim(),
  };
  const r = await fetch("/api/sources", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(entry),
  });
  if (!r.ok) { alert("add failed: " + (await r.text()).slice(0, 200)); return; }
  ["srcId", "srcLabel", "srcUrl", "srcPath"].forEach((id) => { $(id).value = ""; });
  loadSources();
});

// ---- ingest ----
let ingestPoll = null;

async function pollIngest() {
  const data = await (await fetch("/api/ingest")).json();
  const logEl = $("ingestLog");
  if (data.log.length) {
    logEl.hidden = false;
    logEl.textContent = data.log.join("\n");
    logEl.scrollTop = logEl.scrollHeight;
  }
  if (data.running) {
    $("ingestState").textContent = "running…";
    $("ingestBtn").disabled = true;
  } else {
    $("ingestState").textContent = data.rc === null ? "" : (data.rc === 0 ? "done ✓" : `failed (rc=${data.rc})`);
    $("ingestBtn").disabled = false;
    if (ingestPoll) { clearInterval(ingestPoll); ingestPoll = null; }
  }
}

async function runIngest(sourceId) {
  const r = await fetch("/api/ingest", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sourceId ? { source: sourceId } : {}),
  });
  if (!r.ok) { alert("ingest: " + (await r.text()).slice(0, 200)); return; }
  if (!ingestPoll) ingestPoll = setInterval(pollIngest, 2000);
  pollIngest();
}

$("ingestBtn").addEventListener("click", () => runIngest());
$("saveBtn").addEventListener("click", save);

loadSettings();
loadSources();
pollIngest();
