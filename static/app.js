/* Shared utilities for Big Red Command Center */

// ── WebSocket ──────────────────────────────────────────────────────────────
let _ws = null;

function connectWS(onMessage) {
  const url = `ws://${location.host}/ws`;
  _ws = new WebSocket(url);
  _ws.onopen    = () => console.info('[WS] connected');
  _ws.onmessage = (e) => { try { onMessage(JSON.parse(e.data)); } catch {} };
  _ws.onerror   = () => console.warn('[WS] error');
  _ws.onclose   = () => setTimeout(() => connectWS(onMessage), 3000);
}

// ── Time formatting ────────────────────────────────────────────────────────
function fmtElapsed(seconds) {
  if (seconds == null || isNaN(seconds) || seconds < 0) return '--:--.---';
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(3).padStart(6, '0');
  return `${m}:${s}`;
}

function fmtWall(ts) {
  if (!ts) return '---';
  return new Date(ts * 1000).toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    fractionalSecondDigits: 3, hour12: false,
  });
}

function fmtDepth(mm) {
  if (mm >= 1000) return `${(mm/1000).toFixed(2)} m`;
  return `${Math.round(mm)} mm`;
}

// ── API helper ─────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/api${path}`, opts);
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Notification toast ─────────────────────────────────────────────────────
function notify(title, body, color = 'var(--red)') {
  let area = document.getElementById('notif-area');
  if (!area) {
    area = document.createElement('div');
    area.id = 'notif-area';
    area.className = 'notif-area';
    document.body.appendChild(area);
  }
  const el = document.createElement('div');
  el.className = 'notif';
  el.style.borderLeftColor = color;
  el.innerHTML = `<div class="notif-title">${title}</div><div>${body}</div>`;
  area.appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

// ── Badge helpers ──────────────────────────────────────────────────────────
function catBadge(cat) {
  return `<span class="badge badge-${cat}">${cat}</span>`;
}
function statusBadge(s) {
  return `<span class="badge badge-${s}">${s}</span>`;
}

// ── Feed line ──────────────────────────────────────────────────────────────
function feedLine(feed, text, cls = '') {
  const now  = new Date().toLocaleTimeString('en-US', { hour12: false });
  const line = document.createElement('div');
  line.className = `feed-line ${cls}`;
  line.textContent = `${now}  ${text}`;
  feed.appendChild(line);
  feed.scrollTop = feed.scrollHeight;
  // Keep feed trim
  while (feed.children.length > 120) feed.removeChild(feed.firstChild);
}
