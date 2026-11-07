/* MusicHub 前端逻辑: 触控优先(Pointer Events), 多源统一播放列表 */
"use strict";

// ---------------- 全局状态 ----------------
const state = {
  source: "netease",
  view: { type: "home" },          // home | search | playlist | mylists
  queue: [],                        // 播放列表(统一 song 结构)
  index: -1,                        // 当前播放下标
  mode: "loop",                     // loop 列表循环 | order 顺序 | single 单曲 | shuffle 随机
  playing: false,
  loggedIn: false,
  feedMode: false,                  // B站推荐/相关流: 使用独立的当前视频列表, 不占用播放列表
  feedQueue: [],                    // 当前视频列表: 仅推荐视频播放上下文, 不持久化, 退出即清空
  plView: "feed",                   // 推荐流时右栏查看的列表: feed=当前视频列表, main=默认播放列表
  mainIndex: -1,                    // 进入推荐流前播放列表的当前下标(供持久化用)
};

const $ = (id) => document.getElementById(id);
// 本地 API 访问令牌: 服务端注入到 index.html 的 <meta name="mh-token">
const MH_TOKEN = (document.querySelector('meta[name="mh-token"]') || {}).content || "";
// fetch 请求统一带令牌头(仅同源相对路径)
const _rawFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  if (typeof input === "string" && input.startsWith("/")) {
    init = Object.assign({}, init);
    init.headers = Object.assign({}, init.headers, { "X-MH-Token": MH_TOKEN });
  }
  return _rawFetch(input, init);
};
// <img>/<audio>/<video> 无法带请求头, 令牌放在查询参数 t
function withTok(url) {
  return url + (url.indexOf("?") === -1 ? "?" : "&") + "t=" + encodeURIComponent(MH_TOKEN);
}
const audio = $("audio");
// Windows 媒体风格播放模式图标
const _st = 'fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"';
const MODES = {
  loop:    { icon: '<svg viewBox="0 0 48 48"><g ' + _st + '><path d="M12 20v-2a6 6 0 0 1 6-6h18"/><path d="M30 6l6 6-6 6"/><path d="M36 28v2a6 6 0 0 1-6 6H12"/><path d="M18 42l-6-6 6-6"/></g></svg>', name: "列表循环" },
  single:  { icon: '<svg viewBox="0 0 48 48"><g ' + _st + '><path d="M12 20v-2a6 6 0 0 1 6-6h18"/><path d="M30 6l6 6-6 6"/><path d="M36 28v2a6 6 0 0 1-6 6H12"/><path d="M18 42l-6-6 6-6"/></g><text x="24" y="30" font-size="16" font-weight="600" text-anchor="middle" fill="currentColor">1</text></svg>', name: "单曲循环" },
  order:   { icon: '<svg viewBox="0 0 48 48"><g ' + _st + '><path d="M8 17h24"/><path d="M25 9l9 8-9 8"/><path d="M8 33h24"/><path d="M25 25l9 8-9 8"/></g></svg>', name: "顺序播放" },
  shuffle: { icon: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M10.59 9.17 5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 8.04 20 10V4h-5.5zm.33 9.41-1.41 1.41 3.13 3.13L14.5 20H20v-5.5l-2.04 2.04-3.13-3.13z"/></svg>', name: "随机播放" },
};

// ---------------- 工具 ----------------
function fmt(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ":" + String(s).padStart(2, "0");
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function proxyUrl(s) {
  if (s.source === "local")
    return withTok("/file?name=" + encodeURIComponent(s.file || s.id || ""));
  return withTok("/proxy?source=" + encodeURIComponent(s.source) +
         "&id=" + encodeURIComponent(s.id) +
         "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {})));
}
let toastTimer = null;
function toast(msg, ms) {
  let t = $("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    t.style.cssText = "position:fixed;bottom:110px;left:50%;transform:translateX(-50%);" +
      "background:rgba(0,0,0,.85);color:#fff;padding:10px 22px;border-radius:20px;" +
      "font-size:14px;z-index:300;pointer-events:none;transition:opacity .3s;max-width:70vw;";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  // 全屏时 toast 需挂在全屏元素内才可见
  const fsEl = document.fullscreenElement;
  if (fsEl && t.parentElement !== fsEl) fsEl.appendChild(t);
  else if (!fsEl && t.parentElement !== document.body) document.body.appendChild(t);
  t.style.opacity = "1";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.style.opacity = "0"; }, ms || 2200);
}
// 应用内确认弹窗(替代原生 confirm, 深色主题, 支持全屏)
function showConfirm(msg, okText) {
  return new Promise((resolve) => {
    const old = document.getElementById("cfm-modal");
    if (old) old.remove();
    const m = document.createElement("div");
    m.id = "cfm-modal";
    m.className = "cfm-mask";
    m.innerHTML =
      '<div class="cfm-box"><div class="cfm-msg">' + esc(msg) + '</div>' +
      '<div class="cfm-actions"><button class="cfm-btn ghost" id="cfm-no">取消</button>' +
      '<button class="cfm-btn danger" id="cfm-yes">' + esc(okText || "确定") + '</button></div></div>';
    const host = document.fullscreenElement || document.body;
    host.appendChild(m);
    const done = (v) => { m.remove(); resolve(v); };
    m.addEventListener("click", (e) => {
      if (e.target === m) done(false);
      else if (e.target.id === "cfm-yes" || e.target.closest("#cfm-yes")) done(true);
      else if (e.target.id === "cfm-no" || e.target.closest("#cfm-no")) done(false);
    });
  });
}
async function api(path) {
  const r = await fetch(path);
  return r.json();
}
const SRC_NAME = { netease: "网易", qq: "QQ", migu: "咪咕", bilibili: "B站", local: "已下载" };
// 各音源品牌 logo(官方图标 / 内联 SVG, 无外网依赖)
const SRC_LOGO = {
  netease: '<img src="/web/img/netease.png" alt="网易云音乐">',
  qq: '<img src="/web/img/qq.png" alt="QQ音乐">',
  migu: '<img src="/web/img/migu.png" alt="咪咕音乐">',
  bilibili: '<img src="/web/img/bilibili.png" alt="哔哩哔哩">',
};

// 队列持久化(防抖)
// 启动恢复保护: 用户修改过队列/设置后, 迟到的 /api/queue、/api/settings 响应不得覆盖
let userTouchedQueue = false;
let userTouchedSettings = false;
let bootSettings = null;
let saveTimer = null;
let setTimer = null;
// 待保存状态跟踪: 保存最近一次尚未发出的 body; flush 后清空避免重复发送, 新修改重新置位
let pendingQueueBody = null;
let pendingSettingsBody = null;
// 队列请求体(契约 B): {songs, index}; feedMode 下 index 取 state.mainIndex, 空队列取 -1
function queueBody() {
  return JSON.stringify({ songs: state.queue, index: state.feedMode ? state.mainIndex : state.index });
}
// 统一发送: 非 2xx 或 reject 都经 toast 可见提示(含状态码/错误类型), 不抛异常/不重排定时器(无重试风暴)
// keepalive=true 用于退出/切后台 flush; 每源约 64KB 预算: 超阈值改用普通 fetch 并可见提示, 不静默截断
function doSave(path, bodyStr, label, keepalive) {
  const init = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: bodyStr,
  };
  if (keepalive) {
    let bytes = bodyStr.length;
    try { bytes = new TextEncoder().encode(bodyStr).length; } catch (e) {}
    if (bytes > 60000) toast(label + "较大, 改用后台保存", 2600);
    else init.keepalive = true;
  }
  fetch(path, init)
    .then((r) => { if (!r.ok) toast(label + "保存失败(" + r.status + ")", 2600); })
    .catch(() => { toast(label + "保存失败(网络错误)", 2600); });
}
// 退出/切后台前统一 flush: 绕过防抖等待, 立即发出尚未发送的队列/设置保存, 然后清待保存标志
function flushPendingSaves() {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  if (setTimer) { clearTimeout(setTimer); setTimer = null; }
  if (pendingQueueBody !== null) {
    const b = pendingQueueBody; pendingQueueBody = null;
    doSave("/api/queue", b, "队列", true);
  }
  if (pendingSettingsBody !== null) {
    const b = pendingSettingsBody; pendingSettingsBody = null;
    doSave("/api/settings", b, "设置", true);
  }
}
function saveQueue() {
  userTouchedQueue = true;
  pendingQueueBody = queueBody();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    if (pendingQueueBody === null) return;
    const b = pendingQueueBody; pendingQueueBody = null;
    doSave("/api/queue", b, "队列", false);
  }, 400);
}

// 设置持久化(音量 + 播放进度 + 代理 + 字体), 保存到本地 data/settings.json
const DEFAULT_PROXY = { mode: "system", http: "", https: "" };
const DEFAULT_FONT = {
  ui_family: "Microsoft YaHei", ui_scale: 1,
  lyrics_family: "Microsoft YaHei UI", lyrics_size: 28,
};
let settings = {
  volume: 0.8, pos: null, rate: 1,
  proxy: Object.assign({}, DEFAULT_PROXY),
  font: Object.assign({}, DEFAULT_FONT),
};  // pos: {key, time}
function saveSettings() {
  userTouchedSettings = true;
  pendingSettingsBody = JSON.stringify(settingSnapshot());
  clearTimeout(setTimer);
  setTimer = setTimeout(() => {
    setTimer = null;
    if (pendingSettingsBody === null) return;
    const b = pendingSettingsBody; pendingSettingsBody = null;
    doSave("/api/settings", b, "设置", false);
  }, 600);
}
let lastPosSave = 0;
function settingSnapshot() {
  return Object.assign({}, bootSettings || {}, settings);
}
function savePos() {
  if (audio.duration && audio.currentTime > 3 && audio.dataset.key) {
    settings.pos = { key: audio.dataset.key, time: audio.currentTime };
  } else {
    settings.pos = null;
  }
  saveSettings();
}

function cssFontFamily(name) {
  // 字体名含空格时加引号; 服务端已禁止引号/分号等危险字符
  return /^[A-Za-z0-9_-]+$/.test(name) ? name : '"' + name + '"';
}
function applyFontSettings() {
  const f = settings.font || DEFAULT_FONT;
  document.documentElement.style.setProperty("--font-family", cssFontFamily(f.ui_family));
  document.documentElement.style.setProperty("--font-scale", String(f.ui_scale));
}
const FONT_FAMILIES = [
  "Microsoft YaHei", "Microsoft YaHei UI", "SimHei", "SimSun", "KaiTi",
  "DengXian", "PingFang SC", "Segoe UI", "Arial", "Consolas",
];
function fontOptions(families, select, current) {
  const names = Array.isArray(families) && families.length ? families.slice() : FONT_FAMILIES.slice();
  if (current && !names.includes(current)) names.push(current);
  select.innerHTML = names.map((n) => '<option value="' + esc(n) + '">' + esc(n) + "</option>").join("");
  select.value = current;
}
function collectSettingsForm() {
  return {
    proxy: {
      mode: $("set-proxy-mode").value,
      http: $("set-proxy-http").value.trim(),
      https: $("set-proxy-https").value.trim(),
    },
    font: {
      ui_family: $("set-ui-family").value,
      ui_scale: Number($("set-ui-scale").value),
      lyrics_family: $("set-lyrics-family").value,
      lyrics_size: Number($("set-lyrics-size").value),
    },
  };
}
function renderSettingsForm(override) {
  const o = override || {};
  const p = Object.assign({}, DEFAULT_PROXY, settings.proxy || {}, o.proxy || {});
  const f = Object.assign({}, DEFAULT_FONT, settings.font || {}, o.font || {});
  $("set-proxy-mode").value = p.mode;
  $("set-proxy-http").value = p.http || "";
  $("set-proxy-https").value = p.https || "";
  $("set-ui-family").value = f.ui_family;
  $("set-ui-scale").value = String(f.ui_scale);
  $("set-ui-scale-val").textContent = Math.round(f.ui_scale * 100) + "%";
  $("set-lyrics-family").value = f.lyrics_family;
  $("set-lyrics-size").value = String(f.lyrics_size);
  $("set-lyrics-size-val").textContent = String(f.lyrics_size);
  $("set-preview").style.fontFamily = cssFontFamily(f.ui_family);
  $("set-msg").textContent = "";
  syncProxyInputs();
}
function syncProxyInputs() {
  const on = $("set-proxy-mode").value === "custom";
  $("set-proxy-http").disabled = !on;
  $("set-proxy-https").disabled = !on;
}
function renderSettingsFormDefault() {
  $("set-proxy-mode").value = DEFAULT_PROXY.mode;
  $("set-proxy-http").value = DEFAULT_PROXY.http;
  $("set-proxy-https").value = DEFAULT_PROXY.https;
  $("set-ui-family").value = DEFAULT_FONT.ui_family;
  $("set-ui-scale").value = String(DEFAULT_FONT.ui_scale);
  $("set-lyrics-family").value = DEFAULT_FONT.lyrics_family;
  $("set-lyrics-size").value = String(DEFAULT_FONT.lyrics_size);
  $("set-ui-scale-val").textContent = "100%";
  $("set-lyrics-size-val").textContent = String(DEFAULT_FONT.lyrics_size);
  $("set-preview").style.fontFamily = cssFontFamily(DEFAULT_FONT.ui_family);
  document.documentElement.style.setProperty("--font-scale", "1");
  $("set-msg").textContent = "已恢复默认，点保存后生效";
  syncProxyInputs();
}
async function openSettings() {
  const nativeFont = {};
  const apiObj = desktopApi();
  if (apiObj && typeof apiObj.desktop_lyrics_state === "function") {
    try {
      const st = await apiObj.desktop_lyrics_state();
      if (st && st.available !== false) {
        if (st.font_family) nativeFont.lyrics_family = st.font_family;
        if (typeof st.font_size === "number") nativeFont.lyrics_size = st.font_size;
        fontOptions(FONT_FAMILIES, $("set-lyrics-family"),
                    nativeFont.lyrics_family || settings.font.lyrics_family);
      }
    } catch (e) {}
  }
  try {
    const r = await api("/api/fonts");
    fontOptions(r.families, $("set-ui-family"), (settings.font || DEFAULT_FONT).ui_family);
    if (!nativeFont.lyrics_family) {
      fontOptions(r.families, $("set-lyrics-family"), (settings.font || DEFAULT_FONT).lyrics_family);
    }
  } catch (e) {
    fontOptions([], $("set-ui-family"), (settings.font || DEFAULT_FONT).ui_family);
    if (!nativeFont.lyrics_family) {
      fontOptions([], $("set-lyrics-family"), (settings.font || DEFAULT_FONT).lyrics_family);
    }
  }
  renderSettingsForm({ font: nativeFont });
  $("settings-modal").classList.remove("hidden");
}
function closeSettings() {
  applyFontSettings();  // 撤销未保存的实时字号预览
  $("settings-modal").classList.add("hidden");
}
async function saveSettingsForm() {
  const draft = collectSettingsForm();
  const body = JSON.stringify(Object.assign({}, settingSnapshot(), draft));
  if (setTimer) { clearTimeout(setTimer); setTimer = null; }
  pendingSettingsBody = null;
  try {
    const r = await fetch("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" }, body,
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    userTouchedSettings = true;
    settings.proxy = draft.proxy;
    settings.font = draft.font;
    bootSettings = JSON.parse(body);
    applyFontSettings();
    const apiObj = desktopApi();
    if (apiObj && typeof apiObj.desktop_lyrics_font === "function") {
      try {
        await apiObj.desktop_lyrics_font(draft.font.lyrics_family, draft.font.lyrics_size);
      } catch (e) {}
    }
    $("set-msg").textContent = "设置已保存";
    setTimeout(() => { if ($("set-msg").textContent === "设置已保存") $("set-msg").textContent = ""; }, 2200);
  } catch (e) {
    $("set-msg").textContent = "保存失败：" + (e.message || e);
  }
}
function setupSettingsUI() {
  $("btn-settings").addEventListener("click", openSettings);
  $("settings-close").addEventListener("click", closeSettings);
  $("settings-modal").addEventListener("click", (e) => { if (e.target.id === "settings-modal") closeSettings(); });
  $("set-proxy-mode").addEventListener("change", syncProxyInputs);
  $("set-ui-family").addEventListener("change", () => {
    $("set-preview").style.fontFamily = cssFontFamily($("set-ui-family").value);
  });
  $("set-ui-scale").addEventListener("input", (e) => {
    $("set-ui-scale-val").textContent = Math.round(Number(e.target.value) * 100) + "%";
    document.documentElement.style.setProperty("--font-scale", e.target.value);
  });
  $("set-lyrics-size").addEventListener("input", (e) => {
    $("set-lyrics-size-val").textContent = e.target.value;
  });
  $("settings-reset").addEventListener("click", () => {
    renderSettingsFormDefault();
  });
  $("settings-save").addEventListener("click", saveSettingsForm);
}
setupSettingsUI();

// ---------------- 中栏渲染 ----------------
const DEFAULT_EMPTY = '<div class="empty-ic">🎵</div><p>选择音源，搜索你喜欢的音乐</p>';
function showEmpty(html) {
  $("list").innerHTML = "";
  const e = $("empty");
  e.classList.remove("hidden");
  e.innerHTML = html || DEFAULT_EMPTY;
}

function renderSongs(songs, opts) {
  opts = opts || {};
  $("empty").classList.add("hidden");
  const list = $("list");
  list.innerHTML = "";
  songs.forEach((s, i) => list.appendChild(songRow(s, i, opts)));
}

function songRow(s, i, opts) {
  const row = document.createElement("div");
  row.className = "song-row";
  row.dataset.source = s.source || "";
  row.dataset.songId = String(s.id == null ? "" : s.id);
  row.dataset.songKey = songKey(s);   // 完整身份: 音源+ID+extra(含B站分P cid)
  const cur = curSong();
  const playing = cur === s ||
    (state.index >= 0 && cur && songKey(cur) === row.dataset.songKey);
  if (playing) row.classList.add("playing");
  const vip = s.extra && s.extra.vip ? '<span class="s-vip">VIP</span>' : "";
  row.innerHTML =
    '<div class="s-idx">' + (i + 1) + "</div>" +
    coverHtml(s) +
    '<div class="s-meta"><div class="s-title">' + esc(s.title) + "</div>" +
    '<div class="s-artist">' + esc(s.artist) + (s.album ? " · " + esc(s.album) : "") + "</div></div>" +
    vip +
    '<span class="src-tag">' + (SRC_NAME[s.source] || s.source) + "</span>" +
    '<span class="s-dur">' + (s.duration ? fmt(s.duration) : "") + "</span>" +
    '<button class="s-add" title="加入播放列表">＋</button>';
  row.querySelector(".s-add").addEventListener("click", (e) => {
    e.stopPropagation();
    addToQueue(s);
  });
  row.addEventListener("click", () => playNow(s, opts.songs));
  bindLongPress(row, () => openCtxMenu(s, opts));
  return row;
}

function coverSrc(s) {
  // B站封面(hdslb.com)有防盗链, 走本地图片代理
  const c = s.cover || "";
  if (c && (s.source === "bilibili" || c.indexOf("hdslb.com") !== -1))
    return withTok("/imgproxy?url=" + encodeURIComponent(c));
  return c;
}
function coverHtml(s) {
  const c = coverSrc(s);
  if (c) {
    return '<img class="s-cover" loading="lazy" src="' + esc(c) +
      '" onerror="this.outerHTML=\'<div class=&quot;s-cover-ph&quot;>♪</div>\'">';
  }
  return '<div class="s-cover-ph">♪</div>';
}

function setCrumb(html) {
  const c = $("crumb");
  if (!html) { c.classList.add("hidden"); return; }
  c.classList.remove("hidden");
  c.innerHTML = html;
}

// ---------------- 搜索 ----------------
const SEARCH_LIMIT = 30;
async function doSearch(page) {
  page = Math.max(1, parseInt(page, 10) || 1);
  const kw = $("search-input").value.trim();
  if (!kw) return;
  const view = state.view = { type: "search", kw: kw, page: page };
  setCrumb("「" + esc(kw) + "」的搜索结果 · " + (SRC_NAME[state.source] || ""));
  showEmpty('<div class="loading">搜索中…</div>');
  try {
    const j = await api("/api/search?source=" + state.source +
      "&keyword=" + encodeURIComponent(kw) + "&limit=" + SEARCH_LIMIT + "&page=" + page);
    if (state.view !== view) return;  // 期间已发起新搜索或切换页面, 丢弃旧结果
    if (j.error) { showEmpty('<div class="loading">' + esc(j.error) + "</div>"); return; }
    const songs = j.songs || [];
    if (!songs.length && page === 1) {
      showEmpty('<div class="empty-ic">🔍</div><p>没有找到相关结果</p>');
      return;
    }
    renderSongs(songs, { songs: songs });
    renderSearchPager(view, j, songs.length);
    $("list").scrollTop = 0;
  } catch (e) {
    if (state.view !== view) return;
    showEmpty('<div class="loading">网络错误: ' + esc(e) + "</div>");
  }
}

function renderSearchPager(view, j, got) {
  const list = $("list");
  const bar = document.createElement("div");
  bar.className = "pager-bar";
  const page = j.page || view.page || 1;
  // 末页判定: has_more 由后端按 total/整页计算; 有 total 时再叠加页码上限(用实际页大小)
  const ps = j.page_size || SEARCH_LIMIT;
  const pages = j.total ? Math.max(1, Math.ceil(j.total / ps)) : null;
  const noMore = j.has_more === false || (pages && page >= pages);
  bar.innerHTML =
    '<button class="go-btn ghost" id="pg-prev"' + (page <= 1 ? " disabled" : "") + ">‹ 上一页</button>" +
    '<span class="pg-info">第 <input type="number" id="pg-input" class="pg-num" min="1"' +
    (pages ? ' max="' + pages + '"' : "") + ' value="' + page + '"> 页' +
    (pages ? " / 共 " + pages + " 页" : "") + "</span>" +
    '<button class="go-btn ghost" id="pg-next"' + (noMore ? " disabled" : "") + ">下一页 ›</button>";
  list.appendChild(bar);
  const prev = $("pg-prev"), next = $("pg-next");
  if (page > 1) prev.addEventListener("click", () => {
    if (state.view === view) doSearch(page - 1);
  });
  if (!noMore) next.addEventListener("click", () => {
    if (state.view === view) doSearch(page + 1);
  });
  // 直接在「第 X 页」处输入页号翻页: 回车或失焦生效, 钳制到 [1, 末页]
  const input = $("pg-input");
  const jump = () => {
    if (state.view !== view) return;
    let t = parseInt(input.value, 10);
    if (!t || t < 1) t = 1;
    if (pages && t > pages) t = pages;
    input.value = t;
    if (t !== page) doSearch(t);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
  });
  input.addEventListener("blur", jump);
}

// ---------------- 歌单: 远端(音源) ----------------
async function openPlaylist(source, ref, name) {
  const view = state.view = { type: "playlist", source: source, ref: ref, name: name };
  setCrumb('<a id="crumb-back">‹ 返回</a> ' + esc(name));
  const back = $("crumb-back");
  if (back) back.addEventListener("click", () => openSourceLists(source));
  showEmpty('<div class="loading">加载中…</div>');
  const j = await api("/api/playlist?source=" + source + "&ref=" + encodeURIComponent(ref));
  if (state.view !== view) return;
  if (j.error) { showEmpty('<div class="loading">' + esc(j.error) + "</div>"); return; }
  const songs = j.songs || [];
  renderSongs(songs, { songs: songs, playlist: true });
  const bar = document.createElement("div");
  bar.style.cssText = "padding:8px 12px;";
  bar.innerHTML = '<button class="go-btn" id="add-all" style="height:40px">全部加入播放列表</button>';
  $("list").prepend(bar);
  $("add-all").addEventListener("click", () => {
    let n = 0;
    songs.forEach((s) => { if (!inQueue(s)) { state.queue.push(s); n++; } });
    if (n) { renderQueue(); saveQueue(); toast("已加入 " + n + " 首"); }
    else toast("歌曲均已在播放列表中");
  });
}

async function openSourceLists(source) {
  const view = state.view = { type: "mylists", source: source };
  setCrumb(SRC_NAME[source] + " · 我的歌单");
  showEmpty('<div class="loading">加载中…</div>');
  const j = await api("/api/mylists?source=" + source);
  if (state.view !== view) return;
  if (j.error) { showEmpty('<div class="empty-ic src-logo-lg">' + (SRC_LOGO[source] || "📂") + "</div><p>" + esc(j.error) + "</p>"); return; }
  const pls = j.playlists || [];
  if (!pls.length) { showEmpty('<div class="empty-ic src-logo-lg">' + (SRC_LOGO[source] || "📂") + "</div><p>暂无歌单（请先登录该音源）</p>"); return; }
  renderPlaylistCards(pls, (p) => openPlaylist(source, p.id, p.name), source);
}

function renderPlaylistCards(pls, onOpen, source) {
  $("empty").classList.add("hidden");
  const list = $("list");
  list.innerHTML = "";
  pls.forEach((p) => {
    const row = document.createElement("div");
    row.className = "song-row";
    row.innerHTML =
      (p.cover ? '<img class="s-cover" loading="lazy" src="' + esc(p.cover) + '">'
               : '<div class="s-cover-ph src-logo">' + (SRC_LOGO[source] || "📂") + "</div>") +
      '<div class="s-meta"><div class="s-title">' + esc(p.name) + "</div>" +
      '<div class="s-artist">' + (p.count || "") + " 首</div></div>";
    row.addEventListener("click", () => onOpen(p));
    list.appendChild(row);
  });
}

// ---------------- 歌单: 本地(收藏) ----------------
async function localPl(action, payload) {
  const r = await fetch("/api/localplaylist/" + action, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  return r.json();
}

async function postJSON(path, payload) {
  const r = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  return r.json();
}

// 歌单封面: http/data: 开头视为图片, 否则回退默认占位符
function plCover(icon) {
  if (icon && /^(https?:|data:)/i.test(icon))
    return '<img class="s-cover" loading="lazy" src="' + esc(icon) + '">';
  return '<div class="s-cover-ph">♪</div>';
}

async function openLocalLists() {
  const view = state.view = { type: "locallists" };
  clearSrcPlActive();
  setCrumb("我的歌单 · 本地收藏");
  showEmpty('<div class="loading">加载中…</div>');
  const j = await api("/api/locallists");
  if (state.view !== view) return;
  const pls = j.lists || [];
  $("empty").classList.add("hidden");
  const list = $("list");
  list.innerHTML = "";
  const bar = document.createElement("div");
  bar.style.cssText = "padding:8px 12px;";
  bar.innerHTML = '<button class="go-btn" id="new-pl" style="height:40px">＋ 新建歌单</button>';
  list.appendChild(bar);
  $("new-pl").addEventListener("click", () => askName("新建歌单", "", (name) => {
    localPl("create", { name }).then((j2) => {
      if (j2.error) { toast(j2.error); return; }
      toast("已创建歌单");
      openLocalLists();
    });
  }));
  pls.forEach((p) => {
    const row = document.createElement("div");
    row.className = "song-row";
    row.innerHTML =
      plCover(p.icon) +
      '<div class="s-meta"><div class="s-title">' + esc(p.name) + "</div>" +
      '<div class="s-artist">' + (p.count || 0) + " 首</div></div>";
    row.addEventListener("click", () => openLocalPlaylist(p.id, p.name));
    bindLongPress(row, () => localListMenu(p));
    list.appendChild(row);
  });
  if (!pls.length) {
    const hint = document.createElement("div");
    hint.className = "s-artist";
    hint.style.cssText = "padding:20px;text-align:center;color:var(--fg-dim)";
    hint.textContent = "还没有歌单，点上方“新建歌单”，再在歌曲上长按“收藏”";
    list.appendChild(hint);
  }
}

function localListMenu(p) {
  const menu = ensureCtxMount();
  menu.innerHTML = "";
  const items = [
    { t: "✎ 重命名", fn: () => askName("重命名歌单", p.name, (nn) => {
        localPl("rename", { id: p.id, name: nn }).then(() => openLocalLists());
      }) },
    { t: "▣ 修改封面", fn: () => openIconPicker(p) },
    { t: "▶ 播放全部", fn: () => openLocalPlaylist(p.id, p.name, true) },
    { t: "✕ 删除歌单", danger: true, fn: async () => {
        if (await showConfirm("删除歌单「" + p.name + "」？（不影响已下载文件）")) {
          localPl("delete", { id: p.id }).then(() => { toast("已删除"); openLocalLists(); });
        }
      } },
  ];
  items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "ctx-item" + (it.danger ? " danger" : "");
    d.textContent = it.t;
    d.addEventListener("click", () => { hideCtx(); it.fn(); });
    menu.appendChild(d);
  });
  menu.classList.remove("hidden");
  const r = lastTouchPos;
  menu.style.left = Math.min(r.x, innerWidth - 190) + "px";
  menu.style.top = Math.max(10, r.y - menu.offsetHeight - 8) + "px";
}

// ---------------- 通用输入弹窗(替代 prompt) ----------------
let nameCb = null;
function askName(title, initial, cb) {
  $("plname-title").textContent = title;
  const inp = $("plname-input");
  inp.value = initial || "";
  nameCb = cb;
  $("plname-modal").classList.remove("hidden");
  setTimeout(() => inp.focus(), 50);
}
function closeNameModal() { $("plname-modal").classList.add("hidden"); nameCb = null; }
$("plname-ok").addEventListener("click", () => {
  const v = $("plname-input").value.trim();
  if (!v) { toast("名称不能为空"); return; }
  const cb = nameCb; closeNameModal(); if (cb) cb(v);
});
$("plname-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("plname-ok").click(); });
$("plname-cancel").addEventListener("click", closeNameModal);
$("plname-close").addEventListener("click", closeNameModal);
$("plname-modal").addEventListener("pointerdown", (e) => { if (e.target === $("plname-modal")) closeNameModal(); });

// ---------------- 歌单封面选择 ----------------
let iconPl = null;
let iconReq = 0;   // 请求代次: 旧歌单的迟到响应不得重建当前网格
async function openIconPicker(p) {
  const req = ++iconReq;
  iconPl = p;
  const grid = $("plicon-grid");
  grid.innerHTML = '<div class="loading" style="grid-column:1/-1">加载中…</div>';
  $("plicon-modal").classList.remove("hidden");
  const j = await api("/api/localplaylist?pid=" + encodeURIComponent(p.id));
  if (req !== iconReq || iconPl !== p) return;  // 已切换/关闭: 丢弃迟到响应
  const covers = [];
  (j.songs || []).forEach((s) => {
    const c = s.cover || s.album_cover || s.pic;
    if (c && !covers.includes(c)) covers.push(c);
  });
  grid.innerHTML = "";
  const mk = (src, label) => {
    const c = document.createElement("div");
    c.className = "icon-cell" + (p.icon === src ? " sel" : "");
    c.innerHTML = '<img src="' + esc(src) + '" loading="lazy" onerror="this.parentNode.remove()">' +
                  (label ? '<span class="ic-label">' + esc(label) + "</span>" : "");
    c.addEventListener("click", () => setPlaylistIcon(p.id, src));
    grid.appendChild(c);
  };
  // 自定义图片入口
  const up = document.createElement("div");
  up.className = "icon-cell upload";
  up.innerHTML = '<span class="up-ic">＋</span><span class="ic-label">本地图片</span>';
  up.addEventListener("click", () => $("plicon-file").click());
  grid.appendChild(up);
  if (covers.length) {
    const t = document.createElement("div");
    t.className = "grid-title";
    t.textContent = "歌单封面";
    grid.appendChild(t);
    covers.slice(0, 24).forEach((c) => mk(c, ""));
  } else {
    const t = document.createElement("div");
    t.className = "grid-title";
    t.textContent = "歌单暂无歌曲，可上传自定义图片";
    grid.appendChild(t);
  }
}

async function setPlaylistIcon(pid, src) {
  // 只写入当前有效歌单: 选择器已切换/关闭时忽略旧网格点击
  if (!iconPl || iconPl.id !== pid) return;
  const r = await localPl("seticon", { id: pid, icon: src });
  if (r.error) { toast(r.error); return; }
  $("plicon-modal").classList.add("hidden");
  iconReq++;  // 关闭后作废在途的网格请求
  toast("已设置封面");
  if (state.view && state.view.type === "locallists") openLocalLists();
}

// 本地图片 -> 裁切弹窗 -> dataURL
$("plicon-file").addEventListener("change", (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!f || !iconPl) return;
  if (f.size > 12 * 1024 * 1024) { toast("图片过大（限 12MB）"); return; }
  const fr = new FileReader();
  fr.onload = () => openCrop(fr.result);
  fr.onerror = () => toast("读取失败");
  fr.readAsDataURL(f);
});

// ---------------- 图片裁切(拖动/缩放自选区域) ----------------
const CROP = { img: null, scale: 1, minScale: 1, x: 0, y: 0, size: 280, out: 320 };
function cropDraw() {
  const cv = $("crop-canvas");
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, CROP.size, CROP.size);
  if (!CROP.img) return;
  const w = CROP.img.width * CROP.scale, h = CROP.img.height * CROP.scale;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(CROP.img, CROP.x, CROP.y, w, h);
}
function cropClamp() {
  const w = CROP.img.width * CROP.scale, h = CROP.img.height * CROP.scale, s = CROP.size;
  // 图片始终铺满裁切框(不允许出现空白边)
  CROP.x = Math.min(0, Math.max(s - w, CROP.x));
  CROP.y = Math.min(0, Math.max(s - h, CROP.y));
}
function openCrop(src) {
  const img = new Image();
  img.onload = () => {
    CROP.img = img;
    // 初始: 等比铺满(cover), 居中
    CROP.minScale = Math.max(CROP.size / img.width, CROP.size / img.height);
    CROP.scale = CROP.minScale;
    CROP.x = (CROP.size - img.width * CROP.scale) / 2;
    CROP.y = (CROP.size - img.height * CROP.scale) / 2;
    const zr = $("crop-zoom");
    zr.min = String(CROP.minScale.toFixed(3));
    zr.max = String((CROP.minScale * 4).toFixed(3));
    zr.step = "0.01";
    zr.value = CROP.scale.toFixed(3);
    cropDraw();
    $("plicon-modal").classList.add("hidden");
    $("crop-modal").classList.remove("hidden");
  };
  img.onerror = () => toast("图片无法读取");
  img.src = src;
}
function closeCrop() { $("crop-modal").classList.add("hidden"); CROP.img = null; }

$("crop-zoom").addEventListener("input", (e) => {
  if (!CROP.img) return;
  const old = CROP.scale;
  CROP.scale = parseFloat(e.target.value) || CROP.minScale;
  // 以画面中心为锚点缩放
  const cx = CROP.size / 2, cy = CROP.size / 2;
  CROP.x = cx - (cx - CROP.x) * (CROP.scale / old);
  CROP.y = cy - (cy - CROP.y) * (CROP.scale / old);
  cropClamp(); cropDraw();
});

(function cropDrag() {
  const stage = $("crop-stage");
  let drag = null;
  stage.addEventListener("pointerdown", (e) => {
    if (!CROP.img) return;
    drag = { px: e.clientX, py: e.clientY, ox: CROP.x, oy: CROP.y };
    stage.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  stage.addEventListener("pointermove", (e) => {
    if (!drag) return;
    CROP.x = drag.ox + (e.clientX - drag.px);
    CROP.y = drag.oy + (e.clientY - drag.py);
    cropClamp(); cropDraw();
  });
  const end = () => { drag = null; };
  stage.addEventListener("pointerup", end);
  stage.addEventListener("pointercancel", end);
  // 滚轮 / 触控板捏合缩放
  stage.addEventListener("wheel", (e) => {
    if (!CROP.img) return;
    e.preventDefault();
    const zr = $("crop-zoom");
    const old = CROP.scale;
    let ns = old * (e.deltaY < 0 ? 1.12 : 1 / 1.12);
    ns = Math.min(parseFloat(zr.max), Math.max(parseFloat(zr.min), ns));
    zr.value = ns.toFixed(3);
    CROP.scale = ns;
    const r = stage.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    CROP.x = cx - (cx - CROP.x) * (ns / old);
    CROP.y = cy - (cy - CROP.y) * (ns / old);
    cropClamp(); cropDraw();
  }, { passive: false });
})();

$("crop-ok").addEventListener("click", () => {
  if (!CROP.img || !iconPl) { closeCrop(); return; }
  const o = CROP.out;
  const cv = document.createElement("canvas");
  cv.width = o; cv.height = o;
  const ctx = cv.getContext("2d");
  ctx.imageSmoothingQuality = "high";
  const k = o / CROP.size;
  ctx.drawImage(CROP.img, CROP.x * k, CROP.y * k,
                CROP.img.width * CROP.scale * k, CROP.img.height * CROP.scale * k);
  closeCrop();
  setPlaylistIcon(iconPl.id, cv.toDataURL("image/jpeg", 0.86));
});
$("crop-cancel").addEventListener("click", closeCrop);
$("crop-close").addEventListener("click", closeCrop);
$("crop-modal").addEventListener("pointerdown", (e) => { if (e.target === $("crop-modal")) closeCrop(); });

$("plicon-none").addEventListener("click", () => {
  if (!iconPl) return;
  setPlaylistIcon(iconPl.id, "");
});
$("plicon-close").addEventListener("click", () => $("plicon-modal").classList.add("hidden"));
$("plicon-modal").addEventListener("pointerdown", (e) => { if (e.target === $("plicon-modal")) $("plicon-modal").classList.add("hidden"); });

async function openLocalPlaylist(pid, name, autoplay) {
  const view = state.view = { type: "localplaylist", pid: pid, name: name };
  setCrumb('<a id="crumb-back">‹ 返回</a> ' + esc(name));
  const back = $("crumb-back");
  if (back) back.addEventListener("click", openLocalLists);
  showEmpty('<div class="loading">加载中…</div>');
  const j = await api("/api/localplaylist?pid=" + encodeURIComponent(pid));
  if (state.view !== view) return;
  if (j.error) { showEmpty('<div class="loading">' + esc(j.error) + "</div>"); return; }
  const songs = j.songs || [];
  renderSongs(songs, { songs: songs, playlist: true, local: true, pid: pid });
  const bar = document.createElement("div");
  bar.style.cssText = "padding:8px 12px;";
  bar.innerHTML = '<button class="go-btn" id="add-all" style="height:40px">全部加入播放列表</button>';
  $("list").prepend(bar);
  $("add-all").addEventListener("click", () => {
    let n = 0;
    songs.forEach((s) => { if (!inQueue(s)) { state.queue.push(s); n++; } });
    if (n) { renderQueue(); saveQueue(); toast("已加入 " + n + " 首"); }
    else toast("歌曲均已在播放列表中");
  });
  if (autoplay && songs.length) playNow(songs[0], songs);
}

async function collectSong(s) {
  const j = await api("/api/locallists");
  const pls = j.lists || [];
  const menu = ensureCtxMount();
  menu.innerHTML = "";
  const items = pls.map((p) => ({
    t: "♪ " + p.name + "（" + (p.count || 0) + "）",
    fn: async () => {
      const a = await localPl("add", { id: p.id, song: s });
      if (a.error) { toast(a.error); return; }
      toast(a.existed ? "该歌单已收藏" : "已收藏到「" + p.name + "」");
    },
  }));
  items.push({ t: "＋ 新建歌单并收藏", fn: () => {
    askName("新建歌单", "", async (name) => {
      const c = await localPl("create", { name });
      if (c.error) { toast(c.error); return; }
      await localPl("add", { id: c.id, song: s });
      toast("已收藏到「" + name + "」");
    });
  } });
  items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "ctx-item";
    d.textContent = it.t;
    d.addEventListener("click", () => { hideCtx(); it.fn(); });
    menu.appendChild(d);
  });
  menu.classList.remove("hidden");
  const r = lastTouchPos;
  menu.style.left = Math.min(r.x, innerWidth - 220) + "px";
  menu.style.top = Math.max(10, Math.min(r.y, innerHeight - menu.offsetHeight - 10)) + "px";
}

async function removeCollected(pid, s) {
  if (!await showConfirm("从该歌单移除《" + s.title + "》？")) return;
  await localPl("remove", { id: pid, song: s });
  openLocalPlaylist(pid, state.view.name);
}

function downloadSong(s) {
  postJSON("/api/dl/add", { song: s }).then((j) => {
    if (j.error) { toast(j.error); return; }
    toast("已加入下载队列：" + (s.title || ""));
    startDlPoll();
  });
}

// ---------------- 下载管理 ----------------
let dlTimer = null;
function startDlPoll() {
  if (dlTimer) return;
  dlTimer = setInterval(() => {
    if (state.view && state.view.type === "downloads") renderDownloads();
    else { clearInterval(dlTimer); dlTimer = null; }
  }, 1200);
}

async function openDownloads() {
  state.view = { type: "downloads" };
  clearSrcPlActive();
  setCrumb("下载管理");
  showEmpty('<div class="loading">加载中…</div>');
  await renderDownloads();
  startDlPoll();
}

function fmtSize(b) {
  if (!b) return "";
  if (b < 1024 * 1024) return (b / 1024).toFixed(0) + " KB";
  return (b / 1024 / 1024).toFixed(1) + " MB";
}
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = (n) => (n < 10 ? "0" : "") + n;
  return (d.getMonth() + 1) + "/" + d.getDate() + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

async function renderDownloads() {
  const [tj, ij] = await Promise.all([api("/api/dl/tasks"), api("/api/downloads")]);
  if (!state.view || state.view.type !== "downloads") return;
  const tasks = (tj.tasks || []).filter((t) => t.status !== "done");
  const items = ij.items || [];
  const list = $("list");
  list.innerHTML = "";
  // 顶部操作条
  const bar = document.createElement("div");
  bar.style.cssText = "padding:8px 12px;display:flex;gap:10px;align-items:center;";
  bar.innerHTML =
    '<span class="s-artist" style="flex:1">进行中 ' + tasks.length + " · 已下载 " + items.length + "</span>" +
    (tasks.length ? '<button class="go-btn ghost" id="dl-clear" style="height:36px">清除已完成</button>' : "") +
    '<button class="go-btn" id="dl-open" style="height:36px">打开文件夹</button>';
  list.appendChild(bar);
  $("dl-open").addEventListener("click", () => {
    if (window.pywebview && pywebview.api && pywebview.api.open_downloads) pywebview.api.open_downloads();
  });
  if ($("dl-clear")) $("dl-clear").addEventListener("click", () => {
    postJSON("/api/dl/clear", {}).then(renderDownloads);
  });

  // 进行中 / 出错的任务
  tasks.forEach((t) => {
    const row = document.createElement("div");
    row.className = "song-row dl-task";
    // 已知总大小才显示百分比, 否则只显示已收字节数
    const prog = t.size
      ? Math.min(100, Math.floor(t.received / t.size * 100)) + "% (" + fmtSize(t.received) + " / " + fmtSize(t.size) + ")"
      : (fmtSize(t.received) || "0 KB");
    const stText = t.status === "downloading" ? "下载中 " + prog
      : t.status === "pending" ? "排队中" + (t.received ? "（已下载 " + fmtSize(t.received) + "）" : "")
      : t.status === "cancelled" ? "已取消"
      : "失败: " + esc(t.error || "");
    const canRetry = t.status === "error" || t.status === "cancelled";
    const canCancel = t.status === "pending" || t.status === "downloading" || t.status === "error";
    row.innerHTML =
      '<div class="s-cover-ph">⬇</div>' +
      '<div class="s-meta"><div class="s-title">' + esc(t.title) + "</div>" +
      '<div class="s-artist">' + esc(t.artist) + " · " + stText + "</div></div>" +
      '<span class="src-tag">' + (SRC_NAME[t.source] || t.source) + "</span>" +
      (canRetry ? '<button class="s-add dl-retry" title="重试">↻</button>' : "") +
      (canCancel ? '<button class="s-add dl-cancel" title="取消">✕</button>' : "");
    const rb = row.querySelector(".dl-retry");
    if (rb) rb.addEventListener("click", (e) => {
      e.stopPropagation();
      postJSON("/api/dl/retry", { task_id: t.task_id }).then((j) => {
        if (j.error) toast(j.error); else { toast("已重新加入下载队列"); startDlPoll(); }
        renderDownloads();
      });
    });
    const cb = row.querySelector(".dl-cancel");
    if (cb) cb.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!await showConfirm("取消下载《" + t.title + "》？已下载的部分会被删除")) return;
      postJSON("/api/dl/cancel", { task_id: t.task_id }).then((j) => {
        if (j.error) toast(j.error);
        renderDownloads();
      });
    });
    list.appendChild(row);
  });

  // 已下载歌曲
  if (!items.length && !tasks.length) {
    showEmpty('<div class="empty-ic">⬇</div><p>还没有下载的歌曲<br><span class="s-artist">在歌曲上长按选择“下载”</span></p>');
    return;
  }
  $("empty").classList.add("hidden");
  items.forEach((it, i) => {
    const s = { source: "local", id: it.file, file: it.file, title: it.title, artist: it.artist, cover: it.cover || "" };
    const row = document.createElement("div");
    row.className = "song-row";
    row.innerHTML =
      '<div class="s-idx">' + (i + 1) + "</div>" +
      '<div class="s-cover-ph">♪</div>' +
      '<div class="s-meta"><div class="s-title">' + esc(it.title) + "</div>" +
      '<div class="s-artist">' + esc(it.artist) + " · " + fmtSize(it.size) + " · " + fmtTime(it.time) + "</div></div>" +
      '<span class="src-tag">' + (SRC_NAME[it.source] || it.source || "") + "</span>" +
      '<button class="s-add" title="删除文件">✕</button>';
    row.querySelector(".s-add").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!await showConfirm("删除文件「" + it.file + "」？")) return;
      postJSON("/api/dl/delete", { file: it.file }).then(() => { toast("已删除"); renderDownloads(); });
    });
    row.addEventListener("click", () => playNow(s, items.map((x) =>
      ({ source: "local", id: x.file, file: x.file, title: x.title, artist: x.artist, cover: x.cover || "" }))));
    bindLongPress(row, () => dlItemMenu(it, s));
    list.appendChild(row);
  });
}

function dlItemMenu(it, s) {
  const menu = ensureCtxMount();
  menu.innerHTML = "";
  const items = [
    { t: "▶ 播放", fn: () => playNow(s, [s]) },
    { t: "＋ 加入播放列表", fn: () => addToQueue(s) },
    { t: "★ 收藏到歌单", fn: () => collectSong(s) },
    { t: "✕ 删除文件", danger: true, fn: async () => {
        if (await showConfirm("删除文件「" + it.file + "」？"))
          postJSON("/api/dl/delete", { file: it.file }).then(() => { toast("已删除"); renderDownloads(); });
      } },
  ];
  items.forEach((it2) => {
    const d = document.createElement("div");
    d.className = "ctx-item" + (it2.danger ? " danger" : "");
    d.textContent = it2.t;
    d.addEventListener("click", () => { hideCtx(); it2.fn(); });
    menu.appendChild(d);
  });
  menu.classList.remove("hidden");
  const r = lastTouchPos;
  menu.style.left = Math.min(r.x, innerWidth - 190) + "px";
  menu.style.top = Math.max(10, r.y - menu.offsetHeight - 8) + "px";
}

// ---------------- 播放队列 ----------------
// canonicalJson: 对象键升序且递归, 数组保序, 不做 ASCII 转义, 紧凑无空白
// (契约 A: 等价 Python json.dumps(separators=(",", ":"), ensure_ascii=False) 且 sort_keys)
function canonicalJson(v) {
  if (Array.isArray(v)) return "[" + v.map(canonicalJson).join(",") + "]";
  if (v && typeof v === "object") {
    return "{" + Object.keys(v).sort().map(function (k) {
      return JSON.stringify(k) + ":" + canonicalJson(v[k]);
    }).join(",") + "}";
  }
  return JSON.stringify(v === undefined ? null : v);
}
// 全文件唯一 songKey 声明(契约 A): source + ":" + String(id) + ":" + canonicalJson(extra || {})
function songKey(s) { return s ? (s.source + ":" + String(s.id) + ":" + canonicalJson(s.extra || {})) : ""; }
// 统一歌曲身份比较: 队列去重/播放高亮/下载去重与推荐流一律走同一身份函数
function sameSong(a, b) { return songKey(a) === songKey(b); }
function inQueue(s) {
  return state.queue.some((q) => sameSong(q, s));
}
function findQueue(s) {
  return state.queue.findIndex((q) => sameSong(q, s));
}

function addToQueue(s) {
  if (inQueue(s)) { toast("已在播放列表中"); return; }
  state.queue.push(s);
  // 首曲自动播放分支也要持久化并置 userTouchedQueue(经 saveQueue): 否则启动恢复守卫失效、首曲漏存
  if (state.index < 0 && !state.feedMode) { playAt(state.queue.length - 1); saveQueue(); }
  else { renderQueue(); saveQueue(); }
  toast("已加入: " + s.title);
}

function playNow(s, fromList) {
  // B站视频: 不加入播放列表, 维护独立的推荐/相关切换流
  if (s.source === "bilibili") { playFeed(s, (fromList && fromList.length) ? fromList : [s]); return; }
  exitFeed();
  let i = findQueue(s);
  if (i < 0) {
    i = Math.min(Math.max(state.index + 1, 0), state.queue.length);
    state.queue.splice(i, 0, s);
  }
  playAt(i);
  renderQueue();
  saveQueue();
}

// 当前播放上下文: 推荐流播放时用独立的"当前视频列表", 否则用播放列表
function aq() { return state.feedMode ? state.feedQueue : state.queue; }
function curSong() { return state.index >= 0 ? aq()[state.index] : null; }

// B站推荐流播放: 进入 feed 模式, 用独立的当前视频列表作为前后切换序列(不占用/不修改播放列表)
function playFeed(s, list) {
  userTouchedQueue = true;   // 进入推荐流也算用户操作, 启动恢复不得覆盖
  if (!state.feedMode) { state.feedMode = true; state.mainIndex = state.index; }
  state.plView = "feed";
  if (list && list.length && list !== state.feedQueue) state.feedQueue = list.slice();
  let i = state.feedQueue.findIndex((q) => sameSong(q, s));
  if (i < 0) {
    i = state.index + 1;
    if (i < 0 || i >= state.feedQueue.length) i = Math.max(0, state.feedQueue.length - 1);
    state.feedQueue.splice(i, 0, s);
  }
  playAt(i);
  renderQueue();
}
// 退出 B站推荐流: 当前视频列表立即清空, 播放列表不受影响
function exitFeed() {
  if (!state.feedMode) return;
  state.feedMode = false;
  state.feedQueue = [];
  state.index = state.mainIndex >= 0 && state.mainIndex < state.queue.length ? state.mainIndex : -1;
  state.plView = "feed";
  renderQueue();
}

let playVersion = 0;
let skipTimer = null;
function playAt(i) {
  const q = aq();
  if (i < 0 || i >= q.length) return;
  playVersion++;
  clearTimeout(skipTimer);
  skipTimer = null;
  state.index = i;
  const s = q[i];
  if (desktopLyrics.enabled) desktopBeginSong(s);
  $("pb-title").textContent = s.title;
  $("pb-artist").textContent = (s.artist || "—") + " · " + (SRC_NAME[s.source] || s.source);
  const pcs = coverSrc(s);
  $("pb-cover").innerHTML = pcs ? '<img src="' + esc(pcs) + '" onerror="this.parentNode.innerHTML=\'♪\'">' : "♪";
  audio.src = proxyUrl(s);
  // 恢复该歌曲上次播放进度
  const key = songKey(s);
  audio.dataset.key = key;
  if (settings.pos && settings.pos.key === key && settings.pos.time > 3) {
    audio.dataset.resume = String(settings.pos.time);
  } else {
    audio.dataset.resume = "";
  }
  audio.play().catch(() => {});
  renderQueue();
  markPlayingRows();
  if (lyricOpen) {
    // 覆盖层打开时切歌: 自动刷新视频/歌词与标题信息
    $("lo-title").textContent = s.title;
    $("lo-artist").textContent = (s.artist || "—") + " · " + (SRC_NAME[s.source] || s.source);
    const cs = coverSrc(s);
    $("lo-bg").style.backgroundImage = cs ? 'url("' + esc(cs) + '")' : "";
    setupVideo(s);
  }
  // 桌面歌词不依赖覆盖层是否打开。
  if (desktopLyrics.enabled) loadLyrics(s);
}

function markPlayingRows() {
  const cur = curSong();
  const curKey = (cur && state.index >= 0) ? songKey(cur) : "";
  document.querySelectorAll("#list .song-row").forEach((r) => {
    // 完整身份(音源+ID+extra): 同名不同 ID 只高亮正确项, 同 BV 不同分 P 只高亮实际播放的分 P
    const playing = !!curKey && r.dataset.songKey === curKey;
    r.classList.toggle("playing", playing);
  });
}

function nextIndex(step) {
  const n = aq().length;
  if (!n) return -1;
  if (state.mode === "shuffle") {
    if (n === 1) return 0;
    let r;
    do { r = Math.floor(Math.random() * n); } while (r === state.index);
    return r;
  }
  let i = state.index + step;
  if (i < 0) i = n - 1;
  if (i >= n) i = state.mode === "order" ? n - 1 : 0;
  if (state.mode === "order" && state.index >= n - 1 && step > 0) return -1;
  return i;
}

function next() { const i = nextIndex(1); if (i >= 0) { playAt(i); saveQueue(); } }
function prev() { const i = nextIndex(-1); if (i >= 0) { playAt(i); saveQueue(); } }

// ---------------- 播放列表右栏渲染 + 触控 ----------------
// 右栏当前显示的列表: 推荐流下可切换查看"当前视频列表"或"默认播放列表"
function vq() { return (state.feedMode && state.plView === "main") ? state.queue : aq(); }
function renderQueue() {
  const box = $("pl-list");
  const tabs = $("pl-tabs");
  if (tabs) tabs.classList.toggle("hidden", !state.feedMode);
  if (state.feedMode && tabs) {
    tabs.querySelectorAll(".pl-tab").forEach((t) => t.classList.toggle("active", t.dataset.v === state.plView));
  }
  const showMain = state.feedMode && state.plView === "main";
  const q = vq();
  $("pl-count").textContent = q.length;
  const ttl = document.querySelector(".pl-title");
  if (ttl) ttl.textContent = (state.feedMode && !showMain) ? "当前视频列表" : "播放列表";
  box.innerHTML = "";
  if (!q.length) {
    box.innerHTML = showMain ? '<div class="pl-empty">播放列表为空<br>点击歌曲行的 ＋ 加入</div>'
      : state.feedMode ? '<div class="pl-empty">当前视频列表为空</div>'
      : '<div class="pl-empty">播放列表为空<br>点击歌曲行的 ＋ 加入</div>';
    return;
  }
  q.forEach((s, i) => box.appendChild(plRow(s, i, showMain)));
}

function plRow(s, i, showMain) {
  const row = document.createElement("div");
  row.className = "pl-row";
  if (!showMain && i === state.index) row.classList.add("playing");
  row.innerHTML =
    '<div class="pl-grip">⠿</div>' +
    '<div class="pl-t"><div class="n">' + esc(s.title) + "</div>" +
    '<div class="a">' + esc(s.artist) + " · " + (SRC_NAME[s.source] || s.source) + "</div></div>" +
    '<button class="pl-del">✕</button>';
  row.querySelector(".pl-del").addEventListener("click", (e) => {
    e.stopPropagation();
    removeAt(i, showMain);
  });
  row.addEventListener("click", () => {
    if (plDragged) { plDragged = false; return; }
    if (showMain) { exitFeed(); playAt(i); saveQueue(); renderQueue(); }
    else { playAt(i); saveQueue(); }
  });
  bindRowTouch(row, i, showMain, s);
  return row;
}

// 左滑删除延迟执行: 绑定渲染时的原队列引用与原歌曲身份, 上下文切换后不误删新队列同下标项
function removeSwiped(q0, s, showMain) {
  const i = q0.indexOf(s);
  if (i < 0) return;                    // 原队列已不含该歌曲(增删/重排): 放弃
  const cur = showMain ? state.queue : aq();
  if (cur !== q0) {
    // 播放上下文已切换: 只操作原队列, 不动当前播放下标
    if (q0 === state.queue) {
      if (i === state.mainIndex) state.mainIndex = -1;
      else if (i < state.mainIndex) state.mainIndex--;
      q0.splice(i, 1);
      renderQueue();
      saveQueue();
    }
    return;                             // q0 是已废弃的旧 feedQueue: 无需处理
  }
  removeAt(i, showMain);
}

function removeAt(i, showMain) {
  const q = showMain ? state.queue : aq();
  const wasPlaying = !showMain && i === state.index;
  q.splice(i, 1);
  if (wasPlaying) {
    audio.pause();
    if (q.length) { state.index = Math.min(i, q.length - 1); playAt(state.index); }
    else { state.index = -1; audio.removeAttribute("src"); resetPlayerBar(); }
  } else if (!showMain && i < state.index) {
    state.index--;
  } else if (showMain && state.feedMode) {
    if (i === state.mainIndex) state.mainIndex = -1;
    else if (i < state.mainIndex) state.mainIndex--;
  }
  renderQueue();
  saveQueue();
}

function resetPlayerBar() {
  playVersion++;
  clearTimeout(skipTimer);
  skipTimer = null;
  $("pb-title").textContent = "未在播放";
  $("pb-artist").textContent = "— · —";
  $("pb-cover").innerHTML = "<span>♪</span>";
  $("seek-fill").style.width = "0";
  $("t-cur").textContent = "0:00";
  $("t-dur").textContent = "0:00";
  setPlayIcon(false);
  settings.pos = null;
  stopAsr();
  desktopReset();
  saveSettings();
}

// Windows 媒体风格播放/暂停图标
const ICON_PLAY = '<svg viewBox="0 0 48 48" fill="currentColor"><path d="M16 10.5v27c0 1.2 1.3 1.9 2.3 1.3l20.6-13.5c1-.6 1-2 0-2.6L18.3 9.2c-1-.6-2.3.1-2.3 1.3z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 48 48" fill="currentColor"><rect x="13" y="10" width="8" height="28" rx="1"/><rect x="27" y="10" width="8" height="28" rx="1"/></svg>';
function setPlayIcon(playing) {
  $("btn-play").innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
  const vp = $("v-play");
  if (vp) vp.innerHTML = playing ? ICON_PAUSE : ICON_PLAY;
}

// 播放列表行触控/鼠标: 长按拖动排序(触屏) / 竖向拖动排序(鼠标) / 左滑删除
let plDragged = false;   // 拖动结束后抑制紧随的 click
function bindRowTouch(row, idx, showMain, song) {
  const q0 = showMain ? state.queue : aq();   // 渲染时的原队列引用
  const s0 = song || q0[idx];                  // 渲染时的原歌曲对象
  let startX = 0, startY = 0, mode = null, lpTimer = null, dx = 0, down = false;
  row.addEventListener("pointerdown", (e) => {
    plDragged = false;
    if (e.target.closest(".pl-del")) return;
    down = true;
    startX = e.clientX; startY = e.clientY; mode = null; dx = 0;
    if (e.pointerType === "mouse") return;      // 鼠标: 竖向移动超阈值直接起拖
    lpTimer = setTimeout(() => {
      mode = "drag";
      document.body.style.touchAction = "none";  // 冻结页面滚动
      dragStart(row, idx, showMain, startY);
      row.setPointerCapture(e.pointerId);
      if (navigator.vibrate) navigator.vibrate(15);
    }, 480);
  });
  // 拖动中阻止浏览器把手势判定为列表滚动(否则 pointercancel 中断拖动)
  row.addEventListener("touchmove", (e) => { if (mode === "drag") e.preventDefault(); }, { passive: false });
  row.addEventListener("pointermove", (e) => {
    if (!down && !mode) return;
    const mx = e.clientX - startX, my = e.clientY - startY;
    if (!mode) {
      if (e.pointerType === "mouse") {
        if (Math.abs(my) > 6) {                  // 鼠标按住竖向移动 -> 拖动排序
          mode = "drag";
          dragStart(row, idx, showMain, e.clientY);
          row.setPointerCapture(e.pointerId);
          dragMove(e.clientY);
        }
        return;
      }
      if (!lpTimer) return;
      if (Math.abs(my) > 12 && Math.abs(my) > Math.abs(mx)) { clearTimeout(lpTimer); lpTimer = null; return; } // 交给滚动
      if (mx < -14 && Math.abs(mx) > Math.abs(my) * 1.6) {  // 左滑
        clearTimeout(lpTimer); lpTimer = null;
        mode = "swipe";
        row.setPointerCapture(e.pointerId);
        row.style.transition = "none";
      } else if (Math.abs(mx) > 12) { clearTimeout(lpTimer); lpTimer = null; }
      return;
    }
    if (mode === "swipe") {
      dx = Math.min(0, mx);
      row.style.transform = "translateX(" + dx + "px)";
      row.style.opacity = String(Math.max(0.3, 1 + dx / 200));
    } else if (mode === "drag") {
      dragMove(e.clientY);
    }
  });
  const finish = (e) => {
    down = false;
    clearTimeout(lpTimer); lpTimer = null;
    if (mode === "swipe") {
      row.style.transition = "transform .2s, opacity .2s";
      if (dx < -80) {
        row.style.transform = "translateX(-100%)"; row.style.opacity = "0";
        setTimeout(() => removeSwiped(q0, s0, showMain), 180);
      } else {
        row.style.transform = ""; row.style.opacity = "";
      }
    } else if (mode === "drag") {
      plDragged = true;
      dragEnd();
    } else {
      // 轻点: 交给 click
    }
    mode = null;
    document.body.style.touchAction = "";
  };
  row.addEventListener("pointerup", finish);
  row.addEventListener("pointercancel", finish);
}

// 拖拽排序: 悬浮跟手 + 指示线 + 松手数据驱动落位
const drag = { active: false, row: null, ghost: null, line: null, from: 0, main: false, oy: 0, target: -1, pid: -1 };
function dragStart(row, idx, showMain, clientY) {
  drag.active = true;
  drag.row = row;
  drag.from = idx;
  drag.main = !!showMain;
  drag.oy = clientY - row.getBoundingClientRect().top;
  drag.target = idx;
  // ghost 挂进滚动容器内用文档坐标定位: 避免 WebView2 高DPI 下 fixed 坐标系错位
  const box = $("pl-list");
  const r = row.getBoundingClientRect();
  const br = box.getBoundingClientRect();
  const g = row.cloneNode(true);
  g.classList.remove("pl-row", "playing", "dragging");
  g.classList.add("pl-ghost");
  g.style.left = (r.left - br.left + box.scrollLeft) + "px";
  g.style.width = r.width + "px";
  g.style.top = (r.top - br.top + box.scrollTop) + "px";
  box.appendChild(g);
  drag.ghost = g;
  // 独立指示线层级高于悬浮卡片, 避免红线被 ghost 背景覆盖
  const line = document.createElement("div");
  line.className = "pl-drop-line";
  box.appendChild(line);
  drag.line = line;
  row.classList.add("dragging");
  dragMove(clientY);  // 起拖立即显示指示线
}
function dragMove(clientY) {
  if (!drag.active) return;
  const box = $("pl-list");
  const all = [...box.querySelectorAll(".pl-row")];   // 含被拖行(占位), 用于命中与画线
  const rows = all.filter((r) => r !== drag.row);     // 排除被拖行, 用于换算插入下标
  const br = box.getBoundingClientRect();
  drag.ghost.style.top = (clientY - drag.oy - br.top + box.scrollTop) + "px";
  if (clientY < br.top + 40) box.scrollTop -= 10;
  else if (clientY > br.bottom - 40) box.scrollTop += 10;
  all.forEach((r) => r.classList.remove("drop-above", "drop-below"));
  let target = rows.length, lineRow = null, below = true;
  for (let i = 0; i < all.length; i++) {
    const r = all[i];
    const rect = r.getBoundingClientRect();
    if (clientY < rect.bottom) {
      const upper = clientY < rect.top + rect.height / 2;
      lineRow = r; below = !upper;
      // 被拖行占位自身: 上/下半都对应它原槽位; 其它行: 上半=该行前, 下半=该行后
      target = r === drag.row ? i : rows.indexOf(r) + (upper ? 0 : 1);
      break;
    }
  }
  drag.target = target;
  if (lineRow) lineRow.classList.add(below ? "drop-below" : "drop-above");
  else if (all.length) all[all.length - 1].classList.add("drop-below");
  const anchor = lineRow || all[all.length - 1];
  if (anchor && drag.line) {
    const ar = anchor.getBoundingClientRect();
    const edge = lineRow ? (below ? ar.bottom : ar.top) : ar.bottom;
    // 与 ghost 一样使用滚动容器文档坐标, 红线居中落在插入边界上
    drag.line.style.top = (edge - br.top + box.scrollTop - 1) + "px";
    drag.line.style.display = "block";
  } else if (drag.line) {
    drag.line.style.display = "none";
  }
}
function dragEnd() {
  if (!drag.active) return;
  drag.active = false;
  const { row, ghost, line, from, main, target } = drag;
  drag.row = null; drag.ghost = null; drag.line = null;
  if (ghost) ghost.remove();
  if (line) line.remove();
  document.querySelectorAll(".pl-row").forEach((r) => r.classList.remove("drop-above", "drop-below", "dragging"));
  const q = main ? state.queue : aq();
  // target 即"排除被拖行"序列中的插入下标, 直接数据驱动落位
  if (target >= 0 && target !== from && from < q.length) {
    const [moved] = q.splice(from, 1);
    q.splice(target, 0, moved);
    // 修正当前播放下标
    const fix = (v) => {
      if (v === from) return target;
      let p = v > from ? v - 1 : v;
      if (p >= target) p += 1;
      return p;
    };
    if (main && state.feedMode) state.mainIndex = fix(state.mainIndex);
    else if (!main || !state.feedMode) state.index = fix(state.index);
    saveQueue();
  }
  renderQueue();
}

// ---------------- 长按上下文菜单(搜索结果行) ----------------
let ctxSong = null;
// 共享菜单挂载点: 全屏时必须在全屏元素内才可见, 否则回到 body;
// 投币菜单曾把节点移入全屏元素, 所有菜单打开前都必须纠正挂载位置
function ensureCtxMount() {
  const menu = $("ctx-menu");
  const host = document.fullscreenElement || document.body;
  if (menu.parentElement !== host) host.appendChild(menu);
  return menu;
}
function openCtxMenu(s, opts) {
  ctxSong = s;
  const menu = ensureCtxMount();
  menu.innerHTML = "";
  const items = [
    { t: "▶ 立即播放", fn: () => playNow(s, opts.songs) },
    { t: "＋ 加入播放列表", fn: () => addToQueue(s) },
    { t: "★ 收藏到歌单", fn: () => collectSong(s) },
    { t: "⬇ 下载", fn: () => downloadSong(s) },
  ];
  if (opts.playlist) items.push({ t: "⇓ 从此处全部加入", fn: () => $("add-all") && $("add-all").click() });
  if (opts.local && opts.pid) items.push({ t: "✕ 从本歌单移除", fn: () => removeCollected(opts.pid, s), danger: true });
  items.forEach((it) => {
    const d = document.createElement("div");
    d.className = "ctx-item" + (it.danger ? " danger" : "");
    d.textContent = it.t;
    d.addEventListener("click", () => { hideCtx(); it.fn(); });
    menu.appendChild(d);
  });
  menu.classList.remove("hidden");
  const r = lastTouchPos;
  menu.style.left = Math.min(r.x, innerWidth - 190) + "px";
  menu.style.top = Math.max(10, r.y - menu.offsetHeight - 8) + "px";
}
function hideCtx() {
  const menu = $("ctx-menu");
  menu.classList.add("hidden");
  ctxSong = null;
  // 关闭菜单时把可能残留在全屏元素内的共享菜单归还 body
  if (!document.fullscreenElement && menu.parentElement !== document.body)
    document.body.appendChild(menu);
}
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest("#ctx-menu")) hideCtx();
}, true);
let lastTouchPos = { x: innerWidth / 2, y: innerHeight / 2 };
document.addEventListener("pointermove", (e) => { lastTouchPos = { x: e.clientX, y: e.clientY }; }, { passive: true });
document.addEventListener("pointerdown", (e) => { lastTouchPos = { x: e.clientX, y: e.clientY }; }, { passive: true });

// 长按检测: 500ms 静止
function bindLongPress(el, fn) {
  let timer = null, sx = 0, sy = 0;
  el.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse") {
      // 鼠标右键也弹菜单
      return;
    }
    sx = e.clientX; sy = e.clientY;
    timer = setTimeout(() => { timer = null; fn(); }, 550);
  });
  el.addEventListener("pointermove", (e) => {
    if (timer && (Math.abs(e.clientX - sx) > 10 || Math.abs(e.clientY - sy) > 10)) {
      clearTimeout(timer); timer = null;
    }
  });
  el.addEventListener("pointerup", () => { if (timer) { clearTimeout(timer); timer = null; } });
  el.addEventListener("contextmenu", (e) => { e.preventDefault(); fn(); });
}

// ---------------- 播放条 ----------------
$("btn-play").addEventListener("click", () => {
  if (!audio.src) { if (aq().length) playAt(Math.max(0, state.index)); return; }
  audio.paused ? audio.play() : audio.pause();
});
$("btn-prev").addEventListener("click", prev);
$("btn-next").addEventListener("click", next);
$("btn-mode").addEventListener("click", () => {
  const order = ["loop", "single", "order", "shuffle"];
  state.mode = order[(order.indexOf(state.mode) + 1) % order.length];
  $("btn-mode").innerHTML = MODES[state.mode].icon;
  $("btn-mode").title = MODES[state.mode].name;
  toast(MODES[state.mode].name);
});

audio.addEventListener("play", () => { state.playing = true; setPlayIcon(true); desktopPush(true); });
audio.addEventListener("pause", () => { state.playing = false; setPlayIcon(false); savePos(); desktopPush(true); });
audio.addEventListener("loadedmetadata", () => {
  const t = parseFloat(audio.dataset.resume || "0");
  if (t > 3 && audio.duration && t < audio.duration - 3) audio.currentTime = t;
});
audio.addEventListener("timeupdate", () => {
  if (seekDrag.on) return;
  const d = audio.duration || 0;
  $("t-cur").textContent = fmt(audio.currentTime);
  $("t-dur").textContent = fmt(d);
  $("seek-fill").style.width = (d ? (audio.currentTime / d * 100) : 0) + "%";
  syncLyric();
  syncLoSeek();
  desktopPush(false);
  // 每 5 秒记录一次播放进度
  const now = Date.now();
  if (d && audio.currentTime > 3 && now - lastPosSave > 5000) {
    lastPosSave = now;
    settings.pos = { key: audio.dataset.key, time: audio.currentTime };
    saveSettings();
  }
});
audio.addEventListener("seeked", () => {
  if (asr.active) { asr.lines = []; asr.partial = ""; }  // 跳转后旧识别行时间轴失效, 从新位置重新积累
  desktopPush(true);
});
audio.addEventListener("ratechange", () => desktopPush(true));
audio.addEventListener("ended", () => {
  settings.pos = null;  // 播完不再恢复
  if (state.mode === "single") { audio.currentTime = 0; audio.play(); return; }
  const i = nextIndex(1);
  if (i >= 0) { playAt(i); saveQueue(); }
});
let skipGuard = 0;
audio.addEventListener("error", () => {
  if (!audio.src) return;
  const s = curSong();
  const restricted = s && (s.source === "netease" || s.source === "qq") &&
    s.extra && s.extra.vip;
  toast(restricted
    ? "「" + s.title + "」需要 VIP 或付费权限，当前非会员无法播放，已跳过"
    : "「" + (s ? s.title : "") + "」播放失败，已跳过", restricted ? 5000 : 2200);
  // 防止全部失败时死循环
  const now = Date.now();
  if (now - skipGuard < 1500) { skipGuard = now; return; }
  skipGuard = now;
  const version = playVersion, queue = aq();
  clearTimeout(skipTimer);
  skipTimer = setTimeout(() => {
    skipTimer = null;
    if (version !== playVersion || aq() !== queue || curSong() !== s) return;
    const i = nextIndex(1);
    if (i >= 0 && i !== state.index) playAt(i);
  }, 600);
});

// 进度条拖动 (Pointer Events, 鼠标/触摸统一)
const seekBar = $("seek-bar");
const seekDrag = { on: false, pid: null };
function seekFromEvent(e) {
  const r = seekBar.getBoundingClientRect();
  const x = Math.min(Math.max(e.clientX - r.left, 0), r.width);
  const ratio = r.width ? x / r.width : 0;
  $("seek-fill").style.width = ratio * 100 + "%";
  $("t-cur").textContent = fmt(ratio * (audio.duration || 0));
  const tip = $("seek-tip");
  tip.textContent = fmt(ratio * (audio.duration || 0));
  tip.style.left = x + "px";
  tip.classList.add("show");
  return ratio;
}
seekBar.addEventListener("pointerdown", (e) => {
  if (!audio.duration) return;
  seekDrag.on = true;
  seekDrag.pid = e.pointerId;
  seekBar.setPointerCapture(e.pointerId);
  seekBar.classList.add("dragging");
  seekDrag.ratio = seekFromEvent(e);
});
seekBar.addEventListener("pointermove", (e) => {
  if (seekDrag.on) seekDrag.ratio = seekFromEvent(e);
});
seekBar.addEventListener("pointerup", (e) => {
  if (!seekDrag.on) return;
  seekDrag.on = false;
  seekBar.classList.remove("dragging");
  $("seek-tip").classList.remove("show");
  if (audio.duration) audio.currentTime = seekDrag.ratio * audio.duration;
});
seekBar.addEventListener("pointercancel", () => {
  seekDrag.on = false;
  seekBar.classList.remove("dragging");
  $("seek-tip").classList.remove("show");
});

// 音量
$("btn-vol").addEventListener("click", (e) => {
  e.stopPropagation();
  $("vol-pop").classList.toggle("hidden");
});
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest("#vol-pop") && !e.target.closest("#btn-vol")) $("vol-pop").classList.add("hidden");
});
const volTrack = $("vol-track");
let volDrag = false;
function volFromEvent(e) {
  const r = volTrack.getBoundingClientRect();
  const y = Math.min(Math.max(r.bottom - e.clientY, 0), r.height);
  const v = r.height ? y / r.height : 0;
  audio.volume = v;
  audio.muted = false;
  $("vol-fill").style.height = v * 100 + "%";
  $("vol-val").textContent = Math.round(v * 100);
  settings.volume = v;
  saveSettings();
}
volTrack.addEventListener("pointerdown", (e) => {
  volDrag = true;
  volTrack.setPointerCapture(e.pointerId);
  volFromEvent(e);
});
volTrack.addEventListener("pointermove", (e) => { if (volDrag) volFromEvent(e); });
volTrack.addEventListener("pointerup", () => { volDrag = false; });

// ---------------- 侧栏 ----------------
function clearSrcPlActive() {
  document.querySelectorAll(".src-pl").forEach((x) => x.classList.remove("active"));
}
// 音源默认空状态: 显示当前音源 logo
function srcEmptyHtml() {
  return '<div class="empty-ic src-logo-lg">' + (SRC_LOGO[state.source] || "🎵") + "</div>";
}
// B站推荐视频: 选中B站音源时中栏直接展示热门视频(网格排布, 点击打开视频, 触底分页加载)
let recPage = 0, recEnd = false, recLoading = false, recSongs = [];
function recCardEl(s) {
  const card = document.createElement("div");
  card.className = "vg-card";
  const ex = s.extra || {};
  // B站官方风格图标: 播放量为圆角三角, 点赞为拇指
  const icPlay = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M4.5 3.05v9.9a.5.5 0 0 0 .76.42l8.3-4.95a.5.5 0 0 0 0-.84l-8.3-4.95a.5.5 0 0 0-.76.42z"/></svg>';
  const icLike = '<svg viewBox="2 2 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M7 10v11H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3Zm0 0 4.2-7a2 2 0 0 1 3.7 1.3L13.8 9H19a2 2 0 0 1 2 2.4l-1.3 7A2 2 0 0 1 17.8 20H7"/></svg>';
  const sub = esc(s.artist || "") +
    (ex.view != null ? ' <span class="vg-stat">' + icPlay + fmtCount(ex.view) + "</span>" : "") +
    (ex.like != null ? ' <span class="vg-stat">' + icLike + fmtCount(ex.like) + "</span>" : "");
  card.innerHTML =
    '<div class="vg-cover">' +
    (s.cover ? '<img loading="lazy" src="' + esc(coverSrc(s)) + '" onerror="this.parentNode.innerHTML=\'♪\'">' : "♪") +
    (s.duration ? '<span class="vg-dur">' + fmt(s.duration) + "</span>" : "") +
    "</div>" +
    '<div class="vg-title">' + esc(s.title) + "</div>" +
    '<div class="vg-sub">' + sub + "</div>";
  card.addEventListener("click", () => {
    hideCardPlayer();
    playNow(s, recSongs);
    openLyric();
  });
  bindLongPress(card, () => openCtxMenu(s, { songs: recSongs }));
  bindCardHover(card, s);
  return card;
}
async function loadMoreRec() {
  if (recLoading || recEnd) return;
  recLoading = true;
  try {
    const j = await api("/api/bili/recommend?limit=20&pn=" + (recPage + 1));
    const songs = j.songs || [];
    if (songs.length) {
      recPage++;
      const grid = $("list").querySelector(".video-grid");
      for (const s of songs) { recSongs.push(s); grid.appendChild(recCardEl(s)); }
      // 最多同时显示100个推荐, 超出时移除最上方的卡片
      while (recSongs.length > 100) {
        recSongs.shift();
        if (grid.firstChild) grid.removeChild(grid.firstChild);
      }
    }
    if (!songs.length || j.no_more) recEnd = true;
  } catch (e) { recEnd = true; }
  recLoading = false;
}
async function showBiliRecommend() {
  const view = state.view = { type: "bili_rec" };
  setCrumb("");
  showEmpty('<div class="loading">推荐加载中…</div>');
  recPage = 0; recEnd = false; recLoading = false; recSongs = [];
  try {
    const j = await api("/api/bili/recommend?limit=20&pn=1");
    if (state.view !== view) return;
    const songs = j.songs || [];
    if (!songs.length) { showEmpty(srcEmptyHtml()); return; }
    recPage = 1;
    recSongs = songs.slice();
    if (j.no_more) recEnd = true;
    $("empty").classList.add("hidden");
    const list = $("list");
    list.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "video-grid";
    for (const s of songs) grid.appendChild(recCardEl(s));
    list.appendChild(grid);
  } catch (e) {
    showEmpty(srcEmptyHtml());
  }
}
$("content").addEventListener("scroll", () => {
  hideCardPlayer();
  if (!state.view || state.view.type !== "bili_rec") return;
  const el = $("content");
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 160) loadMoreRec();
});

// 推荐卡片内嵌预览播放器: 悬停时封面变视频, 自带弹幕/声音/进度条/触控手势/控件自动隐藏
const CARD_IC = {
  play: '<svg viewBox="7 4 13 16" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  pause: '<svg viewBox="6 4 12 16" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  dm: '<svg viewBox="0 0 24 24"><rect x="2.5" y="4.5" width="19" height="15" rx="3.5" fill="none" stroke="currentColor" stroke-width="1.8"/><text x="12" y="16" text-anchor="middle" font-size="10" font-weight="700" fill="currentColor">弹</text></svg>',
  vol: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3z"/><path d="M16 8a4 4 0 0 1 0 8" fill="none" stroke="currentColor" stroke-width="2"/></svg>',
  mute: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 9v6h4l5 5V4L7 9H3z" fill="currentColor"/><path d="M16 9l5 5M21 9l-5 5"/></svg>',
};
let vgTimer = null, cardP = null;

function hideCardPlayer() {
  if (vgTimer) { clearTimeout(vgTimer); vgTimer = null; }
  if (!cardP) return;
  const p = cardP; cardP = null;
  if (p.dmTimer) clearInterval(p.dmTimer);
  if (p.hideTimer) clearTimeout(p.hideTimer);
  try { p.v.pause(); p.v.removeAttribute("src"); p.v.load(); } catch (e) {}
  if (p.el && p.el.parentNode) p.el.parentNode.removeChild(p.el);
  if (p.v && p.v.parentNode) p.v.parentNode.removeChild(p.v);
  if (p.img) p.img.style.visibility = "";
}

function cardSpawnDm(p, d) {
  if (p.mode === "off") return;
  const W = p.cover.clientWidth, el = document.createElement("div");
  el.className = "dm-item";
  el.textContent = d.text;
  el.style.color = (d.color && d.color !== 16777215) ? colorHex(d.color) : "#fff";
  el.style.fontSize = Math.min(18, Math.max(12, (d.size || 25) * 0.5)) + "px";
  const fixed = (d.mode === 4 || d.mode === 5);
  if (fixed) {
    const key = d.mode === 5 ? "top" : "bottom";
    const lanes = p.fixedLanes[key] || (p.fixedLanes[key] = {});
    let lane = 0;
    while (lanes[lane] && lanes[lane] > Date.now()) lane++;
    if (lane > 5) return;
    lanes[lane] = Date.now() + 4200;
    el.style.left = "0"; el.style.right = "0"; el.style.textAlign = "center"; el.style.transform = "none";
    if (key === "top") el.style.top = (4 + lane * 20) + "px"; else el.style.bottom = (30 + lane * 20) + "px";
    el.classList.add(key === "top" ? "dm-top" : "dm-bottom");
    el.style.animationDuration = "4.2s";
    p.dm.appendChild(el);
    setTimeout(() => el.remove(), 4400);
    return;
  }
  const lanes = Math.max(3, Math.floor(p.cover.clientHeight * (p.mode === "half" ? 0.5 : 0.7) / 22));
  if (p.scrollLanes.length < lanes) { const nl = new Array(lanes).fill(0); for (let i = 0; i < p.scrollLanes.length; i++) nl[i] = p.scrollLanes[i]; p.scrollLanes = nl; }
  let lane = -1;
  for (let i = 0; i < p.scrollLanes.length; i++) if (p.scrollLanes[i] <= Date.now()) { lane = i; break; }
  if (lane < 0) lane = p.scrollLanes.length - 1;
  const speed = 90;
  el.style.top = (4 + lane * 22) + "px";
  el.classList.add("dm-scroll");
  p.dm.appendChild(el);
  const dur = (W + el.offsetWidth + 20) / speed;
  el.style.setProperty("--dm-travel", (W + el.offsetWidth) + "px");
  el.style.animationDuration = dur.toFixed(2) + "s";
  p.scrollLanes[lane] = Date.now() + dur * 1000 * 0.4;
  setTimeout(() => el.remove(), dur * 1000 + 300);
}
function cardTick(p) {
  if (!p.dmList.length) return;
  const t = p.v.currentTime;
  if (p.dmLastT < 0 || Math.abs(t - p.dmLastT) > 1.2) {
    p.dmPtr = 0;
    while (p.dmPtr < p.dmList.length && p.dmList[p.dmPtr].time < t) p.dmPtr++;
    p.dm.innerHTML = "";
  }
  p.dmLastT = t;
  if (p.v.paused) return;
  let guard = 0;
  while (p.dmPtr < p.dmList.length && p.dmList[p.dmPtr].time <= t && guard++ < 10) { cardSpawnDm(p, p.dmList[p.dmPtr]); p.dmPtr++; }
}
function cardShowCtl(p) {
  p.el.classList.add("show");
  if (p.hideTimer) clearTimeout(p.hideTimer);
  p.hideTimer = setTimeout(() => p.el.classList.remove("show"), 3000);
}
function cardToggleDm(p) {
  p.mode = p.mode === "full" ? "half" : p.mode === "half" ? "off" : "full";
  p.dmBtn.classList.toggle("on", p.mode !== "off");
  p.dm.classList.toggle("half", p.mode === "half");
  if (p.mode === "off") p.dm.innerHTML = "";
  cardShowCtl(p);
}
function cardToggleMute(p) {
  p.muted = !p.muted;
  p.v.muted = p.muted;
  p.volBtn.innerHTML = p.muted ? CARD_IC.mute : CARD_IC.vol;
  p.volBtn.classList.toggle("on", !p.muted);
  cardShowCtl(p);
}
function cardTogglePlay(p) {
  if (p.v.paused) p.v.play().catch(() => {}); else p.v.pause();
  p.ppBtn.innerHTML = p.v.paused ? CARD_IC.play : CARD_IC.pause;
  cardShowCtl(p);
}
function cardSeekRatio(p, clientX) {
  const r = p.seekBar.getBoundingClientRect();
  return Math.min(Math.max((clientX - r.left) / r.width, 0), 1);
}

function createCardPlayer(card, s) {
  const cover = card.querySelector(".vg-cover");
  if (!cover) return null;
  const img = cover.querySelector("img");
  if (img) img.style.visibility = "hidden";
  const el = document.createElement("div");
  el.className = "vg-player show";
  el.innerHTML =
    '<div class="vg-dm"></div>' +
    '<div class="vg-ctl">' +
      '<div class="vg-seek"><div class="vg-seek-fill"></div></div>' +
      '<div class="vg-row">' +
        '<button class="vg-btn vg-pp">' + CARD_IC.pause + '</button>' +
        '<button class="vg-btn vg-dmb on">' + CARD_IC.dm + '</button>' +
        '<button class="vg-btn vg-vol">' + CARD_IC.mute + '</button>' +
        '<span class="vg-time">0:00</span>' +
      '</div>' +
    '</div>';
  const v = document.createElement("video");
  v.className = "vg-video"; v.muted = true; v.playsInline = true; v.preload = "auto";
  cover.appendChild(v); cover.appendChild(el);
  const p = {
    card, s, cover, img, v, el, mode: "full", muted: true,
    dm: el.querySelector(".vg-dm"), fill: el.querySelector(".vg-seek-fill"), seekBar: el.querySelector(".vg-seek"),
    ppBtn: el.querySelector(".vg-pp"), dmBtn: el.querySelector(".vg-dmb"), volBtn: el.querySelector(".vg-vol"),
    timeEl: el.querySelector(".vg-time"),
    dmList: [], dmPtr: 0, dmLastT: -1, dmTimer: null, scrollLanes: [], fixedLanes: {}, hideTimer: null, lastTap: 0,
  };
  v.src = withTok("/proxy?source=bilibili&id=" + encodeURIComponent(s.id) +
          "&kind=video&qn=32&extra=" + encodeURIComponent(JSON.stringify(s.extra || {})));
  v.play().catch(() => {});
  v.addEventListener("timeupdate", () => {
    const d = v.duration || 0;
    p.fill.style.width = (d ? (v.currentTime / d) * 100 : 0) + "%";
    p.timeEl.textContent = fmt(v.currentTime) + (d ? " / " + fmt(d) : "");
  });
  v.addEventListener("play", () => { p.ppBtn.innerHTML = CARD_IC.pause; });
  v.addEventListener("pause", () => { p.ppBtn.innerHTML = CARD_IC.play; });
  p.dmTimer = setInterval(() => cardTick(p), 200);
  (async () => {
    try {
      const url = "/api/bili/danmaku?id=" + encodeURIComponent(s.id) + "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {}));
      const j = await (await fetch(url)).json();
      p.dmList = j.danmaku || [];
    } catch (e) {}
  })();
  // 控制条按钮
  p.ppBtn.addEventListener("pointerdown", (e) => { e.stopPropagation(); cardTogglePlay(p); });
  p.dmBtn.addEventListener("pointerdown", (e) => { e.stopPropagation(); cardToggleDm(p); });
  p.volBtn.addEventListener("pointerdown", (e) => { e.stopPropagation(); cardToggleMute(p); });
  // 进度条拖动
  const sb = p.seekBar;
  sb.addEventListener("pointerdown", (e) => {
    e.stopPropagation(); sb.setPointerCapture(e.pointerId); sb._on = true;
    const r = cardSeekRatio(p, e.clientX); v.currentTime = r * (v.duration || 0);
  });
  sb.addEventListener("pointermove", (e) => {
    if (!sb._on) return; e.stopPropagation();
    const r = cardSeekRatio(p, e.clientX); p.fill.style.width = r * 100 + "%";
  });
  const sbEnd = (e) => { if (sb._on) { sb._on = false; try { sb.releasePointerCapture(e.pointerId); } catch (err) {} } };
  sb.addEventListener("pointerup", sbEnd); sb.addEventListener("pointercancel", sbEnd);
  // 视频区触控手势(除评论区外): 横向 seek / 单击打开视频 / 左右半屏竖向调亮度音量
  bindCardGesture(p);
  // 播放器内: 控制条点击不外泄; 拖动后的合成 click 抑制; 视频区点击冒泡到卡片 -> 打开视频
  el.addEventListener("click", (e) => {
    if (e.target.closest(".vg-ctl")) { e.stopPropagation(); return; }
    if (p._suppress) { p._suppress = false; e.stopPropagation(); }
  });
  el.addEventListener("mousemove", () => cardShowCtl(p));
  el.addEventListener("mouseenter", () => cardShowCtl(p));
  cardShowCtl(p);
  return p;
}

function bindCardGesture(p) {
  const v = p.v, el = p.el;
  const st = { id: null, x0: 0, y0: 0, mode: null, moved: false, base: 0, bright: 1 };
  el.addEventListener("pointerdown", (e) => {
    if (e.target !== v && e.target !== el && !e.target.classList.contains("vg-dm")) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    st.id = e.pointerId; st.x0 = e.clientX; st.y0 = e.clientY; st.mode = null; st.moved = false;
    st.base = v.duration ? v.currentTime / v.duration : 0;
    try { el.setPointerCapture(e.pointerId); } catch (err) {}
  });
  el.addEventListener("pointermove", (e) => {
    if (st.id !== e.pointerId) return;
    const dx = e.clientX - st.x0, dy = st.y0 - e.clientY;
    if (!st.mode) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      st.mode = Math.abs(dx) > Math.abs(dy) ? "seek" : "vert"; st.moved = true;
    }
    if (st.mode === "seek") {
      const d = v.duration || 0; if (!d) return;
      const ratio = Math.min(Math.max(st.base + dx * 0.7 / el.clientWidth, 0), 1);
      p.fill.style.width = ratio * 100 + "%"; st.ratio = ratio;
    } else {
      const delta = dy / el.clientHeight;
      if (e.clientX - el.getBoundingClientRect().left < el.clientWidth / 2) {
        st.bright = Math.min(Math.max(st.bright + delta, 0.3), 1.6);
        v.style.filter = "brightness(" + st.bright + ")";
      } else {
        let vol = Math.min(Math.max((v.muted ? 0.5 : v.volume) + delta, 0), 1);
        v.muted = false; v.volume = vol; p.muted = false;
        p.volBtn.innerHTML = CARD_IC.vol; p.volBtn.classList.add("on");
      }
      st.y0 = e.clientY;
    }
    cardShowCtl(p);
  });
  function end(e) {
    if (st.id !== e.pointerId) return;
    st.id = null;
    if (st.mode === "seek") { if (st.ratio != null) v.currentTime = st.ratio * (v.duration || 0); p._suppress = true; }
    else if (st.mode === "vert") { p._suppress = true; }
    else if (!st.moved) {
      const now = Date.now();
      if (now - p.lastTap < 320) { p.lastTap = 0; p._suppress = true; cardTogglePlay(p); } // 双击=暂停/播放
      else { p.lastTap = now; cardShowCtl(p); }  // 单击: 放行 click 冒泡 -> 打开视频
    }
    st.mode = null; st.moved = false;
  }
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

function bindCardHover(card, s) {
  card.addEventListener("pointerenter", (e) => {
    if (e.pointerType !== "mouse") return;
    if (vgTimer) clearTimeout(vgTimer);
    vgTimer = setTimeout(() => {
      vgTimer = null;
      if (cardP && cardP.card === card) return;
      hideCardPlayer();
      cardP = createCardPlayer(card, s);
    }, 600);
  });
  card.addEventListener("pointerleave", () => {
    if (vgTimer) { clearTimeout(vgTimer); vgTimer = null; }
    if (cardP && cardP.card === card) hideCardPlayer();
  });
}
document.querySelectorAll(".src-btn").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".src-btn").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    clearSrcPlActive();
    state.view = null;  // 退出下载管理等页面视图, 停止其轮询重渲染
    state.source = b.dataset.source;
    setCrumb("");
    if (state.source === "bilibili") showBiliRecommend();
    else showEmpty(srcEmptyHtml());
    $("search-input").value = "";
  });
});
document.querySelectorAll(".src-pl").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".src-btn").forEach((x) => x.classList.remove("active"));
    clearSrcPlActive();
    b.classList.add("active");
    openSourceLists(b.dataset.source);
  });
});
$("btn-mylists").addEventListener("click", openLocalLists);
$("btn-downloads").addEventListener("click", openDownloads);
$("btn-queue").addEventListener("click", () => {
  // 播放列表非常驻: 点击展开, 再次点击收起
  const on = document.body.classList.toggle("show-queue");
  $("btn-queue").classList.toggle("active", on);
  if (on) $("playlist-panel").scrollTop = 0;
});
$("pl-clear").addEventListener("click", async () => {
  const showMain = state.feedMode && state.plView === "main";
  const q = showMain ? state.queue : aq();
  if (!q.length) return;
  if (state.feedMode && !showMain) { state.feedQueue = []; state.index = -1; audio.pause(); audio.removeAttribute("src"); resetPlayerBar(); renderQueue(); return; }
  if (!await showConfirm("确定清空播放列表？")) return;
  state.queue = [];
  state.mainIndex = -1;
  // 推荐流中清空默认播放列表: 不打断正在播放的视频
  if (!state.feedMode) {
    state.index = -1;
    audio.pause();
    audio.removeAttribute("src");
    resetPlayerBar();
  }
  renderQueue();
  saveQueue();
});
// 右栏标签: 推荐流下切换查看"当前视频列表"/"默认播放列表"
$("pl-tabs").addEventListener("click", (e) => {
  const t = e.target.closest(".pl-tab");
  if (!t || !state.feedMode) return;
  if (state.plView === t.dataset.v) return;
  state.plView = t.dataset.v;
  renderQueue();
});
$("btn-search").addEventListener("click", () => doSearch(1));
$("search-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
$("search-clear").addEventListener("click", () => {
  $("search-input").value = "";
  $("search-input").focus();
});

// ---------------- 网易云登录 ----------------
$("btn-login").addEventListener("click", () => {
  $("login-modal").classList.remove("hidden");
  $("ne-logout").classList.toggle("hidden", !state.loggedIn);
  if (state.loggedIn) { $("qr-mask").innerHTML = "已登录 ✓"; }
  else refreshQR();
});
$("ne-logout").addEventListener("click", async () => {
  try {
    await fetch("/api/netease/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    toast("已退出网易云登录");
    $("ne-logout").classList.add("hidden");
    $("qr-mask").innerHTML = "";
    refreshLoginState();
  } catch (e) { toast("退出失败: " + e); }
});
$("login-close").addEventListener("click", () => {
  $("login-modal").classList.add("hidden");
  stopPoll();
});
document.querySelectorAll(".lt").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".lt").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("pane-qr").classList.toggle("hidden", t.dataset.tab !== "qr");
    $("pane-cookie").classList.toggle("hidden", t.dataset.tab !== "cookie");
    if (t.dataset.tab === "qr") stopPoll(), refreshQR(); else stopPoll();
  });
});
async function refreshQR() {
  $("qr-mask").innerHTML = "";
  $("qr-tip").textContent = "用网易云音乐 App 扫码";
  $("qr-img").src = withTok("/api/netease/qr?ts=" + Date.now());
  startPoll();
}
$("qr-refresh").addEventListener("click", refreshQR);
let pollTimer = null;
function startPoll() {
  stopPoll();
  pollTimer = setInterval(async () => {
    try {
      const j = await api("/api/netease/poll");
      if (j.code === 803) {
        stopPoll();
        $("qr-mask").innerHTML = "登录成功 ✓";
        refreshLoginState();
        setTimeout(() => $("login-modal").classList.add("hidden"), 900);
      } else if (j.code === 800 || j.code === 802) {
        $("qr-tip").textContent = { 800: "等待确认扫码…", 802: "已扫码，请在手机上确认" }[j.code];
      } else if (j.code === 805) {
        $("qr-mask").innerHTML = "二维码已过期<br>点击刷新";
        stopPoll();
      } else if (j.code === 8821) {
        $("qr-mask").innerHTML = "扫码登录被平台风控拦截";
        $("qr-tip").textContent = j.error || "请改用粘贴 Cookie 登录";
        stopPoll();
      } else if (j.error) {
        $("qr-tip").textContent = "登录状态查询失败：" + j.error;
      }
    } catch (e) {
      $("qr-tip").textContent = "登录状态查询失败，请检查网络后刷新二维码";
    }
  }, 2000);
}
function stopPoll() { clearInterval(pollTimer); pollTimer = null; }

// 在应用内打开网易云官方登录页：官方页面自带风控/验证码处理，绕开 8821
$("qr-native").addEventListener("click", async () => {
  const msg = $("qr-native-msg");
  const bridge = window.pywebview && window.pywebview.api;
  if (!bridge || !bridge.netease_login) {
    msg.textContent = "当前是浏览器模式，请在桌面客户端内使用";
    return;
  }
  msg.textContent = "已打开登录窗口，登录完成后会自动写入…";
  stopPoll();
  try {
    const r = await bridge.netease_login();
    if (r && r.started === false) {
      msg.textContent = r.message || "登录窗口已经打开了";
      return;
    }
  } catch (e) {
    msg.textContent = "打开登录窗口失败：" + e;
    return;
  }
  const timer = setInterval(async () => {
    try {
      const s = await api("/api/netease/status");
      if (s.logged_in) {
        clearInterval(timer);
        msg.textContent = "登录成功 ✓";
        refreshLoginState();
        setTimeout(() => $("login-modal").classList.add("hidden"), 900);
      }
    } catch (e) {}
  }, 1500);
  setTimeout(() => clearInterval(timer), 300000);
});
$("cookie-save").addEventListener("click", async () => {
  const raw = $("cookie-input").value.trim();
  if (!raw) { $("cookie-msg").textContent = "请粘贴 Cookie"; return; }
  $("cookie-msg").textContent = "保存中…";
  try {
    const j = await api("/api/netease/manual?x=1"); // 占位防缓存
  } catch (e) {}
  const r = await fetch("/api/netease/manual", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookie: raw }),
  });
  const j = await r.json();
  if (j.ok || j.logged_in) {
    $("cookie-msg").textContent = "登录成功";
    refreshLoginState();
    setTimeout(() => $("login-modal").classList.add("hidden"), 800);
  } else {
    $("cookie-msg").textContent = j.error || "保存失败，Cookie 可能无效";
  }
});
async function refreshLoginState() {
  try {
    const j = await api("/api/netease/status");
    state.loggedIn = !!(j.logged_in || j.nickname);
    const q = await api("/api/qq/status");
    state.qqLoggedIn = !!(q.logged_in || q.uin);
    const b = await api("/api/bili/status");
    state.biliLoggedIn = !!(b.logged_in);
    state.biliVip = !!(b.vip);
    const bl = $("btn-login"), bq = $("btn-qqlogin"), bb = $("btn-bililogin");
    bl.title = state.loggedIn ? "网易云账号（已登录）" : "网易云登录";
    bq.title = state.qqLoggedIn ? "QQ音乐账号（已登录）" : "QQ音乐登录";
    bb.title = state.biliLoggedIn ? ("哔哩哔哩账号（已登录" + (state.biliVip ? " · 大会员" : "") + "）") : "哔哩哔哩登录";
    bl.classList.toggle("logged-in", state.loggedIn);
    bq.classList.toggle("logged-in", state.qqLoggedIn);
    bb.classList.toggle("logged-in", state.biliLoggedIn);
    const nl = $("ne-logout");
    if (nl) nl.classList.toggle("hidden", !state.loggedIn);
  } catch (e) {}
}

// ---------------- QQ音乐登录 ----------------
$("btn-qqlogin").addEventListener("click", () => {
  $("qqlogin-modal").classList.remove("hidden");
  $("qq-cookie-msg").textContent = "";
  $("qq-native-msg").textContent = "";
  $("qq-logout").classList.toggle("hidden", !state.qqLoggedIn);
});
$("qq-native").addEventListener("click", async () => {
  const msg = $("qq-native-msg");
  const bridge = window.pywebview && window.pywebview.api;
  if (!bridge || !bridge.qq_login) {
    msg.textContent = "当前是浏览器模式，请在桌面客户端内使用";
    return;
  }
  msg.textContent = "已打开登录窗口，登录完成后会自动写入…";
  try {
    const r = await bridge.qq_login();
    if (r && r.started === false) {
      msg.textContent = r.message || "登录窗口已经打开了";
      return;
    }
  } catch (e) {
    msg.textContent = "打开登录窗口失败：" + e;
    return;
  }
  const timer = setInterval(async () => {
    try {
      const s = await api("/api/qq/status");
      if (s.logged_in) {
        clearInterval(timer);
        msg.textContent = "登录成功 ✓";
        refreshLoginState();
        setTimeout(() => $("qqlogin-modal").classList.add("hidden"), 900);
      }
    } catch (e) {}
  }, 1500);
  setTimeout(() => clearInterval(timer), 300000);
});
$("qqlogin-close").addEventListener("click", () => {
  $("qqlogin-modal").classList.add("hidden");
});
$("qq-cookie-save").addEventListener("click", async () => {
  const raw = $("qq-cookie-input").value.trim();
  if (!raw) { $("qq-cookie-msg").textContent = "请粘贴 Cookie"; return; }
  $("qq-cookie-msg").textContent = "保存中…";
  try {
    const r = await fetch("/api/qq/manual", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie: raw }),
    });
    const j = await r.json();
    if (j.ok || j.logged_in) {
      $("qq-cookie-msg").textContent = "登录成功";
      refreshLoginState();
      setTimeout(() => $("qqlogin-modal").classList.add("hidden"), 800);
    } else {
      $("qq-cookie-msg").textContent = j.error || "保存失败，Cookie 可能无效";
    }
  } catch (e) {
    $("qq-cookie-msg").textContent = "请求失败: " + e;
  }
});
$("qq-logout").addEventListener("click", async () => {
  try {
    await fetch("/api/qq/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    $("qq-cookie-msg").textContent = "已退出登录";
    $("qq-logout").classList.add("hidden");
    refreshLoginState();
  } catch (e) {}
});

// ---------------- 哔哩哔哩登录 ----------------
let biliPollTimer = null;
$("btn-bililogin").addEventListener("click", () => {
  $("bililogin-modal").classList.remove("hidden");
  $("bili-cookie-msg").textContent = "";
  $("bili-logout").classList.toggle("hidden", !state.biliLoggedIn);
  if (state.biliLoggedIn) { $("bili-qr-mask").innerHTML = "已登录 ✓"; }
  else refreshBiliQR();
});
$("bililogin-close").addEventListener("click", () => {
  $("bililogin-modal").classList.add("hidden");
  stopBiliPoll();
});
document.querySelectorAll(".blt").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".blt").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("bili-pane-qr").classList.toggle("hidden", t.dataset.tab !== "qr");
    $("bili-pane-cookie").classList.toggle("hidden", t.dataset.tab !== "cookie");
    if (t.dataset.tab === "qr") { stopBiliPoll(); refreshBiliQR(); } else stopBiliPoll();
  });
});
async function refreshBiliQR() {
  $("bili-qr-mask").innerHTML = "";
  $("bili-qr-tip").textContent = "用哔哩哔哩 App 扫码";
  $("bili-qr-img").src = withTok("/api/bili/qr?ts=" + Date.now());
  startBiliPoll();
}
$("bili-qr-refresh").addEventListener("click", refreshBiliQR);
function startBiliPoll() {
  stopBiliPoll();
  biliPollTimer = setInterval(async () => {
    try {
      const j = await api("/api/bili/poll");
      if (j.code === 0 && j.logged_in) {
        stopBiliPoll();
        $("bili-qr-mask").innerHTML = "登录成功 ✓";
        refreshLoginState();
        setTimeout(() => $("bililogin-modal").classList.add("hidden"), 900);
      } else if (j.code === 86038) {
        $("bili-qr-mask").innerHTML = "二维码已过期<br>点击刷新";
        stopBiliPoll();
      } else if (j.code === 86090) {
        $("bili-qr-tip").textContent = "已扫码，请在手机上确认";
      } else if (j.code === 86101) {
        $("bili-qr-tip").textContent = "等待扫码…";
      } else if (j.code === 0 && !j.logged_in) {
        stopBiliPoll();
        $("bili-qr-mask").innerHTML = "登录未完成";
        $("bili-qr-tip").textContent = j.message || "平台未返回登录凭证，请刷新二维码或改用 Cookie";
      } else if (j.code === -1 || j.error) {
        $("bili-qr-tip").textContent = "登录状态查询失败：" + (j.message || j.error || "请检查网络");
      }
    } catch (e) {
      $("bili-qr-tip").textContent = "登录状态查询失败，请检查网络后刷新二维码";
    }
  }, 2000);
}
function stopBiliPoll() { clearInterval(biliPollTimer); biliPollTimer = null; }
$("bili-cookie-save").addEventListener("click", async () => {
  const raw = $("bili-cookie-input").value.trim();
  if (!raw) { $("bili-cookie-msg").textContent = "请粘贴 Cookie"; return; }
  $("bili-cookie-msg").textContent = "保存中…";
  try {
    const r = await fetch("/api/bili/manual", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie: raw }),
    });
    const j = await r.json();
    if (j.ok || j.logged_in) {
      $("bili-cookie-msg").textContent = "登录成功";
      refreshLoginState();
      setTimeout(() => $("bililogin-modal").classList.add("hidden"), 800);
    } else {
      $("bili-cookie-msg").textContent = j.error || "保存失败，Cookie 可能无效";
    }
  } catch (e) {
    $("bili-cookie-msg").textContent = "请求失败: " + e;
  }
});
$("bili-logout").addEventListener("click", async () => {
  try {
    await fetch("/api/bili/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    $("bili-cookie-msg").textContent = "已退出登录";
    $("bili-logout").classList.add("hidden");
    refreshLoginState();
  } catch (e) {}
});

// ---------------- 自定义标题栏 ----------------
(function initTitlebar() {
  const bar = $("titlebar");
  if (!bar) return;
  const api = () => (window.pywebview && pywebview.api) ? pywebview.api : null;
  $("tb-min").addEventListener("click", () => { const a = api(); if (a) a.minimize(); });
  $("tb-close").addEventListener("click", () => { const a = api(); if (a) a.close(); });
  // 拖拽: 交给 Windows 原生标题栏拖拽循环(系统接管, 零延迟跟手, 鼠标/触控通用)
  bar.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".tb-btns")) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const a = api();
    if (a && a.start_drag) { e.preventDefault(); a.start_drag(); }
  });
  // 双击标题栏: 缩到任务托盘(与点 X 行为一致)
  bar.addEventListener("dblclick", (e) => {
    if (e.target.closest(".tb-btns")) return;
    const a = api(); if (a) a.close();
  });
})();

// ---------------- 歌词/视频覆盖层 ----------------
let lyricOpen = false;
let lyricLines = [];      // [{t, text}]
let lyricActive = -1;
let lyricUserScroll = 0;  // 用户手动滚动后暂停自动跟随的时间戳

// ---------------- 桌面歌词 ----------------
// 桌面歌词由 Windows 原生置顶窗口绘制。前端只发送歌词快照和低频进度
// 心跳；原生窗口会在心跳之间按播放速率外推时间，因此主窗口缩到托盘后仍能跟随。
const desktopLyrics = {
  enabled: false,
  locked: false,
  available: null,
  color: "#ff69b4",
  key: "",
  lines: [],
  lineRevision: 0,
  sentLineRevision: -1,
  lastPush: 0,
  pushTimer: null,
  statePromise: null,
};

function desktopApi() {
  return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
}

function rgbHex(c) {
  if (typeof c === "string") return c;
  if (Array.isArray(c) && c.length === 3) {
    return "#" + c.map((x) => ("0" + (x & 255).toString(16)).slice(-2)).join("");
  }
  return "";
}

function setDesktopButton() {
  const b = $("btn-desktop-lyrics");
  if (!b) return;
  b.classList.toggle("active", desktopLyrics.enabled);
  b.classList.toggle("unavailable", desktopLyrics.available === false);
  b.setAttribute("aria-pressed", desktopLyrics.enabled ? "true" : "false");
  b.title = desktopLyrics.enabled ? "关闭桌面歌词" : "桌面歌词";
  const u = $("btn-desktop-unlock");
  if (u) {
    u.classList.toggle("hidden", !desktopLyrics.enabled);
    u.title = desktopLyrics.locked ? "解锁桌面歌词" : "锁定桌面歌词";
    u.setAttribute("aria-label", u.title);
    // 锁图标随状态切换: 锁定=开锁图标(点击解锁), 未锁定=上锁图标(点击锁定)
    const ic = u.querySelector(".ic-lock-closed");
    const io = u.querySelector(".ic-lock-open");
    if (ic && io) {
      ic.classList.toggle("hidden", desktopLyrics.locked);
      io.classList.toggle("hidden", !desktopLyrics.locked);
    }
  }
  const lc = $("btn-lyric-color");
  if (lc) {
    lc.classList.toggle("hidden", !desktopLyrics.enabled);
    const swatch = lc.querySelector("span");
    if (swatch && desktopLyrics.color) swatch.style.background = desktopLyrics.color;
  }
}

// 应用内桌面歌词取色器: 用圆角深色面板替代浏览器原生白底直角取色器
const colorPicker = { hue: 330, sat: 1, val: 1, hex: "#ff69b4", dragging: false };

function hexToHsv(hex) {
  const s = String(hex || "").replace("#", "");
  if (!/^[0-9a-fA-F]{6}$/.test(s)) return null;
  const r = parseInt(s.slice(0, 2), 16) / 255;
  const g = parseInt(s.slice(2, 4), 16) / 255;
  const b = parseInt(s.slice(4, 6), 16) / 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d) {
    if (max === r) h = 60 * (((g - b) / d) % 6);
    else if (max === g) h = 60 * ((b - r) / d + 2);
    else h = 60 * ((r - g) / d + 4);
  }
  if (h < 0) h += 360;
  return { h, s: max ? d / max : 0, v: max };
}
function hsvToHex(h, s, v) {
  const c = v * s, x = c * (1 - Math.abs((h / 60) % 2 - 1)), m = v - c;
  let rgb;
  if (h < 60) rgb = [c, x, 0];
  else if (h < 120) rgb = [x, c, 0];
  else if (h < 180) rgb = [0, c, x];
  else if (h < 240) rgb = [0, x, c];
  else if (h < 300) rgb = [x, 0, c];
  else rgb = [c, 0, x];
  return "#" + rgb.map((n) => ("0" + Math.round((n + m) * 255).toString(16)).slice(-2)).join("");
}
function renderColorPicker() {
  const { hue, sat, val } = colorPicker;
  const hex = hsvToHex(hue, sat, val);
  colorPicker.hex = hex;
  $("color-sv").style.background =
    "linear-gradient(to top, #000, transparent), linear-gradient(to right, #fff, hsl(" + hue + " 100% 50%))";
  $("color-sv-cursor").style.left = (sat * 100) + "%";
  $("color-sv-cursor").style.top = ((1 - val) * 100) + "%";
  $("color-hue").value = String(Math.round(hue));
  $("color-preview").style.background = hex;
  $("color-hex").textContent = hex;
}
function openColorPicker() {
  const hsv = hexToHsv(desktopLyrics.color || "#ff69b4") || { h: 330, s: 1, v: 1 };
  colorPicker.hue = hsv.h;
  colorPicker.sat = hsv.s;
  colorPicker.val = hsv.v;
  renderColorPicker();
  $("color-modal").classList.remove("hidden");
}
function closeColorPicker() {
  $("color-modal").classList.add("hidden");
}
function setColorFromPointer(e) {
  const r = $("color-sv").getBoundingClientRect();
  colorPicker.sat = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  colorPicker.val = Math.min(1, Math.max(0, 1 - (e.clientY - r.top) / r.height));
  renderColorPicker();
}
function applyDesktopLyricsColor(hex) {
  desktopLyrics.color = hex;
  const swatch = $("btn-lyric-color").querySelector("span");
  if (swatch) swatch.style.background = hex;
  const api = desktopApi();
  if (api && typeof api.desktop_lyrics_color === "function") api.desktop_lyrics_color(hex);
}
function setupColorPicker() {
  const sv = $("color-sv");
  $("btn-lyric-color").addEventListener("click", openColorPicker);
  $("color-close").addEventListener("click", closeColorPicker);
  $("color-cancel").addEventListener("click", closeColorPicker);
  $("color-modal").addEventListener("click", (e) => { if (e.target.id === "color-modal") closeColorPicker(); });
  $("color-hue").addEventListener("input", (e) => { colorPicker.hue = Number(e.target.value); renderColorPicker(); });
  sv.addEventListener("pointerdown", (e) => {
    colorPicker.dragging = true;
    sv.setPointerCapture(e.pointerId);
    setColorFromPointer(e);
  });
  sv.addEventListener("pointermove", (e) => { if (colorPicker.dragging) setColorFromPointer(e); });
  sv.addEventListener("pointerup", () => { colorPicker.dragging = false; });
  sv.addEventListener("pointercancel", () => { colorPicker.dragging = false; });
  $("color-ok").addEventListener("click", () => {
    applyDesktopLyricsColor(colorPicker.hex);
    closeColorPicker();
  });
}
setupColorPicker();

function desktopStatusFor(s) {
  if (!s) return "idle";
  if (s.source === "bilibili") return "video";
  if (s.source === "local") return "none";
  return "loading";
}

function desktopBeginSong(s) {
  const key = songKey(s);
  if (desktopLyrics.key === key) return;
  desktopLyrics.key = key;
  desktopLyrics.lines = [];
  desktopLyrics.lineRevision++;
  desktopLyrics.sentLineRevision = -1;
  desktopLyrics.status = desktopStatusFor(s);
  desktopPush(true);
}

function desktopSetLines(s, lines, status) {
  if (!s || songKey(s) !== desktopLyrics.key) return;
  desktopLyrics.lines = Array.isArray(lines) ? lines.slice() : [];
  desktopLyrics.lineRevision++;
  desktopLyrics.status = status || (desktopLyrics.lines.length ? "ready" : "empty");
  desktopPush(true);
}

function desktopReset() {
  if (!desktopLyrics.enabled) return;
  desktopLyrics.key = "";
  desktopLyrics.lines = [];
  desktopLyrics.lineRevision++;
  desktopLyrics.sentLineRevision = -1;
  desktopLyrics.status = "idle";
  desktopPush(true);
}

function desktopPayload() {
  const s = curSong();
  const key = s ? songKey(s) : "";
  // curPos() intentionally falls back to settings.pos when the restored song
  // has not been loaded by the audio element yet.
  const t = s ? curPos() : 0;
  const p = {
    key: key,
    title: s ? (s.title || "") : "",
    artist: s ? (s.artist || "") : "",
    time: Number.isFinite(t) ? Math.max(0, t) : 0,
    duration: s ? ((audio.duration && Number.isFinite(audio.duration)) ? audio.duration : (s.duration || 0)) : 0,
    playing: !!(s && audio.src && !audio.paused),
    rate: Number.isFinite(audio.playbackRate) && audio.playbackRate > 0 ? audio.playbackRate : (settings.rate || 1),
    status: s ? (desktopLyrics.status || desktopStatusFor(s)) : "idle",
  };
  if (desktopLyrics.sentLineRevision !== desktopLyrics.lineRevision) {
    p.lines = desktopLyrics.lines.map((l) => ({ t: Number(l.t) || 0, text: String(l.text || "") }));
  }
  return p;
}

function desktopPush(force) {
  if (!desktopLyrics.enabled) return;
  const api = desktopApi();
  if (!api || typeof api.desktop_lyrics_update !== "function") {
    desktopLyrics.available = false;
    setDesktopButton();
    return;
  }
  const now = Date.now();
  if (!force && now - desktopLyrics.lastPush < 220) {
    if (!desktopLyrics.pushTimer) {
      desktopLyrics.pushTimer = setTimeout(() => {
        desktopLyrics.pushTimer = null;
        desktopPush(false);
      }, Math.max(20, 220 - (now - desktopLyrics.lastPush)));
    }
    return;
  }
  const body = desktopPayload();
  const rev = desktopLyrics.lineRevision;
  desktopLyrics.lastPush = now;
  Promise.resolve(api.desktop_lyrics_update(body)).then((r) => {
    if (r && r.available === false) {
      desktopLyrics.available = false;
      setDesktopButton();
      return;
    }
    desktopLyrics.available = true;
    if (desktopLyrics.lineRevision === rev) desktopLyrics.sentLineRevision = rev;
  }).catch(() => {
    desktopLyrics.available = false;
    setDesktopButton();
  });
}

function desktopReadState() {
  const api = desktopApi();
  if (!api || typeof api.desktop_lyrics_state !== "function") {
    desktopLyrics.available = false;
    setDesktopButton();
    return Promise.resolve(null);
  }
  if (desktopLyrics.statePromise) return desktopLyrics.statePromise;
  desktopLyrics.statePromise = Promise.resolve(api.desktop_lyrics_state()).then((st) => {
    if (!st || st.available === false) {
      desktopLyrics.available = false;
      setDesktopButton();
      return null;
    }
    desktopLyrics.available = true;
    desktopLyrics.enabled = !!st.enabled;
    desktopLyrics.locked = !!st.locked;
    if (st.color) desktopLyrics.color = rgbHex(st.color);
    desktopLyrics.sentLineRevision = -1;
    setDesktopButton();
    if (desktopLyrics.enabled) {
      const s = curSong();
      if (s) {
        if (desktopLyrics.key !== songKey(s)) desktopBeginSong(s);
        else desktopPush(true);
        loadLyrics(s);
      } else desktopPush(true);
    }
    return st;
  }).catch(() => {
    desktopLyrics.available = false;
    setDesktopButton();
    return null;
  }).finally(() => { desktopLyrics.statePromise = null; });
  return desktopLyrics.statePromise;
}

// 原生桌面歌词窗口的 ×/锁 与主界面联动: 仅在 enabled/locked 真的变化时
// 触发完整 desktopReadState, 避免轮询带来歌词重载和桌面窗口重绘。
async function desktopSyncState() {
  const api = desktopApi();
  if (!api || typeof api.desktop_lyrics_state !== "function") return;
  try {
    const st = await api.desktop_lyrics_state();
    if (!st || st.available === false) return;
    if (!!st.enabled === desktopLyrics.enabled && !!st.locked === desktopLyrics.locked) return;
    desktopReadState();
  } catch (e) {}
}

async function toggleDesktopLyrics() {
  const api = desktopApi();
  if (!api || typeof api.desktop_lyrics_enable !== "function") {
    toast("桌面歌词仅支持桌面版");
    desktopLyrics.available = false;
    setDesktopButton();
    return;
  }
  // The native window also has its own close control. Refresh the actual
  // native state first so clicking the player button reopens it immediately
  // after that control was used.
  let current = desktopLyrics.enabled;
  try {
    const actual = await api.desktop_lyrics_state();
    if (actual && actual.available !== false) {
      current = !!actual.enabled;
      desktopLyrics.enabled = current;
      desktopLyrics.locked = !!actual.locked;
      desktopLyrics.available = true;
      setDesktopButton();
    }
  } catch (e) {}
  const target = !current;
  try {
    const st = await api.desktop_lyrics_enable(target);
    if (!st || st.available === false || st.error) throw new Error((st && st.error) || "不可用");
    desktopLyrics.available = true;
    desktopLyrics.enabled = !!st.enabled;
    desktopLyrics.locked = !!st.locked;
    if (st.color) desktopLyrics.color = rgbHex(st.color);
    desktopLyrics.sentLineRevision = -1;
    setDesktopButton();
    if (desktopLyrics.enabled) {
      const s = curSong();
      if (s) {
        if (desktopLyrics.key !== songKey(s)) desktopBeginSong(s);
        else desktopPush(true);
        loadLyrics(s);
      } else desktopPush(true);
    }
  } catch (e) {
    desktopLyrics.available = false;
    setDesktopButton();
    toast("桌面歌词不可用");
  }
}

async function toggleDesktopLyricsLock() {
  const api = desktopApi();
  if (!api || typeof api.desktop_lyrics_lock !== "function") {
    toast("桌面歌词仅支持桌面版");
    return;
  }
  try {
    const st = await api.desktop_lyrics_lock();
    if (!st || st.available === false || st.error) throw new Error((st && st.error) || "不可用");
    desktopLyrics.available = true;
    desktopLyrics.locked = !!st.locked;
    setDesktopButton();
    toast(desktopLyrics.locked ? "桌面歌词已锁定" : "桌面歌词已解锁", 1200);
  } catch (e) {
    toast("桌面歌词锁定状态更新失败");
  }
}

function parseLRC(text) {
  const out = [];
  for (const rawLine of (text || "").split(/\r?\n/)) {
    const times = [];
    let rest = rawLine;
    const re = /\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;
    let m;
    while ((m = re.exec(rawLine))) {
      const ms = m[3] ? parseInt(m[3].padEnd(3, "0").slice(0, 3), 10) : 0;
      times.push(parseInt(m[1], 10) * 60 + parseInt(m[2], 10) + ms / 1000);
    }
    if (!times.length) continue;
    rest = rawLine.replace(re, "").trim();
    if (!rest) continue;
    for (const t of times) out.push({ t, text: rest });
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}

function renderLyric(lines) {
  const inner = $("lo-lyric-inner");
  lyricLines = lines;
  lyricActive = -1;
  if (!lines.length) {
    inner.innerHTML = '<div class="lo-empty">暂无歌词</div>';
    return;
  }
  inner.innerHTML = lines.map((l, i) =>
    '<div class="lo-line" data-i="' + i + '">' + esc(l.text) + '</div>').join("");
  inner.querySelectorAll(".lo-line").forEach((el) => {
    el.addEventListener("click", () => {
      const l = lyricLines[+el.dataset.i];
      if (l && audio.duration) audio.currentTime = l.t;
    });
  });
  syncLyric(true);
}

function syncLyric(force) {
  if (!lyricOpen || !lyricLines.length) return;
  const t = audio.currentTime;
  let idx = -1;
  for (let i = 0; i < lyricLines.length; i++) {
    if (lyricLines[i].t <= t + 0.2) idx = i; else break;
  }
  if (idx === lyricActive && !force) return;
  lyricActive = idx;
  const inner = $("lo-lyric-inner");
  const els = inner.children;
  for (let i = 0; i < els.length; i++) {
    els[i].classList.toggle("active", i === idx);
    els[i].classList.toggle("past", i < idx);
  }
  // 用户 3 秒内未手动滚动才自动跟随
  if (Date.now() - lyricUserScroll > 3000 && els[idx]) {
    els[idx].scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

const lyricCache = new Map();
let lyricLoadSeq = 0;

async function loadLyrics(s) {
  const inner = $("lo-lyric-inner");
  const seq = ++lyricLoadSeq;
  const key = s ? songKey(s) : "";
  if (asr.active && asr.key !== key) stopAsr();  // 切歌即停实时识别
  if (desktopLyrics.enabled && s && desktopLyrics.key !== key) desktopBeginSong(s);
  if (desktopLyrics.enabled && s && desktopLyrics.key === key) {
    desktopLyrics.status = desktopStatusFor(s);
    desktopPush(true);
  }
  if (!s || s.source === "local") {
    // 本地: 无平台歌词接口
    renderLyric([]);
    if (desktopLyrics.enabled && s) desktopSetLines(s, [], "none");
    return;
  }
  if (s.source === "bilibili") {
    // B站视频: 官方/AI字幕优先, 无字幕时系统音频回环实时识别兜底
    loadVideoSubtitle(s, seq);
    return;
  }
  if (lyricCache.has(key)) {
    const cached = lyricCache.get(key) || [];
    if (seq !== lyricLoadSeq || curSong() !== s) return;
    renderLyric(cached);
    if (desktopLyrics.enabled) desktopSetLines(s, cached, cached.length ? "ready" : "empty");
    return;
  }
  inner.innerHTML = '<div class="lo-empty">歌词加载中…</div>';
  try {
    const url = "/api/lyrics?source=" + encodeURIComponent(s.source) +
                "&id=" + encodeURIComponent(s.id) +
                "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {}));
    const j = await (await fetch(url)).json();
    // 歌词请求是独立异步任务: 切歌后即使旧请求晚到也不能污染桌面窗口。
    if (seq !== lyricLoadSeq || curSong() !== s) return;
    const lines = j.error ? [] : parseLRC(j.lyric);
    lyricCache.set(key, lines);
    renderLyric(lines);
    if (desktopLyrics.enabled) desktopSetLines(s, lines, lines.length ? "ready" : "empty");
  } catch (e) {
    if (seq !== lyricLoadSeq || curSong() !== s) return;
    lyricCache.set(key, []);
    renderLyric([]);
    if (desktopLyrics.enabled) desktopSetLines(s, [], "empty");
  }
}

// ---------------- B站视频字幕: 仅官方/AI 字幕, 无字幕显示「无字幕」 ----------------
const asr = { active: false, key: "", seq: 0, lines: [], partial: "", timer: null };

// 服务端识别段带墙钟时间戳: 距识别 N 秒前的句子 ≈ 播放进度 currentTime - N
function asrMapTime(segT) {
  return Math.max(0, (audio.currentTime || 0) - Math.max(0, Date.now() / 1000 - segT));
}
function asrApply() {
  const s = curSong();
  if (!asr.active || !s || songKey(s) !== asr.key) return;
  const lines = asr.lines.slice();
  // 连续讲话时 Vosk 迟迟不定稿, 必须把 partial 作为当前行, 否则桌面歌词长期空白
  if (asr.partial) lines.push({ t: Math.max(0, (audio.currentTime || 0) - 0.1), text: asr.partial });
  lines.sort((a, b) => a.t - b.t);
  if (desktopLyrics.enabled) desktopSetLines(s, lines, "ready");
  if (lyricOpen) renderLyric(lines);
}
async function asrTick() {
  const s = curSong();
  if (!asr.active || !s || songKey(s) !== asr.key) return;
  if (!desktopLyrics.enabled && !lyricOpen) { stopAsr(); return; }  // 无人消费即停
  try {
    const j = await api("/api/asr/poll?after=" + asr.seq);
    if (!asr.active || curSong() !== s) return;
    asr.seq = j.seq || asr.seq;
    const newPartial = audio.paused ? "" : (j.partial || "");
    let changed = newPartial !== asr.partial;
    asr.partial = newPartial;
    if (!audio.paused && (j.segments || []).length) {
      for (const seg of j.segments) asr.lines.push({ t: asrMapTime(seg.t), text: seg.text });
      if (asr.lines.length > 300) asr.lines = asr.lines.slice(-300);
      changed = true;
    }
    if (changed) asrApply();
    if (j.error) { stopAsr(); toast("实时识别失败: " + j.error, 3000); }
    else if (j.running === false) { stopAsr(); }
  } catch (e) {}
}
async function startAsr(s) {
  stopAsr();
  try {
    const r = await fetch("/api/asr/start", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
    const j = await r.json();
    if (j.error) { toast("实时字幕不可用: " + j.error, 3200); return; }
  } catch (e) { return; }
  asr.active = true; asr.key = songKey(s); asr.seq = 0; asr.lines = [];
  asr.timer = setInterval(asrTick, 1200);
  toast("该视频无字幕, 已开启语音实时识别", 2600);
}
function stopAsr() {
  if (asr.timer) { clearInterval(asr.timer); asr.timer = null; }
  if (asr.active) {
    asr.active = false;
    fetch("/api/asr/stop", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
  }
  asr.lines = []; asr.seq = 0; asr.key = ""; asr.partial = "";
}
async function loadVideoSubtitle(s, seq) {
  const key = songKey(s);
  // 已放弃 ASR/系统实时字幕: 无官方字幕时只显示「无字幕」
  stopAsr();
  if (lyricCache.has(key)) {
    const cached = lyricCache.get(key) || [];
    if (seq !== lyricLoadSeq || curSong() !== s) return;
    renderLyric(cached);
    if (desktopLyrics.enabled) {
      desktopSetLines(s, cached.length ? cached : [{ t: 0, text: "无字幕" }],
                      cached.length ? "ready" : "empty");
    }
    return;
  }
  try {
    const extra = s.extra || {};
    const j = await api("/api/subtitle?source=bilibili&id=" + encodeURIComponent(s.id) +
                        "&cid=" + encodeURIComponent(extra.cid || ""));
    if (seq !== lyricLoadSeq || curSong() !== s || songKey(s) !== key) return;
    const lines = (j.lines || []).filter((l) => l && l.text);
    lyricCache.set(key, lines);
    renderLyric(lines);
    if (desktopLyrics.enabled) {
      desktopSetLines(s, lines.length ? lines : [{ t: 0, text: "无字幕" }],
                      lines.length ? "ready" : "empty");
    }
  } catch (e) {
    if (seq !== lyricLoadSeq || curSong() !== s) return;
    lyricCache.set(key, []);
    renderLyric([]);
    if (desktopLyrics.enabled) desktopSetLines(s, [{ t: 0, text: "无字幕" }], "empty");
  }
}

// B站视频: 静音播放并跟随音频进度
let videoSyncTimer = null;
function setupVideo(s) {
  const box = $("lo-video");
  const vel = $("lo-video-el");
  const lyricEl = $("lo-lyric");
  const qSel = $("lo-quality");
  if (s && s.source === "bilibili") {
    box.classList.remove("hidden");
    lyricEl.classList.add("hidden");  // B站不显示"暂无歌词", 视频占满宽度
    $("lyric-overlay").classList.add("video-mode");  // 视频模式下隐藏头部进度条
    resetCmtUI();
    loadStats(s);
    loadQualities(s);
    setVideoSrc(s, biliQn);
    startVideoSync();
    loadDanmaku(s);
  } else {
    resetCmtUI();
    stopVideoSync();
    stopDanmaku();
    box.classList.add("hidden");
    vel.removeAttribute("src");
    $("lyric-overlay").classList.remove("video-mode");
    lyricEl.classList.remove("hidden");
    lyricEl.classList.add("solo");
    qSel.classList.add("hidden");
    // 全屏下切到非视频源: 模拟一次 ESC 退出全屏(覆盖层保持打开, 显示歌词)
    if (box.classList.contains("fs")) {
      box.classList.remove("fs");
      syncFsCtl(false);
      if (document.fullscreenElement && document.exitFullscreen)
        document.exitFullscreen().catch(() => {});
    }
  }
}

// 视频清晰度: biliQn 记录当前档位, loadQualities 拉取可选档位填充下拉框
let biliQn = 64;  // 默认 720P, boot 时从 settings.qn 恢复
function setVideoSrc(s, qn) {
  const vel = $("lo-video-el");
  const resumeAt = audio.currentTime;
  vel.src = withTok("/proxy?source=bilibili&id=" + encodeURIComponent(s.id) +
            "&kind=video&qn=" + qn + "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {})));
  vel.muted = true;
  vel.load();
  vel.playbackRate = settings.rate || 1;
  vel.addEventListener("loadedmetadata", function once() {
    vel.removeEventListener("loadedmetadata", once);
    vel.playbackRate = settings.rate || 1;
    try { vel.currentTime = resumeAt; } catch (e) {}
    if (audio.paused === false) vel.play().catch(() => {});
  });
}
async function loadQualities(s) {
  const qSel = $("lo-quality");
  qSel.innerHTML = "";
  qSel.classList.add("hidden");
  try {
    const url = "/api/bili/playinfo?id=" + encodeURIComponent(s.id) +
                "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {}));
    const j = await (await fetch(url)).json();
    if (curSong() !== s) return;
    const qs = j.qualities || [];
    if (!qs.length) return;
    // 从高到低排序展示
    qs.sort((a, b) => b.qn - a.qn);
    qSel.innerHTML = qs.map((q) =>
      '<option value="' + q.qn + '"' + (q.qn === biliQn ? " selected" : "") + '>' + esc(q.desc) + '</option>').join("");
    qSel.classList.remove("hidden");
    // 若当前档位不可用, 选最高可用
    if (!qs.some((q) => q.qn === biliQn)) {
      biliQn = qs[0].qn;
      qSel.value = String(biliQn);
      setVideoSrc(s, biliQn);
    }
  } catch (e) { /* 拉取失败则不显示选择器 */ }
}

// ---------------- B站弹幕 ----------------
let dmList = [];        // 已按 time 排序的弹幕
let dmPtr = 0;          // 下一个待发射的索引
let dmLastT = -1;       // 上帧时间, 用于检测 seek
let dmTimer = null;
let dmOn = true;         // 兼容: dmMode!=="off"
let dmMode = "full";     // full=全屏 / half=半屏(仅上半) / off=关闭
let dmScrollLanes = []; // 滚动轨道释放时间
let dmFixedLanes = {};  // 顶部/底部固定轨道
function stopDanmaku() {
  dmList = []; dmPtr = 0; dmLastT = -1;
  dmScrollLanes = []; dmFixedLanes = {};
  if (dmTimer) { clearInterval(dmTimer); dmTimer = null; }
  const c = $("lo-danmaku");
  if (c) c.innerHTML = "";
}
async function loadDanmaku(s) {
  stopDanmaku();
  try {
    const url = "/api/bili/danmaku?id=" + encodeURIComponent(s.id) +
                "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {}));
    const j = await (await fetch(url)).json();
    if (curSong() !== s) return;  // 已切到其他视频, 不覆盖新视频弹幕
    dmList = j.danmaku || [];
  } catch (e) {
    if (curSong() !== s) return;
    dmList = [];
  }
  applyDmMode();
  dmTimer = setInterval(danmakuTick, 180);
}
function dmLaneCount() {
  const box = $("lo-video");
  const h = dmMode === "half" ? box.clientHeight * 0.5 : box.clientHeight * 0.42;
  return Math.max(3, Math.floor(h / 28));
}
function colorHex(n) {
  return "#" + ("000000" + (n >>> 0).toString(16)).slice(-6);
}
function spawnDanmaku(d) {
  const c = $("lo-danmaku");
  if (!c || !dmOn) return;
  const box = $("lo-video");
  const W = box.clientWidth, H = box.clientHeight;
  const el = document.createElement("div");
  el.className = "dm-item";
  el.textContent = d.text;
  el.style.color = (d.color && d.color !== 16777215) ? colorHex(d.color) : "#fff";
  el.style.fontSize = Math.min(26, Math.max(16, (d.size || 25) * 0.8)) + "px";
  const fixed = (d.mode === 4 || d.mode === 5);  // 4=底部, 5=顶部
  if (fixed) {
    const key = d.mode === 5 ? "top" : "bottom";
    const lanes = dmFixedLanes[key] || (dmFixedLanes[key] = {});
    // 找空位
    let lane = 0;
    while (lanes[lane] && lanes[lane] > Date.now()) lane++;
    if (lane > 8) return;  // 满则丢弃
    lanes[lane] = Date.now() + 4200;
    el.style.left = "0"; el.style.right = "0"; el.style.textAlign = "center";
    el.style.transform = "none";
    if (key === "top") el.style.top = (8 + lane * 28) + "px";
    else el.style.bottom = (46 + lane * 28) + "px";
    el.classList.add(key === "top" ? "dm-top" : "dm-bottom");
    el.style.animationDuration = "4.2s";
    c.appendChild(el);
    setTimeout(() => el.remove(), 4400);
    return;
  }
  // 滚动弹幕: 找释放中的空轨道
  const lanes = dmLaneCount();
  if (!dmScrollLanes.length || dmScrollLanes.length < lanes) {
    const nl = new Array(lanes).fill(0);
    for (let i = 0; i < dmScrollLanes.length; i++) nl[i] = dmScrollLanes[i];
    dmScrollLanes = nl;
  }
  let lane = -1;
  for (let i = 0; i < dmScrollLanes.length; i++) {
    if (dmScrollLanes[i] <= Date.now()) { lane = i; break; }
  }
  if (lane < 0) lane = dmScrollLanes.length - 1;  // 全满挤用最下轨道
  const speed = 130;  // px/s
  el.style.top = (10 + lane * 28) + "px";
  el.classList.add("dm-scroll");
  c.appendChild(el);
  const dur = (W + el.offsetWidth + 40) / speed;
  el.style.setProperty("--dm-travel", (W + el.offsetWidth) + "px");
  el.style.animationDuration = dur.toFixed(2) + "s";
  dmScrollLanes[lane] = Date.now() + dur * 1000 * 0.35;
  setTimeout(() => el.remove(), dur * 1000 + 300);
}
function danmakuTick() {
  if (!dmList.length) return;
  const t = audio.currentTime;
  // seek 检测: 回退或大幅跳转会重置指针并清屏
  if (dmLastT < 0 || Math.abs(t - dmLastT) > 1.2) {
    dmPtr = 0;
    while (dmPtr < dmList.length && dmList[dmPtr].time < t) dmPtr++;
    $("lo-danmaku").innerHTML = "";
  }
  dmLastT = t;
  if (audio.paused) return;
  let guard = 0;
  while (dmPtr < dmList.length && dmList[dmPtr].time <= t && guard++ < 12) {
    spawnDanmaku(dmList[dmPtr]);
    dmPtr++;
  }
}
function applyDmMode() {
  dmOn = dmMode !== "off";
  const c = $("lo-danmaku");
  c.classList.toggle("off", dmMode === "off");
  c.classList.toggle("half", dmMode === "half");
  const btn = $("v-danmaku");
  btn.classList.toggle("on", dmOn);
  btn.classList.toggle("half", dmMode === "half");
  btn.title = "弹幕: " + (dmMode === "full" ? "全屏" : dmMode === "half" ? "半屏" : "关闭") + " (点击切换)";
  if (!dmOn) c.innerHTML = "";
}
$("v-danmaku").addEventListener("click", (e) => {
  e.stopPropagation();
  dmMode = dmMode === "full" ? "half" : dmMode === "half" ? "off" : "full";
  applyDmMode();
  showVidCtl();
});
// 发送弹幕
async function sendDanmaku() {
  const s = curSong();
  if (!s || s.source !== "bilibili") return;
  const input = $("dm-input");
  const text = input.value.trim();
  if (!text) return;
  const mode = parseInt($("dm-mode").value, 10) || 1;
  const color = parseInt($("dm-color").value, 10) || 16777215;
  const btn = $("dm-btn");
  btn.disabled = true; btn.textContent = "…";
  try {
    const r = await fetch("/api/bili/send_dm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: s.id, extra: s.extra || {}, text: text,
        mode: mode, color: color, fontsize: 25,
        progress: audio.currentTime, length: Math.floor(audio.duration || 0),
      }),
    });
    const j = await r.json();
    if (j.ok) {
      input.value = "";
      // 本地立即上屏预览
      spawnDanmaku({ time: audio.currentTime, mode: mode, color: color, size: 25, text: text });
    } else {
      indicator('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16v.5"/></svg>', j.error || "发送失败");
    }
  } catch (e) {
    indicator('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16v.5"/></svg>', "网络错误");
  }
  btn.disabled = false; btn.textContent = "发送";
}
$("dm-btn").addEventListener("click", (e) => { e.stopPropagation(); sendDanmaku(); });
$("dm-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); sendDanmaku(); }
  e.stopPropagation();
});
$("dm-input").addEventListener("pointerdown", (e) => e.stopPropagation());
$("lo-quality").addEventListener("change", (e) => {
  const s = curSong();
  if (!s || s.source !== "bilibili") return;
  biliQn = parseInt(e.target.value, 10) || 64;
  settings.qn = biliQn;
  saveSettings();
  setVideoSrc(s, biliQn);
});
// 倍速播放: 音频为主, 视频跟随
$("lo-speed").addEventListener("change", (e) => {
  const r = parseFloat(e.target.value) || 1;
  settings.rate = r;
  audio.playbackRate = r;
  $("lo-video-el").playbackRate = r;
  saveSettings();
  showVidCtl();
});

// 覆盖层进度条
const loSeek = { on: false };
// 拖动最多到第一秒/最后一秒, 避免拖到末尾触发切歌
function clampSeek(t, d) {
  if (!d) return 0;
  return Math.min(Math.max(t, 1), Math.max(d - 1, 0));
}
// seek 时长: 音频已加载用 audio.duration; 启动恢复未加载时回退歌单元数据
function seekDur() {
  if (audio.duration) return audio.duration;
  const s = curSong();
  return (s && s.duration) || 0;
}
// 当前进度: 音频未加载时用上次保存的进度
function curPos() {
  if (audio.src && audio.duration) return audio.currentTime;
  const s = curSong();
  if (s && settings.pos && settings.pos.key === songKey(s)) return settings.pos.time;
  return 0;
}
// 应用跳转: 音频未加载(启动恢复)时经 playAt 从目标进度加载并播放
function seekTo(ratio) {
  const d = seekDur();
  if (!d || state.index < 0) return;
  const t = clampSeek(ratio * d, d);
  if (!audio.src) {
    const s = curSong();
    settings.pos = { key: songKey(s), time: t };
    playAt(state.index);
  } else {
    audio.currentTime = t;
  }
}
function bindSeekBar(barId, fillId, timeId) {
  const bar = $(barId);
  function ratioOf(e) {
    const r = bar.getBoundingClientRect();
    return Math.min(Math.max((e.clientX - r.left) / (r.width || 1), 0), 1);
  }
  function paint(r) {
    const d = seekDur();
    $(fillId).style.width = r * 100 + "%";
    $(timeId).textContent = fmt(clampSeek(r * d, d));
  }
  bar.addEventListener("pointerdown", (e) => {
    if (!seekDur()) return;
    loSeek.on = true;
    loSeek.ratio = ratioOf(e);
    bar.setPointerCapture(e.pointerId);
    bar.classList.add("dragging");
    paint(loSeek.ratio);
  });
  bar.addEventListener("pointermove", (e) => {
    if (!loSeek.on) return;
    loSeek.ratio = ratioOf(e);
    paint(loSeek.ratio);
  });
  function end() {
    if (!loSeek.on) return;
    loSeek.on = false;
    bar.classList.remove("dragging");
    seekTo(loSeek.ratio);
  }
  bar.addEventListener("pointerup", end);
  bar.addEventListener("pointercancel", end);
}
bindSeekBar("lo-seek-bar", "lo-seek-fill", "lo-t-cur");
bindSeekBar("v-seek-bar", "v-seek-fill", "v-t-cur");

function syncLoSeek() {
  if (!lyricOpen || loSeek.on) return;
  const d = audio.duration || 0;
  const pct = (d ? audio.currentTime / d * 100 : 0) + "%";
  $("lo-seek-fill").style.width = pct;
  $("v-seek-fill").style.width = pct;
  const cur = fmt(audio.currentTime), dur = fmt(d);
  $("lo-t-cur").textContent = cur;
  $("lo-t-dur").textContent = dur;
  $("v-t-cur").textContent = cur;
  $("v-t-dur").textContent = dur;
}
function startVideoSync() {
  const vel = $("lo-video-el");
  stopVideoSync();
  videoSyncTimer = setInterval(() => {
    if (!lyricOpen) return;
    // 跟随音频播放状态与进度
    if (audio.paused) { if (!vel.paused) vel.pause(); }
    else { if (vel.paused) vel.play().catch(() => {}); }
    if (Math.abs(vel.currentTime - audio.currentTime) > 0.6 && vel.duration) {
      try { vel.currentTime = audio.currentTime; } catch (e) {}
    }
  }, 500);
}
function stopVideoSync() {
  if (videoSyncTimer) { clearInterval(videoSyncTimer); videoSyncTimer = null; }
}

// ---------------- B站视频触控交互 ----------------
// 亮度/音量/进度手势: 左右半屏分别绑定亮度/音量, 横向拖动统一 seek
let biliBrightness = 1;
const vidCtl = { hideTimer: null, lastTap: 0 };
function showVidCtl() {
  const box = $("lo-video");
  box.classList.add("show-ctl");
  clearTimeout(vidCtl.hideTimer);
  vidCtl.hideTimer = setTimeout(() => {
    box.classList.remove("show-ctl");  // 3 秒无移动/点击则隐藏所有按钮
  }, 3000);
}
$("lo-video").addEventListener("mouseenter", showVidCtl);
$("lo-video").addEventListener("mousemove", showVidCtl);
$("lo-video").addEventListener("touchstart", showVidCtl, { passive: true });

// 清晰度选择器始终位于视频窗口内底栏, 无需随全屏移动
function syncFsCtl() {}

function indicator(icon, text) {
  const el = $("lo-indicator");
  el.innerHTML = icon + "<span>" + text + "</span>";
  el.classList.add("on");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("on"), 800);
}
const IC_BRIGHT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const IC_VOL = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3z"/><path d="M16 8a4 4 0 0 1 0 8" fill="none" stroke="currentColor" stroke-width="2"/></svg>';
const IC_FF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M13 5l7 7-7 7M5 5l7 7-7 7"/></svg>';
const IC_RW = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 5L4 12l7 7M19 5l-7 7 7 7"/></svg>';

// 视频区域内活跃指针(供双指手势判断): pointerId -> {x0, y0, xc, yc}
const vidPts = new Map();
const videoGestureResetters = [];

function bindVideoGesture(el, side) {
  const st = { id: null, x0: 0, y0: 0, t0: 0, mode: null, base: 0, moved: false };
  function cancelGesture() {
    if (st.mode === "seek") {
      loSeek.on = false;
      $("lo-seek-bar").classList.remove("dragging");
      syncLoSeek();
    }
    st.id = null; st.mode = null; st.moved = false; st.ratio = null;
    // 保留指针捕获到松手, 双指移出半屏后仍能收到结束事件
  }
  videoGestureResetters.push(cancelGesture);
  el.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    try { el.setPointerCapture(e.pointerId); } catch (err) {}
    if (vidPts.size >= 2 || (st.id !== null && st.id !== e.pointerId)) {
      cancelGesture();
      return;
    }
    st.id = e.pointerId; st.x0 = e.clientX; st.y0 = e.clientY; st.ys = e.clientY; st.t0 = Date.now();
    st.mode = null; st.moved = false;
    const d = seekDur();
    st.base = d ? (curPos() / d) : 0;
  });
  el.addEventListener("pointermove", (e) => {
    if (st.id !== e.pointerId) return;
    if (vidPts.size >= 2) { cancelGesture(); return; }
    const dx = e.clientX - st.x0, dy = st.y0 - e.clientY;  // dy 向上为正
    if (!st.mode) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      st.mode = Math.abs(dx) > Math.abs(dy) ? "seek" : "vert";
      st.moved = true;
    }
    if (st.mode === "seek") {
      const d = seekDur();
      if (!d) return;
      // 手势 seek 倍率: 降低灵敏度(约0.7倍), 全宽拖动约为 1.4 倍时长行程
      const ratio = Math.min(Math.max(st.base + dx * 0.7 / el.clientWidth, 0), 1);
      st.ratio = ratio;
      loSeek.on = true;
      $("lo-seek-fill").style.width = ratio * 100 + "%";
      $("v-seek-fill").style.width = ratio * 100 + "%";
      // 最多拖到第一秒/最后一秒, 避免拖到末尾触发切歌
      const target = clampSeek(ratio * d, d);
      $("lo-t-cur").textContent = fmt(target);
      $("v-t-cur").textContent = fmt(target);
      if (audio.src && Math.abs(audio.currentTime - target) > 1.5) audio.currentTime = target;
      indicator(dx > 0 ? IC_FF : IC_RW, fmt(target));
    } else {
      const delta = dy / el.clientHeight;  // 上滑增大
      if (side === "left") {
        biliBrightness = Math.min(Math.max(0.2, biliBrightness + delta * 1.2), 1);
        $("lo-video-el").style.filter = "brightness(" + biliBrightness + ")";
        indicator(IC_BRIGHT, Math.round(biliBrightness * 100) + "%");
      } else {
        let v = Math.min(Math.max(audio.volume + delta * 1.2, 0), 1);
        audio.volume = v; audio.muted = false;
        $("vol-fill").style.height = v * 100 + "%";
        $("vol-val").textContent = Math.round(v * 100);
        settings.volume = v;
        indicator(IC_VOL, Math.round(v * 100) + "%");
      }
      st.y0 = e.clientY;  // 增量式, 避免跳变
    }
  });
  function end(e) {
    if (st.id !== e.pointerId) return;
    if (e.type === "pointercancel" || vidPts.size >= 2) { cancelGesture(); return; }
    st.id = null;
    if (st.mode === "seek") {
      loSeek.on = false;
      $("lo-seek-bar").classList.remove("dragging");
      if (st.ratio != null && !audio.src) seekTo(st.ratio);  // 音频未加载: 松手时从目标进度加载播放
    }
    else if (!st.moved) {
      // 无移动 = 单击: 双击暂停/播放, 单击显示控件
      const now = Date.now();
      if (now - vidCtl.lastTap < 320) {
        vidCtl.lastTap = 0;
        toggleAudioPlay();
      } else {
        vidCtl.lastTap = now;
        showVidCtl();
      }
    }
    st.mode = null; st.moved = false;
  }
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
  el.addEventListener("lostpointercapture", (e) => {
    if (st.id === e.pointerId) cancelGesture();
  });
}
function toggleAudioPlay() {
  // 启动恢复后音频尚未加载(无 src): 走 playAt 加载并播放
  if (!audio.src) { if (aq().length) playAt(Math.max(0, state.index)); return; }
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
}
// 空格键: 暂停/播放; 左右方向键: 快退/快进 5 秒 (输入框聚焦时不拦截)
document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  if (e.code === "Space" || e.key === " ") {
    e.preventDefault();
    toggleAudioPlay();
    return;
  }
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    const d = seekDur();
    if (!d || state.index < 0) return;
    e.preventDefault();
    const delta = e.key === "ArrowRight" ? 5 : -5;
    seekTo(clampSeek(curPos() + delta, d) / d);
    if ($("lyric-overlay").classList.contains("video-mode")) showVidCtl();
  }
});
bindVideoGesture($("lo-g-left"), "left");
bindVideoGesture($("lo-g-right"), "right");

// 双指滑动: 常规窗口双指左滑 -> 打开评论区; 小窗双指上滑 -> 全屏
(function bindTwoFinger() {
  const box = $("lo-video");
  let fired = false;
  box.addEventListener("pointerdown", (e) => {
    vidPts.set(e.pointerId, { x0: e.clientX, y0: e.clientY, xc: e.clientX, yc: e.clientY });
    if (vidPts.size >= 2) {
      fired = false;
      for (const reset of videoGestureResetters) reset();
    }
  });
  box.addEventListener("pointermove", (e) => {
    const p = vidPts.get(e.pointerId);
    if (p) { p.xc = e.clientX; p.yc = e.clientY; }
    if (vidPts.size < 2 || fired) return;
    let sumY = 0, sumX = 0;
    for (const q of vidPts.values()) { sumY += q.y0 - q.yc; sumX += q.xc - q.x0; }  // sumY 向上为正, sumX 向右为正
    const avgY = sumY / vidPts.size, avgX = sumX / vidPts.size;
    const cmtOpen = !$("lo-comments").classList.contains("hidden");
    // 小窗(评论区打开): 双指上滑 -> 全屏; 双指右滑 -> 关闭评论区
    if (box.classList.contains("mini")) {
      if (avgY > 60 && avgY > Math.abs(avgX)) {
        fired = true;
        $("mini-fs").click();
      } else if (cmtOpen && avgX > 60 && avgX > Math.abs(avgY)) {
        fired = true;
        swipeCloseComments();
      }
      return;
    }
    // 全屏: 双指右滑 -> 关闭评论区
    if (cmtOpen) {
      if (avgX > 60 && avgX > Math.abs(avgY)) {
        fired = true;
        swipeCloseComments();
      }
      return;
    }
    // 常规窗口: 双指左滑 -> 打开评论区
    if (avgX < -60 && Math.abs(avgX) > Math.abs(avgY)) {
      fired = true;
      openComments();
    }
  });
  function up(e) { vidPts.delete(e.pointerId); if (vidPts.size < 2) fired = false; }
  box.addEventListener("pointerup", up);
  box.addEventListener("pointercancel", up);
})();

// 评论区打开时: 双指右滑关闭评论区(与小窗/全屏内手势统一)
// 右滑与"收起"按钮共用的关闭逻辑
function swipeCloseComments() {
  closeComments();
  showVidCtl();
}
(function bindCmtSwipeClose() {
  const ov = $("lyric-overlay");
  const pts = new Map();
  let fired = false;
  ov.addEventListener("pointerdown", (e) => {
    if ($("lo-comments").classList.contains("hidden")) return;
    // 视频区域由 bindTwoFinger 单独处理(避免重复触发)
    if (e.target.closest("#lo-video")) return;
    // 按钮/输入框等交互元素上不起作用: setPointerCapture 会把 click 重定向到覆盖层, 导致按钮失效
    if (e.target.closest("button,select,input,a,.lo-cmt-item,.lo-rel-item")) return;
    pts.set(e.pointerId, { x0: e.clientX, y0: e.clientY, xc: e.clientX, yc: e.clientY });
    if (pts.size >= 2) fired = false;
    try { ov.setPointerCapture(e.pointerId); } catch (err) {}
  });
  ov.addEventListener("pointermove", (e) => {
    const p = pts.get(e.pointerId);
    if (p) { p.xc = e.clientX; p.yc = e.clientY; }
    if (pts.size < 2 || fired) return;
    let sumY = 0, sumX = 0;
    for (const q of pts.values()) { sumY += q.y0 - q.yc; sumX += q.xc - q.x0; }  // 向右为正
    const avgX = sumX / pts.size, avgY = sumY / pts.size;
    if (avgX > 60 && avgX > Math.abs(avgY)) {
      fired = true;
      swipeCloseComments();
    }
  });
  function up(e) { pts.delete(e.pointerId); if (pts.size < 2) fired = false; }
  ov.addEventListener("pointerup", up);
  ov.addEventListener("pointercancel", up);
})();

// ---------------- 点赞 / 投币 / 评论区 ----------------
function fmtCount(n) {
  n = n || 0;
  return n >= 10000 ? (n / 10000).toFixed(1).replace(/\.0$/, "") + "万" : String(n);
}
let biliStat = null;   // 当前视频统计 {like, coin, liked, coined}
function resetCmtUI() {
  closeComments();
  biliStat = null;
  $("v-like").classList.remove("liked");
  $("v-coin").classList.remove("coined");
  $("v-like-n").textContent = "0";
  $("v-coin-n").textContent = "0";
  $("v-cmt-n").textContent = "0";
}
async function loadStats(s) {
  try {
    const url = "/api/bili/stats?id=" + encodeURIComponent(s.id) +
                "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {}));
    const j = await (await fetch(url)).json();
    if (curSong() !== s || j.error || !j.aid) return;
    biliStat = j;
    $("v-like-n").textContent = fmtCount(j.like);
    $("v-coin-n").textContent = fmtCount(j.coin);
    $("v-cmt-n").textContent = fmtCount(j.reply);
    $("v-like").classList.toggle("liked", !!j.liked);
    $("v-coin").classList.toggle("coined", !!j.coined);
  } catch (e) { /* 静默失败 */ }
}
$("v-like").addEventListener("click", async (e) => {
  e.stopPropagation();
  const s = curSong();
  if (!s || s.source !== "bilibili") return;
  if (biliStat && biliStat.liked) { toast("已经点过赞啦"); return; }
  try {
    const r = await fetch("/api/bili/like", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: s.id, extra: s.extra || {} }) });
    const j = await r.json();
    if (curSong() !== s) return;  // 已切换视频: 迟到响应不得改写当前状态
    if (j.ok) {
      if (biliStat) { biliStat.liked = true; biliStat.like++; }
      $("v-like").classList.add("liked");
      $("v-like-n").textContent = fmtCount(biliStat ? biliStat.like : 0);
      indicator('<svg viewBox="0 0 24 24" fill="none" stroke="#fb7299" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round"><path d="M7 22V11l5-9c1.4.3 2.2 1.4 2.2 2.9L13.5 9H19c1.2 0 2.1 1.1 1.8 2.3l-1.6 8A2 2 0 0 1 17.2 21H7Z"/><path d="M7 11H4a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h3"/></svg>', "点赞成功");
    } else toast(j.error || "点赞失败");
  } catch (err) { toast("网络错误"); }
  showVidCtl();
});
$("v-coin").addEventListener("click", (e) => {
  e.stopPropagation();
  const s = curSong();
  if (!s || s.source !== "bilibili") return;
  if (biliStat && biliStat.coined) { toast("已经投过币啦"); return; }
  showCoinMenu(s);
  showVidCtl();
});
// 投币选择菜单: 先选 1 枚 / 2 枚, 确认后才执行投币
function showCoinMenu(s) {
  const menu = ensureCtxMount();
  menu.innerHTML = "";
  const mk = (t, fn) => {
    const d = document.createElement("div");
    d.className = "ctx-item";
    d.textContent = t;
    d.addEventListener("click", () => { hideCtx(); fn(); });
    menu.appendChild(d);
  };
  mk("🪙 投 1 枚", () => doCoin(s, 1));
  mk("🪙🪙 投 2 枚", () => doCoin(s, 2));
  menu.classList.remove("hidden");
  const r = lastTouchPos;
  menu.style.left = Math.min(r.x, innerWidth - 190) + "px";
  menu.style.top = Math.max(10, r.y - menu.offsetHeight - 8) + "px";
}
async function doCoin(s, n) {
  try {
    const r = await fetch("/api/bili/coin", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: s.id, extra: s.extra || {}, multiply: n }) });
    const j = await r.json();
    if (curSong() !== s) return;  // 已切换视频: 迟到响应不得改写当前状态
    if (j.ok) {
      if (biliStat) { biliStat.coined = n; biliStat.coin += n; }
      $("v-coin").classList.add("coined");
      $("v-coin-n").textContent = fmtCount(biliStat ? biliStat.coin : 0);
      indicator('<svg viewBox="0 0 24 24" fill="none" stroke="#23ade5" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><text x="12" y="16.2" text-anchor="middle" font-size="11.5" font-weight="700" fill="#23ade5" stroke="none">币</text></svg>', "投币成功 ×" + n);
    } else toast(j.error || "投币失败");
  } catch (err) { toast("网络错误"); }
  showVidCtl();
}

// 评论区
let cmtPage = 0, cmtEnd = false, cmtLoading = false, cmtSort = 2;
let cmtRequestId = 0;
function cmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000), now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + "分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + "小时前";
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + "天前";
  return (d.getMonth() + 1) + "月" + d.getDate() + "日";
}
let cmtItems = [];      // 当前已加载的一级评论
let cmtAid = 0;         // 当前视频 aid
let cmtMyMid = "";      // 当前登录用户 mid(用于判断自己的评论)
let cmtReplyTo = null;  // {rpid, name} 回复目标
function renderCmtItem(c) {
  const el = document.createElement("div");
  el.className = "lo-cmt-item";
  el.dataset.rpid = c.rpid;
  const mine = cmtMyMid && String(c.mid) === cmtMyMid;
  el.innerHTML =
    '<img class="lo-cmt-face" loading="lazy" referrerpolicy="no-referrer" src="' + esc(c.face || "") + '" onerror="this.style.visibility=\'hidden\'">' +
    '<div class="lo-cmt-body"><div class="lo-cmt-name">' + esc(c.name) + (mine ? ' <span class="lo-cmt-mine">我</span>' : "") + '</div>' +
    '<div class="lo-cmt-msg">' + esc(c.msg) + '</div>' +
    '<div class="lo-cmt-meta"><span>' + cmtTime(c.time) + '</span>' +
    '<button class="lo-cmt-like' + (c.liked ? " on" : "") + '" data-act="like">👍 <b>' + fmtCount(c.like) + '</b></button>' +
    '<button class="lo-cmt-reply" data-act="reply">回复' + (c.reply ? " " + fmtCount(c.reply) : "") + '</button>' +
    (mine ? '<button class="lo-cmt-del" data-act="del">删除</button>' : "") +
    '</div></div>';
  return el;
}
async function loadComments(more) {
  const s = curSong();
  if (!s || s.source !== "bilibili" || cmtTab !== "c" || (more && (cmtLoading || cmtEnd))) return;
  const requestId = ++cmtRequestId, sort = cmtSort, page = more ? cmtPage + 1 : 1;
  const current = () => requestId === cmtRequestId && curSong() === s && cmtTab === "c" && cmtSort === sort;
  cmtLoading = true;
  const list = $("lo-cmt-list");
  if (!more) { list.innerHTML = '<div class="lo-cmt-empty">加载中…</div>'; cmtPage = 0; cmtEnd = false; }
  try {
    const url = "/api/bili/comments?id=" + encodeURIComponent(s.id) +
                "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {})) +
                "&sort=" + sort + "&page=" + page;
    const j = await (await fetch(url)).json();
    if (!current()) return;
    if (j.error) throw new Error(j.error);
    cmtPage = page;
    cmtEnd = !!j.cursor;
    cmtAid = j.aid || cmtAid;
    cmtMyMid = String(j.my_mid || cmtMyMid || "");
    const items = j.comments || [];
    if (!more) { list.innerHTML = ""; cmtItems = []; }
    if (!items.length && !more) list.innerHTML = '<div class="lo-cmt-empty">暂无评论</div>';
    for (const c of items) {
      cmtItems.push(c);
      list.appendChild(renderCmtItem(c));
    }
  } catch (e) {
    if (current() && !more) list.innerHTML = '<div class="lo-cmt-empty">评论加载失败</div>';
  } finally {
    if (requestId === cmtRequestId) cmtLoading = false;
  }
}
// 评论点赞(点击 👍 切换)
$("lo-cmt-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  e.stopPropagation();
  const item = btn.closest(".lo-cmt-item");
  if (!item) return;
  const c = cmtItems.find((x) => String(x.rpid) === item.dataset.rpid);
  if (!c) return;
  const s = curSong();
  if (!s) return;
  if (btn.dataset.act === "reply") {
    cmtReplyTo = { rpid: c.rpid, name: c.name };
    const tip = $("cmt-reply-tip");
    tip.innerHTML = '回复 <b>@' + esc(c.name) + '</b> <button id="cmt-reply-x" title="取消回复">✕</button>';
    tip.classList.remove("hidden");
    $("cmt-text").focus();
    return;
  }
  if (btn.dataset.act === "del") {
    if (!await showConfirm("确定删除这条评论？")) return;
    btn.disabled = true;
    try {
      const j = await postJSON("/api/bili/cmt_del", {
        id: s.id, extra: s.extra || {}, rpid: c.rpid });
      if (j.ok) {
        toast("已删除");
        item.remove();
        cmtItems = cmtItems.filter((x) => String(x.rpid) !== String(c.rpid));
      } else {
        toast(j.error || "删除失败");
        btn.disabled = false;
      }
    } catch (err) { toast("删除失败"); btn.disabled = false; }
    return;
  }
  // 点赞
  const want = !c.liked;
  btn.disabled = true;
  try {
    const j = await postJSON("/api/bili/cmt_like", {
      id: s.id, extra: s.extra || {}, rpid: c.rpid, like: want ? 1 : 0 });
    if (j.ok) {
      c.liked = want;
      c.like += want ? 1 : -1;
      btn.classList.toggle("on", c.liked);
      btn.querySelector("b").textContent = fmtCount(c.like);
    } else {
      toast(j.error || "点赞失败");
    }
  } catch (err) { toast("点赞失败"); }
  btn.disabled = false;
});
// 取消回复目标
$("cmt-reply-tip").addEventListener("click", (e) => {
  if (e.target.id === "cmt-reply-x") {
    cmtReplyTo = null;
    e.currentTarget.classList.add("hidden");
  }
});
// 发表评论 / 回复
let cmtSending = false;
async function sendComment() {
  const s = curSong();
  if (!s || s.source !== "bilibili") return;
  const inp = $("cmt-text");
  const text = inp.value.trim();
  if (!text || cmtSending) return;
  cmtSending = true;
  $("cmt-send").disabled = true;
  try {
    const j = await postJSON("/api/bili/cmt_post", {
      id: s.id, extra: s.extra || {}, text,
      root: cmtReplyTo ? cmtReplyTo.rpid : 0,
      parent: cmtReplyTo ? cmtReplyTo.rpid : 0 });
    if (j.ok) {
      inp.value = "";
      cmtReplyTo = null;
      $("cmt-reply-tip").classList.add("hidden");
      toast("发送成功");
      loadComments(false);
    } else {
      toast(j.error || "发送失败");
    }
  } catch (e) { toast("发送失败"); }
  cmtSending = false;
  $("cmt-send").disabled = false;
}
$("cmt-send").addEventListener("click", (e) => { e.stopPropagation(); sendComment(); });
$("cmt-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); sendComment(); }
  e.stopPropagation();
});
$("cmt-text").addEventListener("pointerdown", (e) => e.stopPropagation());
function openComments() {
  const box = $("lo-video");
  const panel = $("lo-comments");
  // 全屏时只有全屏元素的子孙会被渲染, 需把评论面板移入视频元素内(视频保持全屏, 面板右侧覆盖)
  if (box.classList.contains("fs")) {
    if (panel.parentElement !== box) box.appendChild(panel);
  } else {
    if (panel.parentElement !== $("lo-video").parentElement) {
      $("lo-video").parentElement.appendChild(panel);
    }
    // 小窗与评论区顶部对齐到视频窗口当前顶部
    const top = Math.round(box.getBoundingClientRect().top);
    document.documentElement.style.setProperty("--cmt-top", top + "px");
    box.classList.add("mini");
  }
  panel.classList.remove("hidden");
  $("lyric-overlay").classList.add("cmt-open");
  switchCmtTab("c");
  showVidCtl();
}
// 评论区/相关推荐 标签切换
let cmtTab = "c";
function switchCmtTab(t) {
  cmtRequestId++;
  cmtLoading = false;
  relatedLoadedFor = null;
  cmtTab = t;
  $("cmt-tab-c").classList.toggle("active", t === "c");
  $("cmt-tab-r").classList.toggle("active", t === "r");
  $("cmt-sort").classList.toggle("hidden", t !== "c");
  if (t === "c") loadComments(false);
  else loadRelated();
}
$("cmt-tab-c").addEventListener("click", (e) => { e.stopPropagation(); switchCmtTab("c"); });
$("cmt-tab-r").addEventListener("click", (e) => { e.stopPropagation(); loadRelated(true); });
// 评论排序: 最热/最新
$("cmt-sort").addEventListener("click", (e) => {
  const b = e.target.closest(".cs-btn");
  if (!b) return;
  e.stopPropagation();
  const v = parseInt(b.dataset.sort, 10);
  if (v === cmtSort) return;
  cmtSort = v;
  for (const x of $("cmt-sort").querySelectorAll(".cs-btn")) x.classList.toggle("active", x === b);
  loadComments(false);
});
let relatedLoadedFor = null;
async function loadRelated(toggle) {
  const s = curSong();
  if (!s || s.source !== "bilibili") return;
  if (cmtTab !== "r") { switchCmtTab("r"); return; }
  if (toggle && relatedLoadedFor === s) return;
  const requestId = ++cmtRequestId;
  const current = () => requestId === cmtRequestId && curSong() === s && cmtTab === "r";
  cmtLoading = false;
  const list = $("lo-cmt-list");
  list.innerHTML = '<div class="lo-cmt-empty">推荐加载中…</div>';
  try {
    const j = await api("/api/bili/recommend?bvid=" + encodeURIComponent(s.id) + "&limit=30");
    if (!current()) return;
    if (j.error) throw new Error(j.error);
    const songs = j.songs || [];
    relatedLoadedFor = s;
    if (!songs.length) { list.innerHTML = '<div class="lo-cmt-empty">暂无相关推荐</div>'; return; }
    list.innerHTML = "";
    for (const r of songs) {
      const el = document.createElement("div");
      el.className = "lo-rel-item";
      el.innerHTML =
        '<img class="lo-rel-cover" loading="lazy" src="' + esc(coverSrc(r)) + '" onerror="this.style.visibility=\'hidden\'">' +
        '<div class="lo-rel-meta"><div class="lo-rel-title">' + esc(r.title) + "</div>" +
        '<div class="lo-rel-sub">' + esc(r.artist || "") +
        (r.duration ? " · " + fmt(r.duration) : "") + "</div></div>";
      el.addEventListener("click", () => playFeed(r, songs));
      list.appendChild(el);
    }
  } catch (e) {
    if (current()) list.innerHTML = '<div class="lo-cmt-empty">推荐加载失败</div>';
  }
}
function closeComments() {
  cmtRequestId++;
  cmtLoading = false;
  relatedLoadedFor = null;
  const panel = $("lo-comments");
  panel.classList.add("hidden");
  // 若评论面板曾移入全屏视频元素, 归还到覆盖层原位置
  const body = panel.closest(".lo-video") ? panel.closest(".lo-body") : null;
  if (body && panel.parentElement !== body) body.appendChild(panel);
  $("lyric-overlay").classList.remove("cmt-open");
  $("lo-video").classList.remove("mini");
}
// 收起按钮复用右滑关闭逻辑
$("lo-cmt-close").addEventListener("click", (e) => { e.stopPropagation(); swipeCloseComments(); });
$("v-cmt").addEventListener("click", (e) => {
  e.stopPropagation();
  // 收起时复用右滑关闭逻辑, 与小窗/收起按钮行为一致
  if (!$("lo-comments").classList.contains("hidden")) swipeCloseComments();
  else openComments();
  showVidCtl();
});
// 小窗全屏按钮: 复用底栏全屏按钮逻辑, 保证全屏内容完整(弹幕等)
$("mini-fs").addEventListener("click", (e) => {
  e.stopPropagation();
  closeComments();
  $("lo-fs-btn").click();
});
$("lo-cmt-list").addEventListener("scroll", (e) => {
  if (cmtTab !== "c") return;
  const el = e.target;
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 80) loadComments(true);
});

// 视频暂停态显示中央播放图标
(function bindVidPauseUI() {
  const box = $("lo-video"), vel = $("lo-video-el");
  const sync = () => box.classList.toggle("paused", audio.paused);
  audio.addEventListener("play", sync);
  audio.addEventListener("pause", sync);
  $("lo-pp").addEventListener("pointerdown", (e) => { e.stopPropagation(); toggleAudioPlay(); });
  // 视频底栏: 播放/上一首/下一首
  $("v-play").addEventListener("click", (e) => { e.stopPropagation(); toggleAudioPlay(); showVidCtl(); });
  $("v-prev").addEventListener("click", (e) => { e.stopPropagation(); prev(); showVidCtl(); });
  $("v-next").addEventListener("click", (e) => { e.stopPropagation(); next(); showVidCtl(); });
  // 全屏按钮: 进入全屏前先收起评论区(复用上方小窗全屏的完整逻辑)
  $("lo-fs-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    const on = !box.classList.contains("fs");
    if (on) closeComments();
    box.classList.toggle("fs", on);
    syncFsCtl(on);
    $("lo-fs-btn").title = on ? "退出全屏" : "全屏";
    if (on && box.requestFullscreen) box.requestFullscreen().catch(() => {});
    else if (!on && document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
    showVidCtl();
  });
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement && box.classList.contains("fs")) {
      box.classList.remove("fs");
      syncFsCtl(false);
      // 退出全屏时若评论区仍打开, 关闭并归还面板位置
      if (!$("lo-comments").classList.contains("hidden")) closeComments();
      // 退出全屏: 共享菜单若还挂在全屏元素内, 归还 body, 否则普通菜单将不可见
      const m = $("ctx-menu");
      if (m.parentElement !== document.body) document.body.appendChild(m);
    }
  });
})();

function openLyric() {
  const s = curSong();
  if (!s) { toast("当前没有播放中的歌曲"); return; }
  lyricOpen = true;
  const ov = $("lyric-overlay");
  ov.classList.remove("hidden");
  $("lo-title").textContent = s.title;
  $("lo-artist").textContent = (s.artist || "—") + " · " + (SRC_NAME[s.source] || s.source);
  $("lo-bg").style.backgroundImage = coverSrc(s) ? 'url("' + esc(coverSrc(s)) + '")' : "";
  setupVideo(s);
  loadLyrics(s);
  setPlayIcon(!audio.paused);  // 按当前播放状态初始化视频底栏播放按钮
  // 打开即同步一次时间; 音频尚未加载(启动恢复未播放)时用歌单元信息兜底
  if (audio.duration) {
    syncLoSeek();
  } else if (s.duration) {
    const t = (settings.pos && settings.pos.key === songKey(s)) ? settings.pos.time : 0;
    const pct = (t ? Math.min(t / s.duration, 1) * 100 : 0) + "%";
    $("lo-seek-fill").style.width = pct; $("v-seek-fill").style.width = pct;
    $("lo-t-cur").textContent = fmt(t); $("v-t-cur").textContent = fmt(t);
    $("lo-t-dur").textContent = fmt(s.duration); $("v-t-dur").textContent = fmt(s.duration);
  }
}
function closeLyric() {
  lyricOpen = false;
  hideCtx();   // 关闭覆盖层时收起共享菜单并归还挂载位置
  for (const reset of videoGestureResetters) reset();
  vidPts.clear();
  loSeek.on = false;
  $("lo-seek-bar").classList.remove("dragging");
  $("v-seek-bar").classList.remove("dragging");
  closeComments();
  $("lyric-overlay").classList.add("hidden");
  stopVideoSync();
  stopDanmaku();
  const vel = $("lo-video-el");
  vel.pause();
  vel.removeAttribute("src");
}

$("pb-cover").addEventListener("click", () => {
  if (state.index >= 0) openLyric();
});
$("btn-desktop-lyrics").addEventListener("click", toggleDesktopLyrics);
$("btn-desktop-unlock").addEventListener("click", toggleDesktopLyricsLock);
$("lo-close").addEventListener("click", closeLyric);
$("lo-prev").addEventListener("click", prev);
$("lo-next").addEventListener("click", next);
$("lo-lyric").addEventListener("wheel", () => { lyricUserScroll = Date.now(); }, { passive: true });
$("lo-lyric").addEventListener("touchmove", () => { lyricUserScroll = Date.now(); }, { passive: true });

// ---------------- 启动 ----------------
async function boot() {
  // pywebview 注入原生 API 后恢复桌面歌词开关；普通浏览器会安全跳过。
  desktopReadState();
  if (window.desktopStateSyncTimer) clearInterval(window.desktopStateSyncTimer);
  window.desktopStateSyncTimer = setInterval(desktopSyncState, 1000);
  // 加载本地设置(音量 + 播放进度); 响应迟到时不覆盖用户已修改的设置
  try {
    const st = await api("/api/settings");
    bootSettings = JSON.parse(JSON.stringify(st));
    if (st && !userTouchedSettings) {
      if (typeof st.volume === "number") settings.volume = Math.min(Math.max(st.volume, 0), 1);
      if (st.pos && st.pos.key) settings.pos = st.pos;
      if (typeof st.rate === "number" && st.rate >= 0.5 && st.rate <= 2) settings.rate = st.rate;
      if (typeof st.qn === "number" && st.qn >= 16) biliQn = st.qn;
      if (st.proxy && typeof st.proxy === "object") settings.proxy = Object.assign({}, DEFAULT_PROXY, st.proxy);
      if (st.font && typeof st.font === "object") settings.font = Object.assign({}, DEFAULT_FONT, st.font);
      applyFontSettings();
      audio.volume = settings.volume;
      audio.playbackRate = settings.rate || 1;
      $("lo-speed").value = String(settings.rate || 1);
      $("vol-fill").style.height = settings.volume * 100 + "%";
      $("vol-val").textContent = Math.round(settings.volume * 100);
    }
  } catch (e) {}
  $("btn-mode").innerHTML = MODES[state.mode].icon;
  renderQueue();
  refreshLoginState();
  try {
    const q = await api("/api/queue");
    // 只在队列仍属于未修改的启动上下文时应用旧快照:
    // 用户已加歌/播放/进入 feed 时, 不得覆盖当前队列, 也不得用主队列下标污染 feed 下标
    if (!userTouchedQueue && !state.feedMode && q.songs && q.songs.length) {
      state.queue = q.songs;
      state.index = Math.min(q.index ?? -1, q.songs.length - 1);
      renderQueue();
      if (state.index >= 0) {
        const s = state.queue[state.index];
        $("pb-title").textContent = s.title;
        $("pb-artist").textContent = (s.artist || "—") + " · " + (SRC_NAME[s.source] || s.source) + "（已恢复，点▶播放）";
        $("pb-cover").innerHTML = coverSrc(s) ? '<img src="' + esc(coverSrc(s)) + '">' : "♪";
        // 预置恢复信息, 用户点▶播放时 playAt 会读取 settings.pos
        audio.dataset.key = songKey(s);
        if (settings.pos && settings.pos.key === songKey(s) && settings.pos.time > 3) {
          audio.dataset.resume = String(settings.pos.time);
          // 启动即显示时长与上次进度(音频尚未加载, 用歌单元信息)
          if (s.duration) {
            $("t-dur").textContent = fmt(s.duration);
            const r = Math.min(settings.pos.time / s.duration, 1);
            $("seek-fill").style.width = r * 100 + "%";
            $("t-cur").textContent = fmt(settings.pos.time);
          }
        } else if (s.duration) {
          $("t-dur").textContent = fmt(s.duration);
        }
        if (desktopLyrics.enabled) {
          desktopLyrics.key = "";
          desktopBeginSong(s);
          loadLyrics(s);
        }
      }
    }
  } catch (e) {}
}
window.addEventListener("pywebviewready", desktopReadState);
boot();

// ---------------- 退出/切后台前统一 flush (新增路径, 绕过防抖等待) ----------------
// pagehide: 卸载阶段用 keepalive fetch 发出尚未保存的队列/设置; 不用同步 XHR(Chromium 卸载阶段已禁用)
window.addEventListener("pagehide", flushPendingSaves);
// 切到后台(visibilityState=hidden)时同样立即 flush, 避免防抖窗口内的修改因页面挂起而丢失
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") flushPendingSaves();
});
