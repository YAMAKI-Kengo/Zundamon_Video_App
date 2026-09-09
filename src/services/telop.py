"""
テロップ（字幕）の折り返し・描画ロジック。

要件:
  - 視認性向上のため、文字の背後に半透明の背景ボックスを敷く。
  - 改行は「ユーザーが読み上げテキスト内に入力した改行（Enterキー）」を
    そのまま尊重したうえで、1行が長すぎる場合のみ自動で折り返す。
    MoviePy の TextClip(method="caption") は、スペース区切りでしか改行判定をしないため
    （日本語のようにスペースを含まない文章では、意図しない位置で折り返される・
    折り返されないまま画面からはみ出る、といった問題が起きる）、この問題を避けるため
    折り返し処理は本モジュールでPillowのフォント計測を使って自前実装している。

このモジュールはPillowのみで完結する軽量な処理のため、UIのプレビューにもそのまま使える。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "NotoSansJP-Regular.otf"

# 字幕エリアの最大幅（画面幅に対する比率）。この幅に収まるように自動折り返しする。
TELOP_MAX_WIDTH_RATIO = 0.85
# フォントサイズ（画面の短辺に対する比率）
TELOP_FONT_SIZE_RATIO = 0.05
# 背景ボックスの色（半透明の黒。話者の文字色・縁取り色によらず共通で使う）
TELOP_BG_COLOR = (0, 0, 0, 150)
# 画面下端からテロップ全体（背景ボックス込み）までの余白（画面の高さに対する比率）
TELOP_MARGIN_BOTTOM_RATIO = 0.05

# 折り返し目安の計算に使う代表文字（全角のひらがな1文字分の幅を基準にする）
_SAMPLE_FULLWIDTH_CHAR = "あ"


def resolve_font_path() -> Optional[str]:
    """テロップ用の日本語フォントのパスを返す。見つからない場合はNone（Pillow既定フォント）。

    既定フォントは日本語グリフを含まないため、日本語テロップは文字化けする可能性がある。
    """
    if FONT_PATH.exists():
        return str(FONT_PATH)
    return None


def load_font(font_path: Optional[str], font_size: int) -> ImageFont.FreeTypeFont:
    """テロップ以外（ビフォーアフターのラベル・PR表記バッジ等）からも使える公開版のフォント読み込み。

    _load_font() と同じ実装で、テロップ用と同じ日本語フォント・フォールバック挙動を
    他の描画箇所でも一貫して使えるようにするための薄いラッパー。
    """
    return _load_font(font_path, font_size)


def _load_font(font_path: Optional[str], font_size: int) -> ImageFont.FreeTypeFont:
    """テロップ用フォントを読み込む。読み込めない場合はPillow既定フォントにフォールバックする。"""
    if font_path:
        try:
            return ImageFont.truetype(font_path, font_size)
        except (OSError, ValueError):
            pass
    try:
        return ImageFont.load_default(size=font_size)
    except TypeError:
        # 古いPillowはload_default()がsize引数を取らない
        return ImageFont.load_default()


def _text_width(font: ImageFont.FreeTypeFont, text: str) -> float:
    try:
        return font.getlength(text)
    except AttributeError:
        # 非常に古いPillow用フォールバック
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]


def _wrap_paragraph(paragraph: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """1つの段落（ユーザーが入力した改行と改行の間の文章）を、max_widthに収まるよう折り返す。

    スペースがあればスペースの位置で優先的に折り返し（英単語混じりの文章向け）、
    スペースが無い/見つからないまま幅を超える場合は、日本語のように単語区切りが
    無い文章でも表示が壊れないよう、文字単位で強制的に折り返す。
    """
    if paragraph == "":
        return [""]

    lines: list[str] = []
    current = ""
    break_at: Optional[int] = None  # current内で「ここで改行してよい」位置（スペース直後）

    for ch in paragraph:
        candidate = current + ch
        if current == "" or _text_width(font, candidate) <= max_width:
            current = candidate
            if ch == " ":
                break_at = len(current)
        else:
            if break_at is not None:
                lines.append(current[:break_at].rstrip())
                current = current[break_at:].lstrip() + ch
            else:
                lines.append(current)
                current = ch
            break_at = None

    lines.append(current)
    return lines


def wrap_telop_text(text: str, font_path: Optional[str], font_size: int, max_width: int) -> str:
    """読み上げテキストを、ユーザー入力の改行を尊重しつつmax_widthに収まるよう折り返す。

    戻り値は改行(\\n)入り文字列。空文字列やNoneを渡しても例外を出さない。
    """
    if not text:
        return ""
    font = _load_font(font_path, font_size)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    wrapped_lines: list[str] = []
    for paragraph in normalized.split("\n"):
        wrapped_lines.extend(_wrap_paragraph(paragraph, font, max_width))
    return "\n".join(wrapped_lines)


def recommended_chars_per_line(resolution: tuple[int, int], font_path: Optional[str]) -> int:
    """そのフォーマット（解像度）で、テロップ1行あたり収まる文字数の目安を返す。

    シーン編集画面で「1行の文字数の目安」としてユーザーに提示するために使う。
    全角文字（ひらがな等）1文字分の幅を基準に概算する。
    """
    width, _height = resolution
    font_size = round(min(resolution) * TELOP_FONT_SIZE_RATIO)
    max_width = round(width * TELOP_MAX_WIDTH_RATIO)
    font = _load_font(font_path, font_size)
    char_width = _text_width(font, _SAMPLE_FULLWIDTH_CHAR)
    if char_width <= 0:
        char_width = font_size
    return max(1, int(max_width // char_width))


def render_telop_image(
    text: str,
    resolution: tuple[int, int],
    font_path: Optional[str],
    color: str = "white",
    stroke_color: str = "black",
    bg_color: tuple[int, int, int, int] = TELOP_BG_COLOR,
) -> Image.Image:
    """テロップ（半透明の背景ボックス＋文字）を、動画と同じ解像度のRGBA画像として描画する。

    テキスト部分以外は完全に透明なので、そのまま動画フレームの上に重ねられる。
    テキストが空の場合は全面透明な画像を返す（呼び出し側で「テロップなし」として扱える）。
    """
    width, height = resolution
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    if not text or not text.strip():
        return canvas

    font_size = round(min(width, height) * TELOP_FONT_SIZE_RATIO)
    stroke_width = max(2, font_size // 18)
    max_text_width = round(width * TELOP_MAX_WIDTH_RATIO)
    line_spacing = round(font_size * 0.35)

    font = _load_font(font_path, font_size)
    wrapped = wrap_telop_text(text, font_path, font_size, max_text_width)
    lines = wrapped.split("\n")

    draw = ImageDraw.Draw(canvas)

    # 各行の実際の描画サイズ（縁取り込み）を計測する
    line_metrics = []  # (line, bbox, width, height)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        line_metrics.append((line, bbox, line_w, line_h))

    text_block_w = max((m[2] for m in line_metrics), default=0)
    text_block_h = sum(m[3] for m in line_metrics) + line_spacing * max(0, len(line_metrics) - 1)

    pad_x = round(font_size * 0.6)
    pad_y = round(font_size * 0.35)
    box_w = min(width, text_block_w + pad_x * 2)
    box_h = text_block_h + pad_y * 2

    margin_bottom = round(height * TELOP_MARGIN_BOTTOM_RATIO)
    box_x0 = (width - box_w) // 2
    box_y0 = height - box_h - margin_bottom
    box_x1 = box_x0 + box_w
    box_y1 = box_y0 + box_h

    radius = max(4, round(font_size * 0.35))
    draw.rounded_rectangle([box_x0, box_y0, box_x1, box_y1], radius=radius, fill=bg_color)

    y = box_y0 + pad_y
    for line, bbox, line_w, line_h in line_metrics:
        x = box_x0 + (box_w - line_w) // 2
        # bboxの原点ずれ（フォントによってはtextbboxの左上が(0,0)にならない）を補正して描画する
        draw.text(
            (x - bbox[0], y - bbox[1]),
            line,
            font=font,
            fill=color,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
        )
        y += line_h + line_spacing

    return canvas
