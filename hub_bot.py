"""
HUB BOT - avto-to'lov tasdiqlovchi.

Bu bot HECH QANDAY to'lov qilmaydi. U faqat:
1) mijozning botini ulaydi (username -> API kalit),
2) mijozning Telegram Business akkauntidagi Xabarkart xabarlarini o'qiydi,
3) pul tushganini tasdiqlaydi va mijozning botiga "shu foydalanuvchiga X so'm" deb yetkazadi.

O'rnatish:  pip install -U aiogram aiohttp
Ishga tushirish:  python hub_bot.py
"""
import asyncio
import os
import random
import re
import secrets
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (BusinessConnection, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)
from aiohttp import web

# ------------------------- SOZLAMALAR -------------------------
# Qiymatlar kodda emas, muhit o'zgaruvchilarida (.env.example ga qarang).
TOKEN = os.getenv("TOKEN", "")
HUB_USERNAME = os.getenv("HUB_USERNAME", "")             # @ siz
XABARKART = os.getenv("XABARKART", "").lower().lstrip("@")  # Xabarkart bot username'i
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)          # sizning Telegram ID
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
PENDING_MINUTES = int(os.getenv("PENDING_MINUTES", "2"))
DB_PATH = os.getenv("DB_PATH", "hub.db")
DEBUG = os.getenv("DEBUG", "1") == "1"                   # sinovda 1, keyin 0
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
""")
waiting_uname = set()


# ------------------------- yordamchilar -------------------------
def active(owner_id: int) -> bool:
    """Admin doim faol. Mijoz uchun /grant bilan muddat beriladi."""
    if owner_id == ADMIN_ID:
        return True
    r = db.execute("SELECT active_until FROM owners WHERE owner_id=?", (owner_id,)).fetchone()
    return bool(r and r[0] and r[0] > time.time())


def new_pending(owner_id: int, user_id: int, base: int):
    """Foydalanuvchiga unikal summa beradi (karta o'tkazmasida izoh yo'q, shuning uchun)."""
    now = time.time()
    db.execute("DELETE FROM pending WHERE exp<?", (now,))
    for _ in range(300):
        u = base + random.randint(1, 99)
        if not db.execute("SELECT 1 FROM pending WHERE owner_id=? AND amount=?",
                          (owner_id, u)).fetchone():
            db.execute("INSERT INTO pending VALUES(?,?,?,?)",
                       (owner_id, u, user_id, now + PENDING_MINUTES * 60))
            db.commit()
            return u
    return None


def parse_kirim(text: str):
    """Xabarkart xabaridan kirim summasini oladi. Haqiqiy format ma'lum bo'lgach moslanadi."""
    if not re.search(r"(?i)(kirim|popolnenie|пополнение|\+)", text):
        return None
    m = re.search(r"\+?\s*([\d\s,.]+?)\s*(?:UZS|so'm|сум)", text, re.I)
    if not m:
        return None
    s = re.sub(r"\D", "", m.group(1).split(".")[0].split(",")[0])
    return int(s) if s else None


def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Botga ulash", callback_data="connect")],
        [InlineKeyboardButton(text="📊 Holatim", callback_data="status")],
    ])


# ------------------------- foydalanuvchi menyusi -------------------------
@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Assalomu alaykum!\n\nBu bot botingizga kartaga tushgan to'lovlarni avtomatik "
        "tasdiqlab beradi. Bot to'lovni o'zi qilmaydi, faqat tushganini tekshiradi.\n\n"
        "Boshlash uchun «🔗 Botga ulash» ni bosing.", reply_markup=menu())


@dp.callback_query(F.data == "connect")
async def connect(c: CallbackQuery):
    waiting_uname.add(c.from_user.id)
    await c.message.answer("Botingiz username'ini yuboring (masalan: @mening_botim)")
    await c.answer()


@dp.message(F.text, F.func(lambda m: m.from_user and m.from_user.id in waiting_uname))
async def get_username(m: Message):
    u = m.text.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", u) or not u.lower().endswith("bot"):
        await m.answer("Username noto'g'ri. Masalan: @mening_botim")
        return
    waiting_uname.discard(m.from_user.id)
    old = db.execute("SELECT api_key FROM owners WHERE owner_id=?", (m.from_user.id,)).fetchone()
    key = old[0] if old and old[0] else secrets.token_urlsafe(24)
    db.execute("INSERT OR IGNORE INTO owners(owner_id) VALUES(?)", (m.from_user.id,))
    db.execute("UPDATE owners SET bot_username=?, api_key=? WHERE owner_id=?",
               ("@" + u, key, m.from_user.id))
    db.commit()
    await m.answer(
        f"✅ @{u} ulandi.\n\n"
        f"<b>1-qadam.</b> Telegram Business'ni yoqing (Premium kerak):\n"
        f"Sozlamalar → Telegram Business → Chatbots → @{HUB_USERNAME} ni qo'shing.\n"
        f"Chatlar: «Только выбранные чаты» → faqat Xabarkart chati.\n\n"
        f"<b>2-qadam.</b> Botingizga shu ikki qiymatni kiriting:\n"
        f"HUB_URL = <code>{PUBLIC_URL}</code>\n"
        f"HUB_KEY = <code>{key}</code>\n\n"
        f"⚠️ Kalitni hech kimga bermang.", parse_mode="HTML")


@dp.callback_query(F.data == "status")
async def status(c: CallbackQuery):
    uid = c.from_user.id
    o = db.execute("SELECT bot_username, active_until FROM owners WHERE owner_id=?", (uid,)).fetchone()
    b = db.execute("SELECT 1 FROM bconn WHERE owner_id=?", (uid,)).fetchone()
    if uid == ADMIN_ID:
        muddat = "cheksiz"
    elif o and o[1] and o[1] > time.time():
        muddat = time.strftime("%d.%m.%Y", time.localtime(o[1]))
    else:
        muddat = "faol emas"
    await c.message.answer(
        f"Bot: {o[0] if o and o[0] else 'ulanmagan'}\n"
        f"Telegram Business: {'ulangan ✅' if b else 'ulanmagan ❌'}\n"
        f"Xizmat muddati: {muddat}")
    await c.answer()


@dp.message(Command("grant"))
async def grant(m: Message):
    """Admin: /grant OWNER_ID KUN  - mijozga oylik xizmatni ochadi."""
    if m.from_user.id != ADMIN_ID:
        return
    try:
        _, oid, days = m.text.split()
        oid, days = int(oid), int(days)
    except ValueError:
        await m.answer("Format: /grant OWNER_ID KUN")
        return
    row = db.execute("SELECT active_until FROM owners WHERE owner_id=?", (oid,)).fetchone()
    if not row:
        await m.answer("Bunday egasi topilmadi.")
        return
    base = max(row[0] or 0, time.time())
    db.execute("UPDATE owners SET active_until=? WHERE owner_id=?", (base + days * 86400, oid))
    db.commit()
    await m.answer(f"✅ {oid} uchun +{days} kun.")


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


@dp.business_message()
async def biznes(m: Message):
    r = db.execute("SELECT owner_id FROM bconn WHERE bc_id=?", (m.business_connection_id,)).fetchone()
    if not r:
        return
    owner = r[0]
    u = m.from_user
    text = m.text or m.caption or ""
    uname = (u.username or "").lower() if u else ""

    if DEBUG and owner == ADMIN_ID:  # xabar formatini ko'rish uchun faqat sizga
        try:
            await bot.send_message(ADMIN_ID, f"DEBUG\n@{uname} bot={u.is_bot if u else None}\n{text}")
        except Exception:
            pass

    # faqat Xabarkart botidan kelgan xabarga ishonamiz (soxta "kirim" xabaridan himoya)
    if not u or not u.is_bot or uname != XABARKART or not active(owner):
        return
    amount = parse_kirim(text)
    if not amount:
        return
    if db.execute("SELECT 1 FROM done WHERE bc_id=? AND chat_id=? AND msg_id=?",
                  (m.business_connection_id, m.chat.id, m.message_id)).fetchone():
        return
    p = db.execute("SELECT user_id FROM pending WHERE owner_id=? AND amount=? AND exp>?",
                   (owner, amount, time.time())).fetchone()
    with db:
        db.execute("INSERT OR IGNORE INTO done VALUES(?,?,?)",
                   (m.business_connection_id, m.chat.id, m.message_id))
        if p:
            db.execute("DELETE FROM pending WHERE owner_id=? AND amount=?", (owner, amount))
            db.execute("INSERT INTO events(owner_id,user_id,amount,ts) VALUES(?,?,?,?)",
                       (owner, p[0], amount, time.time()))
    try:
        if p:
            await bot.send_message(owner, f"💰 Kirim tasdiqlandi: {amount:,} so'm (foydalanuvchi {p[0]})")
        else:
            await bot.send_message(owner, f"⚠️ Kirim {amount:,} so'm tushdi, lekin kutilayotgan to'lov "
                                          f"topilmadi (muddat tugagan yoki summa boshqa). Qo'lda tekshiring.")
    except Exception:
        pass


# ------------------------- mijoz botlari uchun API -------------------------
def owner_by_key(key: str):
    r = db.execute("SELECT owner_id FROM owners WHERE api_key=?", (key,)).fetchone()
    return r[0] if r and active(r[0]) else None


async def api_topup(req: web.Request):
    """Mijoz boti: foydalanuvchi 'to'ldirish' bosganda unikal summa so'raydi."""
    owner = owner_by_key(req.query.get("key", ""))
    if not owner:
        return web.json_response({"error": "kalit noto'g'ri yoki muddat tugagan"}, status=403)
    try:
        uid, base = int(req.query["user_id"]), int(req.query["amount"])
    except (KeyError, ValueError):
        return web.json_response({"error": "user_id va amount kerak"}, status=400)
    if base <= 0:
        return web.json_response({"error": "amount noto'g'ri"}, status=400)
    u = new_pending(owner, uid, base)
    if not u:
        return web.json_response({"error": "band, keyinroq urinib ko'ring"}, status=503)
    return web.json_response({"amount": u, "expires_in": PENDING_MINUTES * 60})


async def api_events(req: web.Request):
    """Mijoz boti: tasdiqlangan to'lovlarni oladi va hamyonga qo'shadi."""
    owner = owner_by_key(req.query.get("key", ""))
    if not owner:
        return web.json_response({"error": "kalit noto'g'ri yoki muddat tugagan"}, status=403)
    try:
        after = int(req.query.get("after", 0) or 0)
    except ValueError:
        after = 0
    rows = db.execute("SELECT id,user_id,amount FROM events WHERE owner_id=? AND id>? ORDER BY id LIMIT 100",
                      (owner, after)).fetchall()
    return web.json_response([{"id": i, "user_id": u, "amount": a} for i, u, a in rows])


async def main():
    app = web.Application()
    app.router.add_post("/api/topup", api_topup)
    app.router.add_get("/api/events", api_events)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(bot, allowed_updates=[
        "message", "callback_query", "business_connection", "business_message"])


if __name__ == "__main__":
    asyncio.run(main())
