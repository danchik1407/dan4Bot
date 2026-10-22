# -*- coding: utf-8 -*-
import os
import json
import random
import time
import asyncio
import html as html_module
import logging
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
    print("❌ ОШИБКА: Не найден BOT_TOKEN в файле .env!")

DATA_FILE = "data.json"
COOLDOWN_SECONDS = 5 * 60 * 60  # 5 часов
CHALLENGE_TIMEOUT = 60

# Шанс выпадения коинов (2%)
COIN_DROP_CHANCE = 0.02
COIN_DROP_MIN = 1
COIN_DROP_MAX = 20

active_summons = {}
active_challenges = {}


# ================= БАЗА ДАННЫХ =================
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"users": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return {"users": {}}
            data = json.loads(content)
            if "users" not in data:
                data["users"] = {}
            return data
    except Exception:
        return {"users": {}}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_or_create_user(data, user_id: int, username: str):
    uid_str = str(user_id)
    if uid_str not in data["users"]:
        data["users"][uid_str] = {
            "coins": 0,
            "username": username or f"id{user_id}",
            "last_chest": 0,
            "last_call": 0,
            "call_sign": None
        }
    else:
        u = data["users"][uid_str]
        if username:
            u["username"] = username
        for key in ["last_chest", "last_call", "call_sign"]:
            if key not in u:
                u[key] = 0 if key != "call_sign" else None
    return data["users"][uid_str]


# ================= ИНИЦИАЛИЗАЦИЯ =================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_chat_admin(user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False


def format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}ч {m}м {s}с"
    elif m > 0:
        return f"{m}м {s}с"
    return f"{s} сек"


def get_name_by_id(data, user_id, fallback_username=None):
    uid_str = str(user_id)
    if uid_str in data["users"]:
        u = data["users"][uid_str]
        if u.get("username"):
            return f"@{html_module.escape(u['username'])}"
    if fallback_username:
        return f"@{html_module.escape(fallback_username)}"
    return f"id{user_id}"


def safe_text(text: str) -> str:
    return html_module.escape(str(text))


async def try_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


# =================  СЛУЧАЙНЫЙ ДРОП КОИНОВ =================
async def process_random_coin_drop(message: Message):
    """Обработка случайного выпадения коинов (2% шанс)."""
    if not message.from_user or message.from_user.is_bot:
        return

    if random.random() > COIN_DROP_CHANCE:
        return

    coins_amount = random.randint(COIN_DROP_MIN, COIN_DROP_MAX)

    data = load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    user["coins"] += coins_amount
    save_data(data)

    username = message.from_user.username or f"id{message.from_user.id}"

    try:
        # Берем картинку прямо из папки с ботом
        photo = FSInputFile("drop.jpg")
        await message.answer_photo(
            photo=photo,
            caption=(
                f"🎉 <b>Поздравляю!</b>\n\n"
                f"🎁 Ты выиграл(а) <b>{coins_amount} 🪙</b>\n\n"
                f"💬 Пиши сообщения в чате и получай возможность так же находить Коины!"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        # Если вдруг файл не найден, бот не упадет, а просто отправит текст
        logger.warning(f"Не удалось отправить картинку: {e}")
        await message.answer(
            f"🎉 <b>Поздравляю!</b>\n\n"
            f"🎁 Ты выиграл(а) <b>{coins_amount} 🪙</b>\n\n"
            f"💬 Пиши сообщения в чате и получай возможность так же находить Коины!",
            parse_mode="HTML"
        )

    logger.info(f"💰 Дроп коинов: {message.from_user.id} получил {coins_amount} 🪙")


# ================= 🛍️ МАГАЗИН =================
@dp.message(F.text.regexp(r'(?i)^[/!]?магазин$'))
async def cmd_shop(message: Message):
    await try_delete(message)
    shop_text = "🛍️ <b>Магазин AURA:</b>\n\n❌ Предметов для покупки пока нет"
    await message.answer(shop_text, parse_mode="HTML")


# ================= 💰 СУНДУКИ =================
@dp.message(F.text.regexp(r'(?i)^[/!]?открыть$'))
async def cmd_open(message: Message):
    await try_delete(message)
    user_id = message.from_user.id
    username = message.from_user.username
    data = load_data()
    user = get_or_create_user(data, user_id, username)
    now = time.time()
    last_chest = user.get("last_chest", 0)
    remaining = COOLDOWN_SECONDS - (now - last_chest)

    if remaining > 0:
        await message.answer(f"⏳ Открытие доступно через {format_time(remaining)}")
        return

    roll = random.randint(1, 100)
    is_golden = roll > 92
    reward = random.randint(10, 100) if is_golden else random.randint(1, 10)

    user["coins"] += reward
    user["last_chest"] = now
    save_data(data)

    if is_golden:
        await message.answer(f"✨ <b>ЗОЛОТОЙ СУНДУК!</b> 🔑\n🎉 Тебе выпало: <b>{reward}</b> 🪙", parse_mode="HTML")
    else:
        await message.answer(f"🎁 <b>Сундук открыт!</b>\n🎉 Тебе выпало: <b>{reward}</b> ", parse_mode="HTML")


# ================= 💵 КАРМАН =================
@dp.message(F.text.regexp(r'(?i)^[/!]?посмотреть карман$|^[/!]?карман$|^[/!]?баланс$'))
async def cmd_pocket(message: Message):
    await try_delete(message)
    data = load_data()

    # Если ответ на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
    else:
        target = message.from_user

    user = get_or_create_user(data, target.id, target.username)
    display_name = f"@{target.username}" if getattr(target, 'username', None) else f"id{target.id}"
    await message.answer(f"💰 <b>Карман {safe_text(display_name)}</b>\n\n{user.get('coins', 0)} 🪙", parse_mode="HTML")


# ================= 📊 ТОП =================
@dp.message(F.text.regexp(r'(?i)^[/!]?топ монет$|^[/!]?топ$'))
async def cmd_top_coins(message: Message):
    await try_delete(message)
    data = load_data()
    users_list = [{"username": udata.get("username", f"id{uid}"), "coins": udata.get("coins", 0)}
                  for uid, udata in data["users"].items()]
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
@dp.message(F.text.regexp(r'(?i)^[/!]?перевод$'))
async def cmd_transfer(message: Message):
    await try_delete(message)
    sender_id = message.from_user.id
    sender_username = message.from_user.username
    data = load_data()
    sender = get_or_create_user(data, sender_id, sender_username)

    target_user = None
    amount = 0

    # Если ответ на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        parts = message.text.strip().split()
        if len(parts) < 2:
            await message.answer("❌ Укажи сумму: <code>!перевод 50</code>", parse_mode="HTML")
            return
        try:
            amount = int(parts[1])
        except ValueError:
            await message.answer("❌ Сумма должна быть числом.")
            return
    else:
        # Формат: !перевод @username сумма
        parts = message.text.strip().split()
        if len(parts) < 3:
            await message.answer("❌ Формат: <code>!перевод @username 50</code>", parse_mode="HTML")
            return

        username_arg = parts[1].lstrip("@").lower()
        try:
            amount = int(parts[2])
        except ValueError:
            await message.answer("❌ Сумма должна быть числом.")
            return

        for uid_str, udata in data["users"].items():
            if udata.get("username", "").lower() == username_arg:
                target_user = type('User', (), {'id': int(uid_str), 'username': udata.get("username")})()
                break

        if not target_user:
            await message.answer("❌ Пользователь не найден.")
            return

    if amount <= 0 or target_user.id == sender_id:
        await message.answer("❌ Сумма должна быть > 0 и нельзя переводить самому себе.")
        return

    if sender["coins"] < amount:
        await message.answer(f"❌ Недостаточно коинов. У тебя: <b>{sender['coins']}</b> ", parse_mode="HTML")
        return

    sender["coins"] -= amount
    target = get_or_create_user(data, target_user.id, target_user.username)
    target["coins"] += amount
    save_data(data)

    sender_name = f"@{sender_username}" if sender_username else f"id{sender_id}"
    target_name = f"@{target_user.username}" if target_user.username else f"id{target_user.id}"

    await message.answer(
        f"✅ <b>Перевод выполнен!</b>\n\n👤 От: {safe_text(sender_name)}\n👤 Кому: {safe_text(target_name)}\n💸 Сумма: <b>{amount}</b> 🪙",
        parse_mode="HTML"
    )


# ================= 💸 АДМИН: ЗАБРАТЬ С КАРМАНА =================
@dp.message(F.text.regexp(r'(?i)^[/!]?забрать с кармана$|^[/!]?забрать$'))
async def cmd_admin_withdraw(message: Message):
    """Списание коинов у пользователя (только для админов)."""
    await try_delete(message)

    if not is_admin(message.from_user.id):
        await message.answer("❌ Команда доступна только администраторам.")
        return

    parts = message.text.strip().split()
    target_user = None
    amount = 0

    # Если ответ на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if len(parts) >= 2:
            try:
                amount = int(parts[1])
            except ValueError:
                await message.answer("❌ Укажи число коинов для списания.")
                return
    # Если через @username
    elif len(parts) >= 3:
        username_arg = parts[1].lstrip("@").lower()
        try:
            amount = int(parts[2])
        except ValueError:
            await message.answer("❌ Укажи число коинов для списания.")
            return

        data = load_data()
        for uid_str, udata in data["users"].items():
            if udata.get("username", "").lower() == username_arg:
                target_user = type('User', (), {'id': int(uid_str), 'username': udata.get("username")})()
                break

        if not target_user:
            await message.answer("❌ Пользователь не найден в базе данных.")
            return
    else:
        await message.answer(
            "❌ Формат: <code>!забрать @username 100</code> или ответом на сообщение <code>!забрать 100</code>",
            parse_mode="HTML"
        )
        return

    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше 0.")
        return

    data = load_data()
    target_data = get_or_create_user(data, target_user.id, target_user.username)

    if target_data["coins"] < amount:
        await message.answer(f"❌ У пользователя недостаточно коинов. Текущий баланс: {target_data['coins']} 🪙")
        return

    target_data["coins"] -= amount
    save_data(data)

    target_name = f"@{target_user.username}" if getattr(target_user, 'username', None) else f"id{target_user.id}"

    await message.answer(
        f"💸 <b>Коины списаны!</b>\n\n"
        f"👤 У кого: {safe_text(target_name)}\n"
        f"➖ Снято: <b>{amount}</b> 🪙\n"
        f" Остаток: <b>{target_data['coins']}</b> 🪙",
        parse_mode="HTML"
    )


# =================  КУБЫ =================
@dp.message(F.text.regexp(r'(?i)^[/!]?кубы$'))
async def cmd_dice_challenge(message: Message):
    await try_delete(message)
    parts = message.text.strip().split()
    target_user = None
    amount = 0

    # Если ответ на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if len(parts) < 2:
            await message.answer("🎲 <b>Игра «Кубы»</b>\n\nФормат: <code>!кубы ставка</code>", parse_mode="HTML")
            return
        try:
            amount = int(parts[1])
        except ValueError:
            await message.answer("❌ Ставка должна быть числом.")
            return
    else:
        # Формат: !кубы @username ставка
        if len(parts) < 3:
            await message.answer("🎲 <b>Игра «Кубы»</b>\n\nФормат: <code>!кубы @username ставка</code>",
                                 parse_mode="HTML")
            return

        username_arg = parts[1].lstrip("@").lower()
        try:
            amount = int(parts[2])
        except ValueError:
            await message.answer("❌ Ставка должна быть числом.")
            return

        data = load_data()
        for uid_str, udata in data["users"].items():
            if udata.get("username", "").lower() == username_arg:
                target_user = type('User', (), {'id': int(uid_str), 'username': udata.get("username")})()
                break

        if not target_user:
            await message.answer("❌ Пользователь не найден.")
            return

    if amount <= 0:
        await message.answer("❌ Ставка должна быть больше 0.")
        return

    data = load_data()
    challenger = get_or_create_user(data, message.from_user.id, message.from_user.username)

    if target_user.id == message.from_user.id:
        await message.answer("❌ Нельзя играть сам с собой 😄")
        return

    if challenger["coins"] < amount:
        await message.answer(f" У тебя недостаточно коинов.\nНужно: <b>{amount}</b> 🪙", parse_mode="HTML")
        return

    challenged = get_or_create_user(data, target_user.id, target_user.username)
    if challenged["coins"] < amount:
        await message.answer(f"❌ У {get_name_by_id(data, target_user.id, target_user.username)} недостаточно коинов.",
                             parse_mode="HTML")
        return

    challenge_id = f"{message.from_user.id}_{target_user.id}_{int(time.time())}"
    challenger_name = get_name_by_id(data, message.from_user.id, message.from_user.username)
    challenged_name = get_name_by_id(data, target_user.id, target_user.username)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"dice_accept_{challenge_id}"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"dice_decline_{challenge_id}")
    ]])

    await message.answer(
        f"🎲 <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"👤 {challenger_name} вызывает {challenged_name}\n"
        f"💰 Ставка: <b>{amount}</b> 🪙\n"
        f"🎯 Победитель забирает: <b>{amount * 2}</b> 🪙\n\n"
        f"⏳ Время на ответ: {CHALLENGE_TIMEOUT} секунд",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ================= 📢 СОЗЫВЫ =================
async def summon_all_members(chat_id: int, call_text: str, starter_id: int):
    active_summons[chat_id] = True
    try:
        data = load_data()
        users_to_tag = [int(uid_str) for uid_str in data["users"].keys() if int(uid_str) != starter_id]
        random.shuffle(users_to_tag)

        if not users_to_tag:
            await bot.send_message(chat_id, "❌ Некого созывать.")
            return

        for i in range(0, len(users_to_tag), 15):
            if not active_summons.get(chat_id, False):
                await bot.send_message(chat_id, "⏹️ Созыв остановлен.")
                break

            batch = users_to_tag[i:i + 15]
            mentions = [f"[Пользователь](tg://user?id={uid})" for uid in batch]
            full_text = f"📢 {call_text}\n\n" + "\n".join(mentions)

            await bot.send_message(chat_id, full_text, parse_mode="Markdown")
            await asyncio.sleep(1.5)
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Ошибка при созыве: {e}")
    finally:
        if chat_id in active_summons:
            del active_summons[chat_id]


@dp.message(F.text.regexp(r'(?i)^[/!]?all$'))
async def cmd_all(message: Message):
    await try_delete(message)
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("❌ Эта команда работает только в групповых чатах!")
        return

    user_id = message.from_user.id
    if not is_admin(user_id) and not await is_chat_admin(user_id, message.chat.id):
        await message.answer("❌ Команда недоступна (только для админов чата)")
        return

    parts = message.text.strip().split()
    call_text = " ".join(parts[1:]) if len(parts) > 1 else "📢 Общий сбор всех участников!"

    await message.answer(
        f"🔄 Начинаю массовый созыв...\nТекст: {safe_text(call_text)}\n\n⚠️ Для остановки напишите: <code>!stop</code>",
        parse_mode="HTML")
    asyncio.create_task(summon_all_members(message.chat.id, call_text, user_id))


@dp.message(F.text.regexp(r'(?i)^[/!]?stop$|^[/!]?стоп$'))
async def cmd_stop(message: Message):
    await try_delete(message)
    chat_id = message.chat.id
    if active_summons.get(chat_id, False):
        active_summons[chat_id] = False
        await message.answer("⏹️ Команда остановки принята. Созыв прекратится.")
    else:
        await message.answer("❌ Активных массовых созывов в этом чате нет.")


@dp.message(F.text.regexp(r'(?i)^[/!]?admins$|^[/!]?админы$'))
async def cmd_admins(message: Message):
    await try_delete(message)
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("❌ Эта команда работает только в групповых чатах!")
        return

    parts = message.text.strip().split()
    call_text = " ".join(parts[1:]) if len(parts) > 1 else "📢 Сбор администраторов!"

    try:
        members = await bot.get_chat_administrators(chat_id=message.chat.id)
        admins_to_tag = [m.user for m in members if not m.user.is_bot]
        if not admins_to_tag:
            await message.answer("❌ Администраторы не найдены.")
            return

        mentions = [f"[{u.full_name[:20]}](tg://user?id={u.id})" for u in admins_to_tag]
        await message.answer(f"📢 {call_text}\n\n" + ", ".join(mentions), parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ================= 📛 ПОЗЫВНЫЕ =================
@dp.message(F.text.regexp(r'(?i)^[/!]?me$|^[/!]?позывной$'))
async def cmd_me(message: Message):
    await try_delete(message)
    user_id = message.from_user.id
    data = load_data()
    user = get_or_create_user(data, user_id, message.from_user.username)
    call_sign = user.get("call_sign")
    name = message.from_user.full_name

    if call_sign:
        await message.answer(f"📛 Твой позывной: {call_sign}\n👤 Имя: {safe_text(name)}")
    else:
        await message.answer(f"👤 Имя: {safe_text(name)}\n💡 Установи позывной командой: <code>!setme 🔥</code>",
                             parse_mode="HTML")


@dp.message(F.text.regexp(r'(?i)^[/!]?setme\s+(.+)$'))
async def cmd_setme(message: Message):
    await try_delete(message)
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("❌ Формат: <code>!setme эмодзи</code>", parse_mode="HTML")
        return

    emoji = parts[1]
    if len(emoji) > 8:
        await message.answer("❌ Позывной должен быть коротким.")
        return

    data = load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    user["call_sign"] = emoji
    save_data(data)
    await message.answer(f"✅ Твой новый позывной: {emoji}")


@dp.message(F.text.regexp(r'(?i)^[/!]?changeme$'))
async def cmd_changeme(message: Message):
    await try_delete(message)
    emojis = ["🔥", "⚡", "🐺", "🦊", "", "🦁", "🐯", "🐲", "👻", "🤖", "👽", "", "💀", "👑", "🎭", "🦅", "", "🐍"]
    emoji = random.choice(emojis)

    data = load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    user["call_sign"] = emoji
    save_data(data)
    await message.answer(f"✅ Твой новый случайный позывной: {emoji}")


# =================  АДМИН: ПОПОЛНИТЬ КАРМАН =================
@dp.message(F.text.regexp(r'(?i)^\+пополнить карман$'))
async def cmd_admin_add(message: Message):
    await try_delete(message)
    if not is_admin(message.from_user.id):
        await message.answer(" Команда доступна только администраторам.")
        return

    parts = message.text.strip().split()
    target_user = None
    amount = None

    # Если ответ на сообщение
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        if len(parts) >= 2:
            try:
                amount = int(parts[1])
            except:
                await message.answer("❌ Укажи число.")
                return
    # Если через @username
    elif len(parts) >= 3:
        username_arg = parts[1].lstrip("@").lower()
        try:
            amount = int(parts[2])
        except:
            await message.answer("❌ Укажи число.")
            return

        data = load_data()
        for uid_str, udata in data["users"].items():
            if udata.get("username", "").lower() == username_arg:
                target_user = type('User', (), {'id': int(uid_str), 'username': udata.get("username")})()
                break

        if not target_user:
            await message.answer("❌ Пользователь не найден.")
            return
    else:
        await message.answer("❌ Формат: <code>+пополнить карман @username 500</code> или ответом", parse_mode="HTML")
        return

    if not target_user:
        target_user = message.from_user

    data = load_data()
    user = get_or_create_user(data, target_user.id, target_user.username)
    user["coins"] += amount
    save_data(data)

    target_name = get_name_by_id(data, target_user.id, target_user.username)
    await message.answer(
        f"💰 <b>Карман пополнен!</b>\n\n👤 Кому: {target_name}\n➕ Начислено: <b>{amount}</b> 🪙\n📊 Баланс: <b>{user['coins']}</b> 🪙",
        parse_mode="HTML"
    )


# ================= 🔧 ПОМОЩЬ =================
@dp.message(F.text.regexp(r'(?i)^[/!]?помощь$|^[/!]?команды$|^[/!]?help$'))
async def cmd_help(message: Message):
    await try_delete(message)
    help_text = (
        "📖 <b>СПИСОК КОМАНД БОТА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 <b>Случайные коины:</b>\n"
        "• Просто пиши в чате — с шансом 2% получишь 1-20 🪙\n\n"
        "📦 <b>Сундуки:</b>\n"
        "• <code>!открыть</code> — открыть сундук (КД 5 часов)\n\n"
        "💰 <b>Экономика:</b>\n"
        "• <code>!карман</code> — твой баланс\n"
        "• <code>!карман</code> (ответом) — баланс другого\n"
        "• <code>!перевод @user сумма</code> — перевести\n"
        "• <code>!перевод сумма</code> (ответом) — быстрый перевод\n"
        "• <code>!топ</code> — топ-10 богачей\n"
        "• <code>!магазин</code> — магазин\n\n"
        " <b>Игры:</b>\n"
        "• <code>!кубы @user ставка</code> — дуэль в кости\n"
        "• <code>!кубы ставка</code> (ответом) — быстрый вызов\n\n"
        "📢 <b>Созывы:</b>\n"
        "• <code>!all</code> — массовый созыв\n"
        "• <code>!admins</code> — созыв админов\n"
        "• <code>!stop</code> — остановить созыв\n\n"
        " <b>Позывные:</b>\n"
        "• <code>!me</code> — мой позывной\n"
        "• <code>!setme эмодзи</code> — установить\n"
        "• <code>!changeme</code> — случайный\n\n"
        "🔧 <b>Админ:</b>\n"
        "• <code>+пополнить карман @user сумма</code> — выдать коины\n"
        "• <code>+пополнить карман сумма</code> (ответом) — быстрый способ\n"
        "• <code>!забрать @user сумма</code> — списать коины\n"
        "• <code>!забрать сумма</code> (ответом) — быстрый способ\n\n"
        "• <code>!тег</code> — проверка бота\n"
        "• <code>!помощь</code> — эта справка"
    )
    await message.answer(help_text, parse_mode="HTML")


@dp.message(F.text.regexp(r'(?i)^[/!]?тег$'))
async def cmd_tag(message: Message):
    await try_delete(message)
    await message.answer(f"👋 Я на месте, {safe_text(message.from_user.full_name)}!")


# ================= 🎁 ОБРАБОТЧИК СООБЩЕНИЙ (ДРОП КОИНОВ) =================
@dp.message()
async def handle_all_messages(message: Message):
    try:
        await process_random_coin_drop(message)
    except Exception as e:
        logger.error(f"Ошибка в обработчике дропа: {e}")


# ================= 🌐 ВЕБ-СЕРВЕР ДЛЯ RENDER =================
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
    print("✅ Бот запущен!")
    print(f"🔐 Админы: {ADMIN_IDS}")
    print("🎁 Случайный дроп: 2% шанс, 1-20 ")
    print("📦 Сундуки, 💰 Экономика, 🎲 Кубы, 📢 Созывы, 🛍️ Магазин")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())