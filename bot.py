# -*- coding: utf-8 -*-
import os
import random
import time
import asyncio
import logging
import json
from types import SimpleNamespace
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, WebAppInfo
from aiogram.enums import ChatMemberStatus
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import aiohttp                     # клиент для проверки сервиса aura-api (/api/health)
from aiohttp import web            # только для start_fake_website (порт Render)
from supabase import create_client, Client
from shop_catalog import (
    ShopPurchaseError,
    available_shop_items,
    inventory_item,
    parse_inventory,
    purchase_shop_item,
    shop_stock_snapshot,
    visible_inventory_items,
    visible_shop_items,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [6338018270, 5184813347, 8890834384]

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
# Необязательный service-role ключ. С ним бот и API пишут в таблицы в обход RLS:
# именно из-за RLS создание промокода падало с «new row violates row-level
# security policy for table promocodes». Ключ живёт только в переменных
# окружения Render — в Mini App он не попадает.
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not BOT_TOKEN: raise SystemExit("❌ ОШИБКА: Не найден BOT_TOKEN!")
if not SUPABASE_URL or not SUPABASE_KEY: raise SystemExit("❌ ОШИБКА: Не найдены SUPABASE_URL или SUPABASE_KEY!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY or SUPABASE_KEY)
if SUPABASE_SERVICE_KEY:
    logger.info("Supabase: используется service-role ключ (RLS обходится)")
else:
    logger.info("Supabase: используется обычный ключ (нужны RLS-политики сервера)")

COOLDOWN_SECONDS = 5 * 60 * 60

# ================= АДРЕСА СЕРВИСОВ (Render) =================
# Проект работает строго на ДВУХ сервисах Render:
#   1) aura-api — FastAPI (api.py): игры + раздача статики Mini App (/miniapp);
#   2) dan4Bot  — этот процесс (bot.py): только aiogram, без веб-сервера кроме
#      start_fake_website (он нужен, чтобы Render видел открытый порт).
# Оба сервиса смотрят в одну базу Supabase, поэтому баланс в боте и в аппке общий.
# Адрес API берём из переменной окружения, а если она не задана — проверяем
# известные варианты имени сервиса. Render на НЕСУЩЕСТВУЮЩЕМ имени сервиса
# отвечает 404 «Not Found», поэтому адрес нельзя просто «угадать» в константе:
# при старте бот сам находит живой и пишет в лог.
API_URL_CANDIDATES = [
    "https://aura-api-mptt.onrender.com",   # рабочий сервис Render
    "https://aura-api-mppt.onrender.com",   # старая опечатка, оставлена для авто-поиска
]
API_URL = (os.getenv("AURA_API_URL") or API_URL_CANDIDATES[0]).rstrip("/")
# Именно этот адрес должен быть прописан в BotFather → Configure Mini App.
WEBAPP_URL = os.getenv("AURA_WEBAPP_URL") or f"{API_URL}/miniapp/"
# Внутри Telegram мини-аппку открывают двумя способами:
#   1) кнопкой с web_app (WebAppInfo) — работает в личке и не зависит от short_name;
#   2) прямой ссылкой t.me/<бот>/<short_name> — нужна, чтобы переслать другу.
# Внутри ссылки (2) имя бота пишется вручную и часто устаревает: бот переименован,
# short_name в BotFather другой — и команда «апка» даёт «не рабочую ссылку».
# Поэтому username берём у Telegram (get_me) при старте, а short_name — из env
# MINIAPP_SHORT_NAME (по умолчанию Auragame, как в BotFather → Configure Mini App).
MINIAPP_SHORT_NAME = (os.getenv("MINIAPP_SHORT_NAME") or "Auragame").strip().strip("/")
BOT_USERNAME = (os.getenv("BOT_USERNAME") or "").strip().lstrip("@")
# Явный override: если ссылку нужно жёстко зафиксировать.
MINIAPP_URL = os.getenv("MINIAPP_URL") or ""


def miniapp_webapp_url() -> str:
    """Адрес Mini App для кнопки WebAppInfo — всегда https и всегда …/miniapp/.

    Именно этот адрес открывает Telegram, когда игрок жмёт кнопку: приложение
    стартует внутри Telegram и получает initData. В ТЕКСТ сообщения он не
    попадает — по прямой ссылке на Render Mini App открывается без initData,
    и игрок видит «технические работы» вместо игры.
    """
    base = (os.getenv("AURA_WEBAPP_URL") or WEBAPP_URL or "").strip()
    if not base:
        base = f"{API_URL}/miniapp/"
    if "://" not in base:
        base = "https://" + base
    if not base.startswith("https://"):
        base = "https://" + base.split("://", 1)[-1]
    base = base.rstrip("/")
    if not base.endswith("/miniapp"):
        base = f"{base}/miniapp"
    return base + "/"


def miniapp_direct_url() -> str:
    """Прямая ссылка t.me/<бот>/<short_name> для пересылки друзьям.

    Строится из НАСТОЯЩЕГО @username (его отдаёт Telegram), поэтому
    переименование бота больше не ломает команду «апка».
    """
    override = (os.getenv("MINIAPP_URL") or MINIAPP_URL or "").strip()
    if override:
        return override
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}/{MINIAPP_SHORT_NAME}"
    return f"https://t.me/{MINIAPP_SHORT_NAME}"

CHALLENGE_TIMEOUT = 60
COIN_DROP_CHANCE = 0.005
COIN_DROP_MIN = 1
COIN_DROP_MAX = 5
DROP_IMAGE = "drop.jpg"
MIN_BET = 10
MAX_BET = 10000
DAILY_TRANSFER_LIMIT = 5000

def coin_amount(amount) -> str:
    """«1 коин», «2 коина», «5 коинов» — чтобы в сообщениях не было «1 коинов»."""
    try:
        number = int(amount)
    except (TypeError, ValueError):
        return f"{amount} коинов"
    tail = abs(number) % 100
    if 11 <= tail <= 19:
        word = "коинов"
    else:
        tail %= 10
        word = "коин" if tail == 1 else ("коина" if 2 <= tail <= 4 else "коинов")
    return f"{number} {word}"


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

# Отдельный роутер для пошагового создания промокода: его хендлеры должны
# получать текстовые ответы админа ДО общего хендлера, который ловит все сообщения.
promo_router = Router()


class PromoCreate(StatesGroup):
    amount = State()   # сколько коинов / капсул выдаёт промокод
    uses = State()     # сколько всего активаций


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
    caption = f"🎉 <b>Поздравляю!</b>\n\n🎁 Ты выиграл(а) <b>{coin_amount(amount)}</b>\n\n💬 Пиши сообщения в чате и получай коины!"
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
        "• <code>апгрейд</code> — ссылка на игру «Апгрейд»\n"
        "• <code>забрать всё @user</code> — полностью очистить инвентарь и баланс\n\n"
        "💰 <b>Экономика:</b>\n"
        "• <code>+пополнить карман @user 500</code> — выдать коины\n"
        "• <code>забрать @user 100</code> — списать коины\n"
        "• <code>выдать ключ @user 5</code> — выдать ключи\n\n"
        "🎟️ <b>Промокоды:</b>\n"
        "• <code>!создать промокод</code> — пошагово: тип награды, количество, активации\n"
        "• <code>+промокод ЛЕТО 100 500</code> — быстро на коины (100 активаций, 500 коинов)\n"
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
        f"🧹 <b>Полная зачистка!</b>\n\n👤 Кому: @{target.username}\n➖ Снято: <b>{old_coins}</b> коинов\n➖ Удалено ключей: <b>{old_keys}</b> 🔑\n➖ Удалены капсулы.\n📊 Новый баланс: <b>0</b> коинов",
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
    if user["coins"] < amount: return await message.answer(f"❌ У пользователя только {user['coins']} коинов")
    await update_user(target.id, coins=user["coins"] - amount)
    await message.answer(
        f"💸 <b>Коины списаны!</b>\n👤 У кого: @{target.username}\n➖ Снято: <b>{amount}</b> коинов\nОстаток: <b>{user['coins'] - amount}</b> коинов",
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
        f"💰 <b>Карман пополнен!</b>\n👤 Кому: @{target.username}\n➕ Начислено: <b>{amount}</b> коинов\n📊 Баланс: <b>{user['coins'] + amount}</b> коинов",
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
            f"❌ Максимальная сумма перевода {DAILY_TRANSFER_LIMIT} коинов")
        if daily_sent + amount > DAILY_TRANSFER_LIMIT: return await message.answer(
            f"❌ Превышен дневной лимит переводов ({DAILY_TRANSFER_LIMIT} коинов). Вы уже отправили сегодня: {daily_sent} коинов")

    if user["coins"] < amount: return await message.answer(f"❌ Недостаточно коинов. У тебя: {user['coins']} коинов")

    target_user = await get_user(target.id, target.username)
    new_daily_sent = daily_sent + amount
    await update_user(message.from_user.id, coins=user["coins"] - amount, daily_transfers=new_daily_sent,
                      last_transfer_reset=today)
    await update_user(target.id, coins=target_user["coins"] + amount)

    text = f"✅ <b>Перевод выполнен!</b>\n👤 Кому: @{target.username}\n💸 Сумма: <b>{amount}</b> коинов"
    if comment: text += f"\n💬 Комментарий к переводу: <i>{safe_text(comment)}</i>"

    if int(message.from_user.id) not in ADMIN_IDS:
        text += f"\n📊 Лимит: {new_daily_sent}/{DAILY_TRANSFER_LIMIT}"

    await message.answer(text, parse_mode="HTML")


# ================= ПРОМОКОДЫ =================
PROMO_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_promo_code() -> str:
    return "AURA-" + "".join(random.choice(PROMO_CODE_ALPHABET) for _ in range(6))


def _promo_code_from_message(message: Message):
    """!создать промокод AURA100 — код можно задать сразу, иначе сгенерируем."""
    parts = strip_prefix(message.text or "").split()
    return parts[2] if len(parts) >= 3 else None


def _promo_reward_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Коины", callback_data="promo_new|coins")],
        [InlineKeyboardButton(text="💊 Капсула", callback_data="promo_new|capsule_menu")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="promo_new|cancel")],
    ])


def _insert_promocode(payload: dict):
    """Вставка промокода с откатом на старую схему, если миграция ещё не применена.

    reward_type/item_id появились в 20261030000000_capsule_stock_promo_types.sql;
    до неё PGRST204 не должен ломать создание обычных «коиновых» промокодов.
    """
    try:
        return supabase.table("promocodes").insert(payload).execute()
    except Exception as exc:
        message = str(exc)
        schema_error = "PGRST204" in message or "reward_type" in message or "item_id" in message
        legacy_ok = payload.get("reward_type", "coins") == "coins" and not payload.get("item_id")
        if schema_error and legacy_ok:
            legacy = {"code": payload["code"], "uses": payload["uses"], "amount": payload["amount"]}
            return supabase.table("promocodes").insert(legacy).execute()
        raise


def _promo_error_hint(exc: Exception, reward_type: str = "coins") -> str:
    """Что именно чинить, если промокод не записался.

    Самая частая причина сейчас — RLS без политик:
    «new row violates row-level security policy for table "promocodes"».
    Раньше на любую ошибку показывалась миграция капсул, и админ правил не то.
    """
    message = f"{type(exc).__name__}: {exc}"
    if "row-level security" in message or "42501" in message:
        return (
            "\n\n💡 Причина: в Supabase включён RLS без политик для таблицы promocodes.\n"
            "Лечение (любое из двух):\n"
            "1) применить <code>supabase/migrations/20261101000000_server_rls_policies.sql</code>;\n"
            "2) или задать в Render переменную <code>SUPABASE_SERVICE_KEY</code> "
            "(service_role key) для aura-api и dan4Bot."
        )
    if "PGRST204" in message or "reward_type" in message or "item_id" in message:
        return ("\n\n💡 Примените миграцию "
                "<code>20261030000000_capsule_stock_promo_types.sql</code> "
                "(колонки reward_type и item_id).")
    if reward_type == "capsule":
        return ("\n\n💡 Проверьте, что применены миграции "
                "<code>20261030000000_capsule_stock_promo_types.sql</code> "
                "и <code>20261101000000_server_rls_policies.sql</code>.")
    return ""


def _promo_capsule_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{item['emoji']} {item['name']}", callback_data=f"promo_item|{item_id}")]
        for item_id, item in visible_shop_items(True).items()
    ]
    rows.append([InlineKeyboardButton(text="✖️ Отмена", callback_data="promo_new|cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@promo_router.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("создать промокод")))
async def promo_wizard_start(message: Message, state: FSMContext):
    """!создать промокод [КОД] — только админы. Дальше бот спрашивает 3 шага."""
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass

    code = _promo_code_from_message(message)
    if code:
        def _exists():
            return supabase.table("promocodes").select("code").eq("code", code).execute()

        try:
            if (await asyncio.to_thread(_exists)).data:
                return await message.answer(f"❌ Промокод <code>{code}</code> уже существует.",
                                            parse_mode="HTML")
        except Exception:
            logger.exception("Не удалось проверить промокод %s", code)

    await state.clear()
    await state.update_data(code=code or _new_promo_code())
    await message.answer(
        "🧾 <b>Создание промокода</b>\n\n"
        "Шаг 1/3 — выбери тип награды:",
        parse_mode="HTML", reply_markup=_promo_reward_keyboard(),
    )


@promo_router.callback_query(F.data == "promo_new|cancel")
async def promo_wizard_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Создание промокода отменено.")
    except Exception:
        pass
    await callback.answer()


@promo_router.callback_query(F.data.startswith("promo_new|"))
async def promo_wizard_type(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Только для админов", show_alert=True)
    action = callback.data.split("|", 1)[1]
    data = await state.get_data()
    if not data.get("code"):
        await state.clear()
        return await callback.answer("Сессия истекла, начни заново: !создать промокод", show_alert=True)

    if action == "coins":
        await state.update_data(reward_type="coins", item_id=None)
        await state.set_state(PromoCreate.amount)
        await callback.message.edit_text(
            f"🧾 Промокод <code>{data['code']}</code>\n\n"
            "Шаг 2/3 — сколько <b>коинов</b> выдавать? Отправь число.",
            parse_mode="HTML")
    elif action == "capsule_menu":
        await callback.message.edit_text(
            f"🧾 Промокод <code>{data['code']}</code>\n\n"
            "Шаг 2/3 — какая <b>капсула</b> в награде?",
            parse_mode="HTML", reply_markup=_promo_capsule_keyboard())
    return await callback.answer()


@promo_router.callback_query(F.data.startswith("promo_item|"))
async def promo_wizard_item(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Только для админов", show_alert=True)
    item_id = callback.data.split("|", 1)[1]
    item = inventory_item(item_id)
    data = await state.get_data()
    if item is None or item_id not in visible_shop_items(True):
        return await callback.answer("Неизвестная капсула", show_alert=True)
    if not data.get("code"):
        await state.clear()
        return await callback.answer("Сессия истекла, начни заново: !создать промокод", show_alert=True)

    await state.update_data(reward_type="capsule", item_id=item_id)
    await state.set_state(PromoCreate.amount)
    await callback.message.edit_text(
        f"🧾 Промокод <code>{data['code']}</code>\n\n"
        f"Шаг 2/3 — сколько капсул «{item['emoji']} {item['name']}» выдавать? Отправь число.",
        parse_mode="HTML")
    return await callback.answer()


@promo_router.message(StateFilter(PromoCreate.amount))
async def promo_wizard_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = strip_prefix(message.text or "").strip()
    try:
        amount = int(text)
    except (TypeError, ValueError):
        return await message.answer("❌ Нужно целое число. Сколько выдавать?")
    if amount <= 0:
        return await message.answer("❌ Количество должно быть больше нуля.")

    data = await state.get_data()
    is_capsule = data.get("reward_type") == "capsule"
    limit = 1000 if is_capsule else 1_000_000
    if amount > limit:
        return await message.answer(f"❌ Слишком много. Максимум — {limit}.")

    await state.update_data(amount=amount)
    await state.set_state(PromoCreate.uses)
    what = f"капсул «{inventory_item(data.get('item_id'))['name']}»" if is_capsule else "коинов"
    await message.answer(f"✅ Награда: <b>{amount}</b> {what}\n\n"
                         "Шаг 3/3 — сколько всего активаций? Отправь число.",
                         parse_mode="HTML")


@promo_router.message(StateFilter(PromoCreate.uses))
async def promo_wizard_uses(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    text = strip_prefix(message.text or "").strip()
    try:
        uses = int(text)
    except (TypeError, ValueError):
        return await message.answer("❌ Нужно целое число. Сколько активаций?")
    if uses <= 0:
        return await message.answer("❌ Активаций должно быть больше нуля.")

    data = await state.get_data()
    code = data.get("code")
    amount = int(data.get("amount") or 0)
    reward_type = data.get("reward_type") or "coins"
    item_id = data.get("item_id")
    if not code or amount <= 0:
        await state.clear()
        return await message.answer("❌ Сессия истекла, начни заново: <code>!создать промокод</code>",
                                    parse_mode="HTML")

    payload = {"code": code, "uses": uses, "amount": amount, "reward_type": reward_type,
               "item_id": item_id if reward_type == "capsule" else None}

    def _add():
        return _insert_promocode(payload)

    try:
        await asyncio.to_thread(_add)
    except Exception as exc:
        logger.exception("Не удалось создать промокод %s", code)
        await state.clear()
        return await message.answer(
            f"❌ Не удалось создать промокод: <code>{safe_text(exc)}</code>{_promo_error_hint(exc, reward_type)}",
            parse_mode="HTML")

    await state.clear()
    if reward_type == "capsule":
        item = inventory_item(item_id) or {"emoji": "💊", "name": "капсула"}
        reward_text = f"{item['emoji']} <b>{item['name']}</b> × {amount}"
    else:
        reward_text = f"<b>{coin_amount(amount)}</b>"
    await message.answer(
        "✅ <b>Промокод создан!</b>\n\n"
        f"🔑 Код: <code>{code}</code>\n"
        f"🎁 Награда: {reward_text}\n"
        f"🎟 Активаций: <b>{uses}</b>\n\n"
        f"Активация игроком: <code>промо {code}</code>",
        parse_mode="HTML")


@promo_router.message(StateFilter(PromoCreate.amount, PromoCreate.uses),
                      F.text.func(lambda t: strip_prefix(t).lower().startswith("отмена")))
async def promo_wizard_cancel_text(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Создание промокода отменено.")


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
        return _insert_promocode({"code": code, "uses": uses, "amount": amount, "reward_type": "coins"})

    try:
        await asyncio.to_thread(_add)
    except Exception as e:
        logger.exception("Не удалось создать промокод %s", code)
        # Раньше ошибка уходила только в лог Render, а админ видел «создан».
        return await message.answer(
            f"❌ Не удалось создать промокод <code>{safe_text(code)}</code>: "
            f"<code>{safe_text(e)}</code>{_promo_error_hint(e)}", parse_mode="HTML")

    await message.answer(f"✅ Промокод <code>{code}</code> создан!\nАктиваций: {uses}\nНаграда: {amount} коинов",
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
    if int(promo.get("uses") or 0) <= 0: return await message.answer("❌ Этот промокод уже закончился.")
    user_id = str(message.from_user.id)

    def _check_used():
        return supabase.table("promocode_uses").select("*").eq("code", code).eq("user_id", user_id).execute()

    used_res = await asyncio.to_thread(_check_used)
    if used_res.data: return await message.answer("❌ Вы уже активировали этот промокод.")

    reward_type = (promo.get("reward_type") or "coins").strip().lower()
    item_id = (promo.get("item_id") or "").strip()
    amount = int(promo.get("amount") or 0)
    item = inventory_item(item_id) if reward_type == "capsule" else None
    if amount <= 0 or (reward_type == "capsule" and item is None):
        return await message.answer("❌ Промокод настроен неверно, напиши администратору.")

    # Условное списание активации: два игрока не смогут активировать последнюю одновременно.
    new_uses = int(promo["uses"]) - 1
    try:
        taken = await asyncio.to_thread(lambda: supabase.table("promocodes")
                                        .update({"uses": new_uses})
                                        .eq("code", code)
                                        .eq("uses", int(promo["uses"]))
                                        .select("code")
                                        .execute())
    except Exception:
        logger.exception("Не удалось списать активацию промокода %s", code)
        return await message.answer("❌ Не удалось активировать промокод, попробуй позже.")
    if not taken.data:
        return await message.answer("❌ Этот промокод уже закончился.")

    user = await get_user(message.from_user.id, message.from_user.username)
    if reward_type == "capsule":
        inventory = parse_inventory(user.get("inventory"))
        inventory.extend([item_id] * amount)
        await update_user(message.from_user.id, inventory=json.dumps(inventory, ensure_ascii=False))
        reward_text = f"{item['emoji']} <b>{item['name']}</b> × {amount}"
        balance_text = f"\n💰 Баланс: <b>{user['coins']}</b> коинов"
    else:
        await update_user(message.from_user.id, coins=user["coins"] + amount)
        reward_text = f"<b>{coin_amount(amount)}</b>"
        balance_text = f"\n💰 Новый баланс: <b>{user['coins'] + amount}</b> коинов"

    await asyncio.to_thread(
        lambda: supabase.table("promocode_uses").insert({"code": code, "user_id": user_id}).execute())
    await message.answer(
        f"🎉 <b>Промокод активирован!</b>\n\n🎁 Вы получили: {reward_text}{balance_text}",
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
        return await message.answer(f"💰 <b>Карман @{safe_text(target.username)}</b>\n\n👛 Баланс: <b>Скрыто</b> 🔒",
                                    parse_mode="HTML")

    inv = parse_inventory(user.get("inventory"))
    name = f"@{safe_text(target.username)}" if getattr(target, 'username', None) else f"id{target.id}"
    text = f"💰 <b>Карман {name}</b>\n\n👛 Баланс: <b>{user['coins']}</b> коинов\n"
    if user.get("keys", 0) > 0: text += f"🔑 Ключи: <b>{user['keys']}</b>\n"
    inventory_lines = []
    for item_id, item in visible_inventory_items(is_admin(message.from_user.id)).items():
        count = inv.count(item_id)
        if count:
            inventory_lines.append(f"{item['emoji']} {item['short_name']}: <b>{count} шт.</b>\n")
    if inventory_lines:
        text += "\n🎒 <b>Инвентарь:</b>\n" + "".join(inventory_lines)
    else:
        text += "\n🎒 <b>Инвентарь:</b> Пусто\n"
    await message.answer(text, parse_mode="HTML")


# ================= ТОП =================
async def get_global_top(limit: int = 10):
    """Топ по балансу из базы, без админов и без привязки к текущему чату."""
    def _fetch():
        return (supabase.table("users")
                .select("user_id, username, coins")
                .order("coins", desc=True)
                .limit(limit + len(ADMIN_IDS) + 10)
                .execute().data)

    rows = await asyncio.to_thread(_fetch)
    players = [u for u in rows if int(u.get("user_id") or 0) not in ADMIN_IDS]
    return players[:limit]


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("топ")))
async def cmd_top(message: Message):
    try:
        users = await get_global_top(10)
    except Exception:
        logger.exception("Не удалось получить топ игроков")
        return await message.answer("❌ Не удалось загрузить топ, попробуй позже.")
    if not users:
        return await message.answer("💰 <b>ТОП-10 ПО КОИНАМ</b>\n\nПока пусто.", parse_mode="HTML")

    msg = "💰 <b>ТОП-10 ПО КОИНАМ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, u in enumerate(users):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        username = str(u.get("username") or "Игрок")
        display_name = username if len(username) < 20 else username[:17] + "..."
        msg += f"{prefix} {safe_text(display_name)} — <b>{coin_amount(u.get('coins') or 0)}</b>\n"
    await message.answer(msg, parse_mode="HTML")


# ================= МАГАЗИН =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("магазин")))
async def cmd_shop(message: Message):
    lines = ["🛍️ <b>Магазин AURA</b>", "", "Выбери капсулу — покупка появится в кармане:"]
    buttons = []
    stock = await asyncio.to_thread(shop_stock_snapshot, supabase)
    for item_id, item, left in available_shop_items(is_admin(message.from_user.id), stock):
        limit_text = f" · лимит {item['daily_limit']}/день" if item["daily_limit"] is not None else ""
        stock_text = f" · осталось {left} из {item['total_limit']}" if left is not None else ""
        price_text = f"{item['price']:,}".replace(",", " ")
        lines.append(f"{item['emoji']} <b>{item['name']}</b> — <b>{price_text}</b> коинов"
                     f"{limit_text}{stock_text}")
        buttons.append(InlineKeyboardButton(
            text=f"{item['emoji']} {item['short_name']} · {price_text}",
            callback_data=f"shop_buy|{item_id}",
        ))
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ================= АПГРЕЙД (АДМИНЫ) =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("апгрейд")))
async def cmd_upgrade(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Команда доступна только администраторам бота.")
    text = (
        "🛠️ <b>Апгрейд</b>\n\n"
        "Кладёшь предмет слева, выбираешь цель справа — круг показывает шанс.\n"
        "Коины: от 50 до 5 000 · кэфы x2, x4, x5, x8, x10 или готовый шанс 35/55/75%\n"
        "Капсулы: обычная 💊 (100 коинов) → уникальная ⚱️ (500 коинов)\n\n"
        "Открывай мини-аппку 👇"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛠️ Открыть Апгрейд", url=miniapp_direct_url())
    ]])
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


# ================= ДИАГНОСТИКА СЕРВИСОВ (АДМИНЫ) =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith(("сервис", "service"))))
async def cmd_services(message: Message):
    """Показывает, что проект действительно живёт на двух сервисах Render."""
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Команда доступна только администраторам бота.")

    result = await fetch_health(API_URL, timeout=12)
    status = result.get("status")
    data = result.get("data") or {}
    # Сундуки живут в таблице chest_claims. Если её нет (не применена миграция)
    # или в PostgREST устарел кэш схемы, клиент получает «Сундуки обновляются,
    # попробуйте чуть позже» и рулетка не запускается вообще. Проверяем явно.
    chest = await probe_chest_claims(API_URL)
    api_status = "не ответил"
    api_details = ""
    if status is None:
        api_status = f"❌ {result.get('error', 'нет связи')}"
        api_details = "\n• проверьте, что Web Service запущен и имя в Render верное"
    elif status == 404:
        api_status = "❌ 404 Not Found"
        api_details = (
            "\n• такого сервиса/пути нет: скорее всего имя Web Service в Render"
            " отличается от адреса в настройках"
            "\n• исправьте AURA_API_URL на реальный адрес сервиса"
        )
    elif status == 200 and isinstance(data, dict) and data.get("service") == "aura-api":
        static = data.get("static") or {}
        api_status = "✅ онлайн"
        api_details = (
            f"\n• статика miniapp: {'✅' if static.get('ready') else '❌'} ({static.get('url', '/miniapp/')})"
            f"\n• техработы: {'включены' if data.get('maintenance') else 'выключены'}"
        )
    else:
        api_status = f"⚠️ ответил кодом {status}"

    if chest.get("status") is None:
        chest_line = f"📦 Сундуки: ⚠️ не проверить ({chest.get('error', 'нет связи')})"
    elif chest.get("ok"):
        chest_line = "📦 Сундуки: ✅ таблица chest_claims на месте"
    else:
        chest_line = (
            "📦 Сундуки: ❌ таблица chest_claims недоступна\n"
            f"   • {chest.get('detail', 'ошибка базы')}\n"
            "   • примените supabase/migrations/20261030000000_capsule_stock_promo_types.sql "
            "и выполните NOTIFY pgrst, 'reload schema'"
        )

    text = (
        "🖥️ <b>СЕРВИСЫ ПРОЕКТА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        f"1️⃣ <b>aura-api</b> (FastAPI): {api_status}{api_details}\n"
        f"   • API: <code>{API_URL}</code>\n"
        f"   • Mini App: <code>{WEBAPP_URL}</code> "
        "ℹ️ (только для BotFather; игрокам эту ссылку не отправляйте — "
        "у них кнопка в <code>апка</code>)\n\n"
        "2️⃣ <b>dan4Bot</b> (aiogram): ✅ этот процесс\n"
        "   • веб-сервер только для порта Render (start_fake_website)\n"
        "   • игровые данные читает из общей базы Supabase\n\n"
        f"🔗 Ссылка для игроков: {miniapp_direct_url()}\n"
        f"{chest_line}\n"
        "ℹ️ В BotFather → Configure Mini App должен быть указан адрес aura-api."
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


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
    if amount < MIN_BET: return await message.answer(f"❌ Минимальная ставка: {MIN_BET} коинов")
    challenger = await get_user(message.from_user.id, message.from_user.username)
    if target.id == message.from_user.id: return await message.answer("❌ Нельзя играть сам с собой 😄")
    if challenger["coins"] < amount: return await message.answer(
        f"❌ У тебя недостаточно коинов.\nНужно: <b>{amount}</b> коинов", parse_mode="HTML")
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
        f"🎲 <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n👤 @{message.from_user.username} вызывает @{target.username}\n💰 Ставка: <b>{amount}</b> коинов\n🎯 Победитель забирает: <b>{amount * 2}</b> коинов\n\n⏳ Время на ответ: {CHALLENGE_TIMEOUT} секунд",
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
        result = f"🏆 Победил @{winner['username']} ({r1} : {r2})\n💰 +{amount} коинов"
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
    await callback.answer(f"✅ {item['name']} куплена за {item['price']:,} коинов", show_alert=True)


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
            f"📊 <b>СТАТИСТИКА БОТА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n👥 Всего пользователей: <b>{total_users}</b>\n🔥 Активных за 24ч: <b>{active_24h}</b>\n💰 Всего коинов в системе: <b>{total_coins}</b>\n")
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await message.answer(f"❌ Ошибка при получении статистики: {e}")


# ================= ПОМОЩЬ =================
@dp.message(
    F.text.func(lambda t: strip_prefix(t).lower().startswith("помощь") or strip_prefix(t).lower().startswith("help")))
async def cmd_help(message: Message):
    text = (
        "📖 <b>СПИСОК КОМАНД БОТА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n🎁 <b>Случайные коины:</b>\n• Просто пиши в чате — с шансом 0.5% получишь 1-5 коинов\n\n🎟️ <b>Промокоды:</b>\n• <code>промо код</code> — активировать промокод\n\n💰 <b>Экономика:</b>\n• <code>карман</code> — твой баланс\n• <code>карман @user</code> — баланс другого\n• <code>перевод @user сумма</code> — перевести\n• <code>перевод сумма</code> (ответом) — быстрый перевод\n• <code>топ</code> — топ-10 богачей\n• <code>магазин</code> — магазин\n\n🎲 <b>Игры:</b>\n• <code>кубы @user ставка</code> — дуэль в кости\n• <code>кубы ставка</code> (ответом) — быстрый вызов\n\n📱 <b>Мини-приложение:</b>\n• <code>апка</code> — ссылка на наше казино\n\n• <code>тег</code> — проверка бота\n• <code>помощь</code> — эта справка")
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("тег")))
async def cmd_tag(message: Message):
    await message.answer(f"👋 Я на месте, {safe_text(message.from_user.full_name)}!")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("апка")))
async def cmd_apka(message: Message):
    """Mini App — ТОЛЬКО через Telegram, без прямых ссылок на Render.

    Прямой адрес сервиса (…onrender.com/miniapp/) открывает приложение вне
    Telegram: initData не передаётся, игры не работают, а игрок видит пустой
    экран или «технические работы». Поэтому в тексте сообщения адреса сервиса
    больше нет — только кнопка:
      • в личке — web_app: Telegram сам открывает приложение и передаёт initData;
      • в группах web_app запрещён Telegram, поэтому там кнопка-ссылка t.me —
        она тоже открывает Mini App внутри Telegram, а не в браузере.
    """
    rows = []
    if message.chat.type == "private":
        rows.append([InlineKeyboardButton(
            text="🎮 Открыть AURA GAME",
            web_app=WebAppInfo(url=miniapp_webapp_url()),
        )])
    else:
        rows.append([InlineKeyboardButton(
            text="🎮 Открыть AURA GAME",
            url=miniapp_direct_url(),
        )])

    text = (
        "📱 <b>Наше Mini App!</b>\n\n"
        "🎮 Заходи в наше казино прямо из Telegram — жми кнопку ниже.\n\n"
        "💰 Там тебя ждут сундуки, рулетка, мины и краш с общим балансом!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


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


# Ловит всё, что не разобрали команды. Регистрируется ПОСЛЕДНИМ роутером,
# иначе он «съедал» бы ответы админа в пошаговом создании промокода.
misc_router = Router()


@misc_router.message()
async def handle_all_messages(message: Message):
    try:
        await process_random_coin_drop(message)
    except Exception as e:
        logger.error(f"Ошибка дропа: {e}")


# Порядок важен: сначала промо-мастер (ждёт текстовые ответы админа), потом всё остальное.
dp.include_router(promo_router)
dp.include_router(misc_router)


async def fetch_health(base: str, timeout: float = 8.0) -> dict:
    """Запрашивает /api/health и возвращает разбор ответа.

    {"status": 200, "data": {...}} — сервис ответил;
    {"status": 404, ...}         — такого сервиса/пути нет (частая причина
                                   «Not Found»: неверное имя Web Service в Render);
    {"status": None, "error": "…"} — сеть/DNS/таймаут.
    """
    url = f"{base.rstrip('/')}/api/health"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url) as response:
                data = None
                try:
                    data = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 — тело может быть не JSON
                    data = None
                return {"status": response.status, "data": data, "url": url}
    except Exception as exc:  # noqa: BLE001 — сетевая диагностика не должна падать
        logger.warning("aura-api недоступен по %s: %s: %s", base, type(exc).__name__, exc)
        return {"status": None, "error": f"{type(exc).__name__}", "url": url}


async def probe_api(base: str, timeout: float = 8.0) -> bool:
    """Живой ли это aura-api: /api/health отвечает service == "aura-api"."""
    result = await fetch_health(base, timeout)
    data = result.get("data") or {}
    return result.get("status") == 200 and isinstance(data, dict) and data.get("service") == "aura-api"


async def probe_chest_claims(base: str, timeout: int = 10) -> dict:
    """Проверяет, доступна ли таблица сундуков на сервере aura-api.

    Возвращает {"ok": bool, "status": int|None, "detail": str, "error": str}.
    Именно из-за недоступной chest_claims Mini App показывает «Сундуки
    обновляются, попробуйте чуть позже» и рулетка не запускается.
    """
    base = (base or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "status": None, "detail": "", "error": "адрес API не задан"}
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(f"{base}/api/chest_ready") as response:
                status = response.status
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {}
    except Exception as exc:
        return {"ok": False, "status": None, "detail": "", "error": f"{type(exc).__name__}"}
    if status == 404:
        return {"ok": False, "status": 404, "detail": "эндпоинт /api/chest_ready не найден — обновите aura-api",
                "error": ""}
    if not isinstance(data, dict):
        return {"ok": False, "status": status, "detail": "неожиданный ответ", "error": ""}
    return {"ok": bool(data.get("ok")), "status": status, "detail": str(data.get("detail") or data.get("error") or ""),
            "error": ""}


async def resolve_api_url() -> str:
    """Проверяет адрес API и при необходимости переключается на рабочий.

    Порядок: адрес из env (если задан) → первый кандидат → остальные.
    Возвращает текущий (уже выправленный) API_URL.
    """
    global API_URL, WEBAPP_URL
    override = (os.getenv("AURA_API_URL") or "").strip().rstrip("/")
    candidates = []
    for base in ([override] if override else []) + [API_URL] + API_URL_CANDIDATES:
        base = (base or "").strip().rstrip("/")
        if base and base not in candidates:
            candidates.append(base)

    for base in candidates:
        if await probe_api(base):
            if base != API_URL:
                logger.warning("aura-api найден по адресу %s (в настройках был %s) — переключаюсь", base, API_URL)
            API_URL = base
            if not os.getenv("AURA_WEBAPP_URL"):
                WEBAPP_URL = f"{API_URL}/miniapp/"
            logger.info("✅ aura-api: %s | Mini App: %s", API_URL, WEBAPP_URL)
            return API_URL

    logger.error(
        "❌ aura-api не найден ни по одному адресу (%s). "
        "Проверьте имя Web Service в Render и переменную AURA_API_URL.",
        ", ".join(candidates),
    )
    return API_URL


async def start_fake_website():
    """Минимальный HTTP-ответ для Render.

    Render убивает Web Service без открытого порта, а бот работает на long-polling.
    Этот сервер НЕ раздаёт Mini App и не обрабатывает игры — за них отвечает
    отдельный сервис aura-api (api.py). Не добавляйте сюда статику/API.
    """
    port = int(os.environ.get('PORT', 10000))
    app = web.Application()

    async def index(request):
        return web.json_response({
            "ok": True,
            "service": "dan4Bot",
            "info": "Telegram-бот на long-polling. Mini App раздаёт сервис aura-api.",
            "miniapp": WEBAPP_URL,
        })

    async def health(request):
        return web.Response(text="OK")

    async def anything_else(request):
        # Самая частая ошибка настройки: в BotFather указан адрес ЭТОГО сервиса.
        # Раньше игрок получал голый 404 «Not Found» — теперь его просто
        # перекидывает в Mini App на aura-api.
        raise web.HTTPFound(WEBAPP_URL)

    app.router.add_get('/', index)
    app.router.add_get('/health', health)
    app.router.add_get('/{tail:.*}', anything_else)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()


async def main():
    global BOT_USERNAME
    await start_fake_website()
    await resolve_api_url()          # выправляем адрес aura-api до первого запроса
    me = await bot.get_me()
    if me.username:
        # Настоящее имя бота: из него строится прямая ссылка на Mini App,
        # поэтому «апка» больше не зависит от устаревшей константы в коде.
        BOT_USERNAME = me.username
    logger.info(f"🤖 Бот: @{me.username} | Mini App: {WEBAPP_URL}")
    logger.info(f"🔗 Прямая ссылка Mini App: {miniapp_direct_url()}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())