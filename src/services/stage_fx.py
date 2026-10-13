"""
漫画風の効果（集中線・スピード線・フラッシュ・どんより縦線・「！」「？」・汗・怒りマーク・キラキラ）の描画。

- 画面全体にかける効果（集中線・スピード線・フラッシュ）は、カメラの動きの後に画面へそのまま重ねる。
- キャラクターに付ける効果（マーク・どんより縦線）は、カメラの動きの前に、キャラクターの頭の近くに描く
  （アップにしたときはマークも一緒に大きくなる）。
同じ絵は何度も作らないよう、画面サイズ・種類・パターンごとに覚えておく（lru_cache）。
"""
from __future__ import annotations

import math
import random
from functools import lru_cache
from typing import Optional

from PIL import Image, ImageDraw

from src.services import telop

SCREEN_FX = {"focus_lines", "speed_lines", "flash"}
CHARACTER_FX = {"gloom", "exclaim", "question", "sweat", "anger", "sparkle"}

FOCUS_SECONDS = 1.8          # 集中線を出す長さ（最後の0.3秒で消える）
FLICKER_SECONDS = 0.08       # 集中線・スピード線のパターンを切り替える間隔（ちらつかせて勢いを出す）
FLASH_SECONDS = 0.35
MARK_POP_SECONDS = 0.18      # マークがポンと出る時間
MARK_SECONDS = 1.8           # マークを出しておく長さ（最後の0.3秒で消える）
FADE_OUT = 0.3


def _fade(t: float, length: float) -> float:
    """0〜length の間は1、最後の FADE_OUT 秒で0へ。"""
    if t >= length:
        return 0.0
    return min(1.0, (length - t) / FADE_OUT)


@lru_cache(maxsize=12)
def _focus_lines(size: tuple[int, int], center: tuple[int, int], seed: int) -> Image.Image:
    width, height = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rng = random.Random(seed)
    cx, cy = center
    radius = math.hypot(width, height)
    inner = min(width, height) * 0.36
    for _ in range(110):
        angle = rng.uniform(0, 2 * math.pi)
        spread = rng.uniform(0.004, 0.014)
        start = inner * rng.uniform(0.9, 1.5)
        p_far1 = (cx + math.cos(angle - spread) * radius, cy + math.sin(angle - spread) * radius)
        p_far2 = (cx + math.cos(angle + spread) * radius, cy + math.sin(angle + spread) * radius)
        p_near = (cx + math.cos(angle) * start, cy + math.sin(angle) * start)
        draw.polygon([p_near, p_far1, p_far2], fill=(30, 30, 40, 150))
    return img


@lru_cache(maxsize=8)
def _speed_lines(size: tuple[int, int], seed: int) -> Image.Image:
    width, height = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rng = random.Random(seed)
    for _ in range(45):
        y = rng.uniform(0, height)
        length = rng.uniform(0.15, 0.45) * width
        x = rng.uniform(-0.2, 1.0) * width
        thickness = max(1, round(rng.uniform(0.002, 0.006) * height))
        draw.rectangle([x, y, x + length, y + thickness], fill=(255, 255, 255, 140))
    return img


def apply_screen_fx(canvas: Image.Image, fx: str, t: float, focus: tuple[float, float],
                    duration: float) -> Image.Image:
    """画面全体の効果を重ねる（canvas は RGB。新しい画像を返すことがある）。"""
    size = canvas.size
    if fx == "flash":
        if t < FLASH_SECONDS:
            alpha = int(220 * (1 - t / FLASH_SECONDS))
            white = Image.new("RGB", size, (255, 255, 255))
            return Image.blend(canvas, white, alpha / 255)
        return canvas
    if fx == "focus_lines":
        strength = _fade(t, min(FOCUS_SECONDS, duration))
        if strength <= 0:
            return canvas
        seed = int(t / FLICKER_SECONDS) % 3
        lines = _focus_lines(size, (round(focus[0]), round(focus[1])), seed)
        if strength < 1:
            lines = lines.copy()
            lines.putalpha(lines.getchannel("A").point(lambda v: int(v * strength)))
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(lines)
        return canvas.convert("RGB")
    if fx == "speed_lines":
        strength = _fade(t, min(FOCUS_SECONDS, duration))
        if strength <= 0:
            return canvas
        lines = _speed_lines(size, int(t / FLICKER_SECONDS) % 4)
        if strength < 1:
            lines = lines.copy()
            lines.putalpha(lines.getchannel("A").point(lambda v: int(v * strength)))
        canvas = canvas.convert("RGBA")
        canvas.alpha_composite(lines)
        return canvas.convert("RGB")
    return canvas


@lru_cache(maxsize=32)
def _mark(kind: str, size_px: int) -> Optional[Image.Image]:
    """キャラクターの頭の近くに出すマーク（正方形の透過画像）。"""
    s = max(16, size_px)
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if kind in ("exclaim", "question"):
        text = "！" if kind == "exclaim" else "？"
        color = (230, 40, 40, 255) if kind == "exclaim" else (40, 110, 230, 255)
        font = telop.load_font(telop.resolve_font_path(), round(s * 0.9))
        box = draw.textbbox((0, 0), text, font=font, stroke_width=max(2, s // 14))
        draw.text(((s - (box[2] - box[0])) / 2 - box[0], (s - (box[3] - box[1])) / 2 - box[1]), text, font=font,
                  fill=color, stroke_width=max(2, s // 14), stroke_fill=(255, 255, 255, 255))
    elif kind == "sweat":
        w = s * 0.42
        cx, top, bottom = s * 0.5, s * 0.1, s * 0.9
        draw.polygon([(cx, top), (cx + w / 2, bottom - w / 2), (cx - w / 2, bottom - w / 2)], fill=(120, 190, 255, 235))
        draw.ellipse([cx - w / 2, bottom - w, cx + w / 2, bottom], fill=(120, 190, 255, 235), outline=(40, 100, 200, 255),
                     width=max(2, s // 24))
    elif kind == "anger":
        red = (230, 40, 50, 255)
        lw = max(3, s // 9)
        c = s / 2
        for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
            box = [c + dx * s * 0.08 - s * 0.2, c + dy * s * 0.08 - s * 0.2, c + dx * s * 0.08 + s * 0.2,
                   c + dy * s * 0.08 + s * 0.2]
            start = {(-1, -1): 0, (1, -1): 90, (1, 1): 180, (-1, 1): 270}[(dx, dy)]
            draw.arc(box, start, start + 90, fill=red, width=lw)
    elif kind == "sparkle":
        def star(cx: float, cy: float, r: float) -> None:
            pts = []
            for i in range(8):
                rr = r if i % 2 == 0 else r * 0.28
                a = math.pi / 4 * i - math.pi / 2
                pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
            draw.polygon(pts, fill=(255, 225, 60, 245), outline=(255, 255, 255, 255))
        star(s * 0.5, s * 0.45, s * 0.38)
        star(s * 0.18, s * 0.8, s * 0.15)
        star(s * 0.85, s * 0.18, s * 0.12)
    else:
        return None
    return img


@lru_cache(maxsize=8)
def _gloom(width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rng = random.Random(7)
    for i in range(9):
        x = width * (0.08 + 0.84 * i / 8) + rng.uniform(-3, 3)
        length = height * rng.uniform(0.45, 0.9)
        draw.line([(x, 0), (x, length)], fill=(70, 50, 120, 170), width=max(2, width // 70))
    return img


def draw_character_fx(canvas: Image.Image, fx: str, t: float, head: tuple[float, float], sprite_size: tuple[int, int],
                      side: int) -> None:
    """キャラクターに付ける効果を、頭の位置 head（画面座標）の近くに描く。side はマークを出す向き（左=-1・右=+1）。"""
    sw, sh = sprite_size
    if fx == "gloom":
        strength = min(1.0, t / 0.3)
        layer = _gloom(max(8, round(sw * 0.8)), max(8, round(sh * 0.45)))
        if strength < 1:
            layer = layer.copy()
            layer.putalpha(layer.getchannel("A").point(lambda v: int(v * strength)))
        x = round(head[0] - layer.width / 2)
        y = round(head[1] - sh * 0.1)
        canvas.paste(layer, (x, y), layer)
        return
    strength = _fade(t, MARK_SECONDS)
    if strength <= 0:
        return
    base = _mark(fx, max(28, round(sh * 0.26)))
    if base is None:
        return
    pop = min(1.0, t / MARK_POP_SECONDS)
    scale = 0.4 + 0.6 * (1 + 0.25 * math.sin(math.pi * pop)) * pop if pop < 1 else 1.0
    size = max(4, round(base.width * scale))
    mark = base.resize((size, size), Image.BILINEAR) if size != base.width else base
    if strength < 1:
        mark = mark.copy()
        mark.putalpha(mark.getchannel("A").point(lambda v: int(v * strength)))
    wobble = math.sin(t * 6) * sh * 0.006
    x = round(head[0] + side * sw * 0.3 - size / 2)
    y = round(head[1] - size * 0.9 + wobble)
    canvas.paste(mark, (x, y), mark)
