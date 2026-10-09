"""
Chat Network — โปรแกรมแชทภายในองค์กร / วง LAN / Tailscale VPN และมือถือ (Flask Single File)
ธีมสีฟ้า (Sky & Ocean Blue) | รองรับ Tailscale VPN, Wi-Fi LAN, มือถือสแกน QR Code | ตั้งห้องมีรหัส/ไม่มีรหัสได้
"""

import base64
import os
import random
import re
import socket
import sqlite3
import time
from flask import (Flask, request, session, redirect, url_for, jsonify,
                   render_template_string, g, flash, abort)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "chat-network-tailscale-blue-2026")
DB_PATH = os.environ.get("CHAT_DB", "chat.db")

MAX_NAME = 30
MAX_ROOM_NAME = 50
MAX_MSG = 3000
MAX_PASSWORD = 30
ONLINE_WINDOW = 10  # วินาที — ถือว่าออนไลน์ถ้า poll ภายในช่วงนี้


# ───────────────────────────── Network Helpers ─────────────────────────────
def get_local_ips():
    """ค้นหา IP ทั้งหมดของเครื่องนี้ โดยจัดลำดับ Tailscale VPN และ Wi-Fi/LAN ขึ้นก่อน"""
    ips = []
    seen = set()

    # ลองดึงจาก hostname
    try:
        host_ips = socket.gethostbyname_ex(socket.gethostname())[2]
        for ip in host_ips:
            if ip not in seen and not ip.startswith("127."):
                seen.add(ip)
                ips.append(ip)
    except Exception:
        pass

    # ลองหา Default route IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        default_ip = s.getsockname()[0]
        s.close()
        if default_ip not in seen and not default_ip.startswith("127."):
            seen.add(default_ip)
            ips.insert(0, default_ip)
    except Exception:
        pass

    categorized = []
    for ip in ips:
        if ip.startswith("100."):
            categorized.append({"ip": ip, "type": "Tailscale VPN", "priority": 1})
        elif ip.startswith("192.168."):
            categorized.append({"ip": ip, "type": "Wi-Fi / LAN", "priority": 2})
        elif ip.startswith("10.") or ip.startswith("172."):
            categorized.append({"ip": ip, "type": "LAN / Network", "priority": 3})
        else:
            categorized.append({"ip": ip, "type": "Network IP", "priority": 4})

    # เรียงลำดับ Tailscale -> Wi-Fi/LAN -> อื่นๆ
    categorized.sort(key=lambda x: x["priority"])

    if not categorized:
        categorized.append({"ip": "127.0.0.1", "type": "Localhost", "priority": 99})

    return categorized


# ───────────────────────────── Database ─────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS rooms (
            code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_by TEXT NOT NULL,
            password TEXT DEFAULT '',
            is_private INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_code TEXT NOT NULL,
            author TEXT,              -- NULL = ข้อความระบบ
            body TEXT NOT NULL,
            msg_type TEXT DEFAULT 'text',
            file_data TEXT,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_msg_room ON messages(room_code, id);
        CREATE TABLE IF NOT EXISTS presence (
            room_code TEXT NOT NULL,
            name TEXT NOT NULL,
            last_seen REAL NOT NULL,
            PRIMARY KEY (room_code, name)
        );
    """)

    # ตรวจสอบและอัปเกรด Schema ถ้าจำเป็น
    cur.execute("PRAGMA table_info(rooms)")
    room_cols = [row[1] for row in cur.fetchall()]
    if "password" not in room_cols:
        cur.execute("ALTER TABLE rooms ADD COLUMN password TEXT DEFAULT ''")
    if "is_private" not in room_cols:
        cur.execute("ALTER TABLE rooms ADD COLUMN is_private INTEGER DEFAULT 0")

    cur.execute("PRAGMA table_info(messages)")
    msg_cols = [row[1] for row in cur.fetchall()]
    if "msg_type" not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN msg_type TEXT DEFAULT 'text'")
    if "file_data" not in msg_cols:
        cur.execute("ALTER TABLE messages ADD COLUMN file_data TEXT")

    conn.commit()
    conn.close()


def get_room(code):
    return get_db().execute("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()


def new_room_code():
    db = get_db()
    used = {r["code"] for r in db.execute("SELECT code FROM rooms")}
    free = [f"{n:04d}" for n in range(1000, 10000) if f"{n:04d}" not in used]
    if not free:
        return None
    return random.choice(free)


def remember_room(code):
    recent = session.get("recent", [])
    if code in recent:
        recent.remove(code)
    recent.insert(0, code)
    session["recent"] = recent[:10]


def is_room_unlocked(code, room=None):
    """ตรวจสอบว่าผู้ใช้นี้ได้รับสิทธิ์เข้าห้องที่มีรหัสผ่านหรือไม่"""
    if not room:
        room = get_room(code)
    if not room:
        return False
    if not room["is_private"] or not room["password"]:
        return True
    if session.get("name") and room["created_by"] == session.get("name"):
        return True
    unlocked = session.get("unlocked_rooms", [])
    return code in unlocked


def unlock_room(code):
    unlocked = session.get("unlocked_rooms", [])
    if code not in unlocked:
        unlocked.append(code)
    session["unlocked_rooms"] = unlocked


# ───────────────────────────── Templates ─────────────────────────────
BASE = r"""<!doctype html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>{{ title or 'Chat Network — ระบบแชทฟ้าคราม (Tailscale & LAN)' }}</title>
<script src="https://cdn.tailwindcss.com"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@300;400;500;600;700&family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap" rel="stylesheet">
<!-- QRCode library for instant mobile scan -->
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<script>
  tailwind.config = {
    theme: {
      extend: {
        fontFamily: {
          sans: ['"IBM Plex Sans Thai"', '"Plus Jakarta Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
          mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', 'monospace']
        },
        colors: {
          brand: {
            50: '#f0f9ff',
            100: '#e0f2fe',
            200: '#bae6fd',
            300: '#7dd3fc',
            400: '#38bdf8',
            500: '#0ea5e9',
            600: '#0284c7',
            700: '#0369a1',
            800: '#075985',
            900: '#0c4a6e',
          }
        }
      }
    }
  }
</script>
<style>
  .scroll-thin::-webkit-scrollbar { width: 5px; height: 5px; }
  .scroll-thin::-webkit-scrollbar-thumb { background: #93c5fd; border-radius: 999px; }
  .scroll-thin::-webkit-scrollbar-track { background: transparent; }
  @keyframes popIn {
    from { opacity: 0; transform: translateY(8px) scale(0.98); }
    to { opacity: 1; transform: translateY(0) scale(1); }
  }
  .pop { animation: popIn 0.2s cubic-bezier(0.16, 1, 0.3, 1) forwards; }
  .glow-blue {
    box-shadow: 0 10px 30px -10px rgba(14, 165, 233, 0.45);
  }
  .glass-card {
    background: rgba(255, 255, 255, 0.94);
    backdrop-filter: blur(12px);
  }
</style>
</head>
<body class="font-sans antialiased text-slate-800 bg-gradient-to-br from-sky-50 via-blue-50/50 to-slate-100 min-h-screen">
{% with msgs = get_flashed_messages(with_categories=true) %}
  {% if msgs %}
  <div id="toast" class="fixed top-5 left-1/2 -translate-x-1/2 z-50 space-y-2 max-w-[90vw] sm:max-w-md">
    {% for cat, m in msgs %}
    <div class="px-4 py-3 rounded-2xl shadow-xl text-sm font-medium flex items-center gap-2 pop
      {{ 'bg-rose-500 text-white' if cat=='error' else 'bg-sky-600 text-white shadow-sky-500/30' }}">
      <svg class="h-5 w-5 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        {% if cat=='error' %}
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
        {% else %}
        <path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"/>
        {% endif %}
      </svg>
      <span>{{ m }}</span>
    </div>
    {% endfor %}
  </div>
  <script>setTimeout(()=>document.getElementById('toast')?.remove(), 4000)</script>
  {% endif %}
{% endwith %}

%%BODY%%

<!-- Global QR Code & Network Info Modal -->
<div id="qrModal" class="fixed inset-0 z-50 bg-slate-900/60 backdrop-blur-sm hidden items-center justify-center p-4">
  <div class="bg-white rounded-3xl p-6 sm:p-8 max-w-sm w-full shadow-2xl pop border border-sky-100 text-center relative">
    <button onclick="closeQrModal()" class="absolute top-4 right-4 h-8 w-8 rounded-full bg-slate-100 hover:bg-slate-200 text-slate-500 flex items-center justify-center transition">&times;</button>
    <div class="h-12 w-12 rounded-2xl bg-sky-100 text-sky-600 flex items-center justify-center mx-auto mb-3">
      <svg class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>
    </div>
    <h3 class="text-xl font-bold text-slate-800" id="qrModalTitle">สแกนเข้าแชทด้วยมือถือ</h3>
    <p class="text-xs text-slate-500 mt-1 mb-4">เปิดกล้องมือถือในวง Tailscale หรือ Wi-Fi เดียวกันแล้วสแกน</p>
    
    <div class="bg-sky-50 p-4 rounded-2xl inline-block border border-sky-100 mb-4 shadow-inner">
      <div id="qrcodeCanvas" class="flex justify-center"></div>
    </div>

    <div class="space-y-2 text-left">
      <label class="text-xs font-semibold text-slate-500">ที่อยู่ URL สำหรับเชื่อมต่อ:</label>
      <div class="flex items-center gap-1.5 bg-slate-50 p-2 rounded-xl border border-slate-200">
        <input id="qrUrlInput" readonly class="text-xs text-sky-700 font-mono bg-transparent w-full outline-none select-all">
        <button onclick="copyQrUrl()" id="copyQrBtn" class="shrink-0 px-2.5 py-1 text-xs font-semibold bg-sky-600 hover:bg-sky-700 text-white rounded-lg transition">คัดลอก</button>
      </div>
    </div>
  </div>
</div>

<script>
function showQrModal(url, title) {
  const modal = document.getElementById('qrModal');
  const canvasBox = document.getElementById('qrcodeCanvas');
  const urlInput = document.getElementById('qrUrlInput');
  if(title) document.getElementById('qrModalTitle').textContent = title;
  
  canvasBox.innerHTML = '';
  urlInput.value = url;
  
  new QRCode(canvasBox, {
    text: url,
    width: 180,
    height: 180,
    colorDark : "#0369a1",
    colorLight : "#ffffff",
    correctLevel : QRCode.CorrectLevel.M
  });
  
  modal.classList.remove('hidden');
  modal.classList.add('flex');
}

function closeQrModal() {
  const modal = document.getElementById('qrModal');
  modal.classList.add('hidden');
  modal.classList.remove('flex');
}

async function copyQrUrl() {
  const urlInput = document.getElementById('qrUrlInput');
  const btn = document.getElementById('copyQrBtn');
  try {
    await navigator.clipboard.writeText(urlInput.value);
    btn.textContent = 'คัดลอกแล้ว!';
    btn.classList.replace('bg-sky-600', 'bg-emerald-600');
    setTimeout(() => {
      btn.textContent = 'คัดลอก';
      btn.classList.replace('bg-emerald-600', 'bg-sky-600');
    }, 1500);
  } catch(e) {
    urlInput.select();
    document.execCommand('copy');
  }
}
</script>
</body>
</html>"""


# ───────────────────────────── Login Page ─────────────────────────────
LOGIN = r"""
<div class="min-h-screen flex items-center justify-center p-4 bg-gradient-to-br from-sky-500 via-blue-600 to-cyan-600 relative overflow-hidden">
  <!-- Decorative background circles -->
  <div class="absolute -top-24 -left-24 w-96 h-96 bg-white/10 rounded-full blur-3xl pointer-events-none"></div>
  <div class="absolute -bottom-24 -right-24 w-96 h-96 bg-cyan-300/20 rounded-full blur-3xl pointer-events-none"></div>

  <div class="w-full max-w-md bg-white/95 backdrop-blur-xl rounded-3xl shadow-2xl p-8 border border-white/40 pop relative z-10">
    <div class="flex flex-col items-center text-center mb-6">
      <div class="h-16 w-16 rounded-2xl bg-gradient-to-br from-sky-400 to-blue-600 flex items-center justify-center shadow-lg shadow-sky-500/30 mb-4 text-white">
        <svg class="h-8 w-8" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/>
        </svg>
      </div>
      <h1 class="text-2xl sm:text-3xl font-extrabold text-slate-800 tracking-tight">Chat Network</h1>
      <p class="text-sky-700 font-semibold text-sm mt-1">ระบบแชท Tailscale VPN / LAN & มือถือ</p>
    </div>

    <!-- Network Info Card for Tailscale / LAN connect -->
    <div class="bg-sky-50 border border-sky-200/80 rounded-2xl p-3.5 mb-6 text-xs text-slate-700 space-y-1.5">
      <div class="flex items-center justify-between font-semibold text-sky-800">
        <span class="flex items-center gap-1.5">
          <span class="h-2 w-2 rounded-full bg-emerald-500 animate-pulse"></span>
          ที่อยู่ IP สำหรับเชื่อมต่อ
        </span>
        <button type="button" onclick="showQrModal('{{ current_url }}', 'สแกนเข้าสู่ระบบผ่านมือถือ')" class="text-sky-600 hover:text-sky-800 font-bold underline flex items-center gap-1">
          📱 QR มือถือ
        </button>
      </div>
      {% for item in net_ips %}
      <div class="flex justify-between items-center font-mono py-0.5 border-b border-sky-100 last:border-0 text-[11px]">
        <span class="font-medium {{ 'text-sky-700 font-bold' if 'Tailscale' in item.type else 'text-slate-500' }}">[{{ item.type }}]</span>
        <span class="text-sky-900 font-bold">http://{{ item.ip }}:{{ port }}</span>
      </div>
      {% endfor %}
    </div>

    <form method="post" action="{{ url_for('login') }}" class="space-y-4">
      <div>
        <label class="block text-sm font-semibold text-slate-700 mb-1.5">ชื่อหรือฉายาที่ใช้ในแชท</label>
        <div class="relative">
          <input name="name" required maxlength="{{ max_name }}" autofocus placeholder="เช่น ต้นคิด, ก้องภพ"
            class="w-full rounded-2xl border border-slate-200 bg-slate-50/50 px-4 py-3.5 pl-11 text-slate-800 focus:bg-white focus:outline-none focus:ring-4 focus:ring-sky-100 focus:border-sky-500 transition font-medium">
          <div class="absolute inset-y-0 left-0 pl-3.5 flex items-center pointer-events-none text-slate-400">
            <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>
          </div>
        </div>
      </div>
      <button class="w-full rounded-2xl bg-gradient-to-r from-sky-500 to-blue-600 hover:from-sky-600 hover:to-blue-700 text-white font-bold py-3.5 shadow-lg shadow-sky-500/30 hover:shadow-xl active:scale-[.99] transition duration-150 flex items-center justify-center gap-2">
        <span>เริ่มเข้าใช้งาน</span>
        <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M14 5l7 7m0 0l-7 7m7-7H3"/></svg>
      </button>
    </form>

    <div class="mt-6 pt-4 border-t border-slate-100 text-center text-xs text-slate-400">
      รองรับมือถือและคอมพิวเตอร์ผ่าน Tailscale VPN และ Wi-Fi / LAN
    </div>
  </div>
</div>
"""


# ───────────────────────────── Lobby Page ─────────────────────────────
LOBBY = r"""
<div class="min-h-screen bg-gradient-to-b from-sky-100/70 via-blue-50/30 to-slate-100">
  <!-- Top Navigation Header -->
  <header class="bg-white/90 backdrop-blur border-b border-sky-100 sticky top-0 z-30 shadow-sm">
    <div class="max-w-6xl mx-auto flex items-center justify-between px-4 py-3.5 sm:py-4">
      <div class="flex items-center gap-3">
        <div class="h-10 w-10 rounded-xl bg-gradient-to-br from-sky-400 to-blue-600 flex items-center justify-center shadow-md shadow-sky-500/20 text-white">
          <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>
        </div>
        <div>
          <span class="text-xl font-bold bg-gradient-to-r from-sky-600 to-blue-700 bg-clip-text text-transparent">Chat Network</span>
          <span class="hidden sm:inline-block ml-2 text-xs font-semibold px-2 py-0.5 rounded-md bg-sky-100 text-sky-800">Tailscale & LAN</span>
        </div>
      </div>

      <div class="flex items-center gap-2 sm:gap-3">
        <!-- Quick QR share button -->
        <button onclick="showQrModal('{{ current_url }}', 'สแกน QR เข้าระบบด้วยมือถือ')"
          class="flex items-center gap-1.5 px-3 py-1.5 rounded-xl bg-sky-50 hover:bg-sky-100 text-sky-700 border border-sky-200 text-xs font-semibold transition" title="เปิด QR Code สำหรับมือถือ">
          <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>
          <span class="hidden sm:inline">QR มือถือ</span>
        </button>

        <div class="flex items-center gap-2 bg-slate-50 rounded-full pl-1.5 pr-3.5 py-1 border border-slate-200 shadow-sm">
          <span class="avatar h-7 w-7 rounded-full flex items-center justify-center text-white text-xs font-bold" data-name="{{ me }}"></span>
          <span class="text-sm font-semibold text-slate-700">{{ me }}</span>
        </div>
        <a href="{{ url_for('logout') }}" class="p-2 text-slate-400 hover:text-rose-600 transition" title="ออกจากระบบ">
          <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"/></svg>
        </a>
      </div>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 py-6 sm:py-8 space-y-8">
    <!-- Welcome & Server Address Bar -->
    <div class="bg-gradient-to-r from-sky-600 via-blue-600 to-cyan-600 rounded-3xl p-6 sm:p-8 text-white shadow-xl glow-blue relative overflow-hidden">
      <div class="relative z-10 flex flex-col md:flex-row md:items-center justify-between gap-6">
        <div>
          <div class="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-white/20 backdrop-blur-md text-xs font-semibold mb-2">
            <span class="h-2 w-2 rounded-full bg-emerald-400 animate-ping"></span>
            สถานะเซิร์ฟเวอร์: ออนไลน์
          </div>
          <h2 class="text-2xl sm:text-3xl font-extrabold tracking-tight">ยินดีต้อนรับ, {{ me }} 👋</h2>
          <p class="text-sky-100 text-sm mt-1 max-w-xl">
            สร้างห้องใหม่แบบเปิดเสรี หรือตั้งรหัสผ่านป้องกัน / กรอกรหัสห้อง 4 หลัก หรือกดเข้าร่วมห้องสาธารณะได้ทันที
          </p>
        </div>

        <div class="bg-white/10 backdrop-blur-md border border-white/20 rounded-2xl p-4 sm:p-5 shrink-0 space-y-2 text-xs">
          <div class="font-bold flex items-center justify-between text-sky-100">
            <span>🌐 ที่อยู่ IP สำหรับเชื่อมต่อ (Tailscale / LAN)</span>
          </div>
          <div class="space-y-1 font-mono text-[11px] sm:text-xs">
            {% for item in net_ips %}
            <div class="flex items-center justify-between gap-3 bg-black/20 px-2.5 py-1 rounded-lg">
              <span class="text-sky-200">[{{ item.type }}]</span>
              <span class="font-bold text-white tracking-wide">http://{{ item.ip }}:{{ port }}</span>
            </div>
            {% endfor %}
          </div>
        </div>
      </div>
    </div>

    <!-- Actions Grid: Join vs Create -->
    <div class="grid md:grid-cols-2 gap-6">
      
      <!-- Join Box -->
      <div class="glass-card rounded-3xl p-6 sm:p-7 shadow-sm border border-sky-100 flex flex-col justify-between hover:shadow-md transition">
        <div>
          <div class="flex items-center gap-3 mb-4">
            <div class="h-11 w-11 rounded-2xl bg-sky-100 text-sky-600 flex items-center justify-center">
              <svg class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M11 16l-4-4m0 0l4-4m-4 4h14m-5 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h7a3 3 0 013 3v1"/></svg>
            </div>
            <div>
              <h3 class="font-bold text-lg text-slate-800">เข้าห้องด้วยรหัส 4 หลัก</h3>
              <p class="text-xs text-slate-500">กรอกรหัสตัวเลข 4 หลักเพื่อเข้าสู่ห้องสนทนา</p>
            </div>
          </div>

          <form id="joinForm" method="post" action="{{ url_for('join') }}" class="mt-4">
            <input type="hidden" name="code" id="codeHidden">
            <div class="flex gap-2.5 sm:gap-3 justify-center mb-5">
              {% for i in range(4) %}
              <input class="digit w-12 h-14 sm:w-16 sm:h-18 text-center text-2xl sm:text-3xl font-black font-mono rounded-2xl border-2 border-sky-100 bg-white focus:border-sky-500 focus:ring-4 focus:ring-sky-100 focus:outline-none transition shadow-sm"
                inputmode="numeric" maxlength="1" autocomplete="off" {% if i==0 %}autofocus{% endif %}>
              {% endfor %}
            </div>
            <button class="w-full rounded-2xl bg-sky-600 hover:bg-sky-700 text-white font-bold py-3.5 shadow-md shadow-sky-600/20 active:scale-[.99] transition flex items-center justify-center gap-2">
              <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M13 9l3 3m0 0l-3 3m3-3H8m13 0a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
              <span>เข้าห้องสนทนา</span>
            </button>
          </form>
        </div>
      </div>

      <!-- Create Box -->
      <div class="glass-card rounded-3xl p-6 sm:p-7 shadow-sm border border-sky-100 flex flex-col justify-between hover:shadow-md transition">
        <div>
          <div class="flex items-center gap-3 mb-4">
            <div class="h-11 w-11 rounded-2xl bg-blue-100 text-blue-600 flex items-center justify-center">
              <svg class="h-6 w-6" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4"/></svg>
            </div>
            <div>
              <h3 class="font-bold text-lg text-slate-800">สร้างห้องแชทใหม่</h3>
              <p class="text-xs text-slate-500">กำหนดได้ว่าต้องใส่รหัสผ่าน หรือไม่ต้องใส่รหัส</p>
            </div>
          </div>

          <form method="post" action="{{ url_for('create') }}" class="space-y-3.5">
            <div>
              <label class="block text-xs font-semibold text-slate-600 mb-1">ชื่อห้อง</label>
              <input name="room_name" maxlength="{{ max_room }}" placeholder="เช่น สนทนาทั่วไป"
                class="w-full rounded-2xl border border-sky-100 bg-white px-4 py-3 text-slate-800 focus:outline-none focus:ring-4 focus:ring-sky-100 focus:border-sky-500 transition">
            </div>

            <!-- Password Option Toggle -->
            <div class="bg-sky-50/70 p-3.5 rounded-2xl border border-sky-100 space-y-2">
              <div class="flex items-center justify-between">
                <span class="text-xs font-bold text-slate-700 flex items-center gap-1.5">
                  <svg class="h-4 w-4 text-sky-600" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
                  การป้องกันด้วยรหัสผ่าน
                </span>
                <label class="relative inline-flex items-center cursor-pointer">
                  <input type="checkbox" id="passToggle" name="is_private" value="1" class="sr-only peer" onchange="togglePasswordInput(this.checked)">
                  <div class="w-11 h-6 bg-slate-200 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-sky-600"></div>
                </label>
              </div>

              <div id="passField" class="hidden pt-2">
                <input type="password" id="roomPassInput" name="password" maxlength="{{ max_pass }}" placeholder="ตั้งรหัสผ่านเข้าห้อง (ตัวเลขหรือข้อความ)"
                  class="w-full rounded-xl border border-sky-200 bg-white px-3.5 py-2.5 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-sky-400">
                <p class="text-[11px] text-slate-500 mt-1">ผู้ที่ต้องการเข้าห้องนี้จะต้องกรอกรหัสผ่านนี้</p>
              </div>
              <p id="passHint" class="text-[11px] text-sky-700">✓ ปัจจุบัน: <b>ไม่ต้องใส่รหัสห้อง</b> (ทุกคนในวง Tailscale/LAN เข้าได้ทันที)</p>
            </div>

            <button class="w-full rounded-2xl bg-gradient-to-r from-sky-500 to-blue-600 hover:from-sky-600 hover:to-blue-700 text-white font-bold py-3.5 shadow-md shadow-sky-500/20 active:scale-[.99] transition">
              + สร้างห้องแชท
            </button>
          </form>
        </div>
      </div>
    </div>

    <!-- Active Rooms in Network -->
    <section>
      <div class="flex items-center justify-between mb-4">
        <div>
          <h3 class="text-lg font-bold text-slate-800">ห้องทั้งหมดในเครือข่าย</h3>
          <p class="text-xs text-slate-500">เลือกกดเข้าร่วมได้ทันที หรือดูห้องที่คุณเคยเข้าใช้งาน</p>
        </div>
        <span class="text-xs font-semibold px-2.5 py-1 rounded-full bg-sky-100 text-sky-700">{{ all_rooms|length }} ห้อง</span>
      </div>

      {% if all_rooms %}
      <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {% for r in all_rooms %}
        <div class="bg-white rounded-2xl p-4 border border-sky-100 shadow-sm hover:shadow-md hover:border-sky-300 transition flex flex-col justify-between group">
          <div>
            <div class="flex items-center justify-between mb-2">
              <span class="font-mono text-xs font-bold tracking-wider text-sky-700 bg-sky-50 px-2.5 py-1 rounded-lg border border-sky-100">
                # {{ r.code }}
              </span>
              <div class="flex items-center gap-1.5">
                {% if r.is_private %}
                <span class="text-[11px] font-medium bg-amber-50 text-amber-700 border border-amber-200 px-2 py-0.5 rounded-full flex items-center gap-1">
                  <svg class="h-3 w-3" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
                  ต้องใส่รหัส
                </span>
                {% else %}
                <span class="text-[11px] font-medium bg-emerald-50 text-emerald-700 border border-emerald-200 px-2 py-0.5 rounded-full flex items-center gap-1">
                  <span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>
                  ห้องสาธารณะ
                </span>
                {% endif %}
              </div>
            </div>

            <h4 class="font-bold text-slate-800 text-base truncate group-hover:text-sky-600 transition">{{ r.name }}</h4>
            <p class="text-xs text-slate-400 mt-0.5">สร้างโดย {{ r.created_by }}</p>
          </div>

          <div class="mt-4 pt-3 border-t border-slate-100 flex items-center justify-between">
            <span class="text-xs text-slate-500 flex items-center gap-1">
              <span class="h-2 w-2 rounded-full {{ 'bg-emerald-500' if r.online > 0 else 'bg-slate-300' }}"></span>
              <b>{{ r.online }}</b> ออนไลน์
            </span>

            <a href="{{ url_for('room', code=r.code) }}"
              class="px-3.5 py-1.5 rounded-xl bg-sky-50 group-hover:bg-sky-600 group-hover:text-white text-sky-700 text-xs font-bold transition flex items-center gap-1">
              <span>เข้าห้อง</span>
              <svg class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/></svg>
            </a>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="bg-white rounded-3xl p-8 text-center border border-sky-100 text-slate-400">
        <svg class="h-12 w-12 mx-auto text-sky-200 mb-2" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>
        <p class="font-medium text-slate-600">ยังไม่มีห้องแชทในระบบ</p>
        <p class="text-xs text-slate-400 mt-1">เริ่มต้นด้วยการสร้างห้องแรกของคุณด้านบนได้เลย!</p>
      </div>
      {% endif %}
    </section>
  </main>

  <footer class="max-w-6xl mx-auto px-4 py-8 text-center text-xs text-slate-400 border-t border-sky-100">
    Chat Network · รองรับการทำงานผ่าน Tailscale VPN, Wi-Fi / LAN และสมาร์ทโฟน
  </footer>
</div>

<script>
%%AVATAR_JS%%
document.querySelectorAll('.avatar').forEach(paintAvatar);

function togglePasswordInput(checked) {
  const field = document.getElementById('passField');
  const hint = document.getElementById('passHint');
  const input = document.getElementById('roomPassInput');
  if(checked) {
    field.classList.remove('hidden');
    hint.innerHTML = '🔒 ปัจจุบัน: <b>ต้องใส่รหัสห้อง</b> ก่อนเข้าร่วม';
    hint.classList.replace('text-sky-700', 'text-amber-700');
    input.focus();
  } else {
    field.classList.add('hidden');
    hint.innerHTML = '✓ ปัจจุบัน: <b>ไม่ต้องใส่รหัสห้อง</b> (ทุกคนในวง Tailscale/LAN เข้าได้ทันที)';
    hint.classList.replace('text-amber-700', 'text-sky-700');
    input.value = '';
  }
}

// 4-Digit PIN Input Controller
const digits = [...document.querySelectorAll('.digit')];
const form = document.getElementById('joinForm');
digits.forEach((d, i) => {
  d.addEventListener('input', e => {
    d.value = d.value.replace(/\D/g, '').slice(-1);
    if(d.value && i < 3) digits[i+1].focus();
    if(digits.every(x => x.value)) submitJoin();
  });
  d.addEventListener('keydown', e => {
    if(e.key === 'Backspace' && !d.value && i > 0) {
      digits[i-1].focus();
      digits[i-1].value = '';
    }
    if(e.key === 'Enter') {
      e.preventDefault();
      submitJoin();
    }
  });
  d.addEventListener('paste', e => {
    const t = (e.clipboardData.getData('text') || '').replace(/\D/g, '').slice(0, 4);
    if(t) {
      e.preventDefault();
      t.split('').forEach((c, k) => digits[k].value = c);
      (digits[t.length] || digits[3]).focus();
      if(t.length === 4) submitJoin();
    }
  });
});

function submitJoin() {
  const code = digits.map(x => x.value).join('');
  if(code.length !== 4) {
    digits.find(x => !x.value)?.focus();
    return;
  }
  document.getElementById('codeHidden').value = code;
  form.submit();
}
form.addEventListener('submit', e => {
  e.preventDefault();
  submitJoin();
});
</script>
"""


# ───────────────────────────── Password Prompt Page ─────────────────────────────
PASSWORD_PROMPT = r"""
<div class="min-h-screen flex items-center justify-center p-4 bg-gradient-to-br from-sky-500 via-blue-600 to-cyan-700">
  <div class="w-full max-w-sm bg-white/95 backdrop-blur-xl rounded-3xl shadow-2xl p-8 border border-white/40 pop text-center">
    <div class="h-16 w-16 rounded-2xl bg-amber-100 text-amber-600 flex items-center justify-center mx-auto mb-4 shadow-inner">
      <svg class="h-8 w-8" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
    </div>
    
    <span class="font-mono text-xs font-bold text-sky-700 bg-sky-50 px-2.5 py-1 rounded-lg border border-sky-100">
      ห้อง #{{ room.code }}
    </span>
    <h2 class="text-xl font-bold text-slate-800 mt-2">{{ room.name }}</h2>
    <p class="text-xs text-slate-500 mt-1 mb-6">ห้องนี้ถูกตั้งรหัสผ่านไว้ กรุณากรอกรหัสผ่านเพื่อเข้าห้อง</p>

    <form method="post" action="{{ url_for('verify_room_password', code=room.code) }}" class="space-y-4">
      <div>
        <input type="password" name="password" required autofocus placeholder="กรอกรหัสผ่านห้อง"
          class="w-full rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3.5 text-center text-base font-semibold text-slate-800 focus:bg-white focus:outline-none focus:ring-4 focus:ring-sky-100 focus:border-sky-500 transition">
      </div>
      <button class="w-full rounded-2xl bg-gradient-to-r from-sky-500 to-blue-600 hover:from-sky-600 hover:to-blue-700 text-white font-bold py-3.5 shadow-lg shadow-sky-500/25 active:scale-[.99] transition">
        ปลดล็อกและเข้าห้อง
      </button>
      <a href="{{ url_for('index') }}" class="block text-xs font-semibold text-slate-400 hover:text-slate-600 pt-2 transition">
        ← กลับหน้าล็อบบี้
      </a>
    </form>
  </div>
</div>
"""


# ───────────────────────────── Room Chat View ─────────────────────────────
ROOM = r"""
<div class="h-[100dvh] flex flex-col bg-slate-100 overflow-hidden select-text">
  
  <!-- Header Bar -->
  <header class="bg-white/95 backdrop-blur border-b border-sky-100 px-3 sm:px-5 py-3 flex items-center gap-2.5 sm:gap-3 shadow-sm z-20 shrink-0">
    <a href="{{ url_for('index') }}" class="h-10 w-10 rounded-xl hover:bg-sky-50 text-slate-500 hover:text-sky-600 flex items-center justify-center transition shrink-0" title="กลับหน้าหลัก">
      <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M15 19l-7-7 7-7"/></svg>
    </a>

    <!-- Room Icon -->
    <div class="h-10 w-10 rounded-xl bg-gradient-to-br from-sky-400 to-blue-600 text-white flex items-center justify-center font-black shadow-md shadow-sky-500/20 shrink-0 text-base">
      {{ room.name[:1] }}
    </div>

    <!-- Room Title & Online Status -->
    <div class="min-w-0 flex-1">
      <div class="flex items-center gap-1.5">
        <h1 class="font-bold text-slate-800 text-sm sm:text-base truncate leading-tight">{{ room.name }}</h1>
        {% if room.is_private %}
        <span class="text-amber-500 shrink-0" title="ห้องมีรหัสผ่าน">
          <svg class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
        </span>
        {% endif %}
      </div>
      <p class="text-xs text-slate-500 flex items-center gap-1.5 mt-0.5">
        <span class="h-2 w-2 rounded-full bg-emerald-500 animate-pulse"></span>
        <span id="onlineCount" class="font-semibold text-emerald-600">–</span> คนออนไลน์
        <span class="text-slate-300">·</span>
        <span class="text-slate-400 truncate hidden sm:inline">ห้องสร้างโดย {{ room.created_by }}</span>
      </p>
    </div>

    <!-- Header Actions -->
    <div class="flex items-center gap-1.5 sm:gap-2 shrink-0">
      <!-- Sound toggle -->
      <button id="soundToggle" onclick="toggleSound()" class="h-9 w-9 rounded-xl hover:bg-slate-100 text-slate-500 flex items-center justify-center transition" title="เปิด/ปิดเสียงแจ้งเตือน">
        <svg id="soundOnIcon" class="h-4.5 w-4.5 text-sky-600" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z"/></svg>
        <svg id="soundOffIcon" class="h-4.5 w-4.5 text-slate-400 hidden" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z"/><path stroke-linecap="round" stroke-linejoin="round" d="M17 14l2-2m0 0l2-2m-2 2l-2-2m2 2l2 2"/></svg>
      </button>

      <!-- QR Code Mobile Share -->
      <button onclick="showQrModal('{{ room_url }}', 'สแกน QR เข้าร่วมห้องนี้ด้วยมือถือ')"
        class="flex items-center gap-1.5 rounded-xl bg-sky-50 hover:bg-sky-100 text-sky-700 border border-sky-200 px-2.5 sm:px-3 py-1.5 text-xs font-bold transition" title="เปิด QR Code สำหรับมือถือเข้าห้องนี้">
        <svg class="h-4 w-4 text-sky-600" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>
        <span class="hidden md:inline">แชร์ QR</span>
      </button>

      <!-- Copy Room Code -->
      <button id="copyBtn" class="flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-sky-500 to-blue-600 hover:from-sky-600 hover:to-blue-700 text-white px-3 py-1.5 shadow-sm shadow-sky-500/30 transition text-xs font-bold" title="คัดลอกรหัสห้อง">
        <span class="hidden sm:inline">รหัส</span>
        <span class="font-mono tracking-widest">{{ room.code }}</span>
        <svg class="h-3.5 w-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
      </button>

      <!-- Mobile Members Drawer Button -->
      <button id="membersBtn" class="lg:hidden h-9 w-9 rounded-xl hover:bg-slate-100 flex items-center justify-center text-slate-600 transition" title="ดูสมาชิก">
        <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M17 20h5v-2a3 3 0 00-5.4-1.8M17 20H7m10 0v-2c0-.7-.1-1.3-.4-1.8M7 20H2v-2a3 3 0 015.4-1.8M7 20v-2c0-.7.1-1.3.4-1.8m0 0a5 5 0 019.2 0M15 7a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
      </button>
    </div>
  </header>

  <!-- Main Chat Body -->
  <div class="flex-1 flex min-h-0 relative">
    
    <!-- Messages Scroll Area -->
    <div class="flex-1 flex flex-col min-w-0 bg-gradient-to-b from-sky-50/40 via-white to-slate-50">
      <div id="messages" class="flex-1 overflow-y-auto scroll-thin px-3 sm:px-6 py-4 space-y-1">
        <div id="loading" class="text-center text-slate-400 text-xs py-8">กำลังเชื่อมต่อห้องสนทนา…</div>
      </div>

      <!-- Image Preview Bar if selected -->
      <div id="imgPreviewBar" class="hidden px-4 py-2 bg-sky-50/90 border-t border-sky-100 flex items-center justify-between">
        <div class="flex items-center gap-3">
          <img id="previewImg" class="h-14 w-14 object-cover rounded-xl border border-sky-200 shadow-sm">
          <div class="text-xs">
            <p class="font-bold text-slate-700">แนบรูปภาพพร้อมส่ง</p>
            <p id="previewSize" class="text-slate-400 text-[11px]"></p>
          </div>
        </div>
        <button type="button" onclick="cancelImage()" class="h-7 w-7 rounded-full bg-slate-200 hover:bg-slate-300 text-slate-600 flex items-center justify-center text-sm">&times;</button>
      </div>

      <!-- Chat Composer Form -->
      <form id="composer" class="bg-white/95 backdrop-blur border-t border-sky-100 p-2.5 sm:p-4 shrink-0">
        <div class="flex items-end gap-2 max-w-5xl mx-auto">
          
          <!-- Image Attachment Button -->
          <input type="file" id="fileInput" accept="image/*" class="hidden" onchange="handleFileSelect(event)">
          <button type="button" onclick="document.getElementById('fileInput').click()"
            class="h-11 w-11 shrink-0 rounded-2xl bg-sky-50 hover:bg-sky-100 text-sky-600 flex items-center justify-center transition border border-sky-200" title="ส่งรูปภาพ หรือ ถ่ายภาพ">
            <svg class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
          </button>

          <!-- Text input -->
          <textarea id="input" rows="1" maxlength="{{ max_msg }}" placeholder="พิมพ์ข้อความ... (กด Enter เพื่อส่ง, วางรูปภาพได้ทันที)"
            class="flex-1 resize-none max-h-36 rounded-2xl border border-slate-200 bg-slate-50/70 px-4 py-2.5 sm:py-3 text-sm text-slate-800 focus:bg-white focus:outline-none focus:ring-4 focus:ring-sky-100 focus:border-sky-500 transition"></textarea>

          <!-- Send Button -->
          <button id="sendBtn" type="submit"
            class="h-11 w-11 shrink-0 rounded-2xl bg-gradient-to-br from-sky-500 to-blue-600 hover:from-sky-600 hover:to-blue-700 text-white flex items-center justify-center shadow-md shadow-sky-500/30 hover:brightness-105 active:scale-95 disabled:opacity-40 transition">
            <svg class="h-5 w-5 rotate-90" fill="none" stroke="currentColor" stroke-width="2.2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 19V5m0 0l-7 7m7-7l7 7"/></svg>
          </button>
        </div>
      </form>
    </div>

    <!-- Members Sidebar -->
    <aside id="members" class="hidden lg:flex w-72 flex-col bg-white border-l border-sky-100 fixed lg:static inset-y-0 right-0 z-30 shadow-2xl lg:shadow-none">
      <div class="px-5 py-4 border-b border-sky-100 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <h2 class="font-bold text-slate-800 text-sm">สมาชิกที่ออนไลน์</h2>
          <span id="sideOnlineBadge" class="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded-full font-bold">0</span>
        </div>
        <button id="closeMembers" class="lg:hidden text-slate-400 hover:text-slate-700 text-xl font-bold leading-none p-1">&times;</button>
      </div>

      <ul id="memberList" class="flex-1 overflow-y-auto scroll-thin p-3 space-y-1.5"></ul>

      <div class="p-4 border-t border-sky-100 bg-slate-50/50 space-y-3 text-xs">
        <div class="bg-white p-3 rounded-2xl border border-sky-100 shadow-sm space-y-1.5">
          <div class="flex items-center justify-between text-slate-600 font-semibold">
            <span>รหัสห้องสำหรับชวนเพื่อน:</span>
            <span class="font-mono font-bold text-sky-700 bg-sky-50 px-2 py-0.5 rounded border border-sky-100">#{{ room.code }}</span>
          </div>
          <button onclick="showQrModal('{{ room_url }}', 'สแกน QR เข้าร่วมห้อง #{{ room.code }}')"
            class="w-full mt-2 py-2 rounded-xl bg-sky-50 hover:bg-sky-100 text-sky-700 font-bold border border-sky-200 transition text-center flex items-center justify-center gap-1.5">
            <svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>
            <span>แสดง QR มือถือ</span>
          </button>
        </div>

        {% if room.created_by == me %}
        <form method="post" action="{{ url_for('delete_room', code=room.code) }}" onsubmit="return confirm('คุณต้องการลบห้องนี้และข้อความทั้งหมดใช่หรือไม่?')">
          <button class="w-full py-2 text-rose-600 hover:bg-rose-50 rounded-xl font-medium transition text-center">
            🗑️ ปิดและลบห้องนี้
          </button>
        </form>
        {% endif %}
      </div>
    </aside>
  </div>
</div>

<!-- Modal Full Image View -->
<div id="imageModal" class="fixed inset-0 z-50 bg-slate-900/90 backdrop-blur-sm hidden items-center justify-center p-4" onclick="this.classList.add('hidden');this.classList.remove('flex')">
  <img id="modalImg" class="max-w-[95vw] max-h-[90vh] object-contain rounded-2xl shadow-2xl pop">
</div>

<script>
%%AVATAR_JS%%
const CODE = {{ room.code|tojson }};
const ME = {{ me|tojson }};
const API = "{{ url_for('api_messages', code=room.code) }}";
let lastId = 0, lastAuthor = null, lastTime = 0, lastDay = null, polling = false;
let soundEnabled = true;
let pendingImageBase64 = null;

// Web Audio API notification sound generator
const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
function playChime() {
  if(!soundEnabled) return;
  try {
    if(audioCtx.state === 'suspended') audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(587.33, audioCtx.currentTime); // D5
    osc.frequency.setValueAtTime(880.00, audioCtx.currentTime + 0.08); // A5
    gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.35);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.35);
  } catch(e){}
}

function toggleSound() {
  soundEnabled = !soundEnabled;
  document.getElementById('soundOnIcon').classList.toggle('hidden', !soundEnabled);
  document.getElementById('soundOffIcon').classList.toggle('hidden', soundEnabled);
}

const box = document.getElementById('messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('sendBtn');

function fmtTime(t){return new Date(t*1000).toLocaleTimeString('th-TH',{hour:'2-digit',minute:'2-digit'});}
function fmtDay(t){
  const d = new Date(t*1000), today = new Date(), y = new Date();
  y.setDate(today.getDate() - 1);
  if(d.toDateString() === today.toDateString()) return 'วันนี้';
  if(d.toDateString() === y.toDateString()) return 'เมื่อวาน';
  return d.toLocaleDateString('th-TH', {day:'numeric', month:'short', year:'numeric'});
}
function el(tag, cls, text){
  const e = document.createElement(tag);
  if(cls) e.className = cls;
  if(text != null) e.textContent = text;
  return e;
}
function nearBottom(){ return box.scrollHeight - box.scrollTop - box.clientHeight < 120; }

function viewFullImage(src) {
  const modal = document.getElementById('imageModal');
  document.getElementById('modalImg').src = src;
  modal.classList.remove('hidden');
  modal.classList.add('flex');
}

function addMessage(m, isHistory) {
  const day = fmtDay(m.created_at);
  if(day !== lastDay){
    const d = el('div', 'flex items-center gap-3 my-4 text-xs text-sky-700/60 font-medium');
    d.append(el('div', 'flex-1 h-px bg-sky-200/60'), el('span', 'px-3 py-0.5 rounded-full bg-sky-100/60', day), el('div', 'flex-1 h-px bg-sky-200/60'));
    box.append(d);
    lastDay = day;
    lastAuthor = null;
  }

  // System Message
  if(m.author === null){
    const s = el('div', 'flex justify-center my-3 pop');
    s.append(el('span', 'text-xs bg-sky-100/80 text-sky-800 px-3.5 py-1.5 rounded-full border border-sky-200 font-medium shadow-sm', m.body));
    box.append(s);
    lastAuthor = null;
    return;
  }

  const mine = m.author === ME;
  const grouped = m.author === lastAuthor && (m.created_at - lastTime) < 180;
  const row = el('div', `flex gap-2.5 pop ${mine ? 'justify-end' : ''} ${grouped ? 'mt-1' : 'mt-4'}`);

  if(!mine){
    const av = el('div', 'avatar h-9 w-9 shrink-0 rounded-full flex items-center justify-center text-white text-xs font-bold shadow-sm');
    av.dataset.name = m.author;
    paintAvatar(av);
    if(grouped) av.style.visibility = 'hidden';
    row.append(av);
  }

  const col = el('div', `flex flex-col max-w-[85%] sm:max-w-[70%] ${mine ? 'items-end' : 'items-start'}`);
  if(!grouped && !mine) {
    col.append(el('span', 'text-xs font-bold text-slate-600 mb-1 ml-1', m.author));
  }

  // Message Bubble
  const bubble = el('div', `px-4 py-2.5 rounded-2xl whitespace-pre-wrap break-words leading-relaxed shadow-sm text-sm ${
      mine ? 'bg-gradient-to-r from-sky-500 to-blue-600 text-white rounded-br-sm shadow-sky-500/10'
           : 'bg-white text-slate-800 rounded-bl-sm border border-slate-200/80 shadow-slate-100'}`);

  // Image Attachment in message
  if(m.msg_type === 'image' && m.file_data) {
    const imgWrap = el('div', 'mb-2 cursor-pointer');
    const img = el('img', 'rounded-xl max-h-64 object-cover hover:opacity-95 transition border border-black/10');
    img.src = m.file_data;
    img.onclick = () => viewFullImage(m.file_data);
    imgWrap.append(img);
    bubble.append(imgWrap);
  }

  if(m.body) {
    bubble.append(document.createTextNode(m.body));
  }

  bubble.title = fmtTime(m.created_at);
  const wrap = el('div', `flex items-end gap-1.5 ${mine ? 'flex-row-reverse' : ''}`);
  wrap.append(bubble, el('span', 'text-[10px] text-slate-400 mb-0.5 shrink-0 select-none', fmtTime(m.created_at)));
  col.append(wrap);
  row.append(col);
  box.append(row);

  lastAuthor = m.author;
  lastTime = m.created_at;

  if(!isHistory && !mine) {
    playChime();
  }
}

function renderMembers(list){
  document.getElementById('onlineCount').textContent = list.length;
  document.getElementById('sideOnlineBadge').textContent = list.length;
  const ul = document.getElementById('memberList');
  ul.innerHTML = '';
  list.forEach(n => {
    const li = el('li', 'flex items-center gap-3 px-3 py-2 rounded-xl hover:bg-sky-50 transition');
    const av = el('div', 'avatar relative h-8 w-8 rounded-full flex items-center justify-center text-white text-xs font-bold shrink-0');
    av.dataset.name = n;
    paintAvatar(av);
    av.append(el('span', 'absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full bg-emerald-500 ring-2 ring-white'));
    li.append(av, el('span', 'text-xs font-semibold text-slate-700 truncate', n + (n === ME ? ' (คุณ)' : '')));
    ul.append(li);
  });
}

async function poll(){
  if(polling) return;
  polling = true;
  try {
    const r = await fetch(`${API}?after=${lastId}`);
    if(r.ok){
      const data = await r.json();
      document.getElementById('loading')?.remove();
      const stick = nearBottom() || lastId === 0;
      const isInitial = lastId === 0;
      data.messages.forEach(m => {
        addMessage(m, isInitial);
        lastId = Math.max(lastId, m.id);
      });
      if(data.messages.length && stick) {
        box.scrollTop = box.scrollHeight;
      }
      renderMembers(data.online);
      if(lastId === 0 && !box.children.length) {
        box.innerHTML = '<div id="loading" class="text-center text-slate-400 text-xs py-10">ยังไม่มีข้อความ เริ่มต้นคุยกันได้เลย! ✨</div>';
      }
    } else if(r.status === 401 || r.status === 403){
      location.href = "{{ url_for('room', code=room.code) }}";
    }
  } catch(e){}
  polling = false;
}

// Client-side image compress & handling
function compressImage(file, callback) {
  const reader = new FileReader();
  reader.onload = e => {
    const img = new Image();
    img.onload = () => {
      const maxDim = 1280;
      let w = img.width, h = img.height;
      if(w > maxDim || h > maxDim) {
        if(w > h) { h = Math.round(h * maxDim / w); w = maxDim; }
        else { w = Math.round(w * maxDim / h); h = maxDim; }
      }
      const canvas = document.createElement('canvas');
      canvas.width = w;
      canvas.height = h;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0, w, h);
      const dataUrl = canvas.toDataURL('image/jpeg', 0.82);
      callback(dataUrl);
    };
    img.src = e.target.result;
  };
  reader.readAsDataURL(file);
}

function handleFileSelect(e) {
  const file = e.target.files?.[0];
  if(!file) return;
  compressImage(file, dataUrl => {
    pendingImageBase64 = dataUrl;
    document.getElementById('previewImg').src = dataUrl;
    document.getElementById('previewSize').textContent = `${(file.size/1024).toFixed(1)} KB`;
    document.getElementById('imgPreviewBar').classList.remove('hidden');
    input.focus();
  });
}

function cancelImage() {
  pendingImageBase64 = null;
  document.getElementById('fileInput').value = '';
  document.getElementById('imgPreviewBar').classList.add('hidden');
}

// Paste image from clipboard
document.addEventListener('paste', e => {
  const items = e.clipboardData?.items;
  if(!items) return;
  for(let i = 0; i < items.length; i++) {
    if(items[i].type.indexOf('image') !== -1) {
      const file = items[i].getAsFile();
      compressImage(file, dataUrl => {
        pendingImageBase64 = dataUrl;
        document.getElementById('previewImg').src = dataUrl;
        document.getElementById('previewSize').textContent = 'รูปภาพจากคลิปบอร์ด';
        document.getElementById('imgPreviewBar').classList.remove('hidden');
      });
      break;
    }
  }
});

async function send(){
  const body = input.value.trim();
  const fileData = pendingImageBase64;
  if(!body && !fileData) return;

  sendBtn.disabled = true;
  try {
    const payload = {
      body: body,
      msg_type: fileData ? 'image' : 'text',
      file_data: fileData
    };
    const r = await fetch(API, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    if(r.ok){
      input.value = '';
      cancelImage();
      autosize();
      await poll();
      box.scrollTop = box.scrollHeight;
    }
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

function autosize(){
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 140) + 'px';
}
input.addEventListener('input', autosize);
input.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){
    e.preventDefault();
    send();
  }
});
document.getElementById('composer').addEventListener('submit', e => {
  e.preventDefault();
  send();
});

document.getElementById('copyBtn').addEventListener('click', async e => {
  const btn = e.currentTarget;
  try {
    await navigator.clipboard.writeText(CODE);
  } catch(_) {
    const t = document.createElement('textarea');
    t.value = CODE;
    document.body.append(t);
    t.select();
    document.execCommand('copy');
    t.remove();
  }
  btn.classList.add('ring-4', 'ring-sky-200');
  setTimeout(() => btn.classList.remove('ring-4', 'ring-sky-200'), 900);
});

const members = document.getElementById('members');
document.getElementById('membersBtn').addEventListener('click', () => {
  members.classList.remove('hidden');
  members.classList.add('flex');
});
document.getElementById('closeMembers').addEventListener('click', () => {
  members.classList.add('hidden');
  members.classList.remove('flex');
});

poll();
setInterval(poll, 1500);
input.focus();
</script>
"""

AVATAR_JS = r"""
function paintAvatar(el){
  const n = el.dataset.name || '?';
  const colors = ['#0284c7', '#0ea5e9', '#0369a1', '#2563eb', '#3b82f6', '#0891b2', '#06b6d4', '#4f46e5', '#38bdf8', '#1d4ed8'];
  let h = 0; for(const c of n) h = (h*31 + c.codePointAt(0)) >>> 0;
  el.style.background = colors[h % colors.length];
  if(!el.firstChild || el.firstChild.nodeType !== 3) el.prepend(document.createTextNode([...n.trim()][0]?.toUpperCase() || '?'));
}
"""


def page(body, **ctx):
    tpl = BASE.replace("%%BODY%%", body.replace("%%AVATAR_JS%%", AVATAR_JS))
    return render_template_string(tpl, **ctx)


# ───────────────────────────── Routes ─────────────────────────────
@app.route("/")
def index():
    port = int(os.environ.get("PORT", 5000))
    net_ips = get_local_ips()
    current_url = request.host_url.rstrip("/")

    me = session.get("name")
    if not me:
        return page(LOGIN, title="เข้าสู่ระบบ · Chat Network", max_name=MAX_NAME,
                    net_ips=net_ips, port=port, current_url=current_url)

    db = get_db()
    now = time.time()
    
    # ดึงห้องทั้งหมดในระบบพร้อมจำนวนคนออนไลน์
    raw_rooms = db.execute("SELECT * FROM rooms ORDER BY created_at DESC").fetchall()
    all_rooms = []
    for r in raw_rooms:
        online = db.execute("SELECT COUNT(*) FROM presence WHERE room_code=? AND last_seen>?",
                            (r["code"], now - ONLINE_WINDOW)).fetchone()[0]
        all_rooms.append({
            "code": r["code"],
            "name": r["name"],
            "created_by": r["created_by"],
            "is_private": bool(r["is_private"]),
            "online": online,
            "created_at": r["created_at"]
        })

    return page(LOBBY, title="Chat Network — หน้าล็อบบี้", me=me,
                all_rooms=all_rooms, net_ips=net_ips, port=port,
                current_url=current_url, max_room=MAX_ROOM_NAME, max_pass=MAX_PASSWORD)


@app.post("/login")
def login():
    name = re.sub(r"\s+", " ", request.form.get("name", "")).strip()[:MAX_NAME]
    if not name:
        flash("กรุณากรอกชื่อสำหรับเข้าใช้งาน", "error")
    else:
        session["name"] = name
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.pop("name", None)
    session.pop("unlocked_rooms", None)
    return redirect(url_for("index"))


@app.post("/create")
def create():
    me = session.get("name")
    if not me:
        return redirect(url_for("index"))
    
    room_name = request.form.get("room_name", "").strip()[:MAX_ROOM_NAME] or f"ห้องของ {me}"
    is_private = 1 if request.form.get("is_private") == "1" else 0
    password = request.form.get("password", "").strip()[:MAX_PASSWORD] if is_private else ""

    db = get_db()
    code = new_room_code()
    if code is None:
        flash("ห้องเต็มแล้ว (ครบ 9,000 ห้อง)", "error")
        return redirect(url_for("index"))

    now = time.time()
    db.execute("""
        INSERT INTO rooms (code, name, created_by, password, is_private, created_at)
        VALUES (?,?,?,?,?,?)
    """, (code, room_name, me, password, is_private, now))

    status_txt = "🔒 ห้องส่วนตัว (ต้องใส่รหัสผ่าน)" if is_private else "🌐 ห้องสาธารณะ"
    db.execute("""
        INSERT INTO messages (room_code, author, body, msg_type, created_at)
        VALUES (?,?,?,?,?)
    """, (code, None, f"{me} สร้างห้อง “{room_name}” [{status_txt}] · รหัสห้อง {code}", "text", now))
    db.commit()

    # ปลดล็อกห้องให้ผู้สร้างทันที
    unlock_room(code)
    flash(f"สร้างห้องสำเร็จ! รหัสห้องคือ {code}", "ok")
    return redirect(url_for("room", code=code))


@app.post("/join")
def join():
    if not session.get("name"):
        return redirect(url_for("index"))
    code = request.form.get("code", "").strip()
    if not re.fullmatch(r"\d{4}", code):
        flash("รหัสห้องต้องเป็นตัวเลข 4 หลัก", "error")
        return redirect(url_for("index"))
    r = get_room(code)
    if not r:
        flash(f"ไม่พบห้องรหัส {code}", "error")
        return redirect(url_for("index"))

    return redirect(url_for("room", code=code))


@app.route("/room/<code>")
def room(code):
    me = session.get("name")
    if not me:
        return redirect(url_for("index"))
    r = get_room(code)
    if not r:
        flash(f"ไม่พบห้องรหัส {code}", "error")
        return redirect(url_for("index"))

    # ตรวจสอบสิทธิ์เข้าห้อง
    if not is_room_unlocked(code, r):
        return page(PASSWORD_PROMPT, title=f"ใส่รหัสผ่านห้อง · #{code}", room=r)

    remember_room(code)
    room_url = f"{request.host_url.rstrip('/')}/room/{code}"
    return page(ROOM, title=f"{r['name']} · #{code}", me=me, room=r,
                room_url=room_url, max_msg=MAX_MSG)


@app.post("/room/<code>/verify")
def verify_room_password(code):
    me = session.get("name")
    if not me:
        return redirect(url_for("index"))
    r = get_room(code)
    if not r:
        flash(f"ไม่พบห้องรหัส {code}", "error")
        return redirect(url_for("index"))

    input_pass = request.form.get("password", "").strip()
    if input_pass == r["password"]:
        unlock_room(code)
        flash("รหัสผ่านถูกต้อง เข้าห้องสำเร็จ", "ok")
        return redirect(url_for("room", code=code))
    else:
        flash("รหัสผ่านห้องไม่ถูกต้อง กรุณาลองใหม่อีกครั้ง", "error")
        return page(PASSWORD_PROMPT, title=f"ใส่รหัสผ่านห้อง · #{code}", room=r)


@app.post("/room/<code>/delete")
def delete_room(code):
    me = session.get("name")
    if not me:
        return redirect(url_for("index"))
    r = get_room(code)
    if not r:
        flash(f"ไม่พบห้องรหัส {code}", "error")
        return redirect(url_for("index"))

    if r["created_by"] != me:
        flash("เฉพาะผู้สร้างห้องเท่านั้นที่สามารถลบห้องได้", "error")
        return redirect(url_for("room", code=code))

    db = get_db()
    db.execute("DELETE FROM messages WHERE room_code=?", (code,))
    db.execute("DELETE FROM presence WHERE room_code=?", (code,))
    db.execute("DELETE FROM rooms WHERE code=?", (code,))
    db.commit()

    flash(f"ลบห้อง “{r['name']}” เรียบร้อยแล้ว", "ok")
    return redirect(url_for("index"))


@app.route("/api/room/<code>/messages", methods=["GET", "POST"])
def api_messages(code):
    me = session.get("name")
    if not me:
        return jsonify(error="unauthorized"), 401
    r = get_room(code)
    if not r:
        abort(404)

    # ตรวจสอบว่าปลดล็อกห้องหรือยัง
    if not is_room_unlocked(code, r):
        return jsonify(error="forbidden_password_required"), 403

    db = get_db()
    now = time.time()

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        body = str(payload.get("body", "")).strip()[:MAX_MSG]
        msg_type = payload.get("msg_type", "text")
        file_data = payload.get("file_data") if msg_type == "image" else None

        if not body and not file_data:
            return jsonify(error="empty"), 400

        db.execute("""
            INSERT INTO messages (room_code, author, body, msg_type, file_data, created_at)
            VALUES (?,?,?,?,?,?)
        """, (code, me, body, msg_type, file_data, now))
        db.commit()
        return jsonify(ok=True)

    # GET: ดึงข้อความใหม่ + อัปเดตสถานะออนไลน์
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0

    db.execute("""
        INSERT INTO presence (room_code, name, last_seen) VALUES (?,?,?)
        ON CONFLICT(room_code, name) DO UPDATE SET last_seen=excluded.last_seen
    """, (code, me, now))
    db.commit()

    if after == 0:
        rows = db.execute("""
            SELECT * FROM (
                SELECT * FROM messages WHERE room_code=? ORDER BY id DESC LIMIT 200
            ) ORDER BY id
        """, (code,)).fetchall()
    else:
        rows = db.execute("""
            SELECT * FROM messages WHERE room_code=? AND id>? ORDER BY id LIMIT 500
        """, (code, after)).fetchall()

    online = [row["name"] for row in db.execute("""
        SELECT name FROM presence WHERE room_code=? AND last_seen>? ORDER BY name
    """, (code, now - ONLINE_WINDOW))]

    return jsonify(
        messages=[{
            "id": row["id"],
            "author": row["author"],
            "body": row["body"],
            "msg_type": row["msg_type"] if "msg_type" in row.keys() else "text",
            "file_data": row["file_data"] if "file_data" in row.keys() else None,
            "created_at": row["created_at"]
        } for row in rows],
        online=online,
    )


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "="*60)
    print(" 🚀 Chat Network กำลังทำงานบนเซิร์ฟเวอร์...")
    print(f" 🌐 สำหรับเครื่องนี้: http://127.0.0.1:{port}")
    ips = get_local_ips()
    for item in ips:
        print(f" 📱 สำหรับมือถือ/เครื่องอื่น [{item['type']}]: http://{item['ip']}:{port}")
    print("="*60 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
