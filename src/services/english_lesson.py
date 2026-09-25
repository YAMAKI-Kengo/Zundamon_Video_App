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

from src.models import Scene
from src.services import book_ai
from src.services.book_ai import _STR, _STR_LIST, AIResult, ProgressCallback, _call, _obj
from src.services.voicevox_client import DEFAULT_SPEECH_SPEED
from src.utils.asset_loader import get_available_expressions, list_illustrations, list_se

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


def link_native_audio(scenes: list[Scene]) -> list[Scene]:
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
            scene.duration = round(scene.lead_in + audio_duration(path) + LINE_GAP_SECONDS + scene.board_hold, 2)
        except Exception:  # noqa: BLE001 - 長さを測れない音声は、秒数を変えずに使う
            pass
        linked.append(scene)
    return linked


_linked_stamps: dict[str, str] = {}  # シーンID → 紐付けた音声ファイル（パスと更新日時。差し替えの検出用）


def native_audio_items(scenes: list[Scene]) -> list[dict]:
    """動画で使うネイティブ音声の一覧（登場順・重複なし）: {"id", "text", "voice", "ready", "path"}。"""
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
            "ready": path is not None,
            "path": str(path) if path else "",
        }
    return list(items.values())


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

## 計画の作り方
- 1週間で1つのテーマ（場面や機能。例: カフェで注文する、道をたずねる、自己紹介、予定を立てる）にする。1〜6日目が毎日のレッスン、7日目は1週間のまとめ（days には1〜6日目だけを書く）。
- 1〜6日目は、同じテーマの中で場面を少しずつ変え、易しいものから順に並べる。前の日のフレーズを後の日の会話でも使えるようにつなげる（くり返し出会うことで定着する）。
- 各日の phrases は2つ。実際の会話でよく使う、短くて応用のきく決まり文句にする（en は英語、ja は自然な日本語訳、point は使い方・言い換え・似た表現との違いを1文で）。
- 教科書的すぎる表現や古い表現は避け、今のネイティブが日常で使う自然なアメリカ英語にする。
- zunda_trouble には、その日の場面で日本人がやりがちな失敗（直訳・カタカナ発音・丁寧すぎる/失礼な言い方など）を、ずんだもんがやらかす小さなエピソードとして1文で書く。
- grammar には、その日に触れる文法か発音（音のつながり・弱く読む音など）のポイントを1つ書く。
- next_theme_idea には、来週のテーマの案を1つ書く。
- 以前の週で扱ったテーマと重ならないようにする。"""


def plan_week(week: int, theme_hint: str = "", level: str = "a2", previous_themes: Optional[list[str]] = None,
              progress: Optional[ProgressCallback] = None) -> AIResult:
    """1週間分（1〜6日目のレッスン＋7日目のまとめ）の計画を作る。"""
    instruction = "\n".join(filter(None, [
        f"第{week}週の計画を作ってください。",
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


def _lesson_schema() -> dict:
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
        "hide": {"type": "array", "items": {"type": "string", "enum": characters}},
        "board": {"type": "boolean"},
        "bullet": {"type": "integer"},
        "note": _obj({"text": _STR, "focus": _STR, "meaning": _STR}),
        "image": {"type": "string", "enum": [""] + [p.stem for p in list_illustrations()]},
        "caption": _STR,
        "image_request": _STR,
        "image_name": _STR,
    })
    block = _obj({
        "section": {"type": "string", "enum": LESSON_SECTIONS_ENUM},
        "slide": _obj({"title": _STR, "bullets": _STR_LIST, "numbered": {"type": "boolean"}}),
        "lines": {"type": "array", "items": line},
    })
    return _obj({
        "book_title": _STR,
        "title_candidates": _STR_LIST,
        "video_title": _STR,
        "description_lead": _STR,
        "hashtags": _STR_LIST,
        "tags": _STR_LIST,
        "readings": {"type": "array", "items": _obj({"word": _STR, "reading": _STR})},
        "phrases": {"type": "array", "items": _obj({"en": _STR, "ja": _STR})},
        "thumbnail": _obj({"text": _STR, "sub": _STR, "layout": {"type": "string", "enum": ["reaction", "duo", "big_text"]}, "zundamon": {"type": "string", "enum": expressions}, "metan": {"type": "string", "enum": expressions}}),
        "blocks": {"type": "array", "items": block},
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
- お手本の英語（ネイティブ音声）: speaker は "shikoku_metan"、reading は空文字にする（録音したネイティブ音声を使う）。voice は、会話（dialog）の2人の役を "A"（めたんの役）と "B"（相手の役）で書き分け、それ以外は "A"。
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
4. summary: slide の title は「今週のフレーズ」、bullets に今週のフレーズを6〜8個（「英語 ― 日本語」）。今週できるようになったことをほめ、来週のテーマ（{plan.get('next_theme_idea', '')}）を予告して、ずんだもんのオチで締める。"""
    today = next((d for d in plan.get("days", []) if int(d.get("day") or 0) == day), {})
    tomorrow = next((d for d in plan.get("days", []) if int(d.get("day") or 0) == day + 1), {})
    return f"""## 構成（{day}日目・約{target_minutes:g}分。blocks はこの順）
1. intro（会話だけ・20〜30秒）: 「今週のテーマ『{plan.get('theme', '')}』の{day}日目」。ずんだもんが今日の場面でやらかした話（{today.get('zunda_trouble', '')}）→ めたんがツッコみ、「今日は〇〇で使える英語を教えてあげるわ」と今日のフレーズを予告する。
2. dialog（ダイアログ）: めたんの前置き（日本語・「まずは会話を聞いてみて」）→ 場面（{today.get('situation', '')}）のネイティブ音声の会話 4〜6行（voice A/B の2役。今日のフレーズ2つを必ず使う）→ ずんだもんの「速すぎて分からないのだ…」のような一言。会話の最初のセリフに、場面のイラスト（image か image_request）を出し、会話の英語のセリフにはすべて同じイラストを付ける（英語の会話が続いている間はずっとイラストを出したままにする。アプリも自動で出したままにする）。
3. phrase（今日のフレーズ1・2で2ブロック）: 各ブロックの slide は title を「今日のフレーズ1」「今日のフレーズ2」、bullets は「英語のフレーズ」「意味」「使い方のポイント」「言い換え・応用」の順に3〜4個。流れ: めたんがフレーズを英語で言う（ネイティブ音声）→ 日本語で意味と使い方を解説 → 例文をもう1つ英語で（ネイティブ音声）→ ずんだもんがカタカナ英語でまねする（reading にカタカナ）→ めたんが発音のコツ（{today.get('grammar', '')}など）をツッコミながら教える → ずんだもんがもう一度言って少し上手くなる。
4. repeat（リピート練習・1ブロック）: めたん「音声のあとに、3・2・1の合図で言ってみて」→ 今日のフレーズ2つと会話の中の大事な文を合わせて3〜4文、ネイティブ音声の英語を1文ずつ（pause 3〜4、pause_style "repeat"、pause_text「リピート！」。アプリが音声のあとに「3・2・1 → リピート！」の合図を出す）→ めたん「次は音声に重ねて言ってみて」→ シャドーイングを1〜2文（その英文の行の pause_style を "shadow"、pause を 0、pause_text を「一緒に言ってみよう！」。アプリが「3・2・1」のあと同じ音声をもう一度流すので、視聴者は音声と一緒に言う）。
5. quiz（瞬発トレーニング・1ブロック・3〜4問）: めたん「日本語を見て、すぐ英語で言ってみて」→ 問題ごとに、めたんが日本語の文を出題（「『〇〇』を英語で言うと？」。pause 4〜5、pause_style "think"、pause_text「英語で言ってみよう！」。残り秒数のタイマーが出る）→ めたんの英語で答え（ネイティブ音声）。今日のフレーズを少し言い換えて使う問題にする（例: 名詞を入れ替える）。ずんだもんの回答の小ネタを1回入れてよい。
6. summary: slide の title は「今日のまとめ」、bullets に今日のフレーズ2つ（「英語 ― 日本語」）。めたんが今日のフレーズを英語でもう一度言う（ネイティブ音声）→ ずんだもんが今日の場面にリベンジする一言（日本語）→ 明日の予告（{tomorrow.get('title', '明日のレッスン')}）でオチ。
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

## 今週の計画
- 第{plan.get('week', 1)}週のテーマ: {plan.get('theme', '')}（{plan.get('theme_en', '')}）。1週間のゴール: {plan.get('goal', '')}
- 今週のフレーズ: {' / '.join(week_phrases)}
{f"- 今日（{day}日目）: {today.get('title', '')}。場面: {today.get('situation', '')}。今日のフレーズ: {' / '.join(today_phrases)}" if today else ""}

{_lesson_structure(day, plan, target_minutes)}

{_english_line_rules(level)}

## テンポ・画面
- 日本語のセリフは10〜30字（字幕1行＝30字）。同じ話者が3セリフ以上続かない。ずんだもんのボケとめたんのツッコミで、1ブロックに1回は小さな笑いを入れる。
- 画面の基本は2人の会話だけ（board は false、image は空文字）。黒板（board を true）は phrase・review・summary でフレーズを紹介するセリフから出し、そのブロックの最後まで出したままにする（アプリも自動で出したままにする）。黒板を出している間は、黒板の1行ごとに2〜3セリフかけて、意味・使い方・例文をじっくり話す。黒板の bullets は、話す順に1行ずつ書き足される。slide の numbered は、手順や順番があるときだけ true。
- mood（背景の雰囲気）は、ずんだもんが英語で失敗して落ち込みがピークになる場面だけ "gloomy"（ショックの瞬間なら "shock"）にし、その場面の2〜4セリフに続けて付ける（1セリフだけ付けない）。動画の最初のセリフには付けない。1本で1〜2場面まで。それ以外は空文字。card（場面転換テロップ。例:「その日の夜…」）は導入で時間が飛ぶときだけ、text を空文字にした独立した行で使い、それ以外は空文字。
- note（重要な表現の解説カード）: phrase ブロックで、今日のフレーズの大事な部分（例: Can I get の部分）を解説するセリフに使う（フレーズごとに1回、1本で2〜4回）。text は英文、meaning は日本語。画面に文が大きく出て、focus の部分に赤い下線が引かれ、矢印の先に meaning が表示される。
  text に文（25字・8語程度まで）、focus に赤線を引く部分（text の中にそのまま含まれる語句）、meaning にその部分の意味・使い方（25字以内。例:「〜をもらえる？ お店で注文するときの定番」）を書く。
  その部分を説明する2〜3セリフに、同じ note を続けて付ける（その間は黒板の代わりに解説カードが出る）。使わない行は text・focus・meaning をすべて空文字にする。
- bullet（黒板のどの行の話か）: 黒板の箇条書きの行を初めて話すセリフに、その行の番号（1から）を書く（その番号の行が、そのセリフで黒板に書き足される）。それ以外のセリフは 0。瞬発トレーニング・ふりかえり（quiz・review）で黒板に答えを書く場合は、答えを言う英語のセリフにその行の番号を付ける（問題を出すセリフには付けない。先に答えが見えてしまうため）。行の番号は、話す順番どおりに1, 2, 3…と増えるようにする。まとめ（summary）は全部の行を最初から出すので、すべて 0 でよい。
- 表情（expression）は、セリフの感情に合うものを次の一覧から選ぶ:
{book_ai._character_guide()}
- se（効果音）は、つかみ・正解・ツッコミ・オチなど3〜8回だけ。使えるのは次の名前のみ（使わないときは空文字）: {book_ai._se_guide()}
- hide は空の配列。イラスト（image・caption・image_request・image_name）は、dialog の最初のセリフなど場面が伝わるところだけに出し、1本で2〜4枚までにする（出すセリフは board を false）。手元にあるイラスト: {', '.join(p.stem for p in list_illustrations()) or '（なし。image_request で依頼する）'}。合うものが無ければ、image_request に欲しいイラスト（検索語付き）、image_name に保存名（日本語4〜12字）を書く。caption は空文字（説明文は画面に出ない）。使わないときはすべて空文字。

## 読み方（readings・日本語の読み方辞書）
- 日本語のセリフ（reading に書いた文の日本語の部分も含む）で、音声合成（VOICEVOX）が読み間違えそうな日本語の言葉の読み方を、readings に {{"word": 表記, "reading": ひらがな or カタカナの読み}} で書く（0〜15個）。
  対象: 人名・地名・店名、難読語、読み方が複数ある語（例: 一日→ついたち/いちにち、今日→きょう、上手→じょうず、何人→なんにん、他人事→ひとごと）、数字＋単位（例: 1杯→いっぱい、3分→さんぷん）、英語の授業でよく使う言葉（例: 例文→れいぶん、発音→はつおん）で読みがあいまいなもの。
  セリフに実際に出てくる言葉だけにし、普通に読める言葉は入れない。英語の読みは readings ではなく、各セリフの reading に書く。

## 投稿用のタイトル・説明文
- book_title には「{plan.get('theme', '')}」のような今週のテーマ名を書く。
- title_candidates: 3つ。各32字以内。「【毎日英会話】Day{day}」を先頭に付け、今日のフレーズか場面が分かるようにする（例:「【毎日英会話】Day{day} カフェで Can I get〜? を使いこなす」）。
- video_title: その中で一番クリックされそうな1つ。
- description_lead: 説明欄の冒頭2行（改行区切り、各40字以内）。今日できるようになること。
- hashtags: 3つ（# は付けない）。「英会話」「英語学習」と今週のテーマ。
- tags: 8〜12個。
- phrases: この動画で教えたフレーズ（en・ja）。
- thumbnail（サムネイルの文言と見せ方）:
  text は2〜3行（改行は \\n）、1行8字以内・全体で10〜18字。タイトルをそのまま縮めるのではなく、一目で「えっ？」「自分のことだ」と思う言葉（今日のフレーズ・その場面・「ネイティブはこう言う」など）にする。一番大事な1語だけを **語** で囲む（赤く目立つ）。
  sub は左上の帯の短いラベル（6〜10字。例: 毎日英会話 Day{day}（7日目は 毎日英会話 まとめ））。
  layout は、感情が強い内容なら "reaction"（ずんだもんのアップ）、本の表紙やイラストを見せたい内容なら "duo"（2人＋画像）、結論が強い一言なら "big_text"（大きな文字）。
  zundamon・metan は、内容の感情が一目で伝わる表情（驚き・ショック・ドヤ顔・指さしなど）を一覧から選ぶ。"""


def generate_lesson(plan: dict, day: int, level: str = "", speech_speed: float = DEFAULT_SPEECH_SPEED,
                    progress: Optional[ProgressCallback] = None) -> AIResult:
    """1週間の計画から、その日（1〜6日目のレッスン / 7日目のまとめ）の台本JSONを作る。"""
    level = level or plan.get("level") or "a2"
    result = _call(
        _lesson_system(plan, day, level, speech_speed),
        [{"type": "text", "text": f"第{plan.get('week', 1)}週の{day}日目の台本を書いてください。"}],
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
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for key in ("se", "hide", "image", "image_request", "caption", "ja", "reading", "pause_text", "mood", "card",
                        "pause_style"):
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
        if not (block.get("slide") or {}).get("title") and not (block.get("slide") or {}).get("bullets"):
            block.pop("slide", None)
    assign_audio_ids(data, week, day)
    return data


LESSON_SAMPLE = {
    "book_title": "カフェで使う英語",
    "phrases": [{"en": "Can I get a coffee?", "ja": "コーヒーをもらえますか？"}],
    "blocks": [
        {"section": "intro", "lines": [
            {"speaker": "zundamon", "expression": "sad", "text": "カフェでコーヒープリーズって言ったら、サイズを聞かれて固まったのだ…", "lang": "ja", "board": False},
            {"speaker": "shikoku_metan", "expression": "happy", "text": "今日は注文で使える英語を教えてあげるわ。", "lang": "ja", "board": False},
        ]},
        {"section": "phrase", "slide": {"title": "今日のフレーズ1", "bullets": ["Can I get a coffee?", "コーヒーをもらえますか？", "Can I は「キャナイ」とつながる"]},
         "lines": [
             {"speaker": "shikoku_metan", "expression": "normal", "text": "Can I get a coffee?", "lang": "en", "ja": "コーヒーをもらえますか？", "voice": "A", "board": True},
             {"speaker": "shikoku_metan", "expression": "explain", "text": "Can I get は「〜をもらえる？」って意味よ。", "lang": "ja", "reading": "キャナイゲットは「〜をもらえる？」って意味よ。",
              "note": {"text": "Can I get a coffee?", "focus": "Can I get", "meaning": "〜をもらえる？（お店で注文するときの定番）"}, "board": True},
             {"speaker": "zundamon", "expression": "happy", "text": "Can I get a coffee?", "lang": "en", "reading": "キャン アイ ゲット ア コーヒー", "board": True},
             {"speaker": "shikoku_metan", "expression": "angry", "text": "Can I は、キャナイってつなげるのよ。", "lang": "ja", "reading": "キャナイは、キャナイってつなげるのよ。", "board": True},
         ]},
        {"section": "repeat", "lines": [
            {"speaker": "shikoku_metan", "expression": "normal", "text": "Can I get a coffee?", "lang": "en", "ja": "コーヒーをもらえますか？", "voice": "A", "pause": 3.5, "pause_style": "repeat", "pause_text": "リピート！", "board": False},
            {"speaker": "shikoku_metan", "expression": "normal", "text": "Can I get a coffee?", "lang": "en", "ja": "コーヒーをもらえますか？", "voice": "A", "pause_style": "shadow", "pause_text": "一緒に言ってみよう！", "board": False},
        ]},
        {"section": "quiz", "lines": [
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
        "（各セリフには speaker・expression・text・lang・ja・reading・voice・pause・pause_text・se・board を書く。"
        "形式の例なので、中身と分量は上のルールに従う）:",
        "```json\n" + json.dumps(LESSON_SAMPLE, ensure_ascii=False, indent=2) + "\n```",
    ])


def prepare_pasted_lesson(data: dict, plan: Optional[dict], day: int) -> dict:
    """手動で作った台本JSON（貼り付け）を、英会話モードの台本として仕上げる（音声IDの付与など）。"""
    plan = plan or {"week": int((data.get("lesson") or {}).get("week") or 1)}
    return finish_lesson_data(data, plan, day, (data.get("lesson") or {}).get("level", ""))

