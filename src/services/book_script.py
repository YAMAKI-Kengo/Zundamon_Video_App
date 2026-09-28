"""
書籍解説用の掛け合い台本（JSON）から、シーンを一括生成する。

「悩みを抱えたずんだもんに、四国めたんが解決策になる本を紹介・解説する」形式の動画を想定し、
台本は「ブロック（場面 + 黒板スライド + セリフのリスト）」の並びとして表す。
LLM（Step 5）に出力させる形式と同じだが、手書き・他のAIチャットで作ったものを貼り付けてもよい。

    {
      "book_title": "本のタイトル",
      "blocks": [
        {
          "section": "intro",                     # intro(導入・悩み) / explain(解説) / summary(まとめ) / ending
          "phase": "hype",                        # 段階（任意）: intro は hype(決意)/fail(失敗)/rescue(めたん登場)、
                                                  #   explain は why(なぜ失敗したのか)/how(成功させるには)。段階ごとにBGMを変えられる
          "slide": {"title": "見出し", "bullets": ["要点1", "**強調**を含む要点2"]},   # 省略可
          "lines": [
            {"speaker": "zundamon", "expression": "troubled", "text": "最近ぜんぜん眠れないのだ…", "se": "ショック1"},
            {"speaker": "めたん", "expression": "喜び", "text": "それならこの本がおすすめですわ。"}
          ]
        }
      ]
    }

- 1セリフ = 1シーン。ブロック内の全シーンに、そのブロックの場面・スライドが設定される
  （スライドはセリフが進んでも黒板に書かれたまま残る）。
- "blocks" の代わりに "sections" というキー名でも受け付ける（LLMの表記ゆれ対策）。
- speaker は内部キー（zundamon / shikoku_metan）・表示名・ニックネーム（ずんだ / めたん）のどれでもよい。
  expression も内部キー（happy）・日本語ラベル（喜び）のどちらでもよく、見つからなければ既定表情にする。
- "hide" に話者のリスト（例: ["めたん"]）を書くと、そのキャラクターを画面に表示しない。
  ブロックに書くとブロック内の全セリフ、セリフに書くとそのセリフだけに効く（両方あれば合わせた分を隠す）。
  例えば導入でずんだもんが1人で悩んでいる場面を作るときに、めたんを隠す使い方を想定している。
- 黒板の箇条書きは、既定ではブロック内のセリフの進行に合わせて1行ずつ書き足していく
  （reveal_bullets=True のとき。ブロックに "reveal": false と書くとそのブロックは最初から全部表示）。
- 各セリフに "camera"（auto / none / zoom_speaker / slow_zoom_speaker / slow_zoom）、
  "shake"（true / false）、"motion"（auto / none / bob / jump）を書くと、カメラワーク・画面の揺れ・
  話者の動きを指定できる（省略時は表情・効果音・場面から自動で決まる。src.services.motion 参照）。
- セリフに "image": "イラスト名"（assets/illustrations/ の画像のファイル名から拡張子を除いたもの）と
  "caption": "短い説明" を書くと、そのシーンではイラストを白いカードに載せて画面中央に大きく表示する（黒板より優先）。
- 合うイラストが無い場合は "image_request"（欲しいイラストの説明）と "image_name"（保存するときのファイル名）を
  書く。その名前で assets/illustrations/ に画像を置くと、そのシーンに表示される（link_requested_illustrations()）。
  まだ置かれていない依頼は illustration_requests() で一覧にできる。
- セリフに "board": false と書くと、そのシーンでは黒板を画面に出さず、2人の会話だけの画面にする（書かなければ出す）。
  黒板の箇条書きは、黒板を出すセリフの順に1行ずつ書き足される（assign_bullet_reveal()）。
- セリフに "show_book": true と書くと、そのシーンで本の表紙画像（Project.book_cover_path）を画面中央に出す
  （本を紹介する場面用。表紙画像が無い場合は書名・著者を書いた黒板で代わりに見せる。apply_book_cover()）。
- トップレベルの "readings"（[{"word": "他人事", "reading": "ひとごと"}, ...]）は、VOICEVOXが読み間違えそうな
  言葉の読み方で、プロジェクトの読み方辞書に追加される。config/reading_defaults.json の既定の読み方のうち、
  台本に出てくる言葉も自動で追加される。
- "se" にはそのセリフの頭で鳴らす効果音の名前（assets/se/ のファイル名から拡張子を除いたもの。
  例: "きらーん1"）を書ける。省略可。見つからない場合は警告して鳴らさない。
- add_ending=True の場合、台本に ending（エンディング）のブロックが無ければ、最後にエンディングを付け足す
  （2人とも笑顔で、画面上部に「ご視聴ありがとうございました！／チャンネル登録＆高評価よろしくお願いします！」、
  ずんだもん→めたんの順に字幕なしで挨拶・合計約12秒）。内容は config/ending.json で変更できる。
- 任意で "author"（著者名）・"video_title"・"title_candidates"（タイトル案）・"description_lead"（説明文の
  冒頭2〜3行）・"video_description"・"hashtags"・"tags"（投稿用）、"style"（"normal" / "short"）も書ける。
  書かれていないものは src.services.video_metadata が台本の内容から自動生成する。
- JSONの前後に説明文や ```json ～ ``` のコードブロック記号が付いていても、中身だけを取り出して読む
  （AIチャットの回答をそのまま貼り付けられるようにするため）。
- 表示秒数は、VOICEVOXが起動していれば実際の読み上げ時間（話速1.0）＋少しの間 に合わせる。
  起動していなければ文字数からの概算にする（動画生成時の話速自動調整で収まるように少し長めに見積もる）。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from src.models import (
    COUNTDOWN_SECONDS,
    GUEST_CHARACTERS,
    CAMERA_LABELS,
    CHAR_MOTION_LABELS,
    PHASE_LABELS,
    SECTION_LABELS,
    SHAKE_LABELS,
    Project,
    Scene,
    VideoFormat,
    phase_label,
)
from src.services import script_import, voicevox_client
from src.services.telop import TELOP_CHARS_PER_LINE, TELOP_MAX_LINES, split_natural
from src.services.voicevox_client import DEFAULT_SPEECH_SPEED
from src.services.voicevox_client import VoicevoxConnectionError, VoicevoxSynthesisError
from src.utils.asset_loader import (
    ASSETS_DIR,
    find_background,
    find_illustration,
    find_se,
    get_character_display_name,
    get_default_expression,
    has_expression_assets,
    list_characters,
)

# 書籍解説動画の既定の背景（画面の向きごと）。ファイルが無い場合は背景を変更しない。
DEFAULT_BOOK_BACKGROUNDS: dict[VideoFormat, Path] = {
    VideoFormat.LANDSCAPE: ASSETS_DIR / "backgrounds" / "zunda_room.png",
    VideoFormat.PORTRAIT: ASSETS_DIR / "backgrounds" / "metan_room.png",
}

ENDING_CONFIG_PATH = ASSETS_DIR.parent / "config" / "ending.json"
DEFAULT_ENDING_SECONDS = 12.0

LINE_GAP_SECONDS = 0.15       # セリフとセリフの間に置く間（秒）。テンポを優先して短め
MIN_SCENE_SECONDS = 1.0       # 1シーンの最短の表示秒数
ESTIMATE_CHARS_PER_SECOND = 6.0  # VOICEVOX未起動時の概算に使う、等速(1.0)での読み上げ速度（話す速さの倍率を掛けて使う）
READING_DEFAULTS_PATH = ASSETS_DIR.parent / "config" / "reading_defaults.json"

_SECTION_ALIASES = {
    "intro": "intro", "introduction": "intro", "導入": "intro", "悩み": "intro", "問題提起": "intro",
    "explain": "explain", "explanation": "explain", "body": "explain", "解説": "explain", "本編": "explain",
    "summary": "summary", "conclusion": "summary", "まとめ": "summary",
    "ending": "ending", "outro": "ending", "エンディング": "ending", "エンド": "ending",
    # 英会話モード
    "dialog": "dialog", "dialogue": "dialog", "ダイアログ": "dialog", "会話": "dialog",
    "phrase": "phrase", "phrases": "phrase", "フレーズ": "phrase", "フレーズ解説": "phrase",
    "repeat": "repeat", "shadowing": "repeat", "リピート": "repeat", "シャドーイング": "repeat",
    "quiz": "quiz", "practice": "quiz", "瞬発": "quiz", "瞬発トレーニング": "quiz",
    "review": "review", "ふりかえり": "review", "振り返り": "review", "復習": "review",
}
ENGLISH_WORDS_PER_SECOND = 2.3   # 英語のセリフの表示秒数の概算に使う、ネイティブの話す速さ（単語/秒。学習用にゆっくりめ）
DEFAULT_PAUSE_TEXT = "リピート！"
DEFAULT_SHADOW_TEXT = "音声に重ねて言ってみよう！"  # シャドーイング（お手本の音声をもう一度流す間）の見出しの既定値
DEFAULT_THINK_TEXT = "考えてみて！"  # 考える・答える間の見出しの既定値  # リピート練習の間（無音）に画面上部へ出す文字の既定値


class BookScriptError(Exception):
    """台本JSONの形式が不正で、シーンを1つも作れない場合のエラー（利用者向けのメッセージ付き）。"""


@dataclass
class BookScriptResult:
    book_title: str
    scenes: list[Scene]
    warnings: list[str] = field(default_factory=list)
    used_voicevox_timing: bool = False
    author: str = ""
    video_title: str = ""
    video_description: str = ""
    tags: list[str] = field(default_factory=list)
    title_candidates: list[str] = field(default_factory=list)
    description_lead: str = ""
    hashtags: list[str] = field(default_factory=list)
    style: str = ""  # "normal" / "short"（台本に書かれていなければ空）
    readings: list[dict] = field(default_factory=list)  # 読み方辞書に追加する [{"word", "reading"}]
    source_kind: str = ""  # "book" / "research"（論文・記事を調べた動画）。台本に書かれていなければ空
    sources: list[dict] = field(default_factory=list)  # 調べた論文・記事の出典
    lesson: dict = field(default_factory=dict)  # 英会話モードの情報（週・日・テーマ・フレーズ）
    thumbnail: dict = field(default_factory=dict)  # サムネイルの指定（台本の "thumbnail"）
    promo_short: dict = field(default_factory=dict)  # 本編紹介ショートの台本（台本の "promo_short"）


def extract_json_text(text: str) -> str:
    """AIチャットの回答などから、JSON本体（最初の { から最後の } まで）を取り出す。"""
    if not text or not text.strip():
        raise BookScriptError("台本JSONが入力されていません。")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise BookScriptError("JSONが見つかりませんでした。{ から始まる台本JSONを貼り付けてください。")
    return candidate[start:end + 1]


def load_book_script(text: str) -> dict:
    """台本JSONの文字列を辞書に変換する。構文エラーは行・列つきの分かりやすいメッセージにする。"""
    json_text = extract_json_text(text)
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise BookScriptError(
            f"JSONの書式が正しくありません（{e.lineno}行目 {e.colno}文字目付近: {e.msg}）。"
            "カンマの過不足や、\" の閉じ忘れがないか確認してください。"
        ) from e
    if not isinstance(data, dict):
        raise BookScriptError("台本JSONの一番外側は { } で囲まれたオブジェクトにしてください。")
    return data


def _normalize_section(value, block_no: int, warnings: list[str]) -> str:
    key = str(value or "").strip().lower()
    if not key:
        return ""
    resolved = _SECTION_ALIASES.get(key)
    if resolved is None:
        warnings.append(f"ブロック{block_no}: 場面「{value}」は認識できないため、場面の指定なしとして扱いました。")
        return ""
    return resolved


_PHASE_ALIASES = {
    "hype": "hype", "決意": "hype", "ワクワク": "hype", "挑戦": "hype",
    "fail": "fail", "失敗": "fail",
    "rescue": "rescue", "登場": "rescue", "めたん登場": "rescue",
    "why": "why", "理由": "why", "失敗の理由": "why",
    "how": "how", "コツ": "how", "成功のコツ": "how", "方法": "how",
}


def _normalize_phase(section: str, value) -> str:
    """ブロックの段階（導入の決意/失敗/めたん登場、解説の失敗の理由/成功のコツ）。場面に合わない値は無視する。"""
    phase = _PHASE_ALIASES.get(str(value or "").strip().lower(), "")
    return phase if phase in PHASE_LABELS.get(section, {}) else ""


CARD_DEFAULT_SECONDS = 2.0  # 場面転換テロップの表示秒数の既定値


def _parse_card_seconds(value) -> float:
    try:
        seconds = float(value or CARD_DEFAULT_SECONDS)
    except (TypeError, ValueError):
        seconds = CARD_DEFAULT_SECONDS
    return round(min(6.0, max(1.0, seconds)), 1)


_MOOD_ALIASES = {
    "gloomy": "gloomy", "sad": "gloomy", "どんより": "gloomy", "落ち込み": "gloomy",
    "shock": "shock", "ガーン": "shock", "ショック": "shock",
    "dark": "dark", "暗い": "dark", "夜": "dark",
    "sepia": "sepia", "回想": "sepia", "flashback": "sepia",
    "bright": "bright", "明るい": "bright", "happy": "bright",
}


def _parse_mood(value) -> str:
    return _MOOD_ALIASES.get(str(value or "").strip().lower(), "")


def _parse_bullet_ref(value) -> int:
    """"bullet": そのセリフで初めて話す黒板の行の番号（1から）。無ければ 0。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_note(value) -> dict:
    """"note": {"text", "focus", "meaning"}（重要な表現の解説カード）を Scene の項目にする。"""
    if not isinstance(value, dict) or not str(value.get("text") or "").strip():
        return {}
    return {
        "note_text": str(value.get("text") or "").strip(),
        "note_focus": str(value.get("focus") or "").strip(),
        "note_meaning": str(value.get("meaning") or "").strip(),
    }


def _parse_pause_style(value, section: str) -> str:
    """間の見せ方。指定が無ければ、リピート練習の場面は「リピート」、それ以外は「考える」。"""
    style = str(value or "").strip().lower()
    aliases = {"repeat": "repeat", "リピート": "repeat", "shadow": "shadow", "shadowing": "shadow",
               "シャドーイング": "shadow", "think": "think", "quiz": "think", "考える": "think"}
    if style in aliases:
        return aliases[style]
    return "repeat" if section == "repeat" else "think"


def _parse_pause(value) -> float:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(min(10.0, seconds), 1) if seconds >= 0.5 else 0.0


def _resolve_speaker(value, label_map: dict[str, str]) -> Optional[str]:
    key = str(value or "").strip()
    return label_map.get(key) or label_map.get(key.lower())


def _resolve_expression(value, speaker: str, expression_maps: dict[str, dict[str, str]]) -> Optional[str]:
    key = str(value or "").strip()
    if not key:
        return None
    mapping = expression_maps.get(speaker, {})
    return mapping.get(key) or mapping.get(key.lower())


def _parse_slide(raw, block_no: int, warnings: list[str]) -> tuple[str, list[str]]:
    if raw is None:
        return "", []
    if not isinstance(raw, dict):
        warnings.append(f"ブロック{block_no}: slide の形式が正しくないため、スライドなしにしました。")
        return "", []
    from src.services.slide_renderer import normalize_title  # 「ポイント1 〜」→「ポイント1：〜」にそろえる

    title = normalize_title(str(raw.get("title") or ""))
    bullets_raw = raw.get("bullets") or raw.get("points") or []
    if isinstance(bullets_raw, str):
        bullets_raw = bullets_raw.splitlines()
    bullets = [str(b).strip() for b in bullets_raw if str(b).strip()]
    return title, bullets


def effective_background_path(project: Project, scene: Scene) -> Optional[str]:
    """シーンで実際に使う背景を返す（シーン個別の背景 → 共通背景 の順）。"""
    return scene.background_path or project.common_background_path


_DEFAULT_ENDING = {
    "duration": DEFAULT_ENDING_SECONDS,
    "headline": "ご視聴ありがとうございました！\nチャンネル登録＆高評価よろしくお願いします！",
    "expression": "happy",
    "se": "",
    "lines": [
        {"speaker": "zundamon", "text": "ご視聴ありがとうございましたなのだ！", "duration": 2.5},
        {"speaker": "shikoku_metan", "text": "チャンネル登録と高評価、よろしくお願いします！", "duration": 9.5},
    ],
}


def _load_ending_config(warnings: Optional[list[str]]) -> dict:
    try:
        config = json.loads(ENDING_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or not isinstance(config.get("lines"), list) or not config["lines"]:
            raise ValueError("lines（セリフ）がありません")
        return {**_DEFAULT_ENDING, **config}
    except FileNotFoundError:
        return dict(_DEFAULT_ENDING)
    except Exception as e:  # noqa: BLE001 - 設定ファイルの不備でシーン生成自体は止めない
        if warnings is not None:
            warnings.append(f"config/ending.json を読み込めなかったため、標準のエンディングにしました（{e}）。")
        return dict(_DEFAULT_ENDING)


def _is_positive_number(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def build_ending_scenes_list(warnings: Optional[list[str]] = None) -> list[Scene]:
    """エンディングのシーンを作る（config/ending.json の設定に従う）。

    2人とも表示・同じ表情（既定は笑顔）のまま、画面上部に挨拶の見出し文字を出し、
    セリフを字幕なしで1人ずつ読み上げる。秒数はセリフごとの duration（既定: ずんだもん2.5秒・めたん9.5秒）で、
    指定の無いセリフは全体の秒数（既定12秒）の残りを等分する。
    """
    config = _load_ending_config(warnings)
    label_map = script_import._build_speaker_label_map()
    characters = list_characters() or ["zundamon", "shikoku_metan"]
    lines = [line for line in config["lines"] if isinstance(line, dict) and str(line.get("text") or "").strip()]
    # セリフごとに duration が書かれていればそれを使い、無いセリフは全体の秒数の残りを等分する
    total = max(1.0, float(config.get("duration") or DEFAULT_ENDING_SECONDS))
    fixed = {i: float(line["duration"]) for i, line in enumerate(lines) if _is_positive_number(line.get("duration"))}
    rest = [i for i in range(len(lines)) if i not in fixed]
    share = max(1.0, (total - sum(fixed.values())) / len(rest)) if rest else 0.0
    durations = [round(fixed.get(i, share), 2) for i in range(len(lines))]
    expression = str(config.get("expression") or "").strip() or None
    se_path = None
    if config.get("se"):
        found = find_se(str(config["se"]))
        if found is None and warnings is not None:
            warnings.append(f"エンディングの効果音「{config['se']}」が assets/se/ に見つからないため、鳴らさないことにしました。")
        se_path = str(found) if found else None

    scenes = []
    for i, line in enumerate(lines):
        speaker = _resolve_speaker(line.get("speaker"), label_map) or characters[i % len(characters)]
        line_expression = str(line.get("expression") or "").strip() or expression
        scenes.append(Scene(
            speaker=speaker,
            expression=line_expression if line_expression and has_expression_assets(speaker, line_expression)
            else get_default_expression(speaker),
            text=str(line["text"]).strip(),
            duration=durations[i],
            section="ending",
            se_path=se_path if i == 0 else None,
            partner_expression=expression,
            show_telop=False,
            headline=str(config.get("headline") or ""),
        ))
    return scenes


def has_ending(scenes: list[Scene]) -> bool:
    return any(scene.section == "ending" for scene in scenes)


def _parse_hidden(raw, label_map: dict[str, str], where: str, warnings: list[str]) -> list[str]:
    """"hide" の値（話者名1つ or リスト）を、キャラクターキーのリストに変換する。"""
    if not raw:
        return []
    names = raw if isinstance(raw, list) else [raw]
    hidden = []
    for name in names:
        key = _resolve_speaker(name, label_map)
        if key is None:
            warnings.append(f"{where}: hide の話者「{name}」を認識できないため無視しました。")
        elif key not in hidden:
            hidden.append(key)
    return hidden


BOARD_HOLD_BASE_SECONDS = 0.3     # 黒板に文字が書かれたシーンで、読む時間として足す秒数（最低限）
BOARD_HOLD_CHARS_PER_SECOND = 14  # 足す秒数の計算に使う、新しく書かれた文字を読む速さ（字/秒。セリフを聞きながら読む前提）
BOARD_HOLD_MAX_SECONDS = 3.0


def _board_text(scene: Scene) -> tuple[Optional[tuple], list[str]]:
    """シーンで画面に見えている黒板・イラストの (キー, 見えている文字の一覧)。何も無ければ (None, [])。"""
    if scene.card_text or scene.silent:
        return None, []  # 場面転換テロップ・考える間（直前と同じ黒板のまま）では、読む時間を足さない
    if scene.illustration_path:
        return ("media", scene.illustration_path), [scene.illustration_caption]
    if scene.note_text:
        return ("note", scene.note_text, scene.note_focus), [scene.note_text, scene.note_meaning]
    if scene.has_slide and scene.show_board:
        bullets = slide_renderer_normalize(scene.slide_bullets)
        shown = bullets if scene.slide_reveal is None else bullets[:max(0, scene.slide_reveal)]
        return ("slide", scene.slide_title.strip(), tuple(bullets)), [scene.slide_title] + shown
    return None, []


def apply_board_hold(scenes: list[Scene], read_time: float = 1.0) -> None:
    """黒板（イラストの説明を含む）に新しく文字が出たシーンに、読む時間をセリフのあとに足す。

    すぐ次のシーンへ流れて黒板の文章を読み切れない、という問題への対策。新しく書かれた文字数に応じて
    秒数を足し（read_time 倍。0 なら足さない）、足した秒数は Scene.board_hold に記録して、設定を変えたときに
    差し替えられるようにする（表示秒数 duration はこの秒数を含む）。
    """
    prev_key, prev_text = None, []
    for scene in scenes:
        key, text = _board_text(scene)
        if key is None:
            new_chars = 0
        elif key == prev_key:
            new_chars = sum(len(t) for t in text[len(prev_text):])  # 同じ黒板に書き足された行だけ
        else:
            new_chars = sum(len(t) for t in text)  # 黒板・イラストが新しく出た
        hold = 0.0
        if new_chars and read_time > 0:
            hold = min(BOARD_HOLD_MAX_SECONDS, BOARD_HOLD_BASE_SECONDS + new_chars / BOARD_HOLD_CHARS_PER_SECOND)
            hold = round(hold * read_time, 1)
        scene.duration = round(max(MIN_SCENE_SECONDS, scene.duration - scene.board_hold + hold), 2)
        scene.board_hold = hold
        if key is not None:
            prev_key, prev_text = key, text
        elif not (scene.card_text or scene.silent):  # テロップ・考える間は黒板を片付けない
            prev_key, prev_text = None, []


def split_long_scenes(scenes: list[Scene], line_chars: int, speech_speed: float = DEFAULT_SPEECH_SPEED) -> list[Scene]:
    """字幕が TELOP_MAX_LINES 行（1行 line_chars 字）に収まらないセリフを、文の切れ目で複数のシーンに分ける。

    分けた2つ目以降のシーンは、話者・表情・場面・黒板・イラストなどは同じまま、効果音・本の表紙の登場・
    カメラの指定は付けない（同じ演出が続けて起きないように）。
    """
    max_chars = line_chars * TELOP_MAX_LINES
    result: list[Scene] = []
    for scene in scenes:
        text = scene.text.strip()
        if not text or scene.lang == "en" or scene.silent or scene.reading \
                or len(split_natural(text, line_chars)) <= TELOP_MAX_LINES:
            result.append(scene)
            continue
        chunks = []
        for chunk in split_natural(text, max_chars):
            # 自然な位置で分けても字幕が3行以上になる塊は、さらに分ける
            if len(split_natural(chunk, line_chars)) > TELOP_MAX_LINES:
                chunks.extend(split_natural(chunk, line_chars))
            else:
                chunks.append(chunk)
        for j, chunk in enumerate(chunks):
            if j == 0:
                part = replace(scene, text=chunk, duration=estimate_scene_duration(chunk, speech_speed))
            else:
                part = replace(
                    scene, id=uuid.uuid4().hex[:8], text=chunk, duration=estimate_scene_duration(chunk, speech_speed),
                    se_path=None, show_book_cover=False, camera="auto", shake="auto",
                )
            result.append(part)
    return result


def _choice(value, labels: dict[str, str], default: str, where: str, name: str, warnings: list[str]) -> str:
    if value is None or value == "":
        return default
    key = str(value).strip().lower()
    if key in labels:
        return key
    warnings.append(f"{where}: {name}「{value}」は認識できないため「{labels[default]}」にしました。")
    return default


def _parse_shake(value) -> str:
    if value is None or value == "":
        return "auto"
    if isinstance(value, bool):
        return "on" if value else "off"
    key = str(value).strip().lower()
    return key if key in SHAKE_LABELS else "auto"


MOOD_SKIP_FIRST_SCENES = 1  # 動画の最初のシーンには雰囲気を付けない（冒頭から暗いと、見る前に離れられやすい）
MOOD_MIN_RUN = 2            # 雰囲気は、この数以上のシーンに続けて付ける（1シーンだけだとチカチカする）
MOOD_MAX_RUNS = 3           # 1本の動画で雰囲気を付ける場面の数の上限（使いすぎると効果が薄れ、目も疲れる）


def tidy_moods(scenes: list[Scene]) -> None:
    """背景の雰囲気（Scene.mood）を、感情のピークの場面だけに、まとまって付くように整える。

    1. 動画の最初の MOOD_SKIP_FIRST_SCENES シーンからは外す
    2. 同じ雰囲気の間に1シーンだけ雰囲気の無いシーンが挟まっていたら、つなげる（点滅しないように）
    3. MOOD_MIN_RUN シーン未満しか続かない雰囲気は外す
    4. 雰囲気の場面が MOOD_MAX_RUNS より多ければ、長く続く場面から残し、ほかは外す
    場面転換テロップ（card_text）は数えない（テロップをはさんでも同じ場面として扱う）。
    """
    idx = [i for i, s in enumerate(scenes) if not s.card_text]
    for i in idx[:MOOD_SKIP_FIRST_SCENES]:
        scenes[i].mood = ""
    for a, b, c in zip(idx, idx[1:], idx[2:]):
        if scenes[a].mood and scenes[a].mood == scenes[c].mood and not scenes[b].mood:
            scenes[b].mood = scenes[a].mood
    runs: list[list[int]] = []
    for i in idx:
        mood = scenes[i].mood
        if mood and runs and scenes[runs[-1][-1]].mood == mood and runs[-1][-1] == idx[idx.index(i) - 1]:
            runs[-1].append(i)
        elif mood:
            runs.append([i])
    keep = [r for r in runs if len(r) >= MOOD_MIN_RUN]
    keep = sorted(sorted(keep, key=len, reverse=True)[:MOOD_MAX_RUNS], key=lambda r: r[0])
    kept = {i for r in keep for i in r}
    for r in runs:
        for i in r:
            if i not in kept:
                scenes[i].mood = ""


def keep_dialog_illustration(block_scenes: list[Scene]) -> None:
    """英会話のダイアログ（会話を聞く場面）では、英語の会話が続いている間、場面のイラストを出したままにする。

    イラストが指定された最初のシーンから、そのブロックの最後の英語のセリフまで、同じイラスト
    （まだ用意されていない依頼中のイラストも同じ名前で）を付ける。会話の途中で絵が消えないように。
    """
    scenes = [s for s in block_scenes if not s.card_text]
    start = next((i for i, s in enumerate(scenes) if s.illustration_path or s.illustration_name), None)
    last_en = max((i for i, s in enumerate(scenes) if s.lang == "en"), default=None)
    if start is None or last_en is None or last_en < start:
        return
    source = scenes[start]
    for scene in scenes[start + 1:last_en + 1]:
        if not (scene.illustration_path or scene.illustration_name):
            scene.illustration_path = source.illustration_path
            scene.illustration_name = source.illustration_name
            scene.illustration_request = source.illustration_request
            scene.illustration_caption = source.illustration_caption


def keep_board_shown(block_scenes: list[Scene]) -> None:
    """黒板は、一度出したらそのブロック（1つのポイント）の最後まで出したままにする。

    セリフごとに黒板が消えたり出たりすると、すぐ流れて読めないため。黒板を出したあとの会話だけのセリフも
    黒板を出す（イラストを出すセリフはイラストが優先され、そのあと黒板に戻る）。
    """
    shown = False
    for scene in block_scenes:
        if scene.card_text or not scene.has_slide:
            continue
        if scene.show_board:
            shown = True
        elif shown:
            scene.show_board = True


REVEAL_ALL_SECTIONS = ("summary",)          # まとめの黒板は、最初から全部の行を一度に出す
ANSWER_SECTIONS = ("quiz", "review")        # 答えを黒板に書く場面（答えを言うまで、その行を出さない）
MATCH_MIN_SCORE = 0.3                       # 黒板の文とセリフが「同じ内容を話している」とみなす似ている度合い
_MATCH_STRIP = re.compile(r"[\s\*・、。，．,.!！?？「」『』（）()〜~―—\-:：;；\"'’]+")


def _bigrams(text: str) -> set[str]:
    text = _MATCH_STRIP.sub("", (text or "").lower())
    return {text[i:i + 2] for i in range(len(text) - 1)} or ({text} if text else set())


def _similarity(bullet: str, line: str) -> float:
    """黒板の1行の文字の並びのうち、セリフに出てくる割合（0〜1）。"""
    a, b = _bigrams(bullet), _bigrams(line)
    return len(a & b) / len(a) if a else 0.0


def _match_bullets(bullets: list[str], candidates: list[tuple[int, Scene]], strict: bool = False) -> Optional[list[int]]:
    """各行を、その内容を初めて話すセリフ（順番は前から）に対応させ、セリフの位置のリストを返す。

    対応が見つからない行は、前の行の次のセリフに書き足す（後ろの行より遅くならないようにする）。
    strict=True（答えの行）は、見つからない行が1つでもあれば None。1行も見つからなければ None。
    """
    found: list[Optional[int]] = []
    start = 0
    for bullet in bullets:
        best, best_score = None, MATCH_MIN_SCORE
        for k in range(start, len(candidates)):
            score = _similarity(bullet, f"{candidates[k][1].text} {candidates[k][1].translation}")
            if score > best_score + 0.05 or (best is None and score >= best_score):
                best, best_score = k, score
        if best is not None:
            start = best + 1 if best + 1 < len(candidates) else best
        found.append(best)
    if all(k is None for k in found) or (strict and any(k is None for k in found)):
        return None
    positions: list[int] = []
    for n, k in enumerate(found):
        if k is None:
            prev = positions[-1] if positions else candidates[0][0] - 1
            nxt = next((candidates[j][0] for j in found[n + 1:] if j is not None), None)
            pos = prev + 1
            if nxt is not None:
                pos = min(pos, nxt)
            positions.append(max(pos, candidates[0][0]))
        else:
            positions.append(candidates[k][0])
    return positions


def assign_bullet_reveal(block_scenes: list[Scene], section: str = "") -> None:
    """同じ黒板を使うシーン（1ブロック）に、表示する箇条書きの数を振り分ける（1行ずつ書き足す演出）。

    各行は、その内容を初めて話すセリフで書き足す（行とセリフの内容がずれないように）:
      1. 台本のセリフに "bullet": 行番号 があれば、そのセリフでその行を書く
      2. 無ければ、黒板の文とセリフの似ている度合いで、その行を話しているセリフを探す
      3. それでも対応が付かなければ、黒板を出しているセリフの数に比例して振り分ける
    まとめ（summary）は最初から全部の行を出す。瞬発トレーニング・ふりかえり（quiz / review）は答えの行なので、
    答えを言う英語のセリフまでその行を出さない（先に答えが見えないように）。
    """
    block_scenes = [s for s in block_scenes if not s.card_text]  # 場面転換テロップは黒板を出さない
    if not block_scenes or not block_scenes[0].has_slide:
        return
    bullets = slide_renderer_normalize(block_scenes[0].slide_bullets)
    total = len(bullets)
    if total <= 1 or section in REVEAL_ALL_SECTIONS:
        for scene in block_scenes:
            scene.slide_reveal = None
        return

    positions: Optional[list[int]] = None
    explicit = {s.bullet_ref: i for i, s in reversed(list(enumerate(block_scenes))) if 1 <= s.bullet_ref <= total}
    if len(explicit) == total:
        positions = [explicit[k] for k in range(1, total + 1)]
    if positions is None:
        candidates = [(i, s) for i, s in enumerate(block_scenes) if s.text.strip() and not s.silent]
        if section in ANSWER_SECTIONS:
            candidates = [(i, s) for i, s in candidates if s.lang == "en"] or candidates
        positions = _match_bullets(bullets, candidates, strict=section in ANSWER_SECTIONS)
        if positions is not None:  # ブロックの最後のセリフまでには、必ず全部の行を出す
            positions = [min(p, len(block_scenes) - 1) for p in positions]
    if positions is None and section in ANSWER_SECTIONS:
        # 答えの英語のセリフの順に1行ずつ（問題を出している間は答えを見せない）
        answers = [i for i, s in enumerate(block_scenes) if s.lang == "en" and not s.silent]
        if len(answers) >= total:
            positions = answers[:total]
    if positions is not None:
        for i, scene in enumerate(block_scenes):
            shown = sum(1 for p in positions if p <= i)
            scene.slide_reveal = None if shown >= total else shown
        return

    # 黒板を出すセリフだけで書き足していく（会話だけのセリフでは黒板が見えないため）。
    # 会話だけのセリフは、直前に黒板を出したセリフと同じ行数にしておく
    shown_scenes = [s for s in block_scenes if s.show_board and not s.illustration_path] or block_scenes
    count = len(shown_scenes)
    shown = 1
    for scene in block_scenes:
        if scene in shown_scenes:
            j = shown_scenes.index(scene)
            shown = min(total, max(1, round((j + 1) * total / count)))
        scene.slide_reveal = None if shown >= total else shown


def slide_renderer_normalize(bullets: list[str]) -> list[str]:
    from src.services.slide_renderer import normalize_bullets  # slide_renderer は重いので必要な時だけ読み込む

    return normalize_bullets(bullets)


def estimate_scene_duration(text: str, speech_speed: float = DEFAULT_SPEECH_SPEED, lang: str = "ja") -> float:
    if lang == "en":  # 英語は単語数から（ネイティブ音声が届いたら、その長さに置き換わる）
        words = len(text.split())
        return max(MIN_SCENE_SECONDS, round(words / ENGLISH_WORDS_PER_SECOND + 0.4 + LINE_GAP_SECONDS, 1))
    chars_per_second = ESTIMATE_CHARS_PER_SECOND * max(0.5, speech_speed)
    return max(MIN_SCENE_SECONDS, round(len(text) / chars_per_second + LINE_GAP_SECONDS, 1))


def build_scenes(
    data: dict,
    use_voicevox_timing: bool = True,
    reading_dict: Optional[list[dict]] = None,
    add_ending: bool = False,
    reveal_bullets: bool = True,
    speech_speed: float = DEFAULT_SPEECH_SPEED,
    video_format: VideoFormat = VideoFormat.LANDSCAPE,
    board_pause: float = 0.0,
) -> BookScriptResult:
    """台本JSON（load_book_script() の戻り値）から Scene のリストを作る。

    add_ending=True なら、台本にエンディングのブロックが無い場合に、最後にエンディングのシーン
    （build_ending_scenes_list()）を付け足す。

    Raises:
        BookScriptError: セリフが1つも読み取れなかった場合。
    """
    warnings: list[str] = []
    blocks = data.get("blocks")
    if blocks is None:
        blocks = data.get("sections")
    if not isinstance(blocks, list) or not blocks:
        raise BookScriptError('台本JSONに "blocks"（場面ごとのセリフのリスト）が見つかりませんでした。')
    # ショート動画（約1分）は台本の最後の一言で締めるため、12秒のエンディングは付けない
    needs_ending = add_ending and str(data.get("style") or "").lower() != "short" and not any(
        isinstance(b, dict) and _SECTION_ALIASES.get(str(b.get("section") or "").strip().lower()) == "ending"
        for b in blocks
    )

    characters = list_characters() or ["zundamon", "shikoku_metan"]
    is_english_lesson = str(data.get("source_kind") or "").strip().lower() == "english"
    label_map = script_import._build_speaker_label_map()
    expression_maps = script_import._build_expression_label_maps(characters)

    scenes: list[Scene] = []
    for block_no, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            warnings.append(f"ブロック{block_no}: 形式が正しくないため読み飛ばしました。")
            continue
        section = _normalize_section(block.get("section"), block_no, warnings)
        phase = _normalize_phase(section, block.get("phase"))
        slide_title, slide_bullets = _parse_slide(block.get("slide"), block_no, warnings)
        slide_numbered = isinstance(block.get("slide"), dict) and block["slide"].get("numbered") is True
        block_mood = _parse_mood(block.get("mood"))
        block_hidden = _parse_hidden(block.get("hide"), label_map, f"ブロック{block_no}", warnings)
        lines = block.get("lines") or []
        if not isinstance(lines, list) or not lines:
            warnings.append(f"ブロック{block_no}: セリフ(lines)が無いため読み飛ばしました。")
            continue
        block_scenes_start = len(scenes)

        for line_no, line in enumerate(lines, start=1):
            where = f"ブロック{block_no}の{line_no}番目のセリフ"
            if not isinstance(line, dict):
                warnings.append(f"{where}: 形式が正しくないため読み飛ばしました。")
                continue
            text = str(line.get("text") or "").strip()
            card = str(line.get("card") or "").strip()
            if card:
                # 場面転換テロップ（「3日後…」など）: 2人を隠し、全画面に文字だけを出すシーン
                scenes.append(Scene(
                    speaker=scenes[-1].speaker if scenes else characters[0], text="", card_text=card,
                    duration=_parse_card_seconds(line.get("card_seconds")), section=section, phase=phase,
                    hidden_characters=list(characters), show_telop=False,
                    se_path=str(find_se(str(line.get("card_se")))) if line.get("card_se") and find_se(str(line.get("card_se"))) else None,
                ))
            if not text:
                continue
            speaker = _resolve_speaker(line.get("speaker"), label_map)
            if speaker is None:
                fallback = scenes[-1].speaker if scenes else characters[0]
                warnings.append(
                    f"{where}: 話者「{line.get('speaker')}」を認識できないため、"
                    f"直前と同じ話者（{get_character_display_name(fallback)}）にしました。"
                )
                speaker = fallback
            expression = _resolve_expression(line.get("expression"), speaker, expression_maps)
            if expression is None:
                if line.get("expression"):
                    warnings.append(f"{where}: 表情「{line.get('expression')}」が見つからないため、既定の表情にしました。")
                expression = get_default_expression(speaker)
            se_path = None
            if line.get("se"):
                found = find_se(str(line.get("se")))
                if found is None:
                    warnings.append(f"{where}: 効果音「{line.get('se')}」が assets/se/ に見つからないため、鳴らさないことにしました。")
                else:
                    se_path = str(found)
            lang = "en" if str(line.get("lang") or "").strip().lower() in ("en", "english", "英語") else "ja"
            reading = str(line.get("reading") or "").strip()
            scenes.append(
                Scene(
                    speaker=speaker,
                    expression=expression,
                    text=text,
                    duration=estimate_scene_duration(reading or text, speech_speed, "ja" if reading else lang),
                    lang=lang,
                    translation=str(line.get("ja") or line.get("translation") or "").strip(),
                    reading=reading,
                    audio_id=_FILENAME_UNSAFE.sub("", str(line.get("audio") or "").strip()),
                    section=section,
                    phase=phase,
                    slide_title=slide_title,
                    slide_bullets=list(slide_bullets),
                    slide_numbered=slide_numbered,
                    mood=_parse_mood(line.get("mood")) or block_mood,
                    se_path=se_path,
                    hidden_characters=sorted(
                        set(block_hidden) | set(_parse_hidden(line.get("hide"), label_map, where, warnings))
                    ),
                    camera=_choice(line.get("camera"), CAMERA_LABELS, "auto", where, "camera", warnings),
                    shake=_parse_shake(line.get("shake")),
                    char_motion=_choice(line.get("motion"), CHAR_MOTION_LABELS, "auto", where, "motion", warnings),
                    show_book_cover=line.get("show_book") is True,
                    show_board=line.get("board") is not False,
                    illustration_path=_resolve_illustration(line.get("image"), where, warnings),
                    illustration_caption=str(line.get("caption") or "").strip(),
                    illustration_request=str(line.get("image_request") or "").strip(),
                    illustration_name=_clean_illustration_name(line.get("image_name")),
                    **_parse_note(line.get("note")),
                    bullet_ref=_parse_bullet_ref(line.get("bullet")),
                )
            )
            # "pause": 秒数 … セリフのあとに、視聴者がリピート・回答する間（音声なし。字幕と見出しは出したまま）
            pause = _parse_pause(line.get("pause"))
            style = _parse_pause_style(line.get("pause_style"), section)
            base = scenes[-1]
            pause_text = str(line.get("pause_text") or "").strip()
            common = dict(
                id=uuid.uuid4().hex[:8], se_path=None, show_book_cover=False,
                camera="none", shake="none", char_motion="none", pause_style=style,
                show_telop=line.get("pause_telop") is not False,
            )
            if style == "shadow" and base.lang == "en":
                # シャドーイング: 3・2・1 のあと、お手本の音声をもう一度流す（視聴者は音声に重ねて言う）
                scenes.append(replace(
                    base, **common, silent=False, lead_in=COUNTDOWN_SECONDS,
                    duration=round(COUNTDOWN_SECONDS + base.duration, 2),
                    headline=pause_text or DEFAULT_SHADOW_TEXT,
                ))
            elif pause and (style != "think" or is_english_lesson):
                # 書籍・論文の解説では「考えてみて」の無音の間は入れない（話のテンポが悪くなるため）。
                # 英会話のリピート・瞬発トレーニングの間は、練習に必要なので残す
                lead = COUNTDOWN_SECONDS if style == "repeat" else 0.0
                scenes.append(replace(
                    base, **common, silent=True, lead_in=lead, duration=round(lead + pause, 2),
                    headline=pause_text or (DEFAULT_PAUSE_TEXT if style == "repeat" else DEFAULT_THINK_TEXT),
                ))

        _apply_block_background(scenes[block_scenes_start:], block, block_no, warnings)
        _apply_block_guests(scenes[block_scenes_start:], block, label_map)
        keep_board_shown(scenes[block_scenes_start:])
        if section == "dialog":
            keep_dialog_illustration(scenes[block_scenes_start:])
        if reveal_bullets and block.get("reveal", True) is not False:
            assign_bullet_reveal(scenes[block_scenes_start:], section)

    if not scenes:
        raise BookScriptError("台本JSONから読み取れるセリフがありませんでした。")
    # 字幕が2行に収まらない長いセリフは、文の切れ目で複数のシーンに分ける（縦画面17字・横画面30字/行）
    is_portrait = str(data.get("style") or "").lower() == "short" or video_format == VideoFormat.PORTRAIT
    line_chars = TELOP_CHARS_PER_LINE["portrait" if is_portrait else "landscape"]
    scenes = split_long_scenes(scenes, line_chars, speech_speed)
    style_problems = check_character_style(scenes)
    if style_problems:
        warnings.append(
            f"キャラクターの口調ルールに合わないセリフが{len(style_problems)}件あります（シーン編集で直せます）:\n- "
            + "\n- ".join(style_problems[:10]) + ("\n- …" if len(style_problems) > 10 else "")
        )

    used_voicevox = False
    if use_voicevox_timing:
        used_voicevox = _fit_durations_with_voicevox(
            scenes, merge_readings(reading_dict or [], _parse_readings(data.get("readings"))), warnings, speech_speed
        )
    tidy_moods(scenes)
    apply_board_hold(scenes, board_pause)
    if needs_ending:
        scenes.extend(build_ending_scenes_list(warnings))

    book_title = str(data.get("book_title") or data.get("title") or "").strip()
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[,、\n]", tags)]
    return BookScriptResult(
        book_title=book_title,
        scenes=scenes,
        warnings=warnings,
        used_voicevox_timing=used_voicevox,
        author=str(data.get("author") or data.get("book_author") or "").strip(),
        video_title=str(data.get("video_title") or "").strip(),
        video_description=str(data.get("video_description") or data.get("description") or "").strip(),
        tags=[str(t).strip() for t in tags if str(t).strip()],
        title_candidates=_str_list(data.get("title_candidates")),
        description_lead=str(data.get("description_lead") or "").strip(),
        hashtags=[h.lstrip("#＃") for h in _str_list(data.get("hashtags"))],
        style=str(data.get("style") or "").strip().lower() if str(data.get("style") or "").strip().lower()
        in ("normal", "short") else "",
        readings=_parse_readings(data.get("readings")),
        source_kind=str(data.get("source_kind") or "").strip().lower()
        if str(data.get("source_kind") or "").strip().lower() in ("research", "english")
        else ("book" if data.get("source_kind") else ""),
        lesson=data.get("lesson") if isinstance(data.get("lesson"), dict) else {},
        thumbnail={k: v for k, v in data["thumbnail"].items() if v} if isinstance(data.get("thumbnail"), dict) else {},
        sources=_parse_sources(data.get("sources")),
        promo_short=promo_short_data(data),
    )


def promo_short_data(data: dict) -> dict:
    """台本JSONの本編紹介ショート（"promo_short"）。ブロックが無ければ空。"""
    promo = data.get("promo_short")
    if isinstance(promo, dict) and isinstance(promo.get("blocks"), list) and promo["blocks"]:
        return promo
    return {}


# 本編紹介ショートに、本編のプロジェクトから引き継ぐ設定
_PROMO_INHERITED = (
    "speech_speed", "reading_dict", "bgm_path", "bgm_volume", "se_volume", "section_bgm",
    "pr_label_enabled", "pr_label_text", "book_cover_path", "auto_camera", "auto_shake", "char_bob",
    "auto_jump", "char_slide_in", "slide_transition", "background_blur",
)


def build_promo_project(main: Project, use_voicevox_timing: bool = True) -> tuple[Project, list[str]]:
    """本編のプロジェクトが持つ本編紹介ショートの台本から、縦画面のショートのプロジェクトを作る。"""
    if not main.promo_short.get("blocks"):
        raise BookScriptError("本編紹介ショートの台本がありません。")
    data = json.loads(json.dumps(main.promo_short, ensure_ascii=False))
    for block in data["blocks"]:
        for line in block.get("lines", []) if isinstance(block, dict) else []:
            if isinstance(line, dict):
                line.pop("pause", None)  # ショートには練習の間を入れない
    data.update({
        "style": "short",
        "book_title": main.book_title,
        "author": main.book_author,
        "source_kind": main.source_kind,
        "sources": main.sources,
        "lesson": main.lesson,
    })
    short = Project()
    for attr in _PROMO_INHERITED:
        setattr(short, attr, json.loads(json.dumps(getattr(main, attr))))
    short.format = VideoFormat.PORTRAIT
    result = build_scenes(
        data, use_voicevox_timing=use_voicevox_timing, reading_dict=main.reading_dict, add_ending=False,
        speech_speed=main.speech_speed, video_format=VideoFormat.PORTRAIT, board_pause=main.board_pause,
    )
    short.promo_of = main.video_title or main.book_title or "本編"
    apply_to_project(short, result)
    short.name = f"ショート: {main.book_title or short.promo_of}"
    short.promo_short = {}
    from src.services import video_metadata  # 循環importを避けるため関数内で読み込む

    video_metadata.apply_generated_metadata(short, overwrite=False)
    return short, result.warnings


def _parse_sources(value) -> list[dict]:
    """出典のリスト [{"title","publisher","year","kind","url"}]（文字列だけの項目は title として扱う）。"""
    sources = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str) and item.strip():
            item = {"title": item.strip()}
        if not isinstance(item, dict):
            continue
        source = {k: str(item.get(k) or "").strip() for k in ("title", "publisher", "year", "kind", "url")}
        if source["title"] or source["url"]:
            sources.append(source)
    return sources


def _str_list(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,、\n]", value)
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _fit_durations_with_voicevox(
    scenes: list[Scene], reading_dict: Optional[list[dict]], warnings: list[str],
    speech_speed: float = DEFAULT_SPEECH_SPEED,
) -> bool:
    """VOICEVOXで各セリフの実際の読み上げ時間を測り、表示秒数に反映する。起動していなければ何もしない。"""
    try:
        voicevox_client.ensure_engine_running()
    except VoicevoxConnectionError:
        warnings.append(
            "VOICEVOXが起動していないため、表示秒数は文字数からの概算にしました"
            "（VOICEVOXを起動してから生成し直すと、実際の読み上げ時間に合わせられます）。"
        )
        return False
    for i, scene in enumerate(scenes, start=1):
        if not scene.text.strip() or scene.silent or scene.voice_path:
            continue
        if scene.lang == "en" and not scene.reading.strip():
            continue  # ネイティブ音声を使う英語のセリフ（音声が届いたらその長さに合わせる）
        try:
            natural = voicevox_client.measure_natural_duration(
                scene.reading.strip() or scene.text, scene.speaker, reading_dict=reading_dict, speech_speed=speech_speed
            )
            scene.duration = max(MIN_SCENE_SECONDS, round(natural + LINE_GAP_SECONDS, 1))
        except (VoicevoxConnectionError, VoicevoxSynthesisError) as e:
            warnings.append(f"シーン{i}: 読み上げ時間を測れなかったため、文字数からの概算にしました（{e}）")
    return True


def build_ending_scenes() -> BookScriptResult:
    """エンディングのシーンだけを作る（既存のプロジェクトの最後に後から付け足す用）。"""
    warnings: list[str] = []
    scenes = build_ending_scenes_list(warnings)
    return BookScriptResult(book_title="", scenes=scenes, warnings=warnings)


# キャラクターの口調ルール（台本のチェック用。プロンプト側は src.services.book_ai._CHARACTERS_TEXT）
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?…])")
_TRAILING = "。！？!?…‥ー〜～w♪、, 　"
_ZUNDA_ENDINGS = ("のだ", "なのだ")
_ZUNDA_FORBIDDEN = re.compile(r"(です|ます|でした|ました|ません|でしょう)(?=[。！？!?…ねよかー〜]|$)")
_METAN_FORBIDDEN = re.compile(r"(ですわ|ますわ|ですの|ますの|ございます)")
_INTERJECTION = re.compile(r"^[ぁ-ゖァ-ヺー]{1,4}$")  # 「えっ」「はぁ」「うぅ」のような仮名だけの短い感嘆は語尾ルールの対象外


def check_character_style(scenes: list[Scene]) -> list[str]:
    """キャラクターの口調ルールに反しているセリフを探して、注意メッセージのリストを返す。

    - ずんだもん: すべての文末が「〜のだ」「〜なのだ」（疑問・感嘆の記号が付いてもよい）。敬語（です・ます）禁止
    - 四国めたん: 「〜ですわ」「〜ますわ」「〜ですの」などの過度な丁寧語を使わない
    """
    problems = []
    for i, scene in enumerate(scenes, start=1):
        text = scene.text.strip()
        if not text or scene.section == "ending":  # エンディングの挨拶は利用者が指定した固定文なので対象外
            continue
        if scene.lang == "en" or scene.silent:  # 英語のセリフ・リピートの間は口調ルールの対象外
            continue
        if scene.speaker == "zundamon":
            bad_endings = []
            for sentence in (part.strip() for part in _SENTENCE_SPLIT.split(text)):
                core = sentence.rstrip(_TRAILING)
                if not core or _INTERJECTION.match(core):
                    continue
                if not core.endswith(_ZUNDA_ENDINGS):
                    bad_endings.append(sentence)
            if bad_endings or _ZUNDA_FORBIDDEN.search(text) or "ございます" in text:
                problems.append(f"シーン{i}（ずんだもん）: 語尾が「〜のだ」になっていない／敬語がある →「{text}」")
        elif scene.speaker == "shikoku_metan" and _METAN_FORBIDDEN.search(text):
            problems.append(f"シーン{i}（四国めたん）: 丁寧すぎる語尾（ですわ・ますわ等）がある →「{text}」")
    return problems


_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def _clean_illustration_name(name) -> str:
    """Claudeが提案したイラストのファイル名から、ファイル名に使えない文字を取り除く。"""
    return _FILENAME_UNSAFE.sub("", str(name or "")).strip(".")[:40]


@dataclass
class IllustrationRequest:
    name: str
    description: str
    captions: list[str]
    scene_numbers: list[int]


def illustration_requests(scenes: list[Scene]) -> list[IllustrationRequest]:
    """まだ用意されていない（assets/illustrations/ に無い）依頼中のイラストの一覧。同じ名前はまとめる。"""
    requests: dict[str, IllustrationRequest] = {}
    for i, scene in enumerate(scenes, start=1):
        if scene.illustration_path or not (scene.illustration_request or scene.illustration_name):
            continue
        name = scene.illustration_name or f"イラスト{len(requests) + 1}"
        req = requests.setdefault(name, IllustrationRequest(name, scene.illustration_request, [], []))
        if scene.illustration_caption and scene.illustration_caption not in req.captions:
            req.captions.append(scene.illustration_caption)
        req.scene_numbers.append(i)
    return list(requests.values())


def _apply_block_guests(block_scenes: list[Scene], block: dict, label_map: dict[str, str]) -> None:
    """ブロックの "guests"（登場するゲスト）と、そのブロックで話すゲストを、ブロックの全シーンに登場させる
    （話すセリフのたびに出たり消えたりしないように）。"""
    guests = set()
    for value in (block.get("guests") or []) if isinstance(block.get("guests"), list) else []:
        key = _resolve_speaker(value, label_map)
        if key in GUEST_CHARACTERS:
            guests.add(key)
    guests |= {s.speaker for s in block_scenes if s.speaker in GUEST_CHARACTERS}
    for scene in block_scenes:
        if not scene.card_text:
            scene.guests = sorted(guests)


def _apply_block_background(block_scenes: list[Scene], block: dict, block_no: int, warnings: list[str]) -> None:
    """ブロックの "background"（手元の背景の名前）/ "background_request"・"background_name"（欲しい背景の依頼）を、
    そのブロックの全シーンに付ける。指定が無ければ、いつもの部屋（共通の背景）のまま。"""
    name = str(block.get("background") or "").strip()
    request = str(block.get("background_request") or "").strip()
    wanted = _clean_illustration_name(block.get("background_name"))
    path = None
    if name:
        found = find_background(name)
        if found is None:
            warnings.append(f"ブロック{block_no}: 背景「{name}」が assets/backgrounds/ に見つからないため、欲しい背景として一覧に出します。")
            wanted = wanted or _clean_illustration_name(name)
        else:
            path = str(found)
    if not (path or wanted):
        return
    for scene in block_scenes:
        if path:
            scene.background_path = path
        else:
            scene.background_name, scene.background_request = wanted, request


@dataclass
class BackgroundRequest:
    name: str
    description: str
    scene_numbers: list[int]


def background_requests(scenes: list[Scene]) -> list[BackgroundRequest]:
    """まだ用意されていない（assets/backgrounds/ に無い）依頼中の背景の一覧。同じ名前はまとめる。"""
    requests: dict[str, BackgroundRequest] = {}
    for i, scene in enumerate(scenes, start=1):
        if scene.background_path or not scene.background_name:
            continue
        req = requests.setdefault(scene.background_name, BackgroundRequest(scene.background_name, scene.background_request, []))
        req.scene_numbers.append(i)
    return list(requests.values())


def link_requested_backgrounds(scenes: list[Scene]) -> list[Scene]:
    """依頼中の背景のうち、その名前で assets/backgrounds/ に画像が置かれたものをシーンに反映する。"""
    linked = []
    for scene in scenes:
        if scene.background_path or not scene.background_name:
            continue
        found = find_background(scene.background_name)
        if found is not None:
            scene.background_path = str(found)
            linked.append(scene)
    return linked


def link_requested_illustrations(scenes: list[Scene]) -> list[Scene]:
    """依頼中のイラストのうち、その名前で assets/illustrations/ に画像が置かれたものをシーンに反映する。

    反映したシーンのリストを返す。
    """
    linked = []
    for scene in scenes:
        if scene.illustration_path or not scene.illustration_name:
            continue
        found = find_illustration(scene.illustration_name)
        if found is not None:
            scene.illustration_path = str(found)
            linked.append(scene)
    return linked


def _resolve_illustration(name, where: str, warnings: list[str]) -> Optional[str]:
    """台本の "image"（イラスト名）を assets/illustrations/ の画像のパスにする。見つからなければ警告して表示しない。"""
    if not name:
        return None
    found = find_illustration(str(name))
    if found is None:
        warnings.append(f"{where}: イラスト「{name}」が assets/illustrations/ に見つからないため、表示しないことにしました。")
        return None
    return str(found)


def _parse_readings(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    entries = []
    for item in raw:
        if isinstance(item, dict):
            word, reading = str(item.get("word") or "").strip(), str(item.get("reading") or "").strip()
            if word and reading and word != reading:
                entries.append({"word": word, "reading": reading})
    return entries


def load_default_readings() -> list[dict]:
    """config/reading_defaults.json の既定の読み方（VOICEVOXがよく読み間違える言葉）。"""
    try:
        data = json.loads(READING_DEFAULTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return _parse_readings(data.get("entries") if isinstance(data, dict) else data)


def default_readings_for(scenes: list[Scene]) -> list[dict]:
    """既定の読み方のうち、台本のセリフに出てくる言葉だけを返す。"""
    texts = "\n".join(scene.text for scene in scenes)
    return [entry for entry in load_default_readings() if entry["word"] in texts]


def merge_readings(existing: list[dict], additions: list[dict]) -> list[dict]:
    """読み方辞書に追加する（同じ言葉が既に登録されていれば、利用者が登録した方を優先して残す）。"""
    merged = list(existing)
    known = {str(item.get("word", "")).strip() for item in existing if isinstance(item, dict)}
    for entry in additions:
        if entry["word"] not in known:
            merged.append(dict(entry))
            known.add(entry["word"])
    return merged


def apply_book_cover(project: Project) -> None:
    """本を紹介するシーン（show_book_cover）に、本の表紙画像を表示する設定をする。

    表紙画像があれば資料メディアとして画面中央に表示し、無ければ書名・著者を書いた黒板で代わりに見せる。
    表紙画像を後からアップロードした場合も、これを呼び直せば反映される。
    """
    cover = project.book_cover_path if project.book_cover_path and Path(project.book_cover_path).exists() else None
    title = project.book_title.strip()
    for scene in project.scenes:
        if not scene.show_book_cover:
            continue
        if cover:
            scene.content_media_path = cover
            scene.slide_title, scene.slide_bullets, scene.slide_reveal = "", [], None
        else:
            scene.content_media_path = None
            scene.slide_title = f"『{title}』" if title else "今日の本"
            scene.slide_bullets = [f"{project.book_author.strip()} 著"] if project.book_author.strip() else []
            scene.slide_reveal = None


def apply_to_project(project: Project, result: BookScriptResult, replace: bool = True) -> None:
    """生成したシーンをプロジェクトに反映し、書籍解説用の既定の背景（画面の向きに合わせる）を設定する。"""
    if replace:
        project.scenes = result.scenes
    else:
        project.scenes.extend(result.scenes)
    if result.source_kind:
        project.source_kind = result.source_kind
        project.sources = list(result.sources)
        if result.source_kind in ("research", "english"):
            project.book_author, project.book_cover_path = "", None
        project.lesson = dict(result.lesson) if result.source_kind == "english" else {}
    if result.book_title:
        project.book_title = result.book_title
        if replace:
            prefix = {"research": "解説", "english": "英会話"}.get(project.source_kind, "書籍解説")
            project.name = f"{prefix}: {result.book_title}"
    if result.author:
        project.book_author = result.author
    # 投稿用のタイトル・説明文・タグ: 台本に書かれていればそれを使い、無いものは台本の内容から自動生成する
    from src.services import video_metadata  # 循環importを避けるため関数内で読み込む

    project.title_candidates = list(result.title_candidates)
    if replace or result.promo_short:
        project.promo_short = dict(result.promo_short)
    if replace or result.thumbnail:
        project.thumbnail = dict(result.thumbnail)
    project.description_lead = result.description_lead
    project.hashtags = list(result.hashtags)
    if result.style:
        project.video_style = result.style
        # 通常の動画は横画面、ショートは縦画面にする（背景も下で画面の向きに合わせる）
        project.format = VideoFormat.PORTRAIT if result.style == "short" else VideoFormat.LANDSCAPE
    project.tag_candidates = list(result.tags)
    project.video_title, project.video_description = (
        video_metadata.with_series_tag(
            result.video_title or (result.title_candidates[0] if result.title_candidates else ""), project),
        result.video_description,
    )
    # タグ = AIが考えたタグ（表記ゆれ・変換ミス）＋ 書名・キャラクター名などの書き方の違い（上限500字に収める）
    project.video_tags = video_metadata.build_tags(project)
    video_metadata.apply_generated_metadata(project, overwrite=False)
    # VOICEVOXが読み間違えそうな言葉を読み方辞書に追加する（台本のreadings + 既定の読み方のうち台本に出てくるもの）
    project.reading_dict = merge_readings(project.reading_dict, result.readings + default_readings_for(project.scenes))
    apply_book_cover(project)
    link_requested_illustrations(project.scenes)
    background = DEFAULT_BOOK_BACKGROUNDS.get(project.format)
    if background is not None and background.exists():
        project.common_background_path = str(background)


def sync_background_to_format(project: Project) -> bool:
    """書籍解説用の既定背景を使っている場合に限り、画面の向き（横/縦）に合った方の部屋へ切り替える。

    台本を取り込んだ後に出力フォーマットを切り替えても、背景が自動で追従するようにするため。
    利用者が自分で別の背景を選んでいる場合は何もしない。切り替えた場合は True を返す。
    """
    defaults = {str(p) for p in DEFAULT_BOOK_BACKGROUNDS.values()}
    wanted = DEFAULT_BOOK_BACKGROUNDS.get(project.format)
    if project.common_background_path in defaults and wanted is not None and wanted.exists():
        if project.common_background_path != str(wanted):
            project.common_background_path = str(wanted)
            return True
    return False


@dataclass
class SceneGroup:
    """同じ場面・同じスライドが続くシーンのまとまり（書籍解説モードの一覧表示用）。"""
    section: str
    slide_title: str
    slide_bullets: list[str]
    scenes: list[Scene]
    phase: str = ""

    @property
    def label(self) -> str:
        return phase_label(self.section, self.phase) or SECTION_LABELS.get(self.section, self.section)

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.scenes)


def group_scenes(scenes: list[Scene]) -> list[SceneGroup]:
    """連続するシーンを「場面 + スライドの内容」が同じもの同士でまとめる（台本のブロックにほぼ対応）。"""
    groups: list[SceneGroup] = []
    for scene in scenes:
        last = groups[-1] if groups else None
        if (
            last is not None
            and last.section == scene.section
            and last.phase == scene.phase
            and last.slide_title == scene.slide_title
            and last.slide_bullets == scene.slide_bullets
        ):
            last.scenes.append(scene)
        else:
            groups.append(SceneGroup(scene.section, scene.slide_title, list(scene.slide_bullets), [scene], scene.phase))
    return groups


def section_summary(scenes: list[Scene]) -> list[tuple[str, int, float]]:
    """場面ごとの (ラベル, シーン数, 合計秒数) を、登場順に返す（取り込み結果の表示用）。"""
    summary: list[tuple[str, int, float]] = []
    for scene in scenes:
        label = SECTION_LABELS.get(scene.section, scene.section)
        if summary and summary[-1][0] == label:
            _, count, total = summary[-1]
            summary[-1] = (label, count + 1, total + scene.duration)
        else:
            summary.append((label, 1, scene.duration))
    return summary


# 動作確認・書き方の見本用のサンプル台本（特定の書籍の内容ではなく、一般的な睡眠の知識で構成した架空の本）
SAMPLE_BOOK_SCRIPT = {
    "book_title": "ぐっすり眠るための科学（サンプル）",
    "author": "サンプル著者",
    "readings": [{"word": "寝入りばな", "reading": "ねいりばな"}],
    "blocks": [
        {
            "section": "intro", "phase": "hype", "hide": ["shikoku_metan"],
            "lines": [
                {"speaker": "zundamon", "expression": "happy", "text": "最近ずっと眠いから、今日から毎日8時間寝るって決めたのだ！", "se": "シャキーン1"},
            ],
        },
        {
            "section": "intro", "phase": "fail", "hide": ["shikoku_metan"], "mood": "gloomy",
            "lines": [
                {"speaker": "zundamon", "card": "3日後…", "text": ""},
                {"speaker": "zundamon", "expression": "troubled", "text": "夜9時に布団に入ったのに、2時間ゴロゴロしただけだったのだ…", "se": "ショック1"},
                {"speaker": "zundamon", "expression": "sad", "text": "しかも朝もぜんぜん起きられなかったのだ…"},
            ],
        },
        {
            "section": "intro", "phase": "rescue",
            "lines": [
                {"speaker": "shikoku_metan", "expression": "angry", "text": "そんなんじゃだめよ。", "se": "ビシッとツッコミ2"},
                {"speaker": "zundamon", "expression": "surprised", "text": "どうしてなのだ？長く寝ればいいはずなのだ！"},
                {"speaker": "shikoku_metan", "expression": "normal", "text": "なぜ失敗したのかと、どうすればぐっすり眠れるのかに分けて話すわ。"},
                {"speaker": "shikoku_metan", "expression": "happy", "text": "今日は『ぐっすり眠るための科学』で教えてあげるわ。", "se": "ジャジャーン", "show_book": True},
            ],
        },
        {
            "section": "explain", "phase": "why",
            "slide": {
                "title": "失敗の理由1：時間だけ気にしていた",
                "bullets": [
                    "眠くないのに布団に入っても**寝つけない**",
                    "大事なのは長さより**眠りの質**",
                    "寝入ってすぐの深い眠りで疲れが取れる",
                ],
            },
            "lines": [
                {"speaker": "shikoku_metan", "expression": "normal", "text": "まずは、なぜ失敗したのか見ていくわよ。", "se": "学校のチャイム", "board": False},
                {"speaker": "shikoku_metan", "expression": "normal", "text": "あんた、眠くないのに早く布団に入ったでしょ。", "board": True},
                {"speaker": "zundamon", "expression": "troubled", "text": "早く寝れば長く眠れると思ったのだ…", "board": True},
                {"speaker": "shikoku_metan", "expression": "normal", "text": "この本によると、大事なのは長さより質なのよ。", "board": True},
            ],
        },
        {
            "section": "explain", "phase": "how",
            "slide": {
                "title": "成功のコツ1：眠り始めを深くする",
                "bullets": [
                    "寝入ってすぐに**一晩でいちばん深い眠り**が来る",
                    "この時間の質で翌朝のすっきり感が決まる",
                ],
            },
            "lines": [
                {"speaker": "shikoku_metan", "expression": "happy", "text": "じゃあ、どうすればぐっすり眠れるのか教えてあげるわ。", "se": "シーン切り替え1", "board": False},
                {"speaker": "shikoku_metan", "expression": "normal", "text": "寝入ってすぐに、いちばん深い眠りが来るのよ。", "board": True},
                {"speaker": "zundamon", "expression": "surprised", "text": "じゃあ最初だけ寝ればいいのだ！", "se": "ボヨヨーン", "board": True},
                {"speaker": "shikoku_metan", "expression": "angry", "text": "そういう意味じゃないわよ！", "se": "ビシッとツッコミ2", "board": False},
            ],
        },
        {
            "section": "explain", "phase": "how",
            "slide": {
                "title": "成功のコツ2：体温を味方につける",
                "bullets": [
                    "寝る**1〜2時間前**にお風呂に入る",
                    "上がった体温が下がるときに自然と眠くなる",
                    "寝る直前の熱いお風呂は逆に目がさえる",
                ],
            },
            "lines": [
                {"speaker": "shikoku_metan", "expression": "normal", "text": "次は、寝る1〜2時間前にお風呂に入ること。", "board": True},
                {"speaker": "shikoku_metan", "expression": "happy", "text": "体温が下がっていくときに、自然と眠くなるのよ。", "board": True},
                {"speaker": "zundamon", "expression": "happy", "text": "僕、それ知ってたのだ！", "board": False},
                {"speaker": "shikoku_metan", "expression": "troubled", "text": "あんた、さっきまで8時間寝る作戦だったじゃないの。", "board": False},
            ],
        },
        {
            "section": "summary",
            "slide": {"title": "まとめ", "numbered": True, "bullets": [
                "失敗の理由：眠くないのに長く寝ようとした",
                "コツ：眠り始めの深い眠りを大事にする",
                "コツ：お風呂は寝る1〜2時間前に済ませる",
            ]},
            "lines": [
                {"speaker": "shikoku_metan", "expression": "happy", "text": "今日のポイント、いくつ言えるかしら？", "se": "パパッ",
                 "board": False},
                {"speaker": "zundamon", "expression": "happy", "text": "今夜はお風呂に入って、眠くなってから寝るのだ！"},
                {"speaker": "shikoku_metan", "expression": "normal", "text": "8時間寝る作戦はどうしたのよ。"},
            ],
        },
    ],
}


def sample_script_text() -> str:
    return json.dumps(SAMPLE_BOOK_SCRIPT, ensure_ascii=False, indent=2)
