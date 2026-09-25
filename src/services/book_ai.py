"""
Claude（Anthropic API）で、本のテキストから書籍解説動画の台本を自動生成する。

処理は2段階:
  1. analyze_book()   … 本の全文を読ませ、章ごとの要点・具体例・頻出テーマ・読者の悩み・意外な事実・
                          よくある誤解・全体のまとめを抽出する
  2. generate_script() … 1の分析結果から、ずんだもん（悩む側・ボケ役）と四国めたん（本を紹介・解説する側・
                          ツッコミ役）の掛け合い台本を、src.services.book_script が読める台本JSONの形式で作る。
                          通常の動画（横・数分）とショート動画（縦・約1分）でプロンプトを作り分ける

プロンプトには、YouTubeで視聴維持率・クリック率を上げる定石を盛り込んでいる:
  - 通常: 冒頭15秒のつかみ（意外な事実を疑問形で）→ 悩みへの共感 → 結論の先出しとポイント数の予告、
          各ポイントはPREP法（結論→理由→具体例/たとえ話）、ボケとツッコミの小さな笑い、次のポイントへの引き、
          まとめは「今日からできること」を1つに絞ってオチで締める
  - ショート: 最初の1〜2秒のフック（挨拶・前置き禁止）、ポイントは1つだけ、1セリフ25字以内の速いテンポ、
          最後のセリフが冒頭につながるループ構成、最後に一言だけ本編/フォローへの誘導
  - タイトル案3つ（悩み解決型・意外性型・ベネフィット型。スマホで切れない長さ・大事な言葉を前に）、
    説明欄の冒頭2〜3行、タイトル上に表示されるハッシュタグ3つも一緒に作る
間で分析結果を画面に出して確認・修正できるようにしている（本の内容の取り違えを台本の前に直せる）。

本の代わりに、論文・ネット記事をClaudeに調べさせて題材にすることもできる（research_topic()）。
Web検索（web_search）・ページ読み込み（web_fetch）で調査レポートを書かせてから、本の分析結果と同じ形式に整理し、
同じ generate_script() で台本にする（出典を示す・数字を作らない等のルールを追加する）。

- どちらも構造化出力（output_config.format の JSON Schema）を使うため、返ってくるJSONは必ず形式どおりになる。
  台本の話者・表情・効果音は、アプリに実際にある値だけを選択肢（enum）として渡す。
- 本の全文は1回の依頼で丸ごと渡す（Claudeは100万トークンまで読める）。本文にはプロンプトキャッシュを付け、
  5分以内に分析をやり直す場合は本文の読み込み料金が約1/10になる。
- 長い本・長い出力でもタイムアウトしないよう、ストリーミングで受け取る。
- Claude Opus 5 等の安全性の判定で依頼が断られた場合に備え、サーバー側の自動フォールバック
  （fallbacks="default"。断られた理由に応じて推奨の別モデルで自動的にやり直す）を有効にしている。

APIキーは .env（"# === Anthropic ===" の ANTHROPIC_API_KEY）または環境変数から読む。
使うモデルは ANTHROPIC_MODEL（既定: claude-opus-5）で変更できる。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

import anthropic

from src.utils.asset_loader import (
    get_available_expressions,
    get_character_display_name,
    get_expression_label,
    list_characters,
    list_illustrations,
    list_se,
    load_illustration_guide,
    load_se_guide,
)
from src.services.voicevox_client import DEFAULT_SPEECH_SPEED
from src.utils.env_config import load_env

DEFAULT_MODEL = "claude-opus-5"
# 100万トークンあたりの料金（USD。入力, 出力）。費用の目安表示に使う（キャッシュ読み込みは入力の0.1倍、書き込みは1.25倍）
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# サーバー側の自動フォールバック（fallbacks="default"）に対応しているモデル
FALLBACK_MODELS = {"claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

ANALYSIS_MAX_TOKENS = 32000
SCRIPT_MAX_TOKENS = 32000
SPOKEN_CHARS_PER_MINUTE = 330  # VOICEVOXの読み上げ速度の目安（約5.5文字/秒）
SHORT_TARGET_SECONDS = 55      # ショート動画の目標秒数（60秒を超えないよう少し余裕を持たせる）

ProgressCallback = Callable[[str], None]


class BookAIError(Exception):
    """台本の自動生成に失敗した（利用者向けのメッセージ付き）。"""


@dataclass
class AIResult:
    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    notes: list[str] = field(default_factory=list)
    searches: int = 0  # Web検索の回数（1回 WEB_SEARCH_PRICE_USD）

    @property
    def cost_usd(self) -> float:
        return estimate_cost(self.model, self.input_tokens, self.output_tokens,
                             self.cache_read_tokens, self.cache_write_tokens) + self.searches * WEB_SEARCH_PRICE_USD


def estimate_cost(model: str, input_tokens: int, output_tokens: int = 0,
                  cache_read: int = 0, cache_write: int = 0) -> float:
    price_in, price_out = MODEL_PRICES.get(model, MODEL_PRICES[DEFAULT_MODEL])
    return (
        input_tokens * price_in + output_tokens * price_out
        + cache_read * price_in * 0.1 + cache_write * price_in * 1.25
    ) / 1_000_000


def current_model() -> str:
    load_env()
    return os.environ.get("ANTHROPIC_MODEL", "").strip() or DEFAULT_MODEL


def api_key_configured() -> bool:
    load_env()
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip())


def _client() -> anthropic.Anthropic:
    load_env()
    return anthropic.Anthropic()


# ---------------------------------------------------------------------------
# JSON Schema（構造化出力）
# ---------------------------------------------------------------------------

def _obj(properties: dict, required: Optional[list[str]] = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_STR_LIST = {"type": "array", "items": {"type": "string"}}

ANALYSIS_SCHEMA = _obj({
    "book_title": _STR,
    "author": _STR,
    "one_line_summary": _STR,
    "target_reader": _STR,
    "worries": _STR_LIST,
    "surprising_facts": _STR_LIST,
    "misconceptions": _STR_LIST,
    "common_mistakes": _STR_LIST,
    "chapters": {"type": "array", "items": _obj({
        "title": _STR,
        "key_points": _STR_LIST,
        "examples": _STR_LIST,
    })},
    "themes": _STR_LIST,
    "key_takeaways": _STR_LIST,
})


def _characters() -> list[str]:
    return list_characters() or ["zundamon", "shikoku_metan"]


def _script_schema() -> dict:
    characters = _characters()
    expressions = sorted({e for c in characters for e in get_available_expressions(c)})
    se_names = [""] + [p.stem for p in list_se()]
    line = _obj({
        "speaker": {"type": "string", "enum": characters},
        "expression": {"type": "string", "enum": expressions},
        "text": _STR,
        "se": {"type": "string", "enum": se_names},
        "hide": {"type": "array", "items": {"type": "string", "enum": characters}},
        "show_book": {"type": "boolean"},
        "board": {"type": "boolean"},
        "bullet": {"type": "integer"},
        "note": _obj({"text": _STR, "focus": _STR, "meaning": _STR}),
        "mood": {"type": "string", "enum": ["", "gloomy", "shock", "dark", "sepia", "bright"]},
        "card": _STR,
        "pause": {"type": "number"},
        "pause_text": _STR,
        "image": {"type": "string", "enum": [""] + [p.stem for p in list_illustrations()]},
        "caption": _STR,
        "image_request": _STR,
        "image_name": _STR,
    })
    block = _obj({
        "section": {"type": "string", "enum": ["intro", "explain", "summary"]},
        "phase": {"type": "string", "enum": ["", "hype", "fail", "rescue", "why", "how"]},
        "slide": _obj({"title": _STR, "bullets": _STR_LIST, "numbered": {"type": "boolean"}}),
        "lines": {"type": "array", "items": line},
    })
    return _obj({
        "book_title": _STR,
        "author": _STR,
        "title_candidates": _STR_LIST,
        "video_title": _STR,
        "description_lead": _STR,
        "hashtags": _STR_LIST,
        "tags": _STR_LIST,
        "readings": {"type": "array", "items": _obj({"word": _STR, "reading": _STR})},
        "thumbnail": _obj({"text": _STR, "sub": _STR, "layout": {"type": "string", "enum": ["reaction", "duo", "big_text"]}, "zundamon": {"type": "string", "enum": expressions}, "metan": {"type": "string", "enum": expressions}}),
        "blocks": {"type": "array", "items": block},
    })


# ---------------------------------------------------------------------------
# プロンプト
# ---------------------------------------------------------------------------

ANALYSIS_SYSTEM = """あなたは書籍の内容を正確に読み解く編集者です。渡された本を読み、YouTubeの書籍解説動画（通常の解説動画と1分のショート動画）の材料になる情報を整理します。

- 本に書かれていることだけを使い、書かれていない内容を補ったり推測で断定したりしないでください。
- 要点・具体例は、原文を長く引き写さず、自分の言葉で短く言い換えてください（1項目40字程度まで）。
- worries には「この本が解決してくれる、読者が抱えていそうな悩み」を、悩んでいる本人の言葉のように書いてください（例: 夜なかなか寝つけない）。
- surprising_facts には、読者が「えっ、そうなの？」と驚く意外な事実・データ・常識とのギャップを3〜6個書いてください。動画の冒頭で視聴者を引きつける「つかみ」に使います。数字が本に書かれている場合は正確に書いてください。
- common_mistakes には、この本のテーマで多くの人がやりがちな失敗・間違ったやり方と、その結果を「〇〇して、〇〇になってしまう」の形で3〜5個書いてください（動画の導入で、ずんだもんが失敗するエピソードに使います）。
- misconceptions には、多くの人が信じているがこの本では違うとされている思い込みを、「〇〇だと思われがちだが、実は〇〇」の形で2〜4個書いてください（無ければ空の配列）。
- chapters は本の章立てに沿って、各章の要点（2〜5個）と、具体例・データ・エピソード（0〜3個）をまとめてください。章の区切りが無い本は、内容のまとまりごとに分けてください。
- themes には本全体で繰り返し出てくる考え方・キーワードを、key_takeaways には読者が明日から実践できる具体的な行動を3〜5個書いてください。
- book_title・author は本文から分かる場合のみ書き、分からなければ利用者のヒントを使い、それも無ければ空文字にしてください。"""


def _character_guide() -> str:
    lines = []
    for c in _characters():
        labels = "、".join(f"{e}（{get_expression_label(c, e)}）" for e in get_available_expressions(c))
        lines.append(f"- {c}（{get_character_display_name(c)}）の表情: {labels}")
    lines.append(EXPRESSION_USAGE_HINTS)
    return "\n".join(lines)


# 表情の使い分けの目安（一覧に無い表情の指示は無視される）。同じ表情を続けず、場面に合う表情を選ばせる
EXPRESSION_USAGE_HINTS = """- 表情の使い分けの目安（上の一覧にある表情だけを使う）:
  黒板の内容を説明する・「つまり〜」とまとめるセリフは explain（めたんが相手や視聴者に強く言うときは point）。
  内緒話・「ここだけの話」は whisper。本を紹介するめたんは book。問いかける・考えるセリフは think。
  ずんだもんが失敗して落ち込む場面は gloomy か sad（背景の mood も gloomy に）、ショックの瞬間は shock、慌てるときは panic、
  知ったかぶり・調子に乗るときは smug か excited。めたんがほめられて照れるときは blush、あきれてツッコむときは troubled か angry。
  同じ表情を3セリフ以上続けない。"""


MAX_ILLUSTRATIONS_PER_VIDEO = 20  # 1本の動画で使うイラストの種類の上限（いらすとやの商用利用は1作品20点まで）


def _illustration_guide() -> str:
    """使えるイラストの一覧（config/illustrations.json に説明があれば添える）。"""
    names = [p.stem for p in list_illustrations()]
    if not names:
        return ""
    guide = load_illustration_guide()
    return "\n".join(f"  - {name}: {guide[name]}" if guide.get(name) else f"  - {name}" for name in names)


def _illustration_rules() -> str:
    listing = _illustration_guide() or "  （まだ1枚もありません。すべて image_request で依頼してください）"
    return f"""## 画面に出すもの（board・image。基本は2人の会話）
- 各ブロックの最初の2〜3セリフ（つかみ・ボケ・問いかけ）は、2人の会話だけ（board は false、image は空文字）にする。
- 黒板（board を true）は、そのポイントの結論（見出し）を言うセリフから出し、そのブロックの最後まで出したままにする（アプリも、一度出した黒板はブロックの最後まで自動で出したままにする）。まとめ（summary）は全セリフ true。
- 黒板を出している間は、黒板に書いてある内容を会話でじっくり話す。箇条書きの1行ごとに2〜3セリフかける（めたんが黒板の文を言う → その理由・具体例・たとえ → ずんだもんの反応や質問・言い換え）。1行を1セリフで流して次へ進まない。黒板の文章を読み終える前に次の話題へ移らない。
  黒板の箇条書きは、黒板を出しているセリフの進行に合わせて1行ずつ書き足されるので、bullets は話す順番に並べる。
- イラスト（image か image_request）を出すのは、具体例・たとえ話・やってしまいがちな行動・おすすめの行動など、絵があるとイメージが付きやすいセリフだけ。イラストを出すセリフは board を false にする。
- 導入（intro）は会話だけ（board は false）でよい。
- 黒板は1セリフごとに出したり消したりしない（画面がチカチカする）。イラストは1〜2セリフだけ出す（そのあとは黒板に戻る）。
- 黒板の箇条書きが手順・順番・ランキング・「〇つの〜」のように順序や数に意味があるときは、slide の numbered を true にする（1. 2. 3. の番号付きで表示される）。それ以外は false（「・」で表示）。
- 手元にあるイラストは次のとおり（ファイル名から推測して選ぶ）:
{listing}
- 話題に合うイラストが上の一覧にある場合: image にその名前を書き、image_request と image_name は空文字にする。
- 合うイラストが一覧に無い場合: image は空文字にして、image_request に「欲しいイラスト」を具体的に書く（誰が・何を・どんな様子か＋いらすとや等で探すときの検索語。例:「湯船につかってリラックスする女性の絵（検索語: お風呂 入浴）」）。image_name には、そのイラストを保存するときのファイル名を日本語で短く書く（4〜12字。例:「お風呂でリラックス」）。同じイラストを別のシーンでも使うときは、同じ image_name にする。
- caption は空文字にする（イラストは画像だけを大きく表示し、説明文は出さない）。
- 同じイラストを連続で使わない。1本の動画で使うイラストの種類（手元にあるもの＋依頼するもの）は{MAX_ILLUSTRATIONS_PER_VIDEO}種類までにする。
- board と image の指定は、すべてのセリフに必ず書く（出さないときは false・空文字）。"""


def _se_guide() -> str:
    """使える効果音と、その用途の一覧（config/se_guide.json。用途が未登録の効果音は名前だけ）。"""
    names = [p.stem for p in list_se()]
    if not names:
        return "（効果音なし。se は常に空文字）"
    guide = load_se_guide()
    return "\n" + "\n".join(
        f"  - {name}: {guide[name]['use']}" if guide.get(name, {}).get("use") else f"  - {name}"
        for name in names
    )


_CHARACTERS_TEXT = """## キャラクター設定と口調のルール（厳密に守る。すべてのセリフで例外なし）
### ずんだもん（zundamon）
- 一人称: 「僕」（ぼく）。「ボク」「俺」「わたし」は使わない。
- 二人称: 「お前」「あんた」「めたん」
- 語尾のルール（超重要）: すべての文末を「〜のだ」「〜なのだ」で終わらせる。疑問文も「〜のだ？」「〜なのだ？」。「〜です」「〜ます」などの敬語は絶対に使わない。1つのセリフに文が2つ以上あるときは、どの文も「のだ」「なのだ」で終える（「えっ」「うわぁ」のような短い感嘆だけは例外）。
- 性格・トーン: 元気で少し生意気。感情豊かで調子に乗りやすく、隙あらば知ったかぶりをするが、たいていめたんに論破される（ボケ役）。悩みを抱えていて、視聴者の代わりに驚いたり、早とちりしたりする。
- 口調の例:「僕、そんなの最初から知ってたのだ！」「めたん、それ本当なのだ？」「お前に言われなくても分かってるのだ…」

### 四国めたん（shikoku_metan）
- 一人称: 「わたくし」
- 二人称: 「あなた」「あんた」「ずんだもん」
- 語尾・口調のルール（超重要）: 基本はお嬢様言葉（「〜わ」「〜のよ」「〜かしら」）。ずんだもんに対してはタメ口が強めの辛辣な口調になる。「〜ですわ」「〜ますわ」「〜ですの」「〜ございます」といった過度な丁寧語は使わず、「〜でしょ」「〜じゃないの」「〜しなさいよ」「〜なのよ」といった砕けた、少し上から目線のタメ口を多く使う。
- 性格・トーン: 没落した元お嬢様。面倒見は良いが、ずんだもんのボケや暴走には呆れつつ、容赦のないツッコミを入れる（ツッコミ役）。本を紹介して、分かりやすく解説する役。
- 口調の例:「知ったかぶりはやめなさいよ」「あんた、さっきと言ってることが違うじゃないの」「しょうがないわね、わたくしが教えてあげるわ」「この本によると、眠り始めがいちばん大事なのよ」"""


def _common_rules() -> str:
    return f"""## 表情・効果音・表示
- expression は、そのキャラクターの表情の一覧から、セリフの感情に合うものを選ぶ（一覧に無い表情は使わない）。同じ表情ばかり続けず、驚き・困り・喜び・怒りなど感情の起伏を表情で見せる:
{_character_guide()}
- se（そのセリフの頭で鳴る効果音）は、ここぞという場面（つかみ・本の紹介・新しいポイント・ボケとツッコミ・オチ）に使い、同じ効果音ばかり続けない。使えるのは次の名前のみで、用途に合うものを選ぶ（使わないときは空文字）: {_se_guide()}
- hide は通常は空の配列（指定がある場合のみ使う）。
- mood（背景の雰囲気。キャラクターと黒板はそのまま）: ふだんは空文字。感情がいちばん高まる場面だけに使う（落ち込みが底になる場面は "gloomy"、ガーンとショックを受ける瞬間は "shock"、夜・不安がピークの場面は "dark"、回想は "sepia"、大成功・大ひらめきの瞬間は "bright"）。
  使うときは、その場面の2〜5セリフに続けて同じ値を付ける（1セリフだけ、1セリフおき、途中で別の雰囲気に変える、はしない。画面がチカチカする）。動画の最初のセリフには付けない。1本の動画で1〜3場面まで（アプリも、これに合わない付け方は自動で直す）。
- card（場面転換テロップ）: 「3日後…」「その夜」「1週間後…」「一方そのころ」のように、時間や場面が飛ぶところで、全画面に短い文字（12字以内）を出す。card を使うときは、そのセリフの前に独立した行を1つ作り、その行の card に文字を書き、text は空文字・speaker は直前と同じにする（その行はセリフとしては読まれない）。時間の経過が話の面白さにつながるところ（導入の「挑戦 → 3日後… → 失敗」など）だけに使い、1本で1〜3回まで。それ以外の行は card を空文字にする。
- note（重要な表現の解説カード）: 特に大事な言い回し・用語を解説するところで、1本の動画で2〜4回使う。画面に文が大きく出て、focus の部分に赤い下線が引かれ、矢印の先に meaning が表示される。
  text に文（25字・8語程度まで）、focus に赤線を引く部分（text の中にそのまま含まれる語句）、meaning にその部分の意味・使い方（25字以内。例:「〜をもらえる？ お店で注文するときの定番」）を書く。
  その部分を説明する2〜3セリフに、同じ note を続けて付ける（その間は黒板の代わりに解説カードが出る）。使わない行は text・focus・meaning をすべて空文字にする。
- bullet（黒板のどの行の話か）: 黒板の箇条書きの行を初めて話すセリフに、その行の番号（1から）を書く（その番号の行が、そのセリフで黒板に書き足される）。それ以外のセリフは 0。行の番号は、話す順番どおりに1, 2, 3…と増えるようにする。まとめ（summary）は全部の行を最初から出すので、すべて 0 でよい。
- pause・pause_text は、視聴者に考えさせる・答えさせる無音の間（秒数と、その間に画面上部に大きく出す短い指示）。使わない行は pause を 0、pause_text を空文字にする。
- show_book は、めたんが本（書名）を紹介するセリフだけ true にする（そのシーンで本の表紙画像が画面に出る）。それ以外はすべて false。

{_illustration_rules()}

## 読み方（readings）
- 音声合成（VOICEVOX）が読み間違えそうな言葉の読み方を、readings に {{"word": 表記, "reading": ひらがな or カタカナの読み}} で書く。
  対象: 書名・著者名・人名・地名、専門用語、難読語、読み方が複数ある語（例: 他人事→ひとごと、一段落→いちだんらく）、英字の略語・英単語（例: ToDo→トゥードゥー）。
  台本のセリフに実際に出てくる言葉だけにし、普通に読める言葉は入れない（0〜15個程度）。

## 内容のルール（必ず守る）
- 分析結果に書かれている内容だけを使い、本に無い情報・数字・エピソードをでっち上げない。本の主張として話すときは「この本によると」のように出典を示す。
- 本の文章を長く引用しない（引用する場合は20字以内を1〜2回まで）。自分の言葉で言い換える。
- 医療・お金などの話題は断定しすぎず、「〜とされているのよ」のように本の主張として紹介する。
- 小学生にも分かる話し言葉で。専門用語には一言説明を添える。「〜である」調は使わない。
- セリフは普通の漢字かな交じりで書く（ひらがなばかりの文は音声合成が「は」を「ハ」と読むなど誤読しやすい）。

## サムネイル
- thumbnail（サムネイルの文言と見せ方）:
  text は2〜3行（改行は \\n）、1行8字以内・全体で10〜18字。タイトルをそのまま縮めるのではなく、一目で「えっ？」「自分のことだ」と思う言葉（悩み・意外な結論・数字）にする。一番大事な1語だけを **語** で囲む（赤く目立つ）。
  sub は左上の帯の短いラベル（6〜10字。例: 本要約、研究で解説、毎日英会話 Day3）。
  layout は、感情が強い内容なら "reaction"（ずんだもんのアップ）、本の表紙やイラストを見せたい内容なら "duo"（2人＋画像）、結論が強い一言なら "big_text"（大きな文字）。
  zundamon・metan は、内容の感情が一目で伝わる表情（驚き・ショック・ドヤ顔・指さしなど）を一覧から選ぶ。"""


def _normal_system(target_minutes: float, why_points: int, speech_speed: float = DEFAULT_SPEECH_SPEED,
                   how_points: int = 3) -> str:
    target_chars = int(target_minutes * SPOKEN_CHARS_PER_MINUTE * speech_speed)
    return f"""あなたは登録者数の多い書籍解説YouTubeチャンネルの構成作家です。ずんだもんと四国めたんの掛け合いで、本の内容を楽しく分かりやすく紹介する、約{target_minutes:g}分（エンディング除く）の横長動画の台本を書きます。
目標は、視聴者が「最後まで見てしまう」「明日から試したくなる」テンポの良い動画にすることです。

{_CHARACTERS_TEXT}

## 構成と視聴維持の設計（blocks の並び。phase も必ず書く）
導入は「ずんだもんが張り切って何かを始める → 失敗する → めたんが登場して、なぜ失敗したかと成功させる方法を解説すると宣言する」という流れにする。導入は黒板を出さないので、intro の3ブロックの slide は title を空文字・bullets を空の配列にする。

1. intro・phase "hype"（導入①決意・ワクワク、1〜3セリフ）: ずんだもんが本のテーマに関係することを、調子に乗って始めると宣言する。
   例:「今はやりの〇〇で、お金を稼ぐのだ！」「最近〇〇だし、今日から〇〇するって決めたのだ！」
   本の内容から、視聴者も「やりがち」なことを選ぶ。挨拶・自己紹介・書名から始めない。このブロックの hide には "shikoku_metan" を入れる（ずんだもんだけ）。
2. intro・phase "fail"（導入②失敗、2〜4セリフ）: ずんだもんが、ありがちな間違ったやり方（分析結果の common_mistakes / misconceptions）で挑戦して失敗し、落ち込む・嘆く。少し笑える失敗にする。このブロックの hide にも "shikoku_metan" を入れる。
3. intro・phase "rescue"（導入③めたん登場、3〜6セリフ）: めたんが登場して「そんなんじゃだめよ」とバッサリ言う → ずんだもん「どうしてなのだ？」→ めたんが「これから『なぜ失敗したのか』と『成功させるにはどうすればいいのか』に分けて、分かりやすく話すわ」と宣言し、「今日は『書名』（著者）で教えてあげるわ」と本を紹介する（このセリフの show_book を true にする）。ここで話す内容の予告をして、最後まで見る理由を作る。hide は空の配列（ここからめたんが表示される）。
4. explain・phase "why"（解説前半：なぜ失敗したのか、{why_points}ブロック）: ずんだもんの失敗の原因を、本の内容をもとに1ブロック1つずつ解説する。最初のブロックの最初のセリフは、めたんの「まずは、なぜ失敗したのか見ていくわよ」のような区切りの一言にする。
5. explain・phase "how"（解説後半：成功させるにはどうすればいいか、{how_points}ブロック）: 本が勧める正しいやり方を1ブロック1つずつ解説する。最初のブロックの最初のセリフは、めたんの「じゃあ、どうすればうまくいくのか教えてあげるわ」のような区切りの一言にする。
   解説ブロック（why・how とも）の共通ルール:
   - 各ブロックは「結論 → 理由 → 具体例（本の中のデータ・エピソード）やたとえ話 → ずんだもんの言い換え・確認」の順（PREP法）。抽象論だけで終わらせず、数字や具体例を必ず1つ入れる。ずんだもんの導入の失敗と結び付けて説明する。
   - たとえ話は身近なもの（スマホ、ゲーム、料理、学校、仕事など）で。
   - ずんだもんは知ったかぶり・早とちり・極端な解釈・調子に乗った発言で小さな笑いを作り、めたんが容赦なくツッコんで正しい理解に戻す（1ブロックに1回以上）。
   - 最後のブロック以外は、ブロックの最後に次への引き（「でも、これだけじゃまだ足りないのよ」「次がいちばん大事よ」など）を入れて中だるみを防ぐ。
   - 同じ話者が3セリフ以上続かない。めたんの長い独演会にせず、ずんだもんのリアクション・質問を1〜2セリフごとに挟む。
6. summary（まとめ・1ブロック、phase は空文字）: 黒板で「失敗の理由」と「成功のコツ」を振り返る → 「今日からできること」を1つだけ提案 → ずんだもんが導入で失敗したことに今度は正しいやり方で再挑戦すると決意し、めたんがツッコむオチで締める。
エンディング（「ご視聴ありがとうございました」やチャンネル登録のお願い）はアプリが自動で付けるので書かないでください。

## 身になって、また見たくなる工夫（必ず入れる）
面白いだけで終わらず「分かった・覚えた・やってみたい」と感じてもらい、次の動画も見たくなるようにする。学習の研究で効果が大きいとされる「思い出す練習（途中の小テスト）」「理由を問う質問」「絵と言葉の組み合わせ」と、動画の視聴維持で使われる「開いたループ（予告して後で回収）」を取り入れる。
1. 途中クイズ（2〜3回）: 解説の途中で、めたんが視聴者に問題を出す（例:「ここで問題よ。〇〇と〇〇、どっちが正しいと思う？」）。そのセリフの pause を 3〜4、pause_text を「考えてみて！」などにする → ずんだもんが自信満々に間違える → めたんが答えと理由を言う。問題は、直前に説明したことを思い出せば答えられるものにする（ひっかけ・雑学クイズにしない）。
2. 理由を掘る: 大事なポイントでは、ずんだもんに「なんでそうなるのだ？」と聞かせ、めたんが仕組み（なぜ効くのか）を身近なたとえで説明する。結論だけで終わらせない。
3. 予告して回収する: 導入の最後か解説の最初に「最後に、いちばん効く方法を教えるわ」のように、後で出す内容を1つ予告し、動画の後半で必ず回収する。
4. まとめは思い出しテスト: summary の最初に、めたんが「今日のポイント、いくつ言えるかしら？」と問いかける（pause を 4、pause_text を「思い出してみて！」）→ そのあと黒板で答え合わせ。
5. 行動は具体的に: 「今日からできること」は、いつ・どこで・何をするかが分かる1文にする（例:「今夜、布団に入る1時間前にスマホを充電器に置く」）。
6. シリーズ感: ずんだもんの「おなじみのボケ」（調子に乗る・すぐ極端に走る）と、めたんの決めゼリフ風のツッコミを毎回同じ型で入れ、最後はずんだもんの「今日のずんだもんメモ」（今日学んだことを自分の言葉で1文）で締める。

## テンポ（速めに）
- 1セリフは10〜30字（字幕1行＝30字に収める）、平均20字程度。どうしても長くなる場合も60字（字幕2行）以内にし、長い説明は必ず複数のセリフに分ける。
- 動画全体のセリフの合計がおよそ{target_chars}字（約{target_minutes:g}分）になるようにする。導入（intro の3ブロック合計）は全体の15%程度に収め、長くしすぎない。
- 前置き・繰り返し・つなぎの言葉（「さて」「それでは」「ということで」）は削る。短いリアクション（「えっ！？」「マジなのだ！？」）で会話を弾ませる。
- 驚き・納得・笑いを交互に入れ、感情の起伏を作る。

## 黒板スライド（explain と summary の slide）
- title は20字以内。why のブロックは「失敗の理由1：〜」、how のブロックは「成功のコツ1：〜」のように、番号の後に全角の「：」を付けてから結論を書く（番号はそれぞれ1から数える）。summary は「まとめ」。
- bullets は3〜5個。一言のキーワードではなく、黒板を読んだだけで要点が伝わる短い文（各15〜28字）にする。1つの文は黒板の1行に収めて表示するので、28字を超えない。
  1つ目は結論、続けて理由・具体例（数字やデータがあれば入れる）・やり方を書く（例:「寝る1〜2時間前にお風呂に入る」「体の内側の温度が下がると自然に眠くなる」）。
  最も大事な語句を1〜2か所 **語句** の形で強調してよい。
- summary の bullets は、「失敗の理由」と「成功のコツ」の結論を1文ずつ（各15〜28字）。
- 黒板の箇条書きはセリフの進行に合わせて上から順に書き足されるので、セリフで話す順番に並べる。

{_common_rules()}

## 投稿用のタイトル・説明文
- title_candidates: タイトル案を3つ。それぞれ違う型で書く:
  ① 失敗あるある型（例:「【本要約】〇〇で失敗する人の共通点｜『書名』」）
  ② 意外性型（例:「その〇〇、逆効果かも？【本要約】」）
  ③ ベネフィット型（例:「〇〇を成功させる3つのコツ【本要約】」）
  各32字以内。スマホで途中が切れても伝わるよう、大事な言葉（悩み・書名）を前半に置き、数字や【】を使う。内容と違う誇張や、本に無い数字を使った釣りタイトルにしない。
- video_title: title_candidates の中で、最もクリックされそうな1つ。
- description_lead: 説明欄の冒頭2〜3行（改行区切り、各40字以内）。1行目で視聴者の「やりがちな失敗」に呼びかけ、2行目でこの動画を見ると何が分かるか（失敗の理由と成功のコツ）を書く。
- hashtags: 3つ（# は付けない）。1つ目は「本要約」、2つ目は書名（長ければ短く）、3つ目は本のテーマ（例: 睡眠）。
- tags: 8〜12個（書名・著者名・テーマ・悩みのキーワードなど）。"""


def _short_system(speech_speed: float = DEFAULT_SPEECH_SPEED) -> str:
    target_chars = int(SHORT_TARGET_SECONDS / 60 * SPOKEN_CHARS_PER_MINUTE * speech_speed)
    return f"""あなたはYouTubeショートで何度も大きく再生されている書籍解説クリエイターです。ずんだもんと四国めたんの掛け合いで、本の一番おいしいところを紹介する、約{SHORT_TARGET_SECONDS}秒の縦型ショート動画の台本を書きます。
ショートは、最初の1〜2秒で見るかスワイプするかが決まり、最後まで見られた割合と繰り返し再生（ループ）で広まります。

{_CHARACTERS_TEXT}

## 構成（blocks は intro → explain → summary の3つ）
1. intro: ホワイトボードは出さないので、slide は title を空文字・bullets を空の配列にする。
   - 1セリフ目（0〜2秒）が命。分析結果の surprising_facts / misconceptions から、本の中で一番意外で、続きが気になる一言を、ずんだもんに言わせる（疑問形・断言・数字）。挨拶・書名の紹介・前置きは禁止。15字前後が理想。この1セリフ目の hide には "shikoku_metan" を入れる。
     例:「寝る前の"あの習慣"が眠りを壊してるのだ！？」
   - 続けてめたんが登場し、「『書名』によると…」と本を一言で紹介して答えに入る（このセリフの show_book を true にする）。
2. explain: 扱うのは本の中の「一番意外で、今日すぐ使える」ポイント1つだけ。理由 → 具体例（本の中のデータ・エピソード）を短く。途中で1回、ずんだもんの知ったかぶりか早とちりにめたんがツッコむ小さなオチを入れる。
3. summary: 今日からやることを1つだけ言い切る → ずんだもんのオチ → 最後の1セリフで、本編への誘導だけを短く言う（例: めたん「詳しくは本編で解説してるわよ」、ずんだもん「続きは本編で見るのだ！」）。フォロー・チャンネル登録・他の動画を見てほしい等のお願いは書かない。最後のセリフの内容が1セリフ目のつかみに自然につながるようにして、ループ再生を誘う。長いお礼・挨拶も書かない。

## 身になる工夫（ショート）
- 答えを言う前に1回だけ、ずんだもんか視聴者に「どっちだと思う？」と問いかけ、そのセリフの pause を 1.5、pause_text を「どっち？」などにする（考える間で、最後まで見る理由を作る）。
- 最後の「今日からやること」は、いつ・何をするかが分かる1文にする。

## テンポ（とにかく速く）
- セリフ全体の合計はおよそ{target_chars}字（{SHORT_TARGET_SECONDS}秒以内に収める。超えない）。セリフの数は12〜20程度。
- 1セリフは17字以内が基本（縦画面の字幕は1行17字）。どうしても長くなる場合も34字（字幕2行）以内にする。1〜2セリフごとに話者を交代し、1〜2セリフごとに感情（表情）を変える。
- 説明を削ってでもテンポを優先する。「えっ」「マジなのだ！？」のような短いリアクションも活用する。

## ホワイトボード（explain と summary の slide。縦画面なのでホワイトボードに表示される）
- title は14字以内。explain は結論、summary は「今日からこれ！」など。
- bullets は2〜4個。一言のキーワードではなく、読んだだけで要点が伝わる短い文（各8〜15字）にする（例:「寝る90分前にお風呂」）。1つの文はホワイトボードの1行に収めて表示するので、15字を超えない。最も大事な語句を1つだけ **語句** の形で強調してよい。

{_common_rules()}
- ショートでは効果音を、つかみ・驚き・オチの2〜4回に絞る。

## 投稿用のタイトル・説明文
- title_candidates: タイトル案を3つ。各28字以内（末尾の「 #Shorts」を含む）。1セリフ目のつかみを活かした疑問形・断言形にし、末尾に「 #Shorts」を付ける。内容と違う誇張や、本に無い数字を使った釣りタイトルにしない。
- video_title: title_candidates の中で、最もスワイプを止めそうな1つ。
- description_lead: 説明欄の冒頭1〜2行（改行区切り、各40字以内）。この動画で分かることを一言で。
- hashtags: 3つ（# は付けない。Shorts はアプリが自動で付けるので不要）。「本要約」、書名またはテーマ、「ずんだもん」。
- tags: 5〜8個。"""


def _script_system(style: str, target_minutes: float, num_points: int,
                   speech_speed: float = DEFAULT_SPEECH_SPEED, how_points: int = 3) -> str:
    """style="normal" では num_points が「失敗の理由」、how_points が「成功のコツ」の数。"""
    if style == "short":
        return _short_system(speech_speed)
    return _normal_system(target_minutes, num_points, speech_speed, how_points)


def _hints_text(title_hint: str, author_hint: str, worry_hint: str) -> str:
    hints = []
    if title_hint.strip():
        hints.append(f"- 本のタイトル: {title_hint.strip()}")
    if author_hint.strip():
        hints.append(f"- 著者: {author_hint.strip()}")
    if worry_hint.strip():
        hints.append(f"- 動画で扱いたい悩み（ずんだもんの悩み）: {worry_hint.strip()}")
    return ("利用者からのヒント:\n" + "\n".join(hints)) if hints else ""


def manual_script_prompt(style: str = "normal", target_minutes: float = 5, num_points: int = 3, title_hint: str = "",
                         author_hint: str = "", worry_hint: str = "",
                         speech_speed: float = DEFAULT_SPEECH_SPEED, how_points: int = 3,
                         source_kind: str = "book", focus: str = "", urls: Optional[list[str]] = None) -> str:
    """APIを使わずに、Claudeのチャット画面（claude.ai）で台本を作ってもらうためのプロンプト。

    本のファイルをチャットに添付し、このプロンプトを貼り付ける。返ってきたJSONを「台本JSONを読み込む」に貼る。
    """
    from src.services.book_script import sample_script_text

    if source_kind == "research":
        url_list = [u.strip() for u in (urls or []) if u.strip()]
        first_step = "\n".join(filter(None, [
            _MANUAL_RESEARCH_STEP,
            f"テーマ: {title_hint.strip()}" if title_hint.strip() else "",
            f"特に知りたいこと・観点: {focus.strip()}" if focus.strip() else "",
            ("必ず読んでほしいURL: " + " / ".join(url_list)) if url_list else "",
        ]))
        title_hint, author_hint = "", ""
    else:
        first_step = "添付した本を読み、上のルールで台本を作ってください。"
    return "\n\n".join(filter(None, [
        _script_system(style, target_minutes, num_points, speech_speed, how_points) + research_script_rules(source_kind),
        first_step + "出力は次の形式のJSONだけにしてください"
        "（blocks には section・phase・slide・lines を、各セリフには speaker・expression・text・se・hide・show_book・board・image・caption・image_request・image_name を必ず書く。"
        "トップレベルには book_title・author・title_candidates・video_title・description_lead・hashtags・tags・readings も書く）。"
        "形式の例（blocks の中身の書き方の参考。セリフの内容や分量は上のルールに従う）:",
        "```json\n" + sample_script_text() + "\n```",
        f'最後に、トップレベルに "style": "{style}" を必ず入れてください。',
        _hints_text(title_hint, author_hint, worry_hint),
    ]))


# ---------------------------------------------------------------------------
# API呼び出し
# ---------------------------------------------------------------------------

def _request_kwargs(model: str, system: str, content: list, schema: dict, max_tokens: int) -> dict:
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    if "haiku" not in model:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"]["effort"] = "high"
    if model in FALLBACK_MODELS:
        kwargs["fallbacks"] = "default"
        kwargs["betas"] = [FALLBACK_BETA]
    return kwargs


def _call(system: str, content: list, schema: dict, max_tokens: int, progress: ProgressCallback) -> AIResult:
    if not api_key_configured():
        raise BookAIError(
            "Claude APIのキーが設定されていません。プロジェクト直下の .env ファイルの "
            "「# === Anthropic ===」の下にある ANTHROPIC_API_KEY= にキーを書いて、アプリを再起動してください。"
        )
    model = current_model()
    client = _client()
    try:
        with client.beta.messages.stream(**_request_kwargs(model, system, content, schema, max_tokens)) as stream:
            progress("Claudeが考えています…（本が長いと数分かかることがあります）")
            received = 0
            for text in stream.text_stream:
                received += len(text)
                if received // 400 != (received - len(text)) // 400:
                    progress(f"Claudeが書いています…（{received:,}文字）")
            message = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise BookAIError("APIキーが正しくありません。.env の ANTHROPIC_API_KEY を確認してください。") from e
    except anthropic.PermissionDeniedError as e:
        raise BookAIError(f"このAPIキーでは利用できません（{e.message}）") from e
    except anthropic.NotFoundError as e:
        raise BookAIError(f"モデル「{model}」が見つかりません。.env の ANTHROPIC_MODEL を確認してください。") from e
    except anthropic.RateLimitError as e:
        raise BookAIError("APIの利用上限に達しました。少し時間をおいてから再度お試しください。") from e
    except anthropic.BadRequestError as e:
        raise BookAIError(f"リクエストが受け付けられませんでした（{e.message}）") from e
    except anthropic.APIStatusError as e:
        raise BookAIError(f"Claude APIでエラーが発生しました（{e.status_code}: {e.message}）。時間をおいて再度お試しください。") from e
    except anthropic.APIConnectionError as e:
        raise BookAIError("Claude APIに接続できませんでした。インターネット接続を確認してください。") from e

    if message.stop_reason == "refusal":
        raise BookAIError("Claudeがこの依頼を処理できませんでした（安全性の判定で断られました）。本の内容や指示を見直してください。")
    if message.stop_reason == "max_tokens":
        raise BookAIError("出力が長すぎて途中で切れました。動画の長さやポイントの数を減らして再度お試しください。")
    text = "".join(block.text for block in message.content if block.type == "text")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise BookAIError(f"Claudeの出力をJSONとして読み取れませんでした（{e}）") from e

    usage = message.usage
    notes = []
    if getattr(message, "model", model) != model:
        notes.append(f"最初のモデルで断られたため、{message.model} が代わりに処理しました。")
    return AIResult(
        data=data,
        model=model,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        notes=notes,
    )


def _book_content(book_text: str, instruction: str) -> list:
    return [
        # 本文は変わらないのでキャッシュする（5分以内に分析をやり直すと、本文の読み込み料金が約1/10）
        {"type": "text", "text": f"<book>\n{book_text}\n</book>", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": instruction},
    ]


def count_book_tokens(book_text: str) -> int:
    """本の分析にかかる入力トークン数を数える（トークン数の計測自体は無料）。"""
    if not api_key_configured():
        raise BookAIError("Claude APIのキーが設定されていません（.env の ANTHROPIC_API_KEY）。")
    try:
        result = _client().messages.count_tokens(
            model=current_model(),
            system=ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": _book_content(book_text, "この本を分析してください。")}],
        )
    except anthropic.APIError as e:
        raise BookAIError(f"トークン数を数えられませんでした（{e}）") from e
    return result.input_tokens


def analyze_book(book_text: str, title_hint: str = "", author_hint: str = "", worry_hint: str = "",
                 progress: Optional[ProgressCallback] = None) -> AIResult:
    """① 本の全文から、章ごとの要点・具体例・テーマ・読者の悩み・まとめを抽出する。"""
    instruction = "\n\n".join(filter(None, [
        "上の <book> の本を読み、書籍解説動画の材料として内容を整理してください。",
        _hints_text(title_hint, author_hint, worry_hint),
    ]))
    return _call(ANALYSIS_SYSTEM, _book_content(book_text, instruction), ANALYSIS_SCHEMA,
                 ANALYSIS_MAX_TOKENS, progress or (lambda _m: None))


def generate_script(analysis: dict, style: str = "normal", target_minutes: float = 5, num_points: int = 3,
                    worry_hint: str = "", progress: Optional[ProgressCallback] = None,
                    speech_speed: float = DEFAULT_SPEECH_SPEED, how_points: int = 3) -> AIResult:
    """② 分析結果から、ずんだもんと四国めたんの掛け合い台本（台本JSON）を作る。

    style="normal" は横画面の通常の解説動画（target_minutes 分・num_points 個のポイント）、
    style="short" は縦画面の約1分のショート動画（ポイントは1つ。target_minutes・num_points は使わない）。
    """
    instruction = "\n\n".join(filter(None, [
        "次の分析結果をもとに、動画の台本を書いてください。",
        "<analysis>\n" + json.dumps(analysis, ensure_ascii=False, indent=2) + "\n</analysis>",
        f"ずんだもんの悩みは「{worry_hint.strip()}」を中心にしてください。" if worry_hint.strip() else "",
    ]))
    source_kind = "research" if analysis.get("source_kind") == "research" else "book"
    result = _call(_script_system(style, target_minutes, num_points, speech_speed, how_points)
                   + research_script_rules(source_kind),
                   [{"type": "text", "text": instruction}],
                   _script_schema(), SCRIPT_MAX_TOKENS, progress or (lambda _m: None))
    result.data["style"] = style
    if source_kind == "research":
        result.data["source_kind"] = "research"
        result.data["sources"] = analysis.get("sources") or []
        result.data["author"] = ""
    # 構造化出力の都合で全項目を必須にしているため、未使用の値（空の効果音・空のhide）は取り除いておく
    for block in result.data.get("blocks", []):
        for line in block.get("lines", []):
            if not line.get("se"):
                line.pop("se", None)
            if not line.get("hide"):
                line.pop("hide", None)
            if not line.get("show_book") or source_kind == "research":  # 調べた論文・記事の動画では本の表紙を出さない
                line.pop("show_book", None)
            if not line.get("image"):
                line.pop("image", None)
            if not line.get("image_request"):
                line.pop("image_request", None)
                line.pop("image_name", None)
            if not line.get("image") and not line.get("image_request"):
                line.pop("caption", None)
            for key in ("mood", "card", "pause", "pause_text"):
                if not line.get(key):
                    line.pop(key, None)
            if not (line.get("note") or {}).get("text"):
                line.pop("note", None)
            if not line.get("bullet"):
                line.pop("bullet", None)
        if block.get("section") == "intro" and not (block.get("slide") or {}).get("title"):
            block.pop("slide", None)  # 導入は黒板を出さない
    return result


# ---------------------------------------------------------------------------
# 論文・ネット記事をClaudeに調べてもらう（本の代わりの題材）
# ---------------------------------------------------------------------------

RESEARCH_MAX_TOKENS = 32000
RESEARCH_MAX_SEARCHES = 15      # 1回の調査でWeb検索する回数の上限（検索1回 $0.01）
RESEARCH_MAX_FETCHES = 12       # ページ・論文PDFを読み込む回数の上限
RESEARCH_MAX_CONTINUATIONS = 5  # サーバー側の検索ループが途中で区切られた（pause_turn）ときに続けさせる回数の上限
WEB_SEARCH_PRICE_USD = 0.01     # Web検索1回の料金

RESEARCH_SYSTEM = """あなたは、科学的な根拠を重視する調査担当の編集者です。YouTubeの解説動画（ずんだもんと四国めたんの掛け合い）の材料にするため、指定されたテーマについて Web検索（web_search）とページの読み込み（web_fetch）で調べ、日本語の調査レポートを書きます。

## 調べ方
- 次の順で信頼できる情報を優先する: ①学術論文（メタ分析・系統的レビュー・大規模な研究を特に優先。PubMed、Google Scholar、arXiv、J-STAGE、CiNii など）②公的機関・大学・研究機関の資料や統計（厚生労働省、WHO、総務省など）③専門家が書いた信頼できる記事・大手報道。個人ブログ・まとめサイト・広告記事は根拠にしない。
- 検索結果の要約だけで判断せず、重要な論文や記事は web_fetch で本文（論文なら要旨・結果・考察）を読み、数字は原典で確かめる。
- 日本語だけでなく英語でも検索する。なるべく新しい研究（ここ10年程度）を優先し、古い研究を使う場合は年を明記する。
- 研究の規模（何人を対象にしたか）、研究の種類（実験・観察研究・メタ分析）、限界（「相関であって因果ではない」「動物実験」「小規模」など）も記録する。
- 見つからなかったこと・研究によって結論が割れていることは、そのように書く。推測で断定したり、数字を作ったりしない。
- 利用者が URL や資料を指定した場合は、それを必ず読んで中心の材料にする。

## レポートの形式（Markdown）
# （動画で使う短いテーマ名）
## 概要
（3〜5行）
## 視聴者の悩み・よくある失敗・誤解
（多くの人がやりがちな間違ったやり方とその結果、思い込みと実際。動画の導入で、ずんだもんが失敗する話に使う）
## 意外な事実・データ
（「えっ、そうなの？」と驚く事実。数字と出典番号 [1] を付ける）
## 研究・記事ごとの要点
（出典ごとに: 何を調べたか / 分かったこと（数字）/ 研究の規模と限界）
## 成功させるための方法・実践できること
（根拠のあるやり方を具体的に。出典番号を付ける）
## 注意点
（個人差、研究の限界、医療・お金などで専門家に相談すべき点）
## 出典
1. タイトル｜著者・発行元｜年｜種類（論文/公的資料/記事）｜URL
（実際に確認できたものだけを書く。URLは実際に開いたものをそのまま書く）"""

RESEARCH_ANALYSIS_SYSTEM = """あなたは編集者です。渡された調査レポート（学術論文・公的資料・ネット記事を調べたもの）を、YouTubeの解説動画の材料として整理します。

- レポートに書かれていることだけを使い、書かれていない内容を補ったり推測で断定したりしないでください。数字は正確に写してください。
- book_title には、動画で使う短いテーマ名（例: 睡眠と記憶の関係）を、author には空文字を書いてください。
- one_line_summary・target_reader・worries・surprising_facts・common_mistakes・misconceptions・themes・key_takeaways の書き方は、本の代わりにレポートを題材にするだけで、次のとおりです。
  - worries: 視聴者が抱えていそうな悩みを、悩んでいる本人の言葉のように（例: 勉強してもすぐ忘れる）。
  - surprising_facts: 「えっ、そうなの？」と驚く事実・データを3〜6個。どの研究・資料によるものかを短く添える（例: 〇〇大学の研究では…）。
  - common_mistakes: 多くの人がやりがちな失敗と、その結果を「〇〇して、〇〇になってしまう」の形で3〜5個。
  - misconceptions: 「〇〇だと思われがちだが、実は〇〇」の形で2〜4個（無ければ空の配列）。
  - key_takeaways: 視聴者が明日から実践できる、根拠のある具体的な行動を3〜5個。
- chapters は、レポートの内容のまとまり（研究・記事ごと、または論点ごと）に分け、各まとまりの要点（2〜5個）と、具体的なデータ・研究の内容（0〜3個。研究の規模や限界も短く）をまとめてください。
- sources には、レポートの「出典」を1件ずつ書いてください（title・publisher（著者・発行元）・year・kind（論文/公的資料/記事）・url）。分からない項目は空文字にしてください。"""

RESEARCH_ANALYSIS_SCHEMA = _obj({
    **ANALYSIS_SCHEMA["properties"],
    "sources": {"type": "array", "items": _obj({
        "title": _STR, "publisher": _STR, "year": _STR, "kind": _STR, "url": _STR,
    })},
})

_RESEARCH_SCRIPT_RULES = """

## 今回の題材について（上のルールより優先）
この動画は1冊の本ではなく、Webで調べた学術論文・公的機関の資料・ネット記事の内容を解説する動画です。上のルールの「本」「書名」「本に書かれていること」は、分析結果の「調べた研究・資料」と読み替えてください。
- show_book は常に false にする（本の表紙は出さない）。本を紹介するセリフの代わりに、めたんが「今日は、〇〇の研究や記事をもとに教えてあげるわ」のように、研究をもとに話すことを伝える。
- 解説では「〇〇大学の研究によると」「〇〇年の調査では」「WHOによると」のように、出典をセリフで示す（黒板にも「〇〇大学の研究」のように書いてよい）。
- 数字・研究結果は分析結果にあるものだけを使い、作らない。1つの研究だけで言い切りすぎず（「〜という研究結果があるわ」「まだ研究の途中だけど」）、相関と因果を混同しない。
- 健康・医療・お金の話では、めたんが一言「個人差があるから、気になる人は専門家に相談してね」のように添える。
- タイトル・説明文・ハッシュタグに「本要約」「書評」は使わない。タイトルは【研究で判明】【論文で解説】【データで解説】などを使う。hashtags は「ずんだもん解説」・テーマ・関連キーワードの3つ。book_title には動画のテーマ名を、author には空文字を書く。"""

_MANUAL_RESEARCH_STEP = """まず、次のテーマについて Web検索で信頼できる情報（学術論文（メタ分析・大規模研究を優先）、公的機関の資料・統計、専門家の記事）を日本語と英語で調べ、重要なものは本文を読んで数字を確かめてください。個人ブログ・まとめサイト・広告記事は根拠にしないでください。調べた内容をもとに、上のルールで台本を作ってください。
トップレベルに "source_kind": "research" と、"sources": [{"title": ..., "publisher": ..., "year": ..., "kind": "論文/公的資料/記事", "url": ...}]（実際に確認できた出典だけ）も入れてください。"""


def research_script_rules(source_kind: str) -> str:
    return _RESEARCH_SCRIPT_RULES if source_kind == "research" else ""


def _research_tools() -> list[dict]:
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": RESEARCH_MAX_SEARCHES,
         "user_location": {"type": "approximate", "country": "JP", "timezone": "Asia/Tokyo"}},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": RESEARCH_MAX_FETCHES,
         "max_content_tokens": 40000},
    ]


def _describe_tool_use(block) -> str:
    data = getattr(block, "input", None) or {}
    if getattr(block, "name", "") == "web_search" and data.get("query"):
        return f"🔎 検索: {data['query']}"
    if getattr(block, "name", "") == "web_fetch" and data.get("url"):
        return f"📄 読み込み: {data['url']}"
    return ""


def _research_call(user_text: str, progress: ProgressCallback) -> AIResult:
    """Web検索・ページ読み込み付きで調査レポート（Markdown）を書かせる。data = {"report": str, "searches": int}。

    調査レポートは出典の引用（citations）付きの文章で返るため、構造化出力（JSON Schema）は使わない
    （引用と構造化出力は同時に使えない）。JSONへの整理は次の段階（_call）で行う。
    """
    if not api_key_configured():
        raise BookAIError(
            "Claude APIのキーが設定されていません。プロジェクト直下の .env ファイルの "
            "「# === Anthropic ===」の下にある ANTHROPIC_API_KEY= にキーを書いて、アプリを再起動してください。"
        )
    model = current_model()
    client = _client()
    kwargs: dict = {
        "model": model,
        "max_tokens": RESEARCH_MAX_TOKENS,
        "system": RESEARCH_SYSTEM,
        "tools": _research_tools(),
    }
    if "haiku" not in model:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "high"}
    if model in FALLBACK_MODELS:
        kwargs["fallbacks"] = "default"
        kwargs["betas"] = [FALLBACK_BETA]

    assistant_blocks: list = []
    totals = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "searches": 0}
    notes: list[str] = []
    message = None
    try:
        for _ in range(RESEARCH_MAX_CONTINUATIONS + 1):
            messages: list = [{"role": "user", "content": user_text}]
            if assistant_blocks:
                # 検索ループが途中で区切られた（pause_turn）ので、ここまでの内容を渡して続きをやらせる
                messages.append({"role": "assistant", "content": assistant_blocks})
            with client.beta.messages.stream(messages=messages, **kwargs) as stream:
                progress("Claudeが調べています…（数分かかることがあります）")
                for event in stream:
                    if event.type == "content_block_stop":
                        block = getattr(event, "content_block", None)
                        if block is not None and block.type == "server_tool_use":
                            desc = _describe_tool_use(block)
                            if desc:
                                progress(desc)
                message = stream.get_final_message()
            assistant_blocks = assistant_blocks + list(message.content)
            usage = message.usage
            totals["in"] += usage.input_tokens or 0
            totals["out"] += usage.output_tokens or 0
            totals["cache_read"] += getattr(usage, "cache_read_input_tokens", 0) or 0
            totals["cache_write"] += getattr(usage, "cache_creation_input_tokens", 0) or 0
            server_use = getattr(usage, "server_tool_use", None)
            totals["searches"] += getattr(server_use, "web_search_requests", 0) or 0
            if message.stop_reason != "pause_turn":
                break
            progress("調査を続けています…")
    except anthropic.AuthenticationError as e:
        raise BookAIError("APIキーが正しくありません。.env の ANTHROPIC_API_KEY を確認してください。") from e
    except anthropic.PermissionDeniedError as e:
        raise BookAIError(f"このAPIキーでは利用できません（{e.message}）。Web検索が組織の設定で無効になっている場合は、"
                          "Claude Console の設定で有効にしてください。") from e
    except anthropic.NotFoundError as e:
        raise BookAIError(f"モデル「{model}」が見つかりません。.env の ANTHROPIC_MODEL を確認してください。") from e
    except anthropic.RateLimitError as e:
        raise BookAIError("APIの利用上限に達しました。少し時間をおいてから再度お試しください。") from e
    except anthropic.BadRequestError as e:
        raise BookAIError(f"リクエストが受け付けられませんでした（{e.message}）") from e
    except anthropic.APIStatusError as e:
        raise BookAIError(f"Claude APIでエラーが発生しました（{e.status_code}: {e.message}）。時間をおいて再度お試しください。") from e
    except anthropic.APIConnectionError as e:
        raise BookAIError("Claude APIに接続できませんでした。インターネット接続を確認してください。") from e

    if message is None:
        raise BookAIError("Claudeから応答がありませんでした。")
    if message.stop_reason == "refusal":
        raise BookAIError("Claudeがこの調査を処理できませんでした（安全性の判定で断られました）。テーマや指示を見直してください。")
    if message.stop_reason == "pause_turn":
        notes.append("調査が長くなったため途中で区切りました（ここまでに調べた内容でレポートを作っています）。")
    if message.stop_reason == "max_tokens":
        notes.append("レポートが長すぎて最後が切れている可能性があります。")
    report = "".join(block.text for block in assistant_blocks if getattr(block, "type", "") == "text").strip()
    if not report:
        raise BookAIError("調査レポートを受け取れませんでした。テーマを変えるか、時間をおいて再度お試しください。")
    if getattr(message, "model", model) != model:
        notes.append(f"最初のモデルで断られたため、{message.model} が代わりに処理しました。")
    return AIResult(
        data={"report": report, "searches": totals["searches"]},
        model=model,
        input_tokens=totals["in"],
        output_tokens=totals["out"],
        cache_read_tokens=totals["cache_read"],
        cache_write_tokens=totals["cache_write"],
        notes=notes,
    )


def research_topic(topic: str, focus: str = "", urls: Optional[list[str]] = None, material_text: str = "",
                   worry_hint: str = "", progress: Optional[ProgressCallback] = None) -> AIResult:
    """① 論文・ネット記事をClaudeにWebで調べさせ、本の分析結果と同じ形式（+ sources）に整理する。

    2段階: (a) Web検索・ページ読み込み付きで調査レポート（Markdown・出典付き）を書かせる
           (b) レポートを分析結果のJSON（ANALYSIS_SCHEMA + sources）に整理する（構造化出力）
    返り値の data は分析結果で、"source_kind": "research" と "research_report"（レポート本文）が入る。
    cost_usd にはWeb検索の料金は含まれないので、searches（検索回数）× WEB_SEARCH_PRICE_USD を足して表示する。
    """
    if not topic.strip():
        raise BookAIError("調べるテーマを入力してください。")
    progress = progress or (lambda _m: None)
    url_list = [u.strip() for u in (urls or []) if u.strip()]
    user_text = "\n\n".join(filter(None, [
        f"テーマ: {topic.strip()}",
        f"特に知りたいこと・観点: {focus.strip()}" if focus.strip() else "",
        f"動画で扱いたい悩み（ずんだもんの悩み）: {worry_hint.strip()}" if worry_hint.strip() else "",
        ("必ず読んで材料にしてほしいURL:\n" + "\n".join(f"- {u}" for u in url_list)) if url_list else "",
        f"利用者が用意した資料（これも材料にする）:\n<material>\n{material_text.strip()}\n</material>"
        if material_text.strip() else "",
        "このテーマを調べて、調査レポートを書いてください。",
    ]))
    research = _research_call(user_text, progress)
    report = research.data["report"]
    progress("調べた内容を整理しています…")
    analysis = _call(
        RESEARCH_ANALYSIS_SYSTEM,
        [{"type": "text", "text": f"<report>\n{report}\n</report>\n\n上の調査レポートを整理してください。"}],
        RESEARCH_ANALYSIS_SCHEMA, ANALYSIS_MAX_TOKENS, progress,
    )
    data = dict(analysis.data)
    data["source_kind"] = "research"
    data["research_report"] = report
    return AIResult(
        data=data,
        model=research.model,
        input_tokens=research.input_tokens + analysis.input_tokens,
        output_tokens=research.output_tokens + analysis.output_tokens,
        cache_read_tokens=research.cache_read_tokens + analysis.cache_read_tokens,
        cache_write_tokens=research.cache_write_tokens + analysis.cache_write_tokens,
        notes=research.notes + analysis.notes,
        searches=research.data.get("searches", 0),
    )


READING_CHECK_SYSTEM = """あなたは日本語の音声合成（VOICEVOX）の読み上げをチェックする校正者です。
各セリフについて、表記（text）と、音声合成が実際に読む予定の読み（kana: カタカナ。' はアクセント、/ は区切り、_ は無声化、「オ」「エ」は長音を表すことがある）を比べ、
読み間違い（例: 他人事→タニンゴト、7時間→シチジカン、英字の略語の不自然な読み）を見つけてください。

- 修正が必要な言葉だけを corrections に {"word": 表記の一部, "reading": 正しい読み（ひらがな or カタカナ）} で返す。
- word はセリフの中に実際に出てくる文字列をそのまま使い、できるだけ短く（その言葉だけ）にする。ただし短すぎて他の言葉の一部まで置き換わる恐れがある場合（1文字の漢字など）は、前後を含めて一意になる長さにする。
- アクセントの違いや、どちらの読みも正しい言葉（例: 重複→じゅうふく/ちょうふく）は指摘しない。
- 間違いが無ければ corrections は空の配列にする。"""

READING_CHECK_SCHEMA = _obj({"corrections": {"type": "array", "items": _obj({"word": _STR, "reading": _STR})}})


def check_readings(lines: list[tuple[str, str]], progress: Optional[ProgressCallback] = None) -> AIResult:
    """VOICEVOXの読み（kana）とセリフを照らし合わせて、読み間違いの修正候補を返す。

    lines は (セリフ, VOICEVOXのkana) のリスト。結果の data["corrections"] を読み方辞書に追加して使う。
    """
    payload = "\n".join(
        json.dumps({"text": text, "kana": kana}, ensure_ascii=False) for text, kana in lines
    )
    return _call(READING_CHECK_SYSTEM, [{"type": "text", "text": payload}], READING_CHECK_SCHEMA,
                 8000, progress or (lambda _m: None))
