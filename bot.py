import asyncio
import hashlib
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import (
    IS_MEMBER,
    IS_NOT_MEMBER,
    ChatMemberUpdatedFilter,
    Command,
    CommandStart,
)
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ======================= НАСТРОЙКИ =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8984423334:AAFrUPJ5UumBQDw9OhPjmzMC9sS1Z_lWAeg")

BASE_DIR = Path(__file__).parent
CARDS_DIR = BASE_DIR / "cards"
PHOTOS_DIR = CARDS_DIR / "photos"
DB_PATH = BASE_DIR / "bot.db"

COOLDOWN_SECONDS = 2 * 60 * 60  # 2 часа

# Слова-триггеры (регистр и знаки препинания не важны)
TRIGGERS = {"спейс", "карточка спейс", "карта спейс"}

# key -> (файл со списком, название, очки, монеты, шанс выпадения)
RARITIES = {
    "common":    {"file": "common.txt",    "title": "Обычная",     "points": 1000,  "coins": 2,  "weight": 60},
    "rare":      {"file": "rare.txt",      "title": "Редкая",      "points": 3000,  "coins": 4,  "weight": 25},
    "mythic":    {"file": "mythic.txt",    "title": "Мифическая",  "points": 5000,  "coins": 6,  "weight": 12},
    "legendary": {"file": "legendary.txt", "title": "Легендарная", "points": 10000, "coins": 10, "weight": 3},
}

# Повторка: сколько процентов очков и сколько монет получает игрок
REPEAT_POINTS_PERCENT = 20
REPEAT_COINS = 1

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
PAGE_SIZE = 8
# =========================================================

router = Router()


# ----------------------- БАЗА ДАННЫХ -----------------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        coins INTEGER NOT NULL DEFAULT 0,
        last_open REAL NOT NULL DEFAULT 0,
        favorite TEXT,
        opens INTEGER NOT NULL DEFAULT 0,
        repeats INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS user_cards (
        tg_id INTEGER NOT NULL,
        card TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (tg_id, card)
    );
    """
)
db.commit()


def get_user(tg_id: int) -> sqlite3.Row:
    db.execute("INSERT OR IGNORE INTO users (tg_id) VALUES (?)", (tg_id,))
    db.commit()
    return db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()


# ----------------------- КАРТОЧКИ -----------------------
def load_cards() -> dict[str, dict]:
    """Читает txt-файлы заново при каждом вызове — править их можно без перезапуска."""
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    photos = {p.stem.strip().lower(): p for p in PHOTOS_DIR.iterdir() if p.suffix.lower() in IMAGE_EXT}
    cards: dict[str, dict] = {}
    for key, r in RARITIES.items():
        path = CARDS_DIR / r["file"]
        if not path.exists():
            path.write_text("", encoding="utf-8")
        for line in path.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if not name or name.startswith("#"):
                continue
            photo = photos.get(name.lower())
            if photo and name not in cards:
                cards[name] = {"name": name, "rarity": key, "path": photo}
            elif not photo:
                logging.warning("Нет фото для карточки «%s» (cards/photos)", name)
    return cards


def card_id(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()[:10]


def pick_card(cards: dict[str, dict]) -> dict:
    by_rarity: dict[str, list] = {}
    for c in cards.values():
        by_rarity.setdefault(c["rarity"], []).append(c)
    keys = list(by_rarity)
    weights = [RARITIES[k]["weight"] for k in keys]
    key = random.choices(keys, weights=weights)[0]
    return random.choice(by_rarity[key])


def fmt(n: int) -> str:
    return f"{n:,}"


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} сек"
    return f"{s} сек"


def user_nick(u) -> str:
    return u.first_name or (f"@{u.username}" if u.username else str(u.id))


# ----------------------- ТИТУЛЫ И ДОСТИЖЕНИЯ -----------------------
def get_title(cards_count: int) -> str:
    if cards_count >= 100:
        return "Повелитель космоса"
    if cards_count >= 50:
        return "Знаток"
    if cards_count >= 25:
        return "Исследователь"
    if cards_count >= 10:
        return "Коллекционер"
    return "Новичок"


def get_stats(tg_id: int, total: int) -> dict:
    u = get_user(tg_id)
    rows = db.execute("SELECT card FROM user_cards WHERE tg_id = ?", (tg_id,)).fetchall()
    cards = load_cards()
    owned = [r["card"] for r in rows if r["card"] in cards]
    by_r = {k: 0 for k in RARITIES}
    for name in owned:
        by_r[cards[name]["rarity"]] += 1
    return {
        "user": u, "cards": len(owned), "total": total, "points": u["points"],
        "coins": u["coins"], "opens": u["opens"], "repeats": u["repeats"],
        "common": by_r["common"], "rare": by_r["rare"],
        "mythic": by_r["mythic"], "legendary": by_r["legendary"],
        "favorite": u["favorite"],
    }


ACHIEVEMENTS = [
    lambda s: s["opens"] >= 1,
    lambda s: s["opens"] >= 10,
    lambda s: s["opens"] >= 50,
    lambda s: s["cards"] >= 10,
    lambda s: s["cards"] >= 25,
    lambda s: s["cards"] >= 50,
    lambda s: s["points"] >= 50_000,
    lambda s: s["points"] >= 250_000,
    lambda s: s["coins"] >= 100,
    lambda s: s["rare"] >= 1,
    lambda s: s["mythic"] >= 1,
    lambda s: s["legendary"] >= 1,
    lambda s: s["legendary"] >= 3,
    lambda s: s["repeats"] >= 10,
    lambda s: s["favorite"] is not None,
]


def profile_text(tg_user, tg_id: int) -> str:
    total = len(load_cards())
    s = get_stats(tg_id, total)
    ach = sum(1 for f in ACHIEVEMENTS if f(s))
    fav = s["favorite"] if s["favorite"] else "Не выбрана"
    return (
        f"Профиль «{user_nick(tg_user)}»\n\n"
        f"🔎 ID • {100000 + s['user']['id']}\n"
        f"🃏 Карт • {s['cards']} из {total}\n"
        f"✨ Очки • {fmt(s['points'])}\n"
        f"💰 Монеты • {fmt(s['coins'])}\n"
        f"🏆 Титул • {get_title(s['cards'])}\n"
        f"🏅 Достижения • {ach} / {len(ACHIEVEMENTS)}\n"
        f"❤️‍🔥 Любимая карта • {fav}"
    )


def profile_kb(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❤️ Выбрать любимую карту", callback_data=f"fav:{tg_id}:0")]]
    )


# ----------------------- ВЫДАЧА КАРТОЧКИ -----------------------
def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🃏 Открыть карточку", callback_data="open"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
        ]]
    )


async def give_card(message: Message, tg_user):
    tg_id = tg_user.id
    cards = load_cards()
    if not cards:
        await message.answer("😔 Пока нет доступных карточек. Администратору нужно добавить фото и заполнить txt-файлы.")
        return

    user = get_user(tg_id)
    now = time.time()
    left = user["last_open"] + COOLDOWN_SECONDS - now
    if left > 0:
        await message.reply(f"⏳ Следующую карточку можно открыть через {fmt_time(left)}")
        return

    card = pick_card(cards)
    r = RARITIES[card["rarity"]]
    row = db.execute(
        "SELECT count FROM user_cards WHERE tg_id = ? AND card = ?", (tg_id, card["name"])
    ).fetchone()
    is_repeat = row is not None

    if is_repeat:
        gain_points = r["points"] * REPEAT_POINTS_PERCENT // 100
        gain_coins = REPEAT_COINS
        db.execute("UPDATE user_cards SET count = count + 1 WHERE tg_id = ? AND card = ?", (tg_id, card["name"]))
    else:
        gain_points = r["points"]
        gain_coins = r["coins"]
        db.execute("INSERT INTO user_cards (tg_id, card) VALUES (?, ?)", (tg_id, card["name"]))

    db.execute(
        "UPDATE users SET points = points + ?, coins = coins + ?, last_open = ?, opens = opens + 1, "
        "repeats = repeats + ? WHERE tg_id = ?",
        (gain_points, gain_coins, now, 1 if is_repeat else 0, tg_id),
    )
    db.commit()
    user = get_user(tg_id)

    head = (
        f"🔁 Повторка — «{card['name']}»!"
        if is_repeat
        else f"🚀 Новая карточка — «{card['name']}» открыта!"
    )
    caption = (
        f"{head}\n\n"
        f"💎 Редкость • {r['title']}\n"
        f"✨ Очки • +{fmt(gain_points)} [{fmt(user['points'])}]\n"
        f"💰 Монеты • +{fmt(gain_coins)} [{fmt(user['coins'])}]"
    )
    await message.reply_photo(FSInputFile(card["path"]), caption=caption)


# ----------------------- ПРОФИЛЬ -----------------------
async def send_profile(message: Message, tg_user):
    get_user(tg_user.id)
    text = profile_text(tg_user, tg_user.id)
    kb = profile_kb(tg_user.id)
    photos = await message.bot.get_user_profile_photos(tg_user.id, limit=1)
    if photos.total_count > 0:
        await message.answer_photo(photos.photos[0][-1].file_id, caption=text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


async def edit_profile_message(msg: Message, text: str, kb: InlineKeyboardMarkup):
    if msg.photo:
        await msg.edit_caption(caption=text, reply_markup=kb)
    else:
        await msg.edit_text(text, reply_markup=kb)


# ----------------------- ХЕНДЛЕРЫ -----------------------
WELCOME = (
    "👋 Всем привет! Я бот, в котором ты можешь собирать уникальные карточки "
    "и соревноваться с другими игроками\n\n"
    "Как начать?\n"
    "Напишите «спейс» для получения первой карточки\n\n\n"
    "Узнать все функции можно по команде /help"
)

HELP = (
    "📖 Команды бота\n\n"
    "🃏 /card — открыть новую карточку (или напиши «спейс»)\n"
    "👤 /profile — посмотреть свой профиль\n"
    "❓ /help — список команд\n\n"
    "⏳ Карточку можно открывать раз в 2 часа.\n"
    "♻️ За повторную карточку очков меньше."
)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added(event: ChatMemberUpdated):
    if event.chat.type in ("group", "supergroup"):
        await event.bot.send_message(event.chat.id, WELCOME)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(WELCOME, reply_markup=main_kb())


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP, reply_markup=main_kb())


@router.message(Command("card"))
async def cmd_card(message: Message):
    await give_card(message, message.from_user)


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    await send_profile(message, message.from_user)


@router.callback_query(F.data == "open")
async def cb_open(call: CallbackQuery):
    await give_card(call.message, call.from_user)
    await call.answer()


@router.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    await send_profile(call.message, call.from_user)
    await call.answer()


@router.callback_query(F.data.startswith("fav:"))
async def cb_fav_list(call: CallbackQuery):
    _, uid, page = call.data.split(":")
    uid, page = int(uid), int(page)
    if call.from_user.id != uid:
        await call.answer("Это не твой профиль 🙂", show_alert=True)
        return

    cards = load_cards()
    rows = db.execute("SELECT card FROM user_cards WHERE tg_id = ?", (uid,)).fetchall()
    owned = sorted(r["card"] for r in rows if r["card"] in cards)
    if not owned:
        await call.answer("У тебя пока нет карточек — напиши «спейс»!", show_alert=True)
        return

    pages = (len(owned) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, pages - 1))
    chunk = owned[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    kb = [
        [InlineKeyboardButton(text=f"{RARITIES[cards[n]['rarity']]['title'][0]} • {n}", callback_data=f"pick:{uid}:{card_id(n)}")]
        for n in chunk
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"fav:{uid}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"fav:{uid}:{page + 1}"))
    kb.append(nav)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back:{uid}")])

    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("back:"))
async def cb_back(call: CallbackQuery):
    uid = int(call.data.split(":")[1])
    if call.from_user.id != uid:
        await call.answer("Это не твой профиль 🙂", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=profile_kb(uid))
    await call.answer()


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(call: CallbackQuery):
    _, uid, cid = call.data.split(":")
    uid = int(uid)
    if call.from_user.id != uid:
        await call.answer("Это не твой профиль 🙂", show_alert=True)
        return

    cards = load_cards()
    name = next((n for n in cards if card_id(n) == cid), None)
    owned = db.execute("SELECT 1 FROM user_cards WHERE tg_id = ? AND card = ?", (uid, name)).fetchone() if name else None
    if not name or not owned:
        await call.answer("Эта карточка недоступна", show_alert=True)
        return

    db.execute("UPDATE users SET favorite = ? WHERE tg_id = ?", (name, uid))
    db.commit()
    await edit_profile_message(call.message, profile_text(call.from_user, uid), profile_kb(uid))
    await call.answer(f"❤️ Любимая карта: {name}")


def normalize(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


@router.message(F.text)
async def on_text(message: Message):
    t = normalize(message.text)
    if t in TRIGGERS:
        await give_card(message, message.from_user)
    elif t in {"профиль", "мой профиль"}:
        await send_profile(message, message.from_user)


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
