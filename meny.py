import sqlite3
import logging
import uuid
import json
from decimal import Decimal, InvalidOperation
from datetime import datetime

import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,  # <--- Mana buni qo'shasiz
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# SOZLAMALAR
# =========================================================

BOT_TOKEN = ""

ADMIN_ID = 5692925792

FAZER_API_KEY = "fc_e2a3d96eda3c7f0bd6b4a139"

FAZER_BASE = "https://api.fzr.cards/api/v2"

# 1 USD = qancha so'm
USD_UZS = Decimal("11800")

# Barcha o'yinlarga 20% ustama
MARKUP_PERCENT = Decimal("20")

DB = "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = conn()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        balance REAL DEFAULT 0,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS payments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        amount REAL,
        photo_id TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS products(
        category_id TEXT,
        offer_id TEXT,
        game_name TEXT,
        offer_name TEXT,
        price_usd REAL DEFAULT 0,
        sale_price REAL,
        active INTEGER DEFAULT 1,
        updated_at TEXT,
        PRIMARY KEY(category_id, offer_id)
    );

    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        fazer_order_id TEXT,
        category_id TEXT,
        offer_id TEXT,
        product_name TEXT,
        fields_json TEXT,
        cost_usd REAL,
        sale_price REAL,
        status TEXT,
        created_at TEXT,
        notified TEXT DEFAULT '0'
    );

    CREATE TABLE IF NOT EXISTS promo_codes(
        code TEXT PRIMARY KEY,
        percent REAL,
        max_uses INTEGER DEFAULT 0,
        used INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS promo_users(
        user_id INTEGER,
        code TEXT,
        PRIMARY KEY(user_id, code)
    );
    """)

    c.commit()
    c.close()


def ensure_user(user):
    c = conn()

    c.execute(
        """
        INSERT OR IGNORE INTO users
        (user_id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            user.id,
            user.username or "",
            user.first_name or "",
            datetime.now().isoformat()
        )
    )

    c.execute(
        """
        UPDATE users
        SET username=?, first_name=?
        WHERE user_id=?
        """,
        (
            user.username or "",
            user.first_name or "",
            user.id
        )
    )

    c.commit()
    c.close()


def get_balance(uid):
    c = conn()

    r = c.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (uid,)
    ).fetchone()

    c.close()

    if not r:
        return Decimal("0")

    return Decimal(str(r["balance"] or 0))


def add_balance(uid, amount):
    c = conn()

    c.execute(
        """
        UPDATE users
        SET balance = balance + ?
        WHERE user_id=?
        """,
        (float(amount), uid)
    )

    c.commit()
    c.close()


# =========================================================
# FAZERCARDS API
# =========================================================

def headers():
    return {
        "X-API-Key": FAZER_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def api_get(path, params=None):
    try:
        r = requests.get(
            FAZER_BASE + path,
            headers=headers(),
            params=params,
            timeout=30
        )

        try:
            data = r.json()
        except Exception:
            data = {
                "ok": False,
                "error": r.text
            }

        return r.status_code, data

    except Exception as e:
        log.error("GET API xato: %s", e)

        return 0, {
            "ok": False,
            "error": str(e)
        }


def api_post(path, body):
    try:
        h = {
            **headers(),
            "Idempotency-Key": str(uuid.uuid4())
        }

        r = requests.post(
            FAZER_BASE + path,
            headers=h,
            json=body,
            timeout=30
        )

        try:
            data = r.json()
        except Exception:
            data = {
                "ok": False,
                "error": r.text
            }

        return r.status_code, data

    except Exception as e:
        log.error("POST API xato: %s", e)

        return 0, {
            "ok": False,
            "error": str(e)
        }


# =========================================================
# KATEGORIYALAR
# =========================================================

def get_all_categories():

    all_items = []
    cursor = None

    max_pages = 100

    for _ in range(max_pages):

        params = {
            "limit": 50
        }

        if cursor:
            params["cursor"] = cursor

        status, data = api_get(
            "/topups",
            params
        )

        if status != 200 or not data.get("ok"):
            log.error(
                "Kategoriyalar API xatosi: %s",
                data
            )
            break

        items = data.get(
            "items",
            []
        )

        all_items.extend(items)

        meta = data.get(
            "meta",
            {}
        )

        next_cursor = meta.get(
            "next_cursor"
        )

        has_more = meta.get(
            "has_more",
            False
        )

        if not has_more or not next_cursor:
            break

        if next_cursor == cursor:
            break

        cursor = next_cursor

    result = []
    seen = set()

    for item in all_items:

        cid = item.get(
            "category_id"
        )

        if not cid:
            continue

        if cid in seen:
            continue

        seen.add(cid)
        result.append(item)

    return result


def find_game_categories(categories):

    priority = [
        "pubg",
        "free fire",
        "roblox",
    ]

    def score(item):

        name = str(
            item.get("name", "")
        ).lower()

        for i, word in enumerate(priority):

            if word in name:
                return i

        return 100

    return sorted(
        categories,
        key=score
    )


# =========================================================
# OFFERS
# =========================================================

def get_offers(cid):

    status, data = api_get(
        "/topups/offers",
        {
            "category_id": cid
        }
    )

    if status != 200:
        return None

    if not data.get("ok"):
        return None

    return data


# =========================================================
# PRICE
# =========================================================

def local_price(cid, oid, usd):

    usd = Decimal(str(usd))

    # API USD narxini so'mga aylantirish
    base_price = usd * USD_UZS

    # 20% ustama
    return base_price * Decimal("1.20")


def save_product(
    cid,
    oid,
    gname,
    oname,
    usd
):

    price = local_price(
        cid,
        oid,
        usd
    )

    c = conn()

    c.execute(
        """
        INSERT OR REPLACE INTO products
        (
            category_id,
            offer_id,
            game_name,
            offer_name,
            price_usd,
            sale_price,
            active,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            cid,
            oid,
            gname,
            oname,
            float(usd),
            float(price),
            datetime.now().isoformat()
        )
    )

    c.commit()
    c.close()


# =========================================================
# MENU
# =========================================================

def main_menu():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🛒 Buyurtma berish",
                callback_data="games"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 Balans to'ldirish",
                callback_data="deposit"
            )
        ],
        [
            InlineKeyboardButton(
                "📦 Buyurtmalarim",
                callback_data="orders"
            )
        ],
        [
            InlineKeyboardButton(
                "🎁 Promo kod",
                callback_data="promo"
            ),
            InlineKeyboardButton(
                "👤 Profil",
                callback_data="profile"
            )
        ]
    ])


# =========================================================
# BOT MENU'NI O'CHIRISH
# =========================================================

async def remove_bot_menu(app):

    try:

        await app.bot.set_my_commands([])

        log.info(
            "Bot command menu tozalandi."
        )

    except Exception as e:

        log.error(
            "Bot menu xatosi: %s",
            e
        )


# =========================================================
# START
# =========================================================

async def start(update, context):

    ensure_user(
        update.effective_user
    )

    await update.message.reply_text(
        "Assalomu Aleykum 👋\n\n"
        "🎮 O'yin donat botiga xush kelibsiz!",
        reply_markup=main_menu()
    )


# =========================================================
# GAMES
# =========================================================

async def games(update, context):

    q = update.callback_query

    categories = get_all_categories()

    if not categories:

        await q.message.reply_text(
            "❌ O'yinlar olinmadi.\n\n"
            "Sabablar:\n"
            "• API key noto'g'ri\n"
            "• FazerCards katalogi yopiq\n"
            "• Internet/API ishlamayapti"
        )

        return

    categories = find_game_categories(
        categories
    )

    chunks = [
        categories[i:i + 50]
        for i in range(
            0,
            len(categories),
            50
        )
    ]

    first = chunks[0] if chunks else []

    kb = []

    for item in first:

        cid = item.get(
            "category_id"
        )

        name = item.get(
            "name",
            cid
        )

        if not cid:
            continue

        kb.append([
            InlineKeyboardButton(
                "🎮 " + str(name)[:55],
                callback_data="g:" + cid
            )
        ])

    if len(chunks) > 1:

        kb.append([
            InlineKeyboardButton(
                "➡️ Keyingi o'yinlar",
                callback_data="games2"
            )
        ])

        context.user_data[
            "categories"
        ] = categories

    await q.message.reply_text(
        f"🎮 O'yinni tanlang:\n\n"
        f"Jami mavjud kategoriya: "
        f"{len(categories)} ta",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# =========================================================
# GAME PAGES
# =========================================================

async def game_page(update, context):

    q = update.callback_query

    try:

        page = int(
            q.data.replace(
                "games",
                ""
            )
        )

    except Exception:

        page = 2

    categories = context.user_data.get(
        "categories"
    )

    if not categories:

        categories = get_all_categories()

    start_index = (
        page - 1
    ) * 50

    items = categories[
        start_index:start_index + 50
    ]

    if not items:

        await q.message.reply_text(
            "❌ Boshqa o'yin topilmadi."
        )

        return

    kb = []

    for item in items:

        cid = item.get(
            "category_id"
        )

        name = item.get(
            "name",
            cid
        )

        if cid:

            kb.append([
                InlineKeyboardButton(
                    "🎮 " + str(name)[:55],
                    callback_data="g:" + cid
                )
            ])

    nav = []

    if page > 2:

        nav.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"games{page-1}"
            )
        )

    if start_index + 50 < len(categories):

        nav.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"games{page+1}"
            )
        )

    if nav:
        kb.append(nav)

    await q.message.reply_text(
        f"🎮 O'yinlar — {page}-sahifa",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# =========================================================
# GAME OFFERS
# =========================================================

async def game(update, context):

    q = update.callback_query

    cid = q.data[2:]

    data = get_offers(cid)

    if not data:

        await q.message.reply_text(
            "❌ Bu o'yinning paketlarini "
            "olib bo'lmadi."
        )

        return

    game_name = data.get(
        "name",
        cid
    )

    fields = data.get(
        "fields",
        []
    )

    context.user_data[
        "category_id"
    ] = cid

    context.user_data[
        "game_name"
    ] = game_name

    context.user_data[
        "fields"
    ] = fields

    kb = []

    offers = data.get(
        "offers",
        []
    )

    for off in offers:

        oid = off.get(
            "offer_id"
        )

        if not oid:
            continue

        name = off.get(
            "name",
            "Paket"
        )

        usd = Decimal(
            str(
                off.get(
                    "price_usd",
                    "0"
                )
            )
        )

        save_product(
            cid,
            oid,
            game_name,
            name,
            usd
        )

        price = local_price(
            cid,
            oid,
            usd
        )

        kb.append([
            InlineKeyboardButton(
                f"{name} — "
                f"{price:,.0f} so'm",
                callback_data=(
                    f"o:{cid}:{oid}"
                )
            )
        ])

    if not kb:

        await q.message.reply_text(
            "❌ Bu o'yinda hozircha "
            "paket mavjud emas."
        )

        return

    await q.message.reply_text(
        f"🎮 {game_name}\n\n"
        "📦 Paketni tanlang:",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# =========================================================
# OFFER
# =========================================================

async def offer(update, context):

    q = update.callback_query

    parts = q.data.split(
        ":",
        2
    )

    if len(parts) != 3:

        await q.message.reply_text(
            "❌ Paket ma'lumotida xato."
        )

        return

    _, cid, oid = parts

    data = get_offers(cid)

    if not data:

        await q.message.reply_text(
            "❌ API xatosi."
        )

        return

    off = next(
        (
            x
            for x in data.get(
                "offers",
                []
            )
            if x.get(
                "offer_id"
            ) == oid
        ),
        None
    )

    if not off:

        await q.message.reply_text(
            "❌ Paket topilmadi."
        )

        return

    usd = Decimal(
        str(
            off.get(
                "price_usd",
                "0"
            )
        )
    )

    price = local_price(
        cid,
        oid,
        usd
    )

    fields = data.get(
        "fields",
        []
    )

    context.user_data.update({
        "category_id": cid,
        "offer_id": oid,
        "offer_name": off.get(
            "name",
            "Paket"
        ),
        "price": price,
        "fields": fields,
        "field_values": {},
        "input_index": 0,
        "state": "order_fields",
        "promo_code": None,
    })

    await ask_next_field(
        q.message,
        context
    )


# =========================================================
# ASK FIELD
# =========================================================

async def ask_next_field(
    message,
    context
):

    fields = context.user_data.get(
        "fields",
        []
    )

    index = context.user_data.get(
        "input_index",
        0
    )

    if index >= len(fields):

        await confirm_order(
            message,
            context
        )

        return

    field = fields[index]

    key = field.get(
        "key",
        "field"
    )

    label = field.get(
        "label",
        key
    )

    context.user_data[
        "state"
    ] = "order_fields"

    await message.reply_text(
        f"🎮 {label}\n\n"
        "👇 Ma'lumotni yuboring:"
    )


# =========================================================
# CONFIRM ORDER
# =========================================================

async def confirm_order(
    message,
    context
):

    base_price = Decimal(
        str(
            context.user_data[
                "price"
            ]
        )
    )

    promo = context.user_data.get(
        "promo_code"
    )

    discount = Decimal("0")

    if promo:

        c = conn()

        r = c.execute(
            """
            SELECT *
            FROM promo_codes
            WHERE code=?
            AND active=1
            """,
            (promo,)
        ).fetchone()

        c.close()

        if r:

            allowed = (
                r["max_uses"] == 0
                or r["used"] < r["max_uses"]
            )

            if allowed:

                discount = (
                    base_price *
                    Decimal(
                        str(r["percent"])
                    ) /
                    Decimal("100")
                )

    final_price = max(
        Decimal("0"),
        base_price - discount
    )

    context.user_data[
        "final_price"
    ] = final_price

    values = context.user_data.get(
        "field_values",
        {}
    )

    lines = ""

    for key, value in values.items():

        lines += (
            f"• {key}: {value}\n"
        )

    discount_text = ""

    if discount > 0:

        discount_text = (
            f"🎁 Chegirma: "
            f"{discount:,.0f} so'm\n"
        )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Tasdiqlash",
                callback_data="confirm"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Bekor qilish",
                callback_data="cancel"
            )
        ]
    ])

    await message.reply_text(
        f"📦 {context.user_data['offer_name']}\n\n"
        f"💰 Narx: "
        f"{base_price:,.0f} so'm\n"
        f"{discount_text}"
        f"💵 Jami: "
        f"{final_price:,.0f} so'm\n\n"
        f"{lines}\n"
        "Buyurtmani tasdiqlaysizmi?",
        reply_markup=kb
    )


# =========================================================
# CONFIRM
# =========================================================

async def confirm(update, context):

    q = update.callback_query

    uid = q.from_user.id

    price = Decimal(
        str(
            context.user_data.get(
                "final_price",
                0
            )
        )
    )

    current_balance = get_balance(uid)

    if current_balance < price:

        await q.message.reply_text(
            "❌ Balans yetarli emas.\n\n"
            f"💰 Balans: "
            f"{current_balance:,.0f} so'm\n"
            f"💵 Kerak: "
            f"{price:,.0f} so'm\n\n"
            "💳 Avval balansni to'ldiring."
        )

        return

    cid = context.user_data.get(
        "category_id"
    )

    oid = context.user_data.get(
        "offer_id"
    )

    fields = context.user_data.get(
        "field_values",
        {}
    )

    body = {
        "category_id": cid,
        "offer_id": oid,
        "fields": fields
    }

    status_code, data = api_post(
        "/topups/order",
        body
    )

    if status_code not in (
        200,
        201
    ) or not data.get("ok"):

        await q.message.reply_text(
            "❌ Buyurtma yuborilmadi.\n\n"
            + str(
                data.get(
                    "error",
                    "FazerCards API xatosi"
                )
            )
        )

        return

    order = data.get(
        "order",
        {}
    )

    fazer_id = order.get(
        "id",
        ""
    )

    order_status = order.get(
        "status",
        "processing"
    )

    add_balance(
        uid,
        -price
    )

    c = conn()

    c.execute(
        """
        INSERT INTO orders
        (
            user_id,
            fazer_order_id,
            category_id,
            offer_id,
            product_name,
            fields_json,
            cost_usd,
            sale_price,
            status,
            created_at,
            notified
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            fazer_id,
            cid,
            oid,
            context.user_data.get(
                "offer_name",
                "Paket"
            ),
            json.dumps(
                fields,
                ensure_ascii=False
            ),
            0,
            float(price),
            order_status,
            datetime.now().isoformat(),
            "0"
        )
    )

    c.commit()
    c.close()

    promo = context.user_data.get(
        "promo_code"
    )

    if promo:

        c = conn()

        c.execute(
            """
            INSERT OR IGNORE INTO promo_users
            (user_id, code)
            VALUES (?, ?)
            """,
            (
                uid,
                promo
            )
        )

        c.execute(
            """
            UPDATE promo_codes
            SET used=used+1
            WHERE code=?
            """,
            (promo,)
        )

        c.commit()
        c.close()

    await q.message.reply_text(
        "✅ BUYURTMA YUBORILDI!\n\n"
        f"🎮 {context.user_data.get('game_name', '')}\n"
        f"📦 {context.user_data.get('offer_name', '')}\n"
        f"💰 {price:,.0f} so'm\n"
        f"🆔 {fazer_id}\n"
        f"📊 Status: {order_status}"
    )

    try:

        await context.bot.send_message(
            ADMIN_ID,
            "🛒 YANGI BUYURTMA\n\n"
            f"👤 User: {uid}\n"
            f"🎮 {context.user_data.get('game_name', '')}\n"
            f"📦 {context.user_data.get('offer_name', '')}\n"
            f"💰 {price:,.0f} so'm\n"
            f"🆔 Fazer ID: {fazer_id}\n"
            f"📊 {order_status}"
        )

    except Exception as e:

        log.error(
            "Admin xabarida xato: %s",
            e
        )

    context.user_data.clear()


# =========================================================
# CANCEL
# =========================================================

async def cancel(update, context):

    q = update.callback_query

    context.user_data.clear()

    await q.message.reply_text(
        "❌ Buyurtma bekor qilindi.",
        reply_markup=main_menu()
    )


# =========================================================
# BALANCE
# =========================================================

async def balance_cb(update, context):

    q = update.callback_query

    uid = q.from_user.id

    await q.message.reply_text(
        f"💰 Balansingiz:\n\n"
        f"{get_balance(uid):,.0f} so'm"
    )


# =========================================================
# DEPOSIT
# =========================================================

async def deposit(update, context):

    q = update.callback_query

    context.user_data[
        "state"
    ] = "deposit_amount"

    await q.message.reply_text(
        "💳 Balans to'ldirish\n\n"
        "Qancha summa to'ldirasiz?\n\n"
        "Masalan:\n"
        "50000"
    )


# =========================================================
# PROFILE
# =========================================================

async def profile(update, context):

    q = update.callback_query

    uid = q.from_user.id

    c = conn()

    row = c.execute(
        """
        SELECT COUNT(*) AS n
        FROM orders
        WHERE user_id=?
        """,
        (uid,)
    ).fetchone()

    c.close()

    await q.message.reply_text(
        "👤 PROFIL\n\n"
        f"🆔 ID: {uid}\n"
        f"💰 Balans: "
        f"{get_balance(uid):,.0f} so'm\n"
        f"📦 Buyurtmalar: {row['n']}"
    )


# =========================================================
# ORDERS
# =========================================================

async def orders_cb(update, context):

    q = update.callback_query

    uid = q.from_user.id

    c = conn()

    rows = c.execute(
        """
        SELECT *
        FROM orders
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT 10
        """,
        (uid,)
    ).fetchall()

    c.close()

    if not rows:

        await q.message.reply_text(
            "📦 Hali buyurtmalar yo'q."
        )

        return

    text = "📦 OXIRGI BUYURTMALAR\n\n"

    for r in rows:

        text += (
            f"#{r['id']} "
            f"{r['product_name']}\n"
            f"💰 {r['sale_price']:,.0f} so'm\n"
            f"📊 {r['status']}\n"
            f"🆔 {r['fazer_order_id']}\n\n"
        )

    await q.message.reply_text(
        text
    )


# =========================================================
# PROMO
# =========================================================

async def promo_cb(update, context):

    q = update.callback_query

    context.user_data[
        "state"
    ] = "promo"

    await q.message.reply_text(
        "🎁 Promo kodni yuboring:"
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(update, context):

    user = update.effective_user

    ensure_user(user)

    text = (
        update.message.text or ""
    ).strip()

    state = context.user_data.get(
        "state"
    )

    if (
        user.id == ADMIN_ID
        and context.user_data.get(
            "admin_state"
        )
    ):

        await admin_text(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # DEPOSIT
    # -----------------------------------------------------

    if state == "deposit_amount":

        try:

            amount = Decimal(
                text.replace(",", "")
                .replace(" ", "")
            )

        except InvalidOperation:

            await update.message.reply_text(
                "❌ Summani raqamda yuboring."
            )

            return

        if amount <= 0:

            await update.message.reply_text(
                "❌ Summa 0 dan katta bo'lishi kerak."
            )

            return

        context.user_data.update({
            "state": "waiting_receipt",
            "deposit_amount": float(amount)
        })

        await update.message.reply_text(
            f"💳 {amount:,.0f} so'm to'lang.\n\n"
            "9860 6067 6078 9275 A.Abdurasul "
            "amalga oshiring.\n\n"
            "Keyin chekni 📸 shu botga yuboring."
        )

        return

    # -----------------------------------------------------
    # PROMO
    # -----------------------------------------------------

    if state == "promo":

        code = text.upper()

        c = conn()

        row = c.execute(
            """
            SELECT *
            FROM promo_codes
            WHERE code=?
            AND active=1
            """,
            (code,)
        ).fetchone()

        c.close()

        if not row:

            await update.message.reply_text(
                "❌ Promo kod noto'g'ri."
            )

            return

        if (
            row["max_uses"] > 0
            and row["used"] >= row["max_uses"]
        ):

            await update.message.reply_text(
                "❌ Promo kod limiti tugagan."
            )

            return

        c = conn()

        used = c.execute(
            """
            SELECT 1
            FROM promo_users
            WHERE user_id=?
            AND code=?
            """,
            (
                user.id,
                code
            )
        ).fetchone()

        c.close()

        if used:

            await update.message.reply_text(
                "❌ Bu promo koddan "
                "allaqachon foydalangansiz."
            )

            return

        context.user_data[
            "promo_code"
        ] = code

        context.user_data[
            "state"
        ] = None

        await update.message.reply_text(
            f"✅ Promo kod qabul qilindi!\n\n"
            f"🎁 {code}\n"
            f"💸 Chegirma: "
            f"{row['percent']}%"
        )

        return

    # -----------------------------------------------------
    # ORDER FIELDS
    # -----------------------------------------------------

    if state == "order_fields":

        fields = context.user_data.get(
            "fields",
            []
        )

        index = context.user_data.get(
            "input_index",
            0
        )

        if index >= len(fields):

            await confirm_order(
                update.message,
                context
            )

            return

        field = fields[index]

        key = field.get(
            "key",
            f"field_{index}"
        )

        context.user_data[
            "field_values"
        ][key] = text

        context.user_data[
            "input_index"
        ] = index + 1

        await ask_next_field(
            update.message,
            context
        )

        return


# =========================================================
# PHOTO / RECEIPT
# =========================================================

async def photo_handler(update, context):

    user = update.effective_user

    ensure_user(user)

    if context.user_data.get(
        "state"
    ) != "waiting_receipt":

        return

    amount = context.user_data.get(
        "deposit_amount"
    )

    if not amount:
        return

    photo = update.message.photo[-1]

    photo_id = photo.file_id

    c = conn()

    cur = c.execute(
        """
        INSERT INTO payments
        (
            user_id,
            amount,
            photo_id,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            user.id,
            amount,
            photo_id,
            datetime.now().isoformat()
        )
    )

    payment_id = cur.lastrowid

    c.commit()
    c.close()

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Tasdiqlash",
                callback_data=f"payok:{payment_id}"
            ),
            InlineKeyboardButton(
                "❌ Rad etish",
                callback_data=f"payno:{payment_id}"
            )
        ]
    ])

    await update.message.reply_text(
        "✅ Chek adminga yuborildi.\n\n"
        "Tekshiruvdan so'ng balansingiz "
        "to'ldiriladi."
    )

    await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=photo_id,
        caption=(
            "💳 YANGI TO'LOV\n\n"
            f"🧾 #{payment_id}\n"
            f"👤 User: {user.id}\n"
            f"💰 Summa: {amount:,.0f} so'm"
        ),
        reply_markup=kb
    )

    context.user_data.clear()


# =========================================================
# PAYMENT ACTION
# =========================================================

async def payment_action(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != ADMIN_ID:
        return

    action, pid = q.data.split(":")

    pid = int(pid)

    c = conn()

    row = c.execute(
        """
        SELECT *
        FROM payments
        WHERE id=?
        """,
        (pid,)
    ).fetchone()

    if not row:

        c.close()

        await q.message.reply_text(
            "❌ To'lov topilmadi."
        )

        return

    if row["status"] != "pending":

        c.close()

        await q.message.reply_text(
            "⚠️ Bu to'lov allaqachon ko'rilgan."
        )

        return

    if action == "payok":

        c.execute(
            """
            UPDATE payments
            SET status='approved'
            WHERE id=?
            """,
            (pid,)
        )

        c.commit()
        c.close()

        add_balance(
            row["user_id"],
            Decimal(
                str(row["amount"])
            )
        )

        await context.bot.send_message(
            row["user_id"],
            "✅ TO'LOV TASDIQLANDI!\n\n"
            f"💰 +{row['amount']:,.0f} so'm\n"
            f"💳 Yangi balans: "
            f"{get_balance(row['user_id']):,.0f} so'm"
        )

        await q.message.reply_text(
            "✅ To'lov tasdiqlandi."
        )

    else:

        c.execute(
            """
            UPDATE payments
            SET status='rejected'
            WHERE id=?
            """,
            (pid,)
        )

        c.commit()
        c.close()

        await context.bot.send_message(
            row["user_id"],
            "❌ To'lovingiz rad etildi."
        )

        await q.message.reply_text(
            "❌ To'lov rad etildi."
        )


# =========================================================
# ADMIN MENU
# =========================================================

def admin_kb():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔄 Katalogni yangilash",
                callback_data="a_sync"
            )
        ],
        [
            InlineKeyboardButton(
                "💰 Narxlarni boshqarish",
                callback_data="a_prices"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Statistika",
                callback_data="a_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "💳 To'lovlar",
                callback_data="a_payments"
            )
        ],
        [
            InlineKeyboardButton(
                "🎁 Promo yaratish",
                callback_data="a_promo"
            )
        ]
    ])


async def admin(update, context):

    if update.effective_user.id != ADMIN_ID:
        return

    await update.message.reply_text(
        "👑 ADMIN PANEL",
        reply_markup=admin_kb()
    )


# =========================================================
# ADMIN CALLBACK
# =========================================================

async def admin_callback(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != ADMIN_ID:
        return

    d = q.data

    if d == "a_sync":

        await q.message.reply_text(
            "🔄 Katalog tekshirilmoqda..."
        )

        categories = get_all_categories()

        await q.message.reply_text(
            "✅ Katalog tekshirildi!\n\n"
            f"🎮 Mavjud kategoriya: "
            f"{len(categories)} ta"
        )

    elif d == "a_prices":

        c = conn()

        rows = c.execute(
            """
            SELECT *
            FROM products
            WHERE active=1
            ORDER BY game_name, offer_name
            LIMIT 50
            """
        ).fetchall()

        c.close()

        if not rows:

            await q.message.reply_text(
                "❌ Mahsulotlar bazada yo'q.\n"
                "Avval o'yinlarni ochib paketlarni yuklang."
            )

            return

        kb = []

        for r in rows:

            text = (
                f"{r['game_name'][:15]} | "
                f"{r['offer_name'][:18]} | "
                f"{r['sale_price']:,.0f}"
            )

            kb.append([
                InlineKeyboardButton(
                    text,
                    callback_data=(
                        f"ap:{r['category_id']}:"
                        f"{r['offer_id']}"
                    )
                )
            ])

        await q.message.reply_text(
            "💰 Narxini o'zgartirish uchun paketni bosing:",
            reply_markup=InlineKeyboardMarkup(kb)
        )

    elif d.startswith("ap:"):

        _, cid, oid = d.split(
            ":",
            2
        )

        c = conn()

        row = c.execute(
            """
            SELECT *
            FROM products
            WHERE category_id=?
            AND offer_id=?
            """,
            (
                cid,
                oid
            )
        ).fetchone()

        c.close()

        if not row:

            await q.message.reply_text(
                "❌ Mahsulot topilmadi."
            )

            return

        context.user_data[
            "admin_state"
        ] = "set_price"

        context.user_data[
            "price_key"
        ] = (
            cid,
            oid
        )

        await q.message.reply_text(
            f"✏️ {row['game_name']}\n"
            f"📦 {row['offer_name']}\n\n"
            f"💰 Hozirgi narx: "
            f"{row['sale_price']:,.0f} so'm\n\n"
            "Yangi narxni yuboring:"
        )

    elif d == "a_stats":

        c = conn()

        income = c.execute(
            """
            SELECT COALESCE(
                SUM(amount),
                0
            ) AS x
            FROM payments
            WHERE status='approved'
            """
        ).fetchone()["x"]

        sales = c.execute(
            """
            SELECT COALESCE(
                SUM(sale_price),
                0
            ) AS x
            FROM orders
            WHERE status NOT IN
            ('failed', 'cancelled')
            """
        ).fetchone()["x"]

        users = c.execute(
            """
            SELECT COUNT(*) AS x
            FROM users
            """
        ).fetchone()["x"]

        orders = c.execute(
            """
            SELECT COUNT(*) AS x
            FROM orders
            """
        ).fetchone()["x"]

        c.close()

        await q.message.reply_text(
            "📊 STATISTIKA\n\n"
            f"💳 Kirim: {income:,.0f} so'm\n"
            f"🛒 Sotuv: {sales:,.0f} so'm\n"
            f"👥 User: {users}\n"
            f"📦 Buyurtmalar: {orders}"
        )

    elif d == "a_payments":

        c = conn()

        rows = c.execute(
            """
            SELECT *
            FROM payments
            WHERE status='pending'
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

        c.close()

        if not rows:

            await q.message.reply_text(
                "✅ Kutilayotgan to'lov yo'q."
            )

            return

        text = "💳 KUTILAYOTGAN TO'LOVLAR\n\n"

        for r in rows:

            text += (
                f"#{r['id']} | "
                f"User {r['user_id']} | "
                f"{r['amount']:,.0f} so'm\n"
            )

        await q.message.reply_text(
            text
        )

    elif d == "a_promo":

        context.user_data[
            "admin_state"
        ] = "promo"

        await q.message.reply_text(
            "🎁 PROMO YARATISH\n\n"
            "Format:\n"
            "KOD FOIZ LIMIT\n\n"
            "Misol:\n"
            "SALE10 10 100\n\n"
            "Limit 0 = cheksiz"
        )


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text(
    update,
    context
):

    state = context.user_data.get(
        "admin_state"
    )

    if state == "set_price":

        try:

            price = Decimal(
                update.message.text
                .replace(",", "")
                .replace(" ", "")
            )

        except InvalidOperation:

            await update.message.reply_text(
                "❌ Narx raqam bo'lishi kerak."
            )

            return

        if price < 0:

            await update.message.reply_text(
                "❌ Narx manfiy bo'lmaydi."
            )

            return

        cid, oid = context.user_data[
            "price_key"
        ]

        c = conn()

        c.execute(
            """
            UPDATE products
            SET sale_price=?,
                updated_at=?
            WHERE category_id=?
            AND offer_id=?
            """,
            (
                float(price),
                datetime.now().isoformat(),
                cid,
                oid
            )
        )

        c.commit()
        c.close()

        context.user_data.pop(
            "admin_state",
            None
        )

        context.user_data.pop(
            "price_key",
            None
        )

        await update.message.reply_text(
            f"✅ Narx o'zgartirildi:\n\n"
            f"💰 {price:,.0f} so'm"
        )

    elif state == "promo":

        parts = (
            update.message.text
            .split()
        )

        if len(parts) != 3:

            await update.message.reply_text(
                "❌ Format:\n"
                "SALE10 10 100"
            )

            return

        code = parts[0].upper()

        try:

            percent = float(
                parts[1]
            )

            limit = int(
                parts[2]
            )

        except ValueError:

            await update.message.reply_text(
                "❌ Foiz va limit raqam bo'lishi kerak."
            )

            return

        if (
            percent <= 0
            or percent > 100
        ):

            await update.message.reply_text(
                "❌ Foiz 1-100 oralig'ida bo'lishi kerak."
            )

            return

        if limit < 0:

            await update.message.reply_text(
                "❌ Limit 0 yoki undan katta bo'lishi kerak."
            )

            return

        c = conn()

        c.execute(
            """
            INSERT OR REPLACE INTO promo_codes
            (
                code,
                percent,
                max_uses,
                used,
                active
            )
            VALUES (?, ?, ?, 0, 1)
            """,
            (
                code,
                percent,
                limit
            )
        )

        c.commit()
        c.close()

        context.user_data.pop(
            "admin_state",
            None
        )

        await update.message.reply_text(
            "✅ PROMO YARATILDI!\n\n"
            f"🎁 Kod: {code}\n"
            f"💸 Chegirma: {percent}%\n"
            f"🔢 Limit: {limit}"
        )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback_router(
    update,
    context
):

    q = update.callback_query

    try:
        await q.answer()
    except Exception:
        pass

    d = q.data

    try:

        if d == "games":

            await games(
                update,
                context
            )

        elif d == "balance":

            await balance_cb(
                update,
                context
            )

        elif d == "deposit":

            await deposit(
                update,
                context
            )

        elif d == "orders":

            await orders_cb(
                update,
                context
            )

        elif d == "profile":

            await profile(
                update,
                context
            )

        elif d == "promo":

            await promo_cb(
                update,
                context
            )

        elif d.startswith("games"):

            await game_page(
                update,
                context
            )

        elif d.startswith("g:"):

            await game(
                update,
                context
            )

        elif d.startswith("o:"):

            await offer(
                update,
                context
            )

        elif d == "confirm":

            await confirm(
                update,
                context
            )

        elif d == "cancel":

            await cancel(
                update,
                context
            )

        elif d.startswith("payok:"):

            await payment_action(
                update,
                context
            )

        elif d.startswith("payno:"):

            await payment_action(
                update,
                context
            )

        elif (
            d.startswith("a_")
            or d.startswith("ap:")
        ):

            await admin_callback(
                update,
                context
            )

        else:

            await q.message.reply_text(
                "❌ Noma'lum knopka."
            )

    except Exception as e:

        log.exception(
            "Callback xatosi"
        )

        try:

            await q.message.reply_text(
                "❌ Xatolik yuz berdi.\n\n"
                f"{str(e)[:500]}"
            )

        except Exception:
            pass


# =========================================================
# ORDER STATUS
# =========================================================

async def check_orders(context):

    try:

        c = conn()

        rows = c.execute(
            """
            SELECT *
            FROM orders
            WHERE status IN
            ('processing', 'pending', 'created')
            AND fazer_order_id IS NOT NULL
            AND fazer_order_id != ''
            LIMIT 30
            """
        ).fetchall()

        c.close()

        for row in rows:

            try:

                status_code, data = api_get(
                    "/orders/"
                    + str(
                        row["fazer_order_id"]
                    )
                )

                if (
                    status_code != 200
                    or not data.get("ok")
                ):

                    status_code, data = api_get(
                        "/topups/order/"
                        + str(
                            row["fazer_order_id"]
                        )
                    )

                if not data.get("ok"):
                    continue

                order = data.get(
                    "order",
                    data
                )

                new_status = order.get(
                    "status",
                    row["status"]
                )

                if new_status == row["status"]:
                    continue

                c = conn()

                c.execute(
                    """
                    UPDATE orders
                    SET status=?
                    WHERE id=?
                    """,
                    (
                        new_status,
                        row["id"]
                    )
                )

                c.commit()
                c.close()

                await context.bot.send_message(
                    row["user_id"],
                    "📦 BUYURTMA STATUSI\n\n"
                    f"🆔 #{row['id']}\n"
                    f"📦 {row['product_name']}\n"
                    f"📊 {new_status}"
                )

            except Exception as e:

                log.error(
                    "Order status xatosi: %s",
                    e
                )

    except Exception as e:

        log.error(
            "check_orders xatosi: %s",
            e
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):

    log.exception(
        "BOT ERROR",
        exc_info=context.error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    init_db()

    if not BOT_TOKEN:
        raise SystemExit(
            "❌ BOT_TOKEN ni yozing."
        )

    if not FAZER_API_KEY:
        raise SystemExit(
            "❌ FAZER_API_KEY ni yozing."
        )

    if not ADMIN_ID:
        raise SystemExit(
            "❌ ADMIN_ID ni yozing."
        )

    print(
        "================================="
    )

    print(
        "🎮 DONAT BOT ISHGA TUSHMOQDA..."
    )

    print(
        "================================="
    )

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(remove_bot_menu)
        .build()
    )

    # START
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # ADMIN
    app.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    # CALLBACK
    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # PHOTO
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            photo_handler
        )
    )

    # TEXT
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler
        )
    )

    # STATUS
    if app.job_queue:

        app.job_queue.run_repeating(
            check_orders,
            interval=30,
            first=30
        )

    # ERROR
    app.add_error_handler(
        error_handler
    )

    print(
        "✅ BOT ISHLADI!"
    )

    print(
        "🎮 PUBG / FREE FIRE / ROBLOX "
        "VA BOSHQA O'YINLAR KATALOGDAN OLINADI."
    )

    app.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
