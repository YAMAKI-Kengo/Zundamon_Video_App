"""
台本の読み間違いの自動チェック（VOICEVOXの実際の読み → Claudeが読み間違いを探す → 読み方辞書に追加）。

サイドバーのボタンと、英会話モードの台本生成のあとの自動チェックから使う。
日本語のセリフは、VOICEVOXに実際に読ませる文（Scene.reading があればそれ。英語まじりの日本語のセリフでは、
英語の部分をカタカナにした文）でチェックする。英語のセリフ（ネイティブ音声・ずんだもんのカタカナ英語）と、
リピート・回答の間（音声なし）はチェックしない。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.models import Project
from src.services import book_ai, book_script, voicevox_client


@dataclass
class ReadingCheckResult:
    added: list[dict] = field(default_factory=list)  # 読み方辞書に新しく追加した {"word", "reading"}
    cost_usd: float = 0.0
    checked_lines: int = 0


def spoken_lines(project: Project) -> list[tuple[str, str]]:
    """チェックする (読ませる文, 話者) の一覧（同じ文は1回だけ）。"""
    lines: dict[str, str] = {}
    for scene in project.scenes:
        if scene.silent or scene.card_text or scene.voice_path or scene.lang == "en":
            continue
        text = (scene.reading.strip() or scene.text).strip()
        if text and text not in lines:
            lines[text] = scene.speaker
    return list(lines.items())


def run_reading_check(project: Project) -> ReadingCheckResult:
    """読み間違いを探して、見つかった読み方を project.reading_dict に追加する。

    Raises:
        voicevox_client.VoicevoxConnectionError / VoicevoxSynthesisError: VOICEVOXが使えない場合
        book_ai.BookAIError: Claude APIが使えない場合
    """
    targets = spoken_lines(project)
    if not targets:
        return ReadingCheckResult()
    lines = [(text, voicevox_client.get_kana(text, speaker, project.reading_dict)) for text, speaker in targets]
    result = book_ai.check_readings(lines)
    corrections = book_script._parse_readings(result.data.get("corrections"))
    before = {(r.get("word"), r.get("reading")) for r in project.reading_dict}
    project.reading_dict = book_script.merge_readings(project.reading_dict, corrections)
    added = [r for r in project.reading_dict if (r.get("word"), r.get("reading")) not in before]
    return ReadingCheckResult(added=added, cost_usd=result.cost_usd, checked_lines=len(lines))
