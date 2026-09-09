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

from typing import Optional

from PIL import Image, ImageDraw

from src.services import background_video, telop
from src.utils.asset_loader import (
    get_default_expression,
    get_expression_image_path,
    has_expression_assets,
    is_video_path,
)

PLACEHOLDER_SIZE = (600, 900)
BACKGROUND_FALLBACK_COLOR = (245, 245, 245, 255)  # 背景画像が無い/読み込めない場合の単色フォールバック
CHARACTER_HEIGHT_RATIO = 0.92  # 立ち絵の高さ = 画面の高さに対する割合（1人表示の場合）

# 2人実況スタイル（常時2人配置）のレイアウト設定
# 画面左から順に配置するキャラクター（左:四国めたん、右:ずんだもん）
DUAL_CHARACTER_ORDER = ("shikoku_metan", "zundamon")
DUAL_CHARACTER_CENTER_X_RATIOS = (0.13, 0.87)   # 各キャラクターの水平中心位置（画面幅に対する比率。端寄りに配置）
DUAL_CHARACTER_MAX_WIDTH_RATIO = 0.40           # 1人あたりの最大幅（画面幅に対する比率）
DUAL_CHARACTER_MAX_HEIGHT_RATIO = 0.80          # 1人あたりの最大高さ（画面高さに対する比率）
DUAL_CHARACTER_CROP_TOP_RATIO = 0.70            # 全身素材の上から何割を使うか（下半身を切ってバストアップにする）
DUAL_CHARACTER_BOTTOM_MARGIN_RATIO = 0.04       # 画面下端からの余白（画面比率）

# 「資料メディア」（シーンごとに画面中央へ表示する写真/動画）のレイアウト設定。
# 背景（画面全体を覆うカバーフィット）とは異なり、こちらは画面上部〜中央に収まる
# containフィットで配置し、下端の2人の立ち絵・テロップと重ならないようにする。
CONTENT_MEDIA_MAX_WIDTH_RATIO = 0.62    # 資料メディアの最大幅（画面幅に対する比率）
CONTENT_MEDIA_MAX_HEIGHT_RATIO = 0.58   # 資料メディアの最大高さ（画面高さに対する比率）
CONTENT_MEDIA_TOP_MARGIN_RATIO = 0.05   # 画面上端からの余白（画面比率）

# 「ビフォーアフター」（1シーンで2枚の画像を左右に並べて見せる）のレイアウト設定。
# 資料メディアと同じ画面上部〜中央のエリアを、2枚の画像で左右に分割して使う
# （静止画2枚のみ対応。動画には非対応）。
BEFORE_AFTER_AREA_WIDTH_RATIO = 0.90     # 2枚合わせて使う横幅（画面幅に対する比率）
BEFORE_AFTER_AREA_HEIGHT_RATIO = 0.58    # 1枚あたりの最大高さ（画面高さに対する比率。資料メディアと同じ）
BEFORE_AFTER_TOP_MARGIN_RATIO = 0.05     # 画面上端からの余白（画面比率）
BEFORE_AFTER_GAP_RATIO = 0.02            # 2枚の画像の間の隙間（画面幅に対する比率）
BEFORE_AFTER_LABEL_COLOR = "white"
BEFORE_AFTER_LABEL_BG_COLOR = (0, 0, 0, 170)
BEFORE_AFTER_LABEL_FONT_SIZE_RATIO = 0.035  # ラベル文字サイズ（画面短辺に対する比率）

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


def load_background_image(path: Optional[str], size: tuple[int, int]) -> Image.Image:
    """背景画像（または背景動画の代表的な1コマ）を読み込み、指定サイズに合わせて
    中央クロップ（カバーフィット）する。

    背景が未指定、ファイルが存在しない、壊れている等の場合でもアプリを落とさず、
    単色のフォールバック画像を返す。動画ファイルの場合は is_video_path() で判定し、
    background_video.extract_preview_frame() で代表的な1コマを取り出して静止画として扱う
    （プレビュー用途であり、動画全体をフレームごとに合成するのは video_builder.py の役目）。
    """
    if path:
        try:
            if is_video_path(path):
                frame = background_video.extract_preview_frame(path)
                img = Image.fromarray(frame).convert("RGBA")
            else:
                img = Image.open(path).convert("RGBA")
            return cover_resize(img, size)
        except Exception:  # noqa: BLE001 - 画像/動画の読み込み失敗要因は多岐にわたるため広く捕捉してフォールバックする
            # 背景が読み込めない場合はフォールバックして処理を継続する
            pass
    return Image.new("RGBA", size, BACKGROUND_FALLBACK_COLOR)


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
        if is_video_path(path):
            frame = background_video.extract_preview_frame(path)
            return Image.fromarray(frame).convert("RGBA")
        return Image.open(path).convert("RGBA")
    except Exception:  # noqa: BLE001 - 読み込み失敗要因は多岐にわたるため広く捕捉してフォールバックする
        return None


def paste_content_media(canvas: Image.Image, media_img: Image.Image, resolution: tuple[int, int]) -> None:
    """資料メディアを画面上部〜中央に、containフィット・水平中央揃えで貼り付ける。"""
    max_w = max(1, round(resolution[0] * CONTENT_MEDIA_MAX_WIDTH_RATIO))
    max_h = max(1, round(resolution[1] * CONTENT_MEDIA_MAX_HEIGHT_RATIO))
    target_w, target_h = _fit_size(media_img.size, max_w, max_h)
    resized = media_img.resize((target_w, target_h), Image.LANCZOS)
    x = (resolution[0] - target_w) // 2
    y = round(resolution[1] * CONTENT_MEDIA_TOP_MARGIN_RATIO)
    canvas.paste(resized, (x, y), resized)


def _fit_size(src_size: tuple[int, int], max_w: int, max_h: int) -> tuple[int, int]:
    """アスペクト比を保ったまま、(max_w, max_h) の枠に収まる最大サイズを返す（containフィット）。"""
    src_w, src_h = src_size
    scale = min(max_w / src_w, max_h / src_h)
    return max(1, round(src_w * scale)), max(1, round(src_h * scale))


def load_before_after_images(
    before_path: Optional[str], after_path: Optional[str]
) -> Optional[tuple[Image.Image, Image.Image]]:
    """ビフォーアフター用の2枚の画像を読み込む。

    2枚そろって初めて成立する表示のため、どちらか一方でも未指定/読み込み失敗の場合は
    Noneを返す（片方だけの中途半端な表示はしない。呼び出し側は通常の資料メディア表示に
    フォールバックする）。現状は静止画のみ対応（動画ファイルは非対応）。
    """
    if not before_path or not after_path:
        return None
    try:
        before_img = Image.open(before_path).convert("RGBA")
        after_img = Image.open(after_path).convert("RGBA")
        return before_img, after_img
    except Exception:  # noqa: BLE001 - 読み込み失敗要因は多岐にわたるため広く捕捉してフォールバックする
        return None


def _draw_label_badge(
    canvas: Image.Image,
    text: str,
    top_left: tuple[int, int],
    font_size: int,
    text_color: str,
    bg_color: tuple[int, int, int, int],
) -> None:
    """半透明の背景ボックス付きの小さなラベル文字を描画する（Before/After・PR表記で共通利用）。"""
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


def paste_before_after(
    canvas: Image.Image,
    before_img: Image.Image,
    after_img: Image.Image,
    resolution: tuple[int, int],
) -> None:
    """ビフォーアフター画像2枚を、画面上部〜中央に左右分割・containフィットで貼り付ける。

    資料メディアと同じ画面エリア（上部〜中央、立ち絵・テロップと重ならない範囲）を
    2分割して使う。それぞれの画像の左上に「BEFORE」「AFTER」のラベルバッジを重ねる。
    """
    area_width = round(resolution[0] * BEFORE_AFTER_AREA_WIDTH_RATIO)
    gap = round(resolution[0] * BEFORE_AFTER_GAP_RATIO)
    half_max_w = max(1, (area_width - gap) // 2)
    half_max_h = max(1, round(resolution[1] * BEFORE_AFTER_AREA_HEIGHT_RATIO))
    area_left = (resolution[0] - area_width) // 2
    top_y = round(resolution[1] * BEFORE_AFTER_TOP_MARGIN_RATIO)
    font_size = max(10, round(min(resolution) * BEFORE_AFTER_LABEL_FONT_SIZE_RATIO))

    slots = [
        ("BEFORE", before_img, area_left),
        ("AFTER", after_img, area_left + half_max_w + gap),
    ]
    for label, img, slot_left in slots:
        target_w, target_h = _fit_size(img.size, half_max_w, half_max_h)
        resized = img.resize((target_w, target_h), Image.LANCZOS)
        x = slot_left + (half_max_w - target_w) // 2
        canvas.paste(resized, (x, top_y), resized)
        _draw_label_badge(
            canvas,
            label,
            (x + round(font_size * 0.3), top_y + round(font_size * 0.3)),
            font_size,
            BEFORE_AFTER_LABEL_COLOR,
            BEFORE_AFTER_LABEL_BG_COLOR,
        )


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


def _paste_dual_characters(
    canvas: Image.Image,
    active_speaker: str,
    active_expression: str,
    active_mouth_open: bool,
    resolution: tuple[int, int],
) -> Image.Image:
    """2人実況スタイルの立ち絵（画面左:四国めたん・右:ずんだもん）をcanvasに貼り付ける。

    compose_dual_scene_frame（背景あり）と compose_dual_character_overlay（背景なし・
    透過キャラクターのみ）の共通処理。話者(active_speaker)だけがリップシンク
    (active_mouth_open)し、表情(active_expression)も話者側にのみ適用される。
    話していないもう一方のキャラクターは、そのキャラクターの既定表情
    (config/characters.jsonのdefault_expression)で口を閉じた待機状態にする。
    """
    max_w = max(1, round(resolution[0] * DUAL_CHARACTER_MAX_WIDTH_RATIO))
    max_h = max(1, round(resolution[1] * DUAL_CHARACTER_MAX_HEIGHT_RATIO))
    bottom_margin = round(resolution[1] * DUAL_CHARACTER_BOTTOM_MARGIN_RATIO)

    for character_key, center_x_ratio in zip(DUAL_CHARACTER_ORDER, DUAL_CHARACTER_CENTER_X_RATIOS):
        if character_key == active_speaker:
            expression = active_expression
            mouth_open = active_mouth_open
        else:
            expression = get_default_expression(character_key)
            mouth_open = False  # 話していないキャラクターは口を閉じた待機状態

        char_img = compose_character_frame(character_key, expression, mouth_open)
        char_img = _crop_bust_up(char_img, DUAL_CHARACTER_CROP_TOP_RATIO)
        center_x = round(resolution[0] * center_x_ratio)
        _paste_character_bottom_center(canvas, char_img, center_x, max_w, max_h, bottom_margin)

    return canvas


def compose_dual_scene_frame(
    active_speaker: str,
    active_expression: str,
    active_mouth_open: bool,
    background_path: Optional[str],
    resolution: tuple[int, int],
    content_media_path: Optional[str] = None,
    before_image_path: Optional[str] = None,
    after_image_path: Optional[str] = None,
    pr_label_overlay: Optional[Image.Image] = None,
) -> Image.Image:
    """2人実況スタイル: 画面左に「四国めたん」、右に「ずんだもん」を常時配置して合成する。

    全身ではなく上半身（バストアップ）だけを画面端寄りに大きめに配置することで、
    中央〜上部の背景（テロップ・資料画像など）が見えるスペースを広く確保する。

    content_media_path を指定すると、背景の上・立ち絵の下のレイヤーとして、
    画面中央上部に写真/動画（資料メディア）をcontainフィットで重ねる。
    before_image_path と after_image_path が両方そろっている場合は、
    そちらを優先してビフォーアフター（左右分割）表示にする（content_media_pathは使われない）。
    pr_label_overlay を渡すと、一番上のレイヤーとしてPR/広告表記バッジを重ねる
    （render_pr_label_overlay()で事前に作った画像をそのまま渡す想定）。
    """
    canvas = load_background_image(background_path, resolution)

    before_after = load_before_after_images(before_image_path, after_image_path)
    if before_after is not None:
        paste_before_after(canvas, before_after[0], before_after[1], resolution)
    else:
        media_img = load_content_media_image(content_media_path)
        if media_img is not None:
            paste_content_media(canvas, media_img, resolution)

    canvas = _paste_dual_characters(canvas, active_speaker, active_expression, active_mouth_open, resolution)

    if pr_label_overlay is not None:
        canvas = Image.alpha_composite(canvas, pr_label_overlay)

    return canvas


def compose_dual_character_overlay(
    active_speaker: str,
    active_expression: str,
    active_mouth_open: bool,
    resolution: tuple[int, int],
) -> Image.Image:
    """2人実況スタイルの立ち絵だけを、背景を含まない透過PNGとして合成する。

    背景が動画ファイルの場合、video_builder.py側でフレームごとに背景動画のコマを取り出し、
    ここで作った立ち絵オーバーレイ（口:開/口:閉の2枚）を都度重ねるために使う
    （背景が画像のときのような「背景込みで2枚だけ作って使い回す」最適化ができないため）。
    """
    canvas = Image.new("RGBA", resolution, (0, 0, 0, 0))
    return _paste_dual_characters(canvas, active_speaker, active_expression, active_mouth_open, resolution)
