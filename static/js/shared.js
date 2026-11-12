/* ============================================================
   Campus Mind – Shared JS Utilities
   ============================================================ */

// ── API base ──
const API = '';  // same origin

// ── Toast ──
function showToast(msg, type = 'info', duration = 3500) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }
  const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${icons[type] || ''}</span><span>${msg}</span>`;
  container.appendChild(t);
  setTimeout(() => { t.style.animation = 'slideIn .3s ease reverse'; setTimeout(() => t.remove(), 300); }, duration);
}

// ── Date/Time ──
function fmtDateTime() {
  const now = new Date();
  const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const h = now.getHours(), m = now.getMinutes();
  const ampm = h >= 12 ? 'PM' : 'AM';
  const hh = (h % 12 || 12).toString().padStart(2,'0');
  const mm = m.toString().padStart(2,'0');
  return `${days[now.getDay()]}, ${now.getDate()} ${months[now.getMonth()]} ${now.getFullYear()} · ${hh}:${mm} ${ampm}`;
}

function startClock(el) {
  if (!el) return;
  el.textContent = fmtDateTime();
  setInterval(() => { el.textContent = fmtDateTime(); }, 10000);
}

// ── Format seconds ──
function fmtSec(s) {
  if (!s || s < 0) s = 0;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0
    ? `${h.toString().padStart(2,'0')}:${m.toString().padStart(2,'0')}:${sec.toString().padStart(2,'0')}`
    : `${m.toString().padStart(2,'0')}:${sec.toString().padStart(2,'0')}`;
}

// ── Sidebar toggle (mobile) ──
function initSidebar() {
  const sidebar  = document.querySelector('.sidebar');
  const overlay  = document.querySelector('.sidebar-overlay');
  const hamburger = document.querySelector('.hamburger');
  if (!sidebar) return;

  function open()  { sidebar.classList.add('open');  overlay && overlay.classList.add('open'); }
  function close() { sidebar.classList.remove('open'); overlay && overlay.classList.remove('open'); }

  hamburger && hamburger.addEventListener('click', () => sidebar.classList.contains('open') ? close() : open());
  overlay   && overlay.addEventListener('click', close);
}

// ── Number stepper ──
function initSteppers() {
  document.querySelectorAll('.number-input-wrap').forEach(wrap => {
    const input = wrap.querySelector('input[type="number"]');
    wrap.querySelectorAll('.num-stepper button').forEach(btn => {
      btn.addEventListener('click', () => {
        const step = parseInt(input.step) || 1;
        const min  = parseInt(input.min)  || 0;
        const max  = parseInt(input.max)  || 99999;
        if (btn.dataset.dir === 'up')   input.value = Math.min(max, (parseInt(input.value)||0) + step);
        if (btn.dataset.dir === 'down') input.value = Math.max(min, (parseInt(input.value)||0) - step);
        input.dispatchEvent(new Event('input'));
      });
    });
  });
}

// ── Progress SVG ring ──
function updateRing(svgId, pct) {
  const circle = document.getElementById(svgId);
  if (!circle) return;
  const r = circle.r.baseVal.value;
  const circ = 2 * Math.PI * r;
  const offset = circ - (pct / 100) * circ;
  circle.style.strokeDasharray  = circ;
  circle.style.strokeDashoffset = offset;
}

// ── Fetch helpers ──
async function apiFetch(url, opts = {}) {
  const defaults = {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts
  };
  if (opts.body && typeof opts.body === 'object') defaults.body = JSON.stringify(opts.body);
  const res = await fetch(API + url, defaults);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json();
}

// ── WebSocket with auto-reconnect ──
class ReconnectingWS {
  constructor(url, onMessage, onOpen, onClose) {
    this.url = url; this.onMessage = onMessage;
    this.onOpen = onOpen; this.onClose = onClose;
    this.ws = null; this.closed = false;
    this.connect();
  }
  connect() {
    if (this.closed) return;
    this.ws = new WebSocket(this.url);
    this.ws.addEventListener('open',    () => this.onOpen && this.onOpen());
    this.ws.addEventListener('message', e => this.onMessage && this.onMessage(JSON.parse(e.data)));
    this.ws.addEventListener('close',   () => { this.onClose && this.onClose(); if (!this.closed) setTimeout(() => this.connect(), 2000); });
    this.ws.addEventListener('error',   () => this.ws.close());
  }
  close() { this.closed = true; this.ws && this.ws.close(); }
  send(data) { this.ws && this.ws.readyState === 1 && this.ws.send(JSON.stringify(data)); }
}

// ── Status badge HTML ──
function statusBadge(status) {
  const map = {
    'Joined':       ['badge-joined',       '✓ Joined'],
    'Paused':       ['badge-paused',       '⏸ Paused'],
    'Disconnected': ['badge-disconnected', '✗ Disconnected'],
    'Present':      ['badge-present',      '✓ Present'],
  };
  const [cls, label] = map[status] || ['', status];
  return `<span class="badge ${cls}">${label}</span>`;
}

// ── Signal HTML ──
function signalHTML(lastSeen) {
  const ago = Date.now()/1000 - lastSeen;
  let cls = 'strong';
  if (ago > 20) cls = 'medium';
  if (ago > 60) cls = 'weak';
  if (ago > 120) cls = 'off';
  return `<div class="signal ${cls}"><span></span><span></span><span></span></div>`;
}

// Init on load
document.addEventListener('DOMContentLoaded', () => {
  initSidebar();
  initSteppers();
  startClock(document.getElementById('clock'));
});

