"""
bot_12.py ga HUB ulagichini qo'shadi. Asl fayl O'ZGARMAYDI, natija: bot_12_hub.py

Ishlatish (bot_12.py bilan bir papkada):
    python patch_hub.py
    python patch_hub.py boshqa_fayl.py
Keyin botni bot_12_hub.py bilan ishga tushiring va ikkita o'zgaruvchi bering:
    HUB_URL=http://SERVER_IP:8080
    HUB_KEY=hub_bot bergan API kalit
"""
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "bot_12.py"
DST = "bot_12_hub.py"

CONNECTOR = r'''# ----------------------------------------------------------------------
# HUB ulagichi: Xabarkart orqali avto-to'lov (hub_bot.py bilan ishlaydi)
# ----------------------------------------------------------------------
HUB_URL = os.getenv("HUB_URL", "").rstrip("/")
HUB_KEY = os.getenv("HUB_KEY", "")
HUB_PENDING_FILE = DATA_DIR / "hub_pending.json"
HUB_LAST_FILE = DATA_DIR / "hub_last_id.json"
HUB_POLL_SECONDS = 4


def hub_ready() -> bool:
    return bool(HUB_URL and HUB_KEY)


def _hub_sync(method: str, path: str, params: dict[str, Any]) -> Any:
    response = requests.request(
        method, f"{HUB_URL}{path}", params={**params, "key": HUB_KEY}, timeout=15
    )
    response.raise_for_status()
    return response.json()


async def hub_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Karta (avtomatik, Xabarkart)» bosilganda: hubdan unikal summa olib, kartani ko'rsatadi."""
    query = update.callback_query
    if query is None or query.message is None or query.from_user is None:
        return
    await query.answer()
    amount = context.user_data.get(TOPUP_PENDING_KEY)
    if not isinstance(amount, int) or amount <= 0:
        await query.message.reply_text("Xatolik yuz berdi. Qaytadan boshlang.")
        return
    try:
        data = await asyncio.to_thread(
            _hub_sync, "POST", "/api/topup", {"user_id": query.from_user.id, "amount": amount}
        )
        unique = int(data["amount"])
        minutes = max(1, int(data.get("expires_in", 120)) // 60)
    except Exception as error:  # noqa: BLE001
        logger.warning("Hub summa bermadi: %r", error)
        await query.message.reply_text(
            "Hozir avtomatik to‘lov ishlamayapti. Boshqa usulni tanlang."
        )
        return
    promo = context.user_data.get(TOPUP_PROMO_KEY)
    record: dict[str, Any] = {"user_id": query.from_user.id, "amount": unique, "ts": time.time()}
    if isinstance(promo, dict):
        record["promo_code"] = promo.get("code")
        record["promo_percent"] = promo.get("percent")
    with _data_lock:
        rows = [r for r in load_json(HUB_PENDING_FILE, []) if time.time() - r.get("ts", 0) < 3600]
        rows.append(record)
        save_json(HUB_PENDING_FILE, rows)
    card_number = load_settings().get("card_number", DEFAULT_CARD_NUMBER)
    await query.message.reply_text(
        "💳 <b>Avtomatik to‘lov</b>\n"
        "━━━━━━━━━━━━━━━\n"
        f"{format_card_block(card_number)}\n\n"
        f"💰 Aynan <b>{format_sum(unique)}</b> o‘tkazing.\n"
        "━━━━━━━━━━━━━━━\n"
        f"⏱ {minutes} daqiqa ichida o‘tkazing. Summa boshqacha bo‘lsa, balans avtomatik to‘ldirilmaydi.\n"
        "To‘lov tushgach balansingiz o‘zi to‘ldiriladi.",
        parse_mode="HTML",
    )


async def hub_credit(bot: Any, event: dict[str, Any]) -> None:
    """Hub tasdiqlagan to'lovni hamyonga qo'shadi (promokod, referal, xabarlar bilan)."""
    user_id = int(event["user_id"])
    amount = int(event["amount"])
    event_id = int(event["id"])
    if amount <= 0:
        return
    wallet = load_wallets().get(str(user_id))
    user_name = (wallet.get("user_name") if isinstance(wallet, dict) else None) or f"User {user_id}"
    new_balance = adjust_wallet_balance(
        user_id, user_name, amount, "hub_topup", f"hub:{event_id}", actor="hub"
    )
    add_total_topup(user_id, user_name, amount)

    rec = None
    with _data_lock:
        rows = load_json(HUB_PENDING_FILE, [])
        rec = next((r for r in rows if r.get("user_id") == user_id and r.get("amount") == amount), None)
        if rec is not None:
            rows.remove(rec)
            save_json(HUB_PENDING_FILE, rows)

    promo_bonus = 0
    if rec and rec.get("promo_code"):
        try:
            percent = int(rec.get("promo_percent") or 0)
            if percent > 0 and consume_promo(str(rec["promo_code"]), user_id):
                promo_bonus = amount * percent // 100
                if promo_bonus > 0:
                    new_balance = adjust_wallet_balance(
                        user_id, user_name, promo_bonus, "promo_bonus",
                        f"{rec['promo_code']}:hub:{event_id}", actor="hub",
                    )
        except Exception:  # noqa: BLE001
            logger.exception("Hub promokod bonusida xato (event %s)", event_id)

    bonus_text = f"\n🎁 Promokod bonusi: +{format_sum(promo_bonus)}" if promo_bonus > 0 else ""
    try:
        await bot.send_message(
            chat_id=user_id,
            text=f"🎉 Balansingiz {format_sum(amount)} ga to‘ldirildi!{bonus_text}\n"
                 f"Joriy balans: {format_sum(new_balance)}",
        )
    except TelegramError:
        pass
    try:
        referral = pay_referral_bonus(user_id, amount)
        if referral:
            referrer_id, bonus = referral
            await bot.send_message(
                chat_id=referrer_id,
                text="🎉 Siz taklif qilgan do‘stingiz balansini to‘ldirdi!\n"
                     f"Referal bonus: +{format_sum(bonus)} balansingizga qo‘shildi.",
            )
    except Exception:  # noqa: BLE001
        logger.exception("Hub referal bonusida xato (event %s)", event_id)
    try:
        await notify_admins(bot, f"🏦 Hub: {user_name} balansini {format_sum(amount)} ga to‘ldirdi.")
    except Exception:  # noqa: BLE001
        pass


async def hub_poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hubdan tasdiqlangan to'lovlarni olib turadi."""
    if not hub_ready():
        return
    last = int((load_json(HUB_LAST_FILE, {}) or {}).get("last", 0) or 0)
    try:
        data = await asyncio.to_thread(_hub_sync, "GET", "/api/events", {"after": last})
    except Exception as error:  # noqa: BLE001
        logger.warning("Hub bilan aloqa yo'q: %r", error)
        return
    if not isinstance(data, list):
        return
    for event in data:
        try:
            await hub_credit(context.bot, event)
        except Exception:  # noqa: BLE001
            logger.exception("Hub to'lovi qo'shilmadi (event %s)", event.get("id"))
            return  # keyingi safar shu to'lovdan qayta uriniladi
        save_json(HUB_LAST_FILE, {"last": int(event["id"])})


'''

# (anchor, qo'shiladigan matn, "oldin" yoki "keyin")
EDITS = [
    (
        "\ndef main() -> None:\n",
        "\n" + CONNECTOR,
        "before",
    ),
    (
        '    application.add_handler(CommandHandler("avtotolov", auto_payment_command))\n',
        '    application.add_handler(CallbackQueryHandler(hub_pay_callback, pattern="^hubpay$"))\n',
        "after",
    ),
    (
        "        # Mijoz botlarini ishga tushirish/to'xtatish va ularning buyurtmalarini kuzatish\n",
        "        application.job_queue.run_repeating(hub_poll_job, interval=HUB_POLL_SECONDS, first=20)\n",
        "before",
    ),
    (
        '    await message.reply_text(\n        "\\n".join(lines) + promo_line,\n',
        "    if hub_ready():\n"
        "        rows.insert(\n"
        "            max(len(rows) - 1, 0),\n"
        '            [InlineKeyboardButton("🏦 Karta (avtomatik, Xabarkart)", callback_data="hubpay")],\n'
        "        )\n",
        "before",
    ),
]


def main() -> None:
    with open(SRC, encoding="utf-8") as f:
        text = f.read()
    if "hub_poll_job" in text:
        print("Bu faylga ulagich allaqachon qo'shilgan.")
        return
    for anchor, addition, where in EDITS:
        count = text.count(anchor)
        if count != 1:
            print(f"XATO: kerakli joy topilmadi yoki bir nechta ({count} ta):\n{anchor!r}")
            print("Hech narsa yozilmadi. Fayl versiyasi boshqacha bo'lishi mumkin.")
            return
        text = text.replace(anchor, addition + anchor if where == "before" else anchor + addition)
    with open(DST, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Tayyor: {DST}  (asl {SRC} o'zgarmadi)")


if __name__ == "__main__":
    main()
