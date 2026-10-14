"""
BGMの曲ライブラリ（気分ごとのフォルダ）と、シーンの雰囲気に合わせたBGMの自動選択・クレジット。

assets/bgm/ の下に、気分ごとのフォルダ（bright・calm・comedy・emotional・mysterious・serious・tense）で曲を置く。
台本の各セリフに「BGMの気分」（bgm。雰囲気が変わるセリフだけ書く）があれば、その気分のフォルダから曲を選び、
雰囲気が変わったところで曲を切り替える（クロスフェードは video_builder）。

- 曲の選び方: 同じ動画の中では、同じ気分には同じ曲を使う（ころころ変わらないように）。どの曲にするかは
  動画のタイトルから決まる（動画ごとに違う曲になりやすく、作り直しても同じ曲になる）。
  利用者は「気分ごとの曲」（Project.mood_bgm）やシーンごとの曲（Scene.bgm_path）で変えられる。
- 短すぎる区間: 雰囲気の区間が MIN_MOOD_SECONDS より短いときは、前の雰囲気のまま流す（曲がころころ変わると
  かえってうるさいため）。「無音」（none）だけは、ショックの一瞬を際立たせるため短くても効かせる。
- 曲の情報（曲名・作者・配布元・ライセンス表記）は assets/bgm/_library.json に1曲ずつ記録する。
  新しい曲を置くと、曲ファイルのタグ（ID3）から曲名・作者を読んで自動で追記する。分からない項目は空のままにし、
  推測で埋めない（説明文には「作者名未登録」と出し、利用者に記入を促す）。
- 音量: 曲によって録音の音量がばらばらなので、曲ごとに音の大きさを測って、同じくらいの大きさにそろえてから
  BGMの音量（Project.bgm_volume）を掛ける（大きな曲でセリフが聞き取りにくくならないように）。
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.models import NO_BGM, Project, Scene
from src.utils.asset_loader import _short_cache

ROOT = Path(__file__).resolve().parents[2]
BGM_DIR = ROOT / "assets" / "bgm"
LIBRARY_PATH = BGM_DIR / "_library.json"
LEGACY_CREDITS_PATH = ROOT / "config" / "bgm_credits.json"
LOUDNESS_CACHE_PATH = ROOT / "tmp" / "bgm_loudness.json"
BGM_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"}

# 気分（フォルダ名）→ 表示名。台本のAIにもこの説明で選ばせる
MOODS: dict[str, str] = {
    "calm": "日常・解説（落ち着いた普段の会話・説明）",
    "bright": "楽しい・明るい（ワクワク・うまくいっている・前向き）",
    "comedy": "コミカル（ボケ・ドタバタ・ずっこけ）",
    "tense": "緊張・ピンチ（焦り・危機・追い詰められる）",
    "serious": "シリアス（重大な事実・深刻な話・決意）",
    "emotional": "感動・悲しい（しんみり・落ち込み・感動の結末）",
    "mysterious": "謎・不思議（なぜ？・意外な真相を探る）",
}
SILENCE = "none"  # BGMを止める（ショックの瞬間など）
MOOD_CHOICES = list(MOODS) + [SILENCE]
MOOD_LABELS = {**{k: v.split("（")[0] for k, v in MOODS.items()}, SILENCE: "無音"}

MIN_MOOD_SECONDS = 8.0     # これより短い雰囲気の区間は、前の雰囲気のまま流す
MIN_SILENCE_SECONDS = 1.0  # 無音はこれ以上あれば効かせる

# 曲の音の大きさをそろえる（RMS。1.0 = 最大）。BGMの音量のスライダーは、この大きさに対して掛ける
TARGET_RMS = 0.12
GAIN_RANGE = (0.4, 2.5)
LOUDNESS_SAMPLE_SECONDS = 90  # 曲の頭からこの秒数で音の大きさを測る
LOUDNESS_FPS = 11025


# ---------------------------------------------------------------------------
# 曲の一覧と情報
# ---------------------------------------------------------------------------

@dataclass
class Track:
    path: Path
    mood: str          # フォルダ名（MOODS のキー。気分のフォルダの外の曲は ""）
    title: str
    artist: str = ""
    source: str = ""        # 配布元の名前
    source_url: str = ""    # 曲の配布ページ（分かる場合だけ）
    license: str = ""       # 必要なライセンス表記（分かる場合だけ）
    credit: str = ""        # 作曲者が指定しているクレジットの書き方（あれば、説明文にはこの文をそのまま載せる）
    conditions: str = ""    # 作曲者独自の利用条件（メモ。説明文には載せない）

    @property
    def key(self) -> str:
        return library_key(self.path)


def library_key(path: Path | str) -> str:
    """_library.json のキー（assets/bgm/ からの相対パス。区切りは /）。"""
    path = Path(path)
    try:
        return path.resolve().relative_to(BGM_DIR.resolve()).as_posix()
    except ValueError:
        return path.name


def list_tracks(mood: Optional[str] = None) -> list[Track]:
    """assets/bgm/ の曲（気分のフォルダの中も含む）。mood を指定すると、その気分のフォルダの曲だけ。"""
    return [t for t in _all_tracks() if mood is None or t.mood == mood]


@_short_cache(3.0)
def _all_tracks() -> list[Track]:
    mood = None
    if not BGM_DIR.exists():
        return []
    library = load_library()
    tracks = []
    for path in sorted(BGM_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in BGM_EXTS:
            continue
        rel = path.relative_to(BGM_DIR)
        folder = rel.parts[0] if len(rel.parts) > 1 else ""
        track_mood = folder if folder in MOODS else ""
        if mood is not None and track_mood != mood:
            continue
        info = library.get(rel.as_posix(), {})
        tracks.append(Track(
            path=path, mood=track_mood,
            title=str(info.get("title") or _title_from_filename(path)),
            artist=str(info.get("artist") or ""), source=str(info.get("source") or ""),
            source_url=str(info.get("source_url") or ""), license=str(info.get("license") or ""),
            credit=str(info.get("credit") or ""), conditions=str(info.get("conditions") or ""),
        ))
    return tracks


def track_for_path(path: str | Path) -> Track:
    key = library_key(path)
    for track in _all_tracks():
        if track.key == key:
            return track
    p = Path(path)
    return Track(path=p, mood="", title=_title_from_filename(p))


def _title_from_filename(path: Path) -> str:
    stem = path.stem
    # 昔の命名「場面_曲名」（例: 導入①_自宅にて）は、曲名の部分だけにする
    if "_" in stem and re.match(r"^(導入|解説|まとめ|エンディング|ダイアログ|リピート練習|瞬発トレーニング|総集編|1週間の振り返り)", stem):
        stem = stem.split("_", 1)[1]
    return stem.replace("_", " ")


def load_library() -> dict[str, dict]:
    try:
        data = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and not k.startswith("_")} if isinstance(data, dict) else {}


def save_library(library: dict[str, dict]) -> None:
    data = {"_説明": LIBRARY_NOTE, **{k: library[k] for k in sorted(library)}}
    LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


LIBRARY_NOTE = (
    "BGMの曲情報。キーは assets/bgm/ からのファイルの場所。動画の説明文のクレジットには、その動画で実際に使った曲だけを、"
    "ここの title（曲名）・artist（作者）・source（配布元）・source_url（曲のページ）・license（必要なライセンス表記）で載せる。"
    "credit があれば（作曲者が表記の仕方を指定している曲）、その文をそのまま載せる。conditions は作曲者独自の利用条件のメモ（説明文には載せない）。"
    "新しい曲を置くと、曲ファイルのタグから曲名・作者を読んで自動で追記する（info_from に情報の出どころ）。"
    "features は曲の音を解析した値（にぎやかさ・音の明るさ・音量の起伏・テンポなど）で、場面に一番合う曲を選ぶのに使う。"
    "uptempo が true の曲（アップテンポ。長く聞くと疲れる）は、その雰囲気にほかの曲が無いときだけ使う（手で書き換えてよい）。"
    "分からない項目は空のままにし、推測で埋めないこと（artist が空の曲は、説明文に「作者名未登録」と出る）。"
)


def _legacy_credits() -> dict[str, str]:
    """以前の config/bgm_credits.json（曲名 → 「曲名 / 作者」）。利用者が確認して書いた作者名として使う。"""
    try:
        data = json.loads(LEGACY_CREDITS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    result = {}
    for title, credit in data.items():
        if str(title).startswith("_"):
            continue
        match = re.match(r"^「?(.*?)」?\s*(?:/|／|\bby\b)\s*([^/／]+)$", str(credit).strip())
        if match and match.group(2).strip():
            result[_norm(title)] = match.group(2).strip()
    return result


def _norm(text: str) -> str:
    return re.sub(r"[\s_:：「」『』!！?？.,、。・\-]+", "", str(text)).lower()


def _ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - ffmpeg が無い環境では、タグを読まずにファイル名だけで登録する
        return None


def read_tags(path: Path) -> dict[str, str]:
    """曲ファイルのタグ（曲名・作者など）を ffmpeg で読む。読めなければ空。"""
    exe = _ffmpeg_exe()
    if not exe:
        return {}
    try:
        out = subprocess.run(
            [exe, "-v", "quiet", "-i", str(path), "-f", "ffmetadata", "-"],
            capture_output=True, timeout=30, check=False,
        ).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return {}
    tags = {}
    for line in out.splitlines():
        if "=" in line and not line.startswith(";"):
            key, value = line.split("=", 1)
            tags[key.strip().lower()] = value.strip()
    return tags


def _plausible_name(value: str) -> bool:
    """文字化けしたタグ（Shift_JIS を別の文字コードで読んだもの）や、配布サイト名だけのタグは作者名として使わない。"""
    if not value or any("\u0080" <= c <= "ÿ" for c in value):
        return False
    return _norm(value) not in {"dovasyndrome", "opentracks", "opentrack"}


def entry_from_tags(path: Path, tags: dict[str, str], legacy: dict[str, str]) -> dict:
    """曲ファイルのタグと、以前のクレジット表記から、ライブラリの1曲分を作る（分からない項目は空）。"""
    filename_title = _title_from_filename(path)
    tag_title = tags.get("title", "")
    title = tag_title if tag_title and _plausible_name(tag_title) and _norm(tag_title) == _norm(filename_title) \
        else filename_title
    artist, info_from = "", ""
    if _norm(title) in legacy:
        artist, info_from = legacy[_norm(title)], "config/bgm_credits.json"
    elif _plausible_name(tags.get("artist", "")):
        artist, info_from = tags["artist"], "曲ファイルのタグ（artist）"
    return {
        "title": title, "artist": artist, "source": "", "source_url": "", "license": "",
        "info_from": info_from or "（作者の情報なし。確認して記入してください）",
    }


FEATURE_SAMPLE_RATE = 11025
FEATURE_SECONDS = 120


def analyze_features(path: Path) -> dict:
    """曲の音の特徴を測る（曲選びの判断材料）。

    busyness: 目立つ音の頭の数（1秒あたり。大きいほど音数が多くにぎやか）
    brightness_hz: 音の重心の周波数（大きいほど高い音が目立つ・明るい音色）
    dynamics: 音量の起伏（大きい音の部分 ÷ 静かな部分。大きいほど急に大きくなる・静かになる）
    loudness_rms・bass_ratio・seconds（長さ）・bpm（参考。推定が不安定なので選曲には使わない）
    """
    import numpy as np
    from moviepy import AudioFileClip

    sr = FEATURE_SAMPLE_RATE
    clip = AudioFileClip(str(path), fps=sr)
    try:
        seconds = float(clip.duration)
        x = np.asarray(clip.subclipped(0, min(seconds, FEATURE_SECONDS)).to_soundarray(fps=sr), dtype=np.float32)
    finally:
        clip.close()
    x = x.mean(axis=1) if x.ndim == 2 else x
    n, hop = 1024, 256
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop] * np.hanning(n)
    spec = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    flux = np.maximum(0, np.diff(np.log1p(spec), axis=0)).sum(axis=1)
    flux = np.maximum(flux - np.convolve(flux, np.ones(16) / 16, mode="same"), 0)
    rate = sr / hop
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    lags = np.arange(len(ac))
    window = (lags > rate * 60 / 190) & (lags < rate * 60 / 60)
    bpm = 60 * rate / lags[window][np.argmax(ac[window])] if window.any() else 0.0
    result = {
        "bpm": round(float(bpm)),
        "brightness_hz": round(float((spec * freqs).sum() / max(float(spec.sum()), 1e-9))),
        "busyness": round(float((flux > flux.mean() + 1.5 * flux.std()).mean() * rate), 2),
        "loudness_rms": round(float(np.sqrt(np.mean(x ** 2))), 4),
        "dynamics": round(float(np.percentile(rms, 95) / max(float(np.percentile(rms, 20)), 1e-6)), 2),
        "bass_ratio": round(float(spec[:, freqs < 250].sum() / max(float(spec.sum()), 1e-9)), 3),
        "seconds": round(seconds, 1),
    }
    try:  # テンポは librosa があれば測る（アップテンポの曲を普段のBGMに使わないための判定）
        import librosa

        y = librosa.resample(x, orig_sr=sr, target_sr=22050)
        onset = librosa.onset.onset_strength(y=y, sr=22050)
        result["tempo_bpm"] = round(float(np.atleast_1d(librosa.feature.tempo(onset_envelope=onset, sr=22050,
                                                                                start_bpm=100))[0]))
        onsets = librosa.onset.onset_detect(onset_envelope=onset, sr=22050, units="time")
        result["note_density"] = round(len(onsets) / max(min(seconds, FEATURE_SECONDS), 1.0), 2)
    except Exception:  # noqa: BLE001 - librosa が無ければテンポは測らない（アップテンポ扱いにしない）
        pass
    return result


def refresh_library(default_source: str = "") -> list[str]:
    """assets/bgm/ にあってライブラリに無い曲を、タグから読んで追記する（追記した曲のキーを返す）。

    default_source は、新しく追記する曲の配布元（利用者が分かっている場合だけ渡す）。
    """
    if not BGM_DIR.exists():
        return []
    library = load_library()
    legacy = _legacy_credits()
    added = []
    for path in sorted(BGM_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in BGM_EXTS:
            continue
        key = path.relative_to(BGM_DIR).as_posix()
        if key in library and library[key].get("features"):
            continue
        entry = library.get(key) or dict(entry_from_tags(path, read_tags(path), legacy), source=default_source)
        try:
            entry["features"] = analyze_features(path)
        except Exception:  # noqa: BLE001 - 測れない曲は特徴なし（中くらいの曲として扱う）で登録する
            entry.setdefault("features", {})
        f = entry.get("features") or {}
        entry.setdefault("uptempo", bool(f.get("tempo_bpm", 0) >= UPTEMPO_MIN_BPM
                                         and f.get("note_density", 0) >= UPTEMPO_MIN_DENSITY))
        library[key] = entry
        added.append(key)
    if added:
        save_library(library)
    return added


# ---------------------------------------------------------------------------
# シーンごとのBGMの決定
# ---------------------------------------------------------------------------

# 曲の選び方: 雰囲気のフォルダ（OpenTracks の曲ページのタグで分けたもの）の中から、曲の音の特徴
# （_library.json の features。analyze_features で測った値）が、その雰囲気の場面に一番合う曲を選ぶ。
# どの曲もセリフの下で流れるので、「セリフの邪魔をしない」ことを全部の雰囲気で重視する:
#   - 音量の起伏が大きい曲（静かな出だしから急に大きくなる等）は、セリフが聞き取りにくくなるので避ける
#   - 音数が多すぎる・高い音が目立つ曲は、解説の場面では避ける
# 雰囲気ごとに、それに加えて好む特徴（楽しい: 明るく音数が多い、感動: 音数が少なく静か…）を足して点数を付ける。
# テンポ（BPM）は推定が不安定なので使わない。
TALK_WEIGHT = {"calm": 1.6, "serious": 1.2, "mysterious": 1.2, "emotional": 1.0, "tense": 0.9, "bright": 0.8,
               "comedy": 0.6}
MOOD_PREFERENCES: dict[str, dict[str, float]] = {
    # 特徴（標準化した値）ごとの重み。+ はその特徴が大きい曲ほど合う
    "calm": {},
    "bright": {"brightness_hz": 0.4, "busyness": -0.2},  # 明るい曲でも、音数が多すぎて疲れる曲は避ける
    "comedy": {"busyness": 0.6, "brightness_hz": 0.3},
    "tense": {"brightness_hz": -0.3},
    "serious": {"busyness": -0.2},
    "emotional": {"busyness": -0.5, "brightness_hz": -0.2},
    "mysterious": {"busyness": -0.3},
}
FEATURE_KEYS = ("busyness", "brightness_hz", "dynamics")


def _feature_stats() -> dict[str, tuple[float, float]]:
    """全曲の特徴の平均と標準偏差（標準化に使う）。"""
    import math

    values: dict[str, list[float]] = {k: [] for k in FEATURE_KEYS}
    for info in load_library().values():
        for k in FEATURE_KEYS:
            v = (info.get("features") or {}).get(k)
            if isinstance(v, (int, float)):
                values[k].append(math.log(v) if k == "dynamics" and v > 0 else float(v))
    stats = {}
    for k, vs in values.items():
        if len(vs) >= 2:
            mean = sum(vs) / len(vs)
            sd = (sum((v - mean) ** 2 for v in vs) / len(vs)) ** 0.5 or 1.0
            stats[k] = (mean, sd)
    return stats


def track_score(track: "Track", mood: str, longest_seconds: float = 0.0,
                stats: Optional[dict] = None) -> float:
    """その雰囲気の場面に、この曲がどれだけ合うか（大きいほど合う）。"""
    import math

    features = (load_library().get(track.key) or {}).get("features") or {}
    stats = stats if stats is not None else _feature_stats()

    def z(key: str) -> float:
        v = features.get(key)
        if not isinstance(v, (int, float)) or key not in stats:
            return 0.0
        v = math.log(v) if key == "dynamics" and v > 0 else float(v)
        mean, sd = stats[key]
        return (v - mean) / sd

    # セリフの邪魔をしにくいか: 音量の起伏が小さく、音数が多すぎず、高い音が目立ちすぎない
    talk = -1.0 * z("dynamics") - 0.6 * z("busyness") - 0.4 * max(0.0, z("brightness_hz"))
    score = TALK_WEIGHT.get(mood, 1.0) * talk
    score += sum(w * z(k) for k, w in MOOD_PREFERENCES.get(mood, {}).items())
    seconds = features.get("seconds")
    if isinstance(seconds, (int, float)) and longest_seconds > 0 and seconds < longest_seconds * 0.5:
        score -= 0.5  # 短い曲を長い場面で何度もループさせると繰り返しが目立つ
    if is_uptempo(track):
        score -= UPTEMPO_PENALTY  # アップテンポの曲は長く聞くと疲れるので、ほかに曲が無いときだけ使う
    return score


UPTEMPO_PENALTY = 10.0
UPTEMPO_MIN_BPM = 115        # この速さ以上で、かつ
UPTEMPO_MIN_DENSITY = 3.5    # 1秒あたりの音の頭がこれ以上（テンポの推定は倍・半分に間違えやすいので、両方そろったときだけ）


def is_uptempo(track: "Track") -> bool:
    """アップテンポの曲か（_library.json の uptempo。手で true/false に書き換えれば、その判定を使う）。"""
    info = load_library().get(track.key) or {}
    if isinstance(info.get("uptempo"), bool):
        return info["uptempo"]
    features = info.get("features") or {}
    return bool(features.get("tempo_bpm", 0) >= UPTEMPO_MIN_BPM and features.get("note_density", 0) >= UPTEMPO_MIN_DENSITY)


def best_track(mood: str, longest_seconds: float = 0.0) -> Optional[str]:
    """その雰囲気のフォルダから、場面に一番合う曲（ランダムではなく、いつも同じ基準で選ぶ）。"""
    tracks = list_tracks(mood)
    if not tracks:
        return None
    stats = _feature_stats()
    return str(max(tracks, key=lambda t: (track_score(t, mood, longest_seconds, stats), t.title)).path)


def auto_track(project: Project, mood: str, use_choice: bool = True, longest_seconds: float = 0.0) -> Optional[str]:
    """その雰囲気で流す曲（利用者が選んだ曲 → 場面に一番合う曲）。気分のフォルダが空なら None。

    use_choice=False なら、利用者の選択を見ずに自動で選ぶ曲だけを返す（画面の「自動（曲名）」の表示用）。
    """
    chosen = project.mood_bgm.get(mood) if use_choice else None
    if chosen == NO_BGM:
        return None
    if chosen and Path(chosen).exists():
        return chosen
    return best_track(mood, longest_seconds)


def uses_moods(project: Project) -> bool:
    return any(s.bgm_mood for s in project.scenes)


# --- 台本に雰囲気（bgm）が書かれていないときの推定 ---
# 場面ごとの基本の雰囲気（重み）
SECTION_PRIORS: dict[str, dict[str, float]] = {
    "intro:hype": {"bright": 2.0}, "intro:fail": {"comedy": 1.5, "emotional": 0.5}, "intro:rescue": {"calm": 1.5},
    "intro": {"bright": 1.5}, "explain:why": {"calm": 2.0}, "explain:how": {"calm": 2.0},
    "explain": {"calm": 2.0}, "summary": {"calm": 2.0, "bright": 0.5}, "ending": {"bright": 3.0},
}
EXPRESSION_MOODS = {
    "cry": "emotional", "sad": "emotional", "gloomy": "emotional", "worried": "emotional",
    "shock": "tense", "panic": "tense",
    "happy": "bright", "excited": "bright", "laugh": "bright", "idea": "bright",
    "smug": "comedy", "angry": "comedy",
    "think": "mysterious",
}
SCENE_MOODS = {"gloomy": "emotional", "sepia": "emotional", "dark": "tense", "shock": "tense", "bright": "bright"}
SE_MOODS = (
    (("ボヨヨーン", "ボヨン", "ちゃんちゃん", "チーン", "ツッコミ", "ずっこけ", "ピューン"), "comedy"),
    (("運命", "ショック", "不安", "ドドン", "爆発", "タイマー"), "tense"),
    (("ファンファーレ", "歓声", "レベルアップ", "イエーイ", "イヤッホー", "パフパフ"), "bright"),
)


def _scene_votes(scene: Scene) -> dict[str, float]:
    votes: dict[str, float] = {}

    def add(mood: str, weight: float) -> None:
        votes[mood] = votes.get(mood, 0.0) + weight

    if scene.mood in SCENE_MOODS:
        add(SCENE_MOODS[scene.mood], 3.0)
    se = Path(scene.se_path).stem if scene.se_path else ""
    for words, mood in SE_MOODS:
        if any(w in se for w in words):
            add(mood, 1.5)
    if scene.expression in EXPRESSION_MOODS and scene.text.strip():
        add(EXPRESSION_MOODS[scene.expression], 1.0)
    if scene.char_motion == "tsukkomi":
        add("comedy", 1.0)
    if scene.shows_board:
        add("calm", 0.5)
    return votes


def infer_moods(project: Project) -> list[str]:
    """台本に雰囲気（bgm）が無いときに、場面・表情・効果音・背景の雰囲気から、ブロックごとの雰囲気を推定する。"""
    scenes = project.scenes
    blocks: list[list[int]] = []
    for i, scene in enumerate(scenes):
        prev = scenes[i - 1] if i else None
        same = prev is not None and prev.section == scene.section and prev.phase == scene.phase \
            and prev.slide_title == scene.slide_title
        if same or (scene.card_text and blocks):
            blocks[-1].append(i)
        else:
            blocks.append([i])
    result = [""] * len(scenes)
    for block in blocks:
        first = scenes[block[0]]
        prior = SECTION_PRIORS.get(f"{first.section}:{first.phase}") or SECTION_PRIORS.get(first.section) \
            or {"calm": 2.0}
        score = {m: w * 2 for m, w in prior.items()}
        for i in block:
            for mood, weight in _scene_votes(scenes[i]).items():
                score[mood] = score.get(mood, 0.0) + weight
        mood = max(score, key=lambda m: (score[m], m == "calm"))
        for i in block:
            result[i] = mood
    return result


def effective_moods(project: Project, durations: Optional[list[float]] = None) -> list[str]:
    """各シーンで実際に流す雰囲気。

    台本の雰囲気（bgm）があれば、そのセリフから切り替える（それより前は推定した雰囲気）。無ければ推定する。
    短すぎる区間は前の雰囲気のまま流す。
    """
    scenes = project.scenes
    durations = durations or [s.duration for s in scenes]
    inferred = infer_moods(project)
    explicit = uses_moods(project)
    raw: list[str] = []
    current = ""
    for scene, guess in zip(scenes, inferred):
        if explicit:
            if scene.bgm_mood in MOOD_CHOICES:
                current = scene.bgm_mood
            elif scene.section == "ending":
                current = guess
            raw.append(current or guess)
        else:
            raw.append(scene.bgm_mood if scene.bgm_mood in MOOD_CHOICES else guess)
    # 同じ雰囲気の続く区間に分け、短すぎる区間は前の区間の雰囲気にする
    result = list(raw)
    i = 0
    while i < len(raw):
        j = i
        while j + 1 < len(raw) and raw[j + 1] == raw[i]:
            j += 1
        length = sum(durations[i:j + 1])
        minimum = MIN_SILENCE_SECONDS if raw[i] == SILENCE else MIN_MOOD_SECONDS
        if raw[i] and i > 0 and length < minimum and result[i - 1]:
            for k in range(i, j + 1):
                result[k] = result[i - 1]
        i = j + 1
    return result


def _existing(path: Optional[str]) -> Optional[str]:
    """設定された曲が実際にあるときだけ返す（フォルダ分けで場所が変わった古い設定は無視する）。"""
    if path == NO_BGM:
        return NO_BGM
    return path if path and Path(path).exists() else None


def scene_bgm_paths(project: Project, durations: Optional[list[float]] = None) -> list[Optional[str]]:
    """各シーンで流すBGMのファイル（None は無音）。何も設定しなくても、場面に合う曲が流れる。

    優先順: シーン個別の曲 → 台本の雰囲気（bgm）の曲 → 利用者が選んだ場面ごと・全体の曲（ファイルがある場合）
    → 推定した雰囲気の曲。
    """
    scenes = project.scenes
    durations = durations or [s.duration for s in scenes]
    moods = effective_moods(project, durations)
    # 雰囲気ごとに、一番長く続く区間の長さ（短い曲を長い場面でループさせないための目安）
    longest: dict[str, float] = {}
    run_mood, run_len = None, 0.0
    for mood, duration in zip(moods, durations):
        run_len = run_len + duration if mood == run_mood else duration
        run_mood = mood
        longest[mood] = max(longest.get(mood, 0.0), run_len)
    chosen: dict[str, Optional[str]] = {}
    explicit_mood = ""
    paths: list[Optional[str]] = []
    for scene, mood in zip(scenes, moods):
        if scene.bgm_mood in MOOD_CHOICES:
            explicit_mood = scene.bgm_mood
        manual = _existing(scene.bgm_path)
        if manual:
            paths.append(None if manual == NO_BGM else manual)
            continue
        setting = None
        if not explicit_mood or scene.section == "ending":  # 台本に雰囲気が書かれていれば、それを優先する
            phase_bgm = project.section_bgm.get(f"{scene.section}:{scene.phase}") if scene.phase else None
            for candidate in (phase_bgm, project.section_bgm.get(scene.section), project.bgm_path):
                setting = _existing(candidate)
                if setting:
                    break
        if setting:
            paths.append(None if setting == NO_BGM else setting)
        elif mood == SILENCE:
            paths.append(None)
        elif mood:
            if mood not in chosen:
                chosen[mood] = auto_track(project, mood, longest_seconds=longest.get(mood, 0.0))
            paths.append(chosen[mood])
        else:
            paths.append(None)
    return paths


def scene_bgm_path(project: Project, scene: Scene) -> Optional[str]:
    for s, path in zip(project.scenes, scene_bgm_paths(project)):
        if s is scene:
            return path
    return None


# ---------------------------------------------------------------------------
# 音の大きさ
# ---------------------------------------------------------------------------

def _load_loudness_cache() -> dict:
    try:
        return json.loads(LOUDNESS_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def loudness_gain(path: str | Path) -> float:
    """曲の音の大きさを TARGET_RMS にそろえる倍率（測れなければ 1.0）。結果は tmp/ に覚えておく。"""
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return 1.0
    cache_key = f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    cache = _load_loudness_cache()
    if cache_key in cache:
        return float(cache[cache_key])
    try:
        import numpy as np
        from moviepy import AudioFileClip
        clip = AudioFileClip(str(path), fps=LOUDNESS_FPS)
        try:
            sample = clip.subclipped(0, min(clip.duration, LOUDNESS_SAMPLE_SECONDS))
            samples = np.asarray(sample.to_soundarray(fps=LOUDNESS_FPS), dtype=float)
        finally:
            clip.close()
        rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    except Exception:  # noqa: BLE001 - 測れない曲はそのままの音量で使う
        return 1.0
    gain = 1.0 if rms <= 1e-6 else max(GAIN_RANGE[0], min(GAIN_RANGE[1], TARGET_RMS / rms))
    cache[cache_key] = round(gain, 4)
    try:
        LOUDNESS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOUDNESS_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return gain


# ---------------------------------------------------------------------------
# クレジット
# ---------------------------------------------------------------------------

# 配布元のサイト名 → サイトのURL（説明文の「BGM：配布元（URL）」の行に出す）
SOURCE_SITES: dict[str, str] = {"OpenTracks": "https://opentracks.com/"}


def credit_lines(paths: list[str]) -> tuple[list[str], list[str]]:
    """実際に使った曲だけのクレジット行（配布元ごと）と、情報が足りない曲の注意（説明文には載せない）を返す。"""
    groups: dict[str, list[str]] = {}
    missing: list[str] = []
    for path in paths:
        track = track_for_path(path)
        if track.credit:
            line = f"　{track.credit}"  # 作曲者が指定した書き方をそのまま載せる
            if line in groups.get(track.source, []):
                continue  # 同じ作曲者の曲を2曲使っても、指定の表記は1回だけ
        elif track.artist:
            line = f"　{track.title} / {track.artist}" + ("" if track.artist.endswith("様") else " 様")
        else:
            line = f"　{track.title}（作者名未登録）"
            missing.append(f"BGM「{track.title}」の作者名が assets/bgm/_library.json に登録されていません（{track.key}）。")
        if track.source_url:
            line += f"\n　　{track.source_url}"
        if track.license:
            line += f"\n　　{track.license}"
        if not track.source:
            missing.append(f"BGM「{track.title}」の配布元が assets/bgm/_library.json に登録されていません（{track.key}）。")
        groups.setdefault(track.source, []).append(line)
    lines: list[str] = []
    for source, items in groups.items():
        if source:
            url = SOURCE_SITES.get(source, "")
            lines.append(f"・BGM：{source}" + (f"（{url}）" if url else ""))
        else:
            lines.append("・BGM：")
        lines += items
    return lines, missing
