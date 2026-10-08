"""
英会話モードのお手本の英文（ネイティブ音声）を、手元のPCで動く読み上げAI「Kokoro」で自動生成する。

Kokoro（hexgrad/Kokoro-82M）は Apache 2.0 ライセンスの読み上げモデルで、登録・APIキー・費用なしで使え、
商用利用（YouTubeの収益化を含む）もできる。初回だけモデル（約330MB）と英語の辞書データが自動でダウンロードされる。
パッケージ: pip install kokoro soundfile（発音の変換に使う espeak-ng は espeakng-loader で一緒に入る）

生成した音声は english_lesson.AUDIO_DIR に「<音声ID>.wav」で保存するので、今までの手作業の音声と同じように
link_native_audio() でシーンに紐付く。すでに音声がある英文は、上書きしない限り作り直さない。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from src.services import english_lesson

SAMPLE_RATE = 24000
EDGE_PADDING_SECONDS = 0.08    # 音声の前後に足す無音（話し始め・終わりが切れて聞こえないように）
DEFAULT_SPEED = 0.9            # 学習用に、少しゆっくりめ（1.0 = 標準）

# 英語の声（a = アメリカ英語、b = イギリス英語 / f = 女性、m = 男性）
VOICES: dict[str, str] = {
    "af_heart": "Heart（女性・米）",
    "af_bella": "Bella（女性・米）",
    "af_nicole": "Nicole（女性・米・ささやき気味）",
    "af_sarah": "Sarah（女性・米）",
    "af_sky": "Sky（女性・米）",
    "am_michael": "Michael（男性・米）",
    "am_adam": "Adam（男性・米）",
    "am_fenrir": "Fenrir（男性・米）",
    "am_puck": "Puck（男性・米）",
    "bf_emma": "Emma（女性・英）",
    "bf_isabella": "Isabella（女性・英）",
    "bm_george": "George（男性・英）",
    "bm_lewis": "Lewis（男性・英）",
}
DEFAULT_VOICE_A = "af_heart"    # 声A（めたん役）
DEFAULT_VOICE_B = "am_michael"  # 声B（会話の相手役）

ProgressCallback = Callable[[str], None]

_pipelines: dict[str, object] = {}
_lock = threading.Lock()  # 読み上げAIは同時に1つだけ使う（ブラウザの複数のタブから同時に呼ばれても安全に）


class EnglishTTSError(Exception):
    """音声の自動生成に失敗した（利用者向けのメッセージ付き）。"""


def is_available() -> bool:
    """読み上げAI（Kokoro）が入っているか。読み込むと数十秒かかるので、入っているかだけを調べる（読み込まない）。"""
    import importlib.util

    return importlib.util.find_spec("kokoro") is not None


def _pipeline(voice: str):
    """声の地域（米/英）ごとの読み上げの準備（初回は数十秒かかるので、作ったものを使い回す）。"""
    lang = "b" if voice.startswith("b") else "a"
    if lang not in _pipelines:
        try:
            from kokoro import KPipeline
        except ImportError as e:
            raise EnglishTTSError(
                "読み上げAI（Kokoro）が入っていません。コマンドで「pip install kokoro soundfile」を実行してから、"
                "アプリを再起動してください。"
            ) from e
        _pipelines[lang] = KPipeline(lang_code=lang, repo_id="hexgrad/Kokoro-82M")
    return _pipelines[lang]


def synthesize(text: str, voice: str = DEFAULT_VOICE_A, speed: float = DEFAULT_SPEED) -> np.ndarray:
    """英文を読み上げた音声（24kHz・モノラル・float32）を返す。"""
    try:
        with _lock:
            chunks = [np.asarray(audio, dtype=np.float32)
                      for _, _, audio in _pipeline(voice)(text, voice=voice, speed=speed) if audio is not None]
    except EnglishTTSError:
        raise
    except Exception as e:  # noqa: BLE001 - モデルのダウンロード失敗など、要因が多岐にわたるため利用者向けの文言にする
        raise EnglishTTSError(f"「{text}」の音声を作れませんでした（{e}）") from e
    if not chunks:
        raise EnglishTTSError(f"「{text}」の音声を作れませんでした（読み上げる文字がありません）")
    pad = np.zeros(int(SAMPLE_RATE * EDGE_PADDING_SECONDS), dtype=np.float32)
    return np.concatenate([pad, *chunks, pad])


@dataclass
class GenerateResult:
    created: list[str] = field(default_factory=list)   # 作った音声のID
    failed: list[str] = field(default_factory=list)    # 作れなかった英文（エラーの説明付き）


def generate_native_audio(
    items: list[dict], voice_a: str = DEFAULT_VOICE_A, voice_b: str = DEFAULT_VOICE_B,
    speed: float = DEFAULT_SPEED, overwrite: bool = False, progress: Optional[ProgressCallback] = None,
) -> GenerateResult:
    """english_lesson.native_audio_items() の一覧のうち、まだ音声が無い英文（overwrite=True なら全部）を読み上げて保存する。

    声A の英文は voice_a、声B（会話の相手役）の英文は voice_b で読む。ファイル名は「<音声ID>.wav」。
    上書きするときは、同じIDの別の形式の音声（手作業で置いた .mp3 など）を消してから保存する
    （同じIDの音声が2つあると、どちらが使われるか分かりにくいため）。
    """
    import soundfile as sf

    progress = progress or (lambda _m: None)
    result = GenerateResult()
    targets = [i for i in items if overwrite or not i["ready"]]
    english_lesson.AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    manifest = english_lesson.load_manifest()
    for n, item in enumerate(targets, start=1):
        voice = voice_b if str(item.get("voice", "A")).upper() == "B" else voice_a
        progress(f"🗣 {n}/{len(targets)}（{VOICES.get(voice, voice)}）: {item['text']}")
        try:
            audio = synthesize(item["text"], voice, speed)
        except EnglishTTSError as e:
            result.failed.append(str(e))
            continue
        if overwrite:
            for ext in english_lesson.AUDIO_EXTENSIONS:
                old = english_lesson.AUDIO_DIR / f"{item['id']}{ext}"
                if old.exists():
                    old.unlink()
        sf.write(english_lesson.AUDIO_DIR / f"{item['id']}.wav", audio, SAMPLE_RATE)
        manifest.setdefault(item["id"], {"text": item["text"], "voice": item.get("voice", "A")})
        manifest[item["id"]]["tts"] = {"engine": "kokoro", "voice": voice, "speed": speed}
        result.created.append(item["id"])
    english_lesson._save_manifest(manifest)
    return result


def sample_audio(voice: str, speed: float = DEFAULT_SPEED, text: str = "Hi, I'm Metan. Nice to meet you!") -> bytes:
    """声の試し聞き用の WAV のバイト列。"""
    import io

    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, synthesize(text, voice, speed), SAMPLE_RATE, format="WAV")
    return buf.getvalue()
