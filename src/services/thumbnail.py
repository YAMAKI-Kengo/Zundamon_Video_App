"""
YouTube のサムネイル画像（1280×720）を作る。

伸びている解説チャンネルのサムネイルの定石に合わせている:
  - 文字は少なく（10〜18字・2〜3行）、とにかく大きく太く。一番大事な1語だけ色を変える（**語** で指定）
  - 背景と文字のコントラストを強く（文字の側の背景を暗くする・太い縁取り・影）
  - キャラクターは大きく、感情がはっきり分かる表情（驚き・ショック・ドヤ顔など）
  - 左上に短いラベル（「本要約」「毎日英会話 Day3」など）の帯を入れて、シリーズが一目で分かるようにする
  - スマホの小さな表示でも読める（プレビューで小さくして確認できる）

レイアウトは3種類（LAYOUTS）: ずんだもんのアップ＋大きな文字 / 2人と本の表紙・イラスト / 文字だけを大きく。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from src.models import Project
from src.services import telop
from src.services.compositor import compose_character_frame, load_background_image
from src.utils.asset_loader import has_expression_assets

THUMB_SIZE = (1280, 720)
LAYOUTS: dict[str, str] = {
    "reaction": "リアクション（ずんだもんのアップ＋大きな文字）",
    "duo": "2人＋画像（本の表紙・イラストを真ん中に）",
    "big_text": "文字どーん（大きな文字＋すみっこにずんだもん）",
}
TEXT_FILL = (255, 255, 255)
TEXT_FILL_ALT = (255, 232, 70)      # 2行目以降の文字色（黄色）
EMPHASIS_FILL = (255, 70, 70)       # **強調** した語の色（赤）
OUTLINE = (20, 20, 40)
LABEL_BG = (225, 40, 50)
_EMPHASIS = re.compile(r"\*\*(.+?)\*\*")


def _runs(line: str) -> list[tuple[str, bool]]:
    """「**語**」で強調した部分を分ける。"""
    runs, pos = [], 0
    for m in _EMPHASIS.finditer(line):
        if m.start() > pos:
            runs.append((line[pos:m.start()], False))
        runs.append((m.group(1), True))
        pos = m.end()
    if pos < len(line):
        runs.append((line[pos:], False))
    return [(telop.strip_emoji(t), e) for t, e in runs if telop.strip_emoji(t)]


def _draw_text_block(canvas: Image.Image, lines: list[str], box: tuple[int, int, int, int], max_size: int,
                     align: str = "left") -> None:
    """太い縁取り・影つきの大きな文字を、box（左, 上, 右, 下）の中に収まる最大の大きさで描く。"""
    font_path = telop.resolve_font_path()
    x0, y0, x1, y1 = box
    parsed = [_runs(line) for line in lines if _runs(line)]
    if not parsed:
        return
    size = max_size
    for _ in range(40):
        font = telop.load_font(font_path, size)
        widest = max(sum(font.getlength(t) for t, _ in runs) for runs in parsed)
        total_h = size * 1.12 * len(parsed)
        if (widest + size * 0.3 <= x1 - x0 and total_h <= y1 - y0) or size <= 24:
            break
        size = int(size * 0.94)
    stroke = max(4, round(size * 0.13))
    bold = max(1, round(size * 0.035))
    line_h = round(size * 1.12)
    y = y0 + ((y1 - y0) - line_h * len(parsed)) // 2

    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    text = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ds, dt = ImageDraw.Draw(shadow), ImageDraw.Draw(text)
    for n, runs in enumerate(parsed):
        width = sum(font.getlength(t) for t, _ in runs)
        x = x0 + ((x1 - x0) - width) / 2 if align == "center" else x0 + size * 0.1
        base_fill = TEXT_FILL if n == 0 else TEXT_FILL_ALT
        for chunk, emphasized in runs:
            fill = EMPHASIS_FILL if emphasized else base_fill
            ds.text((x + size * 0.06, y + size * 0.08), chunk, font=font, fill=(0, 0, 0, 170),
                    stroke_width=stroke, stroke_fill=(0, 0, 0, 170))
            dt.text((x, y), chunk, font=font, fill=OUTLINE, stroke_width=stroke, stroke_fill=OUTLINE)
            dt.text((x, y), chunk, font=font, fill=fill, stroke_width=bold, stroke_fill=fill)  # 太字に見せる
            x += font.getlength(chunk)
        y += line_h
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(2, size // 18))))
    canvas.alpha_composite(text)


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


def render_thumbnail(spec: dict, background_path: Optional[str]) -> Image.Image:
    """spec: {"text": 2〜3行（改行区切り・**強調**可）, "sub": 左上の帯, "layout", "zundamon", "metan", "image"}"""
    layout = spec.get("layout") if spec.get("layout") in LAYOUTS else "reaction"
    lines = [line.strip() for line in str(spec.get("text") or "").split("\n") if line.strip()][:3]
    z_expr, m_expr = spec.get("zundamon") or "surprised", spec.get("metan") or "point"
    w, h = THUMB_SIZE

    if layout == "duo":
        canvas = _background(background_path)
        shade = Image.new("RGBA", THUMB_SIZE, (10, 10, 30, 90))
        canvas = Image.alpha_composite(canvas, shade)
        pic = _picture(spec.get("image"), 420, 330)
        if pic is not None:
            shadow = Image.new("RGBA", pic.size, (0, 0, 0, 140))
            shadow.putalpha(pic.getchannel("A").point(lambda a: min(a, 140)))
            canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)), ((w - pic.width) // 2 + 12, 330 + 14))
            canvas.alpha_composite(pic, ((w - pic.width) // 2, 330))
        metan = _character("shikoku_metan", m_expr, 480, bust=0.62)
        zunda = _character("zundamon", z_expr, 510, bust=0.62)
        if metan is not None:
            _paste(canvas, _outline_sprite(metan), -20, h - metan.height + 30)
        if zunda is not None:
            _paste(canvas, _outline_sprite(zunda), w - zunda.width + 20, h - zunda.height + 30)
        _draw_text_block(canvas, lines, (60, 105, w - 60, 320), 150, align="center")
    elif layout == "big_text":
        canvas = _background(background_path)
        canvas = Image.alpha_composite(canvas, Image.new("RGBA", THUMB_SIZE, (10, 10, 30, 150)))
        zunda = _character("zundamon", z_expr, 470, bust=0.62)
        if zunda is not None:
            _paste(canvas, _outline_sprite(zunda, 8), w - zunda.width + 10, h - zunda.height + 20)
        _draw_text_block(canvas, lines, (40, 115, w - 320, h - 40), 210, align="center")
    else:  # reaction
        canvas = _background(background_path, darken_side="left", darkness=0.75)
        zunda = _character("zundamon", z_expr, 820, bust=0.66)
        if zunda is not None:
            _paste(canvas, _outline_sprite(zunda, 12), w - zunda.width + 60, h - zunda.height + 120)
        _draw_text_block(canvas, lines, (40, 120, int(w * 0.62), h - 40), 170)

    _draw_label(canvas, str(spec.get("sub") or ""))
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
    spec.setdefault("layout", "reaction")
    spec.setdefault("zundamon", "surprised")
    spec.setdefault("metan", "point")
    if not spec.get("image"):
        if project.book_cover_path and Path(project.book_cover_path).exists():
            spec["image"] = project.book_cover_path
        else:
            spec["image"] = next((s.illustration_path for s in project.scenes if s.illustration_path), None)
    return spec
