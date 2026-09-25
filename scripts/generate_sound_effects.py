"""
効果音（SE）の素材を、外部サービス・ダウンロードなしで numpy だけで合成して assets/se/ に保存する。

書籍解説動画でよく使う定番の効果音を、動作確認・すぐ使える素材として用意するためのスクリプト。
より本格的な音を使いたい場合は、効果音ラボ等の利用規約を確認したうえで、好きな効果音ファイル
（mp3 / wav / m4a / ogg）を assets/se/ に置けば、同じようにシーンごとに選べるようになる。

実行方法:
    python scripts/generate_sound_effects.py
    （既存のファイルは上書きしない。作り直したい場合は --force を付ける）
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
SE_DIR = Path(__file__).resolve().parents[1] / "assets" / "se"


def _t(duration: float) -> np.ndarray:
    return np.arange(int(SAMPLE_RATE * duration)) / SAMPLE_RATE


def _env(t: np.ndarray, attack: float, decay: float) -> np.ndarray:
    """立ち上がり attack 秒・指数減衰（時定数 decay 秒）のエンベロープ。"""
    a = np.clip(t / max(attack, 1e-4), 0, 1)
    return a * np.exp(-np.maximum(t - attack, 0) / decay)


def _sweep(t: np.ndarray, f0: float, f1: float, duration: float) -> np.ndarray:
    """f0 → f1 へ指数的に周波数が変わる正弦波。"""
    k = np.log(f1 / f0) / duration
    phase = 2 * np.pi * f0 * (np.exp(k * t) - 1) / k
    return np.sin(phase)


def _tone(freq: float, duration: float, harmonics=(1.0, 0.3, 0.1), attack=0.005, decay=0.2) -> np.ndarray:
    t = _t(duration)
    wave_ = sum(amp * np.sin(2 * np.pi * freq * (i + 1) * t) for i, amp in enumerate(harmonics))
    return wave_ * _env(t, attack, decay)


def _bandpass_noise(duration: float, low: float, high: float, rng: np.random.Generator) -> np.ndarray:
    n = int(SAMPLE_RATE * duration)
    spectrum = np.fft.rfft(rng.normal(0, 1, n))
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    spectrum[(freqs < low) | (freqs > high)] = 0
    return np.fft.irfft(spectrum, n)


def _place(total: float, *parts: tuple[float, np.ndarray]) -> np.ndarray:
    """(開始秒, 波形) の組を、total 秒のバッファに足し合わせる。"""
    out = np.zeros(int(SAMPLE_RATE * total))
    for start, part in parts:
        i = int(SAMPLE_RATE * start)
        end = min(len(out), i + len(part))
        out[i:end] += part[: end - i]
    return out


def se_pop() -> np.ndarray:
    """ポン: 話題の切り替え・スライドの表示に。"""
    t = _t(0.18)
    return _sweep(t, 900, 420, 0.18) * _env(t, 0.002, 0.045)


def se_sparkle() -> np.ndarray:
    """キラーン: ひらめき・おすすめの紹介に。"""
    notes = [2093.0, 2637.0, 3136.0, 4186.0]
    parts = [(i * 0.05, _tone(f, 0.9, harmonics=(1.0, 0.15), attack=0.003, decay=0.25)) for i, f in enumerate(notes)]
    t = _t(1.05)
    shimmer = 1 + 0.25 * np.sin(2 * np.pi * 14 * t)
    return _place(1.05, *parts) * shimmer


def se_impact() -> np.ndarray:
    """ドン: 衝撃の事実・大事なポイントの強調に。"""
    t = _t(0.7)
    body = _sweep(t, 110, 42, 0.7) * _env(t, 0.003, 0.18)
    rng = np.random.default_rng(1)
    hit = _bandpass_noise(0.7, 40, 900, rng) * _env(t, 0.001, 0.03) * 0.6
    return body + hit


def se_question() -> np.ndarray:
    """はてな: 疑問・「どういうこと？」の場面に。"""
    t1, t2 = _t(0.12), _t(0.3)
    n1 = np.sin(2 * np.pi * 620 * t1) * _env(t1, 0.005, 0.06)
    n2 = _sweep(t2, 700, 1150, 0.3) * _env(t2, 0.005, 0.12)
    return _place(0.45, (0.0, n1 * 0.8), (0.13, n2))


def se_swish() -> np.ndarray:
    """シュッ: 場面転換・まとめへの切り替えに。"""
    rng = np.random.default_rng(2)
    duration = 0.35
    t = _t(duration)
    # 中心周波数が上がっていく帯域ノイズを、短い区間ごとに作ってつなげる
    out = np.zeros_like(t)
    seg = int(SAMPLE_RATE * 0.02)
    for i in range(0, len(t), seg):
        center = 800 + 5000 * (i / len(t))
        chunk = _bandpass_noise(0.02 + 0.005, center * 0.6, center * 1.4, rng)[: min(seg, len(t) - i)]
        out[i:i + len(chunk)] = chunk
    shape = np.sin(np.pi * np.clip(t / duration, 0, 1)) ** 2
    return out * shape


def se_shock() -> np.ndarray:
    """ガーン: ショック・落ち込み（悩みの場面）に。"""
    duration = 1.4
    t = _t(duration)
    out = np.zeros_like(t)
    for freq in (98.0, 116.5, 146.8, 196.0):  # 短調の和音（G・B♭・D・G）
        saw = sum(np.sin(2 * np.pi * freq * k * t) / k for k in range(1, 9))
        out += saw
    return out * _env(t, 0.004, 0.45) * 0.5


def se_jingle() -> np.ndarray:
    """チャンチャン: オチ・エンディングに。"""
    n1 = _tone(784.0, 0.25, harmonics=(1.0, 0.5, 0.25, 0.1), attack=0.004, decay=0.07)
    n2 = _tone(1046.5, 0.6, harmonics=(1.0, 0.5, 0.25, 0.1), attack=0.004, decay=0.16)
    return _place(0.85, (0.0, n1), (0.2, n2))


SOUND_EFFECTS = {
    "ポン": se_pop,
    "キラーン": se_sparkle,
    "ドン": se_impact,
    "はてな": se_question,
    "シュッ": se_swish,
    "ガーン": se_shock,
    "チャンチャン": se_jingle,
}


def save_wav(path: Path, samples: np.ndarray, peak: float = 0.8) -> None:
    samples = samples / (np.max(np.abs(samples)) or 1.0) * peak
    fade = min(len(samples), int(SAMPLE_RATE * 0.01))  # 末尾のプツッというノイズを防ぐ短いフェードアウト
    samples[-fade:] *= np.linspace(1, 0, fade)
    pcm = (samples * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def main(force: bool = False) -> list[Path]:
    SE_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, fn in SOUND_EFFECTS.items():
        path = SE_DIR / f"{name}.wav"
        if path.exists() and not force:
            continue
        save_wav(path, fn())
        written.append(path)
    return written


if __name__ == "__main__":
    for p in main(force="--force" in sys.argv):
        print(f"作成: {p.relative_to(SE_DIR.parents[1])}")
