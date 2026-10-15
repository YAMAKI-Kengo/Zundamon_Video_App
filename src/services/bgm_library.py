"""
BGMの曲ライブラリ（気分ごとのフォルダ）と、場面の種類に合わせたBGMの自動選択・クレジット。

assets/bgm/ の下に、気分ごとのフォルダ（bright・calm・comedy・emotional・mysterious・serious・tense）で曲を置く。
台本の各ブロック・セリフに「場面の種類」（bgm。ふだんの会話・調子に乗る・ピンチ…。MOODS）があれば、
その種類の場面に一番合う曲を、全部の曲から選んで流し、種類が変わったところで切り替える（クロスフェードは video_builder）。

- 曲の選び方（track_score）: フォルダの気分がその場面の種類でよく使われるものか ＋ 曲の音の特徴（勢い・明るさ・
  音量の起伏）が合うか。同じ動画の中では、種類ごとに別の曲を割り当て、同じ種類には同じ曲を使う。
  利用者は「場面の種類ごとの曲」（Project.mood_bgm）やシーンごとの曲（Scene.bgm_path）で変えられる。
- 切り替え: 台本の種類はその場面のまとまり（ブロック）の終わりまで続き、次のまとまりではそのまとまりの種類に戻る。
  まとまりの途中の MIN_MOOD_SECONDS より短い区間は前の曲のまま流す（曲がころころ変わるとかえってうるさいため）。
  「無音」（none）だけは、ショックの一瞬を際立たせるため短くても効かせる。
- 曲の情報（曲名・作者・配布元・ライセンス表記）は assets/bgm/_library.json に1曲ずつ記録する。
  新しい曲を置くと、曲ファイルのタグ（ID3）から曲名・作者を読んで自動で追記する。分からない項目は空のままにし、
  推測で埋めない（説明文には「作者名未登録」と出し、利用者に記入を促す）。
- 音量: 曲によって録音の音量がばらばらなので、曲ごとに音の大きさを測って、同じくらいの大きさにそろえてから
  BGMの音量（Project.bgm_volume）を掛ける（大きな曲でセリフが聞き取りにくくならないように）。
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
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

# 曲のフォルダ（OpenTracks の曲ページのタグで分けた気分）→ 表示名
FOLDERS: dict[str, str] = {
    "calm": "落ち着いた", "bright": "明るい", "comedy": "コミカル", "tense": "緊張",
    "serious": "シリアス", "emotional": "感動・悲しい", "mysterious": "謎・不思議",
}
# 場面の種類（台本の各セリフの bgm）→ 説明。台本のAIにもこの説明で選ばせる。
# 動画のどういう場面で流れる曲か、で分ける（曲の気分のフォルダとは別。1つの種類に、いくつかのフォルダの曲が合う）
MOODS: dict[str, str] = {
    "talk": "ふだんの会話・解説（説明・振り返り・種明かし。セリフの下でずっと流れる、いちばん長く使うベース）",
    "opening": "つかみ・導入（これから何かが始まるワクワク・問いをぶつける）",
    "cheerful": "明るい会話（楽しく話している場面・英会話の会話シーン。明るいが落ち着いていて、セリフや英語の邪魔をしない）",
    "hype": "ノリノリ・調子に乗る（うまくいっている・浮かれている・自信満々で挑戦中）",
    "gag": "ドタバタ・ボケ（ずっこけ・妄想の暴走・ツッコミの応酬）",
    "trouble": "雲行きが怪しい・ピンチ（焦り・危機・追い詰められる）",
    "fail": "やらかした後・トホホ（失敗のあとの間の抜けた空気・がっかり・言い訳）",
    "sad": "どん底・しんみり（本気の落ち込み・反省・感動）",
    "mystery": "謎・なぜ？（理由を探る・意外な真相に近づく）",
    "serious": "核心・重大な事実（本当のことを突きつける・厳しい現実）",
    "success": "成功・逆転（やり方を変えてうまくいく・達成）",
    "ending": "まとめ・締め（答えを言い切る・さわやかに終わる）",
}
LEGACY_MOODS = {  # 以前の気分の名前（台本・保存したプロジェクト）→ 場面の種類
    "calm": "talk", "bright": "hype", "comedy": "gag", "tense": "trouble",
    "emotional": "sad", "mysterious": "mystery",
}
SILENCE = "none"  # BGMを止める（ショックの瞬間など）
MOOD_CHOICES = list(MOODS) + [SILENCE]
MOOD_LABELS = {**{k: v.split("（")[0] for k, v in MOODS.items()}, SILENCE: "無音"}

MIN_MOOD_SECONDS = 6.0     # これより短い区間（場面のまとまりの途中の切り替え）は、前の曲のまま流す
MIN_BLOCK_MOOD_SECONDS = 3.0  # 場面のまとまりの切れ目での切り替えは、これ以上あれば効かせる
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
    mood: str          # フォルダ名（FOLDERS のキー。気分のフォルダの外の曲は ""）
    title: str
    artist: str = ""
    source: str = ""        # 配布元の名前
    source_url: str = ""    # 曲の配布ページ（分かる場合だけ）
    license: str = ""       # 必要なライセンス表記（分かる場合だけ）
    credit: str = ""        # 作曲者が指定しているクレジットの書き方（あれば、説明文にはこの文をそのまま載せる）
    conditions: str = ""    # 作曲者独自の利用条件（メモ。説明文には載せない）

    @property
    def key(self) -> str:
        return library_key(str(self.path))


@lru_cache(maxsize=2048)
def library_key(path: Path | str) -> str:
    """_library.json のキー（assets/bgm/ からの相対パス。区切りは /）。

    画面の描画のたびに何千回も呼ばれるので、覚えておく（ファイルの場所の解決はディスクを見るため遅い）。
    """
    path = Path(path)
    try:
        return path.resolve().relative_to(_bgm_dir_resolved()).as_posix()
    except ValueError:
        return path.name


@lru_cache(maxsize=1)
def _bgm_dir_resolved() -> Path:
    return BGM_DIR.resolve()


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
        track_mood = folder if folder in FOLDERS else ""
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
    key = library_key(str(path))
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


_LIBRARY_MEMO: dict = {}


def _library_signature() -> tuple:
    try:
        stat = LIBRARY_PATH.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (0, 0)


def load_library() -> dict[str, dict]:
    """_library.json の中身。ファイルが変わらない間は読み直さない（画面の描画のたびに何百回も呼ばれるため）。

    返す辞書は共有しているので、書き換えるときは copy.deepcopy してから（refresh_library のように）使う。
    """
    signature = _library_signature()
    if _LIBRARY_MEMO.get("signature") != signature:
        try:
            data = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        library = ({k: v for k, v in data.items() if isinstance(v, dict) and not k.startswith("_")}
                   if isinstance(data, dict) else {})
        _LIBRARY_MEMO.clear()
        _LIBRARY_MEMO.update(signature=signature, library=library)
    return _LIBRARY_MEMO["library"]


def _memo_by_library(func):
    """_library.json が変わるまで結果を覚えておく（引数なしの関数用）。"""
    memo: dict = {}

    def wrapper():
        signature = _library_signature()
        if memo.get("signature") != signature:
            memo.update(signature=signature, value=func())
        return memo["value"]

    wrapper.__doc__ = func.__doc__
    return wrapper


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
    "せわしない曲（長く聞くと疲れる曲）は features の attack_hf・busy_share・rest_share から自動で判定し、"
    "その雰囲気にほかの曲が無いときだけ使う（判定を変えたい曲だけ busy: true/false を書けばそれに従う）。"
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
    せわしなさ（長く聞くと疲れるか）の判定に使う値:
      attack_hf: 高い音（3kHz以上）の音の立ち上がりの強さ（キンキンした音が次々に鳴るほど大きい）
      busy_share: 音の頭が1秒に4つ以上ある時間の割合（ずっと音が詰まっているほど大きい）
      rest_share: 音が小さくなって一息つける時間の割合（大きいほど休みがある）
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
    result.update(_busy_features(spec, freqs, flux, rms, rate))
    return result


def _busy_features(spec, freqs, flux, rms, rate: float) -> dict:
    """せわしなさを測る値（attack_hf・busy_share・rest_share。analyze_features の説明を参照）。"""
    import numpy as np

    attack_hf = np.maximum(0, np.diff(np.log1p(spec[:, freqs > 3000]), axis=0)).sum(axis=1)
    middle = flux[1:-1]
    peaks = np.nonzero((middle > flux[:-2]) & (middle >= flux[2:]) & (middle > flux.mean() + 0.5 * flux.std()))[0] + 1
    onsets, last = [], -1e9
    for i in peaks:  # 50ミリ秒より近い音の頭は1つと数える
        if i - last >= 0.05 * rate:
            onsets.append(i / rate)
            last = i
    length = len(flux) / rate
    per_second = np.histogram(onsets, bins=np.arange(0, length + 3, 3))[0] / 3  # 3秒ごとの音の頭の数（1秒あたり）
    return {
        "attack_hf": round(float(attack_hf.mean()), 2),
        "busy_share": round(float((per_second >= 4).mean()), 2) if len(per_second) else 0.0,
        "rest_share": round(float((rms < np.percentile(rms, 95) * 0.25).mean()), 2),
    }


def refresh_library(default_source: str = "") -> list[str]:
    """assets/bgm/ にあってライブラリに無い曲を、タグから読んで追記する（追記した曲のキーを返す）。

    default_source は、新しく追記する曲の配布元（利用者が分かっている場合だけ渡す）。
    """
    if not BGM_DIR.exists():
        return []
    library = copy.deepcopy(load_library())
    legacy = _legacy_credits()
    added = []
    for path in sorted(BGM_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in BGM_EXTS:
            continue
        key = path.relative_to(BGM_DIR).as_posix()
        if key in library and "attack_hf" in (library[key].get("features") or {}):
            continue
        entry = library.get(key) or dict(entry_from_tags(path, read_tags(path), legacy), source=default_source)
        try:
            entry["features"] = analyze_features(path)
        except Exception:  # noqa: BLE001 - 測れない曲は特徴なし（中くらいの曲として扱う）で登録する
            entry.setdefault("features", {})
        library[key] = entry
        added.append(key)
    if added:
        save_library(library)
    return added


# ---------------------------------------------------------------------------
# シーンごとのBGMの決定
# ---------------------------------------------------------------------------

# 曲の選び方: 台本の場面の種類（MOODS。「ふだんの会話」「調子に乗る」「ピンチ」…）ごとに、その場面に一番合う曲を、
# 全部の曲から点数で選ぶ（ランダムではない）。点数は次の足し算:
#   1. 曲のフォルダ（OpenTracks の曲ページのタグで分けた気分）が、その場面の種類でよく使われる気分か（MOOD_FOLDERS）
#   2. 曲の音の特徴（_library.json の features）が、その場面に合うか（MOOD_PREFERENCES。全曲の中で標準化した値）
#        energy: 音の勢い（音の頭の多さ・高い音の立ち上がり・音が詰まっている時間 − 休みの時間）
#        brightness_hz: 音の明るさ（高い音が目立つほど大きい）
#        dynamics: 音量の起伏（急に大きくなる・静かになる曲は、どの場面でもセリフが聞き取りにくい）
#   3. せわしない曲（is_busy）は、長く流れる場面（ふだんの会話・まとめ等）では使わない。短い場面（ドタバタ等）では少し減点だけ
#   4. 短い曲を長い区間でループさせると繰り返しが目立つので少し減点
# 1本の動画の中では、場面の種類ごとに別の曲を割り当てる（同じ曲が別の場面で使い回されて単調にならないように）。
MOOD_FOLDERS: dict[str, dict[str, float]] = {
    "talk": {"calm": 3.0, "bright": 0.8, "comedy": 0.4},
    "opening": {"bright": 3.0, "comedy": 1.5, "calm": 1.0},
    "cheerful": {"bright": 3.0, "calm": 2.0, "comedy": 0.5},
    "hype": {"bright": 3.0, "comedy": 2.0},
    "gag": {"comedy": 3.0, "bright": 0.5},
    "trouble": {"tense": 3.0, "mysterious": 1.0, "serious": 1.0},
    "fail": {"comedy": 3.0, "emotional": 1.0, "calm": 0.5},
    "sad": {"emotional": 3.0, "calm": 0.5},
    "mystery": {"mysterious": 3.0, "tense": 1.5},
    "serious": {"serious": 3.0, "tense": 1.5, "emotional": 0.5},
    "success": {"bright": 3.0, "emotional": 1.0},
    "ending": {"calm": 2.0, "bright": 2.0, "emotional": 0.5},
}
MOOD_PREFERENCES: dict[str, dict[str, float]] = {
    # 特徴（標準化した値）ごとの重み。+ はその特徴が大きい曲ほど合う
    "talk": {"energy": -1.0, "dynamics": -0.8, "brightness_hz": -0.3},  # セリフの下でずっと流れても邪魔にならない
    "opening": {"energy": 0.4, "brightness_hz": 0.4},
    "cheerful": {"energy": -0.6, "brightness_hz": 0.3, "dynamics": -0.6},  # 明るいけれど、長く流れても疲れない
    "hype": {"energy": 0.8, "brightness_hz": 0.4},
    "gag": {"energy": 0.6},
    "trouble": {"energy": 0.3, "brightness_hz": -0.4},
    "fail": {"energy": -0.8},             # やらかした後の、間の抜けたゆるい曲
    "sad": {"energy": -0.8, "brightness_hz": -0.3},
    "mystery": {"energy": -0.4},
    "serious": {"brightness_hz": -0.2},
    "success": {"energy": 0.4, "brightness_hz": 0.5},
    "ending": {"energy": -0.2, "brightness_hz": 0.3, "dynamics": -0.4},
}
SHORT_MOODS = {"hype", "gag", "trouble", "fail"}  # 短い場面で使う種類（せわしない曲でも少しの減点で使ってよい）
DYNAMICS_WEIGHT = 0.5      # どの場面もセリフの下で流れるので、音量の起伏が大きい曲は全体で減点
BUSY_PENALTY = 10.0
BUSY_SHORT_PENALTY = 1.5
FEATURE_KEYS = ("busyness", "brightness_hz", "dynamics", "attack_hf", "busy_share", "rest_share")


@_memo_by_library
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
        if len(vs) >= 5:
            mean = sum(vs) / len(vs)
            stats[k] = (mean, (sum((v - mean) ** 2 for v in vs) / len(vs)) ** 0.5 or 1.0)
    return stats


def _z(track: "Track", stats: dict) -> dict[str, float]:
    """曲の特徴を、全曲の中で標準化した値（測っていない特徴は0＝ふつう）。energy・busy も計算する。"""
    import math

    features = (load_library().get(track.key) or {}).get("features") or {}
    z: dict[str, float] = {}
    for key in FEATURE_KEYS:
        v = features.get(key)
        if isinstance(v, (int, float)) and key in stats:
            v = math.log(v) if key == "dynamics" and v > 0 else float(v)
            z[key] = (v - stats[key][0]) / stats[key][1]
        else:
            z[key] = 0.0
    z["energy"] = (z["busyness"] + z["attack_hf"] + z["busy_share"] - z["rest_share"]) / 2
    z["busy"] = z["attack_hf"] + z["busy_share"] - z["rest_share"]
    return z


BUSY_KEYS = ("attack_hf", "busy_share", "rest_share")
BUSY_THRESHOLD = 3.0  # せわしなさの点数（全曲の中での標準化した値の合計）がこれ以上なら、せわしない曲


def busy_score(track: "Track") -> Optional[float]:
    """せわしなさの点数（大きいほど、長く聞くと疲れる）。測っていない曲は None。

    テンポの速さではなく、「高い音の立ち上がりが強い（キンキンした音が次々に鳴る）」「ずっと音が詰まっている」
    「一息つける静かな所がない」の3つを、全曲の中でどれだけ目立つか（標準化した値）で足し合わせる。
    """
    features = (load_library().get(track.key) or {}).get("features") or {}
    if not all(isinstance(features.get(k), (int, float)) for k in BUSY_KEYS):
        return None
    stats = _feature_stats()
    if not all(k in stats for k in BUSY_KEYS):
        return None
    return _z(track, stats)["busy"]


def is_busy(track: "Track") -> bool:
    """せわしない曲か。音から自動で判定する（_library.json に busy: true/false を書いた曲だけはそれに従う）。"""
    manual = (load_library().get(track.key) or {}).get("busy")
    if isinstance(manual, bool):
        return manual
    score = busy_score(track)
    return score is not None and score >= BUSY_THRESHOLD


def track_score(track: "Track", mood: str, longest_seconds: float = 0.0,
                stats: Optional[dict] = None) -> float:
    """その場面の種類に、この曲がどれだけ合うか（大きいほど合う）。フォルダがその場面で使われない曲は -inf。"""
    folder_weight = MOOD_FOLDERS.get(mood, {}).get(track.mood, 0.0)
    if folder_weight <= 0:
        return float("-inf")
    stats = stats if stats is not None else _feature_stats()
    z = _z(track, stats)
    score = folder_weight - DYNAMICS_WEIGHT * z["dynamics"]
    score += sum(w * z.get(k, 0.0) for k, w in MOOD_PREFERENCES.get(mood, {}).items())
    if is_busy(track):
        score -= BUSY_SHORT_PENALTY if mood in SHORT_MOODS else BUSY_PENALTY
    seconds = ((load_library().get(track.key) or {}).get("features") or {}).get("seconds")
    if isinstance(seconds, (int, float)) and longest_seconds > 0 and seconds < longest_seconds * 0.5:
        score -= 0.5  # 短い曲を長い場面で何度もループさせると繰り返しが目立つ
    return score


def candidates(mood: str) -> list[Track]:
    """その場面の種類に使える曲（合う順）。"""
    stats = _feature_stats()
    scored = [(track_score(t, mood, 0.0, stats), t.title, t) for t in _all_tracks()]
    return [t for s, _, t in sorted(scored, key=lambda x: (-x[0], x[1])) if s > float("-inf")]


def best_track(mood: str, longest_seconds: float = 0.0, exclude: Optional[set[str]] = None) -> Optional[str]:
    """その場面の種類に一番合う曲（ランダムではなく、いつも同じ基準で選ぶ）。exclude の曲は、ほかに無いときだけ使う。"""
    stats = _feature_stats()
    scored = [(track_score(t, mood, longest_seconds, stats), t.title, t) for t in _all_tracks()]
    scored = [x for x in scored if x[0] > float("-inf")]
    if not scored:
        return None
    exclude = exclude or set()
    fresh = [x for x in scored if str(x[2].path) not in exclude] or scored
    return str(max(fresh, key=lambda x: (x[0], x[1]))[2].path)


def user_choice(project: Project, mood: str) -> Optional[str]:
    """利用者がその場面の種類に選んだ曲（NO_BGM＝その場面はBGMなし。古い気分の名前での設定も読む）。"""
    chosen = project.mood_bgm.get(mood)
    if chosen is None:
        for old, new in LEGACY_MOODS.items():
            if new == mood and project.mood_bgm.get(old):
                chosen = project.mood_bgm[old]
                break
    if chosen == NO_BGM:
        return NO_BGM
    return chosen if chosen and Path(chosen).exists() else None


def auto_track(project: Project, mood: str, use_choice: bool = True, longest_seconds: float = 0.0,
               exclude: Optional[set[str]] = None) -> Optional[str]:
    """その場面の種類で流す曲（利用者が選んだ曲 → 一番合う曲）。

    use_choice=False なら、利用者の選択を見ずに自動で選ぶ曲だけを返す（画面の「自動（曲名）」の表示用）。
    """
    if use_choice:
        chosen = user_choice(project, mood)
        if chosen == NO_BGM:
            return None
        if chosen:
            return chosen
    return best_track(mood, longest_seconds, exclude)


def scene_mood(scene: Scene) -> str:
    """台本に書かれた場面の種類（以前の気分の名前は、今の種類に読み替える）。書かれていなければ空文字。"""
    mood = LEGACY_MOODS.get(scene.bgm_mood, scene.bgm_mood)
    return mood if mood in MOOD_CHOICES else ""


def uses_moods(project: Project) -> bool:
    return any(scene_mood(s) for s in project.scenes)


# --- 台本に場面の種類（bgm）が書かれていないときの推定 ---
# 場面（section:phase）ごとの基本の種類（重み）
SECTION_PRIORS: dict[str, dict[str, float]] = {
    "intro:hype": {"opening": 2.0}, "intro:fail": {"gag": 1.5, "fail": 0.5}, "intro:rescue": {"talk": 1.5},
    "intro": {"opening": 1.5},
    "explain:try": {"hype": 1.5, "trouble": 0.5}, "explain:reveal": {"talk": 2.0},
    "explain:why": {"talk": 2.0}, "explain:how": {"talk": 2.0}, "explain": {"talk": 2.0},
    "summary": {"talk": 1.5, "ending": 1.0}, "ending": {"ending": 3.0},
    # 英会話
    "dialog": {"cheerful": 2.0}, "phrase": {"talk": 2.0}, "repeat": {"talk": 2.0}, "quiz": {"cheerful": 1.5},
    "review": {"talk": 2.0},
}
EXPRESSION_MOODS = {
    "cry": "sad", "sad": "sad", "gloomy": "sad", "worried": "trouble",
    "shock": "trouble", "panic": "trouble",
    "happy": "hype", "excited": "hype", "smug": "hype", "laugh": "gag", "angry": "gag",
    "idea": "success", "think": "mystery",
}
SCENE_MOODS = {"gloomy": "sad", "sepia": "sad", "dark": "trouble", "shock": "trouble", "bright": "success"}
SE_MOODS = (
    (("ボヨヨーン", "ボヨン", "ちゃんちゃん", "ツッコミ", "ずっこけ", "ピューン"), "gag"),
    (("チーン",), "fail"),
    (("運命", "ショック", "不安", "ドドン", "爆発", "タイマー"), "trouble"),
    (("ファンファーレ", "歓声", "レベルアップ", "イエーイ", "イヤッホー", "パフパフ"), "success"),
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
        add("gag", 1.0)
    if scene.shows_board:
        add("talk", 0.5)
    return votes


def scene_blocks(project: Project) -> list[list[int]]:
    """場面のまとまり（場面・段階が同じシーンの連続）。BGMの基本の種類はこのまとまりごとに決まる。

    場面転換テロップ（「1か月後…」）と、黒板の見出しが別の見出しに変わったところからは新しいまとまりにする
    （黒板が出ていない所から出る所へは同じまとまり。本の紹介で表紙を出すだけ等で切らない）。
    """
    scenes = project.scenes
    blocks: list[list[int]] = []
    for i, scene in enumerate(scenes):
        prev = scenes[i - 1] if i else None
        same = prev is not None and prev.section == scene.section and prev.phase == scene.phase \
            and (prev.slide_title == scene.slide_title or not (prev.slide_title and scene.slide_title))
        if same and not scene.card_text:
            blocks[-1].append(i)
        else:
            blocks.append([i])
    return blocks


def infer_moods(project: Project) -> list[str]:
    """台本に場面の種類（bgm）が無いときに、場面・表情・効果音・背景の雰囲気から、まとまりごとの種類を推定する。"""
    scenes = project.scenes
    result = [""] * len(scenes)
    for block in scene_blocks(project):
        first = scenes[block[0]]
        prior = SECTION_PRIORS.get(f"{first.section}:{first.phase}") or SECTION_PRIORS.get(first.section) \
            or {"talk": 2.0}
        score = {m: w * 2 for m, w in prior.items()}
        for i in block:
            for mood, weight in _scene_votes(scenes[i]).items():
                score[mood] = score.get(mood, 0.0) + weight
        mood = max(score, key=lambda m: (score[m], m == "talk"))
        for i in block:
            result[i] = mood
    return result


def effective_moods(project: Project, durations: Optional[list[float]] = None) -> list[str]:
    """各シーンで実際に流す場面の種類。

    台本の種類（bgm）が書かれたセリフから、その種類に切り替える。書かれた種類が続くのは、その場面のまとまりの終わりまで
    （次のまとまりに入ったら、そのまとまりの最初に書かれた種類か、推定した種類に戻す。導入のコミカルな曲が、
    ふだんの解説に入っても流れ続けないように）。
    短すぎる区間は前の種類のまま流す。
    """
    scenes = project.scenes
    durations = durations or [s.duration for s in scenes]
    inferred = infer_moods(project)
    raw = [""] * len(scenes)
    starts = []
    for block in scene_blocks(project):
        starts.append(block[0])
        current = ""
        for i in block:
            if scene_mood(scenes[i]):
                current = scene_mood(scenes[i])
            raw[i] = current or inferred[i]
    block_start = set(starts)
    # 同じ種類の続く区間に分け、短すぎる区間は前の区間の種類にする（まとまりの切れ目の切り替えは少し短くても効かせる）
    result = list(raw)
    i = 0
    while i < len(raw):
        j = i
        while j + 1 < len(raw) and raw[j + 1] == raw[i]:
            j += 1
        length = sum(durations[i:j + 1])
        if raw[i] == SILENCE:
            minimum = MIN_SILENCE_SECONDS
        elif i in block_start:
            minimum = MIN_BLOCK_MOOD_SECONDS
        else:
            minimum = MIN_MOOD_SECONDS
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


def assign_tracks(project: Project, moods: list[str], durations: list[float]) -> dict[str, Optional[str]]:
    """この動画で使う場面の種類ごとに、流す曲を決める（種類ごとに別の曲。長く流れる種類から先に一番合う曲を取る）。"""
    total: dict[str, float] = {}
    longest: dict[str, float] = {}
    run_mood, run_len = None, 0.0
    for mood, duration in zip(moods, durations):
        if not mood or mood == SILENCE:
            run_mood = None
            continue
        total[mood] = total.get(mood, 0.0) + duration
        run_len = run_len + duration if mood == run_mood else duration
        run_mood = mood
        longest[mood] = max(longest.get(mood, 0.0), run_len)
    chosen: dict[str, Optional[str]] = {}
    used: set[str] = set()
    for mood in sorted(total, key=lambda m: -total[m]):
        mine = user_choice(project, mood)
        if mine:
            chosen[mood] = None if mine == NO_BGM else mine
        else:
            chosen[mood] = best_track(mood, longest.get(mood, 0.0), used)
        if chosen[mood]:
            used.add(chosen[mood])
    return chosen


def scene_bgm_paths(project: Project, durations: Optional[list[float]] = None) -> list[Optional[str]]:
    """各シーンで流すBGMのファイル（None は無音）。何も設定しなくても、場面に合う曲が流れる。

    優先順: シーン個別の曲 → 台本の場面の種類（bgm）の曲 → 利用者が選んだ場面ごと・全体の曲（ファイルがある場合）
    → 推定した種類の曲。
    """
    scenes = project.scenes
    durations = durations or [s.duration for s in scenes]
    moods = effective_moods(project, durations)
    chosen = assign_tracks(project, moods, durations)
    explicit = uses_moods(project)
    paths: list[Optional[str]] = []
    for scene, mood in zip(scenes, moods):
        manual = _existing(scene.bgm_path)
        if manual:
            paths.append(None if manual == NO_BGM else manual)
            continue
        setting = None
        if not explicit or scene.section == "ending":  # 台本に場面の種類が書かれていれば、それを優先する
            phase_bgm = project.section_bgm.get(f"{scene.section}:{scene.phase}") if scene.phase else None
            for candidate in (phase_bgm, project.section_bgm.get(scene.section), project.bgm_path):
                setting = _existing(candidate)
                if setting:
                    break
        if setting:
            paths.append(None if setting == NO_BGM else setting)
        elif mood and mood != SILENCE:
            paths.append(chosen.get(mood))
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
