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
LOVE_ACTION_COOLDOWN = 5 * 3600     # награда не чаще, чем раз в 5 часов
LOVE_ICE_COST = 3                   # «ускорить» за 3 i¢ (донат-валюта)

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
