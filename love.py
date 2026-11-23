"""Система отношений: этапы, РП-действия и общие константы.

Модуль специально без зависимостей: его импортируют и API (чтобы показать
плашку и этап в профиле Mini App), и бот (команды `+любовь`, `поцеловать` и
кнопки «Да/Нет»). Одна таблица этапов = одна правда о наградах, поэтому
сервер и бот не могут разойтись в цифрах.
"""

import logging
import time

logger = logging.getLogger(__name__)

# Этапы отношений. min/max — границы по «Love коинам» пары (общий банк),
# reward — сколько получает КАЖДЫЙ партнёр при первом достижении этапа.
#
# Задача 61 (уточнение заказчика): «1 этап: Случайное знакомство 0/50, как только
# достигают 50 — каждому по 1000 коинов». Значит этап «закрыт» уже на его ПРАВОЙ
# границе: 50 Love = 2-й этап, 200 = 3-й и т.д. Поэтому `min` следующего этапа и
# есть «сколько нужно» (поле `need` в публичной карточке).
# Задача 72: заказчик сам прислал таблицу призов и правило её выдачи:
#   «1 этап: Случайное знакомство 0/50 — награда за первый уровень НЕ даётся;
#    2 этап: Дружба влюблённых 50/200 — приз 1 000 каждому;
#    3 Близость 200/500 — 3 000; 4 Признание 500/2000 — 8 000;
#    5 Крепкая любовь 2000/5000 — 15 000; 6 Вместе до смерти 5000/12000 — 40 000;
#    7 Почти брак 12000/20000 — 100 000; 8 Вечная любовь 20000∞ — 500 000».
# То есть reward — это приз ЗА ПЕРЕХОД на уровень (за уровень 2 — 1000).
# Уровень 1 не оплачивается никогда: reward = 0, поэтому в базе даже не
# появляется отметка о выплате, и «нахимичить» повторно её нельзя.
LOVE_STAGES = [
    {"index": 0, "min": 0, "max": 50, "title": "Случайное знакомство", "reward": 0},
    {"index": 1, "min": 50, "max": 200, "title": "Дружба влюблённых", "reward": 1000},
    {"index": 2, "min": 200, "max": 500, "title": "Близость", "reward": 3000},
    {"index": 3, "min": 500, "max": 2000, "title": "Признание", "reward": 8000},
    {"index": 4, "min": 2000, "max": 5000, "title": "Крепкая любовь", "reward": 15000},
    {"index": 5, "min": 5000, "max": 12000, "title": "Вместе до смерти", "reward": 40000},
    {"index": 6, "min": 12000, "max": 20000, "title": "Почти брак", "reward": 100000},
    {"index": 7, "min": 20000, "max": None, "title": "Вечная любовь", "reward": 500000},
]

# Этапы, за которые приз не выдаётся вообще (первый уровень).
LOVE_FREE_STAGES = frozenset({0})

# Сколько Love нужно, чтобы получить награду этапа (правая граница этапа).
# Нужно для честного текста «Достигли этапа N: +X коинов каждому».
LOVE_STAGE_TARGETS = [50, 200, 500, 2000, 5000, 12000, 20000]

# Экономика Love.
LOVE_ACTION_BONUS = 10              # +10 Love коинов в общий банк пары за действие
# Задача 76 (прямое требование заказчика): «если использовали одну РП-команду,
# больше никакие нельзя использовать в течение 5 часов». То есть КД — ОДИН на
# игрока, общий для всех РП. Раньше (задача 61) он считался на каждую команду
# отдельно, и после «обнять» можно было сразу «смотреть фильм» — заказчик
# увидел в чате именно это.
#
# Задача 76: команда `кд` удалена полностью, значение НЕ настраивается:
# 5 часов — жёсткое правило. Строки в game_settings, оставшиеся от старых
# сборок, больше не читаются, поэтому «3 часа» из прежней настройки тоже
# не действует.
LOVE_ACTION_COOLDOWN = 5 * 3600
LOVE_ACTION_COOLDOWN_DEFAULT = 5 * 3600   # значение по умолчанию = единственное
# Минимум/максимум оставлены для арифметики страховок: clamp не даст поставить
# отрицательное или бесконечное значение из старых данных.
LOVE_COOLDOWN_MIN = 5 * 3600
LOVE_COOLDOWN_FIXED = LOVE_ACTION_COOLDOWN
LOVE_COOLDOWN_MAX = 5 * 3600

# Задача 68: ускорения за i¢ больше нет (заказчик убрал и текст, и кнопку).
# Константа оставлена только для совместимости формата /api/love_*: старые
# сборки Mini App читают поле ice_cost. Никаких списаний по ней не происходит.
LOVE_ICE_COST = 3

# Задача 76: очередь одна на игрока. Значение всегда 5 часов, поэтому состояние
# в памяти не нужно — оставлены только совместимые функции, которые его отдают.
def love_cooldown_seconds(action=None) -> int:
    """Кулдаун РП-награды в секундах. Один на игрока, 5 часов, не настраивается.

    `action` принимается только ради совместимости со старыми вызовами
    (задача 61 считала КД на каждую команду отдельно) — значение от него не
    зависит.
    """
    return LOVE_ACTION_COOLDOWN


def clamp_love_cooldown(seconds) -> int:
    """Любое значение → 5 часов: настройки больше нет (задача 76)."""
    return LOVE_ACTION_COOLDOWN


def set_love_cooldown_seconds(seconds, action=None) -> int:
    """Совместимость: настройка отключена, всегда возвращает 5 часов."""
    return LOVE_ACTION_COOLDOWN


def love_cooldown_overrides() -> dict:
    """Совместимость: персональных значений больше нет."""
    return {}


def load_love_cooldown_overrides(raw) -> None:
    """Совместимость: старые значения из базы игнорируются (задача 76)."""
    return None


def reset_love_cooldown_override(action) -> int:
    """Совместимость: сбрасывать нечего — КД один и всегда 5 часов."""
    return LOVE_ACTION_COOLDOWN


# Метка старого «кулдаун выключен» в game_settings (задача 34). Больше ничего
# не значит: код её только узнаёт, чтобы честно игнорировать старые строки.
COOLDOWN_OFF_MARKER = "off"


def cooldown_from_setting(raw, default: int = 0):
    """Старая настройка из game_settings → (5 часов, False): игнорируется.

    Задача 76: команда `кд` удалена, поэтому любые строки `love_cooldown_seconds`
    и `love_cooldown_actions` в базе не влияют на игру.
    """
    return LOVE_ACTION_COOLDOWN, False


def format_love_cooldown(seconds=None, action=None) -> str:
    """«без ограничения» / «5 ч» / «30 мин» / «45 сек» — для сообщений.

    `action` — показать кулдаун конкретной РП-команды.
    """
    # Задача 67: кулдаун всегда включён (минимум 5 часов), поэтому ветку
    # «без ограничения» оставляем только для явно переданного нуля — например,
    # для текстов о других механиках. Для РП-команд она недостижима.
    value = love_cooldown_seconds(action) if seconds is None else max(0, int(seconds))
    if value <= 0:
        return "без ограничения"
    if value % 3600 == 0:
        return f"{value // 3600} ч"
    if value % 60 == 0:
        return f"{value // 60} мин"
    return f"{value} сек"

# РП-команды: `поцеловать @user`, `обнять @user` и т.д. Ключи — то, что пишет
# игрок; значение — эмодзи и текст для сообщения.
RP_ACTIONS = {
    "прижать": {"emoji": "🤗", "text": "крепко прижимает к себе"},
    "поцеловать": {"emoji": "😘", "text": "целует"},
    "обнять": {"emoji": "🫂", "text": "обнимает"},
    "погладить": {"emoji": "🖐️", "text": "гладит по голове"},
    "укусить": {"emoji": "😈", "text": "игриво кусает"},
    "шлепнуть": {"emoji": "🍑", "text": "шлёпает"},
    "держать за руку": {"emoji": "🤝", "text": "берёт за руку"},
    "укрыть пледом": {"emoji": "🛌", "text": "укрывает пледом"},
    "накормить": {"emoji": "🍰", "text": "угощает вкусненьким"},
    "станцевать": {"emoji": "💃", "text": "приглашает на танец"},
    # Расширенный набор (задача 29): список доступен командой «все рп».
    "поцеловать в щёку": {"emoji": "😚", "text": "целует в щёку"},
    "поцеловать в лоб": {"emoji": "😌", "text": "целует в лоб"},
    "чмокнуть": {"emoji": "💋", "text": "чмокает"},
    "обнять крепко": {"emoji": "🫂", "text": "обнимает так крепко, что не отпускает"},
    "приобнять": {"emoji": "🫶", "text": "приобнимает за плечи"},
    "погладить по волосам": {"emoji": "💆", "text": "перебирает волосы"},
    "щекотать": {"emoji": "🤭", "text": "щекочет"},
    "подмигнуть": {"emoji": "😉", "text": "подмигивает"},
    "укутать": {"emoji": "🧣", "text": "укутывает в шарф"},
    "носить на руках": {"emoji": "👐", "text": "берёт на руки"},
    "сделать массаж": {"emoji": "💆‍♂️", "text": "делает массаж"},
    "шептать на ушко": {"emoji": "🤫", "text": "шепчет что-то на ушко"},
    "подарить цветы": {"emoji": "💐", "text": "дарит букет цветов"},
    "спеть серенаду": {"emoji": "🎶", "text": "поёт серенаду"},
    "дать пять": {"emoji": "🙌", "text": "даёт пять"},
    "чокнуться": {"emoji": "🥂", "text": "чокается бокалом"},
    "смотреть фильм": {"emoji": "🎬", "text": "устраивается смотреть фильм"},
    "гулять под луной": {"emoji": "🌙", "text": "зовёт гулять под луной"},
    "танцевать вальс": {"emoji": "🩰", "text": "кружит в вальсе"},
    # Задача 70: интимные команды «на свой вкус» — по просьбе заказчика.
    # Тексты держим флиртующими, но без пошлости: бот играет и в чатах.
    "соблазнить": {"emoji": "🔥", "text": "соблазняет, глядя в глаза"},
    "поцеловать в шею": {"emoji": "🫦", "text": "целует в шею"},
    "прикусить за ушко": {"emoji": "😏", "text": "прикусывает за ушко"},
    "обнять сзади": {"emoji": "🫂", "text": "обнимает со спины, прижимаясь"},
    "затащить в постель": {"emoji": "🛏️", "text": "утаскивает в постель"},
    "лечь в обнимку": {"emoji": "🌙", "text": "ложится рядом в обнимку"},
    "провести ночь": {"emoji": "🌃", "text": "проводит вместе всю ночь"},
    "снять одежду": {"emoji": "🫣", "text": "медленно снимает одежду"},
    "заняться любовью": {"emoji": "💞", "text": "занимается любовью"},
    "сделать сюрприз": {"emoji": "🎁", "text": "устраивает романтический сюрприз"},
}


def stage_index(score) -> int:
    """Индекс этапа по накопленным Love коинам (мусор → 0)."""
    try:
        value = max(0, int(float(score or 0)))
    except (TypeError, ValueError):
        value = 0
    for stage in reversed(LOVE_STAGES):
        if value >= stage["min"]:
            return stage["index"]
    return 0


def stage_info(score) -> dict:
    """Полная карточка этапа: границы, название, награда и прогресс."""
    index = stage_index(score)
    stage = LOVE_STAGES[index]
    try:
        value = max(0, int(float(score or 0)))
    except (TypeError, ValueError):
        value = 0
    ceiling = stage["max"]
    if ceiling is None:
        progress = 100.0
    else:
        span = max(1, ceiling - stage["min"])
        progress = min(100.0, max(0.0, (value - stage["min"]) / span * 100.0))
    return {
        "index": index,
        "title": stage["title"],
        "reward": stage["reward"],
        "min": stage["min"],
        "max": ceiling,
        "score": value,
        "progress": round(progress, 1),
    }


def next_stage(score):
    """Следующий этап (или None, если уже «Вечная любовь»)."""
    index = stage_index(score)
    if index + 1 >= len(LOVE_STAGES):
        return None
    return dict(LOVE_STAGES[index + 1])


def stages_reached(previous_score, new_score) -> list:
    """Этапы, которые пара перешагнула за одно действие: за каждый — награда."""
    previous_index = stage_index(previous_score)
    new_index = stage_index(new_score)
    return [dict(stage) for stage in LOVE_STAGES[previous_index + 1:new_index + 1]]


def stage_is_paid(stage) -> bool:
    """Платный ли этап: за 1-й уровень приз не дают никогда (задача 72)."""
    if not stage:
        return False
    index = _safe_int(stage.get("index", stage.get("stage")), -1)
    if index in LOVE_FREE_STAGES:
        return False
    return _safe_int(stage.get("reward"), 0) > 0


def _safe_int(value, default=0):
    """Терпимое к мусору число: None/«abc» → default."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


# ==================== ПУБЛИЧНАЯ ЧАСТЬ ЭТАПОВ ====================
# Задача 29: этапы и награды за них — СЕКРЕТНАЯ механика. Игрок видит только
# текущий уровень пары и Love коины, без сумм наград и без «следующего этапа»:
# интерес должен быть в самом процессе, а не в таблице выплат.
def public_stage(score) -> dict:
    """Уровень пары без наград: index, title, love, progress.

    Задача 55: добавляем «сколько до следующего уровня». Без этого игрок видел
    «Уровень 1 · Случайное знакомство · Love 0» и не понимал, что вообще
    двигает уровень: казалось, что счёт не растёт и наград нет.
    """
    info = stage_info(score)
    index = info["index"]
    current = LOVE_STAGES[index]
    nxt = LOVE_STAGES[index + 1] if index + 1 < len(LOVE_STAGES) else None
    return {
        "level": index + 1,
        "index": index,
        "title": info["title"],
        "love": info["score"],
        "progress": info["progress"],
        # Границы этапа: от «стало» до «нужно».
        "stage_min": current["min"],
        "stage_max": current["max"],
        # Задача 61: «до 50 Love» / для финального этапа — None (только растёт).
        # Сумму награды клиенту НЕ отдаём: призы не показываются заранее.
        "need": current["max"],
        "next_level": (index + 2) if nxt else None,
        "next_title": nxt["title"] if nxt else None,
        # Сколько Love осталось до следующего уровня (None — максимальный).
        "to_next": max(0, nxt["min"] - info["score"]) if nxt else None,
    }


def public_stages() -> list:
    """Список уровней для клиента: номер, название и «сколько нужно Love»."""
    return [{"level": stage["index"] + 1, "index": stage["index"],
             "title": stage["title"], "need": stage["max"], "min": stage["min"]}
            for stage in LOVE_STAGES]


def next_level_text(score, currency: str = "Love") -> str:
    """«До уровня 2 — 40 Love» / «Максимальный уровень» — для бота и API.

    Задача 55: `currency` нужен одиночной ветке — там счёт называется Unlove,
    и «До уровня 2 — 40 Love» у одинокого игрока только путало.
    """
    info = public_stage(score)
    if info["next_level"] is None:
        return "Максимальный уровень"
    return f"До уровня {info['next_level']} — {info['to_next']} {currency}"


def action_progress_text(score, solo: bool = False) -> str:
    """«До уровня 2 — 10/50💔» — прогресс внутри текущего уровня.

    Задача 72: заказчик прислал ровно такой вид ответа на РП-команду:
        🫂 Broker обнимает
        +10 Unlove💔
        ➡️ До уровня 2 — 10/40💔
    Числа — «сколько Love уже есть на этом уровне / сколько нужно до
    следующего», поэтому видно и прибавку, и остаток, без слова «всего».
    """
    info = public_stage(score)
    heart = "💔" if solo else "❤️"
    if info["next_level"] is None:
        return f"💞 Максимальный уровень — {info['love']}{heart}"
    have = max(0, info["love"] - int(info["stage_min"] or 0))
    need = max(0, int(info["need"] or 0) - int(info["stage_min"] or 0))
    return f"➡️ До уровня {info['next_level']} — {have}/{need}{heart}"


def _coins_fmt(amount) -> str:
    """«1 000» — коины с пробелом между тысячами (так читается на телефоне)."""
    value = _safe_int(amount, 0)
    return f"{value:,}".replace(",", " ")


def celebration_text(stage, amount_per_partner=0, solo: bool = False,
                     partner_name: str = "", balance=None,
                     next_stage=None) -> str:
    """ПОЗДРАВЛЕНИЕ о новом уровне отношений (задача 81).

    Заказчик: «сделай красивый текст поздравительный, типа ты перешёл на новый
    уровень, вот тебе награда» — раньше в ответе была одна сухая строка без
    суммы, без «поздравляем» и без пояснения, что деньги уже на балансе.

    Возвращает HTML (parse_mode="HTML"):
        🎉 ПОЗДРАВЛЯЕМ!
        Вы перешли на 2 уровень отношений
        💗 «Дружба влюблённых»
        🎁 Награда: +1 000 коинов — уже на балансе
        ...
    """
    stage = stage or {}
    index = _safe_int(stage.get("index", stage.get("stage")), 0)
    level = index + 1
    title = str(stage.get("title") or "")
    amount = _safe_int(amount_per_partner, 0) or _safe_int(stage.get("reward"), 0)

    lines = ["🎉 <b>ПОЗДРАВЛЯЕМ!</b>", ""]
    lines.append(f"Вы перешли на <b>{level} уровень отношений</b>")
    if title:
        lines.append(f"💗 «{title}»")
    lines.append("")
    if amount > 0:
        if solo:
            lines.append(f"🎁 Награда: <b>+{_coins_fmt(amount)} коинов</b> — уже на балансе")
        else:
            partner = f" с {partner_name}" if partner_name else ""
            lines.append(f"🎁 Награда за новый уровень{partner}:")
            lines.append(f"<b>+{_coins_fmt(amount)} коинов каждому</b> — уже на балансе")
            lines.append("У вас обоих уровень вырос одновременно, поэтому коины "
                         "получил каждый.")
    if balance is not None:
        lines.append(f"💰 Теперь на балансе: <b>{_coins_fmt(balance)}</b> коинов")
    nxt_title = str((next_stage or {}).get("title") or "")
    nxt_level = _safe_int((next_stage or {}).get("level"), 0)  # 0 — финальный уровень
    if nxt_level:
        lines.append("")
        line = f"➡️ Следующий уровень — <b>{nxt_level}</b>"
        if nxt_title:
            line += f" «{nxt_title}»"
        need = _safe_int((next_stage or {}).get("love_need"), 0)
        if need > 0:
            line += f", нужно {_coins_fmt(need)} Love❤️"
        lines.append(line)
    return "\n".join(lines)


def stage_reached_text(stage, amount_per_partner, solo: bool = False) -> str:
    """«Вы достигли 2 уровня отношений «Дружба влюблённых» — каждому по 2 000».

    Задача 61: награда за этап всегда начисляется (её не нужно «забирать»), но
    игрок должен узнать об этом — иначе рост баланса выглядит случайным.
    """
    stage = stage or {}
    title = str(stage.get("title") or "")
    # ВАЖНО: claim_stage_rewards отдаёт номер этапа в ключе "stage", а карточка
    # public_stage — в "index". Раньше здесь читался только "index", поэтому
    # ЛЮБОЕ сообщение о новом уровне говорило «1 уровня» — даже восьмого.
    index = stage.get("index", stage.get("stage"))
    level = _safe_int(index, 0) + 1
    amount = _safe_int(amount_per_partner, 0) or _safe_int(stage.get("reward"), 0)
    head = f"💗 Вы достигли {level} уровня отношений"
    if title:
        head += f" «{title}»"
    # У одиночки нет второго участника — «каждому участнику» ей врало.
    tail = f"вам +{amount} коинов!" if solo else f"каждому участнику +{amount} коинов!"
    return f"{head} — {tail}"


def rp_action_key(raw):
    """Нормализовать имя команды из админ-сообщения: «Обнять» → «обнять».

    Возвращает ключ из RP_ACTIONS или None, если такой команды нет. Нужно для
    настройки кулдауна по конкретному действию (`кд обнять 1ч`).
    """
    text = str(raw or "").strip().lower().replace("ё", "е")
    if not text:
        return None
    text = " ".join(text.split())
    for key in RP_ACTIONS:
        normalized = key.replace("ё", "е")
        if text == normalized:
            return key
    action, _meta = rp_action_for(text)
    return action


def rp_action_for(text) -> tuple:
    """`поцеловать` → ('поцеловать', {emoji, text}). Не РП-команда → (None, None)."""
    raw = str(text or "").strip().lower()
    if not raw:
        return None, None
    parts = raw.split()
    if not parts:
        return None, None
    # Двухсловные команды («держать за руку») проверяем раньше однословных.
    for key in sorted(RP_ACTIONS, key=lambda item: len(item.split()), reverse=True):
        if raw.startswith(key):
            return key, RP_ACTIONS[key]
    return None, None


# ==================== НАГРАДЫ ЗА ЭТАПЫ ====================
# Начисление вынесено в общий модуль: и бот (после РП-действия), и API
# (при открытии Mini App) вызывают одну и ту же функцию, поэтому награда не
# может быть выдана дважды или не выдана вовсе.
LOVE_REWARDS_TABLE = "love_stage_rewards"


def paid_stages(supabase, relationship_id) -> set:
    try:
        res = (supabase.table(LOVE_REWARDS_TABLE).select("stage")
               .eq("relationship_id", int(relationship_id)).execute())
    except Exception:
        return set()
    return {_safe_int(row.get("stage"), -1) for row in (res.data or [])}


def _credit_coins(supabase, user_id, amount: int, attempts: int = 3) -> bool:
    """Начислить коины с повтором. False — не смогли (тогда награду повторим).

    Задача 62: раньше сбой одного `update` молча проглатывался, а этап уже был
    отмечен выплаченным — награда терялась навсегда. Теперь неудачу видно
    наверх: вызывающий код снимает отметку и попробует снова.
    """
    uid = _safe_int(user_id, 0)
    amount = _safe_int(amount, 0)
    if uid <= 0 or amount <= 0:
        return False
    for attempt in range(max(1, attempts)):
        try:
            res = supabase.table("users").select("coins").eq("user_id", uid).execute()
            if res.data:
                coins = _safe_int(res.data[0].get("coins"), 0) + amount
                supabase.table("users").update({"coins": coins}).eq("user_id", uid).execute()
                return True
            # Строки игрока нет — повторять бессмысленно.
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("Коины игроку %s не начислены (попытка %s): %s",
                           uid, attempt + 1, exc)
            time.sleep(0.2 * (attempt + 1))
    return False


def _unmark_stage(supabase, table: str, match: dict, stage_index) -> None:
    """Снять отметку о выплате этапа, если награда не дошла."""
    try:
        query = supabase.table(table).delete()
        for column, value in match.items():
            query = query.eq(column, value)
        query.eq("stage", int(stage_index)).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Отметку этапа %s в %s снять не удалось: %s",
                       stage_index, table, exc)


def claim_stage_rewards(supabase, relationship) -> list:
    """Награды за достигнутые этапы: каждому партнёру, ровно один раз на этап."""
    if not relationship:
        return []
    relationship_id = _safe_int(relationship.get("id"), 0)
    if relationship_id <= 0:
        return []
    score = _safe_int(relationship.get("love"), 0)
    current_index = stage_index(score)
    already = paid_stages(supabase, relationship_id)
    partners = [_safe_int(relationship.get("user_a"), 0), _safe_int(relationship.get("user_b"), 0)]
    granted = []
    for stage in LOVE_STAGES:
        if stage["index"] > current_index or stage["index"] in already:
            continue
        # Задача 72: за 1-й уровень приз не даётся — этап просто пропускаем,
        # отметку в базе не ставим (выдавать там нечего).
        if not stage_is_paid(stage):
            continue
        # Отметку ставим ДО начисления: повторный вызов и гонка двух действий
        # не выдадут награду второй раз.
        try:
            supabase.table(LOVE_REWARDS_TABLE).insert({
                "relationship_id": relationship_id, "stage": stage["index"],
            }).execute()
        except Exception:
            continue
        failed = False
        for partner_id in partners:
            if partner_id <= 0:
                continue
            if not _credit_coins(supabase, partner_id, int(stage["reward"])):
                failed = True
        if failed:
            # Деньги кому-то не дошли (сбой базы, нет строки игрока): отметку
            # снимаем, чтобы награда выдалась при следующем действии. Раньше
            # отметка оставалась, и награда за этап терялась НАВСЕГДА, а игрок
            # при этом получал сообщение «вам начислено».
            _unmark_stage(supabase, LOVE_REWARDS_TABLE, {"relationship_id": relationship_id},
                          stage["index"])
            logger.warning("Награда за этап %s пары %s не начислена — повторим позже",
                           stage["index"], relationship_id)
            continue
        granted.append({"stage": stage["index"], "title": stage["title"],
                        "reward": stage["reward"]})
    return granted


# ==================== ОДИНОКИЕ (UNLOVE) ====================
# Задача 30: если пары нет — игрок не «застревает», а качает свою ветку.
# Экономика ровно та же (те же +10 за действие, тот же кулдаун, те же этапы и
# те же награды), но:
#   • валюта называется Unlove и принадлежит одному человеку;
#   • награду за этап получает только этот один человек;
#   • передать Love/Unlove другому нельзя — только заработать самим;
#   • как только игрок вступает в отношения, вся его одиночная ветка сгорает
#     (счёт обнуляется), а этапы, за которые награда уже выдана, остаются
#     закрытыми — иначе награды можно было бы фармить «пара — развод — пара».
SOLO_LOVE_TABLE = "solo_love"
SOLO_REWARDS_TABLE = "solo_love_stage_rewards"
SOLO_CURRENCY = "Unlove"


def solo_state(supabase, user_id) -> dict:
    """Счёт одиночки. Нет таблицы/строки — просто нули, а не падение."""
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return {"love": 0, "created_at": None}
    try:
        res = (supabase.table(SOLO_LOVE_TABLE).select("love, created_at")
               .eq("user_id", uid).limit(1).execute())
    except Exception:
        return {"love": 0, "created_at": None}
    row = (res.data or [None])[0]
    if not row:
        return {"love": 0, "created_at": None}
    return {"love": _safe_int(row.get("love"), 0), "created_at": row.get("created_at")}


def add_solo_love(supabase, user_id, amount: int) -> tuple:
    """+Unlove одиночке. Возвращает (новый счёт, выданные награды).

    Задача 55: если запись не прошла, счёт возвращается ПРЕЖНИМ, а не
    «нарисованным». Раньше бот писал «+10 Unlove (всего 20)», хотя в базе
    оставалось 10: игрок видел рост, а после рестарта счёт откатывался.
    """
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return 0, []
    current = solo_state(supabase, uid)["love"]
    new_score = current + int(amount)
    try:
        supabase.table(SOLO_LOVE_TABLE).upsert(
            {"user_id": uid, "love": new_score}, on_conflict="user_id").execute()
    except Exception:
        # Таблицы нет (миграция не применена) — начислять некуда, врём про рост.
        return current, []
    # Обратное чтение: upsert может «пройти» и ничего не изменить (RLS).
    saved = solo_state(supabase, uid)["love"]
    if saved < new_score:
        return saved, []
    return saved, claim_solo_stage_rewards(supabase, uid, saved)


def solo_saved(supabase, user_id, expected: int) -> bool:
    """Реально ли +Unlove лежит в базе (для честного ответа игроку)."""
    return _safe_int(solo_state(supabase, user_id)["love"], 0) >= int(expected)


def reset_solo_love(supabase, user_id) -> bool:
    """Вступление в отношения: одиночный счёт сгорает.

    Таблицу выданных наград НЕ трогаем: она защищает от повторного получения
    тех же этапов после «пара → развод → снова один».
    """
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return False
    try:
        supabase.table(SOLO_LOVE_TABLE).delete().eq("user_id", uid).execute()
        return True
    except Exception:
        return False


def solo_paid_stages(supabase, user_id) -> set:
    try:
        res = (supabase.table(SOLO_REWARDS_TABLE).select("stage")
               .eq("user_id", int(user_id)).execute())
    except Exception:
        return set()
    return {_safe_int(row.get("stage"), -1) for row in (res.data or [])}


def claim_solo_stage_rewards(supabase, user_id, score) -> list:
    """Награды за этапы одиночке: один человек, один раз на этап."""
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return []
    current_index = stage_index(score)
    already = solo_paid_stages(supabase, uid)
    granted = []
    for stage in LOVE_STAGES:
        if stage["index"] > current_index or stage["index"] in already:
            continue
        # Задача 72: 1-й уровень не оплачивается и в одиночной ветке.
        if not stage_is_paid(stage):
            continue
        # Отметку — ДО начисления: гонка двух действий не выдаст награду дважды.
        try:
            supabase.table(SOLO_REWARDS_TABLE).insert({
                "user_id": uid, "stage": stage["index"],
            }).execute()
        except Exception:
            continue
        if not _credit_coins(supabase, uid, int(stage["reward"])):
            # Не смогли начислить — снимаем отметку, награда выдастся позже
            # (см. тот же разбор в claim_stage_rewards).
            _unmark_stage(supabase, SOLO_REWARDS_TABLE, {"user_id": uid}, stage["index"])
            logger.warning("Награда за этап %s игрока %s не начислена — повторим позже",
                           stage["index"], uid)
            continue
        granted.append({"stage": stage["index"], "title": stage["title"],
                        "reward": stage["reward"]})
    return granted
