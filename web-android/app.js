/* MusicHub Android 移动端界面: 复用同一套 /api/* 后端, 只重写交互与布局 */
"use strict";

const TOKEN = (document.querySelector('meta[name="mh-token"]') || {}).content || "";
const $ = (id) => document.getElementById(id);
const SRC_NAME = {
  netease: "网易云", qq: "QQ音乐", migu: "咪咕", bilibili: "哔哩哔哩", douyin: "抖音",
};
const SRC_ORDER = ["netease", "qq", "migu", "bilibili", "douyin"];

const state = {
  source: "netease",
  queue: [],
  index: -1,
  mode: "loop",       // loop | order | single | shuffle
  view: null,
  homeCards: [],
  searchSongs: [],
  login: {},          // src -> status 对象
  tab: "home",        // 一级为音源时的二级页签: home | mylists | history
  root: "src",        // src | mine
  mineLists: [],      // 本机歌单
};

const audio = $("audio");

/* ---------------- 基础工具 ---------------- */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function fmt(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m + ":" + (s < 10 ? "0" : "") + s;
}
function withTok(url) {
  return url + (url.indexOf("?") === -1 ? "?" : "&") + "t=" + encodeURIComponent(TOKEN);
}
async function api(url, opts) {
  const o = Object.assign({}, opts);
  o.headers = Object.assign({}, o.headers, { "X-MH-Token": TOKEN });
  const r = await fetch(url, o);
  return r.json();
}
let toastTimer = null;
function toast(msg, ms) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms || 2200);
}

/* ---------------- 媒体地址 ---------------- */
function proxyUrl(s) {
  if (s.source === "local") return withTok("/file?name=" + encodeURIComponent(s.file || s.id || ""));
  return withTok("/proxy?source=" + encodeURIComponent(s.source) +
    "&id=" + encodeURIComponent(s.id) +
    "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {})));
}
// 可代理的图片源(与 server.IMG_HOSTS 白名单一致)
const IMG_PROXY_HOST = /(^|\.)(music\.126\.net|gtimg\.cn|y\.qq\.com|qpic\.cn|hdslb\.com|biliimg\.com|bilibili\.com|bytecdn\.cn|byteimg\.com|douyincdn\.com|douyinpic\.com|douyinstatic\.com|ixigua\.com|pstatp\.com|snssdk\.com|iesdouyin\.com)$/i;
function coverUrl(url) {
  let c = String(url || "").trim();
  if (!c) return "";
  if (c.startsWith("data:") || c.startsWith("blob:")) return c;
  if (c.startsWith("//")) c = "https:" + c;
  if (!/^https?:\/\//i.test(c)) return c;
  // 统一走本地图片代理: 绕开安卓明文限制/防盗链/证书问题, 由后端补 UA+Referer 拉取
  try {
    if (IMG_PROXY_HOST.test(new URL(c).hostname)) {
      return withTok("/imgproxy?url=" + encodeURIComponent(c));
    }
  } catch (e) {}
  return c.replace(/^http:\/\//i, "https://");
}

/* ---------------- 列表渲染 ---------------- */
function vipTag(s) {
  return s && s.extra && s.extra.vip ? '<span class="vip-tag">VIP</span>' : "";
}
function rowHtml(s, i) {
  const cov = coverUrl(s.cover);
  return '<div class="row" data-i="' + i + '">' +
    '<div class="idx">' + (i + 1) + "</div>" +
    '<div class="cov"' + (cov ? ' style="background-image:url(\'' + esc(cov) + '\')"' : "") + ">" +
    (cov ? "" : "♪") + "</div>" +
    '<div class="info"><div class="t">' + esc(s.title || "未命名") + vipTag(s) + "</div>" +
    '<div class="s">' + esc((s.artist || "未知") + " · " + (SRC_NAME[s.source] || s.source || "")) + "</div></div>" +
    '<div class="dur">' + (s.duration ? fmt(s.duration) : "") + "</div></div>";
}
function renderList(songs, title, opts) {
  state.queue = songs || [];
  state.index = -1;
  if (!songs || !songs.length) {
    $("view").innerHTML = '<div class="loading">没有内容</div>';
    return;
  }
  const count = opts && opts.noCount ? "" : "<span>" + songs.length + " 首</span>";
  $("view").innerHTML = '<section class="section"><div class="sec-head"><h3>' +
    esc(title || "播放列表") + "</h3>" + count + "</div>" +
    '<div class="list">' + songs.map(rowHtml).join("") + "</div></section>";
}

/* ---------------- 首页 ---------------- */
async function loadHome(nocache) {
  const src = state.source;
  state.tab = "home";
  renderTabs();
  const view = state.view = { type: "home", src: src };
  notifyBack();
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  try {
    const homeReq = api("/api/home?source=" + encodeURIComponent(src) + "&limit=30" +
      (nocache ? "&nocache=1" : ""));
    // 网易云/QQ 默认展示每日推荐(歌单外的歌曲列表)
    const dailyReq = (src === "netease" || src === "qq") ? fetchDaily(src) : Promise.resolve([]);
    const [j, daily] = await Promise.all([homeReq, dailyReq]);
    if (state.view !== view) return;
    if (Array.isArray(j.songs) && j.songs.length) {
      renderList(j.songs, (SRC_NAME[src] || src) + " · 推荐");
      return;
    }
    const sections = [];
    if (daily.length) sections.push(["每日推荐", daily, "daily"]);
    if (j.playlists && j.playlists.length) sections.push(["推荐歌单", j.playlists, "cards", true]);
    if (j.charts && j.charts.length) sections.push(["排行榜", j.charts]);
    if (sections.length) { renderHome(sections); return; }
    await loadRecommend({}, "推荐");
  } catch (e) {
    $("view").innerHTML = '<div class="loading">加载失败：' + esc(e) + "</div>";
  }
}

async function fetchDaily(src) {
  try {
    const mode = src === "netease" ? "&mode=daily" : "";
    const j = await api("/api/recommend?source=" + encodeURIComponent(src) + mode + "&limit=50");
    return (j.songs || []).slice(0, 50);
  } catch (e) {
    return [];
  }
}

function renderHome(sections) {
  state.homeCards = sections;
  $("view").innerHTML = sections.map((sec, si) => {
    const title = sec[0], cards = sec[1] || [], kind = sec[2] || "cards", refresh = !!sec[3];
    if (kind === "daily") {
      // 每日推荐: 一张卡片入口, 点击进入歌曲列表
      const first = cards[0] || {};
      const cov = coverUrl(first.cover);
      return '<section class="section"><div class="sec-head"><h3>' + esc(title) + "</h3></div>" +
        '<button class="daily-card" type="button" data-daily="1"' +
        (cov ? ' style="background-image:url(\'' + esc(cov) + '\')"' : "") + ">" +
        '<span class="dmask"></span><span class="dmeta"><span class="dtitle">每日推荐</span>' +
        '<span class="dsub">根据你的口味生成 · ' + cards.length + " 首</span></span></button></section>";
    }
    const right = refresh
      ? '<div class="sec-right"><span>' + cards.length + '</span>' +
        '<button class="sec-refresh" type="button" data-refresh="1">↻ 刷新</button></div>'
      : "<span>" + cards.length + "</span>";
    return '<section class="section"><div class="sec-head"><h3>' + esc(title) +
      "</h3>" + right + '</div><div class="grid">' +
      cards.map((c, ci) => {
        const cov = coverUrl(c.cover);
        return '<div class="card" data-si="' + si + '" data-ci="' + ci + '">' +
          '<div class="cov"' + (cov ? ' style="background-image:url(\'' + esc(cov) + '\')"' : "") + ">" +
          (cov ? "" : "♪") + "</div>" +
          '<div class="cap">' + esc(c.title || "未命名") + "</div></div>";
      }).join("") + "</div></section>";
  }).join("");
}

function openDaily() {
  if (state.source === "netease") loadRecommend({ mode: "daily" }, "每日推荐");
  else loadRecommend({}, "每日推荐");
}

async function openCard(c) {
  const src = c.src || state.source;
  try {
    if (c.action === "playlist" && c.ref) {
      if (src === "douyin") {
        if (c.keyword) return doSearch(c.keyword);
        return loadRecommend({ mode: "hot" }, c.title || "推荐");
      }
      if (src === "qq" && c.topid) return loadRecommend({ topid: c.topid }, c.title || "排行榜");
      return loadPlaylistOf(src, c.ref, c.title);
    }
    if (c.topid) {
      return loadRecommend({ topid: c.topid, mode: src === "netease" ? "toplist" : "" }, c.title || "排行榜");
    }
    if (c.ref) return loadPlaylistOf(src, c.ref, c.title);
    if (c.keyword) return doSearch(c.keyword);
  } catch (e) {
    toast("打开失败：" + e);
  }
}

async function loadPlaylistOf(src, ref, title) {
  const view = state.view = { type: "playlist", src: src, ref: ref };
  notifyBack();
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  const j = await api("/api/playlist?source=" + encodeURIComponent(src) +
    "&ref=" + encodeURIComponent(ref));
  if (state.view !== view) return;
  renderList(j.songs || [], title || j.title || "歌单");
}

async function loadRecommend(opts, title) {
  const src = state.source;
  const view = state.view = Object.assign({ type: "recommend", src: src, pn: 1 }, opts || {});
  notifyBack();
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  // 网易云/QQ 接口支持大分页, 直接全量拉取; 其他音源按页取, 由下拉继续加载
  const limit = (src === "netease" || src === "qq") ? 300 : 60;
  const params = Object.assign({ source: src, limit: limit, pn: 1 }, opts || {});
  const q = new URLSearchParams(params);
  const j = await api("/api/recommend?" + q.toString());
  if (state.view !== view) return;
  // B站推荐不显示条数统计
  renderList(j.songs || [], title || "推荐", { noCount: src === "bilibili" });
}

function canPullMore() {
  return !!(state.view && state.view.type === "recommend" && state.view.src === "bilibili");
}

// B站推荐: 下拉加载下一页, 新内容排在最上方, 最多 100 条, 超出丢弃最早加载的
async function loadMoreRecommend() {
  const v = state.view;
  if (!canPullMore()) return;
  v.pn += 1;
  const q = new URLSearchParams({ source: v.src, limit: 20, pn: v.pn });
  if (v.mode) q.set("mode", v.mode);
  let j;
  try { j = await api("/api/recommend?" + q.toString()); } catch (e) {
    toast("加载失败：" + e);
    return;
  }
  if (state.view !== v) return;
  const seen = new Set(state.queue.map(songKey));
  const fresh = (j.songs || []).filter((s) => !seen.has(songKey(s)));
  if (!fresh.length) { toast("没有更多推荐了"); return; }
  const playingKey = state.index >= 0 ? songKey(state.queue[state.index]) : null;
  let queue = fresh.concat(state.queue);
  // 超过 100 时从末尾(最早加载的)开始丢弃, 保持 100 条且最新在最上
  if (queue.length > 100) queue = queue.slice(0, 100);
  state.queue = queue;
  state.index = playingKey ? queue.findIndex((s) => songKey(s) === playingKey) : -1;
  const list = document.querySelector("#view .list");
  if (list) list.innerHTML = queue.map(rowHtml).join("");
  markRows();
  toast("已加载 " + fresh.length + " 条推荐");
}

/* ---------------- 搜索 ---------------- */
const HISTORY_KEY = "mh.search.history";
function loadHistory() {
  try {
    const arr = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(arr) ? arr.filter((x) => x && x.kw) : [];
  } catch (e) {
    return [];
  }
}
function saveHistory(list) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify((list || []).slice(0, 20))); } catch (e) {}
}
function addHistory(kw, src) {
  kw = (kw || "").trim();
  if (!kw) return;
  const list = loadHistory().filter((x) => !(x.kw === kw && x.source === src));
  list.unshift({ kw: kw, source: src, ts: Date.now() });
  saveHistory(list);
}
function renderHistory() {
  const list = loadHistory();
  if (!list.length) return '<div class="loading">输入关键词开始搜索</div>';
  return '<section class="hist"><div class="hist-head"><h3>搜索记录</h3>' +
    '<button id="hist-clear" type="button">清空</button></div>' +
    list.map((h, i) => '<button class="hist-row" type="button" data-hi="' + i + '">' +
      '<span class="hist-kw">' + esc(h.kw) + "</span>" +
      '<span class="hist-src">' + esc(SRC_NAME[h.source] || h.source || "") + "</span></button>").join("") +
    "</section>";
}

async function doSearch(kw, srcOverride) {
  kw = (kw || "").trim();
  if (!kw) return;
  const src = srcOverride || state.source;
  addHistory(kw, src);
  $("searchpage").classList.remove("hidden");
  $("sinput").value = kw;
  $("sresults").innerHTML = '<div class="loading">搜索中…</div>';
  try {
    const j = await api("/api/search?source=" + encodeURIComponent(src) +
      "&keyword=" + encodeURIComponent(kw) + "&limit=40");
    const songs = j.songs || [];
    state.searchSongs = songs;
    $("sresults").innerHTML = songs.length
      ? songs.map(rowHtml).join("")
      : '<div class="loading">' + esc(j.error || "没有结果") + "</div>";
  } catch (e) {
    $("sresults").innerHTML = '<div class="loading">搜索失败：' + esc(e) + "</div>";
  }
}

/* ---------------- 播放器 ---------------- */
function playAt(i) {
  if (i < 0 || i >= state.queue.length) return;
  state.index = i;
  const s = state.queue[i];
  audio.src = proxyUrl(s);
  audio.play().catch(() => toast("播放失败，可能需要先登录该音源"));
  updatePlayerBar(s);
  markRows();
  // 视频源(B站/抖音): 打开静音画面并跟随音频
  if (isVideoSong(s)) openVideo(s); else closeVideo();
}
function updatePlayerBar(s) {
  $("player").classList.remove("hidden");
  $("ptitle").innerHTML = esc(s.title || "未在播放") + vipTag(s);
  $("partist").textContent = (s.artist || "未知") + " · " + (SRC_NAME[s.source] || s.source || "");
  const cov = coverUrl(s.cover);
  const box = $("pcover");
  box.innerHTML = cov ? '<img src="' + esc(cov) + '" alt="">' : "♪";
}
function setToggleIcon() {
  $("ptoggle").textContent = audio.paused ? "▶" : "❚❚";
}
function markRows() {
  document.querySelectorAll(".row").forEach((el) => {
    el.classList.toggle("on", Number(el.dataset.i) === state.index);
  });
  const list = $("queue-list");
  if (list) {
    list.querySelectorAll(".row").forEach((el) => {
      el.classList.toggle("on", Number(el.dataset.i) === state.index);
    });
  }
}
function playStep(step) {
  if (!state.queue.length) return;
  const n = state.queue.length;
  if (state.mode === "single" && step > 0) {
    audio.currentTime = 0;
    audio.play().catch(() => {});
    return;
  }
  let i = state.index;
  if (state.mode === "shuffle") {
    i = Math.floor(Math.random() * n);
  } else {
    i = i + step;
    if (i < 0) i = state.mode === "order" ? 0 : n - 1;
    if (i >= n) i = state.mode === "order" ? n - 1 : 0;
  }
  playAt(i);
}

/* ---------------- 队列面板 ---------------- */
function openQueue() {
  const list = $("queue-list");
  list.innerHTML = state.queue.length
    ? state.queue.map(rowHtml).join("")
    : '<div class="loading">队列为空</div>';
  $("sheet").classList.remove("hidden");
  markRows();
  notifyBack();
}

/* ---------------- 长按操作菜单 ---------------- */
let lpTimer = null, lpStart = null, lpMoved = false, suppressClick = false;
let menuActions = [];

function songKey(s) {
  return (s.source || "") + ":" + s.id + ":" + JSON.stringify(s.extra || {});
}
function suppressNextClick() {
  suppressClick = true;
  setTimeout(() => { suppressClick = false; }, 800);
}
function consumeSuppress() {
  if (!suppressClick) return false;
  suppressClick = false;
  return true;
}
function openMenu(title, labels, actions) {
  menuActions = actions || [];
  $("menu-title").textContent = title || "操作";
  $("menu-body").innerHTML = labels.map((t, i) =>
    '<button class="menu-item" type="button" data-mi="' + i + '">' + esc(t) + "</button>").join("") +
    '<button class="menu-item cancel" type="button" data-cancel="1">取消</button>';
  $("menu").classList.remove("hidden");
  notifyBack();
}
function closeMenu() {
  $("menu").classList.add("hidden");
  notifyBack();
}
function queueInsert(s, where) {
  if (!s) return;
  const key = songKey(s);
  const exists = state.queue.some((x) => songKey(x) === key);
  if (!exists) {
    if (!state.queue.length || state.index < 0) {
      state.queue.unshift(s);
      if (where === "next") state.index = -1;
    } else {
      const at = where === "next" ? Math.min(state.index + 1, state.queue.length) : state.queue.length;
      state.queue.splice(at, 0, s);
    }
  }
  if (!$("sheet").classList.contains("hidden")) openQueue();
  toast(where === "next" ? "已设为下一首播放" : "已加入播放队列");
}
function openSongMenu(s, play) {
  openMenu(s.title || "歌曲", ["立即播放", "下一首播放", "加入队列末尾", "收藏到歌单"], [
    () => { if (play) play(); },
    () => queueInsert(s, "next"),
    () => queueInsert(s, "end"),
    () => openFavMenu(s),
  ]);
}

/* ---------------- 本机歌单: 新建 / 重命名 / 删除 / 收藏 ---------------- */
let inputCb = null;
function askText(title, value, cb) {
  inputCb = cb;
  $("input-title").textContent = title || "输入";
  $("input-field").value = value || "";
  $("inputsheet").classList.remove("hidden");
  notifyBack();
  setTimeout(() => { try { $("input-field").focus(); } catch (e) {} }, 50);
}
function closeInput() {
  $("inputsheet").classList.add("hidden");
  inputCb = null;
  notifyBack();
}
async function localPl(action, payload) {
  const r = await fetch("/api/localplaylist/" + action, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-MH-Token": TOKEN },
    body: JSON.stringify(payload || {}),
  });
  try { return await r.json(); } catch (e) { return { error: String(e) }; }
}
function newLocalList() {
  askText("新建歌单", "", async (name) => {
    const r = await localPl("create", { name: name });
    if (r.error) { toast(r.error); return; }
    toast("已创建歌单");
    loadMineView();
  });
}
function openLocalMenu(p) {
  openMenu(p.name || "歌单", ["重命名", "删除"], [
    () => askText("重命名歌单", p.name, async (name) => {
      const r = await localPl("rename", { id: p.id, name: name });
      if (r.error) { toast(r.error); return; }
      toast("已重命名");
      loadMineView();
    }),
    () => openMenu("删除「" + (p.name || "歌单") + "」？", ["删除", "取消"], [
      async () => {
        const r = await localPl("delete", { id: p.id });
        if (r.error) { toast(r.error); return; }
        toast("已删除");
        loadMineView();
      },
      () => {},
    ]),
  ]);
}
async function openFavMenu(s) {
  let lists = [];
  try {
    const j = await api("/api/locallists");
    lists = j.lists || [];
  } catch (e) {}
  const labels = lists.map((p) => "收藏到：" + (p.name || "歌单"));
  const actions = lists.map((p) => () => addToLocalList(p.id, s));
  labels.push("＋ 新建歌单并收藏");
  actions.push(() => askText("新建歌单", "", async (name) => {
    const r = await localPl("create", { name: name });
    if (r.error) { toast(r.error); return; }
    addToLocalList(r.id, s);
  }));
  openMenu("收藏到歌单", labels, actions);
}
async function addToLocalList(pid, s) {
  const r = await localPl("add", { id: pid, song: s });
  if (r.error) { toast(r.error); return; }
  toast(r.existed ? "已在歌单中" : "已收藏");
}
$("input-close").addEventListener("click", closeInput);
$("input-cancel").addEventListener("click", closeInput);
$("input-ok").addEventListener("click", () => {
  const v = ($("input-field").value || "").trim();
  if (!v) { toast("请输入名称"); return; }
  const cb = inputCb;
  closeInput();
  if (cb) cb(v);
});
$("input-field").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("input-ok").click();
});
function bindLongPress(root, resolve) {
  if (!root) return;
  root.addEventListener("touchstart", (e) => {
    if (e.touches.length !== 1) return;
    const el = e.target.closest(".row, .card");
    if (!el) return;
    const t = e.touches[0];
    lpStart = { x: t.clientX, y: t.clientY };
    lpMoved = false;
    if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; }
    lpTimer = setTimeout(() => {
      lpTimer = null;
      if (lpMoved) return;
      suppressNextClick();
      const r = resolve(el);
      if (!r) return;
      if (r.song) openSongMenu(r.song, r.play);
      else if (r.card) openMenu(r.card.title || "操作", ["打开"], [() => openCard(r.card)]);
      else if (r.local) openLocalMenu(r.local);
    }, 550);
  }, { passive: true });
  root.addEventListener("touchmove", (e) => {
    if (!lpTimer || !lpStart) return;
    const t = e.touches[0];
    if (Math.abs(t.clientX - lpStart.x) > 10 || Math.abs(t.clientY - lpStart.y) > 10) {
      lpMoved = true;
      clearTimeout(lpTimer);
      lpTimer = null;
    }
  }, { passive: true });
  const stop = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };
  root.addEventListener("touchend", stop, { passive: true });
  root.addEventListener("touchcancel", stop, { passive: true });
}

/* ---------------- 事件绑定 ---------------- */
function chipHtml(key, label, on, act) {
  return '<button class="chip' + (on ? " on" : "") + '" data-src="' + key + '"' +
    (act ? ' data-act="' + act + '"' : "") + ">" + esc(label) + "</button>";
}
function renderSources() {
  const el = $("sources");
  // 一级导航固定: 我的 + 五个音源
  el.innerHTML = chipHtml("mine", "我的", state.root === "mine", "mine") +
    SRC_ORDER.map((k) =>
      chipHtml(k, SRC_NAME[k], state.root !== "mine" && k === state.source)).join("");
  const on = el.querySelector(".chip.on");
  if (on) on.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
}

/* ---------------- 页签: 推荐 / 歌单 / 历史 ---------------- */
function tabsForSrc(src) {
  const tabs = [{ key: "home", label: "推荐" }];
  if (src === "netease" || src === "qq") tabs.push({ key: "mylists", label: "歌单" });
  if (src === "bilibili" || src === "douyin") tabs.push({ key: "history", label: "历史" });
  return tabs;
}
function renderTabs() {
  const el = $("tabs");
  if (state.root === "mine") {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  const tabs = tabsForSrc(state.source);
  if (tabs.length < 2) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = tabs.map((t) =>
    '<button class="tab' + (t.key === (state.tab || "home") ? " on" : "") +
    '" type="button" data-tab="' + t.key + '">' + esc(t.label) + "</button>").join("");
}
function switchTab(key) {
  if (state.root === "mine") return;
  state.tab = key;
  renderTabs();
  if (key === "mylists") return loadMyListsView();
  if (key === "history") return loadHistoryView();
  return loadHome();
}

/* ---------------- 全局翻页(侧滑): 我的 -> 各音源的推荐/歌单/历史 ---------------- */
function pageList() {
  const pages = [{ root: "mine", tab: "mine" }];
  SRC_ORDER.forEach((src) => {
    tabsForSrc(src).forEach((t) => pages.push({ root: "src", src: src, tab: t.key }));
  });
  return pages;
}
function currentPageIndex() {
  if (state.root === "mine") return 0;
  return pageList().findIndex((p) =>
    p.root === "src" && p.src === state.source && p.tab === (state.tab || "home"));
}
function openPage(p) {
  if (!p) return;
  if (p.root === "mine") {
    state.root = "mine";
    renderSources();
    renderTabs();
    return loadMineView();
  }
  state.root = "src";
  if (state.source !== p.src) {
    state.source = p.src;
    renderSources();
  }
  state.tab = p.tab;
  renderTabs();
  if (p.tab === "mylists") return loadMyListsView();
  if (p.tab === "history") return loadHistoryView();
  return loadHome();
}
function stepPage(step) {
  const pages = pageList();
  if (!pages.length) return;
  let i = currentPageIndex();
  if (i < 0) i = 0;
  openPage(pages[(i + step + pages.length) % pages.length]);
}

$("sources").addEventListener("click", (e) => {
  const b = e.target.closest(".chip");
  if (!b) return;
  if (b.dataset.act === "mine") {
    state.root = "mine";
    renderSources();
    renderTabs();
    loadMineView();
    return;
  }
  if (b.dataset.src === "mine") return;
  state.root = "src";
  state.source = b.dataset.src;
  state.tab = "home";
  renderSources();
  renderTabs();
  loadHome();
});
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab");
  if (!b || !b.dataset.tab || b.dataset.tab === (state.tab || "home")) return;
  switchTab(b.dataset.tab);
});

$("view").addEventListener("click", (e) => {
  if (consumeSuppress()) return;
  if (e.target.closest("[data-refresh]")) { loadHome(true); return; }
  if (e.target.closest("[data-daily]")) { openDaily(); return; }
  if (e.target.closest("[data-newpl]")) { newLocalList(); return; }
  const lr = e.target.closest(".row[data-li]");
  if (lr) { openLocalList(Number(lr.dataset.li)); return; }
  const card = e.target.closest(".card");
  if (card) {
    const sec = state.homeCards[Number(card.dataset.si)];
    if (sec) openCard(sec[1][Number(card.dataset.ci)]);
    return;
  }
  const row = e.target.closest(".row");
  if (row && row.dataset.i != null) playAt(Number(row.dataset.i));
});

$("search-open").addEventListener("click", () => {
  $("searchpage").classList.remove("hidden");
  // 每次打开都清空输入与旧结果, 只保留搜索记录
  $("sinput").value = "";
  state.searchSongs = [];
  $("sresults").innerHTML = renderHistory();
  $("sinput").focus();
  notifyBack();
});
$("sback").addEventListener("click", () => {
  $("searchpage").classList.add("hidden");
  notifyBack();
});
$("sinput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch($("sinput").value);
});
$("sresults").addEventListener("click", (e) => {
  if (consumeSuppress()) return;
  if (e.target.closest("#hist-clear")) {
    saveHistory([]);
    $("sresults").innerHTML = renderHistory();
    return;
  }
  const hb = e.target.closest(".hist-row");
  if (hb) {
    const h = loadHistory()[Number(hb.dataset.hi)];
    if (h) doSearch(h.kw, h.source);
    return;
  }
  const row = e.target.closest(".row");
  if (!row) return;
  state.queue = state.searchSongs.slice();
  playAt(Number(row.dataset.i));
  $("searchpage").classList.add("hidden");
  notifyBack();
});

$("ptoggle").addEventListener("click", () => {
  if (!audio.src) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
});
$("pprev").addEventListener("click", () => playStep(-1));
$("pnext").addEventListener("click", () => playStep(1));
$("pqueue").addEventListener("click", openQueue);
$("sheet-close").addEventListener("click", () => {
  $("sheet").classList.add("hidden");
  notifyBack();
});
$("queue-list").addEventListener("click", (e) => {
  if (consumeSuppress()) return;
  const row = e.target.closest(".row");
  if (!row) return;
  playAt(Number(row.dataset.i));
});

$("menu-close").addEventListener("click", closeMenu);
$("menu-body").addEventListener("click", (e) => {
  const b = e.target.closest(".menu-item");
  if (!b) return;
  const idx = b.dataset.mi;
  closeMenu();
  if (b.dataset.cancel != null || idx == null) return;
  const fn = menuActions[Number(idx)];
  if (fn) fn();
});
// 关闭系统默认长按菜单(文字复制/图片另存)
document.addEventListener("contextmenu", (e) => e.preventDefault());

bindLongPress($("view"), (el) => {
  if (el.classList.contains("card")) {
    const sec = state.homeCards[Number(el.dataset.si)];
    const card = sec && sec[1][Number(el.dataset.ci)];
    return card ? { card: card } : null;
  }
  if (el.dataset.li != null) {
    const p = (state.mineLists || [])[Number(el.dataset.li)];
    return p ? { local: p } : null;
  }
  const i = Number(el.dataset.i);
  const s = state.queue[i];
  return s ? { song: s, play: () => playAt(i) } : null;
});
bindLongPress($("sresults"), (el) => {
  const i = Number(el.dataset.i);
  const s = state.searchSongs[i];
  if (!s) return null;
  return { song: s, play: () => {
    state.queue = state.searchSongs.slice();
    playAt(i);
    $("searchpage").classList.add("hidden");
    notifyBack();
  } };
});
bindLongPress($("queue-list"), (el) => {
  const i = Number(el.dataset.i);
  const s = state.queue[i];
  return s ? { song: s, play: () => playAt(i) } : null;
});

/* ---------------- 左右滑动: 全局翻页(我的/各音源页签) ---------------- */
let swipeFrom = null;
document.addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1 || hasOpenLayer()) { swipeFrom = null; return; }
  const t = e.touches[0];
  // 左缘交给返回手势; 顶部导航与播放条自身可横向滚动/操作, 不参与翻页
  if (t.clientX <= 24) { swipeFrom = null; return; }
  if (e.target.closest("#sources, #tabs, #player, #top, input, textarea")) {
    swipeFrom = null;
    return;
  }
  swipeFrom = { x: t.clientX, y: t.clientY, at: Date.now(), horizontal: false };
}, { passive: true });
document.addEventListener("touchmove", (e) => {
  if (!swipeFrom) return;
  const t = e.touches[0];
  const dx = t.clientX - swipeFrom.x, dy = t.clientY - swipeFrom.y;
  if (Math.abs(dx) > 12 && Math.abs(dx) > Math.abs(dy) * 1.2) swipeFrom.horizontal = true;
}, { passive: true });
document.addEventListener("touchend", (e) => {
  const s = swipeFrom;
  swipeFrom = null;
  if (!s || !s.horizontal) return;
  const t = e.changedTouches[0];
  const dx = t.clientX - s.x;
  const dy = Math.abs(t.clientY - s.y);
  if (Date.now() - s.at > 800) return;
  if (Math.abs(dx) < 80 || dy > Math.abs(dx) * 0.8) return;
  // 左滑下一页, 右滑上一页; 空列表同样可翻(绑定在整个页面而非列表上)
  stepPage(dx < 0 ? 1 : -1);
}, { passive: true });
document.addEventListener("touchcancel", () => { swipeFrom = null; }, { passive: true });

/* ---------------- 下拉加载更多 / 刷新 ---------------- */
let pullFrom = null, pulling = false;
document.addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1 || hasOpenLayer() || !canPullMore()) { pullFrom = null; return; }
  if (e.target.closest("#sources, #tabs, #player, #top, input, textarea")) {
    pullFrom = null;
    return;
  }
  const t = e.touches[0];
  const el = document.scrollingElement || document.documentElement;
  pullFrom = { x: t.clientX, y: t.clientY, top: el.scrollTop };
}, { passive: true });
document.addEventListener("touchmove", (e) => {
  if (!pullFrom) return;
  if (pullFrom.top > 0 || !canPullMore()) { pullFrom = null; return; }
  const t = e.touches[0];
  const dy = t.clientY - pullFrom.y;
  const dx = Math.abs(t.clientX - pullFrom.x);
  if (dy > 45 && dy > dx * 1.2) {
    if (!pulling) {
      pulling = true;
      const h = $("pullhint");
      h.textContent = "松开加载更多推荐";
      h.classList.remove("hidden");
    }
  }
}, { passive: true });
document.addEventListener("touchend", async (e) => {
  const from = pullFrom;
  pullFrom = null;
  if (!pulling) return;
  pulling = false;
  $("pullhint").classList.add("hidden");
  if (!from || !canPullMore()) return;
  const dy = e.changedTouches[0].clientY - from.y;
  if (dy >= 70) await loadMoreRecommend();
}, { passive: true });
document.addEventListener("touchcancel", () => {
  pullFrom = null;
  if (pulling) { pulling = false; $("pullhint").classList.add("hidden"); }
}, { passive: true });

$("pbar").addEventListener("click", (e) => {
  if (!audio.duration) return;
  const r = e.getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  audio.currentTime = ratio * audio.duration;
});

audio.addEventListener("play", () => { state.playing = true; setToggleIcon(); });
audio.addEventListener("pause", () => { state.playing = false; setToggleIcon(); });
audio.addEventListener("timeupdate", () => {
  const d = audio.duration || 0, t = audio.currentTime || 0;
  $("pfill").style.width = (d ? Math.min(1, t / d) * 100 : 0) + "%";
  $("ptcur").textContent = fmt(t);
  $("ptdur").textContent = fmt(d);
});
audio.addEventListener("ended", () => playStep(1));
audio.addEventListener("error", () => {
  if (audio.src) toast("音频加载失败，请检查网络或登录状态");
});
/* 切歌模式: 点时间栏循环切换 */
$("ptdur").addEventListener("click", () => {
  const order = ["loop", "order", "single", "shuffle"];
  const name = { loop: "列表循环", order: "顺序播放", single: "单曲循环", shuffle: "随机播放" };
  state.mode = order[(order.indexOf(state.mode) + 1) % order.length];
  toast(name[state.mode], 1200);
});

/* ---------------- 返回 / 侧滑手势 ---------------- */
const BACK_LAYERS = [
  ["inputsheet", () => closeInput()],
  ["menu", () => closeMenu()],
  ["loginpage", () => closeLogin()],
  ["videopage", () => closeVideo()],
  ["searchpage", () => $("searchpage").classList.add("hidden")],
  ["sheet", () => $("sheet").classList.add("hidden")],
];
function hasOpenLayer() {
  return BACK_LAYERS.some(([id]) => !$(id).classList.contains("hidden"));
}
// 通知原生: 有可返回的内容时才拦截系统返回键, 否则交还给系统退出应用
function notifyBack() {
  const canBack = hasOpenLayer() || !!(state.view && state.view.type !== "home");
  const native = window.MHNative;
  if (native && native.setBackAvailable) {
    try { native.setBackAvailable(canBack); } catch (e) {}
  }
}
// 原生返回键/侧滑系统手势都会调到这个函数
window.mhBack = function () {
  for (const [id, close] of BACK_LAYERS) {
    if (!$(id).classList.contains("hidden")) {
      close();
      notifyBack();
      return true;
    }
  }
  if (state.root === "mine") {
    // 我的: 歌单详情先回到我的歌单列表, 列表页再返回则回到当前音源推荐
    if (state.view && state.view.type === "localpl") {
      loadMineView();
      return true;
    }
    state.root = "src";
    state.tab = "home";
    renderSources();
    renderTabs();
    loadHome();
    return true;
  }
  if (state.view && state.view.type !== "home") {
    loadHome();
    return true;
  }
  return false;
};
// 手动侧滑: 左缘右滑返回(三键导航或个别机型系统未接管时生效)
let edgeX = null, edgeY = 0, edgeAt = 0;
document.addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1) { edgeX = null; return; }
  const t = e.touches[0];
  edgeX = t.clientX <= 24 ? t.clientX : null;
  edgeY = t.clientY;
  edgeAt = Date.now();
}, { passive: true });
document.addEventListener("touchend", (e) => {
  if (edgeX == null) return;
  const t = e.changedTouches[0];
  const dx = t.clientX - edgeX;
  const dy = Math.abs(t.clientY - edgeY);
  const ok = dx > 60 && dy < dx * 0.6 && Date.now() - edgeAt < 700;
  edgeX = null;
  if (ok) window.mhBack();
}, { passive: true });
document.addEventListener("touchcancel", () => { edgeX = null; }, { passive: true });

/* ---------------- 视频画面(B站/抖音) ---------------- */
const vid = $("vid");
let videoTimer = null;

function isVideoSong(s) {
  return !!s && (s.source === "bilibili" || s.source === "douyin");
}
function videoUrl(s) {
  return withTok("/proxy?source=" + encodeURIComponent(s.source) +
    "&id=" + encodeURIComponent(s.id) + "&kind=video&extra=" +
    encodeURIComponent(JSON.stringify(s.extra || {})));
}
function openVideo(song) {
  const s = song || state.queue[state.index];
  if (!isVideoSong(s)) return;
  $("video-title").textContent = s.title || "";
  $("videopage").classList.remove("hidden");
  if (vid.dataset.key !== String(s.id)) {
    vid.dataset.key = String(s.id);
    vid.src = videoUrl(s);
  }
  try { vid.currentTime = audio.currentTime || 0; } catch (e) {}
  vid.play().catch(() => {});
  if (videoTimer) clearInterval(videoTimer);
  videoTimer = setInterval(syncVideo, 400);
  notifyBack();
}
function closeVideo() {
  $("videopage").classList.add("hidden");
  if (videoTimer) { clearInterval(videoTimer); videoTimer = null; }
  try { vid.pause(); } catch (e) {}
  notifyBack();
}
function syncVideo() {
  const s = state.queue[state.index];
  if (!isVideoSong(s)) { closeVideo(); return; }
  if (audio.paused) { if (!vid.paused) vid.pause(); }
  else if (vid.paused) { vid.play().catch(() => {}); }
  if (vid.duration && Math.abs(vid.currentTime - audio.currentTime) > 0.7) {
    try { vid.currentTime = audio.currentTime; } catch (e) {}
  }
}

/* ---------------- 登录(扫码) ---------------- */
const QR_LOGIN = {
  netease: {
    name: "网易云", qr: "/api/netease/qr", poll: "/api/netease/poll",
    tip: "打开网易云音乐 App 扫码登录",
    state(j) {
      if (j.code === 803) return ["ok", "登录成功"];
      if (j.code === 802) return ["scanned", "已扫码，请在手机上确认"];
      if (j.code === 800) return ["expired", "二维码已过期"];
      if (j.code === 8821) return ["blocked", "扫码登录被网易云风控拦截，请稍后重试"];
      if (j.error) return ["wait", "查询失败：" + j.error];
      return ["wait", "等待扫码…"];
    },
  },
  bilibili: {
    name: "哔哩哔哩", qr: "/api/bili/qr", poll: "/api/bili/poll",
    tip: "打开哔哩哔哩 App 扫码登录",
    state(j) {
      if (j.code === 0 && j.logged_in) return ["ok", "登录成功"];
      if (j.code === 86090) return ["scanned", "已扫码，请在手机上确认"];
      if (j.code === 86038) return ["expired", "二维码已过期"];
      if (j.code === 0) return ["blocked", j.message || "平台未返回登录凭证"];
      if (j.code === -1 || j.error) return ["wait", "查询失败：" + (j.message || j.error)];
      return ["wait", "等待扫码…"];
    },
  },
};
const LOGIN_WEB_ONLY = [["qq", "QQ音乐"], ["douyin", "抖音"]];

let qrTimer = null;
let qrSeq = 0;
let qrBlocked = false;
function stopQr() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
  qrSeq++;
}

// 网页登录: 打开原生 WebView 里的官方登录页, 登录后由原生回收 Cookie
function openWebLogin(key) {
  const native = window.MHNative;
  if (native && native.openWebLogin) {
    try { native.openWebLogin(key); return; } catch (e) {}
  }
  toast("当前环境不支持网页登录");
}

/* ---------------- 登录状态 / 我的歌单 / 播放历史 ---------------- */
const STATUS_API = {
  netease: "/api/netease/status", qq: "/api/qq/status",
  bilibili: "/api/bili/status", douyin: "/api/douyin/status",
};
async function refreshLoginStatus() {
  const out = {};
  await Promise.all(Object.keys(STATUS_API).map(async (k) => {
    try { out[k] = (await api(STATUS_API[k])) || {}; } catch (e) { out[k] = {}; }
  }));
  state.login = out;
  const any = Object.keys(out).some((k) => out[k] && out[k].logged_in);
  $("login-open").textContent = any ? "我的" : "登录";
  return out;
}
function loggedIn(src) {
  const s = state.login[src];
  return !!(s && s.logged_in);
}
function loginWho(src) {
  const s = state.login[src] || {};
  return s.uname || s.uin || "";
}

async function loadMyListsView() {
  return loadMylistsOf(state.source, { type: "mylists", src: state.source });
}

async function loadMylistsOf(src, view) {
  state.view = view;
  notifyBack();
  if (!loggedIn(src)) {
    $("view").innerHTML = '<div class="loading">请先登录' + esc(SRC_NAME[src] || src) + "后查看歌单</div>";
    return;
  }
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  const j = await api("/api/mylists?source=" + encodeURIComponent(src));
  if (state.view !== view) return;
  const lists = (j.playlists || []).map((p) => ({
    title: p.name || p.title || "歌单",
    cover: p.cover || "",
    ref: p.id,
    src: src,
    action: "playlist",
  }));
  if (!lists.length) {
    $("view").innerHTML = '<div class="loading">' + esc(j.error || "没有歌单") + "</div>";
    return;
  }
  renderHome([["我的歌单 · " + (SRC_NAME[src] || src), lists]]);
}

/* ---------------- 我的 ---------------- */
function loadMineView() {
  return loadLocalListsView();
}

async function loadLocalListsView() {
  const view = state.view = { type: "mine", tab: "mine" };
  notifyBack();
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  const j = await api("/api/locallists");
  if (state.view !== view) return;
  const lists = j.lists || [];
  state.mineLists = lists;
  const rows = lists.map((p, i) => '<div class="row" data-li="' + i + '">' +
    '<div class="cov">♪</div><div class="info"><div class="t">' + esc(p.name || "歌单") + "</div>" +
    '<div class="s">' + (p.count || 0) + " 首 · 长按可重命名/删除</div></div></div>").join("");
  $("view").innerHTML = '<section class="section">' +
    '<div class="sec-head"><h3>我的歌单</h3><span>' + lists.length + " 个</span></div>" +
    '<button class="login-big primary" type="button" data-newpl="1">＋ 新建歌单</button>' +
    (lists.length
      ? '<div class="list">' + rows + "</div>"
      : '<div class="loading">还没有歌单，点上方新建</div>') +
    "</section>";
}

async function openLocalList(i) {
  const p = (state.mineLists || [])[i];
  if (!p) return;
  const view = state.view = { type: "localpl", id: p.id };
  notifyBack();
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  const j = await api("/api/localplaylist?pid=" + encodeURIComponent(p.id));
  if (state.view !== view) return;
  const songs = j.songs || [];
  if (!songs.length) {
    $("view").innerHTML = '<div class="loading">' + esc(j.error || "歌单为空") + "</div>";
    return;
  }
  renderList(songs, p.name || "我的歌单");
}

async function loadHistoryView() {
  const src = state.source;
  const view = state.view = { type: "history", src: src };
  notifyBack();
  if (!loggedIn(src)) {
    $("view").innerHTML = '<div class="loading">请先登录' + esc(SRC_NAME[src] || src) + "后查看播放历史</div>";
    return;
  }
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  const path = src === "bilibili" ? "/api/bili/history" : "/api/douyin/history";
  const j = await api(path + "?limit=30");
  if (state.view !== view) return;
  const songs = j.songs || [];
  if (!songs.length) {
    $("view").innerHTML = '<div class="loading">' + esc(j.error || "暂无播放历史") + "</div>";
    return;
  }
  renderList(songs, (SRC_NAME[src] || src) + " · 播放历史");
}

function loginRowHtml(key, name) {
  const ok = loggedIn(key);
  const who = loginWho(key);
  const right = ok ? ("已登录" + (who ? " · " + esc(who) : "")) : "未登录 ›";
  return '<button class="login-row" data-src="' + key + '">' +
    '<span class="login-left">' + esc(name) + (ok ? '<i class="on-dot"></i>' : "") + "</span>" +
    '<span class="login-state' + (ok ? " on" : "") + '">' + right + "</span></button>";
}

function renderLoginList() {
  stopQr();
  const html = Object.keys(QR_LOGIN).map((k) => loginRowHtml(k, QR_LOGIN[k].name)).join("") +
    LOGIN_WEB_ONLY.map(([k, n]) => loginRowHtml(k, n)).join("") +
    '<p class="login-note">扫码被风控时请用网页登录：在官方页面输入账号密码，成功后自动保存登录态。</p>';
  $("login-body").innerHTML = html;
}

function renderLoginSource(key) {
  const cfg = QR_LOGIN[key];
  const name = cfg ? cfg.name : (SRC_NAME[key] || key);
  $("login-body").innerHTML =
    '<div class="login-panel"><div class="login-tip">' + esc(name) + " 登录方式</div>" +
    '<button id="lg-web" type="button" class="login-big primary">网页登录（账号 / 密码）</button>' +
    (cfg ? '<button id="lg-qr" type="button" class="login-big">扫码登录</button>' : "") +
    '<button id="lg-back" type="button" class="login-big">返回</button>' +
    '<p class="login-note">' + (cfg
      ? "扫码被风控时请用网页登录；网页登录由官方页面处理验证码，登录成功后自动保存。"
      : "该音源暂未提供扫码，请在官方网页登录，登录成功后自动保存。") + "</p></div>";
  $("lg-web").onclick = () => openWebLogin(key);
  if (cfg) $("lg-qr").onclick = () => openQr(key);
  $("lg-back").onclick = renderLoginList;
}

function loadQrImage(cfg) {
  stopQr();
  qrBlocked = false;
  const seq = qrSeq;
  $("qr-mask").classList.add("hidden");
  $("qr-tip").textContent = cfg.tip;
  const img = $("qr-img");
  img.onerror = () => { $("qr-tip").textContent = "二维码加载失败，请点「刷新二维码」重试"; };
  img.src = withTok(cfg.qr + "?ts=" + Date.now());
  qrTimer = setInterval(async () => {
    if (seq !== qrSeq) return;
    let j;
    try { j = await api(cfg.poll); } catch (e) {
      if (seq === qrSeq) $("qr-tip").textContent = "查询登录状态失败，请检查网络";
      return;
    }
    if (seq !== qrSeq) return;
    const [kind, msg] = cfg.state(j);
    if (kind === "ok") {
      stopQr();
      $("qr-mask").classList.remove("hidden");
      $("qr-mask").textContent = "登录成功 ✓";
      $("qr-tip").textContent = "已登录";
      toast(cfg.name + " 登录成功");
      refreshLoginStatus();
      setTimeout(() => {
        $("loginpage").classList.add("hidden");
        notifyBack();
        loadHome();
      }, 800);
    } else if (kind === "scanned" || kind === "wait") {
      $("qr-tip").textContent = msg;
    } else {
      stopQr();
      qrBlocked = kind === "blocked";
      $("qr-mask").classList.remove("hidden");
      $("qr-mask").textContent = kind === "blocked"
        ? "扫码被风控，点击改用网页登录"
        : msg + "，点击刷新";
      $("qr-tip").textContent = msg;
    }
  }, 2000);
}

function openQr(key) {
  const cfg = QR_LOGIN[key];
  if (!cfg) return;
  $("login-body").innerHTML =
    '<div class="qr-panel"><div id="qr-tip" class="login-tip"></div>' +
    '<div class="qr-wrap"><img id="qr-img" alt="登录二维码">' +
    '<div id="qr-mask" class="qr-mask hidden"></div></div>' +
    '<div class="login-actions"><button id="qr-back" type="button">返回</button>' +
    '<button id="qr-web" type="button">网页登录</button>' +
    '<button id="qr-refresh" type="button" class="primary">刷新二维码</button></div></div>';
  $("qr-back").onclick = renderLoginList;
  $("qr-web").onclick = () => openWebLogin(key);
  $("qr-refresh").onclick = () => loadQrImage(cfg);
  // 被风控时点击蒙层直接切到网页登录, 其余情况刷新二维码
  $("qr-mask").onclick = () => (qrBlocked ? openWebLogin(key) : loadQrImage(cfg));
  loadQrImage(cfg);
}

function closeLogin() {
  stopQr();
  $("loginpage").classList.add("hidden");
  notifyBack();
}
$("login-open").addEventListener("click", async () => {
  $("loginpage").classList.remove("hidden");
  notifyBack();
  $("login-body").innerHTML = '<div class="loading">读取登录状态…</div>';
  await refreshLoginStatus();
  renderLoginList();
});
$("login-close").addEventListener("click", closeLogin);
$("login-body").addEventListener("click", (e) => {
  const row = e.target.closest(".login-row");
  if (!row) return;
  if (row.dataset.src) renderLoginSource(row.dataset.src);
});
$("video-back").addEventListener("click", closeVideo);
$("pcover").addEventListener("click", () => {
  const s = state.queue[state.index];
  if (isVideoSong(s)) openVideo(s);
});

/* ---------------- 启动 ---------------- */
(function showVersion() {
  const v = new URLSearchParams(location.search).get("v");
  if (v) $("ver").textContent = "v" + v;
})();
// 原生网页登录成功后回调
window.mhLoginDone = function () {
  closeLogin();
  toast("登录成功");
  refreshLoginStatus();
  loadHome();
};
renderSources();
refreshLoginStatus();
loadHome();
