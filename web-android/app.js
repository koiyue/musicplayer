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
  $("view").innerHTML = '<div class="loading">加载中…</div>';
  const j = await api("/api/playlist?source=" + encodeURIComponent(state.source) +
    "&ref=" + encodeURIComponent(ref));
  if (state.view !== view) return;
  renderList(j.songs || [], title || j.title || "歌单");
}

async function loadRecommend(opts, title) {
  const view = state.view = { type: "recommend", src: state.source };
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
});
$("sback").addEventListener("click", () => $("searchpage").classList.add("hidden"));
$("sinput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doSearch($("sinput").value);
});
$("sresults").addEventListener("click", (e) => {
  const row = e.target.closest(".row");
  if (!row) return;
  state.queue = state.searchSongs.slice();
  playAt(Number(row.dataset.i));
  $("searchpage").classList.add("hidden");
});

$("ptoggle").addEventListener("click", () => {
  if (!audio.src) return;
  if (audio.paused) audio.play().catch(() => {}); else audio.pause();
});
$("pprev").addEventListener("click", () => playStep(-1));
$("pnext").addEventListener("click", () => playStep(1));
$("pqueue").addEventListener("click", openQueue);
$("sheet-close").addEventListener("click", () => $("sheet").classList.add("hidden"));
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

/* ---------------- 启动 ---------------- */
renderSources();
loadHome();

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
}
function closeVideo() {
  $("videopage").classList.add("hidden");
  if (videoTimer) { clearInterval(videoTimer); videoTimer = null; }
  try { vid.pause(); } catch (e) {}
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

/* ---------------- 登录(粘贴 Cookie) ---------------- */
const LOGIN_SOURCES = [
  ["netease", "网易云"], ["qq", "QQ音乐"], ["bilibili", "哔哩哔哩"], ["douyin", "抖音"],
];
function renderLoginList() {
  $("login-body").innerHTML = LOGIN_SOURCES.map(([k, n]) =>
    '<button class="login-row" data-src="' + k + '">' + esc(n) + ' <span>›</span></button>').join("") +
    '<p style="color:var(--sub);font-size:12px;line-height:1.6;padding:12px 2px">' +
    "在手机浏览器登录对应网站后，复制 Cookie 粘贴到下一步即可。登录状态保存在应用私有目录。</p>";
}
function renderLoginForm(src) {
  const name = SRC_NAME[src] || src;
  $("login-body").innerHTML =
    '<div class="login-form"><div class="login-tip">粘贴 ' + esc(name) + " 的 Cookie</div>" +
    '<textarea id="login-cookie" placeholder="SESSDATA=...; bili_jct=...; 或完整 Cookie 串"></textarea>' +
    '<div class="login-actions"><button id="login-back" type="button">返回</button>' +
    '<button id="login-submit" type="button" class="primary">登录</button></div>' +
    '<div id="login-msg" class="login-msg"></div></div>';
  $("login-back").onclick = renderLoginList;
  $("login-submit").onclick = async () => {
    const cookie = ($("login-cookie").value || "").trim();
    if (!cookie) { $("login-msg").textContent = "请先粘贴 Cookie"; return; }
    $("login-msg").textContent = "提交中…";
    try {
      const r = await api("/api/" + src + "/manual", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookie: cookie }),
      });
      const ok = r && (r.logged_in === true || r.ok === true || r.status === "ok");
      $("login-msg").textContent = ok ? "登录成功" : ("结果: " + JSON.stringify(r));
      if (ok) {
        setTimeout(() => {
          $("loginpage").classList.add("hidden");
          loadHome();
        }, 600);
      }
    } catch (e) {
      $("login-msg").textContent = "登录失败: " + e;
    }
  };
}
$("login-open").addEventListener("click", () => {
  $("loginpage").classList.remove("hidden");
  renderLoginList();
});
$("login-close").addEventListener("click", () => $("loginpage").classList.add("hidden"));
$("login-body").addEventListener("click", (e) => {
  const row = e.target.closest(".login-row");
  if (row) renderLoginForm(row.dataset.src);
});
$("video-back").addEventListener("click", closeVideo);
$("pcover").addEventListener("click", () => {
  const s = state.queue[state.index];
  if (isVideoSong(s)) openVideo(s);
});
