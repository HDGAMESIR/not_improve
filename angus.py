# -*- coding: utf-8 -*-
"""
ربات مدیریت فروش وی‌پی‌ان (تلگرام) - نسخه ۵
------------------------------------------------
نصب پیش‌نیاز:
    pip install "python-telegram-bot[job-queue]==21.6"
توی requirements.txt هاست بات هم همین خط رو بذار.

قبل از اجرا این‌ها رو پر کن: BOT_TOKEN, ADMIN_IDS, WALLET_ADDRESS
"""

import sqlite3
import logging
import asyncio
import socket
import time
import base64
import json
import re
from urllib.parse import urlparse
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# ==================== تنظیمات ====================
BOT_TOKEN = ""
ADMIN_IDS = []
WALLET_ADDRESS = "TXVMHiECmNZrXJbCtUL7VvN4rg23aHV9xk"
WALLET_NETWORK = "USDT (TRC20)"
DB_PATH = "vpnshop.db"
REMINDER_DAYS_BEFORE = 3
REMINDER_HOUR_UTC = 8
PING_CANDIDATES_LIMIT = 8
PING_TIMEOUT_SECONDS = 2.5
HEALTH_CHECK_INTERVAL_SECONDS = 6 * 3600  # هر ۶ ساعت چک خودکار سلامت کانفیگ‌ها

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# States مکالمه
(
    ADD_NAME, ADD_PRICE, ADD_DURATION, ADD_DATA_LIMIT, ADD_DESC,
    WAIT_TXID,
    ADD_CONFIG_TYPE, ADD_CONFIG_TEXT,
    EDIT_PLAN_VALUE,
    EDIT_CONFIG_TEXT,
    MANUAL_SUB_USERID,
    BROADCAST_TEXT,
    SET_SUPPORT_TEXT,
    SET_WELCOME_TEXT,
    SET_RULES_TEXT,
) = range(15)


# ==================== دیتابیس ====================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, price REAL, duration_days INTEGER, description TEXT,
        data_limit TEXT DEFAULT ''
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, username TEXT, plan_id INTEGER,
        status TEXT DEFAULT 'pending', txid TEXT,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, username TEXT, plan_id INTEGER,
        start_date TEXT, expire_date TEXT,
        active INTEGER DEFAULT 1, reminded INTEGER DEFAULT 0,
        pending_config TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS customers(
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS configs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id INTEGER, config_text TEXT,
        type TEXT DEFAULT 'normal',
        used INTEGER DEFAULT 0, order_id INTEGER,
        healthy INTEGER DEFAULT 1, last_ping_ms REAL, last_checked TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS config_recipients(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        config_id INTEGER, user_id INTEGER, subscription_id INTEGER, sent_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY, value TEXT
    )""")
    conn.commit()
    conn.close()
    migrate_db()


def migrate_db():
    conn = db()
    cols_cfg = [r["name"] for r in conn.execute("PRAGMA table_info(configs)").fetchall()]
    if "type" not in cols_cfg:
        conn.execute("ALTER TABLE configs ADD COLUMN type TEXT DEFAULT 'normal'")
    if "healthy" not in cols_cfg:
        conn.execute("ALTER TABLE configs ADD COLUMN healthy INTEGER DEFAULT 1")
    if "last_ping_ms" not in cols_cfg:
        conn.execute("ALTER TABLE configs ADD COLUMN last_ping_ms REAL")
    if "last_checked" not in cols_cfg:
        conn.execute("ALTER TABLE configs ADD COLUMN last_checked TEXT")
    cols_sub = [r["name"] for r in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
    if "pending_config" not in cols_sub:
        conn.execute("ALTER TABLE subscriptions ADD COLUMN pending_config TEXT")
    cols_plan = [r["name"] for r in conn.execute("PRAGMA table_info(plans)").fetchall()]
    if "data_limit" not in cols_plan:
        conn.execute("ALTER TABLE plans ADD COLUMN data_limit TEXT DEFAULT ''")
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def upsert_customer(user):
    conn = db()
    conn.execute(
        "INSERT INTO customers(user_id, username, first_name, joined_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
        (user.id, user.username or "", user.first_name or "", datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ==================== تنظیمات (کلید-مقدار) ====================
def get_setting(key: str, default: str = None) -> str:
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = db()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


DEFAULT_WELCOME = "به فروشگاه وی‌پی‌ان خوش اومدی! 👋\nامیدواریم تجربه خوبی داشته باشی."
DEFAULT_RULES = "قانون خاصی هنوز تنظیم نشده. ادمین می‌تونه از پنل، متن قوانین رو تنظیم کنه."


# ==================== تست پینگ (سرعت اتصال TCP) ====================
def extract_host_port(config_text: str):
    text = config_text.strip()
    try:
        if text.startswith("vmess://"):
            raw = text[len("vmess://"):]
            padded = raw + "=" * (-len(raw) % 4)
            data = json.loads(base64.b64decode(padded).decode("utf-8", errors="ignore"))
            host = data.get("add")
            port = int(data.get("port"))
            if host and port:
                return host, port
        if text.startswith(("vless://", "trojan://", "ss://", "ssr://")):
            parsed = urlparse(text)
            if parsed.hostname and parsed.port:
                return parsed.hostname, parsed.port
        m = re.search(r"([a-zA-Z0-9\.\-]+):(\d{2,5})", text)
        if m:
            return m.group(1), int(m.group(2))
    except Exception:
        pass
    return None, None


def _tcp_latency_ms(host: str, port: int, timeout: float = PING_TIMEOUT_SECONDS):
    try:
        start = time.monotonic()
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return (time.monotonic() - start) * 1000
    except Exception:
        return None


async def test_configs_status(configs):
    """لیست (config, ping_ms یا None) برمی‌گردونه، بدون مرتب‌سازی."""
    loop = asyncio.get_event_loop()
    tasks = []
    for c in configs:
        host, port = extract_host_port(c["config_text"])
        if host and port:
            tasks.append(loop.run_in_executor(None, _tcp_latency_ms, host, port))
        else:
            async def _none():
                return None
            tasks.append(_none())
    results = await asyncio.gather(*tasks)
    return list(zip(configs, results))


async def rank_configs_by_ping(configs):
    pairs = await test_configs_status(configs)
    pairs.sort(key=lambda x: (x[1] is None, x[1] if x[1] is not None else 0))
    return [c for c, _ in pairs]


# ==================== اختصاص و ارسال کانفیگ ====================
async def pop_free_config(plan_id: int):
    type_priority = get_setting("type_priority", "normal_first")
    ping_enabled = get_setting("ping_enabled", "1") == "1"
    order = ["normal", "multi"] if type_priority == "normal_first" else ["multi", "normal"]

    for t in order:
        conn = db()
        base_where = "plan_id=? AND type=? " + ("AND used=0 " if t == "normal" else "")
        healthy_rows = conn.execute(
            f"SELECT * FROM configs WHERE {base_where} AND (healthy IS NULL OR healthy=1) LIMIT ?",
            (plan_id, t, PING_CANDIDATES_LIMIT),
        ).fetchall()
        candidates = healthy_rows
        if not candidates:
            candidates = conn.execute(
                f"SELECT * FROM configs WHERE {base_where} LIMIT ?", (plan_id, t, PING_CANDIDATES_LIMIT)
            ).fetchall()
        conn.close()
        if not candidates:
            continue

        chosen = candidates[0]
        if ping_enabled and len(candidates) > 1:
            ranked = await rank_configs_by_ping(candidates)
            chosen = ranked[0]

        if t == "normal":
            conn = db()
            conn.execute("UPDATE configs SET used=1 WHERE id=?", (chosen["id"],))
            conn.commit()
            conn.close()
        return chosen
    return None


def record_recipient(config_id: int, user_id: int, subscription_id: int):
    conn = db()
    conn.execute(
        "INSERT INTO config_recipients(config_id, user_id, subscription_id, sent_at) VALUES(?,?,?,?)",
        (config_id, user_id, subscription_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


async def create_subscription_and_deliver(context, user_id, username, plan, order_id=None):
    start_date = datetime.utcnow()
    expire_date = start_date + timedelta(days=plan["duration_days"])
    conn = db()
    cur = conn.execute(
        "INSERT INTO subscriptions(user_id, username, plan_id, start_date, expire_date, active) VALUES(?,?,?,?,?,1)",
        (user_id, username, plan["id"], start_date.isoformat(), expire_date.isoformat()),
    )
    sub_id = cur.lastrowid
    conn.commit()
    conn.close()

    cfg = await pop_free_config(plan["id"])
    if cfg:
        record_recipient(cfg["id"], user_id, sub_id)

    data_limit_text = plan["data_limit"] or "نامحدود"
    base_msg = (
        f"اشتراک شما فعال شد. 🎉\nپلن: {plan['name']}\n"
        f"حجم: {data_limit_text}\n"
        f"تاریخ انقضا: {expire_date.strftime('%Y-%m-%d')}\n"
    )
    full_msg = base_msg + (
        f"\n📡 اطلاعات اتصال شما:\n`{cfg['config_text']}`" if cfg else "\nکانفیگ شما به‌محض موجود شدن ارسال میشه."
    )

    delivered = False
    try:
        await context.bot.send_message(user_id, full_msg, parse_mode="Markdown")
        delivered = True
    except Exception as e:
        logger.warning(f"ارسال به کاربر {user_id} ناموفق (احتمالاً استارت نزده): {e}")

    if not delivered and cfg:
        conn = db()
        conn.execute("UPDATE subscriptions SET pending_config=? WHERE id=?", (cfg["config_text"], sub_id))
        conn.commit()
        conn.close()

    if not cfg:
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id, f"⚠️ موجودی کانفیگ پلن «{plan['name']}» تموم شده. لطفاً کانفیگ اضافه کن."
                )
            except Exception:
                pass

    return delivered, cfg


async def deliver_pending_configs(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    subs = conn.execute(
        "SELECT * FROM subscriptions WHERE user_id=? AND pending_config IS NOT NULL", (user_id,)
    ).fetchall()
    for s in subs:
        try:
            await context.bot.send_message(
                user_id,
                f"📡 این کانفیگ برای اشتراکی که برات فعال شده بود آماده‌ست:\n`{s['pending_config']}`",
                parse_mode="Markdown",
            )
            conn.execute("UPDATE subscriptions SET pending_config=NULL WHERE id=?", (s["id"],))
        except Exception:
            pass
    conn.commit()
    conn.close()


# ==================== منوها ====================
def main_menu_kb(user_id: int):
    rows = [
        [InlineKeyboardButton("📦 پلن‌ها", callback_data="plans")],
        [InlineKeyboardButton("🧾 سفارش‌های من", callback_data="my_orders")],
        [InlineKeyboardButton("👤 حساب من", callback_data="my_account")],
        [InlineKeyboardButton("📜 قوانین", callback_data="show_rules"),
         InlineKeyboardButton("💬 پشتیبانی", callback_data="show_support")],
    ]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton("🛠 پنل ادمین", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)


def admin_menu_kb():
    rows = [
        [InlineKeyboardButton("➕ افزودن پلن", callback_data="admin_add_plan")],
        [InlineKeyboardButton("✏️ ویرایش/حذف پلن‌ها", callback_data="admin_edit_plans")],
        [InlineKeyboardButton("📦 افزودن کانفیگ به پلن", callback_data="admin_add_config")],
        [InlineKeyboardButton("🩺 وضعیت و پینگ کانفیگ‌ها", callback_data="admin_config_health")],
        [InlineKeyboardButton("🗑 حذف کانفیگ چندمنظوره", callback_data="admin_del_multi")],
        [InlineKeyboardButton("📊 موجودی کانفیگ‌ها", callback_data="admin_config_stock")],
        [InlineKeyboardButton("⚙️ تنظیمات اولویت ارسال", callback_data="admin_settings")],
        [InlineKeyboardButton("🆘 تنظیم آیدی پشتیبانی", callback_data="admin_set_support")],
        [InlineKeyboardButton("✏️ متن خوش‌آمدگویی", callback_data="admin_set_welcome")],
        [InlineKeyboardButton("✏️ متن قوانین", callback_data="admin_set_rules")],
        [InlineKeyboardButton("⏳ سفارش‌های در انتظار", callback_data="admin_pending")],
        [InlineKeyboardButton("👥 لیست مشترکین", callback_data="admin_customers")],
        [InlineKeyboardButton("🎁 افزودن اشتراک دستی", callback_data="admin_manual_sub")],
        [InlineKeyboardButton("📢 پیام همگانی", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📈 گزارش فروش", callback_data="admin_report")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(rows)


# ==================== شروع / قوانین / پشتیبانی ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    existing = conn.execute("SELECT 1 FROM customers WHERE user_id=?", (update.effective_user.id,)).fetchone()
    conn.close()
    is_new = existing is None

    upsert_customer(update.effective_user)
    await deliver_pending_configs(update.effective_user.id, context)

    if is_new:
        welcome = get_setting("welcome_text") or DEFAULT_WELCOME
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📜 مشاهده قوانین", callback_data="show_rules")]])
        await update.message.reply_text(welcome, reply_markup=kb)

    await update.message.reply_text("از منوی زیر انتخاب کن:", reply_markup=main_menu_kb(update.effective_user.id))


async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rules = get_setting("rules_text") or DEFAULT_RULES
    await q.edit_message_text(
        rules, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])
    )


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    support_id = get_setting("support_id", "")
    if not support_id:
        await q.edit_message_text(
            "آیدی پشتیبانی هنوز تنظیم نشده.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]]),
        )
        return
    uname = support_id.lstrip("@")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 گفتگو با پشتیبانی", url=f"https://t.me/{uname}")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")],
    ])
    await q.edit_message_text("برای ارتباط با پشتیبانی روی دکمه زیر بزن:", reply_markup=kb)


async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("منوی اصلی:", reply_markup=main_menu_kb(update.effective_user.id))


# ==================== نمایش پلن‌ها و خرید ====================
async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    plans = conn.execute("SELECT * FROM plans").fetchall()
    conn.close()
    if not plans:
        await q.edit_message_text(
            "فعلاً پلنی ثبت نشده. بعداً دوباره سر بزن.",
            reply_markup=main_menu_kb(update.effective_user.id),
        )
        return
    rows = []
    for p in plans:
        vol = p["data_limit"] or "نامحدود"
        label = f"{p['name']} - {p['price']}$ - {p['duration_days']} روزه - {vol}"
        rows.append([InlineKeyboardButton(label, callback_data=f"buy_{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")])
    await q.edit_message_text("پلن‌های موجود:", reply_markup=InlineKeyboardMarkup(rows))


async def buy_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_id = int(q.data.split("_")[1])
    conn = db()
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    if not plan:
        await q.edit_message_text("این پلن دیگه موجود نیست.")
        return ConversationHandler.END

    context.user_data["pending_plan_id"] = plan_id
    vol = plan["data_limit"] or "نامحدود"
    text = (
        f"پلن انتخابی: {plan['name']}\n"
        f"قیمت: {plan['price']}$\n"
        f"مدت: {plan['duration_days']} روز\n"
        f"حجم: {vol}\n\n"
        f"مبلغ رو به این آدرس واریز کن:\n`{WALLET_ADDRESS}`\n"
        f"شبکه: {WALLET_NETWORK}\n\n"
        f"بعد از واریز، هش تراکنش (TXID) رو همینجا برام بفرست."
    )
    await q.edit_message_text(text, parse_mode="Markdown")
    return WAIT_TXID


async def receive_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan_id = context.user_data.get("pending_plan_id")
    if not plan_id:
        await update.message.reply_text("ابتدا از منو یک پلن انتخاب کن. /start")
        return ConversationHandler.END

    txid = update.message.text.strip()
    user = update.effective_user
    conn = db()
    cur = conn.execute(
        "INSERT INTO orders(user_id, username, plan_id, status, txid, created_at) VALUES(?,?,?,?,?,?)",
        (user.id, user.username or "", plan_id, "pending", txid, datetime.utcnow().isoformat()),
    )
    order_id = cur.lastrowid
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "سفارش شما ثبت شد و برای تأیید نزد ادمین ارسال شد. ✅\nبعد از تأیید، کانفیگت خودکار برات ارسال میشه."
    )

    admin_text = (
        f"🆕 سفارش جدید #{order_id}\n"
        f"کاربر: @{user.username or user.id} (ID: {user.id})\n"
        f"پلن: {plan['name']} ({plan['price']}$، {plan['duration_days']} روز)\n"
        f"TXID: `{txid}`"
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ تأیید", callback_data=f"confirm_{order_id}"),
          InlineKeyboardButton("❌ رد", callback_data=f"reject_{order_id}")]]
    )
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, admin_text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"ارسال به ادمین {admin_id} ناموفق: {e}")

    context.user_data.pop("pending_plan_id", None)
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END


# ==================== تأیید/رد سفارش ====================
async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("دسترسی نداری.", show_alert=True)
        return
    order_id = int(q.data.split("_")[1])
    conn = db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "pending":
        await q.edit_message_text("این سفارش قبلاً پردازش شده.")
        conn.close()
        return
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (order["plan_id"],)).fetchone()
    conn.execute("UPDATE orders SET status='paid' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()

    await q.edit_message_text(f"سفارش #{order_id} تأیید شد. ✅ (در حال یافتن بهترین کانفیگ...)")
    await create_subscription_and_deliver(context, order["user_id"], order["username"], plan, order_id=order_id)


async def reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("دسترسی نداری.", show_alert=True)
        return
    order_id = int(q.data.split("_")[1])
    conn = db()
    order = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "pending":
        await q.edit_message_text("این سفارش قبلاً پردازش شده.")
        conn.close()
        return
    conn.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()

    await q.edit_message_text(f"سفارش #{order_id} رد شد. ❌")
    try:
        await context.bot.send_message(order["user_id"], "متاسفانه پرداخت شما تأیید نشد. با پشتیبانی تماس بگیر.")
    except Exception as e:
        logger.warning(f"ارسال پیام به کاربر ناموفق: {e}")


# ==================== سفارش‌ها و حساب کاربر ====================
async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    orders = conn.execute(
        "SELECT o.*, p.name as plan_name FROM orders o LEFT JOIN plans p ON o.plan_id=p.id "
        "WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()
    if not orders:
        text = "هنوز سفارشی ثبت نکردی."
    else:
        status_fa = {"pending": "در انتظار", "paid": "تأیید شده", "rejected": "رد شده"}
        lines = [f"#{o['id']} - {o['plan_name']} - {status_fa.get(o['status'], o['status'])}" for o in orders]
        text = "سفارش‌های اخیر شما:\n" + "\n".join(lines)
    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])
    )


async def my_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    subs = conn.execute(
        "SELECT s.*, p.name as plan_name, p.data_limit as data_limit FROM subscriptions s "
        "LEFT JOIN plans p ON s.plan_id=p.id WHERE s.user_id=? AND s.active=1 ORDER BY s.expire_date DESC",
        (update.effective_user.id,),
    ).fetchall()
    conn.close()
    if not subs:
        text = "اشتراک فعالی نداری."
    else:
        lines = []
        for s in subs:
            exp = datetime.fromisoformat(s["expire_date"])
            days_left = (exp - datetime.utcnow()).days
            vol = s["data_limit"] or "نامحدود"
            lines.append(f"{s['plan_name']} - حجم: {vol} - انقضا: {exp.strftime('%Y-%m-%d')} ({days_left} روز مانده)")
        text = "اشتراک‌های فعال شما:\n" + "\n".join(lines)
    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="back_main")]])
    )


# ==================== پنل ادمین: اصلی ====================
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        await q.answer("دسترسی نداری.", show_alert=True)
        return
    await q.edit_message_text("پنل ادمین:", reply_markup=admin_menu_kb())


async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    orders = conn.execute(
        "SELECT o.*, p.name as plan_name FROM orders o LEFT JOIN plans p ON o.plan_id=p.id "
        "WHERE o.status='pending' ORDER BY o.id"
    ).fetchall()
    conn.close()
    if not orders:
        await q.edit_message_text(
            "سفارش در انتظاری نیست.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]]),
        )
        return
    for o in orders:
        text = f"#{o['id']} @{o['username'] or o['user_id']} - {o['plan_name']} - TXID: {o['txid']}"
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ تأیید", callback_data=f"confirm_{o['id']}"),
              InlineKeyboardButton("❌ رد", callback_data=f"reject_{o['id']}")]]
        )
        await context.bot.send_message(update.effective_user.id, text, reply_markup=kb)
    await q.edit_message_text("لیست بالا ⬆️", reply_markup=admin_menu_kb())


async def admin_customers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    subs = conn.execute(
        "SELECT s.*, p.name as plan_name FROM subscriptions s LEFT JOIN plans p ON s.plan_id=p.id "
        "WHERE s.active=1 ORDER BY s.expire_date"
    ).fetchall()
    conn.close()
    text = "مشترک فعالی نیست." if not subs else "\n".join(
        f"@{s['username'] or s['user_id']} - {s['plan_name']} - تا "
        f"{datetime.fromisoformat(s['expire_date']).strftime('%Y-%m-%d')}"
        for s in subs
    )
    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]])
    )


async def admin_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) as cnt, COALESCE(SUM(p.price),0) as revenue "
        "FROM orders o JOIN plans p ON o.plan_id=p.id WHERE o.status='paid'"
    ).fetchone()
    pending_cnt = conn.execute("SELECT COUNT(*) as c FROM orders WHERE status='pending'").fetchone()["c"]
    active_subs = conn.execute("SELECT COUNT(*) as c FROM subscriptions WHERE active=1").fetchone()["c"]
    total_customers = conn.execute("SELECT COUNT(*) as c FROM customers").fetchone()["c"]
    conn.close()
    text = (
        f"📈 گزارش فروش\n\n"
        f"سفارش‌های تأیید شده: {row['cnt']}\n"
        f"درآمد کل: {row['revenue']}$\n"
        f"سفارش‌های در انتظار: {pending_cnt}\n"
        f"مشترکین فعال: {active_subs}\n"
        f"کل کاربران ثبت‌نامی: {total_customers}"
    )
    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]])
    )


# ==================== پنل ادمین: تنظیمات اولویت ارسال ====================
async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    type_priority = get_setting("type_priority", "normal_first")
    ping_enabled = get_setting("ping_enabled", "1") == "1"

    type_label = "🔒 عادی → سپس ♾ چندمنظوره" if type_priority == "normal_first" else "♾ چندمنظوره → سپس 🔒 عادی"
    ping_label = "روشن ✅" if ping_enabled else "خاموش ❌"

    text = (
        f"⚙️ تنظیمات اولویت ارسال کانفیگ\n\n"
        f"اولویت نوع: {type_label}\n"
        f"تست پینگ (تأخیر اتصال): {ping_label}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 تغییر اولویت نوع", callback_data="setting_toggle_type")],
        [InlineKeyboardButton(f"تست پینگ رو {'خاموش' if ping_enabled else 'روشن'} کن", callback_data="setting_toggle_ping")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")],
    ])
    await q.edit_message_text(text, reply_markup=kb)


async def setting_toggle_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = get_setting("type_priority", "normal_first")
    set_setting("type_priority", "multi_first" if current == "normal_first" else "normal_first")
    await admin_settings(update, context)


async def setting_toggle_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current = get_setting("ping_enabled", "1") == "1"
    set_setting("ping_enabled", "0" if current else "1")
    await admin_settings(update, context)


# ==================== پنل ادمین: تنظیم پشتیبانی / خوش‌آمدگویی / قوانین ====================
async def admin_set_support_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("یوزرنیم پشتیبانی رو بفرست (مثلاً @mysupport):")
    return SET_SUPPORT_TEXT


async def admin_set_support_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting("support_id", update.message.text.strip())
    await update.message.reply_text("آیدی پشتیبانی ثبت شد. ✅", reply_markup=admin_menu_kb())
    return ConversationHandler.END


async def admin_set_welcome_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("متن خوش‌آمدگویی جدید رو بفرست (فقط بار اول استارت‌زدن کاربر نمایش داده میشه):")
    return SET_WELCOME_TEXT


async def admin_set_welcome_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting("welcome_text", update.message.text)
    await update.message.reply_text("متن خوش‌آمدگویی ثبت شد. ✅", reply_markup=admin_menu_kb())
    return ConversationHandler.END


async def admin_set_rules_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("متن قوانین جدید رو بفرست:")
    return SET_RULES_TEXT


async def admin_set_rules_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_setting("rules_text", update.message.text)
    await update.message.reply_text("متن قوانین ثبت شد. ✅", reply_markup=admin_menu_kb())
    return ConversationHandler.END


# ==================== پنل ادمین: افزودن پلن ====================
async def add_plan_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await q.edit_message_text("نام پلن رو بفرست (مثلاً: پلن یک ماهه):")
    return ADD_NAME


async def add_plan_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_plan_name"] = update.message.text.strip()
    await update.message.reply_text("قیمت به دلار رو بفرست (فقط عدد):")
    return ADD_PRICE


async def add_plan_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["new_plan_price"] = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("عدد معتبر بفرست:")
        return ADD_PRICE
    await update.message.reply_text("مدت اعتبار به روز رو بفرست (فقط عدد):")
    return ADD_DURATION


async def add_plan_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["new_plan_duration"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("عدد صحیح بفرست:")
        return ADD_DURATION
    await update.message.reply_text("حجم پلن رو بفرست (مثلاً 50GB یا بنویس نامحدود):")
    return ADD_DATA_LIMIT


async def add_plan_data_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_plan_data_limit"] = update.message.text.strip()
    await update.message.reply_text("توضیحات پلن رو بفرست (یا بنویس -):")
    return ADD_DESC


async def add_plan_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text.strip()
    desc = "" if desc == "-" else desc
    conn = db()
    conn.execute(
        "INSERT INTO plans(name, price, duration_days, data_limit, description) VALUES(?,?,?,?,?)",
        (context.user_data["new_plan_name"], context.user_data["new_plan_price"],
         context.user_data["new_plan_duration"], context.user_data["new_plan_data_limit"], desc),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("پلن با موفقیت اضافه شد. ✅", reply_markup=admin_menu_kb())
    context.user_data.clear()
    return ConversationHandler.END


# ==================== پنل ادمین: ویرایش/حذف پلن ====================
async def admin_edit_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    plans = conn.execute("SELECT * FROM plans").fetchall()
    conn.close()
    if not plans:
        await q.edit_message_text(
            "پلنی ثبت نشده.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]]),
        )
        return
    rows = []
    for p in plans:
        rows.append([
            InlineKeyboardButton(f"✏️ {p['name']}", callback_data=f"editplan_{p['id']}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"delplan_{p['id']}"),
        ])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")])
    await q.edit_message_text("پلن مورد نظر رو انتخاب کن:", reply_markup=InlineKeyboardMarkup(rows))


async def edit_plan_pick_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_id = int(q.data.split("_")[1])
    context.user_data["edit_plan_id"] = plan_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("نام", callback_data="editfield_name"),
         InlineKeyboardButton("قیمت", callback_data="editfield_price")],
        [InlineKeyboardButton("مدت (روز)", callback_data="editfield_duration"),
         InlineKeyboardButton("حجم", callback_data="editfield_datalimit")],
        [InlineKeyboardButton("توضیحات", callback_data="editfield_description")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_edit_plans")],
    ])
    await q.edit_message_text("کدوم فیلد رو می‌خوای ویرایش کنی؟", reply_markup=kb)


async def edit_plan_ask_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    field = q.data.split("_")[1]
    context.user_data["edit_field"] = field
    prompts = {
        "name": "نام جدید رو بفرست:",
        "price": "قیمت جدید رو بفرست (فقط عدد):",
        "duration": "مدت جدید رو به روز بفرست (فقط عدد):",
        "datalimit": "حجم جدید رو بفرست (مثلاً 50GB یا بنویس نامحدود):",
        "description": "توضیحات جدید رو بفرست:",
    }
    await q.edit_message_text(prompts[field])
    return EDIT_PLAN_VALUE


async def edit_plan_save_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data.get("edit_field")
    plan_id = context.user_data.get("edit_plan_id")
    value = update.message.text.strip()
    column_map = {
        "name": "name", "price": "price", "duration": "duration_days",
        "datalimit": "data_limit", "description": "description",
    }
    column = column_map.get(field)
    if column == "price":
        try:
            value = float(value)
        except ValueError:
            await update.message.reply_text("عدد معتبر بفرست:")
            return EDIT_PLAN_VALUE
    if column == "duration_days":
        try:
            value = int(value)
        except ValueError:
            await update.message.reply_text("عدد صحیح بفرست:")
            return EDIT_PLAN_VALUE
    conn = db()
    conn.execute(f"UPDATE plans SET {column}=? WHERE id=?", (value, plan_id))
    conn.commit()
    conn.close()
    await update.message.reply_text("پلن به‌روزرسانی شد. ✅", reply_markup=admin_menu_kb())
    context.user_data.pop("edit_field", None)
    context.user_data.pop("edit_plan_id", None)
    return ConversationHandler.END


async def delete_plan_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_id = int(q.data.split("_")[1])
    conn = db()
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    if not plan:
        await q.edit_message_text("پلن پیدا نشد.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"delplanyes_{plan_id}"),
         InlineKeyboardButton("❌ انصراف", callback_data="admin_edit_plans")],
    ])
    await q.edit_message_text(f"مطمئنی می‌خوای پلن «{plan['name']}» رو حذف کنی؟", reply_markup=kb)


async def delete_plan_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_id = int(q.data.split("_")[1])
    conn = db()
    conn.execute("DELETE FROM plans WHERE id=?", (plan_id,))
    conn.execute("DELETE FROM configs WHERE plan_id=? AND type='normal' AND used=0", (plan_id,))
    conn.commit()
    conn.close()
    await q.edit_message_text("پلن حذف شد. ✅", reply_markup=admin_menu_kb())


# ==================== پنل ادمین: افزودن کانفیگ (عادی/چندمنظوره) ====================
async def add_config_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    plans = conn.execute("SELECT * FROM plans").fetchall()
    conn.close()
    if not plans:
        await q.edit_message_text(
            "اول باید یه پلن بسازی.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]]),
        )
        return
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"cfgplan_{p['id']}")] for p in plans]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")])
    await q.edit_message_text("کانفیگ‌ها برای کدوم پلن هستن؟", reply_markup=InlineKeyboardMarkup(rows))


async def add_config_pick_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_id = int(q.data.split("_")[1])
    context.user_data["cfg_plan_id"] = plan_id
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔒 عادی (یک‌بارمصرف)", callback_data="cfgtype_normal")],
        [InlineKeyboardButton("♾ چندمنظوره (نامحدود)", callback_data="cfgtype_multi")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")],
    ])
    await q.edit_message_text(
        "نوع کانفیگ رو انتخاب کن:\n\n"
        "🔒 عادی: هر خط فقط یک‌بار برای یک نفر استفاده میشه و بعدش موجودیش تموم میشه.\n"
        "♾ چندمنظوره: هیچ‌وقت تموم نمیشه، برای هزاران نفر هم قابل ارساله، فقط خودت می‌تونی دستی حذفش کنی.",
        reply_markup=kb,
    )
    return ADD_CONFIG_TYPE


async def add_config_pick_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg_type = q.data.split("_")[1]
    context.user_data["cfg_type"] = cfg_type
    await q.edit_message_text(
        "کانفیگ‌ها رو بفرست، هر کدوم توی یه خط جدا (می‌تونی چندتا با هم بفرستی، هر خط = یک کانفیگ):"
    )
    return ADD_CONFIG_TEXT


async def add_config_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan_id = context.user_data.get("cfg_plan_id")
    cfg_type = context.user_data.get("cfg_type", "normal")
    if not plan_id:
        await update.message.reply_text("خطا: پلن انتخاب نشده. دوباره از منو شروع کن.")
        return ConversationHandler.END
    lines = [l.strip() for l in update.message.text.splitlines() if l.strip()]
    conn = db()
    for line in lines:
        conn.execute(
            "INSERT INTO configs(plan_id, config_text, type, used, healthy) VALUES(?,?,?,0,1)",
            (plan_id, line, cfg_type),
        )
    conn.commit()
    conn.close()
    type_fa = "عادی" if cfg_type == "normal" else "چندمنظوره"
    await update.message.reply_text(f"{len(lines)} کانفیگ ({type_fa}) اضافه شد. ✅", reply_markup=admin_menu_kb())
    context.user_data.pop("cfg_plan_id", None)
    context.user_data.pop("cfg_type", None)
    return ConversationHandler.END


async def admin_config_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    rows = conn.execute(
        "SELECT p.name as name, "
        "SUM(CASE WHEN c.type='normal' AND c.used=0 THEN 1 ELSE 0 END) as free_normal, "
        "SUM(CASE WHEN c.type='normal' AND c.used=1 THEN 1 ELSE 0 END) as used_normal, "
        "SUM(CASE WHEN c.type='multi' THEN 1 ELSE 0 END) as multi_count "
        "FROM plans p LEFT JOIN configs c ON c.plan_id=p.id GROUP BY p.id"
    ).fetchall()
    conn.close()
    if not rows:
        text = "پلنی ثبت نشده."
    else:
        lines = []
        for r in rows:
            lines.append(
                f"📦 {r['name']}\n"
                f"  🔒 عادی: {r['free_normal'] or 0} آزاد / {r['used_normal'] or 0} استفاده‌شده\n"
                f"  ♾ چندمنظوره: {r['multi_count'] or 0} عدد"
            )
        text = "📊 موجودی کانفیگ‌ها:\n\n" + "\n\n".join(lines)
    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]])
    )


# ==================== پنل ادمین: وضعیت و پینگ کانفیگ‌ها + ویرایش ====================
async def admin_config_health_pick_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    plans = conn.execute("SELECT * FROM plans").fetchall()
    conn.close()
    if not plans:
        await q.edit_message_text(
            "پلنی ثبت نشده.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]]),
        )
        return
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"healthplan_{p['id']}")] for p in plans]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")])
    await q.edit_message_text("وضعیت کانفیگ‌های کدوم پلن رو ببینم؟", reply_markup=InlineKeyboardMarkup(rows))


async def admin_config_health_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_id = int(q.data.split("_")[1])
    await q.edit_message_text("در حال تست پینگ همه‌ی کانفیگ‌های این پلن... ⏳")

    conn = db()
    configs = conn.execute("SELECT * FROM configs WHERE plan_id=?", (plan_id,)).fetchall()
    conn.close()
    if not configs:
        await context.bot.send_message(
            update.effective_user.id, "کانفیگی برای این پلن ثبت نشده.", reply_markup=admin_menu_kb()
        )
        return

    results = await test_configs_status(configs)

    conn = db()
    for c, ping in results:
        healthy = 1 if ping is not None else 0
        conn.execute(
            "UPDATE configs SET healthy=?, last_ping_ms=?, last_checked=? WHERE id=?",
            (healthy, ping, datetime.utcnow().isoformat(), c["id"]),
        )
    conn.commit()
    conn.close()

    for c, ping in results:
        type_fa = "🔒 عادی" if c["type"] == "normal" else "♾ چندمنظوره"
        used_fa = " (در حال استفاده)" if c["type"] == "normal" and c["used"] else ""
        status = f"✅ سالم — {int(ping)}ms" if ping is not None else "❌ پینگ نداره، باید عوض بشه"
        label = c["config_text"][:30] + ("…" if len(c["config_text"]) > 30 else "")
        text = f"{type_fa}{used_fa}\n{label}\n\nوضعیت: {status}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ ویرایش این کانفیگ", callback_data=f"editcfg_{c['id']}")]])
        await context.bot.send_message(update.effective_user.id, text, reply_markup=kb)

    await context.bot.send_message(update.effective_user.id, "پایان لیست ⬆️", reply_markup=admin_menu_kb())


async def edit_config_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg_id = int(q.data.split("_")[1])
    context.user_data["edit_cfg_id"] = cfg_id
    await q.edit_message_text(
        "متن جدید این کانفیگ رو بفرست.\n"
        "توجه: به همه کسایی که این کانفیگ رو قبلاً گرفتن خودکار اطلاع‌رسانی میشه."
    )
    return EDIT_CONFIG_TEXT


async def edit_config_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg_id = context.user_data.get("edit_cfg_id")
    new_text = update.message.text.strip()

    conn = db()
    conn.execute(
        "UPDATE configs SET config_text=?, healthy=1, last_ping_ms=NULL, last_checked=? WHERE id=?",
        (new_text, datetime.utcnow().isoformat(), cfg_id),
    )
    recipients = conn.execute(
        "SELECT DISTINCT user_id FROM config_recipients WHERE config_id=?", (cfg_id,)
    ).fetchall()
    conn.commit()
    conn.close()

    ok, fail = 0, 0
    for r in recipients:
        try:
            await context.bot.send_message(
                r["user_id"],
                f"🔄 اطلاعات اتصال سرویس شما به‌روزرسانی شد.\n\n📡 اطلاعات جدید:\n`{new_text}`",
                parse_mode="Markdown",
            )
            ok += 1
        except Exception:
            fail += 1

    msg = f"کانفیگ آپدیت شد ✅ و به {ok} کاربر اطلاع داده شد."
    if fail:
        msg += f"\n({fail} نفر چون هنوز /start نزده بودن پیام نگرفتن — به‌محض استارت‌زدن خودکار می‌فرستم.)"
    await update.message.reply_text(msg, reply_markup=admin_menu_kb())
    context.user_data.pop("edit_cfg_id", None)
    return ConversationHandler.END


async def periodic_config_health_check(context: ContextTypes.DEFAULT_TYPE):
    """هر چند ساعت یه‌بار خودکار اجرا میشه: اگه کانفیگی که قبلاً سالم بود بخوابه، به ادمین هشدار میده."""
    conn = db()
    configs = conn.execute("SELECT * FROM configs").fetchall()
    conn.close()
    if not configs:
        return

    results = await test_configs_status(configs)
    conn = db()
    for c, ping in results:
        was_healthy = 1 if c["healthy"] is None else c["healthy"]
        now_healthy = 1 if ping is not None else 0
        conn.execute(
            "UPDATE configs SET healthy=?, last_ping_ms=?, last_checked=? WHERE id=?",
            (now_healthy, ping, datetime.utcnow().isoformat(), c["id"]),
        )
        if was_healthy == 1 and now_healthy == 0:
            label = c["config_text"][:35] + ("…" if len(c["config_text"]) > 35 else "")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ ویرایش", callback_data=f"editcfg_{c['id']}")]])
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"🚨 هشدار: این کانفیگ دیگه پینگ نمی‌ده و باید عوض بشه:\n`{label}`",
                        reply_markup=kb,
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
    conn.commit()
    conn.close()


# ==================== پنل ادمین: حذف کانفیگ چندمنظوره ====================
async def admin_del_multi_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    plans = conn.execute("SELECT * FROM plans").fetchall()
    conn.close()
    if not plans:
        await q.edit_message_text(
            "پلنی ثبت نشده.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]]),
        )
        return
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"delmultiplan_{p['id']}")] for p in plans]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")])
    await q.edit_message_text("کانفیگ چندمنظوره‌ی کدوم پلن رو مدیریت کنیم؟", reply_markup=InlineKeyboardMarkup(rows))


async def admin_del_multi_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_id = int(q.data.split("_")[1])
    conn = db()
    configs = conn.execute("SELECT * FROM configs WHERE plan_id=? AND type='multi'", (plan_id,)).fetchall()
    conn.close()
    if not configs:
        await q.edit_message_text(
            "کانفیگ چندمنظوره‌ای برای این پلن ثبت نشده.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_del_multi")]]),
        )
        return
    rows = []
    for c in configs:
        label = c["config_text"][:25] + ("…" if len(c["config_text"]) > 25 else "")
        rows.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"delmulticfg_{c['id']}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_del_multi")])
    await q.edit_message_text("برای حذف روی کانفیگ مورد نظر بزن:", reply_markup=InlineKeyboardMarkup(rows))


async def admin_del_multi_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cfg_id = int(q.data.split("_")[1])
    conn = db()
    conn.execute("DELETE FROM configs WHERE id=?", (cfg_id,))
    conn.commit()
    conn.close()
    await q.edit_message_text("کانفیگ چندمنظوره حذف شد. ✅", reply_markup=admin_menu_kb())


# ==================== پنل ادمین: افزودن اشتراک دستی ====================
async def manual_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    conn = db()
    plans = conn.execute("SELECT * FROM plans").fetchall()
    conn.close()
    if not plans:
        await q.edit_message_text(
            "اول باید یه پلن بسازی.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")]]),
        )
        return
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"manualplan_{p['id']}")] for p in plans]
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="admin_menu")])
    await q.edit_message_text("اشتراک دستی برای کدوم پلن؟", reply_markup=InlineKeyboardMarkup(rows))


async def manual_sub_pick_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan_id = int(q.data.split("_")[1])
    context.user_data["manual_plan_id"] = plan_id
    await q.edit_message_text(
        "کاربر رو مشخص کن — یکی از این راه‌ها:\n"
        "• یه پیام از اون کاربر برام فوروارد کن\n"
        "• یوزرنیمش رو بفرست (مثلاً @username) — فقط اگه قبلاً حداقل یک‌بار با ربات صحبت کرده باشه\n"
        "• یا آیدی عددیش رو بفرست"
    )
    return MANUAL_SUB_USERID


async def manual_sub_identify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plan_id = context.user_data.get("manual_plan_id")
    target_id = None
    target_username = ""

    origin = getattr(update.message, "forward_origin", None)
    legacy_forward = getattr(update.message, "forward_from", None)

    if origin is not None:
        sender = getattr(origin, "sender_user", None)
        if sender:
            target_id = sender.id
            target_username = sender.username or ""
        else:
            await update.message.reply_text(
                "این کاربر تنظیمات مخفی‌سازی فوروارد رو فعال کرده و نمی‌تونم آیدیش رو بگیرم.\n"
                "یوزرنیم (@user) یا آیدی عددیش رو بفرست."
            )
            return MANUAL_SUB_USERID
    elif legacy_forward is not None:
        target_id = legacy_forward.id
        target_username = legacy_forward.username or ""
    elif update.message.text:
        text = update.message.text.strip()
        if text.startswith("@"):
            uname = text[1:].lower()
            conn = db()
            row = conn.execute(
                "SELECT user_id, username FROM customers WHERE lower(username)=?", (uname,)
            ).fetchone()
            conn.close()
            if row:
                target_id = row["user_id"]
                target_username = row["username"]
            else:
                await update.message.reply_text(
                    "این یوزرنیم بین کاربرانی که قبلاً با ربات صحبت کردن پیدا نشد.\n"
                    "یه پیام ازش فوروارد کن یا آیدی عددیش رو بفرست."
                )
                return MANUAL_SUB_USERID
        elif text.isdigit():
            target_id = int(text)
        else:
            await update.message.reply_text("فرمت نامعتبره. آیدی عددی، @یوزرنیم یا فوروارد پیام بفرست.")
            return MANUAL_SUB_USERID

    if not target_id:
        await update.message.reply_text("نتونستم کاربر رو تشخیص بدم، دوباره امتحان کن.")
        return MANUAL_SUB_USERID

    conn = db()
    plan = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    conn.close()
    if not plan:
        await update.message.reply_text("پلن پیدا نشد.")
        return ConversationHandler.END

    await update.message.reply_text("در حال یافتن بهترین کانفیگ (تست پینگ)... ⏳")
    delivered, cfg = await create_subscription_and_deliver(context, target_id, target_username, plan)

    if delivered:
        note = "و پیام حاوی کانفیگ براش ارسال شد. ✅"
    else:
        note = (
            "اما نتونستم پیام رو براش ارسال کنم — طبق قوانین تلگرام، ربات‌ها فقط می‌تونن به کسی پیام بدن "
            "که قبلاً یک‌بار /start ربات رو زده باشه؛ این محدودیت خود پلتفرمه.\n"
            "اشتراکش توی سیستم ثبت شد و به‌محض اینکه اون کاربر /start بزنه، کانفیگش خودکار براش ارسال میشه."
        )

    await update.message.reply_text(f"اشتراک دستی ثبت شد. {note}", reply_markup=admin_menu_kb())
    context.user_data.pop("manual_plan_id", None)
    return ConversationHandler.END


# ==================== پنل ادمین: پیام همگانی ====================
async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("متن پیام همگانی رو بفرست:")
    return BROADCAST_TEXT


async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    conn = db()
    customers = conn.execute("SELECT user_id FROM customers").fetchall()
    conn.close()
    ok, fail = 0, 0
    for c in customers:
        try:
            await context.bot.send_message(c["user_id"], text)
            ok += 1
        except Exception:
            fail += 1
    await update.message.reply_text(
        f"پیام همگانی ارسال شد.\nموفق: {ok}\nناموفق (استارت‌نزده): {fail}", reply_markup=admin_menu_kb()
    )
    return ConversationHandler.END


# ==================== یادآوری خودکار تمدید ====================
async def daily_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    conn = db()
    now = datetime.utcnow()
    soon = now + timedelta(days=REMINDER_DAYS_BEFORE)

    subs = conn.execute(
        "SELECT * FROM subscriptions WHERE active=1 AND reminded=0 AND expire_date <= ? AND expire_date > ?",
        (soon.isoformat(), now.isoformat()),
    ).fetchall()
    for s in subs:
        exp = datetime.fromisoformat(s["expire_date"])
        try:
            await context.bot.send_message(
                s["user_id"],
                f"⏰ یادآوری: اشتراک شما تا {exp.strftime('%Y-%m-%d')} اعتبار داره. "
                f"برای تمدید از منوی پلن‌ها اقدام کن.",
            )
        except Exception as e:
            logger.warning(f"خطا در ارسال یادآوری: {e}")
        conn.execute("UPDATE subscriptions SET reminded=1 WHERE id=?", (s["id"],))

    expired = conn.execute(
        "SELECT * FROM subscriptions WHERE active=1 AND expire_date <= ?", (now.isoformat(),)
    ).fetchall()
    for s in expired:
        conn.execute("UPDATE subscriptions SET active=0 WHERE id=?", (s["id"],))
        try:
            await context.bot.send_message(s["user_id"], "❗️ اشتراک شما به پایان رسید. برای تمدید اقدام کن.")
        except Exception as e:
            logger.warning(f"خطا در اطلاع‌رسانی انقضا: {e}")

    conn.commit()
    conn.close()


# ==================== main ====================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(30).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern="^back_main$"))
    app.add_handler(CallbackQueryHandler(show_plans, pattern="^plans$"))
    app.add_handler(CallbackQueryHandler(my_orders, pattern="^my_orders$"))
    app.add_handler(CallbackQueryHandler(my_account, pattern="^my_account$"))
    app.add_handler(CallbackQueryHandler(show_rules, pattern="^show_rules$"))
    app.add_handler(CallbackQueryHandler(show_support, pattern="^show_support$"))

    app.add_handler(CallbackQueryHandler(admin_menu, pattern="^admin_menu$"))
    app.add_handler(CallbackQueryHandler(admin_pending, pattern="^admin_pending$"))
    app.add_handler(CallbackQueryHandler(admin_customers, pattern="^admin_customers$"))
    app.add_handler(CallbackQueryHandler(admin_report, pattern="^admin_report$"))
    app.add_handler(CallbackQueryHandler(admin_config_stock, pattern="^admin_config_stock$"))
    app.add_handler(CallbackQueryHandler(admin_edit_plans, pattern="^admin_edit_plans$"))
    app.add_handler(CallbackQueryHandler(edit_plan_pick_field, pattern="^editplan_"))
    app.add_handler(CallbackQueryHandler(delete_plan_confirm, pattern="^delplan_"))
    app.add_handler(CallbackQueryHandler(delete_plan_do, pattern="^delplanyes_"))
    app.add_handler(CallbackQueryHandler(confirm_order, pattern="^confirm_"))
    app.add_handler(CallbackQueryHandler(reject_order, pattern="^reject_"))
    app.add_handler(CallbackQueryHandler(add_config_start, pattern="^admin_add_config$"))
    app.add_handler(CallbackQueryHandler(admin_del_multi_plans, pattern="^admin_del_multi$"))
    app.add_handler(CallbackQueryHandler(admin_del_multi_list, pattern="^delmultiplan_"))
    app.add_handler(CallbackQueryHandler(admin_del_multi_do, pattern="^delmulticfg_"))
    app.add_handler(CallbackQueryHandler(manual_sub_start, pattern="^admin_manual_sub$"))
    app.add_handler(CallbackQueryHandler(admin_settings, pattern="^admin_settings$"))
    app.add_handler(CallbackQueryHandler(setting_toggle_type, pattern="^setting_toggle_type$"))
    app.add_handler(CallbackQueryHandler(setting_toggle_ping, pattern="^setting_toggle_ping$"))
    app.add_handler(CallbackQueryHandler(admin_config_health_pick_plan, pattern="^admin_config_health$"))
    app.add_handler(CallbackQueryHandler(admin_config_health_list, pattern="^healthplan_"))

    buy_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(buy_plan, pattern="^buy_")],
        states={WAIT_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_txid)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(buy_conv)

    add_plan_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_plan_start, pattern="^admin_add_plan$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plan_name)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plan_price)],
            ADD_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plan_duration)],
            ADD_DATA_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plan_data_limit)],
            ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_plan_desc)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(add_plan_conv)

    edit_plan_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_plan_ask_value, pattern="^editfield_")],
        states={EDIT_PLAN_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_plan_save_value)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(edit_plan_conv)

    add_config_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_config_pick_plan, pattern="^cfgplan_")],
        states={
            ADD_CONFIG_TYPE: [CallbackQueryHandler(add_config_pick_type, pattern="^cfgtype_")],
            ADD_CONFIG_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_save)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(add_config_conv)

    edit_config_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_config_ask, pattern="^editcfg_")],
        states={EDIT_CONFIG_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_config_save)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(edit_config_conv)

    manual_sub_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(manual_sub_pick_plan, pattern="^manualplan_")],
        states={MANUAL_SUB_USERID: [MessageHandler(filters.ALL & ~filters.COMMAND, manual_sub_identify)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(manual_sub_conv)

    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(broadcast_start, pattern="^admin_broadcast$")],
        states={BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(broadcast_conv)

    support_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_set_support_start, pattern="^admin_set_support$")],
        states={SET_SUPPORT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_support_save)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(support_conv)

    welcome_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_set_welcome_start, pattern="^admin_set_welcome$")],
        states={SET_WELCOME_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_welcome_save)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(welcome_conv)

    rules_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_set_rules_start, pattern="^admin_set_rules$")],
        states={SET_RULES_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_rules_save)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )
    app.add_handler(rules_conv)

    if app.job_queue is not None:
        app.job_queue.run_daily(
            daily_reminder_job, time=datetime.strptime(f"{REMINDER_HOUR_UTC}:00", "%H:%M").time()
        )
        app.job_queue.run_repeating(
            periodic_config_health_check, interval=HEALTH_CHECK_INTERVAL_SECONDS, first=120
        )
    else:
        logger.warning(
            "JobQueue نصب نیست، یادآوری خودکار و چک سلامت خودکار غیرفعاله. "
            "برای فعال‌سازی: pip install \"python-telegram-bot[job-queue]==21.6\""
        )

    logger.info("ربات در حال اجراست...")
    app.run_polling()


if __name__ == "__main__":
    main()
