"""
音声波形（音量/RMS）を解析し、フレームごとの口の開閉（リップシンク）を判定する。

標準ライブラリの `wave` と `numpy` のみで完結させ、追加の音声ライブラリ（soundfile等）
への依存を増やさないようにしている（非エンジニアへの配布時のインストール負荷を下げるため）。
"""
from __future__ import annotations

import wave
from pathlib import Path
from typing import Optional

import numpy as np

# 音量のしきい値（0.0〜1.0に正規化したRMS値と比較する）。
# 声が小さいキャラクターの場合は下げる、環境音が多い音声の場合は上げる、など調整可能。
DEFAULT_VOLUME_THRESHOLD = 0.02


class LipsyncAnalysisError(Exception):
    """音声ファイルの読み込み・解析に失敗した場合の例外。"""


def analyze_mouth_frames(
    audio_path: Optional[Path],
    duration_sec: float,
    fps: int = 30,
    volume_threshold: float = DEFAULT_VOLUME_THRESHOLD,
) -> list[bool]:
    """フレームごとの口の開閉状態のリストを返す。

    Args:
        audio_path: 解析対象のwavファイル。None または無音扱いの場合は全フレーム「口を閉じる」を返す。
        duration_sec: 動画側で確定させたい表示秒数（Sceneの表示秒数が絶対）。
                      フレーム数は必ず round(duration_sec * fps) 件になる。
        fps: フレームレート。
        volume_threshold: この値を超える区間のRMS音量を「口を開ける」と判定する。

    Returns:
        list[bool]: 長さ = round(duration_sec * fps)。True = mouth_open, False = mouth_close。
                    音声の解析に失敗した場合も例外を投げず、全フレーム False（口を閉じた状態）
                    にフォールバックする（動画生成全体を止めないため）。
    """
    total_frames = max(1, round(duration_sec * fps))

    if audio_path is None:
        return [False] * total_frames

    audio_path = Path(audio_path)
    if not audio_path.exists():
        return [False] * total_frames

    try:
        samples, framerate = _read_wav_as_float(audio_path)
    except LipsyncAnalysisError:
        # 解析に失敗しても動画生成自体は継続する（口パクなしで書き出す）
        return [False] * total_frames

    if samples.size == 0 or framerate <= 0:
        return [False] * total_frames

    samples_per_frame = max(1, round(framerate / fps))
    mouth_flags: list[bool] = []
    for i in range(total_frames):
        start = i * samples_per_frame
        end = start + samples_per_frame
        chunk = samples[start:end]
        if chunk.size == 0:
            # 音声の長さを超えたフレーム（表示秒数の方が音声より長いケース）は無音として口を閉じる
            mouth_flags.append(False)
            continue
        rms = float(np.sqrt(np.mean(np.square(chunk))))
        mouth_flags.append(rms > volume_threshold)

    return mouth_flags


def _read_wav_as_float(audio_path: Path) -> tuple[np.ndarray, int]:
    """wavファイルを読み込み、-1.0〜1.0に正規化したfloat32のモノラル配列とサンプルレートを返す。"""
    try:
        with wave.open(str(audio_path), "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except (wave.Error, EOFError, FileNotFoundError, OSError) as e:
        raise LipsyncAnalysisError(f"音声ファイルの読み込みに失敗しました: {audio_path}") from e

    if not raw:
        return np.array([], dtype=np.float32), framerate

    if sample_width == 1:
        # 8bit wav は unsigned (0-255, 中心128)
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif sample_width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise LipsyncAnalysisError(
            f"未対応の音声フォーマットです (sample_width={sample_width} bytes): {audio_path}"
        )

    if n_channels > 1:
        usable_len = (len(data) // n_channels) * n_channels
        data = data[:usable_len].reshape(-1, n_channels).mean(axis=1)

    return data, framerate
