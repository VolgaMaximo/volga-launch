import os
import re
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, Response, redirect, send_file

# ---------------------------
# Config
# ---------------------------
APP_TITLE = os.getenv("APP_TITLE", "VOLGA Lunch")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
APP_VERSION = os.getenv("APP_VERSION", "1")
DB_PATH = os.getenv("DB_PATH", "/tmp/orders.sqlite")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Madrid"))

MAX_PER_DAY = int(os.getenv("MAX_PER_DAY", "30"))
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", "12"))  # 12:00
ORDER_PREFIX = os.getenv("ORDER_PREFIX", "VO")

# ✅ Один офис — ALAMEDA
OFFICE = "ALAMEDA"
OFFICES = ["ALAMEDA"]

FLOORS_BY_OFFICE = {
    "ALAMEDA": ["1st floor", "6th floor"]
}

# --- Меню: RU / EN ---
MENU = {
    "zakuska": [
        "Оливье / Olivier salad",
        "Винегрет / Vinigret salad",
        "Икра из баклажанов / Eggplant caviar",
        "Паштет из куриной печени / Chicken liver pâté",
        "Шуба / Herring under a fur coat",
    ],
    # ✅ Супы теперь управляются из БД (admin_soups),
    # этот список — только fallback если в БД пусто
    "soup": [
        "Борщ / Borscht",
        "Солянка сборная мясная / Meat soup solyanka",
        "Куриный суп с лапшой и яйцом / Chicken soup with noodles & egg",
    ],
    "hot": [
        "Куриные котлеты с пюре / Chicken cutlets with mashed potatoes",
        "Куриные котлеты с гречкой / Chicken cutlets with buckwheat",
        "Вареники с картошкой / Vareniki with potatoes",
        "Пельмени со сметаной / Pelmeni with sour cream",
        "Плов с бараниной / Lamb plov (+3€)",
    ],
    "dessert": [
        "Торт Наполеон / Napoleon cake",
        "Пирожное Картошка / Kartoshka cake",
        "Трубочка со сгущенкой / Wafer roll with dulce de leche",
    ],
}

PRICES = {"opt1": 15.0, "opt2": 16.0, "opt3": 17.0}
PLOV_SURCHARGE = 3.0

BREAD_OPTIONS = ["Белый / White", "Чёрный / Black"]

# --- Напитки ---
DRINKS = [
    ("", "— без напитка / no drink —", 0.0),
    ("kvas", "Квас / Kvas €3.5", 3.5),
    ("mors", "Морс / Berry drink (Mors) €4.0", 4.0),
    ("water", "Вода / Water €2.2", 2.2),
    ("tea_black", "Чай чёрный с чабрецом (сашет) / Black tea with thyme (sachet) €3.5", 3.5),
    ("tea_green", "Чай зелёный (сашет) / Green tea (sachet) €3.5", 3.5),
    ("tea_herbal", "Чай травяной (сашет) / Herbal tea (sachet) €3.5", 3.5),
]
DRINK_PRICE = {k: p for (k, _, p) in DRINKS}
DRINK_LABEL = {k: lbl for (k, lbl, _) in DRINKS}

app = Flask(__name__)


# ---------------------------
# DB
# ---------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_columns(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "drink_code" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_code TEXT")
    if "drink_label" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_label TEXT")
    if "drink_price_eur" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_price_eur REAL")
    if "floor" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN floor TEXT")


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT NOT NULL UNIQUE,
            office TEXT NOT NULL,
            order_date TEXT NOT NULL,

            floor TEXT,

            name TEXT NOT NULL,
            phone_raw TEXT NOT NULL,
            phone_norm TEXT NOT NULL,

            zakuska TEXT,
            soup TEXT NOT NULL,
            hot TEXT,
            dessert TEXT,

            bread TEXT,

            option_code TEXT NOT NULL,
            price_eur REAL NOT NULL,
            comment TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_office_date ON orders(office, order_date)")

    # ✅ НЕТ уникального индекса по телефону — разрешаем два заказа с одного номера

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_special (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            office TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            title TEXT NOT NULL,
            surcharge_eur INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_special_office_dates ON weekly_special(office, start_date, end_date)")

    # ✅ Таблица для управления супами через админку
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_soups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_ru TEXT NOT NULL,
            title_en TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )

    ensure_columns(conn)
    conn.commit()
    conn.close()


init_db()


# ---------------------------
# Helpers
# ---------------------------
def now_local():
    return datetime.now(TZ)


def cutoff_dt(d: date) -> datetime:
    return datetime.combine(d, time(CUTOFF_HOUR, 0), TZ)


def is_workday(d: date) -> bool:
    return d.weekday() in (1, 2, 3, 4)  # Tue–Fri


def next_workday(d: date) -> date:
    x = d + timedelta(days=1)
    while not is_workday(x):
        x += timedelta(days=1)
    return x


def prev_workday(d: date) -> date:
    x = d - timedelta(days=1)
    while not is_workday(x):
        x -= timedelta(days=1)
    return x


def ordering_window_for(d: date):
    start = cutoff_dt(prev_workday(d))
    end = cutoff_dt(d)
    return start, end


def is_closed_day(d: date) -> bool:
    return not is_workday(d)


def validate_order_time(d: date):
    n = now_local()
    start, end = ordering_window_for(d)
    if is_closed_day(d):
        return False, start, end, n
    return (start <= n < end), start, end, n


def normalize_phone(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    has_plus = raw.lstrip().startswith("+")
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return ""
    return ("+" if has_plus else "") + digits


def compute_default_date():
    n = now_local()
    today = n.date()
    if is_workday(today) and n < cutoff_dt(today):
        return today
    return next_workday(today)


def check_admin():
    return request.args.get("token", "") == ADMIN_TOKEN


def options_html(items):
    return "".join([f"<option>{x}</option>" for x in items])


def get_weekly_special(d: date):
    conn = db()
    row = conn.execute(
        """
        SELECT * FROM weekly_special
        WHERE office=? AND start_date <= ? AND end_date >= ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (OFFICE, d.isoformat(), d.isoformat()),
    ).fetchone()
    conn.close()
    return row


# ✅ Получить активные супы из БД (или fallback из кода)
def get_soups_list() -> list[str]:
    # Всегда начинаем с трёх базовых супов
    result = list(MENU["soup"])
    # Добавляем супы из админки (активные)
    conn = db()
    rows = conn.execute(
        "SELECT title_ru, title_en FROM admin_soups WHERE active=1 ORDER BY sort_order ASC, id ASC"
    ).fetchall()
    conn.close()
    for r in rows:
        en = (r["title_en"] or "").strip()
        ru = (r["title_ru"] or "").strip()
        if ru:
            result.append(f"{ru} / {en}" if en else ru)
    return result


def hot_menu_with_special(d: date):
    items = MENU["hot"].copy()
    special = get_weekly_special(d)
    if special:
        label = f"Блюдо недели: {special['title']} / Weekly special: {special['title']}"
        s = int(special["surcharge_eur"])
        if s > 0:
            label += f" (+{s}€)"
        items.insert(0, label)
    return items


def compute_option_base_price(zakuska, soup, hot, dessert, d: date):
    has_z = bool(zakuska)
    has_s = bool(soup)
    has_h = bool(hot)
    has_d = bool(dessert)

    if not has_s:
        return None, None, "Суп обязателен / Soup is required."

    if has_z and has_s and has_d and not has_h:
        option = "opt1"
        price = PRICES[option]
    elif (not has_z) and has_s and has_h and has_d:
        option = "opt2"
        price = PRICES[option]
    elif has_z and has_s and has_h and (not has_d):
        option = "opt3"
        price = PRICES[option]
    else:
        return None, None, "Нужно выбрать ровно 3 категории по правилам опций / Please select exactly 3 categories per options."

    if hot and "Плов с бараниной" in hot:
        price += PLOV_SURCHARGE

    if hot and hot.startswith("Блюдо недели:"):
        special = get_weekly_special(d)
        if special:
            price += float(int(special["surcharge_eur"]))

    return option, float(price), None


def compute_total_price(base_price: float, drink_code: str) -> float:
    add = float(DRINK_PRICE.get((drink_code or "").strip(), 0.0))
    return round(float(base_price) + add, 2)


def generate_order_code(conn: sqlite3.Connection, d: date) -> str:
    ymd = d.strftime("%Y%m%d")
    like_prefix = f"{ORDER_PREFIX}-{ymd}-"
    row = conn.execute(
        """
        SELECT order_code FROM orders
        WHERE office=? AND order_date=? AND order_code LIKE ?
        ORDER BY order_code DESC
        LIMIT 1
        """,
        (OFFICE, d.isoformat(), like_prefix + "%"),
    ).fetchone()

    if not row:
        seq = 1
    else:
        last = row["order_code"].split("-")[-1]
        try:
            seq = int(last) + 1
        except ValueError:
            seq = 1

    return f"{ORDER_PREFIX}-{ymd}-{seq:03d}"


def file_path(name: str) -> str:
    return os.path.join(os.path.dirname(__file__), name)


def validate_floor_for_office(floor: str | None) -> tuple[bool, str | None]:
    allowed = set(FLOORS_BY_OFFICE.get(OFFICE, []))
    if allowed:
        if floor not in allowed:
            return False, None
        return True, floor
    return True, None


# ---------------------------
# PWA minimal
# ---------------------------
@app.get("/manifest.webmanifest")
def manifest():
    import json
    data = {
        "name": APP_TITLE,
        "short_name": "VOLGA Lunch",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#EDE7D3",
        "theme_color": "#EDE7D3",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


@app.get("/icon.svg")
def icon_svg():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
<rect width="512" height="512" fill="#EDE7D3"/>
<rect x="64" y="64" width="384" height="384" fill="#EDE7D3" stroke="#0E238E" stroke-width="14"/>
<path d="M110 170 L402 110 L402 180 L110 240 Z" fill="#E73F24" opacity="0.95"/>
<path d="M110 330 L402 270 L402 340 L110 400 Z" fill="#0E238E" opacity="0.95"/>
<text x="256" y="290" font-family="Arial, sans-serif" font-size="64" text-anchor="middle" fill="#0E238E">VOLGA</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.get("/logo.png")
def logo_png():
    p = file_path("logo.png")
    if not os.path.exists(p):
        return Response("logo.png not found", status=404, mimetype="text/plain")
    return send_file(p)


@app.get("/banner.png")
def banner_png():
    p = file_path("banner.png")
    if not os.path.exists(p):
        return Response("banner.png not found", status=404, mimetype="text/plain")
    return send_file(p)


@app.get("/sw.js")
def sw_js():
    js = f"""
const CACHE = 'volga-lunch-{APP_VERSION}';
const ASSETS = ['/', '/edit', '/manifest.webmanifest', '/icon.svg', '/logo.png', '/banner.png'];

self.addEventListener('install', (e) => {{
  e.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(ASSETS))
  );
  self.skipWaiting();
}});

self.addEventListener('activate', (e) => {{
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => k !== CACHE).map(k => caches.delete(k))
      )
    )
  );
  self.clients.claim();
}});

self.addEventListener('fetch', (e) => {{
  const url = new URL(e.request.url);

  if (e.request.method === 'GET' && url.origin === self.location.origin) {{
    e.respondWith(
      fetch(e.request)
        .then(resp => {{
          const copy = resp.clone();
          caches.open(CACHE).then(cache => cache.put(e.request, copy));
          return resp;
        }})
        .catch(() => caches.match(e.request))
    );
  }}
}});
"""
    return Response(js, mimetype="application/javascript")


# ---------------------------
# HTML shell
# ---------------------------
def html_page(body: str) -> str:
    shell = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VOLGA Lunch</title>

<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#EDE7D3">

<style>
:root{
  --volga-blue:#0E238E;
  --volga-red:#E73F24;
  --volga-burgundy:#8E2C1F;
  --volga-bg:#EDE7D3;
}

*{ box-sizing:border-box; }

body{
  font-family:-apple-system, system-ui, Arial;
  margin:18px;
  background:var(--volga-bg);
  color:var(--volga-blue);
}

.card{
  background:transparent;
  border:2px solid var(--volga-blue);
  border-radius:0;
  padding:28px;
  margin:30px auto;
  max-width:900px;
  overflow:hidden;
}

h1{
  color:var(--volga-blue);
  font-weight:800;
  letter-spacing:1px;
  margin:0 0 14px 0;
  line-height:1.0;
}
h1 small{
  display:block;
  color:var(--volga-red);
  font-weight:800;
  line-height:1.00;
  margin-top:4px;
}

.hero-title{
  text-align:center;
  font-weight:800;
  font-size:28px;
  line-height:1.15;
  letter-spacing:1px;
  margin-bottom:14px;
}
.hero-title .ru{ color: var(--volga-blue); }
.hero-title .en{ color: var(--volga-red); }

label{
  display:block;
  margin:0 0 4px 0;
  font-weight:800;
  overflow-wrap:anywhere;
  color:var(--volga-red);
}

input, select, textarea{
  width:100%;
  max-width:520px;
  padding:12px;
  margin:0;
  font-size:16px;
  background:var(--volga-bg);
  color:var(--volga-blue);
  border:2px solid var(--volga-blue);
  border-radius:0;
}

input:focus, select:focus, textarea:focus{
  outline:none;
  border:2px solid var(--volga-blue);
}

.row{
  display:grid;
  grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  column-gap:18px;
  row-gap:10px;
  align-items:start;
  margin-top:10px;
}
.row > div{
  width:100%;
  max-width:520px;
}

.muted{ color:var(--volga-burgundy); }
.danger{ color:var(--volga-red); font-weight:800; }
small{
  display:block;
  margin:2px 0 0 0;
  line-height:1.1;
  color:var(--volga-burgundy);
}

a{ color:var(--volga-blue); text-decoration:none; font-weight:700; }
a:hover{ color:var(--volga-red); }

.pill{
  display:inline-block;
  padding:6px 10px;
  border-radius:999px;
  border:1px solid var(--volga-blue);
  margin-right:8px;
  color:var(--volga-blue);
}

.lead{
  color:var(--volga-blue);
  text-align:center;
  font-weight:900;
  margin:12px 0 0 0;
}
.lead .en{ color:var(--volga-red); font-weight:800; }

.hours{
  margin:14px 0 0 0;
  text-align:center;
  font-weight:900;
}
.hours .ru{ color:var(--volga-blue); }
.hours .en{ color:var(--volga-red); }

.btn-confirm{
  display:block;
  width:100%;
  max-width:520px;
  padding:16px 24px;
  font-size:16px;
  font-weight:800;
  background:var(--volga-blue);
  color:var(--volga-bg);
  border:none;
  border-radius:0;
  cursor:pointer;
  transition:0.2s ease;
}
.btn-confirm:active{ background:var(--volga-red); }

.btn-edit{
  display:flex;
  text-align:center;
  align-items:center;
  justify-content:center;
  width:100%;
  max-width:520px;
  padding:16px 24px;
  font-size:16px;
  font-weight:800;
  background:var(--volga-red);
  color:var(--volga-bg);
  border:none;
  border-radius:0;
  cursor:pointer;
  transition:0.2s ease;
  margin-top:18px;
}
.btn-edit:active{ background:var(--volga-blue); }

.comment-block{ margin-top:18px; }

.banner-block{
  margin-top:18px;
  margin-bottom:18px;
}

@media (max-width: 700px){
  .card{ padding:20px; }
  .row{ grid-template-columns:1fr; column-gap:0; row-gap:10px; margin-top:10px; }
  .row > div{ max-width:none; }
  input, select, textarea{ max-width:100%; }
  h1{ letter-spacing:0.5px; }

  input[type="date"]{
    -webkit-appearance: none;
    appearance: none;
  }
  #order_date{
    width: 100%;
    max-width: 100%;
    min-width: 0;
    display: block;
    margin-left: 2px;
    margin-right: 2px;
  }
}

.btn-primary{
  display:block;
  width:100%;
  margin-top:20px;
  padding:14px 24px;
  font-size:16px;
  font-weight:700;
  border:2px solid var(--volga-blue);
  background:var(--volga-blue);
  color:var(--volga-bg);
  border-radius:0;
  text-align:center;
}
.btn-primary:hover{ background:var(--volga-red); border-color:var(--volga-red); }
.btn-primary:active{ background:var(--volga-red); border-color:var(--volga-red); }

.btn-danger{
  display:block;
  width:100%;
  margin-top:14px;
  padding:14px 24px;
  font-size:16px;
  font-weight:700;
  border:2px solid var(--volga-red);
  background:var(--volga-red);
  color:var(--volga-bg);
  border-radius:0;
  text-align:center;
}
.btn-danger:hover{ background:var(--volga-blue); border-color:var(--volga-blue); }
.btn-danger:active{ background:var(--volga-blue); border-color:var(--volga-blue); }

.admin-table{
  width:100%;
  border-collapse:collapse;
  margin-top:10px;
  font-size:14px;
}
.admin-table th,
.admin-table td{
  border:2px solid var(--volga-blue);
  padding:8px 10px;
  vertical-align:top;
}
.admin-table th{
  background:var(--volga-bg);
  color:var(--volga-blue);
  text-align:left;
  font-weight:800;
}
.admin-table td small{ color:var(--volga-burgundy); }
.admin-table tbody tr:hover{
  outline:2px solid var(--volga-red);
  outline-offset:-2px;
}
@media (max-width: 700px){
  .admin-table{ font-size:13px; }
  .admin-table th.created,
  .admin-table td.created{ display:none; }
}
</style>
</head>
<body>
__BODY__

<script>
(function(){
  // anti-double-submit
  document.querySelectorAll('form').forEach((f) => {
    f.addEventListener('submit', () => {
      const btns = f.querySelectorAll('button[type="submit"]');
      btns.forEach(b => {
        b.disabled = true;
        b.textContent = 'Отправка… / Sending…';
      });
    });
  });

  // service worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(()=>{});
  }
})();
</script>

<style>
#volgaPopupOverlay{
  position:fixed;
  inset:0;
  background:rgba(0,0,0,0.35);
  display:none;
  align-items:center;
  justify-content:center;
  z-index:9999;
}

#volgaPopupBox{
  background:var(--volga-blue);
  color:var(--volga-bg);
  border:3px solid var(--volga-blue);
  padding:20px 24px;
  max-width:420px;
  width:90%;
  text-align:center;
  font-weight:800;
  line-height:1.4;
}

#volgaPopupBox button{
  margin-top:14px;
  padding:8px 18px;
  border:2px solid var(--volga-bg);
  background:var(--volga-red);
  color:var(--volga-bg);
  font-weight:800;
  cursor:pointer;
}
</style>

<div id="volgaPopupOverlay">
  <div id="volgaPopupBox">
    <div id="volgaPopupText"></div>
    <button type="button" onclick="hideVolgaPopup()">OK</button>
  </div>
</div>

<script>
function showVolgaPopup(text){
  const t = document.getElementById("volgaPopupText");
  const o = document.getElementById("volgaPopupOverlay");
  if (!t || !o) return;
  t.innerHTML = text;
  o.style.display = "flex";
}
function hideVolgaPopup(){
  const o = document.getElementById("volgaPopupOverlay");
  if (!o) return;
  o.style.display = "none";
}
document.addEventListener("click", (e)=>{
  const o = document.getElementById("volgaPopupOverlay");
  if (!o) return;
  if (e.target === o) hideVolgaPopup();
});
</script>
<script>
/* ====== FLOOR CHECK ====== */
(() => {
  const form = document.querySelector('form[action="/order"]');
  const floorEl = document.getElementById("floor");
  const floorCell = document.getElementById("floorCell");

  if (!form || !floorEl || !floorCell) return;
  if (typeof showVolgaPopup !== "function") return;

  form.addEventListener("submit", (e) => {
    if (!floorEl.value || floorEl.value.trim() === "") {
      e.preventDefault();
      e.stopImmediatePropagation();
      showVolgaPopup(
        "Пожалуйста, выберите этаж.<br><br>" +
        "Please choose a floor."
      );
      floorEl.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => floorEl.focus(), 150);
    }
  }, true);
})();
</script>
<script>
/* ====== DISH RULES CHECK ====== */
(() => {
  const form = document.querySelector('form[action="/order"]');
  if (!form) return;
  if (typeof showVolgaPopup !== "function") return;

  const z = document.getElementById("zakuska");
  const s = document.getElementById("soup");
  const h = document.getElementById("hot");
  const d = document.getElementById("dessert");

  if (!z || !s || !h || !d) return;

  function has(el){
    return !!(el.value && el.value.trim() !== "");
  }

  function focusEl(el){
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => el.focus(), 150);
  }

  form.addEventListener("submit", (e) => {
    const hasZ = has(z);
    const hasS = has(s);
    const hasH = has(h);
    const hasD = has(d);

    const count = (hasZ?1:0) + (hasS?1:0) + (hasH?1:0) + (hasD?1:0);

    if (!hasS){
      e.preventDefault();
      e.stopImmediatePropagation();
      showVolgaPopup(
        "Любая опция включает суп. Выберите пожалуйста суп.<br><br>" +
        "All options come with soup. Please choose a soup."
      );
      focusEl(s);
      return;
    }

    if (count !== 3){
      e.preventDefault();
      e.stopImmediatePropagation();
      showVolgaPopup(
        "Нужно выбрать 3 блюда. Любая опция включает суп.<br><br>" +
        "Please select 3 dishes. All options come with soup."
      );
      if (!hasZ) focusEl(z);
      else if (!hasH) focusEl(h);
      else if (!hasD) focusEl(d);
      return;
    }

    const isOpt1 = hasZ && hasS && hasD && !hasH;
    const isOpt2 = !hasZ && hasS && hasH && hasD;
    const isOpt3 = hasZ && hasS && hasH && !hasD;

    if (!(isOpt1 || isOpt2 || isOpt3)){
      e.preventDefault();
      e.stopImmediatePropagation();
      showVolgaPopup(
        "Комбинация выбрана неверно.<br>" +
        "Выберите одну из опций (3 блюда).<br><br>" +
        "Wrong combination. Please follow the options (3 dishes)."
      );
      focusEl(h);
      return;
    }
  }, true);
})();
</script>
<script>
/* ====== DISH LIMIT ====== */
(function () {
  const MAX_DISHES = 3;
  const dishIds = ["zakuska", "soup", "hot", "dessert"];

  const form = document.querySelector('form[action="/order"]') || document.querySelector("form");
  if (!form) return;

  const selects = dishIds
    .map(id => document.getElementById(id))
    .filter(Boolean);

  function countSelected() {
    let c = 0;
    for (const s of selects) {
      if (s.value && s.value.trim() !== "") c++;
    }
    return c;
  }

  for (const s of selects) {
    s.dataset.prev = s.value || "";

    s.addEventListener("focus", () => {
      s.dataset.prev = s.value || "";
    });

    s.addEventListener("change", () => {
      const c = countSelected();

      if (c > MAX_DISHES) {
        s.value = s.dataset.prev || "";
        showVolgaPopup(
          `Можно выбрать максимум ${MAX_DISHES} блюда. Любая опция включает суп. <br>` +
          `You can select maximum ${MAX_DISHES} dishes. All options come with soup.`
        );
      } else {
        s.dataset.prev = s.value || "";
      }
    });
  }

  form.addEventListener("submit", (e) => {
    const c = countSelected();
    if (c > MAX_DISHES) {
      e.preventDefault();
      showVolgaPopup(
        `ОШИБКА: ВЫБРАНО ${c} БЛЮДА. МАКСИМУМ — ${MAX_DISHES}.<br><br>` +
        `ERROR: ${c} DISHES SELECTED. MAXIMUM ALLOWED IS ${MAX_DISHES}.`
      );
    }
  });
})();
</script>

<script>
/* ====== DATE VALIDATION (Tue–Fri, 11:00 rule) ====== */
(() => {
  const dateInput = document.getElementById("order_date");
  if (!dateInput) return;

  const form = dateInput.closest("form");
  const CUT_OFF_HOUR = 12;

  function pad(n){ return String(n).padStart(2,"0"); }
  function ymd(d){ return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`; }

  function isAllowedDay(d){
    return d.getDay() >= 2 && d.getDay() <= 5;
  }

  function startOfDay(d){
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }

  function isAfterCutoff(now){
    const hh = now.getHours();
    const mm = now.getMinutes();
    return (hh > CUT_OFF_HOUR) || (hh === CUT_OFF_HOUR && mm > 0);
  }

  function nextAllowedFrom(day0){
    const x = new Date(day0.getFullYear(), day0.getMonth(), day0.getDate());
    x.setDate(x.getDate() + 1);
    while(!isAllowedDay(x)) x.setDate(x.getDate() + 1);
    return x;
  }

  function allowedDateYMD(){
    const now = new Date();
    const today = startOfDay(now);

    if (!isAfterCutoff(now) && isAllowedDay(today)) {
      return ymd(today);
    }
    return ymd(nextAllowedFrom(today));
  }

  function validateOrderDate(selectedYMD){
    if (!selectedYMD) return true;

    const now = new Date();
    const [Y,M,D] = selectedYMD.split("-").map(Number);
    const sel = new Date(Y, M-1, D);
    const today = startOfDay(now);

    if (startOfDay(sel) < today){
      showVolgaPopup("Вы выбрали прошедшую дату.<br><br>You can't choose a past date.");
      return false;
    }

    if (!isAllowedDay(sel)){
      showVolgaPopup("Заказ доступен вторник–пятница.<br><br>Order available Tuesday–Friday only.");
      return false;
    }

    const mustBe = allowedDateYMD();
    if (selectedYMD !== mustBe){
      showVolgaPopup(
        "Дата заказа выбрана неверно.<br>" +
        "До 12:00 можно заказать на сегодня.<br>" +
        "После 12:00 — только на следующий рабочий день.<br><br>" +
        "Wrong order date.<br>" +
        "Before 12:00 you can order for today.<br>" +
        "After 12:00 — only for the next working day."
      );
      return false;
    }

    return true;
  }

  function resetToAllowed(){
    dateInput.value = allowedDateYMD();
  }

  dateInput.addEventListener("change", () => {
    if(!validateOrderDate(dateInput.value)){
      resetToAllowed();
    }
  });

  if(form){
    form.addEventListener("submit", (e)=>{
      if(!validateOrderDate(dateInput.value)){
        e.preventDefault();
        resetToAllowed();
      }
    });
  }
})();
</script>


<style>
/* === BANNER 12:00 === */
#cutoffBannerOverlay{
  position:fixed;
  inset:0;
  background:rgba(0,0,0,0.5);
  display:flex;
  align-items:center;
  justify-content:center;
  z-index:99999;
}
#cutoffBannerBox{
  background:var(--volga-blue);
  color:var(--volga-bg);
  border:4px solid var(--volga-red);
  padding:28px 32px;
  max-width:460px;
  width:92%;
  text-align:center;
  line-height:1.5;
}
#cutoffBannerBox .banner-title{
  font-size:22px;
  font-weight:900;
  color:var(--volga-bg);
  margin-bottom:14px;
}
#cutoffBannerBox .banner-title span{
  color:#FFD700;
}
#cutoffBannerBox p{
  margin:6px 0;
  font-size:15px;
  font-weight:700;
}
#cutoffBannerBox p.en{
  color:rgba(255,255,255,0.75);
  font-size:14px;
  font-weight:600;
}
#cutoffBannerBox button{
  margin-top:20px;
  padding:12px 32px;
  border:2px solid var(--volga-bg);
  background:var(--volga-red);
  color:var(--volga-bg);
  font-weight:900;
  font-size:16px;
  cursor:pointer;
}
#cutoffBannerBox button:hover{
  background:var(--volga-bg);
  color:var(--volga-blue);
}
</style>

<div id="cutoffBannerOverlay">
  <div id="cutoffBannerBox">
    <div class="banner-title">\u231b \u0422\u0435\u043f\u0435\u0440\u044c \u0437\u0430\u043a\u0430\u0437 \u0434\u043e <span>12:00</span></div>
    <p>\u041c\u044b \u043f\u0440\u043e\u0434\u043b\u0438\u043b\u0438 \u0432\u0440\u0435\u043c\u044f \u043f\u0440\u0438\u0451\u043c\u0430 \u0437\u0430\u043a\u0430\u0437\u043e\u0432!</p>
    <p>\u0417\u0430\u043a\u0430\u0437\u044b\u0432\u0430\u0439\u0442\u0435 \u0431\u0438\u0437\u043d\u0435\u0441-\u043b\u0430\u043d\u0447 \u0434\u043e <b>12:00</b>.</p>
    <p class="en">We extended the order time!</p>
    <p class="en">Order your business lunch before <b>12:00</b>.</p>
    <button type="button" onclick="closeCutoffBanner()">\u041f\u043e\u043d\u044f\u0442\u043d\u043e / Got it</button>
  </div>
</div>

<script>
(function(){
  var BANNER_KEY = 'volga_cutoff12_seen';
  function closeCutoffBanner(){
    var el = document.getElementById('cutoffBannerOverlay');
    if(el) el.style.display = 'none';
    try{ localStorage.setItem(BANNER_KEY, '1'); }catch(e){}
  }
  window.closeCutoffBanner = closeCutoffBanner;
  try{
    if(localStorage.getItem(BANNER_KEY)){
      var el = document.getElementById('cutoffBannerOverlay');
      if(el) el.style.display = 'none';
    }
  }catch(e){}
})();
</script>

</html>"""
    return shell.replace("__BODY__", body)


# ---------------------------
# Routes
# ---------------------------
@app.get("/")
def form():
    default_date = compute_default_date()
    d_str = request.args.get("date", default_date.isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = default_date

    soups = get_soups_list()
    hot_items = hot_menu_with_special(d)
    ok_time, start, end, now_ = validate_order_time(d)

    conn = db()
    ensure_columns(conn)
    cnt = conn.execute(
        "SELECT COUNT(*) as c FROM orders WHERE office=? AND order_date=? AND status='active'",
        (OFFICE, d.isoformat()),
    ).fetchone()["c"]
    conn.close()

    limit_reached = cnt >= MAX_PER_DAY

    warn = ""
    if is_closed_day(d):
        warn += "<p class='danger'><b>В понедельник мы не работаем.</b><br><small>We are closed on Mondays.</small></p>"
    if not ok_time and not is_closed_day(d):
        warn += (
            f"<p class='danger'><b>Приём заказов на {d.isoformat()} закрыт.</b><br>"
            f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')} (Europe/Madrid). "
            f"Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
        )
    if limit_reached:
        warn += "<p class='danger'><b>На выбранную дату заказы временно недоступны.</b><br><small>Orders are temporarily unavailable for this date.</small></p>"

    drink_options = "".join([f"<option value='{k}'>{lbl}</option>" for (k, lbl, _) in DRINKS])

    body = f"""
<div style="text-align:center; margin-bottom:18px;">
  <img src="/logo.png" alt="VOLGA" style="max-height:120px;">
</div>

<h1 class="hero-title">
  <span class="ru">БИЗНЕС-ЛАНЧ RingCentral</span><br>
  <span class="en">BUSINESS LUNCH RingCentral</span>
</h1>

<p class="lead">
  Доставка в 13:00. Заказ до 12:00.<br>
  <span class="en">Delivery at 13:00. Order before 12:00.</span>
</p>

<p class="hours">
  <span class="ru">Вторник — Пятница</span><br>
  <span class="en">Tuesday — Friday</span>
</p>

{warn}

<div class="card">
  <form method="post" action="/order" autocomplete="on">

    <div class="row">
      <div>
        <label>Этаж / Floor</label>
        <select id="floor" name="floor" required>
          <option value="">— выбери этаж / choose floor —</option>
          <option value="1st floor">1 этаж / 1st floor</option>
          <option value="6th floor">6 этаж / 6th floor</option>
        </select>
      </div>
      <div>
        <label>Дата доставки / Delivery date</label>
        <input id="order_date" type="date" name="order_date" value="{d.isoformat()}" required>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Как вас зовут / Your name</label>
        <input name="name" required>
      </div>
      <div>
        <label>Телефон / Phone</label>
        <input name="phone" required>
        <small>для связи и поиска заказа / for contact &amp; order lookup</small>
      </div>
    </div>

    <div class="banner-block">
      <img src="/banner.png" alt="Options" style="width:100%; display:block; border:2px solid var(--volga-blue);">
    </div>

    <div class="row">
      <div>
        <label>Закуска / Starter</label>
        <select id="zakuska" name="zakuska">
          <option value="">— без закуски / no starter —</option>
          {options_html(MENU["zakuska"])}
        </select>
      </div>
      <div>
        <label>Суп / Soup</label>
        <select id="soup" name="soup" required>
          <option value="">— выбери суп / choose soup —</option>
          {options_html(soups)}
        </select>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Горячее / Main</label>
        <select id="hot" name="hot">
          <option value="">— без горячего / no main —</option>
          {options_html(hot_items)}
        </select>
      </div>
      <div>
        <label>Десерт / Dessert</label>
        <select id="dessert" name="dessert">
          <option value="">— без десерта / no dessert —</option>
          {options_html(MENU["dessert"])}
        </select>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Напиток / Drink</label>
        <select id="drink" name="drink">{drink_options}</select>
        <small>оплачивается отдельно / not included</small>
      </div>
      <div>
        <label>Хлеб / Bread</label>
        <select id="bread" name="bread">
          <option value="">— без хлеба / no bread —</option>
          {options_html(BREAD_OPTIONS)}
        </select>
      </div>
    </div>

    <div class="comment-block">
      <label>Комментарий / Notes</label>
      <textarea name="comment" rows="3" placeholder=""></textarea>
    </div>

    <button type="submit" class="btn-confirm" style="margin-top:22px;">
      Подтвердить заказ / Confirm order
    </button>

    <a href="/edit" class="btn-edit">
      Изменить или отменить заказ / Edit or cancel
    </a>
  </form>
</div>
"""
    return html_page(body)


@app.post("/order")
def order():
    d_str = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата / Invalid date.</p><p><a href='/'>Назад / Back</a></p>"), 400

    floor = (request.form.get("floor", "") or "").strip() or None
    ok_floor, floor = validate_floor_for_office(floor)
    if not ok_floor:
        return html_page("<p class='danger'>Выберите этаж / Please choose floor.</p><p><a href='/'>Назад / Back</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        if is_closed_day(d):
            return html_page("<p class='danger'><b>В понедельник мы не работаем.</b><br><small>We are closed on Mondays.</small></p><p><a href='/'>Назад / Back</a></p>"), 403
        return html_page(
            f"<p class='danger'><b>Приём заказов открыт на сегодня до 12:00. На завтра после 12:00.</b><br>"
            f"<small>Доступно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
            f"<p><a href='/'>Назад / Back</a></p>"
        ), 403

    name = (request.form.get("name", "") or "").strip()
    phone_raw = (request.form.get("phone", "") or "").strip()
    phone_norm = normalize_phone(phone_raw)

    zakuska = (request.form.get("zakuska", "") or "").strip() or None
    soup = (request.form.get("soup", "") or "").strip()
    hot = (request.form.get("hot", "") or "").strip() or None
    dessert = (request.form.get("dessert", "") or "").strip() or None

    drink_code = (request.form.get("drink", "") or "").strip()
    if drink_code not in DRINK_PRICE:
        drink_code = ""
    drink_label_val = DRINK_LABEL.get(drink_code, "") if drink_code else None
    drink_price = float(DRINK_PRICE.get(drink_code, 0.0))

    bread = (request.form.get("bread", "") or "").strip() or None
    comment = (request.form.get("comment", "") or "").strip() or None

    if not name or not soup or not phone_norm:
        return html_page("<p class='danger'>Ошибка: имя, телефон и суп обязательны / Name, phone and soup are required.</p><p><a href='/'>Назад / Back</a></p>"), 400

    option_code, base_price, err = compute_option_base_price(zakuska, soup, hot, dessert, d)
    if err:
        return html_page(f"<p class='danger'>Ошибка: {err}</p><p><a href='/'>Назад / Back</a></p>"), 400

    total_price = compute_total_price(base_price, drink_code)

    conn = db()
    ensure_columns(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")

        cnt = conn.execute(
            "SELECT COUNT(*) as c FROM orders WHERE office=? AND order_date=? AND status='active'",
            (OFFICE, d.isoformat()),
        ).fetchone()["c"]
        if cnt >= MAX_PER_DAY:
            conn.execute("ROLLBACK")
            return html_page("<p class='danger'><b>Заказы на выбранную дату временно недоступны.</b><br><small>Orders are temporarily unavailable for this date.</small></p><p><a href='/'>Назад / Back</a></p>"), 409

        # ✅ Разрешаем два заказа с одного телефона — убрана проверка на дубль

        order_code = generate_order_code(conn, d)

        conn.execute(
            """
            INSERT INTO orders(
              order_code, office, order_date, floor,
              name, phone_raw, phone_norm,
              zakuska, soup, hot, dessert,
              drink_code, drink_label, drink_price_eur,
              bread,
              option_code, price_eur, comment, status, created_at
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order_code, OFFICE, d.isoformat(), floor,
                name, phone_raw, phone_norm,
                zakuska, soup, hot, dessert,
                drink_code or None, drink_label_val, drink_price if drink_code else None,
                bread,
                option_code, float(total_price), comment,
                "active", datetime.utcnow().isoformat()
            ),
        )

        conn.commit()
    finally:
        conn.close()

    opt_human = {"opt1": "Опция 1 / Option 1", "opt2": "Опция 2 / Option 2", "opt3": "Опция 3 / Option 3"}[option_code]
    drink_line = f"{drink_label_val} (+{drink_price}€)" if drink_code else "—"
    floor_line = floor or "—"

    return html_page(
        f"""
      <h2>✅ Заказ принят / Order confirmed</h2>
      <div class="card">
        <p><span class="pill"><b>{order_code}</b></span></p>
        <p><b>{name}</b> — {OFFICE} — <span class="muted">{phone_raw}</span></p>
        <p>Этаж / Floor: <b>{floor_line}</b></p>
        <p>Дата доставки / Delivery date: <b>{d.isoformat()}</b> (13:00)</p>
        <p><span class="pill">{opt_human}</span><span class="pill">Итого / Total: {total_price}€</span></p>
        <ul>
          <li>Суп / Soup: {soup}</li>
          <li>Закуска / Starter: {zakuska or "—"}</li>
          <li>Горячее / Main: {hot or "—"}</li>
          <li>Десерт / Dessert: {dessert or "—"}</li>
          <li>Напиток / Drink: {drink_line}</li>
          <li>Хлеб / Bread: {bread or "—"}</li>
        </ul>
        <p class="muted">Комментарий / Notes: {comment or "—"}</p>
        <p><a class="btn-secondary" href="/edit?date={d.isoformat()}&phone={phone_raw}">Изменить / отменить / Edit / cancel</a></p>
      </div>
      <p><a href="/">Новый заказ / New order</a></p>
    """
    )


# ---------------------------
# Edit / Cancel
# ---------------------------
@app.get("/edit")
def edit_get():
    default_date = compute_default_date()

    d_str = request.args.get("date", default_date.isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = default_date

    phone_raw = (request.args.get("phone", "") or "").strip()
    phone_norm = normalize_phone(phone_raw) if phone_raw else ""

    # ✅ Поиск по коду заказа или телефону (для двух заказов с одного номера)
    order_code_search = (request.args.get("code", "") or "").strip()

    found = None
    conn = db()
    ensure_columns(conn)
    if order_code_search:
        found = conn.execute(
            "SELECT * FROM orders WHERE order_code=? AND status='active'",
            (order_code_search,),
        ).fetchone()
    elif phone_norm:
        # Если с одного телефона два заказа — показываем список
        rows = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
            (OFFICE, d.isoformat(), phone_norm),
        ).fetchall()
        if len(rows) == 1:
            found = rows[0]
        elif len(rows) > 1:
            conn.close()
            ok_time, start, end, now_ = validate_order_time(d)
            items_html = "".join([
                f"<p><a href='/edit?date={d.isoformat()}&code={r['order_code']}'>"
                f"<b>{r['order_code']}</b> — {r['name']} — {r['floor'] or '—'} — {r['soup']}"
                f"</a></p>"
                for r in rows
            ])
            body = f"""
<h1>Выберите заказ / Choose order</h1>
<div class="card">
  <p>На {d.isoformat()} найдено несколько заказов с этого телефона:</p>
  {items_html}
  <p><a href="/edit">← Назад / Back</a></p>
</div>
"""
            return html_page(body)
    conn.close()

    ok_time, start, end, now_ = validate_order_time(d)

    soups = get_soups_list()
    hot_items = hot_menu_with_special(d)

    if found:
        floor_edit_block = ""
        fval = (found["floor"] or "")
        floor_edit_block = f"""
        <div class="row" style="margin-top:10px;">
          <div>
            <label>Этаж / Floor</label>
            <select name="floor" required>
              <option value="">— choose floor —</option>
              <option value="1st floor" {"selected" if fval=="1st floor" else ""}>1 этаж / 1st floor</option>
              <option value="6th floor" {"selected" if fval=="6th floor" else ""}>6 этаж / 6th floor</option>
            </select>
          </div>
          <div></div>
        </div>
        """

        drink_options = ""
        for (k, lbl, _) in DRINKS:
            sel = "selected" if (found["drink_code"] or "") == (k or "") else ""
            drink_options += f"<option value='{k}' {sel}>{lbl}</option>"

        body = f"""
        <h1>Изменить / отменить заказ<br><small>Edit / cancel order</small></h1>
        <div class="card">
          <p><span class="pill"><b>{found['order_code']}</b></span>
             <span class="pill">Доставка / Delivery: {d.isoformat()} 13:00</span></p>

          <p class="muted">Окно изменений:
            <b>{start.strftime('%d.%m %H:%M')}</b> — <b>{end.strftime('%d.%m %H:%M')}</b>.
            Сейчас: <b>{now_.strftime('%d.%m %H:%M')}</b>.
          </p>
          {"<p class='danger'><b>Сейчас окно закрыто — изменения/отмена недоступны.</b><br><small>Window is closed.</small></p>" if not ok_time else ""}

          <form method="post" action="/edit">
            <input type="hidden" name="order_date" value="{d.isoformat()}">
            <input type="hidden" name="order_code" value="{found['order_code']}">

            <label>Как вас зовут / Your name</label>
            <input name="name" value="{found['name']}" required>

            {floor_edit_block}

            <div class="row">
              <div>
                <label>Закуска / Starter</label>
                <select name="zakuska">
                  <option value="" {"selected" if not found["zakuska"] else ""}>— без закуски / no starter —</option>
                  {options_html(MENU["zakuska"])}
                </select>
              </div>
              <div>
                <label>Суп / Soup</label>
                <select name="soup" required>
                  <option value="">— выбери суп / choose soup —</option>
                  {options_html(soups)}
                </select>
              </div>
            </div>

            <div class="row">
              <div>
                <label>Горячее / Main</label>
                <select name="hot">
                  <option value="" {"selected" if not found["hot"] else ""}>— без горячего / no main —</option>
                  {options_html(hot_items)}
                </select>
              </div>
              <div>
                <label>Десерт / Dessert</label>
                <select name="dessert">
                  <option value="" {"selected" if not found["dessert"] else ""}>— без десерта / no dessert —</option>
                  {options_html(MENU["dessert"])}
                </select>
              </div>
            </div>

            <label>Напиток / Drink</label>
            <select name="drink">{drink_options}</select>
            <small>оплачивается отдельно / not included</small>

            <label style="margin-top:16px;">Хлеб / Bread</label>
            <select name="bread">
              <option value="" {"selected" if not found["bread"] else ""}>— без хлеба / no bread —</option>
              {options_html(BREAD_OPTIONS)}
            </select>

            <label>Комментарий / Notes</label>
            <textarea name="comment" rows="3">{found["comment"] or ""}</textarea>

            <button type="submit" class="btn-primary">Сохранить / Save</button>
          </form>

          <form method="post" action="/cancel" style="margin-top:12px;">
            <input type="hidden" name="order_date" value="{d.isoformat()}">
            <input type="hidden" name="order_code" value="{found['order_code']}">
            <button type="submit" class="btn-danger">Отменить заказ / Cancel</button>
          </form>

          <p style="margin-top:16px;"><a href="/">← На главную / Home</a></p>
        </div>
        """
        return html_page(body)

    body = f"""
<h1>Изменить / отменить заказ<br><small>Edit / cancel order</small></h1>

<div class="card">
  <form method="get" action="/edit">
    <div class="row">
      <div>
        <label>Дата доставки / Delivery date</label>
        <input type="date" name="date" value="{d.isoformat()}" required>
      </div>
      <div>
        <label>Телефон (как в заказе) / Phone</label>
        <input name="phone" value="{phone_raw}" placeholder="" required>
      </div>
    </div>
    <small style="margin-top:6px;">Если два заказа — выберите нужный из списка.</small>
    <button type="submit" class="btn-primary">Найти заказ / Find order</button>
  </form>

  <p class="muted" style="margin-top:12px;">Если заказ не найден — проверь дату и телефон.<br>
  <small>If not found — check date and phone.</small></p>

  <p><a href="/">← На главную / Home</a></p>
</div>
"""
    return html_page(body)


@app.post("/edit")
def edit_post():
    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата / Invalid date.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        if is_closed_day(d):
            return html_page("<p class='danger'><b>В понедельник мы не работаем.</b></p><p><a href='/edit'>Назад</a></p>"), 403
        return html_page(
            f"<p class='danger'><b>Окно редактирования закрыто.</b><br>"
            f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
            f"<p><a href='/edit'>Назад / Back</a></p>"
        ), 403

    # ✅ Находим по коду заказа
    order_code_val = (request.form.get("order_code", "") or "").strip()
    if not order_code_val:
        return html_page("<p class='danger'>Ошибка: код заказа не указан.</p><p><a href='/edit'>Назад</a></p>"), 400

    name = (request.form.get("name", "") or "").strip()
    zakuska = (request.form.get("zakuska", "") or "").strip() or None
    soup = (request.form.get("soup", "") or "").strip()
    hot = (request.form.get("hot", "") or "").strip() or None
    dessert = (request.form.get("dessert", "") or "").strip() or None

    floor = (request.form.get("floor", "") or "").strip() or None
    ok_floor, floor = validate_floor_for_office(floor)
    if not ok_floor:
        return html_page("<p class='danger'>Выберите этаж / Please choose floor.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    drink_code = (request.form.get("drink", "") or "").strip()
    if drink_code not in DRINK_PRICE:
        drink_code = ""
    drink_label_val = DRINK_LABEL.get(drink_code, "") if drink_code else None
    drink_price = float(DRINK_PRICE.get(drink_code, 0.0))

    bread = (request.form.get("bread", "") or "").strip() or None
    comment = (request.form.get("comment", "") or "").strip() or None

    if not name or not soup:
        return html_page("<p class='danger'>Ошибка: имя и суп обязательны.</p><p><a href='/edit'>Назад</a></p>"), 400

    option_code, base_price, err = compute_option_base_price(zakuska, soup, hot, dessert, d)
    if err:
        return html_page(f"<p class='danger'>Ошибка: {err}</p><p><a href='/edit'>Назад</a></p>"), 400

    total_price = compute_total_price(base_price, drink_code)

    conn = db()
    ensure_columns(conn)

    existing = conn.execute(
        "SELECT * FROM orders WHERE order_code=? AND status='active'",
        (order_code_val,),
    ).fetchone()

    if not existing:
        conn.close()
        return html_page("<p class='danger'>Активный заказ не найден / Active order not found.</p><p><a href='/edit'>Назад</a></p>"), 404

    conn.execute(
        """
        UPDATE orders
        SET name=?, floor=?, zakuska=?, soup=?, hot=?, dessert=?,
            drink_code=?, drink_label=?, drink_price_eur=?,
            bread=?, option_code=?, price_eur=?, comment=?
        WHERE id=?
        """,
        (
            name, floor, zakuska, soup, hot, dessert,
            drink_code or None, drink_label_val, drink_price if drink_code else None,
            bread, option_code, float(total_price), comment,
            existing["id"],
        ),
    )
    conn.commit()
    conn.close()

    opt_human = {"opt1": "Опция 1 / Option 1", "opt2": "Опция 2 / Option 2", "opt3": "Опция 3 / Option 3"}[option_code]
    drink_line = f"{drink_label_val} (+{drink_price}€)" if drink_code else "—"
    floor_line = floor or "—"

    return html_page(
        f"""
      <h2>✅ Изменения сохранены / Saved</h2>
      <div class="card">
        <p><span class="pill"><b>{existing['order_code']}</b></span></p>
        <p><b>{name}</b> — {OFFICE} — <span class="muted">{existing['phone_raw']}</span></p>
        <p>Этаж / Floor: <b>{floor_line}</b></p>
        <p>Дата доставки / Delivery date: <b>{d.isoformat()}</b> (13:00)</p>
        <p><span class="pill">{opt_human}</span><span class="pill">Итого / Total: {total_price}€</span></p>
        <ul>
          <li>Суп / Soup: {soup}</li>
          <li>Закуска / Starter: {zakuska or "—"}</li>
          <li>Горячее / Main: {hot or "—"}</li>
          <li>Десерт / Dessert: {dessert or "—"}</li>
          <li>Напиток / Drink: {drink_line}</li>
          <li>Хлеб / Bread: {bread or "—"}</li>
        </ul>
        <p class="muted">Комментарий / Notes: {comment or "—"}</p>
      </div>
      <p><a href="/">← На главную / Home</a></p>
    """
    )


@app.post("/cancel")
def cancel_post():
    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата / Invalid date.</p><p><a href='/edit'>Назад</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        if is_closed_day(d):
            return html_page("<p class='danger'><b>В понедельник мы не работаем.</b></p><p><a href='/edit'>Назад</a></p>"), 403
        return html_page(
            f"<p class='danger'><b>Окно отмены закрыто.</b><br>"
            f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
            f"<p><a href='/edit'>Назад / Back</a></p>"
        ), 403

    order_code_val = (request.form.get("order_code", "") or "").strip()
    if not order_code_val:
        return html_page("<p class='danger'>Ошибка: код заказа не указан.</p><p><a href='/edit'>Назад</a></p>"), 400

    conn = db()
    ensure_columns(conn)

    existing = conn.execute(
        "SELECT * FROM orders WHERE order_code=? AND status='active'",
        (order_code_val,),
    ).fetchone()

    if not existing:
        conn.close()
        return html_page("<p class='danger'>Активный заказ не найден / Active order not found.</p><p><a href='/edit'>Назад</a></p>"), 404

    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (existing["id"],))
    conn.commit()
    conn.close()

    return html_page(
        f"""
      <h2>🗑 Заказ отменён / Order cancelled</h2>
      <div class="card">
        <p><span class="pill"><b>{existing['order_code']}</b></span></p>
        <p><b>{existing['name']}</b> — {OFFICE} — <span class="muted">{existing['phone_raw']}</span></p>
        <p>Дата доставки / Delivery date: <b>{d.isoformat()}</b> (13:00)</p>
      </div>
      <p><a href="/">← На главную / Home</a></p>
    """
    )


# ===========================
# Admin
# ===========================

def _ru_only(s: str) -> str:
    s = "" if s is None else str(s)
    return s.split(" / ")[0].strip()

SHORT = {
    "Оливье": "Оливье",
    "Винегрет": "Винегрет",
    "Икра из баклажанов": "Икра",
    "Паштет из куриной печени": "Паштет",
    "Шуба": "Шуба",
    "Борщ": "Борщ",
    "Солянка сборная мясная": "Солянка",
    "Куриный суп с лапшой и яйцом": "Кур. суп",
    "Куриные котлеты с пюре": "Котл+пюре",
    "Куриные котлеты с гречкой": "Котл+греча",
    "Вареники с картошкой": "Вареники",
    "Пельмени со сметаной": "Пельмени",
    "Плов с бараниной (+3€)": "Плов",
    "Торт Наполеон": "Наполеон",
    "Пирожное Картошка": "Картошка",
    "Трубочка со сгущенкой": "Трубочка",
    "Белый": "Хлеб белый",
    "Чёрный": "Хлеб чёрный",
}

def _short_name(s: str) -> str:
    ru = _ru_only(s)
    return SHORT.get(ru, ru)

def _fmt_money(x):
    try:
        return f"{float(x):.2f}€"
    except Exception:
        return f"{x}€"

def _floor_norm(f) -> str:
    f = (f or "").strip()
    return f if f else "Без этажа"

def _floor_sort_key(k: str):
    kk = (k or "").lower()
    if "1st" in kk or "1 этаж" in kk:
        return (0, 1)
    if "6th" in kk or "6 этаж" in kk:
        return (0, 6)
    if "без" in kk:
        return (2, 999)
    return (1, k)

def _rows_table_v2(rows):
    head = """
    <table class="admin-table">
      <thead>
        <tr>
          <th>Код</th>
          <th>Имя</th>
          <th>Телефон</th>
          <th>Этаж</th>
          <th>Итого</th>
          <th>Суп</th>
          <th>Закуска</th>
          <th>Горячее</th>
          <th>Десерт</th>
          <th>Напиток</th>
          <th>Хлеб</th>
          <th>Комментарий</th>
        </tr>
      </thead>
      <tbody>
    """
    if not rows:
        return head + "<tr><td colspan='12' class='muted'>—</td></tr></tbody></table>"

    body = ""
    for r in rows:
        drink = "—"
        if r["drink_label"]:
            dp = r["drink_price_eur"] or 0
            drink = f"{_ru_only(r['drink_label'])} (+{float(dp):.2f}€)"
        body += f"""
        <tr>
          <td><b>{r['order_code']}</b></td>
          <td>{r['name']}</td>
          <td>{r['phone_raw']}</td>
          <td>{_floor_norm(r['floor'])}</td>
          <td><b>{_fmt_money(r['price_eur'])}</b></td>
          <td>{_short_name(r['soup']) if r['soup'] else '—'}</td>
          <td>{_short_name(r['zakuska']) if r['zakuska'] else '—'}</td>
          <td>{_short_name(r['hot']) if r['hot'] else '—'}</td>
          <td>{_short_name(r['dessert']) if r['dessert'] else '—'}</td>
          <td>{drink}</td>
          <td>{_short_name(r['bread']) if r['bread'] else '—'}</td>
          <td>{r['comment'] or '—'}</td>
        </tr>
        """
    return head + body + "</tbody></table>"

def _summary_counts(rows):
    opt_counts = {"opt1": 0, "opt2": 0, "opt3": 0}
    dish_counts = {}
    drink_counts = {}

    for r in rows:
        if r["option_code"] in opt_counts:
            opt_counts[r["option_code"]] += 1
        for k in ["soup", "zakuska", "hot", "dessert", "bread"]:
            v = r[k]
            if v:
                vv = _short_name(v)
                dish_counts[vv] = dish_counts.get(vv, 0) + 1
        if r["drink_label"]:
            dd = _ru_only(r["drink_label"])
            drink_counts[dd] = drink_counts.get(dd, 0) + 1

    return opt_counts, dish_counts, drink_counts

def _simple_table(title: str, counts: dict) -> str:
    rows_html = ""
    for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        rows_html += f"<tr><td>{k}</td><td style='text-align:right;'><b>{v}</b></td></tr>"
    if not rows_html:
        rows_html = "<tr><td colspan='2' class='muted'>—</td></tr>"

    return f"""
    <div class="card">
      <h3 style="margin:0 0 10px 0;">{title}</h3>
      <table class="admin-table">
        <thead><tr><th>Позиция</th><th style="text-align:right;">Кол-во</th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    """

def _active_by_floor(rows):
    g = {}
    for r in rows:
        k = _floor_norm(r["floor"])
        g.setdefault(k, []).append(r)
    return g


@app.get("/admin")
def admin_v2():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2><p>Нужен token.</p>"), 403

    # ✅ По умолчанию — сегодняшняя дата
    today_str = date.today().isoformat()
    d_str = request.args.get("date", today_str)
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = date.today()

    conn = db()
    ensure_columns(conn)

    active_rows = conn.execute(
        """
        SELECT * FROM orders
        WHERE office=? AND order_date=? AND status='active'
        ORDER BY created_at ASC
        """,
        (OFFICE, d.isoformat()),
    ).fetchall()

    cancelled_rows = conn.execute(
        """
        SELECT * FROM orders
        WHERE office=? AND order_date=? AND status='cancelled'
        ORDER BY created_at ASC
        """,
        (OFFICE, d.isoformat()),
    ).fetchall()

    conn.close()

    active_groups = _active_by_floor(active_rows)
    opt_counts, dish_counts, drink_counts = _summary_counts(active_rows)

    active_html = ""
    for floor_name in sorted(active_groups.keys(), key=_floor_sort_key):
        rr = active_groups[floor_name]
        active_html += f"""
        <div class="card">
          <div style="display:flex; align-items:baseline; justify-content:space-between; gap:10px; flex-wrap:wrap;">
            <h3 style="margin:0;">Активные — {floor_name}</h3>
            <div class="muted" style="font-weight:800;">{len(rr)} шт.</div>
          </div>
          <div class="no-print" style="margin-top:10px; display:flex; gap:10px; flex-wrap:wrap;">
            <a class="btn-primary" href="/admin/print?date={d.isoformat()}&floor={floor_name}&token={ADMIN_TOKEN}">
              🖨 Печать: {floor_name}
            </a>
          </div>
          {_rows_table_v2(rr)}
        </div>
        """

    body = f"""
    <h1>Админка — {OFFICE}</h1>

    <div class="card">
      <form method="get" action="/admin">
        <input type="hidden" name="token" value="{ADMIN_TOKEN}">
        <div class="row">
          <div>
            <label>Дата</label>
            <input type="date" name="date" value="{d.isoformat()}">
          </div>
          <div></div>
        </div>
        <button class="btn-primary" type="submit">Показать</button>
      </form>

      <p style="margin-top:14px;">
        <a href="/export.csv?date={d.isoformat()}&token={ADMIN_TOKEN}">⬇️ CSV (активные)</a>
        &nbsp;|&nbsp;
        <a href="/admin/print?date={d.isoformat()}&token={ADMIN_TOKEN}">🖨 Печать всех</a>
        &nbsp;|&nbsp;
        <a href="/admin/summary?date={d.isoformat()}&token={ADMIN_TOKEN}">🧾 Сводка</a>
        &nbsp;|&nbsp;
        <a href="/admin/specials?date={d.isoformat()}&token={ADMIN_TOKEN}">⭐ Блюдо недели</a>
        &nbsp;|&nbsp;
        <a href="/admin/soups?token={ADMIN_TOKEN}">🍲 Управление супами</a>
      </p>

      <p>
        <span class="pill">Всего активных: {len(active_rows)}</span>
        <span class="pill">Опция 1: {opt_counts.get('opt1',0)}</span>
        <span class="pill">Опция 2: {opt_counts.get('opt2',0)}</span>
        <span class="pill">Опция 3: {opt_counts.get('opt3',0)}</span>
      </p>
    </div>

    {active_html}

    <div class="card">
      <h3>Отменённые заказы</h3>
      {_rows_table_v2(cancelled_rows)}
    </div>

    {_simple_table("Сводка по блюдам (активные)", dish_counts)}
    {_simple_table("Сводка по напиткам (активные)", drink_counts)}
    """
    return html_page(body)


# --- Summary ---
ADMIN_SUMMARY_CSS = """
<style>
  @media print {
    .no-print { display:none !important; }
    body { margin:0; background:#fff !important; }
    .card { border:none; margin:0; padding:0; background:#fff !important; }
    table, th, td { background:#fff !important; border-color:#000 !important; }
    .admin-table th { background:#fff !important; color:#000 !important; }
    .admin-table td { color:#000 !important; }
    * { -webkit-print-color-adjust: economy; print-color-adjust: economy; }
    a { color:#000; text-decoration:none; }
  }
</style>
"""

@app.get("/admin/summary")
def admin_summary_v2():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2><p>Нужен token.</p>"), 403

    d_str = request.args.get("date", date.today().isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = date.today()

    conn = db()
    ensure_columns(conn)
    rows = conn.execute(
        "SELECT option_code, soup, zakuska, hot, dessert, bread, drink_label FROM orders WHERE office=? AND order_date=? AND status='active'",
        (OFFICE, d.isoformat()),
    ).fetchall()
    conn.close()

    dish_counts = {}
    drink_counts = {}
    for r in rows:
        for k in ["soup", "zakuska", "hot", "dessert", "bread"]:
            v = r[k]
            if v:
                vv = _short_name(v)
                dish_counts[vv] = dish_counts.get(vv, 0) + 1
        if r["drink_label"]:
            dd = _ru_only(r["drink_label"])
            drink_counts[dd] = drink_counts.get(dd, 0) + 1

    body = f"""
    {ADMIN_SUMMARY_CSS}
    <h1>Сводка (кухня/бар)</h1>
    <div class="card">
      <p><b>Офис:</b> {OFFICE} &nbsp; | &nbsp; <b>Дата:</b> {d.isoformat()}</p>
      <div class="no-print" style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
        <a class="btn-primary" href="/admin?date={d.isoformat()}&token={ADMIN_TOKEN}">← Назад в админку</a>
        <button class="btn-primary" type="button" onclick="window.print()">Печать / PDF</button>
      </div>
      <div style="margin-top:16px;">{_simple_table("Блюда (активные)", dish_counts)}</div>
      <div style="margin-top:18px;">{_simple_table("Напитки (активные)", drink_counts)}</div>
    </div>
    """
    return html_page(body)


# --- Print ---
ADMIN_PRINT_CSS = """
<style>
  @media print{
    body{ margin:0; background:#fff !important; }
    .card{ border:0 !important; margin:0; padding:0; background:#fff !important; }
    table, th, td{ background:#fff !important; }
    .admin-table th{ background:#fff !important; }
    *{ -webkit-print-color-adjust: economy; print-color-adjust: economy; }
    body{ font-size:11px; }
    .admin-table{ font-size:10px; }
    .admin-table th, .admin-table td{ padding:4px 6px; }
    .no-print, button, a{ display:none !important; }
  }
</style>
"""

@app.get("/admin/print")
def admin_print_active_v2():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2><p>Нужен token.</p>"), 403

    d_str = request.args.get("date", date.today().isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = date.today()

    floor_filter = (request.args.get("floor", "") or "").strip()

    conn = db()
    ensure_columns(conn)

    if floor_filter:
        rows = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND status='active' AND COALESCE(floor,'')=? ORDER BY created_at ASC",
            (OFFICE, d.isoformat(), floor_filter if floor_filter != "Без этажа" else ""),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND status='active' ORDER BY created_at ASC",
            (OFFICE, d.isoformat()),
        ).fetchall()
    conn.close()

    title = "Печать — активные заказы" + (f" — {floor_filter}" if floor_filter else "")

    body = f"""
    {ADMIN_PRINT_CSS}
    <h1 style="text-align:center;">{title}</h1>
    <p style="text-align:center; font-weight:800;">Офис: {OFFICE} | Дата: {d.isoformat()}</p>
    <div class="card">
      {_rows_table_v2(rows)}
      <div class="no-print" style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap;">
        <button class="btn-primary" type="button" onclick="window.print()">🖨 Печать</button>
        <a class="btn-danger" href="/admin?date={d.isoformat()}&token={ADMIN_TOKEN}">← Назад</a>
      </div>
    </div>
    """
    return html_page(body)


# --- Specials management ---
@app.get("/admin/specials")
def admin_specials_get():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2><p>Нужен token.</p>"), 403

    d_str = request.args.get("date", date.today().isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = date.today()

    conn = db()
    rows = conn.execute(
        "SELECT * FROM weekly_special WHERE office=? ORDER BY id DESC LIMIT 30",
        (OFFICE,),
    ).fetchall()
    conn.close()

    start_default = d.isoformat()
    end_default = (d + timedelta(days=6)).isoformat()

    list_html = ""
    for r in rows:
        list_html += f"""
        <tr>
          <td><b>{r['id']}</b></td>
          <td>{r['start_date']} → {r['end_date']}</td>
          <td>{r['title']}</td>
          <td style="text-align:right;">+{int(r['surcharge_eur'])}€</td>
          <td style="text-align:right;">
            <form method="post" action="/admin/specials/delete?token={ADMIN_TOKEN}" onsubmit="return confirm('Удалить?');">
              <input type="hidden" name="id" value="{r['id']}">
              <input type="hidden" name="date" value="{d.isoformat()}">
              <button class="btn-danger" type="submit">Удалить</button>
            </form>
          </td>
        </tr>
        """
    if not list_html:
        list_html = "<tr><td colspan='5' class='muted'>—</td></tr>"

    body = f"""
    <h1>Блюдо недели — управление</h1>
    <div class="card">
      <p><a href="/admin?date={d.isoformat()}&token={ADMIN_TOKEN}">← Назад в админку</a></p>
    </div>
    <div class="card">
      <h3>Добавить блюдо недели</h3>
      <form method="post" action="/admin/specials/create?token={ADMIN_TOKEN}">
        <div class="row">
          <div>
            <label>Начало</label>
            <input type="date" name="start_date" value="{start_default}" required>
          </div>
          <div>
            <label>Конец</label>
            <input type="date" name="end_date" value="{end_default}" required>
          </div>
        </div>
        <label>Название блюда</label>
        <input name="title" placeholder="Напр. Бефстроганов" required>
        <label>Доплата, €</label>
        <input name="surcharge_eur" type="number" min="0" step="1" value="0" required>
        <button class="btn-primary" type="submit">Сохранить</button>
      </form>
    </div>
    <div class="card">
      <h3>Последние 30 записей</h3>
      <table class="admin-table">
        <thead><tr><th>ID</th><th>Период</th><th>Название</th><th style="text-align:right;">Доплата</th><th>Действия</th></tr></thead>
        <tbody>{list_html}</tbody>
      </table>
    </div>
    """
    return html_page(body)


@app.post("/admin/specials/create")
def admin_specials_create_post():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403

    try:
        start_date = date.fromisoformat((request.form.get("start_date", "") or "").strip())
        end_date = date.fromisoformat((request.form.get("end_date", "") or "").strip())
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверные даты.</p>"), 400

    if end_date < start_date:
        return html_page("<p class='danger'>Ошибка: дата конца раньше начала.</p>"), 400

    title = (request.form.get("title", "") or "").strip()
    if not title:
        return html_page("<p class='danger'>Ошибка: пустое название.</p>"), 400

    try:
        surcharge = int(request.form.get("surcharge_eur", "0"))
        if surcharge < 0:
            raise ValueError
    except ValueError:
        return html_page("<p class='danger'>Ошибка: доплата должна быть целым числом ≥ 0.</p>"), 400

    conn = db()
    conn.execute(
        "INSERT INTO weekly_special(office, start_date, end_date, title, surcharge_eur, created_at) VALUES (?,?,?,?,?,?)",
        (OFFICE, start_date.isoformat(), end_date.isoformat(), title, surcharge, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    return redirect(f"/admin/specials?date={start_date.isoformat()}&token={ADMIN_TOKEN}")


@app.post("/admin/specials/delete")
def admin_specials_delete_post():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403

    try:
        sid = int(request.form.get("id", "0"))
    except ValueError:
        sid = 0
    if sid <= 0:
        return html_page("<p class='danger'>Ошибка: неверный id.</p>"), 400

    conn = db()
    conn.execute("DELETE FROM weekly_special WHERE id=?", (sid,))
    conn.commit()
    conn.close()

    d = (request.form.get("date", date.today().isoformat()) or "").strip()
    return redirect(f"/admin/specials?date={d}&token={ADMIN_TOKEN}")


# ===========================
# ✅ Управление супами через админку
# ===========================

@app.get("/admin/soups")
def admin_soups_get():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2><p>Нужен token.</p>"), 403

    conn = db()
    rows = conn.execute(
        "SELECT * FROM admin_soups ORDER BY sort_order ASC, id ASC"
    ).fetchall()
    conn.close()

    list_html = ""
    for r in rows:
        active_checked = "checked" if r["active"] else ""
        en_part = r["title_en"] or ""
        display = f"{r['title_ru']} / {en_part}" if en_part else r["title_ru"]
        list_html += f"""
        <tr>
          <td><b>{r['id']}</b></td>
          <td>{display}</td>
          <td style="text-align:center;">{'✅' if r['active'] else '❌'}</td>
          <td style="text-align:center;">{r['sort_order']}</td>
          <td>
            <form method="post" action="/admin/soups/toggle?token={ADMIN_TOKEN}" style="display:inline;">
              <input type="hidden" name="id" value="{r['id']}">
              <button class="btn-primary" type="submit" style="margin-top:0; padding:6px 12px; font-size:13px;">
                {'Скрыть' if r['active'] else 'Показать'}
              </button>
            </form>
            &nbsp;
            <form method="post" action="/admin/soups/delete?token={ADMIN_TOKEN}" style="display:inline;" onsubmit="return confirm('Удалить суп?');">
              <input type="hidden" name="id" value="{r['id']}">
              <button class="btn-danger" type="submit" style="margin-top:0; padding:6px 12px; font-size:13px;">Удалить</button>
            </form>
          </td>
        </tr>
        """

    if not list_html:
        list_html = "<tr><td colspan='5' class='muted'>Супы не добавлены (используется встроенный список)</td></tr>"

    body = f"""
    <h1>Управление супами</h1>

    <div class="card">
      <p><a href="/admin?token={ADMIN_TOKEN}">← Назад в админку</a></p>
      <p class="muted">Если список пустой — в форме заказа используются супы по умолчанию из кода.<br>
      Добавленные супы полностью заменяют встроенный список.</p>
    </div>

    <div class="card">
      <h3>Добавить суп</h3>
      <form method="post" action="/admin/soups/create?token={ADMIN_TOKEN}">
        <div class="row">
          <div>
            <label>Название (RU) *</label>
            <input name="title_ru" placeholder="Борщ" required>
          </div>
          <div>
            <label>Название (EN, необязательно)</label>
            <input name="title_en" placeholder="Borscht">
          </div>
        </div>
        <label>Порядок сортировки (0 = первый)</label>
        <input name="sort_order" type="number" value="0" min="0" style="max-width:120px;">
        <button class="btn-primary" type="submit">Добавить суп</button>
      </form>
    </div>

    <div class="card">
      <h3>Текущие супы ({len(rows)} шт.)</h3>
      <table class="admin-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Название</th>
            <th style="text-align:center;">Активен</th>
            <th style="text-align:center;">Порядок</th>
            <th>Действия</th>
          </tr>
        </thead>
        <tbody>{list_html}</tbody>
      </table>
    </div>
    """
    return html_page(body)


@app.post("/admin/soups/create")
def admin_soups_create():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403

    title_ru = (request.form.get("title_ru", "") or "").strip()
    title_en = (request.form.get("title_en", "") or "").strip()
    if not title_ru:
        return html_page("<p class='danger'>Ошибка: название (RU) обязательно.</p><p><a href='/admin/soups?token={ADMIN_TOKEN}'>Назад</a></p>"), 400

    try:
        sort_order = int(request.form.get("sort_order", "0"))
        if sort_order < 0:
            sort_order = 0
    except ValueError:
        sort_order = 0

    conn = db()
    conn.execute(
        "INSERT INTO admin_soups(title_ru, title_en, sort_order, active, created_at) VALUES (?,?,?,1,?)",
        (title_ru, title_en, sort_order, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    return redirect(f"/admin/soups?token={ADMIN_TOKEN}")


@app.post("/admin/soups/toggle")
def admin_soups_toggle():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403

    try:
        sid = int(request.form.get("id", "0"))
    except ValueError:
        sid = 0
    if sid <= 0:
        return redirect(f"/admin/soups?token={ADMIN_TOKEN}")

    conn = db()
    row = conn.execute("SELECT active FROM admin_soups WHERE id=?", (sid,)).fetchone()
    if row:
        new_active = 0 if row["active"] else 1
        conn.execute("UPDATE admin_soups SET active=? WHERE id=?", (new_active, sid))
        conn.commit()
    conn.close()

    return redirect(f"/admin/soups?token={ADMIN_TOKEN}")


@app.post("/admin/soups/delete")
def admin_soups_delete():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403

    try:
        sid = int(request.form.get("id", "0"))
    except ValueError:
        sid = 0
    if sid > 0:
        conn = db()
        conn.execute("DELETE FROM admin_soups WHERE id=?", (sid,))
        conn.commit()
        conn.close()

    return redirect(f"/admin/soups?token={ADMIN_TOKEN}")


# --- CSV export ---
@app.get("/export.csv")
def export_csv():
    if not check_admin():
        return Response("Forbidden", status=403)

    d_str = request.args.get("date", date.today().isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = date.today()

    conn = db()
    ensure_columns(conn)
    rows = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND status='active' ORDER BY created_at ASC",
        (OFFICE, d.isoformat()),
    ).fetchall()
    conn.close()

    import csv
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["order_code", "office", "order_date", "floor", "name", "phone_raw",
                     "zakuska", "soup", "hot", "dessert", "drink_label", "bread",
                     "option_code", "price_eur", "comment", "created_at"])
    for r in rows:
        writer.writerow([
            r["order_code"], r["office"], r["order_date"], r["floor"] or "",
            r["name"], r["phone_raw"],
            r["zakuska"] or "", r["soup"], r["hot"] or "", r["dessert"] or "",
            r["drink_label"] or "", r["bread"] or "",
            r["option_code"], r["price_eur"], r["comment"] or "", r["created_at"],
        ])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=orders_{OFFICE}_{d.isoformat()}.csv"},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)













































































