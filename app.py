import os
import re
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, Response, redirect, send_file, abort

# ---------------------------
# Config
# ---------------------------
APP_TITLE = os.getenv("APP_TITLE", "VOLGA Lunch")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
DB_PATH = os.getenv("DB_PATH", "/tmp/orders.sqlite")
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
# Твои цены: морс 4, вода 2.2, чай 3.5, квас 3.5
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
    if "drink_code" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_code TEXT")
    if "drink_label" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_label TEXT")
    if "drink_price_eur" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN drink_price_eur REAL")


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
    return today if n < cutoff_dt(today) else (today + timedelta(days=1))


def check_admin():
    return request.args.get("token", "") == ADMIN_TOKEN


def options_html(items):
    return "".join([f"<option>{x}</option>" for x in items])


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

    if hot and "Плов с бараниной" in hot:
        price += PLOV_SURCHARGE

    if hot and hot.startswith("Блюдо недели:"):
        special = get_weekly_special(office, d)
        if special:
            price += float(int(special["surcharge_eur"]))

    return option, float(price), None


def compute_total_price(base_price: float, drink_code: str) -> float:
    add = float(DRINK_PRICE.get((drink_code or "").strip(), 0.0))
    return round(float(base_price) + add, 2)


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
    # ВАЖНО: меняй версию CACHE при изменениях, чтобы Chrome обновлялся
    js = """
const CACHE = 'volga-lunch-v3';
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
  overflow:hidden;
}

h1{
  color:var(--volga-blue);
  font-weight:800;
  letter-spacing:1px;
  margin:0 0 14px 0;
  line-height:0.95;
  


}
h1 small{
  display:block;
  color:var(--volga-red);
  font-weight:800;
  line-height:1.00;
  margin-top:4px;
}


label{
  display:block;
  margin-top:10px;
  font-weight:800;
  overflow-wrap:anywhere;
  color:var(--volga-red); /* <-- заголовки полей КРАСНЫЕ (как ты просил) */
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

input:focus, select:focus, textarea:focus{
  outline:none;
  border:2px solid var(--volga-blue);
}

.row{
  display:grid;
  grid-template-columns:minmax(0,1fr) minmax(0,1fr);
  gap:14px;
  align-items:start;
}

.row > div{
  width:100%;
  max-width:520px;
}

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

/* Доставка RU синий жирный, EN красный (как ты хотел) */
.lead{
  color:var(--volga-blue);
  font-weight:900;
  margin:12px 0 0 0;
}
.lead .en{
  color:var(--volga-red);
  font-weight:800;
}

/* Часы работы: RU синий, EN красный */
.hours{
  margin:14px 0 0 0;
  font-weight:900;
}
.hours .ru{ color:var(--volga-blue); }
.hours .en{ color:var(--volga-red); }

/* кнопки */
/* --- Основная кнопка (Confirm) --- */
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

/* при нажатии — красная */
.btn-confirm:active{
  background:var(--volga-red);
}


/* --- Вторая кнопка (Edit / Cancel) --- */
.btn-edit{
  display:block;
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

/* при нажатии — синяя */
.btn-edit:active{
  background:var(--volga-blue);
}

.btn-secondary:hover{
  background: var(--volga-blue);
  color: var(--volga-bg);
}
.btn-secondary:active{
  background: var(--volga-red);
  border-color: var(--volga-red);
  color: var(--volga-bg);
}

@media (max-width: 700px){
  .card{ padding:20px; }
  .row{ grid-template-columns:1fr; gap:16px; }
  .row > div{ max-width:none; }
  input, select, textarea{ max-width:100%; }
  h1{ letter-spacing:0.5px; }

  /* чтобы рамка date не была “шире” и не сливалась с границей */
  #order_date{
    width:100%;
    max-width:100%;
    display:block;
  }
}
/* ✅ Mobile fix: date input should not overflow and should not "merge" with card border */
@media (max-width: 700px){

  /* iOS/Safari часто делает date шире из-за системной кнопки/иконки */
  input[type="date"]{
    -webkit-appearance: none;
    appearance: none;
  }

  /* конкретно наша дата */
  #order_date{
    width: 100%;
    max-width: 100%;
    min-width: 0;
    display: block;

    /* маленький "внутренний отступ" от рамки карточки,
       чтобы визуально не сливалось */
    margin-left: 2px;
    margin-right: 2px;
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
    drink_options = "".join([f"<option value='{k}'>{lbl}</option>" for (k, lbl, _) in DRINKS])

    body = f"""
<div style="margin-bottom:18px;">
  <img src="/logo.png" alt="VOLGA" style="max-height:90px; display:block;">
</div>


<h1>БИЗНЕС-ЛАНЧ ДЛЯ RingCentral<br>
<small>BUSINESS LUNCH FOR RingCentral</small></h1>

<p class="lead">
  Доставка в 13:00. Заказ до 11:00.<br>
  <span class="en">Delivery at 13:00. Order before 11:00.</span>
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
        <label>Офис / Office</label>
        <select id="office" name="office" required>{office_opts}</select>
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
        <label>Горячее / Main </label>
        <select id="hot" name="hot">
          <option value="">— без горячего / no main —</option>
          {options_html(hot_items)}
        </select>
      </div>
      <div>
        <label>Десерт / Dessert </label>
        <select id="dessert" name="dessert">
          <option value="">— без десерта / no dessert —</option>
          {options_html(MENU["dessert"])}
        </select>
      </div>
    </div>

    <label>Напиток / Drink </label>
    <select id="drink" name="drink">{drink_options}</select>
    <small>Не входит в стоимость опции / Not included in option price</small>

    <label style="margin-top:16px;">Хлеб / Bread </label>
    <select id="bread" name="bread">
      <option value="">— без хлеба / no bread —</option>
      {options_html(BREAD_OPTIONS)}
    </select>

    <label>Комментарий / Notes </label>
    <textarea name="comment" rows="3" placeholder=""></textarea>

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
        return html_page(
            f"<p class='danger'><b>Приём заказов закрыт.</b><br>"
            f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
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
            return html_page("<p class='danger'><b>Заказы на выбранную дату временно недоступны.</b><br><small>Orders are temporarily unavailable for this date.</small></p><p><a href='/'>Назад / Back</a></p>"), 409

        existing = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
            (office, d.isoformat(), phone_norm),
        ).fetchone()
        if existing:
            conn.execute("ROLLBACK")
            return html_page(
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
            ), 409

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
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
    finally:
        conn.close()

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
# Edit / Cancel (оставляем как было в твоей версии)
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

        body = f"""
        <h1>Изменить / отменить заказ<br><small>Edit / cancel order</small></h1>
        <div class="card">
          <p><span class="pill"><b>{found['order_code']}</b></span>
             <span class="pill">Доставка / Delivery: {d.isoformat()} 13:00</span></p>

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

            <button type="submit" style="margin-top:30px;" {"disabled" if not ok_time else ""}>Сохранить / Save</button>
          </form>

          <form method="post" action="/cancel" style="margin-top:12px;">
            <input type="hidden" name="office" value="{office}">
            <input type="hidden" name="order_date" value="{d.isoformat()}">
            <input type="hidden" name="phone" value="{found['phone_raw']}">
            <button type="submit" {"disabled" if not ok_time else ""}>Отменить заказ / Cancel</button>
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

        <button type="submit" style="margin-top:30px;">Найти заказ / Find order</button>
      </form>

      <p class="muted">Если заказ не найден — проверь офис, дату и телефон.<br>
      <small>If not found — check office, date and phone.</small></p>
      <p><a href="/">← На главную / Home</a></p>
    </div>
    """
    return html_page(body)


@app.post("/edit")
def edit_post():
    return Response("Edit POST not included in this minimal reset. If you need it, tell me and I’ll paste it back.", status=501)


@app.post("/cancel")
def cancel_post():
    return Response("Cancel POST not included in this minimal reset. If you need it, tell me and I’ll paste it back.", status=501)


# ---------------------------
# Admin/CSV оставляем как следующий шаг (сначала убираем 500)
# ---------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)









