"""Система отношений: этапы, РП-действия и общие константы.

Модуль специально без зависимостей: его импортируют и API (чтобы показать
плашку и этап в профиле Mini App), и бот (команды `+любовь`, `поцеловать` и
кнопки «Да/Нет»). Одна таблица этапов = одна правда о наградах, поэтому
сервер и бот не могут разойтись в цифрах.
"""

# Этапы отношений. min/max — границы по «Love коинам» пары (общий банк),
# reward — сколько получает КАЖДЫЙ партнёр при первом достижении этапа.
LOVE_STAGES = [
    {"index": 0, "min": 0, "max": 50, "title": "Случайное знакомство", "reward": 1000},
    {"index": 1, "min": 50, "max": 200, "title": "Дружба влюблённых", "reward": 2000},
    {"index": 2, "min": 200, "max": 500, "title": "Близость", "reward": 5000},
    {"index": 3, "min": 500, "max": 2000, "title": "Признание", "reward": 10000},
    {"index": 4, "min": 2000, "max": 5000, "title": "Крепкая любовь", "reward": 25000},
    {"index": 5, "min": 5000, "max": 12000, "title": "Вместе до смерти", "reward": 50000},
    {"index": 6, "min": 12000, "max": 20000, "title": "Почти брак", "reward": 100000},
    {"index": 7, "min": 20000, "max": None, "title": "Вечная любовь", "reward": 500000},
]

# Экономика Love.
LOVE_ACTION_BONUS = 10              # +10 Love коинов в общий банк пары за действие
# Задача 34: кулдаун ВРЕМЕННО выключен (0), чтобы можно было за один вечер
# прогнать уровень и проверить награды. Значение можно менять на лету
# секретной админ-командой `кд` (см. bot.py) — она пишет его в game_settings.
LOVE_ACTION_COOLDOWN = 0
LOVE_ACTION_COOLDOWN_DEFAULT = 5 * 3600   # «как задумано» — вернуть сюда
LOVE_ICE_COST = 3                   # «ускорить» за 3 i¢ (донат-валюта)

# Текущий кулдаун в секундах. Живёт в памяти процесса и переопределяется
# админ-командой; и бот (награды), и API (цифра в Mini App) читают его отсюда.
_COOLDOWN_STATE = {"seconds": LOVE_ACTION_COOLDOWN}


def love_cooldown_seconds() -> int:
    """Действующий кулдаун награды за РП, в секундах (0 — без ограничения)."""
    try:
        value = int(_COOLDOWN_STATE.get("seconds") or 0)
    except (TypeError, ValueError):
        value = 0
    return max(0, value)


def set_love_cooldown_seconds(seconds) -> int:
    """Поставить кулдаун награды. Возвращает применённое значение."""
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        value = 0
    value = max(0, value)
    _COOLDOWN_STATE["seconds"] = value
    return value


def format_love_cooldown(seconds=None) -> str:
    """«без ограничения» / «5 ч» / «30 мин» / «45 сек» — для сообщений."""
    value = love_cooldown_seconds() if seconds is None else max(0, int(seconds))
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


# ==================== ПУБЛИЧНАЯ ЧАСТЬ ЭТАПОВ ====================
# Задача 29: этапы и награды за них — СЕКРЕТНАЯ механика. Игрок видит только
# текущий уровень пары и Love коины, без сумм наград и без «следующего этапа»:
# интерес должен быть в самом процессе, а не в таблице выплат.
def public_stage(score) -> dict:
    """Уровень пары без наград: index, title, love, progress."""
    info = stage_info(score)
    return {
        "level": info["index"] + 1,
        "index": info["index"],
        "title": info["title"],
        "love": info["score"],
        "progress": info["progress"],
    }


def public_stages() -> list:
    """Список уровней для клиента — тоже без наград."""
    return [{"level": stage["index"] + 1, "index": stage["index"], "title": stage["title"]}
            for stage in LOVE_STAGES]


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


def _safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def paid_stages(supabase, relationship_id) -> set:
    try:
        res = (supabase.table(LOVE_REWARDS_TABLE).select("stage")
               .eq("relationship_id", int(relationship_id)).execute())
    except Exception:
        return set()
    return {_safe_int(row.get("stage"), -1) for row in (res.data or [])}


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
        # Отметку ставим ДО начисления: повторный вызов и гонка двух действий
        # не выдадут награду второй раз.
        try:
            supabase.table(LOVE_REWARDS_TABLE).insert({
                "relationship_id": relationship_id, "stage": stage["index"],
            }).execute()
        except Exception:
            continue
        for partner_id in partners:
            if partner_id <= 0:
                continue
            try:
                res = supabase.table("users").select("coins").eq("user_id", partner_id).execute()
                if not res.data:
                    continue
                coins = _safe_int(res.data[0].get("coins"), 0) + int(stage["reward"])
                supabase.table("users").update({"coins": coins}).eq("user_id", partner_id).execute()
            except Exception:
                continue
        granted.append({"stage": stage["index"], "title": stage["title"], "reward": stage["reward"]})
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
    """+Unlove одиночке. Возвращает (новый счёт, выданные награды)."""
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return 0, []
    current = solo_state(supabase, uid)["love"]
    new_score = current + int(amount)
    try:
        supabase.table(SOLO_LOVE_TABLE).upsert(
            {"user_id": uid, "love": new_score}, on_conflict="user_id").execute()
    except Exception:
        # Таблицы нет (миграция не применена) — считаем, что начислять некуда.
        return new_score, []
    return new_score, claim_solo_stage_rewards(supabase, uid, new_score)


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
        # Отметку — ДО начисления: гонка двух действий не выдаст награду дважды.
        try:
            supabase.table(SOLO_REWARDS_TABLE).insert({
                "user_id": uid, "stage": stage["index"],
            }).execute()
        except Exception:
            continue
        try:
            res = supabase.table("users").select("coins").eq("user_id", uid).execute()
            if res.data:
                coins = _safe_int(res.data[0].get("coins"), 0) + int(stage["reward"])
                supabase.table("users").update({"coins": coins}).eq("user_id", uid).execute()
        except Exception:
            pass
        granted.append({"stage": stage["index"], "title": stage["title"],
                        "reward": stage["reward"]})
    return granted
