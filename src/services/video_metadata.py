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
from src.utils.asset_loader import get_character_display_name

TITLE_MAX_CHARS = 100          # YouTubeのタイトルの文字数上限
TITLE_VISIBLE_CHARS = 32       # スマホの一覧で省略されずに見える文字数の目安
CHAPTER_MIN_SECONDS = 10.0     # YouTubeのチャプターとして認識される1区間の最短秒数
BASE_TAGS = ["本要約", "書評", "本紹介", "読書", "ずんだもん", "四国めたん", "VOICEVOX"]
SHORT_TAGS = ["Shorts", "本要約", "ずんだもん"]
RESEARCH_TAGS = ["ずんだもん解説", "解説", "研究", "論文", "ずんだもん", "四国めたん", "VOICEVOX"]
RESEARCH_SHORT_TAGS = ["Shorts", "ずんだもん解説", "ずんだもん"]
BGM_CREDITS_PATH = Path(__file__).resolve().parents[2] / "config" / "bgm_credits.json"
ENGLISH_TAGS =["英会話", "英語学習", "毎日英会話", "英語リスニング", "シャドーイング", "ずんだもん", "四国めたん", "VOICEVOX"]

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
        title = re.sub(r"^(ポイント|失敗の理由|成功のコツ)\s*[0-9０-９]+\s*[：:．.、]?\s*", "", _plain(scene.slide_title))
        if scene.section == "explain" and title and title not in points:
            points.append(title)
    return points


def _first_line(scenes: list[Scene]) -> str:
    return next((_plain(s.text).split("\n")[0] for s in scenes if s.text.strip()), "")


def _fit(title: str) -> str:
    return title if len(title) <= TITLE_MAX_CHARS else title[: TITLE_MAX_CHARS - 1] + "…"


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def build_title_candidates(project: Project) -> list[str]:
    """タイトル案（AIの案があれば先頭に、続けてテンプレートの案）。"""
    book = project.book_title.strip()
    if is_research(project):
        return _research_title_candidates(project)
    if is_english(project):
        lesson = project.lesson or {}
        day = lesson.get("day", "")
        theme = lesson.get("theme") or book or "英会話"
        phrase = next((p.get("en", "") for p in lesson.get("phrases", []) if isinstance(p, dict)), "")
        label = "1週間のまとめ" if str(day) == "7" else f"Day{day}"
        template = [
            f"【毎日英会話】{label} {phrase}｜{theme}" if phrase else "",
            f"【毎日英会話】{label}｜{theme}",
        ]
        return _dedupe([_fit(t.strip()) for t in project.title_candidates if t.strip()] + [_fit(t) for t in template])
    book_part = f"『{book}』" if book else "話題の本"
    worries = _section_bullets(project.scenes, "intro")
    worry = worries[0] if worries else ""
    ai = [_fit(t.strip()) for t in project.title_candidates if t.strip()]

    if is_short(project):
        hook = _first_line(project.scenes).rstrip("。")
        template = [
            f"{hook} #Shorts" if hook and len(hook) <= 30 else "",
            f"「{worry}」を今日から変える方法 #Shorts" if worry else "",
            f"{book_part}の一番大事なこと #Shorts",
        ]
        return _dedupe(ai + [_fit(t) for t in template])

    n_points = len(_explain_points(project.scenes))
    points_part = f"{n_points}つのポイント" if n_points else "要点"
    minutes = max(1, math.ceil(project.total_duration / 60))
    template = [
        f"【本要約】{worry}人へ｜{book_part}の{points_part}" if worry else "",
        f"「{worry}」を解決する{points_part}【本要約】{book_part}" if worry else "",
        f"【本要約】{book_part}を{minutes}分で解説【ずんだもん】",
        f"【{minutes}分で分かる】{book_part}の{points_part}｜ずんだもん解説",
    ]
    return _dedupe(ai + [_fit(t) for t in template])


def _research_title_candidates(project: Project) -> list[str]:
    topic = project.book_title.strip() or "話題のテーマ"
    ai = [_fit(t.strip()) for t in project.title_candidates if t.strip()]
    if is_short(project):
        hook = _first_line(project.scenes).rstrip("。")
        template = [
            f"{hook} #Shorts" if hook and len(hook) <= 30 else "",
            f"【研究で判明】{topic}の意外な真実 #Shorts",
        ]
        return _dedupe(ai + [_fit(t) for t in template])
    n_points = len(_explain_points(project.scenes))
    points_part = f"{n_points}つのポイント" if n_points else "要点"
    minutes = max(1, math.ceil(project.total_duration / 60))
    template = [
        f"【研究で判明】{topic}で失敗する理由と成功のコツ",
        f"【論文で解説】{topic}の{points_part}｜ずんだもん解説",
        f"【{minutes}分で分かる】{topic}を研究データで解説",
    ]
    return _dedupe(ai + [_fit(t) for t in template])


def build_hashtags(project: Project) -> list[str]:
    """説明文に入れるハッシュタグ。先頭3つがタイトルの上に表示されるので、大事な順に並べる。"""
    if is_english(project):
        default = ["英会話", "英語学習", project.book_title.strip()]
    elif is_research(project):
        default = ["ずんだもん解説", project.book_title.strip(), "研究"]
    else:
        default = ["本要約", project.book_title.strip(), "ずんだもん"]
    raw = list(project.hashtags) or default
    if is_short(project):
        raw = ["Shorts"] + raw
    tags = []
    for text in raw:
        tag = _HASHTAG_UNSAFE.sub("", text)
        if tag and len(tag) <= 20:
            tags.append(f"#{tag}")
    return _dedupe(tags)


def build_tags(project: Project) -> list[str]:
    tags = [project.book_title.strip(), project.book_author.strip()]
    tags += [t.lstrip("#") for t in project.hashtags]
    if is_english(project):
        tags += [p.split(" ― ")[0] for p in _lesson_phrases(project)] + ENGLISH_TAGS
    elif is_research(project):
        tags += RESEARCH_SHORT_TAGS if is_short(project) else RESEARCH_TAGS
    else:
        tags += SHORT_TAGS if is_short(project) else BASE_TAGS
    return _dedupe(tags)


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
            return credit
    return f"{title.replace('_', ' ')}（作者名を config/bgm_credits.json に登録してください）"


def _credits(project: Project) -> list[str]:
    speakers = _dedupe([s.speaker for s in project.scenes if s.text.strip()])
    bgm_paths = _dedupe([p for p in (project.resolve_bgm_path(s) for s in project.scenes) if p])
    lines = ["■ クレジット・使用素材"]
    if speakers:
        lines.append("・音声：" + "、".join(f"VOICEVOX:{get_character_display_name(s)}" for s in speakers))
    if any(s.voice_path for s in project.scenes):
        lines.append("・英語音声：（使用した音声サービス名をここに記載してください）")
    lines += [
        "・立ち絵：坂本アヒル 様",
        "・効果音：効果音ラボ（https://soundeffect-lab.info/sound/anime/）",
    ]
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
    """説明文の「参考文献・出典」（論文・記事を調べた動画用）。"""
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
        "毎日更新・1週間ごとにテーマが変わります。チャンネル登録して一緒に続けましょう！",
        "",
    ]
    lines += _credits(project) + ["", " ".join(build_hashtags(project)[:5])]
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
        lines += _credits(project) + ["", " ".join(hashtags[:4])]
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
    lines += _credits(project) + ["", " ".join(hashtags[:5])]
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
    return meta


def export_text(project: Project) -> str:
    """投稿時にコピーしやすいよう、タイトル・説明文・タグを1つのテキストにまとめる。"""
    return "\n".join([
        "【タイトル】", project.video_title, "",
        "【説明文】", refresh_credits(project.video_description, project), "",
        "【タグ】", ", ".join(project.video_tags),
    ])
