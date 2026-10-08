"""
本編紹介ショート（本編の要点をまとめて「詳しくは本編で」と締める縦型ショート）の台本を、あとから作る。

ふだんは本編の台本と一緒にAIが書く（台本JSONの "promo_short"。book_ai.promo_short_rules）。
それが入っていない台本（古い台本・手動で作った台本など）のために、本編のセリフの一覧から
ショートの台本だけを作る（Claude API / チャット画面に貼るプロンプト）。
"""
from __future__ import annotations

import json
from typing import Optional

from src.models import Project
from src.services import book_ai, english_lesson
from src.services.book_ai import AIResult, ProgressCallback
from src.services.book_script import BookScriptError, promo_short_data
from src.utils.asset_loader import get_character_display_name

PROMO_MAX_TOKENS = 16000


def _role(project: Project) -> str:
    return "english" if project.source_kind == "english" else "book"


def script_digest(project: Project) -> str:
    """本編の台本の内容（黒板の見出し・箇条書きとセリフ）を、AIに渡す文章にまとめる。"""
    lines = [f"動画のタイトル: {project.video_title or project.book_title}"]
    if project.book_title:
        lines.append(f"題材: {project.book_title}" + (f"（{project.book_author}）" if project.book_author else ""))
    phrases = [f"{p.get('en', '')}（{p.get('ja', '')}）" for p in (project.lesson or {}).get("phrases", [])
               if isinstance(p, dict)]
    if phrases:
        lines.append("今日のフレーズ: " + " / ".join(phrases))
    last_board = None
    for scene in project.scenes:
        if scene.section == "ending" or scene.card_text:
            continue
        board = (scene.slide_title, tuple(b for b in scene.slide_bullets if b.strip()))
        if scene.has_slide and board != last_board:
            lines.append(f"【黒板】{scene.slide_title}：" + " / ".join(board[1]))
            last_board = board
        if scene.text.strip():
            ja = f"（{scene.translation}）" if scene.lang == "en" and scene.translation else ""
            lines.append(f"{get_character_display_name(scene.speaker)}: {scene.text.strip()}{ja}")
    return "\n".join(lines)


def _system(role: str, speech_speed: float) -> str:
    return f"""あなたはYouTubeショートで何度も大きく再生されている構成作家です。これから渡す本編の動画の台本（黒板とセリフの一覧）をもとに、ずんだもんと四国めたんの掛け合いで、本編の要点をまとめて本編の視聴を促す縦型ショート動画の台本だけを書きます。

{book_ai._CHARACTERS_TEXT}

{book_ai.CHANNEL_CORE}

{book_ai.ENGLISH_POLICY if role == "english" else book_ai.EXPLAINER_POLICY}
{book_ai.promo_short_rules(role, speech_speed)}

## 表情・効果音・使わない項目
- expression は、そのキャラクターの表情の一覧から、セリフの感情に合うものを選ぶ:
{book_ai._character_guide()}
- se（効果音）は次の名前のみ（使わないときは空文字）: {book_ai._se_guide()}
- 使わない項目は、空文字・空の配列・false・0 にする。promo_short 以外のことは書かない。"""


def _schema(role: str) -> dict:
    full = english_lesson._lesson_schema() if role == "english" else book_ai._script_schema(with_promo=True)
    return book_ai._obj({"promo_short": full["properties"]["promo_short"]})


def _instruction(project: Project) -> str:
    return "次の本編の台本をもとに、本編紹介ショートの台本（promo_short）を書いてください。\n\n<main_script>\n" \
        + script_digest(project) + "\n</main_script>"


def generate(project: Project, progress: Optional[ProgressCallback] = None) -> AIResult:
    """Claude API で、本編のプロジェクトから本編紹介ショートの台本を作り、プロジェクトに入れる。"""
    role = _role(project)
    result = book_ai._call(_system(role, project.speech_speed), [{"type": "text", "text": _instruction(project)}],
                           _schema(role), PROMO_MAX_TOKENS, progress or (lambda _m: None))
    attach(project, result.data)
    return result


def manual_prompt(project: Project) -> str:
    """APIを使わずに、Claudeのチャット画面で本編紹介ショートの台本を作ってもらうためのプロンプト。"""
    return "\n\n".join([
        _system(_role(project), project.speech_speed),
        _instruction(project),
        '出力は {"promo_short": {"title_candidates": [...], "video_title": "...", "hook": "...", "description_lead": "...", '
        '"hashtags": [...], "tags": [...], "blocks": [{"section": "intro", "slide": {"title": "", "bullets": []}, '
        '"lines": [{"speaker": "zundamon", "expression": "surprised", "text": "...", "hide": ["shikoku_metan"], '
        '"board": false}]}, ...]}} の形式のJSONだけにしてください。',
    ])


def attach(project: Project, data: dict) -> None:
    """AIの出力（{"promo_short": {...}} または promo_short の中身だけ）を、本編のプロジェクトに入れる。"""
    if not isinstance(data, dict):
        raise BookScriptError("本編紹介ショートの台本の形式が正しくありません。")
    wrapped = data if "promo_short" in data else {"promo_short": data}
    book_ai.tidy_script_lines(wrapped, project.source_kind)
    promo = promo_short_data(wrapped)
    if not promo:
        raise BookScriptError('本編紹介ショートの台本（"promo_short" の "blocks"）が見つかりませんでした。')
    if project.source_kind == "english":
        lesson = project.lesson or {}
        english_lesson.assign_audio_ids(promo, int(lesson.get("week") or 1), int(lesson.get("day") or 1))
    project.promo_short = json.loads(json.dumps(promo, ensure_ascii=False))
