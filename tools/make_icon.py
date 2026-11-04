"""Рисует иконку LAN Chat: белое облачко сообщения на синем скруглённом квадрате.
Результат: assets/icon.png, assets/icon.ico (Windows), assets/icon.icns (macOS)."""
import os
from PIL import Image, ImageDraw, ImageFilter

S = 2048                      # рисуем крупно, потом уменьшаем — так края гладкие
OUT = os.path.join(os.path.dirname(__file__), "..", "assets")


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw(full_bleed):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # macOS ожидает поля вокруг квадрата (824 из 1024), Windows — почти во весь размер
    pad = int(S * (0.02 if full_bleed else 0.098))
    size = S - 2 * pad
    radius = int(size * 0.225)

    if not full_bleed:      # мягкая тень под плиткой
        sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([pad, pad + 24, pad + size, pad + size + 24], radius, fill=(0, 0, 0, 90))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(36)))

    # синий градиент сверху вниз
    grad = Image.new("RGBA", (S, S))
    top, bottom = (92, 140, 255), (29, 78, 216)
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        gd.line([(0, y), (S, y)], fill=lerp(top, bottom, y / S) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([pad, pad, pad + size, pad + size], radius, fill=255)
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    img = Image.alpha_composite(img, tile)

    # белое облачко с хвостиком
    bubble = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bubble)
    cx, cy = S / 2, S / 2 - size * 0.03
    bw, bh = size * 0.58, size * 0.42
    x0, y0, x1, y1 = cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2
    bd.rounded_rectangle([x0, y0, x1, y1], int(bh * 0.42), fill=(255, 255, 255, 255))
    tx = x0 + bw * 0.22
    bd.polygon([(tx, y1 - bh * 0.2), (tx + bw * 0.2, y1 - 2), (x0 + bw * 0.08, y1 + bh * 0.24)],
               fill=(255, 255, 255, 255))
    # три точки «печатает»
    r = bh * 0.085
    for i in (-1, 0, 1):
        px = cx + i * bw * 0.22
        bd.ellipse([px - r, cy - r, px + r, cy + r], fill=(37, 99, 235, 255))
    # лёгкая тень облачка
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    shadow.paste((10, 30, 90, 70), (0, 0), bubble.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    sh2 = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh2.paste(shadow, (0, 18))
    tile_mask = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile_mask.paste(sh2, (0, 0), mask)
    img = Image.alpha_composite(img, tile_mask)
    img = Image.alpha_composite(img, bubble)
    return img.resize((1024, 1024), Image.LANCZOS)


os.makedirs(OUT, exist_ok=True)
mac = draw(full_bleed=False)
win = draw(full_bleed=True)
mac.save(os.path.join(OUT, "icon.png"))
mac.save(os.path.join(OUT, "icon.icns"))
win.save(os.path.join(OUT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ok")
