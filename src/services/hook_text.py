"""
ショートの冒頭0〜2秒に出す「フック」の文字（スワイプされないよう、最初のセリフでは画面の真ん中に特大で出す）。

- 1セリフ目（HOOK_STYLE）: 画面の中央に、画面の幅いっぱいの特大の文字（0.5秒で「何の話か・自分のことか」が分かるように）。
- 2セリフ目以降の導入（HOOK_TOP_STYLE）: 同じ文字を、画面の上の方に一回り小さく出したまま（キャラクターの動きを見せる）。

ふつうの見出し（telop.render_headline_image）より大きく、サムネイルと同じ太い縁取り・影の文字で描く。
**語** で囲んだ語は赤く大きく、==語== は黄色になる（サムネイルと同じ書き方）。
"""
from __future__ import annotations

from PIL import Image

from src.services.thumbnail import _pop_text

HOOK_STYLE = "hook"            # Scene.headline_style の値（1セリフ目。画面の中央に特大）
HOOK_TOP_STYLE = "hook_top"    # 2セリフ目以降の導入（画面の上に一回り小さく）
HOOK_STYLES = (HOOK_STYLE, HOOK_TOP_STYLE)
# (文字の中心の高さ, 最大の高さ, 最大の文字サイズ)。高さは画面の高さ、文字サイズは画面の幅に対する比率。
# 縦画面の字幕（中心 0.6）・キャラクターの顔（0.65〜）にかからない位置にする
HOOK_LAYOUT = {HOOK_STYLE: (0.36, 0.3, 0.19), HOOK_TOP_STYLE: (0.2, 0.2, 0.13)}
HOOK_MAX_WIDTH_RATIO = 0.96
HOOK_ANGLE = -3.0              # 少し傾けて勢いを出す
HOOK_OUTLINE_SCALE = 1.0       # 縁取りの太さ（サムネイルと同じ）
HOOK_FILL_BOLD = 0.0           # 中の文字は太らせない（画数の多い漢字の線のすき間がつぶれないように）


def render_hook_image(text: str, resolution: tuple[int, int], style: str = HOOK_STYLE) -> Image.Image:
    """画面と同じ大きさの透明な画像に、フックの文字を描く（style: HOOK_STYLE＝中央に特大 / HOOK_TOP_STYLE＝上に小さめ）。"""
    w, h = resolution
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    lines = [line.strip() for line in str(text or "").replace("\\n", "\n").split("\n") if line.strip()][:3]
    if not lines:
        return canvas
    center_y, max_h, font = HOOK_LAYOUT.get(style, HOOK_LAYOUT[HOOK_STYLE])
    img = _pop_text(lines, int(w * HOOK_MAX_WIDTH_RATIO), int(h * max_h), int(w * font),
                    align="center", outline_scale=HOOK_OUTLINE_SCALE, fill_bold=HOOK_FILL_BOLD)
    img = img.rotate(HOOK_ANGLE, resample=Image.BICUBIC, expand=True)
    x = (w - img.width) // 2
    y = int(h * center_y) - img.height // 2
    canvas.alpha_composite(img, (max(0, x), max(0, y)))
    return canvas
