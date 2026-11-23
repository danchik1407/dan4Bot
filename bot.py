# -*- coding: utf-8 -*-
import os
import random
import time
import asyncio
import logging
import json
import re
import hashlib
import hmac
from urllib.parse import quote, urlencode
from datetime import datetime, timezone
from types import SimpleNamespace
from dotenv import load_dotenv
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           FSInputFile, WebAppInfo, Update, MenuButtonWebApp,
                           BotCommand, BotCommandScopeDefault)
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramUnauthorizedError
import aiohttp                     # клиент для проверки сервиса aura-api (/api/health)
from aiohttp import web            # только для start_fake_website (порт Render)
from supabase import create_client, Client
from mentions import clean_name, merge_names, parse_names
from love import (
    LOVE_ACTION_BONUS,
    action_progress_text,
    LOVE_ACTION_COOLDOWN_DEFAULT,
    cooldown_from_setting,
    RP_ACTIONS,
    SOLO_CURRENCY,
    add_solo_love,
    claim_stage_rewards,
    solo_saved,
    format_love_cooldown,
    love_cooldown_seconds,
    next_level_text,
    public_stage,
    reset_solo_love,
    rp_action_for,
    rp_action_key,
    solo_state,
    stage_reached_text,
)
import player_stats
from db_schema import audit as db_schema_audit, format_report as db_schema_report
from shop_catalog import (
    DAILY_LIMITS_ENABLED,
    ShopPurchaseError,
    available_shop_items,
    inventory_item,
    parse_inventory,
    purchase_shop_item,
    shop_purchase_stats,
    shop_stats_report,
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
# Задача 43: «угаданные» имена сервиса УБРАНЫ. Раньше здесь стояли
# с двумя «примерными» именами сервиса, и бот молча отправлял игроков на домен,
# которого может не быть — Render отвечал «Not Found» прямо в Mini App.
# Теперь адрес берётся ТОЛЬКО из настроек Render (AURA_API_URL / AURA_WEBAPP_URL),
# а если их нет — из RENDER_EXTERNAL_URL текущего окружения. Никаких догадок.
API_URL_CANDIDATES = [
    (os.getenv("AURA_API_URL") or "").strip().rstrip("/"),
]
# Публичный адрес ЭТОГО сервиса — его Render выставляет сам.
RENDER_EXTERNAL_URL = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
API_URL = (os.getenv("AURA_API_URL") or os.getenv("AURA_WEBAPP_URL")
           or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
# Именно этот адрес должен быть прописан в BotFather → Configure Mini App.
def _as_miniapp_url(base: str) -> str:
    """Приводит любой адрес сервиса к виду https://host/miniapp/ (без порта)."""
    raw = (base or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    raw = raw.split("#")[0].split("?")[0].rstrip("/")
    if raw.endswith("/miniapp"):
        raw = raw[: -len("/miniapp")]
    elif "/miniapp/" in raw:
        raw = raw[: raw.index("/miniapp/")]
    if not raw:
        return ""
    # Отрезаем путь, оставляем хост без порта. Схема ВСЕГДА https: у Render
    # наружу опубликован только 443, а Telegram вообще не откроет http-web_app.
    rest = raw.split("://", 1)[-1]
    host = rest.split("/")[0].split(":")[0]
    return f"https://{host}/miniapp/"


WEBAPP_URL = _as_miniapp_url(os.getenv("AURA_WEBAPP_URL") or API_URL)
# Внутри Telegram мини-аппку открывают двумя способами:
#   1) кнопкой с web_app (WebAppInfo) — работает в личке и не зависит от short_name;
#   2) прямой ссылкой t.me/<бот>/<short_name> — нужна, чтобы переслать другу.
# Внутри ссылки (2) имя бота пишется вручную и часто устаревает: бот переименован,
# short_name в BotFather другой — и получается «не рабочая ссылка».
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
    base = (os.getenv("AURA_WEBAPP_URL") or WEBAPP_URL or RENDER_EXTERNAL_URL or "").strip()
    if not base:
        base = API_URL
    # _as_miniapp_url сам приводит адрес к https://host/miniapp/ и выбрасывает
    # порт: у Render наружу опубликован только 443, и `:PORT` в ссылке — это
    # гарантированный «Not Found» в Telegram.
    url = _as_miniapp_url(base)
    if not url:
        # Совсем ничего не настроено: честно отдаём пустую строку, чтобы
        # вызывающий код показал понятную ошибку, а не битую кнопку.
        return ""
    return url


def miniapp_direct_url() -> str:
    """Прямая ссылка t.me/<бот>/<short_name> для пересылки друзьям.

    Строится из НАСТОЯЩЕГО @username (его отдаёт Telegram), поэтому
    переименование бота больше не ломает ссылку на Mini App.
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


def ledger_note(user_id, kind: str, amount: int, title: str = "") -> None:
    """История ставок и пополнений профиля (задача 77) — не мешая команде."""
    def _write():
        try:
            player_stats.add_ledger(supabase, user_id, kind, amount, title)
        except Exception:  # noqa: BLE001
            logger.debug("история операций: запись не удалась", exc_info=True)
    try:
        asyncio.create_task(asyncio.to_thread(_write))
    except RuntimeError:
        _write()


def stats_note(user_id, **deltas) -> None:
    """Игровые моменты профиля (задача 74) — не мешая основному действию.

    Всё, что пишется здесь, видно в профиле игрока: сколько перевёл, сколько
    выиграл в кубах, сколько получил призов. Ошибка статистики не должна
    ломать команду, поэтому пишем «в фоне» и глушим исключения.
    """
    def _write():
        try:
            player_stats.add_stats(supabase, user_id, **deltas)
        except Exception:  # noqa: BLE001
            logger.debug("статистика профиля: запись не удалась", exc_info=True)
    try:
        asyncio.create_task(asyncio.to_thread(_write))
    except RuntimeError:      # нет цикла событий (скрипты, тесты)
        _write()

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


USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{3,32})")


def username_from_text(text) -> str:
    """Первый @username в тексте — без хвостовой пунктуации.

    Задача 55: раньше ник брался «сырым» (`@Contact_blocked,` с запятой или
    `@user.` с точкой), и поиск в базе не находил никого. Теперь регулярка
    берёт только допустимые Telegram-символы: латиница, цифры, подчёркивание.
    """
    match = USERNAME_RE.search(str(text or ""))
    return match.group(1) if match else ""


async def get_user_global(arg_username: str):
    clean_username = username_from_text(arg_username) or str(arg_username or "").lstrip("@").lower()
    if not clean_username:
        return None

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


def strip_prefix(text) -> str:
    """Убирает ! или / из начала строки.

    Задача 39: раньше здесь был `text.lstrip("!/")` без проверки на None, и
    любое сообщение БЕЗ текста (фото, стикер, документ, гифка, голосовое)
    роняло обработчик aiogram:

        AttributeError: 'NoneType' object has no attribute 'lstrip'
        в bot.py, строка 260 (strip_prefix) ← фильтр `F.text.func(...)`

    aiogram подставляет в такой фильтр `message.text` = None. Теперь None и
    любой не-текст превращаются в пустую строку: фильтры просто не совпадут,
    и апдейт обработается дальше, а не падает в лог стектрейсом.
    """
    if text is None:
        return ""
    return str(text).lstrip("!/").strip()


def message_text(message) -> str:
    """Текст сообщения, который никогда не бывает None.

    Задача 40: в aiogram `message.text` есть только у текстовых сообщений. У фото,
    стикера, документа, гифки, голосового, опроса, системного события (вход в
    чат, смена названия) и у постов в канале поле равно None. Любой код вида
    `message.text.lower()` или `message.text.split()` на таком апдейте падает
    с `AttributeError: 'NoneType' object has no attribute ...`.

    Возвращает текст вместе с подписью (caption): у фото с подписью команда
    `!помощь` тоже должна работать.
    """
    if message is None:
        return ""
    text = getattr(message, "text", None)
    if text is None:
        text = getattr(message, "caption", None)
    return text or ""


def has_text(message) -> bool:
    """Есть ли у сообщения текст или подпись (иначе текстовая команда не применима)."""
    return bool(message_text(message))


def message_actor(message):
    """Кто отправил сообщение — с запасным вариантом для постов в канале.

    У `channel_post` нет `from_user` (автор — сам канал), поэтому
    `message.from_user.id` там падает. Возвращаем `sender_chat`/`chat`
    как «актёра», чтобы существующие проверки прав не падали.
    """
    if message is None:
        return None
    actor = getattr(message, "from_user", None)
    if actor is not None:
        return actor
    return getattr(message, "sender_chat", None) or getattr(message, "chat", None)


def update_message(update):
    """Сообщение из апдейта (или None) — включая отредактированные."""
    if not isinstance(update, Update):
        return None
    return (update.message or update.edited_message or update.channel_post
            or update.edited_channel_post)


class AnonymousSenderMiddleware(BaseMiddleware):
    """Не пропускает к командам сообщения без отправителя (посты в канале).

    У `channel_post` нет `from_user`, и любой хендлер с `message.from_user.id`
    падал бы на `AttributeError: 'NoneType' object has no attribute 'id'`.
    Команды в канале бот всё равно не обслуживает, поэтому такие сообщения
    просто не доходят до хендлеров.

    ВАЖНО: сообщения без ТЕКСТА здесь не отбрасываются — у оплаты Stars
    (`successful_payment`) текста тоже нет, и платёж должен обрабатываться.

    Регистрируется на `dp.message` (outer), т.е. ДО магических фильтров.
    """

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and getattr(event, "from_user", None) is None:
            logger.info("Пропускаю сообщение без отправителя (чат %s)",
                        getattr(getattr(event, "chat", None), "id", "?"))
            return None
        return await handler(event, data)


# ================= ДОСТУП К БОТУ (задачи 57 и 62) =================
# Задача 57 закрывала игру «до админов» (ADMINS_ONLY_BOT=1), и это работало
# даже по умолчанию — сразу после деплоя обычный игрок получал «Технические
# работы». Задача 62 это отменила: игра ОТКРЫТА всем, флаг по умолчанию
# выключен, а команды `техработы` в боте больше нет. Если когда-нибудь
# понадобится снова закрыть чат — ADMINS_ONLY_BOT=1 в Render у сервиса dan4Bot.
def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


# Задача 67: «ОТКРОЙ АПКУ ЧТОБЫ ВСЕ МОГЛИ ВХОДИТЬ» — в чате играют все и всегда.
# Задача 71: тумблер техработ вернулся, но как ЯВНОЕ действие админа
# (`техработы вкл` / `техработы выкл` или кнопка в Mini App), а не флаг в
# окружении Render. Значение читается из game_settings (ключ maintenance_mode):
# нет строки — игра открыта всем.
ADMINS_ONLY_BOT = False
MAINTENANCE_SETTING_KEY = "maintenance_mode"
# Кэш на 15 секунд: иначе каждый апдейт в чате делал бы запрос в базу.
_MAINTENANCE_BOT_CACHE = {"value": False, "at": 0.0}
MAINTENANCE_BOT_TTL = 15.0


def maintenance_bot_enabled() -> bool:
    """Включены ли техработы (только админ может включить, по умолчанию — нет)."""
    now = time.time()
    if (now - float(_MAINTENANCE_BOT_CACHE["at"] or 0)) < MAINTENANCE_BOT_TTL:
        return bool(_MAINTENANCE_BOT_CACHE["value"])
    value = bool(_MAINTENANCE_BOT_CACHE["value"])
    raw = _setting_get_sync(MAINTENANCE_SETTING_KEY)
    if raw is not None:
        value = str(raw).strip().lower() in ("1", "true", "yes", "on", "вкл")
    _MAINTENANCE_BOT_CACHE.update({"value": value, "at": now})
    return value


def set_maintenance_bot(enabled: bool) -> bool:
    ok = _setting_set_sync(MAINTENANCE_SETTING_KEY, "1" if enabled else "0")
    _MAINTENANCE_BOT_CACHE.update({"value": bool(enabled), "at": time.time()})
    return bool(ok)

# Что остаётся доступным не-админам, пока идёт закрытие: вход в бот и
# модерация чата. Это не игра — денег эти команды не начисляют.
PUBLIC_PREFIXES = (
    "/start", "start", "апка", "аура", "помощь", "help", "меню",
    "аурапред", "аурапредлист", "аурапред лист", "аураварн", "аураварны",
    "снять игнор", "снятьигнор", "размут", "снять мут", "снять наказание",
    "разбан", "апбан", "список банов", "адм помощь", "адмпомощь",
)

ADMINS_ONLY_BOT_MESSAGE = ("🛠️ <b>Технические работы</b>\n\n"
                           "Игра временно доступна только администраторам. "
                           "Скоро откроем для всех — спасибо за терпение 💙")


class AdminOnlyMiddleware(BaseMiddleware):
    """Задача 57: гейт «только админы» для всех команд и кнопок игры.

    До этого закрыт был только Mini App (403 в API), а в чате обычный игрок
    мог написать «кубы», «Love», «обнять» или нажать inline-кнопку и получить
    настоящую награду из базы; !аурапред вообще писал в базу. Теперь любое
    игровое действие не-админа отсекается ДО фильтров (outer-слой), поэтому
    ни один хендлер не успевает ничего записать.

    Пропускаем: ADMIN_IDS, /start и подсказки (PUBLIC_PREFIXES), модерацию
    чата. Всё остальное — вежливый отказ одним сообщением.
    """

    async def __call__(self, handler, event, data):
        try:
            allowed = self._allow(event)
        except Exception:  # noqa: BLE001 — гейт не должен ронять бота
            logger.exception("Проверка доступа к командам не сработала")
            return None
        if allowed:
            return await handler(event, data)
        await _answer_admins_only(event)
        return None

    def _allow(self, event) -> bool:
        if not (ADMINS_ONLY_BOT or maintenance_bot_enabled()):
            return True
        actor = getattr(event, "from_user", None)
        if actor is None:
            # Пост в канале или служебный апдейт: команд там нет, а сообщения
            # без отправителя отбросит AnonymousSenderMiddleware.
            return True
        if is_admin(int(actor.id)):
            return True
        if isinstance(event, CallbackQuery):
            return False                      # все кнопки игры — только админам
        message = event if isinstance(event, Message) else getattr(event, "message", None)
        return _is_public_text(message)


def _is_public_text(message) -> bool:
    """Текст доступен без админских прав: /start, подсказки и модерация чата.

    Сравниваем по НАЧАЛУ текста (префиксы короче 4 букв — только целым словом):
    иначе «start» пропускал бы «start_game», а «меню» — «меню игры».
    """
    low = strip_prefix(message_text(message)).lower().replace("ё", "е").strip()
    if not low:
        return False
    for prefix in PUBLIC_PREFIXES:
        if low.startswith(prefix):
            if len(prefix) >= 4:
                return True
            if low == prefix or low[len(prefix):len(prefix) + 1] in (" ", "\n", "\t", "", "?", "!"):
                return True
    return False


def _looks_like_command(message) -> bool:
    """Явная команда (!кубы, /start) — на неё можно ответить отказом.

    В групповом чате бот видит ВСЕ сообщения. Отвечать «техработы» на каждое
    «привет» — это спам, поэтому в чатах отвечаем только на явные команды
    (начинаются с «!» или «/»), а обычную болтовню просто игнорируем: игра
    всё равно отключена, случайные дропы коинов не начисляются.
    В личке с ботом отвечаем на любое сообщение — это точно попытка играть.
    """
    chat = getattr(message, "chat", None)
    if chat is None or str(getattr(chat, "type", "")) == "private":
        return True
    raw = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    return raw.startswith(("!", "/"))


async def _answer_admins_only(event) -> None:
    """Один короткий ответ на попытку играть во время техработ."""
    try:
        if isinstance(event, CallbackQuery):
            await event.answer("🛠️ Технические работы: игра временно недоступна",
                               show_alert=True)
            return
        if not _looks_like_command(event):
            return                      # болтовня в чате — молча игнорируем
        await event.answer(ADMINS_ONLY_BOT_MESSAGE, parse_mode="HTML")
    except Exception:  # noqa: BLE001 — ответ не должен ронять апдейт
        logger.warning("Не удалось ответить отказом на команду во время техработ")


class CrashGuardMiddleware(BaseMiddleware):
    """Ловит ЛЮБОЕ исключение обработки апдейта, включая падения фильтров.

    Задача 40: `dp.errors` в aiogram 3.19 не спасает от исключения В ФИЛЬТРЕ —
    `HandlerObject.check()` вызывается вне try/except в `observer.trigger()`,
    поэтому ошибка уходит в лог как «Task exception was never retrieved», а при
    `feed_update` (webhook) ещё и в ответ. Здесь ловим всё: пишем одну понятную
    строку и, если хендлер не успел ответить, честно говорим игроку, что
    что-то пошло не так.
    """

    async def __call__(self, handler, event, data):
        try:
            return await handler(event, data)
        except Exception as exc:  # noqa: BLE001 — намеренно ловим всё
            logger.error("Ошибка обработки апдейта: %s: %s", type(exc).__name__, exc)
            logger.debug("Трассировка ошибки апдейта", exc_info=True)
            await _apologize(event)
            return None


async def on_error(event):
    """Ни одно обновление не должно ронять обработчик стектрейсом в лог.

    Задача 39: фильтры вида `F.text.func(lambda t: strip_prefix(t)...)` получали
    `message.text = None` (фото, стикер, документ, гифка) и падали с
    `AttributeError: 'NoneType' object has no attribute 'lstrip'`. Сам `strip_prefix`
    уже починен, но обработчик ошибок нужен как страховка: любая будущая
    неожиданность логируется одной строкой с типом и местом, а бот продолжает
    работать.

    Важно: `dp.errors` ловит ошибки ХЕНДЛЕРОВ, но не фильтров — от падений в
    фильтрах защищает CrashGuardMiddleware выше.
    """
    logger.error("Ошибка обработки апдейта: %s: %s", type(event.exception).__name__,
                 event.exception)


async def _apologize(event):
    """Аккуратный ответ игроку после внутренней ошибки (сам не падает)."""
    message = update_message(event) or (event if isinstance(event, Message) else None)
    callback = event.callback_query if isinstance(event, Update) else (
        event if isinstance(event, CallbackQuery) else None)
    try:
        if callback is not None:
            await callback.answer("⚠️ Что-то пошло не так, попробуй ещё раз.", show_alert=True)
        elif message is not None:
            await message.answer("⚠️ Что-то пошло не так, попробуй ещё раз.")
    except Exception:  # noqa: BLE001 — ответ не должен ронять обработчик
        pass


# Защита всего диспетчера — регистрируется сразу при импорте модуля.
# CrashGuard снаружи: он ловит и падения фильтров, которые `dp.errors`
# в aiogram 3.19 не перехватывает (HandlerObject.check вызывается вне try).
dp.update.outer_middleware(CrashGuardMiddleware())
# Задача 57: команды игры — только админам; ставится outer-слоем, то есть
# ДО магических фильтров: посторонний игрок не доходит ни до одного хендлера.
dp.message.outer_middleware(AdminOnlyMiddleware())
dp.callback_query.outer_middleware(AdminOnlyMiddleware())
dp.edited_message.outer_middleware(AdminOnlyMiddleware())
dp.message.outer_middleware(AnonymousSenderMiddleware())
# Плюс штатный обработчик ошибок aiogram — для ошибок внутри самих хендлеров.
dp.errors.register(on_error)


async def process_random_coin_drop(message: Message):
    if not message.from_user or message.from_user.is_bot: return
    if message.chat.type not in ("group", "supergroup"): return
    text = message_text(message)
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
        "• <code>!апбан @user Причина</code> — забанить (обнуляет баланс и капсулы)\n"
        "• <code>разбан @user</code> — разбанить\n"
        "• <code>список банов</code> — список всех забаненных\n"
        "• <code>забрать капсулы @user</code> — удалить все капсулы\n"
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
        "💗 <b>Отношения (кулдаун — 5 часов, общий для всех РП):</b>\n"
        "• после действия бот пишет, сколько ждать\n"
        "• КД один на игрока: использовал «обнять» — остальные команды ждут те же 5 часов\n"
        "• настройки нет: команда <code>кд</code> удалена, значения в базе не действуют\n\n"
        "📊 <b>Статистика:</b>\n"
        "• <code>стата бота</code> — общая статистика (работает только в ЛС)\n"
        "• <code>бд</code> — проверка схемы базы: каких миграций не хватает\n\n"
        "👑 <b>Панель:</b>\n"
        "• <code>/admin</code> — статистика, шансы апгрейда, подсказки по выдаче и бану\n\n"
        "💡 <i>Совет: писать команды лучше в ЛС с ботом, чтобы не спалить промокоды.</i>"
    )
    await message.answer(text, parse_mode="HTML")


# ================= ЗАДАНИЕ: ПОДПИСКА НА КАНАЛ =================
# Задача 35: задание выполняется в личке бота. Mini App только открывает канал и
# просит сервер прислать сюда сообщение с кнопкой «Проверить подписку» — бонус
# начисляется по этой кнопке, а задание после этого из аппки исчезает навсегда.
CHANNEL_USERNAME = (os.getenv("CHANNEL_USERNAME") or "Auragameplay_Channel").strip().lstrip("@")
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME}"
try:
    SUBSCRIPTION_BONUS = int(os.getenv("SUBSCRIPTION_BONUS") or 200)
except ValueError:
    SUBSCRIPTION_BONUS = 200
SUBSCRIPTIONS_TABLE = "subscriptions_history"


SUB_FALLBACK_PREFIX = "sub_bonus_"


def _db_error_text(exc) -> str:
    return f"{type(exc).__name__}: {exc}".lower()


def _db_error_column(exc):
    """Имя отсутствующей колонки из ошибки PostgREST/Postgres (или None)."""
    import re
    text = str(exc).lower()
    found = (re.search(r"'([a-z_][a-z0-9_]*)'\s+column", text)
             or re.search(r'column\s+"([a-z_][a-z0-9_]*)"', text)
             or re.search(r"column\s+'?\"?([a-z_][a-z0-9_]*)", text))
    return found.group(1) if found else None


def _db_error_kind(exc) -> str:
    """Классификация ошибки базы: no_table / rls / duplicate / column / db."""
    text = _db_error_text(exc)
    if ("does not exist" in text or "pgrst205" in text or "pgrst202" in text
            or ("schema cache" in text and "table" in text)
            or "не найдена" in text):
        return "no_table"
    if "row-level security" in text or "42501" in text or "rls" in text:
        return "rls"
    if "23505" in text or "duplicate key" in text:
        return "duplicate"
    if _db_error_column(exc):
        return "column"
    return "db"


def _insert_tolerant_sync(table, payload: dict, logger_prefix: str = ""):
    """INSERT, который не падает из-за отсутствия «украшающих» колонок.

    (ok, error). Если база ругается на конкретную колонку — выкидываем её и
    повторяем: из-за отсутствия created_at/reward бонус ломаться не должен.
    """
    data = dict(payload)
    last_error = None
    for _ in range(5):
        try:
            supabase.table(table).insert(data).execute()
            return True, None
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            column = _db_error_column(exc)
            if column and column in data and _db_error_kind(exc) == "column":
                logger.warning(f"{logger_prefix}{table}: колонки «{column}» нет — пишем без неё. {exc}")
                data.pop(column, None)
                continue
            break
    return False, last_error


def _setting_get_sync(key):
    try:
        res = (supabase.table("game_settings").select("value")
               .eq("key", key).limit(1).execute())
    except Exception:  # noqa: BLE001
        return None
    return res.data[0].get("value") if res.data else None


def _setting_set_sync(key, value) -> bool:
    try:
        supabase.table("game_settings").upsert(
            {"key": key, "value": str(value)}, on_conflict="key").execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_settings: не удалось записать {key}: {exc}")
        return False


def _sub_claimed_sync(user_id):
    """True/False — получен ли бонус. None — база не дала прочитать.

    Читаем два источника: основную таблицу subscriptions_history и запасную
    отметку в game_settings (она есть в любой схеме — по ней же живёт кулдаун
    награды за РП). Вторая нужна, если миграция со subscriptions_history ещё
    не применена: бонус всё равно выдаётся ровно один раз.
    """
    uid = int(user_id)
    saw_table = False
    try:
        res = (supabase.table(SUBSCRIPTIONS_TABLE).select("user_id")
               .eq("user_id", uid).limit(1).execute())
        saw_table = True
        if res.data:
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"subscriptions_history недоступна: {exc}")
    if _setting_get_sync(f"{SUB_FALLBACK_PREFIX}{uid}"):
        return True
    return False if saw_table else None


def _sub_new_user_sync(uid):
    """Создаём строку игрока так же, как get_user (иначе NOT NULL колонки мешают)."""
    row = {
        "user_id": uid, "coins": 0, "keys": 0, "username": f"id{uid}",
        "last_chest": 0, "last_call": 0, "last_daily_case": 0, "call_sign": None,
        "inventory": "[]", "daily_transfers": 0, "last_transfer_reset": 0,
    }
    data = dict(row)
    for _ in range(6):
        try:
            supabase.table("users").insert(data).execute()
            return True, None
        except Exception as exc:  # noqa: BLE001
            column = _db_error_column(exc)
            if column and column in data:
                data.pop(column, None)
                continue
            return False, exc
    return False, RuntimeError("users: не удалось создать строку игрока")


def _primary_user_row(rows):
    """Строка профиля, которую видит игрок: максимум коинов (задача 69).

    В users иногда остаются старые дубли с одним user_id. Профиль показывает
    строку с максимумом коинов, а начисление брало data[0] — «мёртвую» с нулём,
    и игрок получал коины не на тот баланс, который видел.
    """
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return None
    def coins_of(row):
        try:
            return int(float(row.get("coins") or 0))
        except (TypeError, ValueError):
            return 0
    return max(rows, key=coins_of)


def _award_coins_sync(uid, amount):
    """Начислить коины. (new_balance, error)."""
    try:
        res = supabase.table("users").select("coins").eq("user_id", uid).execute()
        if not res.data:
            ok, err = _sub_new_user_sync(uid)
            if not ok:
                return None, err
            coins = 0
        else:
            _row = _primary_user_row(res.data)
            coins = int(_row.get("coins") or 0) if _row else 0
        new_coins = coins + int(amount)
        supabase.table("users").update({"coins": new_coins}).eq("user_id", uid).execute()
        return new_coins, None
    except Exception as exc:  # noqa: BLE001
        return None, exc


# Последняя техническая ошибка выдачи бонуса — её видит админ в алерте,
# чтобы «Не удалось начислить бонус» больше не было загадкой.
SUB_LAST_ERROR = {"detail": ""}


def _sub_claim_and_award_sync(user_id, bonus: int):
    """Отметка «бонус получен» + начисление.

    Возвращает (new_balance, error): error пустой — всё хорошо; "claimed" —
    бонус уже был получен; "no_table"/"rls"/"db" — реальная причина сбоя,
    чтобы игрок увидел не «попробуйте позже», а что именно чинить.
    """
    uid = int(user_id)
    SUB_LAST_ERROR["detail"] = ""
    claimed = _sub_claimed_sync(uid)
    if claimed:
        return None, "claimed"

    marker_key = f"{SUB_FALLBACK_PREFIX}{uid}"
    ok, error = _insert_tolerant_sync(
        SUBSCRIPTIONS_TABLE,
        {"user_id": uid, "bonus_claimed": True, "reward": int(bonus),
         "created_at": datetime.now(timezone.utc).isoformat()},
        logger_prefix="ПОДПИСКА: ")
    used_fallback = False
    if not ok:
        kind = _db_error_kind(error)
        if kind == "duplicate":
            return None, "claimed"
        logger.error(f"Подписка: не удалось записать отметку ({kind}): {error}")
        SUB_LAST_ERROR["detail"] = f"marker/{kind}: {error}"[:300]
        # Таблицы нет / нет прав — ставим отметку в game_settings, она есть
        # в базе всегда (там же лежит кулдаун РП).
        if not _setting_set_sync(marker_key, "1"):
            return None, kind
        used_fallback = True

    new_balance, award_error = _award_coins_sync(uid, bonus)
    if award_error is not None:
        logger.error(f"Подписка: не удалось начислить коины: {award_error}")
        SUB_LAST_ERROR["detail"] = f"award: {award_error}"[:300]
        # Начисление не прошло — снимаем отметку, чтобы бонус не сгорел.
        if used_fallback:
            try:
                supabase.table("game_settings").delete().eq("key", marker_key).execute()
            except Exception:
                pass
        else:
            try:
                supabase.table(SUBSCRIPTIONS_TABLE).delete().eq("user_id", uid).execute()
            except Exception:
                pass
        return None, _db_error_kind(award_error)
    return new_balance, ""


def _sub_status_text(user_id) -> str:
    return (f"✅ <b>Бонус за подписку уже получен</b>\n\n"
            f"Твои +{SUBSCRIPTION_BONUS} коинов на месте — второй раз это "
            f"задание не выполняется.\n\n"
            f"Больше искать его в приложении не нужно: оно убрано.")


async def _chat_member_subscribed(user_id):
    """(subscribed, check_failed) — подписан ли игрок на канал."""
    try:
        member = await bot.get_chat_member(chat_id=f"@{CHANNEL_USERNAME}", user_id=int(user_id))
        status = getattr(member, "status", None)
        raw = getattr(status, "value", status)
        return (str(raw) in ("creator", "administrator", "member")), False
    except Exception as e:  # noqa: BLE001
        logger.warning(f"getChatMember не сработал: {e}")
        return False, True


@dp.callback_query(F.data.startswith("sub_check|"))
async def cb_sub_check(callback: CallbackQuery):
    """Кнопка «Проверить подписку» в личке бота."""
    target = (callback.data or "").split("|", 1)[1] if "|" in (callback.data or "") else ""
    if str(callback.from_user.id) != str(target):
        return await callback.answer("Это не твоё задание", show_alert=True)
    uid = int(callback.from_user.id)

    if await asyncio.to_thread(_sub_claimed_sync, uid):
        try:
            await callback.message.edit_text(_sub_status_text(uid), reply_markup=None,
                                             parse_mode="HTML")
        except Exception:
            pass
        return await callback.answer("Бонус уже получен — второй раз он не начисляется.", show_alert=True)

    subscribed, check_failed = await _chat_member_subscribed(uid)
    if not subscribed and not check_failed:
        return await callback.answer("Вы ещё не подписаны на канал. Подпишитесь и нажмите кнопку снова.",
                                     show_alert=True)

    new_balance, error = await asyncio.to_thread(_sub_claim_and_award_sync, uid, SUBSCRIPTION_BONUS)
    if error == "claimed":
        try:
            await callback.message.edit_text(_sub_status_text(uid), reply_markup=None,
                                             parse_mode="HTML")
        except Exception:
            pass
        return await callback.answer("Бонус уже получен.", show_alert=True)
    if error:
        reasons = {
            "no_table": ("База не нашла таблицу subscriptions_history.\n\n"
                         "Примените в Supabase → SQL Editor миграцию "
                         "supabase/migrations/20261109000000_economy_social.sql "
                         "и нажмите кнопку ещё раз."),
            "rls": ("База отклонила запись (RLS без политики).\n\n"
                    "Добавьте переменную SUPABASE_SERVICE_KEY сервису dan4Bot "
                    "(Render → Environment) или примените миграцию с политиками."),
            "column": ("В базе нет нужной колонки у subscriptions_history — "
                       "примените миграцию supabase/migrations/20261109000000_economy_social.sql."),
            "db": "База не ответила. Попробуйте ещё раз чуть позже.",
        }
        text = reasons.get(error, "Не удалось начислить бонус. Попробуйте позже.")
        if is_admin(uid) and SUB_LAST_ERROR.get("detail"):
            text += f"\n\n🔧 Технически: {SUB_LAST_ERROR['detail']}"
        return await callback.answer(text[:900], show_alert=True)

    text = _sub_status_text(uid)
    if new_balance is not None:
        text = (f"🎉 <b>Спасибо за подписку!</b>\n\n"
                f"Накинули <b>+{SUBSCRIPTION_BONUS} коинов</b>, "
                f"на балансе теперь <b>{new_balance}</b>.\n\n"
                f"Задание одноразовое — в приложении оно больше не появится.")
    try:
        await callback.message.edit_text(text, reply_markup=None, parse_mode="HTML")
    except Exception:
        try:
            await callback.message.answer(text, parse_mode="HTML")
        except Exception:
            pass
    await callback.answer(f"✅ +{SUBSCRIPTION_BONUS} коинов зачислено!")


# ================= КУЛДАУН НАГРАДЫ ЗА РП =================
# Задача 76: настройки больше нет. Заказчик: «команду кд убери полностью» —
# после ЛЮБОГО РП-действия нельзя ни одно другое в течение 5 часов. Значение
# Hardcode в love.py (LOVE_ACTION_COOLDOWN), строки game_settings от старых
# сборок (`love_cooldown_seconds`, `love_cooldown_actions`) не читаются.
_REMOVED_COOLDOWN_SETTING_KEYS = ("love_cooldown_seconds", "love_cooldown_actions")

# Задача 79: админ может выключить КД РП кнопкой в админ-панели Mini App — тогда
# ждать не нужно вообще (0 секунд). Флаг живёт в game_settings и читается с
# кэшем; по умолчанию КД ВКЛЮЧЁН. Ошибка базы не должна выключать кулдаун
# (иначе награда капала бы за каждое действие) — при сбое живём по последнему
# известному значению, а в самом начале считаем его включённым.
LOVE_COOLDOWN_ENABLED_KEY = "love_cooldown_enabled"
_LOVE_COOLDOWN_ENABLED_CACHE = {"value": True, "at": 0.0}
LOVE_COOLDOWN_ENABLED_TTL = 10.0


def love_cooldown_enabled_sync() -> bool:
    now = time.time()
    if (now - float(_LOVE_COOLDOWN_ENABLED_CACHE["at"] or 0)) < LOVE_COOLDOWN_ENABLED_TTL:
        return bool(_LOVE_COOLDOWN_ENABLED_CACHE["value"])
    value = bool(_LOVE_COOLDOWN_ENABLED_CACHE["value"])
    try:
        raw = _setting_get_sync(LOVE_COOLDOWN_ENABLED_KEY)
        if raw is not None:
            value = str(raw).strip().lower() not in ("0", "false", "no", "off", "выкл", "")
        else:
            legacy = str(_setting_get_sync("love_cooldown_seconds") or "").strip().lower()
            if legacy in ("off", "0", "выкл"):
                value = False
    except Exception:  # noqa: BLE001
        pass
    _LOVE_COOLDOWN_ENABLED_CACHE.update({"value": value, "at": now})
    return value


def active_love_cooldown_seconds() -> int:
    """Действующий КД РП: 5 часов или 0, если админ выключил его кнопкой."""
    return love_cooldown_seconds() if love_cooldown_enabled_sync() else 0


# ================= ПРЕДУПРЕЖДЕНИЯ В ЧАТЕ (задача 44) =================
# `!аурапред` — «фиолетовые» (создатели/админы) выдают предупреждение игроку.
# Счёт идёт ОТДЕЛЬНО В КАЖДОМ ЧАТЕ: у каждого чата свой лист предупреждений.
# На 3-м предупреждении — игнор на 5 часов ТОЛЬКО в этом чате, счётчик в 0.
# Пока игнор активен, бот не реагирует ни на одну команду игрока в этом чате,
# а один раз отвечает «вы наказаны в течение (время)».
CHAT_WARNS_TABLE = "chat_warns"
WARN_LIMIT = 3                 # сколько предупреждений до игнора
WARN_MUTE_SECONDS = 5 * 3600   # игнор после третьего предупреждения — 5 часов
# Память процесса: если миграции chat_warns ещё нет, предупреждения всё равно
# работают (и сбрасываются только рестартом бота). Ключ — (chat_id, user_id).
_CHAT_WARN_MEMORY = {}
_WARN_MUTE_NOTICE_AT = {}


def _warn_now() -> float:
    """Единый источник времени для варнов: тесты подменяют только его."""
    return time.time()


def _warn_row_key(chat_id, user_id):
    return (int(chat_id), int(user_id))


def _chat_warn_read(chat_id, user_id) -> dict:
    """(warns, muted_until) из базы; при любой ошибке — из памяти процесса."""
    key = _warn_row_key(chat_id, user_id)
    try:
        res = (supabase.table(CHAT_WARNS_TABLE).select("warns, muted_until")
               .eq("chat_id", int(chat_id)).eq("user_id", int(user_id)).limit(1).execute())
        row = res.data[0] if res.data else {}
        record = {"warns": int(row.get("warns") or 0),
                  "muted_until": _parse_iso_stamp(row.get("muted_until"))}
        _CHAT_WARN_MEMORY[key] = record
        return record
    except Exception as exc:  # noqa: BLE001
        logger.debug("chat_warns недоступна (работаю на памяти): %s", exc)
        return dict(_CHAT_WARN_MEMORY.get(key) or {"warns": 0, "muted_until": 0.0})


def _parse_iso_stamp(raw) -> float:
    """ISO-строка из базы → timestamp. Пустое/битое значение → 0."""
    if not raw:
        return 0.0
    try:
        from datetime import datetime, timezone
        if isinstance(raw, (int, float)):
            stamp = datetime.fromtimestamp(float(raw), tz=timezone.utc)
        else:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


# Задача 55: почему «в базу не сохранилось» и что делать. Раньше бот писал
# только «после рестарта счёт сбросится», и админ не понимал причину.
_WARN_WRITE_ERRORS = {}


def _warn_error_hint(exc) -> str:
    """Человеческая причина, почему запись в chat_warns не прошла."""
    text = str(exc or "")
    low = text.lower()
    if "does not exist" in low or "42p01" in low or "could not find the table" in low:
        return ("таблицы <code>chat_warns</code> нет — примените миграцию "
                "<code>20261129000000_full_schema_all_points.sql</code> и сделайте "
                "<code>NOTIFY pgrst, 'reload schema'</code>")
    if "on_conflict" in low or "no unique or exclusion constraint" in low:
        return ("у таблицы <code>chat_warns</code> нет составного ключа "
                "(chat_id, user_id) — пересоздайте её скриптом из миграции")
    if "row-level security" in low or "42501" in low or "permission denied" in low:
        return ("база закрыта RLS — выдайте политике <code>chat_warns_all</code> "
                "права из миграции (или используйте service-role ключ)")
    if "invalid input syntax" in low or "22p02" in low:
        return ("не совпал тип chat_id/user_id — в таблице должны быть BIGINT")
    return f"ошибка базы: <code>{safe_text(text[:120])}</code>"


def _chat_warn_write(chat_id, user_id, warns: int, muted_until: float) -> bool:
    """Сохраняем предупреждение. Возвращает True, только если запись реально в базе.

    Задача 55: раньше при сбое мы всё равно писали в память и говорили
    «сохранилось», а при рестарте счёт обнулялся. Теперь:
      • пишем в базу и читаем строку обратно (upsert без select может «пройти»,
        даже когда политика RLS молча отбросила запись);
      • пишем без «украшающих» колонок, если их нет (updated_at);
      • причину сбоя сохраняем и показываем админу в ответе на команду.
    """
    key = _warn_row_key(chat_id, user_id)
    _CHAT_WARN_MEMORY[key] = {"warns": int(warns), "muted_until": float(muted_until or 0.0)}
    payload = {"chat_id": int(chat_id), "user_id": int(user_id),
               "warns": int(warns),
               "muted_until": (_iso_from_stamp(muted_until) if muted_until else None),
               "updated_at": datetime_now_iso()}
    attempts = [payload,
                {k: v for k, v in payload.items() if k != "updated_at"},
                {"chat_id": payload["chat_id"], "user_id": payload["user_id"],
                 "warns": payload["warns"]}]
    last_error = None
    for index, body in enumerate(attempts):
        try:
            supabase.table(CHAT_WARNS_TABLE).upsert(
                body, on_conflict="chat_id,user_id").execute()
            # Проверка, что строка действительно есть: upsert может «пройти»
            # и ничего не записать, если сработала RLS-политика только на чтение.
            res = (supabase.table(CHAT_WARNS_TABLE).select("warns, muted_until")
                   .eq("chat_id", int(chat_id)).eq("user_id", int(user_id))
                   .limit(1).execute())
            if res.data:
                _WARN_WRITE_ERRORS.pop(key, None)
                return True
            last_error = RuntimeError("строка не появилась в chat_warns (RLS?)")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            # Отсутствие updated_at — не повод сдаваться, пробуем без него.
            if index == 0 and "updated_at" not in str(exc):
                continue
    _WARN_WRITE_ERRORS[key] = last_error
    logger.warning("chat_warns: не удалось сохранить (%s): %s", key, last_error)
    return False


def _iso_from_stamp(stamp: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(float(stamp), tz=timezone.utc).isoformat()


def _mute_left(record: dict) -> int:
    """Сколько секунд игнора осталось (0 — не наказан)."""
    left = float(record.get("muted_until") or 0.0) - _warn_now()
    return max(0, int(round(left)))


async def chat_mute_left(chat_id, user_id) -> int:
    """Игнор игрока в конкретном чате (секунды). 0 — можно писать."""
    record = await asyncio.to_thread(_chat_warn_read, chat_id, user_id)
    return _mute_left(record)


async def _warn_is_allowed(message) -> bool:
    """Кто может выдавать предупреждения: админы бота и админы/создатель чата."""
    actor = message_actor(message)
    user_id = getattr(actor, "id", None)
    if not user_id:
        return False
    if is_admin(int(user_id)):
        return True
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None)
    if not chat_id or int(chat_id) == int(user_id):
        return False            # личка — только админы бота
    return await is_chat_admin(int(user_id), int(chat_id))


async def _warn_target_from_parts(message, allow_id: bool = False):
    """Ответ на сообщение, @ник или (для админ-команд) числовой id → (id, имя).

    Задача 55: у игрока может не быть username (в базе лежит «Contact_blocked»
    или пусто) — тогда снять игнор можно только ответом на его сообщение или
    по числовому id, поэтому `allow_id=True` для `!снять игнор`.
    """
    reply = getattr(message, "reply_to_message", None)
    if reply is not None and getattr(reply, "from_user", None):
        who = reply.from_user
        return int(who.id), (getattr(who, "username", None) or getattr(who, "first_name", None))
    parts = strip_prefix(message_text(message)).split()
    for part in parts[1:]:
        username = username_from_text(part)
        if username:
            found = await get_user_global(username)
            if found:
                return int(found.id), found.username
            return None, username
        if allow_id and part.isdigit() and int(part) > 0:
            return int(part), None
    return None, None


def _warn_mention(user_id, name: str) -> str:
    """Ссылка на профиль вместо @username (задача 44)."""
    import html as html_module
    label = html_module.escape(str(name or f"id{user_id}").lstrip("@"))
    return f'<a href="tg://user?id={int(user_id)}">{label}</a>'


def _mute_until_text(seconds: int) -> str:
    """«5 ч 0 мин» — сколько ещё длится игнор (для ответа наказанному)."""
    seconds = max(0, int(seconds))
    hours, minutes = divmod(seconds // 60, 60)
    if hours and minutes:
        return f"{hours} ч {minutes} мин"
    if hours:
        return f"{hours} ч"
    return f"{max(1, minutes)} мин"


async def _warn_reply_muted(message, left: int) -> None:
    """Один ответ на игнор в чате (не спамим на каждое сообщение)."""
    key = _warn_row_key(message.chat.id, message.from_user.id)
    now = _warn_now()
    if now - float(_WARN_MUTE_NOTICE_AT.get(key) or 0.0) < 300:
        return              # чаще раза в 5 минут не повторяем
    _WARN_MUTE_NOTICE_AT[key] = now
    await message.answer(
        f"🚫 <b>Вы наказаны в течение {_mute_until_text(left)}</b>\n\n"
        "Пока игнор не снят, бот не отвечает на команды в этом чате. "
        "В других чатах ограничения нет.",
        parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    ("аурапредлист", "аурапред лист", "аураварны"))))
async def cmd_warn_list(message: Message):
    """`!аурапредлист` — кто и сколько предупреждений набрал В ЭТОМ ЧАТЕ."""
    if not await _warn_is_allowed(message):
        return
    chat_id = message.chat.id
    try:
        res = (supabase.table(CHAT_WARNS_TABLE).select("user_id, warns, muted_until")
               .eq("chat_id", int(chat_id)).execute())
        rows = res.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("аурапредлист: база недоступна (%s): %s", chat_id, exc)
        rows = [{"user_id": uid, "warns": rec.get("warns"),
                 "muted_until": (rec.get("muted_until") or None)}
                for (cid, uid), rec in _CHAT_WARN_MEMORY.items() if cid == int(chat_id)]
    rows = [row for row in rows
            if int(row.get("warns") or 0) > 0 or _mute_left(
                {"muted_until": _parse_iso_stamp(row.get("muted_until"))}) > 0]
    if not rows:
        return await message.answer(
            "📋 <b>Предупреждения в этом чате</b>\n\nПока ни у кого нет "
            "предупреждений. Так и держать 🙂", parse_mode="HTML")
    rows.sort(key=lambda row: (-int(row.get("warns") or 0), int(row.get("user_id") or 0)))
    lines = ["📋 <b>ПРЕДУПРЕЖДЕНИЯ В ЭТОМ ЧАТЕ</b>",
             "━━━━━━━━━━━━━━━━━━━━",
             f"👥 Всего на листе: <b>{len(rows)}</b> · лимит {WARN_LIMIT} → "
             f"игнор {WARN_MUTE_SECONDS // 3600} ч", ""]
    for row in rows[:50]:
        user_id = int(row.get("user_id") or 0)
        name = await _player_name(user_id)
        warns = int(row.get("warns") or 0)
        mute_until = _parse_iso_stamp(row.get("muted_until"))
        if mute_until and mute_until > _warn_now():
            # После 3-го варна счётчик обнуляется, остаётся только игнор.
            lines.append(f"• {_warn_mention(user_id, name)} — 🚫 игнор ещё "
                         f"{_mute_until_text(int(mute_until - _warn_now()))} "
                         f"(варны сброшены)")
        else:
            lines.append(f"• {_warn_mention(user_id, name)} — <b>{warns}</b>/{WARN_LIMIT}")
    lines.append("\n<i>У каждого чата свой лист. Счёт сбрасывается после игнора.</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    ("аурапред", "аура пред", "аураварн"))))
async def cmd_warn(message: Message):
    """`!аурапред` — предупреждение игроку в этом чате (только «фиолетовые»)."""
    if not await _warn_is_allowed(message):
        return await message.answer(
            "🔒 Команда только для создателей и админов чата.", parse_mode="HTML")
    chat = getattr(message, "chat", None)
    chat_type = str(getattr(chat, "type", ""))
    if chat_type in ("private", "None", ""):
        return await message.answer(
            "📋 Предупреждения работают только в чатах: в личке наказывать некого.",
            parse_mode="HTML")

    actor = message_actor(message)
    target_id, target_name = await _warn_target_from_parts(message)
    if target_id is None:
        return await message.answer(
            "📋 Формат: <code>!аурапред @ник причина</code> "
            "(или ответом на сообщение нарушителя).\n"
            "Список: <code>!аурапредлист</code>", parse_mode="HTML")
    if int(target_id) == int(getattr(actor, "id", 0)):
        return await message.answer("🙃 Себя предупреждать не нужно.")
    if is_admin(int(target_id)):
        return await message.answer("👑 Админа бота предупредить нельзя.")
    # Бот сам не должен оказаться «наказанным»: иначе модерация в этом чате
    # перестанет работать до конца игнора.
    if getattr(bot, "id", None) and int(target_id) == int(bot.id):
        return await message.answer("🤖 Бота предупреждать не нужно.")

    chat_id = int(chat.id)
    record = await asyncio.to_thread(_chat_warn_read, chat_id, target_id)
    muted_left = _mute_left(record)
    if muted_left > 0:
        return await message.answer(
            f"🚫 {_warn_mention(target_id, target_name)} уже наказан(а) в этом чате "
            f"ещё на {_mute_until_text(muted_left)}.", parse_mode="HTML")

    warns = int(record.get("warns") or 0) + 1
    muted_until = 0.0
    if warns >= WARN_LIMIT:
        muted_until = _warn_now() + WARN_MUTE_SECONDS
        warns = 0            # счётчик сбрасывается: наказание выдано
    saved = await asyncio.to_thread(_chat_warn_write, chat_id, target_id, warns, muted_until)

    if muted_until:
        _WARN_MUTE_NOTICE_AT.pop(_warn_row_key(chat_id, target_id), None)
        text = (f"🚫 {_warn_mention(target_id, target_name)} получает "
                f"<b>{WARN_LIMIT}-е предупреждение</b>.\n\n"
                f"⏱ Игнор <b>{_mute_until_text(WARN_MUTE_SECONDS)}</b> только в этом "
                f"чате: бот не будет отвечать на команды.\n"
                f"Счётчик предупреждений сброшен в 0.")
    else:
        text = (f"📝 {_warn_mention(target_id, target_name)} — предупреждение "
                f"<b>{warns} из {WARN_LIMIT}</b>.\n\n"
                f"На {WARN_LIMIT}-м — игнор {_mute_until_text(WARN_MUTE_SECONDS)} "
                f"в этом чате.")
    if not saved:
        # Задача 55: показываем ПРИЧИНУ, а не только «не сохранилось».
        hint = _warn_error_hint(_WARN_WRITE_ERRORS.get(_warn_row_key(chat_id, target_id)))
        text += f"\n<i>⚠️ В базу не сохранилось: {hint}.\n" \
                f"Пока причина не устранена, счёт после рестарта бота сбросится.</i>"
    text += "\nСписок: <code>!аурапредлист</code>"
    await message.answer(text, parse_mode="HTML")


# ================= СНЯТИЕ ИГНОРА (задача 55) =================
# `!снять игнор @ник` — админ в чате снимает наказание досрочно. Это
# единственный способ вернуть игрока в чат, не дожидаясь 5 часов: раньше
# команды не было вообще, и «случайный» игнор приходилось терпеть.
_UNMUTE_PREFIXES = ("снять игнор", "снятьигнор", "размут", "снять мут",
                    "снять наказание", "снятьигнор чат")


def _unmute_usage() -> str:
    return ("📋 Формат: <code>!снять игнор @ник</code> (или ответом на сообщение "
            "игрока, или <code>!снять игнор 123456789</code>).\n"
            "Список наказанных: <code>!аурапредлист</code>")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    _UNMUTE_PREFIXES)))
async def cmd_unmute(message: Message):
    """`!снять игнор` — досрочно снять наказание в этом чате (админ/создатель)."""
    if not await _warn_is_allowed(message):
        return await message.answer(
            "🔒 Команда только для создателей и админов чата.", parse_mode="HTML")
    chat = getattr(message, "chat", None)
    chat_type = str(getattr(chat, "type", ""))
    if chat_type in ("private", "None", ""):
        return await message.answer(
            "📋 Игнор снимается только в чатах — в личке наказаний нет.",
            parse_mode="HTML")
    target_id, target_name = await _warn_target_from_parts(message, allow_id=True)
    if target_id is None:
        return await message.answer(_unmute_usage(), parse_mode="HTML")
    chat_id = int(chat.id)
    actor = message_actor(message)
    if int(target_id) == int(getattr(actor, "id", 0)):
        return await message.answer("🙃 С себя игнор снимать не нужно.")

    record = await asyncio.to_thread(_chat_warn_read, chat_id, target_id)
    left = _mute_left(record)
    warns = int(record.get("warns") or 0)
    if left <= 0 and warns <= 0:
        name = await _display_name(message, target_id, target_name)
        return await message.answer(
            f"✅ {_warn_mention(target_id, name)} и так не наказан(а) в этом чате.",
            parse_mode="HTML")

    # Снимаем и игнор, и счётчик: игрок начинает с чистого листа.
    saved = await asyncio.to_thread(_chat_warn_write, chat_id, target_id, 0, 0.0)
    _WARN_MUTE_NOTICE_AT.pop(_warn_row_key(chat_id, target_id), None)
    name = await _display_name(message, target_id, target_name)
    lines = [f"✅ <b>Игнор снят:</b> {_warn_mention(target_id, name)}"]
    if left > 0:
        lines.append(f"⏱ Оставалось: {_mute_until_text(left)} — теперь можно писать.")
    if warns:
        lines.append(f"📝 Предупреждения обнулены (было {warns}/{WARN_LIMIT}).")
    else:
        lines.append("📝 Предупреждения обнулены.")
    if not saved:
        hint = _warn_error_hint(_WARN_WRITE_ERRORS.get(_warn_row_key(chat_id, target_id)))
        lines.append(f"<i>⚠️ В базу не сохранилось: {hint}. После рестарта бота "
                     f"игнор может вернуться.</i>")
    lines.append("Список: <code>!аурапредлист</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


class ChatWarnMiddleware(BaseMiddleware):
    """Игнор наказанных в конкретном чате (задача 44).

    Стоит ПЕРЕД всеми хендлерами сообщений: пока игрок наказан, бот не
    реагирует ни на одну его команду в этом чате. Ответ «вы наказаны в
    течение (время)» даётся не на каждое сообщение, а раз в 5 минут, чтобы
    бот не превращался в спамера. В других чатах ограничения нет — игнор
    привязан к паре (чат, игрок).
    """

    async def __call__(self, handler, event, data):
        try:
            message = event
            actor = getattr(message, "from_user", None)
            chat = getattr(message, "chat", None)
            chat_id = getattr(chat, "id", None)
            chat_type = str(getattr(chat, "type", ""))
            if (actor is None or chat_id is None
                    or getattr(actor, "is_bot", False)
                    or chat_type in ("private", "")):
                return await handler(event, data)
            if is_admin(int(actor.id)):
                return await handler(event, data)     # админа бота не блокируем
            # Служебные команды проходят всегда: иначе в чате некому было бы
            # выдать предупреждение наказанному и не открыть приложение.
            low = strip_prefix(message_text(message)).lower().replace("ё", "е")
            if low.startswith(("апка", "аурапред", "аурапредлист", "аурапред лист",
                               "аураварн", "аураварны", "разбан", "снять игнор",
                               "снятьигнор", "размут", "снять мут", "снять наказание")):
                return await handler(event, data)
            left = await chat_mute_left(chat_id, actor.id)
            if left <= 0:
                return await handler(event, data)
            if message_text(message):
                await _warn_reply_muted(message, left)
            return None                                # команду не обрабатываем
        except Exception:  # noqa: BLE001 — модерация не должна ломать бота
            logger.exception("Проверка игнора в чате не сработала")
            return await handler(event, data)


# ================= БАНЫ =================
@dp.message(
    F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
        ("апбан", "разбан"))))
async def cmd_admin_ban_unban(message: Message):
    """`!апбан` (задача 44: старое `бан` переименовано) и `разбан`."""
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")

    clean_text = strip_prefix(message_text(message)).lower().replace("ё", "е")
    parts = strip_prefix(message_text(message)).split(maxsplit=2)
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
            return await message.answer("❌ Формат: <code>!апбан @user Причина</code>", parse_mode="HTML")

    try:
        if clean_text.startswith("апбан"):
            await asyncio.to_thread(
                lambda: supabase.table("bans").upsert({"user_id": target.id, "reason": reason}).execute())
            await update_user(target.id, coins=0, keys=0, inventory="[]")

            try:
                await bot.send_message(
                    target.id,
                    f"🚫 <b>Вы были забанены в боте.</b>\n\nПричина: <i>{reason}</i>\n\n"
                    "Вы не можете пользоваться ботом и заходить в мини-приложение, пока вас не разбанят.",
                    parse_mode="HTML")
            except:
                pass

            await message.answer(
                f"✅ Пользователь <b>{_player_mention(target.id, target.username)}</b> "
                f"забанен.\nПричина: <i>{reason}</i>\nБаланс обнулен, капсулы удалены.",
                parse_mode="HTML")
        elif clean_text.startswith("разбан"):
            await asyncio.to_thread(lambda: supabase.table("bans").delete().eq("user_id", target.id).execute())
            await message.answer(
                f"✅ Пользователь <b>{_player_mention(target.id, target.username)}</b> разбанен.",
                parse_mode="HTML")
            try:
                await bot.send_message(
                    target.id,
                    "✅ <b>Вы были разбанены!</b>\n\nТеперь вы снова можете пользоваться ботом и мини-приложением.",
                    parse_mode="HTML")
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
    parts = strip_prefix(message_text(message)).split()
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
    await message.answer(f"✅ У пользователя <b>@{safe_text(target.username)}</b> забраны все капсулы.",
                         parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("забрать всё")))
async def cmd_admin_wipe_all(message: Message):
    try:
        await message.delete()
    except:
        pass
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    parts = strip_prefix(message_text(message)).split()
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
    clean_text = strip_prefix(message_text(message)).lower()
    if clean_text.startswith("забрать всё") or clean_text.startswith("забрать капсулы"): return
    try:
        await message.delete()
    except:
        pass
    if not is_admin(message.from_user.id): return await message.answer("❌ Только для админов бота.")
    parts = strip_prefix(message_text(message)).split()
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
    parts = strip_prefix(message_text(message)).split()
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
    parts = strip_prefix(message_text(message)).split(maxsplit=2)
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

    # Задача 74: «сколько коинов передал» и «сколько получил» — в профиле обоих.
    stats_note(message.from_user.id, sent_transfer=amount, sent_transfer_cnt=1,
               best_transfer=amount, coins_spent=amount)
    stats_note(target.id, got_transfer=amount, coins_earned=amount)
    ledger_note(message.from_user.id, "transfer_out", amount,
                f"📤 {('@' + str(target.username)) if target.username else 'другу'}")
    ledger_note(target.id, "transfer_in", amount, "📥 Перевод от друга")

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
    parts = strip_prefix(message_text(message)).split()
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
            "1) применить <code>supabase/migrations/20261107000000_open_launch_promo_rls_and_stock_reset.sql</code>\n"
            "   (он же обновляет тиражи капсул к открытию игры);\n"
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
    text = strip_prefix(message_text(message)).strip()
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
    text = strip_prefix(message_text(message)).strip()
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
    parts = strip_prefix(message_text(message)).split()
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
    parts = strip_prefix(message_text(message)).split()
    if len(parts) < 3: return await message.answer("❌ Формат: <code>-удалить промокод НАЗВАНИЕ</code>",
                                                   parse_mode="HTML")
    code = parts[2]

    def _del():
        supabase.table("promocodes").delete().eq("code", code).execute()

    await asyncio.to_thread(_del)
    await message.answer(f"✅ Промокод <code>{code}</code> удалён.", parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("промо")))
async def cmd_promo(message: Message):
    parts = strip_prefix(message_text(message)).split()
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

    # Отметку «этот игрок уже активировал» пишем отдельно от выдачи награды:
    # если база её не примет (RLS), награда всё равно должна остаться у игрока,
    # а админ увидит причину в логах Render.
    try:
        await asyncio.to_thread(
            lambda: supabase.table("promocode_uses").insert({"code": code, "user_id": user_id}).execute())
    except Exception:
        logger.exception("Промокод %s активирован, но promocode_uses не записался "
                         "(нужна миграция 20261107000000_open_launch_promo_rls_and_stock_reset.sql)", code)
    await message.answer(
        f"🎉 <b>Промокод активирован!</b>\n\n🎁 Вы получили: {reward_text}{balance_text}",
        parse_mode="HTML")


# ================= КАРМАН (обновлено: поддержка @юзера) =================
@dp.message(
    F.text.func(lambda t: strip_prefix(t).lower().startswith("карман") or strip_prefix(t).lower().startswith("баланс")))
async def cmd_pocket(message: Message):
    parts = strip_prefix(message_text(message)).split()

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
        # Задача 44: вместо @username — ник со ссылкой на профиль.
        return await message.answer(
            f"💰 <b>Карман {_player_mention(target.id, getattr(target, 'username', None))}</b>"
            f"\n\n👛 Баланс: <b>Скрыто</b> 🔒", parse_mode="HTML")

    inv = parse_inventory(user.get("inventory"))
    name = _player_mention(target.id, getattr(target, "username", None))
    text = f"💰 <b>Карман {name}</b>\n\n👛 Баланс: <b>{user['coins']}</b> коинов\n"
    # Задача 72: «в кармане» теперь видно и любовь — ❤️ Love у пары или
    # 💔 Unlove у одиночки. Раньше эти числа жили только в «любовь инфа».
    love_line = await _pocket_love_line(target.id)
    if user.get("keys", 0) > 0: text += f"🔑 Ключи: <b>{user['keys']}</b>\n"
    if love_line: text += love_line
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


async def _pocket_love_line(user_id) -> str:
    """Строка «❤️ Love: N» или «💔 Unlove: N» для кармана (задача 72)."""
    try:
        row = await get_relationship(user_id)
    except Exception:
        row = None
    if row:
        score = int(row.get("love") or 0)
        return f"❤️ Love: <b>{coin_amount(score)}</b>\n"
    score = await get_solo_love(user_id)
    if score > 0:
        return f"💔 {SOLO_CURRENCY}: <b>{coin_amount(score)}</b>\n"
    return ""


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
    # `топ отношений` / `топ любви` — отдельная статистика пар. Проверяем здесь,
    # потому что этот хендлер зарегистрирован раньше любовного блока и иначе
    # съел бы команду.
    clean = strip_prefix(message_text(message)).lower()
    # «топ отношений», «топ любви», «топ любовь» — всё это статистика пар.
    # Проверяем по началу фразы: обычный `топ` по коинам не должен попасть сюда.
    if "отношен" in clean or clean.startswith(("топ люб", "top love", "top lov")):
        return await cmd_top_love(message)
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
        limit_text = (f" · лимит {item['daily_limit']}/день"
                      if DAILY_LIMITS_ENABLED and item["daily_limit"] is not None else "")
        stock_text = f" · осталось {left} из {item['total_limit']}" if left is not None else ""
        price_text = f"{item['price']:,}".replace(",", " ")
        lines.append(f"{item['emoji']} <b>{item['name']}</b> — <b>{price_text}</b> коинов"
                     f"{limit_text}{stock_text}")
        buttons.append(InlineKeyboardButton(
            text=f"{item['emoji']} {item['short_name']} · {price_text}",
            callback_data=f"shop_buy|{item_id}",
        ))
    if not buttons:
        # Весь тираж распродан: пустой список выглядел бы как поломка.
        lines.append("")
        lines.append("🔒 <b>Всё распродано.</b> Тираж закончился — следи за новостями.")
    rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )



# ================= ДИАГНОСТИКА СЕРВИСОВ (АДМИНЫ) =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith(("сервис", "service"))))
def miniapp_link_line(check: dict) -> str:
    """Строка «!сервисы» про адрес Mini App (задача 48)."""
    if not isinstance(check, dict):
        return ""
    url = check.get("url") or "—"
    direct = check.get("direct") or ""
    if check.get("ok"):
        head = f"🎮 Mini App: ✅ {url}"
    elif check.get("status") is None:
        head = f"🎮 Mini App: ⚠️ не проверить ({check.get('error') or 'нет связи'})\n   • {url}"
    elif check.get("status") == 404:
        head = (f"🎮 Mini App: ❌ {url} → 404 Not Found\n"
                "   • этот адрес открывает Telegram по кнопке, значит в BotFather "
                "записан старый/чужой URL\n"
                "   • BotFather → /mybots → бот → Bot Settings → Configure Mini App "
                "→ Edit Mini App URL → вставь адрес выше")
    else:
        head = (f"🎮 Mini App: ⚠️ {url} → {check.get('error') or 'не наша страница'}\n"
                "   • проверь, что в BotFather → Configure Mini App указан именно этот адрес")
    lines = [head]
    if direct:
        lines.append(f"   • кнопка в группе: {direct}")
    short = check.get("short_name")
    if short:
        lines.append(f"   • short name в BotFather должен быть: {short}")
    return "\n".join(lines)


def arena_payout_line(data: dict) -> str:
    """Строка про выплаты арены для «!сервисы» (задача 47).

    Показывает, применена ли миграция: без журнала выплат выигрыш не переживёт
    перезапуск сервиса — это и была жалоба «арена сбрасывается, баланс не отдаёт».
    """
    if not isinstance(data, dict):
        return ""
    parts = []
    if "arena_payouts" in data:
        parts.append(f"журнал выплат: {'✅' if data.get('arena_payouts') else '❌ нет таблицы arena_payouts'}")
        if data.get("arena_payouts") and not data.get("arena_payout_rpc"):
            parts.append("❌ нет функции arena_payout_apply")
    if "arena_rounds" in data:
        parts.append(f"сохранение раундов: {'✅' if data.get('arena_rounds') else '❌ нет таблицы arena_rounds'}")
    pending = data.get("arena_pending_payouts")
    if isinstance(pending, int) and pending > 0:
        parts.append(f"⏳ невыплаченных: {pending}")
    if not parts:
        return ""
    line = "\n• арена: " + " · ".join(parts)
    if not data.get("arena_payouts") or not data.get("arena_rounds"):
        line += ("\n   • примените supabase/migrations/20261127000000_arena_rounds.sql"
                 " и выполните NOTIFY pgrst, 'reload schema'")
    return line


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
    # Диагностика /api/chest_ready с задачи 57 закрыта админами: команду
    # «сервис» вызывает админ, поэтому передаём его initData (Telegram-подпись
    # строится на BOT_TOKEN, который есть только у бота).
    chest = await probe_chest_claims(API_URL, init_data=admin_probe_init_data(message.from_user.id))
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
        awake = data.get("awake") or {}
        uptime = awake.get("uptime_seconds")
        woke = " — только что проснулся после сна" if awake.get("just_woke") else ""
        uptime_line = (f"\n• uptime aura-api: {uptime} с{woke}" if isinstance(uptime, int) else "")
        api_details = (
            f"\n• статика miniapp: {'✅' if static.get('ready') else '❌'} ({static.get('url', '/miniapp/')})"
            f"\n• техработы: {'включены' if data.get('maintenance') else 'выключены'}"
            + arena_payout_line(data)
            + f"{uptime_line}"
        )
    else:
        api_status = f"⚠️ ответил кодом {status}"

    if chest.get("status") is None:
        chest_line = f"📦 Сундуки: ⚠️ не проверить ({chest.get('error', 'нет связи')})"
    elif chest.get("ok") and chest.get("rpc_ready"):
        chest_line = "📦 Сундуки: ✅ таблица chest_claims и функции на месте"
    elif chest.get("ok"):
        chest_line = (
            "📦 Сундуки: ⚠️ таблица есть, SQL-функций нет\n"
            "   • награда ждёт кнопку «Забрать» в памяти сервера (до его перезапуска)\n"
            "   • примените supabase/migrations/20261102000000_stock_and_chest_rpc.sql "
            "и 20261103000000_shop_take_stock_amount.sql"
        )
    else:
        chest_line = (
            "📦 Сундуки: ⚠️ таблица chest_claims недоступна — включён резервный режим\n"
            f"   • {chest.get('detail', 'ошибка базы')}\n"
            "   • играть можно: награда ждёт «Забрать» в памяти сервера (пропадёт при перезапуске)\n"
            "   • чтобы вернуть «Забрать»: примените "
            "supabase/migrations/20261030000000_capsule_stock_promo_types.sql "
            "и 20261102000000_stock_and_chest_rpc.sql, затем NOTIFY pgrst, 'reload schema'"
        )

    # Прогрев: включён ли и когда последний раз отвечал aura-api.
    # Задача 45: плюс чем закончилась последняя `!апка` (какая кнопка не прошла).
    # Задача 48: и живой ли адрес, который открывает Telegram по кнопке.
    miniapp_check = await check_miniapp_url()
    chest_line = (f"{chest_line}\n   • {keepwarm_line()}\n   • {app_link_line()}\n"
                  f"{miniapp_link_line(miniapp_check)}")

    # Шанс обычной капсулы в сундуках (задача 22) — прямо из /api/health, чтобы
    # админ видел реальные числа сервера, а не догадки.
    chests = data.get("chests") if isinstance(data, dict) else None
    if isinstance(chests, dict):
        def _pct(key):
            try:
                value = float((chests.get(key) or {}).get("capsule_chance"))
            except (TypeError, ValueError):
                return None
            return value * 100
        normal_pct, gold_pct = _pct("normal"), _pct("gold")
        if normal_pct is not None and gold_pct is not None:
            chest_line += (f"\n   • 💊 капсула: обычный {normal_pct:g}%, золотой {gold_pct:g}%")

    # Тираж капсул: если в health нет данных о стоке — значит магазин не знает остаток.
    # Задача 57: /api/stock_debug теперь требует initData, поэтому в тексте —
    # ссылка с подписью этого админа (открывается прямо в браузере).
    stock_debug_url = f"{API_URL}/api/stock_debug"
    probe_data = admin_probe_init_data(message.from_user.id)
    if probe_data:
        stock_debug_url = f"{stock_debug_url}?initData={quote(probe_data, safe='')}"
    stock = data.get("shop_stock") if isinstance(data, dict) else None
    build = (data.get("build") if isinstance(data, dict) else None) or "?"
    if not isinstance(stock, dict):
        stock_line = (
            "🎟️ Тираж: ❌ сервер НЕ умеет читать сток (старый код)\n"
            "   • задеплойте aura-api из ветки main и проверьте /api/stock_debug\n"
            f"     (открывать с initData: {stock_debug_url})"
        )
    elif stock.get("ready"):
        left = stock.get("left") or {}
        preview = ", ".join(f"{k}: {v}" for k, v in list(left.items())[:3])
        missing = stock.get("missing_rows") or []
        if missing:
            # Ровно тот баг, из-за которого карточка обещала «1000 из 1000»,
            # а покупка отвечала «Товар распродан»: строк тиража нет.
            stock_line = (
                f"🎟️ Тираж: ⚠️ нет строк для: {', '.join(missing)}\n"
                "   • магазин честно пишет «Остаток уточняется», покупка не блокируется\n"
                "   • лечение: примените supabase/migrations/20261106000000_stock_limits_on.sql\n"
                f"   • проверка: {stock_debug_url}"
            )
        else:
            stock_line = (
                f"🎟️ Тираж: ✅ {preview or 'пусто'}\n"
                f"   • RPC shop_take_stock: {stock.get('rpc', 'shop_take_stock')}"
                f" · проверка: {stock_debug_url}"
            )
    elif stock.get("limits_enabled") is False:
        stock_line = (
            "🎟️ Тираж: ⚠️ лимиты выключены в коде (STOCK_LIMITS_ENABLED = False)\n"
            "   • капсулы продаются без счётчика, «Осталось» не показывается"
        )
    else:
        stock_line = (
            "🎟️ Тираж: ❌ сервер не читает capsule_stock\n"
            f"   • {str(stock.get('error') or 'таблица недоступна')[:160]}\n"
            "   • примените supabase/migrations/20261106000000_stock_limits_on.sql\n"
            f"   • диагностика: {stock_debug_url}"
        )

    text = (
        "🖥️ <b>СЕРВИСЫ ПРОЕКТА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        f"1️⃣ <b>aura-api</b> (FastAPI): {api_status}{api_details}\n"
        f"   • API: <code>{API_URL}</code>\n"
        f"   • Mini App: <code>{WEBAPP_URL}</code> "
        "ℹ️ (только для BotFather; игрокам эту ссылку не отправляйте — "
        "у них кнопка Mini App в меню бота)\n"
        f"   • кнопка меню: <code>{miniapp_webapp_url()}</code>\n"
        f"   • {safe_text(startup_diagnostics())}\n\n"
        "2️⃣ <b>dan4Bot</b> (aiogram): ✅ этот процесс\n"
        "   • веб-сервер только для порта Render (start_fake_website)\n"
        "   • игровые данные читает из общей базы Supabase\n\n"
        f"🧱 Сборка API: <code>{build}</code>\n"
        f"🔗 Ссылка для игроков: {miniapp_direct_url()}\n"
        f"{chest_line}\n"
        f"{stock_line}\n"
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
    parts = strip_prefix(message_text(message)).split()
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
    parts = strip_prefix(message_text(message)).split()
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
        # Задача 74: кубы — ставка, победа и рекорд для профиля обоих.
        stats_note(winner_id, coins_earned=amount, won_dice=amount,
                   best_dice_win=amount, best_dice_bet=amount)
        ledger_note(winner_id, "win", amount, "🎲 Кубы")
        ledger_note(loser_id, "bet", amount, "🎲 Кубы")
        stats_note(loser_id, coins_spent=amount, spent_dice=amount,
                   lost_games=amount, best_dice_bet=amount)
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
        await asyncio.to_thread(purchase_shop_item, supabase, callback.from_user.id, item_id,
                                 ADMIN_IDS, "bot")
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


# ================= ОТНОШЕНИЯ (ЛЮБОВЬ) =================
# Таблицы: relationships (пара), love_stage_rewards (какие этапы оплачены),
# love_cooldowns (когда игрок последний раз получал +10 Love коинов).
# Этапы и награды живут в love.py — там же, откуда их читает Mini App,
# поэтому бот и профиль не могут показать разные цифры.
RELATIONSHIPS_TABLE = "relationships"
LOVE_COOLDOWNS_TABLE = "love_cooldowns"
# Задача 44: запасная отметка кулдауна РП в памяти процесса — чтобы награда
# не «размножалась» за каждое действие, если таблицы love_cooldowns нет.
# Задача 61: ключ — (user_id, команда), поэтому «обнять» и «поцеловать»
# считаются отдельно; старый ключ user_id оставлен для совместимости.
_LOVE_COOLDOWN_MEMORY = {}

# Предложения отношений ждут ответа не дольше 15 минут. Храним в памяти:
# если бот перезапустится — предложение просто истечёт (деньги не теряются).
love_proposals = {}
LOVE_PROPOSAL_TTL = 15 * 60


def _relationship_sync(user_id):
    """Активная пара игрока (в базе user_id бывает и числом, и строкой).

    Задача 44: колонки `status` может не быть (старая схема). Тогда фильтр по
    статусу падает, и раньше это выглядело как «пары нет» — хотя пара уже
    создана. Второй проход читает без фильтра и отсекает неактивные в Python.
    """
    try:
        res = supabase.table(RELATIONSHIPS_TABLE).select("*").eq("status", "active").execute()
        rows = res.data or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("relationships: чтение по статусу не удалось (%s) — читаю всё", exc)
        rows = supabase.table(RELATIONSHIPS_TABLE).select("*").execute().data or []
    for row in rows:
        status = str(row.get("status") or "active").lower()
        if status not in ("active", ""):
            continue
        if str(row.get("user_a")) == str(user_id) or str(row.get("user_b")) == str(user_id):
            return row
    return None


async def get_relationship(user_id):
    try:
        return await asyncio.to_thread(_relationship_sync, user_id)
    except Exception as e:
        logger.warning(f"Отношения недоступны (миграция не применена?): {e}")
        return None


# Попытка создать пару. Раньше любой сбой базы превращался в бесполезное
# «примените миграцию», хотя причин может быть три: нет таблицы, нет колонки
# (старая схема) или RLS без политики. Теперь пишем, что именно случилось, и
# сами повторяем вставку без «украшающих» колонок, если их нет в таблице.
_RELATIONSHIP_COLUMNS_DROPPABLE = ("bank", "status", "love", "updated_at")


def _relationship_error_text(exc) -> str:
    return str(exc or "")


def _missing_column_from_error(text: str):
    """PGRST204: "Could not find the 'bank' column of 'relationships'..." → bank."""
    import re as _re
    patterns = (
        r"could not find the '([a-z_]+)' column",
        r"column [\"']?(?:public\.)?relationships\.([a-z_]+)[\"']? does not exist",
        r"column [\"']([a-z_]+)[\"'] of relation",
    )
    low = text.lower()
    for pattern in patterns:
        match = _re.search(pattern, low)
        if match:
            return match.group(1)
    return None


def _create_relationship(proposer_id, target_id, logger_prefix="ПАРА: "):
    """Вставка пары с повтором без колонок, которых нет в старой таблице.

    Задача 44: если `status` в таблице нет, запись всё равно проходит — а
    значит ищется она через _relationship_sync (он смотрит status='active' и
    молча ничего не находил → «пары нет» сразу после создания).
    """
    payload = {
        "user_a": int(proposer_id), "user_b": int(target_id),
        "love": 0, "bank": 0, "status": "active",
    }
    last_error = None
    for _ in range(len(_RELATIONSHIP_COLUMNS_DROPPABLE) + 1):
        try:
            return supabase.table(RELATIONSHIPS_TABLE).insert(dict(payload)).execute()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            text = _relationship_error_text(exc)
            column = _missing_column_from_error(text)
            if column and column in payload and column in _RELATIONSHIP_COLUMNS_DROPPABLE:
                logger.warning("%sколонки «%s» нет в relationships — пишем без неё (%s)",
                               logger_prefix, column, text[:200])
                payload.pop(column, None)
                continue
            raise
    if last_error:
        raise last_error
    return None


def _relationship_failure_message(exc) -> str:
    """Человеческая причина, почему пара не создалась (и что делать)."""
    text = _relationship_error_text(exc)
    low = text.lower()
    if "row-level security" in low or "42501" in low or "permission denied" in low:
        return ("⚠️ База не даёт записать пару (RLS). Примените миграцию "
                "20261129000000_full_schema_all_points.sql в Supabase SQL Editor.")
    if ("could not find the table" in low or "does not exist" in low and "relationship" in low
            or "pgrst205" in low or "42p01" in low):
        return ("⚠️ Таблицы relationships нет. Примените миграцию "
                "20261129000000_full_schema_all_points.sql в Supabase SQL Editor.")
    if "уже состоит в активных отношениях" in low or "single active" in low:
        return "💔 Кто-то из вас уже в паре — сначала <code>-отны</code>."
    column = _missing_column_from_error(text)
    if column:
        return ("⚠️ В таблице relationships нет колонки «%s». Примените миграцию "
                "20261129000000_full_schema_all_points.sql." % column)
    return "❌ Не удалось создать пару. Попробуйте позже."


def _partner_of(row, user_id):
    try:
        a, b = int(row.get("user_a")), int(row.get("user_b"))
    except (TypeError, ValueError):
        return None
    return b if a == int(user_id) else a


def _stage_text(score: int) -> str:
    """Уровень пары для самих партнёров.

    Задача 29: этапы и награды за них — секретная механика. Показываем только
    текущий уровень и Love коины: игрок не должен считать «сколько дадут за
    следующий этап», иначе теряется смысл заводить отношения.
    """
    info = public_stage(score)
    # Задача 55: без строки «до следующего уровня» игрок видел «Уровень 1 ·
    # Love 0» и не понимал, что вообще двигает уровень (и растёт ли счёт).
    # Плюс прямая подсказка, чем его качать: РП-команды в паре.
    # Задача 72: прогресс показываем как «10/50❤️» — видно, сколько уже есть
    # на уровне и сколько осталось, вместо сухого «До уровня 2 — 40 Love».
    tail = ("➡️ Максимальный уровень — дальше только теплее"
            if info["next_level"] is None
            else f"{safe_text(action_progress_text(info['love']))} "
                 f"(качается РП-командами в паре)")
    return (f"💗 <b>Уровень {info['level']} · {safe_text(info['title'])}</b>\n"
            f"Love коинов: <b>{coin_amount(info['love'])}</b>\n{tail}")


def _love_days(row) -> int:
    """Сколько дней пара вместе (день создания = 1-й день)."""
    raw = row.get("created_at") if row else None
    if not raw:
        return 0
    try:
        from datetime import datetime, timezone
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        days = int((datetime.now(timezone.utc) - stamp).total_seconds() // 86400)
        return max(0, days)
    except Exception:
        return 0


def _days_text(days: int) -> str:
    """«1 день», «3 дня», «21 день», «31 день», «12 дней» — без «31 дней»."""
    if days <= 0:
        return "сегодня"
    tail = days % 100
    if 11 <= tail <= 14:
        word = "дней"
    else:
        last = tail % 10
        word = "день" if last == 1 else "дня" if last in (2, 3, 4) else "дней"
    return f"{days} {word}"


# Живые имена из Telegram (getChatMember). Кэш на час: имена меняются редко,
# а в списке предупреждений иначе уходил бы запрос на каждого игрока.
_LIVE_NAME_CACHE = {}
_LIVE_NAME_TTL = 3600


_NAME_CACHE: dict[int, tuple[float, str]] = {}
_NAME_CACHE_TTL = 300     # 5 минут: ники меняются редко, лишние SELECT не нужны


def _names_sync(user_ids) -> dict:
    """Ники пачкой: один запрос на всех (задача 53).

    Раньше `_player_name` делал отдельный SELECT на каждого игрока, и топ
    отношений из 10 пар превращался в 20 запросов подряд.
    """
    ids = []
    for uid in user_ids or []:
        try:
            value = int(uid)
        except (TypeError, ValueError):
            continue
        if value and value not in ids:
            ids.append(value)
    if not ids:
        return {}
    res = (supabase.table("users").select("user_id, username")
           .in_("user_id", ids).execute())
    return parse_names(res.data or [])


async def _names_map(user_ids) -> dict:
    """Ники с кэшем на 5 минут; при сбое базы — что есть в кэше (не падаем)."""
    now = time.time()
    ids = []
    for uid in user_ids or []:
        try:
            value = int(uid)
        except (TypeError, ValueError):
            continue
        if value and value not in ids:
            ids.append(value)
    wanted = [uid for uid in ids
              if not (_NAME_CACHE.get(uid) and now - _NAME_CACHE[uid][0] < _NAME_CACHE_TTL)]
    if wanted:
        try:
            fetched = await asyncio.to_thread(_names_sync, wanted)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ники игроков не прочитались: %s", exc)
            fetched = {}
        for uid in wanted:
            cached_live = _LIVE_NAME_CACHE.get(uid)
            if cached_live and now - cached_live[0] < _LIVE_NAME_TTL and cached_live[1]:
                # Живое имя из Telegram важнее базы: в users.username бывает
                # «протухший» ник вроде Contact_blocked (задача 55).
                _NAME_CACHE[uid] = (now, cached_live[1])
            else:
                _NAME_CACHE[uid] = (now, fetched.get(uid, ""))
    return merge_names({uid: _NAME_CACHE[uid][1] for uid in ids if uid in _NAME_CACHE},
                       {uid: rec[1] for uid, rec in _LIVE_NAME_CACHE.items()
                        if uid in ids and rec[1]})


async def _player_names(rows, keys=("user_a", "user_b")) -> dict:
    """Ники всех участников пары: `{id: ник}` без запросов по одному."""
    ids = []
    for row in rows or []:
        for key in keys:
            try:
                value = int(row.get(key))
            except (TypeError, ValueError):
                continue
            if value and value not in ids:
                ids.append(value)
    return await _names_map(ids)


async def _player_name(user_id) -> str:
    """Ник игрока без «@»: из базы, иначе id<число>.

    Задача 44: @username в ответах бота больше не выводим — вместо ника
    отдаём ссылку на профиль (`tg://user?id=…`), см. _player_mention.
    Задача 53: ник берём из общего кэша имён (один SELECT на всех), поэтому
    на каждое РП-действие больше не уходит отдельный запрос в базу.
    """
    if not user_id:
        return "игрок"
    cached = _LIVE_NAME_CACHE.get(int(user_id))
    if cached and time.time() - cached[0] < _LIVE_NAME_TTL and cached[1]:
        return cached[1]
    names = await _names_map([user_id])
    name = clean_name(names.get(int(user_id)))
    return name or f"id{int(user_id)}"


async def _display_name(message, user_id, known=None) -> str:
    """Как назвать игрока в чате: известное имя → база → участник чата → id.

    Задача 53: если в базе ника нет (человек писал боту без username), бот
    спрашивает имя у Telegram. Без этого в ответе появлялось «id123456789»,
    и игрок снова не понимал, о ком речь.
    """
    # 1) Имя, которое точно относится к этому человеку: Telegram-имя из
    #    сообщения-ответа. Задача 55: именно оно главное, потому что username
    #    в базе бывает «протухшим» (занят владельцем, который сменил ник).
    name = clean_name(known)
    if name:
        return name
    if not user_id:
        return "игрок"
    # 2) Живое имя из Telegram — оно главнее базы: в `users.username` бывает
    #    «протухший» ник (занят другим владельцем), и в чат попадал мусор
    #    вроде «Contact_blocked» вместо человека (задача 55).
    live = await _telegram_name(message, user_id)
    if live:
        return live
    # 3) База: username, который игрок сам указывал боту.
    names = await _names_map([user_id])
    name = clean_name(names.get(int(user_id)))
    if name:
        return name
    return f"id{int(user_id)}"


async def _telegram_name(message, user_id) -> str:
    """Имя игрока из Telegram: full_name, иначе @username.

    Задача 55: раньше здесь бралось только имя, и при пустом `first_name`
    в ответе появлялся мусорный `id<число>` — а в базе при этом лежал чужой
    username (например «Contact_blocked»). Теперь порядок такой: живое имя
    из Telegram, затем @username из Telegram, и только потом id.
    """
    if not user_id:
        return ""
    uid = int(user_id)
    cached = _LIVE_NAME_CACHE.get(uid)
    if cached and time.time() - cached[0] < _LIVE_NAME_TTL:
        return cached[1]
    try:
        member = await message.bot.get_chat_member(message.chat.id, uid)
        person = getattr(member, "user", None)
        name = clean_name(getattr(person, "full_name", None)
                          or getattr(person, "first_name", None))
        if not name:
            name = clean_name(getattr(person, "username", None))
        if name:
            _LIVE_NAME_CACHE[uid] = (time.time(), name)
        return name
    except Exception:  # noqa: BLE001 — нет прав/игрока нет в чате
        return ""


def _player_mention(user_id, name=None) -> str:
    """Ник со ссылкой на профиль (HTML). Без «@» — так требует задача 44."""
    if not user_id:
        return "игрок"
    import html as html_module
    label = html_module.escape(str(name or f"id{int(user_id)}").strip().lstrip("@"))
    return f'<a href="tg://user?id={int(user_id)}">{label}</a>'


async def _player_display(user_id) -> str:
    """Как показать игрока в тексте с parse_mode=HTML — только ссылкой."""
    if not user_id:
        return "игрок"
    return _player_mention(int(user_id), await _player_name(user_id))


async def _relationship_notice(row, user_id, subject: str, about_self: bool = False) -> str:
    """«Этот игрок состоит с этим-то в отношениях» — с ником партнёра.

    Раньше бот писал просто «этот игрок уже в отношениях»: в каком чате ни
    спроси, было непонятно, с кем именно, и игроки не верили, что пара реальна.
    """
    partner_id = _partner_of(row, user_id) if row else None
    partner_name = await _player_display(partner_id)
    verb = "уже состоишь" if about_self else "уже состоит"
    return f"💔 {subject} {verb} в отношениях с <b>{safe_text(partner_name)}</b>."


async def resolve_partner_target(message: Message):
    """@username или ответ на сообщение → id партнёра."""
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        return target.id, target.username or target.full_name
    for part in strip_prefix(message_text(message)).split()[1:]:
        if part.startswith("@"):
            found = await get_user_global(part)
            if found:
                return int(found.id), found.username
            # Игрок мог ещё не писать боту — ищем по users.username без @.
            return None, part
    return None, None


# ================= АДМИН: СВЕСТИ / РАЗВЕСТИ ПАРУ (задача 70) =================
# Заказчик: «!свести пару @юз @юз» и «!развести пару @юз @юз».
# Обе — только для админов бота. Сведение создаёт пару сразу, без согласия
# второго игрока (это инструмент админа), разведение удаляет строку пары,
# как обычная «-любовь»: статус снимается у обоих, Love сгорает.
_PAIR_SET_PREFIXES = ("!свести пару", "!свести", "свести пару")
_PAIR_UNSET_PREFIXES = ("!развести пару", "!развести", "развести пару")


def _pair_target_ids(text) -> list:
    """@ники и/или числовые id из сообщения — по порядку."""
    raw = strip_prefix(text or "")
    parts = raw.split()[2:] if raw.lower().startswith(("!свести", "!развести", "свести", "развести")) else raw.split()[1:]
    result = []
    for part in parts:
        token = part.strip().strip(",")
        if token.startswith("@"):
            result.append(token[1:])
        elif token.isdigit():
            result.append(token)
    return result


async def _resolve_pair_member(token):
    """@ник / id → (user_id, имя) или (None, причина)."""
    if str(token).isdigit():
        uid = int(token)
        return uid, await _player_name(uid)
    found = await get_user_global("@" + token)
    if found:
        return int(found.id), found.username or token
    # Игрок мог ещё не писать боту — ищем по users.username без @.
    def _by_name():
        try:
            rows = (supabase.table("users").select("user_id, username")
                    .ilike("username", token).limit(1).execute()).data or []
            return rows[0] if rows else None
        except Exception:
            return None
    row = await asyncio.to_thread(_by_name)
    if row and row.get("user_id"):
        return int(row["user_id"]), row.get("username") or token
    return None, token


# ================= АДМИН: ТЕХРАБОТЫ (задача 71) =================
# «Поставь всё на тех работы» — тумблер, а не деплой: `техработы вкл` закрывает
# игру всем, кроме ADMIN_IDS (и в чате, и в Mini App), `техработы выкл` снова
# открывает. По умолчанию выключено: нет строки в game_settings — играют все.
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    ("техработы", "/техработы", "!техработы", "тех работы", "/тех работы", "!тех работы"))))
async def cmd_maintenance(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Эта команда доступна только администраторам бота.")
    arg = strip_prefix(message_text(message)).lower().replace("ё", "е")
    for word in ("!техработы", "/техработы", "тех работы", "/тех работы", "!тех работы", "техработы"):
        arg = arg.replace(word, "", 1)
    arg = arg.strip()
    current = maintenance_bot_enabled()
    if arg in ("вкл", "включить", "включи", "on", "1", "да", "старт"):
        enabled = True
    elif arg in ("выкл", "выключить", "выключи", "off", "0", "нет", "стоп"):
        enabled = False
    elif arg in ("", "статус", "status", "?"):
        return await message.answer(
            ("🛠️ <b>Техработы включены</b>\n\nИграют только администраторы. "
             "Игроки видят «Технические работы: скоро вернёмся».\n\n"
             "Открыть всем: <code>техработы выкл</code>")
            if current else
            ("✅ <b>Техработы выключены</b> — играют все.\n\n"
             "Закрыть игру: <code>техработы вкл</code>"),
            parse_mode="HTML")
    else:
        return await message.answer(
            "🛠️ Формат: <code>техработы вкл</code> или <code>техработы выкл</code> "
            "(без аргумента — показать статус)", parse_mode="HTML")
    if not await asyncio.to_thread(set_maintenance_bot, enabled):
        return await message.answer("❌ Не удалось сохранить настройку, попробуй позже.")
    logger.warning("Техработы %s (админ %s)", "ВКЛЮЧЕНЫ" if enabled else "выключены",
                   message.from_user.id)
    if enabled:
        return await message.answer(
            "🛠️ <b>Техработы включены</b>\n\nИгра закрыта для всех, кроме администраторов — "
            "и в чате, и в приложении. Настройка уже применилась (до 15 секунд на сервере).\n\n"
            "Открыть обратно: <code>техработы выкл</code>", parse_mode="HTML")
    await message.answer(
        "✅ <b>Техработы выключены</b> — игра снова открыта всем.\n\n"
        "Закрыть игру: <code>техработы вкл</code>", parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    _PAIR_SET_PREFIXES)))
async def cmd_pair_set(message: Message):
    """`!свести пару @a @b` — админ сводит двух игроков."""
    if not message.from_user or message.from_user.is_bot:
        return
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Эта команда доступна только администраторам бота.")
    tokens = _pair_target_ids(message_text(message))
    if len(tokens) < 2:
        return await message.answer(
            "💞 Формат: <code>!свести пару @ник1 @ник2</code>",
            parse_mode="HTML")
    first_id, first_name = await _resolve_pair_member(tokens[0])
    second_id, second_name = await _resolve_pair_member(tokens[1])
    missing = [tokens[i] for i, uid in enumerate((first_id, second_id)) if uid is None]
    if missing:
        return await message.answer(
            f"❌ Не нашёл игрока: {', '.join('@' + str(m) for m in missing)}.\n"
            "Он должен хотя бы раз написать боту.", parse_mode="HTML")
    if first_id == second_id:
        return await message.answer("🙃 Это один и тот же игрок — пару так не свести.")
    existing = await get_relationship(first_id)
    if existing:
        partner = _partner_of(existing, first_id)
        return await message.answer(
            f"💔 {await _player_display(first_id)} уже в паре с {await _player_display(partner)}.\n"
            "Сначала: <code>!развести пару</code>.", parse_mode="HTML")
    existing = await get_relationship(second_id)
    if existing:
        partner = _partner_of(existing, second_id)
        return await message.answer(
            f"💔 {await _player_display(second_id)} уже в паре с {await _player_display(partner)}.\n"
            "Сначала: <code>!развести пару</code>.", parse_mode="HTML")
    try:
        await asyncio.to_thread(_create_relationship, first_id, second_id, "СВЕСТИ: ")
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось свести пару %s + %s: %s", first_id, second_id, exc)
        return await message.answer(_relationship_failure_message(exc), parse_mode="HTML")
    # Сведение — это не «случайное знакомство»: обнуляем одиночные счётчики,
    # иначе у игроков остаётся Unlove, которого в паре уже не должно быть.
    for uid in (first_id, second_id):
        try:
            await drop_solo_status(uid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось сбросить Unlove при сведении %s: %s", uid, exc)
    first_mention = await _player_display(first_id)
    second_mention = await _player_display(second_id)
    stage = public_stage(0)
    await message.answer(
        f"👩‍❤️‍👨 <b>{safe_text(message.from_user.full_name)} свёл {first_mention} и {second_mention}</b>\n\n"
        f"💗 Теперь вы пара!\n\n"
        f"💗 Уровень {stage['level']} · {safe_text(stage['title'])}\n"
        f"Love коинов: <b>0</b>\n"
        f"➡️ {safe_text(next_level_text(0))} (качается РП-командами в паре)\n\n"
        f"Пишите друг другу РП-команды: <code>поцеловать</code>, <code>обнять</code>, "
        f"<code>прижать</code>… — так копится Love.\n"
        f"Весь список: <code>все рп</code> · ваша пара: <code>любовь инфа</code>",
        parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    _PAIR_UNSET_PREFIXES)))
async def cmd_pair_unset(message: Message):
    """`!развести пару @a @b` — админ разводит пару."""
    if not message.from_user or message.from_user.is_bot:
        return
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Эта команда доступна только администраторам бота.")
    tokens = _pair_target_ids(message_text(message))
    if not tokens:
        return await message.answer(
            "💔 Формат: <code>!развести пару @ник1 @ник2</code>",
            parse_mode="HTML")
    first_id, _first_name = await _resolve_pair_member(tokens[0])
    if first_id is None:
        return await message.answer(
            f"❌ Не нашёл игрока @{tokens[0]}.", parse_mode="HTML")
    row = await get_relationship(first_id)
    if not row:
        return await message.answer(
            f"💔 {await _player_display(first_id)} ни с кем не в паре.",
            parse_mode="HTML")
    if len(tokens) >= 2:
        second_id, _second_name = await _resolve_pair_member(tokens[1])
        if second_id is not None and _partner_of(row, first_id) != second_id:
            return await message.answer(
                f"💔 {await _player_display(first_id)} в паре не с "
                f"{await _player_display(second_id)}, а с "
                f"{await _player_display(_partner_of(row, first_id))}.",
                parse_mode="HTML")
    partner = _partner_of(row, first_id)
    try:
        await asyncio.to_thread(lambda: supabase.table(RELATIONSHIPS_TABLE)
                                .delete().eq("id", row["id"]).execute())
    except Exception as exc:  # noqa: BLE001
        logger.error("Не удалось развести пару %s: %s", row.get("id"), exc)
        return await message.answer("❌ Не удалось развести пару, попробуй позже.")
    love_proposals.pop(first_id, None)
    love_proposals.pop(partner, None)
    await message.answer(
        f"💔 <b>{safe_text(message.from_user.full_name)} развёл пару</b>\n\n"
        f"{await _player_display(first_id)} и {await _player_display(partner)} "
        f"снова свободны: статус снят у обоих, накопленные Love сброшены.\n"
        f"Заработанные награды за уровни остаются у игроков — их никто не отбирает.\n\n"
        f"Начать заново: <code>+любовь</code> · все РП: <code>все рп</code>",
        parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("+любовь")))
async def cmd_love_propose(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    proposer_id = message.from_user.id
    target_id, target_name = await resolve_partner_target(message)
    if target_id is None:
        return await message.answer(
            "💗 Формат: <code>+любовь</code> ответом на сообщение или "
            "<code>+любовь @ник</code>.\n"
            "Все РП-команды: <code>все рп</code> · своя пара: <code>любовь инфа</code>",
            parse_mode="HTML")
    if target_id == proposer_id:
        return await message.answer("🙃 Себя любить можно, но пару так не создать.")
    existing = await get_relationship(proposer_id)
    if existing:
        notice = await _relationship_notice(existing, proposer_id, "Ты", about_self=True)
        return await message.answer(f"{notice}\nСначала <code>-любовь</code>.", parse_mode="HTML")
    target_relationship = await get_relationship(target_id)
    if target_relationship:
        # Работает в ЛЮБОМ чате: отношения общие, и второй пары быть не может.
        subject = _player_mention(target_id, target_name or "Этот игрок")
        notice = await _relationship_notice(target_relationship, target_id, subject)
        return await message.answer(notice, parse_mode="HTML")

    love_proposals[target_id] = {"from": proposer_id, "at": time.time()}
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💗 Да", callback_data=f"love_yes|{proposer_id}"),
        InlineKeyboardButton(text="💔 Нет", callback_data=f"love_no|{proposer_id}"),
    ]])
    await message.answer(
        f"💌 <b>Предложение отношений</b>\n\n"
        f"{_player_mention(target_id, target_name or 'Игрок')}, "
        f"{_player_mention(proposer_id, message.from_user.full_name)} "
        f"зовёт тебя в пару!\n\n"
        f"За РП-команды пара получает +{LOVE_ACTION_BONUS} Love коинов. "
        f"Список команд: <code>все рп</code>.",
        parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("love_yes|") | F.data.startswith("love_no|"))
async def cb_love_answer(callback: CallbackQuery):
    action, _, raw_from = (callback.data or "").partition("|")
    proposer_id = int(raw_from or 0)
    target_id = callback.from_user.id
    proposal = love_proposals.get(target_id)
    if not proposal or proposal.get("from") != proposer_id or time.time() - proposal["at"] > LOVE_PROPOSAL_TTL:
        love_proposals.pop(target_id, None)
        return await callback.answer("Предложение уже неактуально", show_alert=True)

    if action == "love_no":
        love_proposals.pop(target_id, None)
        await callback.message.edit_text("💔 Предложение отклонено. Ничего страшного — впереди ещё много игроков.")
        return await callback.answer()

    own = await get_relationship(target_id)
    other = await get_relationship(proposer_id)
    if own or other:
        love_proposals.pop(target_id, None)
        row = own or other
        owner = target_id if own else proposer_id
        partner_id = _partner_of(row, owner)
        # alert — обычный текст: HTML там не работает, поэтому ник без тегов.
        partner_name = await _player_name(partner_id)
        return await callback.answer(
            f"💔 Уже есть пара: {partner_name}. Сначала -любовь.", show_alert=True)

    try:
        await asyncio.to_thread(_create_relationship, proposer_id, target_id)
    except Exception as e:
        logger.error(f"Не удалось создать пару: {e}")
        return await callback.answer(_relationship_failure_message(e), show_alert=True)

    # Задача 30: одиночный статус обоих сгорает — начинается общая история пары.
    await drop_solo_status(proposer_id)
    await drop_solo_status(target_id)
    love_proposals.pop(target_id, None)
    await callback.message.edit_text(
        f"💗 <b>Теперь вы пара!</b>\n\n{_stage_text(0)}\n\n"
        f"Пишите друг другу РП-команды: <code>поцеловать</code>, <code>обнять</code>, "
        f"<code>прижать</code>… — так копится Love.\n"
        f"Весь список: <code>все рп</code> · ваша пара: <code>любовь инфа</code>",
        parse_mode="HTML")
    await callback.answer("💗")


# Разрыв: `-любовь` (основная команда с задачи 44), плюс `-отны`, `-отн`,
# `-отношения`, `-пара` и старое `разлюбить` — все варианты продолжают
# работать, чтобы игроки, привыкшие к старой форме, ничего не заметили.
# Удаляем СТРОКУ пары, а не помечаем её broken: у обоих исчезает статус и весь
# накопленный Love, а заново начать историю можно с чистого листа. Уже
# выплаченные награды за этапы не отзываются (деньги не отбираем).
# ВАЖНО: порядок проверки в фильтре — сначала «-любовь», потом остальные.
# Отдельного хендлера на «-любовь» больше нет (задача 44), поэтому старый
# «-отны» и короткие формы остаются синонимами.
_BREAK_PREFIXES = ("-любовь", "-отны", "-отн", "-отношения", "-отнош", "-пара",
                   "разлюбить")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    _BREAK_PREFIXES)))
async def cmd_love_break(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    row = await get_relationship(message.from_user.id)
    if not row:
        return await message.answer(
            "💔 Ты пока ни с кем не в отношениях.\n\n"
            "Своя пара: <code>любовь инфа</code> · РП-команды: <code>все рп</code>",
            parse_mode="HTML")
    try:
        await asyncio.to_thread(lambda: supabase.table(RELATIONSHIPS_TABLE)
                                .delete().eq("id", row["id"]).execute())
    except Exception as e:
        logger.error(f"Не удалось разорвать отношения: {e}")
        return await message.answer("❌ Не удалось разорвать отношения, попробуй позже.")
    partner = _partner_of(row, message.from_user.id)
    love_proposals.pop(partner, None)
    love_proposals.pop(message.from_user.id, None)
    partner_mention = await _player_display(partner)
    await message.answer(
        f"💔 <b>Отношения завершены.</b>\n\n"
        f"Вы с {partner_mention} снова свободны: статус снят у обоих, "
        f"накопленные Love сброшены.\n"
        f"Заработанные награды остаются у вас — их никто не отбирает.\n\n"
        f"Теперь можно копить <b>{SOLO_CURRENCY}</b> в одиночку "
        f"(<code>любовь инфа</code>) или начать новую историю: "
        f"<code>+любовь</code> ответом на сообщение.",
        parse_mode="HTML")


def _love_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _love_short(value, limit: int = 14) -> str:
    text = str(value or "игрок")   # пустое имя подменяет вызывающий (id<число>)
    return text if len(text) <= limit else text[:limit - 1] + "…"


async def love_top_rows():
    """Все активные пары + ники игроков одним проходом по базе."""
    def _fetch():
        rows = (supabase.table(RELATIONSHIPS_TABLE)
                .select("id, user_a, user_b, love, bank, status")
                .eq("status", "active")
                .order("love", desc=True)
                .execute().data) or []
        ids = set()
        for row in rows:
            for key in ("user_a", "user_b"):
                try:
                    ids.add(int(row.get(key)))
                except (TypeError, ValueError):
                    continue
        # Задача 53: один запрос только по нужным id (раньше тянулась вся
        # таблица users целиком — на большом боте это тысячи строк).
        names = {}
        if ids:
            users = (supabase.table("users").select("user_id, username")
                     .in_("user_id", sorted(ids)).execute().data) or []
            names = parse_names(users)
        return rows, names

    return await asyncio.to_thread(_fetch)


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith(("топ отношений", "топ любви"))))
async def cmd_top_love(message: Message):
    """Общая статистика пар: Love коины, этап и место в топе.

    Отношения хранятся в Supabase (таблица relationships) и не привязаны к
    чату: пара, созданная в одном чате, видна и в другом.
    """
    try:
        rows, names = await love_top_rows()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Топ отношений недоступен (миграция не применена?): %s", exc)
        return await message.answer(
            "❌ Не удалось загрузить отношения. Проверьте, что применена миграция "
            "<code>20261109000000_economy_social.sql</code>.", parse_mode="HTML")

    if not rows:
        return await message.answer(
            "💗 <b>ТОП ОТНОШЕНИЙ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
            "Пока ни одной пары. Начни первым: <code>+любовь</code> ответом на сообщение"
            " или <code>+любовь @ник</code>",
            parse_mode="HTML")

    total_love = sum(int(row.get("love") or 0) for row in rows)
    # Задача 29: этапы и награды — секретная механика. В топе только пары и
    # Love коины: иначе игроки считали бы «сколько дадут за следующий этап».
    lines = [
        "💗 <b>ТОП ОТНОШЕНИЙ</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 Пар: <b>{len(rows)}</b> · всего Love: <b>{coin_amount(total_love)}</b>",
        "",
    ]
    medals = ["🥇", "🥈", "🥉"]
    for index, row in enumerate(rows[:10]):
        score = int(row.get("love") or 0)
        # Задача 53: если ника нет — подпись id<число>, но не безликий «игрок».
        pair = [(uid, names.get(uid) or f"id{uid}")
                for uid in (_love_int(row.get("user_a")), _love_int(row.get("user_b")))]
        # Задача 44: вместо @username — ник со ссылкой на профиль.
        shown = " 💞 ".join(_player_mention(uid, _love_short(name)) for uid, name in pair)
        place = medals[index] if index < 3 else f"{index + 1}."
        days = _love_days(row)
        lines.append(
            f"{place} <b>{shown}</b>\n"
            f"    💗 {coin_amount(score)} Love · вместе {_days_text(days)}")
    if len(rows) > 10:
        lines.append(f"\n…и ещё {len(rows) - 10} пар(ы).")
    lines.append("\n<i>Отношения общие для всех чатов. Второй пары быть не может.\n"
                 "Одиночки в топ не попадают — здесь только пары.</i>")
    await message.answer("\n".join(lines), parse_mode="HTML")


# Команды «инфо» и «список РП» начинаются со слов, которые этот фильтр тоже
# ловит («отношения инфо»), поэтому исключаем их явно — иначе до своего
# обработчика дело не доходило бы: aiogram берёт первый совпавший хендлер.
# Задача 44: основная команда — «любовь инфа» (старые формы остаются рабочими).
_LOVE_INFO_PREFIXES = ("любовь инфа", "любовь инфо", "отн инфа", "отн инфо",
                       "инфа отношений", "отношения инфа", "отношения инфо",
                       "любовь инфо")
_RP_LIST_PREFIXES = ("все рп", "рп команды", "рп список", "список рп")


def _love_status_filter(text) -> bool:
    clean = strip_prefix(text).lower().replace("ё", "е")
    if clean.startswith(_LOVE_INFO_PREFIXES) or clean.startswith(_RP_LIST_PREFIXES):
        return False
    if clean.startswith(("-", "+")):
        return False        # «-любовь» — разрыв, «+любовь» — предложение
    return clean.startswith("любовь") or clean.startswith(("пара", "отношения"))


@dp.message(F.text.func(_love_status_filter))
async def cmd_love_status(message: Message):
    if not message.from_user:
        return
    row = await get_relationship(message.from_user.id)
    if not row:
        solo = await get_solo_love(message.from_user.id)
        info = public_stage(solo)
        return await message.answer(
            f"💔 <b>Пока один(а)</b> · {SOLO_CURRENCY}: <b>{coin_amount(solo)}</b>\n"
            f"🏅 Уровень: <b>{info['level']} · {safe_text(info['title'])}</b>\n"
            f"➡️ {safe_text(next_level_text(solo, SOLO_CURRENCY))} — уровень растёт "
            f"от {SOLO_CURRENCY}\n\n"
            f"РП-команды работают и без пары — {SOLO_CURRENCY} капает только тебе.\n"
            f"Предложить отношения: <code>+любовь</code> ответом на сообщение\n"
            f"Все РП-команды: <code>все рп</code> · подробно: <code>любовь инфа</code>",
            parse_mode="HTML")
    partner_id = _partner_of(row, message.from_user.id)
    partner_mention = await _player_display(partner_id)
    score = int(row.get("love") or 0)
    await message.answer(
        f"💗 <b>В отношениях</b> с {partner_mention}\n\n{_stage_text(score)}\n\n"
        f"Подробно: <code>любовь инфа</code> · РП: <code>все рп</code>",
        parse_mode="HTML")


# ================= ОТНОШЕНИЯ: ИНФО И СПИСОК РП =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    _LOVE_INFO_PREFIXES)))
async def cmd_love_info(message: Message):
    """Информация об отношениях: своя — всем, чужая — только админам.

    «Заглядывать в чужой карман» можно, а в отношения — нет: это личное,
    поэтому бот отвечает только за себя, а админ может проверить любого
    (например, по жалобе «он пишет, что мы в паре»).
    """
    if not message.from_user or message.from_user.is_bot:
        return
    viewer_id = message.from_user.id
    target_id, target_name = await resolve_partner_target(message)
    if target_id is None:
        target_id = viewer_id
    is_admin_viewer = is_admin(viewer_id)

    if target_id != viewer_id and not is_admin_viewer:
        return await message.answer(
            "🔒 Отношения — личное. Этот игрок должен написать "
            "<code>любовь инфа</code> сам, чужие отношения видят только админы.",
            parse_mode="HTML")

    row = await get_relationship(target_id)
    who_name = await _player_name(target_id)
    # Задача 44: без @username — ник со ссылкой на профиль (как и в остальных ответах).
    header = ("ТВОИ ОТНОШЕНИЯ" if target_id == viewer_id
              else f"ОТНОШЕНИЯ · {safe_text(who_name.upper())}")
    if not row:
        solo = await get_solo_love(target_id)
        info = public_stage(solo)
        if target_id == viewer_id:
            return await message.answer(
                f"💔 <b>ТВОИ ОТНОШЕНИЯ</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Пока ни с кем — и это нормально.\n"
                f"🏅 Уровень: <b>{info['level']} · {safe_text(info['title'])}</b>\n"
                f"💔 {SOLO_CURRENCY}: <b>{coin_amount(solo)}</b>\n"
                f"➡️ {safe_text(next_level_text(solo, SOLO_CURRENCY))} — уровень "
                f"растёт от {SOLO_CURRENCY}\n\n"
                f"РП-команды работают и в одиночку: просто напиши <code>обнять</code>, "
                f"<code>поцеловать</code>, <code>подарить цветы</code>… — "
                f"+{LOVE_ACTION_BONUS} {SOLO_CURRENCY} только тебе.\n"
                f"Передать {SOLO_CURRENCY} другому нельзя — только заработать.\n"
                f"Вступишь в отношения — одиночный счёт сгорит, начнёте общий.\n\n"
                f"Список РП: <code>все рп</code> · предложить пару: <code>+любовь</code> "
                f"ответом на сообщение",
                parse_mode="HTML")
        return await message.answer(
            f"💔 У этого игрока сейчас нет отношений.\n"
            f"{SOLO_CURRENCY}: <b>{coin_amount(solo)}</b> · уровень {info['level']}",
            parse_mode="HTML")

    partner_id = _partner_of(row, target_id)
    partner_mention = await _player_display(partner_id)
    score = int(row.get("love") or 0)
    info = public_stage(score)
    days = _love_days(row)
    await message.answer(
        f"💗 <b>{header}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💞 В паре с: <b>{partner_mention}</b>\n"
        f"📅 Вместе: <b>{_days_text(days)}</b>\n"
        f"🏅 Уровень: <b>{info['level']} · {safe_text(info['title'])}</b>\n"
        f"💗 Love коинов: <b>{coin_amount(score)}</b>\n"
        f"{safe_text(action_progress_text(score))}",
        parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().replace("ё", "е").startswith(
    _RP_LIST_PREFIXES)))
async def cmd_rp_list(message: Message):
    """Список всех РП-команд одной командой: «все рп»."""
    lines = ["💞 <b>ВСЕ РП-КОМАНДЫ</b>", "━━━━━━━━━━━━━━━━━━━━",
             "Пиши команду и ник партнёра: <code>поцеловать @ник</code> "
             "(или ответом на его сообщение).", ""]
    for key in RP_ACTIONS:
        meta = RP_ACTIONS[key]
        lines.append(f"{meta['emoji']} <code>{key}</code> — {meta['text']}")
    lines.append("")
    # Задача 76: КД ОДИН на игрока — после любого действия нельзя ничего 5 часов.
    # Задача 79: админ может выключить его кнопкой в админ-панели — тогда пишем
    # честно «без ограничения», а не обещаем 5 часов.
    cooldown_seconds = active_love_cooldown_seconds()
    if cooldown_seconds > 0:
        lines.append(f"💗 За действие пара получает +{LOVE_ACTION_BONUS} Love. "
                     f"Кулдаун: <b>{format_love_cooldown(cooldown_seconds)}</b> — один на все "
                     f"команды сразу: использовал «обнять», значит остальные тоже ждут.")
    else:
        lines.append(f"💗 За действие пара получает +{LOVE_ACTION_BONUS} Love. "
                     f"Кулдаун сейчас <b>выключен</b> — РП-команды не ждут.")
    lines.append(f"💔 Без пары то же действие даёт +{LOVE_ACTION_BONUS} {SOLO_CURRENCY} "
                 f"— и только тому, кто пишет: передать Love другому нельзя.")
    lines.append("💞 Своя пара: <code>любовь инфа</code> · все пары: <code>топ отношений</code>")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def love_cooldown_left(user_id, action=None) -> int:
    """Сколько секунд осталось до следующей РП-награды (0 — можно).

    Задача 76: заказчик развернул правило задачи 61 — КД ОДИН на игрока, общий
    для всех РП-команд. «Использовал обнять — 5 часов нельзя ничего»; в чате на
    скрине после «обнять» тут же сработало «посмотреть фильм», и это было
    ошибкой. `action` больше ни на что не влияет и оставлен для совместимости.

    Источники отметки, по приоритету:
      1) таблица love_cooldowns — ЛЮБАЯ отметка игрока (после задачи 76 пишется
         ровно одна строка с action='*all');
      2) память процесса, если таблица недоступна;
      3) старые строки по командам (сборки до задачи 76) — они тоже считаются
         «игрок недавно получал награду», иначе после обновления можно было бы
         собрать награду повторно.

    Ошибка базы не должна «обнулять» кулдаун — иначе награда капает за каждое
    действие. Поэтому при сбое читаем память, а не возвращаем 0.
    """
    uid = int(user_id)

    def _read():
        try:
            res = (supabase.table(LOVE_COOLDOWNS_TABLE)
                   .select("action, action_at").eq("user_id", uid).execute())
        except Exception:
            # Старая схема без колонки action (миграция не применена): одна
            # отметка на игрока — ровно то, что теперь и нужно.
            res = (supabase.table(LOVE_COOLDOWNS_TABLE)
                   .select("action_at").eq("user_id", uid).execute())
        return [row.get("action_at") for row in (res.data or []) if row.get("action_at")]

    try:
        stamps = await asyncio.to_thread(_read)
    except Exception as exc:  # noqa: BLE001 — таблицы нет / нет прав на чтение
        logger.debug("Кулдаун не прочитался из базы (%s) — считаю по памяти", exc)
        stamps = []
    # Память: новый ключ (игрок, '*all') + старые ключи по командам.
    stamps = list(stamps)
    for (memory_uid, _key), stamp in _LOVE_COOLDOWN_MEMORY.items():
        if memory_uid == uid and stamp:
            stamps.append(stamp)
    # Задача 79: «0 секунд», если админ выключил КД кнопкой в админ-панели.
    cooldown = await asyncio.to_thread(active_love_cooldown_seconds)
    if cooldown <= 0:
        return 0
    best = 0
    from datetime import datetime, timezone
    for value in stamps:
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            best = max(best, int(cooldown - (time.time() - stamp.timestamp())))
        except Exception:  # noqa: BLE001 — битая отметка не должна ломать КД
            continue
    return max(0, best)


async def love_cooldown_clear(user_id, action=None) -> None:
    """Снять отметку кулдауна (награда не выдалась — ждать нечего).

    Задача 76: отметка одна на игрока, поэтому удаляем все его строки — в том
    числе оставшиеся от старой схемы «на команду».
    """
    uid = int(user_id)
    for key in [key for key in _LOVE_COOLDOWN_MEMORY if key[0] == uid]:
        _LOVE_COOLDOWN_MEMORY.pop(key, None)

    def _delete():
        try:
            supabase.table(LOVE_COOLDOWNS_TABLE).delete().eq("user_id", uid).execute()
        except Exception as exc:  # noqa: BLE001 — старая схема/нет прав
            logger.debug("Отметку кулдауна снять не удалось: %s", exc)

    try:
        await asyncio.to_thread(_delete)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Отметку кулдауна снять не удалось: %s", exc)


# Задача 64: гонка двух одинаковых команд. Два сообщения «обнять» подряд (или
# команда + кнопка) успевали пройти проверку КД до того, как первый запишет
# отметку, — награда приходила дважды.
# Задача 76: КД общий, поэтому блокировка тоже одна на игрока: иначе «обнять»
# и «посмотреть фильм», отправленные одновременно, оба прошли бы проверку.
_COOLDOWN_LOCKS = {}


def love_action_lock(user_id, action=None):
    """asyncio-блокировка на игрока: проверка и начисление РП — по очереди."""
    key = (int(user_id), "*all")
    lock = _COOLDOWN_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _COOLDOWN_LOCKS[key] = lock
    # Словарь не должен расти бесконечно: держим только активные блокировки.
    if len(_COOLDOWN_LOCKS) > 2000:
        for stale_key, stale_lock in list(_COOLDOWN_LOCKS.items()):
            if not stale_lock.locked() and stale_key != key:
                _COOLDOWN_LOCKS.pop(stale_key, None)
    return lock


async def love_cooldown_touch(user_id, action=None):
    """Отметить «РП-награда получена сейчас» — одна отметка на игрока.

    Задача 76: команда больше не важна, поэтому пишем одну строку с
    action='*all' (это же значение понимают старые схемы и старые сборки).

    Задача 64: отметку в памяти ставим ПЕРВОЙ строкой — даже если запись в базу
    упадёт (нет таблицы, RLS, не применена миграция), кулдаун продолжит
    работать до рестарта.
    """
    stamp = datetime_now_iso()
    uid = int(user_id)
    # Память — полноценный источник: ключ (игрок, '*all').
    _LOVE_COOLDOWN_MEMORY[(uid, "*all")] = stamp

    def _write():
        try:
            supabase.table(LOVE_COOLDOWNS_TABLE).upsert(
                {"user_id": uid, "action": "*all", "action_at": stamp},
                on_conflict="user_id,action").execute()
            return
        except Exception as exc:  # noqa: BLE001 — старая схема без колонки action
            logger.debug("upsert кулдауна по '*' не удался (%s)", exc)
        # Старая схема: одна строка на игрока.
        supabase.table(LOVE_COOLDOWNS_TABLE).upsert(
            {"user_id": uid, "action_at": stamp}, on_conflict="user_id").execute()

    try:
        await asyncio.to_thread(_write)
    except Exception as e:
        # Запись не прошла (нет таблицы/прав). В памяти отметка уже стоит, и
        # love_cooldown_left её учитывает — кулдаун продолжает работать до
        # рестарта, а причина видна в логе.
        logger.warning("Не удалось сохранить кулдаун любви (работаю по памяти): %s", e)


def datetime_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def love_write_hint() -> str:
    """Человеческая причина, почему Love/Unlove не записались (для игрока)."""
    return ("Скорее всего в базе нет таблицы отношений или политики записи — "
            "передай админу: <code>relationships</code> / <code>solo_love</code>, "
            "миграция <code>20261129000000_full_schema_all_points.sql</code>.")


async def add_love_points(row, amount: int):
    """+Love коины паре и награды → (new_love, granted, записалось ли).

    Задача 44: если `bank` в таблице нет (старая схема), пишем только love —
    раньше update падал целиком и Love не рос, хотя игрок видел успех.
    Задача 55: третий элемент — «реально записалось». Без него бот писал
    «+10 Love (всего 10)», а в базе оставался ноль: игрок открывал
    «любовь инфа» и видел «Love коинов: 0» сразу после РП-действия.
    """
    old_score = int(row.get("love") or 0)
    new_score = old_score + int(amount)
    saved = False
    for payload in ({"love": new_score, "bank": new_score}, {"love": new_score}):
        try:
            res = await asyncio.to_thread(lambda p=payload: supabase.table(RELATIONSHIPS_TABLE)
                                          .update(p).eq("id", row["id"]).execute())
            saved = True
            # Берём число из ответа базы: если строка в памяти была старой,
            # игрок увидит именно то, что лежит в таблице.
            written = (getattr(res, "data", None) or [{}])[0].get("love")
            if written is not None:
                new_score = int(written)
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Love не записался (%s): %s", list(payload), exc)
    if not saved:
        # База отказала: не выдаём награды и не рисуем рост, которого нет.
        return old_score, [], False
    row = dict(row)
    row["love"] = new_score
    try:
        granted = await asyncio.to_thread(claim_stage_rewards, supabase, row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Награды за этап Love не выданы: %s", exc)
        granted = []
    # Пустые этапы не считаем: если награду уже выплатили раньше (или её закрыла
    # страховка API), игроку не нужно второе «вы достигли уровня».
    granted = [item for item in granted if int(item.get("reward") or 0) > 0]
    # Задача 74: приз за уровень — «заработано» в профиле каждого партнёра.
    if granted:
        total = sum(int(item.get("reward") or 0) for item in granted)
        for partner in (row.get("user_a"), row.get("user_b")):
            try:
                partner_id = int(partner)
            except (TypeError, ValueError):
                continue
            stats_note(partner_id, coins_earned=total, won_love=total)
            ledger_note(partner_id, "reward", total, "💗 Награда за уровень")
    return new_score, granted, True


# ================= ОДИНОКИЕ: UNLOVE =================
# Задача 30: у кого пары нет — тот не «застревает», а качает свою ветку.
# Экономика та же (те же +10, тот же кулдаун, те же этапы), но награду за этап
# получает один человек, а не двое. Как только игрок вступает в отношения,
# одиночный счёт сгорает (см. reset_solo_love).
async def get_solo_love(user_id) -> int:
    """Сколько Unlove накопил одиночка (нет таблицы — 0)."""
    try:
        state = await asyncio.to_thread(solo_state, supabase, user_id)
    except Exception as e:
        logger.warning(f"Unlove недоступен: {e}")
        return 0
    return int((state or {}).get("love") or 0)


async def add_solo_points(user_id, amount: int):
    """+Unlove → (счёт, награды, записалось ли). База отказала — счёт прежний."""
    before = await get_solo_love(user_id)
    try:
        score, granted = await asyncio.to_thread(add_solo_love, supabase, user_id, amount)
    except Exception as e:
        logger.warning(f"Не удалось начислить Unlove: {e}")
        return before, [], False
    if int(score) < int(before) + int(amount):
        return int(score), [], False        # прибавки нет — значит не записалось
    try:
        saved = await asyncio.to_thread(solo_saved, supabase, user_id, score)
    except Exception:  # noqa: BLE001
        saved = False
    # Задача 74: приз одиночке — тоже «заработано призами» в профиле.
    paid = [item for item in (granted or []) if int(item.get("reward") or 0) > 0]
    if paid and saved:
        total = sum(int(item.get("reward") or 0) for item in paid)
        stats_note(user_id, coins_earned=total, won_love=total)
        ledger_note(user_id, "reward", total, "💗 Награда за уровень")
    return score, granted, saved


async def drop_solo_status(user_id):
    """Вступление в отношения: одиночный счёт сгорает."""
    try:
        await asyncio.to_thread(reset_solo_love, supabase, user_id)
    except Exception as e:
        logger.warning(f"Не удалось сбросить Unlove: {e}")


def _human_left(seconds) -> str:
    """«5 ч 0 мин» / «12 мин» / «40 сек» — сколько ждать до следующей награды."""
    left = max(0, int(seconds or 0))
    hours, minutes = left // 3600, (left % 3600) // 60
    if hours > 0:
        return f"{hours} ч {minutes} мин"
    if minutes > 0:
        return f"{minutes} мин"
    return f"{left} сек"


def _cooldown_wait_text(action, left, currency="Love") -> str:
    """«Следующая РП-награда через 4 ч 12 мин» — без обещаний про другие команды.

    Задача 76: кулдаун ОДИН на игрока и общий для всех РП, поэтому фраза
    «кулдаун считается отдельно для каждой команды» была прямым обманом —
    заказчик видел в чате, что после «обнять» работает «смотреть фильм».
    """
    name = f"«{action}»" if action else "РП"
    return (f"⏳ После {name} награда за РП будет через {_human_left(left)}. "
            f"Кулдаун один на все команды: пока он идёт, другие РП тоже не дают награду.")


async def _notify_stage_rewards(row, actor_id, granted) -> None:
    """Личное сообщение КАЖДОМУ участнику пары о достигнутом этапе.

    Задача 61: «награды игрокам не видны, только когда достигают уровня — им в
    личку пишут: вы достигли N уровня отношений, получаете столько коинов
    (каждому участнику столько)». Начисление делает love.claim_stage_rewards,
    здесь только уведомление — и второму партнёру тоже, даже если он сейчас
    ничего не писал.
    """
    if not row or not granted:
        return
    partners = []
    for key in ("user_a", "user_b"):
        try:
            uid = int(row.get(key))
        except (TypeError, ValueError):
            continue
        if uid > 0 and uid not in partners:
            partners.append(uid)
    for uid in partners:
        lines = ["💗 <b>Новый этап отношений!</b>", ""]
        for stage in granted:
            lines.append(stage_reached_text(stage, stage.get("reward")))
        lines += ["", "Коины уже начислены на баланс — забирать ничего не нужно.",
                  "Прогресс: <code>любовь инфа</code>"]
        try:
            await bot.send_message(uid, "\n".join(lines), parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001 — игрок мог не писать боту
            logger.info("Не удалось написать о награде за этап игроку %s: %s", uid, exc)


@dp.message(F.text.func(lambda t: rp_action_for(strip_prefix(t))[0] is not None))
async def cmd_rp_action(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    action, meta = rp_action_for(strip_prefix(message_text(message)))
    if not action:
        return
    actor_id = message.from_user.id
    target_id, target_name = await resolve_partner_target(message)
    row = await get_relationship(actor_id)

    if not row:
        return await solo_rp_action(message, meta, target_id, target_name)

    partner_id = _partner_of(row, actor_id)
    if target_id and target_id != partner_id:
        return await message.answer("💔 Это не твой партнёр — РП-команды работают только для пары.")

    # Задача 53: раньше здесь было «… обнимает партнёра» — без ника и без
    # ссылки, поэтому в чате выглядело как обращение в пустоту. Теперь второй
    # участник подписан своим ником (и остаётся кликабельным).
    partner_name = await _display_name(message, partner_id, target_name)
    text = (f"{meta['emoji']} <a href=\"tg://user?id={actor_id}\">"
            f"{safe_text(message.from_user.full_name)}</a> {meta['text']} "
            f"{_player_mention(partner_id, partner_name)}")

    # Задача 76: КД один на игрока и общий для всех РП — после «обнять»
    # остальные команды тоже ждут 5 часов (это и просил заказчик).
    # Задача 64: проверка и начисление идут под блокировкой (игрок + команда).
    # Без неё два сообщения «обнять» подряд проходили проверку одновременно, и
    # награда начислялась дважды — на скрине видно 10 → 20 → 30 → 40 → 50
    # в одну минуту.
    async with love_action_lock(actor_id, action):
        left = await love_cooldown_left(actor_id, action)
        if left > 0:
            # Задача 68: обычный кулдаун и ничего больше. Заказчик прямо убрал
            # «докупить за i¢» и кнопку под сообщением — игрок просто ждёт.
            return await message.answer(
                f"{text}\n\n⏳ {_cooldown_wait_text(action, left)}",
                parse_mode="HTML")

        # Отметку ставим ДО начисления: пока идёт запись в базу, второй заход
        # уже увидит кулдаун и не выдаст награду повторно.
        await love_cooldown_touch(actor_id, action)
        new_score, granted, saved = await add_love_points(row, LOVE_ACTION_BONUS)
        if not saved:
            # Задача 55: не рисуем «+10», если база не приняла запись — иначе
            # игрок видит рост в ответе и ноль в «любовь инфа». Отметку КД
            # снимаем: награды не было, значит и ждать нечего.
            await love_cooldown_clear(actor_id, action)
            return await message.answer(
                f"{text}\n\n⚠️ Love не записался в базу — бонус не начислен.\n"
                f"{love_write_hint()}", parse_mode="HTML")
    # Задача 72: текст ответа — как просил заказчик:
    #     🫂 Broker обнимает
    #     +10 Love❤️
    #     ➡️ До уровня 2 — 10/50❤️
    reply = (f"{text}\n\n+{LOVE_ACTION_BONUS} Love❤️\n"
             f"{safe_text(action_progress_text(new_score))}")
    # Задача 61: награда за этап выдаётся ВСЕГДА и сразу, но игрок должен видеть,
    # за что пришли коины — и в чате, и личным сообщением каждому участнику.
    for stage in granted:
        reply += "\n" + safe_text(stage_reached_text(stage, stage.get("reward")))
    if granted:
        reply += "\n<i>Награда уже зачислена каждому участнику.</i>"
        await _notify_stage_rewards(row, actor_id, granted)
    await message.answer(reply, parse_mode="HTML")


async def solo_rp_action(message: Message, meta: dict, target_id, target_name):
    """РП-действие без пары: Unlove копится только автору.

    «Любовные моменты передавать невозможно»: получатель (если его упомянули)
    не получает ни Love, ни Unlove — награда есть только у того, кто пишет.
    """
    actor_id = message.from_user.id
    text = (f"{meta['emoji']} <a href=\"tg://user?id={actor_id}\">"
            f"{safe_text(message.from_user.full_name)}</a> {meta['text']}")
    if target_id and target_id != actor_id:
        # Задача 53: ник вместо безымянного «игрока».
        text += f" {_player_mention(target_id, await _display_name(message, target_id, target_name))}"
    # Задача 37: убрали длинное объяснение «Пока один(а) — копится Unlove…» —
    # оно занимало пол-сообщения и повторялось на каждое действие. Счёт и так
    # виден в ответе («+10 Unlove (всего N)»).

    action_key = rp_action_for(strip_prefix(message_text(message)))[0]
    # Задача 64: та же блокировка, что и в парной ветке, — без неё два «обнять»
    # подряд без пары начисляли награду дважды (скрин заказчика: 10 → 20 → 30…).
    async with love_action_lock(actor_id, action_key):
        left = await love_cooldown_left(actor_id, action_key)
        if left > 0:
            return await message.answer(
                f"{text}\n\n⏳ {_cooldown_wait_text(action_key, left, SOLO_CURRENCY)}",
                parse_mode="HTML")

        await love_cooldown_touch(actor_id, action_key)
        score, granted, saved = await add_solo_points(actor_id, LOVE_ACTION_BONUS)
        if not saved:
            await love_cooldown_clear(actor_id, action_key)
            return await message.answer(
                f"{text}\n\n⚠️ {SOLO_CURRENCY} не записался в базу — бонус не начислен.\n"
                f"{love_write_hint()}", parse_mode="HTML")
    # Задача 72: «+10 Unlove💔 / ➡️ До уровня 2 — 10/40💔» — без слова «всего».
    reply = (f"{text}\n\n+{LOVE_ACTION_BONUS} {SOLO_CURRENCY}💔\n"
             f"{safe_text(action_progress_text(score, solo=True))}")
    # Одиночке награда тоже выдаётся всегда — говорим об этом прямо.
    for stage in granted:
        reply += "\n" + safe_text(stage_reached_text(stage, stage.get("reward")))
    if granted:
        reply += "\n<i>Награда уже зачислена.</i>"
    await message.answer(reply, parse_mode="HTML")


@dp.callback_query(F.data.startswith("love_ice|"))
async def cb_love_ice(callback: CallbackQuery):
    """Задача 68: покупки ускорения больше нет — кнопка из старых сообщений молчит.

    Заказчик убрал и текст «выполнить за 3 i¢», и кнопку под сообщением: обычное
    КД, просто ждёшь. Игрок мог нажать старую кнопку в истории чата, поэтому
    обработчик не удаляем совсем, а честно отвечаем «уже не работает» и ничего
    не списываем. Раньше здесь были списание i¢ и выдача Love в обход кулдауна.
    """
    try:
        await callback.answer("Это уже не работает — просто дождись конца кулдауна 🙂",
                              show_alert=True)
    except Exception:  # noqa: BLE001 — протухший callback не должен ломать поллинг
        pass


# ================= АДМИН-ПАНЕЛЬ (/admin) =================
# Скрытая команда: работает только для ADMIN_IDS. В общую справку `помощь`
# она не попадает — админы узнают о ней из `адм помощь`.
UPGRADE_ITEM_PRICES_BOT = {"capsule": 100, "uniq_capsule": 500}


def _admin_stats_sync():
    """Статистика для панели. Любая недоступная таблица — просто ноль."""
    data = {"users": 0, "coins": 0, "spins": 0, "bets": 0, "paid": 0,
            "crash_rounds": 0, "pairs": 0, "referrals": 0, "solo": 0}
    try:
        users = supabase.table("users").select("user_id, coins").execute().data or []
        data["users"] = len(users)
        data["coins"] = sum(int(row.get("coins") or 0) for row in users)
    except Exception:
        pass
    try:
        log = supabase.table("upgrade_log").select("amount_from, win_amount, item_from").execute().data or []
        data["spins"] = len(log)
        data["bets"] = sum(
            int(row.get("amount_from") or 0) * (1 if row.get("item_from") == "coin"
                                                else UPGRADE_ITEM_PRICES_BOT.get(row.get("item_from"), 1))
            for row in log)
        data["paid"] = sum(int(row.get("win_amount") or 0) for row in log)
    except Exception:
        pass
    try:
        data["crash_rounds"] = len(supabase.table("crash_history").select("round_id").execute().data or [])
    except Exception:
        pass
    try:
        data["pairs"] = len(supabase.table("relationships").select("id").eq("status", "active").execute().data or [])
    except Exception:
        pass
    try:
        data["solo"] = len(supabase.table("solo_love").select("user_id").execute().data or [])
    except Exception:
        pass
    try:
        data["referrals"] = len(supabase.table("referrals").select("referred_id").execute().data or [])
    except Exception:
        pass
    return data


def _admin_keyboard():
    """Панель без «Шансов апгрейда»: шанс апгрейда фиксирован (90 / X).

    Кнопка-крутилка жила здесь с задачи 25 и позволяла множителем изменить
    реальный шанс. Игроку при этом показывался честный процент, а играли по
    другому — то есть одна кнопка могла молча сломать экономику. Теперь шанс
    один и тот же на сервере и на экране, крутить его нечем.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm|stats")],
        [InlineKeyboardButton(text="💰 Выдать коины", callback_data="adm|give"),
         InlineKeyboardButton(text="🚫 Бан", callback_data="adm|ban")],
    ])


@dp.message(F.text.func(lambda t: strip_prefix(t).lower() == "admin"
                        or strip_prefix(t).lower().startswith("admin ")))
async def cmd_admin_panel(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        # Не админ — молчим: команда скрытая, палить её незачем.
        return
    stats = await asyncio.to_thread(_admin_stats_sync)
    text = (
        "👑 <b>АДМИН-ПАНЕЛЬ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Игроков: <b>{stats['users']}</b>\n"
        f"💰 Коинов в системе: <b>{stats['coins']:,}</b>\n"
        f"🛠️ Апгрейдов: <b>{stats['spins']}</b> (поставлено {stats['bets']:,}, выплачено {stats['paid']:,})\n"
        f"🚀 Раундов Ракеты: <b>{stats['crash_rounds']}</b>\n"
        f"💗 Пар: <b>{stats['pairs']}</b> · 💔 Одиноких: <b>{stats['solo']}</b> · "
        f"👥 Рефералов: <b>{stats['referrals']}</b>\n\n"
        "🎛 Шанс апгрейда зафиксирован: <b>90 / X</b> (x1.75 → 51.43%). "
        "Крутить его нечем — что видит игрок, по тому и играем.\n\n"
        "Команды: <code>+пополнить карман @user 500</code>, <code>!апбан @user причина</code>, "
        "<code>разбан @user</code>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_admin_keyboard())


@dp.callback_query(F.data.startswith("adm|"))
async def cb_admin_panel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Только для администраторов", show_alert=True)
    parts = (callback.data or "").split("|")
    action = parts[1] if len(parts) > 1 else ""

    if action == "stats":
        stats = await asyncio.to_thread(_admin_stats_sync)
        await callback.message.answer(
            "📊 <b>СТАТИСТИКА</b>\n"
            f"Игроков: <b>{stats['users']}</b>\n"
            f"Коинов в системе: <b>{stats['coins']:,}</b>\n"
            f"Апгрейдов: <b>{stats['spins']}</b>\n"
            f"Поставлено (в коинах): <b>{stats['bets']:,}</b>\n"
            f"Выплачено (в коинах): <b>{stats['paid']:,}</b>\n"
            f"Раундов Ракеты: <b>{stats['crash_rounds']}</b>\n"
            f"Пар: <b>{stats['pairs']}</b> · Рефералов: <b>{stats['referrals']}</b>",
            parse_mode="HTML")
        return await callback.answer()

    if action in ("settings", "set"):
        # Старые сообщения с кнопкой «Шансы апгрейда» могли остаться в чате —
        # отвечаем честно, а не молчим.
        return await callback.answer(
            "Шанс апгрейда фиксирован: 90 / X, менять его нечем", show_alert=True)

    if action == "give":
        await callback.message.answer(
            "💰 <b>Выдать коины</b>\n\n"
            "Команда: <code>+пополнить карман @username 500</code>\n"
            "Списать: <code>забрать @username 100</code>\n"
            "Ключи: <code>выдать ключ @username 5</code>", parse_mode="HTML")
        return await callback.answer()

    if action == "ban":
        await callback.message.answer(
            "🚫 <b>Баны</b>\n\n"
            "Забанить: <code>!апбан @username причина</code>\n"
            "Разбанить: <code>разбан @username</code>\n"
            "Список: <code>список банов</code>\n\n"
            "🚫 Игнор в чате (5 ч): <code>!снять игнор @username</code> — "
            "снять досрочно, <code>!аурапредлист</code> — кто наказан",
            parse_mode="HTML")
        return await callback.answer()

    await callback.answer()


# ================= TELEGRAM STARS =================
# Оплата приходит в бот: pre_checkout_query подтверждает заказ, а successful_payment
# начисляет коины. Ключ идемпотентности — telegram_payment_charge_id, поэтому
# повторный апдейт от Telegram не начислит деньги дважды.
# Задача 70: прайс заказчика + бонус за крупный донат (см. api.STARS_PACKAGES —
# там же витрина; здесь нужно только «сколько коинов придёт» по payload).
STARS_PACKAGES = {
    "stars_1": 15, "stars_5": 100, "stars_10": 210, "stars_25": 550,
    "stars_50": 1150 + 150, "stars_100": 2400 + 400, "stars_250": 6250 + 1000,
    "stars_500": 13000 + 2500, "stars_1000": 27500 + 5000,
    # Старые инвойсы (могли остаться в переписке игроков) — по тем же ценам.
    "coins_1000": 100, "coins_5000": 550, "coins_15000": 1150 + 150,
}
STARS_PAYMENTS_TABLE = "stars_payments"


# Комиссия: сколько коинов за столько звёзд — на случай незнакомого payload.
STARS_COINS_BY_STARS = {1: 15, 5: 100, 10: 210, 25: 550, 50: 1150 + 150,
                        100: 2400 + 400, 250: 6250 + 1000, 500: 13000 + 2500,
                        1000: 27500 + 5000}


def _stars_coins_by_amount_sync(stars: int) -> int:
    """Сколько коинов начислить за N звёзд (бонус уже внутри)."""
    try:
        stars = int(stars or 0)
    except (TypeError, ValueError):
        return 0
    if stars in STARS_COINS_BY_STARS:
        return STARS_COINS_BY_STARS[stars]
    # Нестандартная сумма: берём ближайший меньший пакет и считаем по его курсу.
    known = sorted(STARS_COINS_BY_STARS)
    for value in reversed(known):
        if stars >= value:
            rate = STARS_COINS_BY_STARS[value] / value
            return int(round(stars * rate))
    return 0


def _credit_stars_payment_sync(charge_id: str, user_id: int, coins: int, stars: int) -> bool:
    try:
        existing = (supabase.table(STARS_PAYMENTS_TABLE).select("charge_id")
                    .eq("charge_id", charge_id).execute())
        if existing.data:
            return False
        supabase.table(STARS_PAYMENTS_TABLE).insert({
            "charge_id": charge_id, "user_id": int(user_id),
            "coins": int(coins), "stars": int(stars),
        }).execute()
    except Exception as e:
        logger.error(f"stars_payments недоступна: {e}")
        return False
    try:
        res = supabase.table("users").select("coins").eq("user_id", int(user_id)).execute()
        if not res.data:
            return False
        _row = _primary_user_row(res.data)
        new_coins = (int(_row.get("coins") or 0) if _row else 0) + int(coins)
        supabase.table("users").update({"coins": new_coins}).eq("user_id", int(user_id)).execute()
        return True
    except Exception as e:
        logger.error(f"Не удалось начислить коины за Stars: {e}")
        return False


@dp.pre_checkout_query()
async def on_pre_checkout(query):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    payment = message.successful_payment
    if not payment:
        return
    payload = (payment.invoice_payload or "").split(":")
    coins = STARS_PACKAGES.get(payload[1], 0) if len(payload) >= 2 else 0
    # Если id незнакомый (новый прайс без обновления бота) — считаем по количеству
    # звёзд: так оплата никогда не останется без начисления.
    if coins <= 0:
        stars_paid = int(payment.total_amount or 0)
        coins = _stars_coins_by_amount_sync(stars_paid)
    actor = message_actor(message)
    user_id = (int(payload[2]) if len(payload) >= 3 and payload[2].isdigit()
               else getattr(actor, "id", None))
    if user_id is None:
        logger.warning("Оплата Stars без отправителя — начислять некому (payload=%s)", payment.invoice_payload)
        return
    if coins <= 0:
        return
    ok = await asyncio.to_thread(_credit_stars_payment_sync, payment.telegram_payment_charge_id,
                                 user_id, coins, int(payment.total_amount or 0))
    if ok:
        await message.answer(f"⭐ Оплата получена! Начислено <b>{coins:,}</b> коинов. Спасибо!",
                             parse_mode="HTML")
    else:
        await message.answer("⭐ Оплата получена, но начисление задерживается. "
                             "Напиши администратору — платёж сохранён.")


def _stars_stats_report_sync() -> str:
    """Сколько куплено за звёзды: 24 ч / неделя / месяц / всё время (задача 70)."""
    try:
        rows = (supabase.table(STARS_PAYMENTS_TABLE)
                .select("stars, coins, created_at").execute().data or [])
    except Exception as exc:  # noqa: BLE001
        return f"• недоступно: {type(exc).__name__}: {exc}\n"
    now = time.time()
    edges = (("24 ч", now - 86400), ("неделя", now - 7 * 86400), ("месяц", now - 30 * 86400))
    lines = []
    for label, edge in edges:
        count = stars = coins = 0
        for row in rows:
            created = row.get("created_at")
            try:
                from datetime import datetime, timezone
                stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                ts = stamp.timestamp()
            except Exception:
                ts = 0
            if ts >= edge:
                count += 1
                stars += int(row.get("stars") or 0)
                coins += int(row.get("coins") or 0)
        lines.append(f"• {label}: <b>{count}</b> покупок · ⭐ {stars:,} · коинов {coins:,}")
    total_stars = sum(int(row.get("stars") or 0) for row in rows)
    total_coins = sum(int(row.get("coins") or 0) for row in rows)
    lines.append(f"• всё время: <b>{len(rows)}</b> покупок · ⭐ {total_stars:,} · коинов {total_coins:,}")
    return "\n".join(lines) + "\n"


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
        total_coins = sum(u.get("coins", 0) or 0 for u in users)

        # ===== Сколько каких капсул купили (задача 17) =====
        # Считаем по таблице shop_purchases через shop_purchase_stats(); если
        # миграции нет — прямо говорим об этом, а не показываем нули.
        stats = await asyncio.to_thread(shop_purchase_stats, supabase)
        text = (
            "📊 <b>СТАТИСТИКА БОТА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Всего пользователей: <b>{total_users}</b>\n"
            f"🔥 Активных за 24ч: <b>{active_24h}</b>\n"
            f"💰 Всего коинов в системе: <b>{total_coins:,}</b>\n\n"
            "🛒 <b>КУПЛЕНО В МАГАЗИНЕ</b>\n"
            + shop_stats_report(users, stats)
            + "\n⭐ <b>ДОНАТЫ (Telegram Stars)</b>\n"
            + await asyncio.to_thread(_stars_stats_report_sync)
        )
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {e}")
        await message.answer(f"❌ Ошибка при получении статистики: {e}")


# ================= СХЕМА БАЗЫ =================
@dp.message(F.text.func(lambda t: strip_prefix(t).lower() in (
        "бд", "база", "миграции", "проверь базу", "схема базы")))
async def cmd_db_schema(message: Message):
    """Админ видит, какие миграции реально применены и что доделать.

    Появилось после жалобы «миграцию применил, а Mini App пишет, что нет
    колонки created_at»: старая ошибка всегда называла users, даже когда
    колонки не хватало в другой таблице. Теперь есть одна команда-диагностика.
    """
    if not is_admin(message.from_user.id):
        return await message.answer("❌ Эта команда доступна только администраторам бота.")
    if message.chat.type != "private":
        return await message.answer("❌ Эта команда работает только в личных сообщениях с ботом.")
    try:
        report = await asyncio.to_thread(db_schema_audit, supabase)
    except Exception as e:  # noqa: BLE001 — админ должен увидеть причину
        logger.error(f"Аудит схемы базы не удался: {e}")
        return await message.answer(f"❌ Не удалось проверить базу: {e}")
    report_text = db_schema_report(report)
    await message.answer(report_text, parse_mode="HTML")


# ================= СТАРТ И КНОПКА MINI APP =================
# Проблема (задача 41): игрок открывал бота — внизу экрана синяя кнопка
# «AURA GAME», нажимал её и получал белый экран «Not Found». Причина в том,
# что кнопку меню в BotFather настраивают ВРУЧНУЮ, и указанный там адрес
# устаревает (сервис переименовали, Mini App переехал на /miniapp/, старый
# сервис удалён). Теперь адрес кнопки ставит сам бот при старте — из того же
# URL, что и в кнопках сообщений, то есть всегда живой.

# Что бот знает о живости aura-api после resolve_api_url().
# Нужно для `!сервисы` и для строчки в логе: если API лежит, кнопка Mini App
# откроет ошибку — лучше сказать об этом прямо, чем молчать.
API_LIVENESS = {"state": "unknown", "checked": [], "at": 0.0}


def api_liveness() -> dict:
    """Состояние aura-api: alive / down / unknown + какие адреса проверяли."""
    return dict(API_LIVENESS)


def startup_diagnostics() -> str:
    """Одна строка для лога и для админа: куда ведёт кнопка и жив ли API."""
    live = API_LIVENESS.get("state")
    if live == "alive":
        return f"Кнопка Mini App → {miniapp_webapp_url()} (aura-api отвечает)"
    return (f"⚠️ Кнопка Mini App → {miniapp_webapp_url()}, но aura-api НЕ отвечает. "
            f"Проверьте AURA_API_URL (сейчас {API_URL}) и что Web Service aura-api запущен.")


def miniapp_menu_button() -> MenuButtonWebApp:
    """Кнопка «AURA GAME» слева от поля ввода — открывает Mini App на aura-api."""
    return MenuButtonWebApp(text="🎮 AURA GAME", web_app=WebAppInfo(url=miniapp_webapp_url()))


async def register_bot_commands() -> None:
    """Синее меню команд + кнопка Mini App. Ошибки не роняют старт бота."""
    commands = [
        BotCommand(command="start", description="🎮 Открыть AURA GAME"),
        BotCommand(command="help", description="📖 Все команды"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        await bot.set_chat_menu_button(menu_button=miniapp_menu_button())
        logger.info("✅ Кнопка Mini App настроена: %s", miniapp_webapp_url())
    except Exception as exc:  # noqa: BLE001 — настройка не должна мешать polling
        logger.warning("Не удалось настроить меню бота: %s: %s", type(exc).__name__, exc)


def start_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Играть». Без настроенного адреса — не битая web_app, а подсказка."""
    url = miniapp_webapp_url()
    if not url:
        # Адрес не задан (нет AURA_API_URL): Telegram отверг бы пустой web_app.
        # Ведём на прямую ссылку t.me/<бот>/<short_name> — её можно настроить
        # в BotFather → Configure Mini App, и она не зависит от домена Render.
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🎮 Играть", url=miniapp_direct_url()),
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Играть", web_app=WebAppInfo(url=url)),
    ]])


# ================= !АПКА — ССЫЛКА НА MINI APP =================
APP_LINK_KEYBOARD_TEXT = "🎮 Открыть AURA GAME"
# Задача 45: мини-диагностика последней попытки `!апка` — по ней видно, какая
# именно кнопка не понравилась Telegram (показывается админу командой !апка тест).
APP_LINK_LAST = {"error": "", "at": 0.0, "sent": ""}


def app_link_keyboard(allow_web_app: bool = True) -> InlineKeyboardMarkup:
    """Кнопки под сообщением `!апка`.

    Задача 45: в ГРУППАХ Telegram не разрешает inline-кнопку `web_app`
    (BUTTON_TYPE_INVALID), поэтому раньше `!апка` в чате падала с «Что-то пошло
    не так». Теперь набор кнопок ОДИН И ТОТ ЖЕ, а в группе первая кнопка — обычная
    url-ссылка на тот же адрес Mini App (в личке — web_app, чтобы открывалось
    прямо в Telegram). Второй кнопки «Ссылка для друзей» больше нет: игрок
    просил ровно одну кнопку «Открыть AURA GAME».
    """
    url = miniapp_webapp_url()
    if url and allow_web_app:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=APP_LINK_KEYBOARD_TEXT, web_app=WebAppInfo(url=url)),
        ]])
    # Группа: кнопку web_app Telegram запрещает. Ведём прямой ссылкой t.me/<бот>/<short>
    # — она открывает приложение ВНУТРИ Telegram (с initData). Голый https-адрес
    # открылся бы в браузере, где initData нет и игра не авторизуется.
    target = miniapp_direct_url() or url
    if not target:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=APP_LINK_KEYBOARD_TEXT, url=target),
    ]])


def _is_private_chat(message) -> bool:
    """Личка? Именно там разрешены web_app-кнопки."""
    chat = getattr(message, "chat", None)
    return str(getattr(chat, "type", "")) in ("private", "ChatType.PRIVATE")


# Задача 49: прямая ссылка на приложение. Telegram открывает её всегда и
# внутри Telegram (с initData), а не через адрес из BotFather — поэтому она
# работает одинаково в личке и в группе и не зависит от настроек Mini App.
APP_LINK_STARTAPP = (os.getenv("AURA_STARTAPP_URL")
                     or "https://t.me/Auragameplay_bot?startapp").strip()


def app_link_text(plain: bool = False) -> str:
    """Текст сообщения `!апка` — ОДИН и тот же в личке и в группе.

    Задача 44: строку «Адрес: https://…/miniapp/» убрали (игрок просил дважды —
    она осталась в старой сборке на сервисе). Задача 45: в группе текст больше
    не дополняется ссылкой — он совпадал с личкой символ в символ, адрес отдавала
    кнопка. Задача 49: кнопку убрали совсем — в сообщении ровно одна красивая
    ссылка `t.me/<бот>?startapp`, по которой Telegram открывает игру сам.
    """
    if plain:
        # Запасной вариант без parse_mode: Telegram сам сделает ссылку
        # кликабельной, а теги не должны быть видны как текст.
        return (
            "🎮 AURA GAME\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🕹 Здесь всё в одном месте:\n"
            "• 🏒 Арена — одиночная и командная\n"
            "• 🎰 Апгрейд, сундуки, магазин\n"
            "• 💗 Отношения и Love-коины\n"
            "• 💤 Пассивный доход с капсул\n\n"
            f"👉 Открыть AURA GAME: {APP_LINK_STARTAPP}\n\n"
            "Открывай с телефона — Telegram запустит игру прямо внутри себя.\n"
            "Если ссылка не открывается — обнови Telegram до последней версии."
        )
    return (
        "🎮 <b>AURA GAME</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🕹 Здесь всё в одном месте:\n"
        "• 🏒 Арена — одиночная и командная\n"
        "• 🎰 Апгрейд, сундуки, магазин\n"
        "• 💗 Отношения и Love-коины\n"
        "• 💤 Пассивный доход с капсул\n\n"
        f'👉 <a href="{APP_LINK_STARTAPP}">Открыть AURA GAME</a>\n'
        f"<code>{APP_LINK_STARTAPP}</code>\n\n"
        "Открывай с телефона — Telegram запустит игру прямо внутри себя.\n"
        "Если ссылка не открывается — обнови Telegram до последней версии."
    )


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("апка")))
async def cmd_app_link(message: Message):
    """`!апка` — красивое сообщение с прямой ссылкой на приложение.

    Задача 49: НИКАКИХ кнопок. Раньше в личке уходила web_app-кнопка, а в группе
    url-кнопка на `t.me/<бот>/<short>`, и Telegram открывал адрес из BotFather —
    если он устарел, игрок видел «Not Found». Теперь в тексте одна ссылка
    `t.me/<бот>?startapp`: её открывает сам Telegram, и Mini App стартует с
    initData в обоих чатах.

    Если Telegram отклонит разметку (HTML), сообщение всё равно уходит — без
    разметки, но с целой ссылкой (Telegram сам сделает её кликабельной).
    """
    # Задача 48: в `!сервисы` видно, куда вела последняя отправка.
    attempts = (
        ("со ссылкой", app_link_text(), "HTML", dict(disable_web_page_preview=False)),
        ("без разметки", app_link_text(plain=True), None,
         dict(disable_web_page_preview=False)),
        ("совсем без параметров", app_link_text(plain=True), None, {}),
    )
    last_error = None
    for label, text, parse_mode, extra in attempts:
        params = dict(extra)
        try:
            await message.answer(text, parse_mode=parse_mode, **params)
            APP_LINK_LAST.update({"sent": label, "error": "", "at": time.time(),
                                  "private": _is_private_chat(message),
                                  "target": APP_LINK_STARTAPP, "buttons": False})
            return
        except Exception as exc:  # noqa: BLE001 — команда не должна падать
            last_error = exc
            APP_LINK_LAST.update({"sent": "", "at": time.time(),
                                  "error": f"{label}: {type(exc).__name__}: {exc}"[:300]})
            logger.warning("!апка (%s) не отправилась: %s: %s", label, type(exc).__name__, exc)
    raise last_error or RuntimeError("!апка: сообщение не отправлено")


@dp.message(CommandStart())
async def cmd_start(message: Message):
    """`/start` — единственная точка входа, которая должна работать всегда.

    Раньше `/start` не обрабатывался вообще: его молча съедал общий хендлер,
    и игрок не видел ни приветствия, ни кнопки. Адрес Mini App берём из того
    же miniapp_webapp_url(), что и кнопка меню — поэтому кнопка внутри
    сообщения не может вести на удалённый сервис.
    """
    # Задача 60: deep-link `start=ref_<id>` — приглашение. Раньше параметр
    # терялся, и ссылка `t.me/<бот>?start=ref_<id>` не давала ничего, пока
    # приглашённый сам не откроет Mini App. В части клиентов команда приходит
    # как «/start ref_123», в части — как «start ref_123».
    text = message_text(message).strip()
    parts = text.split(maxsplit=1)
    code = _referral_code_from_args(parts[1]) if len(parts) > 1 else ""
    if code and await handle_referral_start(message, code):
        return
    actor = message_actor(message)
    name = safe_text(getattr(actor, "first_name", None) or "друг")
    await message.answer(
        f"👋 <b>Привет, {name}!</b>\n\n"
        f"Это <b>AURA GAME</b> — игры, коины и отношения прямо в Telegram.\n\n"
        f"Жми кнопку ниже, чтобы открыть приложение. Меню бота тоже ведёт в игру.",
        parse_mode="HTML",
        reply_markup=start_keyboard(),
    )


# ================= РЕФЕРАЛЫ В БОТЕ (задача 60) =================
# Ссылка вида `t.me/<бот>?start=ref_<id>` бот раньше не обрабатывал: параметр
# `start` уходил в общий хендлер и терялся, поэтому приглашение по такой ссылке
# не записывалось, пока приглашённый сам не откроет Mini App. Теперь команда
# `/start ref_<id>` (и просто `start ref_<id>`) привязывает реферала прямо в
# боте — через тот же API, чтобы правила награды остались в одном месте.


def _referral_code_from_args(raw_args) -> str:
    """`ref_123` / `123` / `startapp=ref_123` → `ref_123` ('' если не код)."""
    text = str(raw_args or "").strip()
    if not text:
        return ""
    # Приходит "ref_123", "start=ref_123" или "startapp=ref_123" (payload ссылки).
    # Ищем по метке, а не «по первому =»: в подписи Telegram бывают пробелы.
    if "startapp=" in text:
        text = text.split("startapp=", 1)[1]
    elif "start=" in text:
        text = text.split("start=", 1)[1]
    text = text.split("&", 1)[0].strip()
    if text.lower().startswith("ref_"):
        text = text[4:]
    return f"ref_{text}" if text.isdigit() else ""


async def _bind_referral_via_api(user_id: int, init_data: str, code: str) -> dict:
    """Привязка реферала через aura-api (одни правила награды на всех)."""
    if not API_URL:
        return {}
    url = f"{API_URL.rstrip('/')}/api/referral_bind"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={"initData": init_data, "ref_code": code}) as resp:
                return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 — привязка не должна мешать /start
        logger.warning("Реферал в боте: API не ответил (%s)", exc)
        return {}


def _referral_start_init_data(user_id: int, username: str = "", first_name: str = "") -> str:
    """Подписанный initData для запроса от имени игрока.

    api.py проверяет подпись тем же BOT_TOKEN, поэтому собрать данные может
    только сервер — клиенту такое не подделать (в Mini App initData приходит
    от Telegram, здесь его формирует сам бот).
    """
    if not BOT_TOKEN:
        return ""
    user = {"id": int(user_id)}
    if username:
        user["username"] = username
    if first_name:
        user["first_name"] = first_name
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": f"bot{int(time.time())}{user_id % 100000}",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def handle_referral_start(message: Message, code: str) -> bool:
    """Привязать реферала из deep-link. True — если это был код приглашения."""
    if not code:
        return False
    actor = message_actor(message)
    if actor is None:
        return False
    uid = int(actor.id)
    init_data = _referral_start_init_data(uid, getattr(actor, "username", "") or "",
                                         getattr(actor, "first_name", "") or "")
    result = await _bind_referral_via_api(uid, init_data, code) if init_data else {}
    # Сначала «уже учтено»: у такого ответа тоже ok=true, и раньше игроку
    # писали «Приглашение принято» во второй раз — выглядело как новая награда.
    if result.get("already"):
        await message.answer("🎁 Приглашение уже учтено — просто открой игру.",
                             parse_mode="HTML", reply_markup=start_keyboard())
        return True
    if result.get("ok"):
        await message.answer("🎁 <b>Приглашение принято!</b>\n\n"
                             "Жми «Играть» — другу начислятся коины за тебя.",
                             parse_mode="HTML", reply_markup=start_keyboard())
        return True
    # Код битый/свой/старый — не мешаем входу, показываем обычное приветствие.
    return False


@dp.message(Command("help"))
async def cmd_help_command(message: Message):
    """`/help` — тот же текст, что и `!помощь`."""
    return await cmd_help(message)


# ================= ПОМОЩЬ =================
@dp.message(
    F.text.func(lambda t: strip_prefix(t).lower().startswith("помощь") or strip_prefix(t).lower().startswith("help")))
async def cmd_help(message: Message):
    text = (
        "📖 <b>СПИСОК КОМАНД БОТА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n🎟️ <b>Промокоды:</b>\n• <code>промо код</code> — активировать промокод\n\n💰 <b>Экономика:</b>\n• <code>карман</code> — твой баланс\n• <code>карман @user</code> — баланс другого\n• <code>перевод @user сумма</code> — перевести\n• <code>перевод сумма</code> (ответом) — быстрый перевод\n• <code>топ</code> — топ-10 богачей\n• <code>магазин</code> — магазин\n\n🎲 <b>Игры:</b>\n• <code>кубы @user ставка</code> — дуэль в кости\n• <code>кубы ставка</code> (ответом) — быстрый вызов\n\n💗 <b>Отношения:</b>\n• <code>+любовь</code> (ответом на сообщение) — предложить отношения\n• <code>все рп</code> — список всех РП-команд\n• <code>поцеловать</code>, <code>обнять</code>, <code>погладить</code>… — РП-команды (+10 Love паре) — одно действие в 5 часов, кулдаун общий на все команды\n• <code>любовь инфа</code> — с кем, сколько дней, уровень и Love (только своё)\n• уровень растёт по Love пары, награду за новый уровень обоим присылают в личку\n• <code>топ отношений</code> — все пары и их Love (одиночки не показываются)\n• <code>-любовь</code> — разорвать отношения (у обоих сгорает статус и Love)\n• одному РП-команда даёт +10 Unlove — награды только тебе\n\n🛡️ <b>Модерация чата:</b>\n• <code>!аурапред</code> (ответом или @ник) — предупреждение: 3 из 3 → игнор 5 ч в этом чате\n• <code>!аурапредлист</code> — предупреждения в этом чате\n• <code>!снять игнор</code> (ответом, @ник или id) — досрочно снять наказание\n\n• <code>апка</code> — ссылка и кнопка на приложение AURA GAME\n• <code>тег</code> — проверка бота\n• <code>помощь</code> — эта справка")
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text.func(lambda t: strip_prefix(t).lower().startswith("тег")))
async def cmd_tag(message: Message):
    await message.answer(f"👋 Я на месте, {safe_text(message.from_user.full_name)}!")


# ================= СКРЫТЫЕ КОМАНДЫ =================
@dp.message(F.text.func(lambda t: any(strip_prefix(t).lower().startswith(x) for x in
                                      ["all", "стоп", "admins", "админы", "setme", "changeme", "me", "позывной"])))
async def cmd_misc(message: Message):
    clean_text = strip_prefix(message_text(message)).lower()
    if clean_text.startswith("all"):
        if message.chat.type not in ("group", "supergroup"): return await message.answer("❌ Только для групп!")
        if not is_admin(message.from_user.id) and not await is_chat_admin(message.from_user.id,
                                                                          message.chat.id): return await message.answer(
            "❌ Только для админов.")
        if active_summons.get(message.chat.id): return await message.answer("⚠️ Созыв уже идёт!")
        parts = strip_prefix(message_text(message)).split(maxsplit=1)
        call_text = parts[1] if len(parts) > 1 else "📢 Общий сбор!"
        await message.answer("🔄 Начинаю созыв...", parse_mode="HTML")
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
        parts = strip_prefix(message_text(message)).split(maxsplit=1)
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
            emoji = strip_prefix(message_text(message)).split(maxsplit=1)[1].strip()
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


def _classify_miniapp_response(status, body: str) -> dict:
    """Разбор ответа по адресу Mini App: это наша страница игры или «Not Found»."""
    text = body or ""
    is_miniapp = ("AURA GAME" in text and "/miniapp/" in text) or (
        'id="screen-hockey"' in text or "app.js" in text)
    result = {"status": status, "is_miniapp": is_miniapp,
              "ok": status == 200 and is_miniapp, "error": ""}
    if status == 404:
        result["error"] = "404 Not Found — сервиса/пути нет"
    elif status != 200:
        result["error"] = f"HTTP {status}"
    elif not is_miniapp:
        result["error"] = "ответил не Mini App (другой сервис или заглушка)"
    return result


async def check_miniapp_url(timeout: float = 8.0) -> dict:
    """Проверяет адрес, который открывает Telegram по кнопке Mini App.

    Задача 48: в группе inline-кнопка `web_app` запрещена, поэтому `!апка`
    отправляет url-ссылку `t.me/<бот>/<short_name>`, а Telegram открывает то,
    что сохранено в BotFather → Configure Mini App. Если там старый/чужой
    адрес, игрок видит внутри окна приложения голый «Not Found» — и по боту
    это никак не понять. Здесь проверяем ровно тот URL, который уйдёт в кнопку:
      * 200 и HTML с игрой → ✅;
      * 200, но не наша страница → ⚠️ (не тот сервис);
      * 404/другое → ❌ «Not Found» — править адрес в BotFather.
    """
    url = miniapp_webapp_url()
    result = {"url": url, "direct": miniapp_direct_url(), "short_name": MINIAPP_SHORT_NAME,
              "status": None, "ok": False, "error": "", "is_miniapp": False}
    if not url:
        result["error"] = "адрес Mini App не настроен (AURA_API_URL/AURA_WEBAPP_URL)"
        return result
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers={"User-Agent": "aura-app-link/1.0"}) as response:
                body = await response.text(errors="ignore")
                result.update(_classify_miniapp_response(response.status, body))
                result["url"] = url
    except Exception as exc:  # noqa: BLE001 — диагностика не должна падать
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
    return result


async def probe_api(base: str, timeout: float = 8.0) -> bool:
    """Живой ли это aura-api: /api/health отвечает service == "aura-api"."""
    result = await fetch_health(base, timeout)
    data = result.get("data") or {}
    return result.get("status") == 200 and isinstance(data, dict) and data.get("service") == "aura-api"


def admin_probe_init_data(user_id) -> str:
    """initData администратора для собственных диагностических запросов бота.

    Задача 57: служебные ручки aura-api (/api/chest_ready, /api/db_audit,
    /api/admin/*) открыты только по подписи initData — обычной ссылкой их уже
    не вызвать. Бот знает BOT_TOKEN и может подписать данные сам: получается
    тот же формат, что присылает Telegram WebApp, и сервер принимает его без
    дополнительных «секретных» параметров. Подпись живёт минуту и никуда не
    сохраняется.
    """
    uid = int(user_id or 0)
    if not uid or not BOT_TOKEN:
        return ""
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": f"AA{uid}{int(time.time())}",
        "user": json.dumps({"id": uid, "first_name": "admin"}, ensure_ascii=False,
                           separators=(",", ":")),
    }
    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def probe_chest_claims(base: str, timeout: int = 10, init_data: str = "") -> dict:
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
            # Задача 57: диагностика /api/chest_ready закрыта админами, поэтому
            # бот передаёт initData администратора. Без него сервис отвечает 403,
            # и плашка «сундуки обновляются» появлялась бы на ровном месте.
            url = f"{base}/api/chest_ready"
            if init_data:
                url = f"{url}?initData={quote(init_data, safe='')}"
            async with session.get(url) as response:
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
    return {"ok": bool(data.get("ok")), "status": status,
            "detail": str(data.get("detail") or data.get("error") or ""),
            "rpc_ready": bool(data.get("rpc_ready")),
            "fallback": str(data.get("fallback") or ""),
            "error": ""}


async def resolve_api_url() -> str:
    """Проверяет адрес API и при необходимости переключается на рабочий.

    Порядок: адрес из env (если задан) → первый кандидат → остальные.
    Возвращает текущий (уже выправленный) API_URL.
    """
    global API_URL, WEBAPP_URL
    override = (os.getenv("AURA_API_URL") or "").strip().rstrip("/")
    candidates = []
    for base in ([override] if override else []) + [API_URL] + API_URL_CANDIDATES + [RENDER_EXTERNAL_URL]:
        base = (base or "").strip().rstrip("/")
        if base and base not in candidates:
            candidates.append(base)
    if not candidates:
        API_LIVENESS.update({"state": "down", "checked": [], "at": time.time(), "url": "",
                             "error": "АДРЕС API НЕ ЗАДАН"})
        logger.error(
            "❌ Не задан адрес aura-api. В Render → dan4Bot → Environment добавьте "
            "AURA_API_URL = https://<имя сервиса aura-api>.onrender.com (без /miniapp и без порта)."
        )
        return ""

    for base in candidates:
        if await probe_api(base):
            if base != API_URL:
                logger.warning("aura-api найден по адресу %s (в настройках был %s) — переключаюсь", base, API_URL)
            API_URL = base
            if not os.getenv("AURA_WEBAPP_URL"):
                WEBAPP_URL = _as_miniapp_url(API_URL)
            API_LIVENESS.update({"state": "alive", "checked": candidates,
                                 "at": time.time(), "url": base})
            logger.info("✅ aura-api: %s | Mini App: %s", API_URL, WEBAPP_URL)
            return API_URL

    API_LIVENESS.update({"state": "down", "checked": candidates,
                         "at": time.time(), "url": API_URL})
    logger.error(
        "❌ aura-api не найден ни по одному адресу (%s). "
        "Проверьте имя Web Service в Render и переменную AURA_API_URL.",
        ", ".join(candidates),
    )
    return API_URL


# ================= ПРОГРЕВ aura-api =================
# Зачем. Render на бесплатном тарифе усыпляет Web Service через 15 минут без
# ВХОДЯЩИХ запросов. Проснувшемуся сервису нужно ~1 минуту, и всё это время
# вместо игры игрок видит экран Render «SERVICE WAKING UP / START BUILDING ON
# RENDER TODAY». Это не баг кода и не ошибка в приложении — это спящий сервис.
#
# Лечение: пока бот жив, он раз в KEEPWARM_INTERVAL секунд дёргает дешёвый
# /api/ping (НЕ /api/health: тот читает тираж капсул из Supabase). Тогда
# aura-api не успевает уснуть.
#
# ВАЖНО про лимиты Render: по умолчанию дефолтный прогрев ВЫКЛЮЧЕН, потому что
# у бесплатного воркспейса 750 инстанс-часов в месяц, а два всегда-включённых
# сервиса — это ~1460 часов. Включать так:
#   KEEPWARM=1              — прогрев aura-api из бота (240 с по умолчанию)
#   KEEPWARM_INTERVAL=240   — как часто пинговать, секунд
#   KEEPWARM_SELF=1         — дополнительно пинговать самого бота по
#                             RENDER_EXTERNAL_URL (держит бота живым);
#                             вместе с KEEPWARM=1 это ~1460 ч/мес > 750 ч.
def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


KEEPWARM = _env_flag("KEEPWARM", False)
KEEPWARM_SELF = _env_flag("KEEPWARM_SELF", False)
# «Просыпание по активности» — включено по умолчанию и почти ничего не стоит:
# как только кто-то пишет боту (или жмёт кнопку), бот дёргает /api/ping, и
# aura-api начинает просыпаться ДО того, как игрок откроет Mini App. Если игрок
# неактивен — пингов нет, часы Render не тратятся. KEEPWARM_ON_ACTIVITY=0 выключает.
KEEPWARM_ON_ACTIVITY = _env_flag("KEEPWARM_ON_ACTIVITY", True)
try:
    KEEPWARM_INTERVAL = max(60, int(os.getenv("KEEPWARM_INTERVAL") or 240))
except ValueError:
    KEEPWARM_INTERVAL = 240
# Пинг самого себя реже: цель — входящий запрос, а не точность.
KEEPWARM_SELF_INTERVAL = max(60, KEEPWARM_INTERVAL * 2)

# Состояние прогрева для `!сервисы`: по нему видно, реально ли идут пинги.
keepwarm_state = {
    "enabled": KEEPWARM or KEEPWARM_SELF or KEEPWARM_ON_ACTIVITY,
    "interval": KEEPWARM_INTERVAL,
    "started_at": 0.0,
    "last_ping_at": 0.0,     # когда последний раз пинговали aura-api
    "last_ok_at": 0.0,       # когда aura-api последний раз ответил ok
    "last_error": "",        # текст последней ошибки (пусто — всё хорошо)
    "ok_count": 0,
    "fail_count": 0,
    "last_uptime": None,     # uptime_seconds из /api/ping: маленький = только проснулся
}


def self_url() -> str:
    """Публичный адрес этого сервиса: нужен для пинга самого себя.

    Render сам подставляет RENDER_EXTERNAL_URL; можно задать вручную через
    AURA_SELF_URL или AURA_BOT_URL.
    """
    return (os.getenv("AURA_SELF_URL") or os.getenv("AURA_BOT_URL")
            or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")


async def ping_once(url: str, timeout: float = 10.0) -> dict:
    """Один GET. Возвращает {status, data, error, ms} — не бросает исключений."""
    started = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.get(url, headers={"User-Agent": "aura-keepwarm/1.0"}) as response:
                data = None
                try:
                    data = await response.json(content_type=None)
                except Exception:  # noqa: BLE001 — тело может быть не JSON
                    data = None
                return {"status": response.status, "data": data, "error": "",
                        "ms": int((time.monotonic() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 — прогрев не должен падать
        return {"status": None, "data": None, "error": f"{type(exc).__name__}: {exc}"[:200],
                "ms": int((time.monotonic() - started) * 1000)}


async def keep_api_warm():
    """Фоновой задачей: не даёт aura-api уснуть. Молчит, пока всё хорошо."""
    if not (KEEPWARM or KEEPWARM_SELF):
        logger.info("Прогрев aura-api по таймеру выключен (KEEPWARM=1 включает; "
                    "по активности в боте режим %s)", "включён" if KEEPWARM_ON_ACTIVITY else "выключен")
        return
    keepwarm_state["started_at"] = time.time()
    logger.info("🔥 Прогрев включён: aura-api каждые %s с%s", KEEPWARM_INTERVAL,
                ", самопинг " + (self_url() or "не задан") if KEEPWARM_SELF else "")
    was_ok = None
    while True:
        if KEEPWARM:
            url = f"{API_URL.rstrip('/')}/api/ping"
            result = await ping_once(url)
            keepwarm_state["last_ping_at"] = time.time()
            data = result.get("data") or {}
            ok = result.get("status") == 200 and isinstance(data, dict) and data.get("service") == "aura-api"
            if ok:
                keepwarm_state["ok_count"] += 1
                keepwarm_state["last_ok_at"] = time.time()
                keepwarm_state["last_error"] = ""
                uptime = data.get("uptime_seconds")
                keepwarm_state["last_uptime"] = uptime
                if was_ok is not True:
                    logger.info("🔥 aura-api отвечает (%s мс, uptime %s с) — сервис прогрет",
                                result.get("ms"), uptime)
                elif isinstance(uptime, int) and uptime < 200:
                    # Процесс свежий, хотя пинги идут: значит сервис всё-таки спал.
                    logger.warning("🔥 aura-api только что проснулся (uptime %s с) — "
                                   "проверьте, не спит ли сервис между пингами", uptime)
                was_ok = True
            else:
                keepwarm_state["fail_count"] += 1
                keepwarm_state["last_error"] = result.get("error") or f"HTTP {result.get('status')}"
                if was_ok is not False:
                    logger.warning("🔥 aura-api не отвечает на прогрев: %s", keepwarm_state["last_error"])
                was_ok = False
        if KEEPWARM_SELF:
            base = self_url()
            if base:
                await ping_once(f"{base}/health", timeout=8.0)
        await asyncio.sleep(KEEPWARM_INTERVAL)


# Не чаще одного пинга в минуту: десять сообщений подряд не должны дать
# десять запросов к серверу.
WARM_TOUCH_MIN_INTERVAL = 60.0
_warm_touch = {"at": 0.0, "task": None}


def touch_api_warm() -> None:
    """Разбудить aura-api «по требованию»: вызывается на любое действие в боте.

    Не блокирует обработчик: пинг уходит фоновой задачей, ответ игроку не ждёт.
    """
    if not KEEPWARM_ON_ACTIVITY:
        return
    now = time.time()
    if now - _warm_touch["at"] < WARM_TOUCH_MIN_INTERVAL:
        return
    task = _warm_touch.get("task")
    if task is not None and not task.done():
        return
    _warm_touch["at"] = now

    async def _ping():
        result = await ping_once(f"{API_URL.rstrip('/')}/api/ping", timeout=6)
        data = result.get("data") or {}
        keepwarm_state["last_ping_at"] = time.time()
        if result.get("status") == 200 and isinstance(data, dict) and data.get("service") == "aura-api":
            keepwarm_state["last_ok_at"] = time.time()
            keepwarm_state["last_uptime"] = data.get("uptime_seconds")
            keepwarm_state["ok_count"] += 1
            keepwarm_state["last_error"] = ""
        else:
            keepwarm_state["fail_count"] += 1
            keepwarm_state["last_error"] = result.get("error") or f"HTTP {result.get('status')}"

    try:
        _warm_touch["task"] = asyncio.create_task(_ping())
    except RuntimeError:      # нет активного event loop (например, в тестах)
        _warm_touch["at"] = 0.0


class WarmApiMiddleware(BaseMiddleware):
    """Любое обновление от Telegram = повод разбудить aura-api.

    Именно так игрок не видит экран Render «SERVICE WAKING UP»: к моменту тапа
    по кнопке Mini App сервис уже просыпается (или уже проснулся).
    """

    async def __call__(self, handler, event, data):
        try:
            touch_api_warm()
        except Exception:  # noqa: BLE001 — прогрев не должен ломать обработку
            logger.exception("Прогрев по активности не сработал")
        return await handler(event, data)


def keepwarm_modes() -> str:
    """Что именно сейчас держит aura-api тёплым — одной строкой."""
    modes = []
    if KEEPWARM_ON_ACTIVITY:
        modes.append("по активности в боте")
    if KEEPWARM:
        modes.append(f"таймер {KEEPWARM_INTERVAL} с")
    if KEEPWARM_SELF:
        modes.append("самопинг бота")
    return ", ".join(modes)


def app_link_line() -> str:
    """Строка для `!сервисы`: чем закончилась последняя `!апка`.

    Задача 45: если у игрока в группе снова «Что-то пошло не так», по этой
    строке видно, на чём именно споткнулся Telegram (кнопка / разметка / сеть).
    """
    error = APP_LINK_LAST.get("error") or ""
    if not error:
        sent = APP_LINK_LAST.get("sent") or "ещё не вызывали"
        target = APP_LINK_LAST.get("target") or miniapp_webapp_url() or "не задан"
        where = "в личке" if APP_LINK_LAST.get("private") else "в чате"
        return f"🎮 !апка: последняя отправка — {sent} {where} (ссылка: {target})"
    return f"🎮 !апка ОШИБКА: {error}"


def keepwarm_line() -> str:
    """Строка для `!сервисы`: чем греем aura-api и когда был последний ответ."""
    modes = keepwarm_modes()
    if not modes:
        return ("♻️ Прогрев: выключен (Render усыпляет aura-api через 15 минут без запросов —"
                " игрок увидит экран «SERVICE WAKING UP»; включить: KEEPWARM=1)")
    last_ok = keepwarm_state["last_ok_at"]
    if not last_ok:
        return f"♻️ Прогрев: {modes}, ответа от aura-api ещё не было"
    ago = int(time.time() - last_ok)
    uptime = keepwarm_state["last_uptime"]
    tail = f", uptime {uptime} с" if isinstance(uptime, int) else ""
    error = f" (последняя ошибка: {keepwarm_state['last_error']})" if keepwarm_state["last_error"] else ""
    return f"♻️ Прогрев: ✅ aura-api отвечал {ago} с назад{tail} — {modes}{error}"


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

    # Задача 38: Render проверяет сервис запросом HEAD / — маршруты регистрируем
    # на оба метода (GET и HEAD), иначе health-check получал 405.
    app.router.add_route('GET', '/', index)
    app.router.add_route('HEAD', '/', index)
    app.router.add_route('GET', '/health', health)
    app.router.add_route('HEAD', '/health', health)
    app.router.add_route('GET', '/api/ping', health)
    app.router.add_route('HEAD', '/api/ping', health)
    app.router.add_route('GET', '/{tail:.*}', anything_else)
    app.router.add_route('HEAD', '/{tail:.*}', anything_else)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()


async def get_me_with_retry(attempts: int = 8, delay: float = 3.0,
                            bot_instance=None):
    """get_me() с повторами: сетевой сбой Telegram не должен убивать процесс.

    Render любой выход процесса на старте показывает как «Application exited
    early» и перезапускает сервис. Один ConnectionResetError/таймаут при
    первом запросе к Telegram раньше ронял бота в цикл перезапусков, хотя
    достаточно было подождать пару секунд. Неверный токен не ретраим — это
    ошибка настройки, и ждать её исправления бессмысленно.
    """
    client = bot_instance or bot
    attempts = max(1, int(attempts))
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await client.get_me()
        except TelegramUnauthorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - сеть, DNS, таймауты, 5xx
            last_error = exc
            logger.warning("get_me: попытка %s/%s не удалась (%s: %s)",
                           attempt, attempts, type(exc).__name__, exc)
            if attempt < attempts:
                await asyncio.sleep(min(30.0, max(1.0, delay * attempt)))
    logger.error("Не удалось связаться с Telegram за %s попыток", attempts)
    raise last_error


async def main():
    global BOT_USERNAME
    # Порт открываем ПЕРВЫМ: Render убивает Web Service без слушающего порта,
    # а Telegram может отвечать с задержкой.
    await start_fake_website()
    await resolve_api_url()          # выправляем адрес aura-api до первого запроса
    me = await get_me_with_retry()
    if me.username:
        # Настоящее имя бота: из него строится прямая ссылка на Mini App,
        # поэтому ссылка не зависит от устаревшей константы в коде.
        BOT_USERNAME = me.username
    logger.info(f"🤖 Бот: @{me.username} | Mini App: {WEBAPP_URL}")
    logger.info(f"🔗 Прямая ссылка Mini App: {miniapp_direct_url()}")
    # Кнопка «AURA GAME» в меню — программно, а не руками в BotFather:
    # адрес в BotFather устаревает и даёт «Not Found» (задача 41).
    await register_bot_commands()
    logger.info("ℹ️ %s", startup_diagnostics())
    # Прогрев aura-api: без него бесплатный Render усыпляет сервис через 15 минут
    # и игрок вместо игры видит экран «SERVICE WAKING UP».
    # 1) по активности — любое действие в боте будит API (включено всегда);
    # 2) по таймеру — только если включили KEEPWARM=1 (тратит часы Render).
    logger.info(f"⏳ Кулдаун награды за РП: {format_love_cooldown()} — один на игрока, "
                f"общий для всех команд (задача 76, не настраивается)")
    # Задача 44: игнор наказанных в чате — до всех хендлеров сообщений.
    dp.message.outer_middleware(ChatWarnMiddleware())
    # Прогрев aura-api по активности в боте (был и раньше — не теряем).
    dp.update.outer_middleware(WarmApiMiddleware())
    asyncio.create_task(keep_api_warm())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())