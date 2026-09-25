"""
動画に動きをつける（カメラワーク・画面の揺れ・キャラクターの動き・黒板の書き足し/切り替え）。

動きの無いシーンは video_builder が「口:開」「口:閉」の2枚を使い回して高速に作るが、動きのあるシーンは
ここで1フレームずつ合成する（build_motion_clip）。1フレームの合成順は次のとおり:

  1. 背景（静止画）
  2. 黒板/資料メディア … 黒板の内容が前のシーンから変わったときは切り替えアニメーション
                         （スライド/フェード/ワイプ）、同じ黒板に箇条書きが増えたときは
                         増えた行が左から書かれていくアニメーション
  3. 立ち絵 … 話者の呼吸（ゆらゆら）・跳ねる動き、非表示から表示に変わったキャラクターの登場（横から入る）
  4. カメラ … 話者へのアップ（パッと/ゆっくり）・全体のゆっくりズームイン・画面の揺れ
               （背景〜立ち絵をまとめて拡大/移動する。字幕・見出し・PR表記は動かさないよう後から重ねる）

各シーンの動きは Scene.camera / shake / char_motion で指定する。"auto" の場合は Project の自動演出の
設定（auto_camera 等）と、表情・効果音・場面から自動で決める（resolve_* 関数）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from moviepy import VideoClip
from PIL import Image, ImageChops

from src.models import Project, Scene
from src.services import slide_renderer
from src.utils.asset_loader import is_video_path, load_se_guide
from src.services.compositor import (
    character_side,
    dual_character_sprites,
    load_content_media_image,
    place_content_media,
)

# --- 自動演出のルール ---
ZOOM_EXPRESSIONS = {"surprised", "shock", "excited", "angry", "cry", "idea"}   # 話者にパッとアップする表情
JUMP_EXPRESSIONS = {"happy", "surprised", "excited"}                          # ぴょんと跳ねる表情
# 画面を揺らす効果音は config/se_guide.json の "shake": true で指定する（旧版の基本セットの名前も残す）
LEGACY_SHAKE_SE_NAMES = {"ガーン", "ドン"}
SHAKE_EXPRESSIONS = {"shock"}                                                  # 画面を揺らす表情

# --- 動きの大きさ・速さ ---
ZOOM_SPEAKER_SCALE = {False: 1.28, True: 1.2}   # 話者へのアップの倍率（キー: 縦画面か）
ZOOM_IN_SECONDS = 0.25                           # パッとアップするときのズーム時間
SLOW_ZOOM_SPEAKER_GAIN = 0.15                    # ゆっくりアップ: シーンの最後で何倍寄るか（+15%）
SLOW_ZOOM_GAIN = 0.05                            # 全体のゆっくりズームイン: ブロックの最後で+5%
SHAKE_SECONDS = 0.6
SHAKE_AMPLITUDE_RATIO = 0.012                    # 揺れ幅（画面幅に対する比率）
SHAKE_BASE_SCALE = 1.03                          # 揺らしても画面の端が見えないよう、揺れるシーンは少し拡大しておく
BOB_PERIOD = 2.0                                 # 呼吸の周期（秒）
BOB_AMPLITUDE_RATIO = 0.006                      # 呼吸の上下幅（画面高さに対する比率）
JUMP_SECONDS = 0.4
JUMP_HEIGHT_RATIO = 0.035                        # 跳ねる高さ（画面高さに対する比率）
ENTER_SECONDS = 0.45                             # 横から入ってくる時間
TRANSITION_SECONDS = 0.5                         # 黒板の切り替え時間
POP_SECONDS = 0.35                               # 画像（本の表紙など）がポンと現れる時間
FADE_SECONDS = 0.25                              # 黒板がふわっと現れる・消えるときのフェード時間
WRITE_SECONDS = 0.8                              # 箇条書き1行を書き足す時間（シーンが短いときは半分まで）


def _ease(p: float) -> float:
    """なめらかに動き出して止まる補間（0→1）。"""
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p)


def _ease_out_back(p: float) -> float:
    """最後に少し行き過ぎてから戻る補間（ポンと弾むような登場用）。"""
    p = min(1.0, max(0.0, p))
    c = 1.70158
    return 1 + (c + 1) * (p - 1) ** 3 + c * (p - 1) ** 2


def _ease_out(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


# ---------------------------------------------------------------------------
# 各シーンの動きの決定（"auto" の解決）
# ---------------------------------------------------------------------------

@dataclass
class SceneContext:
    """シーンの前後関係（動きの決定に使う）。"""
    prev: Optional[Scene]
    block_offset: float     # 同じ黒板（ブロック）が始まってから、このシーンの開始までの秒数
    block_duration: float   # 同じ黒板（ブロック）全体の秒数


def build_contexts(scenes: list[Scene], durations: Optional[list[float]] = None) -> list[SceneContext]:
    """全シーンの SceneContext を作る。ブロック = 場面と黒板の内容が同じシーンの連続。"""
    durations = durations or [s.duration for s in scenes]
    blocks: list[list[int]] = []
    for i, scene in enumerate(scenes):
        prev = scenes[i - 1] if i else None
        same_block = prev is not None and prev.section == scene.section and \
            slide_renderer.slide_key(prev) == slide_renderer.slide_key(scene)
        if same_block:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    contexts: list[Optional[SceneContext]] = [None] * len(scenes)
    for block in blocks:
        total = sum(durations[i] for i in block)
        offset = 0.0
        for i in block:
            contexts[i] = SceneContext(scenes[i - 1] if i else None, offset, max(total, 1e-3))
            offset += durations[i]
    return contexts  # type: ignore[return-value]


def _speaker_visible(scene: Scene) -> bool:
    return scene.speaker not in scene.hidden_characters


def resolve_camera(project: Project, scene: Scene) -> str:
    mode = scene.camera or "auto"
    if mode == "auto":
        if not project.auto_camera or scene.section == "ending":
            mode = "none"
        elif scene.expression in ZOOM_EXPRESSIONS and scene.text.strip():
            mode = "zoom_speaker"
        elif scene.section == "explain" and scene.has_slide:
            mode = "slow_zoom"
        else:
            mode = "none"
    if mode in ("zoom_speaker", "slow_zoom_speaker") and not _speaker_visible(scene):
        mode = "none"  # 話者が画面にいないので寄れない
    return mode


def resolve_shake(project: Project, scene: Scene) -> bool:
    if scene.shake == "on":
        return True
    if scene.shake == "off" or not project.auto_shake:
        return False
    se_name = Path(scene.se_path).stem if scene.se_path else ""
    shake_se = LEGACY_SHAKE_SE_NAMES | {name for name, cfg in load_se_guide().items() if cfg.get("shake")}
    return se_name in shake_se or scene.expression in SHAKE_EXPRESSIONS


def resolve_char_motion(project: Project, scene: Scene, prev: Optional[Scene] = None) -> set[str]:
    """話者の動き（"bob" / "jump" の集合）。話者が非表示・セリフが無いシーンでは動かさない。

    自動の「ぴょん」は、喜び・驚き系の表情になった瞬間（話者が交代した・表情が変わった）だけにする
    （同じ話者が同じ表情で話し続ける間に毎回跳ねると、しつこく見えるため）。
    """
    if not _speaker_visible(scene) or not scene.text.strip():
        return set()
    mode = scene.char_motion or "auto"
    if mode == "none":
        return set()
    if mode == "bob":
        return {"bob"}
    if mode == "jump":
        return {"jump", "bob"} if project.char_bob else {"jump"}
    motions = set()
    if project.char_bob:
        motions.add("bob")
    expression_changed = prev is None or prev.speaker != scene.speaker or prev.expression != scene.expression
    if project.auto_jump and scene.expression in JUMP_EXPRESSIONS and expression_changed:
        motions.add("jump")
    return motions


def entering_characters(project: Project, scene: Scene, prev: Optional[Scene]) -> set[str]:
    """前のシーンでは非表示で、このシーンから表示されるキャラクター（横から入ってくる）。"""
    if not project.char_slide_in or prev is None:
        return set()
    return set(prev.hidden_characters) - set(scene.hidden_characters)


def _board_key(scene: Scene):
    """画面中央に出しているもの（黒板の内容 or 資料メディアの画像）を表すキー。何も出していなければ None。"""
    if scene.illustration_path:
        return ("media", scene.illustration_path, scene.illustration_caption)
    if scene.note_text.strip():
        return ("media", "note", scene.note_text, scene.note_focus, scene.note_meaning)
    key = slide_renderer.slide_key(scene)
    if key is not None:
        return key if scene.show_board else None
    return ("media", scene.content_media_path) if scene.content_media_path else None


def board_change(project: Project, scene: Scene, prev: Optional[Scene]) -> Optional[str]:
    """前のシーンからの画面中央（黒板・資料画像）の変化。

    "pop"（イラスト・本の表紙などの画像が現れる） / "fade"（会話だけの画面・画像から黒板が現れる） /
    "hide"（黒板・画像が消えて会話だけの画面になる） / "transition"（別の黒板に切り替わる） /
    "write"（同じ黒板に箇条書きが増える） / None（変化なし・アニメーションしない）
    """
    if prev is None:
        return None
    before, after = _board_key(prev), _board_key(scene)
    if before == after:
        if after is not None and slide_renderer.slide_key(scene) is not None and \
                slide_renderer.visible_count(scene) > slide_renderer.visible_count(prev):
            return "write"
        return None
    # 動画の資料メディアが絡む切り替えはアニメーションしない
    if is_video_path(prev.content_media_path) or is_video_path(scene.content_media_path):
        return None
    if after is not None and after[0] == "media":
        return "pop"  # イラスト・本の表紙などの画像は、何から切り替わる場合もポンと現れる
    if after is None:
        return "hide"  # 黒板・画像を片付けて、2人の会話だけの画面に戻る
    if before is None:
        return "fade"  # 会話だけの画面に黒板が現れるときは、ふわっと表示する
    if before is not None and before[0] == "media" and after is not None:
        return "fade"  # 画像から黒板に戻るときは、ふわっと表示する（毎回スライドすると目まぐるしいため）
    if project.slide_transition == "none":
        return None
    return "transition"


def needs_motion(project: Project, scene: Scene, ctx: SceneContext) -> bool:
    if scene.before_image_path and scene.after_image_path:
        return False  # ビフォーアフター表示のシーンは動き無し（従来どおりの合成）
    return (
        resolve_camera(project, scene) != "none"
        or resolve_shake(project, scene)
        or bool(resolve_char_motion(project, scene, ctx.prev))
        or bool(entering_characters(project, scene, ctx.prev))
        or board_change(project, scene, ctx.prev) is not None
    )


# ---------------------------------------------------------------------------
# 1フレームずつの合成
# ---------------------------------------------------------------------------

def _load_board(scene: Optional[Scene], resolution: tuple[int, int]):
    """シーンの黒板/資料メディア画像を、画面上の表示サイズ・位置で返す（無ければ None）。"""
    if scene is None:
        return None
    path = slide_renderer.resolve_scene_content_media(scene, resolution)
    img = load_content_media_image(path)
    if img is None:
        return None
    return place_content_media(img, resolution)


def _text_bands(diff: Image.Image) -> list[tuple[int, int, int, int]]:
    """書き足された部分（差分マスク）を、文字の行ごとの帯 (上, 下, 左, 右) に分けて上から順に返す。

    差分のある行（y）が続いている範囲を1行とみなす（行間の空白で区切る）。行頭の「●」なども同じ帯に入る。
    """
    arr = np.asarray(diff) > 0
    rows = np.flatnonzero(arr.any(axis=1))
    if rows.size == 0:
        return []
    bands = []
    start = prev = int(rows[0])
    gap = max(2, diff.height // 100)
    for y in rows[1:]:
        y = int(y)
        if y - prev > gap:
            bands.append((start, prev + 1))
            start = y
        prev = y
    bands.append((start, prev + 1))
    result = []
    for y0, y1 in bands:
        cols = np.flatnonzero(arr[y0:y1].any(axis=0))
        result.append((y0, y1, int(cols[0]), int(cols[-1]) + 1))
    return result


def _paste_alpha(canvas: Image.Image, img: Image.Image, position: tuple[int, int], opacity: float = 1.0) -> None:
    if opacity <= 0:
        return
    mask = img.getchannel("A")
    if opacity < 1:
        mask = mask.point(lambda v: int(v * opacity))
    canvas.paste(img, position, mask)


def build_motion_clip(
    project: Project,
    scene: Scene,
    ctx: SceneContext,
    resolution: tuple[int, int],
    fps: int,
    duration: float,
    mouth_flags: list[bool],
    background: Image.Image,
    pr_label_overlay: Optional[Image.Image] = None,
) -> VideoClip:
    """動き付きのシーン映像（字幕・見出しは含まない）を作る。background は画面サイズの静止画。"""
    width, height = resolution
    is_portrait = height > width
    bg = background.convert("RGB")

    # --- 黒板 ---
    board_now = _load_board(scene, resolution)
    change = board_change(project, scene, ctx.prev)
    board_before = None
    write_mask = None
    write_bands: list[tuple[int, int, int, int]] = []
    if change in ("transition", "hide"):
        board_before = _load_board(ctx.prev, resolution)
    elif change == "write" and board_now is not None:
        previous_state = _load_board(ctx.prev, resolution)
        if previous_state is not None and previous_state[0].size == board_now[0].size:
            board_before = previous_state
            diff = ImageChops.difference(previous_state[0], board_now[0]).convert("L").point(lambda v: 255 if v > 8 else 0)
            write_bands = _text_bands(diff)
            if write_bands:
                write_mask = diff
            else:
                change = None
    transition_style = project.slide_transition
    write_seconds = min(WRITE_SECONDS, duration * 0.5)

    # --- 立ち絵 ---
    sprites = dual_character_sprites(
        scene.speaker, scene.expression, resolution, scene.hidden_characters, scene.partner_expression
    )
    motions = resolve_char_motion(project, scene, ctx.prev)
    entering = entering_characters(project, scene, ctx.prev) & set(sprites)
    total_frames = len(mouth_flags)

    # --- カメラ ---
    camera = resolve_camera(project, scene)
    shake = resolve_shake(project, scene)
    focus = (width / 2, height * 0.42)
    if scene.speaker in sprites:
        img, (x, y) = sprites[scene.speaker][False]
        focus = (x + img.width / 2, y + img.height * 0.22)  # 顔のあたり
    zoom_max = ZOOM_SPEAKER_SCALE[is_portrait]

    # 字幕・見出し・PR表記（カメラの動きの影響を受けない固定表示）。毎フレーム画面全体を重ねると重いため、
    # 描かれている部分だけを切り出しておく
    fixed_overlay = None
    if pr_label_overlay is not None:
        bbox = pr_label_overlay.getbbox()
        if bbox:
            fixed_overlay = (pr_label_overlay.crop(bbox), (bbox[0], bbox[1]))
    shake_amp = width * SHAKE_AMPLITUDE_RATIO

    def board_layer(canvas: Image.Image, t: float) -> None:
        if change == "hide" and board_before is not None and t < FADE_SECONDS:
            _paste_alpha(canvas, board_before[0], board_before[1], 1 - _ease(t / FADE_SECONDS))
            return
        if change == "fade" and board_now is not None and t < FADE_SECONDS:
            _paste_alpha(canvas, board_now[0], board_now[1], _ease(t / FADE_SECONDS))
            return
        if change == "pop" and board_now is not None and t < POP_SECONDS:
            p = t / POP_SECONDS
            scale = 0.3 + 0.7 * _ease_out_back(p)
            img, (x, y) = board_now
            w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
            small = img.resize((w, h), Image.BILINEAR)
            _paste_alpha(canvas, small, (x + (img.width - w) // 2, y + (img.height - h) // 2), min(1.0, p * 2.5))
            return
        if change == "transition" and t < TRANSITION_SECONDS and transition_style != "none":
            p = _ease(t / TRANSITION_SECONDS)
            if transition_style == "fade":
                if board_before is not None:
                    _paste_alpha(canvas, board_before[0], board_before[1], 1 - p)
                if board_now is not None:
                    _paste_alpha(canvas, board_now[0], board_now[1], p)
            elif transition_style == "wipe":
                if board_before is not None:
                    _paste_alpha(canvas, *board_before)
                if board_now is not None:
                    img, pos = board_now
                    cut = int(img.width * p)
                    if cut > 0:
                        _paste_alpha(canvas, img.crop((0, 0, cut, img.height)), pos)
            else:  # slide: 古い黒板が左へ出ていき、新しい黒板が右から入ってくる
                shift = int(width * p)
                if board_before is not None:
                    img, (x, y) = board_before
                    _paste_alpha(canvas, img, (x - shift, y))
                if board_now is not None:
                    img, (x, y) = board_now
                    _paste_alpha(canvas, img, (x + width - shift, y))
            return
        if change == "write" and write_mask is not None and t < write_seconds:
            before_img, pos = board_before
            _paste_alpha(canvas, before_img, pos)
            # 増えた文字を、上の行から順に・各行は左から右へ書いていく
            remaining = _ease_out(t / write_seconds) * sum(x1 - x0 for _, _, x0, x1 in write_bands)
            mask = Image.new("L", write_mask.size, 0)
            for y0, y1, x0, x1 in write_bands:
                cut = min(x1, x0 + int(remaining))
                if cut > x0:
                    mask.paste(write_mask.crop((x0, y0, cut, y1)), (x0, y0))
                remaining -= x1 - x0
                if remaining <= 0:
                    break
            canvas.paste(board_now[0], pos, ImageChops.multiply(mask, board_now[0].getchannel("A")))
            return
        if board_now is not None:
            _paste_alpha(canvas, *board_now)

    def character_offset(key: str, img: Image.Image, t: float) -> tuple[int, int]:
        dx = dy = 0.0
        if key == scene.speaker:
            if "bob" in motions:
                dy += height * BOB_AMPLITUDE_RATIO * math.sin(2 * math.pi * t / BOB_PERIOD)
            if "jump" in motions and t < JUMP_SECONDS:
                dy -= height * JUMP_HEIGHT_RATIO * math.sin(math.pi * t / JUMP_SECONDS)
        if key in entering and t < ENTER_SECONDS:
            dx += character_side(key) * (1 - _ease_out(t / ENTER_SECONDS)) * (img.width + width * 0.05)
        return round(dx), round(dy)

    def camera_box(t: float) -> Optional[tuple[float, float, float, float]]:
        scale, progress = 1.0, 0.0  # progress: 焦点へどれだけ寄せるか（0=画面中央、1=焦点）
        center_focus = focus
        if camera == "zoom_speaker":
            progress = _ease(t / ZOOM_IN_SECONDS)
            scale = 1 + (zoom_max - 1) * progress
        elif camera == "slow_zoom_speaker":
            progress = min(1.0, t / max(duration, 1e-3))
            scale = 1 + SLOW_ZOOM_SPEAKER_GAIN * progress
        elif camera == "slow_zoom":
            progress = min(1.0, (ctx.block_offset + t) / ctx.block_duration)
            scale = 1 + SLOW_ZOOM_GAIN * progress
            center_focus = (width / 2, height * 0.4)
        dx = dy = 0.0
        if shake:
            scale = max(scale, SHAKE_BASE_SCALE)
            if t < SHAKE_SECONDS:
                decay = math.exp(-t / 0.18)
                dx = shake_amp * decay * math.sin(2 * math.pi * 16 * t)
                dy = shake_amp * 0.6 * decay * math.cos(2 * math.pi * 13 * t)
        if scale <= 1.0001 and dx == 0 and dy == 0:
            return None
        box_w, box_h = width / scale, height / scale
        cx = width / 2 + (center_focus[0] - width / 2) * progress + dx
        cy = height / 2 + (center_focus[1] - height / 2) * progress + dy
        left = min(max(cx - box_w / 2, 0.0), width - box_w)
        top = min(max(cy - box_h / 2, 0.0), height - box_h)
        return (left, top, left + box_w, top + box_h)

    def make_frame(t: float):
        frame_idx = min(int(round(t * fps)), max(total_frames - 1, 0))
        mouth_open = mouth_flags[frame_idx] if mouth_flags else False
        canvas = bg.copy()
        board_layer(canvas, t)
        for key, variants in sprites.items():
            img, (x, y) = variants[mouth_open if key == scene.speaker else False]
            dx, dy = character_offset(key, img, t)
            canvas.paste(img, (x + dx, y + dy), img)
        box = camera_box(t)
        if box is not None:
            canvas = canvas.resize((width, height), Image.BILINEAR, box=box)
        if fixed_overlay is not None:
            canvas.paste(fixed_overlay[0], fixed_overlay[1], fixed_overlay[0])
        return np.asarray(canvas)

    return VideoClip(frame_function=make_frame, duration=duration).with_fps(fps)


def describe_motion(project: Project, scene: Scene, ctx: SceneContext) -> list[str]:
    """UI表示用: このシーンで実際に付く動きの説明。"""
    from src.models import CAMERA_LABELS, TRANSITION_LABELS

    items = []
    camera = resolve_camera(project, scene)
    if camera != "none":
        items.append(f"🎥 {CAMERA_LABELS[camera]}")
    if resolve_shake(project, scene):
        items.append("📳 画面の揺れ")
    motions = resolve_char_motion(project, scene, ctx.prev)
    if "jump" in motions:
        items.append("🐸 ぴょん")
    if "bob" in motions:
        items.append("〰 ゆらゆら")
    if entering_characters(project, scene, ctx.prev):
        items.append("🚶 登場")
    change = board_change(project, scene, ctx.prev)
    if change == "transition":
        items.append(f"🔀 黒板切り替え（{TRANSITION_LABELS.get(project.slide_transition, '')}）")
    elif change == "write":
        items.append("✍ 箇条書きを書き足し")
    elif change == "pop":
        items.append("🖼 画像がポンと登場")
    elif change == "fade":
        items.append("🔀 黒板がふわっと登場")
    elif change == "hide":
        items.append("💬 黒板を片付けて会話だけに")
    return items
