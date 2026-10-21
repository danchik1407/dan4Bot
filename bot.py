from aiohttp import web
import os
import json
import random
import time
import asyncio
import html as html_module
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ChatMemberStatus

# Безопасная загрузка .env (работает локально, но не ломает сервер Render)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # На Render этой библиотеки может не быть, это нормально

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_ids_str.split(",") if x.strip().isdigit()]

if not BOT_TOKEN:
    print("❌ ОШИБКА: Не найден BOT_TOKEN! Проверь переменные окружения на Render или файл .env")

DATA_FILE = "data.json"
COOLDOWN_SECONDS = 5 * 60 * 60
CHALLENGE_TIMEOUT = 60

CONTEST_MIN_TIME = 5
CONTEST_MAX_TIME = 3600

active_summons = {}
active_challenges = {}
active_contests = {}
contest_setups = {}


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
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
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


def format_duration(seconds: int) -> str:
    m = seconds // 60
    s = seconds % 60
    if m > 0:
        return f"{m} мин {s} сек"
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


# ================= 🎯 ПЕРЕБИВ: КОНКУРС =================

async def contest_timer(chat_id: int):
    """Таймер конкурса - ждёт пока появится лидер, потом отсчитывает."""
    try:
        while chat_id in active_contests and active_contests[chat_id]["active"]:
            contest = active_contests[chat_id]

            # Если лидера ещё нет - просто ждём, таймер не идёт
            if contest.get("leader_id") is None:
                await asyncio.sleep(1.0)
                continue

            # Лидер есть - отсчитываем время
            remaining = contest["end_time"] - time.time()
            logger.info(f"⏱️ Таймер чата {chat_id}: осталось {remaining:.1f} сек")

            if remaining <= 0:
                logger.info(f"⏰ Таймер чата {chat_id} ИСТЁК → вызываю finish_contest")
                await finish_contest(chat_id)
                return
            await asyncio.sleep(min(1.0, remaining))
    except asyncio.CancelledError:
        logger.info(f"❌ Таймер чата {chat_id} отменён (CancelledError)")
    except Exception as e:
        logger.error(f"💥 Ошибка таймера в чате {chat_id}: {e}", exc_info=True)


async def finish_contest(chat_id: int):
    """Завершение конкурса."""
    logger.info(f" finish_contest вызван для чата {chat_id}")

    if chat_id not in active_contests:
        logger.warning(f"⚠️ Чата {chat_id} нет в active_contests")
        return

    contest = active_contests[chat_id]

    if not contest["active"]:
        logger.warning(f"⚠️ Конкурс в чате {chat_id} уже не активен")
        return

    contest["active"] = False
    logger.info(f"🏁 contest['active'] = False")

    # Отменяем таймер
    if contest.get("timer_task") and not contest["timer_task"].done():
        contest["timer_task"].cancel()
        logger.info(f"🏁 Таймер отменён")

    # Удаляем сообщение с таймером (если есть и отличается от стартового)
    timer_msg_id = contest.get("timer_message_id")
    start_msg_id = contest.get("start_message_id")

    if timer_msg_id and timer_msg_id != start_msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=timer_msg_id)
            logger.info(f"🗑️ Сообщение таймера удалено (id={timer_msg_id})")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось удалить сообщение таймера: {e}")

    winner_id = contest.get("leader_id")
    prize = contest.get("prize", "Не указан")
    winner_msg_id = contest.get("winner_message_id")

    logger.info(f" winner_id={winner_id}, prize='{prize}', winner_msg_id={winner_msg_id}")

    # Если никто не участвовал
    if winner_id is None:
        logger.info(f"🏆 Никто не участвовал, отправляю сообщение")
        try:
            await bot.send_message(
                chat_id,
                f"🔴 <b>Перебив завершён.</b>\n\n"
                f"🎁 Приз: {safe_text(prize)}\n\n"
                f" Никто не участвовал в конкурсе.",
                parse_mode="HTML"
            )
            logger.info(f"✅ Сообщение 'никто не участвовал' отправлено")
        except Exception as e:
            logger.error(f"💥 Ошибка отправки: {e}", exc_info=True)
        del active_contests[chat_id]
        logger.info(f"🏁 Конкурс удалён из active_contests")
        return

    winner_username = contest.get("leader_username", f"id{winner_id}")
    winner_mention = f"@{winner_username}" if winner_username and not str(winner_username).startswith(
        "id") else f"id{winner_id}"

    logger.info(f"🏆 winner_mention={winner_mention}")

    # Ссылка на победное сообщение
    link_text = ""
    if winner_msg_id:
        try:
            chat_info = await bot.get_chat(chat_id)
            if chat_info.username:
                link = f"https://t.me/{chat_info.username}/{winner_msg_id}"
            else:
                chat_username = str(chat_info.id).replace("-100", "")
                link = f"https://t.me/c/{chat_username}/{winner_msg_id}"
            link_text = f'\n<a href="{link}">Ссылка на победное сообщение</a>'
        except Exception as e:
            logger.warning(f"⚠️ Не удалось создать ссылку: {e}")
            link_text = "\nСсылка на победное сообщение"

    win_text = (
        f" <b>ПОБЕДИТЕЛЬ: {safe_text(winner_mention)}</b>, удержавший последнее сообщение 🔥\n\n"
        f"🎁 <b>Приз:</b> {safe_text(prize)}"
        f"{link_text}\n\n"
        f"Приз выдаётся вручную администратором."
    )

    logger.info(f"🏆 Отправляю сообщение о победе...")
    try:
        win_msg = await bot.send_message(chat_id, win_text, parse_mode="HTML")
        logger.info(f"✅ Сообщение о победе отправлено (msg_id={win_msg.message_id})")

        # Закрепляем
        try:
            await bot.pin_chat_message(chat_id=chat_id, message_id=win_msg.message_id)
            logger.info(f"📌 Сообщение закреплено")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось закрепить: {e}. Бот должен быть админом чата с правом закрепления.")
    except Exception as e:
        logger.error(f"💥 Ошибка отправки сообщения о победе: {e}", exc_info=True)

    # Финальное сообщение
    try:
        await bot.send_message(chat_id, " <b>Перебив завершён.</b>", parse_mode="HTML")
    except Exception:
        pass

    del active_contests[chat_id]
    logger.info(f"🏁 Конкурс полностью завершён, удалён из active_contests")


async def process_contest_message(message: Message):
    """Обработка сообщения для конкурса 'Перебив'."""
    chat_id = message.chat.id

    if chat_id not in active_contests or not active_contests[chat_id]["active"]:
        return

    contest = active_contests[chat_id]

    if not message.from_user or message.from_user.is_bot:
        return

    if message.text and (message.text.startswith("/") or message.text.startswith("+")):
        return

    if message.from_user.id == contest["leader_id"]:
        return

    if not contest.get("admins_participate", True):
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=message.from_user.id)
            if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                return
        except Exception as e:
            logger.warning(f"Не удалось проверить статус пользователя: {e}")
            return

    old_leader = contest.get("leader_id")

    contest["leader_id"] = message.from_user.id
    contest["leader_username"] = message.from_user.username or f"id{message.from_user.id}"
    contest["end_time"] = time.time() + contest["duration"]
    contest["winner_message_id"] = message.message_id

    # Если это ПЕРВЫЙ лидер - запускаем таймер
    if old_leader is None:
        if contest.get("timer_task") is None or contest["timer_task"].done():
            contest["timer_task"] = asyncio.create_task(contest_timer(chat_id))
            logger.info(f"⏱️ Таймер запущен в чате {chat_id} после первого участника")

    if contest.get("timer_message_id"):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=contest["timer_message_id"])
        except Exception as e:
            logger.warning(f"Не удалось удалить старое сообщение таймера: {e}")

    remaining = int(contest["duration"])
    leader_name = f"@{contest['leader_username']}" if contest['leader_username'] and not str(
        contest['leader_username']).startswith("id") else f"id{contest['leader_id']}"

    timer_text = (
        f"🏆 <b>Конкурс!</b>\n\n"
        f"👑 <b>Лидер:</b> {safe_text(leader_name)}\n"
        f"⏰ <b>До конца:</b> {remaining} сек\n\n"
        f"Чтобы перебить лидера, отправьте любое сообщение "
    )

    try:
        timer_msg = await message.answer(timer_text, parse_mode="HTML")
        contest["timer_message_id"] = timer_msg.message_id
        logger.info(f" Новый лидер в чате {chat_id}: {contest['leader_id']}")
    except Exception as e:
        logger.error(f"Ошибка при отправке сообщения таймера: {e}")


@dp.message(lambda m: m.text and (
        m.text.strip().lower().startswith("+запустить перебив") or
        m.text.strip().lower().startswith("/start_contest") or
        m.text.strip().lower().startswith("/запустить_перебив")
))
async def cmd_start_contest(message: Message):
    """Запуск настройки конкурса 'Перебив'."""
    await try_delete(message)

    if not is_admin(message.from_user.id) and not await is_chat_admin(message.from_user.id, message.chat.id):
        await message.answer("❌ Только администратор может запустить игру.")
        return

    if message.chat.type not in ["group", "supergroup"]:
        await message.answer(" Конкурс можно запустить только в групповом чате.")
        return

    parts = message.text.strip().split()

    cmd = parts[0].lower()
    if cmd in ["/start_contest", "/запустить_перебив"]:
        time_index = 1
    else:
        time_index = 2

    if len(parts) < time_index + 1:
        await message.answer(
            "❌ Неправильный формат.\n\n"
            "💡 Используйте:\n"
            "<code>+запустить перебив 60</code> — без приза\n"
            "<code>+запустить перебив 60 1000 монет</code> — с призом",
            parse_mode="HTML"
        )
        return

    try:
        duration = int(parts[time_index])
    except ValueError:
        await message.answer("❌ Время должно быть числом (в секундах).")
        return

    if duration < CONTEST_MIN_TIME or duration > CONTEST_MAX_TIME:
        await message.answer(f"❌ Время должно быть от {CONTEST_MIN_TIME} до {CONTEST_MAX_TIME} секунд.")
        return

    prize = " ".join(parts[time_index + 1:]) if len(parts) > time_index + 1 else "Не указан"

    if message.chat.id in active_contests and active_contests[message.chat.id]["active"]:
        await message.answer("⚠️ В этом чате уже идёт перебив.")
        return

    if message.chat.id in contest_setups:
        await message.answer("️ В этом чате уже идёт настройка конкурса. Дождитесь её завершения.")
        return

    contest_setups[message.chat.id] = {
        "duration": duration,
        "prize": prize,
        "initiator_id": message.from_user.id
    }

    duration_text = format_duration(duration)

    setup_text = (
        f"🎮 <b>Настройка игры</b>\n\n"
        f"⏱️ <b>Длительность:</b> {duration_text}\n"
        f"🎁 <b>Приз:</b> {safe_text(prize)}\n\n"
        f"Могут ли администраторы участвовать в перебиве?"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, все участвуют", callback_data=f"contest_admins_yes_{message.chat.id}"),
            InlineKeyboardButton(text="❌ Нет, только обычные", callback_data=f"contest_admins_no_{message.chat.id}")
        ]
    ])

    await message.answer(setup_text, reply_markup=keyboard, parse_mode="HTML")


@dp.callback_query(F.data.startswith("contest_admins_"))
async def cb_contest_admins(callback: CallbackQuery):
    """Обработка выбора участия админов."""
    parts = callback.data.split("_")
    admins_participate = parts[2] == "yes"
    chat_id = int(parts[3])

    if not is_admin(callback.from_user.id) and not await is_chat_admin(callback.from_user.id, chat_id):
        try:
            await callback.answer("Только администратор может настроить игру.", show_alert=True)
        except Exception:
            pass
        return

    setup = contest_setups.get(chat_id)
    if not setup:
        try:
            await callback.answer("Параметры конкурса не найдены.", show_alert=True)
        except Exception:
            pass
        return

    try:
        await callback.answer()
    except Exception:
        pass

    # Удаляем сообщение настройки
    try:
        await callback.message.delete()
    except Exception:
        pass

    duration = setup["duration"]
    prize = setup["prize"]

    contest = {
        "chat_id": chat_id,
        "duration": duration,
        "prize": prize,
        "leader_id": None,
        "leader_username": None,
        "timer_message_id": None,
        "winner_message_id": None,
        "end_time": time.time() + duration,
        "admins_participate": admins_participate,
        "active": True,
        "timer_task": None,
        "start_message_id": None
    }

    active_contests[chat_id] = contest
    # Таймер НЕ запускается здесь - он запустится когда появится первый участник

    # Удаляем параметры настройки
    contest_setups.pop(chat_id, None)

    mode_text = "Все (включая администраторов)" if admins_participate else "Только обычные пользователи"
    duration_text = format_duration(duration)

    start_text = (
        f" <b>Игра запущена!</b>\n\n"
        f"⏱ <b>Длительность:</b> {duration_text}\n"
        f"🎁 <b>Приз:</b> {safe_text(prize)}\n"
        f"✅ <b>Задача:</b> продержать своё сообщение самым последним, без перебивов.\n\n"
        f"👥 <b>Участники:</b> {mode_text}\n\n"
        f" <b>Ожидание первого участника...</b>\n"
        f"Отправьте любое сообщение, чтобы стать лидером и запустить таймер!"
    )

    try:
        start_msg = await callback.message.answer(start_text, parse_mode="HTML")
        active_contests[chat_id]["start_message_id"] = start_msg.message_id
        # timer_message_id = None, будет установлен когда появится первый лидер
        logger.info(f" Конкурс запущен в чате {chat_id}. Приз: {prize}")
    except Exception as e:
        logger.error(f"Ошибка при старте конкурса: {e}")


@dp.callback_query(F.data.startswith("contest_stop_"))
async def cb_contest_stop(callback: CallbackQuery):
    """Кнопка 'Завершить перебив'."""
    chat_id = int(callback.data.split("_")[2])

    if chat_id not in active_contests or not active_contests[chat_id]["active"]:
        await callback.answer("Игра не активна в этом чате.", show_alert=True)
        return

    if not is_admin(callback.from_user.id) and not await is_chat_admin(callback.from_user.id, chat_id):
        await callback.answer("Только администратор может завершить игру.", show_alert=True)
        return

    try:
        await callback.answer()
    except Exception:
        pass

    await finish_contest(chat_id)


@dp.callback_query(F.data.startswith("contest_pocket_"))
async def cb_contest_pocket(callback: CallbackQuery):
    """Кнопка '!Посмотреть карман'."""
    user_id = callback.from_user.id

    data = load_data()
    user = get_or_create_user(data, user_id, callback.from_user.username)
    display_name = f"@{callback.from_user.username}" if callback.from_user.username else f"id{user_id}"

    await callback.message.answer(
        f"💰 <b>Карман {safe_text(display_name)}</b>\n\n{user.get('coins', 0)} coin 🪙",
        parse_mode="HTML"
    )
    try:
        await callback.answer()
    except Exception:
        pass


@dp.message(lambda m: m.text and (
        m.text.strip().lower() in ["+завершить перебив", "+остановить перебив"] or
        m.text.strip().lower() in ["/stop_contest", "/остановить_перебив", "завершить перебив"]
))
async def cmd_stop_contest(message: Message):
    """Досрочное завершение конкурса."""
    await try_delete(message)

    if not is_admin(message.from_user.id) and not await is_chat_admin(message.from_user.id, message.chat.id):
        await message.answer("❌ Только администратор может завершить игру.")
        return

    chat_id = message.chat.id

    if chat_id not in active_contests or not active_contests[chat_id]["active"]:
        await message.answer("Игра не активна в этом чате.")
        return

    await finish_contest(chat_id)


@dp.message(lambda m: m.text and m.text.strip().lower() in ["/status", "/статус", "+статус перебива"])
async def cmd_contest_status(message: Message):
    """Показать статус текущего конкурса."""
    await try_delete(message)

    chat_id = message.chat.id

    if chat_id not in active_contests or not active_contests[chat_id]["active"]:
        await message.answer("❌ В этом чате нет активного перебива.")
        return

    contest = active_contests[chat_id]

    if contest.get("leader_id") is None:
        status_text = (
            f"📊 <b>СТАТУС ПЕРЕБИВА</b>\n\n"
            f"⏳ <b>Ожидание первого участника...</b>\n"
            f"🎁 <b>Приз:</b> {safe_text(contest['prize'])}\n\n"
            f"💬 Отправьте сообщение, чтобы стать лидером!"
        )
    else:
        remaining = max(0, int(contest["end_time"] - time.time()))
        leader_name = f"@{contest['leader_username']}" if contest.get('leader_username') and not str(
            contest['leader_username']).startswith("id") else "не определён"

        status_text = (
            f" <b>СТАТУС ПЕРЕБИВА</b>\n\n"
            f" <b>Текущий лидер:</b> {safe_text(leader_name)}\n"
            f"⏰ <b>Осталось:</b> {remaining} сек\n"
            f" <b>Приз:</b> {safe_text(contest['prize'])}\n\n"
            f"💬 Отправьте сообщение, чтобы перебить лидера!"
        )

    await message.answer(status_text, parse_mode="HTML")


# =================  ПОМОЩЬ =================
@dp.message(lambda m: m.text and m.text.strip().lower() in ["!помощь", "!команды", "!help"])
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>СПИСОК КОМАНД БОТА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📦 <b>Сундуки:</b>\n"
        "• <code>!открыть</code> — открыть сундук (КД 5 часов)\n\n"
        "💰 <b>Экономика:</b>\n"
        "• <code>!посмотреть карман</code> — твой баланс\n"
        "• <code>!посмотреть карман @username</code> — баланс другого\n"
        "• <code>!перевод @username сумма</code> — перевести коины\n"
        "• <code>!топ монет</code> — топ-10 богачей\n\n"
        "🎲 <b>Игры:</b>\n"
        "• <code>!кубы @username ставка</code> — дуэль в кости\n"
        "• Ответь на сообщение + <code>!кубы ставка</code> — быстрый вызов\n\n"
        "📢 <b>Созывы:</b>\n"
        "• <code>!all</code> — массовый созыв\n"
        "• <code>!admins</code> — созыв админов\n"
        "• <code>!stop</code> — остановить созыв\n"
        "• <code>!me</code> / <code>!setme эмодзи</code> / <code>!changeme</code> — позывной\n\n"
        "🔧 <b>Системные:</b>\n"
        "• <code>!тег</code> — проверка бота\n"
        "• <code>!помощь</code> — эта справка"
    )
    await message.answer(help_text, parse_mode="HTML")


# =================  СОЗЫВЫ =================
async def summon_all_members(bot, chat_id: int, call_text: str, starter_id: int):
    active_summons[chat_id] = True
    try:
        data = load_data()
        users_to_tag = []
        for uid_str, udata in data["users"].items():
            user_id = int(uid_str)
            if user_id != starter_id:
                users_to_tag.append(user_id)
        random.shuffle(users_to_tag)
        batch_size = 15
        total = len(users_to_tag)
        if total == 0:
            await bot.send_message(chat_id, "❌ Некого созывать. Пользователи должны хотя бы раз написать боту.")
            return
        for i in range(0, total, batch_size):
            if not active_summons.get(chat_id, False):
                await bot.send_message(chat_id, "⏹️ Созыв остановлен.")
                break
            batch = users_to_tag[i:i + batch_size]
            mentions = [f"[Пользователь](tg://user?id={uid})" for uid in batch]
            mention_text = "\n".join(mentions)
            full_text = f" {call_text}\n\n{mention_text}"
            await bot.send_message(chat_id, full_text, parse_mode="Markdown")
            await asyncio.sleep(1.5)
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Ошибка при созыве: {e}")
    finally:
        if chat_id in active_summons:
            del active_summons[chat_id]


@dp.message(lambda m: m.text and (
        m.text.strip().lower().startswith("!all ") or m.text.strip().lower() in ["!all", "!каллалл", "!алл",
                                                                                 "!callall"]))
async def cmd_all(message: Message):
    chat_id = message.chat.id
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("❌ Эта команда работает только в групповых чатах!")
        return
    user_id = message.from_user.id
    is_bot_admin = is_admin(user_id)
    is_chat_admin_user = await is_chat_admin(user_id, chat_id)
    if not is_bot_admin and not is_chat_admin_user:
        await message.answer("❌ команда недоступна (только для админов чата)")
        return
    parts = message.text.strip().split()
    call_text = " ".join(parts[1:]) if len(parts) > 1 else "📢 Общий сбор всех участников!"
    await message.answer(
        f"🔄 Начинаю массовый созыв...\nТекст: {safe_text(call_text)}\n\n⚠️ Для остановки напишите: <code>!stop</code>",
        parse_mode="HTML")
    asyncio.create_task(summon_all_members(bot, chat_id, call_text, user_id))


@dp.message(lambda m: m.text and m.text.strip().lower() in ["!stop", "!стоп", "!остановить", "!отмена", "!стой"])
async def cmd_stop(message: Message):
    chat_id = message.chat.id
    if active_summons.get(chat_id, False):
        active_summons[chat_id] = False
        await message.answer("️ Команда остановки принята. Созыв прекратится после текущего сообщения.")
    else:
        await message.answer("❌ Активных массовых созывов в этом чате нет.")


@dp.message(lambda m: m.text and m.text.strip().lower() in ["!admins", "!калладминс", "!админы"])
async def cmd_admins(message: Message):
    chat_id = message.chat.id
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("❌ Эта команда работает только в групповых чатах!")
        return
    parts = message.text.strip().split()
    call_text = " ".join(parts[1:]) if len(parts) > 1 else "📢 Сбор администраторов!"
    try:
        members = await bot.get_chat_administrators(chat_id=chat_id)
        admins_to_tag = [m.user for m in members if not m.user.is_bot]
        if not admins_to_tag:
            await message.answer("❌ Администраторы не найдены.")
            return
        mentions = [f"[{u.full_name[:20]}](tg://user?id={u.id})" for u in admins_to_tag]
        mention_text = ", ".join(mentions)
        full_text = f"📢 {call_text}\n\n{mention_text}"
        await message.answer(full_text, parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")


# ================= 📛 ПОЗЫВНЫЕ =================
@dp.message(lambda m: m.text and m.text.strip().lower() in ["!me", "!ми", "!позывной"])
async def cmd_me(message: Message):
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


@dp.message(lambda m: m.text and m.text.strip().lower().startswith("!setme"))
async def cmd_setme(message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("❌ Формат: <code>!setme эмодзи</code>\nПример: <code>!setme 🔥</code>", parse_mode="HTML")
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


@dp.message(lambda m: m.text and m.text.strip().lower() in ["!changeme", "!чанджми"])
async def cmd_changeme(message: Message):
    emojis = ["", "⚡", "🐺", "🦊", "🐻", "🦁", "🐯", "", "👻", "🤖", "👽", "🎯", "", "👑", "🎭", "🦅", "🦈", ""]
    emoji = random.choice(emojis)
    data = load_data()
    user = get_or_create_user(data, message.from_user.id, message.from_user.username)
    user["call_sign"] = emoji
    save_data(data)
    await message.answer(f"✅ Твой новый случайный позывной: {emoji}")


# ================= 💸 ПЕРЕВОД =================
@dp.message(lambda m: m.text and m.text.strip().lower().startswith("!перевод"))
async def cmd_transfer(message: Message):
    sender_id = message.from_user.id
    sender_username = message.from_user.username
    data = load_data()
    sender = get_or_create_user(data, sender_id, sender_username)
    target_user, amount = None, None
    if message.reply_to_message:
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
        await message.answer("❌ Сумма > 0 и нельзя себе.")
        return
    if sender["coins"] < amount:
        await message.answer(f"❌ Недостаточно коинов. У тебя: <b>{sender['coins']}</b> 🪙", parse_mode="HTML")
        return
    sender["coins"] -= amount
    target = get_or_create_user(data, target_user.id, target_user.username)
    target["coins"] = target.get("coins", 0) + amount
    save_data(data)
    sender_name = f"@{sender_username}" if sender_username else f"id{sender_id}"
    target_name = f"@{target_user.username}" if target_user.username else f"id{target_user.id}"
    await message.answer(
        f"✅ <b>Перевод выполнен!</b>\n\n👤 От: {safe_text(sender_name)}\n👤 Кому: {safe_text(target_name)}\n💸 Сумма: <b>{amount}</b> coin 🪙",
        parse_mode="HTML")


# ================= 🎲 КУБЫ =================
@dp.message(lambda m: m.text and m.text.strip().lower().startswith("!кубы"))
async def cmd_dice_challenge(message: Message):
    parts = message.text.strip().split()
    target_user, amount = None, None
    if message.reply_to_message:
        target_user = message.reply_to_message.from_user
        if len(parts) < 2:
            await message.answer("🎲 <b>Игра «Кубы»</b>\n\nФормат: <code>!кубы ставка</code> (ответом на сообщение)",
                                 parse_mode="HTML")
            return
        try:
            amount = int(parts[1])
        except ValueError:
            await message.answer("❌ Ставка должна быть числом.")
            return
    else:
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
        if target_user is None:
            await message.answer("❌ Пользователь не найден.")
            return
    if amount <= 0:
        await message.answer("❌ Ставка должна быть больше 0.")
        return
    data = load_data()
    challenger_id = message.from_user.id
    challenger_username = message.from_user.username
    challenger = get_or_create_user(data, challenger_id, challenger_username)
    if target_user.id == challenger_id:
        await message.answer("❌ Нельзя играть сам с собой 😄")
        return
    if challenger["coins"] < amount:
        await message.answer(f"❌ У тебя недостаточно коинов.\nНужно: <b>{amount}</b> ", parse_mode="HTML")
        return
    challenged = get_or_create_user(data, target_user.id, target_user.username)
    if challenged["coins"] < amount:
        await message.answer(f"❌ У {get_name_by_id(data, target_user.id, target_user.username)} недостаточно коинов.",
                             parse_mode="HTML")
        return
    challenge_id = f"{challenger_id}_{target_user.id}_{int(time.time())}"
    active_challenges[challenge_id] = {
        "challenger_id": challenger_id,
        "challenger_username": challenger_username,
        "challenged_id": target_user.id,
        "challenged_username": target_user.username,
        "amount": amount,
        "chat_id": message.chat.id,
        "message_id": None,
        "created_at": time.time()
    }
    challenger_name = get_name_by_id(data, challenger_id, challenger_username)
    challenged_name = get_name_by_id(data, target_user.id, target_user.username)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"dice_accept_{challenge_id}"),
        InlineKeyboardButton(text="❌ Отказ", callback_data=f"dice_decline_{challenge_id}")
    ]])
    sent_msg = await message.answer(
        f"🎲 <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n👤 {challenger_name} вызывает {challenged_name}\n💰 Ставка: <b>{amount}</b> coin 🪙\n🎯 Победитель забирает: <b>{amount * 2}</b> coin 🪙\n\n Время на ответ: {CHALLENGE_TIMEOUT} секунд",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    active_challenges[challenge_id]["message_id"] = sent_msg.message_id
    await asyncio.sleep(CHALLENGE_TIMEOUT)
    if challenge_id in active_challenges:
        try:
            await sent_msg.edit_text(f"🎲 <b>Вызов отменён</b> (время вышло)\n\n⏰ {challenged_name} не успел ответить.",
                                     parse_mode="HTML")
        except Exception:
            pass
        del active_challenges[challenge_id]


@dp.callback_query(F.data.startswith("dice_accept_"))
async def cb_dice_accept(callback: CallbackQuery):
    challenge_id = callback.data.replace("dice_accept_", "")
    if challenge_id not in active_challenges:
        await callback.answer("⏰ Этот вызов уже неактуален.", show_alert=True)
        return
    challenge = active_challenges[challenge_id]
    if callback.from_user.id != challenge["challenged_id"]:
        await callback.answer("❌ Этот вызов не для тебя!", show_alert=True)
        return
    await callback.answer("🎲 Бросаем кубики...")
    data = load_data()
    challenger = get_or_create_user(data, challenge["challenger_id"], challenge.get("challenger_username"))
    challenged = get_or_create_user(data, challenge["challenged_id"], callback.from_user.username)
    amount = challenge["amount"]
    chat_id = challenge["chat_id"]
    challenger["coins"] -= amount
    challenged["coins"] -= amount
    save_data(data)
    challenger_name = get_name_by_id(data, challenge["challenger_id"], challenge.get("challenger_username"))
    challenged_name = get_name_by_id(data, challenge["challenged_id"], callback.from_user.username)
    try:
        await callback.message.edit_text(f"🎲 <b>Дуэль началась!</b>", parse_mode="HTML")
    except Exception:
        pass
    queue_msg = await callback.message.answer(f"🎲 <b>Бросает</b> {challenger_name}...", parse_mode="HTML")
    dice1_msg = await bot.send_dice(chat_id=chat_id, emoji="🎲")
    dice1_value = dice1_msg.dice.value
    await asyncio.sleep(1.5)
    try:
        await queue_msg.edit_text(f"🎲 <b>Теперь бросает</b> {challenged_name}...", parse_mode="HTML")
    except Exception:
        pass
    dice2_msg = await bot.send_dice(chat_id=chat_id, emoji="🎲")
    dice2_value = dice2_msg.dice.value
    await asyncio.sleep(1.5)
    if dice1_value > dice2_value:
        challenger["coins"] += amount * 2
        result_text = f"🎲 <b>РЕЗУЛЬТАТ ДУЭЛИ!</b>\n\n👤 {challenger_name} выбросил: <b>{dice1_value}</b> 🎲\n👤 {challenged_name} выбросил: <b>{dice2_value}</b> \n\n🏆 <b>Победил {challenger_name}!</b>\n💰 Выигрыш: <b>{amount * 2}</b> coin 🪙"
    elif dice2_value > dice1_value:
        challenged["coins"] += amount * 2
        result_text = f"🎲 <b>РЕЗУЛЬТАТ ДУЭЛИ!</b>\n\n {challenger_name} выбросил: <b>{dice1_value}</b> 🎲\n👤 {challenged_name} выбросил: <b>{dice2_value}</b> 🎲\n\n🏆 <b>Победил {challenged_name}!</b>\n💰 Выигрыш: <b>{amount * 2}</b> coin 🪙"
    else:
        challenger["coins"] += amount
        challenged["coins"] += amount
        result_text = f"🎲 <b>РЕЗУЛЬТАТ ДУЭЛИ!</b>\n\n {challenger_name} выбросил: <b>{dice1_value}</b> 🎲\n👤 {challenged_name} выбросил: <b>{dice2_value}</b> 🎲\n\n🤝 <b>Ничья!</b> Ставки возвращены."
    save_data(data)
    del active_challenges[challenge_id]
    try:
        await bot.delete_message(chat_id=chat_id, message_id=dice1_msg.message_id)
    except Exception:
        pass
    try:
        await bot.delete_message(chat_id=chat_id, message_id=dice2_msg.message_id)
    except Exception:
        pass
    try:
        await queue_msg.delete()
    except Exception:
        pass
    await callback.message.answer(result_text, parse_mode="HTML")


@dp.callback_query(F.data.startswith("dice_decline_"))
async def cb_dice_decline(callback: CallbackQuery):
    challenge_id = callback.data.replace("dice_decline_", "")
    if challenge_id not in active_challenges:
        await callback.answer("⏰ Этот вызов уже неактуален.", show_alert=True)
        return
    challenge = active_challenges[challenge_id]
    if callback.from_user.id != challenge["challenged_id"]:
        await callback.answer("❌ Этот вызов не для тебя!", show_alert=True)
        return
    await callback.answer()
    decliner_name = callback.from_user.username or f"id{challenge['challenged_id']}"
    try:
        await callback.message.edit_text(
            f"🎲 <b>Вызов отклонён</b>\n\n❌ @{safe_text(decliner_name)} отказался от дуэли.", parse_mode="HTML")
    except Exception:
        pass
    del active_challenges[challenge_id]


# ================= 🎰 СУНДУКИ =================
@dp.message(lambda m: m.text and m.text.strip().lower() == "!открыть")
async def cmd_open(message: Message):
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
    user["coins"] = user.get("coins", 0) + reward
    user["last_chest"] = now
    save_data(data)
    if is_golden:
        await message.answer(
            f"✨ <b>ЗОЛОТОЙ СУНДУК!</b> 🔑\n\n Невероятная удача!\n🎉 Тебе выпало: <b>{reward}</b> coin 🪙",
            parse_mode="HTML")
    else:
        await message.answer(f"🎁 <b>Сундук открыт!</b>\n\n🎉 Тебе выпало: <b>{reward}</b> coin 🪙", parse_mode="HTML")


@dp.message(lambda m: m.text and m.text.strip().lower().startswith("!посмотреть карман"))
async def cmd_pocket(message: Message):
    data = load_data()
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if not target and len(message.text.strip().split()) >= 3:
        username_arg = message.text.strip().split()[2].lstrip("@").lower()
        for uid_str, udata in data["users"].items():
            if udata.get("username", "").lower() == username_arg:
                target = type('User', (), {'id': int(uid_str), 'username': udata.get("username")})()
                break
    if not target:
        target = message.from_user
    user = get_or_create_user(data, target.id, target.username)
    display_name = f"@{target.username}" if target.username else f"id{target.id}"
    await message.answer(f" <b>Карман {safe_text(display_name)}</b>\n\n{user.get('coins', 0)} coin 🪙",
                         parse_mode="HTML")


@dp.message(lambda m: m.text and m.text.strip().lower() == "!топ монет")
async def cmd_top_coins(message: Message):
    data = load_data()
    users_list = [{"username": udata.get("username", f"id{uid_str}"), "coins": udata.get("coins", 0)}
                  for uid_str, udata in data["users"].items()]
    users_list.sort(key=lambda x: x["coins"], reverse=True)
    top10 = users_list[:10]
    if not top10 or top10[0]["coins"] == 0:
        await message.answer(" Топ пока пуст. Открывай сундуки!")
        return
    msg = " <b>ТОП-10 ПО КОИНАМ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
    medals = ["🥇", "", "🥉"]
    for i, u in enumerate(top10):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = u['username'] if not u['username'].startswith("id") else u['username']
        msg += f"{prefix} {safe_text(name)} — <b>{u['coins']}</b> coin 🪙\n"
    await message.answer(msg, parse_mode="HTML")


# ================= 🔐 АДМИН: ПОПОЛНИТЬ КАРМАН =================
@dp.message(lambda m: m.text and m.text.strip().lower().startswith("+пополнить карман"))
async def cmd_admin_add(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ команда недоступна")
        return
    parts = message.text.strip().split()
    target_user = message.reply_to_message.from_user if message.reply_to_message else None
    amount = None
    if target_user and len(parts) >= 2:
        try:
            amount = int(parts[1])
        except:
            await message.answer("❌ Укажи число.")
            return
    elif len(parts) == 3:
        try:
            amount = int(parts[2])
        except:
            await message.answer("❌ Укажи число.")
            return
    elif len(parts) == 4:
        username_arg = parts[2].lstrip("@").lower()
        try:
            amount = int(parts[3])
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
        await message.answer("❌ Формат: <code>+пополнить карман 500</code> или ответом", parse_mode="HTML")
        return
    if not target_user:
        target_user = message.from_user
    data = load_data()
    user = get_or_create_user(data, target_user.id, target_user.username)
    user["coins"] = user.get("coins", 0) + amount
    save_data(data)
    target_name = get_name_by_id(data, target_user.id, target_user.username)
    await message.answer(
        f"💰 <b>Карман пополнен!</b>\n\n👤 Кому: {target_name}\n➕ Начислено: <b>{amount}</b> coin 🪙\n Баланс: <b>{user['coins']}</b> coin 🪙",
        parse_mode="HTML")


@dp.message(lambda m: m.text and m.text.strip().lower() == "!тег")
async def cmd_tag(message: Message):
    await message.answer(f"Пользователь {safe_text(message.from_user.full_name)} тегнул меня, на месте")


# ============================================
# ⚠️ ОБРАБОТЧИК ПЕРЕБИВА - ДОЛЖЕН БЫТЬ ПОСЛЕДНИМ!
# ============================================
@dp.message()
async def handle_contest(message: Message):
    try:
        await process_contest_message(message)
    except Exception as e:
        logger.error(f"Ошибка в обработчике перебива: {e}", exc_info=True)


async def start_fake_website():
    """Этот код обманывает Render, заставляя его думать, что это сайт"""
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
    print("✅ Бот запущен!")
    print(f"🔐 Админы: {ADMIN_IDS}")
    print("📦 Сундуки, 💰 Экономика, 🎲 Кубы, 📢 Созывы")
    print("🎯 Перебив: +запустить перебив, +завершить перебив, +статус перебива")
    await dp.start_polling(bot)


async def main():
    # 1. СНАЧАЛА запускаем "сайт", чтобы Render не убил процесс
    await start_fake_website()

    # 2. ПОТОМ запускаем бота
    print("✅ Бот запущен!")
    print(f"🔐 Админы: {ADMIN_IDS}")
    print("📦 Сундуки, 💰 Экономика, 🎲 Кубы, 📢 Созывы")

    await dp.start_polling(bot)