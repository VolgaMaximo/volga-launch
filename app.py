import os
import re
import json
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

OFFICE = "ALAMEDA"
OFFICES = ["ALAMEDA"]

FLOORS_BY_OFFICE = {
    "ALAMEDA": ["1st floor", "6th floor"]
}

MENU = {
    "zakuska": [
        "Оливье / Olivier salad",
        "Винегрет / Vinigret salad",
        "Икра из баклажанов / Eggplant caviar",
        "Паштет из куриной печени / Chicken liver pâté",
        "Шуба / Herring under a fur coat",
    ],
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

# Названия категорий
CAT_NAMES = {
    "zakuska": "Закуска / Starter",
    "soup":    "Суп / Soup",
    "hot":     "Горячее / Main",
    "dessert": "Десерт / Dessert",
}

app = Flask(__name__)


# ---------------------------
# DB
# ---------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    for col, typ in [
        ("drink_code", "TEXT"),
        ("drink_label", "TEXT"),
        ("drink_price_eur", "REAL"),
        ("floor", "TEXT"),
        ("order_type", "TEXT"),
        ("alacarte_items", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")

    # weekly_special: alacarte_price
    ws_cols = {r["name"] for r in conn.execute("PRAGMA table_info(weekly_special)").fetchall()}
    if "alacarte_price_eur" not in ws_cols:
        conn.execute("ALTER TABLE weekly_special ADD COLUMN alacarte_price_eur REAL DEFAULT 0")

    # alacarte_prices: migrate from category-based to item_key-based
    ac_cols = {r["name"] for r in conn.execute("PRAGMA table_info(alacarte_prices)").fetchall()}
    if "item_key" not in ac_cols:
        # Old table has (category PRIMARY KEY, price_eur) — drop and recreate
        conn.execute("DROP TABLE IF EXISTS alacarte_prices")
        conn.execute("""
            CREATE TABLE alacarte_prices (
                item_key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                price_eur REAL NOT NULL DEFAULT 0
            )
        """)


def init_db():
    conn = db()

    conn.execute("""
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
            soup TEXT,
            hot TEXT,
            dessert TEXT,
            bread TEXT,
            option_code TEXT,
            price_eur REAL NOT NULL,
            comment TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            order_type TEXT NOT NULL DEFAULT 'complex',
            alacarte_items TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_office_date ON orders(office, order_date)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_special (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            office TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            title TEXT NOT NULL,
            surcharge_eur INTEGER NOT NULL DEFAULT 0,
            alacarte_price_eur REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_special_office_dates ON weekly_special(office, start_date, end_date)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_soups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title_ru TEXT NOT NULL,
            title_en TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    # Сезонные блюда
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seasonal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title_ru TEXT NOT NULL,
            title_en TEXT NOT NULL,
            alacarte_price_eur REAL NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    # Цены à la carte по конкретным блюдам
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alacarte_prices (
            item_key TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            price_eur REAL NOT NULL DEFAULT 0
        )
    """)

    ensure_columns(conn)
    conn.commit()
    conn.close()


init_db()


# ---------------------------
# Helpers
# ---------------------------
def now_local():
    return datetime.now(TZ)


def cutoff_dt(d):
    return datetime.combine(d, time(CUTOFF_HOUR, 0), TZ)


def is_workday(d):
    return d.weekday() in (1, 2, 3, 4)


def next_workday(d):
    x = d + timedelta(days=1)
    while not is_workday(x):
        x += timedelta(days=1)
    return x


def prev_workday(d):
    x = d - timedelta(days=1)
    while not is_workday(x):
        x -= timedelta(days=1)
    return x


def ordering_window_for(d):
    return cutoff_dt(prev_workday(d)), cutoff_dt(d)


def is_closed_day(d):
    return not is_workday(d)


def validate_order_time(d):
    n = now_local()
    start, end = ordering_window_for(d)
    if is_closed_day(d):
        return False, start, end, n
    return (start <= n < end), start, end, n


def normalize_phone(raw):
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


def file_path(name):
    return os.path.join(os.path.dirname(__file__), name)


def validate_floor_for_office(floor):
    allowed = set(FLOORS_BY_OFFICE.get(OFFICE, []))
    if allowed:
        if floor not in allowed:
            return False, None
        return True, floor
    return True, None


# --- Menu helpers ---
def get_weekly_special(d):
    conn = db()
    row = conn.execute(
        "SELECT * FROM weekly_special WHERE office=? AND start_date<=? AND end_date>=? ORDER BY id DESC LIMIT 1",
        (OFFICE, d.isoformat(), d.isoformat()),
    ).fetchone()
    conn.close()
    return row


def get_soups_list():
    result = list(MENU["soup"])
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


def get_seasonal_items(category):
    """Активные сезонные блюда для категории."""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM seasonal_items WHERE category=? AND active=1 ORDER BY id ASC",
        (category,)
    ).fetchall()
    conn.close()
    return rows


def get_alacarte_prices():
    """Словарь item_key->price_eur."""
    conn = db()
    rows = conn.execute("SELECT item_key, price_eur FROM alacarte_prices").fetchall()
    conn.close()
    return {r["item_key"]: float(r["price_eur"]) for r in rows}

def make_item_key(item_label):
    """Создаём ключ из названия блюда — берём RU часть без спецсимволов."""
    ru = item_label.split(" / ")[0].strip().replace(" 🌿", "")
    return ru[:80]


def menu_for_category(category, d=None):
    """
    Возвращает список строк для категории,
    включая сезонные блюда (помечены тегом [seasonal]).
    Для soup — включает доп. супы из admin_soups.
    Для hot — включает блюдо недели.
    """
    if category == "soup":
        items = get_soups_list()
    elif category == "hot":
        items = MENU["hot"].copy()
        if d:
            special = get_weekly_special(d)
            if special:
                lbl = f"Блюдо недели: {special['title']} / Weekly special: {special['title']}"
                s = int(special["surcharge_eur"])
                if s > 0:
                    lbl += f" (+{s}€)"
                items.insert(0, lbl)
    else:
        items = MENU[category].copy()

    # Сезонные
    for s in get_seasonal_items(category):
        en = (s["title_en"] or "").strip()
        ru = (s["title_ru"] or "").strip()
        lbl = f"{ru} / {en}" if en else ru
        lbl += " 🌿"  # маркер сезонного
        items.append(lbl)

    return items


def alacarte_price_for_item(item_label, category, alacarte_prices, d=None):
    """
    Возвращает цену за конкретное блюдо в режиме à la carte.
    Сезонные — своя цена из seasonal_items.
    Блюдо недели — своя цена из weekly_special.
    Остальные — цена из alacarte_prices по item_key.
    """
    if item_label.endswith(" 🌿"):
        conn = db()
        rows = conn.execute(
            "SELECT alacarte_price_eur, title_ru, title_en FROM seasonal_items WHERE category=? AND active=1",
            (category,)
        ).fetchall()
        conn.close()
        for r in rows:
            en = (r["title_en"] or "").strip()
            ru = (r["title_ru"] or "").strip()
            lbl = f"{ru} / {en}" if en else ru
            if item_label == lbl + " 🌿":
                return float(r["alacarte_price_eur"])
        return 0.0

    if item_label.startswith("Блюдо недели:") and d:
        special = get_weekly_special(d)
        if special:
            return float(special["alacarte_price_eur"] or 0)

    key = make_item_key(item_label)
    return alacarte_prices.get(key, 0.0)


def hot_menu_with_special(d):
    return menu_for_category("hot", d)


def compute_option_base_price(zakuska, soup, hot, dessert, d):
    has_z = bool(zakuska)
    has_s = bool(soup)
    has_h = bool(hot)
    has_d = bool(dessert)

    if not has_s:
        return None, None, "Суп обязателен / Soup is required."

    if has_z and has_s and has_d and not has_h:
        option = "opt1"; price = PRICES[option]
    elif (not has_z) and has_s and has_h and has_d:
        option = "opt2"; price = PRICES[option]
    elif has_z and has_s and has_h and (not has_d):
        option = "opt3"; price = PRICES[option]
    else:
        return None, None, "Нужно выбрать ровно 3 категории / Please select exactly 3 categories."

    if hot and "Плов с бараниной" in hot:
        price += PLOV_SURCHARGE
    if hot and hot.startswith("Блюдо недели:"):
        special = get_weekly_special(d)
        if special:
            price += float(int(special["surcharge_eur"]))

    return option, float(price), None


def compute_total_price(base_price, drink_code):
    add = float(DRINK_PRICE.get((drink_code or "").strip(), 0.0))
    return round(float(base_price) + add, 2)


def generate_order_code(conn, d):
    ymd = d.strftime("%Y%m%d")
    like_prefix = f"{ORDER_PREFIX}-{ymd}-"
    row = conn.execute(
        "SELECT order_code FROM orders WHERE office=? AND order_date=? AND order_code LIKE ? ORDER BY order_code DESC LIMIT 1",
        (OFFICE, d.isoformat(), like_prefix + "%"),
    ).fetchone()
    if not row:
        seq = 1
    else:
        try:
            seq = int(row["order_code"].split("-")[-1]) + 1
        except ValueError:
            seq = 1
    return f"{ORDER_PREFIX}-{ymd}-{seq:03d}"


# ---------------------------
# PWA
# ---------------------------
@app.get("/manifest.webmanifest")
def manifest():
    data = {
        "name": APP_TITLE, "short_name": "VOLGA Lunch",
        "start_url": "/", "display": "standalone",
        "background_color": "#EDE7D3", "theme_color": "#EDE7D3",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


@app.get("/icon.svg")
def icon_svg():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
<rect width="512" height="512" fill="#EDE7D3"/>
<path d="M110 170 L402 110 L402 180 L110 240 Z" fill="#E73F24" opacity="0.95"/>
<path d="M110 330 L402 270 L402 340 L110 400 Z" fill="#0E238E" opacity="0.95"/>
<text x="256" y="290" font-family="Arial" font-size="64" text-anchor="middle" fill="#0E238E">VOLGA</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.get("/logo.png")
def logo_png():
    p = file_path("logo.png")
    if not os.path.exists(p):
        return Response("not found", status=404)
    return send_file(p)


@app.get("/banner.png")
def banner_png():
    p = file_path("banner.png")
    if not os.path.exists(p):
        return Response("not found", status=404)
    return send_file(p)


@app.get("/sw.js")
def sw_js():
    js = f"""
const CACHE = 'volga-lunch-{APP_VERSION}';
const ASSETS = ['/', '/edit', '/manifest.webmanifest', '/icon.svg', '/logo.png', '/banner.png'];
self.addEventListener('install', e => {{ e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS))); self.skipWaiting(); }});
self.addEventListener('activate', e => {{ e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))); self.clients.claim(); }});
self.addEventListener('fetch', e => {{
  const url = new URL(e.request.url);
  if(e.request.method==='GET' && url.origin===self.location.origin){{
    e.respondWith(fetch(e.request).then(r=>{{ const c=r.clone(); caches.open(CACHE).then(ca=>ca.put(e.request,c)); return r; }}).catch(()=>caches.match(e.request)));
  }}
}});
"""
    return Response(js, mimetype="application/javascript")


# ---------------------------
# HTML shell
# ---------------------------
CSS = """
<style>
:root{
  --volga-blue:#0E238E;
  --volga-red:#E73F24;
  --volga-burgundy:#8E2C1F;
  --volga-bg:#EDE7D3;
}
*{ box-sizing:border-box; }
body{ font-family:-apple-system,system-ui,Arial; margin:18px; background:var(--volga-bg); color:var(--volga-blue); }
.card{ background:transparent; border:2px solid var(--volga-blue); border-radius:0; padding:28px; margin:30px auto; max-width:900px; overflow:hidden; }
h1{ color:var(--volga-blue); font-weight:800; letter-spacing:1px; margin:0 0 14px 0; line-height:1.0; }
h1 small{ display:block; color:var(--volga-red); font-weight:800; line-height:1.00; margin-top:4px; }
.hero-title{ text-align:center; font-weight:800; font-size:28px; line-height:1.15; letter-spacing:1px; margin-bottom:14px; }
.hero-title .ru{ color:var(--volga-blue); }
.hero-title .en{ color:var(--volga-red); }
label{ display:block; margin:0 0 4px 0; font-weight:800; overflow-wrap:anywhere; color:var(--volga-red); }
input,select,textarea{ width:100%; max-width:520px; padding:12px; margin:0; font-size:16px; background:var(--volga-bg); color:var(--volga-blue); border:2px solid var(--volga-blue); border-radius:0; }
input:focus,select:focus,textarea:focus{ outline:none; border:2px solid var(--volga-blue); }
.row{ display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); column-gap:18px; row-gap:10px; align-items:start; margin-top:10px; }
.row > div{ width:100%; max-width:520px; }
.muted{ color:var(--volga-burgundy); }
.danger{ color:var(--volga-red); font-weight:800; }
small{ display:block; margin:2px 0 0 0; line-height:1.1; color:var(--volga-burgundy); }
a{ color:var(--volga-blue); text-decoration:none; font-weight:700; }
a:hover{ color:var(--volga-red); }
.pill{ display:inline-block; padding:6px 10px; border-radius:999px; border:1px solid var(--volga-blue); margin-right:8px; color:var(--volga-blue); }
.lead{ color:var(--volga-blue); text-align:center; font-weight:900; margin:12px 0 0 0; }
.lead .en{ color:var(--volga-red); font-weight:800; }
.hours{ margin:14px 0 0 0; text-align:center; font-weight:900; }
.hours .ru{ color:var(--volga-blue); }
.hours .en{ color:var(--volga-red); }
.btn-confirm{ display:block; width:100%; max-width:520px; padding:16px 24px; font-size:16px; font-weight:800; background:var(--volga-blue); color:var(--volga-bg); border:none; border-radius:0; cursor:pointer; transition:0.2s ease; }
.btn-confirm:active{ background:var(--volga-red); }
.btn-edit{ display:flex; text-align:center; align-items:center; justify-content:center; width:100%; max-width:520px; padding:16px 24px; font-size:16px; font-weight:800; background:var(--volga-red); color:var(--volga-bg); border:none; border-radius:0; cursor:pointer; transition:0.2s ease; margin-top:18px; }
.btn-edit:active{ background:var(--volga-blue); }
.comment-block{ margin-top:18px; }
.banner-block{ margin-top:18px; margin-bottom:18px; }
.btn-primary{ display:block; width:100%; margin-top:20px; padding:14px 24px; font-size:16px; font-weight:700; border:2px solid var(--volga-blue); background:var(--volga-blue); color:var(--volga-bg); border-radius:0; text-align:center; cursor:pointer; }
.btn-primary:hover{ background:var(--volga-red); border-color:var(--volga-red); }
.btn-danger{ display:block; width:100%; margin-top:14px; padding:14px 24px; font-size:16px; font-weight:700; border:2px solid var(--volga-red); background:var(--volga-red); color:var(--volga-bg); border-radius:0; text-align:center; cursor:pointer; }
.btn-danger:hover{ background:var(--volga-blue); border-color:var(--volga-blue); }
.admin-table{ width:100%; border-collapse:collapse; margin-top:10px; font-size:14px; }
.admin-table th,.admin-table td{ border:2px solid var(--volga-blue); padding:8px 10px; vertical-align:top; }
.admin-table th{ background:var(--volga-bg); color:var(--volga-blue); text-align:left; font-weight:800; }
.admin-table td small{ color:var(--volga-burgundy); }
.admin-table tbody tr:hover{ outline:2px solid var(--volga-red); outline-offset:-2px; }

/* --- MODE SWITCHER --- */
.mode-switcher{ display:flex; max-width:520px; margin:0 auto 24px auto; border:2px solid var(--volga-blue); }
.mode-btn{ flex:1; padding:14px 8px; font-size:15px; font-weight:800; background:var(--volga-bg); color:var(--volga-blue); border:none; cursor:pointer; transition:0.2s; text-align:center; }
.mode-btn.active{ background:var(--volga-blue); color:var(--volga-bg); }
.mode-btn:not(.active):hover{ background:var(--volga-red); color:var(--volga-bg); }

/* --- ALACARTE TABLE --- */
.alacarte-section{ margin-top:16px; }
.alacarte-cat{ margin-top:18px; }
.alacarte-cat-title{ font-weight:800; color:var(--volga-red); margin-bottom:8px; font-size:15px; }
.alacarte-item{ display:flex; align-items:center; justify-content:space-between; border:1px solid var(--volga-blue); padding:10px 14px; margin-bottom:6px; cursor:pointer; background:var(--volga-bg); transition:0.15s; }
.alacarte-item:hover{ border-color:var(--volga-red); }
.alacarte-item.selected{ background:var(--volga-blue); color:var(--volga-bg); }
.alacarte-item input[type=checkbox]{ width:20px; height:20px; margin-right:12px; flex-shrink:0; accent-color:var(--volga-red); cursor:pointer; }
.alacarte-item label{ flex:1; cursor:pointer; font-weight:600; margin:0; color:inherit; }
.alacarte-item .price-tag{ font-weight:800; white-space:nowrap; margin-left:10px; }
.alacarte-total{ margin-top:16px; padding:14px; border:2px solid var(--volga-blue); font-size:18px; font-weight:800; text-align:right; }
.alacarte-total span{ color:var(--volga-red); }
.seasonal-badge{ font-size:11px; background:var(--volga-red); color:var(--volga-bg); padding:2px 6px; border-radius:3px; margin-left:6px; vertical-align:middle; }

@media (max-width:700px){
  .card{ padding:20px; }
  .row{ grid-template-columns:1fr; column-gap:0; row-gap:10px; margin-top:10px; }
  .row > div{ max-width:none; }
  input,select,textarea{ max-width:100%; }
  h1{ letter-spacing:0.5px; }
  input[type="date"]{ -webkit-appearance:none; appearance:none; }
  .admin-table{ font-size:13px; }
  .admin-table th.created,.admin-table td.created{ display:none; }
}

/* PRINT */
@media print{
  .no-print{ display:none !important; }
  body{ margin:0; background:#fff !important; }
  .card{ border:none; margin:0; padding:0; background:#fff !important; }
  table,th,td{ background:#fff !important; border-color:#000 !important; }
  .admin-table th{ background:#fff !important; color:#000 !important; }
  .admin-table td{ color:#000 !important; }
  *{ -webkit-print-color-adjust:economy; print-color-adjust:economy; }
  a{ color:#000; text-decoration:none; }
}
</style>
"""

POPUP_HTML = """
<style>
#volgaPopupOverlay{ position:fixed; inset:0; background:rgba(0,0,0,0.35); display:none; align-items:center; justify-content:center; z-index:9999; }
#volgaPopupBox{ background:var(--volga-blue); color:var(--volga-bg); border:3px solid var(--volga-blue); padding:20px 24px; max-width:420px; width:90%; text-align:center; font-weight:800; line-height:1.4; }
#volgaPopupBox button{ margin-top:14px; padding:8px 18px; border:2px solid var(--volga-bg); background:var(--volga-red); color:var(--volga-bg); font-weight:800; cursor:pointer; }
</style>
<div id="volgaPopupOverlay">
  <div id="volgaPopupBox">
    <div id="volgaPopupText"></div>
    <button type="button" onclick="hideVolgaPopup()">OK</button>
  </div>
</div>
<script>
function showVolgaPopup(text){ document.getElementById("volgaPopupText").innerHTML=text; document.getElementById("volgaPopupOverlay").style.display="flex"; }
function hideVolgaPopup(){ document.getElementById("volgaPopupOverlay").style.display="none"; }
document.addEventListener("click",e=>{ if(e.target===document.getElementById("volgaPopupOverlay")) hideVolgaPopup(); });
</script>
"""

ALACARTE_BANNER = """
<style>
#alacarteBannerOverlay{ position:fixed; inset:0; background:rgba(0,0,0,0.55); display:flex; align-items:center; justify-content:center; z-index:99999; }
#alacarteBannerBox{ background:var(--volga-blue); color:var(--volga-bg); border:4px solid var(--volga-red); padding:28px 32px; max-width:480px; width:92%; text-align:center; line-height:1.5; }
#alacarteBannerBox .btitle{ font-size:22px; font-weight:900; margin-bottom:14px; }
#alacarteBannerBox .btitle span{ color:#FFD700; }
#alacarteBannerBox p{ margin:7px 0; font-size:15px; font-weight:700; }
#alacarteBannerBox p.en{ color:rgba(255,255,255,0.75); font-size:14px; font-weight:600; }
#alacarteBannerBox button{ margin-top:20px; padding:12px 32px; border:2px solid var(--volga-bg); background:var(--volga-red); color:var(--volga-bg); font-weight:900; font-size:16px; cursor:pointer; }
#alacarteBannerBox button:hover{ background:var(--volga-bg); color:var(--volga-blue); }
</style>
<div id="alacarteBannerOverlay">
  <div id="alacarteBannerBox">
    <div class="btitle">🍽 Теперь можно заказать <span>блюда отдельно!</span></div>
    <p>Выберите любые блюда из меню без комплекса.</p>
    <p>Используйте переключатель <b>«Блюда отдельно»</b> в форме заказа.</p>
    <p class="en">You can now order individual dishes!</p>
    <p class="en">Use the <b>«À la carte»</b> switch in the order form.</p>
    <button type="button" onclick="closeAlacarteBanner()">Понятно / Got it</button>
  </div>
</div>
<script>
(function(){
  var KEY = 'volga_alacarte_banner_v1';
  function closeAlacarteBanner(){ document.getElementById('alacarteBannerOverlay').style.display='none'; try{localStorage.setItem(KEY,'1');}catch(e){} }
  window.closeAlacarteBanner = closeAlacarteBanner;
  try{ if(localStorage.getItem(KEY)){ document.getElementById('alacarteBannerOverlay').style.display='none'; } }catch(e){}
})();
</script>
"""

def html_page(body, show_banner=False):
    banner = ALACARTE_BANNER if show_banner else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VOLGA Lunch</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#EDE7D3">
{CSS}
</head>
<body>
{body}
{POPUP_HTML}
{banner}
<script>
(function(){{
  document.querySelectorAll('form').forEach(f=>{{
    f.addEventListener('submit',()=>{{
      f.querySelectorAll('button[type="submit"]').forEach(b=>{{ b.disabled=true; b.textContent='Отправка… / Sending…'; }});
    }});
  }});
  if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{{}});
}})();
</script>
</body>
</html>"""


# ---------------------------
# Main order form
# ---------------------------
@app.get("/")
def form():
    default_date = compute_default_date()
    d_str = request.args.get("date", default_date.isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = default_date

    soups = menu_for_category("soup", d)
    hot_items = menu_for_category("hot", d)
    zakuski = menu_for_category("zakuska", d)
    desserts = menu_for_category("dessert", d)

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

    # drink prices for JS
    drink_prices_js = json.dumps({k: p for k, _, p in DRINKS})

    # À la carte prices JSON для JS
    ac_prices = get_alacarte_prices()

    # Сезонные блюда с ценами для à la carte (для JS)
    seasonal_prices = {}
    for cat in ["zakuska", "soup", "hot", "dessert"]:
        for s in get_seasonal_items(cat):
            en = (s["title_en"] or "").strip()
            ru = (s["title_ru"] or "").strip()
            lbl = (f"{ru} / {en}" if en else ru) + " 🌿"
            seasonal_prices[lbl] = float(s["alacarte_price_eur"])

    # Блюдо недели — цена à la carte
    special = get_weekly_special(d)
    special_alacarte_price = float(special["alacarte_price_eur"]) if special else 0.0
    special_label_prefix = "Блюдо недели:"

    ac_prices_js = json.dumps(ac_prices)
    seasonal_prices_js = json.dumps(seasonal_prices)

    # Build à la carte section
    def ac_category_html(cat, items, label):
        html = f'<div class="alacarte-cat"><div class="alacarte-cat-title">{label}</div>'
        for idx, item in enumerate(items):
            iid = f"ac_{cat}_{idx}"
            is_seasonal = item.endswith(" 🌿")
            is_special = item.startswith("Блюдо недели:")

            if is_seasonal:
                item_price = seasonal_prices.get(item, 0.0)
            elif is_special:
                item_price = special_alacarte_price
            else:
                # Ищем по item_key в ac_prices
                item_price = ac_prices.get(make_item_key(item), 0.0)

            badge = '<span class="seasonal-badge">сезон</span>' if is_seasonal else ''
            item_display = item.replace(" 🌿", "")

            html += f"""
            <div class="alacarte-item" id="wrap_{iid}" onclick="toggleAC('{iid}')">
              <input type="checkbox" name="ac_item" value="{item}|{cat}|{item_price}"
                     id="{iid}" onchange="updateACTotal()" onclick="event.stopPropagation()">
              <label for="{iid}">{item_display}{badge}</label>
              <span class="price-tag">{item_price:.2f}€</span>
            </div>"""
        html += "</div>"
        return html

    ac_html = '<div class="alacarte-section">'
    ac_html += ac_category_html("zakuska", zakuski, "Закуска / Starter")
    ac_html += ac_category_html("soup", soups, "Суп / Soup")
    ac_html += ac_category_html("hot", hot_items, "Горячее / Main")
    ac_html += ac_category_html("dessert", desserts, "Десерт / Dessert")
    ac_html += """
    <div class="alacarte-total">
      Итого / Total: <span id="acTotal">0.00€</span>
    </div>
    </div>"""

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

  <!-- MODE SWITCHER -->
  <div class="mode-switcher">
    <button type="button" class="mode-btn active" id="btnComplex" onclick="switchMode('complex')">
      🍱 Бизнес-ланч / Business lunch
    </button>
    <button type="button" class="mode-btn" id="btnAlacarte" onclick="switchMode('alacarte')">
      🍽 Блюда отдельно / À la carte
    </button>
  </div>

  <!-- ФОРМА БИЗНЕС-ЛАНЧ -->
  <form method="post" action="/order" autocomplete="on" id="formComplex">
    <input type="hidden" name="order_type" value="complex">

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
          {options_html(zakuski)}
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
          {options_html(desserts)}
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
      <textarea name="comment" rows="3" placeholder="🎁 Привёл коллегу? Напиши его имя здесь и получи квас или тархун в подарок! / Referred a colleague? Write their name here and get a free drink!"></textarea>
    </div>

    <button type="submit" class="btn-confirm" style="margin-top:22px;">
      Подтвердить заказ / Confirm order
    </button>
    <a href="/edit" class="btn-edit">Изменить или отменить заказ / Edit or cancel</a>
  </form>

  <!-- ФОРМА БЛЮДА ОТДЕЛЬНО -->
  <form method="post" action="/order" autocomplete="on" id="formAlacarte" style="display:none;">
    <input type="hidden" name="order_type" value="alacarte">

    <div class="row">
      <div>
        <label>Этаж / Floor</label>
        <select name="floor" required>
          <option value="">— выбери этаж / choose floor —</option>
          <option value="1st floor">1 этаж / 1st floor</option>
          <option value="6th floor">6 этаж / 6th floor</option>
        </select>
      </div>
      <div>
        <label>Дата доставки / Delivery date</label>
        <input class="order_date_ac" type="date" name="order_date" value="{d.isoformat()}" required>
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

    <p style="margin-top:18px; font-weight:800; color:var(--volga-burgundy); font-size:13px;">
      🌿 — сезонное блюдо / seasonal dish
    </p>

    {ac_html}

    <div class="row" style="margin-top:18px;">
      <div>
        <label>Напиток / Drink</label>
        <select name="drink">{drink_options}</select>
        <small>оплачивается отдельно / not included</small>
      </div>
      <div>
        <label>Хлеб / Bread</label>
        <select name="bread">
          <option value="">— без хлеба / no bread —</option>
          {options_html(BREAD_OPTIONS)}
        </select>
      </div>
    </div>

    <div class="comment-block">
      <label>Комментарий / Notes</label>
      <textarea name="comment" rows="3" placeholder="🎁 Привёл коллегу? Напиши его имя здесь и получи квас или тархун в подарок! / Referred a colleague? Write their name here and get a free drink!"></textarea>
    </div>

    <button type="submit" class="btn-confirm" style="margin-top:22px;" id="acSubmitBtn">
      Подтвердить заказ / Confirm order
    </button>
    <a href="/edit" class="btn-edit">Изменить или отменить заказ / Edit or cancel</a>
  </form>

</div>

<script>
/* MODE SWITCHER */
function switchMode(mode) {{
  var fc = document.getElementById('formComplex');
  var fa = document.getElementById('formAlacarte');
  var bc = document.getElementById('btnComplex');
  var ba = document.getElementById('btnAlacarte');
  if(mode==='complex') {{
    fc.style.display=''; fa.style.display='none';
    bc.classList.add('active'); ba.classList.remove('active');
  }} else {{
    fc.style.display='none'; fa.style.display='';
    ba.classList.add('active'); bc.classList.remove('active');
  }}
  try{{ localStorage.setItem('volga_mode', mode); }}catch(e){{}}
}}

/* Restore last mode */
(function(){{
  try{{
    var m = localStorage.getItem('volga_mode');
    if(m==='alacarte') switchMode('alacarte');
  }}catch(e){{}}
}})();

/* À la carte checkbox toggle */
function toggleAC(id) {{
  var cb = document.getElementById(id);
  var wrap = document.getElementById('wrap_' + id);
  cb.checked = !cb.checked;
  wrap.classList.toggle('selected', cb.checked);
  updateACTotal();
}}

/* Sync wrap style on direct checkbox click */
document.querySelectorAll('.alacarte-item input[type=checkbox]').forEach(function(cb){{
  cb.addEventListener('change', function(){{
    var wrap = document.getElementById('wrap_' + cb.id);
    if(wrap) wrap.classList.toggle('selected', cb.checked);
  }});
}});

function updateACTotal() {{
  var total = 0;
  document.querySelectorAll('#formAlacarte input[name="ac_item"]:checked').forEach(function(cb){{
    var parts = cb.value.split('|');
    total += parseFloat(parts[2]||0);
  }});
  // добавляем напиток
  var drinkSel = document.querySelector('#formAlacarte select[name="drink"]');
  if(drinkSel && drinkSel.value) {{
    var drinkPrices = {drink_prices_js};
    total += drinkPrices[drinkSel.value] || 0;
  }}
  document.getElementById('acTotal').textContent = total.toFixed(2) + '€';
}}

document.querySelector('#formAlacarte select[name="drink"]').addEventListener('change', updateACTotal);

/* À la carte validation */
document.getElementById('formAlacarte').addEventListener('submit', function(e){{
  var checked = document.querySelectorAll('#formAlacarte input[name="ac_item"]:checked');
  if(checked.length === 0){{
    e.preventDefault();
    e.stopImmediatePropagation();
    showVolgaPopup('Выберите хотя бы одно блюдо.<br><br>Please select at least one dish.');
  }}
}}, true);

/* COMPLEX form validations */
(function(){{
  var form = document.getElementById('formComplex');
  if(!form) return;

  // floor check
  form.addEventListener('submit', function(e){{
    var fl = document.getElementById('floor');
    if(!fl.value){{
      e.preventDefault(); e.stopImmediatePropagation();
      showVolgaPopup('Пожалуйста, выберите этаж.<br><br>Please choose a floor.');
      fl.focus(); return;
    }}
  }}, true);

  // dish rules
  form.addEventListener('submit', function(e){{
    var z = document.getElementById('zakuska');
    var s = document.getElementById('soup');
    var h = document.getElementById('hot');
    var d = document.getElementById('dessert');
    var hasZ=!!(z&&z.value), hasS=!!(s&&s.value), hasH=!!(h&&h.value), hasD=!!(d&&d.value);
    var cnt=(hasZ?1:0)+(hasS?1:0)+(hasH?1:0)+(hasD?1:0);
    if(!hasS){{ e.preventDefault(); e.stopImmediatePropagation(); showVolgaPopup('Любая опция включает суп.<br><br>All options come with soup.'); return; }}
    if(cnt!==3){{ e.preventDefault(); e.stopImmediatePropagation(); showVolgaPopup('Нужно выбрать 3 блюда.<br><br>Please select 3 dishes.'); return; }}
    var ok=(hasZ&&hasS&&hasD&&!hasH)||(!hasZ&&hasS&&hasH&&hasD)||(hasZ&&hasS&&hasH&&!hasD);
    if(!ok){{ e.preventDefault(); e.stopImmediatePropagation(); showVolgaPopup('Неверная комбинация блюд.<br><br>Wrong dish combination.'); }}
  }}, true);

  // date validation
  var dateInput = document.getElementById('order_date');
  var CUT = 12;
  function ymd(d){{ return d.getFullYear()+'-'+(''+(d.getMonth()+1)).padStart(2,'0')+'-'+(''+ d.getDate()).padStart(2,'0'); }}
  function isAllowed(d){{ return d.getDay()>=2&&d.getDay()<=5; }}
  function afterCutoff(n){{ return n.getHours()>CUT||(n.getHours()===CUT&&n.getMinutes()>0); }}
  function nextAllowed(d0){{ var x=new Date(d0); x.setDate(x.getDate()+1); while(!isAllowed(x)) x.setDate(x.getDate()+1); return x; }}
  function allowedDate(){{ var n=new Date(),t=new Date(n.getFullYear(),n.getMonth(),n.getDate()); return (!afterCutoff(n)&&isAllowed(t))?ymd(t):ymd(nextAllowed(t)); }}
  if(dateInput){{
    dateInput.addEventListener('change',function(){{
      if(dateInput.value!==allowedDate()){{ showVolgaPopup('Дата выбрана неверно.<br>До 12:00 — на сегодня, после — на следующий рабочий день.<br><br>Wrong date. Before 12:00 — today, after — next working day.'); dateInput.value=allowedDate(); }}
    }});
    form.addEventListener('submit',function(e){{ if(dateInput.value!==allowedDate()){{ e.preventDefault(); dateInput.value=allowedDate(); }} }});
  }}
}})();
</script>
"""
    return html_page(body, show_banner=True)


# ---------------------------
# POST /order
# ---------------------------
@app.post("/order")
def order():
    order_type = (request.form.get("order_type", "complex") or "complex").strip()

    d_str = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата.</p><p><a href='/'>Назад</a></p>"), 400

    floor = (request.form.get("floor", "") or "").strip() or None
    ok_floor, floor = validate_floor_for_office(floor)
    if not ok_floor:
        return html_page("<p class='danger'>Выберите этаж.</p><p><a href='/'>Назад</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        if is_closed_day(d):
            return html_page("<p class='danger'>В понедельник мы не работаем.</p><p><a href='/'>Назад</a></p>"), 403
        return html_page(
            f"<p class='danger'>Приём заказов закрыт.<br>"
            f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
            f"<p><a href='/'>Назад</a></p>"
        ), 403

    name = (request.form.get("name", "") or "").strip()
    phone_raw = (request.form.get("phone", "") or "").strip()
    phone_norm = normalize_phone(phone_raw)
    drink_code = (request.form.get("drink", "") or "").strip()
    if drink_code not in DRINK_PRICE:
        drink_code = ""
    drink_label_val = DRINK_LABEL.get(drink_code, "") if drink_code else None
    drink_price = float(DRINK_PRICE.get(drink_code, 0.0))
    bread = (request.form.get("bread", "") or "").strip() or None
    comment = (request.form.get("comment", "") or "").strip() or None

    if not name or not phone_norm:
        return html_page("<p class='danger'>Имя и телефон обязательны.</p><p><a href='/'>Назад</a></p>"), 400

    if order_type == "alacarte":
        # --- À LA CARTE ---
        ac_items_raw = request.form.getlist("ac_item")
        if not ac_items_raw:
            return html_page("<p class='danger'>Выберите хотя бы одно блюдо.</p><p><a href='/'>Назад</a></p>"), 400

        ac_items = []
        base_price = 0.0
        for raw in ac_items_raw:
            parts = raw.split("|")
            if len(parts) == 3:
                item_name, cat, price_str = parts
                try:
                    price_val = float(price_str)
                except ValueError:
                    price_val = 0.0
                ac_items.append({"name": item_name, "category": cat, "price": price_val})
                base_price += price_val

        total_price = compute_total_price(base_price, drink_code)

        # Собираем для отображения в подтверждении
        soup_val = next((i["name"] for i in ac_items if i["category"] == "soup"), None) or ""

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
                return html_page("<p class='danger'>Заказы на выбранную дату недоступны.</p><p><a href='/'>Назад</a></p>"), 409

            order_code = generate_order_code(conn, d)
            conn.execute("""
                INSERT INTO orders(order_code, office, order_date, floor, name, phone_raw, phone_norm,
                    zakuska, soup, hot, dessert, bread, drink_code, drink_label, drink_price_eur,
                    option_code, price_eur, comment, status, created_at, order_type, alacarte_items)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                order_code, OFFICE, d.isoformat(), floor,
                name, phone_raw, phone_norm,
                None, soup_val or None, None, None,
                bread, drink_code or None, drink_label_val, drink_price if drink_code else None,
                "alacarte", float(total_price), comment,
                "active", datetime.utcnow().isoformat(),
                "alacarte", json.dumps(ac_items, ensure_ascii=False)
            ))
            conn.commit()
        finally:
            conn.close()

        items_list = "".join([
            f"<li>{i['name'].replace(' 🌿','')} — <b>{i['price']:.2f}€</b></li>"
            for i in ac_items
        ])
        drink_line = f"{drink_label_val} (+{drink_price}€)" if drink_code else "—"

        return html_page(f"""
        <h2>✅ Заказ принят / Order confirmed</h2>
        <div class="card">
          <p><span class="pill"><b>{order_code}</b></span>
             <span class="pill">🍽 Блюда отдельно / À la carte</span></p>
          <p><b>{name}</b> — {OFFICE} — <span class="muted">{phone_raw}</span></p>
          <p>Этаж / Floor: <b>{floor or "—"}</b></p>
          <p>Дата доставки / Delivery date: <b>{d.isoformat()}</b> (13:00)</p>
          <p><span class="pill">Итого / Total: {total_price:.2f}€</span></p>
          <ul>{items_list}</ul>
          <li>Напиток / Drink: {drink_line}</li>
          <li>Хлеб / Bread: {bread or "—"}</li>
          <p class="muted">Комментарий / Notes: {comment or "—"}</p>
        </div>
        <p><a href="/">Новый заказ / New order</a></p>
        """)

    else:
        # --- COMPLEX ---
        zakuska = (request.form.get("zakuska", "") or "").strip() or None
        soup = (request.form.get("soup", "") or "").strip()
        hot = (request.form.get("hot", "") or "").strip() or None
        dessert = (request.form.get("dessert", "") or "").strip() or None

        if not soup:
            return html_page("<p class='danger'>Суп обязателен.</p><p><a href='/'>Назад</a></p>"), 400

        option_code, base_price, err = compute_option_base_price(zakuska, soup, hot, dessert, d)
        if err:
            return html_page(f"<p class='danger'>{err}</p><p><a href='/'>Назад</a></p>"), 400

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
                return html_page("<p class='danger'>Заказы на выбранную дату недоступны.</p><p><a href='/'>Назад</a></p>"), 409

            order_code = generate_order_code(conn, d)
            conn.execute("""
                INSERT INTO orders(order_code, office, order_date, floor, name, phone_raw, phone_norm,
                    zakuska, soup, hot, dessert, bread, drink_code, drink_label, drink_price_eur,
                    option_code, price_eur, comment, status, created_at, order_type, alacarte_items)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                order_code, OFFICE, d.isoformat(), floor,
                name, phone_raw, phone_norm,
                zakuska, soup, hot, dessert,
                bread, drink_code or None, drink_label_val, drink_price if drink_code else None,
                option_code, float(total_price), comment,
                "active", datetime.utcnow().isoformat(),
                "complex", None
            ))
            conn.commit()
        finally:
            conn.close()

        opt_human = {"opt1": "Опция 1", "opt2": "Опция 2", "opt3": "Опция 3"}.get(option_code, option_code)
        drink_line = f"{drink_label_val} (+{drink_price}€)" if drink_code else "—"

        return html_page(f"""
        <h2>✅ Заказ принят / Order confirmed</h2>
        <div class="card">
          <p><span class="pill"><b>{order_code}</b></span>
             <span class="pill">🍱 Бизнес-ланч</span></p>
          <p><b>{name}</b> — {OFFICE} — <span class="muted">{phone_raw}</span></p>
          <p>Этаж / Floor: <b>{floor or "—"}</b></p>
          <p>Дата доставки / Delivery date: <b>{d.isoformat()}</b> (13:00)</p>
          <p><span class="pill">{opt_human}</span><span class="pill">Итого / Total: {total_price:.2f}€</span></p>
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
        <p><a href="/">Новый заказ / New order</a></p>
        """)


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
    order_code_search = (request.args.get("code", "") or "").strip()

    found = None
    conn = db()
    ensure_columns(conn)
    if order_code_search:
        found = conn.execute("SELECT * FROM orders WHERE order_code=? AND status='active'", (order_code_search,)).fetchone()
    elif phone_norm:
        rows = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
            (OFFICE, d.isoformat(), phone_norm),
        ).fetchall()
        if len(rows) == 1:
            found = rows[0]
        elif len(rows) > 1:
            conn.close()
            items_html = []
            for r in rows:
                otype = '🍽 À la carte' if (r['order_type'] or '')=='alacarte' else '🍱 Бизнес-ланч'
                code = r['order_code']
                items_html.append(
                    f"<p><a href='/edit?date={d.isoformat()}&code={code}'>"
                    f"<b>{code}</b> — {r['name']} — {r['floor'] or '—'} — {otype}</a></p>"
                )
            items_html = "".join(items_html)
            return html_page(f"""
            <h1>Выберите заказ / Choose order</h1>
            <div class="card">
              <p>На {d.isoformat()} найдено несколько заказов с этого телефона:</p>
              {items_html}
              <p><a href="/edit">← Назад</a></p>
            </div>""")
    conn.close()

    ok_time, start, end, now_ = validate_order_time(d)

    if found:
        order_type = found["order_type"] or "complex"

        # À la carte edit
        if order_type == "alacarte":
            ac_items = json.loads(found["alacarte_items"] or "[]")
            items_list = "".join([
                f"<li>{i['name'].replace(' 🌿','')} — {i['price']:.2f}€</li>"
                for i in ac_items
            ])
            body = f"""
            <h1>Заказ / Order — 🍽 Блюда отдельно</h1>
            <div class="card">
              <p><span class="pill"><b>{found['order_code']}</b></span></p>
              <p><b>{found['name']}</b> — {OFFICE} — <span class="muted">{found['phone_raw']}</span></p>
              <p>Этаж: <b>{found['floor'] or '—'}</b> | Дата: <b>{d.isoformat()}</b></p>
              <p>Итого: <b>{found['price_eur']:.2f}€</b></p>
              <ul>{items_list}</ul>
              <p class="muted">{"<b>Окно закрыто — изменения недоступны.</b>" if not ok_time else ""}</p>
              <form method="post" action="/cancel" style="margin-top:12px;">
                <input type="hidden" name="order_date" value="{d.isoformat()}">
                <input type="hidden" name="order_code" value="{found['order_code']}">
                <button type="submit" class="btn-danger">Отменить заказ / Cancel</button>
              </form>
              <p style="margin-top:16px;"><a href="/">← На главную</a></p>
            </div>"""
            return html_page(body)

        # Complex edit
        soups = menu_for_category("soup", d)
        hot_items = menu_for_category("hot", d)
        zakuski = menu_for_category("zakuska", d)
        desserts = menu_for_category("dessert", d)

        fval = found["floor"] or ""
        drink_options = "".join([
            f"<option value='{k}' {'selected' if (found['drink_code'] or '')==(k or '') else ''}>{lbl}</option>"
            for (k, lbl, _) in DRINKS
        ])

        def sel_opts(items, current):
            out = ""
            for item in items:
                sel = "selected" if item == current else ""
                out += f"<option {sel}>{item}</option>"
            return out

        body = f"""
        <h1>Изменить / отменить заказ<br><small>Edit / cancel order</small></h1>
        <div class="card">
          <p><span class="pill"><b>{found['order_code']}</b></span>
             <span class="pill">Доставка: {d.isoformat()} 13:00</span></p>
          <p class="muted">Окно: <b>{start.strftime('%d.%m %H:%M')}</b> — <b>{end.strftime('%d.%m %H:%M')}</b>. Сейчас: <b>{now_.strftime('%d.%m %H:%M')}</b>.</p>
          {"<p class='danger'><b>Окно закрыто — изменения недоступны.</b></p>" if not ok_time else ""}

          <form method="post" action="/edit">
            <input type="hidden" name="order_date" value="{d.isoformat()}">
            <input type="hidden" name="order_code" value="{found['order_code']}">
            <label>Имя / Name</label>
            <input name="name" value="{found['name']}" required>
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
            <div class="row">
              <div>
                <label>Закуска / Starter</label>
                <select name="zakuska">
                  <option value="" {"selected" if not found["zakuska"] else ""}>— без закуски —</option>
                  {sel_opts(zakuski, found["zakuska"] or "")}
                </select>
              </div>
              <div>
                <label>Суп / Soup</label>
                <select name="soup" required>
                  <option value="">— выбери суп —</option>
                  {sel_opts(soups, found["soup"] or "")}
                </select>
              </div>
            </div>
            <div class="row">
              <div>
                <label>Горячее / Main</label>
                <select name="hot">
                  <option value="" {"selected" if not found["hot"] else ""}>— без горячего —</option>
                  {sel_opts(hot_items, found["hot"] or "")}
                </select>
              </div>
              <div>
                <label>Десерт / Dessert</label>
                <select name="dessert">
                  <option value="" {"selected" if not found["dessert"] else ""}>— без десерта —</option>
                  {sel_opts(desserts, found["dessert"] or "")}
                </select>
              </div>
            </div>
            <label>Напиток / Drink</label>
            <select name="drink">{drink_options}</select>
            <label style="margin-top:16px;">Хлеб / Bread</label>
            <select name="bread">
              <option value="" {"selected" if not found["bread"] else ""}>— без хлеба —</option>
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
          <p style="margin-top:16px;"><a href="/">← На главную</a></p>
        </div>"""
        return html_page(body)

    # Search form
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
        <label>Телефон / Phone</label>
        <input name="phone" value="{phone_raw}" required>
      </div>
    </div>
    <small style="margin-top:6px;">Если два заказа — выберите нужный из списка.</small>
    <button type="submit" class="btn-primary">Найти заказ / Find order</button>
  </form>
  <p class="muted" style="margin-top:12px;">Если заказ не найден — проверь дату и телефон.</p>
  <p><a href="/">← На главную</a></p>
</div>"""
    return html_page(body)


@app.post("/edit")
def edit_post():
    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Неверная дата.</p><p><a href='/edit'>Назад</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        return html_page(f"<p class='danger'>Окно редактирования закрыто.<br><small>{start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p><p><a href='/edit'>Назад</a></p>"), 403

    order_code_val = (request.form.get("order_code", "") or "").strip()
    conn = db()
    ensure_columns(conn)
    existing = conn.execute("SELECT * FROM orders WHERE order_code=? AND status='active'", (order_code_val,)).fetchone()
    if not existing:
        conn.close()
        return html_page("<p class='danger'>Заказ не найден.</p><p><a href='/edit'>Назад</a></p>"), 404

    name = (request.form.get("name", "") or "").strip()
    zakuska = (request.form.get("zakuska", "") or "").strip() or None
    soup = (request.form.get("soup", "") or "").strip()
    hot = (request.form.get("hot", "") or "").strip() or None
    dessert = (request.form.get("dessert", "") or "").strip() or None
    floor = (request.form.get("floor", "") or "").strip() or None
    ok_floor, floor = validate_floor_for_office(floor)
    if not ok_floor:
        conn.close()
        return html_page("<p class='danger'>Выберите этаж.</p><p><a href='/edit'>Назад</a></p>"), 400

    drink_code = (request.form.get("drink", "") or "").strip()
    if drink_code not in DRINK_PRICE:
        drink_code = ""
    drink_label_val = DRINK_LABEL.get(drink_code, "") if drink_code else None
    drink_price = float(DRINK_PRICE.get(drink_code, 0.0))
    bread = (request.form.get("bread", "") or "").strip() or None
    comment = (request.form.get("comment", "") or "").strip() or None

    option_code, base_price, err = compute_option_base_price(zakuska, soup, hot, dessert, d)
    if err:
        conn.close()
        return html_page(f"<p class='danger'>{err}</p><p><a href='/edit'>Назад</a></p>"), 400

    total_price = compute_total_price(base_price, drink_code)
    conn.execute("""
        UPDATE orders SET name=?, floor=?, zakuska=?, soup=?, hot=?, dessert=?,
            drink_code=?, drink_label=?, drink_price_eur=?,
            bread=?, option_code=?, price_eur=?, comment=?
        WHERE id=?
    """, (name, floor, zakuska, soup, hot, dessert,
          drink_code or None, drink_label_val, drink_price if drink_code else None,
          bread, option_code, float(total_price), comment, existing["id"]))
    conn.commit()
    conn.close()

    return html_page(f"""
    <h2>✅ Изменения сохранены / Saved</h2>
    <div class="card">
      <p><span class="pill"><b>{existing['order_code']}</b></span></p>
      <p><b>{name}</b> — {OFFICE}</p>
      <p>Этаж: <b>{floor or "—"}</b> | Дата: <b>{d.isoformat()}</b></p>
      <p>Итого: <b>{total_price:.2f}€</b></p>
    </div>
    <p><a href="/">← На главную</a></p>
    """)


@app.post("/cancel")
def cancel_post():
    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Неверная дата.</p><p><a href='/edit'>Назад</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        return html_page(f"<p class='danger'>Окно отмены закрыто.</p><p><a href='/edit'>Назад</a></p>"), 403

    order_code_val = (request.form.get("order_code", "") or "").strip()
    conn = db()
    ensure_columns(conn)
    existing = conn.execute("SELECT * FROM orders WHERE order_code=? AND status='active'", (order_code_val,)).fetchone()
    if not existing:
        conn.close()
        return html_page("<p class='danger'>Заказ не найден.</p><p><a href='/edit'>Назад</a></p>"), 404

    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (existing["id"],))
    conn.commit()
    conn.close()

    return html_page(f"""
    <h2>🗑 Заказ отменён / Order cancelled</h2>
    <div class="card">
      <p><span class="pill"><b>{existing['order_code']}</b></span></p>
      <p><b>{existing['name']}</b> — {OFFICE} — <span class="muted">{existing['phone_raw']}</span></p>
      <p>Дата: <b>{d.isoformat()}</b></p>
    </div>
    <p><a href="/">← На главную</a></p>
    """)


# ===========================
# Admin helpers
# ===========================
def _ru_only(s):
    s = "" if s is None else str(s)
    return s.split(" / ")[0].strip()

SHORT = {
    "Оливье":"Оливье","Винегрет":"Винегрет","Икра из баклажанов":"Икра",
    "Паштет из куриной печени":"Паштет","Шуба":"Шуба",
    "Борщ":"Борщ","Солянка сборная мясная":"Солянка","Куриный суп с лапшой и яйцом":"Кур.суп",
    "Куриные котлеты с пюре":"Котл+пюре","Куриные котлеты с гречкой":"Котл+греча",
    "Вареники с картошкой":"Вареники","Пельмени со сметаной":"Пельмени","Плов с бараниной (+3€)":"Плов",
    "Торт Наполеон":"Наполеон","Пирожное Картошка":"Картошка","Трубочка со сгущенкой":"Трубочка",
    "Белый":"Хлеб белый","Чёрный":"Хлеб чёрный",
}

def _short_name(s):
    ru = _ru_only(s)
    return SHORT.get(ru, ru)

def _fmt_money(x):
    try: return f"{float(x):.2f}€"
    except: return f"{x}€"

def _floor_norm(f):
    f = (f or "").strip()
    return f if f else "Без этажа"

def _floor_sort_key(k):
    kk = (k or "").lower()
    if "1st" in kk: return (0,1)
    if "6th" in kk: return (0,6)
    if "без" in kk: return (2,999)
    return (1,k)

def _rows_table_v2(rows):
    head = """<table class="admin-table"><thead><tr>
      <th>Код</th><th>Тип</th><th>Имя</th><th>Телефон</th><th>Этаж</th><th>Итого</th>
      <th>Суп</th><th>Закуска</th><th>Горячее</th><th>Десерт</th><th>Напиток</th><th>Хлеб</th><th>Комментарий</th>
    </tr></thead><tbody>"""
    if not rows:
        return head + "<tr><td colspan='13' class='muted'>—</td></tr></tbody></table>"
    body = ""
    for r in rows:
        drink = "—"
        if r["drink_label"]:
            dp = r["drink_price_eur"] or 0
            drink = f"{_ru_only(r['drink_label'])} (+{float(dp):.2f}€)"
        otype = "🍽 À la carte" if (r["order_type"] or "")=="alacarte" else "🍱 Ланч"

        # Для à la carte показываем список блюд в колонке sup/zakuska/hot/dessert
        if (r["order_type"] or "") == "alacarte":
            ac = json.loads(r["alacarte_items"] or "[]")
            by_cat = {}
            for i in ac:
                by_cat.setdefault(i["category"], []).append(i["name"].replace(" 🌿",""))
            soup_d = ", ".join(by_cat.get("soup",[]) or ["—"])
            zak_d  = ", ".join(by_cat.get("zakuska",[]) or ["—"])
            hot_d  = ", ".join(by_cat.get("hot",[]) or ["—"])
            des_d  = ", ".join(by_cat.get("dessert",[]) or ["—"])
        else:
            soup_d = _short_name(r["soup"]) if r["soup"] else "—"
            zak_d  = _short_name(r["zakuska"]) if r["zakuska"] else "—"
            hot_d  = _short_name(r["hot"]) if r["hot"] else "—"
            des_d  = _short_name(r["dessert"]) if r["dessert"] else "—"

        body += f"""<tr>
          <td><b>{r['order_code']}</b></td><td>{otype}</td>
          <td>{r['name']}</td><td>{r['phone_raw']}</td>
          <td>{_floor_norm(r['floor'])}</td><td><b>{_fmt_money(r['price_eur'])}</b></td>
          <td>{soup_d}</td><td>{zak_d}</td><td>{hot_d}</td><td>{des_d}</td>
          <td>{drink}</td><td>{_short_name(r['bread']) if r['bread'] else '—'}</td>
          <td>{r['comment'] or '—'}</td>
        </tr>"""
    return head + body + "</tbody></table>"

def _summary_counts(rows):
    opt_counts = {"opt1":0,"opt2":0,"opt3":0,"alacarte":0}
    dish_counts = {}
    drink_counts = {}
    for r in rows:
        ot = r["order_type"] or "complex"
        if ot == "alacarte":
            opt_counts["alacarte"] += 1
            ac = json.loads(r["alacarte_items"] or "[]")
            for i in ac:
                nm = i["name"].replace(" 🌿","")
                dish_counts[_short_name(nm)] = dish_counts.get(_short_name(nm),0)+1
        else:
            if r["option_code"] in opt_counts:
                opt_counts[r["option_code"]] += 1
            for k in ["soup","zakuska","hot","dessert","bread"]:
                v = r[k]
                if v:
                    vv = _short_name(v)
                    dish_counts[vv] = dish_counts.get(vv,0)+1
        if r["drink_label"]:
            dd = _ru_only(r["drink_label"])
            drink_counts[dd] = drink_counts.get(dd,0)+1
    return opt_counts, dish_counts, drink_counts

def _simple_table(title, counts):
    rows_html = "".join([f"<tr><td>{k}</td><td style='text-align:right;'><b>{v}</b></td></tr>" for k,v in sorted(counts.items(),key=lambda x:(-x[1],x[0]))])
    if not rows_html: rows_html = "<tr><td colspan='2' class='muted'>—</td></tr>"
    return f"""<div class="card"><h3 style="margin:0 0 10px 0;">{title}</h3>
    <table class="admin-table"><thead><tr><th>Позиция</th><th style="text-align:right;">Кол-во</th></tr></thead>
    <tbody>{rows_html}</tbody></table></div>"""

def _active_by_floor(rows):
    g = {}
    for r in rows:
        k = _floor_norm(r["floor"])
        g.setdefault(k,[]).append(r)
    return g


# ===========================
# Admin routes
# ===========================
@app.get("/admin")
def admin_v2():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403

    d_str = request.args.get("date", date.today().isoformat())
    try: d = date.fromisoformat(d_str)
    except: d = date.today()

    conn = db()
    ensure_columns(conn)
    active_rows = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND status='active' ORDER BY created_at ASC",
        (OFFICE, d.isoformat()),
    ).fetchall()
    cancelled_rows = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND status='cancelled' ORDER BY created_at ASC",
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
          <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;flex-wrap:wrap;">
            <h3 style="margin:0;">Активные — {floor_name}</h3>
            <div class="muted" style="font-weight:800;">{len(rr)} шт.</div>
          </div>
          <div class="no-print" style="margin-top:10px;">
            <a class="btn-primary" style="width:auto;display:inline-block;margin-top:0;padding:8px 16px;"
               href="/admin/print?date={d.isoformat()}&floor={floor_name}&token={ADMIN_TOKEN}">
              🖨 Печать: {floor_name}
            </a>
          </div>
          {_rows_table_v2(rr)}
        </div>"""

    complex_cnt = sum(1 for r in active_rows if (r["order_type"] or "complex")=="complex")
    alacarte_cnt = sum(1 for r in active_rows if (r["order_type"] or "complex")=="alacarte")

    body = f"""
    <h1>Админка — {OFFICE}</h1>
    <div class="card">
      <form method="get" action="/admin">
        <input type="hidden" name="token" value="{ADMIN_TOKEN}">
        <div class="row">
          <div><label>Дата</label><input type="date" name="date" value="{d.isoformat()}"></div>
          <div></div>
        </div>
        <button class="btn-primary" type="submit">Показать</button>
      </form>
      <p style="margin-top:14px;">
        <a href="/export.csv?date={d.isoformat()}&token={ADMIN_TOKEN}">⬇️ CSV</a> &nbsp;|&nbsp;
        <a href="/admin/print?date={d.isoformat()}&token={ADMIN_TOKEN}">🖨 Печать всех</a> &nbsp;|&nbsp;
        <a href="/admin/summary?date={d.isoformat()}&token={ADMIN_TOKEN}">🧾 Сводка</a> &nbsp;|&nbsp;
        <a href="/admin/specials?date={d.isoformat()}&token={ADMIN_TOKEN}">⭐ Блюдо недели</a> &nbsp;|&nbsp;
        <a href="/admin/soups?token={ADMIN_TOKEN}">🍲 Супы</a> &nbsp;|&nbsp;
        <a href="/admin/seasonal?token={ADMIN_TOKEN}">🌿 Сезонные блюда</a> &nbsp;|&nbsp;
        <a href="/admin/alacarte_prices?token={ADMIN_TOKEN}">💰 Цены à la carte</a>
      </p>
      <p>
        <span class="pill">Всего активных: {len(active_rows)}</span>
        <span class="pill">🍱 Бизнес-ланч: {complex_cnt}</span>
        <span class="pill">🍽 À la carte: {alacarte_cnt}</span>
        <span class="pill">Опция 1: {opt_counts.get('opt1',0)}</span>
        <span class="pill">Опция 2: {opt_counts.get('opt2',0)}</span>
        <span class="pill">Опция 3: {opt_counts.get('opt3',0)}</span>
      </p>
    </div>
    {active_html}
    <div class="card"><h3>Отменённые</h3>{_rows_table_v2(cancelled_rows)}</div>
    {_simple_table("Сводка по блюдам (активные)", dish_counts)}
    {_simple_table("Сводка по напиткам (активные)", drink_counts)}
    """
    return html_page(body)


@app.get("/admin/summary")
def admin_summary_v2():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    d_str = request.args.get("date", date.today().isoformat())
    try: d = date.fromisoformat(d_str)
    except: d = date.today()

    conn = db()
    ensure_columns(conn)
    rows = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND status='active'",
        (OFFICE, d.isoformat()),
    ).fetchall()
    conn.close()

    dish_counts = {}
    drink_counts = {}
    for r in rows:
        if (r["order_type"] or "complex") == "alacarte":
            ac = json.loads(r["alacarte_items"] or "[]")
            for i in ac:
                nm = i["name"].replace(" 🌿","")
                dish_counts[_short_name(nm)] = dish_counts.get(_short_name(nm),0)+1
        else:
            for k in ["soup","zakuska","hot","dessert","bread"]:
                v = r[k]
                if v:
                    vv = _short_name(v)
                    dish_counts[vv] = dish_counts.get(vv,0)+1
        if r["drink_label"]:
            dd = _ru_only(r["drink_label"])
            drink_counts[dd] = drink_counts.get(dd,0)+1

    body = f"""
    <h1>Сводка — {OFFICE} — {d.isoformat()}</h1>
    <div class="card no-print">
      <a class="btn-primary" style="width:auto;display:inline-block;margin-top:0;padding:8px 16px;"
         href="/admin?date={d.isoformat()}&token={ADMIN_TOKEN}">← Назад</a>
      <button class="btn-primary" style="width:auto;display:inline-block;margin-top:0;margin-left:10px;padding:8px 16px;"
              onclick="window.print()">🖨 Печать</button>
    </div>
    {_simple_table("Блюда (активные)", dish_counts)}
    {_simple_table("Напитки (активные)", drink_counts)}
    """
    return html_page(body)


@app.get("/admin/print")
def admin_print_active_v2():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    d_str = request.args.get("date", date.today().isoformat())
    try: d = date.fromisoformat(d_str)
    except: d = date.today()
    floor_filter = (request.args.get("floor","") or "").strip()

    conn = db()
    ensure_columns(conn)
    if floor_filter:
        rows = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND status='active' AND COALESCE(floor,'')=? ORDER BY created_at ASC",
            (OFFICE, d.isoformat(), floor_filter if floor_filter!="Без этажа" else ""),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND status='active' ORDER BY created_at ASC",
            (OFFICE, d.isoformat()),
        ).fetchall()
    conn.close()

    title = "Активные заказы" + (f" — {floor_filter}" if floor_filter else "")
    body = f"""
    <h1 style="text-align:center;">{title}</h1>
    <p style="text-align:center;font-weight:800;">Офис: {OFFICE} | Дата: {d.isoformat()}</p>
    <div class="card">
      {_rows_table_v2(rows)}
      <div class="no-print" style="margin-top:14px;display:flex;gap:10px;">
        <button class="btn-primary" onclick="window.print()">🖨 Печать</button>
        <a class="btn-danger" href="/admin?date={d.isoformat()}&token={ADMIN_TOKEN}">← Назад</a>
      </div>
    </div>"""
    return html_page(body)


# --- Specials ---
@app.get("/admin/specials")
def admin_specials_get():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    d_str = request.args.get("date", date.today().isoformat())
    try: d = date.fromisoformat(d_str)
    except: d = date.today()

    conn = db()
    rows = conn.execute("SELECT * FROM weekly_special WHERE office=? ORDER BY id DESC LIMIT 30", (OFFICE,)).fetchall()
    conn.close()

    list_html = ""
    for r in rows:
        list_html += f"""<tr>
          <td><b>{r['id']}</b></td>
          <td>{r['start_date']} → {r['end_date']}</td>
          <td>{r['title']}</td>
          <td style="text-align:right;">+{int(r['surcharge_eur'])}€</td>
          <td style="text-align:right;">{float(r['alacarte_price_eur'] or 0):.2f}€</td>
          <td>
            <form method="post" action="/admin/specials/delete?token={ADMIN_TOKEN}" onsubmit="return confirm('Удалить?');">
              <input type="hidden" name="id" value="{r['id']}">
              <input type="hidden" name="date" value="{d.isoformat()}">
              <button class="btn-danger" style="margin-top:0;padding:6px 12px;font-size:13px;" type="submit">Удалить</button>
            </form>
          </td>
        </tr>"""
    if not list_html:
        list_html = "<tr><td colspan='6' class='muted'>—</td></tr>"

    body = f"""
    <h1>Блюдо недели</h1>
    <div class="card">
      <p><a href="/admin?date={d.isoformat()}&token={ADMIN_TOKEN}">← Назад в админку</a></p>
    </div>
    <div class="card">
      <h3>Добавить блюдо недели</h3>
      <form method="post" action="/admin/specials/create?token={ADMIN_TOKEN}">
        <div class="row">
          <div><label>Начало</label><input type="date" name="start_date" value="{d.isoformat()}" required></div>
          <div><label>Конец</label><input type="date" name="end_date" value="{(d+timedelta(days=6)).isoformat()}" required></div>
        </div>
        <label>Название блюда (горячее)</label>
        <input name="title" placeholder="Напр. Бефстроганов" required>
        <div class="row">
          <div>
            <label>Доплата в комплексе, €</label>
            <input name="surcharge_eur" type="number" min="0" step="1" value="0" required>
          </div>
          <div>
            <label>Цена à la carte, €</label>
            <input name="alacarte_price_eur" type="number" min="0" step="0.5" value="0" required>
            <small>Цена при заказе блюда отдельно</small>
          </div>
        </div>
        <button class="btn-primary" type="submit">Сохранить</button>
      </form>
    </div>
    <div class="card">
      <h3>Записи</h3>
      <table class="admin-table">
        <thead><tr><th>ID</th><th>Период</th><th>Название</th><th style="text-align:right;">Доплата</th><th style="text-align:right;">À la carte</th><th>Действия</th></tr></thead>
        <tbody>{list_html}</tbody>
      </table>
    </div>"""
    return html_page(body)


@app.post("/admin/specials/create")
def admin_specials_create_post():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    try:
        start_date = date.fromisoformat((request.form.get("start_date","") or "").strip())
        end_date = date.fromisoformat((request.form.get("end_date","") or "").strip())
    except ValueError:
        return html_page("<p class='danger'>Неверные даты.</p>"), 400
    if end_date < start_date:
        return html_page("<p class='danger'>Конец раньше начала.</p>"), 400
    title = (request.form.get("title","") or "").strip()
    if not title:
        return html_page("<p class='danger'>Пустое название.</p>"), 400
    try:
        surcharge = int(request.form.get("surcharge_eur","0"))
        alacarte_price = float(request.form.get("alacarte_price_eur","0"))
        if surcharge < 0 or alacarte_price < 0: raise ValueError
    except ValueError:
        return html_page("<p class='danger'>Неверные цены.</p>"), 400

    conn = db()
    conn.execute(
        "INSERT INTO weekly_special(office,start_date,end_date,title,surcharge_eur,alacarte_price_eur,created_at) VALUES(?,?,?,?,?,?,?)",
        (OFFICE, start_date.isoformat(), end_date.isoformat(), title, surcharge, alacarte_price, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return redirect(f"/admin/specials?date={start_date.isoformat()}&token={ADMIN_TOKEN}")


@app.post("/admin/specials/delete")
def admin_specials_delete_post():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    try: sid = int(request.form.get("id","0"))
    except: sid = 0
    if sid > 0:
        conn = db()
        conn.execute("DELETE FROM weekly_special WHERE id=?", (sid,))
        conn.commit()
        conn.close()
    d = (request.form.get("date", date.today().isoformat()) or "").strip()
    return redirect(f"/admin/specials?date={d}&token={ADMIN_TOKEN}")


# --- Soups ---
@app.get("/admin/soups")
def admin_soups_get():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    conn = db()
    rows = conn.execute("SELECT * FROM admin_soups ORDER BY sort_order ASC, id ASC").fetchall()
    conn.close()

    list_html = ""
    for r in rows:
        en = r["title_en"] or ""
        display = f"{r['title_ru']} / {en}" if en else r["title_ru"]
        list_html += f"""<tr>
          <td><b>{r['id']}</b></td><td>{display}</td>
          <td style="text-align:center;">{'✅' if r['active'] else '❌'}</td>
          <td style="text-align:center;">{r['sort_order']}</td>
          <td>
            <form method="post" action="/admin/soups/toggle?token={ADMIN_TOKEN}" style="display:inline;">
              <input type="hidden" name="id" value="{r['id']}">
              <button class="btn-primary" style="margin-top:0;padding:6px 12px;font-size:13px;" type="submit">
                {'Скрыть' if r['active'] else 'Показать'}
              </button>
            </form>
            <form method="post" action="/admin/soups/delete?token={ADMIN_TOKEN}" style="display:inline;" onsubmit="return confirm('Удалить?');">
              <input type="hidden" name="id" value="{r['id']}">
              <button class="btn-danger" style="margin-top:0;padding:6px 12px;font-size:13px;" type="submit">Удалить</button>
            </form>
          </td>
        </tr>"""
    if not list_html:
        list_html = "<tr><td colspan='5' class='muted'>Нет дополнительных супов (используются базовые)</td></tr>"

    body = f"""
    <h1>Управление супами</h1>
    <div class="card"><p><a href="/admin?token={ADMIN_TOKEN}">← Назад в админку</a></p>
    <p class="muted">Базовые три супа всегда показываются. Здесь добавляются дополнительные.</p></div>
    <div class="card">
      <h3>Добавить суп</h3>
      <form method="post" action="/admin/soups/create?token={ADMIN_TOKEN}">
        <div class="row">
          <div><label>Название (RU) *</label><input name="title_ru" placeholder="Окрошка" required></div>
          <div><label>Название (EN)</label><input name="title_en" placeholder="Okroshka"></div>
        </div>
        <label>Порядок (0 = первый)</label>
        <input name="sort_order" type="number" value="0" min="0" style="max-width:120px;">
        <button class="btn-primary" type="submit">Добавить</button>
      </form>
    </div>
    <div class="card">
      <h3>Дополнительные супы ({len(rows)} шт.)</h3>
      <table class="admin-table">
        <thead><tr><th>ID</th><th>Название</th><th style="text-align:center;">Активен</th><th style="text-align:center;">Порядок</th><th>Действия</th></tr></thead>
        <tbody>{list_html}</tbody>
      </table>
    </div>"""
    return html_page(body)


@app.post("/admin/soups/create")
def admin_soups_create():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    title_ru = (request.form.get("title_ru","") or "").strip()
    title_en = (request.form.get("title_en","") or "").strip()
    if not title_ru:
        return html_page("<p class='danger'>Название обязательно.</p>"), 400
    try: sort_order = max(0, int(request.form.get("sort_order","0")))
    except: sort_order = 0
    conn = db()
    conn.execute("INSERT INTO admin_soups(title_ru,title_en,sort_order,active,created_at) VALUES(?,?,?,1,?)",
                 (title_ru, title_en, sort_order, datetime.utcnow().isoformat()))
    conn.commit(); conn.close()
    return redirect(f"/admin/soups?token={ADMIN_TOKEN}")


@app.post("/admin/soups/toggle")
def admin_soups_toggle():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    try: sid = int(request.form.get("id","0"))
    except: sid = 0
    if sid > 0:
        conn = db()
        row = conn.execute("SELECT active FROM admin_soups WHERE id=?", (sid,)).fetchone()
        if row:
            conn.execute("UPDATE admin_soups SET active=? WHERE id=?", (0 if row["active"] else 1, sid))
            conn.commit()
        conn.close()
    return redirect(f"/admin/soups?token={ADMIN_TOKEN}")


@app.post("/admin/soups/delete")
def admin_soups_delete():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    try: sid = int(request.form.get("id","0"))
    except: sid = 0
    if sid > 0:
        conn = db(); conn.execute("DELETE FROM admin_soups WHERE id=?", (sid,)); conn.commit(); conn.close()
    return redirect(f"/admin/soups?token={ADMIN_TOKEN}")


# --- Seasonal items ---
@app.get("/admin/seasonal")
def admin_seasonal_get():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    conn = db()
    rows = conn.execute("SELECT * FROM seasonal_items ORDER BY category ASC, id ASC").fetchall()
    conn.close()

    list_html = ""
    for r in rows:
        en = r["title_en"] or ""
        display = f"{r['title_ru']} / {en}" if en else r["title_ru"]
        list_html += f"""<tr>
          <td><b>{r['id']}</b></td>
          <td>{CAT_NAMES.get(r['category'], r['category'])}</td>
          <td>{display}</td>
          <td style="text-align:right;">{float(r['alacarte_price_eur']):.2f}€</td>
          <td style="text-align:center;">{'✅' if r['active'] else '❌'}</td>
          <td>
            <form method="post" action="/admin/seasonal/toggle?token={ADMIN_TOKEN}" style="display:inline;">
              <input type="hidden" name="id" value="{r['id']}">
              <button class="btn-primary" style="margin-top:0;padding:6px 12px;font-size:13px;" type="submit">
                {'Скрыть' if r['active'] else 'Показать'}
              </button>
            </form>
            <form method="post" action="/admin/seasonal/delete?token={ADMIN_TOKEN}" style="display:inline;" onsubmit="return confirm('Удалить?');">
              <input type="hidden" name="id" value="{r['id']}">
              <button class="btn-danger" style="margin-top:0;padding:6px 12px;font-size:13px;" type="submit">Удалить</button>
            </form>
          </td>
        </tr>"""
    if not list_html:
        list_html = "<tr><td colspan='6' class='muted'>Нет сезонных блюд</td></tr>"

    cat_opts = "".join([f"<option value='{k}'>{v}</option>" for k, v in CAT_NAMES.items()])

    body = f"""
    <h1>🌿 Сезонные блюда</h1>
    <div class="card">
      <p><a href="/admin?token={ADMIN_TOKEN}">← Назад в админку</a></p>
      <p class="muted">Сезонные блюда помечаются 🌿 и появляются в форме заказа в своей категории — и в бизнес-ланче, и в «Блюдах отдельно».</p>
    </div>
    <div class="card">
      <h3>Добавить сезонное блюдо</h3>
      <form method="post" action="/admin/seasonal/create?token={ADMIN_TOKEN}">
        <div class="row">
          <div><label>Категория</label><select name="category" required>{cat_opts}</select></div>
          <div><label>Цена à la carte, €</label>
               <input name="alacarte_price_eur" type="number" min="0" step="0.5" value="0" required>
               <small>Цена при заказе блюда отдельно</small></div>
        </div>
        <div class="row">
          <div><label>Название (RU) *</label><input name="title_ru" placeholder="Окрошка" required></div>
          <div><label>Название (EN)</label><input name="title_en" placeholder="Okroshka"></div>
        </div>
        <button class="btn-primary" type="submit">Добавить блюдо</button>
      </form>
    </div>
    <div class="card">
      <h3>Сезонные блюда ({len(rows)} шт.)</h3>
      <table class="admin-table">
        <thead><tr><th>ID</th><th>Категория</th><th>Название</th><th style="text-align:right;">À la carte</th><th style="text-align:center;">Активно</th><th>Действия</th></tr></thead>
        <tbody>{list_html}</tbody>
      </table>
    </div>"""
    return html_page(body)


@app.post("/admin/seasonal/create")
def admin_seasonal_create():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    category = (request.form.get("category","") or "").strip()
    if category not in CAT_NAMES:
        return html_page("<p class='danger'>Неверная категория.</p>"), 400
    title_ru = (request.form.get("title_ru","") or "").strip()
    title_en = (request.form.get("title_en","") or "").strip()
    if not title_ru:
        return html_page("<p class='danger'>Название обязательно.</p>"), 400
    try:
        price = float(request.form.get("alacarte_price_eur","0"))
        if price < 0: raise ValueError
    except ValueError:
        return html_page("<p class='danger'>Неверная цена.</p>"), 400
    conn = db()
    conn.execute(
        "INSERT INTO seasonal_items(category,title_ru,title_en,alacarte_price_eur,active,created_at) VALUES(?,?,?,?,1,?)",
        (category, title_ru, title_en, price, datetime.utcnow().isoformat())
    )
    conn.commit(); conn.close()
    return redirect(f"/admin/seasonal?token={ADMIN_TOKEN}")


@app.post("/admin/seasonal/toggle")
def admin_seasonal_toggle():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    try: sid = int(request.form.get("id","0"))
    except: sid = 0
    if sid > 0:
        conn = db()
        row = conn.execute("SELECT active FROM seasonal_items WHERE id=?", (sid,)).fetchone()
        if row:
            conn.execute("UPDATE seasonal_items SET active=? WHERE id=?", (0 if row["active"] else 1, sid))
            conn.commit()
        conn.close()
    return redirect(f"/admin/seasonal?token={ADMIN_TOKEN}")


@app.post("/admin/seasonal/delete")
def admin_seasonal_delete():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403
    try: sid = int(request.form.get("id","0"))
    except: sid = 0
    if sid > 0:
        conn = db(); conn.execute("DELETE FROM seasonal_items WHERE id=?", (sid,)); conn.commit(); conn.close()
    return redirect(f"/admin/seasonal?token={ADMIN_TOKEN}")


# --- À la carte prices (per dish) ---

def _all_menu_items_for_alacarte(d=None):
    """
    Возвращает список (category, item_label, item_key) для всех блюд меню
    кроме сезонных и блюда недели (у них своя цена).
    """
    if d is None:
        d = date.today()
    items = []
    # zakuska
    for item in MENU["zakuska"]:
        items.append(("zakuska", item, make_item_key(item)))
    # soups — base + admin_soups
    for item in get_soups_list():
        if not item.endswith(" 🌿"):
            items.append(("soup", item, make_item_key(item)))
    # hot — base only (special has own price)
    for item in MENU["hot"]:
        items.append(("hot", item, make_item_key(item)))
    # dessert
    for item in MENU["dessert"]:
        items.append(("dessert", item, make_item_key(item)))
    return items


@app.get("/admin/alacarte_prices")
def admin_alacarte_prices_get():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403

    conn = db()
    rows = conn.execute("SELECT item_key, price_eur FROM alacarte_prices").fetchall()
    conn.close()
    saved = {r["item_key"]: float(r["price_eur"]) for r in rows}

    all_items = _all_menu_items_for_alacarte()

    # Group by category for display
    cats_html = ""
    prev_cat = None
    for cat, label, key in all_items:
        if cat != prev_cat:
            if prev_cat is not None:
                cats_html += "</div>"
            cats_html += f"""
            <div class="card" style="margin-bottom:12px;">
              <h3 style="margin:0 0 16px 0; color:var(--volga-blue);">{CAT_NAMES.get(cat, cat)}</h3>
            """
            prev_cat = cat

        price = saved.get(key, 0.0)
        # Show only RU part for cleaner display
        display = label.split(" / ")[0] if " / " in label else label

        cats_html += f"""
        <div style="display:flex; align-items:center; justify-content:space-between;
                    gap:12px; padding:10px 0; border-bottom:1px solid #e8e0d0; flex-wrap:wrap;">
          <div style="font-size:14px; color:var(--volga-blue); font-weight:600; flex:1; min-width:180px;">
            {display}
          </div>
          <div style="display:flex; align-items:center; gap:8px;">
            <input type="number" name="price_{key}" value="{price:.2f}"
                   min="0" step="0.5"
                   style="width:100px; padding:8px; font-size:15px; font-weight:700;
                          text-align:right; border:2px solid var(--volga-blue); background:var(--volga-bg);">
            <span style="font-size:14px; font-weight:800; color:var(--volga-blue);">€</span>
          </div>
        </div>
        <input type="hidden" name="key_{key}" value="{key}">
        <input type="hidden" name="cat_{key}" value="{cat}">
        """

    if prev_cat:
        cats_html += "</div>"

    # Weekly special note
    special_note = """
    <div class="card" style="background:var(--volga-bg);">
      <p style="font-size:13px; color:var(--volga-burgundy);">
        ⭐ <b>Блюдо недели</b> — цена à la carte задаётся отдельно в разделе
        <a href="/admin/specials?token=__TOKEN__">«Блюдо недели»</a>.<br>
        🌿 <b>Сезонные блюда</b> — цена à la carte задаётся при добавлении в
        <a href="/admin/seasonal?token=__TOKEN__">«Сезонные блюда»</a>.
      </p>
    </div>
    """.replace("__TOKEN__", ADMIN_TOKEN)

    body = f"""
    <h1>💰 Цены «Блюда отдельно»</h1>
    <div class="card">
      <p><a href="/admin?token={ADMIN_TOKEN}">← Назад в админку</a></p>
      <p class="muted" style="margin-top:8px;">
        Установите цену для каждого блюда. Цена 0,00€ означает что блюдо показывается бесплатно —
        не забудьте заполнить перед запуском.
      </p>
    </div>

    <form method="post" action="/admin/alacarte_prices/save?token={ADMIN_TOKEN}">
      {cats_html}
      <div class="card">
        <button class="btn-primary" type="submit" style="max-width:300px;">
          💾 Сохранить все цены
        </button>
      </div>
    </form>

    {special_note}
    """
    return html_page(body)


@app.post("/admin/alacarte_prices/save")
def admin_alacarte_prices_save():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2>"), 403

    all_items = _all_menu_items_for_alacarte()
    keys = {key for _, _, key in all_items}

    conn = db()
    for key in keys:
        price_str = request.form.get(f"price_{key}", "0")
        cat = request.form.get(f"cat_{key}", "")
        try:
            price = float(price_str)
            if price < 0: raise ValueError
        except ValueError:
            conn.close()
            return html_page(f"<p class='danger'>Неверная цена для {key}.</p>"), 400
        conn.execute(
            "INSERT OR REPLACE INTO alacarte_prices(item_key, category, price_eur) VALUES(?,?,?)",
            (key, cat, price)
        )
    conn.commit()
    conn.close()
    return redirect(f"/admin/alacarte_prices?token={ADMIN_TOKEN}")


# --- CSV ---
@app.get("/export.csv")
def export_csv():
    if not check_admin():
        return Response("Forbidden", status=403)
    d_str = request.args.get("date", date.today().isoformat())
    try: d = date.fromisoformat(d_str)
    except: d = date.today()

    conn = db()
    ensure_columns(conn)
    rows = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND status='active' ORDER BY created_at ASC",
        (OFFICE, d.isoformat()),
    ).fetchall()
    conn.close()

    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["order_code","order_type","office","order_date","floor","name","phone_raw",
                     "zakuska","soup","hot","dessert","alacarte_items","drink_label","bread",
                     "option_code","price_eur","comment","created_at"])
    for r in rows:
        writer.writerow([
            r["order_code"], r["order_type"] or "complex", r["office"], r["order_date"],
            r["floor"] or "", r["name"], r["phone_raw"],
            r["zakuska"] or "", r["soup"] or "", r["hot"] or "", r["dessert"] or "",
            r["alacarte_items"] or "",
            r["drink_label"] or "", r["bread"] or "",
            r["option_code"] or "", r["price_eur"], r["comment"] or "", r["created_at"],
        ])

    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=orders_{OFFICE}_{d.isoformat()}.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","5000")), debug=True)
