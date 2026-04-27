/* ═══════════════════════════════════════════════════════════════════════════
   Big Red Command Center — v2026.ULTRA-ELITE
   Shared Utilities · ChimeEngine · WebSocket Manager · API Client
   ═══════════════════════════════════════════════════════════════════════════ */

// ── Auth Token Manager ──────────────────────────────────────────────────────
let _token = sessionStorage.getItem('race_token') || '';

function _getToken() {
  if (!_token) {
    const t = prompt(
      'Enter admin token\n(value of RACE_API_TOKEN from .env — check server logs on first run)'
    );
    if (t) {
      _token = t.trim();
      sessionStorage.setItem('race_token', _token);
    }
  }
  return _token;
}

function clearToken() {
  _token = '';
  sessionStorage.removeItem('race_token');
}

// ── WebSocket Manager ───────────────────────────────────────────────────────
let _ws = null;
let _wsReconnectDelay = 1000;
const _wsMaxDelay = 30000;

function connectWS(onMessage) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${proto}://${location.host}/ws`;

  try {
    _ws = new WebSocket(url);
  } catch {
    setTimeout(() => connectWS(onMessage), _wsReconnectDelay);
    return;
  }

  _ws.onopen = () => {
    console.info('[WS] ✓ connected');
    _wsReconnectDelay = 1000; // reset on success
  };

  _ws.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type !== 'pong') {
        onMessage(msg);
      }
    } catch {}
  };

  _ws.onerror = () => console.warn('[WS] error');

  _ws.onclose = () => {
    console.info('[WS] disconnected — retrying in', _wsReconnectDelay, 'ms');
    setTimeout(() => connectWS(onMessage), _wsReconnectDelay);
    _wsReconnectDelay = Math.min(_wsReconnectDelay * 1.5, _wsMaxDelay);
  };

  // Keep-alive ping every 25s
  setInterval(() => {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
      _ws.send('ping');
    }
  }, 25000);
}

// ── API Client ──────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {} };

  if (method !== 'GET') {
    const tok = _getToken();
    if (tok) opts.headers['Authorization'] = `Bearer ${tok}`;
  }

  if (body instanceof FormData) {
    opts.body = body;
  } else if (body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }

  const res = await fetch(`/api${path}`, opts);

  if (res.status === 401) {
    clearToken();
    throw new Error('Authentication failed — refresh and enter the correct token.');
  }
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(txt || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Chime Engine (Web Audio API) ────────────────────────────────────────────
const ChimeEngine = (() => {
  let _ctx = null;
  let _enabled = true;

  function _getCtx() {
    if (!_ctx) {
      _ctx = new (window.AudioContext || window.webkitAudioContext)();
    }
    return _ctx;
  }

  function enable()  { _enabled = true; }
  function disable() { _enabled = false; }
  function toggle()  { _enabled = !_enabled; return _enabled; }
  function isEnabled() { return _enabled; }

  /**
   * Play a professional Olympic-style confirmation chime.
   * Three ascending tones: C5 → E5 → G5 (major chord arpeggio)
   */
  function playFinishChime() {
    if (!_enabled) return;
    const ctx = _getCtx();
    const now = ctx.currentTime;
    const notes = [523.25, 659.25, 783.99]; // C5, E5, G5
    const durations = [0.15, 0.15, 0.35];
    const delays = [0, 0.12, 0.24];

    notes.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();

      osc.type = 'sine';
      osc.frequency.value = freq;

      // Smooth envelope
      gain.gain.setValueAtTime(0, now + delays[i]);
      gain.gain.linearRampToValueAtTime(0.25, now + delays[i] + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, now + delays[i] + durations[i]);

      osc.connect(gain);
      gain.connect(ctx.destination);

      osc.start(now + delays[i]);
      osc.stop(now + delays[i] + durations[i] + 0.05);
    });
  }

  /**
   * Play a soft notification ping (single tone)
   */
  function playPing() {
    if (!_enabled) return;
    const ctx = _getCtx();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = 'sine';
    osc.frequency.value = 880; // A5

    gain.gain.setValueAtTime(0, now);
    gain.gain.linearRampToValueAtTime(0.15, now + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.2);

    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.25);
  }

  /**
   * Play a warning tone
   */
  function playWarning() {
    if (!_enabled) return;
    const ctx = _getCtx();
    const now = ctx.currentTime;
    const freqs = [440, 349]; // A4 → F4 (descending)

    freqs.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0, now + i * 0.15);
      gain.gain.linearRampToValueAtTime(0.2, now + i * 0.15 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.15 + 0.2);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(now + i * 0.15);
      osc.stop(now + i * 0.15 + 0.25);
    });
  }

  return { enable, disable, toggle, isEnabled, playFinishChime, playPing, playWarning };
})();

// ── Time Formatting ─────────────────────────────────────────────────────────
function fmtElapsed(seconds) {
  if (seconds == null || isNaN(seconds) || seconds < 0) return '--:--.---';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = (seconds % 60).toFixed(3).padStart(6, '0');
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${s}`;
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
  if (!mm || mm < 0) return '—';
  return mm >= 1000 ? `${(mm / 1000).toFixed(2)} m` : `${Math.round(mm)} mm`;
}

// ── Badge Helpers ───────────────────────────────────────────────────────────
function catBadge(cat) {
  const icons = { Students: '🎓', Alumni: '🏛', Parents: '👪' };
  const icon = icons[cat] || '';
  return `<span class="badge badge-${cat}">${icon} ${cat || '—'}</span>`;
}

function statusBadge(s) {
  const icons = { pending: '⏳', accepted: '✓', overridden: '↻', rejected: '✗' };
  const icon = icons[s] || '';
  return `<span class="badge badge-${s}">${icon} ${s}</span>`;
}

// ── Feed Line ───────────────────────────────────────────────────────────────
function feedLine(feed, text, cls = '') {
  const now = new Date().toLocaleTimeString('en-US', { hour12: false });
  const line = document.createElement('div');
  line.className = `feed-line ${cls}`;
  line.textContent = `${now}  ${text}`;

  // Animate entry
  line.style.opacity = '0';
  line.style.transform = 'translateX(-10px)';
  feed.appendChild(line);

  requestAnimationFrame(() => {
    line.style.transition = 'all 0.3s ease';
    line.style.opacity = '1';
    line.style.transform = 'translateX(0)';
  });

  feed.scrollTop = feed.scrollHeight;
  while (feed.children.length > 150) feed.removeChild(feed.firstChild);
}

// ── Notification Toast ──────────────────────────────────────────────────────
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
  el.innerHTML = `<div class="notif-title">${title}</div><div style="color:var(--text-secondary)">${body}</div>`;
  area.appendChild(el);

  // Auto-dismiss with fade
  setTimeout(() => {
    el.style.transition = 'all 0.4s ease';
    el.style.opacity = '0';
    el.style.transform = 'translateX(30px)';
    setTimeout(() => el.remove(), 400);
  }, 4000);
}

// ── Animated Number Counter ─────────────────────────────────────────────────
function animateNumber(el, target, duration = 400) {
  const start = parseInt(el.textContent) || 0;
  if (start === target) return;
  const startTime = performance.now();

  function tick(now) {
    const progress = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
    el.textContent = Math.round(start + (target - start) * eased);
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

// ── Export CSV ───────────────────────────────────────────────────────────────
function downloadCSV(rows, filename) {
  if (!rows.length) return;
  const headers = Object.keys(rows[0]);
  const csv = [
    headers.join(','),
    ...rows.map(r => headers.map(h => `"${(r[h] ?? '').toString().replace(/"/g, '""')}"`).join(','))
  ].join('\n');

  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
