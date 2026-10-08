"""
ショートの冒頭0〜2秒に出す「フック」の文字（スワイプされないよう、画面の中央上に特大で出す）。

ふつうの見出し（telop.render_headline_image）より大きく、サムネイルと同じ太い縁取り・影の文字で描く。
**語** で囲んだ語は赤く大きく、==語== は黄色になる（サムネイルと同じ書き方）。
"""
from __future__ import annotations

from PIL import Image

from src.services.thumbnail import _pop_text

HOOK_STYLE = "hook"            # Scene.headline_style の値
HOOK_CENTER_Y_RATIO = 0.27     # 文字の中心の高さ（画面の高さに対する比率。キャラクターや黒板に重なりにくい位置）
HOOK_MAX_WIDTH_RATIO = 0.94
HOOK_MAX_HEIGHT_RATIO = 0.24
HOOK_FONT_RATIO = 0.15         # 最大の文字サイズ（画面幅に対する比率）
HOOK_ANGLE = -3.0              # 少し傾けて勢いを出す
HOOK_OUTLINE_SCALE = 1.0       # 縁取りの太さ（サムネイルと同じ）
HOOK_FILL_BOLD = 0.0           # 中の文字は太らせない（画数の多い漢字の線のすき間がつぶれないように）


def render_hook_image(text: str, resolution: tuple[int, int]) -> Image.Image:
    """画面と同じ大きさの透明な画像に、フックの文字を描く。"""
    w, h = resolution
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    lines = [line.strip() for line in str(text or "").replace("\\n", "\n").split("\n") if line.strip()][:3]
    if not lines:
        return canvas
    img = _pop_text(lines, int(w * HOOK_MAX_WIDTH_RATIO), int(h * HOOK_MAX_HEIGHT_RATIO), int(w * HOOK_FONT_RATIO),
                    align="center", outline_scale=HOOK_OUTLINE_SCALE, fill_bold=HOOK_FILL_BOLD)
    img = img.rotate(HOOK_ANGLE, resample=Image.BICUBIC, expand=True)
    x = (w - img.width) // 2
    y = int(h * HOOK_CENTER_Y_RATIO) - img.height // 2
    canvas.alpha_composite(img, (max(0, x), max(0, y)))
    return canvas
