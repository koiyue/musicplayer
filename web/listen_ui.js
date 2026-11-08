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

  UI.setSoundButton = (on) => {
    let b = $("room-sound-btn");
    if (!on) { if (b) b.remove(); return; }
    if (!b) {
      b = document.createElement("button");
      b.id = "room-sound-btn"; b.className = "go-btn room-sound-btn";
      b.textContent = "点击开启一起听声音";
      const box = $("room-panel") || document.body;
      box.appendChild(b);
    }
    b.onclick = () => {
      const v = $("room-remote-video"), a = $("room-fallback-audio");
      if (v && v.srcObject) v.play().catch(() => {});
      if (a && a.src) a.play().catch(() => {});
      b.remove();
    };
  };

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
        '<div class="room-badge">' + esc(L.transport === "webrtc" ? "WebRTC 音视频" : "LAN HTTP 回退") +
        " · seq " + L.seq + " · " + L.members.length + " 人</div>" +
        '<div class="room-members">' + membersHtml() + "</div>" +
        '<p class="set-hint">' + esc((L.room && L.room.fallback_urls || []).join(" / ")) + "</p>" +
        '<p class="set-hint">' + esc(L.room && L.room.firewall_hint) + "</p>" +
        (L.fallbackReason ? '<p class="set-hint">回退原因：' + esc(L.fallbackReason) + "</p>" : "") +
        '<button class="go-btn ghost room-sound-btn" id="room-enable-sound">开启远端声音</button>';
      const snd = $("room-enable-sound");
      if (snd) snd.onclick = () => UI.setSoundButton(true);
    }
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
