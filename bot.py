# -*- coding: utf-8 -*-
import os
import random
import time
import asyncio
import logging
from types import SimpleNamespace
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, Dice
from aiogram.enums import ChatMemberStatus
from aiohttp import web
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not BOT_TOKEN:
    raise SystemExit("❌ ОШИБКА: Не найден BOT_TOKEN!")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise SystemExit("❌ ОШИБКА: Не найдены SUPABASE_URL или SUPABASE_KEY!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

COOLDOWN_SECONDS = 5 * 60 * 60
CHALLENGE_TIMEOUT = 60
COIN_DROP_CHANCE = 0.03
COIN_DROP_MIN = 2
COIN_DROP_MAX = 10
DROP_IMAGE = "drop.jpg"
MIN_BET = 10

active_summons: dict[int, bool] = {}
active_challenges: dict[str, dict] = {}
active_mines_games: dict[str, dict] = {}


# ================= БАЗА ДАННЫХ =================
async def get_user(user_id: int, username: str = None, chat_id: int = None):
    def _fetch():
        response = supabase.table("users").select("*").eq("user_id", user_id).execute()
        return response.data[0] if response.data else None

    user = await asyncio.to_thread(_fetch)

    if not user:
        new_user = {
            "user_id": user_id, "coins": 0, "username": username or f"id{user_id}",
            "last_chest": 0, "last_call": 0, "call_sign": None, "chat_id": chat_id
        }
        await asyncio.to_thread(lambda: supabase.table("users").insert(new_user).execute())
        return new_user

    updates = {}
    if username and user.get("username") != username: updates["username"] = username
    if chat_id and user.get("chat_id") != chat_id: updates["chat_id"] = chat_id

    if updates:
        try:
            await asyncio.to_thread(lambda: supabase.table("users").update(updates).eq("user_id", user_id).execute())
            user.update(updates)
        except Exception as e:
            logger.error(f"Ошибка обновления юзера {user_id}: {e}")

    return user


async def update_user(user_id: int, **kwargs):
    await asyncio.to_thread(lambda: supabase.table("users").update(kwargs).eq("user_id", user_id).execute())


async def get_users_from_chat(chat_id: int):
    def _fetch(): return supabase.table("users").select("*").eq("chat_id", chat_id).execute().data

    return await asyncio.to_thread(_fetch)


def find_user_in_list(users_list, arg_username):
    clean_username = arg_username.lstrip("@").lower()
    for u in users_list:
        if u["username"].lower() == clean_username:
            return SimpleNamespace(id=int(u["user_id"]), username=u["username"])
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
    except Exception:
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


# ================= 🎁 ДРОП =================
async def process_random_coin_drop(message: Message):
    if not message.from_user or message.from_user.is_bot: return
    text = message.text or ""
    if text.startswith("!") or text.startswith("/"): return

    chat_id = message.chat.id if message.chat.type in ("group", "supergroup") else None
    user = await get_user(message.from_user.id, message.from_user.username, chat_id)

    if random.random() > COIN_DROP_CHANCE: return

    amount = random.randint(COIN_DROP_MIN, COIN_DROP_MAX)
    await update_user(message.from_user.id, coins=user["coins"] + amount)

    caption = f"🎉 <b>Поздравляю!</b>\n\n🎁 Ты выиграл(а) <b>{amount} 🪙</b>\n\n💬 Пиши сообщения в чате и получай коины!"
    try:
        if os.path.exists(DROP_IMAGE):
            await message.answer_photo(photo=FSInputFile(DROP_IMAGE), caption=caption, parse_mode="HTML",
                                       reply_to_message_id=message.message_id)
        else:
            await message.answer(caption, parse_mode="HTML", reply_to_message_id=message.message_id)
    except Exception:
        pass


# ================= 🛍️ МАГАЗИН =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?магазин\s*$'))
async def cmd_shop(message: Message):
    await message.answer("🛍️ <b>Магазин AURA:</b>\n\n❌ Предметов для покупки пока нет", parse_mode="HTML")


# ================= 💰 ЭКОНОМИКА =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(карман|баланс)\s*$'))
async def cmd_pocket(message: Message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    user = await get_user(target.id, target.username)
    name = f"@{safe_text(target.username)}" if target.username else f"id{target.id}"
    await message.answer(f"💰 <b>Карман {name}</b>\n\n{user['coins']} 🪙", parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(топ)\s*$'))
async def cmd_top(message: Message):
    users = await get_users_from_chat(message.chat.id) if message.chat.type in ("group",
                                                                                "supergroup") else await get_users_from_chat(
        None)
    users.sort(key=lambda x: x.get("coins", 0), reverse=True)
    msg = "💰 <b>ТОП-10 ПО КОИНАМ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, u in enumerate(users[:10]):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        msg += f"{prefix} {safe_text(u['username'])} — <b>{u['coins']}</b> 🪙\n"
    await message.answer(msg, parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?открыть\s*$'))
async def cmd_open(message: Message):
    user = await get_user(message.from_user.id, message.from_user.username, message.chat.id)
    now = time.time()
    last_chest = float(user.get("last_chest") or 0)
    rem = COOLDOWN_SECONDS - (now - last_chest)

    if rem > 0: return await message.answer(f"⏳ Открытие доступно через {format_time(rem)}")

    is_gold = random.random() < 0.05
    reward = random.randint(10, 100) if is_gold else random.randint(5, 25)

    await update_user(message.from_user.id, coins=user["coins"] + reward, last_chest=now)

    if is_gold:
        await message.answer(f"✨ <b>ЗОЛОТОЙ СУНДУК!</b> 🔑\n🎉 Выпало: <b>{reward}</b> 🪙", parse_mode="HTML")
    else:
        await message.answer(f"🎁 <b>Сундук открыт!</b>\n🎉 Выпало: <b>{reward}</b> 🪙", parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?перевод(?:\s|$)'))
async def cmd_transfer(message: Message):
    user = await get_user(message.from_user.id, message.from_user.username)
    parts = message.text.strip().split()

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if len(parts) < 2: return await message.answer("❌ Формат: <code>!перевод 50</code> (ответом)",
                                                       parse_mode="HTML")
        try:
            amount = int(parts[1])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом.")
    else:
        if len(parts) < 3: return await message.answer("❌ Формат: <code>!перевод @user 50</code>", parse_mode="HTML")
        target = find_user_in_list(await get_users_from_chat(message.chat.id), parts[1])
        if not target: return await message.answer("❌ Пользователь не найден в базе.")
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Сумма должна быть числом.")

    if amount <= 0 or target.id == message.from_user.id: return await message.answer(
        "❌ Нельзя переводить себе или сумму <= 0.")
    if user["coins"] < amount: return await message.answer(f"❌ Недостаточно коинов. У тебя: {user['coins']} 🪙")

    target_user = await get_user(target.id, target.username)
    await update_user(message.from_user.id, coins=user["coins"] - amount)
    await update_user(target.id, coins=target_user["coins"] + amount)

    await message.answer(f"✅ <b>Перевод выполнен!</b>\n👤 Кому: @{target.username}\n💸 Сумма: <b>{amount}</b> 🪙",
                         parse_mode="HTML")


# ================= 🎲 КУБЫ =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?кубы(?:\s|$)'))
async def cmd_dice(message: Message):
    parts = message.text.strip().split()
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        if len(parts) < 2: return await message.answer("🎲 Формат: <code>!кубы ставка</code> (ответом)",
                                                       parse_mode="HTML")
        try:
            amount = int(parts[1])
        except ValueError:
            return await message.answer("❌ Ставка должна быть числом.")
    else:
        if len(parts) < 3: return await message.answer("🎲 Формат: <code>!кубы @username ставка</code>",
                                                       parse_mode="HTML")
        target = find_user_in_list(await get_users_from_chat(message.chat.id), parts[1])
        if not target: return await message.answer("❌ Пользователь не найден.")
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Ставка должна быть числом.")

    if amount < MIN_BET: return await message.answer(f"❌ Минимальная ставка: {MIN_BET} 🪙")

    challenger = await get_user(message.from_user.id, message.from_user.username)
    if target.id == message.from_user.id: return await message.answer("❌ Нельзя играть сам с собой 😄")
    if challenger["coins"] < amount: return await message.answer(
        f"❌ У тебя недостаточно коинов.\nНужно: <b>{amount}</b> 🪙", parse_mode="HTML")

    challenged = await get_user(target.id, target.username)
    if challenged["coins"] < amount: return await message.answer(f"❌ У @{target.username} недостаточно коинов.",
                                                                 parse_mode="HTML")

    challenge_id = f"{message.from_user.id}_{target.id}_{int(time.time())}"
    active_challenges[challenge_id] = {"challenger_id": message.from_user.id, "challenged_id": target.id,
                                       "amount": amount}

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"dice_accept|{challenge_id}"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"dice_decline|{challenge_id}"),
    ]])

    await message.answer(
        f"🎲 <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"👤 @{message.from_user.username} вызывает @{target.username}\n"
        f"💰 Ставка: <b>{amount}</b> 🪙\n🎯 Победитель забирает: <b>{amount * 2}</b> 🪙\n\n"
        f"⏳ Время на ответ: {CHALLENGE_TIMEOUT} секунд",
        parse_mode="HTML", reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("dice_accept|"))
async def cb_dice_accept(callback: CallbackQuery):
    challenge_id = callback.data.split("|", 1)[1]
    ch = active_challenges.get(challenge_id)
    if not ch: return await callback.answer("⌛ Вызов неактуален", show_alert=True)
    if callback.from_user.id != ch["challenged_id"]: return await callback.answer("Это не твой вызов!", show_alert=True)

    amount = ch["amount"]
    challenger = await get_user(ch["challenger_id"])
    challenged = await get_user(ch["challenged_id"], callback.from_user.username)

    if challenger["coins"] < amount or challenged["coins"] < amount:
        active_challenges.pop(challenge_id, None)
        return await callback.message.edit_text("❌ У кого-то не хватает коинов. Вызов отменён.")

    await callback.message.edit_reply_markup(reply_markup=None)
    dice_msg1 = await callback.message.answer_dice(emoji="🎲")
    dice_msg2 = await callback.message.answer_dice(emoji="🎲")

    await asyncio.sleep(3.5)
    r1, r2 = dice_msg1.dice.value, dice_msg2.dice.value

    try:
        await dice_msg1.delete()
        await dice_msg2.delete()
    except Exception:
        pass

    if r1 == r2:
        result = f"🤝 <b>Ничья!</b> {r1} : {r2}\nНикто не получает коины."
    else:
        winner_id = ch["challenger_id"] if r1 > r2 else ch["challenged_id"]
        loser_id = ch["challenged_id"] if winner_id == ch["challenger_id"] else ch["challenger_id"]
        winner = await get_user(winner_id)
        loser = await get_user(loser_id)
        await update_user(winner_id, coins=winner["coins"] + amount)
        await update_user(loser_id, coins=loser["coins"] - amount)
        result = f"🏆 Победил @{winner['username']} ({r1} : {r2})\n💰 +{amount} 🪙"

    active_challenges.pop(challenge_id, None)
    await callback.message.answer(f"🎲 <b>Кубы брошены!</b>\n\n{result}", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("dice_decline|"))
async def cb_dice_decline(callback: CallbackQuery):
    challenge_id = callback.data.split("|", 1)[1]
    ch = active_challenges.get(challenge_id)
    if not ch: return await callback.answer("⌛ Вызов неактуален", show_alert=True)
    if callback.from_user.id != ch["challenged_id"]: return await callback.answer("Это не твой вызов!", show_alert=True)
    active_challenges.pop(challenge_id, None)
    await callback.message.edit_text("❌ Вызов отклонён.")
    await callback.answer()


# ================= 💣 МИНЫ =================
MINES_STEP_COEFS = {1: 1.10, 2: 1.15, 3: 1.25}


def get_mines_kb(game_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 💣 (x1.10)", callback_data=f"mines_start_1|{game_id}"),
         InlineKeyboardButton(text="2 💣 (x1.15)", callback_data=f"mines_start_2|{game_id}"),
         InlineKeyboardButton(text="3 💣 (x1.25)", callback_data=f"mines_start_3|{game_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"mines_cancel|{game_id}")]
    ])


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?мины\s+(\d+)$'))
async def cmd_mines_bet(message: Message):
    try:
        await message.delete()
    except:
        pass

    amount = int(message.text.split()[1])
    user = await get_user(message.from_user.id, message.from_user.username, message.chat.id)

    if amount < MIN_BET: return await message.answer(f"❌ Минимальная ставка: {MIN_BET} 🪙")
    if user["coins"] < amount: return await message.answer(f"❌ Мало коинов! У тебя {user['coins']} 🪙")

    game_id = f"{message.from_user.id}_{message.message_id}"
    active_mines_games[game_id] = {
        "user_id": message.from_user.id,
        "amount": amount,
        "active": False
    }

    await message.answer(
        f"💣 <b>Ставка: {amount} 🪙</b>\nВыбери количество мин:",
        reply_markup=get_mines_kb(game_id),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("mines_start_"))
async def cb_mines_start(callback: CallbackQuery):
    parts = callback.data.split("|")
    mines_count = int(parts[0].split("_")[2])
    game_id = parts[1]

    game = active_mines_games.get(game_id)
    if not game:
        return await callback.answer("❌ Игра устарела. Напиши !мины заново.", show_alert=True)
    if game["user_id"] != callback.from_user.id:
        return await callback.answer("❌ Это не твоя игра!", show_alert=True)

    if game["active"]:
        return await callback.answer("❌ Игра уже начата!", show_alert=True)

    game["active"], game["mines_count"] = True, mines_count
    game["board"] = [False] * 16
    game["opened"] = [False] * 16
    for i in random.sample(range(16), mines_count): game["board"][i] = True

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for r in range(4):
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="⬜", callback_data=f"mines_open_{r * 4 + c}|{game_id}")
            for c in range(4)
        ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text=f"💰 Забрать ({game['amount']} 🪙) | Кэф: x1.0",
            callback_data=f"mines_cashout|{game_id}"
        )
    ])

    await callback.message.edit_text(
        f"💣 <b>Мины: {mines_count}</b>\n"
        f"Ставка: <b>{game['amount']}</b> 🪙\n"
        f"Текущий кэф: <b>x1.0</b>\n"
        f"Открыто: 0/16",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mines_open_"))
async def cb_mines_open(callback: CallbackQuery):
    parts = callback.data.split("|")
    idx = int(parts[0].split("_")[2])
    game_id = parts[1]

    game = active_mines_games.get(game_id)
    if not game:
        return await callback.answer("❌ Игра устарела или сброшена.", show_alert=True)
    if game["user_id"] != callback.from_user.id:
        return await callback.answer("❌ Это не твоя игра!", show_alert=True)

    if game["opened"][idx]:
        return await callback.answer("Уже открыто!", show_alert=True)

    if game["board"][idx]:
        game["active"] = False
        active_mines_games.pop(game_id, None)
        user = await get_user(callback.from_user.id)
        await update_user(callback.from_user.id, coins=user["coins"] - game["amount"])

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in range(4):
            kb.inline_keyboard.append([
                InlineKeyboardButton(text="💥" if game["board"][r * 4 + c] else "⬜", callback_data="ignore")
                for c in range(4)
            ])
        await callback.message.edit_text(
            f"💥 <b>БАБАХ!</b>\nТы потерял {game['amount']} 🪙",
            reply_markup=kb,
            parse_mode="HTML"
        )
        return await callback.answer("💥 БУМ!")

    game["opened"][idx] = True
    safe_opened = sum(1 for i in range(16) if game["opened"][i] and not game["board"][i])

    step_coef = MINES_STEP_COEFS[game["mines_count"]]
    current_coef = round(step_coef ** safe_opened, 2)
    win_amount = round(game["amount"] * current_coef)

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for r in range(4):
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text="✅" if game["opened"][r * 4 + c] else "⬜",
                callback_data="ignore" if game["opened"][r * 4 + c] else f"mines_open_{r * 4 + c}|{game_id}"
            ) for c in range(4)
        ])

    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text=f"💰 Забрать ({win_amount} 🪙) | Кэф: x{current_coef}",
            callback_data=f"mines_cashout|{game_id}"
        )
    ])

    await callback.message.edit_text(
        f"💣 <b>Мины: {game['mines_count']}</b>\n"
        f"Ставка: <b>{game['amount']}</b> 🪙\n"
        f"Текущий кэф: <b>x{current_coef}</b>\n"
        f"Открыто: {safe_opened}/16",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("mines_cashout"))
async def cb_mines_cashout(callback: CallbackQuery):
    parts = callback.data.split("|")
    game_id = parts[1]

    game = active_mines_games.get(game_id)
    if not game:
        return await callback.answer("❌ Эта игра уже завершена.", show_alert=True)
    if game["user_id"] != callback.from_user.id:
        return await callback.answer("❌ Это не твоя игра!", show_alert=True)

    game["active"] = False
    active_mines_games.pop(game_id, None)

    safe_opened = sum(1 for i in range(16) if game["opened"][i] and not game["board"][i])

    if safe_opened == 0:
        win_amount, coef = game["amount"], 1.0
    else:
        step_coef = MINES_STEP_COEFS[game["mines_count"]]
        coef = round(step_coef ** safe_opened, 2)
        win_amount = round(game["amount"] * coef)

    user = await get_user(callback.from_user.id)
    profit = win_amount - game["amount"]
    await update_user(callback.from_user.id, coins=user["coins"] + profit)

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for r in range(4):
        kb.inline_keyboard.append([
            InlineKeyboardButton(text="💣" if game["board"][r * 4 + c] else "✅", callback_data="ignore")
            for c in range(4)
        ])

    await callback.message.edit_text(
        f"💰 <b>Забрано!</b>\n"
        f"Кэф: <b>x{coef}</b>\n"
        f"Выигрыш: <b>{win_amount}</b> 🪙\n"
        f"Прибыль: <b>{profit}</b> 🪙",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer("💰 Забрано!")

    # Автоудаление сообщения с результатом через 2 минуты
    await asyncio.sleep(120)
    try:
        await callback.message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение: {e}")


@dp.callback_query(F.data.startswith("mines_cancel|"))
async def cb_mines_cancel(callback: CallbackQuery):
    game_id = callback.data.split("|")[1]

    game = active_mines_games.get(game_id)
    if not game:
        return await callback.answer("❌ Игра уже неактивна.", show_alert=True)
    if game["user_id"] != callback.from_user.id:
        return await callback.answer("❌ Это не твоя игра!", show_alert=True)

    active_mines_games.pop(game_id, None)
    await callback.message.edit_text("❌ Игра отменена.")
    await callback.answer()


@dp.callback_query(F.data == "ignore")
async def cb_ignore(callback: CallbackQuery):
    await callback.answer()


# ================= 📛 ПОЗЫВНЫЕ =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(me|позывной)\s*$'))
async def cmd_me(message: Message):
    user = await get_user(message.from_user.id, message.from_user.username)
    if user.get("call_sign"):
        await message.answer(f"📛 Твой позывной: {user['call_sign']}\n👤 Имя: {safe_text(message.from_user.full_name)}")
    else:
        await message.answer(
            f"👤 Имя: {safe_text(message.from_user.full_name)}\n💡 Установи позывной: <code>!setme 🔥</code>",
            parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?setme\s+(.+)$'))
async def cmd_setme(message: Message):
    emoji = message.text.split(maxsplit=1)[1].strip()
    if len(emoji) > 8: return await message.answer("❌ Слишком длинный позывной.")
    await update_user(message.from_user.id, call_sign=emoji)
    await message.answer(f"✅ Новый позывной: {emoji}")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?changeme\s*$'))
async def cmd_changeme(message: Message):
    emoji = random.choice(["🔥", "⚡", "🐺", "🦊", "🦁", "🐯", "🐲", "👻", "🤖", "👽", "💀", "👑", "🎭", "🦅", "🐍"])
    await update_user(message.from_user.id, call_sign=emoji)
    await message.answer(f"✅ Новый случайный позывной: {emoji}")


# ================= 📢 СОЗЫВЫ (скрыто из помощи) =================
async def summon_all_members(chat_id: int, call_text: str, starter_id: int):
    active_summons[chat_id] = True
    try:
        users = await get_users_from_chat(chat_id)
        users_to_tag = [u["user_id"] for u in users if u["user_id"] != starter_id]
        random.shuffle(users_to_tag)

        if not users_to_tag:
            active_summons[chat_id] = False
            return await bot.send_message(chat_id, "❌ В этом чате пока нет людей в базе.")

        for i in range(0, len(users_to_tag), 25):
            if not active_summons.get(chat_id, False): break
            batch = users_to_tag[i:i + 25]
            mentions = [f'<a href="tg://user?id={uid}">Пользователь</a>' for uid in batch]
            await bot.send_message(chat_id, f"📢 {call_text}\n\n" + "\n".join(mentions), parse_mode="HTML")
            await asyncio.sleep(2.0)
    except Exception as e:
        logger.error(f"Ошибка при созыве: {e}")
    finally:
        active_summons.pop(chat_id, None)


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?all(?:\s|$)'))
async def cmd_all(message: Message):
    if message.chat.type not in ("group", "supergroup"): return await message.answer("❌ Только для групп!")
    if not is_admin(message.from_user.id) and not await is_chat_admin(message.from_user.id, message.chat.id):
        return await message.answer("❌ Только для админов.")

    if active_summons.get(message.chat.id):
        return await message.answer("⚠️ Созыв уже идёт!")

    parts = message.text.strip().split(maxsplit=1)
    call_text = parts[1] if len(parts) > 1 else "📢 Общий сбор!"
    await message.answer(f"🔄 Начинаю созыв...", parse_mode="HTML")
    asyncio.create_task(summon_all_members(message.chat.id, call_text, message.from_user.id))


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(stop|стоп)\s*$'))
async def cmd_stop(message: Message):
    if message.chat.type in ("group", "supergroup"):
        if not is_admin(message.from_user.id) and not await is_chat_admin(message.from_user.id, message.chat.id):
            return await message.answer("❌ Только для админов.")
    if active_summons.get(message.chat.id):
        active_summons[message.chat.id] = False
        await message.answer("⏹️ Созыв прекращён.")
    else:
        await message.answer("❌ Активных созывов нет.")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(admins|админы)(?:\s|$)'))
async def cmd_admins(message: Message):
    if message.chat.type not in ("group", "supergroup"): return await message.answer("❌ Только для групп!")
    parts = message.text.strip().split(maxsplit=1)
    call_text = parts[1] if len(parts) > 1 else "📢 Сбор администраторов!"
    try:
        members = await bot.get_chat_administrators(chat_id=message.chat.id)
        admins = [m.user for m in members if not m.user.is_bot]
        if not admins: return await message.answer("❌ Администраторы не найдены.")
        mentions = [f'<a href="tg://user?id={u.id}">{safe_text(u.full_name[:20])}</a>' for u in admins]
        await message.answer(f"📢 {safe_text(call_text)}\n\n" + ", ".join(mentions), parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ================= 🔧 АДМИН-КОМАНДЫ =================
@dp.message(F.text & F.text.regexp(r'(?i)^\+пополнить\s+карман(?:\s|$)'))
async def cmd_admin_add(message: Message):
    try:
        await message.delete()
    except:
        pass
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    if message.chat.type in ("group", "supergroup") and not await is_chat_admin(message.from_user.id,
                                                                                message.chat.id): return await message.answer(
        "❌ Только для админов чата.")

    parts = message.text.strip().split()
    if message.reply_to_message:
        target, amount_idx = message.reply_to_message.from_user, 2
    else:
        if len(parts) < 4: return await message.answer("❌ Формат: <code>+пополнить карман @user 500</code>",
                                                       parse_mode="HTML")
        target = find_user_in_list(await get_users_from_chat(message.chat.id), parts[2])
        if not target: return await message.answer("❌ Пользователь не найден (пусть напишет в чат).")
        amount_idx = 3

    try:
        amount = int(parts[amount_idx])
    except:
        return await message.answer("❌ Укажи число.")

    user = await get_user(target.id, target.username)
    await update_user(target.id, coins=user["coins"] + amount)
    await message.answer(
        f"💰 <b>Карман пополнен!</b>\n👤 Кому: @{target.username}\n➕ Начислено: <b>{amount}</b> 🪙\n📊 Баланс: <b>{user['coins'] + amount}</b> 🪙",
        parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?забрать(?:\s|$)'))
async def cmd_admin_withdraw(message: Message):
    try:
        await message.delete()
    except:
        pass
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    if message.chat.type in ("group", "supergroup") and not await is_chat_admin(message.from_user.id,
                                                                                message.chat.id): return await message.answer(
        "❌ Только для админов чата.")

    parts = message.text.strip().split()
    if message.reply_to_message:
        target, amount_idx = message.reply_to_message.from_user, 1
    else:
        if len(parts) < 3: return await message.answer("❌ Формат: <code>!забрать @user 100</code>", parse_mode="HTML")
        target = find_user_in_list(await get_users_from_chat(message.chat.id), parts[1])
        if not target: return await message.answer("❌ Пользователь не найден.")
        amount_idx = 2

    try:
        amount = int(parts[amount_idx])
    except:
        return await message.answer("❌ Укажи число.")

    user = await get_user(target.id, target.username)
    if user["coins"] < amount: return await message.answer(f"❌ У пользователя только {user['coins']} 🪙")

    await update_user(target.id, coins=user["coins"] - amount)
    await message.answer(
        f"💸 <b>Коины списаны!</b>\n👤 У кого: @{target.username}\n➖ Снято: <b>{amount}</b> 🪙\nОстаток: <b>{user['coins'] - amount}</b> 🪙",
        parse_mode="HTML")


# ================= 🔧 ПОМОЩЬ =================
@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?(помощь|help)\s*$'))
async def cmd_help(message: Message):
    text = (
        "📖 <b>СПИСОК КОМАНД БОТА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 <b>Случайные коины:</b>\n"
        "• Просто пиши в чате — с шансом 3% получишь 2-10 🪙\n\n"
        "📦 <b>Сундуки:</b>\n"
        "• <code>!открыть</code> — открыть сундук (КД 5 часов)\n\n"
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
        "📛 <b>Позывные:</b>\n"
        "• <code>!me</code> — мой позывной\n"
        "• <code>!setme эмодзи</code> — установить\n"
        "• <code>!changeme</code> — случайный\n\n"
        "• <code>!тег</code> — проверка бота\n"
        "• <code>!помощь</code> — эта справка"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?тег\s*$'))
async def cmd_tag(message: Message):
    await message.answer(f"👋 Я на месте, {safe_text(message.from_user.full_name)}!")


# ================= 🌐 FALLBACK =================
@dp.message()
async def handle_all_messages(message: Message):
    try:
        await process_random_coin_drop(message)
    except Exception as e:
        logger.error(f"Ошибка дропа: {e}")


# ================= 🚀 ЗАПУСК =================
async def start_fake_website():
    port = int(os.environ.get('PORT', 10000))
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="Bot is running!"))
    app.router.add_get('/health', lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()


async def main():
    await start_fake_website()
    me = await bot.get_me()
    logger.info(f"🤖 Бот: @{me.username} | can_read_all_group_messages={me.can_read_all_group_messages}")
    if not me.can_read_all_group_messages:
        logger.warning("⚠️ Privacy Mode ВКЛЮЧЕН! Бот не видит '!команды'. Отключи в BotFather.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())