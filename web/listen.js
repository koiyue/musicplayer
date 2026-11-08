/* MusicHub P2P 一起听: 手工邀请码 + WebRTC 音视频 + LAN HTTP 回退 */
"use strict";
(function () {
  const L = window.Listen || {};
  window.Listen = L;
  const ROOM_VERSION = 1;
  const LWW_MS = 150;
  const ICE_STUN = [
    "stun:stun.l.google.com:19302",
    "stun:stun.cloudflare.com:3478",
  ];
  const MAX_PEERS = 3;

  L.role = L.role || "";
  L.active = false;
  L.room = null;
  L.slots = [];
  L.members = [];
  L.chat = [];
  L.seq = 0;
  L.rev = 0;
  L.transport = "webrtc";
  L.generation = 0;
  let memberPc = null, memberDc = null, remoteStream = null;
  L.debug = () => ({ role: L.role, active: L.active, room_id: L.room && L.room.room_id,
                     seq: L.seq, rev: L.rev, transport: L.transport,
                     members: L.members.slice(), slots: L.slots.map((s) => s.state),
                     fallback_urls: L.room && L.room.fallback_urls });

  const $ = (id) => document.getElementById(id);
  const clone = (v) => v == null ? v : JSON.parse(JSON.stringify(v));
  const now = () => Date.now();
  const nick = () => (L.nickname || (L.role === "host" ? "房主" : "成员"));
  function toast(msg, ms) { if (window.toast) window.toast(msg, ms); }

  function b64urlEncode(str) {
    const b = new TextEncoder().encode(str);
    let s = "";
    for (let i = 0; i < b.length; i += 0x8000)
      s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function b64urlDecode(s) {
    s = String(s || "").replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    const bin = atob(s), u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return new TextDecoder().decode(u);
  }
  function encodeInvite(obj) {
    return "MH1." + b64urlEncode(JSON.stringify(obj));
  }
  function decodeInvite(text) {
    const p = String(text || "").trim();
    if (!p.startsWith("MH1.")) throw new Error("邀请码格式不正确");
    const obj = JSON.parse(b64urlDecode(p.slice(4)));
    if (!obj || obj.v !== ROOM_VERSION || !obj.room_id) throw new Error("邀请码版本或房间无效");
    if (obj.exp && obj.exp < now()) throw new Error("邀请码已过期");
    return obj;
  }
  L.encodeInvite = encodeInvite;
  L.decodeInvite = decodeInvite;

  function canonical(v) {
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    if (v && typeof v === "object")
      return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
    return JSON.stringify(v === undefined ? null : v);
  }
  async function stateHash(state) {
    const stable = {
      room_id: L.room && L.room.room_id, song_key: state.song_key || "",
      playing: !!state.playing, rate: Number(state.rate) || 1,
      index: state.index, feedMode: !!state.feedMode, mode: state.mode || "loop",
      queue: (state.queue || []).map((s) => window.MusicHubPlayer.songKey(s)),
      feedQueue: (state.feedQueue || []).map((s) => window.MusicHubPlayer.songKey(s)),
      generation: L.generation, transport: L.transport,
    };
    if (window.crypto && crypto.subtle) {
      const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(stable)));
      return Array.from(new Uint8Array(d)).map((x) => x.toString(16).padStart(2, "0")).join("").slice(0, 16);
    }
    let h = 0;
    for (const c of canonical(stable)) h = ((h << 5) - h + c.charCodeAt(0)) | 0;
    return String(h >>> 0);
  }

  function snapshot() { return window.MusicHubPlayer.snapshot(); }
  function applyIntent(p) { window.MusicHubPlayer.applyIntent(p); }
  function updateTimeline(st) {
    L.timeline = { basePos: Number(st && st.time) || 0, baseTs: now(),
                   rate: Number(st && st.rate) || 1,
                   playing: !!(st && st.playing), duration: Number(st && st.duration) || 0 };
  }

  async function commitProposal(p) {
    if (L.role !== "host") return;
    applyIntent(p);
    const st = snapshot();
    if (p.type === "play") st.playing = true;
    if (p.type === "pause") st.playing = false;
    if (p.type === "seek") st.time = Math.max(0, Number((p.payload || {}).time) || 0);
    if (p.type === "set_rate") st.rate = Number((p.payload || {}).rate) || 1;
    updateTimeline(st);
    L.seq += 1; L.rev += 1;
    const hash = await stateHash(st);
    if (p.type === "chat") {
      L.chat.push({ actor: p.actor, nickname: (p.payload || {}).nickname || "",
                    text: (p.payload || {}).text || "", ts: p.ts });
      if (L.chat.length > 50) L.chat.shift();
      if (window.ListenUI) window.ListenUI.renderChat();
    }
    const ev = { v: ROOM_VERSION, room_id: L.room.room_id, seq: L.seq, rev: L.rev,
                 state_hash: hash, type: "commit", actor: p.actor || L.room.host_id,
                 ts: now(), transport: L.transport,
                 members: L.members.slice(), chat_tail: L.chat.slice(-50),
                 payload: { proposal: p, state: st } };
    L.lastEvent = Object.assign({}, ev, {
      members: L.members.slice(), chat_tail: L.chat.slice(-50) });
    try {
      const r = await fetch("/api/room/commit", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ event: ev, state: st, members: L.members,
                               chat_tail: L.chat.slice(-50), transport: L.transport,
                               generation: L.generation }),
      });
      const j = await r.json();
      if (j.ok && j.event) {
        const canonical = Object.assign({}, ev, { seq: j.event.seq, rev: j.event.rev });
        L.seq = j.seq; L.rev = j.rev;
        const live = L.members.slice(), tail = L.chat.slice(-50);
        L.lastEvent = Object.assign({}, canonical, {
          members: live, chat_tail: tail });
        broadcast(canonical);
        if (window.ListenUI) window.ListenUI.renderChat();
        if (window.ListenUI) window.ListenUI.render();
        return;
      }
    } catch (e) {}
    broadcast(ev);
    if (window.ListenUI) window.ListenUI.render();
  }

  let pending = [];
  let lwwTimer = null;
  function propose(type, payload) {
    const p = { type: type, payload: payload || {}, actor: L.role === "host" ? (L.room && L.room.host_id) : L.member_id,
                ts: now(), op_id: Math.random().toString(36).slice(2) };
    if (L.role === "host") {
      pending.push(p);
      if (!lwwTimer) lwwTimer = setTimeout(() => {
        const batch = pending; pending = []; lwwTimer = null;
        batch.sort((a, b) => b.ts - a.ts || String(a.actor).localeCompare(String(b.actor)) || a.op_id.localeCompare(b.op_id));
        commitProposal(batch[0]);
      }, LWW_MS);
    } else if (L.transport === "webrtc") {
      sendData({ kind: "propose", proposal: p });
    } else {
      postJSON(L.base + "/room/propose?grant=" + encodeURIComponent(L.grant),
               Object.assign({ member_id: L.member_id }, p));
    }
  }
  L.propose = propose;
  L.hostAdvance = () => propose("advance", {});

  function sendData(obj) {
    const text = JSON.stringify(obj);
    if (L.role === "member" && memberDc && memberDc.readyState === "open") {
      try { memberDc.send(text); } catch (e) {}
    }
    L.slots.forEach((s) => {
      if (s.dc && s.dc.readyState === "open") {
        try { s.dc.send(text); } catch (e) {}
      }
    });
  }
  async function postJSON(url, body) {
    try { await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); }
    catch (e) {}
  }
  function broadcast(ev) {
    if (!L.active || L.transport !== "webrtc") return;
    sendData({ kind: "event", event: ev });
  }

  function handleProposal(p) {
    if (!p || typeof p !== "object" || L.role !== "host") return;
    pending.push(p);
    if (!lwwTimer) lwwTimer = setTimeout(() => {
      const batch = pending; pending = []; lwwTimer = null;
      batch.sort((a, b) => b.ts - a.ts || String(a.actor).localeCompare(String(b.actor)) || a.op_id.localeCompare(b.op_id));
      commitProposal(batch[0]);
    }, LWW_MS);
  }

  function handleEvent(ev) {
    if (!ev || ev.type !== "commit") return;
    if (ev.seq <= L.seq) return;
    if (ev.seq > L.seq + 1) { requestResync(); return; }
    L.seq = ev.seq; L.rev = ev.rev || ev.seq;
    if (ev.payload && ev.payload.state) {
      const st = ev.payload.state;
      window.MusicHubPlayer.apply(st);
      updateTimeline(st);
    }
    if (ev.members) L.members = ev.members.slice();
    if (ev.chat_tail) { L.chat = ev.chat_tail.slice(-50); if (window.ListenUI) window.ListenUI.renderChat(); }
    if (window.ListenUI) window.ListenUI.render();
  }
  function requestResync() {
    if (L.role !== "host" && L.base && L.grant) {
      syncHttpState();
      return;
    }
    if (L.role === "host") return;
    sendData({ kind: "resync", since: L.seq });
  }

  // ---------- WebRTC 房主媒体捕获 ----------
  let audioCtx = null, mediaSource = null, localGain = null, sendGain = null, audioTrack = null;
  let videoTrack = null, canvasTrack = null, captureCanvas = null, captureTimer = null;
  async function ensureAudioTrack() {
    if (audioTrack && audioTrack.readyState === "live") return audioTrack;
    const a = window.MusicHubPlayer.audio;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("当前环境不支持 WebAudio");
    audioCtx = audioCtx || new Ctx();
    if (audioCtx.state === "suspended") await audioCtx.resume();
    if (!mediaSource) mediaSource = audioCtx.createMediaElementSource(a);
    localGain = localGain || audioCtx.createGain();
    sendGain = sendGain || audioCtx.createGain();
    const dest = audioCtx.createMediaStreamDestination();
    mediaSource.connect(localGain); localGain.connect(audioCtx.destination);
    mediaSource.connect(sendGain); sendGain.connect(dest);
    localGain.gain.value = 1; sendGain.gain.value = 1;
    audioTrack = dest.stream.getAudioTracks()[0];
    return audioTrack;
  }
  function placeholderVideoTrack() {
    const c = $("room-capture-canvas") || document.createElement("canvas");
    c.width = 320; c.height = 180;
    const ctx = c.getContext("2d");
    ctx.fillStyle = "#12121a"; ctx.fillRect(0, 0, c.width, c.height);
    const stream = c.captureStream ? c.captureStream(1) : null;
    if (!stream) return null;
    return stream.getVideoTracks()[0] || null;
  }
  async function ensureVideoTrack() {
    const vel = $("lo-video-el");
    if (vel && typeof vel.captureStream === "function" && vel.videoWidth) {
      try {
        const t = vel.captureStream(15).getVideoTracks()[0];
        if (t) { videoTrack = t; return t; }
      } catch (e) {}
    }
    if (canvasTrack && canvasTrack.readyState === "live") return canvasTrack;
    const c = $("room-capture-canvas");
    if (!c || !c.captureStream || !vel) return placeholderVideoTrack();
    c.width = 640; c.height = 360;
    const ctx = c.getContext("2d");
    if (!ctx) return placeholderVideoTrack();
    if (captureTimer) clearInterval(captureTimer);
    const draw = () => {
      try { ctx.drawImage(vel, 0, 0, c.width, c.height); }
      catch (e) { ctx.fillStyle = "#12121a"; ctx.fillRect(0, 0, c.width, c.height); }
    };
    draw();
    captureTimer = setInterval(draw, 66);
    canvasTrack = c.captureStream(15).getVideoTracks()[0] || placeholderVideoTrack();
    return canvasTrack;
  }
  async function hostTracks() {
    const a = await ensureAudioTrack();
    let v = null;
    try { v = await ensureVideoTrack(); } catch (e) { v = placeholderVideoTrack(); }
    return { audio: a, video: v };
  }
  L.captureHostMedia = hostTracks;

  function attachTracks(pc, tracks) {
    const senders = pc.getSenders();
    const transceivers = pc.getTransceivers();
    transceivers.forEach((tr) => {
      const kind = tr.receiver && tr.receiver.track && tr.receiver.track.kind;
      const sender = tr.sender || senders.find((x) => x.track === null);
      if (!sender || !kind || !tracks[kind]) return;
      try { sender.replaceTrack(tracks[kind]); } catch (e) {}
    });
  }

  function pcConfig() {
    return { iceServers: [{ urls: ICE_STUN }], bundlePolicy: "max-bundle",
             rtcpMuxPolicy: "require", iceCandidatePoolSize: 0 };
  }
  function waitIceComplete(pc, ms) {
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { pc.removeEventListener("icegatheringstatechange", on); clearTimeout(timer); resolve(); };
      const on = () => { if (pc.iceGatheringState === "complete") done(); };
      pc.addEventListener("icegatheringstatechange", on);
      const timer = setTimeout(done, ms || 10000);
    });
  }
  function addCandidates(pc, list) {
    (list || []).forEach(async (c) => {
      try { await pc.addIceCandidate(c); } catch (e) {}
    });
  }

  // ---------- 房主: 3 个 slot ----------
  async function createRoom(reset) {
    const j = await fetch("/api/room/local?reset=" + (reset ? "1" : "0")).then((r) => r.json());
    if (!j.ok) throw new Error(j.error || "创建房间失败");
    L.role = "host"; L.active = true; L.room = j; L.member_id = j.host_id;
    L.members = [{ member_id: j.host_id, nickname: "房主", role: "host", online: true }];
    L.slots = j.slots.map((s) => ({ slot: s.slot, member_id: s.member_id,
                                    grant: s.grant, state: "pending", pc: null, dc: null,
                                    offer: "", answer: "" }));
    L.base = j.loopback_url; L.fallback_urls = j.fallback_urls || [];
    L.transport = "webrtc"; L.fallbackReason = "";
    L.generation += 1;
    L.chat = [];
    L.seq = 0; L.rev = 0; L.lastEvent = null; L.hostProposalSeq = 0;
    const st = snapshot(); updateTimeline(st);
    const hash = await stateHash(st);
    const initial = { v: ROOM_VERSION, room_id: j.room_id, seq: 1, rev: 1,
      state_hash: hash, type: "commit", actor: j.host_id, ts: now(), transport: L.transport,
      payload: { proposal: { type: "state" }, state: st } };
    const r = await fetch("/api/room/commit", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event: initial, state: st, members: L.members, chat_tail: [],
                             transport: L.transport, generation: L.generation }) });
    const commit = await r.json();
    if (commit.ok && commit.event) {
      L.seq = commit.seq; L.rev = commit.rev;
      L.lastEvent = Object.assign({}, initial, { seq: commit.seq, rev: commit.rev });
    } else {
      L.seq = 1; L.rev = 1; L.lastEvent = initial;
    }
    startHostProposalPoll();
    startAnchor();
    try { await hostTracks(); } catch (e) { toast("音频捕获不可用，将尝试 LAN 回退：" + e.message); enableHttpFallback("media_capture_failed"); }
    if (window.ListenUI) window.ListenUI.render();
    return j;
  }
  L.createRoom = createRoom;

  async function makeOffer(slot) {
    const s = L.slots[slot - 1];
    if (!s || typeof RTCPeerConnection !== "function") throw new Error("当前环境不支持 WebRTC");
    if (s.pc) { try { s.pc.close(); } catch (e) {} }
    s.pc = null; s.dc = null; s.offer = ""; s.answer = ""; s.state = "pending";
    if (L.transport === "http") {
      try {
        const j = await fetch("/api/room/grant?slot=" + encodeURIComponent(slot)).then((r) => r.json());
        if (j.ok && j.grant) s.grant = j.grant;
      } catch (e) {}
    }
    const pc = new RTCPeerConnection(pcConfig());
    s.pc = pc;
    pc.addTransceiver("audio", { direction: "sendonly" });
    pc.addTransceiver("video", { direction: "sendonly" });
    const dc = pc.createDataChannel("room", { ordered: true });
    s.dc = dc;
    dc.onopen = () => {
      s.state = "online";
      dc.send(JSON.stringify({ kind: "room_state", event: L.lastEvent, state: snapshot(),
                               members: L.members, chat: L.chat, seq: L.seq, rev: L.rev }));
      if (window.ListenUI) window.ListenUI.render();
    };
    dc.onmessage = (e) => {
      let m; try { m = JSON.parse(e.data); } catch (_) { return; }
      if (m.kind === "propose") handleProposal(m.proposal);
      if (m.kind === "chat") handleProposal({ type: "chat", payload: m.payload,
                                               actor: s.member_id, ts: m.ts || now(), op_id: m.op_id });
      if (m.kind === "hello") {
        if (!L.members.some((x) => x.member_id === s.member_id))
          L.members.push({ member_id: s.member_id, nickname: m.nickname || "成员", role: "member", online: true });
        const live = L.members.slice(), tail = L.chat.slice(-50);
        if (L.lastEvent) {
          L.lastEvent = Object.assign({}, L.lastEvent, { members: live, chat_tail: tail });
          dc.send(JSON.stringify({ kind: "event", event: L.lastEvent }));
        }
        if (window.ListenUI) window.ListenUI.render();
      }
      if (m.kind === "resync") {
        const live = L.members.slice(), tail = L.chat.slice(-50);
        const resync = Object.assign({}, L.lastEvent || {}, {
          v: ROOM_VERSION, room_id: L.room.room_id, seq: L.seq, rev: L.rev,
          type: "commit", actor: L.room.host_id, ts: now(), transport: L.transport,
          members: live, chat_tail: tail,
          payload: Object.assign({}, (L.lastEvent && L.lastEvent.payload) || {}, { state: snapshot() }) });
        dc.send(JSON.stringify({ kind: "event", event: resync }));
      }
    };
    dc.onclose = () => { s.state = "offline"; if (window.ListenUI) window.ListenUI.render(); };
    pc.onicecandidate = () => {};
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") enableHttpFallback("ice_failed");
    };
    try { attachTracks(pc, await hostTracks()); } catch (e) {}
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitIceComplete(pc, 10000);
    s.offer = encodeInvite({ v: ROOM_VERSION, kind: "offer", room_id: L.room.room_id,
      host_id: L.room.host_id, epoch: L.room.epoch, slot: s.slot, member_id: s.member_id,
      role: "member", sdp: pc.localDescription, grant: s.grant,
      fallback_urls: L.room.fallback_urls || [], room_port: L.room.room_port,
      exp: now() + 600000, cap: { audio: true, video: true } });
    s.state = "pending";
    if (window.ListenUI) window.ListenUI.render();
    return s.offer;
  }
  L.makeOffer = makeOffer;

  async function acceptAnswer(slot, text) {
    const inv = decodeInvite(text);
    const s = L.slots[slot - 1];
    if (!s || inv.kind !== "answer" || inv.room_id !== L.room.room_id ||
        inv.member_id !== s.member_id) throw new Error("应答码与房间席位不匹配");
    await s.pc.setRemoteDescription(inv.sdp);
    addCandidates(s.pc, inv.candidates || []);
    s.answer = text;
    if (window.ListenUI) window.ListenUI.render();
  }
  L.acceptAnswer = acceptAnswer;

  // ---------- 成员: 加入房间 ----------
  async function joinRoom(text) {
    const inv = decodeInvite(text);
    if (inv.kind !== "offer" || typeof RTCPeerConnection !== "function") throw new Error("邀请码无效或当前环境不支持 WebRTC");
    L.role = "member"; L.active = true; L.room = inv; L.member_id = inv.member_id;
    L.seq = 0; L.rev = 0; L.generation = 0; L.chat = []; L.members = [];
    L.lastEvent = null; L.fallbackReason = ""; L.transport = "webrtc";
    L.grant = inv.grant; L.fallback_urls = inv.fallback_urls || [];
    L.base = await chooseBase(L.fallback_urls, inv.grant);
    const pc = new RTCPeerConnection(pcConfig());
    memberPc = pc;
    remoteStream = new MediaStream();
    pc.ontrack = (e) => {
      remoteStream.addTrack(e.track);
      mountRemote();
    };
    const dcReady = new Promise((resolve) => {
      pc.ondatachannel = (e) => {
        memberDc = e.channel;
        memberDc.onmessage = (ev) => {
          let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
          if (m.kind === "event") handleEvent(m.event);
          if (m.kind === "anchor" && m.payload && m.payload.state) updateTimeline(m.payload.state);
          if (m.kind === "room_state") {
            L.seq = m.seq || 0; L.rev = m.rev || 0;
            L.members = (m.event && m.event.members) || m.members || [];
            L.chat = (m.event && m.event.chat_tail) || m.chat || [];
            if (m.state) { window.MusicHubPlayer.apply(m.state); updateTimeline(m.state); }
            if (window.ListenUI) { window.ListenUI.renderChat(); window.ListenUI.render(); }
          }
        };
        memberDc.onopen = () => {
          memberDc.send(JSON.stringify({ kind: "hello", member_id: L.member_id, nickname: nick(),
                                        last_seq: L.seq }));
          resolve();
        };
      };
    });
    await pc.setRemoteDescription(inv.sdp);
    addCandidates(pc, inv.candidates || []);
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    await waitIceComplete(pc, 10000);
    L.answerCode = encodeInvite({ v: ROOM_VERSION, kind: "answer", room_id: inv.room_id,
      host_id: inv.host_id, epoch: inv.epoch, slot: inv.slot, member_id: inv.member_id,
      role: "member", sdp: pc.localDescription, exp: now() + 600000 });
    const timeout = setTimeout(() => enableHttpFallback("webrtc_timeout"), 12000);
    dcReady.then(() => { clearTimeout(timeout); if (window.ListenUI) window.ListenUI.render(); });
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") enableHttpFallback("ice_failed");
    };
    if (window.ListenUI) window.ListenUI.render();
    return L.answerCode;
  }
  L.joinRoom = joinRoom;

  function UI_hideRemoteVideo() {
    const box = $("lo-video");
    if (box) box.classList.add("hidden");
  }
  function mountRemote() {
    const v = $("room-remote-video");
    if (!v || !remoteStream) return;
    v.classList.remove("hidden");
    const box = $("lo-video");
    if (box) box.classList.remove("hidden");
    if (v.srcObject !== remoteStream) v.srcObject = remoteStream;
    v.muted = false;
    const p = v.play(); if (p && p.catch) p.catch(() => toast("点击开启一起听声音", 4000));
    const a = window.MusicHubPlayer.audio;
    a.pause(); a.removeAttribute("src"); a.load();
    if (window.ListenUI) window.ListenUI.setSoundButton(true);
  }

  async function chooseBase(urls, grant) {
    for (const u of urls || []) {
      try {
        const r = await fetch(u + "/room/ping?grant=" + encodeURIComponent(grant), { mode: "cors" });
        if (r.ok) return u;
      } catch (e) {}
    }
    return (urls && urls[0]) || "";
  }
  async function syncHttpState() {
    if (!L.base || !L.grant) return;
    try {
      const j = await fetch(L.base + "/room/state?grant=" + encodeURIComponent(L.grant), { mode: "cors" }).then((r) => r.json());
      if (!j.ok) return;
      L.seq = j.seq; L.rev = j.rev; L.members = j.members || []; L.chat = j.chat_tail || [];
      if (j.state) { window.MusicHubPlayer.apply(j.state); updateTimeline(j.state); }
      if (window.ListenUI) window.ListenUI.render();
    } catch (e) {}
  }
  async function pollHttpEvents() {
    if (L.role !== "member" || L.transport !== "http" || !L.active) return;
    try {
      const j = await fetch(L.base + "/room/events?grant=" + encodeURIComponent(L.grant) +
                            "&since=" + L.seq + "&wait=5", { mode: "cors" }).then((r) => r.json());
      if (j.ok) {
        (j.events || []).forEach(handleEvent);
        if (j.state) updateTimeline(j.state);
        L.members = j.members || L.members;
        L.chat = j.chat_tail || L.chat;
        if (window.ListenUI) { window.ListenUI.renderChat(); window.ListenUI.render(); }
      }
    } catch (e) {}
    if (L.active && L.transport === "http" && L.role === "member") setTimeout(pollHttpEvents, 200);
  }
  async function pollHostProposals() {
    if (L.role !== "host" || !L.active) return;
    try {
      const j = await fetch("/api/room/proposals?since=" + L.hostProposalSeq + "&wait=5").then((r) => r.json());
      if (j.ok) { L.hostProposalSeq = j.seq || 0; (j.proposals || []).forEach(handleProposal); }
    } catch (e) {}
    if (L.active && L.role === "host") setTimeout(pollHostProposals, 200);
  }
  function startHostProposalPoll() { L.hostProposalSeq = L.hostProposalSeq || 0; pollHostProposals(); }
  function startAnchor() {
    if (L.anchorTimer) clearInterval(L.anchorTimer);
    L.anchorTimer = setInterval(async () => {
      if (L.role !== "host" || !L.active) return;
      if (L.role === "host" && L.transport === "webrtc")
        sendData({ kind: "anchor", payload: { state: snapshot() } });
      const st = snapshot(); updateTimeline(st);
      try {
        await fetch("/api/room/commit", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ event: { v: ROOM_VERSION, room_id: L.room.room_id,
            seq: L.seq, rev: L.rev, state_hash: "", type: "anchor", actor: L.room.host_id,
            ts: now(), transport: L.transport, payload: { state: st } },
            state: st, members: L.members, chat_tail: L.chat.slice(-50),
            transport: L.transport, generation: L.generation }) });
      } catch (e) {}
    }, 2000);
  }

  function enableHttpFallback(reason) {
    if (L.transport === "http" || !L.active) return;
    L.transport = "http"; L.fallbackReason = reason || "";
    toast("WebRTC 不可用，已自动切换 LAN 回退", 4000);
    if (L.role === "member") {
      const gen = L.generation + 1;
      L.generation = gen;
      const v = $("room-remote-video"), a = $("room-fallback-audio");
      if (v) { v.classList.remove("hidden"); const box = $("lo-video"); if (box) box.classList.remove("hidden"); v.srcObject = null; v.muted = true; v.src = L.base + "/room/stream?kind=video&grant=" + encodeURIComponent(L.grant) + "&gen=" + gen; v.play().catch(() => {}); }
      if (a) { a.src = L.base + "/room/stream?kind=audio&grant=" + encodeURIComponent(L.grant) + "&gen=" + gen; a.play().catch(() => {}); }
      const localAudio = window.MusicHubPlayer.audio;
      localAudio.pause(); localAudio.removeAttribute("src"); localAudio.load();
      syncHttpState(); pollHttpEvents();
    }
    if (window.ListenUI) window.ListenUI.render();
  }
  L.enableHttpFallback = enableHttpFallback;
  function retryWebRTC() {
    if (!L.active) { toast("请先创建或加入房间"); return; }
    if (L.role === "host") {
      L.slots.forEach((s) => { try { s.dc && s.dc.close(); } catch (e) {} try { s.pc && s.pc.close(); } catch (e) {} s.pc = null; s.dc = null; s.offer = ""; s.answer = ""; s.state = "pending"; });
    } else if (memberPc) {
      try { memberPc.close(); } catch (e) {}
      memberPc = null; memberDc = null; remoteStream = null;
    }
    L.transport = "webrtc"; L.fallbackReason = "";
    const v = $("room-remote-video"), a = $("room-fallback-audio");
    if (v) { v.pause(); v.srcObject = null; v.removeAttribute("src"); v.classList.add("hidden"); }
    if (a) { a.pause(); a.removeAttribute("src"); }
    toast("已重置为 WebRTC，请重新生成/粘贴邀请码");
    if (window.ListenUI) window.ListenUI.render();
  }
  L.retryWebRTC = retryWebRTC;

  async function sendChat(text) {
    text = String(text || "").trim().slice(0, 200);
    if (!text) return;
    const p = { type: "chat", payload: { text: text, nickname: nick() }, actor: L.member_id,
                ts: now(), op_id: Math.random().toString(36).slice(2) };
    if (L.role === "host") handleProposal(p);
    else if (L.transport === "webrtc") sendData({ kind: "chat", payload: p.payload,
                                                  member_id: L.member_id, ts: p.ts, op_id: p.op_id });
    else postJSON(L.base + "/room/chat?grant=" + encodeURIComponent(L.grant),
                  { member_id: L.member_id, text: text, nickname: nick(), ts: now(), op_id: p.op_id });
  }
  L.sendChat = sendChat;

  async function leaveRoom() {
    L.active = false;
    if (L.anchorTimer) clearInterval(L.anchorTimer);
    if (captureTimer) clearInterval(captureTimer);
    L.slots.forEach((s) => { try { s.dc && s.dc.close(); } catch (e) {} try { s.pc && s.pc.close(); } catch (e) {} });
    if (memberPc) { try { memberPc.close(); } catch (e) {} }
    const v = $("room-remote-video"), a = $("room-fallback-audio");
    if (v) { v.pause(); v.srcObject = null; v.removeAttribute("src"); v.classList.add("hidden"); }
    if (a) { a.pause(); a.removeAttribute("src"); }
    UI_hideRemoteVideo();
    if (L.role === "host") { try { await fetch("/api/room/close", { method: "POST" }); } catch (e) {} }
    L.role = ""; L.transport = "webrtc"; L.room = null; L.slots = []; L.members = []; L.chat = [];
    if (window.ListenUI) window.ListenUI.render();
  }
  L.leaveRoom = leaveRoom;
  L.debug = () => ({ role: L.role, active: L.active, transport: L.transport, seq: L.seq,
                     rev: L.rev, members: L.members.length, slots: L.slots.map((s) => s.state),
                     room_id: L.room && L.room.room_id, base: L.base, reason: L.fallbackReason });
})();
