"""
背景として動画ファイルを使うためのユーティリティ。

画像背景と違い、動画背景は再生時刻によって絵そのものが変わるため、
compositor.py の「口:開」「口:閉」2枚を使い回す軽量化はそのままでは使えない。
そのため video_builder.py 側では、背景動画のその時刻のコマを都度取得し、
立ち絵オーバーレイ（透過PNG）を重ねてフレームを合成する（詳細は video_builder.py 参照）。

このモジュールは「背景動画の読み込み・ループ／トリミング」と「プレビュー用の代表フレーム抽出」
のみを担当し、キャラクターとの合成は行わない。

なお、MoviePy(moviepy)のimportは各関数内で遅延して行っている。compositor.pyがこのモジュールを
importするため、モジュールの先頭でmoviepyをimportしてしまうと、動画背景を1つも使わない場合
（Pillowだけで完結する軽量なプレビュー合成のみを行いたい場合）でもmoviepyのインストールが
必須になってしまうのを避けるため。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np


def build_looping_background_clip(path: str, target_duration: float):
    """背景動画を読み込み、target_duration に合わせてループ／トリミングして返す。

    - 動画の音声は無視する（VOICEVOXのナレーション・BGMと混ざって邪魔にならないよう常にミュート）。
    - 動画がtarget_durationより短い場合は自動でループさせ、長い場合は先頭からtarget_durationぶんだけ使う。

    読み込み・デコードに失敗した場合（壊れたファイル・非対応コーデック等）は、
    そのままの例外を送出する。呼び出し側(video_builder.py)で捕まえて、
    アプリを落とさず警告に変換したうえで単色背景にフォールバックすること。
    """
    from moviepy import VideoFileClip, vfx

    clip = VideoFileClip(str(path)).without_audio()
    if not clip.duration or clip.duration <= 0:
        raise ValueError("背景動画の長さを取得できませんでした。ファイルが壊れている可能性があります。")

    if clip.duration < target_duration:
        clip = clip.with_effects([vfx.Loop(duration=target_duration)])
    else:
        clip = clip.subclipped(0, target_duration)

    return clip


@lru_cache(maxsize=8)
def _extract_preview_frame_cached(path: str, mtime: float) -> np.ndarray:
    """(path, 最終更新時刻)をキーにキャッシュする、プレビュー用フレーム抽出の実処理。

    Streamlitは操作のたびにスクリプト全体を再実行するため、同じ動画のプレビューを
    毎回デコードし直さないようにキャッシュしている（ファイルが更新されればmtimeが
    変わるため、古いフレームが表示され続けることはない）。
    """
    from moviepy import VideoFileClip

    clip = VideoFileClip(path)
    try:
        # 先頭ぴったり(t=0)だと真っ黒なコマのことがあるため、少しだけ進んだ位置を代表フレームにする
        t = min(0.1, (clip.duration or 0.2) / 2)
        return clip.get_frame(t)
    finally:
        clip.close()


def extract_preview_frame(path: str) -> np.ndarray:
    """プレビュー表示用に、背景動画の代表的な1コマ(RGBのnumpy配列)を取得する。"""
    resolved = Path(path)
    mtime = resolved.stat().st_mtime
    return _extract_preview_frame_cached(str(resolved), mtime)
