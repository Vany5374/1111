import asyncio
import re
import logging
import os
import sys
import subprocess
import time
import uuid
import glob
import contextlib
import json

# ─────────────────────────────────────────────
#  📦 Автоустановка зависимостей
# ─────────────────────────────────────────────
REQUIRED_PACKAGES = ["aiogram", "aiohttp", "aiosqlite", "python-dotenv"]

def _ensure_packages(packages):
    import importlib
    missing = []
    for pkg in packages:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[setup] Отсутствуют пакеты: {missing}. Устанавливаю...", flush=True)
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--no-input", *missing]
            )
        except subprocess.CalledProcessError as e:
            print(f"[setup] ❌ Не удалось установить зависимости: {e}", flush=True)
            sys.exit(1)
        print("[setup] ✅ Зависимости установлены, продолжаю запуск...", flush=True)

_ensure_packages(REQUIRED_PACKAGES)

import aiosqlite
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ─────────────────────────────────────────────
#  📋 FSM СОСТОЯНИЯ
# ─────────────────────────────────────────────
class Order(StatesGroup):
    cat = State()
    svctype = State()
    subtype = State()
    service = State()
    link = State()
    extra = State()
    quantity = State()
    confirm = State()

class Admin(StatesGroup):
    topup_uid = State()
    topup_amount = State()
    broadcast = State()
    markup = State()
    ref_percent = State()

class Topup(StatesGroup):
    amount = State()

class Review(StatesGroup):
    waiting = State()

# ─────────────────────────────────────────────
#  ⚙️  НАСТРОЙКИ (ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ BOTHOST)
# ─────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()

BOT_TOKEN         = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID          = int(os.environ.get("ADMIN_ID", "8033170309"))
_EXTRA_ADMIN_IDS  = {6069313124}
ADMIN_IDS         = {ADMIN_ID} | _EXTRA_ADMIN_IDS
for _part in os.environ.get("ADMIN_IDS_EXTRA", "").split(","):
    _part = _part.strip()
    if _part.isdigit():
        ADMIN_IDS.add(int(_part))

MARKUP_PCT        = float(os.environ.get("MARKUP_PCT", "30"))
SMM_API_KEY       = os.environ.get("SMM_API_KEY", "").strip()
SMM_API_URL       = os.environ.get("SMM_API_URL", "https://twiboost.com/api/v2").strip()

ROLLYPAY_API_KEY     = os.environ.get("ROLLYPAY_API_KEY", "").strip()
ROLLYPAY_BASE_URL    = os.environ.get("ROLLYPAY_BASE_URL", "https://rollypay.io").strip()
ROLLYPAY_MIN_TOPUP   = float(os.environ.get("ROLLYPAY_MIN_TOPUP", "10"))
ROLLYPAY_MAX_TOPUP   = float(os.environ.get("ROLLYPAY_MAX_TOPUP", "100000"))
ROLLYPAY_POLL_SEC    = int(os.environ.get("ROLLYPAY_POLL_SEC", "15"))

DB_PATH           = os.path.abspath(os.path.join(_SCRIPT_DIR, "smm_bot.db"))
BACKUP_DIR        = os.path.abspath(os.path.join(_SCRIPT_DIR, "backups"))
REVIEW_CHAT_USERNAME = "RocketBoost111"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

if not BOT_TOKEN:
    log.critical("❌ BOT_TOKEN не задан в настройках хостинга!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp  = Dispatcher(storage=MemoryStorage())
BOT_USERNAME = ""

# ─────────────────────────────────────────────
#  📡 API И СЕТЬ (TWIBOOST И ROLLYPAY)
# ─────────────────────────────────────────────
HTTP_SESSION: aiohttp.ClientSession = None

async def get_http_session():
    global HTTP_SESSION
    if HTTP_SESSION is None or HTTP_SESSION.closed:
        HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    return HTTP_SESSION

async def api_request(action: str, **kwargs):
    if not SMM_API_KEY: return {"error": "API key missing"}
    data = {"key": SMM_API_KEY, "action": action}
    data.update(kwargs)
    try:
        session = await get_http_session()
        async with session.post(SMM_API_URL, data=data) as r:
            return json.loads(await r.text())
    except Exception as e:
        return {"error": str(e)}

async def api_services(): return await api_request("services")
async def api_add(svc, link, qty): return await api_request("add", service=svc, link=link, quantity=qty)
async def api_balance(): return await api_request("balance")

_SERVICES_CACHE = None
_SERVICES_CACHE_TIME = 0

async def api_services_cached():
    global _SERVICES_CACHE, _SERVICES_CACHE_TIME
    if _SERVICES_CACHE and (time.time() - _SERVICES_CACHE_TIME) < 300:
        return _SERVICES_CACHE
    res = await api_services()
    if isinstance(res, list):
        _SERVICES_CACHE = res
        _SERVICES_CACHE_TIME = time.time()
        return res
    return _SERVICES_CACHE or []

async def rp_request(method: str, path: str, json_body: dict = None):
    if not ROLLYPAY_API_KEY: return None, "RollyPay API key is missing"
    url = f"{ROLLYPAY_BASE_URL}{path}"
    headers = {"Content-Type": "application/json", "X-API-Key": ROLLYPAY_API_KEY, "X-Nonce": str(uuid.uuid4())}
    try:
        session = await get_http_session()
        async with session.request(method, url, json=json_body, headers=headers) as r:
            try: data = await r.json(content_type=None)
            except: data = None
            if r.status >= 400:
                msg = (data or {}).get("message") if isinstance(data, dict) else None
                return None, msg or f"HTTP {r.status}"
            return data, None
    except Exception as e:
        return None, str(e)

async def rp_create_payment(user_id: int, amount_rub: float):
    order_id = f"topup_{user_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    body = {
        "amount": f"{amount_rub:.2f}", "payment_currency": "RUB", "order_id": order_id,
        "description": f"Пополнение баланса (ID {user_id})", "metadata": {"telegram_user_id": str(user_id)}
    }
    return await rp_request("POST", "/api/v1/payments", body)

async def rp_get_payment(payment_id: str):
    return await rp_request("GET", f"/api/v1/payments/{payment_id}")

# ─────────────────────────────────────────────
#  🧩 КЛАССИФИКАТОР СОЦСЕТЕЙ
# ─────────────────────────────────────────────
CATEGORY_EMOJI = {
    "Telegram": "📱", "Instagram": "📸", "TikTok": "🎵", "YouTube": "▶️",
    "VK": "💙", "Twitter/X": "🐦", "Threads": "🧵", "Facebook": "📘",
    "Discord": "🎮", "Twitch": "🟣", "Spotify": "🟢", "SoundCloud": "🎧",
    "Одноклассники": "🧡", "Rutube": "📺", "Дзен": "🟧", "LinkedIn": "💼",
    "Pinterest": "📌", "Likee": "💫", "Yandex Music": "🍓", "MAX": "🆕", "Другое": "🌐"
}

NETWORK_KEYWORDS = {
    "Telegram": [r"telegram", r"телеграм", r"\btg\b"],
    "Instagram": [r"instagram", r"инстаграм", r"\big\b"],
    "TikTok": [r"tiktok", r"тик\s*-?\s*ток", r"\btt\b"],
    "YouTube": [r"youtube", r"ютуб", r"\byt\b"],
    "VK": [r"vkontakte", r"вконтакте", r"\bvk\b"],
    "Twitter/X": [r"twitter", r"твиттер", r"\bx\.com\b", r"\btw\b"],
    "Threads": [r"threads", r"тредс"],
    "Facebook": [r"facebook", r"фейсбук", r"\bfb\b"],
    "Discord": [r"discord", r"дискорд", r"\bdc\b"],
    "Twitch": [r"twitch", r"твич"],
    "Spotify": [r"spotify", r"спотифай"],
    "SoundCloud": [r"soundcloud", r"саундклауд"],
    "Одноклассники": [r"одноклассник", r"\bok\.ru\b"],
    "Rutube": [r"rutube", r"рутуб"],
    "Дзен": [r"дзен", r"zen"],
    "LinkedIn": [r"linkedin"],
    "Pinterest": [r"pinterest"],
    "Likee": [r"likee"],
    "Yandex Music": [r"яндекс\s*музык"],
    "MAX": [r"\bmax\b"],
}

ORDERED_NETWORKS = list(NETWORK_KEYWORDS.keys()) + ["Другое"]

def detect_network(svc: dict) -> str:
    text = f"{svc.get('category','')} {svc.get('name','')}".lower()
    if any(x in text for x in ["api", "dtf.ru"]): return "SKIP"
    for net, pats in NETWORK_KEYWORDS.items():
        for p in pats:
            if re.search(p, text): return net
    return "Другое"

SERVICE_TYPE_PATTERNS = {
    "Подписчики": [r"подписч", r"фолловер", r"участник", r"\bsub", r"\bfollow"],
    "Просмотры": [r"просмотр", r"\bview"],
    "Лайки": [r"лайк", r"\blike"],
    "Реакции": [r"реакц", r"\breaction"],
    "Комментарии": [r"коммент", r"\bcomment"],
}
TYPE_EMOJI = {"Подписчики": "👤", "Просмотры": "👁", "Лайки": "❤️", "Реакции": "⚡", "Комментарии": "💬", "Прочее": "📦"}

def detect_service_type(svc: dict) -> str:
    text = f"{svc.get('category','')} {svc.get('name','')}".lower()
    for stype, patterns in SERVICE_TYPE_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text): return stype
    return "Прочее"

# ─────────────────────────────────────────────
#  ⚖️ БАЗА ДАННЫХ И НАДЁЖНЫЕ БЭКАПЫ
# ─────────────────────────────────────────────
@contextlib.asynccontextmanager
async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        yield db

async def db_init():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    async with get_db() as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, balance REAL DEFAULT 0.0,
            total_spent REAL DEFAULT 0.0, referrer_id INTEGER DEFAULT 0, ref_earned REAL DEFAULT 0.0,
            review_asked INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service_id TEXT, service_name TEXT,
            link TEXT, quantity INTEGER, charge REAL, status TEXT DEFAULT 'pending',
            ext_order_id TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, note TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')))""")
        await db.execute("""CREATE TABLE IF NOT EXISTS rollypay_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, payment_id TEXT UNIQUE, order_id TEXT,
            amount REAL, status TEXT DEFAULT 'created', pay_url TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')))""")
        await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        
        await db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('markup_pct','30')")
        await db.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('ref_percent','10')")
        await db.commit()

async def create_backup(reason: str = "manual") -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = os.path.join(BACKUP_DIR, f"smm_bot_{ts}_{reason}.db")
    async with aiosqlite.connect(DB_PATH) as src, aiosqlite.connect(backup_file) as dst:
        await src.backup(dst)
    return backup_file

async def auto_backup_loop():
    await asyncio.sleep(15)
    while True:
        try:
            bf = await create_backup("auto")
            doc = FSInputFile(bf, filename=os.path.basename(bf))
            for aid in ADMIN_IDS:
                with contextlib.suppress(Exception):
                    await bot.send_document(aid, doc, caption=f"📦 <b>Автобэкап базы данных</b>\n🕒 {time.strftime('%d.%m.%Y %H:%M:%S')}")
        except Exception as e:
            log.error(f"Backup loop error: {e}")
        await asyncio.sleep(6 * 3600)

async def get_setting(key, default):
    async with get_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            r = await c.fetchone()
            return r[0] if r else default

async def set_setting(key, value):
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, str(value)))
        await db.commit()

async def get_balance(uid) -> float:
    async with get_db() as db:
        async with db.execute("SELECT balance FROM users WHERE user_id=?", (uid,)) as c:
            r = await c.fetchone()
            if r and r[0] is not None:
                return float(r[0])
            return 0.0

async def change_balance(uid, delta: float):
    async with get_db() as db:
        # Убедимся, что пользователь есть в базе перед обновлением баланса
        await db.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0.0)", (uid,))
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (float(delta), uid))
        await db.commit()

async def ensure_user(user: types.User, ref_id=0):
    async with get_db() as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name, referrer_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name
        """, (user.id, user.username or "", user.full_name or "", ref_id))
        await db.commit()

def kb_main(is_admin=False):
    b = ReplyKeyboardBuilder()
    b.button(text="🛒 Каталог услуг"); b.button(text="💰 Баланс")
    b.button(text="💳 Пополнить"); b.button(text="📋 Мои заказы")
    b.button(text="🤝 Партнёрка"); b.button(text="ℹ️ Помощь")
    b.button(text="📄 Информация")
    if is_admin: b.button(text="⚙️ Админ")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)

# ─────────────────────────────────────────────
#  ОСНОВНОЙ КАТАЛОГ С ПАГИНАЦИЕЙ
# ─────────────────────────────────────────────
SERVICES_PAGE_SIZE = 10

def build_services_keyboard(svcs: list, markup: float, page: int, total_pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    start = page * SERVICES_PAGE_SIZE
    chunk = svcs[start:start + SERVICES_PAGE_SIZE]
    for s in chunk:
        price = round(float(s['rate']) * (1 + markup / 100), 2)
        mark = "♻️ " if "гаранти" in s.get('name', '').lower() or s.get('refill') else ""
        name = s.get('name', '')[:28]
        kb.button(text=f"{price}₽ • {mark}{name}", callback_data=f"svc:{s['service']}")
    kb.adjust(1)

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️ Назад", callback_data=f"svcpg:{page-1}")
    if total_pages > 1:
        nav.button(text=f"📄 {page+1}/{total_pages}", callback_data="noop")
    if page + 1 < total_pages:
        nav.button(text="Вперед ▶️", callback_data=f"svcpg:{page+1}")
    nav.adjust(3)
    kb.attach(nav)

    back = InlineKeyboardBuilder()
    back.button(text="◀️ Назад к категориям", callback_data="back_types")
    back.adjust(1)
    kb.attach(back)
    return kb.as_markup()

@dp.message(F.text.in_(["🛒 Каталог услуг"]))
async def catalog_main(msg: types.Message, state: FSMContext):
    await state.clear()
    await ensure_user(msg.from_user)
    
    msg_wait = await msg.answer("⏳ Загрузка...")
    services = await api_services_cached()
    
    net_groups = {}
    for s in services:
        net = detect_network(s)
        if net == "SKIP": continue
        net_groups.setdefault(net, []).append(s)
    
    nets = [n for n in ORDERED_NETWORKS if n in net_groups]
    await state.update_data(net_groups=net_groups, nets=nets)
    
    kb = InlineKeyboardBuilder()
    for i, n in enumerate(nets):
        kb.button(text=f"{CATEGORY_EMOJI.get(n, '🌐')} {n} ({len(net_groups[n])})", callback_data=f"net:{i}")
    kb.adjust(2)
    
    await msg_wait.delete()
    await msg.answer("📱 <b>Выбери соцсеть:</b>", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("net:"))
async def catalog_types(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = int(cb.data.split(":")[1])
    net = data["nets"][idx]
    svcs = data["net_groups"][net]
    
    type_groups = {}
    for s in svcs:
        t = detect_service_type(s)
        type_groups.setdefault(t, []).append(s)
    
    types_list = sorted(list(type_groups.keys()))
    await state.update_data(cur_net=net, type_groups=type_groups, types_list=types_list)
    
    kb = InlineKeyboardBuilder()
    for i, t in enumerate(types_list):
        kb.button(text=f"{TYPE_EMOJI.get(t, '📦')} {t} ({len(type_groups[t])})", callback_data=f"type:{i}")
    kb.button(text="◀️ Назад", callback_data="back_nets")
    kb.adjust(2)
    await cb.message.edit_text(f"📱 <b>{net}</b> — ⚙️ <b>Выбери тип услуги:</b>", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("type:"))
async def catalog_services(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = int(cb.data.split(":")[1])
    t_name = data["types_list"][idx]
    svcs = sorted(data["type_groups"][t_name], key=lambda x: float(x['rate']))
    
    await state.update_data(cur_type=t_name, final_svcs=svcs, cur_page=0)
    markup = float(await get_setting("markup_pct", "30"))
    total_pages = max(1, (len(svcs) + SERVICES_PAGE_SIZE - 1) // SERVICES_PAGE_SIZE)
    
    kb = build_services_keyboard(svcs, markup, 0, total_pages)
    await cb.message.edit_text(f"📦 <b>{data['cur_net']} / {t_name}</b> (Всего: {len(svcs)}):", reply_markup=kb)

@dp.callback_query(F.data.startswith("svcpg:"))
async def catalog_page_nav(cb: types.CallbackQuery, state: FSMContext):
    page = int(cb.data.split(":")[1])
    data = await state.get_data()
    svcs = data.get("final_svcs", [])
    if not svcs:
        await cb.answer()
        return
    markup = float(await get_setting("markup_pct", "30"))
    total_pages = max(1, (len(svcs) + SERVICES_PAGE_SIZE - 1) // SERVICES_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    await state.update_data(cur_page=page)
    
    kb = build_services_keyboard(svcs, markup, page, total_pages)
    await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data == "noop")
async def noop_click(cb: types.CallbackQuery):
    await cb.answer()

@dp.callback_query(F.data == "back_nets")
async def back_to_nets(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    kb = InlineKeyboardBuilder()
    for i, n in enumerate(data["nets"]):
        kb.button(text=f"{CATEGORY_EMOJI.get(n, '🌐')} {n} ({len(data['net_groups'][n])})", callback_data=f"net:{i}")
    kb.adjust(2)
    await cb.message.edit_text("📱 <b>Выбери соцсеть:</b>", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "back_types")
async def back_to_types(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    kb = InlineKeyboardBuilder()
    for i, t in enumerate(data["types_list"]):
        kb.button(text=f"{TYPE_EMOJI.get(t, '📦')} {t} ({len(data['type_groups'][t])})", callback_data=f"type:{i}")
    kb.button(text="◀️ Назад", callback_data="back_nets")
    kb.adjust(2)
    await cb.message.edit_text(f"📱 <b>{data['cur_net']}</b> — ⚙️ <b>Выбери тип услуги:</b>", reply_markup=kb.as_markup())

# ─────────────────────────────────────────────
#  ОФОРМЛЕНИЕ ЗАКАЗА
# ─────────────────────────────────────────────
@dp.callback_query(F.data.startswith("svc:"))
async def order_start(cb: types.CallbackQuery, state: FSMContext):
    sid = cb.data.split(":")[1]
    data = await state.get_data()
    svc = next(s for s in data["final_svcs"] if str(s["service"]) == sid)
    
    markup = float(await get_setting("markup_pct", "30"))
    price = round(float(svc['rate']) * (1 + markup/100), 2)
    desc = re.sub(r'<[^>]+>', '', svc.get('description', '')).strip()
    
    await state.update_data(svc_id=sid, svc_name=svc['name'], rate=price, minq=int(svc['min']))
    await state.set_state(Order.link)
    
    text = f"✅ <b>{svc['name']}</b>\n💰 Цена: {price}₽ / 1000\n\n📄 <b>Описание:</b>\n<i>{desc[:600]}</i>\n\n🔗 Введи ссылку:"
    await cb.message.edit_text(text)

@dp.message(Order.link)
async def order_link(msg: types.Message, state: FSMContext):
    await state.update_data(link=msg.text.strip())
    data = await state.get_data()
    await state.set_state(Order.quantity)
    await msg.answer(f"📦 Введи количество (Мин: {data['minq']}):")

@dp.message(Order.quantity)
async def order_confirm(msg: types.Message, state: FSMContext):
    try:
        qty = int(msg.text.strip())
        data = await state.get_data()
        if qty < data['minq']: raise ValueError
        total = round((data['rate']/1000)*qty, 2)
        await state.update_data(qty=qty, total=total)
        await state.set_state(Order.confirm)
        
        bal = await get_balance(msg.from_user.id)
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Подтвердить", callback_data="do_order")
        kb.button(text="❌ Отменить", callback_data="cancel_order")
        
        await msg.answer(f"📝 <b>Подтверждение заказа:</b>\n\nУслуга: {data['svc_name']}\nКол-во: {qty}\nИтого: <b>{total}₽</b>\nВаш баланс: {bal:.2f}₽", reply_markup=kb.as_markup())
    except:
        await msg.answer("Введите правильное число!")

@dp.callback_query(F.data == "do_order")
async def order_final(cb: types.CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    data = await state.get_data()
    
    bal = await get_balance(uid)
    if bal < data['total']:
        await cb.answer("❌ Недостаточно средств!", show_alert=True); return
        
    await cb.message.edit_text("⏳ Загрузка...")
    res = await api_add(data['svc_id'], data['link'], data['qty'])
    
    if isinstance(res, dict) and "order" in res:
        ext_id = res["order"]
        await change_balance(uid, -data['total'])
        async with get_db() as db:
            await db.execute("INSERT INTO orders (user_id,service_id,service_name,link,quantity,charge,ext_order_id) VALUES (?,?,?,?,?,?,?)",
                             (uid, data['svc_id'], data['svc_name'], data['link'], data['qty'], data['total'], str(ext_id)))
            await db.execute("UPDATE users SET total_spent = total_spent + ? WHERE user_id=?", (data['total'], uid))
            await db.commit()
        await cb.message.edit_text(f"✅ <b>Заказ принят!</b>\nID: <code>{ext_id}</code>")
        for aid in ADMIN_IDS:
            with contextlib.suppress(Exception):
                await bot.send_message(aid, f"💰 Новый заказ #{ext_id}\nЮзер: {uid}\nСумма: {data['total']}₽")
        await state.clear()
    else:
        err = res.get("error", "Error") if isinstance(res, dict) else str(res)
        await cb.message.edit_text(f"❌ Ошибка: {err}\n\nДеньги не списаны.")
        await state.clear()

@dp.callback_query(F.data == "cancel_order")
async def order_cancel_cb(cb: types.CallbackQuery, state: FSMContext):
    await state.clear(); await cb.message.edit_text("❌ Заказ отменен.")

# ─────────────────────────────────────────────
#  ПОПОЛНЕНИЕ ЧЕРЕЗ ROLLYPAY
# ─────────────────────────────────────────────
@dp.message(F.text.in_(["💳 Пополнить"]))
async def topup_menu(msg: types.Message, state: FSMContext):
    await ensure_user(msg.from_user)
    if not ROLLYPAY_API_KEY:
        await msg.answer("💳 Пополнение временно недоступно. Укажите ROLLYPAY_API_KEY в переменных окружения Bothost.")
        return
    await state.set_state(Topup.amount)
    await msg.answer(f"💳 <b>Пополнение баланса</b>\n\nВведите сумму пополнения в рублях (от {ROLLYPAY_MIN_TOPUP} до {ROLLYPAY_MAX_TOPUP} ₽):")

@dp.message(Topup.amount)
async def topup_process_amount(msg: types.Message, state: FSMContext):
    try:
        amount = float(msg.text.strip().replace(",", "."))
        if not (ROLLYPAY_MIN_TOPUP <= amount <= ROLLYPAY_MAX_TOPUP): raise ValueError
    except:
        await msg.answer(f"⚠️ Введите корректную сумму от {ROLLYPAY_MIN_TOPUP} до {ROLLYPAY_MAX_TOPUP} ₽:")
        return
    
    await state.clear()
    user_id = msg.from_user.id
    await ensure_user(msg.from_user)
    
    data, err = await rp_create_payment(user_id, amount)
    if err or not data or not data.get("pay_url"):
        await msg.answer(f"❌ Ошибка платежной системы: {err or 'Не удалось создать ссылку'}")
        return
        
    payment_id = data.get("payment_id")
    pay_url = data.get("pay_url")
    
    async with get_db() as db:
        await db.execute("INSERT INTO rollypay_payments (user_id, payment_id, order_id, amount, pay_url) VALUES (?,?,?,?,?)",
                         (user_id, payment_id, data.get("order_id"), amount, pay_url))
        await db.commit()
        
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", url=pay_url)
    kb.button(text="🔄 Проверить оплату", callback_data=f"chk_pay:{payment_id}")
    kb.adjust(1)
    
    await msg.answer(f"✅ Счет создан на сумму <b>{amount:.2f} ₽</b>\n\nНажмите кнопку ниже для оплаты:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("chk_pay:"))
async def check_rollypay_payment(cb: types.CallbackQuery):
    payment_id = cb.data.split(":")[1]
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM rollypay_payments WHERE payment_id=?", (payment_id,)) as c:
            row = await c.fetchone()
            
    if not row or row["status"] == "paid":
        await cb.answer("Платёж уже зачислен или не найден", show_alert=True); return
        
    data, err = await rp_get_payment(payment_id)
    if data and data.get("status") == "paid":
        amount = float(row["amount"])
        user_id = row["user_id"]
        
        async with get_db() as db:
            await db.execute("UPDATE rollypay_payments SET status='paid' WHERE payment_id=?", (payment_id,))
            await db.execute("INSERT INTO topups (user_id, amount, note) VALUES (?,?,?)", (user_id, amount, "RollyPay"))
            await db.commit()
            
        await change_balance(user_id, amount)
        
        ref_pct = float(await get_setting("ref_percent", "10"))
        if ref_pct > 0:
            async with get_db() as db:
                async with db.execute("SELECT referrer_id FROM users WHERE user_id=?", (user_id,)) as c:
                    r = await c.fetchone()
                    if r and r[0]:
                        ref_id = r[0]
                        bonus = round(amount * ref_pct / 100, 2)
                        await db.execute("UPDATE users SET balance = balance + ?, ref_earned = ref_earned + ? WHERE user_id=?", (bonus, bonus, ref_id))
                        await db.commit()
                        with contextlib.suppress(Exception):
                            await bot.send_message(ref_id, f"🤝 Реферальный бонус: +{bonus} ₽ за пополнение реферала!")

        await cb.message.edit_text(f"✅ <b>Баланс успешно пополнен на {amount:.2f} ₽!</b>")
        with contextlib.suppress(Exception):
            await bot.send_message(user_id, f"💰 Баланс пополнен на <b>{amount:.2f} ₽</b>!")
    else:
        await cb.answer("⏳ Платёж еще не поступил. Оплатите по ссылке.", show_alert=True)

# ─────────────────────────────────────────────
#  КНОПКИ МЕНЮ
# ─────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start_msg(msg: types.Message):
    await ensure_user(msg.from_user)
    is_admin = msg.from_user.id in ADMIN_IDS
    bal = await get_balance(msg.from_user.id)
    text = (
        f"👋 Привет, <b>{msg.from_user.first_name}</b>!\n\n"
        f"🚀 <b>SMM-панель</b> — накрутка подписчиков, просмотров и активности в 15+ соцсетях.\n\n"
        f"💰 Твой баланс: <b>{bal:.2f} ₽</b>\n\n"
        f"Нажми <b>🛒 Каталог услуг</b> чтобы сделать заказ 👇"
    )
    await msg.answer(text, reply_markup=kb_main(is_admin))

@dp.message(F.text.in_(["💰 Баланс"]))
async def balance_msg(msg: types.Message):
    await ensure_user(msg.from_user)
    bal = await get_balance(msg.from_user.id)
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(charge),0) FROM orders WHERE user_id=?", (msg.from_user.id,)) as c:
            row = await c.fetchone()
            cnt = row[0] if row else 0
            spent = float(row[1]) if row and row[1] is not None else 0.0
            
    text = (
        f"💰 <b>Баланс</b>\n\n"
        f"Доступно: <b>{bal:.2f} ₽</b>\n"
        f"Потрачено: <b>{spent:.2f} ₽</b>\n"
        f"Заказов: <b>{cnt}</b>\n\n"
        f"Для пополнения нажмите кнопку <b>💳 Пополнить</b> ниже."
    )
    await msg.answer(text)

@dp.message(F.text.in_(["📋 Мои заказы"]))
async def orders_msg(msg: types.Message):
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (msg.from_user.id,)) as c:
            rows = await c.fetchall()
    if not rows: await msg.answer("Заказов нет."); return
    lines = ["📋 Последние заказы:\n"]
    for r in rows: lines.append(f"• #{r['id']} | {r['service_name'][:20]} | {r['charge']}₽")
    await msg.answer("\n".join(lines))

@dp.message(F.text.in_(["🤝 Партнёрка"]))
async def partner_msg(msg: types.Message):
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{msg.from_user.id}"
    ref_pct = await get_setting("ref_percent", "10")
    await msg.answer(f"🤝 Партнерская программа\n\nПолучайте <b>{ref_pct}%</b> с пополнений друзей.\nСсылка: <code>{link}</code>")

@dp.message(F.text.in_(["ℹ️ Помощь"]))
async def help_msg(msg: types.Message):
    await msg.answer("📖 Нажмите «Каталог», выберите соцсеть и услугу, вставьте ссылку и оплатите.")

@dp.message(F.text.in_(["📄 Информация"]))
async def info_msg(msg: types.Message):
    await msg.answer("📄 SMM-Бот для продвижения в социальных сетях.")

# ─────────────────────────────────────────────
#  👑 АДМИН-ПАНЕЛЬ
# ─────────────────────────────────────────────
async def get_stats_data():
    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c: users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(charge),0) FROM orders") as c: orders, rev = await c.fetchone()
    return users, orders, rev

def kb_admin_full(markup_pct, ref_pct):
    b = InlineKeyboardBuilder()
    b.button(text="👥 Пользователи", callback_data="adm:users:0")
    b.button(text="📊 Статистика", callback_data="adm:stats")
    b.button(text="💸 Пополнить баланс", callback_data="adm:topup")
    b.button(text="📢 Рассылка", callback_data="adm:broadcast")
    b.button(text=f"🏷 Наценка: {markup_pct}%", callback_data="adm:markup")
    b.button(text=f"🤝 Реф. процент: {ref_pct}%", callback_data="adm:refpct")
    b.button(text="💾 Скачать бэкап", callback_data="adm:backup")
    b.button(text="💼 Баланс поставщика", callback_data="adm:tbbalance")
    b.adjust(2)
    return b.as_markup()

@dp.message(F.text.in_(["⚙️ Админ"]))
async def admin_msg(msg: types.Message):
    if msg.from_user.id not in ADMIN_IDS: return
    users, orders, rev = await get_stats_data()
    markup = await get_setting("markup_pct", "30")
    ref_pct = await get_setting("ref_percent", "10")
    
    text = f"⚙️ <b>Админ-панель управления:</b>\n\n👥 Пользователей: <b>{users}</b>\n📦 Заказов: <b>{orders}</b>\n💸 Оборот: <b>{rev:.2f} ₽</b>"
    await msg.answer(text, reply_markup=kb_admin_full(markup, ref_pct))

@dp.callback_query(F.data == "adm:back")
async def adm_back_cb(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    users, orders, rev = await get_stats_data()
    markup = await get_setting("markup_pct", "30")
    ref_pct = await get_setting("ref_percent", "10")
    text = f"⚙️ <b>Админ-панель управления:</b>\n\n👥 Пользователей: <b>{users}</b>\n📦 Заказов: <b>{orders}</b>\n💸 Оборот: <b>{rev:.2f} ₽</b>"
    await cb.message.edit_text(text, reply_markup=kb_admin_full(markup, ref_pct))

@dp.callback_query(F.data == "adm:stats")
async def adm_stats_cb(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    users, orders, rev = await get_stats_data()
    markup = await get_setting("markup_pct", "30")
    ref_pct = await get_setting("ref_percent", "10")
    text = f"📊 <b>Детальная статистика:</b>\n\n👥 Всего пользователей: <b>{users}</b>\n📦 Всего заказов: <b>{orders}</b>\n💸 Общий оборот: <b>{rev:.2f} ₽</b>"
    await cb.message.edit_text(text, reply_markup=kb_admin_full(markup, ref_pct))

@dp.callback_query(F.data.startswith("adm:users"))
async def adm_users_cb(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    parts = cb.data.split(":")
    page = int(parts[2]) if len(parts) > 2 else 0
    page_size = 10

    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total_users = (await c.fetchone())[0]

        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT user_id, username, full_name, balance FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (page_size, page * page_size)
        ) as c:
            rows = await c.fetchall()

    if total_users == 0 or not rows:
        await cb.message.edit_text("👥 <b>Пользователи:</b>\n\nВ базе пока нет зарегистрированных пользователей.", reply_markup=InlineKeyboardBuilder().button(text="◀️ В админку", callback_data="adm:back").as_markup())
        return

    lines = [f"👥 <b>Пользователи (Всего: {total_users}):</b>\n"]
    for u in rows:
        tag = f"@{u['username']}" if u['username'] else "<i>без тега</i>"
        name = u['full_name'] or "—"
        bal = float(u['balance']) if u['balance'] is not None else 0.0
        lines.append(f"• ID: <code>{u['user_id']}</code> | {tag} ({name}) | <b>{bal:.2f} ₽</b>")

    total_pages = max(1, (total_users + page_size - 1) // page_size)
    
    kb = InlineKeyboardBuilder()
    if page > 0:
        kb.button(text="◀️ Назад", callback_data=f"adm:users:{page-1}")
    if total_pages > 1:
        kb.button(text=f"📄 {page+1}/{total_pages}", callback_data="noop")
    if page + 1 < total_pages:
        kb.button(text="Вперед ▶️", callback_data=f"adm:users:{page+1}")
    kb.button(text="◀️ В админку", callback_data="adm:back")
    kb.adjust(3, 1)

    await cb.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())

@dp.callback_query(F.data == "adm:backup")
async def adm_backup_cb(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    await cb.answer("⏳ Создаю бэкап базы...")
    try:
        bf = await create_backup("manual")
        doc = FSInputFile(bf, filename=os.path.basename(bf))
        await bot.send_document(
            cb.from_user.id,
            doc,
            caption=f"💾 <b>Ручной бэкап базы данных готов!</b>\n🕒 {time.strftime('%d.%m.%Y %H:%M:%S')}"
        )
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка бэкапа: {e}")

@dp.callback_query(F.data == "adm:topup")
async def adm_topup_cb(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(Admin.topup_uid)
    await cb.message.edit_text("💸 <b>Пополнение баланса</b>\n\nВведите Telegram ID пользователя:")

@dp.message(Admin.topup_uid)
async def adm_topup_uid_msg(msg: types.Message, state: FSMContext):
    try: uid = int(msg.text.strip())
    except: await msg.answer("⚠️ Введите числовой Telegram ID:"); return
    await state.update_data(target_uid=uid)
    await state.set_state(Admin.topup_amount)
    await msg.answer(f"💸 Введите сумму для начисления пользователю <code>{uid}</code> (например, 500 или -100):")

@dp.message(Admin.topup_amount)
async def adm_topup_amount_msg(msg: types.Message, state: FSMContext):
    try: amt = float(msg.text.strip().replace(",", "."))
    except: await msg.answer("⚠️ Введите число:"); return
    data = await state.get_data()
    uid = data["target_uid"]
    await change_balance(uid, amt)
    is_admin = msg.from_user.id in ADMIN_IDS
    await msg.answer(f"✅ Баланс пользователя <code>{uid}</code> изменён на <b>{amt:+.2f} ₽</b>", reply_markup=kb_main(is_admin))
    with contextlib.suppress(Exception):
        await bot.send_message(uid, f"💰 Ваш баланс изменён на <b>{amt:+.2f} ₽</b>")
    await state.clear()

@dp.callback_query(F.data == "adm:broadcast")
async def adm_broadcast_cb(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(Admin.broadcast)
    await cb.message.edit_text("📢 <b>Рассылка сообщений</b>\n\nВведите текст рассылки (поддерживается HTML):")

@dp.message(Admin.broadcast)
async def adm_broadcast_send_msg(msg: types.Message, state: FSMContext):
    if msg.from_user.id not in ADMIN_IDS: return
    async with get_db() as db:
        async with db.execute("SELECT user_id FROM users") as c:
            users = await c.fetchall()
    sent, errs = 0, 0
    status_msg = await msg.answer(f"📢 Выполняется рассылка... 0/{len(users)}")
    for u in users:
        try:
            await bot.send_message(u[0], msg.text, parse_mode="HTML")
            sent += 1
        except: errs += 1
        await asyncio.sleep(0.04)
    await status_msg.edit_text(f"📢 <b>Рассылка завершена!</b>\n\n✅ Успешно: <b>{sent}</b>\n❌ Ошибок: <b>{errs}</b>")
    await state.clear()

@dp.callback_query(F.data == "adm:markup")
async def adm_markup_cb(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(Admin.markup)
    await cb.message.edit_text(f"🏷 Текущая наценка: <b>{await get_setting('markup_pct', '30')}%</b>\n\nВведите новую наценку в процентах:")

@dp.message(Admin.markup)
async def adm_markup_save(msg: types.Message, state: FSMContext):
    try:
        val = float(msg.text.strip().replace(",", "."))
        await set_setting("markup_pct", str(val))
        is_admin = msg.from_user.id in ADMIN_IDS
        await msg.answer(f"✅ Наценка успешно обновлена: <b>{val}%</b>", reply_markup=kb_main(is_admin))
        await state.clear()
    except:
        await msg.answer("⚠️ Введите число!")

@dp.callback_query(F.data == "adm:refpct")
async def adm_refpct_cb(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS: return
    await state.set_state(Admin.ref_percent)
    await cb.message.edit_text(f"🤝 Текущий реферальный процент: <b>{await get_setting('ref_percent', '10')}%</b>\n\nВведите новый процент:")

@dp.message(Admin.ref_percent)
async def adm_refpct_save(msg: types.Message, state: FSMContext):
    try:
        val = float(msg.text.strip().replace(",", "."))
        await set_setting("ref_percent", str(val))
        is_admin = msg.from_user.id in ADMIN_IDS
        await msg.answer(f"✅ Реферальный процент успешно обновлен: <b>{val}%</b>", reply_markup=kb_main(is_admin))
        await state.clear()
    except:
        await msg.answer("⚠️ Введите число!")

@dp.callback_query(F.data == "adm:tbbalance")
async def adm_bal_cb(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    r = await api_balance()
    markup = await get_setting("markup_pct", "30")
    ref_pct = await get_setting("ref_percent", "10")
    txt = f"💼 Баланс у поставщика: <b>{r.get('balance', 'Ошибка')} ₽</b>" if r else "❌ Ошибка запроса API"
    await cb.message.edit_text(txt, reply_markup=kb_admin_full(markup, ref_pct))

# ─────────────────────────────────────────────
#  ЗАПУСК
# ─────────────────────────────────────────────
async def main():
    await db_init()
    asyncio.create_task(auto_backup_loop())
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
