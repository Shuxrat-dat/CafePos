"""Рисует иконки приложения (PWA) в static/icons/. Нужен Pillow (только для этого скрипта, не для кассы):
    py -m pip install pillow && py scripts/make_icons.py
Готовые PNG лежат в проекте, поэтому запускать скрипт нужно только при смене дизайна иконки."""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
BRAND = (122, 63, 30, 255)      # #7A3F1E, цвет бренда
CREAM = (247, 243, 236, 255)    # #F7F3EC, цвет фона приложения
SCALE = 4                       # рисуем крупнее и уменьшаем: гладкие края


def draw(size, content, rounded):
    """content — доля стороны, которую занимает тарелка с приборами (для maskable — внутри безопасной зоны)."""
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BRAND)
    else:
        d.rectangle([0, 0, s, s], fill=BRAND)
    cx = cy = s / 2
    r = s * content / 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CREAM)

    # Вилка слева и нож справа, коричневым по тарелке
    u = r / 10  # условная единица размера
    x_fork, x_knife = cx - 3.4 * u, cx + 3.4 * u
    top, bottom = cy - 6.2 * u, cy + 6.2 * u
    stem = 0.85 * u / 2
    # ручка вилки и перекладина зубцов
    d.rounded_rectangle([x_fork - stem, cy - 1.2 * u, x_fork + stem, bottom], radius=int(stem), fill=BRAND)
    d.rounded_rectangle([x_fork - 2.1 * u, cy - 3.3 * u, x_fork + 2.1 * u, cy - 1.2 * u], radius=int(u * 1.1), fill=BRAND)
    # три зубца
    for dx in (-1.75, 0, 1.75):
        d.rounded_rectangle(
            [x_fork + dx * u - 0.35 * u, top, x_fork + dx * u + 0.35 * u, cy - 2.6 * u], radius=int(0.35 * u), fill=BRAND
        )
    # нож: ручка и лезвие
    d.rounded_rectangle([x_knife - stem, cy - 0.4 * u, x_knife + stem, bottom], radius=int(stem), fill=BRAND)
    d.rounded_rectangle([x_knife - 1.15 * u, top, x_knife + 0.2 * u, cy + 0.6 * u], radius=int(1.0 * u), fill=BRAND)
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = {
        "icon-192.png": (192, 0.64, True),
        "icon-512.png": (512, 0.64, True),
        # maskable: Android обрезает иконку по своей маске, важное держим в центральных 60%
        "icon-maskable-512.png": (512, 0.56, False),
        # iOS сам скругляет углы и не любит прозрачность
        "apple-touch-icon.png": (180, 0.64, False),
        "favicon-32.png": (32, 0.7, True),
    }
    for name, (size, content, rounded) in jobs.items():
        draw(size, content, rounded).save(OUT / name, optimize=True)
        print("written", name, size)


if __name__ == "__main__":
    main()
