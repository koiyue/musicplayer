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
function coverUrl(url) {
  const c = url || "";
  if (!c) return "";
  if (/hdslb\.com|byte|douyin|ixigua|pstatp|snssdk|douyinpic|byteimg/.test(c)) {
    return withTok("/imgproxy?url=" + encodeURIComponent(c));
  }
  return c;
}

/* ---------------- 列表渲染 ---------------- */
function rowHtml(s, i) {
  const cov = coverUrl(s.cover);
  return '<div class="row" data-i="' + i + '">' +
    '<div class="idx">' + (i + 1) + "</div>" +
    '<div class="cov"' + (cov ? ' style="background-image:url(\'' + esc(cov) + '\')"' : "") + ">" +
    (cov ? "" : "♪") + "</div>" +
    '<div class="info"><div class="t">' + esc(s.title || "未命名") + "</div>" +
    '<div class="s">' + esc((s.artist || "未知") + " · " + (SRC_NAME[s.source] || s.source || "")) + "</div></div>" +
    '<div class="dur">' + (s.duration ? fmt(s.duration) : "") + "</div></div>";
}
function renderList(songs, title) {
  state.queue = songs || [];
  state.index = -1;
  if (!songs || !songs.length) {
    $("view").innerHTML = '<div class="loading">没有内容</div>';
    return;
  }
  $("view").innerHTML = '<section class="section"><div class="sec-head"><h3>' +
    esc(title || "播放列表") + "</h3><span>" + songs.length + " 首</span></div>" +
    '<div class="list">' + songs.map(rowHtml).join("") + "</div></section>";
}

/* ---------------- 首页 ---------------- */
async function loadHome() {
  const view = state.view = { type: "home", src: state.source };
  notifyBack();
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  try {
    const j = await api("/api/home?source=" + encodeURIComponent(state.source) + "&limit=30");
    if (state.view !== view) return;
    if (Array.isArray(j.songs) && j.songs.length) {
      renderList(j.songs, (SRC_NAME[state.source] || state.source) + " · 推荐");
      return;
    }
    const sections = [];
    if (j.playlists && j.playlists.length) sections.push(["推荐歌单", j.playlists]);
    if (j.charts && j.charts.length) sections.push(["排行榜", j.charts]);
    if (sections.length) { renderHome(sections); return; }
    await loadRecommend({}, "推荐");
  } catch (e) {
    $("view").innerHTML = '<div class="loading">加载失败：' + esc(e) + "</div>";
  }
}
function renderHome(sections) {
  state.homeCards = sections;
  $("view").innerHTML = sections.map((sec, si) => {
    const cards = sec[1];
    return '<section class="section"><div class="sec-head"><h3>' + esc(sec[0]) +
      "</h3><span>" + cards.length + "</span></div><div class=\"grid\">" +
      cards.map((c, ci) => {
        const cov = coverUrl(c.cover);
        return '<div class="card" data-si="' + si + '" data-ci="' + ci + '">' +
          '<div class="cov"' + (cov ? ' style="background-image:url(\'' + esc(cov) + '\')"' : "") + ">" +
          (cov ? "" : "♪") + "</div>" +
          '<div class="cap">' + esc(c.title || "未命名") + "</div></div>";
      }).join("") + "</div></section>";
  }).join("");
}

async function openCard(c) {
  const src = state.source;
  try {
    if (c.action === "playlist" && c.ref) {
      if (src === "douyin") {
        if (c.keyword) return doSearch(c.keyword);
        return loadRecommend({ mode: "hot" }, c.title || "推荐");
      }
      if (src === "qq" && c.topid) return loadRecommend({ topid: c.topid }, c.title || "排行榜");
      return loadPlaylist(c.ref, c.title);
    }
    if (c.topid) {
      return loadRecommend({ topid: c.topid, mode: src === "netease" ? "toplist" : "" }, c.title || "排行榜");
    }
    if (c.ref) return loadPlaylist(c.ref, c.title);
    if (c.keyword) return doSearch(c.keyword);
  } catch (e) {
    toast("打开失败：" + e);
  }
}

async function loadPlaylist(ref, title) {
  const view = state.view = { type: "playlist", src: state.source, ref: ref };
  notifyBack();
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  const j = await api("/api/playlist?source=" + encodeURIComponent(state.source) +
    "&ref=" + encodeURIComponent(ref));
  if (state.view !== view) return;
  renderList(j.songs || [], title || j.title || "歌单");
}

async function loadRecommend(opts, title) {
  const view = state.view = { type: "recommend", src: state.source };
  notifyBack();
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  const q = new URLSearchParams(Object.assign({ source: state.source, limit: 50 }, opts || {}));
  const j = await api("/api/recommend?" + q.toString());
  if (state.view !== view) return;
  renderList(j.songs || [], title || "推荐");
}

/* ---------------- 搜索 ---------------- */
async function doSearch(kw) {
  kw = (kw || "").trim();
  if (!kw) return;
  $("searchpage").classList.remove("hidden");
  $("sinput").value = kw;
  $("sresults").innerHTML = '<div class="loading">搜索中…</div>';
  try {
    const j = await api("/api/search?source=" + encodeURIComponent(state.source) +
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
  $("ptitle").textContent = s.title || "未在播放";
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

/* ---------------- 事件绑定 ---------------- */
function renderSources() {
  $("sources").innerHTML = SRC_ORDER.map((k) =>
    '<button class="chip' + (k === state.source ? " on" : "") + '" data-src="' + k + '">' +
    esc(SRC_NAME[k]) + "</button>").join("");
}

$("sources").addEventListener("click", (e) => {
  const b = e.target.closest(".chip");
  if (!b) return;
  state.source = b.dataset.src;
  renderSources();
  loadHome();
});

$("view").addEventListener("click", (e) => {
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
  const row = e.target.closest(".row");
  if (!row) return;
  playAt(Number(row.dataset.i));
});

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

function renderLoginList() {
  stopQr();
  $("login-body").innerHTML =
    Object.keys(QR_LOGIN).map((k) =>
      '<button class="login-row" data-src="' + k + '">' + esc(QR_LOGIN[k].name) +
      " <span>网页 / 扫码 ›</span></button>").join("") +
    LOGIN_WEB_ONLY.map(([k, n]) =>
      '<button class="login-row" data-src="' + k + '">' + esc(n) +
      " <span>网页登录 ›</span></button>").join("") +
    '<p class="login-note">扫码被风控时请用网页登录：在官方页面输入账号密码，成功后自动保存登录态。</p>';
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
$("login-open").addEventListener("click", () => {
  $("loginpage").classList.remove("hidden");
  renderLoginList();
  notifyBack();
});
$("login-close").addEventListener("click", closeLogin);
$("login-body").addEventListener("click", (e) => {
  const row = e.target.closest(".login-row");
  if (row && row.dataset.src) renderLoginSource(row.dataset.src);
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
  loadHome();
};
renderSources();
loadHome();
