# -*- coding: utf-8 -*-
import os
import random
import time
import asyncio
import logging
import json
from types import SimpleNamespace
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, Dice, \
    WebAppInfo
from aiogram.enums import ChatMemberStatus
from aiohttp import web
from supabase import create_client, Client
from shop_catalog import ShopPurchaseError, parse_inventory, purchase_shop_item, visible_shop_items

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [6338018270, 5184813347, 8890834384]

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not BOT_TOKEN: raise SystemExit("❌ ОШИБКА: Не найден BOT_TOKEN!")
if not SUPABASE_URL or not SUPABASE_KEY: raise SystemExit("❌ ОШИБКА: Не найдены SUPABASE_URL или SUPABASE_KEY!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

COOLDOWN_SECONDS = 5 * 60 * 60
CHALLENGE_TIMEOUT = 60
COIN_DROP_CHANCE = 0.005
COIN_DROP_MIN = 1
COIN_DROP_MAX = 5
DROP_IMAGE = "drop.jpg"
MIN_BET = 10
MAX_BET = 10000
DAILY_TRANSFER_LIMIT = 5000

active_summons: dict[int, bool] = {}
active_challenges: dict[str, dict] = {}


async def get_user(user_id: int, username: str = None, chat_id: int = None):
    def _fetch():
        response = supabase.table("users").select("*").eq("user_id", user_id).execute()
        return response.data[0] if response.data else None

    user = await asyncio.to_thread(_fetch)
    if not user:
        new_user = {
            "user_id": user_id, "coins": 0, "keys": 0, "username": username or f"id{user_id}",
            "last_chest": 0, "last_call": 0, "last_daily_case": 0, "call_sign": None, "chat_id": chat_id,
            "inventory": "[]", "daily_transfers": 0, "last_transfer_reset": 0
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


async def get_user_global(arg_username: str):
    clean_username = arg_username.lstrip("@").lower()

    def _fetch(): return supabase.table("users").select("*").ilike("username", clean_username).execute().data

    res = await asyncio.to_thread(_fetch)
    if res: return SimpleNamespace(id=int(res[0]["user_id"]), username=res[0]["username"])
    return None


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_admin(user_id: int) -> bool: return user_id in ADMIN_IDS


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


def strip_prefix(text: str) -> str:
    """Убирает ! или / из начала строки"""
    return text.lstrip("!/").strip()


async def process_random_coin_drop(message: Message):
    if not message.from_user or message.from_user.is_bot: return
    if message.chat.type not in ("group", "supergroup"): return
    text = message.text or ""
    if text.startswith("!") or text.startswith("/"): return
    chat_id = message.chat.id
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


# ================= АДМИН ПОМОЩЬ =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("адм помощь")))
async def cmd_admin_help(message: Message):
    if not is_admin(message.from_user.id): return await message.answer(
        "❌ Эта команда доступна только администраторам бота.")

    text = (
        "👑 <b>АДМИН-ПАНЕЛЬ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🚫 <b>Баны и модерация:</b>\n"
        "• <code>бан @user Причина</code> — забанить (обнуляет баланс и капсулы)\n"
        "• <code>разбан @user</code> — разбанить\n"
        "• <code>список банов</code> — список всех забаненных\n"
        "• <code>забрать капсулы @user</code> — удалить все капсулы\n"
        "• <code>забрать всё @user</code> — полностью очистить инвентарь и баланс\n\n"
        "💰 <b>Экономика:</b>\n"
        "• <code>+пополнить карман @user 500</code> — выдать коины\n"
        "• <code>забрать @user 100</code> — списать коины\n"
        "• <code>выдать ключ @user 5</code> — выдать ключи\n\n"
        "🎟️ <b>Промокоды:</b>\n"
        "• <code>+промокод ЛЕТО 100 500</code> — создать (100 активаций, 500 коинов)\n"
        "• <code>-удалить промокод ЛЕТО</code> — удалить\n\n"
        "📢 <b>Созывы:</b>\n"
        "• <code>all Текст</code> — пингануть всех в чате\n"
        "• <code>админы</code> — пингануть админов чата\n"
        "• <code>стоп</code> — остановить созыв\n\n"
        "📊 <b>Статистика:</b>\n"
        "• <code>стата бота</code> — общая статистика (работает только в ЛС)\n\n"
        "💡 <i>Совет: писать команды лучше в ЛС с ботом, чтобы не спалить промокоды.</i>"
    )
    await message.answer(text, parse_mode="HTML")


# ================= БАНЫ =================
@dp.message(
    F.text.func(lambda t: strip_prefix(t).lower().startswith("бан") or strip_prefix(t).lower().startswith("разбан")))
async def cmd_admin_ban_unban(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")

    clean_text = strip_prefix(message.text).lower()
    parts = strip_prefix(message.text).split(maxsplit=2)
    target = None
    reason = "Багоюз"

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if len(parts) > 1: reason = " ".join(parts[1:])
    else:
        if len(parts) >= 2:
            target = await get_user_global(parts[1])
            if not target: return await message.answer("❌ Пользователь не найден в базе.")
            if len(parts) >= 3: reason = parts[2]
        else:
            return await message.answer("❌ Формат: <code>бан @user Причина</code>", parse_mode="HTML")

    try:
        if clean_text.startswith("бан"):
            await asyncio.to_thread(
                lambda: supabase.table("bans").upsert({"user_id": target.id, "reason": reason}).execute())
            await update_user(target.id, coins=0, keys=0, inventory="[]")

            try:
                await bot.send_message(target.id,
                                       f"🚫 <b>Вы были забанены в боте.</b>\n\nПричина: <i>{reason}</i>\n\nВы не можете пользоваться ботом и заходить в мини-приложение, пока вас не разбанят.")
            except:
                pass

            await message.answer(
                f"✅ Пользователь <b>@{target.username}</b> забанен.\nПричина: <i>{reason}</i>\nБаланс обнулен, капсулы удалены.",
                parse_mode="HTML")
        elif clean_text.startswith("разбан"):
            await asyncio.to_thread(lambda: supabase.table("bans").delete().eq("user_id", target.id).execute())
            await message.answer(f"✅ Пользователь <b>@{target.username}</b> разбанен.", parse_mode="HTML")
            try:
                await bot.send_message(target.id,
                                       "✅ <b>Вы были разбанены!</b>\n\nТеперь вы снова можете пользоваться ботом и мини-приложением.")
            except:
                pass
    except Exception as e:
        await message.answer(
            f"❌ Ошибка базы данных: <code>{str(e)}</code>\n\nУбедись, что таблица <code>bans</code> создана в Supabase.",
            parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("список банов")))
async def cmd_admin_banlist(message: Message):
    if not is_admin(message.from_user.id): return
    try:
        def _fetch():
            return supabase.table("bans").select("*").execute().data

        bans = await asyncio.to_thread(_fetch)
        if not bans: return await message.answer("Список банов пуст.")
        text = "🚫 <b>Список забаненных:</b>\n\n"
        for b in bans: text += f"ID: <code>{b['user_id']}</code> | Причина: {b['reason']}\n"
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ================= УПРАВЛЕНИЕ КАПСУЛАМИ И БАЛАНСОМ =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("забрать капсулы")))
async def cmd_admin_take_caps(message: Message):
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    parts = strip_prefix(message.text).split()
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
    else:
        if len(parts) >= 3:
            target = await get_user_global(parts[2])
            if not target: return await message.answer("❌ Пользователь не найден.")
        else:
            return await message.answer("❌ Формат: <code>забрать капсулы @user</code>", parse_mode="HTML")
    await update_user(target.id, inventory="[]")
    await message.answer(f"✅ У пользователя <b>@{target.username}</b> забраны все капсулы.")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("забрать всё")))
async def cmd_admin_wipe_all(message: Message):
    try:
        await message.delete()
    except:
        pass
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    parts = strip_prefix(message.text).split()
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
    elif len(parts) >= 3:
        target = await get_user_global(parts[2])
        if not target: return await message.answer("❌ Пользователь не найден.")
    else:
        return await message.answer("❌ Формат: <code>забрать всё @user</code> или ответом на сообщение",
                                    parse_mode="HTML")
    user = await get_user(target.id, target.username)
    old_coins = user.get("coins", 0)
    old_keys = user.get("keys", 0)
    await update_user(target.id, coins=0, keys=0, inventory="[]")
    await message.answer(
        f"🧹 <b>Полная зачистка!</b>\n\n👤 Кому: @{target.username}\n➖ Снято коинов: <b>{old_coins}</b> 🪙\n➖ Удалено ключей: <b>{old_keys}</b> 🔑\n➖ Удалены капсулы.\n📊 Новый баланс: <b>0</b> 🪙",
        parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("забрать")))
async def cmd_admin_withdraw(message: Message):
    clean_text = strip_prefix(message.text).lower()
    if clean_text.startswith("забрать всё") or clean_text.startswith("забрать капсулы"): return
    try:
        await message.delete()
    except:
        pass
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    parts = strip_prefix(message.text).split()
    if message.reply_to_message:
        target, amount_idx = message.reply_to_message.from_user, 1
    else:
        if len(parts) < 3: return await message.answer("❌ Формат: <code>забрать @user 100</code>", parse_mode="HTML")
        target = await get_user_global(parts[1])
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


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("+пополнить карман")))
async def cmd_admin_add(message: Message):
    try:
        await message.delete()
    except:
        pass
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    parts = strip_prefix(message.text).split()
    if message.reply_to_message:
        target, amount_idx = message.reply_to_message.from_user, 2
    else:
        if len(parts) < 4: return await message.answer("❌ Формат: <code>+пополнить карман @user 500</code>",
                                                       parse_mode="HTML")
        target = await get_user_global(parts[2])
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


# ================= ПЕРЕВОДЫ =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("перевод")))
async def cmd_transfer(message: Message):
    user = await get_user(message.from_user.id, message.from_user.username)
    parts = strip_prefix(message.text).split(maxsplit=2)
    comment = ""
    if len(parts) >= 3:
        comment = parts[2]
    elif len(parts) >= 2 and not parts[1].startswith("@"):
        comment = parts[1]
    amount = 0
    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if len(parts) >= 2:
            try:
                amount = int(parts[1].split()[0])
            except:
                pass
    else:
        if len(parts) >= 2:
            if parts[1].startswith("@"):
                target = await get_user_global(parts[1].split()[0])
                if not target: return await message.answer("❌ Пользователь не найден в базе (даже глобально).")
                if len(parts) >= 3:
                    try:
                        amount = int(parts[2].split()[0])
                    except:
                        pass
            else:
                return await message.answer("❌ Формат: <code>перевод @user 500 Комментарий</code> (или ответом)",
                                            parse_mode="HTML")
    if amount <= 0: return await message.answer("❌ Сумма перевода должна быть больше 0.")
    if target.id == message.from_user.id: return await message.answer("❌ Нельзя переводить себе.")

    today = int(time.time() // 86400)
    last_reset = user.get("last_transfer_reset", 0) or 0
    daily_sent = user.get("daily_transfers", 0) or 0
    if today != last_reset: daily_sent = 0

    if int(message.from_user.id) not in ADMIN_IDS:
        if amount > DAILY_TRANSFER_LIMIT: return await message.answer(
            f"❌ Максимальная сумма перевода {DAILY_TRANSFER_LIMIT} 🪙")
        if daily_sent + amount > DAILY_TRANSFER_LIMIT: return await message.answer(
            f"❌ Превышен дневной лимит переводов ({DAILY_TRANSFER_LIMIT} 🪙). Вы уже отправили сегодня: {daily_sent} 🪙")

    if user["coins"] < amount: return await message.answer(f"❌ Недостаточно коинов. У тебя: {user['coins']} 🪙")

    target_user = await get_user(target.id, target.username)
    new_daily_sent = daily_sent + amount
    await update_user(message.from_user.id, coins=user["coins"] - amount, daily_transfers=new_daily_sent,
                      last_transfer_reset=today)
    await update_user(target.id, coins=target_user["coins"] + amount)

    text = f"✅ <b>Перевод выполнен!</b>\n👤 Кому: @{target.username}\n💸 Сумма: <b>{amount}</b> 🪙"
    if comment: text += f"\n💬 Комментарий к переводу: <i>{safe_text(comment)}</i>"

    if int(message.from_user.id) not in ADMIN_IDS:
        text += f"\n📊 Лимит: {new_daily_sent}/{DAILY_TRANSFER_LIMIT}"

    await message.answer(text, parse_mode="HTML")


# ================= ПРОМОКОДЫ =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("+промокод")))
async def cmd_admin_promocode(message: Message):
    if not is_admin(message.from_user.id): return
    try:
        await message.delete()
    except:
        pass
    parts = strip_prefix(message.text).split()
    if len(parts) < 4: return await message.answer("❌ Формат: <code>+промокод НАЗВАНИЕ 10 500</code>",
                                                   parse_mode="HTML")
    code = parts[1]
    try:
        uses, amount = int(parts[2]), int(parts[3])
    except:
        return await message.answer("❌ Активации и сумма должны быть числами.")

    def _add():
        try:
            supabase.table("promocodes").insert({"code": code, "uses": uses, "amount": amount}).execute()
        except Exception as e:
            print(e)

    await asyncio.to_thread(_add)
    await message.answer(f"✅ Промокод <code>{code}</code> создан!\nАктиваций: {uses}\nНаграда: {amount} 🪙",
                         parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("-удалить промокод")))
async def cmd_admin_delpromo(message: Message):
    if not is_admin(message.from_user.id): return
    try:
        await message.delete()
    except:
        pass
    parts = strip_prefix(message.text).split()
    if len(parts) < 3: return await message.answer("❌ Формат: <code>-удалить промокод НАЗВАНИЕ</code>",
                                                   parse_mode="HTML")
    code = parts[2]

    def _del():
        supabase.table("promocodes").delete().eq("code", code).execute()

    await asyncio.to_thread(_del)
    await message.answer(f"✅ Промокод <code>{code}</code> удалён.", parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("промо")))
async def cmd_promo(message: Message):
    parts = strip_prefix(message.text).split()
    if len(parts) < 2: return await message.answer("❌ Формат: <code>промо КОД</code>", parse_mode="HTML")
    code = parts[1].strip()

    def _fetch():
        return supabase.table("promocodes").select("*").eq("code", code).execute()

    res = await asyncio.to_thread(_fetch)
    if not res.data: return await message.answer("❌ Промокод не найден.")
    promo = res.data[0]
    if promo["uses"] <= 0: return await message.answer("❌ Этот промокод уже закончился.")
    user_id = str(message.from_user.id)

    def _check_used():
        return supabase.table("promocode_uses").select("*").eq("code", code).eq("user_id", user_id).execute()

    used_res = await asyncio.to_thread(_check_used)
    if used_res.data: return await message.answer("❌ Вы уже активировали этот промокод.")
    new_uses = promo["uses"] - 1
    await asyncio.to_thread(lambda: supabase.table("promocodes").update({"uses": new_uses}).eq("code", code).execute())
    user = await get_user(message.from_user.id, message.from_user.username)
    await update_user(message.from_user.id, coins=user["coins"] + promo["amount"])
    await asyncio.to_thread(
        lambda: supabase.table("promocode_uses").insert({"code": code, "user_id": user_id}).execute())
    await message.answer(
        f"🎉 <b>Промокод активирован!</b>\n\n🎁 Вы получили: <b>{promo['amount']}</b> 🪙\n💰 Новый баланс: <b>{user['coins'] + promo['amount']}</b> 🪙",
        parse_mode="HTML")


# ================= КАРМАН (обновлено: поддержка @юзера) =================
@dp.message(
    F.text.func(lambda t: strip_prefix(t).lower().startswith("карман") or strip_prefix(t).lower().startswith("баланс")))
async def cmd_pocket(message: Message):
    parts = strip_prefix(message.text).split()

    target = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
    elif len(parts) >= 2 and parts[1].startswith("@"):
        target = await get_user_global(parts[1])
        if not target: return await message.answer("❌ Пользователь не найден в базе.")
    else:
        target = message.from_user

    user = await get_user(target.id, getattr(target, 'username', None))

    if target.id in ADMIN_IDS and message.from_user.id != target.id:
        return await message.answer(f"💰 <b>Карман @{safe_text(target.username)}</b>\n\n💎 Баланс: <b>Скрыто</b> 🔒",
                                    parse_mode="HTML")

    inv = parse_inventory(user.get("inventory"))
    name = f"@{safe_text(target.username)}" if getattr(target, 'username', None) else f"id{target.id}"
    text = f"💰 <b>Карман {name}</b>\n\n💎 Баланс: <b>{user['coins']}</b> 🪙\n"
    if user.get("keys", 0) > 0: text += f"🔑 Ключи: <b>{user['keys']}</b>\n"
    inventory_lines = []
    for item_id, item in visible_shop_items(is_admin(message.from_user.id)).items():
        count = inv.count(item_id)
        if count:
            inventory_lines.append(f"{item['emoji']} {item['short_name']}: <b>{count} шт.</b>\n")
    if inventory_lines:
        text += "\n🎒 <b>Инвентарь:</b>\n" + "".join(inventory_lines)
    else:
        text += "\n🎒 <b>Инвентарь:</b> Пусто\n"
    await message.answer(text, parse_mode="HTML")


# ================= ТОП =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("топ")))
async def cmd_top(message: Message):
    users = await get_users_from_chat(message.chat.id) if message.chat.type in ("group",
                                                                                "supergroup") else await get_users_from_chat(
        None)
    users = [u for u in users if int(u["user_id"]) not in ADMIN_IDS]
    users.sort(key=lambda x: x.get("coins", 0), reverse=True)
    msg = "💰 <b>ТОП-10 ПО КОИНАМ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, u in enumerate(users[:10]):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        username = u.get("username", "Игрок")
        display_name = username if len(username) < 20 else username[:17] + "..."
        msg += f"{prefix} {safe_text(display_name)} — <b>{u['coins']}</b> 🪙\n"
    await message.answer(msg, parse_mode="HTML")


# ================= СУНДУКИ =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("открыть")))
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


# ================= МАГАЗИН =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("магазин")))
async def cmd_shop(message: Message):
    lines = ["🛍️ <b>Магазин AURA</b>", "", "Выбери капсулу — покупка появится в кармане:"]
    buttons = []
    for item_id, item in visible_shop_items(is_admin(message.from_user.id)).items():
        limit_text = f" · лимит {item['daily_limit']}/день" if item["daily_limit"] is not None else ""
        price_text = f"{item['price']:,}".replace(",", " ")
        lines.append(f"{item['emoji']} <b>{item['name']}</b> — <b>{price_text}</b> 🪙{limit_text}")
        buttons.append(InlineKeyboardButton(
            text=f"{item['emoji']} {item['short_name']} · {price_text} 🪙",
            callback_data=f"shop_buy|{item_id}",
        ))
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ================= ВЫДАТЬ КЛЮЧИ =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("выдать ключ")))
async def cmd_admin_give_key(message: Message):
    try:
        await message.delete()
    except:
        pass
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    parts = strip_prefix(message.text).split()
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        if len(parts) < 3:
            amount = 1
        else:
            try:
                amount = int(parts[2])
            except:
                return await message.answer("❌ Укажи число ключей.")
    else:
        if len(parts) < 4: return await message.answer("❌ Формат: <code>выдать ключ @user 5</code>", parse_mode="HTML")
        target = await get_user_global(parts[2])
        if not target: return await message.answer("❌ Пользователь не найден в базе.")
        try:
            amount = int(parts[3])
        except:
            return await message.answer("❌ Укажи число ключей.")
    if amount <= 0: return await message.answer("❌ Количество должно быть больше 0.")
    user = await get_user(target.id, target.username)
    new_keys = user.get("keys", 0) + amount
    await update_user(target.id, keys=new_keys)
    await message.answer(
        f"🔑 <b>Ключи выданы!</b>\n\n👤 Кому: @{safe_text(target.username)}\n➕ Выдано: <b>{amount}</b> шт.\n📊 Всего ключей: <b>{new_keys}</b>",
        parse_mode="HTML")


# ================= КУБЫ =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("кубы")))
async def cmd_dice(message: Message):
    parts = strip_prefix(message.text).split()
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        if len(parts) < 2: return await message.answer("🎲 Формат: <code>кубы ставка</code> (ответом)",
                                                       parse_mode="HTML")
        try:
            amount = int(parts[1])
        except ValueError:
            return await message.answer("❌ Ставка должна быть числом.")
    else:
        if len(parts) < 3: return await message.answer("🎲 Формат: <code>кубы @username ставка</code>",
                                                       parse_mode="HTML")
        target = await get_user_global(parts[1])
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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"dice_accept|{challenge_id}"),
         InlineKeyboardButton(text="❌ Отказ", callback_data=f"dice_decline|{challenge_id}")]])
    await message.answer(
        f"🎲 <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n👤 @{message.from_user.username} вызывает @{target.username}\n💰 Ставка: <b>{amount}</b> 🪙\n🎯 Победитель забирает: <b>{amount * 2}</b> 🪙\n\n⏳ Время на ответ: {CHALLENGE_TIMEOUT} секунд",
        parse_mode="HTML", reply_markup=kb)


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


async def _complete_shop_purchase(callback: CallbackQuery, item_id: str):
    item = visible_shop_items(is_admin(callback.from_user.id)).get(item_id)
    if item is None:
        return await callback.answer("❌ Неизвестный товар", show_alert=True)
    try:
        await get_user(callback.from_user.id, callback.from_user.username)
        await asyncio.to_thread(purchase_shop_item, supabase, callback.from_user.id, item_id, ADMIN_IDS)
    except ShopPurchaseError as exc:
        logger.warning("Покупка %s пользователем %s отклонена: %s", item_id, callback.from_user.id, exc.message)
        return await callback.answer(f"❌ {exc.message}", show_alert=True)
    except Exception:
        logger.exception("Не удалось обработать покупку %s пользователем %s", item_id, callback.from_user.id)
        return await callback.answer("❌ Не удалось завершить покупку. Попробуйте позже.", show_alert=True)
    await callback.answer(f"✅ {item['name']} куплена за {item['price']:,} 🪙", show_alert=True)


@dp.callback_query(F.data.startswith("shop_buy|"))
async def cb_buy_shop_item(callback: CallbackQuery):
    item_id = callback.data.split("|", 1)[1]
    await _complete_shop_purchase(callback, item_id)


# Keep already-sent keyboards functional after updating the callback format.
@dp.callback_query(F.data == "buy_capsule")
async def cb_buy_capsule_legacy(callback: CallbackQuery):
    await _complete_shop_purchase(callback, "capsule")


@dp.callback_query(F.data == "buy_uniq_capsule")
async def cb_buy_uniq_capsule_legacy(callback: CallbackQuery):
    await _complete_shop_purchase(callback, "uniq_capsule")

# ================= СТАТИСТИКА =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("стата бота")))
async def cmd_bot_stats(message: Message):
    if not is_admin(message.from_user.id): return await message.answer(
        "❌ Эта команда доступна только администраторам бота.")
    if message.chat.type != "private": return await message.answer(
        "❌ Эта команда работает только в личных сообщениях с ботом.")
    try:
        response = await asyncio.to_thread(lambda: supabase.table("users").select("*").execute())
        users = response.data
        total_users = len(users)
        now = time.time()
        active_24h = sum(1 for u in users if u.get("last_chest") and (now - float(u.get("last_chest"))) < 86400)
        total_coins = sum(u.get("coins", 0) for u in users)
        text = (
            f"📊 <b>СТАТИСТИКА БОТА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n👥 Всего пользователей: <b>{total_users}</b>\n🔥 Активных за 24ч: <b>{active_24h}</b>\n💰 Всего коинов в системе: <b>{total_coins}</b> 🪙\n")
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await message.answer(f"❌ Ошибка при получении статистики: {e}")


# ================= ПОМОЩЬ =================
@dp.message(
    F.text.func(lambda t: strip_prefix(t).lower().startswith("помощь") or strip_prefix(t).lower().startswith("help")))
async def cmd_help(message: Message):
    text = (
        "📖 <b>СПИСОК КОМАНД БОТА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n🎁 <b>Случайные коины:</b>\n• Просто пиши в чате — с шансом 0.5% получишь 1-5 🪙\n\n🎟️ <b>Промокоды:</b>\n• <code>промо код</code> — активировать промокод\n\n📦 <b>Сундуки:</b>\n• <code>открыть</code> — открыть сундук (КД 5 часов)\n\n💰 <b>Экономика:</b>\n• <code>карман</code> — твой баланс\n• <code>карман @user</code> — баланс другого\n• <code>перевод @user сумма</code> — перевести\n• <code>перевод сумма</code> (ответом) — быстрый перевод\n• <code>топ</code> — топ-10 богачей\n• <code>магазин</code> — магазин\n\n🎲 <b>Игры:</b>\n• <code>кубы @user ставка</code> — дуэль в кости\n• <code>кубы ставка</code> (ответом) — быстрый вызов\n\n📱 <b>Мини-приложение:</b>\n• <code>апка</code> — ссылка на наше казино\n\n• <code>тег</code> — проверка бота\n• <code>помощь</code> — эта справка")
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("тег")))
async def cmd_tag(message: Message):
    await message.answer(f"👋 Я на месте, {safe_text(message.from_user.full_name)}!")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("апка")))
async def cmd_apka(message: Message):
    text = (
        "📱 <b>Наше Mini App!</b>\n\n🎮 Заходи в наше казино прямо из Telegram:\n👉 <a href='http://t.me/Auragameplay_bot/Auragame'>http://t.me/Auragameplay_bot/Auragame</a>\n\n💰 Там тебя ждут сундуки, рулетка, мины и краш с общим балансом!")
    await message.answer(text, parse_mode="HTML")


# ================= СКРЫТЫЕ КОМАНДЫ =================
@dp.message(F.text.func(lambda t: any(strip_prefix(t).lower().startswith(x) for x in
                                      ["all", "стоп", "admins", "админы", "setme", "changeme", "me", "позывной"])))
async def cmd_misc(message: Message):
    clean_text = strip_prefix(message.text).lower()
    if clean_text.startswith("all"):
        if message.chat.type not in ("group", "supergroup"): return await message.answer("❌ Только для групп!")
        if not is_admin(message.from_user.id) and not await is_chat_admin(message.from_user.id,
                                                                          message.chat.id): return await message.answer(
            "❌ Только для админов.")
        if active_summons.get(message.chat.id): return await message.answer("⚠️ Созыв уже идёт!")
        parts = strip_prefix(message.text).split(maxsplit=1)
        call_text = parts[1] if len(parts) > 1 else "📢 Общий сбор!"
        await message.answer(f"🔄 Начинаю созыв...", parse_mode="HTML")
        asyncio.create_task(summon_all_members(message.chat.id, call_text, message.from_user.id))
    elif clean_text.startswith("стоп"):
        if message.chat.type in ("group", "supergroup"):
            if not is_admin(message.from_user.id) and not await is_chat_admin(message.from_user.id,
                                                                              message.chat.id): return await message.answer(
                "❌ Только для админов.")
        if active_summons.get(message.chat.id):
            active_summons[message.chat.id] = False
            await message.answer("⏹️ Созыв прекращён.")
        else:
            await message.answer("❌ Активных созывов нет.")
    elif clean_text.startswith("admins") or clean_text.startswith("админы"):
        if message.chat.type not in ("group", "supergroup"): return await message.answer("❌ Только для групп!")
        parts = strip_prefix(message.text).split(maxsplit=1)
        call_text = parts[1] if len(parts) > 1 else "📢 Сбор администраторов!"
        try:
            members = await bot.get_chat_administrators(chat_id=message.chat.id)
            admins = [m.user for m in members if not m.user.is_bot]
            if not admins: return await message.answer("❌ Администраторы не найдены.")
            mentions = [f'<a href="tg://user?id={u.id}">{safe_text(u.full_name[:20])}</a>' for u in admins]
            await message.answer(f"📢 {safe_text(call_text)}\n\n" + ", ".join(mentions), parse_mode="HTML")
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
    elif clean_text.startswith("me") or clean_text.startswith("позывной"):
        user = await get_user(message.from_user.id, message.from_user.username)
        if user.get("call_sign"):
            await message.answer(
                f"📛 Твой позывной: {user['call_sign']}\n👤 Имя: {safe_text(message.from_user.full_name)}")
        else:
            await message.answer(
                f"👤 Имя: {safe_text(message.from_user.full_name)}\n💡 Установи позывной: <code>setme 🔥</code>",
                parse_mode="HTML")
    elif clean_text.startswith("setme"):
        try:
            emoji = strip_prefix(message.text).split(maxsplit=1)[1].strip()
            if len(emoji) > 8: return await message.answer("❌ Слишком длинный позывной.")
            await update_user(message.from_user.id, call_sign=emoji)
            await message.answer(f"✅ Новый позывной: {emoji}")
        except:
            await message.answer("❌ Формат: <code>setme 🔥</code>", parse_mode="HTML")
    elif clean_text.startswith("changeme"):
        emoji = random.choice(["🔥", "⚡", "🐺", "🦊", "🦁", "🐯", "🐲", "👻", "🤖", "👽", "💀", "👑", "🎭", "🦅", "🐍"])
        await update_user(message.from_user.id, call_sign=emoji)
        await message.answer(f"✅ Новый случайный позывной: {emoji}")


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


@dp.message()
async def handle_all_messages(message: Message):
    try:
        await process_random_coin_drop(message)
    except Exception as e:
        logger.error(f"Ошибка дропа: {e}")


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
    logger.info(f"🤖 Бот: @{me.username}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())