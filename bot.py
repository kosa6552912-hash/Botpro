#!/usr/bin/env python3
"""
PRO DIGITAL STORE — Telegram Bot (MongoDB edition) — FIXED VERSION
"""

import logging
import os
import time
from datetime import datetime, timezone
from uuid import uuid4

import motor.motor_asyncio
from pymongo import ReturnDocument

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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
MONGODB_URL = os.environ.get("MONGODB_URL", "").strip()
ADMIN_ID = 7820423676
CHANNEL_USERNAME = "PDIGITALSTORE"

# ── MongoDB ───────────────────────────────────────────────────────────────────
_mongo_client: motor.motor_asyncio.AsyncIOMotorClient | None = None
_db: motor.motor_asyncio.AsyncIOMotorDatabase | None = None


def get_db() -> motor.motor_asyncio.AsyncIOMotorDatabase:
    return _db  # type: ignore[return-value]


async def init_db() -> None:
    global _mongo_client, _db
    _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
        MONGODB_URL,
        tlsAllowInvalidCertificates=True,
        serverSelectionTimeoutMS=10000,
        connectTimeoutMS=10000,
        socketTimeoutMS=30000,
    )
    _db = _mongo_client["pro_digital_store"]

    # Ensure indexes
    await _db.users.create_index("telegram_id", unique=True)
    await _db.recharge_requests.create_index("_seq")
    await _db.settings.create_index("key", unique=True)
    await _db.services.create_index("slug", unique=True)
    await _db.accounts.create_index([("service_slug", 1), ("status", 1)])
    await _db.purchases.create_index("purchased_at")

    # Seed exchange rate if missing
    await _db.settings.update_one(
        {"key": "exchange_rate"},
        {"$setOnInsert": {"key": "exchange_rate", "value": "14000"}},
        upsert=True,
    )
    for service in ("icloud", "gmail", "outlook", "paypal"):
        await _db.services.update_one(
            {"slug": service},
            {
                "$setOnInsert": {
                    "slug": service,
                    "name": service,
                    "active": True,
                    "created_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )
    logger.info("✅ MongoDB connected and initialized.")


async def db_get_rate() -> float:
    doc = await get_db().settings.find_one({"key": "exchange_rate"})
    return float(doc["value"]) if doc else 14000.0


async def db_set_rate(rate: float) -> None:
    await get_db().settings.update_one(
        {"key": "exchange_rate"},
        {"$set": {"value": str(rate)}},
    )


async def db_ensure_user(telegram_id: int, username: str | None = None) -> None:
    await get_db().users.update_one(
        {"telegram_id": telegram_id},
        {
            "$setOnInsert": {
                "telegram_id": telegram_id,
                "username": username,
                "balance": 0.0,
                "joined_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


async def db_get_balance(telegram_id: int) -> float:
    doc = await get_db().users.find_one({"telegram_id": telegram_id})
    return doc["balance"] if doc else 0.0


async def db_add_balance(telegram_id: int, amount: float) -> None:
    await get_db().users.update_one(
        {"telegram_id": telegram_id},
        {"$inc": {"balance": amount}},
    )


async def _next_request_id() -> int:
    """Auto-increment counter stored in a 'counters' collection."""
    result = await get_db().counters.find_one_and_update(
        {"_id": "recharge_requests"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return result["seq"]


async def db_create_request(
    user_id: int,
    method: str,
    amount: float,
    currency: str,
    tx_id: str,
    media_file_id: str | None = None,
) -> int:
    req_id = await _next_request_id()
    await get_db().recharge_requests.insert_one(
        {
            "id": req_id,
            "user_id": user_id,
            "method": method,
            "amount": amount,
            "currency": currency,
            "tx_id": tx_id,
            "media_file_id": media_file_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        }
    )
    return req_id


async def db_get_request(req_id: int):
    return await get_db().recharge_requests.find_one({"id": req_id})


async def db_set_status(req_id: int, status: str) -> None:
    await get_db().recharge_requests.update_one(
        {"id": req_id},
        {"$set": {"status": status}},
    )


async def db_all_users() -> list[int]:
    cursor = get_db().users.find({}, {"telegram_id": 1})
    return [doc["telegram_id"] async for doc in cursor]


# ── Services, inventory, purchases ────────────────────────────────────────────
async def db_active_services() -> list[dict]:
    return await get_db().services.find({"active": True}).sort("name", 1).to_list(length=100)


async def db_get_service(slug: str) -> dict | None:
    return await get_db().services.find_one({"slug": slug, "active": True})


async def db_add_service(name: str) -> bool:
    slug = "-".join(name.lower().strip().split())
    if not slug:
        return False
    existing = await get_db().services.find_one({"slug": slug})
    if existing:
        if existing.get("active"):
            return False
        await get_db().services.update_one(
            {"slug": slug},
            {"$set": {"name": name.strip(), "active": True}},
        )
        return True
    try:
        await get_db().services.insert_one({
            "slug": slug,
            "name": name.strip(),
            "active": True,
            "created_at": datetime.now(timezone.utc),
        })
        return True
    except Exception:
        return False


async def db_delete_service(slug: str) -> bool:
    result = await get_db().services.update_one(
        {"slug": slug},
        {"$set": {"active": False}},
    )
    return result.modified_count > 0


async def db_add_account(service_slug: str, info: str, price: float) -> str:
    account_id = uuid4().hex[:12]
    service = await get_db().services.find_one({"slug": service_slug})
    await get_db().accounts.insert_one({
        "account_id": account_id,
        "service_slug": service_slug,
        "service_name": service["name"] if service else service_slug,
        "info": info,
        "price": price,
        "status": "available",
        "created_at": datetime.now(timezone.utc),
    })
    return account_id


async def db_available_accounts(service_slug: str) -> list[dict]:
    return await get_db().accounts.find(
        {"service_slug": service_slug, "status": "available"}
    ).sort("created_at", 1).to_list(length=100)


async def db_purchase_account(user_id: int, account_id: str, username: str | None, full_name: str) -> dict | None:
    account = await get_db().accounts.find_one(
        {"account_id": account_id, "status": "available"}
    )
    if not account:
        return None
    price = float(account["price"])
    debit = await get_db().users.update_one(
        {"telegram_id": user_id, "balance": {"$gte": price}},
        {"$inc": {"balance": -price}},
    )
    if debit.modified_count != 1:
        return None
    claimed = await get_db().accounts.update_one(
        {"account_id": account_id, "status": "available"},
        {
            "$set": {
                "status": "sold",
                "sold_at": datetime.now(timezone.utc),
                "sold_to": user_id,
            }
        },
    )
    if claimed.modified_count != 1:
        await db_add_balance(user_id, price)
        return None
    purchase = {
        "purchase_id": uuid4().hex[:12],
        "user_id": user_id,
        "username": username,
        "full_name": full_name,
        "service_slug": account["service_slug"],
        "service_name": account["service_name"],
        "account_id": account_id,
        "price": price,
        "purchased_at": datetime.now(timezone.utc),
    }
    # FIX: was "paurchases" (typo) — now correctly "purchases"
    await get_db().purchases.insert_one(purchase)
    return {**account, **purchase}


async def db_recent_purchases(limit: int = 50) -> list[dict]:
    return await get_db().purchases.find().sort("purchased_at", -1).to_list(length=limit)


async def db_stats() -> dict:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    users = get_db().users
    purchases = get_db().purchases
    today_users = await users.count_documents({"joined_at": {"$gte": day_start}})
    total_users = await users.count_documents({})
    orders = await purchases.count_documents({})
    today_orders = await purchases.count_documents({"purchased_at": {"$gte": day_start}})
    month_orders = await purchases.count_documents({"purchased_at": {"$gte": month_start}})
    charged = await get_db().recharge_requests.aggregate([
        {"$match": {"status": "approved"}},
        {"$group": {"_id": None, "total": {"$sum": "$usd_amount"}}},
    ]).to_list(length=1)
    charged_total = float(charged[0]["total"]) if charged else 0.0
    revenue_today = await purchases.aggregate([
        {"$match": {"purchased_at": {"$gte": day_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$price"}}},
    ]).to_list(length=1)
    revenue_month = await purchases.aggregate([
        {"$match": {"purchased_at": {"$gte": month_start}}},
        {"$group": {"_id": None, "total": {"$sum": "$price"}}},
    ]).to_list(length=1)
    top = await purchases.aggregate([
        {"$group": {"_id": "$service_name", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 1},
    ]).to_list(length=1)
    last = await purchases.find_one(sort=[("purchased_at", -1)])
    return {
        "total_users": total_users,
        "today_users": today_users,
        "charged_total": charged_total,
        "orders": orders,
        "today_orders": today_orders,
        "month_orders": month_orders,
        "products_sold": orders,
        "revenue_today": float(revenue_today[0]["total"]) if revenue_today else 0.0,
        "revenue_month": float(revenue_month[0]["total"]) if revenue_month else 0.0,
        "top_product": top[0]["_id"] if top else "لا يوجد",
        "last_purchase": last,
    }


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
    SERVICES_MENU,
    SERVICE_ACCOUNTS,
    ACCOUNT_INFO,
    ACCOUNT_PRICE,
    ADMIN_ADD_SERVICE,
    ADMIN_DELETE_SERVICE,
    ADMIN_ADD_ACCOUNT_SERVICE,
    ADMIN_ADD_ACCOUNT_INFO,
    ADMIN_ADD_ACCOUNT_PRICE,
    ADMIN_PURCHASES,
    ADMIN_STATS,
) = range(29)


# ── Helpers ───────────────────────────────────────────────────────────────────
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["🛍 الخدمات", "🆘 الدعم"], ["💰 الرصيد", "💱 سعر الصرف"]],
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
    await db_ensure_user(user.id, user.username)
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
    await db_ensure_user(user.id, user.username)

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

    if text == "🛍 الخدمات":
        services = await db_active_services()
        if not services:
            await update.message.reply_text("لا توجد خدمات متاحة حالياً.")
            return MAIN_MENU
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{index}. {service['name']}", callback_data=f"service_{service['slug']}")]
            for index, service in enumerate(services, 1)
        ])
        await update.message.reply_text("اختر الخدمة التي تريد شراء حساب منها:", reply_markup=kb)

    elif text == "🆘 الدعم":
        await update.message.reply_text(
            "للتواصل مع الدعم اضغط الزر التالي:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 التواصل مع الدعم", url="https://t.me/KOKE6552")
            ]]),
        )

    elif text == "💰 الرصيد":
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👛 رصيدي",       callback_data="my_balance"),
            InlineKeyboardButton("💳 شحن الرصيد",  callback_data="recharge_menu"),
        ]])
        await update.message.reply_text("اختر:", reply_markup=kb)

    elif text == "💱 سعر الصرف":
        rate = await db_get_rate()
        await update.message.reply_text(
            f"💱 سعر الصرف الحالي:\n1 دولار = {rate:,.0f} ليرة سورية"
        )

    return MAIN_MENU


async def cb_service_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    service_slug = query.data.removeprefix("service_")
    service = await db_get_service(service_slug)
    if not service:
        await query.edit_message_text("هذه الخدمة غير متاحة حالياً.")
        return MAIN_MENU
    accounts = await db_available_accounts(service_slug)
    if not accounts:
        await query.edit_message_text(f"لا توجد حسابات متاحة لخدمة {service['name']} حالياً.")
        return MAIN_MENU
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🛒 {service['name']} — ${float(account['price']):.2f}",
            callback_data=f"buy_{account['account_id']}",
        )]
        for account in accounts
    ])
    await query.edit_message_text(
        f"الحسابات المتاحة في خدمة {service['name']}:\nاختر المنتج المناسب:",
        reply_markup=kb,
    )
    return MAIN_MENU


async def cb_buy_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    purchase = await db_purchase_account(
        user.id,
        query.data.removeprefix("buy_"),
        user.username,
        user.full_name,
    )
    if not purchase:
        balance = await db_get_balance(user.id)
        await query.edit_message_text(
            f"تعذر إتمام الشراء. قد يكون المنتج بيع لمستخدم آخر أو رصيدك غير كافٍ.\n"
            f"رصيدك الحالي: ${balance:.2f}"
        )
        return MAIN_MENU
    await query.edit_message_text(
        f"✅ تمت عملية الشراء بنجاح\n"
        f"📦 الخدمة: {purchase['service_name']}\n"
        f"💵 السعر: ${float(purchase['price']):.2f}\n"
        f"🆔 رقم العملية: {purchase['purchase_id']}\n\n"
        f"🔐 معلومات الحساب:\n{purchase['info']}"
    )
    await context.bot.send_message(
        ADMIN_ID,
        f"🛒 عملية شراء جديدة\n"
        f"الخدمة: {purchase['service_name']}\n"
        f"السعر: ${float(purchase['price']):.2f}\n"
        f"المستخدم: {user.full_name} (@{user.username or 'بدون معرف'})\n"
        f"ID: {user.id}\n"
        f"التاريخ: {purchase['purchased_at'].astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"رقم العملية: {purchase['purchase_id']}",
    )
    return MAIN_MENU


async def cb_my_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    bal = await db_get_balance(uid)
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
    rate = await db_get_rate()
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
    rate = await db_get_rate()
    usd = amount / rate

    req_id = await db_create_request(uid, "سيريتل كاش", amount, "SYP", tx)
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

    rate = await db_get_rate()
    usd = amount if cur == "USD" else amount / rate
    req_id = await db_create_request(uid, "شام كاش", amount, cur, tx, media_id)

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
    req_id = await db_create_request(uid, "كوين اكس", amount, "USD", "[screenshot]", media_id)

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
    req_id = await db_create_request(uid, "بايننس", amount, "USD", "[screenshot]", media_id)

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

    req_id = await db_create_request(uid, f"USDT {chain}", amount, "USD", tx, media_id)
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


# ── Admin approve / reject ────────────────────────────────────────────────────
async def cb_approve_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if update.effective_user.id != ADMIN_ID:
        await query.answer("⛔ غير مصرح لك.", show_alert=True)
        return

    action, req_id_str = query.data.split("_", 1)
    req_id = int(req_id_str)
    doc = await db_get_request(req_id)

    if not doc:
        await query.answer("❌ الطلب غير موجود.", show_alert=True)
        return

    if doc["status"] != "pending":
        await query.answer(f"تمت معالجة هذا الطلب مسبقاً ({doc['status']})", show_alert=True)
        return

    await query.answer()

    user_id = doc["user_id"]
    method = doc["method"]
    amount = doc["amount"]
    currency = doc["currency"]
    media_file_id = doc.get("media_file_id")

    if action == "approve":
        rate = await db_get_rate()
        usd = amount / rate if currency == "SYP" else amount
        await db_set_status(req_id, "approved")
        await db_add_balance(user_id, usd)

        caption_text = (
            f"✅ تم قبول الطلب #{req_id}\n"
            f"👤 المستخدم: {user_id}\n"
            f"💵 المبلغ المضاف: {usd:.2f}$"
        )
        if media_file_id:
            await query.edit_message_caption(caption=caption_text)
        else:
            await query.edit_message_text(caption_text)

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
        await db_set_status(req_id, "rejected")

        reject_text = f"❌ تم رفض الطلب #{req_id}\n👤 المستخدم: {user_id}"
        if media_file_id:
            await query.edit_message_caption(caption=reject_text)
        else:
            await query.edit_message_text(reject_text)

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
        [
            InlineKeyboardButton("➕ إضافة خدمة", callback_data="admin_add_service"),
            InlineKeyboardButton("➖ حذف خدمة", callback_data="admin_delete_service"),
        ],
        [InlineKeyboardButton("📦 إضافة حساب", callback_data="admin_add_account")],
        [
            InlineKeyboardButton("🛒 عمليات الشراء", callback_data="admin_purchases"),
            InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats"),
        ],
    ])
    await update.message.reply_text("🛠 لوحة الإدارة:", reply_markup=kb)
    return MAIN_MENU


def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ لوحة الإدارة", callback_data="admin_home"),
    ]])



async def cb_admin_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return MAIN_MENU
    await query.edit_message_text(
        "🛠 لوحة الإدارة\nاستخدم /admin لعرض كل أدوات الإدارة.",
        reply_markup=admin_panel_kb(),
    )
    return MAIN_MENU


async def cb_admin_add_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return MAIN_MENU
    await query.edit_message_text(
        "أرسل اسم الخدمة الجديدة فقط، مثل: Netflix أو Spotify"
    )
    return ADMIN_ADD_SERVICE


async def admin_add_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    name = update.message.text.strip()
    if len(name) < 2 or len(name) > 40:
        await update.message.reply_text("❌ اسم الخدمة يجب أن يكون بين حرفين و40 حرفاً.")
        return ADMIN_ADD_SERVICE
    if await db_add_service(name):
        await update.message.reply_text(f"✅ تمت إضافة الخدمة: {name}", reply_markup=main_kb())
    else:
        await update.message.reply_text("❌ لم تتم الإضافة. ربما الخدمة موجودة مسبقاً.")
    return MAIN_MENU


async def cb_admin_delete_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return MAIN_MENU
    services = await db_active_services()
    if not services:
        await query.edit_message_text("لا توجد خدمات لحذفها.")
        return MAIN_MENU
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🗑 {service['name']}", callback_data=f"delete_service_{service['slug']}")]
        for service in services
    ])
    await query.edit_message_text("اختر الخدمة التي تريد إيقافها:", reply_markup=kb)
    return ADMIN_DELETE_SERVICE


async def cb_delete_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return MAIN_MENU
    slug = query.data.removeprefix("delete_service_")
    if await db_delete_service(slug):
        await query.edit_message_text("✅ تم حذف الخدمة من قائمة الخدمات. الحسابات القديمة محفوظة في السجل.")
    else:
        await query.edit_message_text("❌ لم يتم العثور على الخدمة.")
    return MAIN_MENU


async def cb_admin_add_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return MAIN_MENU
    services = await db_active_services()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(service["name"], callback_data=f"add_account_service_{service['slug']}")]
        for service in services
    ])
    await query.edit_message_text("اختر الخدمة التي تريد إضافة حساب إليها:", reply_markup=kb)
    return ADMIN_ADD_ACCOUNT_SERVICE


async def cb_add_account_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return MAIN_MENU
    slug = query.data.removeprefix("add_account_service_")
    if not await db_get_service(slug):
        await query.edit_message_text("❌ الخدمة غير متاحة.")
        return MAIN_MENU
    context.user_data["account_service"] = slug
    await query.edit_message_text(
        "أدخل معلومات الحساب الذي تريد بيعه.\n"
        "يمكنك إرسال البريد وكلمة المرور وأي تفاصيل إضافية في رسالة واحدة."
    )
    return ADMIN_ADD_ACCOUNT_INFO


async def admin_add_account_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    info = update.message.text.strip()
    if not info:
        await update.message.reply_text("❌ أرسل معلومات الحساب.")
        return ADMIN_ADD_ACCOUNT_INFO
    context.user_data["account_info"] = info
    await update.message.reply_text("أدخل سعر المنتج بالدولار، مثل: 5.50")
    return ADMIN_ADD_ACCOUNT_PRICE


async def admin_add_account_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_ID:
        return MAIN_MENU
    try:
        price = float(update.message.text.strip())
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ أدخل سعراً صحيحاً أكبر من صفر.")
        return ADMIN_ADD_ACCOUNT_PRICE
    account_id = await db_add_account(
        context.user_data["account_service"],
        context.user_data["account_info"],
        price,
    )
    await update.message.reply_text(
        f"✅ تمت إضافة الحساب للمخزون\n🆔 رقم المنتج: {account_id}\n💵 السعر: ${price:.2f}",
        reply_markup=main_kb(),
    )
    return MAIN_MENU


async def cb_admin_purchases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return MAIN_MENU
    purchases = await db_recent_purchases()
    if not purchases:
        await query.edit_message_text("لا توجد عمليات شراء حتى الآن.", reply_markup=admin_panel_kb())
        return ADMIN_PURCHASES
    lines = ["🛒 آخر عمليات الشراء:"]
    for purchase in purchases:
        when = purchase["purchased_at"].astimezone().strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"\n#{purchase['purchase_id']} | {when}\n"
            f"📦 {purchase['service_name']} — ${float(purchase['price']):.2f}\n"
            f"👤 {purchase['full_name']} | ID: {purchase['user_id']}"
        )
    text = "\n".join(lines)
    await query.edit_message_text(text[:4000], reply_markup=admin_panel_kb())
    return ADMIN_PURCHASES


async def cb_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return MAIN_MENU
    stats = await db_stats()
    last = stats["last_purchase"]
    last_text = "لا توجد عمليات شراء"
    if last:
        last_text = last["purchased_at"].astimezone().strftime("%Y-%m-%d %H:%M")
    text = (
        "📊 إحصائيات البوت\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 عدد المستخدمين: {stats['total_users']:,}\n"
        f"🟢 المستخدمون اليوم: {stats['today_users']:,}\n"
        f"💰 إجمالي الرصيد المشحون: ${stats['charged_total']:,.2f}\n"
        f"🛒 عدد الطلبات: {stats['orders']:,}\n"
        f"📦 المنتجات المباعة: {stats['products_sold']:,}\n"
        f"💵 أرباح اليوم: ${stats['revenue_today']:,.2f}\n"
        f"💵 أرباح هذا الشهر: ${stats['revenue_month']:,.2f}\n"
        f"⭐ أكثر منتج مبيعاً: {stats['top_product']}\n"
        f"📅 آخر عملية شراء: {last_text}"
    )
    await query.edit_message_text(text, reply_markup=admin_panel_kb())
    return ADMIN_STATS


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
    users = await db_all_users()
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
    rate = await db_get_rate()
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
    await db_set_rate(rate)
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
    await db_ensure_user(target_id)
    await db_add_balance(target_id, amount)
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
async def post_init(application: Application) -> None:
    """Called after the Application is initialized — connect to MongoDB here."""
    await init_db()


def main() -> None:
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is not set!")
        return
    if not MONGODB_URL:
        logger.error("MONGODB_URL environment variable is not set!")
        return

    # FIX: increased timeouts for stable polling on Railway
    request = HTTPXRequest(
        connect_timeout=60,
        read_timeout=60,
        write_timeout=60,
        pool_timeout=60,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

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
                CallbackQueryHandler(cb_service_accounts, pattern=r"^service_[\w-]+$"),
                CallbackQueryHandler(cb_buy_account, pattern=r"^buy_[a-f0-9]+$"),
                CallbackQueryHandler(cb_my_balance,        pattern="^my_balance$"),
                CallbackQueryHandler(cb_recharge_menu,     pattern="^recharge_menu$"),
                CallbackQueryHandler(cb_syriatel,          pattern="^rc_syriatel$"),
                CallbackQueryHandler(cb_shamcash,          pattern="^rc_shamcash$"),
                CallbackQueryHandler(cb_coinex,            pattern="^rc_coinex$"),
                CallbackQueryHandler(cb_binance,           pattern="^rc_binance$"),
                CallbackQueryHandler(cb_usdt,              pattern="^rc_usdt$"),
                CallbackQueryHandler(cb_admin_broadcast,   pattern="^admin_broadcast$"),
                CallbackQueryHandler(cb_admin_rate,        pattern="^admin_rate$"),
                CallbackQueryHandler(cb_admin_add_balance, pattern="^admin_add_balance$"),
                CallbackQueryHandler(cb_admin_add_service, pattern="^admin_add_service$"),
                CallbackQueryHandler(cb_admin_delete_service, pattern="^admin_delete_service$"),
                CallbackQueryHandler(cb_admin_add_account, pattern="^admin_add_account$"),
                CallbackQueryHandler(cb_admin_purchases, pattern="^admin_purchases$"),
                CallbackQueryHandler(cb_admin_stats, pattern="^admin_stats$"),
                CallbackQueryHandler(cb_admin_home, pattern="^admin_home$"),
            ],
            SYRIATEL_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, syriatel_amount)],
            SYRIATEL_TX:       [MessageHandler(filters.TEXT & ~filters.COMMAND, syriatel_tx)],
            SHAMCASH_CURRENCY: [CallbackQueryHandler(cb_sham_currency, pattern="^sham_(usd|syp)$")],
            SHAMCASH_AMOUNT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, shamcash_amount)],
            SHAMCASH_TX: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, shamcash_tx),
                MessageHandler(filters.PHOTO, shamcash_tx),
            ],
            COINEX_AMOUNT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, coinex_amount)],
            COINEX_SCREENSHOT: [MessageHandler(filters.PHOTO, coinex_screenshot)],
            BINANCE_AMOUNT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, binance_amount)],
            BINANCE_SCREENSHOT:[MessageHandler(filters.PHOTO, binance_screenshot)],
            USDT_CHAIN:        [CallbackQueryHandler(cb_usdt_chain, pattern="^usdt_(bep20|trc20)$")],
            USDT_AMOUNT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, usdt_amount)],
            USDT_TXID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, usdt_txid),
                MessageHandler(filters.PHOTO, usdt_txid),
            ],
            ADMIN_BROADCAST:   [MessageHandler(filters.ALL & ~filters.COMMAND, admin_broadcast_msg)],
            ADMIN_CHANGE_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_change_rate)],
            ADMIN_ADD_ID:      [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_id)],
            ADMIN_ADD_AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_amount)],
            SERVICES_MENU: [
                CallbackQueryHandler(cb_service_accounts, pattern=r"^service_[\w-]+$"),
            ],
            SERVICE_ACCOUNTS: [
                CallbackQueryHandler(cb_buy_account, pattern=r"^buy_[a-f0-9]+$"),
            ],
            ACCOUNT_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_account_info),
            ],
            ACCOUNT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_account_price),
            ],
            ADMIN_ADD_SERVICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_service),
            ],
            ADMIN_DELETE_SERVICE: [
                CallbackQueryHandler(cb_delete_service, pattern=r"^delete_service_[\w-]+$"),
            ],
            ADMIN_ADD_ACCOUNT_SERVICE: [
                CallbackQueryHandler(cb_add_account_service, pattern=r"^add_account_service_[\w-]+$"),
            ],
            ADMIN_ADD_ACCOUNT_INFO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_account_info),
            ],
            ADMIN_ADD_ACCOUNT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_account_price),
            ],
            ADMIN_PURCHASES: [
                CallbackQueryHandler(cb_admin_home, pattern="^admin_home$"),
            ],
            ADMIN_STATS: [
                CallbackQueryHandler(cb_admin_home, pattern="^admin_home$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("start",  cmd_start),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(
        CallbackQueryHandler(cb_approve_reject, pattern=r"^(approve|reject)_\d+$")
    )

    logger.info("🚀 PRO DIGITAL STORE bot is running (MongoDB edition)...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            break
        except Exception as e:
            logger.error("Bot crashed: %s — restarting in 5 seconds...", e)
            time.sleep(5)
