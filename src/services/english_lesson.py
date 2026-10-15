"""
英会話レッスン動画（毎日更新・1週間ごとにテーマを変える）の台本づくりと、ネイティブ音声の管理。

番組の構成は、NHK「ラジオ英会話」（ダイアログ → 解説 → リピート → 練習 → 応用練習）と
「英会話タイムトライアル」（日本語を制限時間内に英語にする瞬発トレーニング・1週間で1テーマ）、
第二言語習得の研究でリスニング・発音・流暢さに効果があるとされるシャドーイング（短い音声を複数回まねる）を参考にしている:

  1〜6日目（各5〜8分）:
    intro   … ずんだもんが今日の場面で英語に困る・やらかす → めたんが今日のフレーズを予告（会話だけ）
    dialog  … ネイティブ音声の短い会話を聞く（英語の字幕＋日本語訳）
    phrase  … 今日のフレーズ2つを黒板で解説。ずんだもんがカタカナ英語でまねして、めたんが発音のコツを教える
    repeat  … ネイティブ音声 → 無音の間（視聴者がリピート）を1文ずつ
    quiz    … 日本語 → 無音の間（視聴者が英語で言う）→ ネイティブ音声で答え合わせ（瞬発トレーニング）
    summary … 今日のフレーズを黒板でおさらい → 明日の予告
  7日目（1週間のまとめ・8〜10分）:
    intro → review（1〜6日目のフレーズを瞬発クイズで総ざらい）→ dialog（今週のフレーズを使った会話）→ summary（来週の予告）

VOICEVOXは英語を読めないため、英語のセリフは次のどちらかにする:
  - めたんの英語（お手本）: 利用者が用意したネイティブ音声（1文 = 1ファイル）を使う。セリフにIDを振り
    （assign_audio_ids）、その名前で assets/english_audio/ に音声を置くと自動で紐付く（link_native_audio）。
    同じ英文には同じIDを使い回す（週をまたいでも、manifest.json で英文からIDを引く）。
  - ずんだもんの英語（まねしてみる役）: 字幕は英語のまま、読み（reading）をカタカナにしてVOICEVOXに読ませる。
  日本語のセリフに英単語が混ざる場合も、reading で英単語の部分をカタカナにする。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from src.models import GUEST_CHARACTERS, Scene
from src.services import bgm_library, book_ai, video_history
from src.services.book_ai import _STR, _STR_LIST, AIResult, ProgressCallback, _call, _obj
from src.services.voicevox_client import DEFAULT_SPEECH_SPEED
from src.utils.asset_loader import get_available_expressions, list_illustrations, list_se, place_background_schema

ROOT = Path(__file__).resolve().parents[2]
AUDIO_DIR = ROOT / "assets" / "english_audio"       # ネイティブ音声（<ID>.mp3 など）を置くフォルダ
MANIFEST_PATH = AUDIO_DIR / "manifest.json"          # ID → 英文 の対応表（同じ英文のIDを使い回すため）
WEEKS_DIR = ROOT / "lessons"                         # 1週間の計画（week_01.json など）を保存するフォルダ
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac")
NATIVE_SPEAKER = "shikoku_metan"                     # ネイティブ音声で英語を話すキャラクター
LEARNER_SPEAKER = "zundamon"                         # カタカナ英語でまねする役
REVIEW_DAY = 7                                       # 1週間のまとめの日
LESSON_DAYS = range(1, REVIEW_DAY)                   # 通常のレッスンの日（1〜6日目）
LEVELS = {
    "a2": "中学英語〜初級（CEFR A2）",
    "a1": "入門（あいさつ・単語レベル）",
    "b1": "中級（CEFR B1）",
}
LEVEL_RULES = {
    "a1": "1文は3〜7語。中学1年生までの単語と文法（be動詞・一般動詞の現在形・命令文・Can I 〜?）だけを使う。",
    "a2": "1文は5〜12語。中学英語の範囲の単語と文法を中心に、日常会話の決まり文句を使う。",
    "b1": "1文は8〜16語。日常会話で自然に使う句動詞・慣用表現も使ってよい。",
}
VOICE_LABELS = {"A": "声A（めたん役・女性）", "B": "声B（会話の相手役）"}
LINE_GAP_SECONDS = 0.35  # ネイティブ音声のあとに置く間（学習用に日本語のセリフより少し長め）
SHORT_LINE_GAP_SECONDS = 0.1  # ショートでは間を詰める（最初の数秒でスワイプされないように）


def native_gap(project) -> float:
    """ネイティブ音声のあとに置く間（ショートは短く）。"""
    return SHORT_LINE_GAP_SECONDS if (project.promo_of or project.video_style == "short") else LINE_GAP_SECONDS


# ---------------------------------------------------------------------------
# ネイティブ音声のIDと紐付け
# ---------------------------------------------------------------------------

def _normalize_english(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def load_manifest() -> dict[str, dict]:
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_manifest(manifest: dict[str, dict]) -> None:
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def needs_native_audio(line: dict) -> bool:
    """ネイティブ音声を使う英語のセリフか（英語で、カタカナの読みが付いていないもの）。"""
    return str(line.get("lang") or "").lower() == "en" and not str(line.get("reading") or "").strip() \
        and bool(str(line.get("text") or "").strip())


def assign_audio_ids(data: dict, week: int, day: int) -> list[str]:
    """台本JSONの、ネイティブ音声を使う英語のセリフに音声ID（例: w01d3_05）を振る（data を書き換える）。

    すでに同じ英文にIDがあれば（前の日・前の週を含めて）それを使い回し、音声を作り直さなくて済むようにする。
    振ったIDの一覧（重複なし・登場順）を返す。
    """
    manifest = load_manifest()
    by_text = {_normalize_english(v.get("text", "")): k for k, v in manifest.items()}
    prefix = f"w{int(week):02d}d{int(day)}_"
    used_numbers = {int(k[len(prefix):]) for k in manifest if k.startswith(prefix) and k[len(prefix):].isdigit()}
    next_no = max(used_numbers, default=0) + 1
    ids: list[str] = []
    for block in data.get("blocks") or []:
        for line in (block.get("lines") or []) if isinstance(block, dict) else []:
            if not isinstance(line, dict) or not needs_native_audio(line):
                continue
            text = str(line["text"]).strip()
            key = _normalize_english(text)
            audio_id = by_text.get(key)
            if audio_id is None:
                audio_id = f"{prefix}{next_no:02d}"
                next_no += 1
                manifest[audio_id] = {"text": text, "voice": str(line.get("voice") or "A").upper()[:1] or "A"}
                by_text[key] = audio_id
            line["audio"] = audio_id
            if audio_id not in ids:
                ids.append(audio_id)
    _save_manifest(manifest)
    return ids


def find_audio_file(audio_id: str) -> Optional[Path]:
    if not audio_id:
        return None
    for ext in AUDIO_EXTENSIONS:
        path = AUDIO_DIR / f"{audio_id}{ext}"
        if path.exists():
            return path
    return None


_duration_cache: dict[tuple[str, float], float] = {}


def audio_duration(path: Path) -> float:
    key = (str(path), path.stat().st_mtime)
    if key not in _duration_cache:
        from moviepy import AudioFileClip

        clip = AudioFileClip(str(path))
        try:
            _duration_cache[key] = float(clip.duration or 0.0)
        finally:
            clip.close()
    return _duration_cache[key]


def link_native_audio(scenes: list[Scene], gap: float = LINE_GAP_SECONDS) -> list[Scene]:
    """音声IDのあるシーンに、assets/english_audio/ に置かれたネイティブ音声を紐付け、表示秒数を音声の長さに合わせる。

    新しく紐付けた（または音声ファイルが差し替えられた）シーンのリストを返す。すでに紐付いていて変わっていない
    シーンは、利用者が手で変えた表示秒数を上書きしないよう、何もしない。
    """
    linked = []
    for scene in scenes:
        if not scene.audio_id or scene.silent:
            continue
        path = find_audio_file(scene.audio_id)
        if path is None:
            continue
        stamp = f"{path}|{path.stat().st_mtime}"
        if scene.voice_path == str(path) and _linked_stamps.get(scene.id) in (None, stamp):
            _linked_stamps.setdefault(scene.id, stamp)
            continue
        scene.voice_path = str(path)
        _linked_stamps[scene.id] = stamp
        try:
            scene.duration = round(scene.lead_in + audio_duration(path) + gap + scene.board_hold, 2)
        except Exception:  # noqa: BLE001 - 長さを測れない音声は、秒数を変えずに使う
            pass
        linked.append(scene)
    return linked


_linked_stamps: dict[str, str] = {}  # シーンID → 紐付けた音声ファイル（パスと更新日時。差し替えの検出用）


def native_audio_items(scenes: list[Scene]) -> list[dict]:
    """動画で使うネイティブ音声の一覧（登場順・重複なし）: {"id", "text", "voice", "tts_voice", "ready", "path"}。

    tts_voice は、読み上げAIで作った音声ならその声の名前（手作業で置いた音声・まだ無い音声は空文字）。
    """
    manifest = load_manifest()
    items: dict[str, dict] = {}
    for scene in scenes:
        if not scene.audio_id or scene.audio_id in items:
            continue
        path = find_audio_file(scene.audio_id)
        items[scene.audio_id] = {
            "id": scene.audio_id,
            "text": scene.text,
            "voice": manifest.get(scene.audio_id, {}).get("voice", "A"),
            "tts_voice": (manifest.get(scene.audio_id, {}).get("tts") or {}).get("voice", "") if path else "",
            "ready": path is not None,
            "path": str(path) if path else "",
        }
    return list(items.values())


def promo_audio_items(project) -> list[dict]:
    """本編紹介ショートの台本（project.promo_short）にある英語の音声の一覧（native_audio_items と同じ形）。

    ショートの英文は、本編と同じ英文なら同じ音声を使い回すが、ショートにしか無い英文もある。ショートはまだ
    シーンになっていないことがあるので、台本の音声IDから一覧を作る。
    """
    manifest = load_manifest()
    items: dict[str, dict] = {}
    for block in (project.promo_short or {}).get("blocks") or []:
        for line in (block.get("lines") or []) if isinstance(block, dict) else []:
            audio_id = str((line or {}).get("audio") or "").strip() if isinstance(line, dict) else ""
            if not audio_id or audio_id in items:
                continue
            path = find_audio_file(audio_id)
            entry = manifest.get(audio_id, {})
            items[audio_id] = {
                "id": audio_id,
                "text": entry.get("text") or str(line.get("text") or ""),
                "voice": entry.get("voice", str(line.get("voice") or "A").upper()[:1] or "A"),
                "tts_voice": (entry.get("tts") or {}).get("voice", "") if path else "",
                "ready": path is not None,
                "path": str(path) if path else "",
            }
    return list(items.values())


def lesson_audio_items(project) -> list[dict]:
    """本編と本編紹介ショートで使う英語の音声の一覧（重複なし。本編の登場順 → ショートだけの英文）。"""
    items = native_audio_items(project.scenes)
    known = {i["id"] for i in items}
    return items + [i for i in promo_audio_items(project) if i["id"] not in known]


def missing_audio_count(scenes: list[Scene]) -> int:
    """ネイティブ音声を使う英語のセリフのうち、まだ音声が紐付いていないものの数。"""
    return sum(1 for s in scenes if s.audio_id and not s.silent and not s.voice_path)


def audio_script_text(items: list[dict], only_missing: bool = True) -> str:
    """音声を作るための一覧（1行 = 「ファイル名 ⇥ 声 ⇥ 英文」）。音声サービスに英文を貼るときや、保存名の確認に使う。"""
    rows = [i for i in items if not (only_missing and i["ready"])]
    return "\n".join(f"{i['id']}.mp3\t声{i['voice']}\t{i['text']}" for i in rows)


# ---------------------------------------------------------------------------
# 1週間の計画
# ---------------------------------------------------------------------------

WEEK_PLAN_SCHEMA = _obj({
    "theme": _STR,        # 今週のテーマ（日本語。例: カフェ・レストランで使う英語）
    "theme_en": _STR,     # 英語のテーマ名（例: At a Cafe）
    "goal": _STR,         # 1週間で何ができるようになるか
    "days": {"type": "array", "items": _obj({
        "day": {"type": "integer"},
        "title": _STR,        # その日のタイトル（例: 注文するときの Can I get 〜?）
        "situation": _STR,    # 会話の場面
        "zunda_trouble": _STR,  # 導入でずんだもんが困る・やらかすこと
        "phrases": {"type": "array", "items": _obj({"en": _STR, "ja": _STR, "point": _STR})},
        "grammar": _STR,      # その日に触れる文法・発音のポイント
    })},
    "next_theme_idea": _STR,  # 来週のテーマの案（7日目の予告に使う）
})


def _week_plan_system(level: str) -> str:
    return f"""あなたは、日本人の英語学習者向けに英会話番組（NHK「ラジオ英会話」「英会話タイムトライアル」のような毎日の短い講座）を作ってきた英語教育の専門家です。YouTubeで毎日更新する、ずんだもんと四国めたんの英会話レッスン動画の「1週間の計画」を作ります。

## 視聴者のレベル
{LEVELS.get(level, LEVELS['a2'])}。{LEVEL_RULES.get(level, LEVEL_RULES['a2'])}

{book_ai.ENGLISH_POLICY}

## 計画の作り方
- シリーズの軸（下の「シリーズの軸」があればそれに沿って、週テーマを旅の進行などの連続した物語にする）。
- 1週間で1つのテーマにする。テーマは文法（現在完了など）ではなく、英語の外側の悩み・欲求から入る「場面と感情」で決める（例: 海外の推しに一言コメントする、空港で固まらない、海外旅行のホテルで困らない、子どもに聞かれた英語に答える、洋画のよくあるセリフを聞き取る、カフェで注文する）。週ごとに、推し活・旅行・親子・日常・仕事などの入口を変えて、いろいろな人が「自分のことだ」と思えるようにする。1〜6日目が毎日のレッスン、7日目は1週間のまとめ（days には1〜6日目だけを書く）。
- 1〜6日目は、同じテーマの中で場面を少しずつ変え、易しいものから順に並べる。前の日のフレーズを後の日の会話でも使えるようにつなげる（くり返し出会うことで定着する）。
- 各日の phrases は2つ。実際の会話でよく使う、短くて応用のきく決まり文句にする（en は英語、ja は自然な日本語訳、point は使い方・言い換え・似た表現との違いを1文で）。
- 各日の title は、その日の動画のタイトル・ショートのフックの元になる。初めて見る人にも一瞬で何の話か分かり、英語を話したい人の多くが「自分のことだ」と思う言葉にする。「正しい表現」を名前にするのではなく、損失回避（「〜はNG」「実は失礼」「言いがちな間違い」「知らないと損」）か、多くの人が感じる気持ち（「聞き取れない」「焦る」「固まる」「沈黙が怖い」）の言葉で書く（例:「What?で聞き返すのは実は失礼」「沈黙が怖くて"えーと"連発してない？」「英語が出てこない時の一言」）。細かい場面（空港の〇〇・ホテルの〇〇）や作中のカタカナ英語を見出しにしない。
- 教科書的すぎる表現や古い表現は避け、今のネイティブが日常で使う自然なアメリカ英語にする。
- zunda_trouble には、その日の場面で日本人がやりがちな失敗（直訳・カタカナ発音・丁寧すぎる/失礼な言い方など）を、ずんだもんがやらかす小さなエピソードとして1文で書く。
- grammar には、その日に触れる文法か発音（音のつながり・弱く読む音など）のポイントを1つ書く。
- next_theme_idea には、来週のテーマの案を1つ書く。
- 以前の週で扱ったテーマと重ならないようにする。"""


def _series_for_plan(week: int) -> str:
    """1週間の計画づくりに渡す、シリーズの軸（連続ドラマ）。"""
    arc = video_history.load_channel()["english"].get("arc", "").strip()
    if not arc:
        return ""
    return (f"## シリーズの軸\n「{arc}」というずんだもんの物語を、週ごとに少しずつ進める（今週は第{week}週）。"
            "週テーマは、この物語の進行に沿った場面にする（例: 出発前の準備 → 空港 → 機内 → 入国審査 → ホテル → レストラン → "
            "買い物 → 観光 → トラブル → 現地の人と仲良くなる → 帰国）。以前の週の続きになるようにし、シリーズの最後は本当に旅行に行く回にする。")


def plan_week(week: int, theme_hint: str = "", level: str = "a2", previous_themes: Optional[list[str]] = None,
              progress: Optional[ProgressCallback] = None) -> AIResult:
    """1週間分（1〜6日目のレッスン＋7日目のまとめ）の計画を作る。"""
    instruction = "\n".join(filter(None, [
        f"第{week}週の計画を作ってください。",
        _series_for_plan(week),
        f"今週のテーマ: {theme_hint.strip()}" if theme_hint.strip() else "テーマはおまかせします（最初の週なら、初心者が一番使う場面から）。",
        ("以前の週のテーマ（重ならないように）: " + "、".join(previous_themes)) if previous_themes else "",
    ]))
    result = _call(_week_plan_system(level), [{"type": "text", "text": instruction}], WEEK_PLAN_SCHEMA,
                   8000, progress or (lambda _m: None))
    result.data["week"] = int(week)
    result.data["level"] = level
    return result


def week_plan_manual_prompt(week: int, theme_hint: str = "", level: str = "a2",
                            previous_themes: Optional[list[str]] = None) -> str:
    example = {
        "theme": "カフェで使う英語", "theme_en": "At a Cafe", "goal": "カフェで注文・会計・席の確保ができる",
        "days": [{"day": 1, "title": "注文の基本 Can I get 〜?", "situation": "カフェのレジで注文する",
                  "zunda_trouble": "「コーヒー、プリーズ」だけで押し通して、サイズを聞かれて固まる",
                  "phrases": [{"en": "Can I get a coffee?", "ja": "コーヒーをもらえますか？", "point": "Can I have 〜? より気軽"}],
                  "grammar": "Can I は「キャナイ」とつながる"}],
        "next_theme_idea": "道をたずねる英語",
    }
    return "\n\n".join(filter(None, [
        _week_plan_system(level),
        f"第{week}週の計画を作ってください。" + (f"今週のテーマ: {theme_hint.strip()}" if theme_hint.strip() else ""),
        _series_for_plan(week),
        ("以前の週のテーマ（重ならないように）: " + "、".join(previous_themes)) if previous_themes else "",
        "出力は次の形式のJSONだけにしてください（days は1〜6日目の6つ）:",
        "```json\n" + json.dumps(example, ensure_ascii=False, indent=2) + "\n```",
    ]))


def week_plan_path(week: int) -> Path:
    return WEEKS_DIR / f"week_{int(week):02d}.json"


def save_week_plan(plan: dict) -> Path:
    WEEKS_DIR.mkdir(parents=True, exist_ok=True)
    path = week_plan_path(int(plan.get("week") or 1))
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_week_plan(week: int) -> Optional[dict]:
    try:
        return json.loads(week_plan_path(week).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def list_week_plans() -> list[dict]:
    plans = []
    for path in sorted(WEEKS_DIR.glob("week_*.json")) if WEEKS_DIR.exists() else []:
        try:
            plans.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return plans


def previous_themes(week: int) -> list[str]:
    return [p.get("theme", "") for p in list_week_plans() if int(p.get("week") or 0) != int(week) and p.get("theme")]


# ---------------------------------------------------------------------------
# 毎日の台本
# ---------------------------------------------------------------------------

LESSON_SECTIONS_ENUM = ["intro", "dialog", "phrase", "repeat", "quiz", "review", "summary"]


def _bgm_choices() -> str:
    """台本の bgm に書ける場面の種類の一覧（AIへの説明）。"""
    return "\n".join(f"  - {key}: {label}" for key, label in bgm_library.MOODS.items()) + \
        "\n  - none: 無音（ショックの一瞬だけ。次のセリフで別の種類に戻す）"


def _lesson_schema(with_promo: bool = True) -> dict:
    characters = book_ai._characters()
    expressions = sorted({e for c in characters for e in get_available_expressions(c)})
    line = _obj({
        "speaker": {"type": "string", "enum": characters},
        "expression": {"type": "string", "enum": expressions},
        "text": _STR,
        "lang": {"type": "string", "enum": ["ja", "en"]},
        "ja": _STR,
        "reading": _STR,
        "voice": {"type": "string", "enum": ["A", "B"]},
        "pause": {"type": "number"},
        "pause_text": _STR,
        "pause_style": {"type": "string", "enum": ["", "repeat", "shadow", "think"]},
        "mood": {"type": "string", "enum": ["", "gloomy", "shock", "dark", "sepia", "bright"]},
        "card": _STR,
        "se": {"type": "string", "enum": [""] + [p.stem for p in list_se()]},
        "motion": {"type": "string", "enum": ["auto", "panic", "hop", "jump", "recoil", "shiver", "slump", "lunge", "tsukkomi", "nod"]},
        "hide": {"type": "array", "items": {"type": "string", "enum": characters}},
        "board": {"type": "boolean"},
        "bullet": {"type": "integer"},
        "note": _obj({"text": _STR, "focus": _STR, "meaning": _STR}),
        "image": {"type": "string", "enum": [""] + [p.stem for p in list_illustrations()]},
        "caption": _STR,
        "image_request": _STR,
        "image_name": _STR,
        "bgm": {"type": "string", "enum": [""] + bgm_library.MOOD_CHOICES},
    })
    guest_keys = [c for c in characters if c in GUEST_CHARACTERS]
    block = _obj({
        "section": {"type": "string", "enum": LESSON_SECTIONS_ENUM},
        "bgm": {"type": "string", "enum": bgm_library.MOOD_CHOICES},
        "slide": _obj({"title": _STR, "bullets": _STR_LIST, "numbered": {"type": "boolean"}}),
        "background": place_background_schema(),
        "background_request": _STR,
        "background_name": _STR,
        **({"guests": {"type": "array", "items": {"type": "string", "enum": guest_keys}}} if guest_keys else {}),
        "lines": {"type": "array", "items": line},
    })
    return _obj({
        "book_title": _STR,
        "title_candidates": _STR_LIST,
        "video_title": _STR,
        "description_lead": _STR,
        "pinned_comment": _STR,
        "hashtags": _STR_LIST,
        "tags": _STR_LIST,
        "readings": {"type": "array", "items": _obj({"word": _STR, "reading": _STR})},
        "phrases": {"type": "array", "items": _obj({"en": _STR, "ja": _STR})},
        "thumbnail": _obj({"text": _STR, "sub": _STR, "shout": _STR, "layout": {"type": "string", "enum": ["before_after", "scene", "reaction", "duo", "big_text"]}, "zundamon": {"type": "string", "enum": expressions}, "metan": {"type": "string", "enum": expressions}, "before": _STR, "after": _STR, "before_face": {"type": "string", "enum": expressions}, "after_face": {"type": "string", "enum": expressions}, "before_shout": _STR, "after_shout": _STR, "scene": _STR, "phrase": _STR, "accent": {"type": "string", "enum": ["auto", "trouble", "town", "cafe", "solution"]}, "visual": _STR}),
        "blocks": {"type": "array", "items": block},
        **({"promo_short": book_ai.promo_short_schema(block)} if with_promo else {}),
    })


_LESSON_CHARACTER_ROLES = """
## この動画での2人の役（上のキャラクター設定に加えて）
- 四国めたん: 英語の先生役。英語はネイティブ並みに話せる（めたんの英語のセリフは、ネイティブの録音音声で再生される）。日本語の解説は、いつものお嬢様言葉のタメ口で。
- ずんだもん: 英語が苦手な生徒役。視聴者の代わりに間違え、カタカナ英語でまねしてみては、めたんにツッコまれる。英語のセリフでも、日本語のセリフでは必ず「〜のだ」で話す。"""


def _english_line_rules(level: str) -> str:
    return f"""## 英語のセリフの書き方（最重要）
- 英語のセリフは lang を "en" にし、text に英文、ja に自然な日本語訳を書く（字幕は英文の下に訳が小さく出る）。日本語のセリフは lang を "ja"、ja は空文字。
- 英語のレベル: {LEVELS.get(level, LEVELS['a2'])}。{LEVEL_RULES.get(level, LEVEL_RULES['a2'])} 今のネイティブが日常で使う、自然なアメリカ英語にする。1つのセリフに英文は1〜2文まで。
- 字幕はセリフの text がそのまま出る。ずんだもんが英語を言うセリフ（まねする・答える・言い間違える）は、必ず lang を "en"、text を英語のつづり（例: "Can I get a coffee?"）にし、カタカナの読みは reading にだけ書く。text をカタカナで書かない。日本語のセリフに英語が混ざる場合も、text の英語の部分は英語のつづりで書く（例: text「Can I get は注文の定番なのだ！」、reading「キャナイゲットは注文の定番なのだ！」）。
- お手本の英語（ネイティブ音声）: speaker は "shikoku_metan"、reading は空文字にする（録音したネイティブ音声を使う）。voice は、会話（dialog）の2人の役を "A"（めたんの役）と "B"（相手の役）で書き分け、それ以外は "A"。{" 登場人物に春日部つむぎ（kasukabe_tsumugi）がいる場合は、voice B の英語のセリフの speaker を kasukabe_tsumugi にし、dialog ブロックの guests に kasukabe_tsumugi を入れる（めたんとつむぎが英語で会話する）。" if "kasukabe_tsumugi" in book_ai._characters() else ""}
- ずんだもんがまねする英語: speaker は "zundamon"、text は英文のまま、reading に、日本人がやりがちなカタカナ英語の読み（例: text "Can I get a coffee?" → reading "キャン アイ ゲット ア コーヒー"）を書く（音声合成は英語を読めないため、読みをカタカナにする）。ここで、めたんが発音のコツ（音のつながり・弱く読む音・アクセント）を日本語で教える流れを作る。
- 日本語のセリフに英単語・英文を混ぜる場合（例:「Can I は、キャナイってつながるのよ」）は、reading にセリフ全体を書き、英語の部分だけを正しい発音に近いカタカナにする（例: reading「キャナイは、キャナイってつながるのよ」）。英語を混ぜない日本語のセリフは reading を空文字にする。
- pause_style は間の見せ方: "repeat"（3・2・1 → リピート！）、"shadow"（3・2・1 → 同じネイティブ音声をもう一度流す）、"think"（残り秒数のタイマー）。間の無い行は空文字。
- pause（秒）は、セリフのあとに視聴者がリピート・回答する無音の間。リピート練習のお手本の英語は 3〜4、瞬発トレーニングの日本語の出題は 4〜5（英語で言う時間）、それ以外は 0。pause_text は、その間に画面上部に大きく出す指示（例:「リピート！」「英語で言ってみよう！」）。pause が 0 のときは空文字。
- 絵文字・丸数字（①②）・矢印（→）・♪★ は動画のフォントに無いので使わない（数字は 1, 2 と書く）。"""


def _lesson_structure(day: int, plan: dict, target_minutes: float) -> str:
    if day >= REVIEW_DAY:
        return f"""## 構成（7日目: 1週間のまとめ・約{target_minutes:g}分。blocks はこの順）
1. intro（会話だけ・20〜30秒）: 今週のテーマ「{plan.get('theme', '')}」の1週間をふりかえる日だと伝える。ずんだもんが「全部覚えたのだ！」と調子に乗り、めたんが「じゃあテストしてあげるわ」と返す。
2. review（1〜6日目ごとに1ブロック、計6ブロック）: 各日のフレーズで瞬発クイズ。めたんが日本語で出題（pause 4〜5、pause_text「英語で言ってみよう！」）→ めたんの英語で答え（ネイティブ音声）→ ずんだもんのリアクション（正解/間違いの小ネタ）。slide は title を「Day1：〇〇」、bullets にその日のフレーズ（「英語 ― 日本語」の形）を書き、答え合わせのセリフから board を true にする。
3. dialog（今週のフレーズを4つ以上使った、少し長めの会話・6〜8行）: 前置きのセリフ（日本語）→ ネイティブ音声の会話 → ずんだもんの感想。
4. summary: slide の title は「今週のフレーズ」、bullets に今週のフレーズを6〜8個（「英語 ― 日本語」）。今週できるようになったことを具体的にほめ（「1週間で〇個のフレーズが言えるようになった」）、来週のテーマ（{plan.get('next_theme_idea', '')}）を予告する。最後のセリフは、ずんだもんの「今週のフレーズ、ぜんぶ言えたのだ！」で締める。"""
    today = next((d for d in plan.get("days", []) if int(d.get("day") or 0) == day), {})
    tomorrow = next((d for d in plan.get("days", []) if int(d.get("day") or 0) == day + 1), {})
    return f"""## 構成（{day}日目・約{target_minutes:g}分。blocks はこの順）
1. intro（会話だけ・20〜40秒）: 1セリフ目は、タイトルの場面で使う日本語を「『〇〇』って英語で何て言う？」と問いかける（答えはまだ言わない。最初の5秒でタイトルの約束に入る）→ すぐにずんだもんが自信満々に間違った英語を言う（直訳・和製英語など。lang "en"・reading にカタカナ。今日の失敗: {today.get('zunda_trouble', '')}）→ めたんが「その英語だと、ネイティブにはこう聞こえてるのよ」と、どう聞こえるかを面白く説明する（ここで笑いと「正解が知りたい」を同時に作る）→「今週のテーマ『{plan.get('theme', '')}』の{day}日目」と、前の日から続く旅の場面を一言 → めたんが「今日は〇〇で使える一言を教えてあげるわ」と予告する。
2. dialog（ダイアログ）: めたんの前置き（日本語・「まずは会話を聞いてみて」）→ 場面（{today.get('situation', '')}）のネイティブ音声の会話 4〜6行（voice A/B の2役。今日のフレーズ2つを必ず使う）→ ずんだもんの「速すぎて分からないのだ…」のような一言。会話の最初のセリフに、場面のイラスト（image か image_request）を出し、会話の英語のセリフにはすべて同じイラストを付ける（英語の会話が続いている間はずっとイラストを出したままにする。アプリも自動で出したままにする）。
3. phrase（今日のフレーズ1・2で2ブロック）: 各ブロックの slide は title を「今日のフレーズ1」「今日のフレーズ2」、bullets は「英語のフレーズ」「意味」「使い方のポイント」「言い換え・応用」の順に3〜4個。流れ: めたんがフレーズを英語で言う（ネイティブ音声）→ 日本語で意味と使い方を解説 → 例文をもう1つ英語で（ネイティブ音声）→ ずんだもんがカタカナ英語でまねする（reading にカタカナ）→ めたんが発音のコツ（{today.get('grammar', '')}など）をツッコミながら教える → ずんだもんがもう一度言って少し上手くなる。
4. repeat（リピート練習・1ブロック）: めたん「音声のあとに、3・2・1の合図で言ってみて」→ 今日のフレーズ2つと会話の中の大事な文を合わせて3〜4文、ネイティブ音声の英語を1文ずつ（pause 3〜4、pause_style "repeat"、pause_text「リピート！」。アプリが音声のあとに「3・2・1 → リピート！」の合図を出す）→ めたん「次は音声に重ねて言ってみて」→ シャドーイングを1〜2文（その英文の行の pause_style を "shadow"、pause を 0、pause_text を「一緒に言ってみよう！」。アプリが「3・2・1」のあと同じ音声をもう一度流すので、視聴者は音声と一緒に言う）。
5. quiz（瞬発トレーニング・1ブロック・3〜4問）: めたん「日本語を見て、すぐ英語で言ってみて」→ 問題ごとに、めたんが日本語の文を出題（「『〇〇』を英語で言うと？」。pause 4〜5、pause_style "think"、pause_text「英語で言ってみよう！」。残り秒数のタイマーが出る）→ めたんの英語で答え（ネイティブ音声）。今日のフレーズを少し言い換えて使う問題にする（例: 名詞を入れ替える）。ずんだもんの回答の小ネタを1回入れてよい。
6. summary: slide の title は「今日のまとめ」、bullets に今日のフレーズ2つ（「英語 ― 日本語」）。めたんが今日のフレーズを英語でもう一度言う（ネイティブ音声）→ ずんだもんが今日の場面にリベンジして、今日のフレーズを言えるようになる → めたんが視聴者にも「あなたも言えたでしょ？」と、今日できるようになったことを実感させる → めたんが「今日のフレーズを使って、コメントで英語を1文書いてみてね」と呼びかける（書くこと自体がアウトプットの練習になる）→ 明日の予告（{tomorrow.get('title', '明日のレッスン')}）→ 最後のセリフは必ず、ずんだもんの「今日の1フレーズ、言えたのだ！」（この言葉で終える型は毎回同じ）。
エンディング（ご視聴ありがとうございました等）はアプリが自動で付けるので書かない。"""


def _lesson_system(plan: dict, day: int, level: str, speech_speed: float = DEFAULT_SPEECH_SPEED) -> str:
    target_minutes = 9 if day >= REVIEW_DAY else 6
    today = next((d for d in plan.get("days", []) if int(d.get("day") or 0) == day), {})
    week_phrases = [f"{p.get('en')}（{p.get('ja')}）" for d in plan.get("days", []) for p in d.get("phrases", [])]
    today_phrases = [f"{p.get('en')}（{p.get('ja')}。{p.get('point', '')}）" for p in today.get("phrases", [])]
    return f"""あなたは、NHKの英会話番組のように分かりやすく、YouTubeで毎日見たくなる英会話レッスン動画の構成作家です。ずんだもんと四国めたんの掛け合いで、約{target_minutes}分（エンディング除く）の横長動画の台本を書きます。
視聴者が「今日のフレーズを実際に口に出して言えるようになる」ことがゴールです。聞く → 意味と使い方が分かる → まねする → 自分で言う、の順に練習させます。

{book_ai._CHARACTERS_TEXT}
{_LESSON_CHARACTER_ROLES}
{book_ai._guest_text("english")}

{book_ai.CHANNEL_CORE}

{book_ai.ENGLISH_POLICY}

## 今週の計画
- 第{plan.get('week', 1)}週のテーマ: {plan.get('theme', '')}（{plan.get('theme_en', '')}）。1週間のゴール: {plan.get('goal', '')}
- 今週のフレーズ: {' / '.join(week_phrases)}
{f"- 今日（{day}日目）: {today.get('title', '')}。場面: {today.get('situation', '')}。今日のフレーズ: {' / '.join(today_phrases)}" if today else ""}

{_lesson_structure(day, plan, target_minutes)}

{_english_line_rules(level)}

## テンポ・画面
- 日本語のセリフは10〜30字（字幕1行＝30字）。同じ話者が3セリフ以上続かない。ずんだもんのボケとめたんのツッコミで、1ブロックに2回以上は笑いどころを作る（楽しく学べることが一番大事）。フレーズの使い方は、思わず笑う面白いたとえや場面で説明する（例:「Could you slow down? は、早口の先生にブレーキを踏んでもらう魔法の言葉よ」）。ずんだもんが英語で大げさにやらかす面白シーン（間違った英語で店員さんを困らせる・勢いで変な注文をしてしまう など）を1本に2回以上入れる。
- 画面の基本は2人の会話だけ（board は false、image は空文字）。黒板（board を true）は phrase・review・summary でフレーズを紹介するセリフから出し、そのブロックの最後まで出したままにする（アプリも自動で出したままにする）。黒板を出している間は、黒板の1行ごとに2〜3セリフかけて、意味・使い方・例文をじっくり話す。黒板の bullets は、話す順に1行ずつ書き足される。slide の numbered は、手順や順番があるときだけ true。
- mood（背景の雰囲気）は、ずんだもんが英語で失敗して落ち込みがピークになる場面だけ "gloomy"（ショックの瞬間なら "shock"）にし、その場面の2〜4セリフに続けて付ける（1セリフだけ付けない）。動画の最初のセリフには付けない。1本で1〜2場面まで。それ以外は空文字。card（場面転換テロップ。例:「その日の夜…」）は導入で時間が飛ぶときだけ、text を空文字にした独立した行で使い、それ以外は空文字。
- bgm（BGMを流す場面の種類。アプリが、その種類の場面に一番合う曲を選んで流す。種類は次のとおり）:
{_bgm_choices()}
  - ブロックの bgm（必ず書く）: そのブロックの基本の種類。目安: 導入（intro）は opening（ずんだもんが外で英語に困る場面から始めるなら trouble）、
    会話（dialog）は cheerful、フレーズ解説（phrase）・リピート（repeat）・ふりかえり（review）は talk、瞬発トレーニング（quiz）は cheerful、まとめ（summary）は ending。
    ブロックが変わると、BGMはそのブロックの種類に戻る。
  - セリフの bgm: ブロックの途中で空気がはっきり変わるセリフにだけ書く（そのセリフからブロックの終わりまでその種類）。
    例: ずんだもんが英語で大失敗 → fail、焦って固まる → trouble、言えた！と大喜び → success、ボケとツッコミの応酬 → gag。それ以外のセリフは空文字。
  - 曲はブロックの途中では6秒以上続かないと切り替わらないので、1〜2セリフごとに変えない。英語のお手本（ネイティブ音声）を聞かせる間は、落ち着いた種類（talk・cheerful）にする。
- note（重要な表現の解説カード）: phrase ブロックで、今日のフレーズの大事な部分（例: Can I get の部分）を解説するセリフに使う（フレーズごとに1回、1本で2〜4回）。text は英文、meaning は日本語。画面に文が大きく出て、focus の部分に赤い下線が引かれ、矢印の先に meaning が表示される。
  text に文（25字・8語程度まで）、focus に赤線を引く部分（text の中にそのまま含まれる語句）、meaning にその部分の意味・使い方（25字以内。例:「〜をもらえる？ お店で注文するときの定番」）を書く。
  その部分を説明する2〜3セリフに、同じ note を続けて付ける（その間は黒板の代わりに解説カードが出る）。使わない行は text・focus・meaning をすべて空文字にする。
- bullet（黒板のどの行の話か）: 黒板の箇条書きの行を初めて話すセリフに、その行の番号（1から）を書く（その番号の行が、そのセリフで黒板に書き足される）。それ以外のセリフは 0。瞬発トレーニング・ふりかえり（quiz・review）で黒板に答えを書く場合は、答えを言う英語のセリフにその行の番号を付ける（問題を出すセリフには付けない。先に答えが見えてしまうため）。行の番号は、話す順番どおりに1, 2, 3…と増えるようにする。まとめ（summary）は全部の行を最初から出すので、すべて 0 でよい。
{book_ai._background_rules().replace("回想・寸劇・たとえ話などで、学校・職場・お店・駅・病院など部屋以外の場所の出来事を「その場面として見せる」ブロックだけ", "導入でずんだもんが外で英語に困る場面や、dialog の会話の場所（カフェ・空港・お店など）を見せるブロックだけ").replace("（例: ずんだもんの会社での失敗を再現する寸劇、学生時代の回想）", "（例: 空港で固まるずんだもん、カフェでの注文の会話）")}
- 表情（expression）は、セリフの感情に合うものを次の一覧から選ぶ:
{book_ai._character_guide()}
- se（効果音）は、つかみ・正解・ツッコミ・オチなど3〜8回だけ。使えるのは次の名前のみ（使わないときは空文字）: {book_ai._se_guide()}
- hide は空の配列。イラスト（image・caption・image_request・image_name）は、dialog の最初のセリフなど場面が伝わるところだけに出し、1本で2〜4枚までにする（出すセリフは board を false）。手元にあるイラスト: {', '.join(p.stem for p in list_illustrations()) or '（なし。image_request で依頼する）'}。合うものが無ければ、image_request に欲しいイラスト（検索語付き）、image_name に保存名（日本語4〜12字）を書く。caption は空文字（説明文は画面に出ない）。使わないときはすべて空文字。

{book_ai.NETA_RULES}

{book_ai.MOTION_RULES}
- 英会話では特に: ずんだもんが、アニメ・漫画の決めゼリフをもじって英語の失敗を言い訳する・調子に乗る（例:「だが断るのだ！」と言ってから正しい断り方を教わる、「長男だったら英語も耐えられたのだ…」）。英語のセリフ（ネイティブ音声）にはネタを入れず、自然な英語のままにする。

## 読み方（readings・日本語の読み方辞書）
- 日本語のセリフ（reading に書いた文の日本語の部分も含む）で、音声合成（VOICEVOX）が読み間違えそうな日本語の言葉の読み方を、readings に {{"word": 表記, "reading": ひらがな or カタカナの読み}} で書く（0〜15個）。
  対象: 人名・地名・店名、難読語、読み方が複数ある語（例: 一日→ついたち/いちにち、今日→きょう、上手→じょうず、何人→なんにん、他人事→ひとごと）、数字＋単位（例: 1杯→いっぱい、3分→さんぷん）、英語の授業でよく使う言葉（例: 例文→れいぶん、発音→はつおん）で読みがあいまいなもの。
  セリフに実際に出てくる言葉だけにし、普通に読める言葉は入れない。英語の読みは readings ではなく、各セリフの reading に書く。

{book_ai.CLICK_RULES}

## 投稿用のタイトル・説明文
- book_title には「{plan.get('theme', '')}」のような今週のテーマ名を書く。
- title_candidates: 3つ。各40字以内。企画の中身（場面と、見たら何ができるようになるか）をタイトルの一番前に置き、シリーズ名は最後に「｜毎日英会話 Day{day}」と付ける（Day の番号が先頭にあると、初めて見る人が「Day1から見ていないから後回しにしよう」と感じてクリックを避けるため）。文法用語や今日の場面の細部ではなく、英語を話したい人なら誰でも「自分のことだ」と思う言葉にする（例:「その英語、ネイティブに通じてないかも！？｜毎日英会話 Day{day}」「英語が聞き取れない時に使える神フレーズ｜毎日英会話 Day{day}」「Whatで聞き返すのはNG？ 失礼にならない聞き返し方｜毎日英会話 Day{day}」）。
- video_title: その中で一番クリックされそうな1つ。
- description_lead: 説明欄の冒頭2行（改行区切り、各40字以内）。今日できるようになること。
- pinned_comment: YouTubeのコメント欄に固定するコメント（4〜7行・全体で200字以内。改行区切り）。今日のフレーズ（英語と意味）→「このフレーズを使って、コメントに英語を1文書いてみてね」と、答えやすいお題と例文を1つ → 明日の予告。絵文字は1〜3個まで。URLは書かない。
- hashtags: 4つ（# は付けない。「ずんだもん解説」はアプリが必ず先頭に付けるので書かない）。書名・人名などの固有名詞ではなく、多くの人が検索・フォローしていて、この動画の内容に関係する一般的な言葉にする（例: 英会話、英語学習、リスニング、TOEIC、英語、スピーキング、海外旅行）。
- tags: {book_ai.TAGS_RULE}
- phrases: この動画で教えたフレーズ（en・ja）。
- thumbnail（サムネイルの文言と見せ方）。一覧で一番に目に入るのは、上部いっぱいに出る特大の一言（text）。左に胸から上の大きなずんだもん（感情の伝わる顔。画面の30〜40%）、右に特大の一言（画面の35〜45%）、その下に答えを伏せた英語の吹き出し（例: Let me 〇〇…？）が出て、場面のイラストは背景にぼかして薄く敷かれる。スマホの一覧ではサムネイルは切手ほどの大きさなので、文字の要素は「大きな一言」と「吹き出しの英語」の2つだけにする（右下は再生時間が重なるので、アプリが何も置かない）:
  text は2行まで（改行は \\n）、1行9字以内・全体で10〜16字。YouTube Studio の分析でも、横長の動画は「Day2」のような通し番号より、見たら何が得られるか（ベネフィット）がはっきりしているほうが届きやすい。見たら何ができるようになるか（得られるメリット）を、場面＋数・結果で、スマホの小さな画面でも一目で分かる一言にする（例:「入国審査で\\n**焦らない**3フレーズ」「もう聞き返されても\\n**焦らない**！」「この一言で\\n注文が**通じる**」）。Day の番号・シリーズ名・「総集編」「まとめ」だけの言葉は入れない（1週間のまとめの回も、その週に言えるようになることを書く）。文字は白で、一番大事な1語だけを **語** で囲む（赤く大きく目立つ）。==語== で囲むと黄色（多用しない）。
  scene は空文字でよい（場面は、背景の絵と大きな一言で伝える）。
  accent は企画の種類の色（強調する文字・吹き出しの枠・外枠の色になる。一覧に並んだとき1本ずつ違う企画に見えるように）: トラブル・NG系（聞き返せない・失礼）は "trouble"（赤）、街中・移動系（道を聞かれた・駅・空港）は "town"（黄・オレンジ）、日常会話・カフェ系（えーと・注文）は "cafe"（カフェラテ色）、解決・神フレーズ系（これでOK）は "solution"（青）。
  phrase は、今日のフレーズのうち一番使える英語を1つ（25字以内）。答えが見えるとその場で満足してクリックされないので、アプリが吹き出しでは後半を伏せて出す（例: Let me check the map. → Let me 〇〇…？）。
  sub は空文字にする（英会話のサムネイルには左上の帯を出さない。文字の要素が増えて視線が迷うため。シリーズ名や Day の番号も入れない）。
  layout は基本 "scene"（使える場面）。感情が強い内容なら "reaction"、結論が強い一言なら "big_text"。"before_after" は使わない。
  zundamon の表情は企画の種類で選ぶ（いつも笑顔にしない。視聴者が一番感情移入するのは「焦り・困惑・気まずさ」の共感）: 困り系（逃げない・聞き返せない・Yes連発・固まる・聞き取れない など）は焦り・パニック・ショック・困り顔、解決系（これ一言でOK・神フレーズ など）はドヤ顔・ひらめき顔。shout は吹き出しのひと言（reaction・big_text で使う。3〜8字）。before・after は空文字、before_face・after_face は zundamon と同じ表情でよい。
  visual は、画像生成AI（ChatGPT・Gemini など）にサムネイルの絵を描いてもらうための「絵の内容」（日本語1〜2文・80字以内）。キャラクターのポーズ・動き・場面・小道具を具体的に書き、一目で笑える・気になる絵にする。アニメ・漫画・ネットミームのパロディ風の構図や演出（例: ジョジョ立ち風の決めポーズに「ゴゴゴゴ」の効果文字、劇画タッチで驚愕する顔、宇宙猫顔、少年漫画の必殺技風の集中線）を入れてもよい。ただし他の作品のキャラクターそのもの・作品名・ロゴは描かせず、ずんだもんたちが「〜風」にまねする形にする。
 {book_ai.promo_short_rules("english", speech_speed)}"""


def generate_lesson(plan: dict, day: int, level: str = "", speech_speed: float = DEFAULT_SPEECH_SPEED,
                    progress: Optional[ProgressCallback] = None) -> AIResult:
    """1週間の計画から、その日（1〜6日目のレッスン / 7日目のまとめ）の台本JSONを作る。"""
    level = level or plan.get("level") or "a2"
    result = _call(
        _lesson_system(plan, day, level, speech_speed),
        [{"type": "text", "text": f"第{plan.get('week', 1)}週の{day}日目の台本を書いてください。"
                                  + video_history.recent_digest("english")}],
        _lesson_schema(), book_ai.SCRIPT_MAX_TOKENS, progress or (lambda _m: None),
    )
    finish_lesson_data(result.data, plan, day, level)
    return result


def finish_lesson_data(data: dict, plan: dict, day: int, level: str = "") -> dict:
    """台本JSONに英会話モードの情報を付け、未使用の項目を取り除き、ネイティブ音声のIDを振る。"""
    week = int(plan.get("week") or 1)
    data["style"] = "normal"
    data["source_kind"] = "english"
    data["author"] = ""
    data["lesson"] = {
        "week": week, "day": int(day), "theme": plan.get("theme", ""), "theme_en": plan.get("theme_en", ""),
        "level": level or plan.get("level") or "a2", "phrases": data.get("phrases") or [],
    }
    promo = data.get("promo_short") if isinstance(data.get("promo_short"), dict) else {}
    for block in list(data.get("blocks", [])) + list(promo.get("blocks") or []):
        for line in block.get("lines", []):
            for key in ("se", "hide", "image", "image_request", "caption", "ja", "reading", "pause_text", "mood", "card",
                        "pause_style", "bgm"):
                if not line.get(key):
                    line.pop(key, None)
            if not line.get("image_request"):
                line.pop("image_name", None)
            if not line.get("pause"):
                line.pop("pause", None)
            if not (line.get("note") or {}).get("text"):
                line.pop("note", None)
            if not line.get("bullet"):
                line.pop("bullet", None)
        for key in ("background", "background_request", "background_name"):
            if not block.get(key):
                block.pop(key, None)
        if not (block.get("slide") or {}).get("title") and not (block.get("slide") or {}).get("bullets"):
            block.pop("slide", None)
    assign_audio_ids(data, week, day)
    if promo.get("blocks"):
        assign_audio_ids(promo, week, day)  # 本編紹介ショートの英語も、同じ英文なら本編と同じ音声を使い回す
    return data


LESSON_SAMPLE = {
    "book_title": "カフェで使う英語",
    "phrases": [{"en": "Can I get a coffee?", "ja": "コーヒーをもらえますか？"}],
    "blocks": [
        {"section": "intro", "bgm": "opening", "lines": [
            {"speaker": "zundamon", "expression": "sad", "text": "カフェでコーヒープリーズって言ったら、サイズを聞かれて固まったのだ…", "lang": "ja", "board": False},
            {"speaker": "shikoku_metan", "expression": "happy", "text": "今日は注文で使える英語を教えてあげるわ。", "lang": "ja", "board": False},
        ]},
        {"section": "phrase", "bgm": "talk", "slide": {"title": "今日のフレーズ1", "bullets": ["Can I get a coffee?", "コーヒーをもらえますか？", "Can I は「キャナイ」とつながる"]},
         "lines": [
             {"speaker": "shikoku_metan", "expression": "normal", "text": "Can I get a coffee?", "lang": "en", "ja": "コーヒーをもらえますか？", "voice": "A", "board": True},
             {"speaker": "shikoku_metan", "expression": "explain", "text": "Can I get は「〜をもらえる？」って意味よ。", "lang": "ja", "reading": "キャナイゲットは「〜をもらえる？」って意味よ。",
              "note": {"text": "Can I get a coffee?", "focus": "Can I get", "meaning": "〜をもらえる？（お店で注文するときの定番）"}, "board": True},
             {"speaker": "zundamon", "expression": "happy", "text": "Can I get a coffee?", "lang": "en", "reading": "キャン アイ ゲット ア コーヒー", "board": True},
             {"speaker": "shikoku_metan", "expression": "angry", "text": "Can I は、キャナイってつなげるのよ。", "lang": "ja", "reading": "キャナイは、キャナイってつなげるのよ。", "board": True},
         ]},
        {"section": "repeat", "bgm": "talk", "lines": [
            {"speaker": "shikoku_metan", "expression": "normal", "text": "Can I get a coffee?", "lang": "en", "ja": "コーヒーをもらえますか？", "voice": "A", "pause": 3.5, "pause_style": "repeat", "pause_text": "リピート！", "board": False},
            {"speaker": "shikoku_metan", "expression": "normal", "text": "Can I get a coffee?", "lang": "en", "ja": "コーヒーをもらえますか？", "voice": "A", "pause_style": "shadow", "pause_text": "一緒に言ってみよう！", "board": False},
        ]},
        {"section": "quiz", "bgm": "cheerful", "lines": [
            {"speaker": "shikoku_metan", "expression": "normal", "text": "「紅茶をもらえますか？」を英語で言うと？", "lang": "ja", "pause": 4.5, "pause_text": "英語で言ってみよう！", "board": False},
            {"speaker": "shikoku_metan", "expression": "happy", "text": "Can I get a tea?", "lang": "en", "ja": "紅茶をもらえますか？", "voice": "A", "board": False},
        ]},
    ],
}


def lesson_manual_prompt(plan: dict, day: int, level: str = "", speech_speed: float = DEFAULT_SPEECH_SPEED) -> str:
    """APIを使わずに、Claudeのチャット画面で台本を作ってもらうためのプロンプト。"""
    level = level or plan.get("level") or "a2"
    return "\n\n".join([
        _lesson_system(plan, day, level, speech_speed),
        f"第{plan.get('week', 1)}週の{day}日目の台本を、次の形式のJSONだけで出力してください"
        "（各ブロックには section・bgm を、各セリフには speaker・expression・text・lang・ja・reading・voice・pause・pause_text・se・board を書く。"
        "セリフの bgm は空気が変わるところだけ書く。"
        "トップレベルには、本編紹介ショートの promo_short "
        '（{"title_candidates": [...], "video_title": "...", "hook": "...", "description_lead": "...", "hashtags": [...], "tags": [...], '
        '"blocks": [本編と同じ形のブロック]}）も必ず書く。'
        "形式の例なので、中身と分量は上のルールに従う）:",
        "```json\n" + json.dumps(LESSON_SAMPLE, ensure_ascii=False, indent=2) + "\n```",
        video_history.recent_digest("english").strip(),
    ])


def prepare_pasted_lesson(data: dict, plan: Optional[dict], day: int) -> dict:
    """手動で作った台本JSON（貼り付け）を、英会話モードの台本として仕上げる（音声IDの付与など）。"""
    plan = plan or {"week": int((data.get("lesson") or {}).get("week") or 1)}
    return finish_lesson_data(data, plan, day, (data.get("lesson") or {}).get("level", ""))

