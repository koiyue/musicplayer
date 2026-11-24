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
  queue: [],          // 播放队列(与当前视图列表解耦, 便于记住上次播放列表)
  viewSongs: [],      // 当前视图展示的歌曲列表
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
  state.viewSongs = songs || [];
  if (!songs || !songs.length) {
    $("view").innerHTML = '<div class="loading">没有内容</div>';
    return;
  }
  const count = opts && opts.noCount ? "" : "<span>" + songs.length + " 首</span>";
  $("view").innerHTML = '<section class="section"><div class="sec-head"><h3>' +
    esc(title || "播放列表") + "</h3>" + count + "</div>" +
    '<div class="list">' + songs.map(rowHtml).join("") + "</div></section>";
  markRows();
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
  const seen = new Set(state.viewSongs.map(songKey));
  const fresh = (j.songs || []).filter((s) => !seen.has(songKey(s)));
  if (!fresh.length) { toast("没有更多推荐了"); return; }
  // 只更新当前视图列表; 播放队列保持播放时的那一份快照
  let view = fresh.concat(state.viewSongs);
  // 超过 100 时从末尾(最早加载的)开始丢弃, 保持 100 条且最新在最上
  if (view.length > 100) view = view.slice(0, 100);
  state.viewSongs = view;
  const list = document.querySelector("#view .list");
  if (list) list.innerHTML = view.map(rowHtml).join("");
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
// 播放当前视图里的第 i 首: 把视图列表作为新的播放队列
function playView(i) {
  if (i < 0 || i >= state.viewSongs.length) return;
  state.queue = state.viewSongs.slice();
  playAt(i);
}
function playAt(i) {
  if (i < 0 || i >= state.queue.length) return;
  state.index = i;
  const s = state.queue[i];
  audio.src = proxyUrl(s);
  audio.play().catch(() => toast("播放失败，可能需要先登录该音源"));
  updatePlayerBar(s);
  markRows();
  savePlayerState(true);
  // 视频源(B站/抖音): 打开静音画面并跟随音频; 音频源: 卡拉OK页开着时直接续播歌词
  if (isVideoSong(s)) { closeKaraoke(); openVideo(s); }
  else { closeVideo(); if (karaokeVisible()) loadKaraoke(s); }
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

/* ---------------- 播放记录: 记住上次的歌曲与播放列表 ---------------- */
const PLAYER_STORE = "mh.player.v1";
let resumePending = null;   // 上次进度, 音频元数据就绪后恢复
let playerSavedAt = 0;

function savePlayerState(force) {
  const now = Date.now();
  if (!force && now - playerSavedAt < 4000) return;
  playerSavedAt = now;
  try {
    localStorage.setItem(PLAYER_STORE, JSON.stringify({
      songs: state.queue,
      index: state.index,
      time: audio.currentTime || 0,
      duration: audio.duration || 0,
      at: now,
    }));
  } catch (e) {}
}

function restorePlayerState() {
  let d = null;
  try { d = JSON.parse(localStorage.getItem(PLAYER_STORE) || "null"); } catch (e) { d = null; }
  if (!d || !Array.isArray(d.songs) || !d.songs.length) return;
  state.queue = d.songs;
  const idx = Number(d.index);
  state.index = (idx >= 0 && idx < d.songs.length) ? idx : -1;
  if (state.index < 0) return;
  const s = d.songs[state.index];
  updatePlayerBar(s);
  const t = Number(d.time) || 0, dur = Number(d.duration) || 0;
  resumePending = { time: t };
  $("ptcur").textContent = fmt(t);
  $("ptdur").textContent = fmt(dur);
  if (dur > 0) $("pfill").style.width = Math.min(100, (t / dur) * 100) + "%";
  audio.src = proxyUrl(s);
}
audio.addEventListener("loadedmetadata", () => {
  if (!resumePending) return;
  const d = audio.duration || 0;
  const t = Math.min(resumePending.time, d > 1 ? d - 1 : 0);
  resumePending = null;
  if (t > 0) { try { audio.currentTime = t; } catch (e) {} }
});
window.addEventListener("pagehide", () => savePlayerState(true));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) savePlayerState(true);
});
function markRows() {
  const cur = state.index >= 0 ? state.queue[state.index] : null;
  const key = cur ? songKey(cur) : "";
  if (!key) {
    document.querySelectorAll(".row.on").forEach((el) => el.classList.remove("on"));
    return;
  }
  // 视图行按视图列表取歌, 队列行按播放队列取歌
  document.querySelectorAll("#view .row").forEach((el) => {
    const s = state.viewSongs[Number(el.dataset.i)];
    el.classList.toggle("on", !!s && songKey(s) === key);
  });
  const list = $("queue-list");
  if (list) {
    list.querySelectorAll(".row").forEach((el) => {
      const s = state.queue[Number(el.dataset.i)];
      el.classList.toggle("on", !!s && songKey(s) === key);
    });
  }
  document.querySelectorAll("#sresults .row").forEach((el) => {
    const s = state.searchSongs[Number(el.dataset.i)];
    el.classList.toggle("on", !!s && songKey(s) === key);
  });
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
    await loadMineView();
    const p = (state.mineLists || []).find((x) => x.id === r.id);
    // 创建后可选自定义封面; 不设置则默认用第一首歌的封面
    openMenu("设置歌单封面", ["选择图片", "跳过（用第一首歌封面）"], [
      () => p && pickPlaylistCover(p),
      () => {},
    ]);
  });
}

/* 自定义歌单封面: 选图 -> 压到 512px 内 -> seticon */
function resizeImage(src, max, cb) {
  const img = new Image();
  img.onload = () => {
    const scale = Math.min(1, max / Math.max(img.width, img.height));
    const w = Math.max(1, Math.round(img.width * scale));
    const h = Math.max(1, Math.round(img.height * scale));
    const cv = document.createElement("canvas");
    cv.width = w;
    cv.height = h;
    cv.getContext("2d").drawImage(img, 0, 0, w, h);
    cb(cv.toDataURL("image/jpeg", 0.85));
  };
  img.onerror = () => cb(src);
  img.src = src;
}
function pickPlaylistCover(p) {
  if (!p) return;
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/*";
  inp.onchange = () => {
    const f = inp.files && inp.files[0];
    if (!f) return;
    const fr = new FileReader();
    fr.onload = () => {
      resizeImage(String(fr.result || ""), 512, async (dataUrl) => {
        const r = await localPl("seticon", { id: p.id, icon: dataUrl });
        if (r.error) { toast(r.error); return; }
        toast("封面已更新");
        loadMineView();
      });
    };
    fr.readAsDataURL(f);
  };
  inp.click();
}
function openMineCreateMenu() {
  openMenu("我的歌单", ["新建歌单"], [newLocalList]);
}
function openLocalMenu(p) {
  openMenu(p.name || "歌单", ["重命名", "修改封面", "删除"], [
    () => askText("重命名歌单", p.name, async (name) => {
      const r = await localPl("rename", { id: p.id, name: name });
      if (r.error) { toast(r.error); return; }
      toast("已重命名");
      loadMineView();
    }),
    () => pickPlaylistCover(p),
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
  const sig = "mine|" + SRC_ORDER.join(",");
  if (el.dataset.sig !== sig) {
    el.innerHTML = chipHtml("mine", "我的", state.root === "mine", "mine") +
      SRC_ORDER.map((k) =>
        chipHtml(k, SRC_NAME[k], state.root !== "mine" && k === state.source)).join("");
    el.dataset.sig = sig;
  } else {
    // 结构不变时只切换高亮, 不重建 DOM
    el.querySelectorAll(".chip").forEach((b) => {
      const on = b.dataset.act === "mine" ? state.root === "mine"
        : (state.root !== "mine" && b.dataset.src === state.source);
      b.classList.toggle("on", on);
    });
  }
  const on = el.querySelector(".chip.on");
  if (!on) return;
  // 只在选中项超出可视范围时做最小幅度滚动, 且不做平滑动画;
  // 否则切到右侧音源(咪咕/B站/抖音)时整条导航会来回滑动, 看起来像界面左右抖动
  const er = el.getBoundingClientRect(), cr = on.getBoundingClientRect();
  if (cr.left < er.left) el.scrollLeft -= er.left - cr.left + 12;
  else if (cr.right > er.right) el.scrollLeft += cr.right - er.right + 12;
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
    el.dataset.sig = "";
    return;
  }
  const tabs = tabsForSrc(state.source);
  if (tabs.length < 2) {
    el.classList.add("hidden");
    el.innerHTML = "";
    el.dataset.sig = "";
    return;
  }
  el.classList.remove("hidden");
  const sig = tabs.map((t) => t.key + ":" + t.label).join("|");
  if (el.dataset.sig !== sig) {
    el.innerHTML = tabs.map((t) => {
      const on = t.key === (state.tab || "home");
      return '<button class="tab' + (on ? " on" : "") + '" type="button" data-tab="' +
        t.key + '">' + esc(t.label) + "</button>";
    }).join("") + '<i class="tab-ink"></i>';
    el.dataset.sig = sig;
    // 新建页签时直接把下划线放到当前位置, 不做从 0 滑入
    const a0 = el.querySelector(".tab.on"), k0 = el.querySelector(".tab-ink");
    if (a0 && k0) {
      k0.style.transition = "none";
      k0.style.left = a0.offsetLeft + "px";
      k0.style.width = a0.offsetWidth + "px";
      requestAnimationFrame(() => { k0.style.transition = ""; });
    }
  } else {
    el.querySelectorAll(".tab").forEach((b) =>
      b.classList.toggle("on", b.dataset.tab === (state.tab || "home")));
  }
  // 下划线移动到当前页签
  const active = el.querySelector(".tab.on");
  const ink = el.querySelector(".tab-ink");
  if (active && ink) {
    ink.style.left = active.offsetLeft + "px";
    ink.style.width = active.offsetWidth + "px";
  }
}

/* 页面切换动画: dir>0 从右侧滑入, dir<0 从左侧滑入 */
function animateView(dir) {
  if (!dir) return;
  const v = $("view");
  v.classList.remove("anim-next", "anim-prev");
  void v.offsetWidth;
  v.classList.add(dir > 0 ? "anim-next" : "anim-prev");
}

function switchTab(key) {
  if (state.root === "mine") return;
  const tabs = tabsForSrc(state.source);
  const cur = tabs.findIndex((t) => t.key === (state.tab || "home"));
  const nxt = tabs.findIndex((t) => t.key === key);
  animateView(nxt >= cur ? 1 : -1);
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
function openPage(p, dir) {
  if (!p) return;
  if (dir) animateView(dir);
  if (p.root === "mine") {
    state.root = "mine";
    renderSources();
    renderTabs();
    return loadMineView();
  }
  state.root = "src";
  if (state.source !== p.src) {
    state.source = p.src;
  }
  // 从「我的」滑回音源时 source 可能本就等于目标音源, 这里必须刷新一级导航高亮
  renderSources();
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
  openPage(pages[(i + step + pages.length) % pages.length], step);
}

$("sources").addEventListener("click", (e) => {
  const b = e.target.closest(".chip");
  if (!b) return;
  if (b.dataset.act === "mine") {
    if (state.root === "mine") return;
    animateView(-1);
    state.root = "mine";
    renderSources();
    renderTabs();
    loadMineView();
    return;
  }
  if (b.dataset.src === "mine") return;
  const fromIdx = state.root === "mine" ? -1 : SRC_ORDER.indexOf(state.source);
  const toIdx = SRC_ORDER.indexOf(b.dataset.src);
  animateView(toIdx >= fromIdx ? 1 : -1);
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
  const lr = e.target.closest(".row[data-li]");
  if (lr) { openLocalList(Number(lr.dataset.li)); return; }
  const card = e.target.closest(".card");
  if (card) {
    const sec = state.homeCards[Number(card.dataset.si)];
    if (sec) openCard(sec[1][Number(card.dataset.ci)]);
    return;
  }
  const row = e.target.closest(".row");
  if (row && row.dataset.i != null) playView(Number(row.dataset.i));
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
  const s = state.viewSongs[i];
  return s ? { song: s, play: () => playView(i) } : null;
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

/* ---------------- 我的歌单: 长按空白处新建 ---------------- */
let mineLpTimer = null, mineLpStart = null, mineLpMoved = false;
$("view").addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1) return;
  if (!(state.view && state.view.type === "mine")) return;
  if (!e.target.closest("[data-mine]")) return;
  if (e.target.closest(".row, .card")) return;
  const t = e.touches[0];
  mineLpStart = { x: t.clientX, y: t.clientY };
  mineLpMoved = false;
  if (mineLpTimer) clearTimeout(mineLpTimer);
  mineLpTimer = setTimeout(() => {
    mineLpTimer = null;
    if (mineLpMoved) return;
    suppressNextClick();
    openMineCreateMenu();
  }, 550);
}, { passive: true });
$("view").addEventListener("touchmove", (e) => {
  if (!mineLpTimer || !mineLpStart) return;
  const t = e.touches[0];
  if (Math.abs(t.clientX - mineLpStart.x) > 10 || Math.abs(t.clientY - mineLpStart.y) > 10) {
    mineLpMoved = true;
    clearTimeout(mineLpTimer);
    mineLpTimer = null;
  }
}, { passive: true });
["touchend", "touchcancel"].forEach((ev) =>
  $("view").addEventListener(ev, () => {
    if (mineLpTimer) { clearTimeout(mineLpTimer); mineLpTimer = null; }
  }, { passive: true }));

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
  if (Math.abs(dx) > 8 && Math.abs(dx) >= Math.abs(dy)) swipeFrom.horizontal = true;
}, { passive: true });
document.addEventListener("touchend", (e) => {
  const s = swipeFrom;
  swipeFrom = null;
  if (!s || !s.horizontal) return;
  const t = e.changedTouches[0];
  const dx = t.clientX - s.x;
  const dy = Math.abs(t.clientY - s.y);
  if (Date.now() - s.at > 1200) return;
  if (Math.abs(dx) < 50 || dy > Math.abs(dx)) return;
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
  pullFrom = { x: t.clientX, y: t.clientY, top: pageScrollTop() };
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

/* 进度条: 点击或拖动都可跳转 */
function seekToX(clientX) {
  const d = audio.duration;
  if (!d) return false;
  const r = $("pbar").getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  try { audio.currentTime = ratio * d; } catch (e) {}
  $("pfill").style.width = ratio * 100 + "%";
  $("ptcur").textContent = fmt(ratio * d);
  return true;
}
let seekDragging = false;
$("pbar").addEventListener("touchstart", (e) => {
  if (!audio.src || !audio.duration) return;
  seekDragging = true;
  seekToX(e.touches[0].clientX);
  e.preventDefault();
}, { passive: false });
document.addEventListener("touchmove", (e) => {
  if (!seekDragging) return;
  seekToX(e.touches[0].clientX);
  e.preventDefault();
}, { passive: false });
["touchend", "touchcancel"].forEach((ev) =>
  document.addEventListener(ev, () => {
    if (!seekDragging) return;
    seekDragging = false;
    savePlayerState(true);
  }, { passive: true }));
$("pbar").addEventListener("click", (e) => {
  if (!audio.duration) return;
  seekToX(e.clientX);
});

audio.addEventListener("play", () => { state.playing = true; setToggleIcon(); });
audio.addEventListener("pause", () => { state.playing = false; setToggleIcon(); savePlayerState(true); });
audio.addEventListener("timeupdate", () => {
  const d = audio.duration || 0, t = audio.currentTime || 0;
  $("pfill").style.width = (d ? Math.min(1, t / d) * 100 : 0) + "%";
  $("ptcur").textContent = fmt(t);
  $("ptdur").textContent = fmt(d);
  syncKaraoke();
  savePlayerState();
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
  ["karaokepage", () => closeKaraoke()],
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

/* ---------------- 卡拉OK(歌词 / 翻译 / 音译) ---------------- */
const karaoke = {
  song: null, lines: [], active: -1, seq: 0,
  showTrans: false, showRomaji: false, userScroll: 0, romajiFor: "",
};
function karaokeVisible() { return !$("karaokepage").classList.contains("hidden"); }

// LRC 解析: 一行可带多个时间戳
function parseLRC(text) {
  const out = [];
  for (const rawLine of String(text || "").split(/\r?\n/)) {
    const times = [];
    const re = /\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;
    let m;
    while ((m = re.exec(rawLine))) {
      const ms = m[3] ? parseInt(m[3].padEnd(3, "0").slice(0, 3), 10) : 0;
      times.push(parseInt(m[1], 10) * 60 + parseInt(m[2], 10) + ms / 1000);
    }
    if (!times.length) continue;
    const rest = rawLine.replace(re, "").trim();
    if (!rest) continue;
    for (const t of times) out.push({ t: t, text: rest });
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}

function romajiBridge() {
  const n = window.MHNative;
  return n && typeof n.transliterate === "function" ? n : null;
}

function openKaraoke(s) {
  if (!s) return;
  $("karaoke-song").textContent = s.title || "未在播放";
  $("karaoke-artist").textContent =
    (s.artist || "未知") + " · " + (SRC_NAME[s.source] || s.source || "");
  const cov = coverUrl(s.cover);
  $("karaoke-cover").innerHTML = cov ? '<img src="' + esc(cov) + '" alt="">' : "♪";
  $("karaokepage").classList.remove("hidden");
  loadKaraoke(s);
  notifyBack();
}

function closeKaraoke() {
  karaoke.seq++;
  $("karaokepage").classList.add("hidden");
  notifyBack();
}

async function loadKaraoke(s) {
  const seq = ++karaoke.seq;
  karaoke.song = s;
  karaoke.lines = [];
  karaoke.active = -1;
  karaoke.romajiFor = "";
  const box = $("karaoke-lyrics");
  box.innerHTML = '<div class="loading">歌词加载中…</div>';
  if (s.source === "local") {
    box.innerHTML = '<div class="loading">本地歌曲暂无歌词</div>';
    setKaraokeTools();
    return;
  }
  let j;
  try {
    j = await api("/api/lyrics?source=" + encodeURIComponent(s.source) +
      "&id=" + encodeURIComponent(s.id) +
      "&extra=" + encodeURIComponent(JSON.stringify(s.extra || {})));
  } catch (e) {
    j = { error: "歌词加载失败" };
  }
  if (seq !== karaoke.seq) return;
  if (j.error) {
    box.innerHTML = '<div class="loading">' + esc(j.error) + "</div>";
    setKaraokeTools();
    return;
  }
  const lines = parseLRC(j.lyric || "");
  if (!lines.length) {
    box.innerHTML = '<div class="loading">暂无歌词</div>';
    setKaraokeTools();
    return;
  }
  // 翻译按时间戳对齐到主歌词行
  const tr = {};
  parseLRC(j.tlyric || "").forEach((x) => { tr[Math.round(x.t * 10)] = x.text; });
  karaoke.lines = lines.map((l) => ({
    t: l.t, text: l.text, tr: tr[Math.round(l.t * 10)] || "", ro: "",
  }));
  box.innerHTML = karaoke.lines.map((l, i) =>
    '<div class="kl-line" data-i="' + i + '"><span class="kt">' + esc(l.text) + "</span>" +
    '<span class="kro"></span><span class="ktr">' + esc(l.tr) + "</span></div>").join("");
  syncKaraoke(true);
  setKaraokeTools();
}

function setKaraokeTools() {
  const hasTr = karaoke.lines.some((l) => !!l.tr);
  const hasRo = karaoke.lines.length > 0 && !!romajiBridge();
  const box = $("karaoke-lyrics");
  box.classList.toggle("show-trans", karaoke.showTrans && hasTr);
  box.classList.toggle("show-romaji", karaoke.showRomaji && hasRo);
  $("karaoke-trans").classList.toggle("on", karaoke.showTrans && hasTr);
  $("karaoke-romaji").classList.toggle("on", karaoke.showRomaji && hasRo);
}

// 音译: 用原生 ICU 把汉字/假名/谚文转成拼音/罗马音, 每首只生成一次
function ensureRomaji(bridge) {
  const song = karaoke.song;
  if (!song) return;
  const key = songKey(song);
  if (karaoke.romajiFor === key) return;
  const els = document.querySelectorAll("#karaoke-lyrics .kro");
  karaoke.lines.forEach((l, i) => {
    if (l.ro) return;
    let ro = "";
    if (/[^\x00-\x7f]/.test(l.text)) {
      try { ro = String(bridge.transliterate(l.text) || "").trim(); } catch (e) { ro = ""; }
    }
    l.ro = ro;
    if (els[i]) els[i].textContent = ro;
  });
  karaoke.romajiFor = key;
}

function syncKaraoke(force) {
  if (!karaoke.lines.length || !karaokeVisible()) return;
  const t = audio.currentTime || 0;
  let idx = -1;
  for (let i = 0; i < karaoke.lines.length; i++) {
    if (karaoke.lines[i].t <= t + 0.2) idx = i; else break;
  }
  if (idx === karaoke.active && !force) return;
  karaoke.active = idx;
  const box = $("karaoke-lyrics");
  const rows = box.querySelectorAll(".kl-line");
  rows.forEach((el, i) => el.classList.toggle("on", i === idx));
  const cur = rows[idx];
  if (cur && Date.now() - karaoke.userScroll > 3000) {
    const y = Math.max(0, cur.offsetTop - box.clientHeight / 2 + cur.offsetHeight / 2);
    try { box.scrollTo({ top: y, behavior: "smooth" }); } catch (e) { box.scrollTop = y; }
  }
}

$("karaoke-back").addEventListener("click", closeKaraoke);
$("karaoke-lyrics").addEventListener("touchstart", () => {
  karaoke.userScroll = Date.now();
}, { passive: true });
$("karaoke-lyrics").addEventListener("click", (e) => {
  const el = e.target.closest(".kl-line");
  if (!el) return;
  const l = karaoke.lines[Number(el.dataset.i)];
  if (l) { try { audio.currentTime = l.t; } catch (err) {} }
});
$("karaoke-trans").addEventListener("click", () => {
  if (!karaoke.lines.length) return;
  if (!karaoke.showTrans && !karaoke.lines.some((l) => !!l.tr)) {
    toast("这首歌暂无翻译");
    return;
  }
  karaoke.showTrans = !karaoke.showTrans;
  setKaraokeTools();
});
$("karaoke-romaji").addEventListener("click", () => {
  if (!karaoke.lines.length) return;
  const bridge = romajiBridge();
  if (!bridge) { toast("当前环境不支持音译"); return; }
  if (!karaoke.showRomaji) ensureRomaji(bridge);
  karaoke.showRomaji = !karaoke.showRomaji;
  setKaraokeTools();
});

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
  const rows = lists.map((p, i) => {
    const cov = coverUrl(p.cover || p.icon || "");
    return '<div class="row" data-li="' + i + '">' +
      '<div class="cov"' + (cov ? ' style="background-image:url(\'' + esc(cov) + '\')"' : "") + ">" +
      (cov ? "" : "♪") + "</div>" +
      '<div class="info"><div class="t">' + esc(p.name || "歌单") + "</div>" +
      '<div class="s">' + (p.count || 0) + " 首 · 长按编辑</div></div></div>";
  }).join("");
  $("view").innerHTML = '<section class="section" data-mine="1">' +
    '<div class="sec-head"><h3>我的歌单</h3><span>' + lists.length + " 个</span></div>" +
    (lists.length
      ? '<div class="list">' + rows + '</div><div class="loading" style="padding:14px 0">长按空白处新建歌单</div>'
      : '<div class="loading">还没有歌单，长按空白处新建</div>') +
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
  if (!s) return;
  // B站/抖音保持视频画面; 其他音源进入卡拉OK歌词页
  if (isVideoSong(s)) openVideo(s);
  else openKaraoke(s);
});

/* ---------------- 启动 ---------------- */
// 页面滚动位置: 禁用横向滚动后滚动容器可能是 body, 不能只看 documentElement
function pageScrollTop() {
  return Math.max(window.scrollY || 0, document.documentElement.scrollTop || 0,
    document.body ? document.body.scrollTop || 0 : 0);
}
// 播放器常驻底部: 把实测高度写入 --player-h, 由 #view 底部内边距腾出空间
function syncPlayerSpace() {
  const p = $("player");
  if (!p) return;
  // 胶囊悬浮在底部, 需要占用的高度 = 屏幕底部到播放器顶部的距离
  const h = Math.ceil(window.innerHeight - p.getBoundingClientRect().top);
  if (h > 0) document.documentElement.style.setProperty("--player-h", h + "px");
}
window.addEventListener("resize", syncPlayerSpace);
window.addEventListener("orientationchange", () => setTimeout(syncPlayerSpace, 120));
window.addEventListener("load", syncPlayerSpace);
syncPlayerSpace();
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
restorePlayerState();
refreshLoginStatus();
loadHome();
