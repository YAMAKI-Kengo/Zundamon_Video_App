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
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = PROJECT_ROOT / "assets"
CONFIG_PATH = PROJECT_ROOT / "config" / "characters.json"

# "[表情名]_open.png" から表情名を取り出す
EXPRESSION_OPEN_PATTERN = re.compile(r"^(.+)_open\.png$")

BACKGROUND_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
BACKGROUND_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".avi", ".mkv"}
# 後方互換のため、旧名でも画像拡張子集合を参照できるようにしておく
BACKGROUND_EXTS = BACKGROUND_IMAGE_EXTS


def _short_cache(seconds: float = 2.0):
    """フォルダの中身の一覧を、短い時間だけ覚えておく（画面の1回の描画で何百回も同じフォルダを読まないように）。

    新しく置いたファイルも、数秒以内の次の描画では一覧に出る。
    """
    def decorator(func):
        memo: dict = {}

        def wrapper(*args):
            now = time.monotonic()
            hit = memo.get(args)
            if hit is not None and now - hit[0] < seconds:
                return list(hit[1])
            result = func(*args)
            memo[args] = (now, result)
            return list(result)

        wrapper.__wrapped__ = func
        wrapper.__doc__ = func.__doc__
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator


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


@_short_cache()
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


DEFAULT_TELOP_STYLE = {"color": "#222222", "stroke_color": "#FFFFFF", "box_color": "#FFFFFFEB"}


def get_telop_style(character_key: str) -> dict[str, str]:
    """話者に応じたテロップの文字色・縁取り色・字幕の背景（箱）の色・枠線の色を返す。

    config/characters.json の "telop"（color / stroke_color / box_color / border_color）を参照する。
    字幕の背景は白（box_color）で、枠線（border_color）を話者のイメージカラーにして誰のセリフか分かるようにする。
    """
    char_cfg = load_character_config().get(character_key, {})
    cfg = char_cfg.get("telop", {})
    return {
        "color": cfg.get("color", DEFAULT_TELOP_STYLE["color"]),
        "stroke_color": cfg.get("stroke_color", DEFAULT_TELOP_STYLE["stroke_color"]),
        "box_color": cfg.get("box_color", DEFAULT_TELOP_STYLE["box_color"]),
        "border_color": cfg.get("border_color", char_cfg.get("accent_color", "")),
    }


ILLUSTRATION_DIR = ASSETS_DIR / "illustrations"
ILLUSTRATION_GUIDE_PATH = CONFIG_PATH.parent / "illustrations.json"
_ILLUSTRATION_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


@_short_cache()
def list_illustrations() -> list[Path]:
    """assets/illustrations/ 配下（サブフォルダも含む）のイラスト画像の一覧。台本でシーンごとに表示する素材。"""
    if not ILLUSTRATION_DIR.exists():
        return []
    return sorted(p for p in ILLUSTRATION_DIR.rglob("*") if p.is_file() and p.suffix.lower() in _ILLUSTRATION_EXTS)


def load_illustration_guide() -> dict[str, str]:
    """config/illustrations.json の {イラスト名: 何の絵か} （任意。ファイル名だけで伝わらない絵の説明用）。"""
    try:
        data = json.loads(ILLUSTRATION_GUIDE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data.get("illustrations", {}) if isinstance(data, dict) else {}
    return {str(k): str(v) for k, v in items.items() if v}


# いつもの部屋（台本で場所を指定しないときの既定の背景）。場所の背景の候補の一覧からは外す
DEFAULT_ROOM_BACKGROUNDS = ("zunda_room", "metan_room", "sample_room")


def find_background(name: str) -> Optional[Path]:
    """背景を名前（ファイル名から拡張子を除いた部分）で探す。完全一致 → 大文字小文字を無視した一致の順。"""
    key = (name or "").strip()
    if not key:
        return None
    candidates = list_backgrounds()
    for match in (lambda p: p.stem == key, lambda p: p.stem.lower() == key.lower(), lambda p: p.name == key):
        for p in candidates:
            if match(p):
                return p
    return None


def list_place_backgrounds() -> list[Path]:
    """場所の背景（学校・職場など。いつもの部屋を除く）の一覧。"""
    return [p for p in list_backgrounds() if p.stem not in DEFAULT_ROOM_BACKGROUNDS
            and not p.stem.startswith(("uploaded_", "common_"))]


def find_illustration(name: str) -> Optional[Path]:
    """イラストを名前（ファイル名から拡張子を除いた部分）で探す。完全一致 → 大文字小文字を無視した一致の順。"""
    key = (name or "").strip()
    if not key:
        return None
    candidates = list_illustrations()
    for match in (lambda p: p.stem == key, lambda p: p.stem.lower() == key.lower(), lambda p: p.name == key):
        for p in candidates:
            if match(p):
                return p
    return None


SE_GUIDE_PATH = CONFIG_PATH.parent / "se_guide.json"


def load_se_guide() -> dict[str, dict]:
    """config/se_guide.json の効果音ごとの設定（{名前: {"use": 用途, "shake": 画面を揺らすか}}）。"""
    try:
        data = json.loads(SE_GUIDE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sounds = data.get("sounds", {}) if isinstance(data, dict) else {}
    return {str(k): v for k, v in sounds.items() if isinstance(v, dict)}


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


@_short_cache()
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


BGM_EXTS = {".mp3", ".wav", ".m4a", ".ogg"}


@_short_cache()
def list_bgm() -> list[Path]:
    """assets/bgm/ 配下の音楽ファイル一覧を返す（場面ごと・シーンごとのBGM選択肢）。"""
    bgm_dir = ASSETS_DIR / "bgm"
    if not bgm_dir.exists():
        return []
    return sorted(p for p in bgm_dir.iterdir() if p.is_file() and p.suffix.lower() in BGM_EXTS)


@_short_cache()
def list_se() -> list[Path]:
    """assets/se/ 配下の効果音ファイル一覧を返す（scripts/generate_sound_effects.py で基本セットを作成できる）。"""
    se_dir = ASSETS_DIR / "se"
    if not se_dir.exists():
        return []
    return sorted(p for p in se_dir.iterdir() if p.is_file() and p.suffix.lower() in BGM_EXTS)


def find_se(name: str) -> Optional[Path]:
    """効果音を名前（ファイル名の拡張子を除いた部分。例: "キラーン"）で探す。見つからなければ None。

    完全一致を優先し、無ければ大文字小文字を無視した一致 → 部分一致の順に探す（台本JSONの表記ゆれ対策）。
    """
    key = (name or "").strip()
    if not key:
        return None
    candidates = list_se()

    def norm(text: str) -> str:
        # ひらがな→カタカナ・小文字化して比べる（「きらーん」でも「キラーン」が見つかるように）
        return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text).lower()

    for match in (
        lambda p: p.stem == key,
        lambda p: norm(p.stem) == norm(key),
        lambda p: norm(key) in norm(p.stem) or norm(p.stem) in norm(key),
    ):
        for p in candidates:
            if match(p):
                return p
    return None


@_short_cache()
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
