import os
import re
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, Response, redirect

APP_TITLE = os.getenv("APP_TITLE", "VOLGA — Обеды для офиса")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "change-me")
DB_PATH = os.getenv("DB_PATH", "orders.sqlite")
TZ = ZoneInfo(os.getenv("TZ", "Europe/Madrid"))

MAX_PER_DAY = int(os.getenv("MAX_PER_DAY", "30"))
CUTOFF_HOUR = int(os.getenv("CUTOFF_HOUR", "11"))  # 11:00
ORDER_PREFIX = os.getenv("ORDER_PREFIX", "VO")

OFFICES = ["Office A", "Office B"]

MENU = {
    "zakuska": ["Оливье", "Винегрет", "Икра из баклажанов", "Паштет из куриной печени", "Шуба"],
    "soup": ["Борщ", "Солянка сборная мясная", "Куриный с домашней лапшой и яйцом"],
    "hot": [
        "Куриные котлеты с пюре",
        "Куриные котлеты с гречкой",
        "Вареники с картошкой",
        "Пельмени со сметаной",
        "Плов с бараниной (+3€)",
    ],
    "dessert": ["Торт Наполеон", "Пирожное Картошка", "Трубочка со сгущенкой"],
}

PRICES = {"opt1": 15, "opt2": 16, "opt3": 17}
PLOV_SURCHARGE = 3

app = Flask(__name__)


# ---------------------------
# DB
# ---------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code TEXT NOT NULL UNIQUE,       -- VO-YYYYMMDD-XXX
            office TEXT NOT NULL,
            order_date TEXT NOT NULL,              -- YYYY-MM-DD (дата доставки)
            name TEXT NOT NULL,
            phone_raw TEXT NOT NULL,               -- как ввёл человек
            phone_norm TEXT NOT NULL,              -- нормализованный для уникальности/поиска

            zakuska TEXT,
            soup TEXT NOT NULL,
            hot TEXT,
            dessert TEXT,

            option_code TEXT NOT NULL,             -- opt1/opt2/opt3
            price_eur INTEGER NOT NULL,
            comment TEXT,
            status TEXT NOT NULL DEFAULT 'active', -- active/cancelled

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
            start_date TEXT NOT NULL,              -- YYYY-MM-DD
            end_date TEXT NOT NULL,                -- YYYY-MM-DD
            title TEXT NOT NULL,
            surcharge_eur INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_special_office_dates ON weekly_special(office, start_date, end_date)")

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
    # окно: [11:00 D-1 ; 11:00 D)
    start = cutoff_dt(d - timedelta(days=1))
    end = cutoff_dt(d)
    return start, end


def validate_order_time(d: date):
    n = now_local()
    start, end = ordering_window_for(d)
    return (start <= n < end), start, end, n


def check_admin():
    return request.args.get("token", "") == ADMIN_TOKEN


def options_html(items):
    return "".join([f"<option>{x}</option>" for x in items])


def normalize_phone(raw: str) -> str:
    """
    Нормализация: оставляем ведущий + (если был) и цифры.
    Пробелы/дефисы/скобки убираем.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    has_plus = raw.lstrip().startswith("+")
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return ""
    return ("+" if has_plus else "") + digits


def compute_default_date():
    """
    Умная дата по умолчанию:
    - если сейчас < 11:00 -> сегодня
    - иначе -> завтра
    """
    n = now_local()
    today = n.date()
    return today if n < cutoff_dt(today) else (today + timedelta(days=1))


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
        label = f"Блюдо недели: {special['title']}"
        s = int(special["surcharge_eur"])
        if s > 0:
            label += f" (+{s}€)"
        items.insert(0, label)
    return items


def compute_option_and_price(zakuska, soup, hot, dessert, office: str, d: date):
    has_z = bool(zakuska)
    has_s = bool(soup)
    has_h = bool(hot)
    has_d = bool(dessert)

    if not has_s:
        return None, None, "Суп обязателен."

    # ровно 3 категории
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
        return None, None, "Нужно выбрать ровно 3 категории по правилам опций (и суп обязателен)."

    if hot and "Плов с бараниной" in hot:
        price += PLOV_SURCHARGE

    if hot and hot.startswith("Блюдо недели:"):
        special = get_weekly_special(office, d)
        if special:
            price += int(special["surcharge_eur"])

    return option, price, None


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


# ---------------------------
# PWA (9)
# ---------------------------
@app.get("/manifest.webmanifest")
def manifest():
    # минимальный манифест (Android/Chrome отлично, iOS "Add to Home Screen" тоже работает частично)
    data = {
        "name": APP_TITLE,
        "short_name": "VOLGA Lunch",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#ffffff",
        "icons": [
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}
        ],
    }
    # вручную, чтобы не тянуть json
    import json
    return Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


@app.get("/icon.svg")
def icon_svg():
    # простая иконка-заглушка (можно потом заменить на фирменную)
    svg = """<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
<rect width="512" height="512" fill="#ffffff"/>
<rect x="64" y="64" width="384" height="384" fill="#f2f2f2" stroke="#111" stroke-width="12"/>
<path d="M110 170 L402 110 L402 180 L110 240 Z" fill="#d00" opacity="0.9"/>
<path d="M110 330 L402 270 L402 340 L110 400 Z" fill="#06c" opacity="0.9"/>
<text x="256" y="280" font-family="Arial, sans-serif" font-size="64" text-anchor="middle" fill="#111">VOLGA</text>
</svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.get("/sw.js")
def sw_js():
    # кэшируем базовые страницы и манифест (простая офлайн-заглушка)
    js = """
const CACHE = 'volga-lunch-v1';
const ASSETS = ['/', '/edit', '/manifest.webmanifest', '/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // кэш-стратегия: cache-first для наших страниц GET
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
# HTML
# ---------------------------
def html_page(body: str) -> str:
    # (5) защита от двойного сабмита: disable submit после отправки
    # (9) подключаем manifest и регистрируем service worker
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{APP_TITLE}</title>

<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#ffffff">

<style>
  body {{ font-family: -apple-system, system-ui, Arial; margin: 18px; max-width: 920px; }}
  .card {{ border: 1px solid #ddd; border-radius: 14px; padding: 14px; margin: 12px 0; }}
  label {{ display:block; margin-top:10px; font-weight:600; }}
  input, select, textarea, button {{ width: 100%; padding: 12px; margin-top: 6px; font-size: 16px; }}
  .row {{ display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
  .muted {{ color:#666; }}
  .pill {{ display:inline-block; padding:6px 10px; border-radius:999px; border:1px solid #ddd; margin-right:8px; }}
  .danger {{ color:#b00; }}
  small {{ color:#666; }}
  a {{ color:#06c; text-decoration:none; }}
</style>
</head>
<body>
{body}

<script>
(function(){
  // (5) anti-double-submit
  document.querySelectorAll('form').forEach((f) => {{
    f.addEventListener('submit', () => {{
      const btns = f.querySelectorAll('button[type="submit"]');
      btns.forEach(b => {{ b.disabled = true; b.dataset._txt = b.textContent; b.textContent = 'Отправка…'; }});
    }});
  }});

  // (9) service worker
  if ('serviceWorker' in navigator) {{
    navigator.serviceWorker.register('/sw.js').catch(()=>{{}});
  }}
})();
</script>

</body>
</html>"""


# ---------------------------
# Routes
# ---------------------------
@app.get("/")
def form():
    # (6) умная дата
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
    cnt = conn.execute(
        "SELECT COUNT(*) as c FROM orders WHERE office=? AND order_date=? AND status='active'",
        (office, d.isoformat()),
    ).fetchone()["c"]
    conn.close()

    limit_reached = cnt >= MAX_PER_DAY

    warn = ""
    if not ok_time:
        warn = (
            f"<p class='danger'><b>Приём заказов на {d.isoformat()} закрыт.</b><br>"
            f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')} (Europe/Madrid). "
            f"Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
        )
    if limit_reached:
        warn += f"<p class='danger'><b>Лимит {MAX_PER_DAY} активных заказов на {d.isoformat()} для {office} достигнут.</b></p>"

    office_opts = "".join([f"<option value='{o}' {'selected' if o==office else ''}>{o}</option>" for o in OFFICES])

    body = f"""
    <h1>{APP_TITLE}</h1>
    <p class="muted">Доставка: <b>13:00</b>. Заказ на дату D принимается с <b>11:00</b> (D-1) до <b>11:00</b> (D). Лимит: <b>{MAX_PER_DAY}</b> активных заказов на офис/дату.</p>
    {warn}

    <div class="card">
      <form method="post" action="/order" autocomplete="on">
        <div class="row">
          <div>
            <label>Офис</label>
            <select id="office" name="office" onchange="reloadWithParams()" required>
              {office_opts}
            </select>
          </div>
          <div>
            <label>Дата доставки</label>
            <input id="order_date" type="date" name="order_date" value="{d.isoformat()}" onchange="reloadWithParams()" required>
          </div>
        </div>

        <div class="row">
          <div>
            <label>Имя</label>
            <input name="name" placeholder="Имя и фамилия" required>
          </div>
          <div>
            <label>Телефон (обязательно)</label>
            <input name="phone" placeholder="+34..." required>
            <small class="muted">Мы нормализуем телефон (пробелы/дефисы не влияют) и не даём сделать два активных заказа на одну дату.</small>
          </div>
        </div>

        <div class="row">
          <div>
            <label>Закуска (если нужна)</label>
            <select id="zakuska" name="zakuska">
              <option value="">— без закуски —</option>
              {options_html(MENU["zakuska"])}
            </select>
          </div>
          <div>
            <label>Суп (обязательно)</label>
            <select id="soup" name="soup" required>
              <option value="">— выбери суп —</option>
              {options_html(MENU["soup"])}
            </select>
          </div>
        </div>

        <div class="row">
          <div>
            <label>Горячее (если нужно)</label>
            <select id="hot" name="hot">
              <option value="">— без горячего —</option>
              {options_html(hot_items)}
            </select>
            <small>“Блюдо недели” — всегда горячее, доплата задаётся в админке.</small>
          </div>
          <div>
            <label>Десерт (если нужен)</label>
            <select id="dessert" name="dessert">
              <option value="">— без десерта —</option>
              {options_html(MENU["dessert"])}
            </select>
          </div>
        </div>

        <label>Комментарий (опционально)</label>
        <textarea name="comment" rows="3" placeholder="Без лука, аллергия на..."></textarea>

        <div class="card" style="background:#fafafa;">
          <div id="summary"></div>
          <small class="muted">
            Опция1=Закуска+Суп+Десерт (15€), Опция2=Суп+Горячее+Десерт (16€), Опция3=Закуска+Суп+Горячее (17€).
            Плов +3€. Блюдо недели — доплата из админки.
          </small>
        </div>

        <button type="submit" {'disabled' if (not ok_time or limit_reached) else ''}>Подтвердить заказ</button>
        <small class="muted">Админка: /admin?token=...</small>
        <br>
        <small class="muted">Изменить/отменить свой заказ: <a href="/edit">/edit</a></small>
      </form>
    </div>

    <script>
    function compute(){{
      const z = document.getElementById('zakuska').value.trim();
      const s = document.getElementById('soup').value.trim();
      const h = document.getElementById('hot').value.trim();
      const d = document.getElementById('dessert').value.trim();

      const hasZ = !!z, hasS = !!s, hasH = !!h, hasD = !!d;
      let option = null, price = null, err = null;

      if(!hasS){{ err = "Суп обязателен."; }}

      if(!err){{
        if(hasZ && hasS && hasD && !hasH){{ option="Опция 1"; price=15; }}
        else if(!hasZ && hasS && hasH && hasD){{ option="Опция 2"; price=16; }}
        else if(hasZ && hasS && hasH && !hasD){{ option="Опция 3"; price=17; }}
        else {{ err = "Нужно выбрать ровно 3 категории по правилам опций."; }}
      }}

      if(!err && h.includes("Плов с бараниной")) price += 3;

      document.getElementById('summary').innerHTML = err
        ? "<span class='danger'>"+err+"</span>"
        : "<span class='pill'>"+option+"</span><span class='pill'>Итого (ориентир): "+price+"€</span><span class='muted'> (финально подтвердит система)</span>";
    }}
    document.addEventListener("change", compute);
    document.addEventListener("DOMContentLoaded", compute);

    function reloadWithParams(){{
      const office = document.getElementById('office').value;
      const od = document.getElementById('order_date').value;
      const url = new URL(window.location.href);
      url.searchParams.set('office', office);
      url.searchParams.set('date', od);
      window.location.href = url.toString();
    }}
    </script>
    """
    return html_page(body)


@app.post("/order")
def order():
    office = request.form.get("office", "").strip()
    if office not in OFFICES:
        return html_page("<p class='danger'>Ошибка: неизвестный офис.</p><p><a href='/'>Назад</a></p>"), 400

    order_date = request.form.get("order_date", "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата.</p><p><a href='/'>Назад</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        return (
            html_page(
                f"<p class='danger'><b>Приём заказов закрыт.</b><br>"
                f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
                f"<p><a href='/'>Назад</a></p>"
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
    comment = (request.form.get("comment", "") or "").strip() or None

    if not name or not soup or not phone_norm:
        return html_page("<p class='danger'>Ошибка: имя, телефон и суп обязательны.</p><p><a href='/'>Назад</a></p>"), 400

    option_code, price, err = compute_option_and_price(zakuska, soup, hot, dessert, office, d)
    if err:
        return html_page(f"<p class='danger'>Ошибка: {err}</p><p><a href='/'>Назад</a></p>"), 400

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        cnt = conn.execute(
            "SELECT COUNT(*) as c FROM orders WHERE office=? AND order_date=? AND status='active'",
            (office, d.isoformat()),
        ).fetchone()["c"]
        if cnt >= MAX_PER_DAY:
            conn.execute("ROLLBACK")
            conn.close()
            return html_page(f"<p class='danger'><b>Лимит {MAX_PER_DAY} активных заказов достигнут.</b></p><p><a href='/'>Назад</a></p>"), 409

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
                    <h2 class="danger">⛔ Заказ уже существует</h2>
                    <div class="card">
                      <p>На этот телефон уже оформлен активный заказ на <b>{d.isoformat()}</b> ({office}).</p>
                      <p><span class="pill">Номер: {existing['order_code']}</span> <span class="pill">Итого: {existing['price_eur']}€</span></p>
                      <p><a href="/edit?office={office}&date={d.isoformat()}&phone={phone_raw}">Открыть /edit</a></p>
                    </div>
                    <p><a href="/">Назад</a></p>
                    """
                ),
                409,
            )

        order_code = generate_order_code(conn, office, d)

        conn.execute(
            """
            INSERT INTO orders(
              order_code, office, order_date, name, phone_raw, phone_norm,
              zakuska, soup, hot, dessert, option_code, price_eur, comment, status, created_at
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                order_code,
                office,
                d.isoformat(),
                name,
                phone_raw,
                phone_norm,
                zakuska,
                soup,
                hot,
                dessert,
                option_code,
                int(price),
                comment,
                "active",
                datetime.utcnow().isoformat(),
            ),
        )

        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return html_page("<p class='danger'>Ошибка: конфликт данных (возможен дубль). Попробуйте ещё раз.</p><p><a href='/'>Назад</a></p>"), 409
    finally:
        try:
            conn.close()
        except Exception:
            pass

    opt_human = {"opt1": "Опция 1", "opt2": "Опция 2", "opt3": "Опция 3"}[option_code]
    return html_page(
        f"""
      <h2>✅ Заказ принят</h2>
      <div class="card">
        <p><span class="pill"><b>{order_code}</b></span></p>
        <p><b>{name}</b> — {office} — <span class="muted">{phone_raw}</span></p>
        <p>Дата доставки: <b>{d.isoformat()}</b> (доставка 13:00)</p>
        <p><span class="pill">{opt_human}</span><span class="pill">Итого: {price}€</span></p>
        <ul>
          <li>Закуска: {zakuska or "—"}</li>
          <li>Суп: {soup}</li>
          <li>Горячее: {hot or "—"}</li>
          <li>Десерт: {dessert or "—"}</li>
        </ul>
        <p class="muted">Комментарий: {comment or "—"}</p>
        <p><a href="/edit?office={office}&date={d.isoformat()}&phone={phone_raw}">Изменить/отменить заказ</a></p>
      </div>
      <p><a href="/">Сделать ещё заказ</a></p>
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
    if phone_norm:
        conn = db()
        found = conn.execute(
            "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
            (office, d.isoformat(), phone_norm),
        ).fetchone()
        conn.close()

    ok_time, start, end, now_ = validate_order_time(d)
    office_opts = "".join([f"<option value='{o}' {'selected' if o==office else ''}>{o}</option>" for o in OFFICES])

    if found:
        hot_items = hot_menu_with_special(office, d)

        body = f"""
        <h1>Изменить / отменить заказ</h1>
        <div class="card">
          <p><span class="pill"><b>{found['order_code']}</b></span>
             <span class="pill">Доставка: {d.isoformat()} 13:00</span></p>

          <p class="muted">Окно изменений:
            <b>{start.strftime('%d.%m %H:%M')}</b> — <b>{end.strftime('%d.%m %H:%M')}</b>.
            Сейчас: <b>{now_.strftime('%d.%m %H:%M')}</b>.
          </p>
          {"<p class='danger'><b>Сейчас окно закрыто — изменения/отмена недоступны.</b></p>" if not ok_time else ""}

          <form method="post" action="/edit">
            <input type="hidden" name="office" value="{office}">
            <input type="hidden" name="order_date" value="{d.isoformat()}">
            <input type="hidden" name="phone" value="{found['phone_raw']}">

            <label>Имя</label>
            <input name="name" value="{found['name']}" required>

            <div class="row">
              <div>
                <label>Закуска (если нужна)</label>
                <select name="zakuska">
                  <option value="" {"selected" if not found["zakuska"] else ""}>— без закуски —</option>
                  {options_html(MENU["zakuska"])}
                </select>
              </div>
              <div>
                <label>Суп (обязательно)</label>
                <select name="soup" required>
                  <option value="">— выбери суп —</option>
                  {options_html(MENU["soup"])}
                </select>
              </div>
            </div>

            <div class="row">
              <div>
                <label>Горячее (если нужно)</label>
                <select name="hot">
                  <option value="" {"selected" if not found["hot"] else ""}>— без горячего —</option>
                  {options_html(hot_items)}
                </select>
              </div>
              <div>
                <label>Десерт (если нужен)</label>
                <select name="dessert">
                  <option value="" {"selected" if not found["dessert"] else ""}>— без десерта —</option>
                  {options_html(MENU["dessert"])}
                </select>
              </div>
            </div>

            <label>Комментарий</label>
            <textarea name="comment" rows="3">{found["comment"] or ""}</textarea>

            <button type="submit" {"disabled" if not ok_time else ""}>Сохранить изменения</button>
          </form>

          <form method="post" action="/cancel" style="margin-top:10px;">
            <input type="hidden" name="office" value="{office}">
            <input type="hidden" name="order_date" value="{d.isoformat()}">
            <input type="hidden" name="phone" value="{found['phone_raw']}">
            <button type="submit" {"disabled" if not ok_time else ""}>Отменить заказ</button>
          </form>

          <p class="muted">Телефон: <b>{found['phone_raw']}</b> (для поиска/уникальности нормализуется автоматически).</p>
          <p><a href="/">← На главную</a></p>
        </div>

        <script>
          (function(){{
            function setVal(name, val){{
              const el = document.querySelector("select[name='"+name+"']");
              if(!el) return;
              for(const opt of el.options){{
                if(opt.text === val) {{ opt.selected = true; return; }}
              }}
            }}
            setVal("zakuska", {repr(found["zakuska"] or "")});
            setVal("soup", {repr(found["soup"] or "")});
            setVal("hot", {repr(found["hot"] or "")});
            setVal("dessert", {repr(found["dessert"] or "")});
          }})();
        </script>
        """
        return html_page(body)

    body = f"""
    <h1>Изменить / отменить заказ</h1>
    <div class="card">
      <form method="get" action="/edit">
        <div class="row">
          <div>
            <label>Офис</label>
            <select name="office" required>{office_opts}</select>
          </div>
          <div>
            <label>Дата доставки</label>
            <input type="date" name="date" value="{d.isoformat()}" required>
          </div>
        </div>

        <label>Телефон (как в заказе)</label>
        <input name="phone" value="{phone_raw}" placeholder="+34..." required>

        <button type="submit">Найти заказ</button>
      </form>

      <p class="muted">Если заказ не найден — проверь офис, дату доставки и телефон.</p>
      <p><a href="/">← На главную</a></p>
    </div>
    """
    return html_page(body)


@app.post("/edit")
def edit_post():
    office = (request.form.get("office", "") or "").strip()
    if office not in OFFICES:
        return html_page("<p class='danger'>Ошибка: неизвестный офис.</p><p><a href='/edit'>Назад</a></p>"), 400

    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата.</p><p><a href='/edit'>Назад</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        return (
            html_page(
                f"<p class='danger'><b>Окно редактирования закрыто.</b><br>"
                f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
                f"<p><a href='/edit'>Назад</a></p>"
            ),
            403,
        )

    phone_raw = (request.form.get("phone", "") or "").strip()
    phone_norm = normalize_phone(phone_raw)
    if not phone_norm:
        return html_page("<p class='danger'>Ошибка: телефон обязателен.</p><p><a href='/edit'>Назад</a></p>"), 400

    name = (request.form.get("name", "") or "").strip()
    zakuska = (request.form.get("zakuska", "") or "").strip() or None
    soup = (request.form.get("soup", "") or "").strip()
    hot = (request.form.get("hot", "") or "").strip() or None
    dessert = (request.form.get("dessert", "") or "").strip() or None
    comment = (request.form.get("comment", "") or "").strip() or None

    if not name or not soup:
        return html_page("<p class='danger'>Ошибка: имя и суп обязательны.</p><p><a href='/edit'>Назад</a></p>"), 400

    option_code, price, err = compute_option_and_price(zakuska, soup, hot, dessert, office, d)
    if err:
        return html_page(f"<p class='danger'>Ошибка: {err}</p><p><a href='/edit'>Назад</a></p>"), 400

    conn = db()
    existing = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
        (office, d.isoformat(), phone_norm),
    ).fetchone()

    if not existing:
        conn.close()
        return html_page("<p class='danger'>Активный заказ не найден.</p><p><a href='/edit'>Назад</a></p>"), 404

    conn.execute(
        """
        UPDATE orders
        SET name=?, zakuska=?, soup=?, hot=?, dessert=?, option_code=?, price_eur=?, comment=?
        WHERE id=?
        """,
        (name, zakuska, soup, hot, dessert, option_code, int(price), comment, existing["id"]),
    )
    conn.commit()
    conn.close()

    opt_human = {"opt1": "Опция 1", "opt2": "Опция 2", "opt3": "Опция 3"}[option_code]
    return html_page(
        f"""
      <h2>✅ Изменения сохранены</h2>
      <div class="card">
        <p><span class="pill"><b>{existing['order_code']}</b></span></p>
        <p><b>{name}</b> — {office} — <span class="muted">{existing['phone_raw']}</span></p>
        <p>Дата доставки: <b>{d.isoformat()}</b> (13:00)</p>
        <p><span class="pill">{opt_human}</span><span class="pill">Итого: {price}€</span></p>
        <ul>
          <li>Закуска: {zakuska or "—"}</li>
          <li>Суп: {soup}</li>
          <li>Горячее: {hot or "—"}</li>
          <li>Десерт: {dessert or "—"}</li>
        </ul>
        <p class="muted">Комментарий: {comment or "—"}</p>
      </div>
      <p><a href="/">← На главную</a></p>
    """
    )


@app.post("/cancel")
def cancel_post():
    office = (request.form.get("office", "") or "").strip()
    if office not in OFFICES:
        return html_page("<p class='danger'>Ошибка: неизвестный офис.</p><p><a href='/edit'>Назад</a></p>"), 400

    order_date = (request.form.get("order_date", "") or "").strip()
    try:
        d = date.fromisoformat(order_date)
    except ValueError:
        return html_page("<p class='danger'>Ошибка: неверная дата.</p><p><a href='/edit'>Назад</a></p>"), 400

    ok_time, start, end, now_ = validate_order_time(d)
    if not ok_time:
        return (
            html_page(
                f"<p class='danger'><b>Окно отмены закрыто.</b><br>"
                f"<small>Окно: {start.strftime('%d.%m %H:%M')} — {end.strftime('%d.%m %H:%M')}. Сейчас: {now_.strftime('%d.%m %H:%M')}.</small></p>"
                f"<p><a href='/edit'>Назад</a></p>"
            ),
            403,
        )

    phone_raw = (request.form.get("phone", "") or "").strip()
    phone_norm = normalize_phone(phone_raw)
    if not phone_norm:
        return html_page("<p class='danger'>Ошибка: телефон обязателен.</p><p><a href='/edit'>Назад</a></p>"), 400

    conn = db()
    existing = conn.execute(
        "SELECT * FROM orders WHERE office=? AND order_date=? AND phone_norm=? AND status='active'",
        (office, d.isoformat(), phone_norm),
    ).fetchone()

    if not existing:
        conn.close()
        return html_page("<p class='danger'>Активный заказ не найден (возможно уже отменён).</p><p><a href='/edit'>Назад</a></p>"), 404

    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (existing["id"],))
    conn.commit()
    conn.close()

    return html_page(
        f"""
      <h2>🗑 Заказ отменён</h2>
      <div class="card">
        <p><span class="pill"><b>{existing['order_code']}</b></span></p>
        <p><b>{existing['name']}</b> — {office} — <span class="muted">{existing['phone_raw']}</span></p>
        <p>Дата доставки: <b>{d.isoformat()}</b></p>
        <p class="muted">Если нужно — оформите новый заказ (пока окно открыто).</p>
      </div>
      <p><a href="/">← На главную</a></p>
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
    plov_count = 0
    weekly_count = 0

    for r in active_rows:
        opt_counts[r["option_code"]] += 1
        for k in ["zakuska", "soup", "hot", "dessert"]:
            v = r[k]
            if v:
                dish_counts[v] = dish_counts.get(v, 0) + 1
        if r["hot"] and "Плов с бараниной" in r["hot"]:
            plov_count += 1
        if r["hot"] and r["hot"].startswith("Блюдо недели:"):
            weekly_count += 1

    special = get_weekly_special(office, d)
    conn.close()

    office_opts = "".join([f"<option value='{o}' {'selected' if o==office else ''}>{o}</option>" for o in OFFICES])

    def rows_list(rows):
        items = ""
        for r in rows:
            items += f"<li><b>{r['order_code']}</b> — <b>{r['name']}</b> <small class='muted'>({r['phone_raw']})</small> — {r['price_eur']}€ — {r['soup']}"
            if r["zakuska"]:
                items += f" / {r['zakuska']}"
            if r["hot"]:
                items += f" / {r['hot']}"
            if r["dessert"]:
                items += f" / {r['dessert']}"
            if r["comment"]:
                items += f" <small class='muted'>— {r['comment']}</small>"
            items += "</li>"
        return items or "<li class='muted'>—</li>"

    dish_list = "".join([f"<li>{k} — {v}</li>" for k, v in sorted(dish_counts.items(), key=lambda x: (-x[1], x[0]))])

    special_block = "<p class='muted'>Блюдо недели: —</p>"
    if special:
        special_block = f"<p><b>Блюдо недели:</b> {special['title']} (доплата +{int(special['surcharge_eur'])}€) <small class='muted'>[{special['start_date']} … {special['end_date']}]</small></p>"

    body = f"""
    <h1>Админка</h1>

    <div class="card">
      <form method="get" action="/admin">
        <input type="hidden" name="token" value="{ADMIN_TOKEN}">
        <div class="row">
          <div>
            <label>Офис</label>
            <select name="office">{office_opts}</select>
          </div>
          <div>
            <label>Дата</label>
            <input type="date" name="date" value="{d.isoformat()}">
          </div>
        </div>
        <button type="submit">Показать</button>
      </form>

      <p>
        <a href="/export.csv?office={office}&date={d.isoformat()}&token={ADMIN_TOKEN}">⬇️ CSV (активные)</a>
        &nbsp;|&nbsp;
        <a href="/admin/special?office={office}&date={d.isoformat()}&token={ADMIN_TOKEN}">⭐ Блюдо недели</a>
      </p>

      {special_block}

      <p><b>Активные:</b> {len(active_rows)} / {MAX_PER_DAY}</p>
      <p>
        <span class="pill">Опция 1: {opt_counts['opt1']}</span>
        <span class="pill">Опция 2: {opt_counts['opt2']}</span>
        <span class="pill">Опция 3: {opt_counts['opt3']}</span>
        <span class="pill">Плов: {plov_count}</span>
        <span class="pill">Блюдо недели: {weekly_count}</span>
      </p>
    </div>

    <div class="card">
      <h3>Список активных заказов</h3>
      <ol>{rows_list(active_rows)}</ol>
    </div>

    <div class="card">
      <h3>Отменённые заказы</h3>
      <ol>{rows_list(cancelled_rows)}</ol>
    </div>

    <div class="card">
      <h3>Сводка по блюдам (активные)</h3>
      <ul>{dish_list or "<li class='muted'>—</li>"}</ul>
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
    <h1>Блюдо недели</h1>
    <div class="card">
      <form method="post" action="/admin/special?token={ADMIN_TOKEN}">
        <label>Офис</label>
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

        <button type="submit">Сохранить</button>
      </form>

      <p class="muted">После сохранения появится в “Горячее” как “Блюдо недели: … (+X€)”.</p>
      <p><a href="/admin?office={office}&date={d.isoformat()}&token={ADMIN_TOKEN}">← Назад в админку</a></p>
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
    rows = conn.execute(
        """
        SELECT order_code, office, order_date, name, phone_raw, option_code, price_eur,
               zakuska, soup, hot, dessert, comment, status, created_at
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

    header = "order_code,office,order_date,name,phone,option_code,price_eur,zakuska,soup,hot,dessert,comment,status,created_at"
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
