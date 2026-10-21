import asyncio
import hashlib
import logging
import os
import random
import re
import sqlite3
import time
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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

# Админы: по username (без @). Для надёжности можно дополнительно задать ID через ADMIN_IDS (команда /myid).
ADMIN_USERNAMES = {"vvlzx"}
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}

BASE_DIR = Path(__file__).parent
CARDS_DIR = BASE_DIR / "cards"
PHOTOS_DIR = CARDS_DIR / "photos"
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "bot.db")))

COOLDOWN_SECONDS = 1 * 60 * 60  # 1 час

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

# Шанс (в %), что выпадет НОВАЯ для игрока карточка, пока у него есть неоткрытые.
# Чем больше число, тем реже повторки. 100 — повторок не будет, пока не откроет все.
NEW_CARD_CHANCE = 85

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
PAGE_SIZE = 8
# =========================================================

router = Router()
BOT_USERNAME = ""
_recent_welcome: dict[int, float] = {}


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
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        title TEXT
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


_last_drop: dict[int, str] = {}  # chat_id -> карточка, выпавшая последней в этом чате


def _weighted_pick(pool: list[dict]) -> dict:
    """Выбор из списка карточек: сначала редкость по весам, потом случайная карточка этой редкости."""
    by_rarity: dict[str, list] = {}
    for c in pool:
        by_rarity.setdefault(c["rarity"], []).append(c)
    keys = list(by_rarity)
    weights = [RARITIES[k]["weight"] for k in keys]
    key = random.choices(keys, weights=weights)[0]
    return random.choice(by_rarity[key])


def pick_card(cards: dict[str, dict], exclude: str | None = None, owned: set[str] | None = None) -> dict:
    """Случайная карточка.
    exclude — название, которое не должно выпасть (если есть выбор).
    owned — карточки, которые уже есть у игрока: новые выпадают с шансом NEW_CARD_CHANCE %.
    """
    owned = owned or set()
    pool = {n: c for n, c in cards.items() if n != exclude} or cards
    new = [c for n, c in pool.items() if n not in owned]
    old = [c for n, c in pool.items() if n in owned]

    if new and old:
        group = new if random.random() * 100 < NEW_CARD_CHANCE else old
    else:
        group = new or old
    return _weighted_pick(group)


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


def start_kb() -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="🃏 Открыть карточку", callback_data="open"),
        InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
    ]]
    if BOT_USERNAME:
        rows.insert(0, [InlineKeyboardButton(
            text="➕ Добавить в группу",
            url=f"https://t.me/{BOT_USERNAME}?startgroup=welcome",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def welcome_group(bot: Bot, chat_id: int):
    """Приветствие в группе (с защитой от двойной отправки)."""
    now = time.time()
    if now - _recent_welcome.get(chat_id, 0) < 60:
        return
    _recent_welcome[chat_id] = now
    await bot.send_message(chat_id, WELCOME)


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

    # не выдаём подряд одну и ту же карточку разным людям в одном чате
    owned = {r["card"] for r in db.execute("SELECT card FROM user_cards WHERE tg_id = ?", (tg_id,))}
    card = pick_card(cards, exclude=_last_drop.get(message.chat.id), owned=owned)
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
    # пока без commit: если отправка фото упадёт, кулдаун и очки не списываются
    user = db.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()

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
    try:
        await message.reply_photo(FSInputFile(card["path"]), caption=caption)
    except Exception:
        db.rollback()
        logging.exception("Не удалось отправить карточку «%s»", card["name"])
        await message.answer("⚠️ Не удалось отправить карточку, попробуй ещё раз. Кулдаун не потрачен.")
        return
    db.commit()
    _last_drop[message.chat.id] = card["name"]


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



# ======================= АДМИН-ПАНЕЛЬ =======================
admin_router = Router()
admin_router.message.filter(F.chat.type == "private")


class Broadcast(StatesGroup):
    waiting_message = State()


def is_admin(user) -> bool:
    uname = (user.username or "").lower()
    return uname in {u.lower() for u in ADMIN_USERNAMES} or user.id in ADMIN_IDS


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📣 Рассылка в группы", callback_data="adm:bc")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="📋 Список групп", callback_data="adm:groups")],
    ])


ADMIN_TEXT = "🛠 Админ-панель\n\nВыбери действие:"


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        await message.answer("⛔ Нет доступа.")
        return
    await state.clear()
    await message.answer(ADMIN_TEXT, reply_markup=admin_kb())


@admin_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if is_admin(message.from_user):
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_kb())


@admin_router.callback_query(F.data.startswith("adm:"))
async def cb_admin(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    action = call.data.split(":")[1]

    if action == "menu":
        await state.clear()
        await call.message.edit_text(ADMIN_TEXT, reply_markup=admin_kb())

    elif action == "stats":
        users = db.execute("SELECT COUNT(*) c, COALESCE(SUM(opens),0) o FROM users").fetchone()
        await call.message.edit_text(
            "📊 Статистика\n\n"
            f"👥 Игроков: {users['c']}\n"
            f"🃏 Открыто карточек: {users['o']}\n"
            f"💬 Групп: {len(_known_chats)}\n"
            f"🖼 Карточек в базе: {len(load_cards())}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:menu")]]),
        )

    elif action == "groups":
        rows = db.execute("SELECT chat_id, title FROM chats ORDER BY title").fetchall()
        text = "📋 Группы бота:\n\n" + ("\n".join(f"• {r['title']}" for r in rows) if rows else "Пока нет ни одной группы.")
        await call.message.edit_text(
            text[:4000],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:menu")]]),
        )

    elif action == "bc":
        if not _known_chats:
            await call.answer("Бот пока не знает ни одной группы. Напишите что-нибудь в группе.", show_alert=True)
            return
        await state.set_state(Broadcast.waiting_message)
        await call.message.edit_text(
            "📣 Рассылка\n\nОтправь сообщение, которое бот разошлёт в группы "
            "(текст, фото, видео, голосовое — что угодно).\n\n/cancel — отмена"
        )
    await call.answer()


@admin_router.message(Broadcast.waiting_message)
async def bc_got_message(message: Message, state: FSMContext):
    if not is_admin(message.from_user):
        return
    if message.text and message.text.startswith("/"):
        return
    await state.update_data(src_chat=message.chat.id, src_msg=message.message_id)
    await message.answer("👆 Так будет выглядеть сообщение. Куда отправить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Во все группы ({len(_known_chats)})", callback_data="bc:all")],
        [InlineKeyboardButton(text="🎯 В одну группу", callback_data="bc:pick:0")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:menu")],
    ]), reply_to_message_id=message.message_id)


async def send_to_chats(bot: Bot, chat_ids: list[int], src_chat: int, src_msg: int) -> tuple[int, int]:
    ok = failed = 0
    for cid in chat_ids:
        for _ in range(3):
            try:
                await bot.copy_message(cid, src_chat, src_msg)
                ok += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (TelegramForbiddenError, TelegramBadRequest):
                failed += 1
                unregister_chat(cid)  # бота выгнали / группа удалена
                break
            except Exception:
                logging.exception("Ошибка рассылки в %s", cid)
                failed += 1
                break
        await asyncio.sleep(0.05)
    return ok, failed


@admin_router.callback_query(F.data.startswith("bc:"))
async def cb_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user):
        await call.answer("⛔ Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    if "src_msg" not in data:
        await call.answer("Сообщение не найдено, начни заново: /admin", show_alert=True)
        return
    parts = call.data.split(":")
    action = parts[1]

    if action == "pick":
        page = int(parts[2])
        rows = db.execute("SELECT chat_id, title FROM chats ORDER BY title").fetchall()
        pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        kb = [[InlineKeyboardButton(text=r["title"][:40], callback_data=f"bc:g:{r['chat_id']}")]
              for r in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"bc:pick:{page - 1}"))
        nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"bc:pick:{page + 1}"))
        kb.append(nav)
        kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="adm:menu")])
        await call.message.edit_text("Выбери группу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await call.answer()
        return

    if action == "all":
        targets = list(_known_chats)
    else:  # bc:g:<chat_id>
        targets = [int(parts[2])]

    await call.message.edit_text(f"⏳ Отправляю в {len(targets)} гр...")
    ok, failed = await send_to_chats(call.bot, targets, data["src_chat"], data["src_msg"])
    await state.clear()
    await call.message.edit_text(
        f"✅ Рассылка завершена\n\nОтправлено: {ok}\nНе удалось: {failed}"
        + ("\n(группы, где бота нет, удалены из списка)" if failed else ""),
        reply_markup=admin_kb(),
    )
    await call.answer()


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
    "⏳ Карточку можно открывать раз в час.\n"
    "♻️ За повторную карточку очков меньше."
)


_known_chats: set[int] = {r["chat_id"] for r in db.execute("SELECT chat_id FROM chats")}


def register_chat(chat_id: int, title: str | None):
    if chat_id in _known_chats:
        return
    _known_chats.add(chat_id)
    db.execute("INSERT OR REPLACE INTO chats (chat_id, title) VALUES (?, ?)", (chat_id, title or str(chat_id)))
    db.commit()


def unregister_chat(chat_id: int):
    _known_chats.discard(chat_id)
    db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
    db.commit()


class ChatTracker(BaseMiddleware):
    """Запоминает группы, в которых кто-то писал (для рассылки)."""

    async def __call__(self, handler, event, data):
        if event.chat.type in ("group", "supergroup"):
            register_chat(event.chat.id, event.chat.title)
        return await handler(event, data)


router.message.outer_middleware(ChatTracker())


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER))
async def bot_added(event: ChatMemberUpdated):
    if event.chat.type in ("group", "supergroup"):
        register_chat(event.chat.id, event.chat.title)
        await welcome_group(event.bot, event.chat.id)


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=IS_MEMBER >> IS_NOT_MEMBER))
async def bot_removed(event: ChatMemberUpdated):
    if event.chat.type in ("group", "supergroup"):
        unregister_chat(event.chat.id)


@router.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой Telegram ID: {message.from_user.id}")


@router.message(F.new_chat_members)
async def on_new_members(message: Message):
    me = await message.bot.me()
    if any(u.id == me.id for u in message.new_chat_members):
        await welcome_group(message.bot, message.chat.id)


@router.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type == "private":
        await message.answer(WELCOME, reply_markup=start_kb())
    else:
        # сюда попадаем после кнопки «Добавить в группу» (startgroup)
        await welcome_group(message.bot, message.chat.id)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP, reply_markup=start_kb())


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
    global BOT_USERNAME
    bot = Bot(BOT_TOKEN)
    BOT_USERNAME = (await bot.me()).username
    me = await bot.get_me()
    if not me.can_read_all_group_messages:
        logging.warning(
            "У бота ВКЛЮЧЁН Group Privacy: в группах он не видит обычные сообщения («спейс»). "
            "Выключи в @BotFather (Bot Settings -> Group Privacy -> Turn off), затем удали бота из группы "
            "и добавь заново, либо сделай бота админом группы."
        )
    dp = Dispatcher()
    dp.include_router(admin_router)  # админка — первой, чтобы FSM-сообщения не ловил общий обработчик
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
