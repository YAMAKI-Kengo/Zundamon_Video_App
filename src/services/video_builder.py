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
全シーンを結合したうえで、プロジェクトにBGM(project.bgm_path)が設定されていれば、
動画全体の長さに合わせてループ/トリミングし、指定音量(project.bgm_volume)・
フェードイン/アウト付きでナレーション音声にミックスしてから1本のMP4として書き出す。
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image
from moviepy import (
    AudioClip,
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    ImageSequenceClip,
    VideoClip,
    afx,
    concatenate_audioclips,
    concatenate_videoclips,
)

from src.models import Project, Scene
from src.services import background_video, lipsync, telop, voicevox_client
from src.services.compositor import (
    BACKGROUND_FALLBACK_COLOR,
    compose_dual_character_overlay,
    compose_dual_scene_frame,
    cover_resize,
    load_background_image,
    load_before_after_images,
    load_content_media_image,
    paste_before_after,
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


def _build_telop_clip(
    text: str,
    resolution: tuple[int, int],
    duration: float,
    font_path: Optional[str],
    color: str = "white",
    stroke_color: str = "black",
):
    """読み上げテキストを、半透明の背景ボックス付きのテロップとして表示するクリップを作成する。

    color/stroke_color は話者ごとに変える（zundamon=緑地に白フチ、shikoku_metan=ピンク地に黒フチ、
    が既定。config/characters.json の "telop" で変更可能）。

    折り返しは src.services.telop で自前実装したものを使う（Pillowでフォント幅を実測して
    折り返すため、日本語のようにスペースを含まない文章でも画面幅に収まる／ユーザーが
    読み上げテキスト内で入力した改行（Enter）もそのまま尊重される）。
    """
    telop_image = telop.render_telop_image(
        text,
        resolution,
        font_path,
        color=color,
        stroke_color=stroke_color,
    )
    return ImageClip(np.array(telop_image), transparent=True).with_duration(duration)


def _resolve_dynamic_background(
    path: Optional[str],
    target_duration: float,
    resolution: tuple[int, int],
    warnings: list[str],
    label: str,
    extra_clips: list,
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
    return None, load_background_image(path, resolution)


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
):
    """背景・資料メディアの少なくとも一方が動画ファイルの場合の映像クリップを構築する。

    背景・資料メディアのいずれの動画も、シーンの表示秒数(actual_duration)に合わせて
    自動でループ／トリミングされ、音声は常にミュートする（VOICEVOXのナレーション・BGMと
    混ざらないように）。読み込みに失敗した場合（壊れたファイル・非対応コーデック等）でも
    アプリを落とさず、警告を追加したうえで処理を継続する。

    合成順序は「背景 → 資料メディア/ビフォーアフター（あれば） → 立ち絵オーバーレイ →
    PR表記バッジ（あれば）」の順で、compose_dual_scene_frame()（画像のみの高速パス）と
    同じ重なり順になるようにしてある。ビフォーアフターは静止画のみ対応のため、
    背景・資料メディアが動画のこのパスでも扱いは変わらない（毎フレーム同じ画像を貼るだけ）。
    """
    bg_clip, bg_static = _resolve_dynamic_background(
        effective_background_path, actual_duration, resolution, warnings, label, extra_clips
    )
    before_after = load_before_after_images(scene.before_image_path, scene.after_image_path)
    if before_after is not None:
        media_clip, media_static = None, None
    else:
        media_clip, media_static = _resolve_dynamic_content_media(
            scene.content_media_path, actual_duration, warnings, label, extra_clips
        )

    try:
        overlay_closed = np.array(compose_dual_character_overlay(scene.speaker, scene.expression, False, resolution))
        overlay_open = np.array(compose_dual_character_overlay(scene.speaker, scene.expression, True, resolution))
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
                canvas = cover_resize(Image.fromarray(bg_frame).convert("RGBA"), resolution)
            except Exception:  # noqa: BLE001 - 背景動画の一部フレーム取得に失敗しても動画全体の書き出しは継続する
                canvas = fallback_bg_rgba.copy()
        else:
            canvas = (bg_static or fallback_bg_rgba).copy()

        # 資料メディア/ビフォーアフターレイヤー（背景の上・立ち絵の下。未指定なら何もしない）
        if before_after is not None:
            paste_before_after(canvas, before_after[0], before_after[1], resolution)
        elif media_clip is not None:
            try:
                media_frame = media_clip.get_frame(t)
                media_img = Image.fromarray(media_frame).convert("RGBA")
                paste_content_media(canvas, media_img, resolution)
            except Exception:  # noqa: BLE001 - 資料動画の一部フレーム取得に失敗しても、その1コマだけ資料なしで継続する
                pass
        elif media_static is not None:
            paste_content_media(canvas, media_static, resolution)

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
):
    """1シーン分の(音声付き)動画クリップを構築する。

    common_background_path はプロジェクト全体の共通背景。シーン自身に背景が
    設定されていればそちらを優先し、未設定の場合のみ共通背景を使う。
    """
    label = f"シーン{scene_index + 1}"
    effective_background_path = scene.background_path or common_background_path

    # 1. 音声合成（指定秒数に収まるよう、必要なら話速を自動調整する）
    progress(f"{label}: VOICEVOXで音声合成中…")
    audio_path = None
    try:
        result = voicevox_client.synthesize_voice(
            scene.text,
            scene.speaker,
            output_path=TMP_AUDIO_DIR / f"scene{scene_index}_{uuid.uuid4().hex[:8]}.wav",
            target_duration=scene.duration,
            reading_dict=reading_dict,
        )
        audio_path = result.audio_path
        if abs(result.speed_scale - 1.0) > 0.01:
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

    # 2. リップシンク解析（表示秒数を基準にフレーム数を確定させる）
    progress(f"{label}: リップシンクを解析中…")
    mouth_flags = lipsync.analyze_mouth_frames(audio_path, scene.duration, fps=fps)
    total_frames = len(mouth_flags)
    actual_duration = total_frames / fps  # フレーム数から逆算した確定尺（音声/字幕もこれに合わせる）

    # 3. 立ち絵+背景+資料メディアの合成（2人常時表示）
    progress(f"{label}: 立ち絵を合成中…")
    needs_dynamic = is_video_path(effective_background_path) or is_video_path(scene.content_media_path)
    if needs_dynamic:
        # 背景・資料メディアの少なくとも一方が動画ファイルの場合、絵そのものがフレームごとに
        # 変わるため「口:開」「口:閉」を使い回す最適化はできず、1フレームずつ合成する
        video_clip = _build_dynamic_scene_clip(
            scene, effective_background_path, resolution, fps, actual_duration, mouth_flags, warnings, label,
            extra_clips, pr_label_overlay=pr_label_overlay,
        )
    else:
        # 背景・資料メディアがどちらも画像（または未指定）の場合、話者だけ「口:開」「口:閉」の
        # 2パターンを作り、使い回す（1フレームずつ再合成しない軽量化）
        try:
            frame_closed = np.array(
                compose_dual_scene_frame(
                    scene.speaker, scene.expression, False, effective_background_path, resolution,
                    content_media_path=scene.content_media_path,
                    before_image_path=scene.before_image_path,
                    after_image_path=scene.after_image_path,
                    pr_label_overlay=pr_label_overlay,
                ).convert("RGB")
            )
            frame_open = np.array(
                compose_dual_scene_frame(
                    scene.speaker, scene.expression, True, effective_background_path, resolution,
                    content_media_path=scene.content_media_path,
                    before_image_path=scene.before_image_path,
                    after_image_path=scene.after_image_path,
                    pr_label_overlay=pr_label_overlay,
                ).convert("RGB")
            )
        except Exception as e:  # noqa: BLE001 - 画像合成の失敗要因は多岐にわたるため広く捕捉して警告に変換する
            raise VideoBuildError(f"{label}: 立ち絵/背景の合成に失敗しました（{e}）") from e

        frame_sequence = [frame_open if flag else frame_closed for flag in mouth_flags]
        video_clip = ImageSequenceClip(frame_sequence, fps=fps)

    # 4. 音声をシーンの確定尺に合わせる（長ければトリミング、短ければ無音で延長）
    audio_clip = AudioFileClip(str(audio_path)) if audio_path else None
    fitted_audio = _fit_audio_to_duration(audio_clip, actual_duration)
    video_clip = video_clip.with_audio(fitted_audio)

    # 5. テロップ（読み上げテキストをそのまま表示。文字色は話者ごとに変える）
    if scene.text and scene.text.strip():
        progress(f"{label}: テロップを合成中…")
        try:
            telop_style = get_telop_style(scene.speaker)
            telop_clip = _build_telop_clip(
                scene.text,
                resolution,
                actual_duration,
                font_path,
                color=telop_style["color"],
                stroke_color=telop_style["stroke_color"],
            )
            video_clip = CompositeVideoClip([video_clip, telop_clip], size=resolution).with_duration(actual_duration)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{label}: テロップの描画に失敗したため、テロップなしで書き出しました（{e}）")

    return video_clip.with_duration(actual_duration)


def _load_bgm_clip(project: Project, total_duration: float, warnings: list[str]) -> Optional[AudioClip]:
    """プロジェクトに設定されたBGMを、動画全体の長さ(total_duration)に合わせて読み込む。

    - BGMが動画より短い場合はループさせて長さを合わせる。
    - BGMが動画より長い場合は先頭からtotal_durationぶんだけ使う。
    - 音量はproject.bgm_volumeで一律にスケールし、頭とお尻に短いフェードを入れる
      （急に鳴り始める/切れる違和感を減らすため）。
    - BGMファイルが未設定・見つからない・壊れている等の場合は、アプリを落とさず
      「BGMなしで書き出した」という警告を追加して処理を継続する。
    """
    if not project.bgm_path:
        return None
    if total_duration <= 0:
        return None

    bgm_path = Path(project.bgm_path)
    if not bgm_path.exists():
        warnings.append(f"BGMファイルが見つからなかったため、BGMなしで書き出しました（{bgm_path.name}）。")
        return None

    try:
        bgm_clip = AudioFileClip(str(bgm_path))
        if not bgm_clip.duration:
            warnings.append("BGMファイルの長さを取得できなかったため、BGMなしで書き出しました。")
            return None

        if bgm_clip.duration < total_duration:
            bgm_clip = bgm_clip.with_effects([afx.AudioLoop(duration=total_duration)])
        else:
            bgm_clip = bgm_clip.subclipped(0, total_duration)

        volume = max(0.0, min(1.0, project.bgm_volume))
        effects = [afx.MultiplyVolume(volume)]
        fade_duration = min(1.5, total_duration / 4)
        if fade_duration > 0:
            effects.append(afx.AudioFadeIn(fade_duration))
            effects.append(afx.AudioFadeOut(fade_duration))

        return bgm_clip.with_effects(effects)
    except Exception as e:  # noqa: BLE001 - BGMファイルの形式不正など様々な失敗要因を丁寧な警告に変換する
        warnings.append(f"BGMの読み込みに失敗したため、BGMなしで書き出しました（{e}）。")
        return None


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
    needs_voicevox = any(scene.text and scene.text.strip() for scene in project.scenes)
    if needs_voicevox:
        progress("VOICEVOXへの接続を確認中…")
        voicevox_client.ensure_engine_running()

    TMP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
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

    scene_clips = []
    extra_clips: list = []  # 背景動画クリップなど、scene_clips自身の.close()では閉じられない付随リソース
    try:
        for i, scene in enumerate(project.scenes):
            clip = _build_scene_clip(
                scene, i, project.resolution, fps, font_path, warnings, progress, extra_clips,
                common_background_path=project.common_background_path,
                pr_label_overlay=pr_label_overlay,
                reading_dict=project.reading_dict,
            )
            scene_clips.append(clip)

        progress("シーンを結合しています…")
        # method="chain"（既定）ではなく明示的に"compose"を指定している。各シーンのクリップは
        # 「立ち絵+背景の動画」に「テロップ(透過PNGのImageClip)」をCompositeVideoClipで重ねた
        # ネスト構造になっているが、透過画像を重ねたCompositeVideoClip同士を"chain"方式で連結すると、
        # moviepy側の合成処理の不具合（2.1.2で修正されたもの含む）により、テロップが正しく
        # 切り替わらず前のシーンの字幕が居座って見えることがある。"compose"方式は全シーンが
        # 同じ解像度であれば見た目は"chain"と変わらず、この問題を避けられる。
        final_video = concatenate_videoclips(scene_clips, method="compose")

        if project.bgm_path:
            progress("BGMを合成中…")
            bgm_clip = _load_bgm_clip(project, final_video.duration, warnings)
            if bgm_clip is not None:
                voice_audio = final_video.audio
                mixed_audio = (
                    CompositeAudioClip([voice_audio, bgm_clip]) if voice_audio is not None else bgm_clip
                )
                final_video = final_video.with_audio(mixed_audio)

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
            temp_audiofile=str(TMP_AUDIO_DIR / f"temp-audio-{uuid.uuid4().hex[:8]}.m4a"),
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
        shutil.rmtree(TMP_AUDIO_DIR, ignore_errors=True)

    return BuildResult(output_path=output_path, warnings=warnings)
