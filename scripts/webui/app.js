"use strict";
// saezuri GUI。Python側（scripts/gui_api.py の Api）を window.pywebview.api 経由で呼ぶ。

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

let state = null;          // get_state() の結果
let files = [];            // まとめ・Excel で使うファイル
let pollTimer = null;
let pollSince = 0;

function ready() {
  return new Promise((resolve) => {
    if (window.pywebview && window.pywebview.api) return resolve();
    window.addEventListener("pywebviewready", () => resolve(), { once: true });
  });
}

async function api(name, ...args) {
  await ready();
  try {
    return await window.pywebview.api[name](...args);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function toast(text, ms = 4200) {
  const el = $("#toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), ms);
}

function fail(res) {
  if (res && !res.ok && res.error) toast(res.error, 6000);
  return !res || !res.ok;
}

// ---- 画面切り替え ----
function show(page) {
  $$(".page").forEach((p) => p.classList.toggle("on", p.id === "page-" + page));
  $$(".nav").forEach((n) => n.classList.toggle("on", n.dataset.page === page));
  try { localStorage.setItem("saezuri.page", page); } catch (e) { /* 保存できなくても動く */ }
}

// ---- 入力 ----
function segValue(seg) {
  const on = $("button.on", seg);
  return on ? on.dataset.value : "";
}

function formParams(form) {
  const params = {};
  $$("input[name], select[name], textarea[name]", form).forEach((el) => {
    params[el.name] = el.type === "checkbox" ? el.checked : el.value;
  });
  $$(".seg[data-name]", form).forEach((seg) => (params[seg.dataset.name] = segValue(seg)));
  return params;
}

function setupSegs() {
  $$(".seg").forEach((seg) => {
    seg.addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b || b.disabled) return;
      $$("button", seg).forEach((x) => x.classList.toggle("on", x === b));
      seg.dispatchEvent(new Event("change"));
    });
  });
  const sns = $('#page-account .seg[data-name="sns"]');
  const kind = $('#page-account .seg[data-name="kind"]');
  const update = () => {
    const ig = segValue(sns) === "instagram";
    const prof = $('button[data-value="profiles"]', kind);
    prof.disabled = ig;
    if (ig && segValue(kind) === "profiles") {
      $$("button", kind).forEach((x) => x.classList.toggle("on", x.dataset.value === "posts"));
    }
    $("#kind-note").textContent = ig ? "Instagramのフォロワー数取得には対応していません" : "";
  };
  sns.addEventListener("change", update);
  update();
}

// ---- 実行と進み具合 ----
function now() {
  const d = new Date();
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function logLine(text, cls = "") {
  const log = $("#log");
  $(".hint", log)?.remove();
  const row = document.createElement("div");
  if (cls) row.className = cls;
  const t = document.createElement("span");
  t.className = "t";
  t.textContent = now();
  row.append(t, document.createTextNode(text));
  log.append(row);
  log.scrollTop = log.scrollHeight;
}

function lineClass(text) {
  if (/^(エラー|ERROR|Traceback)/.test(text)) return "bad";
  if (/^(保存先|上位Markdown|出力|取得|合計|ダウンロード)[:：\s]/.test(text)) return "em";
  return "";
}

function setRunning(running, title = "") {
  $$("button[type=submit], [data-login], #btn-chrome, #btn-csv, #btn-digest, #btn-media").forEach((b) => (b.disabled = running));
  $("#btn-stop").hidden = !running;
  const chip = $("#status-chip");
  chip.className = "chip";
  const prog = $("#prog");
  prog.className = "prog" + (running ? " run" : "");
  if (running) chip.textContent = "実行中：" + title;
}

async function start(task, params) {
  const res = await api("run", task, params);
  if (fail(res)) return;
  $("#result").hidden = true;
  $("#log").innerHTML = "";
  logLine(res.title + " を開始しました");
  pollSince = 0;
  setRunning(true, res.title);
  $("#status").scrollIntoView({ behavior: "smooth", block: "nearest" });
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 500);
}

async function poll() {
  const res = await api("poll", pollSince);
  if (!res.ok || res.idle) return;
  res.lines.forEach((l) => l.trim() && logLine(l, lineClass(l)));
  pollSince = res.next;
  if (!res.done) return;
  clearInterval(pollTimer);
  pollTimer = null;
  finish(res);
}

function finish(res) {
  setRunning(false);
  const ok = res.code === 0;
  const chip = $("#status-chip");
  chip.textContent = res.status;
  chip.className = "chip" + (ok ? "" : res.code === null ? " warn" : " err");
  $("#prog").className = "prog " + (ok ? "done" : "err");
  logLine(res.status, ok ? "em" : "bad");

  const box = $("#result");
  box.innerHTML = "";
  box.className = "result" + (ok ? "" : " err");
  const p = document.createElement("p");
  p.textContent = res.message;
  box.append(p);
  const jsonl = res.outputs.filter((o) => o.endsWith(".jsonl"));
  const other = res.outputs.filter((o) => !o.endsWith(".jsonl"));
  if (other.length) addButton(box, "結果を開く", "btn primary", () => openPath(other[other.length - 1]));
  if (jsonl.length) {
    addButton(box, "Excelで開く", other.length ? "btn" : "btn primary", () => exportCsv(res.last_files));
    addButton(box, "まとめを作る", "btn", () => start("digest", { files: res.last_files, title: "" }));
  }
  if (res.last_files && res.last_files.length) setFiles(res.last_files);
  box.hidden = false;
  refreshLogin();
}

function addButton(parent, text, cls, onClick) {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = text;
  b.addEventListener("click", onClick);
  parent.append(b);
}

async function openPath(path) {
  fail(await api("open_path", path));
}

async function exportCsv(list) {
  const res = await api("export_csv", list, true);
  if (res.cancelled) return;
  if (!fail(res)) toast(`${res.rows}行のCSVを作りました（${baseName(res.path)}）`);
}

// ---- まとめ・Excel ----
function baseName(p) {
  return p.split(/[\\/]/).pop();
}

function setFiles(list) {
  files = list || [];
  const el = $("#file-list");
  el.classList.toggle("empty", !files.length);
  el.textContent = files.length
    ? files.slice(0, 3).map(baseName).join("、") + (files.length > 3 ? ` ほか${files.length - 3}件` : "")
    : "まだ選ばれていません";
  el.title = files.join("\n");
}

// ---- ログイン状況 ----
function renderLogin(list) {
  const box = $("#login-box");
  box.innerHTML = "";
  list.forEach((s) => {
    const row = document.createElement("div");
    row.className = "row";
    const name = document.createElement("span");
    const dot = document.createElement("span");
    dot.className = "dot " + s.state;
    name.append(dot, document.createTextNode(s.label));
    const text = document.createElement("span");
    text.textContent = s.text;
    row.append(name, text);
    box.append(row);
  });
}

async function refreshLogin() {
  const res = await api("get_login_status");
  if (res.ok) renderLogin(res.status);
}

// ---- 設定 ----
function fillSettings(s) {
  const f = $("#settings-form");
  ["x", "instagram", "threads", "tiktok"].forEach((k) => (f.elements["acc-" + k].value = (s.accounts[k] || []).join("\n")));
  f.elements.keywords.value = (s.keywords || []).join("\n");
  f.elements.use_cdp.checked = !!s.use_cdp;
  const sel = f.elements.browser;
  sel.innerHTML = "";
  s.browser_choices.forEach((c) => {
    const o = document.createElement("option");
    o.value = c.value;
    o.textContent = c.label;
    sel.append(o);
  });
  sel.value = s.browser_choices.some((c) => c.value === s.browser) ? s.browser : "chromium";
}

async function loadState() {
  const res = await api("get_state");
  if (fail(res)) return;
  state = res;
  if (!res.config_found) toast("config.yaml が見つかりません。saezuriのフォルダから起動してください。", 8000);
  renderLogin(res.login);
  fillSettings(res);
  if (res.last_files.length) setFiles(res.last_files);
}

// ---- 起動 ----
function bind() {
  $$(".nav").forEach((n) => n.addEventListener("click", () => show(n.dataset.page)));
  setupSegs();

  $$("form[data-task]").forEach((form) =>
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      start(form.dataset.task, formParams(form));
    })
  );
  $$("[data-login]").forEach((b) => b.addEventListener("click", () => start("login", { sns: b.dataset.login })));
  $("#btn-chrome").addEventListener("click", async () => {
    const res = await api("launch_chrome");
    if (fail(res)) return;
    toast(res.enabled_cdp
      ? "ログイン用Chromeを開きました。X・TikTokの収集でこのChromeを使う設定にしました。"
      : "ログイン用Chromeを開きました。ログインしたら、開いたままにしてください。");
    setTimeout(refreshLogin, 2500);
    if (res.enabled_cdp) loadState();
  });
  $("#btn-stop").addEventListener("click", async () => {
    if (!confirm("実行中の処理を中止しますか？\n途中までの収集分は保存されないことがあります。")) return;
    fail(await api("stop"));
  });
  $("#btn-choose").addEventListener("click", async () => {
    const res = await api("choose_files");
    if (!fail(res) && res.files.length) setFiles(res.files);
  });
  $("#btn-csv").addEventListener("click", () => (files.length ? exportCsv(files) : toast("ファイルを選んでください。")));
  $("#btn-digest").addEventListener("click", () => start("digest", { files, title: $("#digest-title").value }));
  $("#btn-media").addEventListener("click", () => start("media", { files }));
  $$("[data-folder]").forEach((b) => b.addEventListener("click", async () => fail(await api("open_folder", b.dataset.folder))));
  $("#login-box").addEventListener("click", refreshLogin);
  $("#btn-reload").addEventListener("click", () => loadState().then(() => toast("保存済みの内容に戻しました。")));
  $("#settings-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const accounts = {};
    ["x", "instagram", "threads", "tiktok"].forEach((k) => (accounts[k] = f.elements["acc-" + k].value));
    const res = await api("save_settings", {
      accounts,
      keywords: f.elements.keywords.value,
      use_cdp: f.elements.use_cdp.checked,
      browser: f.elements.browser.value,
    });
    if (fail(res)) return;
    state = res.state;
    fillSettings(res.state);
    renderLogin(res.state.login);
    toast("設定を保存しました。");
  });

  let page = "setup";
  try { page = localStorage.getItem("saezuri.page") || page; } catch (e) { /* 既定のまま */ }
  show($("#page-" + page) ? page : "setup");
  loadState();
  setInterval(() => { if (!pollTimer) refreshLogin(); }, 15000);
}

document.addEventListener("DOMContentLoaded", bind);
