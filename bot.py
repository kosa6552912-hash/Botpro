#!/usr/bin/env python3
"""
PRO DIGITAL STORE — Telegram Bot
"""

import logging
import sqlite3
import os

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = 7820423676
CHANNEL_USERNAME = "PDIGITALSTORE"

# ── Conversation states ───────────────────────────────────────────────────────
(
    CHECK_SUB,
    MAIN_MENU,
    SYRIATEL_AMOUNT,
    SYRIATEL_TX,
    SHAMCASH_CURRENCY,
    SHAMCASH_AMOUNT,
    SHAMCASH_TX,
    COINEX_AMOUNT,
    COINEX_SCREENSHOT,
    BINANCE_AMOUNT,
    BINANCE_SCREENSHOT,
    USDT_CHAIN,
    USDT_AMOUNT,
    USDT_TXID,
    ADMIN_BROADCAST,
    ADMIN_CHANGE_RATE,
    ADMIN_ADD_ID,
    ADMIN_ADD_AMOUNT,
) = range(18)

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username    TEXT,
                balance     REAL    DEFAULT 0,
                joined_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS recharge_requests (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER,
                method        TEXT,
                amount        REAL,
                currency      TEXT    DEFAULT 'USD',
                tx_id         TEXT,
                media_file_id TEXT,
                status        TEXT    DEFAULT 'pending',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        c.execute("INSERT OR IGNORE INTO settings VALUES ('exchange_rate', '14000')")
        conn.commit()


def db_get_rate() -> float:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key='exchange_rate'")
        return float(c.fetchone()[0])


def db_set_rate(rate: float) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE settings SET value=? WHERE key='exchange_rate'", (str(rate),)
        )


def db_ensure_user(telegram_id: int, username: str | None = None) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username) VALUES (?,?)",
            (telegram_id, username),
        )


def db_get_balance(telegram_id: int) -> float:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE telegram_id=?", (telegram_id,))
        row = c.fetchone()
        return row[0] if row else 0.0


def db_add_balance(telegram_id: int, amount: float) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE users SET balance=balance+? WHERE telegram_id=?",
            (amount, telegram_id),
        )


def db_create_request(
    user_id: int,
    method: str,
    amount: float,
    currency: str,
    tx_id: str,
    media_file_id: str | None = None,
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO recharge_requests "
            "(user_id,method,amount,currency,tx_id,media_file_id) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, method, amount, currency, tx_id, media_file_id),
        )
        return c.lastrowid  # type: ignore[return-value]


def db_get_request(req_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM recharge_requests WHERE id=?", (req_id,))
        return c.fetchone()


def db_set_status(req_id: int, status: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE recharge_requests SET status=? WHERE id=?", (status, req_id)
        )


def db_all_users() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT telegram_id FROM users")
        return [r[0] for r in c.fetchall()]


# ── Helpers ───────────────────────────────────────────────────────────────────
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["💰 الرصيد", "💱 سعر الصرف"]],
        resize_keyboard=True,
    )


async def is_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(f"@{CHANNEL_USERNAME}", user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False


def approve_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ قبول", callback_data=f"approve_{req_id}"),
            InlineKeyboardButton("❌ رفض",  callback_data=f"reject_{req_id}"),
        ]]
    )


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    db_ensure_user(user.id, user.username)
    context.user_data.clear()

    if await is_subscribed(user.id, context):
        await update.message.reply_text(
            "مرحبا بك في بوت PRO DIGITAL STORE\n"
            "المتجر الرقمي رقم 1\n"
            "اختر من القائمة أدناه 😘",
            reply_markup=main_kb(),
        )
        return MAIN_MENU

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 اشترك في القناة", url=f"https://t.me/{CHANNEL_USERNAME}")],
        [InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="verify_sub")],
    ])
    await update.message.reply_text(
        "👋 مرحباً!\n\n"
        "للاستمرار يرجى الاشتراك في قناتنا أولاً 👇",
        reply_markup=kb,
    )
    return CHECK_SUB


async def cb_verify_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    db_ensure_user(user.id, user.username)

    if await is_subscribed(user.id, context):
        await query.edit_message_text(
            "مرحبا بك في بوت PRO DIGITAL STORE\n"
            "المتجر الرقمي رقم 1\n"
            "اختر من القائمة أدناه 😘"
        )
        await context.bot.send_message(
            user.id,
            "اختر من القائمة أدناه 😘",
            reply_markup=main_kb(),
        )
        return MAIN_MENU

    await query.answer(
        "❌ لم تشترك بعد! اشترك في القناة ثم اضغط تحقق.",
        show_alert=True,
    )
    return CHECK_SUB


# ── Main menu ─────────────────────────────────────────────────────────────────
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text

    if text == "💰 الرصيد":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👛 رصيدي",       callback_data="my_balance"),
            InlineKeyboardButton("💳 شحن الرصيد",  callback_data="recharge_menu"),
        ]])
        await update.message.reply_text("اختر:", reply_markup=kb)

    elif text == "💱 سعر الصرف":
        rate = db_get_rate()
        await update.message.reply_text(
            f"💱 سعر الصرف الحالي:\n1 دولار = {rate:,.0f} ليرة سورية"
        )

    return MAIN_MENU


async def cb_my_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    bal = db_get_balance(uid)
    await query.edit_message_text(
        f"👛 رصيدك الحالي:\n💵 {bal:.2f} دولار\n🆔 معرفك: `{uid}`",
        parse_mode="Markdown",
    )
    return MAIN_MENU


async def cb_recharge_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📱 سيريتل كاش", callback_data="rc_syriatel"),
            InlineKeyboardButton("📱 شام كاش",    callback_data="rc_shamcash"),
        ],
        [
            InlineKeyboardButton("🪙 كوين اكس",   callback_data="rc_coinex"),
            InlineKeyboardButton("💛 بايننس",      callback_data="rc_binance"),
        ],
        [InlineKeyboardButton("💲 USDT",          callback_data="rc_usdt")],
    ])
    await query.edit_message_text("اختر طريقة الشحن:", reply_markup=kb)
    return MAIN_MENU


# ── Syriatel Cash ─────────────────────────────────────────────────────────────
async def cb_syriatel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "لشحن الرصيد عبر syriatel cash:\n\n"
        "1️⃣ أرسل المبلغ إلى أحد هذه الأرقام عن طريق التحويل اليدوي:\n"
        "📞 0934595626\n"
        "📞 0935579034\n\n"
        "2️⃣ أرسل قيمة المبلغ الذي قمت بتحويله\n"
        "3️⃣ أرسل رقم عملية التحويل\n\n"
        "⬇️ الرجاء إرسال مبلغ الشحن:"
    )
    return SYRIATEL_AMOUNT


async def syriatel_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح.")
        return SYRIATEL_AMOUNT
    context.user_data["rc_amount"] = amount
    rate = db_get_rate()
    usd = amount / rate
    await update.message.reply_text(
        f"💸 تفاصيل طلب الشحن\n"
        f"💰 طريقة الدفع: سيريتل كاش\n"
        f"💵 المبلغ المحول: {amount:,.0f} ليرة (~{usd:.2f}$)\n\n"
        "⬇️ الرجاء إرسال رقم عملية التحويل:\n"
        "(أرسل رقم عملية التحويل من سيريتل كاش)"
    )
    return SYRIATEL_TX


async def syriatel_tx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tx = update.message.text.strip()
    uid = update.effective_user.id
    amount = context.user_data.get("rc_amount", 0)
    rate = db_get_rate()
    usd = amount / rate

    req_id = db_create_request(uid, "سيريتل كاش", amount, "SYP", tx)
    await update.message.reply_text(
        "✅ تم إرسال طلب الشحن!\nسيتم مراجعته وإضافة الرصيد قريباً.",
        reply_markup=main_kb(),
    )
    await context.bot.send_message(
        ADMIN_ID,
        f"🔔 طلب شحن جديد\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 المستخدم: {uid}\n"
        f"💳 الطريقة: سيريتل كاش\n"
        f"💵 المبلغ: {amount:,.0f} ليرة (~{usd:.2f}$)\n"
        f"🔢 رقم العملية: {tx}\n"
        f"🆔 رقم الطلب: #{req_id}",
        reply_markup=approve_kb(req_id),
    )
    return MAIN_MENU


# ── Sham Cash ─────────────────────────────────────────────────────────────────
async def cb_shamcash(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("💵 دولار",       callback_data="sham_usd"),
        InlineKeyboardButton("💴 ليرة سورية", callback_data="sham_syp"),
    ]])
    await query.edit_message_text("اختر نوع العملة للشحن: 👇", reply_markup=kb)
    return SHAMCASH_CURRENCY


async def cb_sham_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "sham_usd":
        context.user_data["sham_cur"] = "USD"
        context.user_data["sham_cur_label"] = "دولار"
    else:
        context.user_data["sham_cur"] = "SYP"
        context.user_data["sham_cur_label"] = "ليرة سورية"

    label = context.user_data["sham_cur_label"]
    await query.edit_message_text(
        f"💸 شحن عبر شام كاش\n"
        f"💰 نوع العملة: {label}\n\n"
        f"⬇️ الرجاء تحويل المبلغ إلى الحساب:\n"
        f"`bc9d9b41336308e2a4f9e0ffe86f48a0`\n\n"
        "⬇️ الرجاء إرسال مبلغ الشحن:\n(أدخل الرقم فقط)",
        parse_mode="Markdown",
    )
    return SHAMCASH_AMOUNT


async def shamcash_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح.")
        return SHAMCASH_AMOUNT
    context.user_data["rc_amount"] = amount
    label = context.user_data.get("sham_cur_label", "")
    await update.message.reply_text(
        f"💸 تفاصيل طلب الشحن\n"
        f"💰 نوع العملة: {label}\n"
        f"💱 المبلغ المحول: {amount}\n\n"
        "⬇️ الرجاء إرسال رقم عملية التحويل أو صورة:\n"
        "(أرسل الرقم الخاص بالعملية من تطبيق شام كاش)"
    )
    return SHAMCASH_TX


async def shamcash_tx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    amount = context.user_data.get("rc_amount", 0)
    cur = context.user_data.get("sham_cur", "USD")
    label = context.user_data.get("sham_cur_label", "")
    media_id = None
    tx = ""

    if update.message.photo:
        media_id = update.message.photo[-1].file_id
        tx = "[صورة]"
    else:
        tx = update.message.text.strip()

    rate = db_get_rate()
    usd = amount if cur == "USD" else amount / rate
    req_id = db_create_request(uid, "شام كاش", amount, cur, tx, media_id)

    await update.message.reply_text(
        "✅ تم إرسال طلب الشحن!\nسيتم مراجعته وإضافة الرصيد قريباً.",
        reply_markup=main_kb(),
    )
    caption = (
        f"🔔 طلب شحن جديد\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 المستخدم: {uid}\n"
        f"💳 الطريقة: شام كاش\n"
        f"💰 العملة: {label}\n"
        f"💵 المبلغ: {amount} (~{usd:.2f}$)\n"
        f"🔢 رقم العملية: {tx}\n"
        f"🆔 رقم الطلب: #{req_id}"
    )
    if media_id:
        await context.bot.send_photo(
            ADMIN_ID, media_id, caption=caption, reply_markup=approve_kb(req_id)
        )
    else:
        await context.bot.send_message(
            ADMIN_ID, caption, reply_markup=approve_kb(req_id)
        )
    return MAIN_MENU


# ── CoinEx ────────────────────────────────────────────────────────────────────
async def cb_coinex(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "لشحن الرصيد عن طريق كوين اكس coinex\n\n"
        "1️⃣ أرسل المبلغ إلى العنوان: `8797256`\n"
        "2️⃣ أرسل قيمة المبلغ الذي قمت بتحويله\n\n"
        "⬇️ أدخل المبلغ:",
        parse_mode="Markdown",
    )
    return COINEX_AMOUNT


async def coinex_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح.")
        return COINEX_AMOUNT
    context.user_data["rc_amount"] = amount
    await update.message.reply_text(
        "📸 أرسل لقطة شاشة بعملية التحويل لتأكيد عملية الدفع.\n"
        "سيتم إضافة الرصيد بعد تأكيد التحويل."
    )
    return COINEX_SCREENSHOT


async def coinex_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("❌ يرجى إرسال صورة لقطة الشاشة.")
        return COINEX_SCREENSHOT
    uid = update.effective_user.id
    amount = context.user_data.get("rc_amount", 0)
    media_id = update.message.photo[-1].file_id
    req_id = db_create_request(uid, "كوين اكس", amount, "USD", "[screenshot]", media_id)

    await update.message.reply_text(
        "✅ تم إرسال طلب الشحن!\nسيتم مراجعته وإضافة الرصيد قريباً.",
        reply_markup=main_kb(),
    )
    await context.bot.send_photo(
        ADMIN_ID,
        media_id,
        caption=(
            f"🔔 طلب شحن جديد\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 المستخدم: {uid}\n"
            f"💳 الطريقة: كوين اكس\n"
            f"💵 المبلغ: {amount}$\n"
            f"🆔 رقم الطلب: #{req_id}"
        ),
        reply_markup=approve_kb(req_id),
    )
    return MAIN_MENU


# ── Binance ───────────────────────────────────────────────────────────────────
async def cb_binance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "لشحن الرصيد أرسل المبلغ المراد تحويله إلى العنوان:\n"
        "`1232265029`\n\n"
        "⬇️ الرجاء إرسال مبلغ الشحن الذي قمت بتحويله:",
        parse_mode="Markdown",
    )
    return BINANCE_AMOUNT


async def binance_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح.")
        return BINANCE_AMOUNT
    context.user_data["rc_amount"] = amount
    await update.message.reply_text(
        "📸 أرسل لقطة شاشة بعملية التحويل لتأكيد عملية الدفع.\n"
        "سيتم إضافة الرصيد بعد تأكيد التحويل."
    )
    return BINANCE_SCREENSHOT


async def binance_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("❌ يرجى إرسال صورة لقطة الشاشة.")
        return BINANCE_SCREENSHOT
    uid = update.effective_user.id
    amount = context.user_data.get("rc_amount", 0)
    media_id = update.message.photo[-1].file_id
    req_id = db_create_request(uid, "بايننس", amount, "USD", "[screenshot]", media_id)

    await update.message.reply_text(
        "✅ تم إرسال طلب الشحن!\nسيتم مراجعته وإضافة الرصيد قريباً.",
        reply_markup=main_kb(),
    )
    await context.bot.send_photo(
        ADMIN_ID,
        media_id,
        caption=(
            f"🔔 طلب شحن جديد\n"
            f"━━━━━━━━━━━━━━\n"
            f"👤 المستخدم: {uid}\n"
            f"💳 الطريقة: بايننس\n"
            f"💵 المبلغ: {amount}$\n"
            f"🆔 رقم الطلب: #{req_id}"
        ),
        reply_markup=approve_kb(req_id),
    )
    return MAIN_MENU


# ── USDT ──────────────────────────────────────────────────────────────────────
async def cb_usdt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔷 USDT BEP20", callback_data="usdt_bep20"),
        InlineKeyboardButton("🔴 USDT TRC20", callback_data="usdt_trc20"),
    ]])
    await query.edit_message_text(
        "اختر نوع السلسلة التي تريد:", reply_markup=kb
    )
    return USDT_CHAIN


async def cb_usdt_chain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "usdt_bep20":
        context.user_data["usdt_chain"] = "BEP20"
        wallet = "0xaace6d4956b27c293018556bedba49a5074d6020"
        chain_label = "Bep 20"
    else:
        context.user_data["usdt_chain"] = "TRC20"
        wallet = "TPJeB72mNWakoh7w7FuiCqEft2af7rBSU4"
        chain_label = "Trc 20"

    await query.edit_message_text(
        f"أرسل المبلغ الى عنوان المحفظة\n\n"
        f"⭐ عبر سلسلة {chain_label} : (حصرا)\n"
        f"`{wallet}`\n\n"
        f"⭐️ ملاحظة هامة : التعامل فقط بعملة USDT ولا يقبل الدفع بأي عملة أخرى\n"
        f"‼️ تستغرق العملية بين 10 دقائق ل 20 دقيقة لتأكيد العملية على الشبكة "
        f"رجاء لا تتواصل مع الدعم قبل انقضاء هذه المدة\n\n"
        "ادخل المبلغ المراد أرساله 👇",
        parse_mode="Markdown",
    )
    return USDT_AMOUNT


async def usdt_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح.")
        return USDT_AMOUNT
    context.user_data["rc_amount"] = amount
    await update.message.reply_text(
        "🔰 قم بإرسال Txid / Hash والمبلغ المشحون بينهما مسافة على الشكل التالي مثال 👇  "
        "أو صورة بعملية التحويل\n\n"
        "`d4c832cf549e7318ea7319c45fb0a8 2`",
        parse_mode="Markdown",
    )
    return USDT_TXID


async def usdt_txid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    amount = context.user_data.get("rc_amount", 0)
    chain = context.user_data.get("usdt_chain", "")
    media_id = None
    tx = ""

    if update.message.photo:
        media_id = update.message.photo[-1].file_id
        tx = "[صورة]"
    else:
        tx = update.message.text.strip()

    req_id = db_create_request(uid, f"USDT {chain}", amount, "USD", tx, media_id)
    await update.message.reply_text(
        "✅ تم إرسال طلب الشحن!\nسيتم مراجعته وإضافة الرصيد قريباً.",
        reply_markup=main_kb(),
    )
    msg_text = (
        f"🔔 طلب شحن جديد\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 المستخدم: {uid}\n"
        f"💳 الطريقة: USDT {chain}\n"
        f"💵 المبلغ: {amount}$\n"
        f"🔢 TxID: {tx}\n"
        f"🆔 رقم الطلب: #{req_id}"
    )
    if media_id:
        await context.bot.send_photo(
            ADMIN_ID, media_id, caption=msg_text, reply_markup=approve_kb(req_id)
        )
    else:
        await context.bot.send_message(
            ADMIN_ID, msg_text, reply_markup=approve_kb(req_id)
        )
    return MAIN_MENU


# ── Admin approve / reject (global handler) ───────────────────────────────────
async def cb_approve_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if update.effective_user.id != ADMIN_ID:
        await query.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    action, req_id_str = query.data.split("_", 1)
    req_id = int(req_id_str)
    row = db_get_request(req_id)

    if not row:
        await query.answer("❌ الطلب غير موجود.", show_alert=True)
        return

    # id, user_id, method, amount, currency, tx_id, media_file_id, status, created_at
    _, user_id, method, amount, currency, tx_id, media_file_id, status, created_at = row

    if status != "pending":
        await query.answer(f"تمت معالجة هذا الطلب مسبقاً ({status})", show_alert=True)
        return

    await query.answer()

    if action == "approve":
        rate = db_get_rate()
        usd = amount / rate if currency == "SYP" else amount
        db_set_status(req_id, "approved")
        db_add_balance(user_id, usd)

        await query.edit_message_caption(
            caption=(
                f"✅ تم قبول الطلب #{req_id}\n"
                f"👤 المستخدم: {user_id}\n"
                f"💵 المبلغ المضاف: {usd:.2f}$"
            )
        ) if media_file_id else await query.edit_message_text(
            f"✅ تم قبول الطلب #{req_id}\n"
            f"👤 المستخدم: {user_id}\n"
            f"💵 المبلغ المضاف: {usd:.2f}$"
        )

        try:
            await context.bot.send_message(
                user_id,
                f"✅ تم قبول طلب الشحن الخاص بك\n"
                f"💵 تم إضافة {usd:.2f}$ إلى رصيدك\n"
                f"🆔 رقم الطلب: #{req_id}",
            )
        except Exception:
            pass

    else:  # reject
        db_set_status(req_id, "rejected")

        await query.edit_message_caption(
            caption=f"❌ تم رفض الطلب #{req_id}\n👤 المستخدم: {user_id}"
        ) if media_file_id else await query.edit_message_text(
            f"❌ تم رفض الطلب #{req_id}\n👤 المستخدم: {user_id}"
        )

        try:
            await context.bot.send_message(
                user_id,
                f"❌ تم رفض طلب الشحن الخاص بك\n"
                f"🆔 رقم الطلب: #{req_id}\n"
                "للاستفسار تواصل مع الدعم.",
            )
        except Exception:
            pass


# ── Admin panel ───────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 رسالة جماعية",        callback_data="admin_broadcast")],
        [InlineKeyboardButton("💱 تغيير سعر الصرف",    callback_data="admin_rate")],
        [InlineKeyboardButton("➕ إضافة رصيد يدوياً",  callback_data="admin_add_balance")],
    ])
    await update.message.reply_text("🛠 لوحة الإدارة:", reply_markup=kb)
    return MAIN_MENU


async def cb_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    await query.edit_message_text(
        "✏️ أرسل الرسالة التي تريد إرسالها لجميع المستخدمين:"
    )
    return ADMIN_BROADCAST


async def admin_broadcast_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    users = db_all_users()
    success = 0
    for uid in users:
        try:
            await context.bot.copy_message(uid, update.message.chat_id, update.message.message_id)
            success += 1
        except Exception:
            pass
    await update.message.reply_text(
        f"✅ تم الإرسال إلى {success}/{len(users)} مستخدم.",
        reply_markup=main_kb(),
    )
    return MAIN_MENU


async def cb_admin_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    rate = db_get_rate()
    await query.edit_message_text(
        f"💱 سعر الصرف الحالي: {rate:,.0f} ليرة/دولار\n\n"
        "ضع سعر صرف الدولار مقابل الليرة السورية:"
    )
    return ADMIN_CHANGE_RATE


async def admin_change_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    try:
        rate = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح.")
        return ADMIN_CHANGE_RATE
    db_set_rate(rate)
    await update.message.reply_text(
        f"✅ تم تحديث سعر الصرف إلى {rate:,.0f} ليرة/دولار",
        reply_markup=main_kb(),
    )
    return MAIN_MENU


async def cb_admin_add_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    await query.edit_message_text("قم بإدخال ID المستخدم لإضافة رصيد يدوياً:")
    return ADMIN_ADD_ID


async def admin_add_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    try:
        target_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال ID صحيح (أرقام فقط).")
        return ADMIN_ADD_ID
    context.user_data["add_balance_target"] = target_id
    await update.message.reply_text("كم دولار تريد أن تضيف له؟")
    return ADMIN_ADD_AMOUNT


async def admin_add_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    try:
        amount = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ يرجى إدخال رقم صحيح.")
        return ADMIN_ADD_AMOUNT
    target_id = context.user_data.get("add_balance_target")
    db_ensure_user(target_id)
    db_add_balance(target_id, amount)
    await update.message.reply_text(
        f"✅ تم إضافة {amount:.2f}$ لحساب المستخدم {target_id}",
        reply_markup=main_kb(),
    )
    try:
        await context.bot.send_message(
            target_id, f"🎉 تم إضافة {amount:.2f}$ إلى رصيدك!"
        )
    except Exception:
        pass
    return MAIN_MENU


# ── Cancel ────────────────────────────────────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("تم الإلغاء.", reply_markup=main_kb())
    return MAIN_MENU


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    init_db()

    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        logger.error("BOT_TOKEN environment variable is not set!")
        return

    request = HTTPXRequest(
        connect_timeout=60,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=60,
    )
    app = Application.builder().token(token).request(request).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("admin", cmd_admin),
        ],
        states={
            CHECK_SUB: [
                CallbackQueryHandler(cb_verify_sub, pattern="^verify_sub$"),
            ],
            MAIN_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu),
                CallbackQueryHandler(cb_my_balance,      pattern="^my_balance$"),
                CallbackQueryHandler(cb_recharge_menu,   pattern="^recharge_menu$"),
                CallbackQueryHandler(cb_syriatel,        pattern="^rc_syriatel$"),
                CallbackQueryHandler(cb_shamcash,        pattern="^rc_shamcash$"),
                CallbackQueryHandler(cb_coinex,          pattern="^rc_coinex$"),
                CallbackQueryHandler(cb_binance,         pattern="^rc_binance$"),
                CallbackQueryHandler(cb_usdt,            pattern="^rc_usdt$"),
                CallbackQueryHandler(cb_admin_broadcast, pattern="^admin_broadcast$"),
                CallbackQueryHandler(cb_admin_rate,      pattern="^admin_rate$"),
                CallbackQueryHandler(cb_admin_add_balance, pattern="^admin_add_balance$"),
            ],
            SYRIATEL_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, syriatel_amount)],
            SYRIATEL_TX:      [MessageHandler(filters.TEXT & ~filters.COMMAND, syriatel_tx)],
            SHAMCASH_CURRENCY:[CallbackQueryHandler(cb_sham_currency, pattern="^sham_(usd|syp)$")],
            SHAMCASH_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, shamcash_amount)],
            SHAMCASH_TX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, shamcash_tx),
                MessageHandler(filters.PHOTO, shamcash_tx),
            ],
            COINEX_AMOUNT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, coinex_amount)],
            COINEX_SCREENSHOT:[MessageHandler(filters.PHOTO, coinex_screenshot)],
            BINANCE_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, binance_amount)],
            BINANCE_SCREENSHOT:[MessageHandler(filters.PHOTO, binance_screenshot)],
            USDT_CHAIN:       [CallbackQueryHandler(cb_usdt_chain, pattern="^usdt_(bep20|trc20)$")],
            USDT_AMOUNT:      [MessageHandler(filters.TEXT & ~filters.COMMAND, usdt_amount)],
            USDT_TXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, usdt_txid),
                MessageHandler(filters.PHOTO, usdt_txid),
            ],
            ADMIN_BROADCAST:  [MessageHandler(filters.ALL & ~filters.COMMAND, admin_broadcast_msg)],
            ADMIN_CHANGE_RATE:[MessageHandler(filters.TEXT & ~filters.COMMAND, admin_change_rate)],
            ADMIN_ADD_ID:     [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_id)],
            ADMIN_ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_amount)],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start",  cmd_start),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    # Approve/reject live outside the conversation so admin can act from any chat context
    app.add_handler(
        CallbackQueryHandler(cb_approve_reject, pattern=r"^(approve|reject)_\d+$")
    )

    logger.info("🚀 PRO DIGITAL STORE bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
