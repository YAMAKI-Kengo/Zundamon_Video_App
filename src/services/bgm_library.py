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

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.models import NO_BGM, Project, Scene

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
        ))
    return tracks


def track_for_path(path: str | Path) -> Track:
    key = library_key(path)
    for track in list_tracks():
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
    "新しい曲を置くと、曲ファイルのタグから曲名・作者を読んで自動で追記する（info_from に情報の出どころ）。"
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
        if key in library:
            continue
        entry = entry_from_tags(path, read_tags(path), legacy)
        entry["source"] = default_source
        library[key] = entry
        added.append(key)
    if added:
        save_library(library)
    return added


# ---------------------------------------------------------------------------
# シーンごとのBGMの決定
# ---------------------------------------------------------------------------

def _seed(project: Project) -> int:
    name = project.video_title or project.book_title or project.name or ""
    return int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16)


def auto_track(project: Project, mood: str, use_choice: bool = True) -> Optional[str]:
    """その気分で流す曲（利用者が選んだ曲 → 動画のタイトルから決まる曲）。気分のフォルダが空なら None。

    use_choice=False なら、利用者の選択を見ずに自動で選ぶ曲だけを返す（画面の「自動（曲名）」の表示用）。
    """
    chosen = project.mood_bgm.get(mood) if use_choice else None
    if chosen == NO_BGM:
        return None
    if chosen and Path(chosen).exists():
        return chosen
    tracks = list_tracks(mood)
    if not tracks:
        return None
    return str(tracks[(_seed(project) + MOOD_CHOICES.index(mood) * 7) % len(tracks)].path)


def uses_moods(project: Project) -> bool:
    return any(s.bgm_mood for s in project.scenes)


def effective_moods(project: Project, durations: Optional[list[float]] = None) -> list[str]:
    """各シーンで実際に流す雰囲気（指定が無いシーンは前の雰囲気を引き継ぐ。短すぎる区間は前のまま）。"""
    scenes = project.scenes
    durations = durations or [s.duration for s in scenes]
    raw: list[str] = []
    current = ""
    for scene in scenes:
        if scene.section == "ending":
            current = ""  # エンディングは場面ごとのBGM（従来の設定）に任せる
        elif scene.bgm_mood in MOOD_CHOICES:
            current = scene.bgm_mood
        raw.append(current)
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


def scene_bgm_paths(project: Project, durations: Optional[list[float]] = None) -> list[Optional[str]]:
    """各シーンで流すBGMのファイル（None は無音）。

    優先順: シーン個別のBGM（Scene.bgm_path）→ 台本の雰囲気（bgm）から選んだ曲 → 場面・プロジェクト全体の設定。
    """
    moods = effective_moods(project, durations) if uses_moods(project) else [""] * len(project.scenes)
    paths: list[Optional[str]] = []
    for scene, mood in zip(project.scenes, moods):
        if scene.bgm_path:
            paths.append(None if scene.bgm_path == NO_BGM else scene.bgm_path)
        elif mood == SILENCE:
            paths.append(None)
        elif mood:
            paths.append(auto_track(project, mood) or project.resolve_bgm_path(scene))
        else:
            paths.append(project.resolve_bgm_path(scene))
    return paths


def scene_bgm_path(project: Project, scene: Scene) -> Optional[str]:
    for s, path in zip(project.scenes, scene_bgm_paths(project)):
        if s is scene:
            return path
    return project.resolve_bgm_path(scene)


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
        if track.artist:
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
