"""
HUB BOT v4 - avto-to'lov tasdiqlovchi + obuna tizimi.

Yangiliklar (v3 ga nisbatan):
- Admin panel: /admin (statistika, botlar tushumi, foydalanuvchilar, ulangan botlar,
  qo'lda obuna, balans qo'shish, obuna narxi, bot limiti, karta raqami, zaxira/tiklash)
- Foydalanuvchi paneli: profil, balans, obuna tugash vaqti, uzaytirish, botlarim
- Balans to'ldirish: admin kartasiga unikal summa orqali AVTOMATIK (Xabarkart orqali)
- Har bir mijozga bir nechta bot ulash (limit bilan), har bir botning o'z kaliti
- Barcha vaqtlar O'zbekiston vaqti (UTC+5)
- Zaxira KANALI: har o'zgarishda pinlangan xabar yangilanadi; baza bo'sh bo'lsa (Render deploy)
  bot o'zi kanaldagi pindan tiklaydi. Eski to'lovlar qayta tasdiqlanmaydi (done jadvali + eski xabar filtri)

O'rnatish:  pip install -U aiogram aiohttp
Ishga tushirish:  python hub_bot_4.py
Eski hub.db fayl avtomatik yangilanadi (migratsiya).
"""
import asyncio
import html
import os
import random
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (BusinessConnection, CallbackQuery, FSInputFile,
                           InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument,
                           Message)
from aiohttp import web

# ------------------------- SOZLAMALAR -------------------------
TOKEN = os.getenv("TOKEN", "")
HUB_USERNAME = os.getenv("HUB_USERNAME", "")             # @ siz
XABARKART = os.getenv("XABARKART", "").lower().lstrip("@")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
PENDING_MINUTES = int(os.getenv("PENDING_MINUTES", "2"))   # mijoz botlari to'lovi uchun
DB_PATH = os.getenv("DB_PATH", "hub.db")
DEBUG = os.getenv("DEBUG", "1") == "1"                   # sinovda 1, keyin 0

# Obuna / balans (admin paneldan ham o'zgartiriladi, panel qiymati ustun turadi)
CARD_NUMBER = os.getenv("CARD_NUMBER", "")               # karta raqamingiz
CARD_HOLDER = os.getenv("CARD_HOLDER", "")               # karta egasi (ixtiyoriy)
SUB_PRICE = int(os.getenv("SUB_PRICE", "50000"))         # boshlang'ich narx
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))              # bir obuna necha kun
MAX_BOTS = int(os.getenv("MAX_BOTS", "3"))               # boshlang'ich bot limiti
TOPUP_MINUTES = int(os.getenv("TOPUP_MINUTES", "10"))    # balans to'ldirish uchun vaqt
MIN_TOPUP = int(os.getenv("MIN_TOPUP", "1000"))
MAX_TOPUP = int(os.getenv("MAX_TOPUP", "5000000"))
AUTO_BACKUP_HOURS = float(os.getenv("AUTO_BACKUP_HOURS", "24"))  # adminga (lichkaga) zaxira, 0 = o'chiq
# Zaxira KANALI: bot kanal admini bo'lishi kerak (xabar yozish, tahrirlash, pin qilish huquqi)
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0)   # masalan -1001234567890
BACKUP_MIN_SECONDS = int(os.getenv("BACKUP_MIN_SECONDS", "30"))     # o'zgarishdan keyin eng ko'p shuncha kutadi
# ---------------------------------------------------------------

_missing = [n for n, v in (("TOKEN", TOKEN), ("XABARKART", XABARKART), ("ADMIN_ID", ADMIN_ID)) if not v]
if _missing:
    raise SystemExit("Muhit o'zgaruvchilari yetishmayapti: " + ", ".join(_missing))

bot = Bot(TOKEN)
dp = Dispatcher()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS owners(owner_id INTEGER PRIMARY KEY, bot_username TEXT,
    api_key TEXT UNIQUE, active_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bconn(bc_id TEXT PRIMARY KEY, owner_id INTEGER);
CREATE TABLE IF NOT EXISTS pending(owner_id INTEGER, amount INTEGER, user_id INTEGER, exp REAL,
    PRIMARY KEY(owner_id, amount));
CREATE TABLE IF NOT EXISTS done(bc_id TEXT, chat_id INTEGER, msg_id INTEGER,
    PRIMARY KEY(bc_id, chat_id, msg_id));
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER,
    user_id INTEGER, amount INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS bots(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER,
    username TEXT, api_key TEXT UNIQUE, created REAL, removed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    amount INTEGER, ts REAL, kind TEXT DEFAULT 'card');
CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER,
    amount INTEGER, days INTEGER, ts REAL);
""")


def add_col(table: str, col: str, ddl: str):
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def migrate():
    """Eski (v3) bazani yangi tuzilishga o'tkazadi. Qayta chaqirish xavfsiz."""
    add_col("owners", "balance", "INTEGER DEFAULT 0")
    add_col("owners", "max_bots", "INTEGER")
    add_col("owners", "username", "TEXT")
    add_col("owners", "full_name", "TEXT")
    add_col("owners", "created", "REAL")
    add_col("pending", "kind", "TEXT DEFAULT 'api'")
    add_col("pending", "bot_id", "INTEGER")
    add_col("events", "bot_id", "INTEGER")
    # eski bitta-bot ma'lumotini bots jadvaliga ko'chiramiz, keyin eski ustunni tozalaymiz
    db.execute("INSERT OR IGNORE INTO bots(owner_id,username,api_key,created) "
               "SELECT owner_id,bot_username,api_key,? FROM owners "
               "WHERE api_key IS NOT NULL AND api_key!=''", (time.time(),))
    db.execute("UPDATE owners SET bot_username=NULL, api_key=NULL WHERE api_key IS NOT NULL")
    db.execute("UPDATE events SET bot_id=(SELECT MIN(id) FROM bots WHERE bots.owner_id=events.owner_id) "
               "WHERE bot_id IS NULL")
    db.execute("UPDATE owners SET balance=0 WHERE balance IS NULL")
    db.execute("CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_events_bot ON events(bot_id)")
    db.commit()


migrate()
state: dict = {}          # user_id -> ("action", ...)  (matn kutayotgan holatlar)
restore_tmp: dict = {}    # admin_id -> yuklangan zaxira fayl yo'li

UZ = timezone(timedelta(hours=5))   # O'zbekiston vaqti (yozgi/qishki vaqt yo'q)


# ------------------------- yordamchilar -------------------------
def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, UZ).strftime("%d.%m.%Y %H:%M")


def num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


def money(n) -> str:
    return num(n) + " so'm"


def esc(s) -> str:
    return html.escape(str(s or ""))


def to_int(s: str):
    s = re.sub(r"[ ,_\u00a0]", "", s.strip())
    return int(s) if re.fullmatch(r"-?\d+", s) else None


def get_setting(k: str, default=None):
    r = db.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_setting(k: str, v):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))
    db.commit()


def get_price() -> int:
    return int(get_setting("price", SUB_PRICE))


def default_limit() -> int:
    return int(get_setting("max_bots", MAX_BOTS))


def get_card() -> str:
    return get_setting("card", CARD_NUMBER) or ""


def user_limit(owner_id: int) -> int:
    r = db.execute("SELECT max_bots FROM owners WHERE owner_id=?", (owner_id,)).fetchone()
    return r[0] if r and r[0] is not None else default_limit()


def bot_count(owner_id: int) -> int:
    return db.execute("SELECT COUNT(*) FROM bots WHERE owner_id=? AND removed=0", (owner_id,)).fetchone()[0]


def balance_of(owner_id: int) -> int:
    r = db.execute("SELECT balance FROM owners WHERE owner_id=?", (owner_id,)).fetchone()
    return (r[0] or 0) if r else 0


def ensure_user(u):
    db.execute("INSERT OR IGNORE INTO owners(owner_id) VALUES(?)", (u.id,))
    db.execute("UPDATE owners SET username=?, full_name=?, created=COALESCE(created,?) WHERE owner_id=?",
               (u.username or "", u.full_name or "", time.time(), u.id))
    db.commit()


def active(owner_id: int) -> bool:
    """Admin doim faol. Mijoz uchun obuna muddati tekshiriladi."""
    if owner_id == ADMIN_ID:
        return True
    r = db.execute("SELECT active_until FROM owners WHERE owner_id=?", (owner_id,)).fetchone()
    return bool(r and r[0] and r[0] > time.time())


def expiry_text(owner_id: int, until) -> str:
    if owner_id == ADMIN_ID:
        return "cheksiz ♾"
    if until and until > time.time():
        return f"{fmt_ts(until)} ✅"
    if until:
        return f"tugagan ({fmt_ts(until)}) ❌"
    return "faol emas ❌"


def do_grant(oid: int, days: int):
    """Muddatni uzaytiradi. Yangi tugash vaqtini qaytaradi (egasi yo'q bo'lsa None)."""
    row = db.execute("SELECT active_until FROM owners WHERE owner_id=?", (oid,)).fetchone()
    if not row:
        return None
    new = max(row[0] or 0, time.time()) + days * 86400
    db.execute("UPDATE owners SET active_until=? WHERE owner_id=?", (new, oid))
    db.commit()
    return new


def first_bot(owner_id: int):
    r = db.execute("SELECT MIN(id) FROM bots WHERE owner_id=? AND removed=0", (owner_id,)).fetchone()
    return r[0] if r else None


def new_pending(owner_id: int, user_id: int, base: int, kind: str = "api",
                bot_id=None, minutes: int = PENDING_MINUTES):
    """Unikal summa beradi. (owner_id, amount) umumiy => hub to'lovlari va mijoz boti
    to'lovlari bir kartada ham HECH QACHON adashmaydi."""
    now = time.time()
    db.execute("DELETE FROM pending WHERE exp<?", (now,))
    if kind == "hub":   # foydalanuvchining eski hal bo'lmagan so'rovini almashtiramiz
        db.execute("DELETE FROM pending WHERE owner_id=? AND kind='hub' AND user_id=?", (owner_id, user_id))
    for _ in range(300):
        u = base + random.randint(1, 99)
        if not db.execute("SELECT 1 FROM pending WHERE owner_id=? AND amount=?",
                          (owner_id, u)).fetchone():
            db.execute("INSERT INTO pending(owner_id,amount,user_id,exp,kind,bot_id) VALUES(?,?,?,?,?,?)",
                       (owner_id, u, user_id, now + minutes * 60, kind, bot_id))
            db.commit()
            return u
    return None


def parse_kirim(text: str):
    """Xabarkart kirim xabaridan summani oladi (➕ dan keyingi summa)."""
    m = re.search(r"[➕+]\s*([\d \u00a0\u202f.,]+?)\s*(?:UZS|so['‘’ʻ`]?m|сум)", text, re.I)
    if not m:
        return None
    raw = re.sub(r"[ \u00a0\u202f]", "", m.group(1))
    raw = re.sub(r"[.,]\d{1,2}$", "", raw)
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


def guide_text(key: str) -> str:
    """Mijoz botiga qo'shilishi kerak bo'lgan ikkita API so'rovi yo'riqnomasi (HTML)."""
    e = html.escape
    topup = e(f"POST {PUBLIC_URL}/api/topup?key={key}&user_id=FOYDALANUVCHI_ID&amount=2000")
    events = e(f"GET {PUBLIC_URL}/api/events?key={key}&after=OXIRGI_ID")
    return (
        "<b>3-qadam. Botingizga avto-to'lovni qo'shing.</b>\n"
        "Agar botingiz bizning tayyor kodimizda bo'lsa, bu qadam kerak emas: "
        "adminlardan tayyor faylni oling.\n"
        "Boshqa kod bo'lsa, botingiz hubga 2 ta so'rov yuborishi kerak "
        "(shu xabarni dasturchingizga bering):\n\n"
        "<b>a) Foydalanuvchi «Avtomatik to'lov» ni bosganda</b> — unikal summa olinadi:\n"
        f"<pre>{topup}</pre>\n"
        'Javob: <code>{"amount": 2037, "expires_in": ' + str(PENDING_MINUTES * 60) + "}</code>\n"
        f"Foydalanuvchiga: «Kartaga aynan 2037 so'm o'tkazing, {PENDING_MINUTES} daqiqa ichida» deb yozing. "
        "Summa aynan shunday bo'lishi shart.\n\n"
        "<b>b) Har 4 soniyada</b> — tasdiqlangan to'lovlarni so'rang:\n"
        f"<pre>{events}</pre>\n"
        'Javob: <code>[{"id": 5, "user_id": 123, "amount": 2037}]</code>\n\n'
        "<b>c) Har bir yozuv uchun:</b>\n"
        "1. <code>user_id</code> balansiga <code>amount</code> qo'shing va unga xabar yuboring.\n"
        "2. Oxirgi <code>id</code> ni saqlang va keyingi so'rovda <code>after</code> ga bering.\n"
        "3. Bir <code>id</code> ni ikki marta qo'shmang.\n\n"
        "⚠️ Kalit bor so'rovlarni faqat o'z dasturchingizga bering."
    )


# ------------------------- UI yordamchilari -------------------------
def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


async def ans(msg: Message, text: str, markup=None):
    return await msg.answer(text, parse_mode="HTML", reply_markup=markup)


async def show(c: CallbackQuery, text: str, markup=None):
    """Xabarni tahrirlaydi (bo'lmasa yangisini yuboradi)."""
    try:
        await c.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await c.message.answer(text, parse_mode="HTML", reply_markup=markup)
    try:
        await c.answer()
    except Exception:
        pass


async def notify(uid: int, text: str, markup=None):
    try:
        await bot.send_message(uid, text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        pass


def paginate(items: list, page: int, size: int):
    total = max(1, (len(items) + size - 1) // size)
    page = min(max(page, 0), total - 1)
    return items[page * size:(page + 1) * size], page, total


def pager_row(prefix: str, page: int, total: int):
    row = []
    if page > 0:
        row.append(("⬅️", f"{prefix}:{page - 1}"))
    row.append((f"{page + 1}/{total}", f"{prefix}:{page}"))
    if page < total - 1:
        row.append(("➡️", f"{prefix}:{page + 1}"))
    return row


def menu(uid: int):
    rows = [
        [("🔗 Botga ulash", "connect"), ("🤖 Botlarim", "bots")],
        [("👤 Profil", "status"), ("💳 Balans to'ldirish", "topup")],
        [("🔄 Obunani uzaytirish", "extend")],
    ]
    if uid == ADMIN_ID:
        rows.append([("⚙️ Admin panel", "a:menu")])
    return kb(*rows)


# ------------------------- foydalanuvchi paneli -------------------------
WELCOME = ("Assalomu alaykum!\n\nBu bot botingizga kartaga tushgan to'lovlarni avtomatik "
           "tasdiqlab beradi. Bot to'lovni o'zi qilmaydi, faqat tushganini tekshiradi.\n\n"
           "Boshlash uchun «🔗 Botga ulash» ni bosing.")


@dp.message(CommandStart())
async def start(m: Message):
    ensure_user(m.from_user)
    state.pop(m.from_user.id, None)
    await ans(m, WELCOME, menu(m.from_user.id))


@dp.callback_query(F.data == "menu")
async def menu_cb(c: CallbackQuery):
    state.pop(c.from_user.id, None)
    await show(c, WELCOME, menu(c.from_user.id))


def profile_text(uid: int) -> str:
    o = db.execute("SELECT active_until FROM owners WHERE owner_id=?", (uid,)).fetchone()
    b = db.execute("SELECT 1 FROM bconn WHERE owner_id=?", (uid,)).fetchone()
    limit = "∞" if uid == ADMIN_ID else str(user_limit(uid))
    return (
        "👤 <b>Profil</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"💰 Balans: <b>{money(balance_of(uid))}</b>\n"
        f"📅 Obuna tugashi: <b>{expiry_text(uid, o[0] if o else 0)}</b>\n"
        f"🤖 Botlar: {bot_count(uid)}/{limit}\n"
        f"🔌 Telegram Business: {'ulangan ✅' if b else 'ulanmagan ❌'}\n\n"
        f"💲 Obuna narxi: {money(get_price())} / {SUB_DAYS} kun\n"
        "🕒 Vaqt: O'zbekiston (UTC+5)")


@dp.callback_query(F.data.in_({"status", "profile"}))
async def status(c: CallbackQuery):
    await show(c, profile_text(c.from_user.id),
               kb([("🔄 Uzaytirish", "extend"), ("💳 Balans to'ldirish", "topup")],
                  [("⬅️ Menyu", "menu")]))


@dp.callback_query(F.data == "extend")
async def extend(c: CallbackQuery):
    uid = c.from_user.id
    if uid == ADMIN_ID:
        await c.answer("Admin uchun obuna cheksiz.", show_alert=True)
        return
    price, bal = get_price(), balance_of(uid)
    if bal >= price:
        await show(c, f"🔄 <b>Obunani uzaytirish</b>\n\nNarx: {money(price)} / {SUB_DAYS} kun\n"
                      f"Balansingiz: {money(bal)}\n\nBalansdan yechilsinmi?",
                   kb([(f"✅ Ha, {money(price)}", "extendok")], [("❌ Bekor", "status")]))
    else:
        await show(c, f"🔄 <b>Obunani uzaytirish</b>\n\nNarx: {money(price)} / {SUB_DAYS} kun\n"
                      f"Balansingiz: {money(bal)}\n❗ Yetmayapti: {money(price - bal)}",
                   kb([("💳 Balans to'ldirish", "topup")], [("⬅️ Orqaga", "status")]))


@dp.callback_query(F.data == "extendok")
async def extend_ok(c: CallbackQuery):
    uid, price = c.from_user.id, get_price()
    if uid == ADMIN_ID:
        await c.answer("Admin uchun obuna cheksiz.", show_alert=True)
        return
    ok = False
    with db:
        cur = db.execute("UPDATE owners SET balance=balance-? WHERE owner_id=? AND balance>=?",
                         (price, uid, price))
        if cur.rowcount == 1:
            ok = True
            row = db.execute("SELECT active_until FROM owners WHERE owner_id=?", (uid,)).fetchone()
            new = max(row[0] or 0, time.time()) + SUB_DAYS * 86400
            db.execute("UPDATE owners SET active_until=? WHERE owner_id=?", (new, uid))
            db.execute("INSERT INTO sales(owner_id,amount,days,ts) VALUES(?,?,?,?)",
                       (uid, price, SUB_DAYS, time.time()))
    if not ok:
        await c.answer("Balans yetarli emas.", show_alert=True)
        return
    await show(c, f"✅ Obuna +{SUB_DAYS} kunga uzaytirildi.\n\n" + profile_text(uid),
               kb([("⬅️ Menyu", "menu")]))


@dp.callback_query(F.data == "topup")
async def topup_start(c: CallbackQuery):
    if not get_card():
        await c.answer("Karta hali kiritilmagan. Admin bilan bog'laning.", show_alert=True)
        return
    state[c.from_user.id] = ("topup",)
    await show(c, "💳 <b>Balans to'ldirish</b>\n\nNecha so'm to'ldirmoqchisiz? Faqat raqam yuboring.\n"
                  f"Eng kam: {money(MIN_TOPUP)}, eng ko'p: {money(MAX_TOPUP)}",
               kb([("❌ Bekor", "menu")]))


@dp.callback_query(F.data == "connect")
async def connect(c: CallbackQuery):
    uid = c.from_user.id
    ensure_user(c.from_user)
    if uid != ADMIN_ID and bot_count(uid) >= user_limit(uid):
        await c.answer(f"Bot limiti to'ldi ({user_limit(uid)} ta). Eskisini o'chiring yoki admin "
                       f"bilan bog'laning.", show_alert=True)
        return
    state[uid] = ("uname",)
    await c.message.answer("Botingiz username'ini yuboring (masalan: @mening_botim)")
    await c.answer()


@dp.callback_query(F.data == "bots")
async def my_bots(c: CallbackQuery):
    uid = c.from_user.id
    rows = db.execute("SELECT id, username FROM bots WHERE owner_id=? AND removed=0 ORDER BY id",
                      (uid,)).fetchall()
    limit = "∞" if uid == ADMIN_ID else user_limit(uid)
    text = f"🤖 <b>Botlarim</b> ({len(rows)}/{limit})\n\n" + \
           ("Botni tanlang:" if rows else "Hali bot ulanmagan.")
    buttons = [[(r[1] or f"bot #{r[0]}", f"bot:{r[0]}")] for r in rows]
    buttons.append([("🔗 Yangi bot ulash", "connect")])
    buttons.append([("⬅️ Menyu", "menu")])
    await show(c, text, kb(*buttons))


def own_bot(uid: int, bid: int):
    return db.execute("SELECT id, username, api_key FROM bots WHERE id=? AND owner_id=? AND removed=0",
                      (bid, uid)).fetchone()


@dp.callback_query(F.data.startswith("bot:"))
async def bot_detail(c: CallbackQuery):
    b = own_bot(c.from_user.id, int(c.data.split(":")[1]))
    if not b:
        await c.answer("Bot topilmadi.", show_alert=True)
        return
    await show(c, f"🤖 <b>{esc(b[1])}</b>\n\nHUB_URL = <code>{esc(PUBLIC_URL)}</code>\n"
                  f"HUB_KEY = <code>{esc(b[2])}</code>\n\n⚠️ Kalitni hech kimga bermang.",
               kb([("📖 Yo'riqnoma", f"guide:{b[0]}")], [("🗑 O'chirish", f"botdel:{b[0]}")],
                  [("⬅️ Botlarim", "bots")]))


@dp.callback_query(F.data.startswith("guide:"))
async def bot_guide(c: CallbackQuery):
    b = own_bot(c.from_user.id, int(c.data.split(":")[1]))
    if not b:
        await c.answer("Bot topilmadi.", show_alert=True)
        return
    await c.message.answer(guide_text(b[2]), parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data.startswith("botdel:"))
async def bot_del(c: CallbackQuery):
    b = own_bot(c.from_user.id, int(c.data.split(":")[1]))
    if not b:
        await c.answer("Bot topilmadi.", show_alert=True)
        return
    await show(c, f"🗑 {esc(b[1])} o'chirilsinmi? Uning kaliti ishlamay qoladi.",
               kb([("✅ Ha, o'chirish", f"botdelok:{b[0]}")], [("❌ Yo'q", f"bot:{b[0]}")]))


@dp.callback_query(F.data.startswith("botdelok:"))
async def bot_del_ok(c: CallbackQuery):
    b = own_bot(c.from_user.id, int(c.data.split(":")[1]))
    if b:
        db.execute("UPDATE bots SET removed=1 WHERE id=?", (b[0],))
        db.commit()
    await my_bots(c)


# ------------------------- matn kutish holatlari -------------------------
@dp.message(F.text, F.func(lambda m: bool(m.from_user) and m.from_user.id in state
                           and not (m.text or "").startswith("/")))
async def on_state_text(m: Message):
    uid = m.from_user.id
    action = state[uid][0]
    txt = m.text.strip()
    is_admin = uid == ADMIN_ID

    if action == "uname":
        u = txt.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", u) or not u.lower().endswith("bot"):
            await m.answer("Username noto'g'ri. Masalan: @mening_botim")
            return
        if not is_admin and bot_count(uid) >= user_limit(uid):
            state.pop(uid, None)
            await m.answer(f"Bot limiti to'ldi ({user_limit(uid)} ta).")
            return
        if db.execute("SELECT 1 FROM bots WHERE owner_id=? AND removed=0 AND lower(username)=lower(?)",
                      (uid, "@" + u)).fetchone():
            await m.answer("Bu bot allaqachon ulangan.")
            return
        state.pop(uid, None)
        ensure_user(m.from_user)
        key = secrets.token_urlsafe(24)
        db.execute("INSERT INTO bots(owner_id,username,api_key,created) VALUES(?,?,?,?)",
                   (uid, "@" + u, key, time.time()))
        db.commit()
        await ans(m,
                  f"✅ @{u} ulandi.\n\n"
                  f"<b>1-qadam.</b> Telegram Business'ni yoqing (Premium kerak):\n"
                  f"Sozlamalar → Telegram Business → Chatbots → @{esc(HUB_USERNAME)} ni qo'shing.\n"
                  f"Chatlar: «Только выбранные чаты» → faqat Xabarkart chati.\n\n"
                  f"<b>2-qadam.</b> Botingizga shu ikki qiymatni kiriting:\n"
                  f"HUB_URL = <code>{esc(PUBLIC_URL)}</code>\n"
                  f"HUB_KEY = <code>{key}</code>\n\n"
                  f"⚠️ Kalitni hech kimga bermang.")
        await ans(m, guide_text(key), kb([("⬅️ Menyu", "menu")]))
        return

    if action == "topup":
        amt = to_int(txt)
        if amt is None or not (MIN_TOPUP <= amt <= MAX_TOPUP):
            await m.answer(f"Summa {money(MIN_TOPUP)} dan {money(MAX_TOPUP)} gacha bo'lishi kerak. "
                           f"Faqat raqam yuboring.")
            return
        if not get_card():
            state.pop(uid, None)
            await m.answer("Karta hali kiritilmagan. Admin bilan bog'laning.")
            return
        u = new_pending(ADMIN_ID, uid, amt, "hub", None, TOPUP_MINUTES)
        state.pop(uid, None)
        if not u:
            await m.answer("Hozir band, birozdan keyin urinib ko'ring.")
            return
        holder = f"\nEgasi: {esc(CARD_HOLDER)}" if CARD_HOLDER else ""
        await ans(m,
                  f"💳 Kartaga aynan <b>{num(u)} so'm</b> o'tkazing:\n"
                  f"<code>{esc(get_card())}</code>{holder}\n\n"
                  f"⏱ {TOPUP_MINUTES} daqiqa ichida. Summa aynan shunday bo'lishi shart "
                  f"(+1–99 so'm to'lovni aniqlash uchun; to'liq summa balansingizga tushadi). "
                  f"Pul tushgach balans avtomatik to'ldiriladi.",
                  kb([("⬅️ Menyu", "menu")]))
        return

    # ---- admin holatlari ----
    if not is_admin:
        state.pop(uid, None)
        return
    back = kb([("⬅️ Admin panel", "a:menu")])

    if action == "a_price":
        v = to_int(txt)
        if v is None or v <= 0:
            await m.answer("Musbat butun son yuboring.")
            return
        state.pop(uid, None)
        set_setting("price", v)
        await ans(m, f"✅ Obuna narxi: <b>{money(v)}</b> / {SUB_DAYS} kun", back)

    elif action == "a_grant":
        p = txt.split()
        oid, days = (to_int(p[0]), to_int(p[1])) if len(p) == 2 else (None, None)
        if oid is None or days is None or not (1 <= days <= 3650):
            await m.answer("Format: <code>ID KUN</code> (masalan: <code>123456789 30</code>)",
                           parse_mode="HTML")
            return
        new = do_grant(oid, days)
        if new is None:
            await m.answer("Bunday foydalanuvchi topilmadi (u botga /start bosgan bo'lishi kerak).")
            return
        state.pop(uid, None)
        await ans(m, f"✅ <code>{oid}</code> uchun +{days} kun.\nYangi muddat: <b>{fmt_ts(new)}</b>", back)
        await notify(oid, f"🎁 Obunangiz {days} kunga uzaytirildi.\nYangi muddat: <b>{fmt_ts(new)}</b>")

    elif action == "a_balance":
        p = txt.split()
        oid, amt = (to_int(p[0]), to_int(p[1])) if len(p) == 2 else (None, None)
        if oid is None or not amt:
            await m.answer("Format: <code>ID SUMMA</code> (kamaytirish uchun minus: <code>123 -5000</code>)",
                           parse_mode="HTML")
            return
        if not db.execute("SELECT 1 FROM owners WHERE owner_id=?", (oid,)).fetchone():
            await m.answer("Bunday foydalanuvchi topilmadi.")
            return
        state.pop(uid, None)
        with db:
            db.execute("UPDATE owners SET balance=MAX(0, COALESCE(balance,0)+?) WHERE owner_id=?", (amt, oid))
            if amt > 0:
                db.execute("INSERT INTO topups(user_id,amount,ts,kind) VALUES(?,?,?,'admin')",
                           (oid, amt, time.time()))
        await ans(m, f"✅ <code>{oid}</code> balansi: <b>{money(balance_of(oid))}</b>", back)
        if amt > 0:
            await notify(oid, f"💰 Balansingizga {money(amt)} qo'shildi. Balans: {money(balance_of(oid))}")

    elif action == "a_limit":
        p = txt.split()
        if len(p) == 1 and to_int(p[0]) is not None and to_int(p[0]) >= 1:
            set_setting("max_bots", to_int(p[0]))
            state.pop(uid, None)
            await ans(m, f"✅ Umumiy bot limiti: <b>{to_int(p[0])}</b>", back)
        elif len(p) == 2 and to_int(p[0]) is not None and (p[1] == "-" or (to_int(p[1]) or 0) >= 1):
            oid = to_int(p[0])
            if not db.execute("SELECT 1 FROM owners WHERE owner_id=?", (oid,)).fetchone():
                await m.answer("Bunday foydalanuvchi topilmadi.")
                return
            val = None if p[1] == "-" else to_int(p[1])
            db.execute("UPDATE owners SET max_bots=? WHERE owner_id=?", (val, oid))
            db.commit()
            state.pop(uid, None)
            await ans(m, f"✅ <code>{oid}</code> uchun limit: <b>{val if val else 'umumiy (' + str(default_limit()) + ')'}</b>", back)
        else:
            await m.answer("Format: <code>LIMIT</code> (hamma uchun) yoki <code>ID LIMIT</code> "
                           "(bitta odam uchun; <code>ID -</code> = umumiyga qaytarish)", parse_mode="HTML")

    elif action == "a_card":
        card = re.sub(r"[^\d ]", "", txt).strip()
        if len(re.sub(r"\D", "", card)) < 12:
            await m.answer("Karta raqamini to'liq yuboring (16 xonali).")
            return
        state.pop(uid, None)
        set_setting("card", card)
        await ans(m, f"✅ Karta raqami saqlandi: <code>{esc(card)}</code>", back)

    elif action == "restore":
        await m.answer("Zaxira <b>.db fayl</b>ini yuboring (matn emas).", parse_mode="HTML")


# ------------------------- ADMIN PANEL -------------------------
def admin_menu_kb():
    return kb(
        [("📊 Statistika", "a:stats"), ("🤖 Botlar tushumi", "a:botstats:0")],
        [("👥 Foydalanuvchilar", "a:users:0"), ("🔌 Ulangan botlar", "a:bots:0")],
        [("➕ Qo'lda obuna", "a:grant"), ("💰 Balans qo'shish", "a:balance")],
        [("💲 Obuna narxi", "a:price"), ("🔢 Bot limiti", "a:limit")],
        [("💳 Karta raqami", "a:card"), ("💾 Zaxira / tiklash", "a:backup")],
        [("⬅️ Menyu", "menu")])


def admin_menu_text() -> str:
    return ("⚙️ <b>Admin panel</b>\n\n"
            f"💲 Obuna narxi: {money(get_price())} / {SUB_DAYS} kun\n"
            f"🔢 Umumiy bot limiti: {default_limit()}\n"
            f"💳 Karta: <code>{esc(get_card() or 'kiritilmagan')}</code>\n"
            f"🕒 Hozir: {fmt_ts(time.time())} (UZ)")


@dp.message(Command("admin"))
async def admin_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    state.pop(m.from_user.id, None)
    await ans(m, admin_menu_text(), admin_menu_kb())


def period_starts() -> dict:
    now = datetime.now(UZ)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "Bugun": day.timestamp(),
        "Bu hafta": (day - timedelta(days=day.weekday())).timestamp(),
        "Bu oy": day.replace(day=1).timestamp(),
        "Umumiy": 0,
    }


def period_sums(table: str, where: str = "1=1", args: tuple = ()) -> dict:
    out = {}
    for name, start in period_starts().items():
        out[name] = db.execute(
            f"SELECT COALESCE(SUM(amount),0), COUNT(*) FROM {table} WHERE {where} AND ts>=?",
            (*args, start)).fetchone()
    return out


def sums_block(title: str, sums: dict) -> str:
    return f"<b>{title}</b>\n" + "\n".join(
        f"• {n}: {money(s)} ({c} ta)" for n, (s, c) in sums.items())


def stats_text() -> str:
    now = time.time()
    users = db.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
    subs = db.execute("SELECT COUNT(*) FROM owners WHERE active_until>? AND owner_id!=?",
                      (now, ADMIN_ID)).fetchone()[0]
    bots = db.execute("SELECT COUNT(*) FROM bots WHERE removed=0").fetchone()[0]
    bal = db.execute("SELECT COALESCE(SUM(balance),0) FROM owners").fetchone()[0]
    return (
        "📊 <b>Statistika</b> (O'zbekiston vaqti)\n\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"✅ Faol obunachilar: {subs}\n"
        f"🤖 Ulangan botlar: {bots}\n"
        f"💰 Foydalanuvchilar balansi jami: {money(bal)}\n\n"
        + sums_block("💳 Barcha botlar to'lovlari (tasdiqlangan)", period_sums("events")) + "\n\n"
        + sums_block("🏦 Hub daromadi — balans to'ldirishlar (karta)",
                     period_sums("topups", "kind='card'")) + "\n\n"
        + sums_block("🧾 Obuna sotuvlari (balansdan yechilgan)", period_sums("sales")))


def botstats_text(page: int):
    rows = db.execute(
        "SELECT b.id, b.username, b.owner_id, b.removed, COALESCE(SUM(e.amount),0) t "
        "FROM bots b LEFT JOIN events e ON e.bot_id=b.id GROUP BY b.id ORDER BY t DESC, b.id").fetchall()
    chunk, page, total = paginate(rows, page, 6)
    lines = [f"🤖 <b>Botlar tushumi</b> (jami {len(rows)} ta bot)\n"]
    for bid, uname, owner, removed, _ in chunk:
        s = period_sums("events", "bot_id=?", (bid,))
        tag = " 🗑" if removed else ""
        lines.append(
            f"<b>{esc(uname or 'bot #' + str(bid))}</b>{tag} (egasi <code>{owner}</code>)\n"
            f"Bugun {num(s['Bugun'][0])} · Hafta {num(s['Bu hafta'][0])} · "
            f"Oy {num(s['Bu oy'][0])}\n"
            f"Jami <b>{money(s['Umumiy'][0])}</b> ({s['Umumiy'][1]} ta to'lov)\n")
    if not rows:
        lines.append("Hali bot yo'q.")
    return "\n".join(lines), page, total


def users_text(page: int):
    rows = db.execute(
        "SELECT o.owner_id, o.username, o.full_name, o.balance, o.active_until, "
        "(SELECT COUNT(*) FROM bots b WHERE b.owner_id=o.owner_id AND b.removed=0) "
        "FROM owners o ORDER BY COALESCE(o.created,0) DESC, o.owner_id DESC").fetchall()
    chunk, page, total = paginate(rows, page, 8)
    lines = [f"👥 <b>Foydalanuvchilar</b> (jami {len(rows)})\n"]
    for oid, un, fn, bal, until, nb in chunk:
        name = f"@{esc(un)}" if un else (esc(fn) or "—")
        lines.append(f"<code>{oid}</code> {name}\n"
                     f"   💰 {money(bal or 0)} · 🤖 {nb} · 📅 {expiry_text(oid, until)}")
    return "\n".join(lines), page, total


def bots_text(page: int):
    rows = db.execute(
        "SELECT b.id, b.username, b.owner_id, o.active_until FROM bots b "
        "LEFT JOIN owners o ON o.owner_id=b.owner_id WHERE b.removed=0 ORDER BY b.id DESC").fetchall()
    chunk, page, total = paginate(rows, page, 8)
    lines = [f"🔌 <b>Ulangan botlar</b> (jami {len(rows)}) — muddat O'zbekiston vaqti bilan\n"]
    for bid, uname, owner, until in chunk:
        lines.append(f"🤖 <b>{esc(uname or 'bot #' + str(bid))}</b> — egasi <code>{owner}</code>\n"
                     f"   ⏳ {expiry_text(owner, until)}")
    if not rows:
        lines.append("Hali bot ulanmagan.")
    return "\n".join(lines), page, total


PROMPTS = {
    "a:price": ("a_price", lambda: f"💲 <b>Obuna narxi</b>\nHozir: {money(get_price())} / {SUB_DAYS} kun\n\n"
                                   "Yangi narxni yuboring (faqat raqam):"),
    "a:grant": ("a_grant", lambda: "➕ <b>Qo'lda obuna berish</b>\n\n<code>ID KUN</code> yuboring.\n"
                                   "Masalan: <code>123456789 30</code>"),
    "a:balance": ("a_balance", lambda: "💰 <b>Balans qo'shish</b>\n\n<code>ID SUMMA</code> yuboring.\n"
                                       "Masalan: <code>123456789 50000</code>\n"
                                       "(kechikkan to'lovni qo'lda kiritish uchun ham shu)"),
    "a:limit": ("a_limit", lambda: f"🔢 <b>Bot ulash limiti</b>\nUmumiy limit: {default_limit()}\n\n"
                                   "• Hamma uchun: <code>5</code>\n"
                                   "• Bitta odam uchun: <code>123456789 10</code>\n"
                                   "• Umumiyga qaytarish: <code>123456789 -</code>"),
    "a:card": ("a_card", lambda: f"💳 <b>Karta raqami</b>\nHozir: <code>{esc(get_card() or 'kiritilmagan')}</code>\n\n"
                                 "Yangi karta raqamini yuboring:"),
}


def make_backup(prefix: str = "backup") -> str:
    path = f"{prefix}_{datetime.now(UZ):%Y%m%d_%H%M%S}.db"
    db.commit()
    dst = sqlite3.connect(path)
    db.backup(dst)
    dst.close()
    return path


async def send_backup(chat_id: int, caption: str):
    path = make_backup()
    try:
        await bot.send_document(chat_id, FSInputFile(path), caption=caption)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def validate_db(path: str):
    """Zaxira fayl haqiqiy hub bazasimi? (owners soni, events soni) yoki None."""
    try:
        c = sqlite3.connect(path)
        ok = c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not (ok and {"owners", "pending", "events", "bconn", "done"} <= tabs):
            c.close()
            return None
        res = (c.execute("SELECT COUNT(*) FROM owners").fetchone()[0],
               c.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        c.close()
        return res
    except Exception:
        return None


@dp.callback_query(F.data.startswith("a:"))
async def admin_cb(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Ruxsat yo'q.", show_alert=True)
        return
    parts = c.data.split(":")
    cmd = ":".join(parts[:2])
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    back = [("⬅️ Admin panel", "a:menu")]

    if cmd == "a:menu":
        state.pop(ADMIN_ID, None)
        await show(c, admin_menu_text(), admin_menu_kb())
    elif cmd == "a:cancel":
        state.pop(ADMIN_ID, None)
        restore_tmp.pop(ADMIN_ID, None)
        await show(c, admin_menu_text(), admin_menu_kb())
    elif cmd == "a:stats":
        await show(c, stats_text(), kb([("🔄 Yangilash", "a:stats")], back))
    elif cmd == "a:botstats":
        text, page, total = botstats_text(page)
        await show(c, text, kb(pager_row("a:botstats", page, total), back))
    elif cmd == "a:users":
        text, page, total = users_text(page)
        await show(c, text, kb(pager_row("a:users", page, total), back))
    elif cmd == "a:bots":
        text, page, total = bots_text(page)
        await show(c, text, kb(pager_row("a:bots", page, total), back))
    elif cmd in PROMPTS:
        action, make_text = PROMPTS[cmd]
        state[ADMIN_ID] = (action,)
        await show(c, make_text(), kb([("❌ Bekor qilish", "a:cancel")]))
    elif cmd == "a:backup":
        await show(c, "💾 <b>Zaxira / tiklash</b>\n\n"
                      "• <b>Zaxira olish</b> — baza fayli sizga yuboriladi.\n"
                      f"• <b>Tiklash</b> — .db faylni yuborasiz, baza o'sha holatga qaytadi.\n"
                      f"• Avtomatik zaxira (lichkaga): " + (f"har {AUTO_BACKUP_HOURS:g} soatda" if AUTO_BACKUP_HOURS > 0 else "o'chiq") +
                      "\n• Zaxira kanali: " + ("ulangan ✅ (har o'zgarishda pinlangan xabar yangilanadi)" if BACKUP_CHANNEL_ID and not backup_state["blocked"]
                                              else ("⚠️ to'xtatilgan" if BACKUP_CHANNEL_ID else "ulanmagan (BACKUP_CHANNEL_ID)")) +
                      "\n\n⚠️ Zaxirada API kalitlar bor — faylni hech kimga bermang.",
                   kb([("📥 Zaxira olish", "a:dobackup")], [("♻️ Fayldan tiklash", "a:restore")],
                      [("☁️ Kanaldan tiklash", "a:chrestore")], back))
    elif cmd == "a:chrestore":
        if not BACKUP_CHANNEL_ID:
            await c.answer("BACKUP_CHANNEL_ID sozlanmagan.", show_alert=True)
            return
        await show(c, "☁️ <b>Kanaldan tiklash</b>\n\nKanaldagi pinlangan zaxira bilan joriy baza almashtiriladi "
                      "(avval joriy holat sizga yuboriladi). Davom etilsinmi?",
                   kb([("✅ Ha, tiklash", "a:chrestoreok")], [("❌ Bekor", "a:cancel")]))
    elif cmd == "a:chrestoreok":
        try:
            await send_backup(ADMIN_ID, "🗄 Tiklashdan oldingi holat (xavfsizlik uchun)")
            res = await restore_from_channel()
            backup_state["blocked"] = False
            await show(c, (f"✅ Kanaldan tiklandi ({res})." if res else "Kanalda pinlangan zaxira topilmadi.")
                       + "\n\n" + admin_menu_text(), admin_menu_kb())
        except Exception as ex:
            await show(c, f"❌ Xatolik: {esc(repr(ex))}", kb(back))
    elif cmd == "a:dobackup":
        await c.answer("Tayyorlanmoqda...")
        await send_backup(ADMIN_ID, f"🗄 Zaxira — {fmt_ts(time.time())} (UZ)")
    elif cmd == "a:restore":
        state[ADMIN_ID] = ("restore",)
        await show(c, "♻️ <b>Tiklash</b>\n\nZaxira <b>.db fayl</b>ini shu chatga yuboring.\n"
                      "Joriy baza almashtiriladi (avval joriy holat zaxiralanib sizga yuboriladi).",
                   kb([("❌ Bekor qilish", "a:cancel")]))
    elif cmd == "a:restoreok":
        path = restore_tmp.pop(ADMIN_ID, None)
        if not path or not os.path.exists(path):
            await show(c, "Fayl topilmadi. Qaytadan yuboring.", kb(back))
            return
        try:
            await send_backup(ADMIN_ID, "🗄 Tiklashdan oldingi holat (xavfsizlik uchun)")
            src = sqlite3.connect(path)
            db.commit()
            src.backup(db)
            src.close()
            migrate()
            await show(c, "✅ Baza tiklandi.\n\n" + admin_menu_text(), admin_menu_kb())
        except Exception as ex:
            await show(c, f"❌ Tiklashda xatolik: {esc(repr(ex))}", kb(back))
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    else:
        await c.answer()


@dp.message(F.document)
async def on_document(m: Message):
    uid = m.from_user.id if m.from_user else 0
    if uid != ADMIN_ID:
        return
    waiting = state.get(uid, (None,))[0] == "restore"
    if not (waiting or (m.caption or "").startswith("/restore")):
        return
    if m.document.file_size and m.document.file_size > 20 * 1024 * 1024:
        await m.answer("Fayl 20 MB dan katta (Telegram bot limiti).")
        return
    tmp = f"restore_{int(time.time())}.db"
    try:
        await bot.download(m.document, destination=tmp)
    except Exception as ex:
        await m.answer(f"Faylni yuklab bo'lmadi: {ex!r}")
        return
    info = validate_db(tmp)
    if not info:
        os.remove(tmp)
        await m.answer("❌ Bu hub zaxira fayli emas yoki buzilgan.")
        return
    state.pop(uid, None)
    restore_tmp[uid] = tmp
    await ans(m, f"Fayl to'g'ri ✅\nFoydalanuvchilar: {info[0]}, tasdiqlangan to'lovlar: {info[1]}\n\n"
                 f"<b>Joriy baza shu fayl bilan almashtirilsinmi?</b>",
              kb([("✅ Ha, tiklash", "a:restoreok")], [("❌ Bekor", "a:cancel")]))


@dp.message(Command("backup"))
async def backup_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    await send_backup(ADMIN_ID, f"🗄 Zaxira — {fmt_ts(time.time())} (UZ)")


@dp.message(Command("grant"))
async def grant(m: Message):
    """Admin: /grant OWNER_ID KUN  - mijozga obuna beradi."""
    if m.from_user.id != ADMIN_ID:
        return
    try:
        _, oid, days = m.text.split()
        oid, days = int(oid), int(days)
    except ValueError:
        await m.answer("Format: /grant OWNER_ID KUN")
        return
    new = do_grant(oid, days)
    if new is None:
        await m.answer("Bunday egasi topilmadi.")
        return
    await m.answer(f"✅ {oid} uchun +{days} kun. Yangi muddat: {fmt_ts(new)}")
    await notify(oid, f"🎁 Obunangiz {days} kunga uzaytirildi.\nYangi muddat: <b>{fmt_ts(new)}</b>")


# ------------------------- ZAXIRA KANALI (avto-saqlash va avto-tiklash) -------------------------
backup_state = {"msg_id": None, "last_changes": None, "blocked": False}


def db_is_empty() -> bool:
    """Baza bo'shmi (yangi deploy / disk o'chib ketgan)?"""
    return all(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
               for t in ("owners", "bots", "events", "settings", "topups", "done"))


def apply_restore(path: str):
    src = sqlite3.connect(path)
    db.commit()
    src.backup(db)
    src.close()
    migrate()


async def pinned_backup_message():
    chat = await bot.get_chat(BACKUP_CHANNEL_ID)
    pm = getattr(chat, "pinned_message", None)
    return pm if pm and pm.document else None


async def restore_from_channel() -> str:
    """Kanaldagi pinlangan zaxiradan tiklaydi. Pin yo'q bo'lsa None qaytaradi."""
    pm = await pinned_backup_message()
    if not pm:
        return None
    tmp = f"chrestore_{int(time.time())}.db"
    try:
        await bot.download(pm.document, destination=tmp)
        info = validate_db(tmp)
        if not info:
            raise RuntimeError("Pinlangan fayl hub zaxirasi emas yoki buzilgan")
        apply_restore(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    backup_state["msg_id"] = pm.message_id
    backup_state["last_changes"] = db.total_changes
    return f"{info[0]} foydalanuvchi, {info[1]} tasdiqlangan to'lov"


async def startup_restore():
    """Ishga tushganda: baza bo'sh bo'lsa kanaldagi pindan avtomatik tiklaydi."""
    if not BACKUP_CHANNEL_ID:
        return
    try:
        if db_is_empty():
            res = await restore_from_channel()
            if res:
                print("HUB: kanaldan tiklandi:", res, flush=True)
                await notify(ADMIN_ID, f"♻️ Baza kanaldagi pinlangan zaxiradan avtomatik tiklandi ({res}).")
            else:
                print("HUB: kanalda pinlangan zaxira yo'q (birinchi ishga tushirish).", flush=True)
        else:
            pm = await pinned_backup_message()
            backup_state["msg_id"] = pm.message_id if pm else None
    except Exception as ex:
        # tiklab bo'lmadi: bo'sh bazani yaxshi zaxira ustiga yozib yubormaslik uchun avto-zaxirani to'xtatamiz
        backup_state["blocked"] = True
        print("HUB: kanaldan tiklashda xato:", repr(ex), flush=True)
        await notify(ADMIN_ID, f"⚠️ Kanaldan tiklab bo'lmadi: {esc(repr(ex))}\n"
                               f"Zaxira kanalini tekshiring (bot admin bo'lishi va BACKUP_CHANNEL_ID to'g'riligi). "
                               f"Avto-zaxira xavfsizlik uchun to'xtatildi — botni qayta ishga tushiring.")


async def push_backup() -> bool:
    """Zaxirani kanaldagi pinlangan xabarga joyida yangilaydi (yangi xabar/spam yo'q)."""
    if not BACKUP_CHANNEL_ID or backup_state["blocked"] or db_is_empty():
        return False
    path = make_backup("chan")
    users = db.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
    evs = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    caption = f"🗄 Hub zaxirasi\n{fmt_ts(time.time())} (UZ)\nfoydalanuvchi: {users} | to'lov: {evs}"
    try:
        mid = backup_state["msg_id"]
        if mid:
            try:
                await bot.edit_message_media(
                    chat_id=BACKUP_CHANNEL_ID, message_id=mid,
                    media=InputMediaDocument(media=FSInputFile(path, filename="hub_backup.db"), caption=caption))
                return True
            except Exception as ex:
                if "not modified" in str(ex).lower():
                    return True
                print("HUB: pinni tahrirlab bo'lmadi, yangisi yuboriladi:", repr(ex), flush=True)
        sent = await bot.send_document(BACKUP_CHANNEL_ID, FSInputFile(path, filename="hub_backup.db"),
                                       caption=caption, disable_notification=True)
        await bot.pin_chat_message(BACKUP_CHANNEL_ID, sent.message_id, disable_notification=True)
        backup_state["msg_id"] = sent.message_id
        if mid:
            for fn in (bot.unpin_chat_message, bot.delete_message):
                try:
                    if fn is bot.unpin_chat_message:
                        await fn(BACKUP_CHANNEL_ID, message_id=mid)
                    else:
                        await fn(BACKUP_CHANNEL_ID, mid)
                except Exception:
                    pass
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def channel_backup_loop():
    """Baza o'zgarganda (to'lov, obuna, balans, bot...) avtomatik zaxiralaydi."""
    if not BACKUP_CHANNEL_ID:
        return
    if backup_state["last_changes"] is None:
        backup_state["last_changes"] = -1     # birinchi aylanishda bitta zaxira olinadi
    while True:
        await asyncio.sleep(BACKUP_MIN_SECONDS)
        snap = db.total_changes
        if snap != backup_state["last_changes"]:
            try:
                if await push_backup():
                    backup_state["last_changes"] = snap
            except Exception as ex:
                print("HUB: kanal zaxirasi xatosi:", repr(ex), flush=True)


async def auto_backup_loop():
    if AUTO_BACKUP_HOURS <= 0:
        return
    while True:
        await asyncio.sleep(AUTO_BACKUP_HOURS * 3600)
        try:
            await send_backup(ADMIN_ID, f"🗄 Avtomatik zaxira — {fmt_ts(time.time())} (UZ)")
        except Exception as ex:
            print("Avto-zaxira xatosi:", repr(ex), flush=True)


# ------------------------- Telegram Business -------------------------
@dp.business_connection()
async def on_bc(bc: BusinessConnection):
    if bc.is_enabled:
        db.execute("INSERT OR IGNORE INTO owners(owner_id) VALUES(?)", (bc.user.id,))
        db.execute("INSERT OR REPLACE INTO bconn VALUES(?,?)", (bc.id, bc.user.id))
        try:
            await bot.send_message(bc.user.id, "✅ Telegram Business ulandi.")
        except Exception:
            pass
    else:
        db.execute("DELETE FROM bconn WHERE bc_id=?", (bc.id,))
    db.commit()


async def verdict(text: str):
    """DEBUG: hub qarorini adminga yozadi."""
    print("HUB:", text, flush=True)
    if DEBUG:
        try:
            await bot.send_message(ADMIN_ID, "🔎 " + text)
        except Exception:
            pass


def pending_report() -> str:
    now = time.time()
    rows = db.execute("SELECT owner_id,amount,user_id,exp,kind FROM pending WHERE exp>? ORDER BY exp",
                      (now,)).fetchall()
    if not rows:
        return "kutilayotgan to'lov yo'q (muddati o'tgan yoki hech kim summa so'ramagan)"
    return "; ".join(f"egasi {o}: {a:,} so'm (foyd. {u}, {int(e - now)} soniya qoldi"
                     f"{', balans' if k == 'hub' else ''})" for o, a, u, e, k in rows[:10])


@dp.message(Command("diag"))
async def diag(m: Message):
    """Admin: hubning holatini ko'rsatadi."""
    if m.from_user.id != ADMIN_ID:
        return
    n_bc = db.execute("SELECT COUNT(*) FROM bconn").fetchone()[0]
    n_bots = db.execute("SELECT COUNT(*) FROM bots WHERE removed=0").fetchone()[0]
    n_ev = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    await m.answer(
        f"Business ulanishlari: {n_bc}\n"
        f"Ulangan botlar: {n_bots}\n"
        f"Tasdiqlangan to'lovlar: {n_ev}\n"
        f"Kutilayotgan: {pending_report()}\n"
        f"XABARKART='{XABARKART}', DEBUG={DEBUG}, muddat={PENDING_MINUTES} daqiqa")


@dp.message(Command("whoami"))
async def whoami(m: Message):
    await m.answer(f"Sizning ID: {m.from_user.id}")


@dp.business_message()
async def biznes(m: Message):
    u = m.from_user
    text = m.text or m.caption or ""
    uname = (u.username or "").lower() if u else ""

    r = db.execute("SELECT owner_id FROM bconn WHERE bc_id=?", (m.business_connection_id,)).fetchone()
    if not r:
        try:
            bc = await bot.get_business_connection(m.business_connection_id)
            if not bc.is_enabled:
                await verdict("Business ulanishi o'chirilgan.")
                return
            db.execute("INSERT OR IGNORE INTO owners(owner_id) VALUES(?)", (bc.user.id,))
            db.execute("INSERT OR REPLACE INTO bconn VALUES(?,?)", (bc.id, bc.user.id))
            db.commit()
            r = (bc.user.id,)
            await verdict(f"Business ulanishi avtomatik tiklandi (egasi {bc.user.id}).")
        except Exception as e:
            await verdict(f"Business ulanishi topilmadi: {e!r}. Chatbots'dan botni olib tashlab, qayta qo'shing.")
            return
    owner = r[0]

    if DEBUG and owner == ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"DEBUG\n@{uname} bot={u.is_bot if u else None}\n{text}")
        except Exception:
            pass

    # faqat Xabarkart botidan kelgan xabarga ishonamiz (soxta "kirim" xabaridan himoya)
    if not u or not u.is_bot:
        return
    if uname != XABARKART:
        await verdict(f"Boshqa bot xabari: @{uname}. Kutilgan Xabarkart: @{XABARKART}. "
                      f"XABARKART sozlamasini tekshiring.")
        return
    # eski xabarlarni (qayta yetkazilgan / tiklashdan oldingi) HECH QACHON qayta o'qimaymiz
    stale = (max(PENDING_MINUTES, TOPUP_MINUTES) + 3) * 60
    if m.date and time.time() - m.date.timestamp() > stale:
        await verdict("Eski xabar (muddati o'tgan), e'tiborga olinmadi.")
        return
    if not active(owner):
        await verdict(f"Egasi {owner} uchun xizmat muddati tugagan: /grant {owner} 30")
        return
    amount = parse_kirim(text)
    if not amount:
        await verdict("Xabarkart xabari keldi, lekin kirim summasi o'qilmadi (chiqim yoki format boshqa).")
        return
    if db.execute("SELECT 1 FROM done WHERE bc_id=? AND chat_id=? AND msg_id=?",
                  (m.business_connection_id, m.chat.id, m.message_id)).fetchone():
        return
    p = db.execute("SELECT user_id, kind, bot_id FROM pending WHERE owner_id=? AND amount=? AND exp>?",
                   (owner, amount, time.time())).fetchone()
    report = None if p else pending_report()
    with db:
        db.execute("INSERT OR IGNORE INTO done VALUES(?,?,?)",
                   (m.business_connection_id, m.chat.id, m.message_id))
        if p:
            db.execute("DELETE FROM pending WHERE owner_id=? AND amount=?", (owner, amount))
            if p[1] == "hub":
                # obuna balansi to'ldirildi (admin kartasi)
                db.execute("UPDATE owners SET balance=COALESCE(balance,0)+? WHERE owner_id=?", (amount, p[0]))
                db.execute("INSERT INTO topups(user_id,amount,ts,kind) VALUES(?,?,?,'card')",
                           (p[0], amount, time.time()))
            else:
                db.execute("INSERT INTO events(owner_id,user_id,amount,ts,bot_id) VALUES(?,?,?,?,?)",
                           (owner, p[0], amount, time.time(), p[2] or first_bot(owner)))

    if p and p[1] == "hub":
        await notify(p[0], f"✅ Balansingiz to'ldirildi: <b>+{money(amount)}</b>\n"
                           f"Balans: <b>{money(balance_of(p[0]))}</b>",
                     kb([("🔄 Obunani uzaytirish", "extend")]))
        await notify(ADMIN_ID, f"💳 Balans to'ldirildi: foydalanuvchi <code>{p[0]}</code>, {money(amount)}")
        return
    if p:
        await verdict(f"✅ Tasdiqlandi: {amount:,} so'm, foydalanuvchi {p[0]}. Mijoz boti 4 soniyada oladi.")
    else:
        await verdict(f"❌ Kirim {amount:,} so'm tushdi, lekin mos kutilayotgan summa yo'q. "
                      f"Kutilayotgan: {report}")
    try:
        if p:
            await bot.send_message(owner, f"💰 Kirim tasdiqlandi: {amount:,} so'm (foydalanuvchi {p[0]})")
        elif owner != ADMIN_ID or not DEBUG:
            await bot.send_message(owner, f"⚠️ Kirim {amount:,} so'm tushdi, lekin kutilayotgan to'lov "
                                          f"topilmadi (muddat tugagan yoki summa boshqa). Qo'lda tekshiring.")
    except Exception:
        pass


# ------------------------- mijoz botlari uchun API -------------------------
def owner_by_key(key: str):
    """(owner_id, bot_id) yoki None. Har bir botning o'z kaliti bor."""
    r = db.execute("SELECT owner_id, id FROM bots WHERE api_key=? AND removed=0", (key,)).fetchone()
    return (r[0], r[1]) if r and active(r[0]) else None


async def api_topup(req: web.Request):
    """Mijoz boti: foydalanuvchi 'to'ldirish' bosganda unikal summa so'raydi."""
    who = owner_by_key(req.query.get("key", ""))
    if not who:
        return web.json_response({"error": "kalit noto'g'ri yoki muddat tugagan"}, status=403)
    owner, bot_id = who
    try:
        uid, base = int(req.query["user_id"]), int(req.query["amount"])
    except (KeyError, ValueError):
        return web.json_response({"error": "user_id va amount kerak"}, status=400)
    if base <= 0:
        return web.json_response({"error": "amount noto'g'ri"}, status=400)
    u = new_pending(owner, uid, base, "api", bot_id)
    if not u:
        return web.json_response({"error": "band, keyinroq urinib ko'ring"}, status=503)
    return web.json_response({"amount": u, "expires_in": PENDING_MINUTES * 60})


async def api_events(req: web.Request):
    """Mijoz boti: tasdiqlangan to'lovlarni oladi (faqat o'z botiniki)."""
    who = owner_by_key(req.query.get("key", ""))
    if not who:
        return web.json_response({"error": "kalit noto'g'ri yoki muddat tugagan"}, status=403)
    owner, bot_id = who
    try:
        after = int(req.query.get("after", 0) or 0)
    except ValueError:
        after = 0
    rows = db.execute("SELECT id,user_id,amount FROM events WHERE owner_id=? AND bot_id=? AND id>? "
                      "ORDER BY id LIMIT 100", (owner, bot_id, after)).fetchall()
    return web.json_response([{"id": i, "user_id": u, "amount": a} for i, u, a in rows])


async def main():
    app = web.Application()
    app.router.add_post("/api/topup", api_topup)
    app.router.add_get("/api/events", api_events)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await startup_restore()          # avval tiklaymiz, keyin xabarlarni qabul qilamiz
    asyncio.create_task(auto_backup_loop())
    asyncio.create_task(channel_backup_loop())
    await dp.start_polling(bot, allowed_updates=[
        "message", "callback_query", "business_connection", "business_message"])


if __name__ == "__main__":
    asyncio.run(main())
