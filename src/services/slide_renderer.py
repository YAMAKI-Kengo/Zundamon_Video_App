"""
黒板風スライドのローカル自動生成（外部サービス不使用・Pillow + numpy のみ）。

書籍解説動画で、リビング風の背景の手前（画面中央）に置く「黒板」を、画像素材を使わずに
プログラムで描画し、その上に見出し・箇条書きをチョークで手書きしたような質感で直接書き込む。

描画の流れ:
  1. 黒板本体: 深緑の地 + 大きなムラ（低周波ノイズ）+ 細かい粒子 + 消し跡のかすれ
  2. 木枠 + 下部のチョーク受け（チョーク数本を置く）+ 背景から浮かせるためのドロップシャドウ
  3. 見出し（黄色チョーク・中央揃え・手書き風の下線）と箇条書き（白チョーク）
     - 1行ごとにわずかに傾け・上下にずらして手書き感を出す
     - 文字のマスクにノイズを掛けてかすれさせ、周囲に薄い粉のにじみを足してチョークらしくする
     - 箇条書き中の **強調** はピンクのチョークで描く
     - 見出し・箇条書きは1項目＝1行（折り返さない）。長い項目がある場合は文字を小さくして1行に収める
     - 文字量が多い場合はフォントサイズを自動で縮めて黒板内に収める

生成した画像は tmp/slides/ にキャッシュし（内容から決まるハッシュ名）、Scene.content_media_path と
同じ「資料メディア」レイヤーとして compositor に渡す。サイズは資料メディア枠
（compositor.content_media_max_size()）にぴったり合わせて生成するため、拡大縮小でぼやけない。
乱数のシードも内容から決めるため、同じ内容なら何度生成しても同じ絵になる
（口:開/口:閉のフレームやプレビューと本番で黒板の見た目が変わらない）。

縦画面（ショート動画）では黒板ではなく「ホワイトボードにマーカーで書いた」スタイルで描画する
（style_for_resolution()。アルミ枠・光沢・消し残り・マーカーとイレーザーの置かれたトレイ、
見出しは青・本文は黒・強調は赤のマーカー）。

手書き風のフォントを使いたい場合は、assets/fonts/ に "chalk" で始まる名前のフォントファイル
（例: chalk.ttf。Google Fonts の「Klee One」「Yomogi」等、商用利用可のOFLフォント）を置くと
自動的にそちらが使われる（ホワイトボード用に別フォントを使いたい場合は "marker" で始まる名前で置く）。
無ければテロップと同じ Noto Sans JP で描画する。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from src.models import Scene
from src.services import telop
from src.services.compositor import content_media_max_size

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"
SLIDE_CACHE_DIR = PROJECT_ROOT / "tmp" / "slides"

# 見た目を変更したらこの値を上げる（古いキャッシュ画像が使われ続けないように）
STYLE_VERSION = 14

# --- 色 ---
BOARD_COLOR = (36, 66, 52)
WOOD_COLOR = (128, 88, 52)
CHALK_WHITE = (244, 244, 236)
CHALK_YELLOW = (250, 226, 120)
CHALK_PINK = (255, 168, 184)
WHITEBOARD_COLOR = (247, 248, 249)
MARKER_BLACK = (34, 36, 42)
MARKER_BLUE = (28, 80, 180)
MARKER_RED = (214, 42, 48)

# --- レイアウト（黒板画像のサイズに対する比率） ---
SHADOW_PAD_RATIO = 0.025       # ドロップシャドウ用の透明な余白（短辺比）
FRAME_RATIO = 0.035            # 木枠の太さ（短辺比）
TRAY_RATIO = 0.045             # 下部のチョーク受けの高さ（短辺比）
PADDING_X_RATIO = 0.06         # 黒板の内側の左右余白（黒板幅比）
PADDING_Y_RATIO = 0.08         # 黒板の内側の上下余白（黒板高さ比）
TITLE_SIZE_RATIO = 0.088       # 見出しの文字サイズ（黒板高さ比）
BODY_SIZE_RATIO = 0.07         # 箇条書きの文字サイズ（黒板高さ比。要点を文で書けるよう少し小さめ）
# 文字の線の太さ（文字サイズに対する比率）。同梱フォントはRegularで線が細く、チョークのかすれで
# さらに細く見えるため、縁取り(stroke)で太らせて疑似的な太字にする
BOLD_STROKE_RATIO = 0.032
# 文字サイズの基準は「板面の高さ」だが、縦長の板（縦画面用）では高さ基準だと文字が大きすぎて
# 1行に数文字しか入らないため、板面の幅/この値 を上限にする（横長の板では高さの方が小さく影響しない）
SIZE_BASE_MAX_WIDTH_DIVISOR = 1.3
LINE_SPACING = 1.45            # 行送り（文字サイズに対する倍率）
MIN_SCALE = 0.45               # 自動縮小の下限（これ以上縮めると読めないため、はみ出しても諦める）
MAX_SCALE = 1.15               # 内容が少ないときの拡大の上限（板が余白だらけにならないよう、収まる範囲で大きくする）
NUMBER_INDENT_RATIO = 1.75     # 番号付きの箇条書きの、行頭の番号ぶんの字下げ（文字サイズに対する比率）
MIN_FONT_PX = 8                # 1行に収めるために文字を小さくするときの下限（px）
ONE_LINE_SAFETY = 0.96         # 1行に収めるときの余裕（手書き風に行を少し傾けるため、はみ出さないよう少し狭めに見る）

# 行頭に来てはいけない文字（禁則処理。前の行末に追い込む）
_NO_LINE_START = "、。，．,.)）」』】〉》！？!?ーぁぃぅぇぉっゃゅょ々"
# 行末に来てはいけない文字（開き括弧など。次の行の頭に送る）
_NO_LINE_END = "（(「『【〈《"
MIN_LAST_LINE_CHARS = 3  # 折り返したときの最終行の最低文字数
_EMPHASIS_PATTERN = re.compile(r"\*\*(.+?)\*\*")
# 行頭の箇条書き記号。"**強調**" で始まる項目の "*" を記号と誤認して削らないよう、"*" は1個だけの場合に限る
_BULLET_PREFIX_PATTERN = re.compile(r"^\s*(?:[・\-•●■□◆◇]|\*(?!\*)|\d+[\.．、)])\s*")


# ---------------------------------------------------------------------------
# フォント
# ---------------------------------------------------------------------------

def _find_font(prefix: str) -> Optional[str]:
    if FONTS_DIR.exists():
        for candidate in sorted(FONTS_DIR.iterdir()):
            if candidate.name.lower().startswith(prefix) and candidate.suffix.lower() in (".ttf", ".otf", ".ttc"):
                return str(candidate)
    return None


def resolve_chalk_font_path() -> Optional[str]:
    """assets/fonts/chalk*.(ttf|otf|ttc) があればそれを、無ければテロップ用フォントを返す。"""
    return _find_font("chalk") or telop.resolve_font_path()


def resolve_marker_font_path() -> Optional[str]:
    """ホワイトボード用: marker* → chalk* → テロップ用フォント の順に探す。"""
    return _find_font("marker") or resolve_chalk_font_path()


# ---------------------------------------------------------------------------
# テキストの前処理（強調の解析・折り返し）
# ---------------------------------------------------------------------------

Run = tuple[str, bool]  # (文字列, 強調かどうか)


def _parse_emphasis(text: str) -> tuple[str, list[bool]]:
    """"**強調**" 記法を取り除いたプレーンな文字列と、1文字ごとの強調フラグを返す。"""
    plain_chars: list[str] = []
    flags: list[bool] = []
    pos = 0
    for m in _EMPHASIS_PATTERN.finditer(text):
        for ch in text[pos:m.start()]:
            plain_chars.append(ch)
            flags.append(False)
        for ch in m.group(1):
            plain_chars.append(ch)
            flags.append(True)
        pos = m.end()
    for ch in text[pos:]:
        plain_chars.append(ch)
        flags.append(False)
    return "".join(plain_chars), flags


def _to_runs(chars: str, flags: list[bool]) -> list[Run]:
    runs: list[Run] = []
    for ch, flag in zip(chars, flags):
        if runs and runs[-1][1] == flag:
            runs[-1] = (runs[-1][0] + ch, flag)
        else:
            runs.append((ch, flag))
    return runs


def _wrap_rich(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[list[Run]]:
    """強調記法付きの1項目を、max_widthに収まるよう文字単位で折り返す（簡易禁則処理つき）。"""
    plain, flags = _parse_emphasis(text)
    ranges: list[list[int]] = []  # 各行の [開始, 終了) の文字位置
    start = 0
    i = 0
    n = len(plain)
    while i < n:
        if i > start and font.getlength(plain[start:i + 1]) > max_width:
            # 行頭禁則文字は前の行に追い込む（多少はみ出しても読みやすさを優先）
            while i < n and plain[i] in _NO_LINE_START:
                i += 1
            if i >= n:
                break
            # 開き括弧が行末に残る場合は、括弧ごと次の行に送る
            if i - 1 > start and plain[i - 1] in _NO_LINE_END:
                i -= 1
            ranges.append([start, i])
            start = i
            while start < n and plain[start] == " ":
                start += 1
            i = start
            continue
        i += 1
    if start < n:
        ranges.append([start, n])

    # 最終行に1〜2文字だけ取り残される（「決ま／る」のような）折り返しは見栄えが悪いため、
    # 前の行の末尾から文字を送って、最終行を最低 MIN_LAST_LINE_CHARS 文字にする
    if len(ranges) >= 2:
        prev, last = ranges[-2], ranges[-1]
        while (
            last[1] - last[0] < MIN_LAST_LINE_CHARS
            and prev[1] - prev[0] > MIN_LAST_LINE_CHARS + 1
            and plain[prev[1] - 1] not in _NO_LINE_START  # 送った結果、行頭に句読点が来ないように
        ):
            prev[1] -= 1
            last[0] -= 1

    lines = [_to_runs(plain[s:e].rstrip(), flags[s:e]) for s, e in ranges]
    return lines or [[("", False)]]


_POINT_TITLE = re.compile(r"^(ポイント|POINT|Point|point|失敗の理由|成功のコツ|理由|コツ|真実|問題|ステップ|STEP|Step|Q)\s*([0-9０-９]+)\s*[：:．.、\-－ー]?\s*(?=\S)")


def normalize_title(title: str) -> str:
    """「ポイント1 眠り始めが大事」のような見出しを「ポイント1：眠り始めが大事」にそろえる。

    半角スペースだと黒板の上では番号と言葉がくっついて見えるため、全角の「：」で区切る。
    """
    return _POINT_TITLE.sub(lambda m: f"{m.group(1)}{m.group(2)}：", (title or "").strip(), count=1)


def normalize_bullets(bullets: list[str]) -> list[str]:
    """空行を除き、ユーザーが手入力した「・」「-」「1.」などの行頭記号を取り除く（記号は描画側で付ける）。"""
    result = []
    for b in bullets:
        b = _BULLET_PREFIX_PATTERN.sub("", b.strip())
        if b:
            result.append(b)
    return result


# ---------------------------------------------------------------------------
# テクスチャ生成
# ---------------------------------------------------------------------------

def _smooth_noise(rng: np.random.Generator, size: tuple[int, int], cell: int) -> np.ndarray:
    """低解像度の乱数を拡大してぼかした、なめらかなムラ用ノイズ（-1〜1程度）。"""
    w, h = size
    small = rng.normal(0, 1, (max(2, h // cell), max(2, w // cell))).astype(np.float32)
    small_img = Image.fromarray(((small + 3) / 6 * 255).clip(0, 255).astype(np.uint8), "L")
    big = small_img.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(cell / 3))
    return np.asarray(big, dtype=np.float32) / 127.5 - 1.0


def _render_board_surface(size: tuple[int, int], rng: np.random.Generator, py_rng: random.Random) -> Image.Image:
    """黒板の板面（深緑 + ムラ + 粒子 + 消し跡）。"""
    w, h = size
    base = np.empty((h, w, 3), dtype=np.float32)
    base[:] = BOARD_COLOR
    mottling = _smooth_noise(rng, size, max(8, min(w, h) // 12))[..., None] * 7
    grain = rng.normal(0, 3.2, (h, w, 1)).astype(np.float32)
    board = base + mottling + grain

    # 消し跡: 黒板消しで横に拭いたような、白っぽいぼんやりした帯
    smudge = Image.new("L", size, 0)
    sd = ImageDraw.Draw(smudge)
    for _ in range(py_rng.randint(5, 8)):
        cx, cy = py_rng.uniform(0, w), py_rng.uniform(0, h)
        length, thick = py_rng.uniform(w * 0.2, w * 0.55), py_rng.uniform(h * 0.06, h * 0.14)
        sd.ellipse([cx - length / 2, cy - thick / 2, cx + length / 2, cy + thick / 2], fill=py_rng.randint(14, 30))
    smudge = smudge.filter(ImageFilter.GaussianBlur(min(w, h) * 0.04))
    s = np.asarray(smudge, dtype=np.float32)[..., None] / 255.0
    board = board * (1 - s) + np.array(CHALK_WHITE, dtype=np.float32) * s

    # 外周をわずかに暗くして奥行きを出す（周辺減光）
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dist = np.maximum(np.abs(xx / w - 0.5) * 2, np.abs(yy / h - 0.5) * 2)
    board *= (1 - 0.18 * np.clip(dist - 0.6, 0, 1) / 0.4)[..., None]

    return Image.fromarray(board.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _render_wood(size: tuple[int, int], rng: np.random.Generator, vertical: bool = False) -> Image.Image:
    """木目テクスチャ（長手方向に沿った筋）。vertical=Trueなら縦方向の木目。"""
    w, h = size
    length, across = (h, w) if vertical else (w, h)
    stripes = rng.normal(0, 1, (across, 1)).astype(np.float32)
    stripes = np.convolve(stripes[:, 0], np.ones(3) / 3, mode="same")[:, None]
    wobble = rng.normal(0, 0.35, (across, length)).astype(np.float32)
    shade = stripes * 12 + wobble * 6
    # 断面方向に丸みのある陰影（上/左が明るく、下/右が暗い）
    profile = np.linspace(14, -18, across, dtype=np.float32)[:, None]
    tex = np.array(WOOD_COLOR, dtype=np.float32) + (shade + profile)[..., None]
    if vertical:
        tex = tex.transpose(1, 0, 2)
    return Image.fromarray(tex.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _draw_chalk_pieces(canvas: Image.Image, tray_box: tuple[int, int, int, int], py_rng: random.Random) -> None:
    """チョーク受けの上に、白・黄・ピンクのチョークと粉を置く。"""
    x0, y0, x1, y1 = tray_box
    tray_h = y1 - y0
    d = ImageDraw.Draw(canvas)
    piece_h = max(3, round(tray_h * 0.42))
    x = x1 - (x1 - x0) * py_rng.uniform(0.08, 0.18)
    for color in (CHALK_WHITE, CHALK_YELLOW, CHALK_PINK):
        length = tray_h * py_rng.uniform(2.2, 3.4)
        top = y0 + (tray_h - piece_h) * 0.45
        d.rounded_rectangle([x - length, top, x, top + piece_h], radius=piece_h // 2, fill=color + (255,))
        d.line([x - length + piece_h / 2, top + 1, x - piece_h / 2, top + 1], fill=(255, 255, 255, 140), width=1)
        x -= length + tray_h * py_rng.uniform(0.5, 1.2)
    # チョークの粉
    for _ in range(14):
        px = py_rng.uniform(x0 + tray_h, x1 - tray_h)
        py = y0 + py_rng.uniform(0.15, 0.5) * tray_h
        r = py_rng.uniform(0.4, 1.1)
        d.ellipse([px - r, py - r, px + r, py + r], fill=(235, 235, 225, py_rng.randint(60, 140)))


def render_blank_board(size: tuple[int, int], seed: int = 0) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """木枠・影つきの空の黒板を描き、(画像, 板面の矩形) を返す。"""
    w, h = size
    rng = np.random.default_rng(seed)
    py_rng = random.Random(seed)
    short = min(w, h)
    pad = max(2, round(short * SHADOW_PAD_RATIO))
    frame = max(4, round(short * FRAME_RATIO))
    tray = max(4, round(short * TRAY_RATIO))

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    # ドロップシャドウ（右下に少しずらしてぼかす）
    outer = (pad, pad, w - pad, h - pad - tray // 2)
    shadow = Image.new("L", size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [outer[0] + pad * 0.3, outer[1] + pad * 0.6, outer[2] + pad * 0.3, outer[3] + tray // 2 + pad * 0.6],
        radius=frame, fill=150,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(pad * 0.5))
    canvas.paste((0, 0, 0, 255), (0, 0), shadow)

    # 木枠（上下は横木目、左右は縦木目）
    ow, oh = outer[2] - outer[0], outer[3] - outer[1]
    frame_img = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
    frame_img.paste(_render_wood((ow, frame), rng), (0, 0))
    frame_img.paste(_render_wood((ow, frame), rng), (0, oh - frame))
    frame_img.paste(_render_wood((frame, oh), rng, vertical=True), (0, 0))
    frame_img.paste(_render_wood((frame, oh), rng, vertical=True), (ow - frame, 0))
    frame_mask = Image.new("L", (ow, oh), 0)
    ImageDraw.Draw(frame_mask).rounded_rectangle([0, 0, ow - 1, oh - 1], radius=max(2, frame // 2), fill=255)
    canvas.paste(frame_img, (outer[0], outer[1]), frame_mask)

    # 板面
    board_box = (outer[0] + frame, outer[1] + frame, outer[2] - frame, outer[3] - frame)
    bw, bh = board_box[2] - board_box[0], board_box[3] - board_box[1]
    canvas.paste(_render_board_surface((bw, bh), rng, py_rng), board_box[:2])

    # 枠の内側の影（板面が一段へこんで見えるように）
    inner_shadow = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(inner_shadow).rectangle([0, 0, bw - 1, bh - 1], outline=255, width=max(2, frame // 3))
    inner_shadow = inner_shadow.filter(ImageFilter.GaussianBlur(max(1, frame / 3)))
    canvas.paste((0, 0, 0, 255), board_box[:2], inner_shadow.point(lambda v: v * 0.55))

    # チョーク受け（下枠から手前にせり出した棚）
    tray_box = (outer[0] + frame, outer[3] - tray // 2, outer[2] - frame, outer[3] + tray // 2)
    tw, th = tray_box[2] - tray_box[0], tray_box[3] - tray_box[1]
    tray_img = _render_wood((tw, th), rng)
    tray_img = Image.alpha_composite(tray_img, Image.new("RGBA", (tw, th), (255, 255, 255, 18)))
    canvas.paste(tray_img, tray_box[:2])
    ImageDraw.Draw(canvas).line([tray_box[0], tray_box[3], tray_box[2], tray_box[3]], fill=(40, 25, 12, 200), width=2)
    _draw_chalk_pieces(canvas, tray_box, py_rng)

    return canvas, board_box


def _render_whiteboard_surface(size: tuple[int, int], rng: np.random.Generator, py_rng: random.Random) -> Image.Image:
    """ホワイトボードの板面（ほぼ白 + 上から下へのわずかな陰影 + 光沢の映り込み + 消し残り）。"""
    w, h = size
    board = np.empty((h, w, 3), dtype=np.float32)
    board[:] = WHITEBOARD_COLOR
    board -= np.linspace(0, 9, h, dtype=np.float32)[:, None, None]  # 下に行くほどわずかに暗く
    board += _smooth_noise(rng, size, max(8, min(w, h) // 10))[..., None] * 2.0
    board += rng.normal(0, 1.2, (h, w, 1)).astype(np.float32)
    surface = Image.fromarray(board.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")

    # 消し残り: 以前書いた文字をイレーザーで消した跡のような、薄い灰色のうねった線
    ghost = Image.new("L", size, 0)
    gd = ImageDraw.Draw(ghost)
    for _ in range(py_rng.randint(4, 7)):
        x, y = py_rng.uniform(0, w), py_rng.uniform(0, h)
        points = [(x, y)]
        for _ in range(py_rng.randint(4, 9)):
            x += py_rng.uniform(w * 0.02, w * 0.07)
            y += py_rng.uniform(-h * 0.02, h * 0.02)
            points.append((x, y))
        gd.line(points, fill=py_rng.randint(18, 34), width=max(2, round(min(w, h) * 0.008)), joint="curve")
    ghost = ghost.filter(ImageFilter.GaussianBlur(min(w, h) * 0.006))
    surface.paste((120, 125, 135, 255), (0, 0), ghost)

    # 光沢: 斜めに入る、ぼんやりした白い映り込み
    gloss = Image.new("L", size, 0)
    gw = w * 0.22
    x0 = w * py_rng.uniform(0.45, 0.7)
    ImageDraw.Draw(gloss).polygon([(x0, 0), (x0 + gw, 0), (x0 + gw - h * 0.5, h), (x0 - h * 0.5, h)], fill=60)
    gloss = gloss.filter(ImageFilter.GaussianBlur(gw * 0.35))
    surface.paste((255, 255, 255, 255), (0, 0), gloss)
    return surface


def _render_aluminum(size: tuple[int, int], vertical: bool = False) -> Image.Image:
    """アルミ枠（断面方向に明→暗のグラデーション + ヘアライン）。"""
    w, h = size
    across = w if vertical else h
    length = h if vertical else w
    profile = np.interp(
        np.linspace(0, 1, across), [0, 0.25, 0.55, 1], [228, 206, 176, 142]
    ).astype(np.float32)[:, None]
    hairline = np.random.default_rng(across * 7 + length).normal(0, 2.2, (1, length)).astype(np.float32)
    tex = np.repeat((profile + hairline)[..., None], 3, axis=2) + np.array([0, 2, 6], dtype=np.float32)
    if vertical:
        tex = tex.transpose(1, 0, 2)
    return Image.fromarray(tex.clip(0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _draw_markers_and_eraser(canvas: Image.Image, tray_box: tuple[int, int, int, int], py_rng: random.Random) -> None:
    """トレイの上に、黒・青・赤のマーカーとイレーザーを置く。"""
    x0, y0, x1, y1 = tray_box
    tray_h = y1 - y0
    d = ImageDraw.Draw(canvas)
    body_h = max(4, round(tray_h * 0.55))
    top = y0 + (tray_h - body_h) * 0.3
    x = x1 - (x1 - x0) * py_rng.uniform(0.06, 0.12)
    for color in (MARKER_BLACK, MARKER_BLUE, MARKER_RED):
        length = min(tray_h * py_rng.uniform(4.2, 4.8), (x1 - x0) * 0.13)
        cap = length * 0.3
        d.rounded_rectangle([x - length, top, x, top + body_h], radius=body_h // 2, fill=(236, 236, 238, 255),
                            outline=(170, 172, 178, 255))
        d.rounded_rectangle([x - cap, top - 1, x, top + body_h + 1], radius=body_h // 2, fill=color + (255,))
        d.rectangle([x - length * 0.8, top + body_h * 0.3, x - length * 0.45, top + body_h * 0.7], fill=color + (255,))
        x -= length + tray_h * py_rng.uniform(0.6, 1.2)
    # イレーザー（左寄り）
    ex = x0 + (x1 - x0) * py_rng.uniform(0.08, 0.16)
    ew, eh = min(tray_h * 5.5, (x1 - x0) * 0.2), tray_h * 1.1
    et = y1 - eh - tray_h * 0.1
    d.rounded_rectangle([ex, et, ex + ew, et + eh * 0.72], radius=max(2, tray_h // 3), fill=(52, 56, 66, 255))
    d.rectangle([ex + 2, et + eh * 0.72, ex + ew - 2, et + eh], fill=(92, 94, 100, 255))
    d.line([ex + ew * 0.15, et + eh * 0.2, ex + ew * 0.85, et + eh * 0.2], fill=(90, 96, 110, 255), width=1)


def render_blank_whiteboard(size: tuple[int, int], seed: int = 0) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """アルミ枠・影・トレイつきの空のホワイトボードを描き、(画像, 板面の矩形) を返す。"""
    w, h = size
    rng = np.random.default_rng(seed)
    py_rng = random.Random(seed)
    short = min(w, h)
    pad = max(2, round(short * SHADOW_PAD_RATIO))
    frame = max(3, round(short * FRAME_RATIO * 0.6))  # 木枠より細いアルミ枠
    tray = max(4, round(short * TRAY_RATIO))

    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    outer = (pad, pad, w - pad, h - pad - tray // 2)

    shadow = Image.new("L", size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [outer[0] + pad * 0.3, outer[1] + pad * 0.6, outer[2] + pad * 0.3, outer[3] + tray // 2 + pad * 0.6],
        radius=frame * 2, fill=110,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(pad * 0.5))
    canvas.paste((0, 0, 0, 255), (0, 0), shadow)

    ow, oh = outer[2] - outer[0], outer[3] - outer[1]
    frame_img = Image.new("RGBA", (ow, oh), (0, 0, 0, 0))
    frame_img.paste(_render_aluminum((ow, frame)), (0, 0))
    frame_img.paste(_render_aluminum((ow, frame)).transpose(Image.FLIP_TOP_BOTTOM), (0, oh - frame))
    frame_img.paste(_render_aluminum((frame, oh), vertical=True), (0, 0))
    frame_img.paste(_render_aluminum((frame, oh), vertical=True).transpose(Image.FLIP_LEFT_RIGHT), (ow - frame, 0))
    frame_mask = Image.new("L", (ow, oh), 0)
    ImageDraw.Draw(frame_mask).rounded_rectangle([0, 0, ow - 1, oh - 1], radius=frame * 2, fill=255)
    canvas.paste(frame_img, (outer[0], outer[1]), frame_mask)

    board_box = (outer[0] + frame, outer[1] + frame, outer[2] - frame, outer[3] - frame)
    bw, bh = board_box[2] - board_box[0], board_box[3] - board_box[1]
    canvas.paste(_render_whiteboard_surface((bw, bh), rng, py_rng), board_box[:2])

    inner_shadow = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(inner_shadow).rectangle([0, 0, bw - 1, bh - 1], outline=255, width=max(1, frame // 3))
    inner_shadow = inner_shadow.filter(ImageFilter.GaussianBlur(max(1, frame / 2)))
    canvas.paste((0, 0, 0, 255), board_box[:2], inner_shadow.point(lambda v: v * 0.25))

    # 枠の四隅のプラスチックのコーナーキャップ
    cd = ImageDraw.Draw(canvas)
    cap = round(frame * 2.2)
    for cx, cy in ((outer[0], outer[1]), (outer[2] - cap, outer[1]), (outer[0], outer[3] - cap), (outer[2] - cap, outer[3] - cap)):
        cd.rounded_rectangle([cx, cy, cx + cap, cy + cap], radius=max(2, cap // 3), fill=(78, 82, 92, 255))

    # トレイ（下枠の手前に付いたアルミの受け皿）
    tray_box = (outer[0] + cap, outer[3] - tray // 2, outer[2] - cap, outer[3] + tray // 2)
    tw, th = tray_box[2] - tray_box[0], tray_box[3] - tray_box[1]
    canvas.paste(_render_aluminum((tw, th)), tray_box[:2])
    cd.line([tray_box[0], tray_box[3], tray_box[2], tray_box[3]], fill=(90, 92, 100, 220), width=2)
    _draw_markers_and_eraser(canvas, tray_box, py_rng)

    return canvas, board_box


# ---------------------------------------------------------------------------
# チョーク文字 / マーカー文字
# ---------------------------------------------------------------------------

def _chalkify(mask: Image.Image, rng: np.random.Generator) -> Image.Image:
    """白黒の文字マスクを、かすれ・粉のにじみのあるチョークの質感のアルファに変換する。"""
    m = np.asarray(mask, dtype=np.float32) / 255.0
    h, w = m.shape
    # 筆圧のムラ（横方向に少し伸びた粒で、チョークを擦ったような筋を出す）
    streak = rng.uniform(0.0, 1.0, (h, max(1, w // 3))).astype(np.float32)
    streak = np.asarray(
        Image.fromarray((streak * 255).astype(np.uint8), "L").resize((w, h), Image.BILINEAR), dtype=np.float32
    ) / 255.0
    fine = rng.uniform(0.0, 1.0, (h, w)).astype(np.float32)
    # 2px程度の粒（1px単位のノイズだと縮小表示で潰れて見えなくなるため）
    grain = rng.uniform(0.0, 1.0, (max(1, h // 2), max(1, w // 2))).astype(np.float32)
    grain = np.asarray(
        Image.fromarray((grain * 255).astype(np.uint8), "L").resize((w, h), Image.NEAREST), dtype=np.float32
    ) / 255.0
    texture = 0.5 + 0.25 * streak + 0.15 * grain + 0.1 * fine
    texture[grain < 0.08] *= 0.3  # ところどころ粉が乗らずに抜ける（読みやすさ優先で控えめに）
    # 文字の輪郭をわずかにガサつかせる（輪郭付近ほど抜けやすくする）
    edge = np.asarray(mask.filter(ImageFilter.GaussianBlur(1.2)), dtype=np.float32) / 255.0
    body = m * np.clip(texture, 0, 1) * np.clip(edge * 1.6, 0, 1)

    # 周囲に薄い粉のにじみ
    halo = np.asarray(mask.filter(ImageFilter.GaussianBlur(max(1.0, h * 0.03))), dtype=np.float32) / 255.0
    alpha = np.clip(body * 1.1 + halo * 0.22, 0, 1)
    out = Image.fromarray((alpha * 255).astype(np.uint8), "L")
    return out.filter(ImageFilter.GaussianBlur(0.45))


def _markerify(mask: Image.Image, rng: np.random.Generator) -> Image.Image:
    """白黒の文字マスクを、ホワイトボードマーカーのインクの質感のアルファに変換する。

    チョークと違って粉っぽさはなく、ほぼベタ塗りだが、インクの乗りのムラ（横方向の
    かすかな筋）と、わずかな半透明感を出す。
    """
    m = np.asarray(mask, dtype=np.float32) / 255.0
    h, w = m.shape
    streak = rng.uniform(0.0, 1.0, (max(1, h // 3), max(1, w // 10))).astype(np.float32)
    streak = np.asarray(
        Image.fromarray((streak * 255).astype(np.uint8), "L").resize((w, h), Image.BICUBIC), dtype=np.float32
    ) / 255.0
    alpha = m * np.clip(0.8 + 0.2 * streak, 0, 1)
    out = Image.fromarray((alpha * 255).astype(np.uint8), "L")
    return out.filter(ImageFilter.GaussianBlur(0.4))


InkFunction = Callable[[Image.Image, np.random.Generator], Image.Image]


@dataclass(frozen=True)
class BoardStyle:
    """板の種類ごとの見た目（板の描画・インクの質感・色・フォント）。"""
    name: str
    render_board: Callable[[tuple[int, int], int], tuple[Image.Image, tuple[int, int, int, int]]]
    ink: InkFunction
    title_color: tuple[int, int, int]
    body_color: tuple[int, int, int]
    emphasis_color: tuple[int, int, int]
    underline_color: tuple[int, int, int]
    bullet_color: tuple[int, int, int]
    resolve_font: Callable[[], Optional[str]]


CHALKBOARD = BoardStyle(
    name="chalkboard", render_board=render_blank_board, ink=_chalkify,
    title_color=CHALK_YELLOW, body_color=CHALK_WHITE, emphasis_color=CHALK_PINK,
    underline_color=CHALK_YELLOW, bullet_color=CHALK_YELLOW, resolve_font=resolve_chalk_font_path,
)
WHITEBOARD = BoardStyle(
    name="whiteboard", render_board=render_blank_whiteboard, ink=_markerify,
    title_color=MARKER_BLUE, body_color=MARKER_BLACK, emphasis_color=MARKER_RED,
    underline_color=MARKER_RED, bullet_color=MARKER_BLUE, resolve_font=resolve_marker_font_path,
)
STYLES: dict[str, BoardStyle] = {st.name: st for st in (CHALKBOARD, WHITEBOARD)}


def style_for_resolution(resolution: tuple[int, int]) -> BoardStyle:
    """横画面は黒板、縦画面（ショート動画）はホワイトボード。"""
    return WHITEBOARD if resolution[1] > resolution[0] else CHALKBOARD


def _render_text_line(
    runs: list[Run],
    font: ImageFont.FreeTypeFont,
    font_size: int,
    colors: tuple[tuple[int, int, int], tuple[int, int, int]],
    rng: np.random.Generator,
    angle: float,
    ink: InkFunction,
) -> tuple[Image.Image, int]:
    """1行分の手書き文字を描き、(RGBA画像, 画像内のベースラインのy座標) を返す。

    colors は (通常色, 強調色)。同じ行の通常部分・強調部分は同じ傾き・同じノイズで描く。
    ink はマスクを質感つきアルファに変換する関数（_chalkify / _markerify）。
    """
    text = "".join(t for t, _ in runs)
    width = max(1, round(font.getlength(text))) + font_size
    height = round(font_size * 1.6)
    baseline = round(font_size * 1.15)
    margin = font_size // 2

    masks = {False: Image.new("L", (width, height), 0), True: Image.new("L", (width, height), 0)}
    x = margin
    for chunk, emphasized in runs:
        ImageDraw.Draw(masks[emphasized]).text(
            (x, baseline), chunk, font=font, fill=255, anchor="ls",
            stroke_width=max(1, round(font_size * BOLD_STROKE_RATIO)), stroke_fill=255,
        )
        x += font.getlength(chunk)

    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    seed = int(rng.integers(0, 2**31))
    for emphasized, color in ((False, colors[0]), (True, colors[1])):
        if masks[emphasized].getbbox() is None:
            continue
        alpha = ink(masks[emphasized], np.random.default_rng(seed))
        solid = Image.new("RGBA", (width, height), color + (255,))
        solid.putalpha(alpha)
        layer = Image.alpha_composite(layer, solid)

    if abs(angle) > 0.01:
        rotated = layer.rotate(angle, resample=Image.BICUBIC, expand=True)
        baseline += (rotated.height - height) // 2
        layer = rotated
    return layer, baseline


def _render_underline(
    width: int, thickness: int, color, rng: np.random.Generator, py_rng: random.Random, ink: InkFunction
) -> Image.Image:
    """手で引いたような、少し波打つ下線。"""
    h = thickness * 5
    mask = Image.new("L", (width, h), 0)
    d = ImageDraw.Draw(mask)
    points = []
    steps = 12
    drift = py_rng.uniform(-thickness, thickness)
    for i in range(steps + 1):
        x = width * i / steps
        y = h / 2 + drift * (i / steps) + py_rng.uniform(-thickness * 0.35, thickness * 0.35)
        points.append((x, y))
    d.line(points, fill=255, width=thickness, joint="curve")
    alpha = ink(mask, rng)
    solid = Image.new("RGBA", (width, h), color + (255,))
    solid.putalpha(alpha)
    return solid


def _draw_bullet_marker(
    canvas: Image.Image, center: tuple[float, float], radius: float, rng: np.random.Generator, style: "BoardStyle"
) -> None:
    """箇条書きの行頭の「●」を手描きする（少し歪んだ円）。"""
    size = int(radius * 4) + 4
    mask = Image.new("L", (size, size), 0)
    c = size / 2
    rx, ry = radius * rng.uniform(0.9, 1.1), radius * rng.uniform(0.9, 1.1)
    ImageDraw.Draw(mask).ellipse([c - rx, c - ry, c + rx, c + ry], fill=255)
    solid = Image.new("RGBA", (size, size), style.bullet_color + (255,))
    solid.putalpha(style.ink(mask, rng))
    canvas.alpha_composite(solid, (round(center[0] - c), round(center[1] - c)))


# ---------------------------------------------------------------------------
# スライド全体
# ---------------------------------------------------------------------------

def _one_line(text: str) -> list[list[Run]]:
    """強調記法付きの1項目を、折り返さずに1行として扱う。"""
    plain, flags = _parse_emphasis(text)
    return [_to_runs(plain, flags)]


WRAP_MIN_SIZE_RATIO = 0.8  # 1行に収めると通常の文字サイズのこの割合より小さくなる項目は、2行に分ける
# 「英文 ― 日本語訳」のような対になった項目の区切り（英単語の中のハイフン check-in などでは区切らない）
_PAIR_SEPARATOR = re.compile(r"\s+[-–—―]{1,2}\s+|\s*[―—]{1,2}\s*")


def split_point(plain: str) -> Optional[int]:
    """1行を2行に分けるのにいちばん自然な位置（後ろの行の先頭の文字位置）。分けられなければ None。

    英語が中心の文は単語と単語の間（スペース）で、日本語の文は句読点・助詞のあとなど、字幕と同じ基準
    （telop._break_penalty）で選ぶ。英単語の途中・「」の途中では分けない。真ん中に近い位置を優先する。
    """
    n = len(plain)
    if n < 6:
        return None
    # 日本語（かな・漢字）が1文字も無い文だけを英文として扱う（「Could you〜?は、」のような混ざった文は日本語扱い）
    english = not any(ord(ch) >= 0x3000 and ch not in "〜　" for ch in plain)
    # かっこ（「」『』（））の中では切らない
    depth, inside = 0, [False] * (n + 1)
    for k, ch in enumerate(plain):
        if ch in "「『（(":
            depth += 1
        elif ch in "」』）)":
            depth = max(0, depth - 1)
        inside[k + 1] = depth > 0
    best, best_score = None, float("inf")
    lo, hi = (0.25, 0.75) if english else (0.2, 0.8)
    for i in range(max(1, int(n * lo)), min(n - 1, int(n * hi)) + 1):
        before, after = plain[i - 1], plain[i]
        if english:
            if before != " ":
                continue
            score = abs(i - n / 2)
        else:
            if inside[i] or after in "」』）)、。，,！？!?：:":
                continue  # かっこの中・行頭に来てはいけない記号の前
            if before.isascii() and after.isascii() and (before.isalnum() or before in "'-") and after.isalnum():
                continue  # 日本語の文の中の英単語の途中
            prev = plain[i - 2] if i >= 2 else ""
            if before == " " and prev.isascii() and (prev.isalnum() or prev in "?!.,'〜~") and after.isascii():
                continue  # 日本語の文の中の英語のフレーズの途中（例: Can you）
            if before in "。！？!?" and "ぁ" <= after <= "ん":
                penalty = 4.0  # 「〜?は、」のように、記号のすぐあとに助詞が続く（まだ文の途中）
            elif before in " 　。！？!?：:" or (before in "）)」』" and after in " 　"):
                penalty = 0.0  # 空白・文の終わり・コロンのあと（いちばん自然な切れ目）
            elif before in "、，," or after in "（(「『":
                penalty = 1.0  # 読点のあと・かっこの前
            elif prev in "」』）)" and before in "とではがをにもの":
                penalty = 0.5  # 「〜」と / 「〜」で のように、かっこ＋助詞のあと
            else:
                penalty = telop._break_penalty(plain, i) + 2.0
            score = penalty + abs(i - n / 2) * 0.15
        if score < best_score:
            best, best_score = i, score
    return best


def _two_rows(plain: str, flags: list[bool], i: int) -> list[tuple[str, list[bool]]]:
    left_end, right_start = i, i
    while left_end > 0 and plain[left_end - 1] == " ":
        left_end -= 1
    while right_start < len(plain) and plain[right_start] == " ":
        right_start += 1
    return [(plain[:left_end], flags[:left_end]), (plain[right_start:], flags[right_start:])]


def _bullet_rows(text: str, wrap: bool = False) -> list[tuple[str, list[bool]]]:
    """箇条書き1項目を、黒板に書く行（文字列, 強調フラグ）に分ける。

    「英文 ― 日本語訳」のように対になった項目は、いつも英文と訳の2行に分ける。
    wrap=True の項目（1行に収めると小さくなりすぎるもの）は、自然な位置で2行に分ける。
    """
    plain, flags = _parse_emphasis(text)
    m = _PAIR_SEPARATOR.search(plain)
    if m and 0 < m.start() and m.end() < len(plain):
        return [(plain[:m.start()], flags[:m.start()]), (plain[m.end():], flags[m.end():])]
    if wrap:
        i = split_point(plain)
        if i:
            return _two_rows(plain, flags, i)
    return [(plain, flags)]


def _text_width_at(texts: list[str], font_path: Optional[str], size: int) -> float:
    """size の文字で書いたときの、texts のうち一番長い1行の幅（太字化の縁取りぶんも含む）。"""
    font = telop.load_font(font_path, size)
    widest = max((font.getlength(_parse_emphasis(t)[0]) for t in texts), default=0.0)
    return widest + size * BOLD_STROKE_RATIO * 2


def _fit_one_line_size(texts: list[str], font_path: Optional[str], size: int, max_width_for) -> int:
    """texts のどれもが1行に収まる文字サイズを返す（収まらなければ小さくする。折り返しはしない）。

    max_width_for(size) はその文字サイズで使える幅（箇条書きは行頭記号のぶん幅が文字サイズで変わるため関数で渡す）。
    """
    for _ in range(8):
        widest = _text_width_at(texts, font_path, size)
        limit = max_width_for(size) * ONE_LINE_SAFETY
        if widest <= limit or size <= MIN_FONT_PX:
            break
        size = max(MIN_FONT_PX, int(size * limit / widest))
    return size


def _layout(title: str, bullets: list[str], font_path: Optional[str], board_w: int, board_h: int, scale: float,
            numbered: bool = False):
    """指定スケールで見出し・箇条書きを配置し、必要な高さと描画情報を返す。

    見出しも箇条書きも1項目＝1行（折り返さない）。1行に収まらない場合は文字を小さくする。
    箇条書きは全項目を同じ文字サイズにそろえる（一番長い項目が1行に収まるサイズ）。
    """
    size_base = min(board_h, board_w / SIZE_BASE_MAX_WIDTH_DIVISOR)
    avail_w = board_w * (1 - PADDING_X_RATIO * 2)
    title_size = max(MIN_FONT_PX, round(size_base * TITLE_SIZE_RATIO * scale))
    body_size = max(MIN_FONT_PX, round(size_base * BODY_SIZE_RATIO * scale))
    if title:
        title_size = _fit_one_line_size([title], font_path, title_size, lambda _s: avail_w)
    indent_ratio = NUMBER_INDENT_RATIO if numbered else 1.1  # 番号（「10.」まで）は「・」より幅を取る
    body_width = lambda s: avail_w - round(s * indent_ratio)  # noqa: E731
    rows = [_bullet_rows(b) for b in bullets]
    if bullets:
        one_line = _fit_one_line_size([t for r in rows for t, _ in r], font_path, body_size, body_width)
        if one_line < body_size * WRAP_MIN_SIZE_RATIO:
            # 1行だと小さくなりすぎる項目だけ、自然な位置で2行に分ける（全部を小さくして読めなくなるのを防ぐ）
            readable = round(body_size * WRAP_MIN_SIZE_RATIO)
            rows = [
                _bullet_rows(b, wrap=len(r) == 1 and _text_width_at([r[0][0]], font_path, readable) > body_width(readable))
                for b, r in zip(bullets, rows)
            ]
        body_size = _fit_one_line_size([t for r in rows for t, _ in r], font_path, body_size, body_width)
    title_font = telop.load_font(font_path, title_size)
    body_font = telop.load_font(font_path, body_size)
    indent = round(body_size * indent_ratio)

    title_lines = _one_line(title) if title else []
    bullet_lines = [[_to_runs(t, f) for t, f in r] for r in rows]

    height = 0.0
    if title_lines:
        height += title_size * 1.3 + title_size * 0.35  # 見出し + 下線
    if title_lines and bullet_lines:
        height += body_size * 0.8
    height += sum(len(lines) for lines in bullet_lines) * body_size * LINE_SPACING
    if bullet_lines:
        height += (len(bullet_lines) - 1) * body_size * 0.25  # 項目間の余白
    return {
        "title_size": title_size, "body_size": body_size,
        "title_font": title_font, "body_font": body_font,
        "title_lines": title_lines, "bullet_lines": bullet_lines,
        "indent": indent, "height": height,
    }


def render_slide(
    title: str,
    bullets: list[str],
    size: tuple[int, int],
    font_path: Optional[str] = None,
    seed: Optional[int] = None,
    style: BoardStyle = CHALKBOARD,
    visible_bullets: Optional[int] = None,
    numbered: bool = False,
) -> Image.Image:
    """黒板風（またはホワイトボード風）スライドを1枚描画して返す（RGBA・余白部分は透明）。

    visible_bullets を指定すると、箇条書きを先頭からその数だけ描く（セリフの進行に合わせて1行ずつ
    書き足していく演出用）。文字の大きさ・配置は常に全項目ぶんで計算するため、項目数を変えて
    描いた画像同士を重ねても、既に書いてある部分の位置・見た目は1ピクセルも変わらない。
    """
    title = telop.strip_emoji(normalize_title(title))  # フォントに無い記号は置き換える・取り除く（豆腐を防ぐ）
    bullets = [telop.strip_emoji(b) for b in normalize_bullets(bullets or [])]
    font_path = font_path if font_path is not None else style.resolve_font()
    if seed is None:
        seed = int(hashlib.sha1(json.dumps([title, bullets, size]).encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed + 1)
    py_rng = random.Random(seed + 1)

    canvas, board_box = style.render_board(size, seed)
    bx0, by0, bx1, by1 = board_box
    bw, bh = bx1 - bx0, by1 - by0
    pad_x, pad_y = round(bw * PADDING_X_RATIO), round(bh * PADDING_Y_RATIO)
    avail_h = bh - pad_y * 2

    scale = MAX_SCALE
    layout = _layout(title, bullets, font_path, bw, bh, scale, numbered)
    while layout["height"] > avail_h and scale > MIN_SCALE:
        scale *= 0.92
        layout = _layout(title, bullets, font_path, bw, bh, scale, numbered)

    # 内容が少ないときは上下中央寄せ（ただし少し上寄りに置いた方が黒板らしく見える）
    y = by0 + pad_y + max(0.0, (avail_h - layout["height"]) * 0.4)
    ts, bs = layout["title_size"], layout["body_size"]

    def jitter_angle() -> float:
        return py_rng.uniform(-0.7, 0.7)

    # 見出し（中央揃え・下線つき）
    if layout["title_lines"]:
        max_line_w = 0.0
        for runs in layout["title_lines"]:
            line_w = layout["title_font"].getlength("".join(t for t, _ in runs))
            max_line_w = max(max_line_w, line_w)
            img, baseline = _render_text_line(
                runs, layout["title_font"], ts, (style.title_color, style.emphasis_color), rng, jitter_angle(), style.ink
            )
            x = bx0 + (bw - line_w) / 2 - ts // 2 - (img.width - line_w - ts) / 2
            target_baseline = y + ts * 1.05 + py_rng.uniform(-ts * 0.03, ts * 0.03)
            canvas.alpha_composite(img, (round(x), round(target_baseline - baseline)))
            y += ts * 1.3
        underline_w = round(min(bw - pad_x * 2, max_line_w + ts))
        underline = _render_underline(
            underline_w, max(2, round(ts * 0.07)), style.underline_color, rng, py_rng, style.ink
        )
        canvas.alpha_composite(underline, (round(bx0 + (bw - underline_w) / 2), round(y - underline.height * 0.35)))
        y += ts * 0.35
        if layout["bullet_lines"]:
            y += bs * 0.8

    # 箇条書き（左揃え。全体の幅が狭いときはブロックごと中央に寄せる）
    if layout["bullet_lines"]:
        body_font = layout["body_font"]
        block_w = max(
            body_font.getlength("".join(t for t, _ in runs))
            for lines in layout["bullet_lines"] for runs in lines
        ) + layout["indent"]
        left = bx0 + max(pad_x, (bw - block_w) / 2)
        for bullet_no, lines in enumerate(layout["bullet_lines"]):
            if visible_bullets is not None and bullet_no >= visible_bullets:
                break  # まだ書いていない項目（以降の乱数を使わないので、書いた部分の見た目は変わらない）
            for line_no, runs in enumerate(lines):
                target_baseline = y + bs * 1.1 + py_rng.uniform(-bs * 0.04, bs * 0.04)
                if line_no == 0 and numbered:
                    # 番号付き（1. 2. 3.）: 行頭の番号を、見出しの下線と同じ色のチョーク/マーカーで書く
                    num_img, num_base = _render_text_line(
                        [(f"{bullet_no + 1}.", False)], body_font, bs, (style.bullet_color, style.bullet_color),
                        rng, jitter_angle() * 0.6, style.ink,
                    )
                    canvas.alpha_composite(num_img, (round(left - bs // 2), round(target_baseline - num_base)))
                elif line_no == 0:
                    _draw_bullet_marker(canvas, (left + bs * 0.35, target_baseline - bs * 0.33), bs * 0.16, rng, style)
                img, baseline = _render_text_line(
                    runs, body_font, bs, (style.body_color, style.emphasis_color), rng, jitter_angle() * 0.6, style.ink
                )
                x = left + layout["indent"] - bs // 2 + py_rng.uniform(-bs * 0.05, bs * 0.05)
                canvas.alpha_composite(img, (round(x), round(target_baseline - baseline)))
                y += bs * LINE_SPACING
            y += bs * 0.25

    return canvas


def slide_size_for(resolution: tuple[int, int]) -> tuple[int, int]:
    """動画解像度に対して、資料メディア枠にぴったり収まるスライド画像のサイズを返す。"""
    max_w, max_h = content_media_max_size(resolution)
    return max(64, max_w), max(64, max_h)


def get_slide_path(
    title: str, bullets: list[str], resolution: tuple[int, int], visible_bullets: Optional[int] = None,
    numbered: bool = False,
) -> Path:
    """スライド画像を生成（またはキャッシュから取得）し、PNGのパスを返す。

    板の種類は解像度から自動で決まる（横画面=黒板、縦画面=ホワイトボード）。
    visible_bullets は表示する箇条書きの数（None または項目数以上なら全部）。乱数のシードは表示数に
    関係なく内容から決めるため、表示数だけが違う画像同士は書いてある部分が完全に一致する。
    """
    size = slide_size_for(resolution)
    style = style_for_resolution(resolution)
    font_path = style.resolve_font()
    normalized = normalize_bullets(bullets)
    if visible_bullets is not None and visible_bullets >= len(normalized):
        visible_bullets = None
    key = json.dumps([STYLE_VERSION, style.name, title.strip(), normalized, size, font_path]
                     + (["numbered"] if numbered else []), ensure_ascii=False)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    suffix = "" if visible_bullets is None else f"_{max(0, visible_bullets)}of{len(normalized)}"
    path = SLIDE_CACHE_DIR / f"slide_{digest}{suffix}.png"
    if not path.exists():
        SLIDE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        render_slide(
            title, bullets, size, font_path=font_path, seed=int(digest[:8], 16), style=style,
            visible_bullets=visible_bullets, numbered=numbered,
        ).save(path)
    return path


CARD_STYLE_VERSION = 2
CARD_BORDER_COLOR = (235, 228, 214, 255)
CARD_CAPTION_COLOR = (45, 45, 55)
CARD_MARKER_COLOR = (255, 226, 90, 200)   # 説明文の下半分に引く蛍光ペン風のマーカー


def render_illustration_card(image_path: str, caption: str, size: tuple[int, int]) -> Image.Image:
    """イラストを白いカードに載せ、下に短い説明（蛍光ペン風のマーカー付き）を添えた画像を作る。

    画面中央の資料エリアいっぱいに表示する前提で、size はその枠のサイズ（slide_size_for()）。
    カードは枠の中央に置き、縦長・横長どちらの枠でも見やすい縦横比にする。
    """
    w, h = size
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    card_h = h * 0.94
    card_w = min(w * 0.94, card_h * 1.25) if w > h else w * 0.94
    x0, y0 = (w - card_w) / 2, (h - card_h) / 2
    radius = round(min(card_w, card_h) * 0.05)
    box = [round(x0), round(y0), round(x0 + card_w), round(y0 + card_h)]

    shadow = Image.new("L", size, 0)
    offset = round(min(w, h) * 0.012)
    ImageDraw.Draw(shadow).rounded_rectangle([box[0] + offset, box[1] + offset * 2, box[2] + offset, box[3] + offset * 2],
                                             radius=radius, fill=110)
    shadow = shadow.filter(ImageFilter.GaussianBlur(offset * 1.5))
    canvas.paste((0, 0, 0, 255), (0, 0), shadow)
    ImageDraw.Draw(canvas).rounded_rectangle(box, radius=radius, fill=(255, 255, 255, 255),
                                             outline=CARD_BORDER_COLOR, width=max(3, round(card_h * 0.008)))

    caption = telop.strip_emoji(caption or "")
    pad = card_h * 0.05
    caption_h = card_h * 0.17 if caption else 0
    area_w, area_h = card_w - pad * 2, card_h - pad * 2 - caption_h
    try:
        illust = Image.open(image_path).convert("RGBA")
        scale = min(area_w / illust.width, area_h / illust.height)
        illust = illust.resize((max(1, round(illust.width * scale)), max(1, round(illust.height * scale))), Image.LANCZOS)
        canvas.alpha_composite(illust, (round(x0 + (card_w - illust.width) / 2), round(y0 + pad + (area_h - illust.height) / 2)))
    except (OSError, ValueError):
        pass  # 読めない画像は説明文だけのカードにする

    if caption:
        font_path = telop.resolve_font_path()
        size_px = round(caption_h * 0.62)
        font = telop.load_font(font_path, size_px)
        stroke = max(1, round(size_px * 0.04))
        text_w = font.getlength(caption)
        if text_w > area_w:
            size_px = max(10, int(size_px * area_w / text_w))
            font = telop.load_font(font_path, size_px)
            text_w = font.getlength(caption)
        cx, cy = x0 + card_w / 2, y0 + card_h - pad - caption_h / 2
        marker = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(marker).rounded_rectangle(
            [cx - text_w / 2 - size_px * 0.2, cy, cx + text_w / 2 + size_px * 0.2, cy + size_px * 0.55],
            radius=round(size_px * 0.15), fill=CARD_MARKER_COLOR,
        )
        canvas.alpha_composite(marker)
        ImageDraw.Draw(canvas).text((cx, cy), caption, font=font, fill=CARD_CAPTION_COLOR, anchor="mm",
                                    stroke_width=stroke, stroke_fill=CARD_CAPTION_COLOR)
    return canvas


ILLUSTRATION_IMAGE_VERSION = 1


def get_illustration_image_path(image_path: str, resolution: tuple[int, int]) -> Path:
    """イメージイラストを、周りの透明な余白を切り落として表示枠の大きさに合わせた画像にし、PNGのパスを返す。

    白いカードや説明文は付けず、画像そのものを大きく出す（compositor.place_content_media() が
    黒板より大きな枠に置く）。小さい素材も、表示枠いっぱいまで拡大しておく（毎フレームの拡大を避ける）。
    """
    from src.services.compositor import ILLUSTRATION_FILE_PREFIX, _fit_size, illustration_max_size

    max_w, max_h = illustration_max_size(resolution)
    try:
        mtime = Path(image_path).stat().st_mtime
    except OSError:
        mtime = 0
    key = json.dumps([ILLUSTRATION_IMAGE_VERSION, str(image_path), mtime, max_w, max_h], ensure_ascii=False)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    path = SLIDE_CACHE_DIR / f"{ILLUSTRATION_FILE_PREFIX}{digest}.png"
    if not path.exists():
        SLIDE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        img = Image.open(image_path).convert("RGBA")
        box = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
        if box:
            img = img.crop(box)
        img = img.resize(_fit_size(img.size, max_w, max_h), Image.LANCZOS)
        img.save(path)
    return path


def get_illustration_card_path(image_path: str, caption: str, resolution: tuple[int, int]) -> Path:
    """イラストのカード画像を生成（またはキャッシュから取得）し、PNGのパスを返す。"""
    size = slide_size_for(resolution)
    try:
        mtime = Path(image_path).stat().st_mtime
    except OSError:
        mtime = 0
    key = json.dumps([CARD_STYLE_VERSION, str(image_path), mtime, caption.strip(), size], ensure_ascii=False)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    path = SLIDE_CACHE_DIR / f"card_{digest}.png"
    if not path.exists():
        SLIDE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        render_illustration_card(image_path, caption, size).save(path)
    return path


# ---------------------------------------------------------------------------
# 重要な表現の解説カード（文の大事な部分に赤い下線 → 矢印 → 意味・使い方）
# ---------------------------------------------------------------------------

NOTE_VERSION = 3
CHALK_RED = (255, 112, 112)
NOTE_LABEL = "ここに注目！"
NOTE_SENTENCE_SIZE_RATIO = 0.15   # 文の文字サイズ（板の高さに対する比率。長い文は幅に合わせて小さくする）
NOTE_MEANING_SIZE_RATIO = 0.62    # 意味の文字サイズ（文の文字サイズに対する比率）
NOTE_MEANING_MIN_RATIO = 0.075    # 意味の文字サイズの下限（板の高さに対する比率。文が長くて小さくなっても読めるように）


def _find_focus(sentence: str, focus: str) -> tuple[int, int]:
    """文の中の、下線を引く部分の位置 [開始, 終了)。見つからなければ文全体。"""
    focus = (focus or "").strip()
    if focus:
        start = sentence.find(focus)
        if start < 0:
            start = sentence.lower().find(focus.lower())
        if start >= 0:
            return start, start + len(focus)
    return 0, len(sentence)


def render_phrase_note(sentence: str, focus: str, meaning: str, size: tuple[int, int],
                       style: BoardStyle = CHALKBOARD, font_path: Optional[str] = None,
                       seed: int = 0) -> Image.Image:
    """重要な表現の解説カード: 黒板（縦画面はホワイトボード）に文を大きく書き、大事な部分に赤い下線を引いて、
    その下に矢印と意味・使い方を書く。"""
    sentence = telop.strip_emoji(sentence).strip()
    meaning = telop.strip_emoji(meaning).strip()
    font_path = font_path if font_path is not None else style.resolve_font()
    rng = np.random.default_rng(seed + 7)
    py_rng = random.Random(seed + 7)
    red = MARKER_RED if style is WHITEBOARD else CHALK_RED

    canvas, (bx0, by0, bx1, by1) = style.render_board(size, seed)
    bw, bh = bx1 - bx0, by1 - by0
    pad_x, pad_y = round(bw * PADDING_X_RATIO), round(bh * PADDING_Y_RATIO)
    avail_w = bw - pad_x * 2

    # 見出し（左上に小さく）
    label_size = max(MIN_FONT_PX, round(bh * 0.075))
    label_font = telop.load_font(font_path, label_size)
    img, base = _render_text_line([(NOTE_LABEL, False)], label_font, label_size,
                                  (style.title_color, style.title_color), rng, -1.0, style.ink)
    canvas.alpha_composite(img, (round(bx0 + pad_x - label_size // 2), round(by0 + pad_y + label_size * 1.05 - base)))

    # 文（中央・下線を引く部分は強調色）
    start, end = _find_focus(sentence, focus)
    size_s = _fit_one_line_size([sentence], font_path, max(MIN_FONT_PX, round(bh * NOTE_SENTENCE_SIZE_RATIO)),
                                lambda _s: avail_w)
    font = telop.load_font(font_path, size_s)
    runs = [r for r in ((sentence[:start], False), (sentence[start:end], True), (sentence[end:], False)) if r[0]]
    text_w = font.getlength(sentence)
    img, base = _render_text_line(runs, font, size_s, (style.body_color, red), rng, 0.0, style.ink)
    baseline_y = by0 + bh * 0.46
    x = bx0 + (bw - text_w) / 2 - size_s // 2
    canvas.alpha_composite(img, (round(x), round(baseline_y - base)))

    # 赤い下線（手書き風）
    ux = x + size_s // 2 + font.getlength(sentence[:start])
    uw = max(8, round(font.getlength(sentence[start:end])))
    thickness = max(3, round(size_s * 0.08))
    underline = _render_underline(uw, thickness, red, rng, py_rng, style.ink)
    uy = baseline_y + size_s * 0.12
    canvas.alpha_composite(underline, (round(ux), round(uy - underline.height / 2)))

    if not meaning:
        return canvas

    # 矢印（下線の真ん中から下へ）
    size_m = max(MIN_FONT_PX, round(size_s * NOTE_MEANING_SIZE_RATIO), round(bh * NOTE_MEANING_MIN_RATIO))
    ax = ux + uw / 2
    ay0, ay1 = uy + thickness * 2, uy + thickness * 2 + size_s * 0.75
    mask = Image.new("L", canvas.size, 0)
    d = ImageDraw.Draw(mask)
    d.line([(ax, ay0), (ax, ay1)], fill=255, width=thickness)
    head = size_s * 0.22
    d.polygon([(ax - head, ay1 - head), (ax + head, ay1 - head), (ax, ay1 + head * 0.4)], fill=255)
    arrow = Image.new("RGBA", canvas.size, red + (255,))
    arrow.putalpha(style.ink(mask, rng))
    canvas.alpha_composite(arrow)

    # 意味・使い方（1〜2行。入らなければ小さくする）
    lines = [meaning]
    font_m = telop.load_font(font_path, size_m)
    if font_m.getlength(meaning) > avail_w:
        i = split_point(meaning)  # 単語の途中・（）の途中では切らず、自然な位置で2行に
        if i:
            lines = [meaning[:i].rstrip(), meaning[i:].lstrip()]
    size_m = _fit_one_line_size(lines, font_path, size_m, lambda _s: avail_w)
    font_m = telop.load_font(font_path, size_m)
    y = ay1 + head + size_m * 1.1
    for line in lines:
        img, base = _render_text_line([(line, False)], font_m, size_m, (style.title_color, style.title_color),
                                      rng, py_rng.uniform(-0.5, 0.5), style.ink)
        lx = bx0 + (bw - font_m.getlength(line)) / 2 - size_m // 2
        lx = min(max(lx, bx0 + pad_x - size_m // 2), bx1 - pad_x - font_m.getlength(line) - size_m // 2)
        canvas.alpha_composite(img, (round(lx), round(y - base)))
        y += size_m * LINE_SPACING
    return canvas


def get_note_path(sentence: str, focus: str, meaning: str, resolution: tuple[int, int]) -> Path:
    """解説カードの画像を生成（またはキャッシュから取得）し、PNGのパスを返す。"""
    size = slide_size_for(resolution)
    style = style_for_resolution(resolution)
    font_path = style.resolve_font()
    key = json.dumps([NOTE_VERSION, STYLE_VERSION, style.name, sentence, focus, meaning, size, font_path],
                     ensure_ascii=False)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    path = SLIDE_CACHE_DIR / f"note_{digest}.png"
    if not path.exists():
        SLIDE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        render_phrase_note(sentence, focus, meaning, size, style, font_path, seed=int(digest[:8], 16)).save(path)
    return path


def resolve_scene_content_media(scene: Scene, resolution: tuple[int, int]) -> Optional[str]:
    """シーンの資料メディアとして実際に表示するパスを返す。

    スライドの内容が入力されていればスライド画像（横画面=黒板/縦画面=ホワイトボード）を生成してそのパスを、
    無ければ従来どおり scene.content_media_path を返す。
    """
    if scene.illustration_path and Path(scene.illustration_path).exists():
        return str(get_illustration_image_path(scene.illustration_path, resolution))
    if scene.note_text.strip():
        return str(get_note_path(scene.note_text, scene.note_focus, scene.note_meaning, resolution))
    if scene.has_slide:
        if not scene.show_board:
            return None  # 黒板を出さないシーン（2人の会話だけ）
        return str(get_slide_path(scene.slide_title, scene.slide_bullets, resolution, scene.slide_reveal,
                                  scene.slide_numbered))
    return scene.content_media_path


def slide_key(scene: Scene) -> Optional[tuple]:
    """黒板に書いてある内容（見出し + 全項目）を表すキー。前後のシーンで黒板が「別の板」に変わったかの判定用。"""
    if not scene.has_slide:
        return None
    return (scene.slide_title.strip(), tuple(normalize_bullets(scene.slide_bullets)), scene.slide_numbered)


def visible_count(scene: Scene) -> int:
    """そのシーンで黒板に表示される箇条書きの数。"""
    total = len(normalize_bullets(scene.slide_bullets))
    return total if scene.slide_reveal is None else max(0, min(total, scene.slide_reveal))
