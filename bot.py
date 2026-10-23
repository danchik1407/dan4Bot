# -*- coding: utf-8 -*-
import os
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

COOLDOWN_SECONDS = 5 * 60 * 60  # 5 часов
CHALLENGE_TIMEOUT = 60
COIN_DROP_CHANCE = 0.05  # 5%
COIN_DROP_MIN = 1
COIN_DROP_MAX = 20
DROP_IMAGE = "drop.jpg"
MIN_BET = 10  # Минимальная ставка для игр

active_summons: dict[int, dict] = {}
active_challenges: dict[str, dict] = {}
active_mines_games: dict[str, dict] = {}


# ================= БАЗА ДАННЫХ (SUPABASE) =================
async def get_user(user_id: int, username: str = None):
    def _fetch():
        response = supabase.table("users").select("*").eq("user_id", user_id).execute()
        return response.data[0] if response.data else None

    user = await asyncio.to_thread(_fetch)

    if not user:
        new_user = {
            "user_id": user_id,
            "coins": 0,
            "username": username or f"id{user_id}",
            "last_chest": 0,
            "last_call": 0,
            "call_sign": None
        }
        await asyncio.to_thread(lambda: supabase.table("users").insert(new_user).execute())
        return new_user

    if username and user.get("username") != username:
        user["username"] = username
        await asyncio.to_thread(
            lambda: supabase.table("users").update({"username": username}).eq("user_id", user_id).execute())
    return user


async def update_user(user_id: int, **kwargs):
    await asyncio.to_thread(lambda: supabase.table("users").update(kwargs).eq("user_id", user_id).execute())


async def get_all_users():
    def _fetch():
        return supabase.table("users").select("*").execute().data

    return await asyncio.to_thread(_fetch)


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


async def try_delete(message: Message):
    return  # Бот не удаляет сообщения пользователей


# ================= 🎁 РАНДОМНЫЙ ДРОП =================
async def process_random_coin_drop(message: Message):
    if not message.from_user or message.from_user.is_bot: return
    text = message.text or ""
    if text.startswith("!") or text.startswith("/"): return
    if random.random() > COIN_DROP_CHANCE: return

    amount = random.randint(COIN_DROP_MIN, COIN_DROP_MAX)
    user = await get_user(message.from_user.id, message.from_user.username)
    await update_user(message.from_user.id, coins=user["coins"] + amount)

    caption = f"🎉 <b>Поздравляю!</b>\n\n🎁 Ты выиграл(а) <b>{amount} 🪙</b>\n\n💬 Пиши сообщения в чате и получай коины!"
    try:
        if os.path.exists(DROP_IMAGE):
            await message.answer_photo(photo=FSInputFile(DROP_IMAGE), caption=caption, parse_mode="HTML")
        else:
            await message.answer(caption, parse_mode="HTML")
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
    users = await get_all_users()
    users.sort(key=lambda x: x.get("coins", 0), reverse=True)
    msg = "💰 <b>ТОП-10 ПО КОИНАМ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, u in enumerate(users[:10]):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        msg += f"{prefix} {safe_text(u['username'])} — <b>{u['coins']}</b> 🪙\n"
    await message.answer(msg, parse_mode="HTML")


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?открыть\s*$'))
async def cmd_open(message: Message):
    user = await get_user(message.from_user.id, message.from_user.username)
    now = time.time()
    rem = COOLDOWN_SECONDS - (now - user["last_chest"])

    if rem > 0:
        return await message.answer(f"⏳ Открытие доступно через {format_time(rem)}")

    is_gold = random.random() > 0.92
    reward = random.randint(10, 100) if is_gold else random.randint(1, 10)

    # Обновляем и монеты, и время
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
        target = next((SimpleNamespace(id=int(u["user_id"]), username=u["username"]) for u in await get_all_users() if
                       u["username"].lower() == parts[1].lstrip("@").lower()), None)
        if not target: return await message.answer("❌ Пользователь не найден.")
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
async def _start_dice_duel(message: Message, target_user, amount: int):
    challenger = await get_user(message.from_user.id, message.from_user.username)
    if target_user.id == message.from_user.id: return await message.answer("❌ Нельзя играть сам с собой 😄")
    if challenger["coins"] < amount: return await message.answer(
        f"❌ У тебя недостаточно коинов.\nНужно: <b>{amount}</b> 🪙", parse_mode="HTML")

    challenged = await get_user(target_user.id, target_user.username)
    if challenged["coins"] < amount: return await message.answer(f"❌ У @{target_user.username} недостаточно коинов.",
                                                                 parse_mode="HTML")

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

    await message.answer(
        f"🎲 <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
        f"👤 @{message.from_user.username} вызывает @{target_user.username}\n"
        f"💰 Ставка: <b>{amount}</b> 🪙\n🎯 Победитель забирает: <b>{amount * 2}</b> 🪙\n\n"
        f"⏳ Время на ответ: {CHALLENGE_TIMEOUT} секунд",
        parse_mode="HTML", reply_markup=kb,
    )


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
        target = next((SimpleNamespace(id=int(u["user_id"]), username=u["username"]) for u in await get_all_users() if
                       u["username"].lower() == parts[1].lstrip("@").lower()), None)
        if not target: return await message.answer("❌ Пользователь не найден.")
        try:
            amount = int(parts[2])
        except ValueError:
            return await message.answer("❌ Ставка должна быть числом.")

    if amount < MIN_BET: return await message.answer(f"❌ Минимальная ставка: {MIN_BET} 🪙")
    await _start_dice_duel(message, target, amount)


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

    r1, r2 = random.randint(1, 6), random.randint(1, 6)
    if r1 == r2:
        result = f"🤝 <b>Ничья!</b> {r1} : {r2}\nНикто не получает коины."
    else:
        winner_id = ch["challenger_id"] if r1 > r2 else ch["challenged_id"]
        loser_id = ch["challenged_id"] if winner_id == ch["challenger_id"] else ch["challenger_id"]
        winner = await get_user(winner_id)
        loser = await get_user(loser_id)
        await update_user(winner_id, coins=winner["coins"] + amount)
        await update_user(loser_id, coins=loser["coins"] - amount)
        winner_name = f"@{winner['username']}"
        result = f"🏆 Победил {winner_name} ({r1} : {r2})\n💰 +{amount} 🪙"

    active_challenges.pop(challenge_id, None)
    await callback.message.edit_text(f"🎲 <b>Кубы брошены!</b>\n\n{result}", parse_mode="HTML")
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


# ================= 💣 ИГРА МИНЫ =================
MINES_COEFS = {1: 1.25, 2: 1.85, 3: 3.75}


def get_mines_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 💣 (x1.25)", callback_data="mines_start_1"),
         InlineKeyboardButton(text="2 💣 (x1.85)", callback_data="mines_start_2"),
         InlineKeyboardButton(text="3 💣 (x3.75)", callback_data="mines_start_3")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="mines_cancel")]
    ])


@dp.message(F.text & F.text.regexp(r'(?i)^[/!]?мины\s+(\d+)$'))
async def cmd_mines_bet(message: Message):
    amount = int(message.text.split()[1])
    user = await get_user(message.from_user.id, message.from_user.username)

    if amount < MIN_BET: return await message.answer(f"❌ Минимальная ставка: {MIN_BET} 🪙")
    if user["coins"] < amount: return await message.answer(f"❌ Мало коинов! У тебя {user['coins']} 🪙")

    game_id = str(random.randint(100000, 999999))
    active_mines_games[game_id] = {"user_id": message.from_user.id, "amount": amount, "active": False}

    await message.answer(f"💣 <b>Ставка: {amount} 🪙</b>\nВыбери количество мин:", reply_markup=get_mines_kb(),
                         parse_mode="HTML")


@dp.callback_query(F.data.startswith("mines_start_"))
async def cb_mines_start(callback: CallbackQuery):
    mines_count = int(callback.data.split("_")[2])
    game_id = next(
        (gid for gid, g in active_mines_games.items() if g["user_id"] == callback.from_user.id and not g["active"]),
        None)
    if not game_id: return await callback.answer("❌ Напиши !мины <ставка> в чат", show_alert=True)

    game = active_mines_games[game_id]
    game["active"], game["mines_count"] = True, mines_count
    game["board"] = [False] * 16
    game["opened"] = [False] * 16
    for i in random.sample(range(16), mines_count): game["board"][i] = True

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for r in range(4):
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text="⬜", callback_data=f"mines_open_{r * 4 + c}") for c in range(4)])
    kb.inline_keyboard.append([InlineKeyboardButton(text="💰 Забрать (x1.0)", callback_data="mines_cashout")])

    await callback.message.edit_text(
        f"💣 <b>Мины: {mines_count}</b>\nСтавка: <b>{game['amount']}</b> 🪙\nТекущий кэф: <b>x1.0</b>", reply_markup=kb,
        parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("mines_open_"))
async def cb_mines_open(callback: CallbackQuery):
    idx = int(callback.data.split("_")[2])
    game_id = next(
        (gid for gid, g in active_mines_games.items() if g["user_id"] == callback.from_user.id and g["active"]), None)
    if not game_id: return await callback.answer("❌ Игра не найдена.", show_alert=True)

    game = active_mines_games[game_id]
    if game["opened"][idx]: return await callback.answer("Уже открыто!", show_alert=True)

    if game["board"][idx]:  # БУМ
        game["active"] = False
        active_mines_games.pop(game_id, None)
        user = await get_user(callback.from_user.id)
        await update_user(callback.from_user.id, coins=user["coins"] - game["amount"])

        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for r in range(4):
            kb.inline_keyboard.append(
                [InlineKeyboardButton(text="💥" if game["board"][r * 4 + c] else "⬜", callback_data="ignore") for c in
                 range(4)])
        await callback.message.edit_text(f"💥 <b>БАБАХ!</b>\nТы потерял {game['amount']} 🪙", reply_markup=kb,
                                         parse_mode="HTML")
        return await callback.answer("💥 БУМ!")

    game["opened"][idx] = True
    safe_opened = sum(1 for i in range(16) if game["opened"][i] and not game["board"][i])
    total_safe = 16 - game["mines_count"]
    if safe_opened == total_safe: return await cashout_mines(callback, game_id, game)

    base_coef = MINES_COEFS[game["mines_count"]]
    current_coef = round(base_coef * (safe_opened / total_safe), 2)
    if current_coef < 1.0: current_coef = 1.0

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for r in range(4):
        kb.inline_keyboard.append([InlineKeyboardButton(text="✅" if game["opened"][r * 4 + c] else "⬜",
                                                        callback_data="ignore" if game["opened"][
                                                            r * 4 + c] else f"mines_open_{r * 4 + c}") for c in
                                   range(4)])

    win_amount = int(game["amount"] * current_coef)
    kb.inline_keyboard.append([InlineKeyboardButton(text=f"💰 Забрать ({win_amount} 🪙)", callback_data="mines_cashout")])

    await callback.message.edit_text(
        f"💣 <b>Мины: {game['mines_count']}</b>\nСтавка: <b>{game['amount']}</b> 🪙\nТекущий кэф: <b>x{current_coef}</b>\nОткрыто: {safe_opened}/{total_safe}",
        reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "mines_cashout")
async def cb_mines_cashout(callback: CallbackQuery):
    game_id = next(
        (gid for gid, g in active_mines_games.items() if g["user_id"] == callback.from_user.id and g["active"]), None)
    if not game_id: return await callback.answer("❌ Игра не найдена.", show_alert=True)
    await cashout_mines(callback, game_id, active_mines_games[game_id])


async def cashout_mines(callback: CallbackQuery, game_id: str, game: dict):
    game["active"] = False
    active_mines_games.pop(game_id, None)

    safe_opened = sum(1 for i in range(16) if game["opened"][i] and not game["board"][i])
    total_safe = 16 - game["mines_count"]
    coef = round(MINES_COEFS[game["mines_count"]] * (safe_opened / total_safe), 2) if safe_opened > 0 else 1.0
    if coef < 1.0: coef = 1.0
    win_amount = int(game["amount"] * coef)

    user = await get_user(callback.from_user.id)
    profit = win_amount - game["amount"]
    await update_user(callback.from_user.id, coins=user["coins"] + profit)

    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for r in range(4):
        kb.inline_keyboard.append(
            [InlineKeyboardButton(text="💣" if game["board"][r * 4 + c] else "✅", callback_data="ignore") for c in
             range(4)])

    await callback.message.edit_text(
        f"💰 <b>Забрано!</b>\nКэф: <b>x{coef}</b>\nВыигрыш: <b>{win_amount}</b> 🪙\nПрибыль: <b>{profit}</b> 🪙",
        reply_markup=kb, parse_mode="HTML")
    await callback.answer("💰 Забрано!")


@dp.callback_query(F.data == "mines_cancel")
async def cb_mines_cancel(callback: CallbackQuery):
    game_id = next(
        (gid for gid, g in active_mines_games.items() if g["user_id"] == callback.from_user.id and not g["active"]),
        None)
    if game_id: active_mines_games.pop(game_id, None)
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


# ================= 📢 СОЗЫВЫ =================
async def summon_all_members(chat_id: int, call_text: str, starter_id: int):
    active_summons[chat_id] = True
    try:
        users = await get_all_users()
        users_to_tag = [u["user_id"] for u in users if u["user_id"] != starter_id]
        random.shuffle(users_to_tag)

        if not users_to_tag: return await bot.send_message(chat_id, "❌ Некого созывать.")

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
    parts = message.text.strip().split(maxsplit=1)
    call_text = parts[1] if len(parts) > 1 else "📢 Общий сбор!"
    await message.answer(f"🔄 Начинаю созыв...\nТекст: {safe_text(call_text)}", parse_mode="HTML")
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
        target = next((SimpleNamespace(id=int(u["user_id"]), username=u["username"]) for u in await get_all_users() if
                       u["username"].lower() == parts[2].lstrip("@").lower()), None)
        if not target: return await message.answer("❌ Пользователь не найден.")
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
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    if message.chat.type in ("group", "supergroup") and not await is_chat_admin(message.from_user.id,
                                                                                message.chat.id): return await message.answer(
        "❌ Только для админов чата.")

    parts = message.text.strip().split()
    if message.reply_to_message:
        target, amount_idx = message.reply_to_message.from_user, 1
    else:
        if len(parts) < 3: return await message.answer("❌ Формат: <code>!забрать @user 100</code>", parse_mode="HTML")
        target = next((SimpleNamespace(id=int(u["user_id"]), username=u["username"]) for u in await get_all_users() if
                       u["username"].lower() == parts[1].lstrip("@").lower()), None)
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
        "• Просто пиши в чате — с шансом 5% получишь 1-20 🪙\n\n"
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