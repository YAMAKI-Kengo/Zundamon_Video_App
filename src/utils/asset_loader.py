"""
assets/ 配下の素材を動的に検出するユーティリティ。

素材の方式（表情ごとに合成済みの画像を用意する方式）:
  事前に「口を開けた状態」「口を閉じた状態」を合成し終えた透過PNGを、表情ごとに1組ずつ用意する。
    assets/[キャラ名]/[表情名]_open.png   … その表情で口を開けている状態
    assets/[キャラ名]/[表情名]_close.png  … その表情で口を閉じている状態
  例: assets/zundamon/normal_open.png, assets/zundamon/normal_close.png,
      assets/zundamon/happy_open.png,  assets/zundamon/happy_close.png ...

  表情のバリエーションはファイル(open/closeの組)を増やすだけで対応できるようにするため、
  「どの表情が使えるか」はここでディスク上のファイルをスキャンして決定する
  （config/characters.json はあくまで表示名・並び順・待機表情のヒントに過ぎない）。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = PROJECT_ROOT / "assets"
CONFIG_PATH = PROJECT_ROOT / "config" / "characters.json"

# "[表情名]_open.png" から表情名を取り出す
EXPRESSION_OPEN_PATTERN = re.compile(r"^(.+)_open\.png$")

BACKGROUND_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
BACKGROUND_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}
# 後方互換のため、旧名でも画像拡張子集合を参照できるようにしておく
BACKGROUND_EXTS = BACKGROUND_IMAGE_EXTS


def is_video_path(path) -> bool:
    """背景として指定されたパスが動画ファイルかどうかを拡張子から判定する。"""
    if not path:
        return False
    return Path(path).suffix.lower() in BACKGROUND_VIDEO_EXTS


@lru_cache(maxsize=1)
def load_character_config() -> dict:
    """config/characters.json を読み込む。無ければ空dictを返す。"""
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _scan_expression_pairs(character_key: str) -> set[str]:
    """assets/[character_key]/ を走査し、[表情名]_open.png と [表情名]_close.png が
    両方そろっている表情名の集合を返す（フォールバック等は行わない生のスキャン結果）。
    """
    char_dir = ASSETS_DIR / character_key
    if not char_dir.exists():
        return set()

    found = set()
    for f in char_dir.glob("*_open.png"):
        m = EXPRESSION_OPEN_PATTERN.match(f.name)
        if not m:
            continue
        expression = m.group(1)
        if (char_dir / f"{expression}_close.png").exists():
            found.add(expression)
    return found


def list_characters() -> list[str]:
    """assets/ 配下に、口の開閉ペアが少なくとも1組そろっているキャラクターのキー一覧を返す。"""
    if not ASSETS_DIR.exists():
        return []
    characters = []
    for d in sorted(ASSETS_DIR.iterdir()):
        if d.is_dir() and _scan_expression_pairs(d.name):
            characters.append(d.name)
    return characters


def get_character_display_name(character_key: str) -> str:
    cfg = load_character_config().get(character_key, {})
    return cfg.get("display_name", character_key)


def get_available_expressions(character_key: str) -> list[str]:
    """そのキャラクターで実際に使える表情キーの一覧を返す
    （[表情名]_open.png / [表情名]_close.png が両方そろっているものだけ）。

    config側に表示順の定義があればその順序を優先し、config未記載でも
    ファイルさえそろっていれば表情として拾う（＝素材を置くだけでUIに反映される）。
    UI表示用の便宜上、素材が1つも無い場合でも空リストではなく ["normal"] を返す
    （その場合はcompositor側でプレースホルダー画像にフォールバックする）。
    """
    found = _scan_expression_pairs(character_key)

    cfg_expressions = list(load_character_config().get(character_key, {}).get("expressions", {}).keys())
    ordered = [e for e in cfg_expressions if e in found]
    ordered += sorted(found - set(ordered))

    return ordered or ["normal"]


def get_expression_label(character_key: str, expression_key: str) -> str:
    """表情キーに対応する日本語ラベルを返す（未定義ならキーをそのまま返す）"""
    cfg = load_character_config().get(character_key, {}).get("expressions", {})
    return cfg.get(expression_key, expression_key)


def get_default_expression(character_key: str) -> str:
    """そのキャラクターが「話していない待機状態」のときに使う表情キーを返す。

    2人常時表示レイアウトで、話者でない方のキャラクターの表情に使用する。
    """
    cfg = load_character_config().get(character_key, {})
    default = cfg.get("default_expression", "normal")
    available = get_available_expressions(character_key)
    return default if default in available else available[0]


DEFAULT_TELOP_STYLE = {"color": "white", "stroke_color": "black"}


def get_telop_style(character_key: str) -> dict[str, str]:
    """話者に応じたテロップの文字色・縁取り色を返す。

    config/characters.json の "telop" (color / stroke_color) を参照し、
    未定義の場合は白地に黒縁のフォールバックにする。
    """
    cfg = load_character_config().get(character_key, {}).get("telop", {})
    return {
        "color": cfg.get("color", DEFAULT_TELOP_STYLE["color"]),
        "stroke_color": cfg.get("stroke_color", DEFAULT_TELOP_STYLE["stroke_color"]),
    }


def has_expression_assets(character_key: str, expression: str) -> bool:
    """指定した表情の口開閉ペア(open/close)が両方そろっているかを返す。

    UI側で「素材が足りません」という警告を出すのに使う。
    """
    return expression in _scan_expression_pairs(character_key)


def get_expression_image_path(character_key: str, expression: str, mouth_open: bool) -> Path:
    """[表情名]_open.png / [表情名]_close.png のパスを返す（存在チェックはしない）。"""
    suffix = "open" if mouth_open else "close"
    return ASSETS_DIR / character_key / f"{expression}_{suffix}.png"


def get_character_asset_path(character_key: str, filename: str) -> Path:
    return ASSETS_DIR / character_key / filename


def list_backgrounds() -> list[Path]:
    """assets/backgrounds/ 配下の画像・動画ファイル一覧を返す。

    動画ファイル（BACKGROUND_VIDEO_EXTS）は、シーンの表示秒数に合わせて
    自動でループ／トリミングされる背景動画として扱われる（video_builder.py / compositor.py）。
    """
    bg_dir = ASSETS_DIR / "backgrounds"
    if not bg_dir.exists():
        return []
    allowed_exts = BACKGROUND_IMAGE_EXTS | BACKGROUND_VIDEO_EXTS
    return sorted(
        p for p in bg_dir.iterdir()
        if p.is_file() and p.suffix.lower() in allowed_exts
    )


def list_content_media() -> list[Path]:
    """assets/content_media/ 配下の画像・動画ファイル一覧を返す。

    「資料メディア」（シーンごとに画面中央へ表示する写真/動画）用の素材一覧。
    背景と違い画面全体を覆わず、中央に収まるサイズで表示される
    （compositor.py の paste_content_media / load_content_media_image を参照）。
    動画ファイルは背景動画と同様、表示秒数に合わせて自動でループ／トリミングされる。
    """
    media_dir = ASSETS_DIR / "content_media"
    if not media_dir.exists():
        return []
    allowed_exts = BACKGROUND_IMAGE_EXTS | BACKGROUND_VIDEO_EXTS
    return sorted(
        p for p in media_dir.iterdir()
        if p.is_file() and p.suffix.lower() in allowed_exts
    )


def list_content_images() -> list[Path]:
    """assets/content_media/ 配下の「画像ファイルのみ」の一覧を返す（動画を除く）。

    ビフォーアフター機能（compositor.paste_before_after）は静止画2枚を左右に並べる
    表示方式のみをサポートしているため、選択肢に動画ファイルは含めない。
    """
    media_dir = ASSETS_DIR / "content_media"
    if not media_dir.exists():
        return []
    return sorted(
        p for p in media_dir.iterdir()
        if p.is_file() and p.suffix.lower() in BACKGROUND_IMAGE_EXTS
    )
