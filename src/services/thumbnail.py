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

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from src.models import Project
from src.services import telop
from src.services.compositor import compose_character_frame, cover_resize
from src.utils.asset_loader import DEFAULT_ROOM_BACKGROUNDS, has_expression_assets, is_video_path

THUMB_SIZE = (1280, 720)
# 右下は、YouTubeの一覧で再生時間（例: 03:15）の黒いバッジが必ず重なる。幅25%×高さ20%には、文字・吹き出し・
# キャラクターなど大事なものを置かない（SAFE_RIGHT より右にキャラクターをはみ出させない）
TIME_BADGE_ZONE = (int(THUMB_SIZE[0] * 0.75), int(THUMB_SIZE[1] * 0.80))  # 右下の幅25%×高さ20%
SAFE_RIGHT = TIME_BADGE_ZONE[0] - 10
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


BASE_TOP, BASE_BOTTOM = (70, 88, 160), (28, 30, 70)  # 下地のグラデーション（上 → 下）

# 背景の色のテーマ。base = 下地のグラデーション（上, 下）、rays = 明るい集中線、dark = 暗い集中線（文字どーん）、frame = 外枠。
# "auto" はレイアウトごとの既定の色（リアクションは黄色、2人は赤オレンジ、使える場面は水色 など）。
THEMES: dict[str, dict] = {
    "auto": {"label": "おまかせ（レイアウトごとの色）"},
    # --- 勝てる配色（3色ルール）: 背景は暗めでシンプル、文字は白か黄色、強調は赤・ピンク・蛍光グリーン ---
    "win_navy": {"label": "🏆 濃紺 × 白 × 赤（定番）", "base": ((40, 54, 115), (12, 14, 38)),
                 "rays": ((255, 205, 60), (220, 120, 30)), "dark": ((52, 66, 130), (22, 26, 62)),
                 "frame": (255, 214, 0), "text": (255, 255, 255), "accent": (235, 30, 45)},
    "win_gray_yellow": {"label": "🏆 濃いグレー × 黄 × 赤", "base": ((72, 72, 80), (20, 20, 24)),
                        "rays": ((255, 225, 70), (215, 160, 20)), "dark": ((82, 82, 92), (34, 34, 40)),
                        "frame": (255, 226, 40), "text": (255, 226, 40), "accent": (235, 30, 45)},
    "win_gray_pink": {"label": "🏆 濃いグレー × 白 × ショッキングピンク", "base": ((72, 72, 80), (20, 20, 24)),
                      "rays": ((255, 120, 190), (215, 40, 130)), "dark": ((82, 82, 92), (34, 34, 40)),
                      "frame": (255, 40, 145), "text": (255, 255, 255), "accent": (255, 40, 145)},
    "win_navy_green": {"label": "🏆 濃紺 × 黄 × 蛍光グリーン", "base": ((40, 54, 115), (12, 14, 38)),
                       "rays": ((120, 255, 140), (40, 190, 80)), "dark": ((52, 66, 130), (22, 26, 62)),
                       "frame": (60, 255, 100), "text": (255, 226, 40), "accent": (60, 255, 100)},
    "win_wood": {"label": "🏆 暗い木目 × 白 × 赤", "base": ((115, 74, 42), (42, 24, 12)),
                 "rays": ((255, 200, 90), (205, 120, 40)), "dark": ((128, 84, 50), (62, 36, 18)),
                 "frame": (255, 214, 0), "text": (255, 255, 255), "accent": (235, 30, 45)},
    # --- そのほかの背景の色 ---
    "navy": {"label": "🌃 ネイビー（落ち着き）", "base": ((70, 88, 160), (28, 30, 70)),
             "rays": ((255, 205, 0), (255, 150, 0)), "dark": ((70, 60, 150), (30, 25, 80)), "frame": (255, 214, 0)},
    "red": {"label": "🔥 レッド（インパクト）", "base": ((205, 45, 55), (95, 12, 22)),
            "rays": ((255, 222, 70), (255, 125, 40)), "dark": ((160, 25, 35), (75, 8, 16)), "frame": (255, 214, 0)},
    "yellow": {"label": "⚡ イエロー（元気）", "base": ((255, 212, 50), (228, 138, 0)),
               "rays": ((255, 246, 160), (255, 200, 40)), "dark": ((228, 150, 0), (150, 80, 0)), "frame": (235, 40, 50)},
    "green": {"label": "🫛 ずんだグリーン", "base": ((110, 185, 70), (32, 85, 32)),
              "rays": ((215, 250, 140), (130, 205, 75)), "dark": ((55, 125, 55), (22, 62, 26)), "frame": (255, 255, 255)},
    "blue": {"label": "🌊 スカイブルー（さわやか）", "base": ((115, 195, 250), (30, 100, 190)),
             "rays": ((205, 242, 255), (120, 200, 250)), "dark": ((40, 110, 200), (15, 50, 120)), "frame": (255, 255, 255)},
    "pink": {"label": "🌸 ピンク（やさしい）", "base": ((248, 150, 195), (175, 62, 125)),
             "rays": ((255, 225, 238), (255, 165, 205)), "dark": ((195, 85, 145), (112, 32, 82)), "frame": (255, 255, 255)},
    "black": {"label": "🖤 ブラック（シック）", "base": ((55, 55, 66), (10, 10, 16)),
              "rays": ((255, 214, 0), (205, 150, 0)), "dark": ((64, 64, 76), (22, 22, 28)), "frame": (255, 214, 0)},
}


def _theme(spec: dict) -> Optional[dict]:
    """選ばれた色のテーマ（おまかせなら None = レイアウトごとの既定の色）。"""
    theme = THEMES.get(str(spec.get("theme") or "auto"))
    return None if theme is None or "base" not in theme else theme


def _text_colors(spec: dict) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """（メインの文字の色, 強調の色）。配色プリセットなら、その3色ルールの色。"""
    theme = _theme(spec) or {}
    return tuple(theme.get("text") or TEXT_FILL), tuple(theme.get("accent") or (235, 30, 45))


def _rays(spec: dict, default: tuple[tuple, tuple], dark: bool = False) -> tuple[tuple, tuple]:
    theme = _theme(spec)
    return default if theme is None else theme["dark" if dark else "rays"]


def _photo(spec: Optional[dict]) -> Optional[Image.Image]:
    """全レイアウト共通の背景の写真（spec の bg_image）。選ばれていなければ None。"""
    return _panel_image((spec or {}).get("bg_image"), THUMB_SIZE)


def _background(darken_side: Optional[str] = None, darkness: float = 0.6, spec: Optional[dict] = None) -> Image.Image:
    """下地。背景の写真があれば、写真を明るさ・色そのままで敷く（文字を置く側だけ少し暗くする）。
    写真がなければ、上から下へのグラデーション。"""
    w, h = THUMB_SIZE
    photo = _photo(spec)
    if photo is not None:
        bg = photo.convert("RGBA")
        darkness *= 0.6  # 写真が見えるように、暗くするのは控えめに
    else:
        theme = _theme(spec or {})
        top, bottom = theme["base"] if theme else (BASE_TOP, BASE_BOTTOM)
        column = Image.new("RGB", (1, h))
        for y in range(h):
            p = y / (h - 1)
            column.putpixel((0, y), tuple(round(a + (b - a) * p) for a, b in zip(top, bottom)))
        bg = column.resize(THUMB_SIZE).convert("RGBA")
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


def _pop_text(lines: list[str], max_w: int, max_h: int, max_size: int, align: str = "left",
              accent: tuple[int, int, int] = (235, 30, 45), shadow_blur: Optional[int] = None,
              fill_color: tuple[int, int, int] = TEXT_FILL, outline_scale: float = 1.0,
              fill_bold: float = 1.0) -> Image.Image:
    """目立つ文字の画像（文字の大きさは枠に収まる最大に自動で決める）。

    縁取りは二重（内側が黒・外側が白。合わせて文字の大きさの約14%）＋影で、どんな背景の上でも読める。
    文字は fill_color（白か黄色）、==語== は黄色、**語** は特大の強調色（accent）＋白の内側の縁取り。
    """
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
        # outline_scale < 1 で縁取りを細くする（画数の多い漢字でも、中の線がつぶれずに読めるように）
        outer = max(3, round(s * 0.10 * outline_scale))
        inner = max(2, round(s * 0.06 * outline_scale))
        ring = max(2, round(s * 0.04 * outline_scale))
        bold = round(s / 30 * fill_bold)  # 中の文字を太く見せる量（0 なら元の字の太さのまま。画数の多い字の線がつぶれない）
        x = pad + ((max_w - width) / 2 if align == "center" else 0)
        for chunk, kind in runs:
            sdraw.text((x + s * 0.07, y + s * 0.09), chunk, font=font, fill=(0, 0, 0, 190), stroke_width=outer + ring,
                       stroke_fill=(0, 0, 0, 190))
            # 二重の縁取り: いちばん外側に白、その内側に黒
            draw.text((x, y), chunk, font=font, fill=(255, 255, 255), stroke_width=outer + ring, stroke_fill=(255, 255, 255))
            draw.text((x, y), chunk, font=font, fill=(15, 15, 25), stroke_width=outer, stroke_fill=(15, 15, 25))
            if kind == "red":
                draw.text((x, y), chunk, font=font, fill=accent, stroke_width=inner, stroke_fill=(255, 255, 255))
                draw.text((x, y), chunk, font=font, fill=accent, stroke_width=bold,
                          stroke_fill=accent)
            else:
                fill = TEXT_FILL_ALT if kind == "yellow" else fill_color
                draw.text((x, y), chunk, font=font, fill=fill, stroke_width=bold, stroke_fill=fill)
            x += font.getlength(chunk)
        y += lh
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(shadow_blur or max(2, size // 16))))
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


def _headline(canvas: Image.Image, lines: list[str], band_y: tuple[int, int] = HEADLINE_BAND,
              colors: Optional[tuple[tuple, tuple]] = None) -> None:
    """画面上部いっぱいに、黒い帯と特大の文字（インパクトのある一言）を置く。"""
    w, _ = THUMB_SIZE
    top, bottom = band_y
    band = Image.new("RGBA", THUMB_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(band).polygon([(0, top + 18), (w, top), (w, bottom - 18), (0, bottom)], fill=(12, 12, 28, 228))
    canvas.alpha_composite(band)
    fill_color, accent = colors or (TEXT_FILL, (235, 30, 45))
    text = _pop_text(lines[:2], w - 90, bottom - top - 16, 190, align="center", accent=accent, fill_color=fill_color)
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


def _phrase_bubble(canvas: Image.Image, text: str, box: tuple[int, int, int, int], tail: tuple[int, int],
                   border: tuple[int, int, int] = (20, 20, 30), border_width: int = 6) -> None:
    """英語のフレーズを大きく書く、角の丸い吹き出し（2行まで）。"""
    text = telop.strip_emoji(text or "").strip()
    if not text:
        return
    x0, y0, x1, y1 = box
    font_path = telop.resolve_font_path()
    # 1行に収まるなら1行で（ある程度の大きさまで縮めても1行に入るなら、2行に折り返さない）
    size = 84
    while size >= 46:
        font = telop.load_font(font_path, size)
        if font.getlength(text) <= (x1 - x0) * 0.86 and size * 1.2 <= (y1 - y0) * 0.8:
            break
        size -= 3
    else:
        size = 84
        while size > 26:
            font = telop.load_font(font_path, size)
            if len(_wrap_words(text, font, (x1 - x0) * 0.86)) <= 2 and 2 * size * 1.2 <= (y1 - y0) * 0.8:
                break
            size -= 3
    lines = _wrap_words(text, font, (x1 - x0) * 0.86)
    height = round(len(lines) * size * 1.2 + size * 0.9)
    cy = (y0 + y1) // 2
    y0, y1 = cy - height // 2, cy + height // 2
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # しっぽの付け根は、しっぽが向かう側に寄せる（短く自然に見えるように）
    bw = (x1 - x0) * 0.12
    bx = min(max(tail[0] - bw * 0.3, x0 + 34), x1 - 34 - bw)
    d.polygon([(bx - border_width + 6, y1 - 10), (bx + bw + border_width - 6, y1 - 10), tail], fill=border + (255,))
    d.rounded_rectangle([x0 - border_width, y0 - border_width, x1 + border_width, y1 + border_width],
                        radius=34 + border_width, fill=border + (255,))
    d.polygon([(bx + 4, y1 - 14), (bx + bw - 4, y1 - 14),
               (tail[0] + (bx + bw / 2 - tail[0]) * 0.12, tail[1] - 12)], fill=(255, 255, 255, 255))
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


BA_DARK = (44, 62, 80)            # 見る前（左）の背景: 彩度を落とした暗い紺（#2C3E50）
BA_BRIGHT = ((255, 214, 40), (255, 150, 20))  # 見た後（右）の背景: 明るい黄の集中線
BA_BEFORE_SHOUT, BA_AFTER_SHOUT = "やばいのだ…", "超かんたんなのだ！"
# 見る前→見た後の表情のペア（左の顔, 右の顔）
EXPRESSION_PAIRS: dict[str, tuple[str, str, str]] = {
    "despair_smile": ("絶望 → 笑顔", "gloomy", "happy"),
    "confused_confident": ("困り顔 → ドヤ顔", "troubled", "smug"),
    "shock_excited": ("ショック → 大喜び", "shock", "excited"),
    "cry_idea": ("泣き顔 → ひらめき", "cry", "idea"),
}


def _render_before_after(spec: dict, lines: list[str]) -> Image.Image:
    """左右2分割の対比: 左は「見る前」（暗い紺・絶望の顔・悩み）、右は「見た後」（明るい黄・笑顔・得られる結果）。

    面積の目安: ずんだもん左右合わせて35〜40%、左右のひと言30〜35%、吹き出し10〜15%、中央の矢印5%。
    「見た後」のひと言は1.25倍大きく黄色にして、視線を「共感・不安」→「強いメリット」へ流す。
    上には、動画の大きな一言を細い帯で出す。before_image / after_image（動画で使った画像）があれば左右に敷く。
    右下（再生時間のバッジ）には、文字も顔も置かない。
    """
    w, h = THUMB_SIZE
    # --- 背景: 斜めの境界で、左を暗く・右を明るく ---
    split = [(0, 0), (int(w * 0.54), 0), (int(w * 0.46), h), (0, h)]
    mask = Image.new("L", THUMB_SIZE, 0)
    ImageDraw.Draw(mask).polygon(split, fill=255)
    left = Image.new("RGBA", THUMB_SIZE, BA_DARK + (255,))
    shade = Image.new("L", (1, h))
    for y in range(h):
        shade.putpixel((0, y), int(110 * y / (h - 1)))
    dark = Image.new("RGBA", THUMB_SIZE, (8, 10, 20, 255))
    dark.putalpha(shade.resize(THUMB_SIZE))
    left.alpha_composite(dark)
    before_img = _panel_image(spec.get("before_image"), THUMB_SIZE) or _photo(spec)
    if before_img is not None:
        # 写真が見えるように、色を少し抑えてやや暗くするだけ（何の写真か分かる程度に）
        gloomy = ImageEnhance.Brightness(ImageEnhance.Color(before_img.convert("RGB")).enhance(0.5)).enhance(0.82)
        left = Image.alpha_composite(gloomy.convert("RGBA"), Image.new("RGBA", THUMB_SIZE, BA_DARK + (45,)))
    theme = _theme(spec)
    rays = theme["rays"] if theme else BA_BRIGHT
    right = Image.new("RGBA", THUMB_SIZE, rays[1] + (255,))
    after_img = _panel_image(spec.get("after_image"), THUMB_SIZE) or _photo(spec)
    if after_img is not None:
        right = ImageEnhance.Brightness(ImageEnhance.Color(after_img.convert("RGB")).enhance(1.2)).enhance(1.05).convert("RGBA")
        burst = _sunburst(THUMB_SIZE, (w * 0.76, h * 0.55), rays, fade=0.95)
        burst.putalpha(burst.getchannel("A").point(lambda a: a * 16 // 100))
        right.alpha_composite(burst)
    else:
        right.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.76, h * 0.55), rays, fade=0.95))
    canvas = Image.composite(left, right, mask)
    divider = Image.new("RGBA", THUMB_SIZE, (0, 0, 0, 0))
    ImageDraw.Draw(divider).line([(split[1][0] + 8, 0), (split[2][0] + 8, h)], fill=(0, 0, 0, 120), width=14)
    divider = divider.filter(ImageFilter.GaussianBlur(4))
    ImageDraw.Draw(divider).line([split[1], split[2]], fill=(255, 255, 255, 255), width=12)
    canvas.alpha_composite(divider)

    # --- ずんだもん: 左は外側（左下）、右は再生時間のバッジを避けて内側寄り ---
    def face(key: str, fallback: str) -> str:
        value = spec.get(key) or fallback
        return value if has_expression_assets("zundamon", value) else fallback

    # 左右対称に、画面の両端へ大きく置く。左は反転して中央（右）を向かせ、右はそのまま中央（左）を向く。
    # 右下の再生時間のバッジには胴体がかかるが、顔や文字はかからないので問題ない
    before_char = _character("zundamon", face("before_face", "gloomy"), 520, 0.62)
    after_char = _character("zundamon", face("after_face", "happy"), 520, 0.62)
    b_top = a_top = h
    # 頭の中心（立ち絵はしっぽの分だけ横に広いので、幅の真ん中ではない。反転した左は右寄り、右は左寄り）
    b_head = a_head = w // 2
    b_w = a_w = 0
    if before_char is not None:
        before_char = ImageOps.mirror(ImageEnhance.Color(before_char).enhance(0.6))
        b_w = before_char.width
        b_top = h - before_char.height + 105
        _paste(canvas, _drop_shadow(_outline_sprite(before_char, 10)), -30, b_top)
        b_head = -30 + int(b_w * 0.58)
    if after_char is not None:
        a_w = after_char.width
        a_top = h - after_char.height + 105
        a_left = w - a_w + 30
        _paste(canvas, _drop_shadow(_outline_sprite(after_char, 12)), a_left, a_top)
        a_head = a_left + int(a_w * 0.42)

    # --- 左右のひと言（見た後は1.25倍・黄色） ---
    def words(key: str) -> list[str]:
        raw = str(spec.get(key) or "").strip()
        if "\n" in raw or len(raw) <= 7:
            return raw.split("\n")[:2]
        from src.services.slide_renderer import split_point  # 黒板と同じ、自然な改行位置（例: 年10万円／浮いた！）

        i = split_point(raw)
        return [raw[:i].strip(), raw[i:].strip()] if i else [raw]

    _, accent = _text_colors(spec)
    before_text = _pop_text(words("before"), int(w * 0.39), 160, 96, align="center", accent=accent,
                            fill_color=(255, 255, 255))
    after_text = _pop_text(words("after"), int(w * 0.41), 200, 120, align="center", accent=accent,
                           fill_color=TEXT_FILL_ALT)
    _paste_rotated(canvas, before_text, (int(w * 0.24), 222), -3.0)
    _paste_rotated(canvas, after_text, (int(w * 0.76), 226), 3.0)

    # --- 吹き出し（小さめ） ---
    # 吹き出しは、それぞれのずんだもんの頭の内側（画面の中央寄り）に、左右対称に（顔にはかけない）
    if before_char is not None:
        bx = b_head + int(b_w * 0.34)
        _speech_bubble(canvas, str(spec.get("before_shout") or BA_BEFORE_SHOUT),
                       (bx, b_top + 110, bx + 220, b_top + 184), (bx - 5, b_top + 200))
    if after_char is not None:
        ax = a_head - int(a_w * 0.34)
        _speech_bubble(canvas, str(spec.get("after_shout") or BA_AFTER_SHOUT),
                       (ax - 220, a_top + 110, ax, a_top + 184), (ax + 5, a_top + 200))

    # --- 中央の矢印: 左右のひと言のあいだ（「見る前 ➔ 見た後」と読めるように） ---
    _arrow(canvas, (w // 2, 245), 130, 120)
    # --- 上の細い帯に、動画の大きな一言 ---
    if lines:
        _headline(canvas, lines, band_y=(6, 150), colors=_text_colors(spec))
    return canvas


# 場面ごとのアクセントカラー（強調する文字・吹き出しの枠・外枠）。一覧に並んだとき「毎回同じ」に見えないように変える
ACCENTS: dict[str, dict] = {
    "auto": {"label": "おまかせ（ずんだもんの表情から）"},
    "trouble": {"label": "🚨 トラブル・NG系（赤）", "color": (235, 30, 60)},
    "town": {"label": "🚉 街中・移動系（黄・オレンジ）", "color": (255, 165, 0)},
    "cafe": {"label": "☕ 日常会話・カフェ系（カフェラテ色）", "color": (196, 132, 78)},
    "solution": {"label": "✨ 解決・神フレーズ系（青）", "color": (25, 150, 255)},
}
_SOLUTION_FACES = {"smug", "idea", "happy", "excited", "laugh"}


def accent_color(spec: dict) -> tuple[int, int, int]:
    """場面ごとのアクセントカラー。おまかせなら、困り顔は赤、ドヤ顔・ひらめき顔は青。"""
    key = str(spec.get("accent") or "auto")
    if key not in ACCENTS or key == "auto":
        key = "solution" if str(spec.get("zundamon") or "") in _SOLUTION_FACES else "trouble"
    return ACCENTS[key]["color"]


def masked_phrase(spec: dict) -> str:
    """吹き出しに出す英語。答えが見えるとその場で満足してクリックされないので、後半を伏せる（例: Let me 〇〇…？）。"""
    phrase = str(spec.get("phrase") or "").strip()
    if not phrase or spec.get("hide_answer") is False:
        return phrase
    if str(spec.get("phrase_hint") or "").strip():
        return str(spec["phrase_hint"]).strip()
    words = phrase.rstrip(".!?。！？ ").split()
    if len(words) == 1:  # 1語だけなら、最初の1文字だけ見せる（例: Pardon? → P〇〇〇〇？）
        word = words[0]
        return word[0] + "〇" * min(max(len(word) - 1, 2), 5) + "？"
    keep = 1 if len(words) <= 3 else 2
    return " ".join(words[:keep]) + " 〇〇…？"


def _render_scene(spec: dict, lines: list[str]) -> Image.Image:
    """英会話: 左に大きなずんだもん（胸から上・感情の伝わる顔）、右に特大の一言、その下に答えを伏せた吹き出し。

    画面の占める割合の目安: ずんだもん30〜40%・メインの文字35〜45%・吹き出し10〜15%。場面の絵は主張しすぎないよう、
    ぼかして暗くした背景として全体に敷く。右下（再生時間のバッジ）には何も置かない。
    """
    w, h = THUMB_SIZE
    accent = accent_color(spec)
    photo = _photo(spec) or _panel_image(spec.get("image"), THUMB_SIZE)
    if photo is not None:
        canvas = photo.convert("RGBA")  # 場面の写真は暗くせず、明るく鮮明なまま（場所が一目で分かるように）
    else:
        canvas = _background(spec=spec)
        canvas.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.22, h * 0.6), _rays(spec, ((120, 210, 255), (60, 150, 240))),
                                         fade=0.95))
    # 文字を置く右側だけを暗くする（左半分は0%、右へ向かって70%まで）
    grad = Image.new("L", (w, 1))
    for x in range(w):
        grad.putpixel((x, 0), int((115 if photo is not None else 180) * max(0.0, min(1.0, (x / w - 0.42) / 0.33))))
    shade = Image.new("RGBA", THUMB_SIZE, (8, 10, 30, 255))
    shade.putalpha(grad.resize(THUMB_SIZE))
    canvas.alpha_composite(shade)

    z_expr = spec.get("zundamon") or "panic"
    zunda = _character("zundamon", z_expr, 640, bust=0.5)
    if zunda is not None:
        zunda = ImageOps.mirror(zunda)  # 文字の方（右）を向かせる（そっぽを向いて見えないように）
    zunda_top = h - (zunda.height if zunda is not None else 0) + 24
    if zunda is not None:
        _paste(canvas, _drop_shadow(_outline_sprite(zunda, 14), 16, 12), -40, zunda_top)

    # メインの文字（1文字が画面の高さの15〜20%くらい）。しっぽの上に重なってもよい
    text_left = int(w * 0.37)
    text = _pop_text(lines[:3], w - text_left - 30, 400, 150, align="center", accent=accent, shadow_blur=14,
                     fill_color=_text_colors(spec)[0])
    _paste_rotated(canvas, text, ((text_left + w - 30) // 2, 245), 2.0)
    _phrase_bubble(canvas, masked_phrase(spec), (int(w * 0.41), 455, SAFE_RIGHT - 5, 595),
                   (int(w * 0.40), 650), border=accent, border_width=9)  # しっぽは短く、ずんだもんの方（左下）へ
    return canvas


DEFAULT_SHOUTS = {
    "shock": "ガーン…！", "surprised": "えっ！？", "panic": "マジなのだ！？", "excited": "すごいのだ！",
    "cry": "うそなのだ…", "smug": "知ってたのだ", "idea": "分かったのだ！", "gloomy": "もうダメなのだ…",
}


def render_thumbnail(spec: dict) -> Image.Image:
    """spec: {"text": 2〜3行（改行区切り・**強調**可）, "sub": 左上の帯, "shout": 吹き出しのひと言,
    "layout", "zundamon", "metan", "image",
    "before" / "after" / "before_face" / "after_face"（見る前→見た後）, "scene" / "phrase"（英会話の使える場面）}"""
    layout = spec.get("layout") if spec.get("layout") in LAYOUTS else "reaction"
    lines = [line.strip() for line in str(spec.get("text") or "").split("\n") if line.strip()][:3]
    z_expr, m_expr = spec.get("zundamon") or "surprised", spec.get("metan") or "point"
    shout = spec.get("shout") if spec.get("shout") is not None else DEFAULT_SHOUTS.get(z_expr, "")
    text_fill, text_accent = _text_colors(spec)
    w, h = THUMB_SIZE

    if layout == "before_after":
        canvas = _render_before_after(spec, lines)
    elif layout == "scene":
        canvas = _render_scene(spec, lines)
    elif layout == "duo":
        canvas = _background(spec=spec)
        if _photo(spec) is None:
            canvas.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.5, h * 0.66), _rays(spec, ((255, 96, 70), (255, 170, 60))),
                                             fade=0.9))
            canvas = Image.alpha_composite(canvas, Image.new("RGBA", THUMB_SIZE, (20, 10, 40, 40)))
        pic = _picture(spec.get("image"), 470, 330)
        if pic is not None:
            pic = _drop_shadow(pic, 14, 12)
            _paste(canvas, pic, (w - pic.width) // 2 + 6, 340)
        band = Image.new("RGBA", THUMB_SIZE, (0, 0, 0, 0))
        ImageDraw.Draw(band).polygon([(0, 128), (w, 104), (w, 300), (0, 324)],
                                     fill=(15, 15, 30, 170 if _photo(spec) is not None else 215))
        canvas.alpha_composite(band)
        metan = _character("shikoku_metan", m_expr, 430, bust=0.62)
        zunda = _character("zundamon", z_expr, 460, bust=0.62)
        if metan is not None:
            _paste(canvas, _drop_shadow(_outline_sprite(metan, 12)), -30, h - metan.height + 40)
        if zunda is not None:
            _paste(canvas, _drop_shadow(_outline_sprite(zunda, 12)), SAFE_RIGHT - zunda.width - 15, h - zunda.height + 40)
        text = _pop_text(lines, w - 140, 215, 140, align="center", accent=text_accent, fill_color=text_fill)
        _paste_rotated(canvas, text, (w // 2, 214), 1.2)
        if zunda is not None:
            head_y = h - zunda.height + 40
            zl = SAFE_RIGHT - zunda.width - 15
            _speech_bubble(canvas, shout, (zl - 210, head_y + 30, zl + 30, head_y + 115), (zl + 80, head_y + 95))
    elif layout == "big_text":
        canvas = _background(spec=spec)
        if _photo(spec) is not None:
            canvas = Image.alpha_composite(canvas, Image.new("RGBA", THUMB_SIZE, (10, 10, 40, 70)))
        else:
            canvas = Image.alpha_composite(canvas, Image.new("RGBA", THUMB_SIZE,
                                                             (10, 10, 40, 170 if _theme(spec) is None else 90)))
            canvas.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.42, h * 0.5),
                                             _rays(spec, ((70, 60, 150), (30, 25, 80)), dark=True), fade=1.0))
        zunda = _character("zundamon", z_expr, 470, bust=0.62)
        if zunda is not None:
            _paste(canvas, _drop_shadow(_outline_sprite(zunda, 10)), SAFE_RIGHT - zunda.width - 15, h - zunda.height + 20)
        text = _pop_text(lines, w - 420, h - 190, 230, align="center", accent=text_accent, fill_color=text_fill)
        _paste_rotated(canvas, text, ((w - 300) // 2 + 20, h // 2 + 30), -2.5)
        if zunda is not None:
            _speech_bubble(canvas, shout, (SAFE_RIGHT - 280, 120, SAFE_RIGHT, 225),
                           (SAFE_RIGHT - zunda.width // 2, h - zunda.height + 60))
    else:  # reaction
        canvas = _background(darken_side="left", darkness=0.8, spec=spec)
        if _photo(spec) is None:
            canvas.alpha_composite(_sunburst(THUMB_SIZE, (w * 0.78, h * 0.42), _rays(spec, ((255, 205, 0), (255, 150, 0))),
                                             fade=0.75))
        zunda = _character("zundamon", z_expr, 780, bust=0.66)
        if zunda is not None:
            _paste(canvas, _drop_shadow(_outline_sprite(zunda, 14), 16, 12), SAFE_RIGHT - zunda.width + 10,
                   h - zunda.height + 130)
        text = _pop_text(lines, int(w * 0.6), h - 170, 175, accent=text_accent, fill_color=text_fill)
        _paste_rotated(canvas, text, (int(w * 0.33), h // 2 + 40), 3.0)
        if zunda is not None:
            left = SAFE_RIGHT - zunda.width + 10
            # 吹き出しは頭の右上に（左上の帯や大きな文字と重ならないように）
            bx1 = w - 40
            _speech_bubble(canvas, shout, (bx1 - 270, 36, bx1, 126), (left + zunda.width * 0.62, 170))

    if layout != "scene":
        _draw_label(canvas, str(spec.get("sub") or ""))  # 空欄なら帯は出さない
    theme = _theme(spec)
    if layout == "scene":
        _frame(canvas, accent_color(spec))  # 場面ごとの色（一覧に並んだとき、1本ずつ違う企画に見えるように）
    else:
        _frame(canvas, theme["frame"] if theme else ((235, 40, 50) if layout in ("big_text", "before_after") else (255, 214, 0)))
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
            # Day の番号は入れない（初めて見る人が「Day1から見ないと」と感じてクリックを避けるため）
            # 大きな一言とは別の角度のメリット（シリーズ名や Day の番号は入れない。文字の要素を増やさないため）
            spec["sub"] = ""
        elif project.source_kind == "research":
            spec["sub"] = "研究で解説"
        else:
            spec["sub"] = "本要約"
    if project.source_kind == "english":
        spec.setdefault("layout", "scene")
        spec.setdefault("scene", lesson.get("theme") or "")
        spec.setdefault("phrase", next((p.get("en", "") for p in lesson.get("phrases", []) if isinstance(p, dict)), ""))
        spec.setdefault("zundamon", "panic")  # 英会話の企画は「焦り・困り」の共感が一番伝わる
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


UPLOAD_DIR = Path(__file__).resolve().parents[2] / "assets" / "thumbnail_images"  # 自分でアップロードしたサムネイル用の画像
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def uploaded_images() -> list[Path]:
    """サムネイル用に自分でアップロードした画像（新しい順）。"""
    if not UPLOAD_DIR.exists():
        return []
    return sorted((p for p in UPLOAD_DIR.iterdir() if p.suffix.lower() in _IMAGE_EXTS),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def save_uploaded_image(name: str, data: bytes) -> Path:
    """アップロードされた画像を assets/thumbnail_images/ に保存する（同じ名前なら上書き）。"""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "_", Path(name).name) or "image.png"
    path = UPLOAD_DIR / safe
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)
    return path


def with_time_badge(img: Image.Image) -> Image.Image:
    """確認用: 再生時間のバッジ（右下の黒い帯）が重なる場所を、半透明で重ねて見せる（保存する画像には入れない）。"""
    out = img.convert("RGBA").copy()
    w, h = out.size
    sx, sy = w / THUMB_SIZE[0], h / THUMB_SIZE[1]
    layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    zx, zy = int(TIME_BADGE_ZONE[0] * sx), int(TIME_BADGE_ZONE[1] * sy)
    d.rectangle([zx, zy, w - 1, h - 1], outline=(255, 60, 60, 230), width=max(2, int(4 * sx)))
    bw, bh = int(150 * sx), int(62 * sy)
    d.rounded_rectangle([w - bw - int(14 * sx), h - bh - int(14 * sy), w - int(14 * sx), h - int(14 * sy)],
                        radius=int(10 * sx), fill=(0, 0, 0, 200))
    font = telop.load_font(telop.resolve_font_path(), max(10, int(40 * sy)))
    d.text((w - bw - int(14 * sx) + int(20 * sx), h - bh - int(14 * sy) + int(8 * sy)), "12:34", font=font,
           fill=(255, 255, 255, 255))
    return Image.alpha_composite(out, layer)


def repeated_words(text: str, sub: str, min_len: int = 3) -> list[str]:
    """大きな一言と左上の補足フックで、同じ言葉（min_len 文字以上）が重なっているところ。"""
    plain = re.sub(r"\*\*|==|\s", "", text or "")
    sub = re.sub(r"\s", "", sub or "")
    found = []
    for size in range(len(sub), min_len - 1, -1):
        for i in range(len(sub) - size + 1):
            piece = sub[i:i + size]
            if piece in plain and not any(piece in f for f in found):
                found.append(piece)
    return found
