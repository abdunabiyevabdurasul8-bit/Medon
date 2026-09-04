# ============================================================
# COINDROP DONAT BOT
# Python 3.10+
#
# Telegram Premium = 7%
# Telegram Stars   = 12%
#
# DB: data/bot.db
# ============================================================

import os
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from datetime import datetime
from urllib.parse import quote

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# SOZLAMALAR
# ============================================================

BOT_TOKEN = "8611684086:AAGlWYOCV4IsmI7DtUBZSzoZiuYSuGQBcWQ"
ADMIN_ID = 5692925792

COINDROP_API_KEY = "cd_3131c619ce12ac55a92a801105d27bcd1df81dc9f8a07578"

BASE_URL = "https://coindrop.uz/api/v1"

CARD_NUMBER = "8600 0000 0000 0000"
CARD_OWNER = "Karta egasi"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

DB_FILE = os.path.join(DATA_DIR, "bot.db")

API_GET_TIMEOUT = 20
API_POST_TIMEOUT = 30


# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("coindrop")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    con = sqlite3.connect(
        DB_FILE,
        timeout=30,
    )
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    con = get_db()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            joined_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            declared_amount REAL DEFAULT 0,
            approved_amount REAL DEFAULT 0,
            receipt_file_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            reviewed_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            amount REAL,
            type TEXT,
            note TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER,
            coindrop_order_id TEXT,
            game_key TEXT,
            game_name TEXT,
            product_id TEXT,
            product_name TEXT,
            player_id TEXT,
            server_id TEXT,
            cost_uzs REAL,
            sell_uzs REAL,
            markup REAL,
            status TEXT,
            created_at TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            amount REAL,
            uses INTEGER DEFAULT 0,
            max_uses INTEGER DEFAULT 1,
            active INTEGER DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS promo_used (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_id INTEGER,
            tg_id INTEGER,
            UNIQUE(code_id, tg_id)
        )
    """)

    con.commit()
    con.close()

    log.info("Database: %s", DB_FILE)


# ============================================================
# USER
# ============================================================

def ensure_user(user):
    con = get_db()
    cur = con.cursor()

    cur.execute(
        "SELECT tg_id FROM users WHERE tg_id=?",
        (user.id,),
    )

    exists = cur.fetchone()

    if exists:
        cur.execute(
            """
            UPDATE users
            SET username=?, first_name=?
            WHERE tg_id=?
            """,
            (
                user.username or "",
                user.first_name or "",
                user.id,
            ),
        )
    else:
        cur.execute(
            """
            INSERT INTO users(
                tg_id,
                username,
                first_name,
                balance,
                joined_at
            )
            VALUES(?,?,?,?,?)
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                0,
                datetime.now().isoformat(),
            ),
        )

    con.commit()
    con.close()


def ensure_user_id(user_id):
    """
    Admin boshqa userga balans qo'shganda,
    user hali bazada bo'lmasa ham yaratadi.
    """
    con = get_db()
    cur = con.cursor()

    cur.execute(
        """
        INSERT OR IGNORE INTO users(
            tg_id,
            username,
            first_name,
            balance,
            joined_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            user_id,
            "",
            "",
            0,
            datetime.now().isoformat(),
        ),
    )

    con.commit()
    con.close()


def get_balance(user_id):
    ensure_user_id(user_id)

    con = get_db()
    cur = con.cursor()

    cur.execute(
        "SELECT balance FROM users WHERE tg_id=?",
        (user_id,),
    )

    row = cur.fetchone()
    con.close()

    if not row:
        return Decimal("0")

    return Decimal(str(row[0] or 0))


def change_balance(
    user_id,
    amount,
    tx_type,
    note="",
):
    amount = Decimal(str(amount))

    ensure_user_id(user_id)

    con = get_db()
    cur = con.cursor()

    cur.execute(
        """
        UPDATE users
        SET balance=balance+?
        WHERE tg_id=?
        """,
        (
            float(amount),
            user_id,
        ),
    )

    cur.execute(
        """
        INSERT INTO transactions(
            tg_id,
            amount,
            type,
            note,
            created_at
        )
        VALUES(?,?,?,?,?)
        """,
        (
            user_id,
            float(amount),
            tx_type,
            note,
            datetime.now().isoformat(),
        ),
    )

    con.commit()
    con.close()


# ============================================================
# API
# ============================================================

def api_headers():
    return {
        "X-API-Key": COINDROP_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def api_get(path):
    url = BASE_URL.rstrip("/") + "/" + path.lstrip("/")

    try:
        response = requests.get(
            url,
            headers=api_headers(),
            timeout=API_GET_TIMEOUT,
        )

        response.raise_for_status()

        try:
            return response.json()

        except Exception:
            return {
                "success": False,
                "error": "API JSON qaytarmadi",
                "raw": response.text[:2000],
            }

    except Exception as e:
        log.exception("GET %s", url)

        return {
            "success": False,
            "error": str(e),
        }


def api_post(path, payload):
    url = BASE_URL.rstrip("/") + "/" + path.lstrip("/")

    try:
        response = requests.post(
            url,
            headers=api_headers(),
            json=payload,
            timeout=API_POST_TIMEOUT,
        )

        response.raise_for_status()

        try:
            return response.json()

        except Exception:
            return {
                "success": False,
                "error": "API JSON qaytarmadi",
                "raw": response.text[:2000],
            }

    except Exception as e:
        log.exception("POST %s", url)

        return {
            "success": False,
            "error": str(e),
        }


# ============================================================
# API DATA
# ============================================================

def unwrap_data(data):
    if not isinstance(data, dict):
        return data

    value = data.get("data")

    if isinstance(value, dict):
        return value

    return data


def extract_games(data):
    if not isinstance(data, dict):
        return []

    d = unwrap_data(data)

    if isinstance(d, dict):
        if isinstance(d.get("games"), list):
            return d["games"]

    if isinstance(data.get("games"), list):
        return data["games"]

    return []


def extract_products(data):
    if not isinstance(data, dict):
        return []

    d = unwrap_data(data)

    if isinstance(d, dict):
        if isinstance(d.get("products"), list):
            return d["products"]

    if isinstance(data.get("products"), list):
        return data["products"]

    return []


def get_game_key(game):
    if isinstance(game, str):
        return game

    if not isinstance(game, dict):
        return ""

    return str(
        game.get("key")
        or game.get("game_key")
        or game.get("slug")
        or game.get("id")
        or ""
    )


def get_game_name(game):
    if isinstance(game, str):
        return game

    if not isinstance(game, dict):
        return "Noma'lum"

    return str(
        game.get("name")
        or game.get("title")
        or game.get("game_name")
        or game.get("key")
        or game.get("slug")
        or "Noma'lum"
    )


def get_product_id(product):
    if not isinstance(product, dict):
        return ""

    return str(
        product.get("id")
        or product.get("product_id")
        or product.get("offer_id")
        or product.get("key")
        or ""
    )


# ============================================================
# MAHSULOT NOMINI O'ZBEKCHAGA O'GIRISH
# ============================================================

def get_product_name(product):
    if not isinstance(product, dict):
        return "Noma'lum paket"

    name = str(
        product.get("name")
        or product.get("title")
        or product.get("product_name")
        or product.get("description")
        or get_product_id(product)
        or "Noma'lum paket"
    )

    # Premium uchun inglizcha oy nomlarini o'zbekchaga o'tkazish
    replacements = [
        ("12 Months", "12 oylik"),
        ("6 Months", "6 oylik"),
        ("3 Months", "3 oylik"),
        ("1 Month", "1 oylik"),

        ("12 months", "12 oylik"),
        ("6 months", "6 oylik"),
        ("3 months", "3 oylik"),
        ("1 month", "1 oylik"),

        ("12 MONTHS", "12 oylik"),
        ("6 MONTHS", "6 oylik"),
        ("3 MONTHS", "3 oylik"),
        ("1 MONTH", "1 oylik"),

        ("Months", "oylik"),
        ("Month", "oylik"),
        ("months", "oylik"),
        ("month", "oylik"),
    ]

    for old, new in replacements:
        name = name.replace(old, new)

    return name


def get_product_cost(product):
    if not isinstance(product, dict):
        return Decimal("0")

    keys = (
        "price_uzs",
        "cost_uzs",
        "wholesale_uzs",
        "retail_uzs",
        "price",
        "amount",
    )

    for key in keys:
        value = product.get(key)

        if value is None:
            continue

        try:
            return Decimal(str(value))
        except Exception:
            continue

    return Decimal("0")


# ============================================================
# USTAMA
# ============================================================

def get_markup(game_key, game_name):
    key = str(game_key).lower().replace("_", "-")
    name = str(game_name).lower()

    text = f"{key} {name}"

    # Telegram Premium = 7%
    if (
        "telegram-premium" in key
        or "tg-premium" in key
        or "telegram premium" in name
        or "tg premium" in name
    ):
        return Decimal("0.07")

    # Telegram Stars = 12%
    if (
        "telegram-stars" in key
        or "tg-stars" in key
        or "telegram stars" in name
        or "tg stars" in name
    ):
        return Decimal("0.12")

    # Boshqa mahsulot bo'lsa
    return Decimal("0.10")


def calculate_sell_price(
    cost,
    game_key,
    game_name,
):
    cost = Decimal(str(cost))

    markup = get_markup(
        game_key,
        game_name,
    )

    price = cost * (
        Decimal("1") + markup
    )

    # 100 so'mgacha yaxlitlash
    price = (
        price / Decimal("100")
    ).quantize(
        Decimal("1")
    ) * Decimal("100")

    return price


def money(value):
    try:
        value = Decimal(str(value))
    except Exception:
        value = Decimal("0")

    return f"{value:,.0f}".replace(",", " ")


# ============================================================
# API BALANCE
# ============================================================

def find_balance(data):
    if not isinstance(data, dict):
        return None

    d = unwrap_data(data)

    possible = (
        "balance_uzs",
        "uzs",
        "balanceUZS",
        "amount_uzs",
        "balance",
        "amount",
    )

    sources = []

    if isinstance(d, dict):
        sources.append(d)

    if isinstance(data, dict):
        sources.append(data)

    for source in sources:
        for key in possible:
            value = source.get(key)

            if value is None:
                continue

            if isinstance(
                value,
                (int, float, str),
            ):
                try:
                    return Decimal(str(value))
                except Exception:
                    pass

    return None


# ============================================================
# KEYBOARD
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            [
                "💎 Telegram Premium",
                "⭐ Telegram Stars",
            ],
            [
                "💎 Mening balansim",
                "💳 Balans to'ldirish",
            ],
            [
                "📦 Buyurtmalarim",
                "🎟 Promokod",
            ],
            [
                "🆘 Yordam",
            ],
        ],
        resize_keyboard=True,
    )


def admin_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "💳 API balans",
                    callback_data="admin_api_balance",
                ),
                InlineKeyboardButton(
                    "🔄 Katalogni yangilash",
                    callback_data="admin_refresh_catalog",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📊 Statistika",
                    callback_data="admin_stats",
                ),
                InlineKeyboardButton(
                    "💰 Kirim",
                    callback_data="admin_income",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👥 Foydalanuvchilar",
                    callback_data="admin_users",
                ),
            ],
            [
                InlineKeyboardButton(
                    "➕ Balans qo'shish",
                    callback_data="admin_add",
                ),
                InlineKeyboardButton(
                    "➖ Balans ayirish",
                    callback_data="admin_sub",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📢 Reklama",
                    callback_data="admin_broadcast",
                ),
                InlineKeyboardButton(
                    "🎟 Promo yaratish",
                    callback_data="admin_promo",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💵 Foyda",
                    callback_data="admin_profit",
                ),
            ],
        ]
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    ensure_user(update.effective_user)

    context.user_data.clear()

    await update.message.reply_text(
        "👋 Assalomu alaykum!\n\n"
        "🎮 Donuz botiga xush kelibsiz.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# CATALOG
# ============================================================

async def get_games(context):
    cached = context.application.bot_data.get(
        "catalog_games"
    )

    if cached:
        return cached

    result = await asyncio.to_thread(
        api_get,
        "/games",
    )

    games = extract_games(result)

    if games:
        context.application.bot_data[
            "catalog_games"
        ] = games

    return games


def find_telegram_game(
    games,
    kind,
):
    """
    kind = premium yoki stars
    """

    for game in games:
        key = get_game_key(game).lower()
        name = get_game_name(game).lower()

        key = key.replace("_", "-")

        if kind == "premium":
            if (
                "telegram-premium" in key
                or "tg-premium" in key
                or "telegram premium" in name
                or "tg premium" in name
            ):
                return game

        elif kind == "stars":
            if (
                "telegram-stars" in key
                or "tg-stars" in key
                or "telegram stars" in name
                or "tg stars" in name
            ):
                return game

    return None


# ============================================================
# TELEGRAM PREMIUM
# ============================================================

async def telegram_premium_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    ensure_user(update.effective_user)

    games = await get_games(context)

    game = find_telegram_game(
        games,
        "premium",
    )

    if not game:
        await update.message.reply_text(
            "❌ Telegram Premium API katalogida topilmadi."
        )
        return

    game_key = get_game_key(game)
    game_name = get_game_name(game)

    result = await asyncio.to_thread(
        api_get,
        f"/games/{quote(game_key, safe='')}/products",
    )

    products = extract_products(result)

    if not products:
        await update.message.reply_text(
            "❌ Telegram Premium paketlari topilmadi."
        )
        return

    context.user_data["game_key"] = game_key
    context.user_data["game_name"] = game_name
    context.user_data["products"] = products
    context.user_data["product_page"] = 0

    await show_products(
        update.message,
        context,
    )


# ============================================================
# TELEGRAM STARS
# ============================================================

async def telegram_stars_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    ensure_user(update.effective_user)

    games = await get_games(context)

    game = find_telegram_game(
        games,
        "stars",
    )

    if not game:
        await update.message.reply_text(
            "❌ Telegram Stars API katalogida topilmadi."
        )
        return

    game_key = get_game_key(game)
    game_name = get_game_name(game)

    result = await asyncio.to_thread(
        api_get,
        f"/games/{quote(game_key, safe='')}/products",
    )

    products = extract_products(result)

    if not products:
        await update.message.reply_text(
            "❌ Telegram Stars paketlari topilmadi."
        )
        return

    context.user_data["game_key"] = game_key
    context.user_data["game_name"] = game_name
    context.user_data["products"] = products
    context.user_data["product_page"] = 0

    await show_products(
        update.message,
        context,
    )


# ============================================================
# PRODUCTS
# ============================================================

async def show_products(
    message,
    context,
):
    products = context.user_data.get(
        "products",
        [],
    )

    page = int(
        context.user_data.get(
            "product_page",
            0,
        )
    )

    per_page = 10

    total_pages = max(
        1,
        (
            len(products)
            + per_page
            - 1
        )
        // per_page,
    )

    page = max(
        0,
        min(
            page,
            total_pages - 1,
        ),
    )

    context.user_data[
        "product_page"
    ] = page

    start = page * per_page
    end = start + per_page

    buttons = []

    game_key = context.user_data.get(
        "game_key",
        "",
    )

    game_name = context.user_data.get(
        "game_name",
        "",
    )

    for index in range(
        start,
        min(
            end,
            len(products),
        ),
    ):
        product = products[index]

        cost = get_product_cost(
            product
        )

        price = calculate_sell_price(
            cost,
            game_key,
            game_name,
        )

        product_name = get_product_name(
            product
        )

        button_text = (
            f"{product_name[:28]} "
            f"— {money(price)} so'm"
        )

        buttons.append(
            InlineKeyboardButton(
                button_text,
                callback_data=f"product:{index}",
            )
        )

    rows = []

    for i in range(
        0,
        len(buttons),
        2,
    ):
        rows.append(
            buttons[i:i + 2]
        )

    navigation = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Oldingi",
                callback_data=f"page:{page - 1}",
            )
        )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                "Keyingi ➡️",
                callback_data=f"page:{page + 1}",
            )
        )

    if navigation:
        rows.append(navigation)

    rows.append(
        [
            InlineKeyboardButton(
                "🔙 Asosiy menyu",
                callback_data="back_main",
            )
        ]
    )

    await message.reply_text(
        f"📦 {game_name}\n\n"
        f"Paketni tanlang:\n"
        f"📄 {page + 1}/{total_pages}",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def product_page_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    try:
        page = int(
            query.data.split(":")[1]
        )
    except Exception:
        page = 0

    context.user_data[
        "product_page"
    ] = page

    await show_products(
        query.message,
        context,
    )


async def back_main_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    context.user_data.clear()

    await query.message.reply_text(
        "🏠 Asosiy menyu:",
        reply_markup=main_keyboard(),
    )


# ============================================================
# PRODUCT SELECT
# ============================================================

async def product_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    try:
        index = int(
            query.data.split(":")[1]
        )
    except Exception:
        await query.message.reply_text(
            "❌ Paket tanlashda xatolik."
        )
        return

    products = context.user_data.get(
        "products",
        [],
    )

    if index < 0 or index >= len(products):
        await query.message.reply_text(
            "❌ Paket topilmadi."
        )
        return

    product = products[index]

    game_key = context.user_data.get(
        "game_key",
        "",
    )

    game_name = context.user_data.get(
        "game_name",
        "",
    )

    cost = get_product_cost(
        product
    )

    price = calculate_sell_price(
        cost,
        game_key,
        game_name,
    )

    if cost <= 0:
        await query.message.reply_text(
            "❌ Paket narxini API dan olib bo'lmadi."
        )
        return

    context.user_data[
        "selected_product"
    ] = product

    context.user_data[
        "selected_price"
    ] = str(price)

    context.user_data[
        "state"
    ] = "player"

    await query.message.reply_text(
        f"🛒 {get_product_name(product)}\n\n"
        f"💰 Narx: {money(price)} so'm\n\n"
        f"🆔 Telegram username yoki ID ni yuboring:"
    )


# ============================================================
# PLAYER
# ============================================================

async def process_player(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    player_id = (
        update.message.text
        .strip()
    )

    if not player_id:
        await update.message.reply_text(
            "❌ Ma'lumot bo'sh bo'lmasin."
        )
        return

    context.user_data[
        "player_id"
    ] = player_id

    context.user_data[
        "server_id"
    ] = "0"

    await process_order_preview(
        update,
        context,
    )


# ============================================================
# ORDER PREVIEW
# ============================================================

async def process_order_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    product = context.user_data.get(
        "selected_product"
    )

    game_name = context.user_data.get(
        "game_name",
        "",
    )

    price = Decimal(
        context.user_data.get(
            "selected_price",
            "0",
        )
    )

    if not product:
        context.user_data.clear()

        await update.message.reply_text(
            "❌ Buyurtma ma'lumotlari topilmadi.",
            reply_markup=main_keyboard(),
        )
        return

    balance = get_balance(
        update.effective_user.id
    )

    if balance < price:
        context.user_data.clear()

        await update.message.reply_text(
            f"❌ Balansingiz yetarli emas.\n\n"
            f"💰 Kerak: {money(price)} so'm\n"
            f"💎 Balans: {money(balance)} so'm\n\n"
            f"Avval balansni to'ldiring.",
            reply_markup=main_keyboard(),
        )
        return

    context.user_data[
        "state"
    ] = None

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Tasdiqlash",
                    callback_data="confirm_order",
                ),
                InlineKeyboardButton(
                    "❌ Bekor qilish",
                    callback_data="cancel_order",
                ),
            ]
        ]
    )

    await update.message.reply_text(
        f"📦 Buyurtmani tekshiring:\n\n"
        f"🎮 {game_name}\n"
        f"📦 {get_product_name(product)}\n"
        f"👤 Telegram: {context.user_data.get('player_id')}\n"
        f"💰 To'lov: {money(price)} so'm\n\n"
        f"Tasdiqlaysizmi?",
        reply_markup=keyboard,
    )


# ============================================================
# CONFIRM ORDER
# ============================================================

async def confirm_order_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    if context.user_data.get(
        "order_processing"
    ):
        await query.message.reply_text(
            "⏳ Buyurtma qayta ishlanmoqda."
        )
        return

    context.user_data[
        "order_processing"
    ] = True

    try:
        product = context.user_data.get(
            "selected_product"
        )

        game_key = context.user_data.get(
            "game_key",
            "",
        )

        game_name = context.user_data.get(
            "game_name",
            "",
        )

        player_id = context.user_data.get(
            "player_id",
            "",
        )

        server_id = "0"

        if not product:
            await query.message.reply_text(
                "❌ Buyurtma topilmadi."
            )
            return

        cost = get_product_cost(
            product
        )

        price = calculate_sell_price(
            cost,
            game_key,
            game_name,
        )

        balance = get_balance(
            query.from_user.id
        )

        if balance < price:
            await query.message.reply_text(
                "❌ Balans yetarli emas."
            )
            return

        product_id = get_product_id(
            product
        )

        payload = {
            "game": game_key,
            "game_key": game_key,
            "product_id": product_id,
            "player_id": player_id,
            "server_id": server_id,
        }

        result = await asyncio.to_thread(
            api_post,
            "/orders",
            payload,
        )

        success = True

        if isinstance(result, dict):

            if result.get("success") is False:
                success = False

            status = str(
                result.get(
                    "status",
                    "",
                )
            ).lower()

            if status in (
                "error",
                "failed",
                "failure",
            ):
                success = False

        if not success:
            error = "API xatosi"

            if isinstance(result, dict):
                error = (
                    result.get("message")
                    or result.get("error")
                    or result.get("detail")
                    or error
                )

            await query.message.reply_text(
                f"❌ Buyurtma bajarilmadi.\n\n"
                f"{error}"
            )
            return

        # API muvaffaqiyatli bo'lsa balansdan ayiriladi
        change_balance(
            query.from_user.id,
            -price,
            "purchase",
            f"{game_name} / "
            f"{get_product_name(product)}",
        )

        order_id = ""

        if isinstance(result, dict):
            data = unwrap_data(result)

            if isinstance(data, dict):
                order_id = str(
                    data.get("order_id")
                    or data.get("id")
                    or data.get("orderId")
                    or ""
                )

        markup_percent = (
            get_markup(
                game_key,
                game_name,
            ) * 100
        )

        con = get_db()
        cur = con.cursor()

        cur.execute(
            """
            INSERT INTO orders(
                tg_id,
                coindrop_order_id,
                game_key,
                game_name,
                product_id,
                product_name,
                player_id,
                server_id,
                cost_uzs,
                sell_uzs,
                markup,
                status,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                query.from_user.id,
                order_id,
                game_key,
                game_name,
                product_id,
                get_product_name(product),
                player_id,
                server_id,
                float(cost),
                float(price),
                float(markup_percent),
                "success",
                datetime.now().isoformat(),
            ),
        )

        con.commit()
        con.close()

        new_balance = get_balance(
            query.from_user.id
        )

        await query.message.reply_text(
            f"✅ Buyurtma qabul qilindi!\n\n"
            f"🎮 {game_name}\n"
            f"📦 {get_product_name(product)}\n"
            f"👤 Telegram: {player_id}\n"
            f"💰 To'lov: {money(price)} so'm\n"
            f"💎 Qolgan balans: {money(new_balance)} so'm",
            reply_markup=main_keyboard(),
        )

        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"🛒 Yangi buyurtma\n\n"
                    f"👤 User: {query.from_user.id}\n"
                    f"🎮 {game_name}\n"
                    f"📦 {get_product_name(product)}\n"
                    f"👤 Telegram: {player_id}\n"
                    f"💰 {money(price)} so'm\n"
                    f"🆔 API order: {order_id or '-'}",
                )
            except Exception:
                pass

        context.user_data.clear()

    finally:
        context.user_data.pop(
            "order_processing",
            None,
        )


async def cancel_order_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    context.user_data.clear()

    await query.message.reply_text(
        "❌ Buyurtma bekor qilindi.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    ensure_user(update.effective_user)

    value = get_balance(
        update.effective_user.id
    )

    await update.message.reply_text(
        f"💎 Mening balansim\n\n"
        f"💰 {money(value)} so'm"
    )


# ============================================================
# DEPOSIT START
# ============================================================

async def deposit_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    ensure_user(update.effective_user)

    context.user_data.clear()

    context.user_data[
        "state"
    ] = "deposit_amount"

    await update.message.reply_text(
        f"💳 Balans to'ldirish\n\n"
        f"💳 Karta: {CARD_NUMBER}\n"
        f"👤 Karta egasi: {CARD_OWNER}\n\n"
        f"Pulni kartaga o'tkazing.\n\n"
        f"Keyin to'lov summasini yuboring.\n\n"
        f"Masalan: 50000"
    )


# ============================================================
# DEPOSIT AMOUNT
# ============================================================

async def deposit_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        update.message.text
        .strip()
        .replace(" ", "")
        .replace(",", "")
    )

    try:
        amount = Decimal(text)

    except InvalidOperation:
        await update.message.reply_text(
            "❌ Summani raqam bilan yuboring."
        )
        return

    if amount <= 0:
        await update.message.reply_text(
            "❌ Summa 0 dan katta bo'lsin."
        )
        return

    context.user_data[
        "deposit_amount"
    ] = str(amount)

    context.user_data[
        "state"
    ] = "deposit_receipt"

    await update.message.reply_text(
        f"💰 Summa: {money(amount)} so'm\n\n"
        f"📸 Endi to'lov chekini rasm qilib yuboring."
    )


# ============================================================
# RECEIPT
# ============================================================

async def deposit_receipt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    amount = Decimal(
        context.user_data.get(
            "deposit_amount",
            "0",
        )
    )

    file_id = None

    if update.message.photo:
        file_id = (
            update.message.photo[-1]
            .file_id
        )

    elif update.message.document:
        file_id = (
            update.message.document.file_id
        )

    if not file_id:
        await update.message.reply_text(
            "❌ Chekni rasm yoki fayl qilib yuboring."
        )
        return

    con = get_db()
    cur = con.cursor()

    cur.execute(
        """
        INSERT INTO deposits(
            tg_id,
            declared_amount,
            approved_amount,
            receipt_file_id,
            status,
            created_at
        )
        VALUES(?,?,?,?,?,?)
        """,
        (
            update.effective_user.id,
            float(amount),
            0,
            file_id,
            "pending",
            datetime.now().isoformat(),
        ),
    )

    deposit_id = cur.lastrowid

    con.commit()
    con.close()

    context.user_data.clear()

    await update.message.reply_text(
        "✅ Chek qabul qilindi.\n\n"
        "⏳ Admin tekshiradi.\n"
        "Tasdiqlangandan keyin balansingizga tushadi.",
        reply_markup=main_keyboard(),
    )

    if not ADMIN_ID:
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Qabul qilish",
                    callback_data=f"dep_accept:{deposit_id}",
                ),
                InlineKeyboardButton(
                    "❌ Rad etish",
                    callback_data=f"dep_reject:{deposit_id}",
                ),
            ]
        ]
    )

    caption = (
        f"💳 Yangi balans to'ldirish\n\n"
        f"🆔 Deposit: {deposit_id}\n"
        f"👤 User: {update.effective_user.id}\n"
        f"💰 Summa: {money(amount)} so'm"
    )

    try:
        if update.message.photo:
            await context.bot.send_photo(
                ADMIN_ID,
                file_id,
                caption=caption,
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_document(
                ADMIN_ID,
                file_id,
                caption=caption,
                reply_markup=keyboard,
            )

    except Exception as e:
        log.exception(
            "Receipt admin send error: %s",
            e,
        )


# ============================================================
# DEPOSIT CALLBACK
# ============================================================

async def deposit_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    try:
        action, dep_id_text = (
            query.data.split(":")
        )

        dep_id = int(
            dep_id_text
        )

    except Exception:
        await query.message.reply_text(
            "❌ Deposit ID xato."
        )
        return

    con = get_db()
    cur = con.cursor()

    cur.execute(
        """
        SELECT
            tg_id,
            declared_amount,
            status
        FROM deposits
        WHERE id=?
        """,
        (dep_id,),
    )

    row = cur.fetchone()

    if not row:
        con.close()

        await query.message.reply_text(
            "❌ Deposit topilmadi."
        )
        return

    tg_id, declared_amount, status = row

    if status != "pending":
        con.close()

        await query.message.reply_text(
            f"⚠️ Bu deposit allaqachon ko'rib chiqilgan.\n"
            f"Status: {status}"
        )
        return

    if action == "dep_reject":
        cur.execute(
            """
            UPDATE deposits
            SET
                status='rejected',
                reviewed_at=?
            WHERE id=?
            """,
            (
                datetime.now().isoformat(),
                dep_id,
            ),
        )

        con.commit()
        con.close()

        await query.message.reply_text(
            f"❌ Deposit #{dep_id} rad etildi."
        )

        try:
            await context.bot.send_message(
                tg_id,
                "❌ To'lov chekingiz rad etildi.\n\n"
                "Agar xatolik bo'lsa admin bilan bog'laning.",
            )
        except Exception:
            pass

        return

    if action == "dep_accept":
        con.close()

        context.user_data[
            "state"
        ] = "admin_deposit_amount"

        context.user_data[
            "admin_deposit_id"
        ] = dep_id

        await query.message.reply_text(
            f"✅ Deposit #{dep_id}\n\n"
            f"Chekdagi tasdiqlangan summani yuboring.\n\n"
            f"Masalan: {money(declared_amount)}"
        )


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(user_id):
    return (
        ADMIN_ID != 0
        and user_id == ADMIN_ID
    )


# ============================================================
# ADMIN COMMAND
# ============================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_admin(
        update.effective_user.id
    ):
        await update.message.reply_text(
            "❌ Ruxsat yo'q."
        )
        return

    context.user_data.clear()

    await update.message.reply_text(
        "👑 ADMIN PANEL",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN API BALANCE
# ============================================================

async def admin_api_balance_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer(
        "⏳ Tekshirilmoqda..."
    )

    result = await asyncio.to_thread(
        api_get,
        "/balance",
    )

    if (
        isinstance(result, dict)
        and result.get("success") is False
    ):
        await query.message.reply_text(
            "❌ API balansni olishda xatolik.\n\n"
            f"{result.get('error') or result.get('message') or 'Nomaʼlum xato'}",
            reply_markup=admin_keyboard(),
        )
        return

    value = find_balance(
        result
    )

    if value is None:
        await query.message.reply_text(
            "⚠️ API javobida balans topilmadi.\n\n"
            f"API javobi:\n"
            f"{str(result)[:1500]}",
            reply_markup=admin_keyboard(),
        )
        return

    await query.message.reply_text(
        "💳 COINDROP API BALANS\n\n"
        f"💰 {money(value)} UZS",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN CATALOG REFRESH
# ============================================================

async def admin_refresh_catalog_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer(
        "⏳ Katalog yangilanmoqda..."
    )

    context.application.bot_data.pop(
        "catalog_games",
        None,
    )

    result = await asyncio.to_thread(
        api_get,
        "/games",
    )

    games = extract_games(result)

    if not games:
        await query.message.reply_text(
            "❌ Katalogni yangilab bo'lmadi.\n\n"
            f"{str(result)[:1500]}",
            reply_markup=admin_keyboard(),
        )
        return

    context.application.bot_data[
        "catalog_games"
    ] = games

    updated = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    context.application.bot_data[
        "catalog_updated"
    ] = updated

    await query.message.reply_text(
        "✅ KATALOG YANGILANDI!\n\n"
        f"🎮 O'yinlar: {len(games)} ta\n"
        f"🕐 Vaqt: {updated}",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def admin_stats_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    con = get_db()
    cur = con.cursor()

    cur.execute(
        "SELECT COUNT(*) FROM users"
    )
    users = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM orders"
    )
    orders = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COUNT(*)
        FROM orders
        WHERE status='success'
        """
    )
    success = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COALESCE(SUM(amount),0)
        FROM transactions
        WHERE type='deposit'
        AND amount > 0
        """
    )
    deposits = cur.fetchone()[0] or 0

    cur.execute(
        """
        SELECT COALESCE(SUM(-amount),0)
        FROM transactions
        WHERE type='purchase'
        AND amount < 0
        """
    )
    purchases = cur.fetchone()[0] or 0

    con.close()

    await query.message.reply_text(
        "📊 STATISTIKA\n\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"📦 Buyurtmalar: {orders}\n"
        f"✅ Muvaffaqiyatli: {success}\n"
        f"💳 Depozitlar: {money(deposits)} so'm\n"
        f"🛒 Sotuvlar: {money(purchases)} so'm",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN KIRIM
# BUGUN / KECHA / HAFTA / OY / UMUMIY
# ============================================================

async def admin_income_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    con = get_db()
    cur = con.cursor()

    # Bugun
    cur.execute(
        """
        SELECT COALESCE(SUM(amount),0)
        FROM transactions
        WHERE type='deposit'
        AND amount > 0
        AND date(created_at)=date('now','localtime')
        """
    )
    today = cur.fetchone()[0] or 0

    # Kecha
    cur.execute(
        """
        SELECT COALESCE(SUM(amount),0)
        FROM transactions
        WHERE type='deposit'
        AND amount > 0
        AND date(created_at)=date('now','localtime','-1 day')
        """
    )
    yesterday = cur.fetchone()[0] or 0

    # Oxirgi 7 kun
    cur.execute(
        """
        SELECT COALESCE(SUM(amount),0)
        FROM transactions
        WHERE type='deposit'
        AND amount > 0
        AND datetime(created_at) >= datetime('now','localtime','-6 days')
        """
    )
    week = cur.fetchone()[0] or 0

    # Shu oy
    cur.execute(
        """
        SELECT COALESCE(SUM(amount),0)
        FROM transactions
        WHERE type='deposit'
        AND amount > 0
        AND strftime('%Y-%m', created_at)
            = strftime('%Y-%m','now','localtime')
        """
    )
    month = cur.fetchone()[0] or 0

    # Umumiy
    cur.execute(
        """
        SELECT COALESCE(SUM(amount),0)
        FROM transactions
        WHERE type='deposit'
        AND amount > 0
        """
    )
    total = cur.fetchone()[0] or 0

    con.close()

    await query.message.reply_text(
        "💰 KIRIM STATISTIKASI\n\n"
        f"🟢 Bugun: {money(today)} so'm\n"
        f"🟡 Kecha: {money(yesterday)} so'm\n"
        f"🔵 Hafta: {money(week)} so'm\n"
        f"🟣 Oy: {money(month)} so'm\n"
        f"⚪ Umumiy: {money(total)} so'm",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN USERS
# ============================================================

async def admin_users_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    con = get_db()
    cur = con.cursor()

    cur.execute(
        "SELECT COUNT(*) FROM users"
    )

    count = cur.fetchone()[0]

    cur.execute(
        """
        SELECT COALESCE(SUM(balance),0)
        FROM users
        """
    )

    total = cur.fetchone()[0] or 0

    con.close()

    await query.message.reply_text(
        "👥 FOYDALANUVCHILAR\n\n"
        f"👤 Jami: {count}\n"
        f"💎 Balanslar jami: {money(total)} so'm",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN ADD
# ============================================================

async def admin_add_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    context.user_data[
        "state"
    ] = "admin_add_user"

    await query.message.reply_text(
        "➕ Balans qo'shish\n\n"
        "User Telegram ID sini yuboring:"
    )


# ============================================================
# ADMIN SUB
# ============================================================

async def admin_sub_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    context.user_data[
        "state"
    ] = "admin_sub_user"

    await query.message.reply_text(
        "➖ Balans ayirish\n\n"
        "User Telegram ID sini yuboring:"
    )


# ============================================================
# ADMIN PROFIT
# ============================================================

async def admin_profit_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    con = get_db()
    cur = con.cursor()

    cur.execute(
        """
        SELECT
            COALESCE(SUM(sell_uzs),0),
            COALESCE(SUM(cost_uzs),0),
            COALESCE(
                SUM(sell_uzs-cost_uzs),
                0
            )
        FROM orders
        WHERE status='success'
        """
    )

    sales, cost, profit = cur.fetchone()

    con.close()

    await query.message.reply_text(
        "💰 FOYDA\n\n"
        f"🛒 Sotuv: {money(sales)} so'm\n"
        f"📦 Tannarx: {money(cost)} so'm\n"
        f"💵 Foyda: {money(profit)} so'm",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN BROADCAST
# ============================================================

async def admin_broadcast_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    context.user_data[
        "state"
    ] = "admin_broadcast"

    await query.message.reply_text(
        "📢 Reklama matnini yuboring:"
    )


# ============================================================
# ADMIN PROMO
# ============================================================

async def admin_promo_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(
        query.from_user.id
    ):
        await query.answer(
            "❌ Ruxsat yo'q.",
            show_alert=True,
        )
        return

    await query.answer()

    context.user_data[
        "state"
    ] = "admin_promo"

    await query.message.reply_text(
        "🎟 Promo yaratish\n\n"
        "Format:\n"
        "KOD SUMMA LIMIT\n\n"
        "Misol:\n"
        "PROMO50 50000 10"
    )


# ============================================================
# PROMO USER
# ============================================================

async def promo_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    context.user_data[
        "state"
    ] = "promo"

    await update.message.reply_text(
        "🎟 Promo kodni yuboring:"
    )


async def process_promo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    code = (
        update.message.text
        .strip()
        .upper()
    )

    con = get_db()
    cur = con.cursor()

    cur.execute(
        """
        SELECT
            id,
            amount,
            uses,
            max_uses,
            active
        FROM promo_codes
        WHERE code=?
        """,
        (code,),
    )

    row = cur.fetchone()

    if not row:
        con.close()

        await update.message.reply_text(
            "❌ Promo topilmadi."
        )

        context.user_data.clear()
        return

    (
        code_id,
        amount,
        uses,
        max_uses,
        active,
    ) = row

    if not active or uses >= max_uses:
        con.close()

        await update.message.reply_text(
            "❌ Promo ishlamaydi yoki limiti tugagan."
        )

        context.user_data.clear()
        return

    cur.execute(
        """
        SELECT id
        FROM promo_used
        WHERE code_id=?
        AND tg_id=?
        """,
        (
            code_id,
            update.effective_user.id,
        ),
    )

    if cur.fetchone():
        con.close()

        await update.message.reply_text(
            "❌ Siz bu promodan foydalangansiz."
        )

        context.user_data.clear()
        return

    cur.execute(
        """
        INSERT INTO promo_used(
            code_id,
            tg_id
        )
        VALUES(?,?)
        """,
        (
            code_id,
            update.effective_user.id,
        ),
    )

    cur.execute(
        """
        UPDATE promo_codes
        SET uses=uses+1
        WHERE id=?
        """,
        (code_id,),
    )

    con.commit()
    con.close()

    change_balance(
        update.effective_user.id,
        Decimal(str(amount)),
        "promo",
        f"Promo {code}",
    )

    context.user_data.clear()

    await update.message.reply_text(
        f"✅ Promo qabul qilindi!\n\n"
        f"🎟 Kod: {code}\n"
        f"💰 Qo'shildi: {money(amount)} so'm\n"
        f"💎 Balans: "
        f"{money(get_balance(update.effective_user.id))} so'm",
        reply_markup=main_keyboard(),
    )


# ============================================================
# MY ORDERS
# ============================================================

async def my_orders(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    con = get_db()
    cur = con.cursor()

    cur.execute(
        """
        SELECT
            game_name,
            product_name,
            player_id,
            sell_uzs,
            status,
            created_at
        FROM orders
        WHERE tg_id=?
        ORDER BY id DESC
        LIMIT 10
        """,
        (
            update.effective_user.id,
        ),
    )

    rows = cur.fetchall()
    con.close()

    if not rows:
        await update.message.reply_text(
            "📦 Hali buyurtmalaringiz yo'q."
        )
        return

    text = "📦 BUYURTMALARIM\n\n"

    for i, row in enumerate(
        rows,
        1,
    ):
        (
            game_name,
            product_name,
            player_id,
            price,
            status,
            created_at,
        ) = row

        text += (
            f"{i}. 🎮 {game_name}\n"
            f"📦 {product_name}\n"
            f"👤 {player_id}\n"
            f"💰 {money(price)} so'm\n"
            f"📌 {status}\n\n"
        )

    await update.message.reply_text(
        text
    )


# ============================================================
# ADMIN TEXT
# ============================================================

async def process_admin_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    state = context.user_data.get(
        "state"
    )

    # --------------------------------------------------------
    # DEPOSIT ACCEPT
    # --------------------------------------------------------

    if state == "admin_deposit_amount":

        dep_id = context.user_data.get(
            "admin_deposit_id"
        )

        text = (
            update.message.text
            .strip()
            .replace(" ", "")
            .replace(",", "")
        )

        try:
            amount = Decimal(text)

        except InvalidOperation:
            await update.message.reply_text(
                "❌ Summani raqam bilan yuboring."
            )
            return

        if amount <= 0:
            await update.message.reply_text(
                "❌ Summa 0 dan katta bo'lsin."
            )
            return

        con = get_db()
        cur = con.cursor()

        cur.execute(
            """
            SELECT tg_id,status
            FROM deposits
            WHERE id=?
            """,
            (dep_id,),
        )

        row = cur.fetchone()

        if not row:
            con.close()

            await update.message.reply_text(
                "❌ Deposit topilmadi."
            )

            context.user_data.clear()
            return

        tg_id, status = row

        if status != "pending":
            con.close()

            await update.message.reply_text(
                "⚠️ Deposit allaqachon ko'rib chiqilgan."
            )

            context.user_data.clear()
            return

        cur.execute(
            """
            UPDATE deposits
            SET
                approved_amount=?,
                status='approved',
                reviewed_at=?
            WHERE id=?
            """,
            (
                float(amount),
                datetime.now().isoformat(),
                dep_id,
            ),
        )

        con.commit()
        con.close()

        change_balance(
            tg_id,
            amount,
            "deposit",
            f"Deposit #{dep_id}",
        )

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ Deposit #{dep_id} tasdiqlandi.\n\n"
            f"💰 Qo'shildi: {money(amount)} so'm",
            reply_markup=admin_keyboard(),
        )

        try:
            user_balance = get_balance(
                tg_id
            )

            await context.bot.send_message(
                tg_id,
                f"✅ To'lov tasdiqlandi!\n\n"
                f"💰 Qo'shildi: {money(amount)} so'm\n"
                f"💎 Balans: {money(user_balance)} so'm",
            )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # ADMIN ADD USER
    # --------------------------------------------------------

    if state == "admin_add_user":

        try:
            tg_id = int(
                update.message.text.strip()
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Telegram ID raqam bo'lishi kerak."
            )
            return

        ensure_user_id(tg_id)

        context.user_data[
            "admin_target_user"
        ] = tg_id

        context.user_data[
            "state"
        ] = "admin_add_amount"

        await update.message.reply_text(
            "💰 Qo'shiladigan summani yuboring:"
        )

        return

    # --------------------------------------------------------
    # ADMIN ADD AMOUNT
    # --------------------------------------------------------

    if state == "admin_add_amount":

        try:
            amount = Decimal(
                update.message.text
                .strip()
                .replace(" ", "")
            )

        except InvalidOperation:
            await update.message.reply_text(
                "❌ Summa noto'g'ri."
            )
            return

        if amount <= 0:
            await update.message.reply_text(
                "❌ Summa 0 dan katta bo'lsin."
            )
            return

        tg_id = context.user_data.get(
            "admin_target_user"
        )

        ensure_user_id(tg_id)

        change_balance(
            tg_id,
            amount,
            "admin_add",
            "Admin balans qo'shdi",
        )

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ User: {tg_id}\n"
            f"➕ Qo'shildi: {money(amount)} so'm",
            reply_markup=admin_keyboard(),
        )

        return

    # --------------------------------------------------------
    # ADMIN SUB USER
    # --------------------------------------------------------

    if state == "admin_sub_user":

        try:
            tg_id = int(
                update.message.text.strip()
            )

        except ValueError:
            await update.message.reply_text(
                "❌ Telegram ID raqam bo'lishi kerak."
            )
            return

        ensure_user_id(tg_id)

        context.user_data[
            "admin_target_user"
        ] = tg_id

        context.user_data[
            "state"
        ] = "admin_sub_amount"

        await update.message.reply_text(
            "💰 Ayiriladigan summani yuboring:"
        )

        return

    # --------------------------------------------------------
    # ADMIN SUB AMOUNT
    # --------------------------------------------------------

    if state == "admin_sub_amount":

        try:
            amount = Decimal(
                update.message.text
                .strip()
                .replace(" ", "")
            )

        except InvalidOperation:
            await update.message.reply_text(
                "❌ Summa noto'g'ri."
            )
            return

        if amount <= 0:
            await update.message.reply_text(
                "❌ Summa 0 dan katta bo'lsin."
            )
            return

        tg_id = context.user_data.get(
            "admin_target_user"
        )

        current = get_balance(
            tg_id
        )

        if current < amount:
            await update.message.reply_text(
                f"❌ Balans yetarli emas.\n\n"
                f"💎 Hozirgi balans: "
                f"{money(current)} so'm"
            )
            return

        change_balance(
            tg_id,
            -amount,
            "admin_sub",
            "Admin balans ayirdi",
        )

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ User: {tg_id}\n"
            f"➖ Ayirildi: {money(amount)} so'm",
            reply_markup=admin_keyboard(),
        )

        return

    # --------------------------------------------------------
    # BROADCAST
    # --------------------------------------------------------

    if state == "admin_broadcast":

        text = update.message.text

        con = get_db()
        cur = con.cursor()

        cur.execute(
            "SELECT tg_id FROM users"
        )

        users = [
            x[0]
            for x in cur.fetchall()
        ]

        con.close()

        ok = 0

        for tg_id in users:
            try:
                await context.bot.send_message(
                    tg_id,
                    text,
                )

                ok += 1

                await asyncio.sleep(
                    0.04
                )

            except Exception:
                pass

        context.user_data.clear()

        await update.message.reply_text(
            f"📢 Reklama tugadi.\n\n"
            f"✅ Yetib bordi: {ok}\n"
            f"👥 Jami: {len(users)}",
            reply_markup=admin_keyboard(),
        )

        return

    # --------------------------------------------------------
    # ADMIN PROMO
    # --------------------------------------------------------

    if state == "admin_promo":

        parts = (
            update.message.text
            .strip()
            .split()
        )

        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Format:\n\n"
                "KOD SUMMA LIMIT\n\n"
                "Misol:\n"
                "PROMO50 50000 10"
            )
            return

        code = parts[0].upper()

        try:
            amount = Decimal(
                parts[1]
            )

            max_uses = (
                int(parts[2])
                if len(parts) > 2
                else 1
            )

        except Exception:
            await update.message.reply_text(
                "❌ Ma'lumot noto'g'ri."
            )
            return

        if (
            amount <= 0
            or max_uses <= 0
        ):
            await update.message.reply_text(
                "❌ Summa va limit 0 dan katta bo'lsin."
            )
            return

        con = get_db()
        cur = con.cursor()

        try:
            cur.execute(
                """
                INSERT INTO promo_codes(
                    code,
                    amount,
                    max_uses,
                    active
                )
                VALUES(?,?,?,1)
                """,
                (
                    code,
                    float(amount),
                    max_uses,
                ),
            )

            con.commit()

        except sqlite3.IntegrityError:
            con.close()

            await update.message.reply_text(
                "❌ Bu promo allaqachon mavjud."
            )
            return

        con.close()

        context.user_data.clear()

        await update.message.reply_text(
            f"✅ Promo yaratildi!\n\n"
            f"🎟 Kod: {code}\n"
            f"💰 Summa: {money(amount)} so'm\n"
            f"👥 Limit: {max_uses}",
            reply_markup=admin_keyboard(),
        )

        return


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    ensure_user(update.effective_user)

    text = update.message.text.strip()
    state = context.user_data.get("state")

    # ========================================================
    # ASOSIY MENYU
    # ========================================================

    if text == "🆘 Yordam":
        context.user_data.clear()

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👨‍💻 Admin bilan bog'lanish",
                    url="https://t.me/donuz1",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Asosiy menyu",
                    callback_data="back_main",
                )
            ],
        ])

        await update.message.reply_text(
            "🆘 YORDAM\n\n"
            "Savol yoki muammo bo‘lsa, admin bilan bog‘laning.\n\n"
            "👨‍💻 Admin: @donuz1",
            reply_markup=keyboard,
        )
        return

    if text in (
        "💎 Telegram Premium",
        "💎 Telegram Premium oylik",
    ):
        context.user_data.clear()
        await telegram_premium_menu(update, context)
        return

    if text == "⭐ Telegram Stars":
        context.user_data.clear()
        await telegram_stars_menu(update, context)
        return

    if text == "💎 Mening balansim":
        context.user_data.clear()
        await balance(update, context)
        return

    if text == "💳 Balans to'ldirish":
        context.user_data.clear()
        await deposit_start(update, context)
        return

    if text == "📦 Buyurtmalarim":
        context.user_data.clear()
        await my_orders(update, context)
        return

    if text == "🎟 Promokod":
        context.user_data.clear()
        await promo_start(update, context)
        return

    # ========================================================
    # ADMIN STATES
    # ========================================================

    if is_admin(update.effective_user.id):

        admin_states = {
            "admin_deposit_amount",
            "admin_add_user",
            "admin_add_amount",
            "admin_sub_user",
            "admin_sub_amount",
            "admin_broadcast",
            "admin_promo",
        }

        if state in admin_states:
            await process_admin_text(update, context)
            return

    # ========================================================
    # USER STATES
    # ========================================================

    if state == "deposit_amount":
        await deposit_amount(update, context)
        return

    if state == "player":
        await process_player(update, context)
        return

    if state == "promo":
        await process_promo(update, context)
        return

    # ========================================================
    # NOTANISH MATN
    # ========================================================

    await update.message.reply_text(
        "Kerakli bo‘limni tanlang 👇",
        reply_markup=main_keyboard(),
    )

    # ========================================================
    # ASOSIY MENYU — AVVAL TEKSHIRILADI
    # ========================================================

    if text == "🆘 Yordam":
        context.user_data.clear()

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👨‍💻 Admin bilan bog'lanish",
                    url="https://t.me/donuz1"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Asosiy menyu",
                    callback_data="back_main"
                )
            ],
        ])

        await update.message.reply_text(
            "🆘 YORDAM\n\n"
            "Savol yoki muammo bo‘lsa, admin bilan bog‘laning.\n\n"
            "👨‍💻 Admin: @donuz1",
            reply_markup=keyboard,
        )
        return

    if text in (
        "💎 Telegram Premium",
        "💎 Telegram Premium oylik",
    ):
        context.user_data.clear()
        await telegram_premium_menu(update, context)
        return

    if text == "⭐ Telegram Stars":
        context.user_data.clear()
        await telegram_stars_menu(update, context)
        return

    if text == "💎 Mening balansim":
        context.user_data.clear()
        await balance(update, context)
        return

    if text == "💳 Balans to'ldirish":
        context.user_data.clear()
        await deposit_start(update, context)
        return

    if text == "📦 Buyurtmalarim":
        context.user_data.clear()
        await my_orders(update, context)
        return

    if text == "🎟 Promokod":
        context.user_data.clear()
        await promo_start(update, context)
        return

    # ========================================================
    # ADMIN STATE
    # ========================================================

    if is_admin(update.effective_user.id):

        admin_states = {
            "admin_deposit_amount",
            "admin_add_user",
            "admin_add_amount",
            "admin_sub_user",
            "admin_sub_amount",
            "admin_broadcast",
            "admin_promo",
        }

        if state in admin_states:
            await process_admin_text(update, context)
            return

    # ========================================================
    # USER STATE
    # ========================================================

    if state == "deposit_amount":
        await deposit_amount(update, context)
        return

    if state == "player":
        await process_player(update, context)
        return

    if state == "promo":
        await process_promo(update, context)
        return

    # ========================================================
    # NOTANISH MATN
    # ========================================================

    await update.message.reply_text(
        "Kerakli bo‘limni tanlang 👇",
        reply_markup=main_keyboard(),
    )
    # --------------------------------------------------------
    # ADMIN STATES
    # --------------------------------------------------------

    if is_admin(
        update.effective_user.id
    ):

        admin_states = {
            "admin_deposit_amount",
            "admin_add_user",
            "admin_add_amount",
            "admin_sub_user",
            "admin_sub_amount",
            "admin_broadcast",
            "admin_promo",
        }

        if state in admin_states:
            await process_admin_text(
                update,
                context,
            )
            return

    # --------------------------------------------------------
    # USER STATES
    # --------------------------------------------------------

    if state == "deposit_amount":
        await deposit_amount(
            update,
            context,
        )
        return

    if state == "player":
        await process_player(
            update,
            context,
        )
        return

    if state == "promo":
        await process_promo(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # MAIN BUTTONS
    # --------------------------------------------------------

    if text in (
        "💎 Telegram Premium",
        "💎 Telegram Premium oylik",
    ):
        await telegram_premium_menu(
            update,
            context,
        )
        return

    if text == "⭐ Telegram Stars":
        await telegram_stars_menu(
            update,
            context,
        )
        return

    if text == "💎 Mening balansim":
        await balance(
            update,
            context,
        )
        return

    if text == "💳 Balans to'ldirish":
        await deposit_start(
            update,
            context,
        )
        return

    if text == "📦 Buyurtmalarim":
        await my_orders(
            update,
            context,
        )
        return

    if text == "🎟 Promokod":
        await promo_start(
            update,
            context,
        )
        return

    await update.message.reply_text(
        "Kerakli bo'limni tanlang.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# PHOTO / DOCUMENT
# ============================================================

async def photo_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    ensure_user(
        update.effective_user
    )

    state = context.user_data.get(
        "state"
    )

    if state == "deposit_receipt":
        await deposit_receipt(
            update,
            context,
        )
        return

    await update.message.reply_text(
        "❌ Hozir rasm kerak emas."
    )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context,
):
    log.exception(
        "Unhandled error",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        print("❌ BOT_TOKEN ni yozing.")
        return

    if not ADMIN_ID:
        print("❌ ADMIN_ID ni yozing.")
        return

    if not COINDROP_API_KEY:
        print("❌ COINDROP_API_KEY ni yozing.")
        return

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # ========================================================
    # COMMANDS
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    # ========================================================
    # DEPOSIT
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            deposit_callback,
            pattern=r"^dep_(accept|reject):\d+$",
        )
    )

    # ========================================================
    # ADMIN
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            admin_api_balance_callback,
            pattern=r"^admin_api_balance$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_refresh_catalog_callback,
            pattern=r"^admin_refresh_catalog$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_stats_callback,
            pattern=r"^admin_stats$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_income_callback,
            pattern=r"^admin_income$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_users_callback,
            pattern=r"^admin_users$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_add_callback,
            pattern=r"^admin_add$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_sub_callback,
            pattern=r"^admin_sub$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_broadcast_callback,
            pattern=r"^admin_broadcast$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_promo_callback,
            pattern=r"^admin_promo$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_profit_callback,
            pattern=r"^admin_profit$",
        )
    )

    # ========================================================
    # PREMIUM / STARS PRODUCTS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            product_page_callback,
            pattern=r"^page:\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            product_callback,
            pattern=r"^product:\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            back_main_callback,
            pattern=r"^back_main$",
        )
    )

    # ========================================================
    # ORDER
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            confirm_order_callback,
            pattern=r"^confirm_order$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_order_callback,
            pattern=r"^cancel_order$",
        )
    )

    # ========================================================
    # RECEIPT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_router,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            photo_router,
        )
    )

    # ========================================================
    # TEXT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("=" * 50)
    print(" COINDROP TELEGRAM BOT")
    print(" PREMIUM + STARS")
    print(" BOT ISHLAYAPTI")
    print("=" * 50)
    print()
    print("Database:")
    print(DB_FILE)
    print()

    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
