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

# Задача 104 (ТЗ «Система Любовь»): у одиночек СВОИ названия уровней при тех
# же границах и тех же призах. Раньше одиночке показывали названия парных
# этапов («Близость», «Вечная любовь») — по ТЗ это отдельная ветка:
#   Знакомство с самим собой · Уверенный одиночка · Бродяга · Депрессивный ·
#   Страдалец с бокалом вина · Одинокий волк · Легенда одиноких · Всю жизнь один
SOLO_STAGES = [
    {"index": 0, "min": 0, "max": 50, "title": "Знакомство с самим собой", "reward": 0},
    {"index": 1, "min": 50, "max": 200, "title": "Уверенный одиночка", "reward": 1000},
    {"index": 2, "min": 200, "max": 500, "title": "Бродяга", "reward": 3000},
    {"index": 3, "min": 500, "max": 2000, "title": "Депрессивный", "reward": 8000},
    {"index": 4, "min": 2000, "max": 5000, "title": "Страдалец с бокалом вина", "reward": 15000},
    {"index": 5, "min": 5000, "max": 12000, "title": "Одинокий волк", "reward": 40000},
    {"index": 6, "min": 12000, "max": 20000, "title": "Легенда одиноких", "reward": 100000},
    {"index": 7, "min": 20000, "max": None, "title": "Всю жизнь один", "reward": 500000},
]


def stages_for(solo: bool = False) -> list:
    """Список этапов нужной ветки: пары или одиночки."""
    return SOLO_STAGES if solo else LOVE_STAGES


# Этапы, за которые приз не выдаётся вообще (первый уровень).
LOVE_FREE_STAGES = frozenset({0})

# Сколько Love нужно, чтобы получить награду этапа (правая граница этапа).
# Нужно для честного текста «Достигли этапа N: +X коинов каждому».
LOVE_STAGE_TARGETS = [50, 200, 500, 2000, 5000, 12000, 20000]

# Экономика Love.
# Задача 104 (ТЗ): «одна RP-команда даёт от 1 до 10 любви (рандом) — всегда
# разное число, не фиксированные 10». LOVE_ACTION_BONUS оставлен как «сколько
# максимум» (его читают старые сборки Mini App и админка), а начисляется
# LOVE_GAIN_MIN..LOVE_GAIN_MAX через love_gain().
LOVE_ACTION_BONUS = 10              # максимум за одно действие (совместимость)
LOVE_GAIN_MIN = 1
LOVE_GAIN_MAX = 10


def love_gain(rng=None) -> int:
    """Сколько Love/Unlove начислить за одно РП: случайное 1..10."""
    import random as _random
    source = rng or _random
    return int(source.randint(LOVE_GAIN_MIN, LOVE_GAIN_MAX))
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

# ==================== РП-КОМАНДЫ (задача 105) ====================
# Ключ — то, что пишет игрок. Поля:
#   emoji    — значок действия;
#   text     — короткая подпись для списка «все рп»;
#   tpl      — живой текст для пары: `{b}` подставляется ником второго;
#   self_tpl — текст, когда игрок один и никого не отметил: `{self}` /
#              `{self_dat}` / `{self_ins}` подставляются по полу игрока
#              («саму себя» / «самой себе» / «самой собой»).
# Задача 105: «давай пару рп не трогай в плане текста, остальным текст
# немного измени». Без изменений остались две самые ходовые команды —
# «обнять» и «поцеловать»; у остальных, включая основной набор из ТЗ,
# появилась «изюминка»: полноценная сценка вместо двух слов.
RP_ACTIONS = {
    # ---- Основной набор (ТЗ задачи 104: «Список (все рп)») ----
    "обнять": {"emoji": "🫂", "text": "обнимает",
               "self_tpl": "обнимает {self}"},
    "взять за руку": {"emoji": "🤝", "text": "берёт за руку",
                      "tpl": "берёт {b} за руку и не отпускает до самого конца прогулки",
                      "self_tpl": "берёт {self} за руку"},
    "помахать": {"emoji": "👋", "text": "машет",
                 "tpl": "машет {b} рукой так старательно, что оборачивается пол-улицы",
                 "self_tpl": "машет {self_dat} в отражении"},
    "поцеловать": {"emoji": "😘", "text": "целует",
                   "self_tpl": "целует {self}"},
    "обнять в сон": {"emoji": "😴", "text": "обнимает перед сном",
                     "tpl": "обнимает {b} перед сном, укрывая одеялом до самых ушей",
                     "self_tpl": "обнимает {self} перед сном"},
    "поцеловать в щёку": {"emoji": "😚", "text": "целует в щёку",
                          "tpl": "целует {b} в щёку и довольно улыбается",
                          "self_tpl": "целует {self} в щёку"},
    "прижать к себе": {"emoji": "🤗", "text": "прижимает к себе",
                       "tpl": "прижимает к себе {b} и не спешит отпускать",
                       "self_tpl": "прижимает к себе {self}"},
    "подарить подарок": {"emoji": "🎁", "text": "дарит подарок",
                         "tpl": "вручает {b} подарок в шуршащей упаковке и ждёт реакции",
                         "self_tpl": "дарит подарок {self_dat}"},
    "написать сообщение": {"emoji": "💌", "text": "пишет сообщение",
                           "tpl": "пишет {b} сообщение и трижды перечитывает "
                                  "перед отправкой",
                           "self_tpl": "пишет сообщение {self_dat}"},
    # ---- Остальные команды: тексты-сценки ----
    "прижать": {"emoji": "🤗", "text": "крепко прижимает к себе",
                "tpl": "крепко прижимает к себе {b}, уткнувшись носом в плечо",
                "self_tpl": "крепко прижимает к себе {self}"},
    "погладить": {"emoji": "🖐️", "text": "гладит по голове",
                  "tpl": "гладит {b} по голове, будто самого близкого человека",
                  "self_tpl": "гладит {self} по голове"},
    "укусить": {"emoji": "😈", "text": "игриво кусает",
                "tpl": "игриво кусает {b} за плечо и довольно щурится",
                "self_tpl": "игриво кусает {self} за плечо"},
    "шлепнуть": {"emoji": "🍑", "text": "шлёпает",
                 "tpl": "со смехом шлёпает {b} и делает вид, что ничего не было",
                 "self_tpl": "шлёпает {self} и делает вид, что ничего не было"},
    "держать за руку": {"emoji": "🤝", "text": "берёт за руку",
                        "tpl": "берёт {b} за руку и переплетает пальцы",
                        "self_tpl": "берёт {self} за руку"},
    "укрыть пледом": {"emoji": "🛌", "text": "укрывает пледом",
                      "tpl": "укрывает {b} тёплым пледом и подтыкает краешек",
                      "self_tpl": "укрывает {self} тёплым пледом"},
    "накормить": {"emoji": "🍰", "text": "угощает вкусненьким",
                  "tpl": "угощает {b} вкусненьким, следя, чтобы добавка была",
                  "self_tpl": "угощает вкусненьким {self}"},
    "станцевать": {"emoji": "💃", "text": "приглашает на танец",
                   "tpl": "приглашает {b} на танец, галантно подав руку",
                   "self_tpl": "приглашает {self} на танец"},
    "поцеловать в лоб": {"emoji": "😌", "text": "целует в лоб",
                         "tpl": "целует {b} в лоб — тихо, по-домашнему",
                         "self_tpl": "целует {self} в лоб"},
    "чмокнуть": {"emoji": "💋", "text": "чмокает",
                 "tpl": "звонко чмокает {b} в щёку",
                 "self_tpl": "чмокает {self} в щёку"},
    "обнять крепко": {"emoji": "🫂", "text": "обнимает так крепко, что не отпускает",
                      "tpl": "обнимает {b} так крепко, что не отпускает",
                      "self_tpl": "обнимает {self} так крепко, что не отпускает"},
    "приобнять": {"emoji": "🫶", "text": "приобнимает за плечи",
                  "tpl": "приобнимает {b} за плечи, притягивая к себе",
                  "self_tpl": "приобнимает {self} за плечи"},
    "погладить по волосам": {"emoji": "💆", "text": "перебирает волосы",
                             "tpl": "перебирает волосы {b}, заправляя прядь за ухо",
                             "self_tpl": "перебирает свои волосы"},
    "щекотать": {"emoji": "🤭", "text": "щекочет",
                 "tpl": "щекочет {b}, пока оба не начинают смеяться",
                 "self_tpl": "щекочет {self}, пока не начинает смеяться"},
    "подмигнуть": {"emoji": "😉", "text": "подмигивает",
                   "tpl": "подмигивает {b} и улыбается уголком рта",
                   "self_tpl": "подмигивает {self_dat} в зеркале"},
    "укутать": {"emoji": "🧣", "text": "укутывает в шарф",
                "tpl": "укутывает {b} в шарф, оставив видимыми только глаза",
                "self_tpl": "укутывает {self} в шарф"},
    "носить на руках": {"emoji": "👐", "text": "берёт на руки",
                        "tpl": "подхватывает {b} на руки и кружит по комнате",
                        "self_tpl": "подхватывает {self} на руки"},
    "сделать массаж": {"emoji": "💆‍♂️", "text": "делает массаж",
                       "tpl": "берёт {b} за плечи и разминает уставшие мышцы",
                       "self_tpl": "делает массаж {self_dat}"},
    "шептать на ушко": {"emoji": "🤫", "text": "шепчет что-то на ушко",
                        "tpl": "наклоняется и шепчет {b} что-то только для них двоих",
                        "self_tpl": "шепчет {self_dat} что-то только для себя"},
    "подарить цветы": {"emoji": "💐", "text": "дарит букет цветов",
                       "tpl": "протягивает {b} букет цветов, смущённо улыбаясь",
                       "self_tpl": "протягивает {self_dat} букет цветов"},
    "спеть серенаду": {"emoji": "🎶", "text": "поёт серенаду",
                       "tpl": "поёт {b} серенаду, ничуть не стесняясь прохожих",
                       "self_tpl": "поёт серенаду {self_dat}"},
    "дать пять": {"emoji": "🙌", "text": "даёт пять",
                  "tpl": "даёт {b} пять с самым серьёзным видом",
                  "self_tpl": "даёт пять {self_dat}"},
    "чокнуться": {"emoji": "🥂", "text": "чокается бокалом",
                  "tpl": "чокается с {b} бокалом и говорит «за нас»",
                  "self_tpl": "чокается бокалом с {self_ins}"},
    "смотреть фильм": {"emoji": "🎬", "text": "устраивается смотреть фильм",
                       "tpl": "устраивается с {b} смотреть фильм под одним пледом",
                       "self_tpl": "устраивается смотреть фильм в одиночестве"},
    "гулять под луной": {"emoji": "🌙", "text": "зовёт гулять под луной",
                         "tpl": "зовёт {b} гулять под луной, обещая не отпускать руку",
                         "self_tpl": "гуляет под луной в одиночестве"},
    "танцевать вальс": {"emoji": "🩰", "text": "кружит партнёра в вальсе",
                        "tpl": "кружит {b} в вальсе прямо посреди комнаты",
                        "self_tpl": "танцует вальс в одиночестве"},
    "соблазнить": {"emoji": "🔥", "text": "соблазняет, глядя в глаза",
                   "tpl": "соблазняет {b}, глядя прямо в глаза",
                   "self_tpl": "соблазняет {self}, глядя в зеркало"},
    "поцеловать в шею": {"emoji": "🫦", "text": "целует в шею",
                         "tpl": "целует {b} в шею, заставляя мурашек бежать по коже",
                         "self_tpl": "целует {self} в шею"},
    "прикусить за ушко": {"emoji": "😏", "text": "прикусывает за ушко",
                          "tpl": "прикусывает {b} за ушко и тихо смеётся",
                          "self_tpl": "прикусывает {self} за ушко"},
    "обнять сзади": {"emoji": "🫂", "text": "обнимает со спины, прижимаясь",
                     "tpl": "обнимает {b} со спины, прижимаясь щекой к плечу",
                     "self_tpl": "обнимает {self} со спины"},
    "затащить в постель": {"emoji": "🛏️", "text": "утаскивает в постель",
                           "tpl": "утаскивает {b} в постель — подальше от всех",
                           "self_tpl": "утаскивает {self} в постель"},
    "лечь в обнимку": {"emoji": "🌙", "text": "ложится рядом в обнимку",
                       "tpl": "ложится рядом с {b} в обнимку",
                       "self_tpl": "ложится в обнимку с {self_ins}"},
    "провести ночь": {"emoji": "🌃", "text": "проводит вместе всю ночь",
                      "tpl": "проводит с {b} всю ночь без сна",
                      "self_tpl": "проводит ночь наедине с {self_ins}"},
    "снять одежду": {"emoji": "🫣", "text": "медленно снимает одежду",
                     "tpl": "медленно снимает с {b} одежду, не отводя взгляда",
                     "self_tpl": "медленно снимает с себя одежду"},
    "заняться любовью": {"emoji": "💞", "text": "занимается любовью",
                         "tpl": "занимается любовью с {b}, забыв про весь мир",
                         "self_tpl": "остаётся наедине с {self_ins}"},
    "сделать сюрприз": {"emoji": "🎁", "text": "устраивает романтический сюрприз",
                        "tpl": "устраивает {b} романтический сюрприз",
                        "self_tpl": "устраивает романтический сюрприз {self_dat}"},
    # ---- Новые команды (задача 105) ----
    "поговорить по душам": {"emoji": "💬", "text": "разговаривает по душам",
                            "tpl": "уводит {b} в тихий уголок поговорить по душам",
                            "self_tpl": "разговаривает по душам с {self_ins}"},
    "написать смс": {"emoji": "📱", "text": "пишет нежное сообщение",
                     "tpl": "пишет {b} нежное сообщение и ждёт ответа",
                     "self_tpl": "пишет {self_dat} нежное сообщение"},
    "подарить кольцо": {"emoji": "💍", "text": "дарит кольцо",
                        "tpl": "достаёт из кармана коробочку, открывает и "
                               "дарит кольцо {b}",
                        "self_tpl": "достаёт из кармана коробочку и дарит "
                                    "кольцо {self_dat}"},
    "подарить машину": {"emoji": "🚗", "text": "дарит ключи от машины",
                        "tpl": "достаёт ключи и с улыбкой дарит машину {b}",
                        "self_tpl": "достаёт ключи и дарит машину {self_dat}"},
    "совместное фото": {"emoji": "📸", "text": "делает совместное фото",
                        "tpl": "крепко прижимает к себе {b}, целуя, делает фото",
                        "self_tpl": "делает селфи и ставит его на аватарку"},
    "принять вместе ванну": {"emoji": "🚿", "text": "набирает ванну вместе",
                             "tpl": "набирает ванну и приглашает {b} разделить её",
                             "self_tpl": "набирает ванну и нежится в ней в одиночестве"},
    "гулять под дождём": {"emoji": "🌧️", "text": "гуляет под дождём, не боясь промокнуть",
                          "tpl": "гуляет с {b} под дождём, не боясь промокнуть",
                          "self_tpl": "гуляет под дождём, не боясь промокнуть"},
    "уложить спать": {"emoji": "🤗", "text": "укладывает, укрывает и гладит по голове",
                      "tpl": "укладывает {b} спать, укрывает и гладит по голове",
                      "self_tpl": "укладывает {self} спать, укрывает и гладит "
                                  "по голове"},
    "рассказать о себе": {"emoji": "💭", "text": "рассказывает о себе",
                          "tpl": "рассказывает {b} о себе то, что не говорит никому",
                          "self_tpl": "рассказывает {self_dat} о себе"},
    "провести время вдвоём": {"emoji": "🕯️", "text": "проводит время вдвоём",
                              "tpl": "проводит с {b} весь вечер вдвоём, забыв про телефон",
                              "self_tpl": "проводит вечер наедине с {self_ins}"},
}

# ТЗ задачи 104: этот набор — «список всех РП-действий» (команда «все рп»
# показывает его первым, остальные команды идут ниже отдельной группой).
RP_MAIN_ACTIONS = ["обнять", "взять за руку", "помахать", "поцеловать",
                   "обнять в сон", "поцеловать в щёку", "прижать к себе",
                   "подарить подарок", "написать сообщение"]


# ==================== ПОЛ ИГРОКА (задача 105) ====================
# Заказчик: «сейчас у всех «обнимает саму себя» — нужно определять пол».
# Порядок: настройка профиля → Telegram → имя → нейтральная форма.
# Telegram Bot API пол НЕ отдаёт (в объекте User его просто нет), поэтому на
# практике работают настройка профиля (`users.gender`) и список имён.
GENDER_FEMALE = "female"
GENDER_MALE = "male"
GENDER_UNKNOWN = ""

FEMALE_NAMES = frozenset("""
вика ника маша даша юля аня катя лена марина настя света диана алиса валя
галя надя оля юлия яна
""".split())

MALE_NAMES = frozenset("""
макс максим миша михаил дима ник никита коля саша андрей артём артем паша влад
владимир иван игорь серёга серега пётр петр
""".split())

# Как подставлять «себя» в зависимости от пола.
_GENDER_FORMS = {
    GENDER_FEMALE: {"self": "саму себя", "self_dat": "самой себе",
                    "self_ins": "самой собой"},
    GENDER_MALE: {"self": "самого себя", "self_dat": "самому себе",
                  "self_ins": "самим собой"},
    GENDER_UNKNOWN: {"self": "себя", "self_dat": "себе", "self_ins": "собой"},
}


def normalize_gender(value) -> str:
    """Любая запись пола → female / male / '' (не знаем)."""
    text = str(value or "").strip().lower()
    if text in ("female", "f", "ж", "женский", "жен", "девушка", "woman", "girl"):
        return GENDER_FEMALE
    if text in ("male", "m", "м", "мужской", "муж", "парень", "man", "boy"):
        return GENDER_MALE
    return GENDER_UNKNOWN


def gender_from_name(name) -> str:
    """Пол по имени (первое слово, без «ё»): «Вика Петрова» → female.

    Не опознали — пустая строка, и фраза строится нейтрально («обнимает себя»).
    """
    text = str(name or "").strip().lower().replace("ё", "е")
    if not text:
        return GENDER_UNKNOWN
    # Отбрасываем @, эмодзи и всё, что не буква: «@vika_pro» → vika_pro.
    parts = [part for part in text.replace("@", " ").split() if part]
    if not parts:
        return GENDER_UNKNOWN
    first = parts[0]
    token = "".join(ch for ch in first if ch.isalpha() or ch in "-_").strip("-_")
    candidates = [token]
    # «Викуля», «Машенька» — сравниваем и основу без уменьшительных хвостов.
    for suffix in ("юля", "уля", "енька", "очка", "ик", "ка", "ша", "ня", "я"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            candidates.append(token[:-len(suffix)])
    for candidate in candidates:
        if candidate in FEMALE_NAMES:
            return GENDER_FEMALE
        if candidate in MALE_NAMES:
            return GENDER_MALE
    return GENDER_UNKNOWN


def self_forms(gender) -> dict:
    """Формы слова «себя» для подстановки в текст действия."""
    return dict(_GENDER_FORMS.get(normalize_gender(gender), _GENDER_FORMS[GENDER_UNKNOWN]))


def rp_self_text(key, gender=GENDER_UNKNOWN) -> str:
    """Как описать действие, когда игрок один и никого не отметил.

    Задача 104: «🫂 Vika обнимает саму себя» вместо «обнимает».
    Задача 105: «себя» склоняется по полу: «саму себя» / «самого себя» /
    нейтральное «себя».
    """
    meta = RP_ACTIONS.get(str(key or "")) or {}
    template = str(meta.get("self_tpl") or meta.get("text") or "обнимает {self}")
    try:
        return template.format(**self_forms(gender))
    except (KeyError, IndexError):
        return template.replace("{self}", "себя").replace("{self_dat}", "себе") \
                       .replace("{self_ins}", "собой")


def rp_pair_text(key, target_html: str) -> str:
    """Фраза действия для пары: «достаёт коробочку и дарит кольцо {ник}»."""
    meta = RP_ACTIONS.get(str(key or "")) or {}
    template = str(meta.get("tpl") or "")
    if template:
        try:
            return template.format(b=target_html)
        except (KeyError, IndexError):
            return f"{meta.get('text', '')} {target_html}".strip()
    return f"{meta.get('text', '')} {target_html}".strip()


def busy_warning(gender) -> str:
    """«⚠️ Осторожно, она занята…» — без скобок «он(а) занят(а)» (задача 105)."""
    forms = {GENDER_FEMALE: "она занята", GENDER_MALE: "он занят",
             GENDER_UNKNOWN: "человек занят"}
    return (f"⚠️ Осторожно, {forms.get(normalize_gender(gender), 'человек занят')}, "
            f"смотри не разрушь их отношения.")


def stage_index(score, solo: bool = False) -> int:
    """Индекс этапа по накопленным коинам любви (мусор → 0)."""
    try:
        value = max(0, int(float(score or 0)))
    except (TypeError, ValueError):
        value = 0
    for stage in reversed(stages_for(solo)):
        if value >= stage["min"]:
            return stage["index"]
    return 0


def stage_info(score, solo: bool = False) -> dict:
    """Полная карточка этапа: границы, название, награда и прогресс."""
    index = stage_index(score, solo)
    stage = stages_for(solo)[index]
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


def next_stage(score, solo: bool = False):
    """Следующий этап (или None, если уровень уже финальный)."""
    stages = stages_for(solo)
    index = stage_index(score, solo)
    if index + 1 >= len(stages):
        return None
    return dict(stages[index + 1])


def stages_reached(previous_score, new_score, solo: bool = False) -> list:
    """Этапы, которые перешагнули за одно действие: за каждый — награда."""
    stages = stages_for(solo)
    previous_index = stage_index(previous_score, solo)
    new_index = stage_index(new_score, solo)
    return [dict(stage) for stage in stages[previous_index + 1:new_index + 1]]


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
def public_stage(score, solo: bool = False) -> dict:
    """Уровень без наград: index, title, love, progress.

    Задача 55: добавляем «сколько до следующего уровня». Без этого игрок видел
    «Уровень 1 · Случайное знакомство · Love 0» и не понимал, что вообще
    двигает уровень: казалось, что счёт не растёт и наград нет.
    Задача 104: у одиночек своя ветка названий (SOLO_STAGES).
    """
    info = stage_info(score, solo)
    stages = stages_for(solo)
    index = info["index"]
    current = stages[index]
    nxt = stages[index + 1] if index + 1 < len(stages) else None
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
        # Задача 104: название следующего уровня — секрет, наружу не отдаём.
        "next_title": "",
        # Сколько Love осталось до следующего уровня (None — максимальный).
        "to_next": max(0, nxt["min"] - info["score"]) if nxt else None,
    }


def public_stages(solo: bool = False) -> list:
    """Список уровней для клиента: номер и «сколько нужно».

    Задача 104: названия уровней — секрет (ТЗ: «не раскрывать заранее ни в
    сообщениях, ни в информации, ни в топе»). Поэтому в публичном списке
    остаются только номера и границы, `title` отдаём пустым — ключ сохранён,
    чтобы старые сборки Mini App не падали.
    """
    return [{"level": stage["index"] + 1, "index": stage["index"],
             "title": "", "need": stage["max"], "min": stage["min"]}
            for stage in stages_for(solo)]


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
    """«➡️ До уровня 3 — 320/500❤️» — прогресс до следующего уровня.

    Задача 104 (ТЗ): формат ровно такой — сколько накоплено / сколько нужно
    до следующего уровня (для пар — ❤️, для одиночек — 💔).
    """
    info = public_stage(score, solo)
    heart = "💔" if solo else "❤️"
    if info["next_level"] is None:
        return f"💞 Максимальный уровень — {info['love']}{heart}"
    return (f"➡️ До уровня {info['next_level']} — "
            f"{info['love']}/{int(info['need'] or 0)}{heart}")


def level_info_text(score, solo: bool = False) -> str:
    """«➡️ До уровня 4: ещё 180 ❤️» — строка для «любовь инфа»."""
    info = public_stage(score, solo)
    heart = "💔" if solo else "❤️"
    if info["next_level"] is None:
        return "💞 Это финальный уровень — счёт растёт дальше"
    return f"➡️ До уровня {info['next_level']}: ещё {info['to_next']} {heart}"


def _coins_fmt(amount) -> str:
    """«1 000» — коины с пробелом между тысячами (так читается на телефоне)."""
    value = _safe_int(amount, 0)
    return f"{value:,}".replace(",", " ")


def _coin_word(amount) -> str:
    """«1 коин» / «2 коина» / «5 коинов» — как в остальных текстах бота."""
    value = _safe_int(amount, 0)
    tail = abs(value) % 100
    if 11 <= tail <= 19:
        return "коинов"
    tail %= 10
    return "коин" if tail == 1 else ("коина" if 2 <= tail <= 4 else "коинов")


def celebration_text(stage, amount_per_partner=0, solo: bool = False,
                     partner_name: str = "", balance=None,
                     next_stage=None) -> str:
    """ПОЗДРАВЛЕНИЕ о новом уровне (формат из ТЗ задачи 104).

        🎉 ПОЗДРАВЛЯЕМ!

        Вы перешли на 3 уровень отношений

        💗 «Близость»

        🎁 Награда: +3 000 коинов — уже на балансе

        💰 Теперь на балансе: 4 544 коинов

        ➡️ Следующий уровень — 4, нужно 500 ❤️

    Для одиночки — «3 уровень одиночества» и 💔. Названия и суммы СЛЕДУЮЩЕГО
    уровня не показываем (секретная механика): только номер и сколько нужно.
    """
    stage = stage or {}
    index = _safe_int(stage.get("index", stage.get("stage")), 0)
    level = index + 1
    title = str(stage.get("title") or "")
    amount = _safe_int(amount_per_partner, 0) or _safe_int(stage.get("reward"), 0)
    heart = "💔" if solo else "❤️"
    kind = "одиночества" if solo else "отношений"

    lines = ["🎉 <b>ПОЗДРАВЛЯЕМ!</b>", "",
             f"Вы перешли на <b>{level} уровень {kind}</b>", ""]
    if title:
        lines += [f"{heart} «{title}»", ""]
    if amount > 0:
        lines += [f"🎁 Награда: <b>+{_coins_fmt(amount)} {_coin_word(amount)}</b> — "
                  f"уже на балансе", ""]
    if balance is not None:
        lines += [f"💰 Теперь на балансе: <b>{_coins_fmt(balance)} "
                  f"{_coin_word(balance)}</b>", ""]
    nxt_level = _safe_int((next_stage or {}).get("level"), 0)
    if nxt_level:
        need = _safe_int((next_stage or {}).get("love_need"), 0)
        line = f"➡️ Следующий уровень — <b>{nxt_level}</b>"
        if need > 0:
            line += f", нужно {_coins_fmt(need)} {heart}"
        lines.append(line)
    else:
        lines.append(f"💞 Это финальный уровень — счёт растёт дальше {heart}")
    return "\n".join(lines).rstrip()


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
    # Многословные команды («держать за руку», «поцеловать в щёку») проверяем
    # раньше однословных, иначе «поцеловать в шею» превращалось в «поцеловать».
    ordered = sorted(RP_ACTIONS, key=lambda item: len(item.split()), reverse=True)
    # Задача 105: «ё» и «е» — игроки пишут по-разному («в щёку» / «в щеку»),
    # поэтому сравниваем сразу в «е»-виде, не теряя приоритет длинных команд.
    flat = raw.replace("ё", "е")
    for key in ordered:
        if raw.startswith(key) or flat.startswith(key.replace("ё", "е")):
            return key, RP_ACTIONS[key]
    return None, None


# ==================== НАГРАДЫ ЗА ЭТАПЫ ====================
# Начисление вынесено в общий модуль: и бот (после РП-действия), и API
# (при открытии Mini App) вызывают одну и ту же функцию, поэтому награда не
# может быть выдана дважды или не выдана вовсе.
LOVE_REWARDS_TABLE = "love_stage_rewards"
# Задача 83: отметка «ЭТОТ игрок в ЭТОЙ паре за ЭТОТ уровень приз уже получил».
# Старая таблица love_stage_rewards помнит только пару (relationship_id, stage)
# и не говорит, дошло ли деньги до КАЖДОГО партнёра: если одному начисление
# сорвалось, второй всё равно записан в «выплачено», и первый не получал уже
# никогда. Теперь отметка стоит на каждого человека отдельно — недоплаченный
# получает приз при следующем действии (профиль/РП), а второй второй раз не
# получит.
LOVE_MEMBER_REWARDS_TABLE = "love_stage_members"
# Задача 84: журнал фактических начислений — единственный источник правды о
# том, что награда действительно пришла на баланс.
LOVE_LOG_TABLE = "love_rewards_log"
# Есть ли таблица в базе. None — ещё не проверяли; False — миграция не
# применена, тогда работаем по старой схеме (иначе награды вообще перестали бы
# выдаваться на базе без миграции).
_MEMBER_TABLE_READY = None
# Когда последний раз проверяли таблицу. Если её нет — перепроверяем раз в
# минуту: человек может применить миграцию, пока бот уже работает, и ждать
# рестарта Render он не должен.
_MEMBER_TABLE_CHECKED_AT = 0.0
_MEMBER_TABLE_RETRY = 60.0


def paid_stages(supabase, relationship_id) -> set:
    try:
        res = (supabase.table(LOVE_REWARDS_TABLE).select("stage")
               .eq("relationship_id", int(relationship_id)).execute())
    except Exception:
        return set()
    return {_safe_int(row.get("stage"), -1) for row in (res.data or [])}


def member_rewards_ready(supabase) -> bool:
    """Можно ли писать отметки персонально (миграция 20261204000000 применена).

    Проверяем не только наличие таблицы, но и право ЗАПИСИ: если таблица есть,
    а политика RLS не даёт insert, раньше каждое начисление «падало» на
    отметке и награда молча не выдавалась — со стороны это выглядело как
    «награды нету». Теперь такая база переводится на старую схему (награды
    выдаются), а в лог пишется, что применить.

    Ответ кэшируется: True — навсегда, False — с перепроверкой раз в минуту
    (миграцию могут применить на работающем боте, рестарт не нужен).
    """
    global _MEMBER_TABLE_READY, _MEMBER_TABLE_CHECKED_AT
    now = time.time()
    if _MEMBER_TABLE_READY is True:
        return True
    if _MEMBER_TABLE_READY is False and now - _MEMBER_TABLE_CHECKED_AT < _MEMBER_TABLE_RETRY:
        return False
    _MEMBER_TABLE_CHECKED_AT = now
    if _can_write_rewards(supabase, LOVE_MEMBER_REWARDS_TABLE):
        if _MEMBER_TABLE_READY is not True:
            logger.info("Награды за уровни: персональные отметки (%s) работают.",
                        LOVE_MEMBER_REWARDS_TABLE)
        _MEMBER_TABLE_READY = True
    else:
        if _MEMBER_TABLE_READY is not True:
            logger.warning("Таблица %s недоступна для записи — награды выдаём по "
                           "старой схеме; примените миграцию "
                           "20261204000000_love_rewards_per_player.sql",
                           LOVE_MEMBER_REWARDS_TABLE)
        _MEMBER_TABLE_READY = False
    return _MEMBER_TABLE_READY


def _can_write_rewards(supabase, table: str) -> bool:
    """Можно ли писать в таблицу отметок (нет таблицы / нет прав → False).

    Проба безвредная: берём несуществующую пару (id 0) и сразу её удаляем.
    """
    probe = {"relationship_id": 0, "user_id": 0, "stage": 0}
    if table == SOLO_REWARDS_TABLE:
        probe = {"user_id": 0, "stage": 0}
    try:
        supabase.table(table).insert(dict(probe)).execute()
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if "duplicate" in text or "23505" in text:
            return True                     # строка уже есть — таблица и права в порядке
        return False
    try:
        query = supabase.table(table).delete()
        for column, value in probe.items():
            query = query.eq(column, value)
        query.execute()
    except Exception:  # noqa: BLE001 — убрать пробную строку не смогли, не критично
        pass
    return True


def _member_mark(supabase, relationship_id, user_id, stage_index) -> bool:
    """Отметка «этому игроку этот уровень в этой паре оплачен».

    Ставится ДО начисления: два одновременных действия не оплатят уровень
    дважды (второй insert упадёт на первичном ключе и просто не заплатит).
    """
    try:
        supabase.table(LOVE_MEMBER_REWARDS_TABLE).insert({
            "relationship_id": int(relationship_id),
            "user_id": int(user_id),
            "stage": int(stage_index),
        }).execute()
        return True
    except Exception:
        return False


def member_paid_stages(supabase, relationship_id, user_id) -> set:
    """Какие уровни ЭТОТ игрок в ЭТОЙ паре реально получил.

    Задача 84, главное правило: **журнал `love_rewards_log` — единственная
    правда**. Он пишется только после того, как коины легли на баланс.

    Почему не по отметкам: отметка `love_stage_members` могла остаться от
    старого бага, неудачной миграции или прошлого сбоя, при котором деньги не
    дошли, — и уровень навсегда считался оплаченным. Именно из-за этого игроки
    видели «награды нету». Поэтому:
      • журнал читается — учитываем ТОЛЬКО его (старые отметки не мешают
        выдать награду; система один раз догонит всё, что должно);
      • журнала нет (миграция не применена) — падаем на отметки, чтобы не
        платить одно и то же на каждое действие.
    """
    relationship_id = _safe_int(relationship_id, 0)
    uid = _safe_int(user_id, 0)
    if relationship_id <= 0 or uid <= 0:
        return set()
    try:
        res = (supabase.table(LOVE_LOG_TABLE).select("stage")
               .eq("relationship_id", relationship_id).eq("user_id", uid).execute())
        return {_safe_int(row.get("stage"), -1) for row in (res.data or [])}
    except Exception as exc:  # noqa: BLE001 — журнала нет: работаем по отметкам
        logger.info("Журнал наград недоступен (%s) — сверяюсь с отметками "
                    "(примените миграцию 20261205000000_love_rewards_log.sql)", exc)
    try:
        res = (supabase.table(LOVE_MEMBER_REWARDS_TABLE).select("stage")
               .eq("relationship_id", relationship_id).eq("user_id", uid).execute())
        return {_safe_int(row.get("stage"), -1) for row in (res.data or [])}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Отметки наград пары %s игрока %s не читаются (%s) — "
                       "считаем, что уровень не оплачен", relationship_id, uid, exc)
        return set()


def log_reward(supabase, relationship_id, user_id, stage_index, reward) -> bool:
    """Записать в журнал ФАКТ начисления (вызывается после успешной оплаты)."""
    try:
        supabase.table(LOVE_LOG_TABLE).insert({
            "relationship_id": _safe_int(relationship_id, 0),
            "user_id": _safe_int(user_id, 0),
            "stage": _safe_int(stage_index, 0),
            "reward": _safe_int(reward, 0),
        }).execute()
        return True
    except Exception as exc:  # noqa: BLE001 — журнал не критичен для награды
        logger.info("Журнал наград не записался (%s): %s", LOVE_LOG_TABLE, exc)
        return False


# Задача 84: страховка в памяти процесса. Если база не даёт записать ни журнал,
# ни отметку (нет таблиц/RLS), единственный способ не выдать награду дважды за
# одно и то же действие — помнить это здесь. Ключ: (пара, игрок) → {уровни}.
_MEMORY_PAID: dict = {}
_MEMORY_PAID_LIMIT = 5000       # чтобы не расти бесконечно на большом боте


def _memory_paid_stages(relationship_id, user_id) -> set:
    return set(_MEMORY_PAID.get((_safe_int(relationship_id, 0), _safe_int(user_id, 0)), set()))


def _memory_mark_paid(relationship_id, user_id, stage_index) -> None:
    key = (_safe_int(relationship_id, 0), _safe_int(user_id, 0))
    stages = _MEMORY_PAID.setdefault(key, set())
    stages.add(_safe_int(stage_index, -1))
    if len(_MEMORY_PAID) > _MEMORY_PAID_LIMIT:
        for old_key in list(_MEMORY_PAID)[:500]:
            _MEMORY_PAID.pop(old_key, None)


def paid_stage_owners(supabase, relationship_id) -> set:
    """Кому из пары какие уровни реально оплачены: {(user_id, stage)}.

    Нужно, чтобы не задваивать начисление, когда отметок нет, а журнал есть.
    """
    relationship_id = _safe_int(relationship_id, 0)
    if relationship_id <= 0:
        return set()
    owners = set()
    try:
        res = (supabase.table(LOVE_LOG_TABLE).select("user_id, stage")
               .eq("relationship_id", relationship_id).execute())
        owners |= {(_safe_int(row.get("user_id"), 0), _safe_int(row.get("stage"), -1))
                   for row in (res.data or [])}
    except Exception:  # noqa: BLE001
        pass
    try:
        res = (supabase.table(LOVE_MEMBER_REWARDS_TABLE).select("user_id, stage")
               .eq("relationship_id", relationship_id).execute())
        owners |= {(_safe_int(row.get("user_id"), 0), _safe_int(row.get("stage"), -1))
                   for row in (res.data or [])}
    except Exception:  # noqa: BLE001
        pass
    return owners


def _mark_ignoring_duplicate(supabase, table: str, match: dict) -> bool:
    """Поставить отметку «уровень оплачен» ДО начисления.

    Задача 84: тут была главная причина «наград нету». Отметка ставилась
    безусловно, и если такая строка уже была в базе (повтор после сбоя,
    миграция перенесла старые отметки, гонка двух действий) — insert падал на
    первичном ключе, а вызывающий код считал это «уровень уже оплачен» и
    молча пропускал награду. Теперь «нарушение уникальности» = отметка стоит,
    а всё остальное (нет таблицы, нет прав RLS) НЕ блокирует начисление:
    деньги игроку дороже отметки в служебной таблице.
    """
    try:
        supabase.table(table).insert(dict(match)).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if "duplicate" in text or "23505" in text or "уже существует" in text:
            return True
        logger.warning("Отметку уровня в %s поставить не удалось (%s) — награду "
                       "всё равно начислим", table, exc)
        return False


def _member_unmark(supabase, relationship_id, user_id, stage_index) -> None:
    """Начисление не удалось — снимаем отметку, повторим позже."""
    try:
        (supabase.table(LOVE_MEMBER_REWARDS_TABLE).delete()
         .eq("relationship_id", int(relationship_id))
         .eq("user_id", int(user_id))
         .eq("stage", int(stage_index)).execute())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Отметку уровня %s игрока %s снять не удалось: %s",
                       stage_index, user_id, exc)


def _remember_stage(supabase, relationship_id, stage_index) -> None:
    """Совместимость: старые сборки читают факт выплаты из love_stage_rewards."""
    try:
        supabase.table(LOVE_REWARDS_TABLE).insert({
            "relationship_id": int(relationship_id), "stage": int(stage_index),
        }).execute()
    except Exception:
        pass          # запись уже есть (или таблицы нет) — это не ошибка


def _coin_rows(supabase, user_id) -> list:
    """Все строки профиля игрока (в users бывают дубли с одним user_id)."""
    uid = _safe_int(user_id, 0)
    rows = []
    for key in (uid, str(uid)):
        try:
            res = supabase.table("users").select("*").eq("user_id", key).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось прочитать users по user_id=%s: %s", key, exc)
            continue
        rows.extend(row for row in (res.data or []) if isinstance(row, dict))
        if rows:
            break
    return rows


def _main_coin_row(rows):
    """Строка, которую ИГРОК видит в профиле: максимум коинов.

    Задача 83: раньше начисление брало `data[0]` — в базе бывают дубли с одним
    user_id, и деньги могли уйти в «мёртвую» строку с нулём, а профиль
    показывает строку с максимумом коинов. Игрок видел «награда начислена» и
    ноль на балансе — «наград нету». Теперь и читаем, и пишем в ту же строку,
    что и профиль (как bot._primary_user_row и api._find_user_rows).
    """
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return None
    return max(rows, key=lambda row: _safe_int(row.get("coins"), 0))


def _update_coins(supabase, uid, row, target, amount) -> bool:
    """Записать новый баланс, перебрав все разумные идентификаторы строки.

    Задача 105: заказчик написал «проверь, что при начислении используется
    правильный идентификатор пользователя — награда не доходит именно из-за
    этого». В базе `users.user_id` бывает числом и строкой, а строк профиля —
    несколько (дубли). Раньше писали по `id` строки, если колонка есть, и
    ТОЛЬКО по `user_id` иначе: если тип колонки не совпадал с типом id
    (bigint ↔ text), UPDATE не находил ни одной строки, обратное чтение
    показывало старый баланс — и награда «не начислялась».

    Теперь пробуем по очереди: `id` строки, `user_id` числом, `user_id`
    строкой. Первый удачный вариант завершает запись.
    """
    variants = []
    row_id = _safe_int((row or {}).get("id"), 0)
    if row_id > 0:
        variants.append(("id", row_id))
    variants.append(("user_id", int(uid)))
    variants.append(("user_id", str(uid)))
    for column, value in variants:
        try:
            supabase.table("users").update({"coins": target}).eq(column, value).execute()
        except Exception as exc:  # noqa: BLE001
            logger.info("Коины %s: update по %s=%s не прошёл: %s",
                        uid, column, value, exc)
            continue
        saved = _main_coin_row(_coin_rows(supabase, uid))
        if _safe_int((saved or {}).get("coins"), 0) >= target:
            return True
    return False


def _create_coin_row(supabase, uid: int) -> bool:
    """Создать строку профиля с нулём коинов (награда должна дойти)."""
    payload = {"user_id": int(uid), "coins": 0}
    for extra in ({}, {"keys": 0}, {"keys": 0, "inventory": "[]"}):
        try:
            supabase.table("users").insert(dict(payload, **extra)).execute()
        except Exception as exc:  # noqa: BLE001 — колонок может не быть
            logger.info("Строка профиля %s не создалась (%s): %s",
                        uid, list(extra), exc)
            continue
        if _coin_rows(supabase, uid):
            return True
    return False


def _credit_coins(supabase, user_id, amount: int, attempts: int = 3) -> bool:
    """Начислить коины с повтором и проверкой. False — не смогли (награду повторим).

    Задача 62: раньше сбой одного `update` молча проглатывался, а этап уже был
    отмечен выплаченным — награда терялась навсегда.
    Задача 83: пишем в ту же строку профиля, что видит игрок (максимум коинов),
    и после записи ПЕРЕЧИТЫВАЕМ баланс. Если прибавки нет — пробуем ещё раз,
    чтобы «начислено» в сообщении и баланс в профиле всегда совпадали.
    Задача 105: перебор идентификаторов (см. _update_coins) + понятный лог,
    чтобы «награда не пришла» больше не было загадкой.
    """
    uid = _safe_int(user_id, 0)
    amount = _safe_int(amount, 0)
    if uid <= 0 or amount <= 0:
        logger.warning("Начисление отменено: игрок=%s сумма=%s", user_id, amount)
        return False
    for attempt in range(max(1, attempts)):
        rows = _coin_rows(supabase, uid)
        row = _main_coin_row(rows)
        if not row:
            # Задача 105: у партнёра может не быть строки профиля (он ещё не
            # заходил в аппку) — раньше из-за этого награда «не доходила»
            # навсегда. Создаём минимальную строку и пробуем снова.
            if attempt == 0 and _create_coin_row(supabase, uid):
                continue
            logger.warning("Начисление %s коинов игроку %s: нет строки в users",
                           amount, uid)
            return False
        before = _safe_int(row.get("coins"), 0)
        target = before + amount
        if _update_coins(supabase, uid, row, target, amount):
            logger.info("Начислено %s коинов игроку %s: %s → %s",
                        amount, uid, before, target)
            return True
        logger.warning("Коины игроку %s не легли в базу (попытка %s): было %s, ждали %s",
                       uid, attempt + 1, before, target)
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


def stage_crossed_any(previous_score, new_score) -> bool:
    """Перешли ли мы новую границу уровня этим действием."""
    return stage_index(previous_score) != stage_index(new_score)


def claim_rewards_after_action(supabase, relationship, previous_score, new_score) -> list:
    """Награды СРАЗУ после РП-действия (задача 84).

    Заказчик: «начисляй в любом случае при повышении». Здесь два независимых
    пути, чтобы награда не могла «не прийти»:
      1) уровень пары изменился этим действием — платим обязательно (даже если
         запись нового счёта в базу «не подтвердилась»);
      2) общий путь claim_stage_rewards — он же закрывает старые долги.
    Событие возвращается один раз на игрока и уровень.
    """
    granted = []
    if stage_crossed_any(previous_score, new_score) and relationship:
        row = dict(relationship)
        row["love"] = max(_safe_int(previous_score, 0), _safe_int(new_score, 0))
        granted.extend(claim_stage_rewards(supabase, row) or [])
    return granted


def claim_stage_rewards(supabase, relationship) -> list:
    """Награды за достигнутые этапы: каждому партнёру, ровно один раз на этап.

    Задача 83: начисление персональное — своя отметка на игрока.
    Задача 84 (жалоба «ничего не начисляется, начисляй в любом случае»):
    начисление больше НЕ может быть пропущено из-за служебных таблиц. Раньше
    любая из трёх вещей молча съедала награду:
      • отметка ставилась insert-ом, и «duplicate key» (после миграции/повтора)
        означал «уже оплачено» → приз не выдавался никогда;
      • если таблицы отметок нет или политика RLS не даёт запись — тоже
        «continue», и награда не начислялась;
      • начисление шло только до текущего уровня пары; после обнуления Love
        долг за прошлые уровни уже не закрывался.
    Теперь порядок такой: деньги → отметка. Отметка нужна только чтобы не
    заплатить дважды; если её поставить нельзя, платим всё равно (лучше
    награда без отметки, чем игрок без награды).

    Уровни платим за всё, что пара прошла: от 2-го до текущего, а долг по
    текущей паре закрываем на любом значении Love (см. love_rewards_gap).
    """
    if not relationship:
        return []
    relationship_id = _safe_int(relationship.get("id"), 0)
    if relationship_id <= 0:
        return []
    score = _safe_int(relationship.get("love"), 0)
    current_index = stage_index(score)
    partners = []
    for key in ("user_a", "user_b"):
        uid = _safe_int(relationship.get(key), 0)
        if uid > 0 and uid not in partners:
            partners.append(uid)
    if not partners:
        return []
    per_player = member_rewards_ready(supabase)
    # Кто что РЕАЛЬНО получил: журнал начислений (+ старые отметки) и память
    # процесса. Память нужна, если база вообще не даёт писать служебные
    # строки — иначе при сбое начисления платили бы за уровень каждое действие.
    member_paid = {pid: member_paid_stages(supabase, relationship_id, pid)
                   for pid in partners}
    for pid in partners:
        member_paid[pid] |= _memory_paid_stages(relationship_id, pid)
    legacy_paid = set() if per_player else paid_stages(supabase, relationship_id)
    granted = []
    for stage in LOVE_STAGES:
        index = _safe_int(stage.get("index"), -1)
        # Задача 72: 1-й уровень не оплачивается — платить нечего.
        if not stage_is_paid(stage):
            continue
        if index > current_index:
            continue          # пара до этого уровня ещё не дошла
        reward = int(stage.get("reward") or 0)
        if per_player:
            for partner_id in partners:
                if index in member_paid.get(partner_id, set()):
                    continue          # этот игрок уровень уже получил
                # Свежая проверка перед начислением: между чтением и записью
                # мог отработать второй апдейт (кнопка + сообщение).
                if index in member_paid_stages(supabase, relationship_id, partner_id) \
                        or index in _memory_paid_stages(relationship_id, partner_id):
                    member_paid.setdefault(partner_id, set()).add(index)
                    continue
                marked = _mark_ignoring_duplicate(
                    supabase, LOVE_MEMBER_REWARDS_TABLE,
                    {"relationship_id": relationship_id, "user_id": partner_id,
                     "stage": index})
                if not _credit_coins(supabase, partner_id, reward):
                    # Деньги не дошли ЭТОМУ партнёру — снимаем его отметку,
                    # награда выдастся при следующем действии. Второй партнёр
                    # свою награду сохраняет.
                    if marked:
                        _member_unmark(supabase, relationship_id, partner_id, index)
                    logger.warning("Награда за этап %s игроку %s (пара %s) не "
                                   "начислена — повторим позже",
                                   index, partner_id, relationship_id)
                    continue
                member_paid.setdefault(partner_id, set()).add(index)
                log_reward(supabase, relationship_id, partner_id, index, reward)
                _memory_mark_paid(relationship_id, partner_id, index)
                _remember_stage(supabase, relationship_id, index)
                granted.append({"stage": index, "title": stage["title"],
                                "reward": stage["reward"], "user_id": partner_id})
        else:
            # Старая схема (миграции нет): одна отметка на пару. Её отсутствие
            # больше не блокирует начисление — см. _mark_ignoring_duplicate.
            if index in legacy_paid:
                continue
            marked = _mark_ignoring_duplicate(
                supabase, LOVE_REWARDS_TABLE,
                {"relationship_id": relationship_id, "stage": index})
            ok = True
            for partner_id in partners:
                if not _credit_coins(supabase, partner_id, reward):
                    ok = False
                    continue
                # Даже в старой схеме пишем, КОМУ пришли деньги: иначе сообщения
                # «награда каждому» уходят тому, кто её не получил.
                log_reward(supabase, relationship_id, partner_id, index, reward)
                _memory_mark_paid(relationship_id, partner_id, index)
                granted.append({"stage": index, "title": stage["title"],
                                "reward": stage["reward"], "user_id": partner_id})
            if not ok:
                logger.warning("Награда за этап %s пары %s начислена не всем — "
                               "повторим позже", index, relationship_id)
                if marked:
                    _unmark_stage(supabase, LOVE_REWARDS_TABLE,
                                  {"relationship_id": relationship_id}, index)
                continue
    return granted


def love_rewards_gap(supabase, relationship) -> dict:
    """Что игроку ещё НЕ заплатили по этой паре (для честного «догона»).

    Задача 84: результат не зависит от текущего счёта Love — если уровень был
    пройден, а деньги не дошли (сбой базы, старая схема отметок, миграция),
    приз всё равно положен. Возвращает {user_id: [индексы уровней]}.
    """
    if not relationship:
        return {}
    relationship_id = _safe_int(relationship.get("id"), 0)
    if relationship_id <= 0:
        return {}
    partners = []
    for key in ("user_a", "user_b"):
        uid = _safe_int(relationship.get(key), 0)
        if uid > 0 and uid not in partners:
            partners.append(uid)
    if not partners:
        return {}
    per_player = member_rewards_ready(supabase)
    legacy_paid = set() if per_player else paid_stages(supabase, relationship_id)
    current_index = stage_index(_safe_int(relationship.get("love"), 0))
    gaps = {}
    for partner_id in partners:
        paid = member_paid_stages(supabase, relationship_id, partner_id) if per_player else legacy_paid
        missing = []
        for stage in LOVE_STAGES:
            index = _safe_int(stage.get("index"), -1)
            if not stage_is_paid(stage) or index > current_index:
                continue
            if index not in paid:
                missing.append(index)
        if missing:
            gaps[partner_id] = missing
    return gaps


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
    """Вступление в отношения: одиночный счёт сгорает, а выплаченные призы — нет.

    Задача 83: раньше здесь просто удалялась строка `solo_love`, и вместе с
    ней исчезал ВЕСЬ прогресс одиночки. Отметки в `solo_love_stage_rewards`
    при этом оставались — человек после «пара → развод → снова один» начинал
    с нуля Unlove, но призы за уже пройденные уровни больше не получал:
    «кто раньше получал — не получает». Заказчик попросил так не делать.

    Теперь прогресс не сгорает: счёт одиночки сохраняется (и продолжает
    качаться, если человек снова останется один), поэтому уровни и призы,
    которые он заработал, остаются при нём.
    """
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return False
    try:
        # Строку НЕ удаляем: читаем и, если её нет, создаём с нулём. Смысл
        # вызова теперь — «подтвердить, что одиночная ветка за игроком».
        current = _safe_int(solo_state(supabase, uid).get("love"), 0)
        supabase.table(SOLO_LOVE_TABLE).upsert(
            {"user_id": uid, "love": current}, on_conflict="user_id").execute()
        return True
    except Exception:
        return False


# ==================== ПЕРЕХОД МЕЖДУ ВЕТКАМИ (задача 105) ====================
# ТЗ: «в топ отношений игрок остаётся в списке одиноких после создания пары».
# Причина была в том, что одиночный счёт жил сам по себе, а отметки наград —
# тем более. Теперь переходы чистые:
#   • создали пару → одиночный прогресс и отметки наград СГОРАЮТ у обоих,
#     прогресс пары начинается с нуля (новая строка relationships с love=0);
#   • расстались → человек уходит в одиночки с нулём (уровень 1), отметки
#     наград одиночной ветки пустые, прошлое значение не подтягивается.
# Заработанные коины при этом не отбираются — они уже на балансе.
def _clear_memory_rewards(user_id) -> None:
    """Забыть «в памяти процесса» оплаченные уровни этого игрока."""
    uid = _safe_int(user_id, 0)
    for key in [key for key in list(_MEMORY_PAID) if _safe_int(key[1], 0) == uid]:
        _MEMORY_PAID.pop(key, None)


def _delete_marks(supabase, table: str, user_id) -> bool:
    """Удалить отметки наград игрока (таблицы может не быть — не падаем)."""
    try:
        supabase.table(table).delete().eq("user_id", int(user_id)).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.info("Отметки наград %s игрока %s не очистились: %s",
                    table, user_id, exc)
        return False


def reset_solo_branch(supabase, user_id) -> bool:
    """Создание пары: одиночный прогресс и его награды сгорают полностью."""
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return False
    ok = True
    try:
        supabase.table(SOLO_LOVE_TABLE).delete().eq("user_id", uid).execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Одиночный счёт %s не обнулился: %s", uid, exc)
        ok = False
    ok = _delete_marks(supabase, SOLO_REWARDS_TABLE, uid) and ok
    # Журнал одиночных начислений (relationship_id = 0) тоже чистим: иначе
    # «догон» наград будет считать старые уровни оплаченными.
    try:
        (supabase.table(LOVE_LOG_TABLE).delete()
         .eq("relationship_id", 0).eq("user_id", uid).execute())
    except Exception:  # noqa: BLE001 — журнала может не быть
        pass
    _clear_memory_rewards(uid)
    return ok


def reset_to_solo(supabase, user_id) -> bool:
    """Расставание: игрок в одиночках с нулём, уровень 1, наград нет."""
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return False
    ok = True
    try:
        supabase.table(SOLO_LOVE_TABLE).upsert(
            {"user_id": uid, "love": 0}, on_conflict="user_id").execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Одиночный счёт %s не обнулился: %s", uid, exc)
        ok = False
    ok = _delete_marks(supabase, SOLO_REWARDS_TABLE, uid) and ok
    try:
        (supabase.table(LOVE_LOG_TABLE).delete()
         .eq("relationship_id", 0).eq("user_id", uid).execute())
    except Exception:  # noqa: BLE001
        pass
    _clear_memory_rewards(uid)
    return ok


def solo_paid_stages(supabase, user_id) -> set:
    try:
        res = (supabase.table(SOLO_REWARDS_TABLE).select("stage")
               .eq("user_id", int(user_id)).execute())
    except Exception:
        return set()
    return {_safe_int(row.get("stage"), -1) for row in (res.data or [])}


def claim_solo_stage_rewards(supabase, user_id, score) -> list:
    """Награды за этапы одиночке: один человек, ровно один раз на этап.

    Задача 84: та же защита, что в паре — отметка о выплате больше не может
    «съесть» награду: пишем деньги, а отметку ставим как получится.
    """
    uid = _safe_int(user_id, 0)
    if uid <= 0:
        return []
    current_index = stage_index(score, solo=True)
    already = solo_paid_stages(supabase, uid)
    granted = []
    for stage in SOLO_STAGES:
        index = _safe_int(stage.get("index"), -1)
        # Задача 72: 1-й уровень не оплачивается и в одиночной ветке.
        if not stage_is_paid(stage) or index > current_index or index in already:
            continue
        marked = _mark_ignoring_duplicate(supabase, SOLO_REWARDS_TABLE,
                                         {"user_id": uid, "stage": index})
        if not _credit_coins(supabase, uid, int(stage.get("reward") or 0)):
            # Не смогли начислить — снимаем отметку и попробуем позже.
            if marked:
                _unmark_stage(supabase, SOLO_REWARDS_TABLE, {"user_id": uid}, index)
            logger.warning("Награда за этап %s игрока %s не начислена — повторим позже",
                           index, uid)
            continue
        already.add(index)
        log_reward(supabase, 0, uid, index, stage.get("reward"))
        _memory_mark_paid(0, uid, index)
        granted.append({"stage": index, "title": stage["title"],
                        "reward": stage["reward"]})
    return granted
