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
DB_PATH = os.getenv("DB_PATH", "/tmp/orders.sqlite")  # без диска ставь /tmp/...
TZ = ZoneInfo(os.getenv("TZ", "Europe/Madrid"))

MAX_PER_DAY = int(os.getenv("MAX_PER_DAY", "30"))
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", "11"))  # 11:00
ORDER_PREFIX = os.getenv("ORDER_PREFIX", "VO")

OFFICES = ["ALAMEDA", "MUSICA"]

# --- Меню: RU / EN ---
MENU = {
    "zakuska": [
        "Оливье / Olivier salad",
        "Винегрет / Vinaigrette beet salad",
        "Икра из баклажанов / Eggplant caviar",
        "Паштет из куриной печени / Chicken liver pâté",
        "Шуба / Herring under a fur coat",
    ],
    "soup": [
        "Борщ / Borscht",
        "Солянка сборная мясная / Meat solyanka",
        "Куриный с домашней лапшой и яйцом / Chicken soup with noodles & egg",
    ],
    "hot": [
        "Куриные котлеты с пюре / Chicken cutlets with mash",
        "Куриные котлеты с гречкой / Chicken cutlets with buckwheat",
        "Вареники с картошкой / Potato vareniki",
        "Пельмени со сметаной / Pelmeni with sour cream",
        "Плов с бараниной (+3€) / Lamb plov (+3€)",
    ],
    "dessert": [
        "Торт Наполеон / Napoleon cake",
        "Пирожное Картошка / Chocolate “Kartoshka” cake",
        "Трубочка со сгущенкой / Puff pastry roll with condensed milk",
    ],
}

PRICES = {"opt1": 15.0, "opt2": 16.0, "opt3": 17.0}
PLOV_SURCHARGE = 3.0

BREAD_OPTIONS = ["Белый / White", "Чёрный / Black"]

# --- Напитки (дополнительно) ---
# value -> (label, price)
DRINKS = [
    ("", "— без напитка / no drink —", 0.0),
    ("kvass", "Квас / Kvass", 3.5),
    ("mors", "Морс / Berry drink (Mors)", 4.0),
    ("water", "Вода / Water", 2.2),
    ("tea_black", "Чай чёрный с чабрецом (сашет) / Black tea with thyme (sachet)", 3.5),
    ("tea_green", "Чай зелёный (сашет) / Green tea (sachet)", 3.5),
    ("tea_herbal", "Чай травяной (сашет) / Herbal tea (sachet)", 3.5),
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
    # migrations (safe)
    if "drink_code" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_code TEXT")
    if "drink_label" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_label TEXT")
    if "drink_price_eur" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_price_eur REAL")
    # price_eur already exists; it will now store TOTAL including drink


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT NOT NULL UNIQUE,
            office TEXT NOT NULL,
            order_date TEXT NOT NULL,

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
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_office_date_phone_norm
        ON orders(office, order_date, phone_norm)
        """
    )

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


def ordering_window_for(d: date):
    # window: [11:00 (D-1), 11:00 D)
    start = cutoff_dt(d - timedelta(days=1))
    end = cutoff_dt(d)
    return start, end


def is_closed_day(d: date) -> bool:
    # Monday closed
    return d.weekday() == 0


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
    d = today if n < cutoff_dt(today) else (today + timedelta(days=1))
    return d


def check_admin():
    return request.args.get("token", "") == ADMIN_TOKEN


def options_html(items):
    # items: list[str]
    return "".join([f"<option>{x}</option>" for x in items])


def options_html_values(items):
    # items: list[tuple(value, label)]
    return "".join([f"<option value='{v}'>{lbl}</option>" for (v, lbl) in items])


def get_weekly_special(office: str, d: date):
    conn = db()
    row = conn.execute(
        """
        SELECT * FROM weekly_special
        WHERE office=? AND start_date <= ? AND end_date >= ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (office, d.isoformat(), d.isoformat()),
    ).fetchone()
    conn.close()
    return row


def hot_menu_with_special(office: str, d: date):
    items = MENU["hot"].copy()
    special = get_weekly_special(office, d)
    if special:
        label = f"Блюдо недели: {special['title']} / Weekly special: {special['title']}"
        s = int(special["surcharge_eur"])
        if s > 0:
            label += f" (+{s}€)"
        items.insert(0, label)
    return items


def compute_option_base_price(zakuska, soup, hot, dessert, office: str, d: date):
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

    # plov surcharge
    if hot and "Плов с бараниной" in hot:
        price += PLOV_SURCHARGE

    # weekly special surcharge
    if hot and hot.startswith("Блюдо недели:"):
        special = get_weekly_special(office, d)
        if special:
            price += float(int(special["surcharge_eur"]))

    return option, float(price), None


def compute_total_price(base_price: float, drink_code: str) -> float:
    drink_code = (drink_code or "").strip()
    add = float(DRINK_PRICE.get(drink_code, 0.0))
    total = float(base_price) + add
    # round to 2 decimals for display/storage
    return round(total, 2)


def generate_order_code(conn: sqlite3.Connection, office: str, d: date) -> str:
    ymd = d.strftime("%Y%m%d")
    like_prefix = f"{ORDER_PREFIX}-{ymd}-"
    row = conn.execute(
        """
        SELECT order_code FROM orders
        WHERE office=? AND order_date=? AND order_code LIKE ?
        ORDER BY order_code DESC
        LIMIT 1
        """,
        (office, d.isoformat(), like_prefix + "%"),
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
    return send_file(file_path("logo.png"))


@app.get("/banner.png")
def banner_png():
    return send_file(file_path("banner.png"))


@app.get("/sw.js")
def sw_js():
    js = """
const CACHE = 'volga-lunch-v2';
const ASSETS = ['/', '/edit', '/manifest.webmanifest', '/icon.svg', '/logo.png', '/banner.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method === 'GET' && url.origin === self.location.origin) {
    e.respondWith(
      caches.match(e.request).then((cached) => cached || fetch(e.request).then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then(cache => cache.put(e.request, copy)).catch(()=>{});
        return resp;
      }).catch(()=>cached))
    );
  }
});
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
  overflow:hidden; /* фикс “вылезает за границы” */
}

h1{
  color:var(--volga-blue);
  font-weight:800;
  letter-spacing:1px;
  margin:0 0 14px 0;
}
h1 small{
  color:var(--volga-red);
  font-weight:800;
}

label{
  display:block;
  margin-top:10px;
  font-weight:700;
  overflow-wrap:anywhere;
  color:var(--volga-blue);
}

input, select, textarea{
  width:100%;
  max-width:520px;
  padding:12px;
  margin-top:6px;
  font-size:16px;

  background:var(--volga-bg);
  color:var(--volga-blue);
  border:2px solid var(--volga-blue);
  border-radius:0;
}

input[type="date"]{
  max-width:100%;
}

input:focus, select:focus, textarea:focus{
  outline:none;
  border:2px solid var(--volga-blue);
}

button{
  background:var(--volga-red);
  color:var(--volga-bg);
  border:none;
  border-radius:0;
  padding:14px 24px;
  font-size:16px;
  font-weight:700;
  cursor:pointer;
  transition:0.2s ease;
}

button:hover{
  background:var(--volga-blue);
}

.row{
  display:grid;
  grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  gap:12px;
}

.row > div{ max-width:520px; }

.muted{ color:var(--volga-burgundy); }
.danger{ color:var(--volga-red); font-weight:800; }

small{ color:var(--volga-burgundy); }

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
  font-weight:800;
  margin:12px 0 0 0;
}
.lead .en{
  color:var(--volga-red);
  font-weight:800;
}

.section-title{
  color:var(--volga-burgundy);
  font-weight:800;
  margin-top:18px;
  margin-bottom:6px;
}

.notes{
  color:var(--volga-burgundy);
  margin-top:6px;
  margin-bottom:0;
}
.notes li{ margin:6px 0; }

.btn-secondary{
  display:inline-block;
  padding:14px 24px;
  border:2px solid var(--volga-blue);
  background:transparent;
  color:var(--volga-blue);
  text-decoration:none;
  font-weight:800;
}
.btn-secondary:hover{
  border-color:var(--volga-red);
  color:var(--volga-red);
}

/* secondary button (link) */
.btn-secondary{
  display:block;
  margin-top:18px;
  text-align:center;
  padding:14px 24px;
  border:2px solid var(--volga-blue);
  color:var(--volga-blue);
  background: transparent;
  font-size:16px;
  font-weight:700;
  text-decoration:none;
  border-radius:0;
}

.btn-secondary:hover{
  background: var(--volga-blue);
  color: var(--volga-bg);
}

/* нажатие (тап на мобиле / клик) — красный */
.btn-secondary:active{
  background: var(--volga-red);
  border-color: var(--volga-red);
  color: var(--volga-bg);
}


@media (max-width: 700px){
  .card{ padding:18px; }
  .row{ grid-template-columns:1fr; }
  .row > div{ max-width:none; }
  input, select, textarea{
    max-width:100%;
    width:100%;
  }
  h1{ letter-spacing:0.5px; }
} /* <-- важно: закрыли @media */

/* --- Mobile fix: date field not merging with card border --- */
input[type="date"]{
  width:100%;
  max-width:520px;
  min-width:0;
}

@media (max-width: 700px){
  /* небольшой “воздух” внутри карточки, чтобы рамки не сливались */
  .card { padding: 20px; }

  /* чуть увеличим зазор между блоками внутри .row */
  .row { gap: 16px; }

  /* дата иногда рисуется шире — фиксируем */
  #order_date{
    width:100%;
    max-width:100%;
    display:block;
  }
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
        b.dataset._txt = b.textContent;
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

</body>
</html>"""
    return shell.replace("__BODY__", body)


# ---------------------------
# Routes
# ---------------------------
@app.get("/")
def form():
    default_date = compute_default_date()

    office = request.args.get("office", OFFICES[0])
    if office not in OFFICES:
        office = OFFICES[0]

    d_str = request.args.get("date", default_date.isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = default_date

    hot_items = hot_menu_with_special(office, d)
    ok_time, start, end, now_ = validate_order_time(d)

    conn = db()
    ensure_columns(conn)
    cnt = conn.execute(
        "SELECT COUNT(*) as c FROM orders WHERE office=? AND order_date=? AND status='active'",
        (office, d.isoformat()),
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

    office_opts = "".join([f"<option value='{o}' {'selected' if o==office else ''}>{o}</option>" for o in OFFICES])

    # Drinks select html
    drink_options = "".join([f"<option value='{k}'>{lbl}</option>" for (k, lbl, _) in DRINKS])

    body = f"""
<div style="text-align:center; margin-bottom:18px;">
  <img src="/logo.png" alt="VOLGA" style="max-height:90px;">
</div>

<h1>РЕСТОРАН VOLGA — БИЗНЕС-ЛАНЧ ДЛЯ RINGCENTRAL<br>
<small>VOLGA RESTAURANT — BUSINESS LUNCH FOR RINGCENTRAL</small></h1>

<p class="lead">
  Доставка в 13:00. Заказ до 11:00.<br>
  <span class="en">Delivery at 13:00. Order before 11:00.</span>
</p>

<p class="section-title">Заказывать можно:</p>
<ul class="notes">
  <li>на сегодня — до 11:00</li>
  <li>на завтра — после 11:00</li>
</ul>

<p class="section-title">You can order:</p>
<ul class="notes">
  <li>for today — until 11:00</li>
  <li>for tomorrow — after 11:00</li>
</ul>

{warn}

<div class="card">
  <form method="post" action="/order" autocomplete="on">
    <div class="row">
      <div>
        <label>Офис / Office</label>
        <select id="office" name="office" required>
          {office_opts}
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
        <small>для связи и поиска заказа / for contact & order lookup</small>
      </div>
    </div>

    <!-- БАННЕР ВМЕСТО КАРТОЧКИ С ОПЦИЯМИ -->
    <div style="margin-top:18px;">
      <img src="/banner.png" alt="Options" style="width:100%; display:block; border:2px solid var(--volga-blue);">
    </div>

    <div class="row" style="margin-top:12px;">
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
          {options_html(MENU["soup"])}
        </select>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Горячее (по желанию) / Main (optional)</label>
        <select id="hot" name="hot">
          <option value="">— без горячего / no main —</option>
          {options_html(hot_items)}
        </select>
      </div>
      <div>
        <label>Десерт (по желанию) / Dessert (optional)</label>
        <select id="dessert" name="dessert">
          <option value="">— без десерта / no dessert —</option>
          {options_html(MENU["dessert"])}
        </select>
      </div>
    </div>

    <!-- НАПИТОК (ДО ХЛЕБА) -->
    <label>Напиток (оплачивается отдельно) / Drink (paid separately)</label>
    <select id="drink" name="drink">
      {drink_options}
    </select>
    <small>Не входит в стоимость опции / Not included in option price</small>

    <label style="margin-top:16px;">Хлеб (бесплатно) / Bread (free)</label>
    <select id="bread" name="bread">
      <option value="">— без хлеба / no bread —</option>
      {options_html(BREAD_OPTIONS)}
    </select>

    <label>Комментарий (если есть аллергии или пожелания) / Notes (allergies/requests)</label>
    <textarea name="comment" rows="3" placeholder="Без лука / No onion, аллергия / allergy..."></textarea>

    <button type="submit" style="margin-top:22px;" {"disabled" if (not ok_time or limit_reached) else ""}>
      Подтвердить заказ / Confirm order
    </button>

    <div style="margin-top:18px;">
      <a class="btn-secondary" href="/edit">Изменить / отменить заказ / Edit / cancel</a>

    </div>
  </form>
</div>
"""
    return html_page(body)


@app.post("/order")
def order():
    office = (request.form.get("office", "") or "").strip()
    if office not in OFFICES:
        return html_page("<p class='danger'>Ошибка: неизвестный офис / Unknown office.</p><p><a href='/'>Назад / Back</a></p>"), 400

    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата / Invalid date.</p><p><a href='/'>Назад / Back</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        if is_closed_day(d):
            return html_page("<p class='danger'><b>В понедельник мы не работаем.</b><br><small>We are closed on Mondays.</small></p><p><a href='/'>Назад / Back</a></p>"), 403
        return (
            html_page(
                f"<p class='danger'><b>Приём заказов закрыт.</b><br>"
                f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
                f"<p><a href='/'>Назад / Back</a></p>"
            ),
            403,
        )

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
    drink_label = DRINK_LABEL.get(drink_code, "") if drink_code else None
    drink_price = float(DRINK_PRICE.get(drink_code, 0.0))

    bread = (request.form.get("bread", "") or "").strip() or None
    comment = (request.form.get("comment", "") or "").strip() or None

    if not name or not soup or not phone_norm:
        return html_page("<p class='danger'>Ошибка: имя, телефон и суп обязательны / Name, phone and soup are required.</p><p><a href='/'>Назад / Back</a></p>"), 400

    option_code, base_price, err = compute_option_base_price(zakuska, soup, hot, dessert, office, d)
    if err:
        return html_page(f"<p class='danger'>Ошибка: {err}</p><p><a href='/'>Назад / Back</a></p>"), 400

    total_price = compute_total_price(base_price, drink_code)

    conn = db()
    ensure_columns(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")

        cnt = conn.execute(
            "SELECT COUNT(*) as c FROM orders WHERE office=? AND order_date=? AND status='active'",
            (office, d.isoformat()),
        ).fetchone()["c"]
        if cnt >= MAX_PER_DAY:
            conn.execute("ROLLBACK")
            conn.close()
            return html_page("<p class='danger'><b>Заказы на выбранную дату временно недоступны.</b><br><small>Orders are temporarily unavailable for this date.</small></p><p><a href='/'>Назад / Back</a></p>"), 409

        existing = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
            (office, d.isoformat(), phone_norm),
        ).fetchone()
        if existing:
            conn.execute("ROLLBACK")
            conn.close()
            return (
                html_page(
                    f"""
                    <h2 class="danger">⛔ Заказ уже существует / Order already exists</h2>
                    <div class="card">
                      <p>На этот телефон уже оформлен активный заказ на <b>{d.isoformat()}</b> ({office}).</p>
                      <p><small>An active order already exists for this phone on <b>{d.isoformat()}</b> ({office}).</small></p>
                      <p><span class="pill">Номер / Code: {existing['order_code']}</span>
                         <span class="pill">Итого / Total: {existing['price_eur']}€</span></p>
                      <p><a href="/edit?office={office}&date={d.isoformat()}&phone={phone_raw}">Открыть / Open /edit</a></p>
                    </div>
                    <p><a href="/">Назад / Back</a></p>
                    """
                ),
                409,
            )

        order_code = generate_order_code(conn, office, d)

        conn.execute(
            """
            INSERT INTO orders(
              order_code, office, order_date, name, phone_raw, phone_norm,
              zakuska, soup, hot, dessert,
              drink_code, drink_label, drink_price_eur,
              bread,
              option_code, price_eur, comment, status, created_at
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order_code, office, d.isoformat(),
                name, phone_raw, phone_norm,
                zakuska, soup, hot, dessert,
                drink_code or None, drink_label, drink_price if drink_code else None,
                bread,
                option_code, float(total_price), comment,
                "active", datetime.utcnow().isoformat()
            ),
        )

        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return html_page("<p class='danger'>Ошибка: конфликт данных (возможен дубль). / Data conflict (possible duplicate).</p><p><a href='/'>Назад / Back</a></p>"), 409
    finally:
        try:
            conn.close()
        except Exception:
            pass

    opt_human = {"opt1": "Опция 1 / Option 1", "opt2": "Опция 2 / Option 2", "opt3": "Опция 3 / Option 3"}[option_code]

    drink_line = f"{drink_label} (+{drink_price}€)" if drink_code else "—"

    return html_page(
        f"""
      <h2>✅ Заказ принят / Order confirmed</h2>
      <div class="card">
        <p><span class="pill"><b>{order_code}</b></span></p>
        <p><b>{name}</b> — {office} — <span class="muted">{phone_raw}</span></p>
        <p>Дата доставки / Delivery date: <b>{d.isoformat()}</b> (13:00)</p>
        <p><span class="pill">{opt_human}</span><span class="pill">Итого / Total: {total_price}€</span></p>
        <ul>
          <li>Закуска / Starter: {zakuska or "—"}</li>
          <li>Суп / Soup: {soup}</li>
          <li>Горячее / Main: {hot or "—"}</li>
          <li>Десерт / Dessert: {dessert or "—"}</li>
          <li>Напиток / Drink: {drink_line}</li>
          <li>Хлеб / Bread: {bread or "—"}</li>
        </ul>
        <p class="muted">Комментарий / Notes: {comment or "—"}</p>
        <p><a class="btn-secondary" href="/edit?office={office}&date={d.isoformat()}&phone={phone_raw}">Изменить / отменить / Edit / cancel</a></p>
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

    office = request.args.get("office", OFFICES[0])
    if office not in OFFICES:
        office = OFFICES[0]

    d_str = request.args.get("date", default_date.isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = default_date

    phone_raw = (request.args.get("phone", "") or "").strip()
    phone_norm = normalize_phone(phone_raw) if phone_raw else ""

    found = None
    conn = db()
    ensure_columns(conn)
    if phone_norm:
        found = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
            (office, d.isoformat(), phone_norm),
        ).fetchone()
    conn.close()

    ok_time, start, end, now_ = validate_order_time(d)
    office_opts = "".join([f"<option value='{o}' {'selected' if o==office else ''}>{o}</option>" for o in OFFICES])

    drink_options = ""
    for (k, lbl, _) in DRINKS:
        sel = ""
        if found and (found["drink_code"] or "") == (k or ""):
            sel = "selected"
        drink_options += f"<option value='{k}' {sel}>{lbl}</option>"

    if found:
        hot_items = hot_menu_with_special(office, d)

        closed_msg = ""
        if is_closed_day(d):
            closed_msg = "<p class='danger'><b>В понедельник мы не работаем.</b><br><small>We are closed on Mondays.</small></p>"

        body = f"""
        <h1>Изменить / отменить заказ<br><small>Edit / cancel order</small></h1>

        <div class="card">
          <p><span class="pill"><b>{found['order_code']}</b></span>
             <span class="pill">Доставка / Delivery: {d.isoformat()} 13:00</span></p>

          {closed_msg}

          <p class="muted">Окно изменений / Edit window:
            <b>{start.strftime('%d.%m %H:%M')}</b> — <b>{end.strftime('%d.%m %H:%M')}</b>.
            Сейчас / Now: <b>{now_.strftime('%d.%m %H:%M')}</b>.
          </p>
          {"<p class='danger'><b>Сейчас окно закрыто — изменения/отмена недоступны.</b><br><small>Window is closed — edit/cancel unavailable.</small></p>" if not ok_time else ""}

          <form method="post" action="/edit">
            <input type="hidden" name="office" value="{office}">
            <input type="hidden" name="order_date" value="{d.isoformat()}">
            <input type="hidden" name="phone" value="{found['phone_raw']}">

            <label>Как вас зовут / Your name</label>
            <input name="name" value="{found['name']}" required>

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
                  {options_html(MENU["soup"])}
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

            <label>Напиток (оплачивается отдельно) / Drink (paid separately)</label>
            <select name="drink">{drink_options}</select>
            <small>Не входит в стоимость опции / Not included in option price</small>

            <label style="margin-top:16px;">Хлеб (бесплатно) / Bread (free)</label>
            <select name="bread">
              <option value="" {"selected" if not found["bread"] else ""}>— без хлеба / no bread —</option>
              {options_html(BREAD_OPTIONS)}
            </select>

            <label>Комментарий / Notes</label>
            <textarea name="comment" rows="3">{found["comment"] or ""}</textarea>

            <button type="submit" style="margin-top:22px;" {"disabled" if not ok_time else ""}>Сохранить / Save</button>
          </form>

          <form method="post" action="/cancel" style="margin-top:12px;">
            <input type="hidden" name="office" value="{office}">
            <input type="hidden" name="order_date" value="{d.isoformat()}">
            <input type="hidden" name="phone" value="{found['phone_raw']}">
            <button type="submit" {"disabled" if not ok_time else ""}>Отменить заказ / Cancel</button>
          </form>

          <p class="muted">Телефон / Phone: <b>{found['phone_raw']}</b></p>
          <p><a href="/">← На главную / Home</a></p>
        </div>
        """
        return html_page(body)

    body = f"""
    <h1>Изменить / отменить заказ<br><small>Edit / cancel order</small></h1>
    <div class="card">
      <form method="get" action="/edit">
        <div class="row">
          <div>
            <label>Офис / Office</label>
            <select name="office" required>{office_opts}</select>
          </div>
          <div>
            <label>Дата доставки / Delivery date</label>
            <input type="date" name="date" value="{d.isoformat()}" required>
          </div>
        </div>

        <label>Телефон (как в заказе) / Phone (as in order)</label>
        <input name="phone" value="{phone_raw}" placeholder="+34..." required>

        <button type="submit">Найти заказ / Find order</button>
      </form>

      <p class="muted">Если заказ не найден — проверь офис, дату и телефон.<br>
      <small>If not found — check office, date and phone.</small></p>
      <p><a href="/">← На главную / Home</a></p>
    </div>
    """
    return html_page(body)


@app.post("/edit")
def edit_post():
    office = (request.form.get("office", "") or "").strip()
    if office not in OFFICES:
        return html_page("<p class='danger'>Ошибка: неизвестный офис / Unknown office.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата / Invalid date.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        if is_closed_day(d):
            return html_page("<p class='danger'><b>В понедельник мы не работаем.</b><br><small>We are closed on Mondays.</small></p><p><a href='/edit'>Назад / Back</a></p>"), 403
        return html_page(
            f"<p class='danger'><b>Окно редактирования закрыто.</b><br>"
            f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
            f"<p><a href='/edit'>Назад / Back</a></p>"
        ), 403

    phone_raw = (request.form.get("phone", "") or "").strip()
    phone_norm = normalize_phone(phone_raw)
    if not phone_norm:
        return html_page("<p class='danger'>Ошибка: телефон обязателен / Phone is required.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    name = (request.form.get("name", "") or "").strip()
    zakuska = (request.form.get("zakuska", "") or "").strip() or None
    soup = (request.form.get("soup", "") or "").strip()
    hot = (request.form.get("hot", "") or "").strip() or None
    dessert = (request.form.get("dessert", "") or "").strip() or None

    drink_code = (request.form.get("drink", "") or "").strip()
    if drink_code not in DRINK_PRICE:
        drink_code = ""
    drink_label = DRINK_LABEL.get(drink_code, "") if drink_code else None
    drink_price = float(DRINK_PRICE.get(drink_code, 0.0))

    bread = (request.form.get("bread", "") or "").strip() or None
    comment = (request.form.get("comment", "") or "").strip() or None

    if not name or not soup:
        return html_page("<p class='danger'>Ошибка: имя и суп обязательны / Name and soup are required.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    option_code, base_price, err = compute_option_base_price(zakuska, soup, hot, dessert, office, d)
    if err:
        return html_page(f"<p class='danger'>Ошибка: {err}</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    total_price = compute_total_price(base_price, drink_code)

    conn = db()
    ensure_columns(conn)
    existing = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
        (office, d.isoformat(), phone_norm),
    ).fetchone()

    if not existing:
        conn.close()
        return html_page("<p class='danger'>Активный заказ не найден / Active order not found.</p><p><a href='/edit'>Назад / Back</a></p>"), 404

    conn.execute(
        """
        UPDATE orders
        SET name=?, zakuska=?, soup=?, hot=?, dessert=?,
            drink_code=?, drink_label=?, drink_price_eur=?,
            bread=?, option_code=?, price_eur=?, comment=?
        WHERE id=?
        """,
        (
            name, zakuska, soup, hot, dessert,
            drink_code or None, drink_label, drink_price if drink_code else None,
            bread, option_code, float(total_price), comment,
            existing["id"],
        ),
    )
    conn.commit()
    conn.close()

    opt_human = {"opt1": "Опция 1 / Option 1", "opt2": "Опция 2 / Option 2", "opt3": "Опция 3 / Option 3"}[option_code]
    drink_line = f"{drink_label} (+{drink_price}€)" if drink_code else "—"

    return html_page(
        f"""
      <h2>✅ Изменения сохранены / Saved</h2>
      <div class="card">
        <p><span class="pill"><b>{existing['order_code']}</b></span></p>
        <p><b>{name}</b> — {office} — <span class="muted">{existing['phone_raw']}</span></p>
        <p>Дата доставки / Delivery date: <b>{d.isoformat()}</b> (13:00)</p>
        <p><span class="pill">{opt_human}</span><span class="pill">Итого / Total: {total_price}€</span></p>
        <ul>
          <li>Закуска / Starter: {zakuska or "—"}</li>
          <li>Суп / Soup: {soup}</li>
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
    office = (request.form.get("office", "") or "").strip()
    if office not in OFFICES:
        return html_page("<p class='danger'>Ошибка: неизвестный офис / Unknown office.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата / Invalid date.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        if is_closed_day(d):
            return html_page("<p class='danger'><b>В понедельник мы не работаем.</b><br><small>We are closed on Mondays.</small></p><p><a href='/edit'>Назад / Back</a></p>"), 403
        return html_page(
            f"<p class='danger'><b>Окно отмены закрыто.</b><br>"
            f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
            f"<p><a href='/edit'>Назад / Back</a></p>"
        ), 403

    phone_raw = (request.form.get("phone", "") or "").strip()
    phone_norm = normalize_phone(phone_raw)
    if not phone_norm:
        return html_page("<p class='danger'>Ошибка: телефон обязателен / Phone is required.</p><p><a href='/edit'>Назад / Back</a></p>"), 400

    conn = db()
    ensure_columns(conn)
    existing = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
        (office, d.isoformat(), phone_norm),
    ).fetchone()

    if not existing:
        conn.close()
        return html_page("<p class='danger'>Активный заказ не найден / Active order not found.</p><p><a href='/edit'>Назад / Back</a></p>"), 404

    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (existing["id"],))
    conn.commit()
    conn.close()

    return html_page(
        f"""
      <h2>🗑 Заказ отменён / Order cancelled</h2>
      <div class="card">
        <p><span class="pill"><b>{existing['order_code']}</b></span></p>
        <p><b>{existing['name']}</b> — {office} — <span class="muted">{existing['phone_raw']}</span></p>
        <p>Дата доставки / Delivery date: <b>{d.isoformat()}</b></p>
      </div>
      <p><a href="/">← На главную / Home</a></p>
    """
    )


# ---------------------------
# Admin + Weekly special + CSV
# ---------------------------
@app.get("/admin")
def admin():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2><p>Нужен token.</p>"), 403

    office = request.args.get("office", OFFICES[0])
    if office not in OFFICES:
        office = OFFICES[0]

    d_str = request.args.get("date", date.today().isoformat())
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
        (office, d.isoformat()),
    ).fetchall()

    cancelled_rows = conn.execute(
        """
        SELECT * FROM orders
        WHERE office=? AND order_date=? AND status='cancelled'
        ORDER BY created_at ASC
        """,
        (office, d.isoformat()),
    ).fetchall()

    opt_counts = {"opt1": 0, "opt2": 0, "opt3": 0}
    dish_counts = {}
    drink_counts = {}

    for r in active_rows:
        opt_counts[r["option_code"]] += 1
        for k in ["zakuska", "soup", "hot", "dessert", "bread"]:
            v = r[k]
            if v:
                dish_counts[v] = dish_counts.get(v, 0) + 1
        if r["drink_label"]:
            drink_counts[r["drink_label"]] = drink_counts.get(r["drink_label"], 0) + 1

    special = get_weekly_special(office, d)
    conn.close()

    office_opts = "".join([f"<option value='{o}' {'selected' if o==office else ''}>{o}</option>" for o in OFFICES])

    def rows_list(rows):
        items = ""
        for r in rows:
            items += f"<li><b>{r['order_code']}</b> — <b>{r['name']}</b> <small class='muted'>({r['phone_raw']})</small> — <b>{r['price_eur']}€</b> — {r['soup']}"
            if r["zakuska"]:
                items += f" / {r['zakuska']}"
            if r["hot"]:
                items += f" / {r['hot']}"
            if r["dessert"]:
                items += f" / {r['dessert']}"
            if r["drink_label"]:
                dp = r["drink_price_eur"] or 0
                items += f" / 🍹 {r['drink_label']} (+{dp}€)"
            if r["bread"]:
                items += f" / {r['bread']}"
            if r["comment"]:
                items += f" <small class='muted'>— {r['comment']}</small>"
            items += "</li>"
        return items or "<li class='muted'>—</li>"

    dish_list = "".join([f"<li>{k} — {v}</li>" for k, v in sorted(dish_counts.items(), key=lambda x: (-x[1], x[0]))])
    drink_list = "".join([f"<li>{k} — {v}</li>" for k, v in sorted(drink_counts.items(), key=lambda x: (-x[1], x[0]))]) or "<li class='muted'>—</li>"

    special_block = "<p class='muted'>Блюдо недели / Weekly special: —</p>"
    if special:
        special_block = f"<p><b>Блюдо недели / Weekly special:</b> {special['title']} (доплата +{int(special['surcharge_eur'])}€) <small class='muted'>[{special['start_date']} … {special['end_date']}]</small></p>"

    body = f"""
    <h1>Админка / Admin</h1>

    <div class="card">
      <form method="get" action="/admin">
        <input type="hidden" name="token" value="{ADMIN_TOKEN}">
        <div class="row">
          <div>
            <label>Офис / Office</label>
            <select name="office">{office_opts}</select>
          </div>
          <div>
            <label>Дата / Date</label>
            <input type="date" name="date" value="{d.isoformat()}">
          </div>
        </div>
        <button type="submit">Показать / Show</button>
      </form>

      <p style="margin-top:14px;">
        <a href="/export.csv?office={office}&date={d.isoformat()}&token={ADMIN_TOKEN}">⬇️ CSV (активные) / active</a>
        &nbsp;|&nbsp;
        <a href="/admin/special?office={office}&date={d.isoformat()}&token={ADMIN_TOKEN}">⭐ Блюдо недели / weekly special</a>
      </p>

      {special_block}

      <p>
        <span class="pill">Опция 1: {opt_counts['opt1']}</span>
        <span class="pill">Опция 2: {opt_counts['opt2']}</span>
        <span class="pill">Опция 3: {opt_counts['opt3']}</span>
      </p>
    </div>

    <div class="card">
      <h3>Список активных заказов / Active orders</h3>
      <ol>{rows_list(active_rows)}</ol>
    </div>

    <div class="card">
      <h3>Отменённые заказы / Cancelled</h3>
      <ol>{rows_list(cancelled_rows)}</ol>
    </div>

    <div class="card">
      <h3>Сводка по блюдам (активные) / Dishes summary</h3>
      <ul>{dish_list or "<li class='muted'>—</li>"}</ul>
    </div>

    <div class="card">
      <h3>Сводка по напиткам (активные) / Drinks summary</h3>
      <ul>{drink_list}</ul>
    </div>
    """
    return html_page(body)


@app.get("/admin/special")
def admin_special_get():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2><p>Нужен token.</p>"), 403

    office = request.args.get("office", OFFICES[0])
    if office not in OFFICES:
        office = OFFICES[0]

    d_str = request.args.get("date", date.today().isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = date.today()

    special = get_weekly_special(office, d)

    start_default = d.isoformat()
    end_default = (d + timedelta(days=6)).isoformat()
    title_default = special["title"] if special else ""
    surcharge_default = int(special["surcharge_eur"]) if special else 0

    office_opts = "".join([f"<option value='{o}' {'selected' if o==office else ''}>{o}</option>" for o in OFFICES])

    body = f"""
    <h1>Блюдо недели / Weekly special</h1>
    <div class="card">
      <form method="post" action="/admin/special?token={ADMIN_TOKEN}">
        <label>Офис / Office</label>
        <select name="office" required>{office_opts}</select>

        <div class="row">
          <div>
            <label>Start date</label>
            <input type="date" name="start_date" value="{start_default}" required>
          </div>
          <div>
            <label>End date</label>
            <input type="date" name="end_date" value="{end_default}" required>
          </div>
        </div>

        <label>Название блюда недели (горячее)</label>
        <input name="title" value="{title_default}" placeholder="Напр. Бефстроганов" required>

        <label>Доплата, €</label>
        <input name="surcharge_eur" type="number" min="0" step="1" value="{surcharge_default}" required>

        <button type="submit">Сохранить / Save</button>
      </form>

      <p class="muted">После сохранения появится в “Горячее” как “Блюдо недели: … (+X€)”.</p>
      <p><a href="/admin?office={office}&date={d.isoformat()}&token={ADMIN_TOKEN}">← Назад / Back</a></p>
    </div>
    """
    return html_page(body)


@app.post("/admin/special")
def admin_special_post():
    if not check_admin():
        return html_page("<h2>⛔ Нет доступа</h2><p>Нужен token.</p>"), 403

    office = (request.form.get("office", "") or "").strip()
    if office not in OFFICES:
        return html_page("<p class='danger'>Ошибка: неизвестный офис.</p>"), 400

    try:
        start_date = date.fromisoformat((request.form.get("start_date", "") or "").strip())
        end_date = date.fromisoformat((request.form.get("end_date", "") or "").strip())
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверные даты.</p>"), 400

    if end_date < start_date:
        return html_page("<p class='danger'>Ошибка: end_date раньше start_date.</p>"), 400

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
        """
        INSERT INTO weekly_special(office, start_date, end_date, title, surcharge_eur, created_at)
        VALUES (?,?,?,?,?,?)
        """,
        (office, start_date.isoformat(), end_date.isoformat(), title, surcharge, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

    return redirect(f"/admin?office={office}&date={start_date.isoformat()}&token={ADMIN_TOKEN}")


@app.get("/export.csv")
def export_csv():
    if not check_admin():
        return Response("Forbidden\n", status=403, mimetype="text/plain")

    office = request.args.get("office", OFFICES[0])
    d_str = request.args.get("date", date.today().isoformat())
    try:
        d = date.fromisoformat(d_str)
    except ValueError:
        d = date.today()

    conn = db()
    ensure_columns(conn)

    rows = conn.execute(
        """
        SELECT order_code, office, order_date, name, phone_raw, option_code, price_eur,
               zakuska, soup, hot, dessert,
               drink_label, drink_price_eur,
               bread, comment, status, created_at
        FROM orders
        WHERE office=? AND order_date=? AND status='active'
        ORDER BY created_at ASC
        """,
        (office, d.isoformat()),
    ).fetchall()
    conn.close()

    def esc(s):
        s = "" if s is None else str(s)
        s = s.replace('"', '""')
        return f'"{s}"'

    header = "order_code,office,order_date,name,phone,option_code,total_eur,zakuska,soup,hot,dessert,drink,drink_price_eur,bread,comment,status,created_at"
    lines = [header]
    for r in rows:
        lines.append(
            ",".join(
                [
                    esc(r["order_code"]),
                    esc(r["office"]),
                    esc(r["order_date"]),
                    esc(r["name"]),
                    esc(r["phone_raw"]),
                    esc(r["option_code"]),
                    esc(r["price_eur"]),
                    esc(r["zakuska"]),
                    esc(r["soup"]),
                    esc(r["hot"]),
                    esc(r["dessert"]),
                    esc(r["drink_label"]),
                    esc(r["drink_price_eur"]),
                    esc(r["bread"]),
                    esc(r["comment"]),
                    esc(r["status"]),
                    esc(r["created_at"]),
                ]
            )
        )
    csv_data = "\n".join(lines) + "\n"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="orders_{office}_{d.isoformat()}.csv"'},
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)

