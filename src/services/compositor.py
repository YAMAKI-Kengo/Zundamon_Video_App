"""
立ち絵の合成・背景合成ロジック。

表情ごとに「口を開けた状態」「口を閉じた状態」を合成済みの1枚絵として用意する方式:
  assets/[キャラ名]/[表情名]_open.png   … その表情で口を開けている状態
  assets/[キャラ名]/[表情名]_close.png  … その表情で口を閉じている状態

このモジュールは、指定された表情・口の開閉に対応する1枚絵をそのまま読み込み、
背景画像の上に配置するだけで、パーツ単位のアルファ合成（旧・福笑い方式）は行わない。

背景には画像だけでなく動画ファイルも指定できる（is_video_path()で判定）。
動画背景の場合、ここでは代表的な1コマ（プレビュー用）を抜き出して静止画として合成するだけで、
動画全体をフレームごとに合成する処理は行わない（それはvideo_builder.pyの役目）。

VOICEVOXでの音声合成やMoviePyでの動画エンコードは重い/外部依存の処理のため
ダミーのままにしているが、この画像合成処理はPillowのみで完結し軽量なため、
UIでのプレビュー確認に使えるよう実際に動作する実装にしてある。
（＝表情・口パクの見た目をこの時点でも確認できる）
"""
from __future__ import annotations

from collections.abc import Collection
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter

from src.services import background_video, telop
from src.utils.asset_loader import (
    get_available_expressions,
    get_default_expression,
    get_expression_image_path,
    has_expression_assets,
    is_video_path,
    list_characters,
)

PLACEHOLDER_SIZE = (600, 900)
BACKGROUND_FALLBACK_COLOR = (245, 245, 245, 255)  # 背景画像が無い/読み込めない場合の単色フォールバック
CHARACTER_HEIGHT_RATIO = 0.92  # 立ち絵の高さ = 画面の高さに対する割合（1人表示の場合）

# 2人実況スタイル（常時2人配置）のレイアウト設定
# 画面左から順に配置するキャラクター（左:四国めたん、右:ずんだもん）
DUAL_CHARACTER_ORDER = ("shikoku_metan", "zundamon")
DUAL_CHARACTER_CENTER_X_RATIOS = (0.095, 0.905)   # 各キャラクターの水平中心位置（画面幅に対する比率。端寄りに配置）
# 描く順番（後ろ → 前）。ゲスト（春日部つむぎ）はめたんの隣（内側）に立つ。縦画面でもめたんの隣
# （黒板を出すシーンにはゲストを出さない。Scene.render_hidden を参照）
CHARACTER_ORDER = ("kasukabe_tsumugi", "shikoku_metan", "zundamon")
GUEST_CENTER_X_RATIO = 0.27
GUEST_PORTRAIT_CENTER_X_RATIO = 0.30
# ゲストがいる横画面では、イラスト・写真を中央のまま少し小さくして、ゲストとの重なりを減らす
GUEST_MEDIA_SCALE = 0.78
DUAL_CHARACTER_MAX_WIDTH_RATIO = 0.40           # 1人あたりの最大幅（画面幅に対する比率）
DUAL_CHARACTER_MAX_HEIGHT_RATIO = 0.80          # 1人あたりの最大高さ（画面高さに対する比率・横画面）
DUAL_CHARACTER_PORTRAIT_MAX_HEIGHT_RATIO = 0.34  # 同上（縦画面。上部の資料エリアを広く取るため小さめ）
# 全身素材（周囲の透明な余白を除いた絵の範囲）の上から何割を使うか。1.0=全身、0.72=太ももあたりまで。
# 切った位置は画面下端にぴったり合わせる（DUAL_CHARACTER_BOTTOM_MARGIN_RATIO=0）ため、切れ目は画面の外に隠れる
DUAL_CHARACTER_CROP_TOP_RATIO = 0.72
DUAL_CHARACTER_BOTTOM_MARGIN_RATIO = 0.0        # 画面下端からの余白（画面比率）

# 「資料メディア」（シーンごとに画面中央へ表示する写真/動画）のレイアウト設定。
# 背景（画面全体を覆うカバーフィット）とは異なり、こちらは画面上部〜中央に収まる
# containフィットで配置し、下端の2人の立ち絵・テロップと重ならないようにする。
# 立ち絵を全身表示にして画面の左右端に寄せたため、中央の資料（黒板など）は縦に大きく使える
# （下端はテロップ1〜2行分の手前まで）。
CONTENT_MEDIA_MAX_WIDTH_RATIO = 0.62    # 資料メディアの最大幅（画面幅に対する比率）
CONTENT_MEDIA_MAX_HEIGHT_RATIO = 0.73   # 資料メディアの最大高さ（画面高さに対する比率）
CONTENT_MEDIA_TOP_MARGIN_RATIO = 0.03   # 画面上端からの余白（画面比率）
# 縦画面（ショート動画）用。立ち絵は画面下部（上端が画面の約6割強の位置）に収まるため、
# 資料メディアは横幅いっぱい近く・画面の上6割強を使う（content_media_max_size()参照）
CONTENT_MEDIA_PORTRAIT_MAX_WIDTH_RATIO = 0.94
CONTENT_MEDIA_PORTRAIT_MAX_HEIGHT_RATIO = 0.60
CONTENT_MEDIA_PORTRAIT_TOP_MARGIN_RATIO = 0.04



# PR/広告表記バッジのレイアウト設定。動画全体・常時、画面右上に固定表示する
# （ステマ規制対応。表示の有無・文言はプロジェクト設定で切り替え可能）。
PR_LABEL_MARGIN_RATIO = 0.03             # 画面端からの余白（画面比率）
PR_LABEL_FONT_SIZE_RATIO = 0.04          # 文字サイズ（画面短辺に対する比率）
PR_LABEL_BG_COLOR = (220, 30, 30, 210)   # 目立つ赤系の半透明背景
PR_LABEL_TEXT_COLOR = "white"


def _placeholder_image(message: str) -> Image.Image:
    """素材が未配置のときにアプリを落とさず表示する代替画像。"""
    img = Image.new("RGBA", PLACEHOLDER_SIZE, (210, 210, 210, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle(
        [4, 4, PLACEHOLDER_SIZE[0] - 4, PLACEHOLDER_SIZE[1] - 4],
        outline=(140, 140, 140, 255),
        width=4,
    )
    draw.multiline_text((24, 24), message, fill=(90, 90, 90, 255))
    return img


def compose_character_frame(character_key: str, expression: str, mouth_open: bool) -> Image.Image:
    """指定した表情・口の開閉に対応する、合成済みの1枚絵をそのまま読み込んで返す。

    assets/[character_key]/[expression]_open.png または [expression]_close.png を読み込む。
    ファイルが存在しない（表情の組み合わせが未配置）場合はアプリを落とさず、
    プレースホルダー画像を返す。
    """
    if not has_expression_assets(character_key, expression):
        mouth_label = "open" if mouth_open else "close"
        return _placeholder_image(
            f"{character_key}\n素材未配置:\n{expression}_open.png\n{expression}_close.png\n"
            f"(必要だったのは {expression}_{mouth_label}.png)"
        )

    path = get_expression_image_path(character_key, expression, mouth_open)
    try:
        return Image.open(path).convert("RGBA")
    except (FileNotFoundError, OSError):
        return _placeholder_image(f"{character_key}\n画像の読み込みに失敗:\n{path.name}")


def blur_background(img: Image.Image, blur: float) -> Image.Image:
    """背景を少しぼかして、手前のキャラクター・黒板を引き立てる（被写界深度のような効果）。

    blur は1080p（短辺1080px）でのぼかし半径。プレビューの縮小画像や縦画面でも見た目が同じになるよう、
    画像の短辺に合わせて換算する。0以下なら何もしない。
    """
    if blur <= 0:
        return img
    radius = blur * min(img.size) / 1080
    return img.filter(ImageFilter.GaussianBlur(radius)) if radius >= 0.3 else img


MOOD_FILTERS: dict[str, dict] = {
    # 明るさ・彩度・色の重ね（RGB と濃さ）。背景だけに掛け、キャラクター・黒板・字幕はそのまま
    "gloomy": {"brightness": 0.55, "saturation": 0.45, "tint": (40, 60, 120), "tint_alpha": 0.28},
    "shock": {"brightness": 0.5, "saturation": 0.15, "tint": (60, 50, 90), "tint_alpha": 0.2},
    "dark": {"brightness": 0.42, "saturation": 0.8, "tint": (10, 15, 40), "tint_alpha": 0.2},
    "sepia": {"brightness": 0.95, "saturation": 0.0, "tint": (150, 105, 55), "tint_alpha": 0.38},
    "bright": {"brightness": 1.12, "saturation": 1.2, "tint": (255, 240, 200), "tint_alpha": 0.1},
}


def apply_mood(img: Image.Image, mood: str) -> Image.Image:
    """背景の色味を、シーンの雰囲気（MOOD_FILTERS。落ち込み・回想など）に合わせて変える。"""
    spec = MOOD_FILTERS.get(mood or "")
    if spec is None:
        return img
    from PIL import ImageEnhance

    rgb = img.convert("RGB")
    rgb = ImageEnhance.Color(rgb).enhance(spec["saturation"])
    rgb = ImageEnhance.Brightness(rgb).enhance(spec["brightness"])
    rgb = Image.blend(rgb, Image.new("RGB", rgb.size, spec["tint"]), spec["tint_alpha"])
    out = rgb.convert("RGBA")
    if img.mode == "RGBA":
        out.putalpha(img.getchannel("A"))
    return out


def load_background_image(path: Optional[str], size: tuple[int, int], blur: float = 0.0,
                          mood: str = "") -> Image.Image:
    """背景画像（または背景動画の代表的な1コマ）を読み込み、指定サイズに合わせて
    中央クロップ（カバーフィット）する。

    背景が未指定、ファイルが存在しない、壊れている等の場合でもアプリを落とさず、
    単色のフォールバック画像を返す。動画ファイルの場合は is_video_path() で判定し、
    background_video.extract_preview_frame() で代表的な1コマを取り出して静止画として扱う
    （プレビュー用途であり、動画全体をフレームごとに合成するのは video_builder.py の役目）。
    """
    if path:
        try:
            # 読み込み・縮小・ぼかしの結果を覚えておき、同じ背景は使い回す（呼び出し側が上に描き込むので複製を返す）
            return _background_cached(str(path), _mtime(path), tuple(size), float(blur), mood or "").copy()
        except Exception:  # noqa: BLE001 - 画像/動画の読み込み失敗要因は多岐にわたるため広く捕捉してフォールバックする
            # 背景が読み込めない場合はフォールバックして処理を継続する
            pass
    return apply_mood(Image.new("RGBA", size, BACKGROUND_FALLBACK_COLOR), mood)


def _mtime(path) -> float:
    return Path(path).stat().st_mtime


@lru_cache(maxsize=32)
def _background_cached(path: str, mtime: float, size: tuple[int, int], blur: float, mood: str) -> Image.Image:
    if is_video_path(path):
        img = Image.fromarray(background_video.extract_preview_frame(path)).convert("RGBA")
    else:
        img = Image.open(path).convert("RGBA")
    return apply_mood(blur_background(cover_resize(img, size), blur), mood)


TRANSITION_CARD_BLUR = 10.0        # 場面転換テロップの背景のぼかし（1080p換算）
TRANSITION_CARD_DARKEN = 0.35      # 場面転換テロップの背景の明るさ
TRANSITION_CARD_FONT_RATIO = 0.11  # 場面転換テロップの文字サイズ（画面短辺に対する比率）


def render_transition_card(background_path: Optional[str], resolution: tuple[int, int], text: str) -> Image.Image:
    """場面転換テロップ（「3日後…」「その夜」など）: 背景を大きくぼかして暗くし、中央に大きな文字を出す。"""
    from PIL import ImageEnhance

    bg = load_background_image(background_path, resolution, TRANSITION_CARD_BLUR).convert("RGB")
    canvas = ImageEnhance.Brightness(bg).enhance(TRANSITION_CARD_DARKEN).convert("RGBA")
    lines = [telop.strip_emoji(line) for line in (text or "").replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return canvas
    width, height = resolution
    size = round(min(width, height) * TRANSITION_CARD_FONT_RATIO)
    font = telop.load_font(telop.resolve_font_path(), size)
    stroke = max(2, size // 16)
    draw = ImageDraw.Draw(canvas)
    widest = max(draw.textlength(line, font=font) for line in lines)
    if widest > width * 0.86:  # 長い文は画面に収まるよう小さくする
        size = max(12, int(size * width * 0.86 / widest))
        font = telop.load_font(telop.resolve_font_path(), size)
        stroke = max(2, size // 16)
    line_h = round(size * 1.35)
    y = (height - line_h * len(lines)) // 2
    for line in lines:
        w = draw.textlength(line, font=font)
        x = (width - w) / 2
        draw.text((x + size * 0.05, y + size * 0.06), line, font=font, fill=(0, 0, 0, 160))  # 影
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(30, 30, 45, 255))
        y += line_h
    # 文字の上下に細い線を引いて、映画の字幕カードのような区切りにする
    line_w = min(width * 0.6, widest + size * 2)
    top = (height - line_h * len(lines)) // 2 - size * 0.45
    bottom = (height + line_h * len(lines)) // 2 + size * 0.15
    for yy in (top, bottom):
        draw.line([((width - line_w) / 2, yy), ((width + line_w) / 2, yy)], fill=(255, 255, 255, 170), width=max(2, size // 22))
    return canvas


def cover_resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """アスペクト比を保ったまま、指定サイズを覆うように拡大し、はみ出た部分を中央クロップする。"""
    target_w, target_h = size
    src_w, src_h = img.size
    if src_w == 0 or src_h == 0:
        return Image.new("RGBA", size, BACKGROUND_FALLBACK_COLOR)

    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def load_content_media_image(path: Optional[str]) -> Optional[Image.Image]:
    """資料メディア（写真/動画）を読み込んで返す。動画の場合は代表的な1コマを静止画として返す。

    background_path用のload_background_image()と異なり、こちらは「資料メディアが
    未指定・読み込み失敗」の場合は単色フォールバックではなくNoneを返す
    （資料メディアは任意項目のため、無指定なら何も描画しないのが正しい挙動）。
    """
    if not path:
        return None
    try:
        return _content_media_cached(str(path), _mtime(path)).copy()
    except Exception:  # noqa: BLE001 - 読み込み失敗要因は多岐にわたるため広く捕捉してフォールバックする
        return None


@lru_cache(maxsize=48)
def _content_media_cached(path: str, mtime: float) -> Image.Image:
    """資料メディア（黒板・イラストのカード・写真など）の読み込み結果を覚えておく（ファイルが更新されたら読み直す）。"""
    if is_video_path(path):
        return Image.fromarray(background_video.extract_preview_frame(path)).convert("RGBA")
    img = Image.open(path).convert("RGBA")
    if Path(path).name.startswith(ILLUSTRATION_FILE_PREFIX):
        img.info["layout"] = "illustration"  # place_content_media() で大きく置く目印（copy・resize でも引き継がれる）
    return img


def content_media_max_size(resolution: tuple[int, int]) -> tuple[int, int]:
    """資料メディア（黒板/ホワイトボードのスライド含む）を置ける枠の最大サイズ (幅, 高さ) を返す。

    縦画面は立ち絵が画面下部に小さく収まり上部が大きく空くうえ、スマホで見られるため、
    横幅いっぱい近くまで使う（横画面と同じ62%幅だと資料が小さすぎて読めない）。
    """
    width, height = resolution
    if height > width:
        return (
            max(1, round(width * CONTENT_MEDIA_PORTRAIT_MAX_WIDTH_RATIO)),
            max(1, round(height * CONTENT_MEDIA_PORTRAIT_MAX_HEIGHT_RATIO)),
        )
    return (
        max(1, round(width * CONTENT_MEDIA_MAX_WIDTH_RATIO)),
        max(1, round(height * CONTENT_MEDIA_MAX_HEIGHT_RATIO)),
    )


# イメージイラスト（カードや説明を付けず、画像だけを大きく出す）の枠。黒板より大きく、立ち絵に少し重なってよい
ILLUSTRATION_MAX_WIDTH_RATIO = 0.74
ILLUSTRATION_MAX_HEIGHT_RATIO = 0.78
ILLUSTRATION_PORTRAIT_MAX_WIDTH_RATIO = 0.96
ILLUSTRATION_PORTRAIT_MAX_HEIGHT_RATIO = 0.64
ILLUSTRATION_FILE_PREFIX = "illust_"   # この名前で始まる画像（slide_renderer が作る）をイラストとして大きく置く


def illustration_max_size(resolution: tuple[int, int]) -> tuple[int, int]:
    width, height = resolution
    if height > width:
        return round(width * ILLUSTRATION_PORTRAIT_MAX_WIDTH_RATIO), round(height * ILLUSTRATION_PORTRAIT_MAX_HEIGHT_RATIO)
    return round(width * ILLUSTRATION_MAX_WIDTH_RATIO), round(height * ILLUSTRATION_MAX_HEIGHT_RATIO)


def guest_on_screen(resolution: tuple[int, int], hidden_characters: Collection[str]) -> bool:
    """ゲストが画面にいる横画面か（イラスト・写真を少し小さくする）。"""
    return resolution[0] >= resolution[1] and any(
        g in list_characters() and g not in hidden_characters for g in CHARACTER_ORDER if g not in DUAL_CHARACTER_ORDER
    )


def place_content_media(media_img: Image.Image, resolution: tuple[int, int],
                        guest: bool = False) -> tuple[Image.Image, tuple[int, int]]:
    """資料メディアを枠に収まるサイズに縮小し、(縮小後の画像, 貼り付け位置(左上)) を返す。

    イメージイラスト（読み込み時に info["layout"] == "illustration" を付けた画像）は、黒板より大きな枠に置く。
    guest=True（ゲストがいる横画面）は、中央のまま少し小さくする（多少キャラクターと重なってもよい）。
    """
    if media_img.info.get("layout") == "illustration":
        max_w, max_h = illustration_max_size(resolution)
    else:
        max_w, max_h = content_media_max_size(resolution)
    if guest:
        max_w, max_h = round(max_w * GUEST_MEDIA_SCALE), round(max_h * GUEST_MEDIA_SCALE)
    target_w, target_h = _fit_size(media_img.size, max_w, max_h)
    resized = media_img if (target_w, target_h) == media_img.size else media_img.resize((target_w, target_h), Image.LANCZOS)
    x = (resolution[0] - target_w) // 2
    is_portrait = resolution[1] > resolution[0]
    top_ratio = CONTENT_MEDIA_PORTRAIT_TOP_MARGIN_RATIO if is_portrait else CONTENT_MEDIA_TOP_MARGIN_RATIO
    y = round(resolution[1] * top_ratio)
    return resized, (x, y)


def paste_content_media(canvas: Image.Image, media_img: Image.Image, resolution: tuple[int, int],
                        guest: bool = False) -> None:
    """資料メディアを画面上部〜中央に、containフィット・水平中央揃えで貼り付ける（guest は place_content_media）。"""
    resized, position = place_content_media(media_img, resolution, guest)
    canvas.paste(resized, position, resized)


def _fit_size(src_size: tuple[int, int], max_w: int, max_h: int) -> tuple[int, int]:
    """アスペクト比を保ったまま、(max_w, max_h) の枠に収まる最大サイズを返す（containフィット）。"""
    src_w, src_h = src_size
    scale = min(max_w / src_w, max_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def _draw_label_badge(
    canvas: Image.Image,
    text: str,
    top_left: tuple[int, int],
    font_size: int,
    text_color: str,
    bg_color: tuple[int, int, int, int],
) -> None:
    """半透明の背景ボックス付きの小さなラベル文字を描画する（PR表記で使用）。"""
    draw = ImageDraw.Draw(canvas)
    font = telop.load_font(telop.resolve_font_path(), font_size)
    pad_x = round(font_size * 0.5)
    pad_y = round(font_size * 0.25)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x0, y0 = top_left
    box_w = text_w + pad_x * 2
    box_h = text_h + pad_y * 2
    radius = max(3, round(font_size * 0.25))
    draw.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=radius, fill=bg_color)
    draw.text((x0 + pad_x - bbox[0], y0 + pad_y - bbox[1]), text, font=font, fill=text_color)


def render_pr_label_overlay(resolution: tuple[int, int], text: str) -> Image.Image:
    """PR/広告表記のバッジを、動画と同じ解像度の透過RGBA画像として描画する。

    毎フレーム再描画すると無駄なため、呼び出し側（video_builder.build_video）で
    動画1本につき1回だけ呼び出し、出来上がった画像を全フレームにalpha_compositeで
    重ねて使い回す想定（telop.render_telop_image()と同じ設計）。
    textが空の場合は全面透明な画像を返す（＝呼び出し側で「表示しない」を素直に表現できる）。
    """
    canvas = Image.new("RGBA", resolution, (0, 0, 0, 0))
    if not text or not text.strip():
        return canvas

    font_size = max(10, round(min(resolution) * PR_LABEL_FONT_SIZE_RATIO))
    margin = round(min(resolution) * PR_LABEL_MARGIN_RATIO)

    draw = ImageDraw.Draw(canvas)
    font = telop.load_font(telop.resolve_font_path(), font_size)
    pad_x = round(font_size * 0.6)
    pad_y = round(font_size * 0.3)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    box_w = text_w + pad_x * 2
    box_h = text_h + pad_y * 2
    x0 = resolution[0] - margin - box_w
    y0 = margin
    radius = max(4, round(font_size * 0.3))
    draw.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=radius, fill=PR_LABEL_BG_COLOR)
    draw.text(
        (x0 + pad_x - bbox[0], y0 + pad_y - bbox[1]),
        text,
        font=font,
        fill=PR_LABEL_TEXT_COLOR,
    )
    return canvas


def _paste_character_bottom_center(
    canvas: Image.Image,
    char_img: Image.Image,
    center_x: int,
    max_w: int,
    max_h: int,
    bottom_margin: int = 0,
) -> None:
    """立ち絵を、指定した水平中心位置・画面下端揃えでキャンバスに貼り付ける（containフィットで縮小）。

    center_x - target_w // 2 が負になる、または右端がcanvas幅を超える場合でも、
    Pillowの paste() ははみ出た部分を自動的に切り詰めて描画するため、
    画面端からキャラクターが少しはみ出るレイアウトも問題なく扱える。
    """
    target_w, target_h = _fit_size(char_img.size, max_w, max_h)
    char_resized = char_img.resize((target_w, target_h), Image.LANCZOS)
    x = center_x - target_w // 2
    y = canvas.height - target_h - bottom_margin
    canvas.paste(char_resized, (x, y), char_resized)


def _crop_bust_up(img: Image.Image, crop_top_ratio: float) -> Image.Image:
    """立ち絵素材の上から crop_top_ratio 分だけを残す（下半身を切ってバストアップにする）。"""
    width, height = img.size
    crop_h = max(1, round(height * crop_top_ratio))
    return img.crop((0, 0, width, crop_h))


def compose_scene_frame(
    character_key: str,
    expression: str,
    mouth_open: bool,
    background_path: Optional[str],
    resolution: tuple[int, int],
) -> Image.Image:
    """背景 + 立ち絵（表情ごとの合成済み1枚絵）1人分を、動画1フレーム分の完成画像として合成する。

    立ち絵は画面の高さの CHARACTER_HEIGHT_RATIO 倍に縮小/拡大し、水平中央・画面下端揃えで配置する。
    現在のUIでは2人常時表示レイアウト（compose_dual_scene_frame）を使用しているが、
    1人だけを合成したい場合のユーティリティとして残してある。
    """
    canvas = load_background_image(background_path, resolution)
    char_img = compose_character_frame(character_key, expression, mouth_open)
    max_h = max(1, round(resolution[1] * CHARACTER_HEIGHT_RATIO))
    _paste_character_bottom_center(canvas, char_img, resolution[0] // 2, resolution[0], max_h)
    return canvas


@lru_cache(maxsize=16)
def _union_bbox_cached(character_key: str, expressions: tuple[str, ...], mtimes: tuple[float, ...]):
    boxes = []
    for expression in expressions:
        for mouth_open in (True, False):
            try:
                with Image.open(get_expression_image_path(character_key, expression, mouth_open)) as img:
                    box = img.convert("RGBA").getbbox()
                    size = img.size
            except (FileNotFoundError, OSError):
                continue
            if box:
                boxes.append((box, size))
    if not boxes:
        return None
    # キャンバスサイズが違う素材が混ざっている場合は合算できないため、最も多いサイズのものだけで計算する
    sizes = [size for _, size in boxes]
    main_size = max(set(sizes), key=sizes.count)
    same = [box for box, size in boxes if size == main_size]
    return main_size, (min(b[0] for b in same), min(b[1] for b in same), max(b[2] for b in same), max(b[3] for b in same))


def _character_crop_box(character_key: str, img: Image.Image) -> Optional[tuple[int, int, int, int]]:
    """立ち絵の周囲の透明な余白を切り落とすための矩形を返す。

    表情ごとの画像で個別に余白を切ると、腕を上げたポーズなどで絵の範囲が変わったときに
    キャラクターの大きさ・位置が表情を変えるたびにズレてしまう。そのため、そのキャラクターの
    全表情の絵が収まる範囲（和集合）で一律に切り落とす（キャラクターごとにキャッシュ。
    素材ファイルが追加・更新されると自動で計算し直す）。
    """
    expressions = tuple(e for e in get_available_expressions(character_key) if has_expression_assets(character_key, e))
    mtimes = tuple(
        get_expression_image_path(character_key, e, m).stat().st_mtime for e in expressions for m in (True, False)
    )
    cached = _union_bbox_cached(character_key, expressions, mtimes)
    if cached is not None and cached[0] == img.size:
        return cached[1]
    return img.getbbox()  # 素材と違うサイズの画像（プレースホルダー等）は個別に切る


def _paste_dual_characters(
    canvas: Image.Image,
    active_speaker: str,
    active_expression: str,
    active_mouth_open: bool,
    resolution: tuple[int, int],
    hidden_characters: Collection[str] = (),
    partner_expression: Optional[str] = None,
) -> Image.Image:
    """2人実況スタイルの立ち絵（画面左:四国めたん・右:ずんだもん）をcanvasに貼り付ける。

    compose_dual_scene_frame（背景あり）と compose_dual_character_overlay（背景なし・
    透過キャラクターのみ）の共通処理。話者(active_speaker)だけがリップシンク
    (active_mouth_open)し、表情(active_expression)も話者側にのみ適用される。
    話していないもう一方のキャラクターは、そのキャラクターの既定表情
    (config/characters.jsonのdefault_expression)で口を閉じた待機状態にする。
    """
    for character_key in CHARACTER_ORDER:
        # 非表示にしたキャラクターは描かない（もう一方は定位置のまま。話者を隠した場合は声だけになる）
        if character_key in hidden_characters or character_key not in _drawable_characters():
            continue
        if character_key == active_speaker:
            expression, mouth_open = active_expression, active_mouth_open
        else:
            expression, mouth_open = _partner_expression(character_key, partner_expression), False
        sprite, position = character_sprite(character_key, expression, mouth_open, resolution)
        canvas.paste(sprite, position, sprite)
    return canvas


def _partner_expression(character_key: str, partner_expression: Optional[str]) -> str:
    """話していないキャラクターの表情: partner_expression が指定されていて、そのキャラクターに
    素材があればそれ（例: エンディングで2人とも笑顔）、無ければ待機表情。口は閉じた状態で使う。"""
    if partner_expression and has_expression_assets(character_key, partner_expression):
        return partner_expression
    return get_default_expression(character_key)


def character_sprite(
    character_key: str, expression: str, mouth_open: bool, resolution: tuple[int, int]
) -> tuple[Image.Image, tuple[int, int]]:
    """立ち絵1人分を、2人実況レイアウトでの表示サイズに縮小した画像と、その定位置（左上座標）を返す。

    余白の切り落とし → 太もも位置でのカット → 画面の向きに応じたサイズへの縮小 → 画面下端揃え、
    という配置の計算をまとめたもの。動画の動き（motion.py）では、この定位置からずらして描画する。
    """
    stamp = 0.0
    if has_expression_assets(character_key, expression):
        stamp = get_expression_image_path(character_key, expression, mouth_open).stat().st_mtime
    # 素材の読み込み・切り抜き・縮小の結果を覚えておき、同じ表情・同じ画面サイズなら使い回す
    # （返した画像は呼び出し側で貼り付けに使うだけで、書き換えない）
    return _character_sprite_cached(character_key, expression, bool(mouth_open), tuple(resolution), stamp)


@lru_cache(maxsize=96)
def _character_sprite_cached(
    character_key: str, expression: str, mouth_open: bool, resolution: tuple[int, int], _stamp: float,
) -> tuple[Image.Image, tuple[int, int]]:
    is_portrait = resolution[1] > resolution[0]
    height_ratio = DUAL_CHARACTER_PORTRAIT_MAX_HEIGHT_RATIO if is_portrait else DUAL_CHARACTER_MAX_HEIGHT_RATIO
    max_w = max(1, round(resolution[0] * DUAL_CHARACTER_MAX_WIDTH_RATIO))
    max_h = max(1, round(resolution[1] * height_ratio))
    bottom_margin = round(resolution[1] * DUAL_CHARACTER_BOTTOM_MARGIN_RATIO)

    char_img = compose_character_frame(character_key, expression, mouth_open)
    # 素材の周囲の透明な余白を切り落とす（そのままだと足元が画面下端から浮いて見える）
    bbox = _character_crop_box(character_key, char_img)
    if bbox:
        char_img = char_img.crop(bbox)
    char_img = _crop_bust_up(char_img, DUAL_CHARACTER_CROP_TOP_RATIO)
    target_w, target_h = _fit_size(char_img.size, max_w, max_h)
    resized = char_img.resize((target_w, target_h), Image.LANCZOS)
    center_ratio = _center_ratio(character_key, resolution)
    x = round(resolution[0] * center_ratio) - target_w // 2
    y = resolution[1] - target_h - bottom_margin
    return resized, (x, y)


def _center_ratio(character_key: str, resolution: tuple[int, int]) -> float:
    """キャラクターの立つ水平位置（画面幅に対する比率）。"""
    if character_key in DUAL_CHARACTER_ORDER:
        return DUAL_CHARACTER_CENTER_X_RATIOS[DUAL_CHARACTER_ORDER.index(character_key)]
    return GUEST_PORTRAIT_CENTER_X_RATIO if resolution[1] > resolution[0] else GUEST_CENTER_X_RATIO


def _drawable_characters() -> set[str]:
    """描けるキャラクター（2人＋素材がそろっているゲスト）。ゲストの素材が無ければ描かない。"""
    return set(DUAL_CHARACTER_ORDER) | set(list_characters())


def character_side(character_key: str) -> int:
    """キャラクターが画面の左右どちら側にいるか（左=-1、右=+1）。登場時にどちらから入ってくるかに使う。"""
    if character_key in DUAL_CHARACTER_ORDER:
        return -1 if DUAL_CHARACTER_CENTER_X_RATIOS[DUAL_CHARACTER_ORDER.index(character_key)] < 0.5 else 1
    return -1  # ゲストは左（めたんの側）から入ってくる


def dual_character_sprites(
    active_speaker: str,
    active_expression: str,
    resolution: tuple[int, int],
    hidden_characters: Collection[str] = (),
    partner_expression: Optional[str] = None,
) -> dict[str, dict[bool, tuple[Image.Image, tuple[int, int]]]]:
    """表示する各キャラクターの立ち絵を {キャラ: {口開き(True/False): (画像, 定位置)}} で返す（動き付きの合成用）。"""
    sprites = {}
    for character_key in CHARACTER_ORDER:
        if character_key in hidden_characters or character_key not in _drawable_characters():
            continue
        if character_key == active_speaker:
            sprites[character_key] = {
                mouth: character_sprite(character_key, active_expression, mouth, resolution) for mouth in (False, True)
            }
        else:
            closed = character_sprite(character_key, _partner_expression(character_key, partner_expression), False, resolution)
            sprites[character_key] = {False: closed, True: closed}
    return sprites


def compose_dual_scene_frame(
    active_speaker: str,
    active_expression: str,
    active_mouth_open: bool,
    background_path: Optional[str],
    resolution: tuple[int, int],
    content_media_path: Optional[str] = None,
    pr_label_overlay: Optional[Image.Image] = None,
    hidden_characters: Collection[str] = (),
    partner_expression: Optional[str] = None,
    background_blur: float = 0.0,
    mood: str = "",
) -> Image.Image:
    """2人実況スタイル: 画面左に「四国めたん」、右に「ずんだもん」を常時配置して合成する。

    全身ではなく上半身（バストアップ）だけを画面端寄りに大きめに配置することで、
    中央〜上部の背景（テロップ・資料画像など）が見えるスペースを広く確保する。

    content_media_path を指定すると、背景の上・立ち絵の下のレイヤーとして、
    画面中央上部に写真/動画（資料メディア）をcontainフィットで重ねる。
    pr_label_overlay を渡すと、一番上のレイヤーとしてPR/広告表記バッジを重ねる
    （render_pr_label_overlay()で事前に作った画像をそのまま渡す想定）。
    """
    canvas = load_background_image(background_path, resolution, background_blur, mood)

    media_img = load_content_media_image(content_media_path)
    if media_img is not None:
        paste_content_media(canvas, media_img, resolution, guest_on_screen(resolution, hidden_characters))

    canvas = _paste_dual_characters(
        canvas, active_speaker, active_expression, active_mouth_open, resolution, hidden_characters, partner_expression
    )

    if pr_label_overlay is not None:
        canvas = Image.alpha_composite(canvas, pr_label_overlay)

    return canvas


def compose_dual_character_overlay(
    active_speaker: str,
    active_expression: str,
    active_mouth_open: bool,
    resolution: tuple[int, int],
    hidden_characters: Collection[str] = (),
    partner_expression: Optional[str] = None,
) -> Image.Image:
    """2人実況スタイルの立ち絵だけを、背景を含まない透過PNGとして合成する。

    背景が動画ファイルの場合、video_builder.py側でフレームごとに背景動画のコマを取り出し、
    ここで作った立ち絵オーバーレイ（口:開/口:閉の2枚）を都度重ねるために使う
    （背景が画像のときのような「背景込みで2枚だけ作って使い回す」最適化ができないため）。
    """
    canvas = Image.new("RGBA", resolution, (0, 0, 0, 0))
    return _paste_dual_characters(
        canvas, active_speaker, active_expression, active_mouth_open, resolution, hidden_characters, partner_expression
    )
