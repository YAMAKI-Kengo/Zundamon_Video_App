"""
作った動画の記録（量産型に見えないようにするため・数字を見て次の動画を決めるため）。

- 台本を読み込むたびに、その動画の「タイトル・導入のシチュエーション・ずんだもんの失敗・構成・締め」を
  history/videos.json に記録する（同じタイトルなら上書き）。
- 次の台本を作るとき、最近の動画の記録をAIに渡して「流れ・ボケ・導入・締めを被らせない」ようにする
  （YouTubeの「量産型のコンテンツ」＝テンプレートで作ったような似た動画、と判定されないため）。
- 投稿後に YouTube アナリティクスの数字（クリック率・平均視聴率・視聴回数）とコメントを書き込み、
  上位の動画から次に作るテーマ（派生・続編）を考える。
- 連続ドラマの軸（シリーズ）と、視聴維持率のグラフの振り返りから決めた台本のルールを config/channel.json に保存し、
  台本づくりのAIに渡す（1本の動画ではなく、キャラクターの続きを見に戻ってくる連続ドラマにする）。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Optional

from src.models import Project

ROOT = Path(__file__).resolve().parents[2]
HISTORY_PATH = ROOT / "history" / "videos.json"
KIND_LABELS = {"book": "📖 本の解説", "research": "🔎 論文・記事の解説", "english": "🗣 英会話"}
METRIC_KEYS = ("ctr", "avg_view", "views", "comments")
CHANNEL_PATH = ROOT / "config" / "channel.json"
GROUP_LABELS = {"explain": "🎓 解説（本・論文記事）", "english": "🗣 英会話"}
DEFAULT_CHANNEL = {
    "explain": {"arc": "ずんだもん、1年で100万円貯めるのだ計画", "rules": ""},
    "english": {"arc": "ずんだもん、3か月後に初めての海外旅行へ行く", "rules": ""},
}
METAN_MISTAKE_EVERY = 4  # 解説動画で、何本に1本「めたんも間違っていた回」にするか


def group_of(kind: str) -> str:
    return "english" if kind == "english" else "explain"


def load_channel() -> dict:
    """チャンネルの設定（連続ドラマの軸・振り返りから決めた台本のルール）。"""
    data = {k: dict(v) for k, v in DEFAULT_CHANNEL.items()}
    try:
        saved = json.loads(CHANNEL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return data
    for group, values in (saved.items() if isinstance(saved, dict) else []):
        if group in data and isinstance(values, dict):
            data[group].update({k: str(v) for k, v in values.items() if k in ("arc", "rules")})
    return data


def save_channel(data: dict) -> None:
    CHANNEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHANNEL_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _kind(project: Project) -> str:
    return project.source_kind if project.source_kind in KIND_LABELS else "book"


def load() -> list[dict]:
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [v for v in data if isinstance(v, dict)] if isinstance(data, list) else []


def save(entries: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _lines(project: Project, section: str, speaker: Optional[str] = None, limit: int = 2) -> list[str]:
    return [s.text.strip() for s in project.scenes
            if s.section == section and s.text.strip() and (speaker is None or s.speaker == speaker)][:limit]


def summarize(project: Project) -> dict:
    """1本の動画の、被りを避けたい要素（導入・ずんだもんの失敗・構成・締め）をまとめる。"""
    boards = []
    for scene in project.scenes:
        if scene.section == "explain" and scene.slide_title.strip() and scene.slide_title not in boards:
            boards.append(scene.slide_title.strip())
    summary_lines = [s.text.strip() for s in project.scenes if s.section == "summary" and s.text.strip()]
    lesson = project.lesson or {}
    return {
        "hook": " / ".join(_lines(project, "intro", "zundamon", 2)),
        "structure": " → ".join(boards[:6]),
        "ending": summary_lines[-1] if summary_lines else "",
        "scene": lesson.get("theme", "") if _kind(project) == "english" else "",
    }


def record(project: Project) -> None:
    """読み込んだ台本の動画を記録する（本編のみ。同じタイトルの記録があれば内容を更新し、数字は残す）。"""
    if project.promo_of or not project.scenes:
        return
    title = (project.video_title or project.book_title).strip()
    if not title:
        return
    entries = load()
    entry = next((e for e in entries if e.get("title") == title), None)
    if entry is None:
        entry = {"title": title, "date": _dt.date.today().isoformat(), **{k: None for k in METRIC_KEYS}, "note": ""}
        entries.append(entry)
    entry.update({"kind": _kind(project), "theme": project.book_title, "style": project.video_style,
                  "minutes": round(project.total_duration / 60, 1), **summarize(project)})
    save(entries)


def _series_text(kind: str, same: list[dict]) -> str:
    """連続ドラマの軸（シリーズ）と、前回の続き・今回の話数をAIに伝える文章。"""
    channel = load_channel()[group_of(kind)]
    arc = channel.get("arc", "").strip()
    rules = channel.get("rules", "").strip()
    episode = len([e for e in same if e.get("style") != "short"]) + 1
    parts = []
    if arc:
        if kind == "english":
            story = (f"週テーマは、この旅の進行に沿って増えていく（例: 出発前の準備 → 空港 → 機内 → 入国審査 → ホテル → "
                     f"レストラン → 買い物 → 観光 → トラブル → 現地の人と仲良くなる）。推し活や日常の場面は、旅の中のエピソードとして入れてよい。"
                     f"シリーズの最終回では、本当に旅行に行く回を作る。")
        else:
            story = (f"テーマがこの目標から遠い回も、ずんだもんの目標とつなげて扱う（例: 睡眠の本なら「寝不足で衝動買いが増えて計画がピンチ」）。")
        parts.append(
            f"## 連続ドラマの軸（シリーズ）\n"
            f"- 視聴者は情報より、キャラクターの続きを見に戻ってくる。1本ずつの動画ではなく連続ドラマにする。裏の軸は「{arc}」。"
            f"毎回の動画は、この目標に向けたずんだもんの1歩として作る（今回は第{episode}話）。{story}\n"
            f"- ずんだもんは前回までのことを覚えていて、すぐ極端に走るけれど、シリーズを通して少しずつ成長している。"
        )
        previous = next((e for e in reversed(same) if e.get("style") != "short"), None)
        if previous:
            if kind == "english":
                parts.append(
                    f"- 前回は「{previous.get('title', '')}」で、最後は「{previous.get('ending', '')}」だった。"
                    "導入の約束と予告のあとで、前回の続きに一言触れる（毎日見ている人のための、いつものつながり）。"
                )
            else:
                parts.append(
                    f"- 前回は「{previous.get('title', '')}」で、最後は「{previous.get('ending', '')}」だった。"
                    "前回の続きに触れるのは、まとめ（summary）の中で一言だけにする"
                    "（例: めたん「前回の自動積立、続いてる？」ずんだもん「実は3日でやめたのだ…でも今日の方法なら続けられそうなのだ！」）。"
                    "導入（intro）には、過去の動画やシリーズ・計画の話を入れない。検索や関連動画から初めて来た人は"
                    "「自分の悩みを早く解決してほしい」と思っているので、内輪の話から始まると30秒以内に離れてしまう。"
                    "シリーズの軸は裏の設定として使い、1本で完結する動画にする。"
                )
        if kind != "english" and episode % METAN_MISTAKE_EVERY == 0:
            parts.append(
                "- 今回は「めたんも間違っていた回」にする: 解説の途中で、いつも正しいめたんも同じ思い込みをしていて、"
                "研究・本の結果に「えっ、わたくしも間違ってたわ…」となる場面を1回入れる"
                "（頭のいい人でも間違えると知って視聴者が安心し、キャラクターにも深みが出る）。"
            )
    if rules:
        parts.append("## 視聴維持率の振り返りから決めた台本のルール（最優先で守る）\n" + rules)
    return "\n\n" + "\n".join(parts) if parts else ""


def recent_digest(kind: str, limit: int = 10) -> str:
    """連続ドラマの軸・振り返りのルール・最近の動画の記録を、台本づくりのAIに渡す文章にする（無ければ空文字）。"""
    same = [e for e in load() if group_of(e.get("kind", "book")) == group_of(kind)]
    series = _series_text(kind, same)
    same = same[-limit:]
    if not same:
        return series
    rows = []
    for e in same:
        parts = [f"「{e.get('title', '')}」"]
        if e.get("hook"):
            parts.append(f"導入: {e['hook']}")
        if e.get("structure"):
            parts.append(f"構成: {e['structure']}")
        if e.get("ending"):
            parts.append(f"締め: {e['ending']}")
        rows.append("- " + " ／ ".join(parts))
    return series + (
        "\n\n## 最近作った動画（量産型に見えないよう、これらと被らせない）\n"
        "YouTubeは、テンプレートで作ったような似た動画を「量産型のコンテンツ」として収益化の対象外にする。"
        "次の最近の動画と、導入のシチュエーション・ずんだもんの失敗や勘違いの種類・ボケ・たとえ話・構成の型・締めのセリフが"
        "同じにならないようにし、この動画だけの小さなストーリーにする。\n" + "\n".join(rows)
    )


def ranked(kind: Optional[str] = None) -> list[dict]:
    """数字が入っている動画を、クリック率 × 平均視聴率 の高い順に並べる。"""
    scored = []
    for e in load():
        if kind and e.get("kind") != kind:
            continue
        try:
            score = float(e.get("ctr") or 0) * float(e.get("avg_view") or 0)
        except (TypeError, ValueError):
            continue
        if score > 0:
            scored.append((score, e))
    return [e for _, e in sorted(scored, key=lambda x: -x[0])]


def next_ideas_prompt(kind: str) -> str:
    """数字の良かった動画から、次に作る動画（派生・続編）を考えてもらうプロンプト。"""
    rows = []
    for e in [e for e in load() if e.get("kind") == kind]:
        metrics = "、".join(f"{label}{e.get(key)}{unit}" for key, label, unit in (
            ("ctr", "クリック率", "%"), ("avg_view", "平均視聴率", "%"), ("views", "視聴回数", "回"))
            if e.get(key) not in (None, ""))
        rows.append(f"- 「{e.get('title', '')}」（{e.get('date', '')}・{e.get('minutes') or '?'}分）{metrics or '数字なし'}"
                    + (f"　離脱が大きかった場所: {e['drop']}" if e.get("drop") else "")
                    + (f"　コメント・気づき: {e['comments']}" if e.get("comments") else "")
                    + (f"　メモ: {e['note']}" if e.get("note") else ""))
    focus = ("どの場面（旅行・推し活・仕事・日常など）のフレーズがよく見られているかを見て、来週以降の週テーマの比重を決める。"
             if kind == "english" else
             "お金寄り・行動寄り・人間関係（心理）寄りのどれが伸びているかを見て、次の方向を決める（最初から決め打ちしない）。")
    return f"""あなたはYouTubeチャンネルの分析担当です。ずんだもんと四国めたんの{KIND_LABELS.get(kind, '')}チャンネルの、これまでの動画の数字（YouTubeアナリティクス）とコメントを渡します。
最初の20本くらいは「当てる」のではなく「何が当たるかを調べる」期間と考えています。

## やってほしいこと
1. クリック率（タイトルとサムネ）と平均視聴率（中身）を分けて見て、上位2本が伸びた理由と、下位の動画がつまずいた理由を短く分析する。
2. 上位2本の派生・続編になる、次に作る動画の案を5つ出す。各案に、タイトル案（【ずんだもん解説】や【毎日英会話】のシリーズ名付き）、サムネの一言、なぜ伸びそうか、を書く。
3. コメントに出ている質問・要望（「〇〇の場合はどうなの？」など）から、次のテーマにできるものを挙げる。
4. {focus}
5. 動画の長さ（分）と平均視聴率・視聴回数の関係から、今のチャンネルに合う長さを提案する（例: 12分版と20分版の比較）。

## これまでの動画
{chr(10).join(rows) or '（まだ記録がありません）'}"""


def rules_prompt(kind: str) -> str:
    """視聴維持率のグラフで離脱が大きかった場所のメモから、台本のルールを考えてもらうプロンプト。"""
    rows = [f"- 「{e.get('title', '')}」（{e.get('minutes') or '?'}分・平均視聴率 {e.get('avg_view') or '?'}%）: {e['drop']}"
            for e in load() if group_of(e.get("kind", "book")) == group_of(kind) and e.get("drop")]
    current = load_channel()[group_of(kind)].get("rules", "").strip()
    return f"""あなたはYouTubeの台本作家です。ずんだもんと四国めたんの{GROUP_LABELS[group_of(kind)]}チャンネルの動画について、
YouTubeアナリティクスの視聴維持率のグラフで「大きく人が離れた場所」をメモしました。原因を考えて、次の台本で守るルールに直してください。

## 離脱が大きかった場所のメモ
{chr(10).join(rows) or '（まだメモがありません）'}

## 今のルール
{current or '（まだありません）'}

## 出力
次の台本から守るルールを、今のルールと合わせて箇条書き（「- 」で始まる行）で5〜10個にまとめてください。
「導入の寸劇は20秒以内」「黒板の説明は1枚30秒以内で、途中にずんだもんのリアクションを挟む」のように、台本を書く人がそのまま守れる具体的な形にします。"""
