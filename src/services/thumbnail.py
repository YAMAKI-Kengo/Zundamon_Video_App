"""
YouTube のサムネイル画像（1280×720）を作る。

伸びている解説チャンネルのサムネイルの定石に合わせている:
  - 文字は少なく（10〜18字・2〜3行）、とにかく大きく太く。一番大事な1語だけ色を変える（**語** で指定）
  - 背景と文字のコントラストを強く（文字の側の背景を暗くする・太い縁取り・影）
  - キャラクターは大きく、感情がはっきり分かる表情（驚き・ショック・ドヤ顔など）
  - 左上に短いラベル（「本要約」「毎日英会話 Day3」など）の帯を入れて、シリーズが一目で分かるようにする
  - スマホの小さな表示でも読める（プレビューで小さくして確認できる）

  - 一番に目に入るのは、インパクトのある一言（上部いっぱいの特大の文字）
  - 解説動画は「見る前 → 見た後」（悩んでいる → 解決した）が一目で想像できるように、英会話は「どんな場面で使えるか」が分かるように

レイアウト（LAYOUTS）: 見る前→見た後 / 使える場面（英会話） / ずんだもんのアップ＋大きな文字 / 2人と本の表紙・イラスト / 文字だけを大きく。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from src.models import Project
from src.services import telop
from src.services.compositor import compose_character_frame, cover_resize, load_background_image
from src.utils.asset_loader import DEFAULT_ROOM_BACKGROUNDS, has_expression_assets, is_video_path

THUMB_SIZE = (1280, 720)
LAYOUTS: dict[str, str] = {
    "before_after": "見る前→見た後（上に特大の一言・下にビフォーアフター）",
    "scene": "使える場面（英会話: 場面の絵＋英語のフレーズ）",
    "reaction": "リアクション（ずんだもんのアップ＋大きな文字）",
    "duo": "2人＋画像（本の表紙・イラストを真ん中に）",
    "big_text": "文字どーん（大きな文字＋すみっこにずんだもん）",
}
TEXT_FILL = (255, 255, 255)
TEXT_FILL_ALT = (255, 226, 40)      # ==語== で指定した文字色（黄色）
EMPHASIS_FILL = (255, 70, 70)       # **強調** した語の色（赤）
OUTLINE = (20, 20, 40)
LABEL_BG = (225, 40, 50)
# 文字の色の指定: 何も付けない文字は白、**語** は赤（特大）、==語== は黄色
_COLOR_MARKUP = re.compile(r"\*\*(.+?)\*\*|==(.+?)==")


def _runs(line: str) -> list[tuple[str, str]]:
    """1行を、色ごとの部分 (文字列, "red" / "yellow" / "") に分ける。"""
    runs, pos = [], 0
    for m in _COLOR_MARKUP.finditer(line):
        if m.start() > pos:
            runs.append((line[pos:m.start()], ""))
        runs.append((m.group(1), "red") if m.group(1) is not None else (m.group(2), "yellow"))
        pos = m.end()
    if pos < len(line):
        runs.append((line[pos:], ""))
    return [(telop.strip_emoji(t), kind) for t, kind in runs if telop.strip_emoji(t)]


def _draw_label(canvas: Image.Image, label: str) -> None:
    """左上の帯（「本要約」「毎日英会話 Day3」など）。"""
    label = telop.strip_emoji(label or "").strip()
    if not label:
        return
    font = telop.load_font(telop.resolve_font_path(), 52)
    draw = ImageDraw.Draw(canvas)
    w = font.getlength(label)
    draw.polygon([(0, 22), (w + 90, 22), (w + 60, 102), (0, 102)], fill=LABEL_BG + (255,))
    draw.text((30, 34), label, font=font, fill=(255, 255, 255), stroke_width=2, stroke_fill=(255, 255, 255))


def _character(character: str, expression: str, height: int, bust: float = 1.0) -> Optional[Image.Image]:
    """キャラクターの立ち絵（透明な余白を切り落とし、上から bust の割合だけ残す）を高さ height にして返す。"""
    if not has_expression_assets(character, expression):
        expression = "normal"
    img = compose_character_frame(character, expression, False)
    box = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if not box:
        return None
    img = img.crop(box)
    img = img.crop((0, 0, img.width, max(1, round(img.height * bust))))
    scale = height / img.height
    return img.resize((max(1, round(img.width * scale)), height), Image.LANCZOS)


def _outline_sprite(sprite: Image.Image, width: int = 10) -> Image.Image:
    """キャラクターの周りに白いふちを付けて、背景から浮き立たせる。"""
    alpha = sprite.getchannel("A")
    grown = alpha.filter(ImageFilter.MaxFilter(width * 2 + 1))
    base = Image.new("RGBA", sprite.size, (255, 255, 255, 0))
    base.putalpha(grown)
    return Image.alpha_composite(base, sprite)


def _paste(canvas: Image.Image, sprite: Optional[Image.Image], x: int, y: int) -> None:
    """画面からはみ出してもよい位置に、透明部分を保ったまま重ねる。"""
    if sprite is None:
        return
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(sprite, (x, y), sprite)
    canvas.alpha_composite(layer)


def _background(path: Optional[str], darken_side: Optional[str] = None, darkness: float = 0.6) -> Image.Image:
    """背景（少しぼかして色を濃くし、文字を置く側を暗くしてコントラストを付ける）。"""
    bg = load_background_image(path, THUMB_SIZE, blur=3.0).convert("RGB")
    bg = ImageEnhance.Color(bg).enhance(1.3)
    bg = bg.convert("RGBA")
    if darken_side:
        w, h = THUMB_SIZE
        grad = Image.new("L", (w, 1))
        for x in range(w):
            p = x / w if darken_side == "right" else 1 - x / w
            grad.putpixel((x, 0), int(255 * darkness * max(0.0, min(1.0, (p - 0.2) / 0.6))))
        shade = Image.new("RGBA", THUMB_SIZE, (10, 10, 30, 255))
        shade.putalpha(grad.resize(THUMB_SIZE))
        bg = Image.alpha_composite(bg, shade)
    return bg


def _picture(path: Optional[str], max_w: int, max_h: int) -> Optional[Image.Image]:
    """真ん中に置く画像（本の表紙・イラスト）: 白いふちと影を付けて少し傾ける。"""
    if not path or not Path(path).exists():
        return None
    img = Image.open(path).convert("RGBA")
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    border = max(6, img.width // 40)
    framed = Image.new("RGBA", (img.width + border * 2, img.height + border * 2), (255, 255, 255, 255))
    framed.alpha_composite(img, (border, border))
    return framed.rotate(-4, resample=Image.BICUBIC, expand=True)


def _sunburst(size: tuple[int, int], center: tuple[float, float], colors: tuple[tuple, tuple], rays: int = 26,
              fade: float = 0.0) -> Image.Image:
    """放射状の集中線（交互の色の扇形）。fade > 0 なら中心から離れるほど薄くする。"""
    import math

    w, h = size
    cx, cy = center
    radius = math.hypot(max(cx, w - cx), max(cy, h - cy)) * 1.1
    img = Image.new("RGBA", size, colors[1] + (255,))
    draw = ImageDraw.Draw(img)
    step = 360 / rays
    for k in range(0, rays, 2):
        a0, a1 = math.radians(k * step), math.radians((k + 1) * step)
        draw.polygon([(cx, cy), (cx + radius * math.cos(a0), cy + radius * math.sin(a0)),
                      (cx + radius * math.cos(a1), cy + radius * math.sin(a1))], fill=colors[0] + (255,))
    if fade > 0:
        mask = Image.new("L", size, 0)
        md = ImageDraw.Draw(mask)
        steps = 24
        for i in range(steps, 0, -1):
            r = radius * fade * i / steps
            md.ellipse([cx - r, cy - r, cx + r, cy + r], fill=int(255 * (1 - i / steps) ** 0.6))
        img.putalpha(mask.filter(ImageFilter.GaussianBlur(30)))
    return img


def _frame(canvas: Image.Image, color: tuple[int, int, int] = (255, 214, 0), width: int = 14) -> None:
    """サムネイルの外枠（色の太い枠＋内側の黒い線）。一覧の中で目立たせる。"""
    w, h = canvas.size
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, w - 1, h - 1], outline=color + (255,), width=width)
    draw.rectangle([width, width, w - 1 - width, h - 1 - width], outline=(20, 20, 30, 255), width=4)


def _pop_text(lines: list[str], max_w: int, max_h: int, max_size: int, align: str = "left") -> Image.Image:
    """目立つ文字の画像: 黒の太い縁取り。文字は白、==語== は黄色、**語** は特大の赤＋白の内側の縁取り。"""
    font_path = telop.resolve_font_path()
    parsed = [_runs(line) for line in lines if _runs(line)]
    if not parsed:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    scales = [1.28 if any(kind == "red" for _, kind in runs) else 1.0 for runs in parsed]  # 赤い語がある行は大きく
    size = max_size
    for _ in range(50):
        fonts = [telop.load_font(font_path, round(size * sc)) for sc in scales]
        widths = [sum(f.getlength(t) for t, _ in runs) for f, runs in zip(fonts, parsed)]
        heights = [round(size * sc * 1.1) for sc in scales]
        if (max(widths) + size * 0.5 <= max_w and sum(heights) <= max_h) or size <= 24:
            break
        size = int(size * 0.95)
    pad = round(size * 0.35)
    img = Image.new("RGBA", (max_w + pad * 2, sum(heights) + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw, sdraw = ImageDraw.Draw(img), ImageDraw.Draw(shadow)
    y = pad
    for n, (runs, font, width, lh, sc) in enumerate(zip(parsed, fonts, widths, heights, scales)):
        s = round(size * sc)
        outer, inner = max(5, round(s * 0.17)), max(2, round(s * 0.07))
        x = pad + ((max_w - width) / 2 if align == "center" else 0)
        for chunk, kind in runs:
            sdraw.text((x + s * 0.07, y + s * 0.09), chunk, font=font, fill=(0, 0, 0, 190), stroke_width=outer,
                       stroke_fill=(0, 0, 0, 190))
            draw.text((x, y), chunk, font=font, fill=(15, 15, 25), stroke_width=outer, stroke_fill=(15, 15, 25))
            if kind == "red":
                draw.text((x, y), chunk, font=font, fill=(235, 30, 45), stroke_width=inner, stroke_fill=(255, 255, 255))
                draw.text((x, y), chunk, font=font, fill=(235, 30, 45), stroke_width=max(1, s // 30),
                          stroke_fill=(235, 30, 45))
            else:
                fill = TEXT_FILL_ALT if kind == "yellow" else TEXT_FILL
                draw.text((x, y), chunk, font=font, fill=fill, stroke_width=max(1, s // 30), stroke_fill=fill)
            x += font.getlength(chunk)
        y += lh
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(2, size // 16))))
    out.alpha_composite(img)
    return out


def _paste_rotated(canvas: Image.Image, layer: Image.Image, center: tuple[int, int], angle: float) -> None:
    rotated = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
    _paste(canvas, rotated, center[0] - rotated.width // 2, center[1] - rotated.height // 2)


def _speech_bubble(canvas: Image.Image, text: str, box: tuple[int, int, int, int], tail: tuple[int, int]) -> None:
    """吹き出し（白い楕円・黒いふち・しっぽ）に、キャラクターのひと言を書く。"""
    text = telop.strip_emoji(text or "").strip()
    if not text:
        return
    x0, y0, x1, y1 = box
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    base_half = (x1 - x0) * 0.09
    tail_poly = [(cx - base_half, cy + (y1 - y0) * 0.25), (cx + base_half, cy + (y1 - y0) * 0.25), tail]
    d.polygon(tail_poly, fill=(20, 20, 30, 255))
    d.ellipse([x0 - 6, y0 - 6, x1 + 6, y1 + 6], fill=(20, 20, 30, 255))
    shrink = [(cx + (px - cx) * 0.8, cy + (py - cy) * 0.8) if (px, py) != tail else (px - (px - cx) * 0.06, py - (py - cy) * 0.06)
              for px, py in tail_poly]
    d.polygon(shrink, fill=(255, 255, 255, 255))
    d.ellipse([x0, y0, x1, y1], fill=(255, 255, 255, 255))
    font_path = telop.resolve_font_path()
    size = round((y1 - y0) * 0.42)
    font = telop.load_font(font_path, size)
    while font.getlength(text) > (x1 - x0) * 0.78 and size > 16:
        size -= 2
        font = telop.load_font(font_path, size)
    tw = font.getlength(text)
    d.text((cx - tw / 2, cy - size * 0.62), text, font=font, fill=(20, 20, 30, 255), stroke_width=max(1, size // 22),
           stroke_fill=(20, 20, 30, 255))
    canvas.alpha_composite(layer)


def _drop_shadow(sprite: Image.Image, offset: int = 12, blur: int = 10) -> Image.Image:
    """キャラクターの後ろに影を付ける（画像は影のぶん大きくなる）。"""
    pad = offset + blur * 2
    out = Image.new("RGBA", (sprite.width + pad, sprite.height + pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", sprite.size, (0, 0, 0, 150))
    shadow.putalpha(sprite.getchannel("A").point(lambda a: min(a, 150)))
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)), (offset, offset))
    out.alpha_composite(sprite, (0, 0))
    return out


# ---------------------------------------------------------------------------
# 見る前→見た後（ビフォーアフター） / 使える場面（英会話）
# ---------------------------------------------------------------------------

HEADLINE_BAND = (95, 330)  # 画面上部の見出しの帯（上端・下端）。一番に目に入る大きな文字を置く


def _headline(canvas: Image.Image, lines: list[str]) -> None:
    """画面上部いっぱいに、黒い帯と特大の文字（インパクトのある一言）を置く。"""
    w, _ = THUMB_SIZE
    top, bottom = HEADLINE_BAND
    band = Image.new("RGBA", THUMB_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(band).polygon([(0, top + 18), (w, top), (w, bottom - 18), (0, bottom)], fill=(12, 12, 28, 228))
    canvas.alpha_composite(band)
    text = _pop_text(lines[:2], w - 90, bottom - top - 16, 190, align="center")
    _paste_rotated(canvas, text, (w // 2, (top + bottom) // 2 + 4), 1.0)


def _tag(canvas: Image.Image, text: str, xy: tuple[int, int], fill: tuple[int, int, int], size: int = 40,
         text_fill: tuple[int, int, int] = (255, 255, 255)) -> tuple[int, int]:
    """小さな札（「BEFORE」「カフェで使える！」など）を描き、札の右下の座標を返す。"""
    text = telop.strip_emoji(text or "").strip()
    if not text:
        return xy
    font = telop.load_font(telop.resolve_font_path(), size)
    x, y = xy
    tw = font.getlength(text)
    pad = round(size * 0.35)
    draw = ImageDraw.Draw(canvas)
    box = [x, y, x + tw + pad * 2, y + size + pad * 2]
    draw.rounded_rectangle([box[0] + 5, box[1] + 6, box[2] + 5, box[3] + 6], radius=pad, fill=(0, 0, 0, 140))
    draw.rounded_rectangle(box, radius=pad, fill=fill + (255,), outline=(255, 255, 255, 255), width=4)
    draw.text((x + pad, y + pad - round(size * 0.12)), text, font=font, fill=text_fill,
              stroke_width=max(1, size // 25), stroke_fill=text_fill)
    return box[2], box[3]


def _arrow(canvas: Image.Image, center: tuple[int, int], length: int = 170, height: int = 150) -> None:
    """ビフォーからアフターへの太い矢印（赤・白ふち・影）。"""
    cx, cy = center
    x0, x1 = cx - length // 2, cx + length // 2
    shaft = height * 0.42
    pts = [(x0, cy - shaft / 2), (x1 - height * 0.55, cy - shaft / 2), (x1 - height * 0.55, cy - height / 2),
           (x1, cy), (x1 - height * 0.55, cy + height / 2), (x1 - height * 0.55, cy + shaft / 2), (x0, cy + shaft / 2)]
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.polygon([(px + 8, py + 10) for px, py in pts], fill=(0, 0, 0, 150))
    layer = layer.filter(ImageFilter.GaussianBlur(6))
    d = ImageDraw.Draw(layer)
    d.polygon(pts, fill=(235, 30, 45, 255), outline=(255, 255, 255, 255), width=7)
    canvas.alpha_composite(layer)


def _wrap_words(text: str, font, max_w: float) -> list[str]:
    """英文を単語の切れ目で折り返す。"""
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and font.getlength(trial) > max_w:
            lines.append(current)
            current = word
        else:
            current = trial
    return lines + ([current] if current else [])


def _phrase_bubble(canvas: Image.Image, text: str, box: tuple[int, int, int, int], tail: tuple[int, int]) -> None:
    """英語のフレーズを大きく書く、角の丸い吹き出し（2行まで）。"""
    text = telop.strip_emoji(text or "").strip()
    if not text:
        return
    x0, y0, x1, y1 = box
    font_path = telop.resolve_font_path()
    size = 84
    while size > 26:
        font = telop.load_font(font_path, size)
        lines = _wrap_words(text, font, (x1 - x0) * 0.86)
        if len(lines) <= 2 and len(lines) * size * 1.2 <= (y1 - y0) * 0.8:
            break
        size -= 3
    height = round(len(lines) * size * 1.2 + size * 0.9)
    cy = (y0 + y1) // 2
    y0, y1 = cy - height // 2, cy + height // 2
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    mid = (x0 + x1) / 2
    d.polygon([(mid + (x1 - x0) * 0.18, y1 - 10), (mid + (x1 - x0) * 0.34, y1 - 10), tail], fill=(20, 20, 30, 255))
    d.rounded_rectangle([x0 - 6, y0 - 6, x1 + 6, y1 + 6], radius=40, fill=(20, 20, 30, 255))
    d.polygon([(mid + (x1 - x0) * 0.2, y1 - 14), (mid + (x1 - x0) * 0.32, y1 - 14),
               (tail[0] - (tail[0] - mid) * 0.08, tail[1] - 14)], fill=(255, 255, 255, 255))
    d.rounded_rectangle([x0, y0, x1, y1], radius=36, fill=(255, 255, 255, 255))
    y = y0 + (height - len(lines) * size * 1.2) / 2 - size * 0.08
    for line in lines:
        tw = font.getlength(line)
        d.text(((x0 + x1 - tw) / 2, y), line, font=font, fill=(25, 60, 170, 255), stroke_width=max(1, size // 22),
               stroke_fill=(25, 60, 170, 255))
        y += size * 1.2
    canvas.alpha_composite(layer)


def _panel_image(path: Optional[str], size: tuple[int, int]) -> Optional[Image.Image]:
    """見る前・見た後の半分の画面に敷く画像（動画で使ったイラスト・背景）。無ければ None。"""
    if not path or not Path(path).exists() or is_video_path(path):
        return None
    try:
        return cover_resize(Image.open(path).convert("RGBA"), size)
    except OSError:
        return None


def _render_before_after(spec: dict, lines: list[str], background_path: Optional[str]) -> Image.Image:
    """上に特大の一言、下の左に「見る前」（暗い・しょんぼり）、右に「見た後」（明るい・笑顔）。

    before_image / after_image（動画で使ったイラスト・背景）があれば、左右それぞれの画面に敷く。
    """
    w, h = THUMB_SIZE
    canvas = _background(background_path)
    split = [(0, 0), (int(w * 0.53), 0), (int(w * 0.47), h), (0, h)]
    mask = Image.new("L", THUMB_SIZE, 0)
    ImageDraw.Draw(mask).polygon(split, fill=255)
    panel_top = HEADLINE_BAND[0] + 110  # 画像の大事なところが見出しの帯に隠れないよう、帯の下寄りに敷く
    half = (int(w * 0.53), h - panel_top)
    before_img = _panel_image(spec.get("before_image"), half)
    after_img = _panel_image(spec.get("after_image"), half)
    left = canvas.copy()
    if before_img is not None:
        left.alpha_composite(before_img, (0, panel_top))
    # 画像を敷くときは、何の画像か分かる程度に少しだけ暗く・色を抑える（画像が無いときは暗い画面にする）
    gloomy = ImageEnhance.Brightness(ImageEnhance.Color(left.convert("RGB")).enhance(
        0.7 if before_img is not None else 0.1)).enhance(0.88 if before_img is not None else 0.55).convert("RGBA")
    gloomy = Image.alpha_composite(gloomy, Image.new("RGBA", THUMB_SIZE, (30, 45, 110, 30 if before_img is not None else 90)))
    bright = canvas.copy()
    if after_img is not None:
        bright.alpha_composite(ImageEnhance.Color(after_img).enhance(1.15), (w - half[0], panel_top))
    else:
        bright.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.8, h * 0.66), ((255, 210, 40), (255, 150, 30)), fade=0.95))
    canvas = Image.composite(gloomy, bright, mask)
    divider = Image.new("RGBA", THUMB_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(divider).line([split[1], split[2]], fill=(255, 255, 255, 255), width=10)
    canvas.alpha_composite(divider)

    before_face = spec.get("before_face") or "gloomy"
    after_face = spec.get("after_face") or "happy"
    before_char = _character("zundamon", before_face if has_expression_assets("zundamon", before_face) else "sad", 370, 0.62)
    after_char = _character("zundamon", after_face if has_expression_assets("zundamon", after_face) else "happy", 390, 0.62)
    if before_char is not None:
        before_char = ImageEnhance.Color(before_char).enhance(0.55)
        _paste(canvas, _drop_shadow(_outline_sprite(before_char, 10)), -45, h - before_char.height + 30)
    if after_char is not None:
        _paste(canvas, _drop_shadow(_outline_sprite(after_char, 12)), w - after_char.width + 45, h - after_char.height + 30)

    # 見る前・見た後のひと言（長ければ2行にして、大きく見せる）
    left_x = (before_char.width - 65) if before_char is not None else 40
    right_x = w - (after_char.width - 45 if after_char is not None else 40)
    areas = ((max(20, left_x - 150), int(w * 0.47) - 55), (int(w * 0.53) + 55, min(w - 20, right_x + 150)))
    for (x0, x1), key, angle in zip(areas, ("before", "after"), (-3.0, 3.0)):
        raw = str(spec.get(key) or "").strip()
        if "\n" in raw or len(raw) <= 7:
            words = raw.split("\n")[:2]
        else:
            words = telop.split_natural(raw, max(4, -(-len(raw) // 2)))[:2]
        text = _pop_text(words, max(120, x1 - x0), 230, 130, align="center")
        _paste_rotated(canvas, text, ((x0 + x1) // 2, 560), angle)
    _arrow(canvas, (w // 2, 555), 130, 120)
    _headline(canvas, lines)
    return canvas


def _render_scene(spec: dict, lines: list[str], background_path: Optional[str]) -> Image.Image:
    """英会話: 上に特大の一言、左下に場面の絵と「〇〇で使える！」、右下にずんだもんと英語のフレーズ。"""
    w, h = THUMB_SIZE
    canvas = _background(background_path)
    canvas.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.3, h * 0.7), ((120, 210, 255), (60, 150, 240)), fade=0.95))
    z_expr = spec.get("zundamon") or "happy"
    zunda = _character("zundamon", z_expr, 440, bust=0.62)
    if zunda is not None:
        _paste(canvas, _drop_shadow(_outline_sprite(zunda, 12)), w - zunda.width + 10, h - zunda.height + 30)
    zunda_left = w - (zunda.width if zunda is not None else 0)
    pic = _picture(spec.get("image"), 500, 330)
    if pic is not None:
        pic = _drop_shadow(pic, 14, 12)
        _paste(canvas, pic, 30, h - pic.height + 10)
        bubble_left = 30 + pic.width - 40
    else:
        bubble_left = 60
    scene = str(spec.get("scene") or "").strip()
    if scene:
        label = scene if scene.endswith(("使える！", "使える", "！")) else f"{scene}で使える！"
        _tag(canvas, label, (40, 356), (235, 40, 50), 44)
    phrase = str(spec.get("phrase") or "").strip()
    right = max(bubble_left + 300, zunda_left + 40)
    _phrase_bubble(canvas, phrase, (bubble_left, 400, right, 610), (zunda_left + 90, 560))
    _headline(canvas, lines)
    return canvas


DEFAULT_SHOUTS = {
    "shock": "ガーン…！", "surprised": "えっ！？", "panic": "マジなのだ！？", "excited": "すごいのだ！",
    "cry": "うそなのだ…", "smug": "知ってたのだ", "idea": "分かったのだ！", "gloomy": "もうダメなのだ…",
}


def render_thumbnail(spec: dict, background_path: Optional[str]) -> Image.Image:
    """spec: {"text": 2〜3行（改行区切り・**強調**可）, "sub": 左上の帯, "shout": 吹き出しのひと言,
    "layout", "zundamon", "metan", "image",
    "before" / "after" / "before_face" / "after_face"（見る前→見た後）, "scene" / "phrase"（英会話の使える場面）}"""
    layout = spec.get("layout") if spec.get("layout") in LAYOUTS else "reaction"
    lines = [line.strip() for line in str(spec.get("text") or "").split("\n") if line.strip()][:3]
    z_expr, m_expr = spec.get("zundamon") or "surprised", spec.get("metan") or "point"
    shout = spec.get("shout") if spec.get("shout") is not None else DEFAULT_SHOUTS.get(z_expr, "")
    w, h = THUMB_SIZE

    if layout == "before_after":
        canvas = _render_before_after(spec, lines, background_path)
    elif layout == "scene":
        canvas = _render_scene(spec, lines, background_path)
    elif layout == "duo":
        canvas = _background(background_path)
        canvas.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.5, h * 0.66), ((255, 96, 70), (255, 170, 60)), fade=0.9))
        canvas = Image.alpha_composite(canvas, Image.new("RGBA", THUMB_SIZE, (20, 10, 40, 40)))
        pic = _picture(spec.get("image"), 470, 330)
        if pic is not None:
            pic = _drop_shadow(pic, 14, 12)
            _paste(canvas, pic, (w - pic.width) // 2 + 6, 340)
        band = Image.new("RGBA", THUMB_SIZE, (0, 0, 0, 0))
        ImageDraw.Draw(band).polygon([(0, 128), (w, 104), (w, 300), (0, 324)], fill=(15, 15, 30, 215))
        canvas.alpha_composite(band)
        metan = _character("shikoku_metan", m_expr, 430, bust=0.62)
        zunda = _character("zundamon", z_expr, 460, bust=0.62)
        if metan is not None:
            _paste(canvas, _drop_shadow(_outline_sprite(metan, 12)), -30, h - metan.height + 40)
        if zunda is not None:
            _paste(canvas, _drop_shadow(_outline_sprite(zunda, 12)), w - zunda.width + 10, h - zunda.height + 40)
        text = _pop_text(lines, w - 140, 215, 140, align="center")
        _paste_rotated(canvas, text, (w // 2, 214), 1.2)
        if zunda is not None:
            head_y = h - zunda.height + 40
            _speech_bubble(canvas, shout, (w - zunda.width - 200, head_y + 30, w - zunda.width + 40, head_y + 115),
                           (w - zunda.width + 90, head_y + 95))
    elif layout == "big_text":
        canvas = _background(background_path)
        canvas = Image.alpha_composite(canvas, Image.new("RGBA", THUMB_SIZE, (10, 10, 40, 170)))
        canvas.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.42, h * 0.5), ((70, 60, 150), (30, 25, 80)), fade=1.0))
        zunda = _character("zundamon", z_expr, 470, bust=0.62)
        if zunda is not None:
            _paste(canvas, _drop_shadow(_outline_sprite(zunda, 10)), w - zunda.width + 10, h - zunda.height + 20)
        text = _pop_text(lines, w - 380, h - 190, 230, align="center")
        _paste_rotated(canvas, text, ((w - 300) // 2 + 20, h // 2 + 30), -2.5)
        if zunda is not None:
            _speech_bubble(canvas, shout, (w - 300, 120, w - 30, 225), (w - zunda.width // 2, h - zunda.height + 60))
    else:  # reaction
        canvas = _background(background_path, darken_side="left", darkness=0.8)
        canvas.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.78, h * 0.42), ((255, 205, 0), (255, 150, 0)), fade=0.75))
        zunda = _character("zundamon", z_expr, 840, bust=0.66)
        if zunda is not None:
            _paste(canvas, _drop_shadow(_outline_sprite(zunda, 14), 16, 12), w - zunda.width + 60, h - zunda.height + 130)
        text = _pop_text(lines, int(w * 0.6), h - 170, 175)
        _paste_rotated(canvas, text, (int(w * 0.33), h // 2 + 40), 3.0)
        if zunda is not None:
            left = w - zunda.width + 60
            _speech_bubble(canvas, shout, (left - 300, 36, left - 30, 126), (left + 40, 150))

    _draw_label(canvas, str(spec.get("sub") or ""))
    _frame(canvas, (235, 40, 50) if layout in ("big_text", "before_after") else (255, 214, 0))
    return canvas.convert("RGB")


def default_spec(project: Project) -> dict:
    """台本にサムネイルの指定が無いときの既定（タイトル・本・英会話の情報から作る）。"""
    spec = dict(project.thumbnail or {})
    lesson = project.lesson or {}
    if not spec.get("text"):
        if project.source_kind == "english" and lesson:
            phrase = next((p.get("en", "") for p in lesson.get("phrases", []) if isinstance(p, dict)), "")
            spec["text"] = f"**{phrase}**\nの使い方" if phrase else (lesson.get("theme") or "今日の英会話")
        else:
            title = re.sub(r"【[^】]*】|#\S+|｜.*$|\|.*$", "", project.video_title or project.book_title or "").strip()
            spec["text"] = "\n".join(telop.split_natural(title, 8)[:3]) if title else "今日の本"
    if not spec.get("sub"):
        if project.source_kind == "english":
            day = lesson.get("day")
            spec["sub"] = "毎日英会話" + (f" Day{day}" if day and str(day) != "7" else " まとめ" if day else "")
        elif project.source_kind == "research":
            spec["sub"] = "研究で解説"
        else:
            spec["sub"] = "本要約"
    if project.source_kind == "english":
        spec.setdefault("layout", "scene")
        spec.setdefault("scene", lesson.get("theme") or "")
        spec.setdefault("phrase", next((p.get("en", "") for p in lesson.get("phrases", []) if isinstance(p, dict)), ""))
        spec.setdefault("zundamon", "happy")
    else:
        spec.setdefault("layout", "before_after")
        spec.setdefault("before", "モヤモヤ…")
        spec.setdefault("after", "スッキリ！")
        spec.setdefault("zundamon", "surprised")
    if project.source_kind != "english":
        before_image, after_image = default_before_after_images(project)
        spec["before_image"] = spec.get("before_image") or before_image  # 空なら、動画で使った画像から自動で選ぶ
        spec["after_image"] = spec.get("after_image") or after_image
    spec.setdefault("before_face", "gloomy")
    spec.setdefault("after_face", "happy")
    spec.setdefault("metan", "point")
    if not spec.get("image"):
        if project.book_cover_path and Path(project.book_cover_path).exists():
            spec["image"] = project.book_cover_path
        else:
            spec["image"] = next((s.illustration_path for s in project.scenes if s.illustration_path), None)
    return spec


def video_images(project: Project) -> list[str]:
    """動画の中で使った画像（イラスト・場面の背景。いつもの部屋の背景は除く）を、出てくる順に返す。"""
    found = []
    for scene in project.scenes:
        for path in (scene.illustration_path, scene.background_path):
            if (path and Path(path).exists() and not is_video_path(path)
                    and Path(path).stem not in DEFAULT_ROOM_BACKGROUNDS and path not in found):
                found.append(path)
    return found


def default_before_after_images(project: Project) -> tuple[Optional[str], Optional[str]]:
    """見る前 = 導入（悩み・失敗の場面）で使った画像、見た後 = 解説・まとめの後半で使った画像。"""
    def images(sections):
        return [p for s in project.scenes if s.section in sections
                for p in (s.illustration_path, s.background_path)
                if p and Path(p).exists() and not is_video_path(p) and Path(p).stem not in DEFAULT_ROOM_BACKGROUNDS]

    all_images = video_images(project)
    before = next(iter(images({"intro"})), all_images[0] if all_images else None)
    later = [p for p in images({"explain", "summary"}) if p != before]
    after = later[-1] if later else next((p for p in reversed(all_images) if p != before), None)
    return before, after
