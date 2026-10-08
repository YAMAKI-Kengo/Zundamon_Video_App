"""
MoviePyを用いた最終的な動画書き出し処理。

処理の流れ（シーンごと）:
  1. voicevox_client で音声(wav)を合成する。表示秒数を超えそうな場合は話速(speedScale)を
     自動的に引き上げて、なるべく指定秒数ぴったりに収まる音声にする（上限あり）。
  2. lipsync でフレームごとの口の開閉パターンを解析する。
  3. compositor で、画面左「ずんだもん」・右「四国めたん」を常時表示する2人実況レイアウトの
     完成フレーム（背景+立ち絵）を合成する。話者だけがリップシンクし、話していない方は
     待機表情・口閉じで固定のため、背景が画像の場合は「口:開」「口:閉」の2パターンだけ
     作って使い回す（1フレームずつ再合成しない軽量化）。背景が動画ファイルの場合は
     背景自体がフレームごとに変わるためこの最適化ができず、background_video.py で
     背景動画のその時刻のコマを取り出し、立ち絵オーバーレイ（口:開/口:閉の透過PNG）を
     都度重ねて1フレームずつ合成する。
  4. 音声はシーンの「表示秒数」に合わせてトリミング／無音パディングする
     （表示秒数を絶対とし、映像は常にその長さぶん維持する。話速調整でも収まらない場合はここで
     強制的にカットされる）。
  5. テロップ（読み上げテキストそのまま）を、視認性向上のための半透明の背景ボックス付きで
     重ねる（src.services.telop）。文字色・縁取り色は話者ごとに config/characters.json の
     "telop" 設定を反映する。改行はユーザーが読み上げテキスト内に入力した改行を尊重しつつ、
     画面幅に収まらない行はPillowでのフォント幅実測に基づき自動で折り返す。
全シーンを結合したうえで、BGMをナレーション音声にミックスしてから1本のMP4として書き出す。
BGMはシーンごとに「シーン個別 → 場面（導入/解説/まとめ）ごと → プロジェクト全体」の優先順で決まり
（Project.resolve_bgm_path）、同じBGMが続くシーンを1区間にまとめて、区間ごとにループ/トリミング・
指定音量(project.bgm_volume)を適用する。曲が切り替わる境界はクロスフェード、動画の頭・お尻と
BGMなしの区間との境界はフェードイン/アウトにする（plan_bgm_segments / _build_bgm_clips）。
書き出し時のエンコード速度（x264のpreset）は speed_preset 引数で選べる（ENCODE_PRESETS参照。
既定は"balanced"＝veryfast）。あわせてffmpegのマルチスレッドエンコードも有効にしている。

非エンジニアへの配布を想定し、想定される失敗（VOICEVOX未起動、背景画像破損、
音声合成の一時的な失敗など）はすべてキャッチし、可能な限り処理を継続したうえで、
最終的に分かりやすい警告メッセージとしてUIに返す設計にしてある。
"""
from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image
from moviepy import (
    AudioClip,
    AudioFileClip,
    CompositeAudioClip,
    ImageSequenceClip,
    VideoClip,
    afx,
    concatenate_audioclips,
    concatenate_videoclips,
)

from src.models import Project, Scene
from src.services import (
    background_video,
    book_script,
    hook_text,
    lipsync,
    motion,
    pause_cue,
    slide_renderer,
    telop,
    video_metadata,
    voicevox_client,
)
from src.services.compositor import (
    BACKGROUND_FALLBACK_COLOR,
    blur_background,
    compose_dual_character_overlay,
    compose_dual_scene_frame,
    fit_background,
    apply_mood,
    load_background_image,
    render_transition_card,
    load_content_media_image,
    guest_on_screen,
    paste_content_media,
    render_pr_label_overlay,
)
from src.services.voicevox_client import VoicevoxConnectionError, VoicevoxSynthesisError
from src.utils.asset_loader import get_telop_style, is_video_path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "output"
TMP_AUDIO_DIR = PROJECT_ROOT / "tmp" / "audio"

DEFAULT_FPS = 30
AUDIO_SAMPLE_RATE = 44100

# 書き出し速度（x264のpreset）。数値が小さい(左)ほどエンコードが速く、ファイルサイズは
# 大きくなりやすい。以前は明示的に指定しておらずMoviePy/ffmpegの既定値"medium"が
# 使われていたが、体感の生成時間を短縮するため既定を"veryfast"に変更した。
# "medium"相当の画質を優先したい場合はUI側で「高画質優先」を選ぶと以前と同じ設定になる。
ENCODE_PRESETS: dict[str, str] = {
    "fast": "ultrafast",     # 高速優先: 最速。プレビュー・下書き向け（ファイルサイズは大きめ）
    "balanced": "veryfast",  # バランス（既定）: 体感速度と画質のバランスを取った設定
    "quality": "medium",     # 高画質優先: MoviePy/ffmpegの従来の既定相当。時間がかかる
}
DEFAULT_SPEED_PRESET = "balanced"

ProgressCallback = Callable[[str], None]


class VideoBuildError(Exception):
    """動画生成処理中に発生した、VOICEVOX未起動以外の予期しないエラー。"""


@dataclass
class BuildResult:
    output_path: Path
    warnings: list[str] = field(default_factory=list)


CARD_FADE_SECONDS = 0.3  # 場面転換テロップの前後で、黒から/黒へフェードする長さ


class _SkipSynthesis(Exception):
    """VOICEVOXでの合成をしないシーン（録音済みの音声を使う・リピート練習の無音）。"""


def _noop(_message: str) -> None:
    pass


def _silent_audio_clip(duration: float) -> AudioClip:
    """指定秒数の無音AudioClipを生成する。"""
    duration = max(duration, 1.0 / AUDIO_SAMPLE_RATE)

    def _frame_function(t):
        t_arr = np.asarray(t)
        if t_arr.ndim == 0:
            return np.array([0.0])
        return np.zeros((t_arr.shape[0], 1))

    return AudioClip(frame_function=_frame_function, duration=duration, fps=AUDIO_SAMPLE_RATE)


VOICE_TARGET_RMS = 0.17   # 声の大きさの目標（話している部分の平均音量。約 -15dBFS）
VOICE_MAX_PEAK = 0.95     # 声を大きくしても、いちばん大きい音がこれを超えない（音割れ防止）
VOICE_MAX_GAIN = 4.0      # 声を大きくする倍率の上限


def _normalize_voice(audio_clip: AudioClip, path) -> AudioClip:
    """セリフの声の大きさを、シーンごとに同じくらいにそろえる（VOICEVOXの声もネイティブ音声も）。

    VOICEVOXの声はそのままだと小さめで、BGMに埋もれやすい。話している部分の平均音量を VOICE_TARGET_RMS に
    合わせ、音が割れないよう VOICE_MAX_PEAK を超えない範囲で大きくする（小さくはしない）。
    音量は音声ファイルを直接読んで測る（MoviePy の to_soundarray は、元と違うサンプリング周波数を指定すると
    正しい値が返らないことがあるため）。
    """
    try:
        import soundfile as sf

        samples = np.asarray(sf.read(str(path), dtype="float32", always_2d=True)[0])
    except Exception:  # noqa: BLE001 - soundfile で読めない形式は MoviePy で、元の周波数のまま読む
        try:
            samples = np.asarray(audio_clip.to_soundarray(fps=audio_clip.fps), dtype=np.float32)
        except Exception:  # noqa: BLE001 - 解析できない音声はそのまま使う
            return audio_clip
    level = np.abs(samples).max(axis=1) if samples.ndim == 2 else np.abs(samples)
    active = level[level > 0.02]
    if active.size == 0:
        return audio_clip
    rms = float(np.sqrt(np.mean(active ** 2)))
    gain = min(VOICE_TARGET_RMS / max(rms, 1e-6), VOICE_MAX_PEAK / max(float(level.max()), 1e-6), VOICE_MAX_GAIN)
    return audio_clip.with_effects([afx.MultiplyVolume(gain)]) if gain > 1.02 else audio_clip


def _fit_audio_to_duration(audio_clip: Optional[AudioClip], target_duration: float) -> AudioClip:
    """音声クリップをシーンの確定尺(target_duration)に合わせる。

    - 音声が長い場合: target_duration で強制的にトリミングする。
    - 音声が短い/存在しない場合: 無音を足してtarget_durationまで延長する
      （その間も映像側は背景・立ち絵を維持し続ける＝呼び出し側で保証済み）。
    """
    if audio_clip is None:
        return _silent_audio_clip(target_duration)

    if audio_clip.duration is None:
        return _silent_audio_clip(target_duration)

    if audio_clip.duration > target_duration:
        return audio_clip.subclipped(0, target_duration)
    if audio_clip.duration < target_duration:
        silence_tail = _silent_audio_clip(target_duration - audio_clip.duration)
        return concatenate_audioclips([audio_clip, silence_tail])
    return audio_clip


def _build_fixed_overlay(
    scene: Scene,
    resolution: tuple[int, int],
    font_path: Optional[str],
    pr_label_overlay: Optional[Image.Image],
    warnings: list[str],
    label: str,
    progress: ProgressCallback,
) -> Optional[Image.Image]:
    """PR表記・画面上部の見出し文字・字幕（テロップ）を、1枚の透過画像にまとめる（無ければ None）。"""
    layers: list[Image.Image] = []
    if pr_label_overlay is not None:
        layers.append(pr_label_overlay)
    # リピート・回答の間の見出しは、カウントダウンに合わせて出し入れするので、ここでは焼き込まない（pause_cue）
    if scene.headline and scene.headline.strip() and not scene.pause_style:
        if scene.headline_style == hook_text.HOOK_STYLE:
            layers.append(hook_text.render_hook_image(scene.headline, resolution))
        else:
            layers.append(telop.render_headline_image(scene.headline, resolution, font_path))
    # 字幕（読み上げテキストをそのまま表示。文字色は話者ごとに変える。字幕なしのシーンは出さない）
    if scene.show_telop and scene.text and scene.text.strip():
        progress(f"{label}: テロップを合成中…")
        try:
            telop_style = get_telop_style(scene.speaker)
            layers.append(telop.render_telop_image(
                scene.text, resolution, font_path,
                color=telop_style["color"], stroke_color=telop_style["stroke_color"],
                bg_color=telop_style["box_color"], border_color=telop_style["border_color"] or None,
                sub_text=scene.translation, word_wrap=scene.lang == "en",
            ))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{label}: テロップの描画に失敗したため、テロップなしで書き出しました（{e}）")
    if not layers:
        return None
    combined = Image.new("RGBA", resolution, (0, 0, 0, 0))
    for layer in layers:
        combined = Image.alpha_composite(combined, layer.convert("RGBA"))
    return combined


def _resolve_dynamic_background(
    path: Optional[str],
    target_duration: float,
    resolution: tuple[int, int],
    warnings: list[str],
    label: str,
    extra_clips: list,
    background_blur: float = 0.0,
    mood: str = "",
):
    """背景（画像 or 動画）を、フレームごと合成用に解決する。

    戻り値は (動画クリップ or None, 静止画像 or None) のタプルで、常にどちらか一方だけが
    値を持つ（動画として開けた場合は動画クリップ側、それ以外は静止画像側に必ず何か入る
    ＝ load_background_image() が未指定/失敗時も単色フォールバックを返すため）。

    ここで開いた動画クリップ(clip)は、返り値のVideoClipのフレーム生成関数が内部で
    参照し続けるだけで、返り値自身の.close()では閉じられない（別オブジェクトのため）。
    そのため呼び出し元(build_video)で確実に.close()できるよう、extra_clipsに追記しておく。
    """
    if path and is_video_path(path):
        try:
            clip = background_video.build_looping_background_clip(path, target_duration)
            extra_clips.append(clip)
            return clip, None
        except Exception as e:  # noqa: BLE001 - 背景動画の破損・非対応コーデック等、様々な失敗要因を丁寧な警告に変換する
            warnings.append(f"{label}: 背景動画の読み込みに失敗したため、単色の背景で書き出しました（{e}）")
            return None, Image.new("RGBA", resolution, BACKGROUND_FALLBACK_COLOR)
    return None, load_background_image(path, resolution, background_blur, mood)


def _resolve_dynamic_content_media(
    path: Optional[str],
    target_duration: float,
    warnings: list[str],
    label: str,
    extra_clips: list,
):
    """資料メディア（画像 or 動画・任意項目）を、フレームごと合成用に解決する。

    戻り値は (動画クリップ or None, 静止画像 or None)。資料メディアは未指定でもよいため、
    背景と違い両方Noneのケース（＝資料メディアなし）もある。
    """
    if not path:
        return None, None
    if is_video_path(path):
        try:
            clip = background_video.build_looping_background_clip(path, target_duration)
            extra_clips.append(clip)
            return clip, None
        except Exception as e:  # noqa: BLE001 - 資料動画の破損・非対応コーデック等、様々な失敗要因を丁寧な警告に変換する
            warnings.append(f"{label}: 資料動画の読み込みに失敗したため、資料なしで書き出しました（{e}）")
            return None, None
    img = load_content_media_image(path)
    if img is None:
        warnings.append(f"{label}: 資料画像の読み込みに失敗したため、資料なしで書き出しました。")
    return None, img


def _changed_background(project: Optional[Project], scene: Scene,
                        motion_context: Optional["motion.SceneContext"], current: Optional[str]) -> Optional[str]:
    """前のシーンと背景の場所（画像）が違えば、前のシーンの背景のパスを返す（ふわっと切り替えるため）。

    前のシーンが場面転換テロップ（「3日後…」など全画面の文字）のときや、背景が動画のときは切り替えない。
    """
    prev = motion_context.prev if motion_context is not None else None
    if project is None or prev is None or prev.card_text.strip() or scene.card_text.strip():
        return None
    before = book_script.effective_background_path(project, prev)
    if not before or not current or before == current or is_video_path(before) or is_video_path(current):
        return None
    return before


def _build_dynamic_scene_clip(
    scene: Scene,
    effective_background_path: Optional[str],
    resolution: tuple[int, int],
    fps: int,
    actual_duration: float,
    mouth_flags: list[bool],
    warnings: list[str],
    label: str,
    extra_clips: list,
    pr_label_overlay: Optional[Image.Image] = None,
    background_blur: float = 0.0,
):
    """背景・資料メディアの少なくとも一方が動画ファイルの場合の映像クリップを構築する。

    背景・資料メディアのいずれの動画も、シーンの表示秒数(actual_duration)に合わせて
    自動でループ／トリミングされ、音声は常にミュートする（VOICEVOXのナレーション・BGMと
    混ざらないように）。読み込みに失敗した場合（壊れたファイル・非対応コーデック等）でも
    アプリを落とさず、警告を追加したうえで処理を継続する。

    合成順序は「背景 → 資料メディア（あれば） → 立ち絵オーバーレイ →
    PR表記バッジ（あれば）」の順で、compose_dual_scene_frame()（画像のみの高速パス）と
    同じ重なり順になるようにしてある。
    """
    bg_clip, bg_static = _resolve_dynamic_background(
        effective_background_path, actual_duration, resolution, warnings, label, extra_clips, background_blur,
        scene.mood,
    )
    media_clip, media_static = _resolve_dynamic_content_media(
        scene.content_media_path, actual_duration, warnings, label, extra_clips
    )

    try:
        overlay_closed = np.array(compose_dual_character_overlay(
            scene.speaker, scene.expression, False, resolution, scene.render_hidden, scene.partner_expression
        ))
        overlay_open = np.array(compose_dual_character_overlay(
            scene.speaker, scene.expression, True, resolution, scene.render_hidden, scene.partner_expression
        ))
    except Exception as e:  # noqa: BLE001 - 立ち絵合成の失敗要因は多岐にわたるため広く捕捉して警告に変換する
        raise VideoBuildError(f"{label}: 立ち絵の合成に失敗しました（{e}）") from e

    total_frames = len(mouth_flags)
    fallback_bg_rgba = Image.new("RGBA", resolution, BACKGROUND_FALLBACK_COLOR)

    def make_frame(t):
        frame_idx = min(int(round(t * fps)), max(total_frames - 1, 0))
        mouth_open_flag = mouth_flags[frame_idx] if mouth_flags else False
        overlay = overlay_open if mouth_open_flag else overlay_closed

        # 背景レイヤー（キャッシュ済みの静止画をそのまま使う場合はcopy()して、
        # 後続のpaste()がキャッシュ済み画像自体を書き換えてしまわないようにする）
        if bg_clip is not None:
            try:
                bg_frame = bg_clip.get_frame(t)
                canvas = apply_mood(blur_background(
                    fit_background(Image.fromarray(bg_frame).convert("RGBA"), resolution), background_blur
                ), scene.mood)
            except Exception:  # noqa: BLE001 - 背景動画の一部フレーム取得に失敗しても動画全体の書き出しは継続する
                canvas = fallback_bg_rgba.copy()
        else:
            canvas = (bg_static or fallback_bg_rgba).copy()

        # 資料メディアレイヤー（背景の上・立ち絵の下。未指定なら何もしない）
        if media_clip is not None:
            try:
                media_frame = media_clip.get_frame(t)
                media_img = Image.fromarray(media_frame).convert("RGBA")
                paste_content_media(canvas, media_img, resolution, guest_on_screen(resolution, scene.render_hidden))
            except Exception:  # noqa: BLE001 - 資料動画の一部フレーム取得に失敗しても、その1コマだけ資料なしで継続する
                pass
        elif media_static is not None:
            paste_content_media(canvas, media_static, resolution, guest_on_screen(resolution, scene.render_hidden))

        composed = Image.alpha_composite(canvas, Image.fromarray(overlay))
        if pr_label_overlay is not None:
            composed = Image.alpha_composite(composed, pr_label_overlay)
        return np.array(composed.convert("RGB"))

    return VideoClip(frame_function=make_frame, duration=actual_duration).with_fps(fps)


def _build_scene_clip(
    scene: Scene,
    scene_index: int,
    resolution: tuple[int, int],
    fps: int,
    font_path: Optional[str],
    warnings: list[str],
    progress: ProgressCallback,
    extra_clips: list,
    common_background_path: Optional[str] = None,
    pr_label_overlay: Optional[Image.Image] = None,
    reading_dict: Optional[list[dict]] = None,
    background_override: Optional[str] = None,
    project: Optional[Project] = None,
    motion_context: Optional[motion.SceneContext] = None,
    audio_dir: Path = TMP_AUDIO_DIR,
):
    """1シーン分の(音声付き)動画クリップを構築する。

    project と motion_context を渡すと、カメラワーク・キャラクターの動き・黒板の切り替えなどの
    動き（src.services.motion）が必要なシーンは、1フレームずつ合成する動き付きの映像にする。

    common_background_path はプロジェクト全体の共通背景。シーン自身に背景が
    設定されていればそちらを優先し、未設定の場合のみ共通背景を使う。
    """
    speech_speed = project.speech_speed if project is not None else voicevox_client.DEFAULT_SPEECH_SPEED
    label = f"シーン{scene_index + 1}"
    effective_background_path = background_override or scene.background_path or common_background_path

    # 場面転換テロップ（「3日後…」など）: 全画面の文字だけのシーン。前後は黒から/黒へ少しフェードする
    if scene.card_text.strip():
        progress(f"{label}: 場面転換テロップを作成中…")
        card = np.array(render_transition_card(effective_background_path, resolution, scene.card_text).convert("RGB"))
        duration = max(1.0 / fps, round(scene.duration * fps) / fps)
        fade = min(CARD_FADE_SECONDS, duration / 3)

        def card_frame(t: float, _card=card, _duration=duration, _fade=fade):
            level = min(1.0, t / _fade if _fade else 1.0, (_duration - t) / _fade if _fade else 1.0)
            return _card if level >= 1.0 else (_card * max(0.0, level)).astype(np.uint8)

        return VideoClip(frame_function=card_frame, duration=duration).with_fps(fps).with_audio(
            _silent_audio_clip(duration)
        )

    # 黒板スライドの内容が入力されていれば、スライド画像を生成して資料メディアとして扱う
    # （以降の合成処理は通常の資料メディアと同じ経路を通る）
    if scene.has_slide or scene.illustration_path:
        progress(f"{label}: 黒板スライド・イラストを生成中…")
        try:
            scene = replace(scene, content_media_path=slide_renderer.resolve_scene_content_media(scene, resolution))
        except Exception as e:  # noqa: BLE001 - スライド描画に失敗しても動画全体の書き出しは継続する
            warnings.append(f"{label}: 黒板スライドの生成に失敗したため、スライドなしで書き出しました（{e}）")

    # 1. 音声合成（指定秒数に収まるよう、必要なら話速を自動調整する）
    #    ネイティブ音声などの録音済みの音声があればそれを使い、リピート練習の間（silent）は音声なしにする
    audio_path = None
    if scene.silent:
        voice_text = ""
    elif scene.voice_path and Path(scene.voice_path).exists():
        voice_text = ""
        audio_path = Path(scene.voice_path)
    else:
        voice_text = scene.reading.strip() or scene.text
        if scene.lang == "en" and not scene.reading.strip() and scene.text.strip():
            warnings.append(
                f"{label}: 英語のセリフにネイティブ音声（{scene.audio_id or 'ID未設定'}）も読み（カタカナ）も無いため、"
                "VOICEVOXでそのまま読ませました（英語は正しく読めません）。"
            )
    if voice_text:
        progress(f"{label}: VOICEVOXで音声合成中…")
    try:
        if not voice_text:
            raise _SkipSynthesis
        result = voicevox_client.synthesize_voice(
            voice_text,
            scene.speaker,
            output_path=audio_dir / f"scene{scene_index}_{uuid.uuid4().hex[:8]}.wav",
            target_duration=scene.duration,
            reading_dict=reading_dict,
            speech_speed=speech_speed,
        )
        audio_path = result.audio_path
        if result.speed_scale - speech_speed > 0.01:
            if result.speed_capped:
                warnings.append(
                    f"{label}: 表示秒数({scene.duration:.1f}秒)に収めるには話速の上限"
                    f"(x{voicevox_client.MAX_SPEED_SCALE:.1f})を超える調整が必要だったため、"
                    f"話速はx{result.speed_scale:.2f}までに留め、超過分は末尾を強制的にカットしました。"
                )
            else:
                warnings.append(
                    f"{label}: 表示秒数({scene.duration:.1f}秒)に収めるため、"
                    f"話速を自動的にx{result.speed_scale:.2f}に調整しました。"
                )
    except VoicevoxConnectionError:
        # VOICEVOX自体が落ちている/止まっている場合は、これ以上処理を続けても無駄なので
        # 呼び出し元(build_video)まで伝播させて即座に止める
        raise
    except VoicevoxSynthesisError as e:
        warnings.append(f"{label}: 音声合成に失敗したため、このシーンは無音で書き出しました（{e}）")
        audio_path = None
    except _SkipSynthesis:
        pass  # 録音済みの音声を使う / 音声なしのシーン

    # 2. リップシンク解析（表示秒数を基準にフレーム数を確定させる）
    progress(f"{label}: リップシンクを解析中…")
    # カウントダウンのあとに声を出すシーン（シャドーイング）は、その間は口を閉じたままにする
    lead_frames = round(scene.lead_in * fps) if scene.lead_in and audio_path else 0
    mouth_flags = [False] * lead_frames + lipsync.analyze_mouth_frames(
        audio_path, max(1.0 / fps, scene.duration - lead_frames / fps), fps=fps
    )
    total_frames = len(mouth_flags)
    actual_duration = total_frames / fps  # フレーム数から逆算した確定尺（音声/字幕もこれに合わせる）

    # 画面に固定で重ねるもの（PR表記・画面上部の見出し・字幕）は、ここで1枚の透過画像にまとめておき、
    # 各合成処理の最後に直接焼き込む（MoviePyのCompositeVideoClipで重ねると、1フレームごとの
    # 合成処理が非常に重く、動きのあるシーンの書き出しが何倍も遅くなるため）。
    # カメラワークでズーム・揺れをしても、これらは拡大されず定位置に表示される。
    pr_label_overlay = _build_fixed_overlay(scene, resolution, font_path, pr_label_overlay, warnings, label, progress)

    background_blur = project.background_blur if project is not None else 0.0

    # 3. 立ち絵+背景+資料メディアの合成（2人常時表示）
    progress(f"{label}: 立ち絵を合成中…")
    needs_dynamic = is_video_path(effective_background_path) or is_video_path(scene.content_media_path)
    background_before_path = _changed_background(project, scene, motion_context, effective_background_path)
    if needs_dynamic:
        # 背景・資料メディアの少なくとも一方が動画ファイルの場合、絵そのものがフレームごとに
        # 変わるため「口:開」「口:閉」を使い回す最適化はできず、1フレームずつ合成する
        video_clip = _build_dynamic_scene_clip(
            scene, effective_background_path, resolution, fps, actual_duration, mouth_flags, warnings, label,
            extra_clips, pr_label_overlay=pr_label_overlay, background_blur=background_blur,
        )
    elif project is not None and motion_context is not None and (
        motion.needs_motion(project, scene, motion_context) or background_before_path is not None
    ):
        # カメラワーク・キャラクターの動き・黒板の切り替え/書き足しがあるシーンは1フレームずつ合成する
        # （背景・資料が動画のシーンは上の分岐で処理され、動きは付かない）
        progress(f"{label}: 動き（カメラ・キャラクター・黒板）を合成中…")
        try:
            video_clip = motion.build_motion_clip(
                project, scene, motion_context, resolution, fps, actual_duration, mouth_flags,
                load_background_image(effective_background_path, resolution, background_blur, scene.mood),
                pr_label_overlay,
                background_before=load_background_image(
                    background_before_path, resolution, background_blur, motion_context.prev.mood,
                ) if background_before_path is not None else None,
            )
        except Exception as e:  # noqa: BLE001 - 動きの合成に失敗しても、動き無しで書き出しを続ける
            warnings.append(f"{label}: 動きの合成に失敗したため、動き無しで書き出しました（{e}）")
            video_clip = None
    else:
        video_clip = None
    if video_clip is None and not needs_dynamic:
        # 背景・資料メディアがどちらも画像（または未指定）の場合、話者だけ「口:開」「口:閉」の
        # 2パターンを作り、使い回す（1フレームずつ再合成しない軽量化）
        try:
            frame_closed = np.array(
                compose_dual_scene_frame(
                    scene.speaker, scene.expression, False, effective_background_path, resolution,
                    content_media_path=scene.content_media_path,
                    pr_label_overlay=pr_label_overlay,
                    hidden_characters=scene.render_hidden,
                    partner_expression=scene.partner_expression,
                    background_blur=background_blur,
                    mood=scene.mood,
                ).convert("RGB")
            )
            frame_open = np.array(
                compose_dual_scene_frame(
                    scene.speaker, scene.expression, True, effective_background_path, resolution,
                    content_media_path=scene.content_media_path,
                    pr_label_overlay=pr_label_overlay,
                    hidden_characters=scene.render_hidden,
                    partner_expression=scene.partner_expression,
                    background_blur=background_blur,
                    mood=scene.mood,
                ).convert("RGB")
            )
        except Exception as e:  # noqa: BLE001 - 画像合成の失敗要因は多岐にわたるため広く捕捉して警告に変換する
            raise VideoBuildError(f"{label}: 立ち絵/背景の合成に失敗しました（{e}）") from e

        frame_sequence = [frame_open if flag else frame_closed for flag in mouth_flags]
        video_clip = ImageSequenceClip(frame_sequence, fps=fps)

    # 4. 音声をシーンの確定尺に合わせる（長ければトリミング、短ければ無音で延長）
    audio_clip = _normalize_voice(AudioFileClip(str(audio_path)), audio_path) if audio_path else None
    if lead_frames:
        # カウントダウン（合図の音）→ 声
        lead_seconds = lead_frames / fps
        fitted_audio = concatenate_audioclips([
            AudioClip(pause_cue.beep_frame_function(scene, lead_seconds), duration=lead_seconds, fps=AUDIO_SAMPLE_RATE),
            _fit_audio_to_duration(audio_clip, actual_duration - lead_seconds),
        ])
    elif scene.pause_style and audio_clip is None:
        # リピート・回答の間（音声なし）: 合図の音だけ
        fitted_audio = AudioClip(
            pause_cue.beep_frame_function(scene, actual_duration), duration=actual_duration, fps=AUDIO_SAMPLE_RATE
        )
    else:
        fitted_audio = _fit_audio_to_duration(audio_clip, actual_duration)
    if scene.pause_style:
        video_clip = pause_cue.with_pause_cue(video_clip.with_duration(actual_duration), scene, resolution, font_path)
    video_clip = video_clip.with_audio(fitted_audio)

    return video_clip.with_duration(actual_duration)


BGM_EDGE_FADE_SECONDS = 1.5   # 動画の頭・お尻のフェードイン/アウトの長さ（上限）
BGM_CROSSFADE_SECONDS = 1.2   # BGMが切り替わる箇所のクロスフェードの長さ（上限）


@dataclass
class BgmSegment:
    """同じBGMが連続して流れる区間（動画全体の時間軸上の秒数）。"""
    path: str
    start: float
    end: float


def plan_bgm_segments(project: Project, scene_durations: list[float]) -> list[BgmSegment]:
    """各シーンのBGM（Project.resolve_bgm_path）を求め、同じBGMが続くシーンを1区間にまとめる。

    BGMなしのシーンは区間に含めない（＝無音）。別の区間で同じ曲が再び使われた場合は、
    その区間の頭から曲をかけ直す。
    """
    segments: list[BgmSegment] = []
    t = 0.0
    for scene, duration in zip(project.scenes, scene_durations):
        path = project.resolve_bgm_path(scene)
        if path:
            if segments and segments[-1].path == path and abs(segments[-1].end - t) < 1e-6:
                segments[-1].end = t + duration
            else:
                segments.append(BgmSegment(path=path, start=t, end=t + duration))
        t += duration
    return segments


def _build_bgm_segment_clip(
    segment: BgmSegment,
    total_duration: float,
    volume: float,
    prev_adjacent: bool,
    next_adjacent: bool,
    warnings: list[str],
    extra_clips: list,
    warned_paths: set[str],
) -> Optional[AudioClip]:
    """1区間分のBGMクリップを作る（ループ/トリミング・音量・フェード・開始位置の設定）。

    - 曲が区間より短ければループし、長ければ区間の長さで切る。
    - 前後に別のBGMが隣接している境界では、区間を BGM_CROSSFADE_SECONDS の半分ずつ
      前後に延ばしたうえでフェードを掛け、前の曲と重ねてクロスフェードさせる。
    - 動画の頭・お尻、および無音（BGMなし）の区間との境界では通常のフェードイン/アウトにする。
    - ファイルが見つからない/壊れている場合は、アプリを落とさず警告を追加して None を返す
      （同じファイルの警告は1回だけ）。
    """
    seg_duration = segment.end - segment.start
    crossfade = min(BGM_CROSSFADE_SECONDS, seg_duration / 3)
    start = max(0.0, segment.start - crossfade / 2) if prev_adjacent else segment.start
    end = min(total_duration, segment.end + crossfade / 2) if next_adjacent else segment.end
    clip_duration = end - start
    if clip_duration <= 0:
        return None

    bgm_path = Path(segment.path)
    name = bgm_path.name
    if not bgm_path.exists():
        if segment.path not in warned_paths:
            warnings.append(f"BGMファイルが見つからなかったため、その区間はBGMなしで書き出しました（{name}）。")
            warned_paths.add(segment.path)
        return None

    try:
        source = AudioFileClip(str(bgm_path))
        extra_clips.append(source)
        if not source.duration:
            raise ValueError("長さを取得できませんでした")

        if source.duration < clip_duration:
            clip = source.with_effects([afx.AudioLoop(duration=clip_duration)])
        else:
            clip = source.subclipped(0, clip_duration)

        edge_fade = min(BGM_EDGE_FADE_SECONDS, clip_duration / 4)
        fade_in = crossfade if prev_adjacent else edge_fade
        fade_out = crossfade if next_adjacent else edge_fade
        effects = [afx.MultiplyVolume(max(0.0, min(1.0, volume)))]
        if fade_in > 0:
            effects.append(afx.AudioFadeIn(fade_in))
        if fade_out > 0:
            effects.append(afx.AudioFadeOut(fade_out))
        return clip.with_effects(effects).with_start(start)
    except Exception as e:  # noqa: BLE001 - BGMファイルの形式不正など様々な失敗要因を丁寧な警告に変換する
        if segment.path not in warned_paths:
            warnings.append(f"BGMの読み込みに失敗したため、その区間はBGMなしで書き出しました（{name}: {e}）。")
            warned_paths.add(segment.path)
        return None


def _build_bgm_clips(
    project: Project, scene_durations: list[float], warnings: list[str], extra_clips: list
) -> list[AudioClip]:
    """場面・シーンごとの設定に従って、動画全体のBGMクリップ（区間ごと）を作る。"""
    total_duration = sum(scene_durations)
    segments = plan_bgm_segments(project, scene_durations)
    warned_paths: set[str] = set()
    clips = []
    for i, segment in enumerate(segments):
        prev_adjacent = i > 0 and abs(segments[i - 1].end - segment.start) < 1e-6
        next_adjacent = i + 1 < len(segments) and abs(segments[i + 1].start - segment.end) < 1e-6
        clip = _build_bgm_segment_clip(
            segment, total_duration, project.bgm_volume, prev_adjacent, next_adjacent,
            warnings, extra_clips, warned_paths,
        )
        if clip is not None:
            clips.append(clip)
    return clips


SE_FADE_OUT_SECONDS = 0.3  # シーンより長い効果音を、シーンの終わりで切るときのフェードアウトの長さ


def _build_se_clips(
    project: Project, scene_durations: list[float], warnings: list[str], extra_clips: list
) -> list[AudioClip]:
    """各シーンに設定された効果音を、そのシーンの開始位置に配置したクリップのリストを作る。

    効果音がシーンより長い場合（学校のチャイムなど）は、そのシーンの終わりで切る（次のシーンまで鳴り続けて
    会話にかぶらないように）。切る直前は SE_FADE_OUT_SECONDS かけて音を小さくし、ブツッと切れないようにする。
    ファイルが見つからない/壊れている場合は、そのシーンだけ効果音なしにして警告する（同じファイルは1回だけ）。
    """
    volume = max(0.0, min(1.0, project.se_volume))
    warned: set[str] = set()
    clips: list[AudioClip] = []
    start = 0.0
    for scene, duration in zip(project.scenes, scene_durations):
        path = scene.se_path
        if path:
            name = Path(path).name
            try:
                if not Path(path).exists():
                    raise FileNotFoundError("ファイルが見つかりません")
                source = AudioFileClip(str(path))
                extra_clips.append(source)
                effects = [afx.MultiplyVolume(volume)]
                clip = source
                if source.duration and source.duration > duration:
                    clip = source.subclipped(0, max(0.01, duration))
                    effects.append(afx.AudioFadeOut(min(SE_FADE_OUT_SECONDS, duration / 2)))
                clips.append(clip.with_effects(effects).with_start(start))
            except Exception as e:  # noqa: BLE001 - 効果音の失敗で動画全体の書き出しは止めない
                if path not in warned:
                    warnings.append(f"効果音「{name}」を読み込めなかったため、鳴らさずに書き出しました（{e}）。")
                    warned.add(path)
        start += duration
    return clips


def _build_scene_cut_ffmpeg_params(scene_clips: list) -> list[str]:
    """シーンの切り替わり（カット点）で、エンコード起因の一瞬の暗転・ノイズを防ぐための
    ffmpeg追加パラメータを組み立てる。

    シーンごとに背景・立ち絵ががらっと変わる「ハードカット」を、Bフレーム（前後のフレームを
    参照して圧縮するフレーム）をまたいでエンコードすると、一部の再生環境でカットの瞬間だけ
    コマが黒っぽく潰れて見える（画面が一瞬暗転する）ことがある。これを避けるため、
    各シーンの切り替わり位置に強制的にキーフレーム（そのフレーム単体で完結する、前後を
    参照しないフレーム）を挿入し（`-force_key_frames`）、さらにBフレーム自体を無効化する
    （`-bf 0`）ことで、カットの前後で参照関係が交差しないようにしている。
    """
    boundary_times: list[float] = [0.0]
    cumulative = 0.0
    for clip in scene_clips[:-1]:
        cumulative += clip.duration or 0.0
        boundary_times.append(cumulative)

    force_key_frames = ",".join(f"{t:.3f}" for t in boundary_times)
    return ["-force_key_frames", force_key_frames, "-bf", "0"]


def build_video(
    project: Project,
    output_filename: str = "output.mp4",
    fps: int = DEFAULT_FPS,
    progress_callback: Optional[ProgressCallback] = None,
    speed_preset: str = DEFAULT_SPEED_PRESET,
) -> BuildResult:
    """プロジェクトから最終的な動画ファイル（MP4）を生成する。

    Args:
        speed_preset: 書き出し速度の設定。"fast"(高速優先) / "balanced"(バランス・既定) /
            "quality"(高画質優先) のいずれか。ENCODE_PRESETSに無い値を渡した場合は
            DEFAULT_SPEED_PRESET にフォールバックする（エラーにはしない）。

    Raises:
        VoicevoxConnectionError: VOICEVOXが起動していない場合。
            呼び出し側(UI)でこの例外を捕まえ、
            「VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。」
            と表示すること。
        VideoBuildError: それ以外の理由で動画生成に失敗した場合。
    """
    progress = progress_callback or _noop

    if not project.scenes:
        raise VideoBuildError("シーンが1つもありません。シーンを追加してから生成してください。")

    # シーンにテキストが1つでもあればVOICEVOXが必要 → 事前に起動確認して早期に失敗させる
    needs_voicevox = any(
        scene.text and scene.text.strip() and not scene.silent
        and not (scene.voice_path and Path(scene.voice_path).exists())
        for scene in project.scenes
    )
    if needs_voicevox:
        progress("VOICEVOXへの接続を確認中…")
        voicevox_client.ensure_engine_running()

    # 書き出しごとに別の作業フォルダを使う（ブラウザの複数のタブで同時に書き出しても、
    # 先に終わった書き出しが、ほかの書き出しの途中の音声ファイルを消してしまわないように）
    work_dir = TMP_AUDIO_DIR / uuid.uuid4().hex[:12]
    work_dir.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    font_path = telop.resolve_font_path()
    warnings: list[str] = []
    if font_path is None:
        warnings.append(
            "日本語フォント(assets/fonts/NotoSansJP-Regular.otf)が見つからなかったため、"
            "テロップが正しく表示されない可能性があります。"
        )

    # PR/広告表記バッジは動画全体で常に同じ見た目のため、シーンごとに作り直さず1回だけ描画して使い回す
    pr_label_overlay = (
        render_pr_label_overlay(project.resolution, project.pr_label_text)
        if project.pr_label_enabled
        else None
    )

    # 画面左上の目次ラベル（説明文の目次と同じ見出し）。PR表記（右上）と同じ固定の重ね画像にまとめ、見出しごとに1回だけ描画する
    chapter_labels = video_metadata.scene_chapter_labels(project)
    fixed_overlays: dict[str, Optional[Image.Image]] = {}

    def fixed_overlay_for(chapter: str) -> Optional[Image.Image]:
        if not chapter:
            return pr_label_overlay
        if chapter not in fixed_overlays:
            label_img = telop.render_chapter_label(chapter, project.resolution, font_path)
            fixed_overlays[chapter] = (
                Image.alpha_composite(pr_label_overlay, label_img) if pr_label_overlay is not None else label_img
            )
        return fixed_overlays[chapter]

    scene_clips = []
    extra_clips: list = []  # 背景動画クリップなど、scene_clips自身の.close()では閉じられない付随リソース
    try:
        motion_contexts = motion.build_contexts(project.scenes)
        for i, scene in enumerate(project.scenes):
            clip = _build_scene_clip(
                scene, i, project.resolution, fps, font_path, warnings, progress, extra_clips,
                common_background_path=project.common_background_path,
                pr_label_overlay=fixed_overlay_for(chapter_labels[i]),
                reading_dict=project.reading_dict,
                background_override=book_script.effective_background_path(project, scene),
                project=project,
                motion_context=motion_contexts[i],
                audio_dir=work_dir,
            )
            scene_clips.append(clip)

        progress("シーンを結合しています…")
        # 字幕・見出し・PR表記は各シーンのフレームに直接焼き込んでいて、透過画像を重ねた
        # CompositeVideoClip は使っていない（全シーン同じ解像度・マスク無し）ため、高速な"chain"方式で連結する。
        # 以前は字幕をCompositeVideoClipで重ねており、"chain"だと前のシーンの字幕が居座る不具合を避けるため
        # 重い"compose"方式を使っていたが、動きのあるシーンで1フレームの合成が数倍遅くなるためやめた。
        final_video = concatenate_videoclips(scene_clips, method="chain")

        scene_durations = [clip.duration or 0.0 for clip in scene_clips]
        extra_audio: list[AudioClip] = []
        if plan_bgm_segments(project, scene_durations):
            progress("BGMを合成中…")
            extra_audio += _build_bgm_clips(project, scene_durations, warnings, extra_clips)
        if any(scene.se_path for scene in project.scenes):
            progress("効果音を合成中…")
            extra_audio += _build_se_clips(project, scene_durations, warnings, extra_clips)
        if extra_audio:
            voice_audio = final_video.audio
            layers = ([voice_audio] if voice_audio is not None else []) + extra_audio
            final_video = final_video.with_audio(
                CompositeAudioClip(layers).with_duration(final_video.duration)
            )

        output_path = OUTPUT_DIR / output_filename
        preset = ENCODE_PRESETS.get(speed_preset, ENCODE_PRESETS[DEFAULT_SPEED_PRESET])
        progress("MP4として書き出しています…（動画の長さによっては数分かかることがあります）")
        final_video.write_videofile(
            str(output_path),
            fps=fps,
            codec="libx264",
            preset=preset,
            threads=os.cpu_count() or 4,
            audio_codec="aac",
            audio_fps=AUDIO_SAMPLE_RATE,
            temp_audiofile=str(work_dir / f"temp-audio-{uuid.uuid4().hex[:8]}.m4a"),
            remove_temp=True,
            logger=None,
            ffmpeg_params=_build_scene_cut_ffmpeg_params(scene_clips),
        )
    except (VoicevoxConnectionError, VideoBuildError):
        raise
    except Exception as e:  # noqa: BLE001 - MoviePy/ffmpeg起因の様々な例外を丁寧なメッセージに変換する
        raise VideoBuildError(
            f"動画の書き出し中に予期しないエラーが発生しました: {e}"
        ) from e
    finally:
        for clip in scene_clips + extra_clips:
            try:
                clip.close()
            except Exception:  # noqa: BLE001 - 後片付けの失敗で本処理を止めない
                pass
        # 一時音声ファイルを掃除する（失敗しても致命的ではないため無視する）
        shutil.rmtree(work_dir, ignore_errors=True)

    return BuildResult(output_path=output_path, warnings=warnings)
