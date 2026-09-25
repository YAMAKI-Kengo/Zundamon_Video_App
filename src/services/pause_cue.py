"""
視聴者がリピート・回答する間（Scene.pause_style）の合図: 画面の「3・2・1」「リピート！」、残り時間のバー、合図の音。

  repeat … 0〜lead_in 秒: 3 → 2 → 1（コチッ・コチッ・コチッ）→ 見出し「リピート！」と、残り時間が減っていくバー
  shadow … 0〜lead_in 秒: 3 → 2 → 1（コチッ・コチッ・コチッ）→ 見出し「音声に重ねて言ってみよう！」（お手本の音声がもう一度流れる）
  think  … 見出し「考えてみて！」と、残り秒数の数字・バー（最後の3秒はコチコチと鳴る）

映像は、書き出したシーンの映像の上に、そのときの合図の画像を1フレームずつ重ねる（合図の画像は状態ごとに1回だけ作る）。
"""
from __future__ import annotations

import math
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageDraw

from src.models import COUNTDOWN_FROM, COUNTDOWN_STEP_SECONDS, Scene
from src.services import telop

CIRCLE_CENTER_Y_RATIO = 0.36    # カウントダウンの円の中心の高さ（画面の高さに対する比率）
CIRCLE_DIAMETER_RATIO = 0.24    # カウントダウンの円の直径（画面の高さに対する比率）
CIRCLE_FILL = (255, 255, 255, 235)
CIRCLE_BORDER = (255, 190, 60, 255)
NUMBER_COLOR = (60, 50, 90, 255)
BAR_Y_RATIO = 0.22              # 残り時間のバーの高さ位置（見出しの下）
BAR_WIDTH_RATIO = 0.36
BAR_HEIGHT_RATIO = 0.014
BAR_COLOR = (255, 200, 70)
BAR_BACK_COLOR = (40, 40, 60)

TICK_HZ = 660.0      # 合図の音（どの間も同じ、低めで短い「コチッ」。高い音は耳に痛いため使わない）
TICK_SECONDS = 0.06
BEEP_VOLUME = 0.25


def _count_image(number: int, resolution: tuple[int, int], font_path: Optional[str]) -> Image.Image:
    width, height = resolution
    canvas = Image.new("RGBA", resolution, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    d = round(height * CIRCLE_DIAMETER_RATIO)
    cx, cy = width // 2, round(height * CIRCLE_CENTER_Y_RATIO)
    border = max(3, d // 22)
    draw.ellipse([cx - d // 2, cy - d // 2, cx + d // 2, cy + d // 2], fill=CIRCLE_FILL, outline=CIRCLE_BORDER, width=border)
    font = telop.load_font(font_path, round(d * 0.62))
    text = str(number)
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (box[0] + box[2]) / 2, cy - (box[1] + box[3]) / 2), text, font=font, fill=NUMBER_COLOR)
    return canvas


class PauseCue:
    """1つの間のシーンの合図（時間 t における重ね画像）。"""

    def __init__(self, scene: Scene, resolution: tuple[int, int], duration: float, font_path: Optional[str]):
        self.scene = scene
        self.resolution = resolution
        self.duration = duration
        self.font_path = font_path
        self.lead = min(scene.lead_in, duration)
        self._cache: dict[tuple, tuple[np.ndarray, np.ndarray, tuple[int, int]]] = {}

    def _state(self, t: float) -> tuple:
        style = self.scene.pause_style
        if style in ("repeat", "shadow") and t < self.lead:
            return ("count", COUNTDOWN_FROM - min(COUNTDOWN_FROM - 1, int(t / COUNTDOWN_STEP_SECONDS)))
        if style == "think":
            return ("think", max(1, math.ceil(self.duration - t)))
        return ("label",)

    def _layer(self, state: tuple):
        """状態ごとの重ね画像を、変化のある範囲だけ切り出して (RGB, アルファ, 左上) で返す。"""
        if state not in self._cache:
            headline = (self.scene.headline or "").strip()
            img = Image.new("RGBA", self.resolution, (0, 0, 0, 0))
            if state[0] == "count":
                img = _count_image(state[1], self.resolution, self.font_path)
            else:
                if headline:
                    img = Image.alpha_composite(img, telop.render_headline_image(headline, self.resolution, self.font_path))
                if state[0] == "think":
                    img = Image.alpha_composite(img, _count_image(state[1], self.resolution, self.font_path))
            box = img.getbbox() or (0, 0, 1, 1)
            crop = np.asarray(img.crop(box)).astype(np.float32)
            self._cache[state] = (crop[..., :3], crop[..., 3:4] / 255.0, (box[0], box[1]))
        return self._cache[state]

    def _progress(self, t: float) -> Optional[float]:
        """残り時間のバーの長さ（1→0）。カウントダウン中・シャドーイングの音声中は出さない。"""
        style = self.scene.pause_style
        if style == "repeat" and t >= self.lead:
            span = max(1e-3, self.duration - self.lead)
            return max(0.0, 1 - (t - self.lead) / span)
        if style == "think":
            return max(0.0, 1 - t / max(1e-3, self.duration))
        return None

    def apply(self, frame: np.ndarray, t: float) -> np.ndarray:
        rgb, alpha, (x, y) = self._layer(self._state(t))
        out = frame.copy()
        h, w = rgb.shape[:2]
        region = out[y:y + h, x:x + w].astype(np.float32)
        out[y:y + h, x:x + w] = (region * (1 - alpha) + rgb * alpha).astype(np.uint8)
        progress = self._progress(t)
        if progress is not None:
            width, height = self.resolution
            bw, bh = round(width * BAR_WIDTH_RATIO), max(4, round(height * BAR_HEIGHT_RATIO))
            bx, by = (width - bw) // 2, round(height * BAR_Y_RATIO)
            out[by:by + bh, bx:bx + bw] = BAR_BACK_COLOR
            out[by:by + bh, bx:bx + round(bw * progress)] = BAR_COLOR
        return out


def with_pause_cue(clip, scene: Scene, resolution: tuple[int, int], font_path: Optional[str]):
    """シーンの映像に、間の合図（カウントダウン・見出し・残り時間）を重ねた映像を返す。"""
    cue = PauseCue(scene, resolution, clip.duration, font_path)
    return clip.transform(lambda get_frame, t: cue.apply(get_frame(t), t))


def beep_times(scene: Scene, duration: float) -> list[tuple[float, float, float]]:
    """合図の音を鳴らす (開始秒, 周波数, 長さ) の一覧。"""
    beeps: list[tuple[float, float, float]] = []
    if scene.pause_style in ("repeat", "shadow"):
        beeps += [(k * COUNTDOWN_STEP_SECONDS, TICK_HZ, TICK_SECONDS) for k in range(COUNTDOWN_FROM)]
    elif scene.pause_style == "think":
        beeps += [(duration - k, TICK_HZ, TICK_SECONDS) for k in (3, 2, 1) if duration - k > 0.2]
    return [b for b in beeps if b[0] < duration]


def beep_frame_function(scene: Scene, duration: float) -> Callable:
    """合図の音（短いサイン波）の音声のフレーム関数（MoviePy の AudioClip 用・モノラル）。"""
    beeps = beep_times(scene, duration)

    def frame_function(t):
        t_arr = np.atleast_1d(np.asarray(t, dtype=np.float64))
        out = np.zeros_like(t_arr)
        for start, hz, length in beeps:
            local = t_arr - start
            mask = (local >= 0) & (local < length)
            if mask.any():
                env = np.minimum(1.0, np.minimum(local[mask] / 0.01, (length - local[mask]) / 0.03))
                out[mask] += BEEP_VOLUME * env * np.sin(2 * np.pi * hz * local[mask])
        return out.reshape(-1, 1) if np.ndim(t) else np.array([out[0]])

    return frame_function
