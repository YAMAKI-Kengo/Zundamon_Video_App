"""
動画のタイトル・説明文・タグの自動生成（YouTube等への投稿用）。通常の動画とショート動画で作り分ける。

AIで台本を作った場合は、AIが提案したタイトル案（Project.title_candidates）・説明文の冒頭
（Project.description_lead）・ハッシュタグ（Project.hashtags）を使い、足りない部分や台本を手で作った場合は、
台本の内容（本のタイトル・導入の悩み・解説のポイント・まとめ・各シーンの秒数）からテンプレートで組み立てる。

調べたYouTubeの定石を反映している:
- タイトル: スマホで全文が見えるのは25〜30字前後 → 大事な言葉（悩み・書名）を前に置き、数字と【】で要点を示す。
  ショートは30字以内で、冒頭のフックそのものをタイトルにする。
- 説明文: 最初の2〜3行（折りたたまれる前に見える部分）に「何が分かる動画か」を書く。
  通常の動画は目次（YouTubeのチャプター形式 "0:00 見出し"）を入れ、ショートは短くして本編への誘導を入れる。
- ハッシュタグ: タイトルの上に表示されるのは説明文の先頭から3つまでなので、大事な3つだけにする
  （ショートは #Shorts を先頭に入れる）。
- クレジット: VOICEVOXの利用規約で必要な表記（"VOICEVOX:ずんだもん" 等）を必ず入れる。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.models import SECTION_LABELS, Project, Scene
from src.utils.asset_loader import get_character_display_name, load_character_config

TITLE_MAX_CHARS = 100          # YouTubeのタイトルの文字数上限
TITLE_VISIBLE_CHARS = 32       # スマホの一覧で省略されずに見える文字数の目安
CHAPTER_MIN_SECONDS = 10.0     # YouTubeのチャプターとして認識される1区間の最短秒数
BGM_CREDITS_PATH = Path(__file__).resolve().parents[2] / "config" / "bgm_credits.json"

_EMPHASIS = re.compile(r"\*\*(.+?)\*\*")
_HASHTAG_UNSAFE = re.compile(r"[\s　・「」『』【】（）()！!？?、。,.:：/／\-－~〜#＃]")


def _plain(text: str) -> str:
    return _EMPHASIS.sub(r"\1", text or "").strip()


def is_research(project: Project) -> bool:
    """論文・ネット記事をClaudeに調べてもらった動画か（本の解説ではない）。"""
    return project.source_kind == "research"


def is_english(project: Project) -> bool:
    """英会話レッスンの動画か（src.services.english_lesson）。"""
    return project.source_kind == "english"


def _lesson_phrases(project: Project) -> list[str]:
    return [
        f"{p.get('en', '')} ― {p.get('ja', '')}".strip(" ―")
        for p in (project.lesson or {}).get("phrases", []) if isinstance(p, dict) and p.get("en")
    ]


def is_short(project: Project) -> bool:
    """ショート動画として扱うか（AIでショート用の台本を作った場合、または縦画面の場合）。"""
    return project.video_style == "short" or project.resolution[1] > project.resolution[0]


@dataclass
class Chapter:
    start: float
    label: str
    duration: float


@dataclass
class VideoMetadata:
    title_candidates: list[str]
    description: str
    tags: list[str] = field(default_factory=list)
    chapters: list[Chapter] = field(default_factory=list)

    @property
    def short_chapters(self) -> list[Chapter]:
        """YouTubeのチャプターとして短すぎる（10秒未満）区間。"""
        return [c for c in self.chapters if c.duration < CHAPTER_MIN_SECONDS]


def format_timestamp(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapter_label(scene: Scene) -> str:
    """目次の見出し（スライドの見出し。スライドが無ければ場面の名前）。"""
    if scene.section == "ending":
        return "エンディング"
    section_name = SECTION_LABELS.get(scene.section, "").split("（")[0] or "本編"
    if scene.show_book_cover:  # 本を紹介するシーンの黒板（書名）は見出しではないので、場面の名前にする
        return section_name
    return _plain(scene.slide_title) or section_name


def scene_chapter_labels(project: Project) -> list[str]:
    """各シーンで画面左上に出す見出し（説明文の目次と同じ）。表示しないシーンは空文字。

    ショート動画（目次が無い）と、表示をオフにした場合はすべて空。エンディングにも出さない。
    """
    if not project.show_chapter_label or is_short(project):
        return ["" for _ in project.scenes]
    return ["" if s.section == "ending" or s.card_text else chapter_label(s) for s in project.scenes]


def build_chapters(scenes: list[Scene]) -> list[Chapter]:
    """スライド（見出し）・場面が切り替わる位置ごとに目次を作る。同じ見出しが続く場合はまとめる。"""
    chapters: list[Chapter] = []
    t = 0.0
    labels = [chapter_label(s) for s in scenes]
    for i in range(len(scenes) - 2, -1, -1):
        if scenes[i].card_text:  # 場面転換テロップは、次の場面の目次に含める（数秒だけの目次を作らない）
            labels[i] = labels[i + 1]
    for scene, label in zip(scenes, labels):
        if chapters and chapters[-1].label == label:
            chapters[-1].duration += scene.duration
        else:
            chapters.append(Chapter(start=t, label=label, duration=scene.duration))
        t += scene.duration
    return chapters


def _section_bullets(scenes: list[Scene], section: str) -> list[str]:
    for scene in scenes:
        if scene.section == section and scene.slide_bullets:
            return [_plain(b) for b in scene.slide_bullets if _plain(b)]
    return []


def _explain_points(scenes: list[Scene]) -> list[str]:
    """解説パートのスライド見出し（重複を除いて登場順。「ポイント1」などの番号は外す）。"""
    points: list[str] = []
    for scene in scenes:
        # 「ポイント1：」「第3位：」「Q2：」「ステップ1：」「第1章：」などの番号の部分を外す
        title = re.sub(r"^(第?\s*[0-9０-９]+\s*[位章]|[^\s：:]{0,6}?\s*[0-9０-９]+)\s*[：:．.、]\s*", "",
                       _plain(scene.slide_title))
        if scene.section == "explain" and title and title not in points:
            points.append(title)
    return points


def _first_line(scenes: list[Scene]) -> str:
    return next((_plain(s.text).split("\n")[0] for s in scenes if s.text.strip()), "")


def _fit(title: str) -> str:
    return title if len(title) <= TITLE_MAX_CHARS else title[: TITLE_MAX_CHARS - 1] + "…"


SERIES_TAG = "【ずんだもん解説】"  # 解説動画（本・論文記事）のタイトルに必ず入れるシリーズ名


def with_series_tag(title: str, project: Project) -> str:
    """解説動画のタイトルの先頭に【ずんだもん解説】を付ける（英会話は【毎日英会話】のシリーズなので付けない）。"""
    title = title.strip()
    if not title or is_english(project) or SERIES_TAG in title:
        return title
    title = re.sub(r"【ずんだもん】|｜ずんだもん解説$", "", title).strip()
    return _fit(SERIES_TAG + title)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def build_title_candidates(project: Project) -> list[str]:
    """タイトル案（AIの案があれば先頭に、続けてテンプレートの案）。解説動画は必ず【ずんだもん解説】入り。"""
    return _dedupe([with_series_tag(t, project) for t in _title_candidates(project)])


def _title_candidates(project: Project) -> list[str]:
    book = project.book_title.strip()
    if is_research(project):
        return _research_title_candidates(project)
    if is_english(project):
        lesson = project.lesson or {}
        day = lesson.get("day", "")
        theme = lesson.get("theme") or book or "英会話"
        phrase = next((p.get("en", "") for p in lesson.get("phrases", []) if isinstance(p, dict)), "")
        label = "1週間のまとめ" if str(day) == "7" else f"Day{day}"
        # 企画の中身を先頭に置き、シリーズ名（Day の番号）は最後に付ける（初めて見る人がクリックを避けないように）
        template = [
            f"{theme}で使える {phrase}｜毎日英会話 {label}" if phrase else "",
            f"{theme}で固まらない英語｜毎日英会話 {label}",
        ]
        if is_short(project):
            template = [f"{phrase} って言えますか？ #Shorts" if phrase else "", f"【毎日英会話】{theme} #Shorts"]
        return _dedupe([_fit(t.strip()) for t in project.title_candidates if t.strip()] + [_fit(t) for t in template])
    book_part = f"『{book}』" if book else "話題の本"
    worries = _section_bullets(project.scenes, "intro")
    worry = worries[0] if worries else ""
    ai = [_fit(t.strip()) for t in project.title_candidates if t.strip()]

    if is_short(project):
        hook = _first_line(project.scenes).rstrip("。")
        template = [
            f"{hook} #Shorts" if hook and len(hook) <= 30 else "",
            f"「{worry}」を一瞬で変える方法 #Shorts" if worry else "",
            f"9割が知らない{book_part}の結論 #Shorts",
        ]
        return _dedupe(ai + [_fit(t) for t in template])

    n_points = len(_explain_points(project.scenes))
    points_part = f"{n_points}つのポイント" if n_points else "要点"
    minutes = max(1, math.ceil(project.total_duration / 60))
    template = [
        f"「{worry}」を解決する{points_part}｜{book_part}" if worry and len(worry) <= 16 else "",
        f"一瞬で変わる{points_part}｜{book_part}",
        f"{book_part}を{minutes}分で要約｜{points_part}",
    ]
    return _dedupe(ai + [_fit(t) for t in template])


def _research_title_candidates(project: Project) -> list[str]:
    topic = project.book_title.strip() or "話題のテーマ"
    ai = [_fit(t.strip()) for t in project.title_candidates if t.strip()]
    if is_short(project):
        hook = _first_line(project.scenes).rstrip("。")
        template = [
            f"{hook} #Shorts" if hook and len(hook) <= 30 else "",
            f"9割が知らない{topic}の真実 #Shorts",
        ]
        return _dedupe(ai + [_fit(t) for t in template])
    n_points = len(_explain_points(project.scenes))
    points_part = f"{n_points}つのポイント" if n_points else "要点"
    minutes = max(1, math.ceil(project.total_duration / 60))
    template = [
        f"研究で判明！{topic}の{points_part}",
        f"{topic}で失敗する人の共通点",
        f"{minutes}分で分かる{topic}｜研究データで解説",
    ]
    return _dedupe(ai + [_fit(t) for t in template])


MAX_HASHTAGS = 5
REQUIRED_HASHTAG = "ずんだもん解説"
# AIのハッシュタグが足りないときに補う、多くの人が見ている一般的なハッシュタグ
DEFAULT_HASHTAGS = {
    "english": ["英会話", "英語学習", "リスニング", "TOEIC"],
    "research": ["雑学", "ライフハック", "勉強", "自己啓発"],
    "book": ["本要約", "読書", "自己啓発", "本紹介"],
}


def build_hashtags(project: Project) -> list[str]:
    """説明文・Xの投稿に入れるハッシュタグ（最大5つ）。先頭は必ず #ずんだもん解説、残りは一般的でよく見られる言葉。
    YouTube では先頭3つがタイトルの上に表示されるので、大事な順に並べる。"""
    kind = "english" if is_english(project) else "research" if is_research(project) else "book"
    tags = []
    for text in [REQUIRED_HASHTAG] + list(project.hashtags) + DEFAULT_HASHTAGS[kind]:
        tag = _HASHTAG_UNSAFE.sub("", str(text).lstrip("#＃"))
        if tag and len(tag) <= 20 and tag.lower() != "shorts":
            tags.append(f"#{tag}")
    return _dedupe(tags)[:MAX_HASHTAGS]


X_MAX_WEIGHT = 280       # X の1投稿の上限（日本語など全角は1文字=2、半角は1、URLは23として数える）
X_URL_PLACEHOLDER = "（ここに動画のURLを貼る）"


def x_post_length(text: str) -> int:
    """X の文字数の数え方（全角=2・半角=1・URL=23）で、投稿文の長さを数える。280まで投稿できる。"""
    total = 0
    for part in re.split(r"(https?://\S+)", text):
        if part.startswith(("http://", "https://")):
            total += 23
            continue
        for ch in part.replace(X_URL_PLACEHOLDER, ""):
            total += 1 if ord(ch) <= 0x10FF or 0x2000 <= ord(ch) <= 0x200D or 0x2010 <= ord(ch) <= 0x201F \
                or 0x2032 <= ord(ch) <= 0x2037 else 2
        total += 23 * part.count(X_URL_PLACEHOLDER)
    return total


def build_x_post(project: Project) -> str:
    """X（旧Twitter）用の投稿文: 見どころの一言 → 動画のタイトル → URL → ハッシュタグ（280以内に収める）。"""
    lead = [line.strip() for line in project.description_lead.splitlines() if line.strip()]
    if not lead:
        spec_text = re.sub(r"\*\*|==", "", str((project.thumbnail or {}).get("text") or "")).replace("\n", "")
        lead = [spec_text] if spec_text else []
    title = project.video_title.strip() or (build_title_candidates(project) or [""])[0]
    hashtags = " ".join(build_hashtags(project))
    label = "▶ 1分で分かるショート" if is_short(project) else "▶ 動画はこちら"
    for n_lead in range(len(lead), -1, -1):
        lines = lead[:n_lead] + ["", f"{label}", title, X_URL_PLACEHOLDER, "", hashtags]
        text = "\n".join(lines).strip()
        if x_post_length(text) <= X_MAX_WEIGHT:
            return text
    return "\n".join([title, X_URL_PLACEHOLDER, hashtags])


TAGS_MAX_CHARS = 500  # YouTubeのタグ欄の上限（カンマ・スペースを含むタグの引用符も数える）
# タグは、検索したときの表記ゆれ・変換ミス・打ち間違いでも動画が見つかるようにするためのもの。
# よく検索される言葉について、ひらがな・カタカナ・英語・よくある打ち間違いを入れておく
CHARACTER_TAGS = ["ずんだもん", "ズンダモン", "zundamon", "ずんだもん解説", "ずんだモン", "四国めたん", "しこくめたん", "めたん",
                  "VOICEVOX", "ボイスボックス"]
GUEST_TAGS = {"kasukabe_tsumugi": ["春日部つむぎ", "かすかべつむぎ", "つむぎ"]}
KIND_TAGS = {
    "english": ["英会話", "えいかいわ", "英語", "えいご", "English", "english conversation", "英語学習", "英語 勉強",
                "リスニング", "listening", "TOEIC", "toeic", "トーイック", "英検", "毎日英会話", "シャドーイング", "shadowing",
                "英語 初心者", "スピーキング"],
    "research": ["解説", "研究", "論文", "雑学", "ざつがく", "豆知識", "ライフハック"],
    "book": ["本要約", "本 要約", "要約", "本紹介", "書評", "読書", "どくしょ", "おすすめ本", "ビジネス書", "自己啓発"],
}
_KATAKANA = re.compile(r"[ァ-ヶー]")


def _to_hiragana(text: str) -> str:
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text)


def _to_katakana(text: str) -> str:
    return "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in text)


def tag_variants(word: str) -> list[str]:
    """1つの言葉の、検索で使われそうな書き方の違い（記号・スペースを除いた形、ひらがな⇔カタカナ）。"""
    word = re.sub(r"[『』「」【】]", "", str(word or "")).strip()
    if not word:
        return []
    variants = [word, re.sub(r"[\s　・]", "", word)]
    if _KATAKANA.search(word):
        variants.append(_to_hiragana(word))
    if re.fullmatch(r"[ぁ-ゖー]+", word):
        variants.append(_to_katakana(word))
    main = re.split(r"[（(：:～〜\-―]", word)[0].strip()  # 副題・かっこを除いた書名
    if main and main != word:
        variants.append(main)
    return _dedupe(variants)


def _tags_length(tags: list[str]) -> int:
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def build_tags(project: Project) -> list[str]:
    """YouTubeのタグ（表記ゆれ・変換ミス・打ち間違いでも検索に引っかかるように）。上限500字に収める。"""
    kind = "english" if is_english(project) else "research" if is_research(project) else "book"
    tags: list[str] = []
    for word in (project.book_title, project.book_author):
        tags += tag_variants(word)
    tags += [t.lstrip("#＃").strip() for t in project.tag_candidates]
    tags += [t.lstrip("#＃") for t in project.hashtags]
    if kind == "english":
        tags += [p.split(" ― ")[0] for p in _lesson_phrases(project)][:3]
    tags += KIND_TAGS[kind] + CHARACTER_TAGS
    speakers = {s.speaker for s in project.scenes}
    for guest, names in GUEST_TAGS.items():
        if guest in speakers:
            tags += names
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        tag = re.sub(r"[<>,，、]", " ", str(tag)).strip()
        if not tag or len(tag) > 30 or tag.lower() in seen:
            continue
        if _tags_length(result + [tag]) > TAGS_MAX_CHARS:
            continue
        seen.add(tag.lower())
        result.append(tag)
    return result


def _load_bgm_credits() -> dict[str, str]:
    """BGMの曲名 → 説明文に出す表記（作者名つき）。config/bgm_credits.json から読む。"""
    try:
        data = json.loads(BGM_CREDITS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items() if not str(k).startswith("_") and str(v).strip()}


def _normalize_title(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip().lower()


def bgm_credit(path: str, credits: dict[str, str]) -> str:
    """BGMファイルのクレジット表記。ファイル名は「場面_曲名」の形なので、曲名の部分で表記を探す。"""
    stem = Path(path).stem
    title = stem.split("_", 1)[1] if "_" in stem else stem
    normalized = _normalize_title(title)
    for key, credit in credits.items():
        if _normalize_title(key) == normalized:
            return _with_honorific(credit)
    return f"{title.replace('_', ' ')}（作者名を config/bgm_credits.json に登録してください）"


def _with_honorific(credit: str) -> str:
    """「曲名 / 作者」「曲名 by 作者」の表記を、作者名に「様」を付けた「曲名 / 作者 様」にそろえる。"""
    credit = credit.strip()
    if credit.endswith("様"):
        return credit
    match = re.match(r"^(.*?)\s*(?:/|／|\bby\b)\s*([^/／]+)$", credit)
    if not match or not match.group(1).strip():
        return credit
    return f"{match.group(1).strip()} / {match.group(2).strip()} 様"


def _credits(project: Project) -> list[str]:
    speakers = _dedupe([s.speaker for s in project.scenes if s.text.strip()])
    bgm_paths = _dedupe([p for p in (project.resolve_bgm_path(s) for s in project.scenes) if p])
    lines = ["■ クレジット・使用素材"]
    if speakers:
        lines.append("・音声：" + "、".join(f"VOICEVOX:{get_character_display_name(s)}" for s in speakers))
    if any(s.voice_path for s in project.scenes):
        lines.append("・英語音声：Kokoro（hexgrad/Kokoro-82M・Apache License 2.0）")
    illustrators: dict[str, list[str]] = {}
    for key in speakers + [g for s in project.scenes for g in s.guests]:
        name = str((load_character_config().get(key) or {}).get("illustrator") or "").strip() \
            or f"（{get_character_display_name(key)}の立ち絵の作者名を config/characters.json の illustrator に書いてください）"
        chars = illustrators.setdefault(name, [])
        if get_character_display_name(key) not in chars:
            chars.append(get_character_display_name(key))
    for name, chars in illustrators.items():
        lines.append(f"・立ち絵（{'・'.join(chars)}）：{name}")
    lines.append("・効果音：効果音ラボ（https://soundeffect-lab.info/sound/anime/）")
    if bgm_paths:
        credits = _load_bgm_credits()
        lines.append("・BGM：OpenTracks（https://opentracks.com/）")
        lines += [f"　{bgm_credit(p, credits)}" for p in bgm_paths]
    return lines


def refresh_credits(description: str, project: Project) -> str:
    """説明文の中のクレジット欄だけを、今のBGM・話者・config/bgm_credits.json に合わせて作り直す。

    説明文は台本を読み込んだときに一度作って保存するので、あとからBGMや作者名の登録を変えても
    古いままになってしまう。手で書き換えた他の部分はそのままにして、クレジット欄（「■ クレジット」の
    見出しから次の空行まで）だけを差し替える。クレジット欄が無ければ何もしない。
    """
    lines = description.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith("■ クレジット")), None)
    if start is None:
        return description
    end = start + 1
    while end < len(lines) and lines[end].strip() and not lines[end].startswith("■"):
        end += 1
    return "\n".join(lines[:start] + _credits(project) + lines[end:])


def _source_lines(project: Project) -> list[str]:
    """説明文の「参考文献・出典」（論文・記事を調べた動画用。あおり系との違いになり、信頼につながる）。"""
    lines = ["■ 参考文献・出典"]
    for i, s in enumerate(project.sources, start=1):
        meta = "、".join(x for x in (s.get("publisher", ""), s.get("year", "")) if x)
        lines.append(f"[{i}] {s.get('title', '')}" + (f"（{meta}）" if meta else ""))
        if s.get("url"):
            lines.append(f"    {s['url']}")
    if len(lines) == 1:
        lines.append("（ここに参考にした論文・記事を書いてください）")
    lines.append("※研究結果には個人差や研究の限界があります。詳しくは各出典をご確認ください。")
    return lines


def _book_line(project: Project) -> str:
    if is_research(project):
        topic = project.book_title.strip()
        return f"「{topic}」" if topic else "今回のテーマ"
    book, author = project.book_title.strip(), project.book_author.strip()
    if not book:
        return "今回の本"
    return f"『{book}』" + (f"（{author}）" if author else "")


def _lead(project: Project) -> list[str]:
    """説明文の冒頭2〜3行（折りたたまれる前に見える部分）。"""
    if project.description_lead.strip():
        return [line for line in project.description_lead.strip().splitlines()]
    points = _explain_points(project.scenes)
    worries = _section_bullets(project.scenes, "intro")
    if is_research(project):
        first = f"{_book_line(project)}について、論文や研究データをもとに、ずんだもんと四国めたんが分かりやすく解説します。"
    elif worries:
        first = f"「{worries[0]}」そんな悩みを、{_book_line(project)}が解決します。"
    else:
        first = f"{_book_line(project)}の要点を、ずんだもんと四国めたんが分かりやすく解説します。"
    second = f"この動画では、{'・'.join(points[:3])}を紹介します。" if points else ""
    return [line for line in (first, second) if line]


def _english_description(project: Project, chapters: list[Chapter]) -> str:
    lesson = project.lesson or {}
    day = str(lesson.get("day", ""))
    theme = lesson.get("theme") or project.book_title
    lead = project.description_lead.strip().splitlines() if project.description_lead.strip() else [
        f"今週のテーマ「{theme}」の" + ("1週間のまとめです。" if day == "7" else f"{day}日目です。"),
        "音声のあとに声に出して、一緒に練習しましょう！",
    ]
    phrases = _lesson_phrases(project)
    if is_short(project):
        lines = lead + ["", "▼ 今日のレッスン（本編）はこちら", "（ここに本編の動画のリンクを貼ってください）",
                        "▼ 毎日のレッスン（再生リスト）", "（ここに再生リストのリンクを貼ってください）", ""]
        if phrases:
            lines += ["■ 今日のフレーズ"] + [f"・{p}" for p in phrases] + [""]
        lines += _credits(project) + ["", " ".join(build_hashtags(project))]
        return "\n".join(lines)
    lines = lead + [""]
    if phrases:
        lines += ["■ " + ("今週のフレーズ" if day == "7" else "今日のフレーズ")] + [f"・{p}" for p in phrases] + [""]
    if chapters:
        lines += ["■ 目次"] + [f"{format_timestamp(c.start)} {c.label}" for c in chapters] + [""]
    lines += [
        "■ 練習のしかた",
        "・「リピート！」が出たら、音声のあとに続けて声に出してみましょう",
        "・「英語で言ってみよう！」が出たら、答えが流れる前に英語で言ってみましょう",
        "",
        "1日6分で、今日ひとつ「言えた！」が増える英会話。ずんだもんと一緒に、間違えながら覚えましょう。",
        "💬 今日のフレーズを使って、コメント欄に英語を1文書いてみてください（書くことが一番の練習になります）。",
        "▼ 今週のレッスン（再生リスト・Day1から順番に）",
        "（ここに再生リストのリンクを貼ってください）",
        "毎日更新・1週間ごとにテーマが変わります。チャンネル登録して一緒に続けましょう！",
        "",
    ]
    lines += _credits(project) + ["", " ".join(build_hashtags(project))]
    return "\n".join(lines)


def build_description(project: Project, chapters: list[Chapter]) -> str:
    if is_english(project):
        return _english_description(project, chapters)
    hashtags = build_hashtags(project)
    if is_short(project):
        lines = _lead(project) + [
            "",
            "▼ 詳しい解説（本編）はこちら",
            "（ここに本編の動画のリンクを貼ってください）",
            "",
        ]
        lines += (_source_lines(project) + [""]) if is_research(project) else \
            ["■ 紹介した本", _book_line(project), "（ここに購入リンクを貼ってください）", ""]
        lines += _credits(project) + ["", " ".join(hashtags)]
        return "\n".join(lines)

    points = _explain_points(project.scenes)
    worries = _section_bullets(project.scenes, "intro")
    summary = _section_bullets(project.scenes, "summary")
    lines = _lead(project) + [""]
    if worries:
        lines += ["■ こんな悩みはありませんか？"] + [f"・{w}" for w in worries] + [""]
    if points:
        lines += ["■ この動画で分かること"] + [f"・{p}" for p in points] + [""]
    if chapters:
        lines += ["■ 目次"] + [f"{format_timestamp(c.start)} {c.label}" for c in chapters] + [""]
    if summary:
        lines += ["■ 今回のまとめ"] + [f"・{s}" for s in summary] + [""]
    if is_research(project):
        lines += _source_lines(project) + [""]
    else:
        lines += ["■ 紹介した本", _book_line(project), "（ここに購入リンクを貼ってください）", ""]
    request = "このテーマを解説してほしい" if is_research(project) else "この本を解説してほしい"
    lines += [
        "役に立ったら、高評価・チャンネル登録をよろしくお願いします！",
        f"感想や「{request}」というリクエストは、コメント欄で教えてください。", "",
    ]
    lines += _credits(project) + ["", " ".join(hashtags)]
    return "\n".join(lines)


def generate_metadata(project: Project) -> VideoMetadata:
    chapters = [] if is_short(project) else build_chapters(project.scenes)
    return VideoMetadata(
        title_candidates=build_title_candidates(project),
        description=build_description(project, chapters),
        tags=build_tags(project),
        chapters=chapters,
    )


def apply_generated_metadata(project: Project, overwrite: bool = True) -> VideoMetadata:
    """自動生成したタイトル・説明文・タグをプロジェクトに設定する（overwrite=False なら空欄だけ埋める）。"""
    meta = generate_metadata(project)
    if overwrite or not project.video_title:
        project.video_title = meta.title_candidates[0] if meta.title_candidates else ""
    if overwrite or not project.video_description:
        project.video_description = meta.description
    if overwrite or not project.video_tags:
        project.video_tags = meta.tags
    if overwrite or not project.x_post:
        project.x_post = build_x_post(project)
    if not project.pinned_comment:
        project.pinned_comment = build_pinned_comment(project)
    return meta


def build_pinned_comment(project: Project) -> str:
    """YouTubeのコメント欄に固定するコメント（AIが書いていないときの型）。コメントしたくなる質問で終える。"""
    if project.promo_of or is_short(project):
        return "\n".join([
            "▶ 詳しい解説（本編）はこちら",
            "（ここに本編の動画のURLを貼ってください）",
            "",
            "💬 気になったところ、コメントで教えてね！",
        ])
    if is_english(project):
        phrases = _lesson_phrases(project)
        lines = ["📌 今日のフレーズ"] + [f"・{p}" for p in phrases[:3]]
        first = phrases[0].split(" ― ")[0] if phrases else ""
        return "\n".join(lines + [
            "",
            "💬 このフレーズを使って、コメントに英語を1文書いてみてね！" + (f"（例: {first}）" if first else ""),
            "明日も一緒に「言えた！」を増やそう。",
        ])
    points = _explain_points(project.scenes)[:3]
    lines = ["📌 今日のポイント"] + [f"・{p}" for p in points] if points else ["📌 見てくれてありがとう！"]
    return "\n".join(lines + [
        "",
        "💬 あなたはどれが一番刺さった？ コメントで教えてね！",
    ])


def export_text(project: Project) -> str:
    """投稿時にコピーしやすいよう、タイトル・説明文・タグを1つのテキストにまとめる。"""
    return "\n".join([
        "【タイトル】", project.video_title, "",
        "【説明文】", refresh_credits(project.video_description, project), "",
        "【タグ】", ", ".join(project.video_tags), "",
        "【X（旧Twitter）の投稿文】", project.x_post or build_x_post(project), "",
        "【固定コメント】", project.pinned_comment or build_pinned_comment(project),
    ])
