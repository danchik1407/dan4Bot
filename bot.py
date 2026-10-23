# -*- coding: utf-8 -*-
import os
import json
import random
import time
import asyncio
import logging
from types import SimpleNamespace
from typing import Optional
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.enums import ChatMemberStatus
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    raise SystemExit("❌ ОШИБКА: Не найден BOT_TOKEN в файле .env!")

DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "data.json")

COOLDOWN_SECONDS = 2 * 60 * 60  # 2 часа
CHALLENGE_TIMEOUT = 60

COIN_DROP_CHANCE = 0.05  # 5%
COIN_DROP_MIN = 1
COIN_DROP_MAX = 20

DROP_IMAGE = "drop.jpg"
SUMMON_BATCH_SIZE = 25
SUMMON_BATCH_DELAY = 2.0
SUMMON_MAX_MESSAGES = 5

active_summons: dict[int, dict] = {}
active_challenges: dict[str, dict] = {}
active_mines_games: dict[str, dict] = {}  # Хранилище игр в мины

_db_lock = asyncio.Lock()


# ================= БАЗА ДАННЫХ =================
def _load_data_sync():
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {"users": {}}
            data = json.loads(content)
            data.setdefault("users", {})
            return data
    except Exception as e:
        logger.error(f"Ошибка чтения БД: {e}")
        return {"users": {}}


def _save_data_sync(data):
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
    except Exception as e:
        logger.error(f"Ошибка записи БД: {e}")


async def load_data():
    async with _db_lock:
        return _load_data_sync()


async def save_data(data):
    async with _db_lock:
        _save_data_sync(data)


def get_or_create_user(data, user_id: int, username: Optional[str]):
    uid_str = str(user_id)
    if uid_str not in data["users"]:
        data["users"][uid_str] = {
            "coins": 0,
            "username": username or f"id{user_id}",
            "last_chest": 0,
            "last_call": 0,
            "call_sign": None,
        }
    else:
        u = data["users"][uid_str]
        if username:
            u["username"] = username
        for key, default in (("last_chest", 0), ("last_call", 0), ("call_sign", None), ("coins", 0)):
            u.setdefault(key, default)
    return data["users"][uid_str]


def find_user_by_username(data, username_arg: str) -> Optional[SimpleNamespace]:
    target = username_arg.removeprefix("@").lower()
    for uid_str, udata in data["users"].items():
        if (udata.get("username") or "").lower() == target:
            return SimpleNamespace(id=int(uid_str), username=udata.get("username"))
    return None


# ================= ИНИЦИАЛИЗАЦИЯ =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ================= ВСПОМОГАТЕЛЬНЫЕ =================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_chat_admin(user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception as e:
        logger.warning(f"is_chat_admin({user_id}, {chat_id}) fail: {e}")
        return False


def format_time(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}м")
    if s and not h: parts.append(f"{s}с")
    return " ".join(parts) or "0с"


def safe_text(text) -> str:
    import html as html_module
    return html_module.escape(str(text))


def get_name_by_id(data, user_id, fallback_username=None) -> str:
    uid_str = str(user_id)
    if uid_str in data["users"]:
        u = data["users"][uid_str]
        if u.get("username"):
            return f"@{safe_text(u['username'])}"
    if fallback_username:
        return f"@{safe_text(fallback_username)}"
    return f"Пользователь {user_id}"


async def try_delete(message: Message):
    # Отключено по твоей просьбе
    return


# ================= 🎁 РАНДОМНЫЙ ДРОП =================
async def process_random_coin_drop(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    text = message.text or ""
    if text.startswith("!") or text.startswith("/"):
        return
    if random.random() > COIN_DROP_CHANCE:
        return

    amount = random.randint(COIN_DROP_MIN, COIN_DROP_MAX)
    data = await load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    user["coins"] += amount
    await save_data(data)

    caption = (
        f"🎉 <b>Поздравляю!</b>\n\n"
        f"🎁 Ты выиграл(а) <b>{amount} 🪙</b>\n\n"
        f"💬 Пиши сообщения в чате и получай возможность находить Коины!"
    )
    try:
        if os.path.exists(DROP_IMAGE):
            await message.answer_photo(photo=FSInputFile(DROP_IMAGE), caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Дроп не отправлен: {e}")


# ================= 🛍️ МАГАЗИН =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?магазин\s*$'))
async def cmd_shop(message: Message):
    await try_delete(message)
    await message.answer("🛍️ <b>Магазин AURA:</b>\n\n❌ Предметов для покупки пока нет", parse_mode="HTML")


# ================= 📦 СУНДУКИ =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?открыть\s*$'))
async def cmd_open(message: Message):
    await try_delete(message)
    data = await load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    now = time.time()
    remaining = COOLDOWN_SECONDS - (now - user.get("last_chest", 0))
    if remaining > 0:
        await message.answer(f"⏳ Открытие доступно через {format_time(remaining)}")
        return

    roll = random.randint(1, 100)
    is_golden = roll > 92
    reward = random.randint(10, 100) if is_golden else random.randint(1, 10)
    user["coins"] += reward
    user["last_chest"] = now
    await save_data(data)

    if is_golden:
        await message.answer(f"✨ <b>ЗОЛОТОЙ СУНДУК!</b> 🔑\n🎉 Тебе выпало: <b>{reward}</b> 🪙", parse_mode="HTML")
    else:
        await message.answer(f"🎁 <b>Сундук открыт!</b>\n🎉 Тебе выпало: <b>{reward}</b> 🪙", parse_mode="HTML")


# ================= 💵 КАРМАН =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(посмотреть\s+карман|карман|баланс)\s*$'))
async def cmd_pocket(message: Message):
    await try_delete(message)
    data = await load_data()
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
    else:
        target = message.from_user
    user = get_or_create_user(data, target.id, target.username)
    name = f"@{safe_text(target.username)}" if getattr(target, "username", None) else f"Пользователь {target.id}"
    await message.answer(f"💰 <b>Карман {name}</b>\n\n{user.get('coins', 0)} 🪙", parse_mode="HTML")


# ================= 📊 ТОП =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(топ\s+монет|топ)\s*$'))
async def cmd_top_coins(message: Message):
    await try_delete(message)
    data = await load_data()
    users_list = [
        {"username": udata.get("username") or f"id{uid}", "coins": udata.get("coins", 0)}
        for uid, udata in data["users"].items()
    ]
    users_list.sort(key=lambda x: x["coins"], reverse=True)
    top10 = users_list[:10]
    if not top10 or top10[0]["coins"] == 0:
        await message.answer("📊 Топ пока пуст. Открывай сундуки!")
        return
    msg = "💰 <b>ТОП-10 ПО КОИНАМ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, u in enumerate(top10):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        msg += f"{prefix} {safe_text(u['username'])} — <b>{u['coins']}</b> 🪙\n"
    await message.answer(msg, parse_mode="HTML")


# ================= 💸 ПЕРЕВОД =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?перевод(?:\s|$)'))
async def cmd_transfer(message: Message):
    await try_delete(message)
    data = await load_data()
    sender = get_or_create_user(data, message.from_user.id, message.from_user.username)
    parts = message.text.strip().split()
    target_user = None
    amount = 0

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if len(parts) < 2:
            return await message.answer("❌ Укажи сумму: <code>!перевод 50</code>", parse_mode="HTML")
        try:
            amount = int(parts[1])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом.")
    else:
        if len(parts) < 3:
            return await message.answer("❌ Формат: <code>!перевод @username 50</code>", parse_mode="HTML")
        target_user = find_user_by_username(data, parts[1])
        if not target_user:
            return await message.answer("❌ Пользователь не найден.")
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом.")

    if amount <= 0 or target_user.id == message.from_user.id:
        return await message.answer("❌ Сумма должна быть > 0 и нельзя переводить самому себе.")
    if sender["coins"] < amount:
        return await message.answer(f"❌ Недостаточно коинов. У тебя: <b>{sender['coins']}</b> 🪙", parse_mode="HTML")

    sender["coins"] -= amount
    target = get_or_create_user(data, target_user.id, target_user.username)
    target["coins"] += amount
    await save_data(data)

    sender_name = f"@{safe_text(message.from_user.username)}" if message.from_user.username else f"id{message.from_user.id}"
    target_name = f"@{safe_text(target_user.username)}" if getattr(target_user, "username",
                                                                   None) else f"id{target_user.id}"
    await message.answer(
        f"✅ <b>Перевод выполнен!</b>\n\n👤 От: {sender_name}\n👤 Кому: {target_name}\n💸 Сумма: <b>{amount}</b> 🪙",
        parse_mode="HTML",
    )


# ================= 💸 АДМИН: СПИСАТЬ =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?забрать(?:\s|$)'))
async def cmd_admin_withdraw(message: Message):
    await try_delete(message)
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Команда доступна только администраторам бота.")
    if message.chat.type in ("group", "supergroup") and not await is_chat_admin(message.from_user.id, message.chat.id):
        return await message.answer("❌ Только для админов этого чата.")

    data = await load_data()
    parts = message.text.strip().split()
    target_user = None
    amount = 0

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if len(parts) < 2:
            return await message.answer("❌ Укажи сумму: <code>!забрать 100</code>", parse_mode="HTML")
        try:
            amount = int(parts[1])
        except ValueError:
            return await message.answer("❌ Укажи число.")
    else:
        if len(parts) < 3:
            return await message.answer("❌ Формат: <code>!забрать @username 100</code>", parse_mode="HTML")
        target_user = find_user_by_username(data, parts[1])
        if not target_user:
            return await message.answer("❌ Пользователь не найден.")
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Укажи число.")

    if amount <= 0:
        return await message.answer("❌ Сумма должна быть больше 0.")

    target_data = get_or_create_user(data, target_user.id, target_user.username)
    if target_data["coins"] < amount:
        return await message.answer(f"❌ У пользователя только {target_data['coins']} 🪙")

    target_data["coins"] -= amount
    await save_data(data)
    name = f"@{safe_text(target_user.username)}" if getattr(target_user, "username", None) else f"id{target_user.id}"
    await message.answer(
        f"💸 <b>Коины списаны!</b>\n\n👤 У кого: {name}\n➖ Снято: <b>{amount}</b> 🪙\nОстаток: <b>{target_data['coins']}</b> 🪙",
        parse_mode="HTML",
    )


# ================= 🔧 АДМИН: ПОПОЛНИТЬ =================
@dp.message(F.text & F.text.regexp(r'(?i)^\+пополнить\s+карман(?:\s|$)'))
async def cmd_admin_add(message: Message):
    await try_delete(message)
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Команда доступна только администраторам бота.")
    if message.chat.type in ("group", "supergroup") and not await is_chat_admin(message.from_user.id, message.chat.id):
        return await message.answer("❌ Только для админов этого чата.")

    data = await load_data()
    parts = message.text.strip().split()
    target_user = None
    amount = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if len(parts) < 3:
            return await message.answer("❌ Укажи сумму: <code>+пополнить карман 500</code>", parse_mode="HTML")
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Укажи число.")
    else:
        if len(parts) < 4:
            return await message.answer("❌ Формат: <code>+пополнить карман @username 500</code>", parse_mode="HTML")
        target_user = find_user_by_username(data, parts[2])
        if not target_user:
            return await message.answer("❌ Пользователь не найден.")
        try:
            amount = int(parts[3])
        except ValueError:
            return await message.answer("❌ Укажи число.")

    if amount is None or amount <= 0:
        return await message.answer("❌ Сумма должна быть больше 0.")

    user = get_or_create_user(data, target_user.id, target_user.username)
    user["coins"] += amount
    await save_data(data)

    target_name = get_name_by_id(data, target_user.id, target_user.username)
    await message.answer(
        f"💰 <b>Карман пополнен!</b>\n\n👤 Кому: {target_name}\n➕ Начислено: <b>{amount}</b> 🪙\n📊 Баланс: <b>{user['coins']}</b> 🪙",
        parse_mode="HTML",
    )


# ================= 🎲 КУБЫ =================
async def _start_dice_duel(message: Message, target_user, amount: int):
    data = await load_data()
    challenger = get_or_create_user(data, message.from_user.id, message.from_user.username)
    if target_user.id == message.from_user.id:
        return await message.answer("❌ Нельзя играть сам с собой 😄")
    if challenger["coins"] < amount:
        return await message.answer(f"❌ У тебя недостаточно коинов.\nНужно: <b>{amount}</b> 🪙", parse_mode="HTML")

    challenged = get_or_create_user(data, target_user.id, target_user.username)
    if challenged["coins"] < amount:
        return await message.answer(
            f"❌ У {get_name_by_id(data, target_user.id, target_user.username)} недостаточно коинов.", parse_mode="HTML")

    challenge_id = f"{message.from_user.id}_{target_user.id}_{int(time.time())}"
    active_challenges[challenge_id] = {
        "challenger_id": message.from_user.id,
        "challenged_id": target_user.id,
        "amount": amount,
        "created_at": time.time(),
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"dice_accept|{challenge_id}"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"dice_decline|{challenge_id}"),
    ]])

    msg = await message.answer(
        f"🎲 <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"👤 {get_name_by_id(data, message.from_user.id, message.from_user.username)} вызывает {get_name_by_id(data, target_user.id, target_user.username)}\n"
        f"💰 Ставка: <b>{amount}</b> 🪙\n🎯 Победитель забирает: <b>{amount * 2}</b> 🪙\n\n"
        f"⏳ Время на ответ: {CHALLENGE_TIMEOUT} секунд",
        parse_mode="HTML", reply_markup=kb,
    )
    asyncio.create_task(_dice_timeout(challenge_id, msg))


async def _dice_timeout(challenge_id: str, msg: Message):
    await asyncio.sleep(CHALLENGE_TIMEOUT)
    ch = active_challenges.pop(challenge_id, None)
    if not ch: return
    try:
        await msg.edit_text("⌛ Время вышло — вызов отменён.")
    except Exception:
        pass


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?кубы(?:\s|$)'))
async def cmd_dice_challenge(message: Message):
    await try_delete(message)
    data = await load_data()
    parts = message.text.strip().split()
    target_user, amount = None, 0

    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if len(parts) < 2: return await message.answer("🎲 Формат: <code>!кубы ставка</code> (ответом)",
                                                       parse_mode="HTML")
        try:
            amount = int(parts[1])
        except ValueError:
            return await message.answer("❌ Ставка должна быть числом.")
    else:
        if len(parts) < 3: return await message.answer("🎲 Формат: <code>!кубы @username ставка</code>",
                                                       parse_mode="HTML")
        target_user = find_user_by_username(data, parts[1])
        if not target_user: return await message.answer("❌ Пользователь не найден.")
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Ставка должна быть числом.")

    if amount <= 0: return await message.answer("❌ Ставка должна быть больше 0.")
    await _start_dice_duel(message, target_user, amount)


@dp.callback_query(F.data.startswith("dice_accept|"))
async def cb_dice_accept(callback: CallbackQuery):
    challenge_id = callback.data.split("|", 1)[1]
    ch = active_challenges.get(challenge_id)
    if not ch: return await callback.answer("⌛ Вызов уже неактуален", show_alert=True)
    if callback.from_user.id != ch["challenged_id"]: return await callback.answer("Это не твой вызов!", show_alert=True)

    amount = ch["amount"]
    data = await load_data()
    challenger = get_or_create_user(data, ch["challenger_id"], None)
    challenged = get_or_create_user(data, ch["challenged_id"], callback.from_user.username)

    if challenger["coins"] < amount or challenged["coins"] < amount:
        active_challenges.pop(challenge_id, None)
        return await callback.message.edit_text("❌ У кого-то не хватает коинов. Вызов отменён.")

    r1, r2 = random.randint(1, 6), random.randint(1, 6)
    if r1 == r2:
        result = f"🤝 <b>Ничья!</b> {r1} : {r2}\nНикто не получает коины."
    else:
        winner_id = ch["challenger_id"] if r1 > r2 else ch["challenged_id"]
        loser_id = ch["challenged_id"] if winner_id == ch["challenger_id"] else ch["challenger_id"]
        winner = get_or_create_user(data, winner_id, None)
        loser = get_or_create_user(data, loser_id, None)
        winner["coins"] += amount
        loser["coins"] -= amount
        result = f"🏆 Победил {get_name_by_id(data, winner_id)} ({r1} : {r2})\n💰 +{amount} 🪙"

    await save_data(data)
    active_challenges.pop(challenge_id, None)
    await callback.message.edit_text(f"🎲 <b>Кубы брошены!</b>\n\n{result}", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("dice_decline|"))
async def cb_dice_decline(callback: CallbackQuery):
    challenge_id = callback.data.split("|", 1)[1]
    ch = active_challenges.get(challenge_id)
    if not ch: return await callback.answer("⌛ Вызов уже неактуален", show_alert=True)
    if callback.from_user.id != ch["challenged_id"]: return await callback.answer("Это не твой вызов!", show_alert=True)
    active_challenges.pop(challenge_id, None)
    await callback.message.edit_text("❌ Вызов отклонён.")
    await callback.answer()


# ================= 💣 ИГРА МИНЫ =================
# Коэффициенты (рассчитаны так, чтобы казино имело преимущество ~5%):
# 1 мина (шанс 75%): x1.25
# 2 мины (шанс 50%): x1.85
# 3 мины (шанс 25%): x3.75
MINES_COEFS = {1: 1.25, 2: 1.85, 3: 3.75}


def get_mines_kb(mines_count: int):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 💣", callback_data="mines_set_1"),
         InlineKeyboardButton(text="2 💣", callback_data="mines_set_2"),
         InlineKeyboardButton(text="3 💣", callback_data="mines_set_3")],
        [InlineKeyboardButton(text="🟩 Играть!", callback_data=f"mines_start_{mines_count}")]
    ])
    return kb


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?мины(?:\s|$)'))
async def cmd_mines(message: Message):
    await try_delete(message)
    await message.answer(
        "💣 <b>Игра «Мины»</b>\n\n"
        "Выбери количество мин и нажми «Играть».\n"
        "Поле 4x4 (16 клеток).\n\n"
        "Коэффициенты:\n"
        "1 мина (75%): x1.25\n"
        "2 мины (50%): x1.85\n"
        "3 мины (25%): x3.75\n\n"
        "❓ Чтобы сделать ставку, напиши: <code>!мины 50</code> (где 50 — сумма)",
        parse_mode="HTML"
    )


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?мины\s+(\d+)$'))
async def cmd_mines_bet(message: Message):
    await try_delete(message)
    parts = message.text.strip().split()
    amount = int(parts[1])
    data = await load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)

    if amount <= 0:
        return await message.answer("❌ Ставка должна быть больше 0.")
    if user["coins"] < amount:
        return await message.answer(f"❌ Недостаточно коинов. У тебя: {user['coins']} 🪙")

    game_id = str(random.randint(100000, 999999))
    active_mines_games[game_id] = {
        "user_id": message.from_user.id,
        "amount": amount,
        "mines_count": 1,  # По умолчанию 1 мина
        "board": [False] * 16,
        "opened": [False] * 16,
        "active": False
    }

    await message.answer(
        f"💣 <b>Стол на {amount} 🪙 создан!</b>\n"
        f"Выбери количество мин на кнопках ниже и нажми «Играть».",
        reply_markup=get_mines_kb(1),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("mines_set_"))
async def cb_mines_set(callback: CallbackQuery):
    mines_count = int(callback.data.split("_")[2])
    # Достаем последнюю игру этого юзера (упрощенно ищем по user_id)
    game_id = next(
        (gid for gid, g in active_mines_games.items() if g["user_id"] == callback.from_user.id and not g["active"]),
        None)

    if not game_id:
        return await callback.answer("❌ Игра не найдена или уже начата.", show_alert=True)

    active_mines_games[game_id]["mines_count"] = mines_count
    await callback.message.edit_reply_markup(reply_markup=get_mines_kb(mines_count))
    await callback.answer(f"Установлено {mines_count} мин(ы)")


@dp.callback_query(F.data.startswith("mines_start_"))
async def cb_mines_start(callback: CallbackQuery):
    game_id = next(
        (gid for gid, g in active_mines_games.items() if g["user_id"] == callback.from_user.id and not g["active"]),
        None)
    if not game_id:
        return await callback.answer("❌ Игра не найдена.", show_alert=True)

    game = active_mines_games[game_id]
    game["active"] = True

    # Расставляем мины
    mine_indices = random.sample(range(16), game["mines_count"])
    for i in mine_indices:
        game["board"][i] = True

    # Создаем кнопки поля 4x4
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for row in range(4):
        row_buttons = []
        for col in range(4):
            idx = row * 4 + col
            row_buttons.append(InlineKeyboardButton(text="⬜", callback_data=f"mines_open_{idx}"))
        kb.inline_keyboard.append(row_buttons)

    # Добавляем кнопку "Забрать"
    kb.inline_keyboard.append([InlineKeyboardButton(text="💰 Забрать (x0)", callback_data="mines_cashout")])

    await callback.message.edit_text(
        f"💣 <b>Мины: {game['mines_count']}</b>\n"
        f"Ставка: <b>{game['amount']}</b> 🪙\n"
        f"Текущий коэффициент: <b>x0</b>\n\n"
        f"Открывай клетки, но не попади на мину!",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mines_open_"))
async def cb_mines_open(callback: CallbackQuery):
    idx = int(callback.data.split("_")[2])
    game_id = next(
        (gid for gid, g in active_mines_games.items() if g["user_id"] == callback.from_user.id and g["active"]), None)
    if not game_id:
        return await callback.answer("❌ Игра не найдена.", show_alert=True)

    game = active_mines_games[game_id]
    if game["opened"][idx]:
        return await callback.answer("Эта клетка уже открыта!", show_alert=True)

    # Проверка на мину
    if game["board"][idx]:
        # Проиграл
        game["active"] = False
        active_mines_games.pop(game_id, None)

        # Показываем всё поле
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for row in range(4):
            row_buttons = []
            for col in range(4):
                i = row * 4 + col
                if game["board"][i]:
                    row_buttons.append(InlineKeyboardButton(text="💥", callback_data="ignore"))
                else:
                    row_buttons.append(InlineKeyboardButton(text="⬜", callback_data="ignore"))
            kb.inline_keyboard.append(row_buttons)

        data = await load_data()
        user = get_or_create_user(data, callback.from_user.id, callback.from_user.username)
        user["coins"] -= game["amount"]
        await save_data(data)

        await callback.message.edit_text(
            f"💥 <b>БАБАХ! Ты подорвался на мине!</b>\n\n"
            f"Ты потерял ставку: <b>{game['amount']}</b> 🪙",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return await callback.answer("💥 БУМ!")

    # Если не мина
    game["opened"][idx] = True

    # Считаем сколько безопасных открыто
    safe_opened = sum(1 for i in range(16) if game["opened"][i] and not game["board"][i])
    total_safe = 16 - game["mines_count"]

    if safe_opened == total_safe:
        # Выиграл всё поле
        return await cashout_mines(callback, game_id, game)

    # Пересчитываем текущий коэффициент (упрощенно: пропорционально открытым)
    # В идеале тут сложная математика, но для простоты берем базовый коэф и умножаем на долю открытых
    base_coef = MINES_COEFS[game["mines_count"]]
    current_coef = round(base_coef * (safe_opened / total_safe), 2)

    # Обновляем клавиатуру
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for row in range(4):
        row_buttons = []
        for col in range(4):
            i = row * 4 + col
            if game["opened"][i]:
                row_buttons.append(InlineKeyboardButton(text="✅", callback_data="ignore"))
            else:
                row_buttons.append(InlineKeyboardButton(text="⬜", callback_data=f"mines_open_{i}"))
        kb.inline_keyboard.append(row_buttons)

    win_amount = int(game["amount"] * current_coef)
    kb.inline_keyboard.append([InlineKeyboardButton(text=f"💰 Забрать ({win_amount} 🪙)", callback_data="mines_cashout")])

    await callback.message.edit_text(
        f"💣 <b>Мины: {game['mines_count']}</b>\n"
        f"Ставка: <b>{game['amount']}</b> 🪙\n"
        f"Текущий коэффициент: <b>x{current_coef}</b>\n\n"
        f"Открыто: {safe_opened} / {total_safe}",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "mines_cashout")
async def cb_mines_cashout(callback: CallbackQuery):
    game_id = next(
        (gid for gid, g in active_mines_games.items() if g["user_id"] == callback.from_user.id and g["active"]), None)
    if not game_id:
        return await callback.answer("❌ Игра не найдена.", show_alert=True)

    await cashout_mines(callback, game_id, active_mines_games[game_id])


async def cashout_mines(callback: CallbackQuery, game_id: str, game: dict):
    game["active"] = False
    active_mines_games.pop(game_id, None)

    safe_opened = sum(1 for i in range(16) if game["opened"][i] and not game["board"][i])
    total_safe = 16 - game["mines_count"]

    # Если открыто 0 клеток, возвращаем ставку (x1)
    if safe_opened == 0:
        win_amount = game["amount"]
        coef = 1.0
    else:
        base_coef = MINES_COEFS[game["mines_count"]]
        coef = round(base_coef * (safe_opened / total_safe), 2)
        win_amount = int(game["amount"] * coef)

    data = await load_data()
    user = get_or_create_user(data, callback.from_user.id, callback.from_user.username)

    # Чистый выигрыш (за вычетом ставки)
    profit = win_amount - game["amount"]
    user["coins"] += profit
    await save_data(data)

    # Показываем всё поле
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for row in range(4):
        row_buttons = []
        for col in range(4):
            i = row * 4 + col
            if game["board"][i]:
                row_buttons.append(InlineKeyboardButton(text="💣", callback_data="ignore"))
            else:
                row_buttons.append(InlineKeyboardButton(text="✅", callback_data="ignore"))
        kb.inline_keyboard.append(row_buttons)

    await callback.message.edit_text(
        f"💰 <b>Ты забрал выигрыш!</b>\n\n"
        f"Коэффициент: <b>x{coef}</b>\n"
        f"Твой выигрыш: <b>{win_amount}</b> 🪙\n"
        f"Чистая прибыль: <b>{profit}</b> 🪙",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer("💰 Выигрыш забран!")


@dp.callback_query(F.data == "ignore")
async def cb_ignore(callback: CallbackQuery):
    await callback.answer()


# ================= 📢 СОЗЫВЫ =================
async def summon_all_members(chat_id: int, call_text: str, starter_id: int):
    state = {"active": True}
    active_summons[chat_id] = state
    try:
        data = await load_data()
        users_to_tag = [int(uid) for uid in data["users"].keys() if int(uid) != starter_id]
        random.shuffle(users_to_tag)
        if not users_to_tag:
            return await bot.send_message(chat_id, "❌ Некого созывать.")

        sent = 0
        for i in range(0, len(users_to_tag), SUMMON_BATCH_SIZE):
            if not state["active"] or sent >= SUMMON_MAX_MESSAGES:
                if sent >= SUMMON_MAX_MESSAGES:
                    await bot.send_message(chat_id, "⏹️ Достигнут лимит сообщений — созыв остановлен.")
                break
            batch = users_to_tag[i:i + SUMMON_BATCH_SIZE]
            mentions = [f'<a href="tg://user?id={uid}">Пользователь</a>' for uid in batch]
            await bot.send_message(chat_id, f"📢 {call_text}\n\n" + "\n".join(mentions), parse_mode="HTML")
            sent += 1
            await asyncio.sleep(SUMMON_BATCH_DELAY)
    except Exception as e:
        logger.error(f"summon error: {e}")
    finally:
        active_summons.pop(chat_id, None)


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?all(?:\s|$)'))
async def cmd_all(message: Message):
    await try_delete(message)
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("❌ Только для групповых чатов!")
    user_id = message.from_user.id
    if not is_admin(user_id) and not await is_chat_admin(user_id, message.chat.id):
        return await message.answer("❌ Команда недоступна (только для админов чата)")
    parts = message.text.strip().split(maxsplit=1)
    call_text = parts[1] if len(parts) > 1 else "📢 Общий сбор всех участников!"
    await message.answer(
        f"🔄 Начинаю массовый созыв...\nТекст: {safe_text(call_text)}\n\n⚠️ Остановить: <code>!stop</code>",
        parse_mode="HTML",
    )
    asyncio.create_task(summon_all_members(message.chat.id, call_text, user_id))


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(stop|стоп)\s*$'))
async def cmd_stop(message: Message):
    await try_delete(message)
    if message.chat.type in ("group", "supergroup"):
        if not is_admin(message.from_user.id) and not await is_chat_admin(message.from_user.id, message.chat.id):
            return await message.answer("❌ Остановить созыв может только админ.")
    state = active_summons.get(message.chat.id)
    if state and state.get("active"):
        state["active"] = False
        await message.answer("⏹️ Созыв прекращён.")
    else:
        await message.answer("❌ Активных созывов нет.")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(admins|админы)(?:\s|$)'))
async def cmd_admins(message: Message):
    await try_delete(message)
    if message.chat.type not in ("group", "supergroup"):
        return await message.answer("❌ Только для групповых чатов!")
    parts = message.text.strip().split(maxsplit=1)
    call_text = parts[1] if len(parts) > 1 else "📢 Сбор администраторов!"
    try:
        members = await bot.get_chat_administrators(chat_id=message.chat.id)
        admins = [m.user for m in members if not m.user.is_bot]
        if not admins:
            return await message.answer("❌ Администраторы не найдены.")
        mentions = [f'<a href="tg://user?id={u.id}">{safe_text(u.full_name[:20])}</a>' for u in admins]
        await message.answer(f"📢 {safe_text(call_text)}\n\n" + ", ".join(mentions), parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ================= 📛 ПОЗЫВНЫЕ =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(me|позывной)\s*$'))
async def cmd_me(message: Message):
    await try_delete(message)
    data = await load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    call_sign = user.get("call_sign")
    name = message.from_user.full_name
    if call_sign:
        await message.answer(f"📛 Твой позывной: {call_sign}\n👤 Имя: {safe_text(name)}")
    else:
        await message.answer(f"👤 Имя: {safe_text(name)}\n💡 Установи позывной: <code>!setme 🔥</code>", parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?setme\s+(.+)$'))
async def cmd_setme(message: Message):
    await try_delete(message)
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("❌ Формат: <code>!setme эмодзи</code>", parse_mode="HTML")
    emoji = parts[1].strip()
    if len(emoji) > 8:
        return await message.answer("❌ Позывной должен быть коротким.")
    data = await load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    user["call_sign"] = emoji
    await save_data(data)
    await message.answer(f"✅ Новый позывной: {emoji}")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?changeme\s*$'))
async def cmd_changeme(message: Message):
    await try_delete(message)
    emojis = ["🔥", "⚡", "🐺", "🦊", "🦁", "🐯", "🐲", "👻", "🤖", "👽", "💀", "👑", "🎭", "🦅", "🐍"]
    emoji = random.choice(emojis)
    data = await load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    user["call_sign"] = emoji
    await save_data(data)
    await message.answer(f"✅ Новый случайный позывной: {emoji}")


# ================= 🔧 ПОМОЩЬ =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(помощь|команды|help)\s*$'))
async def cmd_help(message: Message):
    await try_delete(message)
    help_text = (
        "📖 <b>СПИСОК КОМАНД БОТА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 <b>Случайные коины:</b>\n"
        "• Просто пиши в чате — с шансом 5% получишь 1-20 🪙\n\n"
        "📦 <b>Сундуки:</b>\n"
        "• <code>!открыть</code> — открыть сундук (КД 2 часа)\n\n"
        "💰 <b>Экономика:</b>\n"
        "• <code>!карман</code> — твой баланс\n"
        "• <code>!карман</code> (ответом) — баланс другого\n"
        "• <code>!перевод @user сумма</code> — перевести\n"
        "• <code>!перевод сумма</code> (ответом) — быстрый перевод\n"
        "• <code>!топ</code> — топ-10 богачей\n"
        "• <code>!магазин</code> — магазин\n\n"
        "🎲 <b>Игры:</b>\n"
        "• <code>!кубы @user ставка</code> — дуэль в кости\n"
        "• <code>!кубы ставка</code> (ответом) — быстрый вызов\n"
        "• <code>!мины сумма</code> — игра в мины\n\n"
        "📢 <b>Созывы:</b>\n"
        "• <code>!all</code> — массовый созыв\n"
        "• <code>!admins</code> — созыв админов\n"
        "• <code>!stop</code> — остановить созыв\n\n"
        "📛 <b>Позывные:</b>\n"
        "• <code>!me</code> — мой позывной\n"
        "• <code>!setme эмодзи</code> — установить\n"
        "• <code>!changeme</code> — случайный\n\n"
        "• <code>!тег</code> — проверка бота\n"
        "• <code>!помощь</code> — эта справка"
    )
    await message.answer(help_text, parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?тег\s*$'))
async def cmd_tag(message: Message):
    await try_delete(message)
    await message.answer(f"👋 Я на месте, {safe_text(message.from_user.full_name)}!")


# ================= 🎁 FALLBACK =================
@dp.message()
async def handle_all_messages(message: Message):
    try:
        await process_random_coin_drop(message)
    except Exception as e:
        logger.error(f"Ошибка в обработчике дропа: {e}", exc_info=True)


# ================= 🌐 ВЕБ-СЕРВЕР =================
async def start_fake_website():
    port = int(os.environ.get('PORT', 10000))
    app = web.Application()

    async def health_check(request):
        return web.Response(text="Bot is running!")

    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Веб-порт {port} открыт. Render доволен!")


# ================= ЗАПУСК =================
async def main():
    await start_fake_website()
    me = await bot.get_me()
    logger.info(f"🤖 Бот: @{me.username} | can_read_all_group_messages={me.can_read_all_group_messages}")
    if not me.can_read_all_group_messages:
        logger.warning("⚠️ Privacy Mode включён! Бот НЕ видит '!команды' в группах.")
        logger.warning("⚠️ Иди в @BotFather → /setprivacy → Disable → перезапусти бота.")
    print("✅ Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())