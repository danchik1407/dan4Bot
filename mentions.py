"""Имена и упоминания игроков в сообщениях бота.

Задача 53: в чате прилетало «🫂 Broker обнимает партнёра» — без ника и без
ссылки на живого человека. Причина: в ответе РП-команды стоял жёстко
прописанный текст «партнёра», а не имя второй половины. Из-за этого
непонятно, кого именно обняли, и работает ли команда вообще.

Здесь собраны две вещи, которых не хватало:

1. ``clean_name`` — превращает «@ник», ссылку или пустоту в нормальный ник
   без собаки (в ответах бота @username не выводим — задача 44);
2. ``parse_names`` / ``ids_from_rows`` — собирают ники и id пачкой, чтобы
   бот делал ОДИН запрос (``user_id in (...)``), а не по SELECT на человека.

Модуль намеренно не тянет aiogram и не ходит в сеть: его можно проверять
тестами без запуска бота.
"""

from __future__ import annotations

import html
import re
from typing import Iterable, Mapping


# Служебные подписи, которые нельзя показывать как ник: раньше они попадали
# в текст и выглядели как «обнимает партнёра», «обнимает игрока».
PLACEHOLDER_NAMES = {
    "", "партнёр", "партнер", "партнёра", "партнера", "игрок", "игрока",
    "кто-то", "кто то", "пользователь", "none", "null",
}

# Задача 66: в `users.username` иногда лежит не ник, а служебный код Telegram —
# на скрине заказчика бот писал «Dan4ik обнимает Contact_blocked». Это не
# человек, а маркер из ответа API, и показывать его как имя нельзя.
#
# Важно не перестараться: обычный ник «Botan», «User» или «Robotik» обязан
# остаться. Поэтому составные маркеры ищем внутри строки, а простые слова
# сравниваем целиком.
SERVICE_NAME_PARTS = (
    "contact_blocked", "contactblocked",
    "deleted_account", "deletedaccount", "account_deleted",
    "channel_bot", "channelbot", "telegram_bot",
)
SERVICE_NAME_EXACT = {
    "telegram", "anonymous", "hidden", "unknown", "deleted", "deletedaccount",
}


def is_service_name(raw) -> bool:
    """Похоже ли значение на служебный код, а не на ник живого человека."""
    text = str(raw or "").strip().strip("@")
    if not text:
        return True
    # camelCase («ContactBlocked») разбираем в нижний регистр без разделителей —
    # так ловятся оба написания, а обычные ники не страдают.
    compact = text.lower().replace("_", "").replace(" ", "")
    if compact in SERVICE_NAME_EXACT:
        return True
    return any(part.replace("_", "") in compact for part in SERVICE_NAME_PARTS)


def clean_name(raw) -> str:
    """Ник для текста: без «@», без лишних пробелов, без HTML-мусора.

    Пустая строка означает «имени нет» — вызывающая сторона сама решает,
    подставить ли ``id123`` или ссылку без подписи.
    """
    if raw is None:
        return ""
    text = html.unescape(str(raw)).strip()
    if not text:
        return ""
    # Пробелы схлопываем, но угловые скобки НЕ вырезаем: экранирование делает
    # escape_name/mention_html, иначе «<b>x</b>» теряло бы смысл уже здесь.
    text = re.sub(r"\s+", " ", text).strip()
    text = text.lstrip("@").strip()
    if text.lower() in PLACEHOLDER_NAMES:
        return ""
    # Задача 66: «Contact_blocked» и подобные коды Telegram — это не ник.
    # Пустая строка → вызывающая сторона возьмёт живое имя или id<число>.
    if is_service_name(text):
        return ""
    return text[:64]


def fallback_name(user_id, raw=None) -> str:
    """Ник или ``id<число>`` — то, что гарантированно читаемо."""
    name = clean_name(raw)
    if name:
        return name
    try:
        return f"id{int(user_id)}"
    except (TypeError, ValueError):
        return "игрок"


def escape_name(raw) -> str:
    """Готовый к parse_mode=HTML ник (без ссылки)."""
    return html.escape(clean_name(raw) or "игрок")


def mention_html(user_id, name=None) -> str:
    """Ссылка на профиль с ником: ``<a href="tg://user?id=…">Ник</a>``.

    Если ника нет — подписью идёт ``id<число>``, но ссылка остаётся: в чате
    видно, что бот говорит про конкретного человека.
    """
    if not user_id:
        return "игрок"
    return (f'<a href="tg://user?id={int(user_id)}">'
            f'{html.escape(fallback_name(user_id, name))}</a>')


def merge_names(*maps) -> dict[int, str]:
    """Объединяет источники имён: последний непустой выигрывает.

    Задача 55: у @username и у отображаемого имени разная судьба — username
    может быть занят человеком, который сменил ник (тогда в базе остаётся
    «Contact_blocked» от прежнего владельца), а Telegram-имя приходит живым.
    Поэтому база и Telegram-имена складываются вместе, а не заменяют друг друга.
    """
    result: dict[int, str] = {}
    for source in maps:
        for key, value in (source or {}).items():
            try:
                uid = int(key)
            except (TypeError, ValueError):
                continue
            name = clean_name(value)
            if name:
                result[uid] = name
    return result


def parse_names(rows: Iterable[Mapping]) -> dict[int, str]:
    """``[{user_id, username}] → {user_id: ник}`` для подстановки в текст."""
    result: dict[int, str] = {}
    for row in rows or []:
        try:
            uid = int(row.get("user_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        name = clean_name(row.get("username"))
        if name:
            result[uid] = name
    return result


def ids_from_rows(rows: Iterable[Mapping], keys=("user_a", "user_b")) -> list[int]:
    """Собирает id пользователей из строк relationships без дублей."""
    found: list[int] = []
    for row in rows or []:
        for key in keys:
            try:
                uid = int(row.get(key))
            except (TypeError, ValueError):
                continue
            if uid and uid not in found:
                found.append(uid)
    return found
