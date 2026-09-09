"""
動作確認用のダミー立ち絵素材を生成するスクリプト。

表情ごとに「口を開けた状態」「口を閉じた状態」を合成済みの1枚絵として、
実際のイラストの代わりに図形描画で自動生成する。UIの動作確認・スクリーンショット
確認用であり、アップロードいただいたPSD（ずんだもん立ち絵素材2.3.psd /
四国めたん立ち絵素材2.1.psd）等から実際に書き出した完成絵に差し替えることを想定している。

実行方法:
    python scripts/generate_placeholder_assets.py

生成物:
    assets/zundamon/[表情名]_open.png, [表情名]_close.png （表情ごとに口の開閉2枚）
    assets/shikoku_metan/[表情名]_open.png, [表情名]_close.png
    assets/backgrounds/sample_room.png

旧方式（base.png / mouth_open.png / mouth_close.png / eyes_*.png のパーツ合成）の
ファイルが残っていた場合は、混乱を避けるためこのスクリプトが削除してから作り直す。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
CANVAS_SIZE = (600, 900)

CHARACTERS = {
    "zundamon": {
        "body_color": (95, 174, 62, 255),      # ずんだもん = 緑
        "outline_color": (60, 120, 40, 255),
        "cheek_color": (150, 220, 120, 255),
    },
    "shikoku_metan": {
        "body_color": (227, 154, 184, 255),    # 四国めたん = ピンク
        "outline_color": (170, 90, 120, 255),
        "cheek_color": (240, 190, 210, 255),
    },
}

EXPRESSIONS = ["normal", "happy", "sad", "angry", "surprised", "troubled"]

# 旧方式（パーツ単位の福笑い合成）で生成されていたファイル名。混乱を避けるため削除する。
LEGACY_FILENAMES = ["base.png", "mouth_open.png", "mouth_close.png"]
LEGACY_GLOB_PATTERNS = ["eyes_*.png"]


def _new_canvas() -> Image.Image:
    return Image.new("RGBA", CANVAS_SIZE, (0, 0, 0, 0))


def _draw_body(draw: ImageDraw.ImageDraw, color: dict) -> None:
    """体・輪郭・ほっぺ（表情/口の開閉によらず共通の部分）。"""
    w, h = CANVAS_SIZE
    # 頭
    draw.ellipse([w * 0.25, h * 0.10, w * 0.75, h * 0.55], fill=color["body_color"], outline=color["outline_color"], width=6)
    # 体
    draw.rounded_rectangle(
        [w * 0.30, h * 0.45, w * 0.70, h * 0.95], radius=60,
        fill=color["body_color"], outline=color["outline_color"], width=6,
    )
    # ほっぺ
    draw.ellipse([w * 0.28, h * 0.36, w * 0.40, h * 0.44], fill=color["cheek_color"])
    draw.ellipse([w * 0.60, h * 0.36, w * 0.72, h * 0.44], fill=color["cheek_color"])


def _draw_eyes(draw: ImageDraw.ImageDraw, expression: str) -> None:
    """表情（目・眉）。表情ごとに簡易的な形を変える。"""
    w, h = CANVAS_SIZE
    ey = h * 0.28
    black = (40, 30, 30, 255)

    if expression == "happy":
        # 笑い目（弧）
        draw.arc([w * 0.33, ey - 15, w * 0.45, ey + 15], start=200, end=340, fill=black, width=8)
        draw.arc([w * 0.55, ey - 15, w * 0.67, ey + 15], start=200, end=340, fill=black, width=8)
    elif expression == "sad":
        draw.line([w * 0.34, ey - 5, w * 0.44, ey + 8], fill=black, width=8)
        draw.line([w * 0.56, ey + 8, w * 0.66, ey - 5], fill=black, width=8)
        draw.line([w * 0.33, ey - 22, w * 0.45, ey - 14], fill=black, width=6)  # 眉(困り)
        draw.line([w * 0.55, ey - 14, w * 0.67, ey - 22], fill=black, width=6)
    elif expression == "angry":
        draw.ellipse([w * 0.34, ey - 10, w * 0.44, ey + 10], fill=black)
        draw.ellipse([w * 0.56, ey - 10, w * 0.66, ey + 10], fill=black)
        draw.line([w * 0.33, ey - 25, w * 0.45, ey - 15], fill=black, width=7)  # 吊り眉
        draw.line([w * 0.55, ey - 15, w * 0.67, ey - 25], fill=black, width=7)
    elif expression == "surprised":
        draw.ellipse([w * 0.35, ey - 14, w * 0.45, ey + 14], outline=black, width=6)
        draw.ellipse([w * 0.55, ey - 14, w * 0.65, ey + 14], outline=black, width=6)
    elif expression == "troubled":
        draw.ellipse([w * 0.34, ey - 9, w * 0.44, ey + 9], fill=black)
        draw.ellipse([w * 0.56, ey - 9, w * 0.66, ey + 9], fill=black)
        draw.arc([w * 0.32, ey - 30, w * 0.46, ey - 10], start=20, end=160, fill=black, width=6)
        draw.arc([w * 0.54, ey - 30, w * 0.68, ey - 10], start=20, end=160, fill=black, width=6)
    else:  # normal
        draw.ellipse([w * 0.34, ey - 12, w * 0.44, ey + 12], fill=black)
        draw.ellipse([w * 0.56, ey - 12, w * 0.66, ey + 12], fill=black)


def _draw_mouth(draw: ImageDraw.ImageDraw, mouth_open: bool) -> None:
    w, h = CANVAS_SIZE
    my = h * 0.42
    black = (40, 30, 30, 255)
    if mouth_open:
        draw.ellipse([w * 0.45, my, w * 0.55, my + 26], fill=black)
    else:
        draw.line([w * 0.45, my + 10, w * 0.55, my + 10], fill=black, width=6)


def draw_character(color: dict, expression: str, mouth_open: bool) -> Image.Image:
    """体+表情+口を1枚に合成済みのキャラクター画像を生成する。"""
    img = _new_canvas()
    draw = ImageDraw.Draw(img)
    _draw_body(draw, color)
    _draw_eyes(draw, expression)
    _draw_mouth(draw, mouth_open)
    return img


def draw_background_sample() -> Image.Image:
    w, h = 1920, 1080
    img = Image.new("RGB", (w, h), (235, 245, 250))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, h * 0.7, w, h], fill=(220, 230, 210))  # 床
    draw.rectangle([0, 0, w, h * 0.7], fill=(210, 230, 245))  # 壁
    draw.text((40, 40), "sample background (placeholder)", fill=(120, 120, 120))
    return img


def _clean_legacy_files(char_dir: Path) -> None:
    """旧方式（パーツ合成）のファイルが残っていたら削除する。"""
    for name in LEGACY_FILENAMES:
        p = char_dir / name
        if p.exists():
            p.unlink()
    for pattern in LEGACY_GLOB_PATTERNS:
        for p in char_dir.glob(pattern):
            p.unlink()


def main() -> None:
    for char_key, color in CHARACTERS.items():
        char_dir = ASSETS_DIR / char_key
        char_dir.mkdir(parents=True, exist_ok=True)
        _clean_legacy_files(char_dir)

        for expr in EXPRESSIONS:
            draw_character(color, expr, mouth_open=True).save(char_dir / f"{expr}_open.png")
            draw_character(color, expr, mouth_open=False).save(char_dir / f"{expr}_close.png")

        print(f"[OK] {char_key}: {{{','.join(EXPRESSIONS)}}} x (open/close)")

    bg_dir = ASSETS_DIR / "backgrounds"
    bg_dir.mkdir(parents=True, exist_ok=True)
    draw_background_sample().save(bg_dir / "sample_room.png")
    print("[OK] backgrounds/sample_room.png")


if __name__ == "__main__":
    main()
