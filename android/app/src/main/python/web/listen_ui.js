/* MusicHub P2P 一起听: 房间面板交互 */
"use strict";
(function () {
  const L = window.Listen;
  const $ = (id) => document.getElementById(id);
  const UI = {};
  window.ListenUI = UI;
  if (!L) return;

  function show(id, on) { const el = $(id); if (el) el.classList.toggle("hidden", !on); }
  function toast(msg, ms) { if (window.toast) window.toast(msg, ms); else console.log(msg); }
  function esc(s) { return window.esc ? window.esc(s) : String(s == null ? "" : s); }
  async function copyText(text, el) {
    if (el && el.select) { el.focus(); el.select(); }
    try { if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(text);
          else document.execCommand("copy"); toast("已复制"); }
    catch (e) { toast("复制失败，请手工全选复制"); }
  }

  function openPanel() {
    $("room-panel").classList.remove("hidden");
    UI.render();
  }
  function closePanel() { $("room-panel").classList.add("hidden"); }
  UI.open = openPanel;
  UI.close = closePanel;

  function membersHtml() {
    if (!L.members || !L.members.length) return '<span class="room-empty">暂无成员</span>';
    return L.members.map((m) => '<span class="room-member"><i></i>' + esc(m.nickname || m.member_id) +
      (m.role === "host" ? " · 房主" : "") + "</span>").join("");
  }
  function chatHtml() {
    if (!L.chat || !L.chat.length) return '<div class="room-empty">还没有消息</div>';
    return L.chat.slice(-30).map((m) =>
      '<div class="room-msg"><b>' + esc(m.nickname || m.actor) + "</b><span>" + esc(m.text) + "</span></div>").join("");
  }
  UI.renderChat = () => {
    ["room-chat", "room-member-chat"].forEach((id) => {
      const el = $(id); if (!el) return;
      el.innerHTML = chatHtml(); el.scrollTop = el.scrollHeight;
    });
  };

  const VIDEO_MODE = {
    A: "视频已同步", B: "跟随房主画面（需手动播放）", C: "当前仅支持音频同步",
  };
  const NET_TEXT = {
    detecting: "正在检测网络", lan: "局域网直连", wan: "跨网络直连",
    relay: "检测到中继（本版未配置 TURN，若出现 relay 需排查）",
    retrying: "网络受限，正在重试", unsupported: "当前网络不支持连接",
  };
  function networkLabel() {
    const n = L.network || {};
    return NET_TEXT[n.state || (L.active ? "lan" : "detecting")] || "网络状态待验证";
  }
  function videoLabel() {
    const mode = (L.video && L.video.mode) || (L.role === "member" ? "C" : "B");
    return VIDEO_MODE[mode] || VIDEO_MODE.C;
  }
  UI.setSoundButton = (on, reason) => {
    let box = $("room-video-host");
    if (!on) { if (box) box.remove(); return; }
    if (!box) {
      box = document.createElement("div");
      box.id = "room-video-host"; box.className = "room-video-host";
      (document.body || $("room-panel")).appendChild(box);
    }
    const names = {
      NotAllowedError: "浏览器自动播放拦截", not_ready: "播放器未准备完成",
      media_error: "视频资源不可用", network: "网络缓冲超时", permission: "跨域或权限限制",
    };
    box.innerHTML = '<div><b>一起听视频</b></div><div class="set-hint">' +
      esc(videoLabel()) + "</div>" + (reason ? '<div class="set-hint">原因：' + esc(names[reason] || reason) + "</div>" : "") +
      '<button class="go-btn" id="room-video-start">点击开始播放</button>';
    const btn = $("room-video-start");
    if (btn) btn.onclick = () => {
      if (L.playRemoteMedia) L.playRemoteMedia();
      box.remove();
    };
  };
  UI.showRemoteVideo = () => {
    const overlay = $("lyric-overlay"), box = $("lo-video"), v = $("room-remote-video");
    if (overlay) overlay.classList.remove("hidden");
    if (box) box.classList.remove("hidden");
    if (v) v.classList.remove("hidden");
    if ($("lo-lyric")) $("lo-lyric").classList.add("hidden");
    UI.setSoundButton(true, L.video && L.video.failReason);
  };
  UI.renderSync = (c) => {
    const staticBadges = '<span class="room-badge">' + esc(networkLabel()) + "</span>" +
      '<span class="room-badge warn">' + esc(videoLabel()) + "</span>" +
      '<p class="set-hint room-video-note">已确认支持局域网连接，非局域网 P2P 能力待验证；本版未配置 TURN 中继。</p>';
    ["room-sync-host", "room-sync-member"].forEach((id) => {
      const el = $(id); if (!el) return;
      if (!L.active || !c) { el.innerHTML = staticBadges; return; }
      el.innerHTML = staticBadges + '<span class="room-badge ' + (c.buffered ? "warn" : "") + '">' + esc(c.status_text || "同步中") +
        " · " + esc(fmtRoomTime(c.pos)) + " / " + esc(fmtRoomTime(c.duration)) + "</span>" +
        '<span class="room-badge">seq ' + Number(c.state_seq || 0) + " · rev " + Number(c.song_rev || 0) + "</span>" +
        '<span class="room-badge ' + (Math.abs(c.drift_ms || 0) > 500 ? "warn" : "") + '">漂移 ' +
        Math.round(c.drift_ms || 0) + "ms · " + esc(c.correction || "none") + "</span>";
    });
  };
  function fmtRoomTime(t) {
    t = Math.max(0, Math.floor(Number(t) || 0));
    return Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0");
  }

  UI.render = () => {
    if (!L) return;
    const panel = $("room-panel");
    if (!panel) return;
    const host = L.role === "host", member = L.role === "member";
    show("room-landing", !L.active);
    show("room-host", host);
    show("room-join", member);
    const status = $("room-host-status");
    if (host && status) {
      status.innerHTML =
        '<div class="room-badge">房间 ' + esc(L.room && L.room.room_id) + "</div>" +
        '<div class="room-badge">' + esc(networkLabel()) + "</div>" +
        '<div class="room-badge ' + (L.transport === "http" ? "warn" : "") + '">' +
        esc(L.transport === "webrtc" ? "WebRTC 连接（音视频能力见下）" : "LAN HTTP 回退") +
        " · seq " + L.seq + " · " + L.members.length + " 人</div>" +
        '<div class="room-badge warn">' + esc(videoLabel()) + "</div>" +
        '<p class="set-hint room-video-note">已确认支持局域网连接，非局域网 P2P 能力待验证；本版未配置 TURN 中继。</p>' +
        '<div class="room-members">' + membersHtml() + "</div>" +
        '<p class="set-hint">' + esc((L.room && L.room.fallback_urls || []).join(" / ")) + "</p>" +
        '<p class="set-hint">' + esc(L.room && L.room.firewall_hint) + "</p>" +
        (L.fallbackReason ? '<p class="set-hint">回退原因：' + esc(L.fallbackReason) + "</p>" : "");
      const snd = $("room-enable-sound");
      if (snd) snd.onclick = () => { if (L.playRemoteMedia) L.playRemoteMedia(); };
    }
    UI.renderSync(window.MusicHubPlayer && window.MusicHubPlayer.clock ? window.MusicHubPlayer.clock() : null);
    const slots = $("room-slots");
    if (host && slots) {
      slots.innerHTML = (L.slots || []).map((s, i) =>
        '<div class="room-slot"><div class="room-slot-head"><b>席位 ' + s.slot + "</b>" +
        '<span class="room-badge ' + (s.state === "online" ? "online" : "") + '">' + esc(s.state) + "</span></div>" +
        '<textarea class="room-code" id="room-offer-' + s.slot + '" spellcheck="false" readonly placeholder="邀请码"></textarea>' +
        '<div class="set-actions"><button class="go-btn" data-room-offer="' + s.slot + '">生成邀请码</button>' +
        '<button class="go-btn ghost" data-room-copy="' + s.slot + '">复制</button></div>' +
        '<textarea class="room-code" id="room-answer-' + s.slot + '" spellcheck="false" placeholder="粘贴该成员的应答码"></textarea>' +
        '<button class="go-btn" data-room-accept="' + s.slot + '">应用应答码</button></div>').join("");
      (L.slots || []).forEach((s) => {
        const off = $("room-offer-" + s.slot);
        if (off && s.offer) off.value = s.offer;
        const ans = $("room-answer-" + s.slot);
        if (ans && s.answer) ans.value = s.answer;
      });
      slots.onclick = async (e) => {
        const t = e.target;
        const offerSlot = t.getAttribute && t.getAttribute("data-room-offer");
        const copySlot = t.getAttribute && t.getAttribute("data-room-copy");
        const acceptSlot = t.getAttribute && t.getAttribute("data-room-accept");
        try {
          if (offerSlot) { const code = await L.makeOffer(Number(offerSlot)); $("room-offer-" + offerSlot).value = code; }
          else if (copySlot) copyText($("room-offer-" + copySlot).value, $("room-offer-" + copySlot));
          else if (acceptSlot) { await L.acceptAnswer(Number(acceptSlot), $("room-answer-" + acceptSlot).value); renderChatSafe(); }
        } catch (err) { toast(err.message || String(err)); }
      };
    }
    UI.renderChat();
  };
  function renderChatSafe() { UI.renderChat(); }

  function bind() {
    const btn = $("btn-room");
    if (btn) btn.addEventListener("click", openPanel);
    $("room-close").addEventListener("click", closePanel);
    $("room-panel").addEventListener("click", (e) => { if (e.target.id === "room-panel") closePanel(); });
    $("room-nick").addEventListener("input", (e) => { L.nickname = e.target.value.trim() || ""; });
    $("room-create").addEventListener("click", async () => {
      try { await L.createRoom(true); toast("房间已创建"); UI.render(); }
      catch (e) { toast(e.message || String(e)); }
    });
    $("room-show-join").addEventListener("click", () => {
      show("room-landing", false); show("room-join", true); show("room-host", false);
    });
    $("room-join-go").addEventListener("click", async () => {
      try {
        const code = await L.joinRoom($("room-invite").value);
        $("room-answer").value = code;
        toast("应答码已生成，请发回房主");
        UI.render();
        show("room-landing", false); show("room-host", false); show("room-join", true);
      } catch (e) { toast(e.message || String(e)); }
    });
    $("room-copy-answer").addEventListener("click", () => copyText($("room-answer").value, $("room-answer")));
    const send = (inputId) => async () => {
      const el = $(inputId);
      const text = el.value; el.value = "";
      await L.sendChat(text);
      UI.renderChat();
    };
    $("room-chat-send").addEventListener("click", send("room-chat-text"));
    $("room-member-chat-send").addEventListener("click", send("room-member-chat-text"));
    ["room-chat-text", "room-member-chat-text"].forEach((id) => {
      $(id).addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); send(id)(); }
      });
    });
    $("room-leave").addEventListener("click", L.leaveRoom);
    $("room-member-leave").addEventListener("click", L.leaveRoom);
    $("room-retry").addEventListener("click", L.retryWebRTC);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind);
  else bind();
})();
