"""Сборка miniapp/bunny_ears.png — обруч с ушами поверх круглой аватарки.

Почему скрипт, а не «вырезал в редакторе»: заказчик присылает PNG 1024×1024,
где внутри круга чёрная заливка, по краю синяя обводка, а уши/пух — почти
белые на белом фоне. Если просто вырезать круг, остаётся:
  * чёрный круг вместо лица;
  * синяя обводка, которая при уменьшении до 80 px становится рваной «пилой»
    (третий круг ревью: «кривовато чутка»);
  * кольцо толщиной 1–2 px, которое при сжатии в 8.5 раза рассыпается.

Поэтому картинка рисуется заново в целевом размере: пушистый ободок (мягкий
градиент #e2e5ea → #ffffff) + уши, вырезанные из исходника по маске
«не-белое выше круга», уменьшенные с мягким краем. Ничего не масштабируется
повторно в браузере — CSS ставит картинку 1:1.

Запуск: python tools/build_ny_ears.py [путь_к_исходнику]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "miniapp" / "bunny_ears.png"
DEFAULT_SRC = Path("/tmp/arena-coding-uploads/image-84.png")

# Геометрия исходника (замерено по синей обводке): центр круга и радиус.
SRC_CX, SRC_CY, SRC_R = 511.0, 645.0, 341.0

# Целевой размер: аватарка в профиле 80 px, обруч = ровно её диаметр,
# поэтому картинка ровно 80 px по ширине круга. Сверху оставляем место ушам.
TARGET_W = 84
TARGET_D = 80          # диаметр ободка
TOP_PAD = 38           # сколько пикселей над обручем занимают уши
BAND = 7               # толщина пушистого ободка, px (было 1–2 при сжатии)
SS = 8                 # суперсэмплинг: рисуем в 8 раз крупнее и уменьшаем


def cut_ears(src_path: Path) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Вырезает уши (всё, что выше круга) из исходника с мягким краем."""
    src = Image.open(src_path).convert("RGB")
    a = np.asarray(src).astype(np.int16)
    h, w, _ = a.shape
    yy, xx = np.mgrid[0:h, 0:w]

    # «Не белое»: уши заметно темнее фона (фон 254–255, уши 200–245).
    not_white = a.min(axis=2) < 250
    top_of_band = SRC_CY - SRC_R
    above = yy < (top_of_band + 12)            # заходим чуть внутрь обруча
    mask = not_white & above
    # Ни одной синей точки: от обводки кружка при уменьшении оставалась
    # голубая «пила» по верхнему краю обруча. У ушей синего быть не должно.
    blue = (a[:, :, 2] > a[:, :, 0] + 25) & (a[:, :, 2] > 110)
    mask &= ~blue
    # Убираем одиночный мусор сжатия: сглаживаем и берём только уверенные зоны.
    m = Image.fromarray((mask * 255).astype(np.uint8))
    m = m.filter(ImageFilter.MedianFilter(5)).filter(ImageFilter.MaxFilter(5))
    m = m.filter(ImageFilter.GaussianBlur(2))
    alpha = np.asarray(m).astype(np.float32)

    # Мягкая кромка снизу: иначе низ ушей — ровная горизонтальная линия,
    # которая торчит из-под обруча и читается как «отрезано ножницами».
    fade = np.clip((top_of_band + 12 - yy) / 14.0, 0, 1)
    alpha *= fade
    alpha = alpha.astype(np.uint8)

    # Синие пиксели на ушах (край обводки, попавший в кадр) перекрашиваем в
    # белый пух: синего в НГ-теме быть не должно.
    rgb = np.asarray(src).astype(np.uint8).copy()
    rgb[blue] = (250, 250, 252)
    rgba = Image.fromarray(rgb, "RGB").convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha))

    ys, xs = np.where(alpha > 30)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return rgba.crop(box), box


def draw_band(size: int, diameter: int, band: int) -> Image.Image:
    """Пушистый ободок: кольцо с мягким краем и «мехом» по внешнему контуру."""
    n = diameter * SS
    yy, xx = np.mgrid[0:n, 0:n]
    cx = cy = (n - 1) / 2
    r = np.hypot(xx - cx, yy - cy)
    outer = n / 2
    inner = outer - band * SS

    # Мягкие края: 1.5 px в целевом масштабе = 1.5 * SS в суперсэмплинге.
    edge = 1.5 * SS
    a = np.clip((outer - r) / edge, 0, 1) * np.clip((r - inner) / edge, 0, 1)

    # «Мех»: детерминированный шум по углу сглаживает идеальную окружность,
    # иначе ободок читается как нарисованный в редакторе круг.
    rng = np.random.default_rng(93)
    ang = np.arctan2(yy - cy, xx - cx)
    # Шум сглаживаем окном: несглаженный даёт «шишки» по контуру, из-за которых
    # обруч снова выглядит кривым. Нужна мягкая волна пуха, а не пила.
    raw = rng.normal(0, 0.75 * SS, 720)
    kernel = np.ones(15) / 15
    smooth = np.convolve(raw, kernel, mode="same")
    jitter = np.interp(ang.ravel(), np.linspace(-np.pi, np.pi, 720),
                       smooth).reshape(ang.shape)
    a *= np.clip((outer + jitter - r) / edge, 0, 1)

    # Цвет: у внутреннего края тень, к внешнему — белый пух.
    t = np.clip((r - inner) / max(1.0, outer - inner), 0, 1)
    base = np.array([206, 211, 219], float)      # #ced3db — тень под ободком
    tip = np.array([255, 255, 255], float)
    rgb = base[None, None, :] * (1 - t[..., None]) + tip[None, None, :] * t[..., None]
    img = np.dstack([rgb, (a * 255)[..., None]]).astype(np.uint8)
    return Image.fromarray(img, "RGBA").resize((diameter, diameter), Image.LANCZOS)


def main() -> None:
    src_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SRC
    ears, box = cut_ears(src_path)
    print("уши из исходника:", box, ears.size)

    w, d, pad = TARGET_W, TARGET_D, TOP_PAD
    h = d + pad
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    # Уши: масштабируем по ширине обруча (обруч = 681 px в исходнике).
    scale = d / (2 * SRC_R)
    ew = max(1, round(ears.width * scale))
    eh = max(1, round(ears.height * scale))
    ears_small = ears.resize((ew, eh), Image.LANCZOS)
    ex = (w - ew) // 2
    # Низ ушей чуть ниже верха обруча — стык уходит под ободок.
    # +5: низ ушей заходит внутрь ободка. Без этого между ушами и обручем
    # остаётся тёмная щель (видно на увеличении), и стык выглядит оторванным.
    ey = h - d - round((SRC_CY - SRC_R - box[1]) * scale) + 5

    # Основание ушей в исходнике широкое (это сама «шапочка» за ушами). Если
    # оставить его как есть, над обручем торчит ровная горизонтальная полка.
    # Гасим её по дуге обруча: у самой дуги уши плавно растворяются.
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    layer.alpha_composite(ears_small, (ex, max(0, ey)))
    la = np.asarray(layer).astype(np.float32)
    yy2, xx2 = np.mgrid[0:h, 0:w]
    cxc, cyc, rr = (w - 1) / 2, h - d / 2, d / 2
    dx = np.clip(np.abs(xx2 - cxc), 0, rr - 0.01)
    arc_top = cyc - np.sqrt(np.maximum(0, rr * rr - dx * dx))
    fade_up = np.clip((arc_top - yy2) / 7.0, 0, 1)
    la[..., 3] *= fade_up
    canvas.alpha_composite(Image.fromarray(la.astype(np.uint8), "RGBA"))

    # Обруч — ПОВЕРХ ушей: ободок «обхватывает» их основание, как настоящий.
    band = draw_band(w, d, BAND)
    canvas.alpha_composite(band, ((w - d) // 2, h - d))

    canvas.save(OUT)
    print("сохранено:", OUT, canvas.size)


if __name__ == "__main__":
    main()
