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

import re
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "NotoSansJP-Regular.otf"

# 字幕エリアの最大幅（画面幅に対する比率）。この幅に収まるように自動折り返しする。
TELOP_MAX_WIDTH_RATIO = 0.85
# フォントサイズ（画面の短辺に対する比率）
TELOP_FONT_SIZE_RATIO = 0.05
# 背景ボックスの既定の色（白。話者ごとの色・枠線は config/characters.json の telop で設定する）
TELOP_BG_COLOR = (255, 255, 255, 235)  # 字幕の背景（白）
# 画面下端からテロップ全体（背景ボックス込み）までの余白（画面の高さに対する比率）
TELOP_MARGIN_BOTTOM_RATIO = 0.05

# 折り返し目安の計算に使う代表文字（全角のひらがな1文字分の幅を基準にする）
_SAMPLE_FULLWIDTH_CHAR = "あ"

# 行頭に来てはいけない文字（禁則処理）
_NO_LINE_START = "、。，．,.)）」』】〉》！？!?ーぁぃぅぇぉっゃゅょ々…"

# 絵文字の組み立てに使う見えない文字（異体字セレクタ・ゼロ幅接合子）
_EMOJI_JOINERS = "︎️‍"
_glyph_cache: dict[str, bool] = {}


def _has_glyph(ch: str) -> bool:
    """動画用のフォントに、その文字の字形があるか（無い文字は豆腐（□）で描かれる）。"""
    if ch not in _glyph_cache:
        font = _load_font(resolve_font_path(), 32)
        missing = font.getmask("\U0010FFFD")  # 字形が無い文字として描かれる形（.notdef）
        mask = font.getmask(ch)
        _glyph_cache[ch] = mask.size != missing.size or bytes(mask) != bytes(missing)
    return _glyph_cache[ch]


# フォント（assets/fonts/NotoSansJP-Regular.otf は収録文字を絞った版）に無い、よく使う記号の置き換え先
_SYMBOL_FALLBACKS = str.maketrans({
    **{chr(0x2460 + i): str(i + 1) for i in range(20)},  # ①〜⑳ → 1〜20
    "→": "＞", "⇒": "＞", "▶": "＞", "►": "＞", "←": "＜", "⇐": "＜",
    "○": "〇", "●": "〇", "◎": "〇", "◯": "〇", "✕": "×", "✖": "×", "✗": "×", "◆": "・", "◇": "・",
})


def strip_emoji(text: str) -> str:
    """フォントに字形が無い絵文字・記号を取り除く（豆腐（□）で描かれるのを防ぐ）。

    丸数字・矢印など、よく使う記号はフォントにある文字に置き換えてから、残りの無い文字を取り除く。
    """
    kept = "".join(
        ch for ch in (text or "").translate(_SYMBOL_FALLBACKS)
        if ch not in _EMOJI_JOINERS and (ord(ch) < 0x2000 or 0x3000 <= ord(ch) <= 0x9FFF
                                         or 0xFF00 <= ord(ch) <= 0xFFEF or _has_glyph(ch))
    )
    return re.sub(r"[ 　]{2,}", " ", kept).strip()


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
        # 句読点・閉じ括弧・「！」などは行頭に来ると読みにくいため、はみ出してでも前の行末に付ける（禁則処理）
        if current == "" or ch in _NO_LINE_START or _text_width(font, candidate) <= max_width:
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


def recommended_chars_per_line(resolution: tuple[int, int], font_path: Optional[str] = None) -> int:
    """そのフォーマット（解像度）で、テロップ1行あたりの文字数（縦画面17字・横画面30字）。

    シーン編集画面で「1行の文字数の目安」としてユーザーに提示するために使う。
    """
    return chars_per_line(resolution)


# 字幕1行の最大文字数（横画面30字・縦画面17字）。超える場合は、句読点・助詞の後などの自然な位置で改行する
TELOP_CHARS_PER_LINE = {"landscape": 30, "portrait": 17}
TELOP_MAX_LINES = 2  # 1シーンの字幕の最大行数（台本の取り込み時に、これを超えるセリフは複数のシーンに分ける）

_SENTENCE_END = "。！？!?…"
_CLAUSE_END = "、，,"
_CLOSE_BRACKETS = ")）」』】"
_PARTICLES = "はがをにでともへのやかよねわ"
_NO_BREAK_BEFORE = _NO_LINE_START + "」』）】"


def chars_per_line(resolution: tuple[int, int]) -> int:
    """字幕1行の最大文字数（縦画面17字・横画面30字）。"""
    return TELOP_CHARS_PER_LINE["portrait" if resolution[1] > resolution[0] else "landscape"]


def _char_kind(ch: str) -> str:
    if "ぁ" <= ch <= "ゖ":
        return "hira"
    if "ァ" <= ch <= "ヺ" or ch == "ー":
        return "kata"
    if "一" <= ch <= "鿿" or ch in "々〆":
        return "kanji"
    if ch.isdigit():
        return "digit"
    if ch.isascii() and ch.isalpha():
        return "alpha"
    return "other"


def _break_penalty(text: str, i: int) -> float:
    """text[:i] と text[i:] の間で改行したときの不自然さ（小さいほど自然な改行位置）。"""
    before, after = text[i - 1], text[i]
    if after in _NO_BREAK_BEFORE:
        return 1000  # 句読点・閉じ括弧・小さい文字で行が始まる
    if before in _SENTENCE_END:
        return 0
    if before in _CLAUSE_END:
        return 1
    if before in _CLOSE_BRACKETS:
        return 2
    kb, ka = _char_kind(before), _char_kind(after)
    if after in "おご" and i + 1 < len(text) and _char_kind(text[i + 1]) == "kanji":
        ka = "kanji"  # 「お風呂」「ご飯」のような、お・ご + 漢字の言葉の頭
    head = text[:i]
    if head.count("『") > head.count("』") or head.count("「") > head.count("」"):
        return 40  # 『本のタイトル』「引用」の途中では切らない
    if kb == "hira" and before in _PARTICLES and ka in ("kanji", "kata", "alpha", "digit", "other"):
        return 2  # 「眠りの｜質は」のような、助詞の後で次が漢字・カタカナの位置
    if kb == "hira" and ka in ("kanji", "kata", "alpha", "digit"):
        return 4
    if kb != ka and "digit" not in (kb, ka) and "alpha" not in (kb, ka):
        return 5
    if kb == ka and kb in ("kata", "digit", "alpha", "kanji"):
        return 30  # カタカナ語・数字・英単語・熟語の途中
    return 8


def split_natural(text: str, limit: int) -> list[str]:
    """text を、1つあたり limit 文字以内の塊に、なるべく自然な位置で分ける。

    必要な塊の数は最小にしたうえで、各塊の長さがそろう位置の近くから、句読点の後 → 読点の後 →
    助詞の後 → 文字の種類が変わる位置、の順に自然な切れ目を選ぶ（単語の途中ではなるべく切らない）。
    """
    text = text.strip()
    pieces: list[str] = []
    while len(text) > limit:
        minimum = -(-len(text) // limit)  # 必要な塊の最小数（切り上げ）
        best_i, best_score = limit, float("inf")
        # 塊の数を最小にすると切れる位置が単語の途中しか無い場合（例: 34字を17字ずつ）もあるため、
        # 1つ多く分ける案とも比べて、自然な位置で切れる方を選ぶ（塊が増えるぶん少しだけ減点）
        for count, extra_penalty in ((minimum, 0.0), (minimum + 1, 3.0)):
            ideal = len(text) / count
            lo = max(1, len(text) - (count - 1) * limit)  # 残りが (count-1) 個に収まるための最小位置
            for i in range(lo, min(limit, len(text) - 1) + 1):
                score = _break_penalty(text, i) + abs(i - ideal) * 0.2 + extra_penalty
                if score < best_score:
                    best_i, best_score = i, score
        pieces.append(text[:best_i].rstrip())
        text = text[best_i:].lstrip()
    if text:
        pieces.append(text)
    return pieces


def wrap_by_chars(text: str, limit: int) -> str:
    """利用者が入力した改行は尊重しつつ、各行を limit 文字以内に自然な位置で折り返す。"""
    lines: list[str] = []
    for paragraph in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.extend(split_natural(paragraph, limit) if paragraph.strip() else [""])
    return "\n".join(lines)


HEADLINE_FONT_SIZE_RATIO = 0.075      # 画面上部の大きな文字のサイズ（画面短辺に対する比率）
HEADLINE_MAX_WIDTH_RATIO = 0.92       # 1行の最大幅（画面幅に対する比率。超える行は縮小して収める）
HEADLINE_TOP_MARGIN_RATIO = 0.05      # 画面上端からの余白（画面高さに対する比率）
HEADLINE_COLORS = ("#FFE45C", "#FFFFFF")  # 行ごとの文字色（1行目=黄色、2行目以降=白）
HEADLINE_STROKE_COLOR = "#2B2250"


def render_headline_image(text: str, resolution: tuple[int, int], font_path: Optional[str]) -> Image.Image:
    """画面上部に大きく表示する見出し文字（エンディングの「ご視聴ありがとうございました！」等）を描画する。

    テロップと違って背景ボックスは付けず、太い縁取り＋影で背景の上でも読めるようにする。
    改行（\\n）ごとに1行とし、画面幅に収まらない行はその行だけ文字を小さくする（折り返さない）。
    """
    width, height = resolution
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    lines = [strip_emoji(line) for line in (text or "").replace("\r\n", "\n").split("\n") if strip_emoji(line)]
    if not lines:
        return canvas

    base_size = round(min(width, height) * HEADLINE_FONT_SIZE_RATIO)
    max_w = width * HEADLINE_MAX_WIDTH_RATIO
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    y = round(height * HEADLINE_TOP_MARGIN_RATIO)
    for i, line in enumerate(lines):
        size = base_size if i == 0 else round(base_size * 0.8)  # 2行目以降は少し小さく
        font = _load_font(font_path, size)
        stroke = max(3, round(size * 0.14))
        line_w = _text_width(font, line) + stroke * 2
        if line_w > max_w:
            size = max(8, int(size * max_w / line_w))
            font = _load_font(font_path, size)
            stroke = max(3, round(size * 0.14))
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke)
        x = (width - (bbox[2] - bbox[0])) // 2 - bbox[0]
        draw.text(
            (x, y - bbox[1]), line, font=font, fill=HEADLINE_COLORS[min(i, len(HEADLINE_COLORS) - 1)],
            stroke_width=stroke, stroke_fill=HEADLINE_STROKE_COLOR,
        )
        y += (bbox[3] - bbox[1]) + round(size * 0.25)

    # 右下にずらしてぼかした影を敷き、背景が明るくても文字が浮いて見えるようにする
    offset = max(2, base_size // 16)
    shadow_alpha = Image.new("L", (width, height), 0)
    shadow_alpha.paste(layer.getchannel("A").crop((0, 0, width - offset, height - offset)), (offset, offset))
    shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(max(1, base_size // 14))).point(lambda v: v * 0.6)
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 255))
    shadow.putalpha(shadow_alpha)
    return Image.alpha_composite(Image.alpha_composite(canvas, shadow), layer)


TELOP_SUB_SIZE_RATIO = 0.72   # 字幕の下に添える訳の文字サイズ（字幕の文字サイズに対する比率）
TELOP_SUB_COLOR = "#555555"   # 訳の文字色（字幕より控えめに）

CHAPTER_LABEL_FONT_SIZE_RATIO = 0.032   # 左上の目次ラベルの見出しの文字サイズ（画面短辺に対する比率）
CHAPTER_LABEL_HEAD_SIZE_RATIO = 0.024   # 見出しの前の番号（「成功のコツ2」など）の文字サイズ
CHAPTER_LABEL_MIN_SIZE_RATIO = 0.75     # 幅に収めるために文字を小さくしてよい下限（通常サイズに対する比率）
CHAPTER_LABEL_MIN_SQUEEZE = 0.6         # さらに文字の横幅を狭めてよい下限（長体。1.0=そのまま）
CHAPTER_LABEL_MARGIN_RATIO = 0.03       # 画面端からの余白（画面短辺に対する比率。PR表記と同じ）
CHAPTER_LABEL_BOARD_GAP_RATIO = 0.008   # 黒板との間のすき間（画面幅に対する比率）
CHAPTER_LABEL_BG_COLOR = (32, 38, 64, 205)
CHAPTER_LABEL_ACCENT_COLOR = (255, 214, 64, 255)
CHAPTER_LABEL_HEAD_COLOR = (255, 214, 64, 255)
CHAPTER_LABEL_TEXT_COLOR = "white"
CHAPTER_LABEL_BOLD_RATIO = 0.03         # 文字を太く見せる縁取りの太さ（文字サイズに対する比率）
_CHAPTER_HEAD = re.compile(r"^(.{1,12}?[0-9０-９]+)\s*[：:]\s*(.+)$")


def _chapter_label_max_width(resolution: tuple[int, int]) -> int:
    """ラベルの最大幅: 画面の左端から、黒板（資料メディアの枠）の左端の手前まで。"""
    from src.services.compositor import content_media_max_size  # 循環importを避けるため関数内で読み込む

    width, height = resolution
    margin = round(min(width, height) * CHAPTER_LABEL_MARGIN_RATIO)
    board_left = (width - content_media_max_size(resolution)[0]) // 2
    return max(1, board_left - margin - round(width * CHAPTER_LABEL_BOARD_GAP_RATIO))


def _render_label_line(text: str, font_path: Optional[str], size: int, color, max_width: float) -> Image.Image:
    """ラベルの1行を透過画像で描く。最大幅を超える場合は、文字を小さく → 横幅を狭める（長体）の順で1行に収め、
    それでも入らない場合だけ末尾を「…」で省略する（改行はしない）。"""
    def draw_line(line: str, font_size: int) -> Image.Image:
        font = _load_font(font_path, font_size)
        bold = max(1, round(font_size * CHAPTER_LABEL_BOLD_RATIO))  # 同じ色の縁取りで太字にする
        probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        ref = probe.textbbox((0, 0), "目", font=font, stroke_width=bold)  # 行の高さは文字によらず一定にする
        box = probe.textbbox((0, 0), line, font=font, stroke_width=bold)
        img = Image.new("RGBA", (max(1, box[2] - box[0]), ref[3] - ref[1]), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((-box[0], -ref[1]), line, font=font, fill=color, stroke_width=bold, stroke_fill=color)
        return img

    min_size = max(8, round(size * CHAPTER_LABEL_MIN_SIZE_RATIO))
    img = draw_line(text, size)
    if img.width > max_width:
        # 文字を小さくする（下限まで）
        smaller = max(min_size, int(size * max_width / img.width))
        img = draw_line(text, smaller)
    if img.width > max_width:
        # 文字の横幅を狭める（長体。下限まで）。それでも入らなければ末尾を省略する
        while len(text) > 1 and img.width * CHAPTER_LABEL_MIN_SQUEEZE > max_width:
            text = text[:-2] + "…"
            img = draw_line(text, min_size)
        if img.width > max_width:
            img = img.resize((max(1, int(max_width)), img.height), Image.LANCZOS)
    return img


def render_chapter_label(text: str, resolution: tuple[int, int], font_path: Optional[str],
                         top: Optional[int] = None) -> Image.Image:
    """画面左上に常に出す「いま話している項目」のラベル（説明文の目次と同じ見出し）を描画する。

    黒板に重ならないよう、画面の左端から黒板の左端の手前までに収める。「成功のコツ2：体温を味方につける」
    のような見出しは、上の段に番号（黄色・小さめ）、下の段に内容（白・太字）を置く。内容は改行せず1行にし、
    長い場合は文字を小さく → 横幅を狭める（長体）の順で収める。
    紺色の半透明ボックスの左端に黄色い帯を付ける。
    """
    width, height = resolution
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    text = strip_emoji(text)
    if not text:
        return canvas
    short_side = min(width, height)
    margin = round(short_side * CHAPTER_LABEL_MARGIN_RATIO)
    size = max(10, round(short_side * CHAPTER_LABEL_FONT_SIZE_RATIO))
    head_size = max(8, round(short_side * CHAPTER_LABEL_HEAD_SIZE_RATIO))
    pad_x, accent_w = round(size * 0.55), max(3, round(size * 0.2))
    pad_y, gap = round(size * 0.35), round(size * 0.2)
    max_text_w = _chapter_label_max_width(resolution) - pad_x * 2 - accent_w

    match = _CHAPTER_HEAD.match(text)
    head, body = (match.group(1), match.group(2)) if match else ("", text)
    rows = []
    if head:
        rows.append(_render_label_line(head, font_path, head_size, CHAPTER_LABEL_HEAD_COLOR, max_text_w))
    rows.append(_render_label_line(body, font_path, size, CHAPTER_LABEL_TEXT_COLOR, max_text_w))

    box_w = max(r.width for r in rows) + pad_x * 2 + accent_w
    box_h = pad_y * 2 + sum(r.height for r in rows) + gap * (len(rows) - 1)
    x0 = margin
    y0 = margin if top is None else top
    draw = ImageDraw.Draw(canvas)
    radius = max(4, round(size * 0.3))
    draw.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=radius, fill=CHAPTER_LABEL_BG_COLOR)
    draw.rounded_rectangle([x0, y0, x0 + accent_w * 2, y0 + box_h], radius=radius, fill=CHAPTER_LABEL_ACCENT_COLOR)
    draw.rectangle([x0 + accent_w, y0, x0 + accent_w * 2, y0 + box_h], fill=CHAPTER_LABEL_BG_COLOR)
    y = y0 + pad_y
    for row in rows:
        canvas.alpha_composite(row, (x0 + accent_w + pad_x, y))
        y += row.height + gap
    return canvas


def render_telop_image(
    text: str,
    resolution: tuple[int, int],
    font_path: Optional[str],
    color: str = "white",
    stroke_color: str = "black",
    bg_color=TELOP_BG_COLOR,
    border_color: Optional[str] = None,
    sub_text: str = "",
    word_wrap: bool = False,
) -> Image.Image:
    """テロップ（半透明の背景ボックス＋文字）を、動画と同じ解像度のRGBA画像として描画する。

    テキスト部分以外は完全に透明なので、そのまま動画フレームの上に重ねられる。
    テキストが空の場合は全面透明な画像を返す（呼び出し側で「テロップなし」として扱える）。
    """
    width, height = resolution
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    text, sub_text = strip_emoji(text), strip_emoji(sub_text)

    if not text or not text.strip():
        return canvas

    max_text_width = round(width * TELOP_MAX_WIDTH_RATIO)
    limit = chars_per_line(resolution)
    # 1行に limit 文字（全角）が収まる文字サイズにする
    font_size = min(round(min(width, height) * TELOP_FONT_SIZE_RATIO), int(max_text_width / (limit + 0.4)))
    stroke_width = max(2, font_size // 18)
    line_spacing = round(font_size * 0.35)

    font = _load_font(font_path, font_size)
    if word_wrap:
        # 英語などスペースで単語を区切る言語は、単語の途中で切らないよう幅で折り返す
        wrapped = wrap_telop_text(text, font_path, font_size, max_text_width)
    else:
        wrapped = wrap_by_chars(text, limit)
        # 半角文字が多いなどで文字数の割に幅が広い行は、念のため幅でも折り返す
        if any(_text_width(font, line) > max_text_width for line in wrapped.split("\n")):
            wrapped = wrap_telop_text(wrapped, font_path, font_size, max_text_width)
    lines = wrapped.split("\n")

    draw = ImageDraw.Draw(canvas)

    # 各行の実際の描画サイズ（縁取り込み）を計測する
    line_metrics = []  # (line, bbox, width, height, font, fill)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        line_metrics.append((line, bbox, line_w, line_h, font, color))
    # 訳（英語のセリフの日本語訳など）は、字幕の下に小さめの文字で添える
    if sub_text and sub_text.strip():
        sub_size = max(8, round(font_size * TELOP_SUB_SIZE_RATIO))
        sub_font = _load_font(font_path, sub_size)
        for line in wrap_telop_text(sub_text.strip(), font_path, sub_size, max_text_width).split("\n"):
            bbox = draw.textbbox((0, 0), line, font=sub_font, stroke_width=stroke_width)
            line_metrics.append((line, bbox, bbox[2] - bbox[0], bbox[3] - bbox[1], sub_font, TELOP_SUB_COLOR))

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
    border_width = max(3, font_size // 12) if border_color else 0
    draw.rounded_rectangle(
        [box_x0, box_y0, box_x1, box_y1], radius=radius, fill=bg_color,
        outline=border_color or None, width=border_width,
    )

    y = box_y0 + pad_y
    for line, bbox, line_w, line_h, line_font, fill in line_metrics:
        x = box_x0 + (box_w - line_w) // 2
        # bboxの原点ずれ（フォントによってはtextbboxの左上が(0,0)にならない）を補正して描画する
        draw.text(
            (x - bbox[0], y - bbox[1]),
            line,
            font=line_font,
            fill=fill,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
        )
        y += line_h + line_spacing

    return canvas
