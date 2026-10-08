"""
「📈 振り返り」タブ: 投稿した動画の数字を書き込み、次に何を作るかを決める。

最初の20本くらいは「当てる」のではなく「何が当たるかを調べる」期間。10本ごとに YouTube アナリティクスの
クリック率（タイトル・サムネ）と平均視聴率（中身）を並べ、上位2本の派生・続編を作る。
伸びた動画のコメントは次のテーマの宝庫なので、気になったコメントもメモしておく。
"""
from __future__ import annotations

import streamlit as st

from src.ui.prompt_box import prompt_box
from src.services import book_ai, video_history
from src.services.book_ai import _STR, _obj

_IDEAS_SCHEMA = _obj({
    "analysis": _STR,
    "ideas": {"type": "array", "items": _obj({"title": _STR, "thumbnail": _STR, "why": _STR})},
    "from_comments": {"type": "array", "items": _STR},
})
_KIND_TABS = {"explain": "🎓 解説（本・論文記事）", "english": "🗣 英会話"}


def _metric(value) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def render_analytics() -> None:
    st.subheader("📈 振り返り（数字を見て、次に何を作るか決める）")
    st.caption(
        "台本を読み込んだ動画は自動でここに記録されます（次の台本では、最近の動画と導入・ボケ・締めが被らないようにAIに伝えます）。"
        "投稿して数日たったら、YouTube アナリティクスの「インプレッションのクリック率」と「平均再生率」を書き込んでください。"
        "最初の20本は「何が当たるかを調べる」期間と考え、10本ごとに上位2本の派生・続編を作るのがおすすめです。"
    )
    entries = video_history.load()
    if not entries:
        st.info("まだ記録がありません。台本を読み込むと、ここに動画が追加されます。")
        return

    group = st.segmented_control("チャンネル", list(_KIND_TABS), format_func=_KIND_TABS.get, default="explain",
                                 key="analytics_group", required=True)
    kinds = {"english"} if group == "english" else {"book", "research"}
    indexes = [i for i, e in enumerate(entries) if e.get("kind", "book") in kinds]
    rows = [{
        "公開日": entries[i].get("date", ""),
        "タイトル": entries[i].get("title", ""),
        "クリック率(%)": _metric(entries[i].get("ctr")),
        "平均再生率(%)": _metric(entries[i].get("avg_view")),
        "視聴回数": _metric(entries[i].get("views")),
        "長さ(分)": _metric(entries[i].get("minutes")),
        "離脱が大きかった場所": entries[i].get("drop") or "",
        "気になったコメント": entries[i].get("comments") or "",
        "メモ": entries[i].get("note") or "",
    } for i in indexes]
    edited = st.data_editor(
        rows, key=f"analytics_table_{group}", width="stretch", hide_index=True,
        column_config={
            "公開日": st.column_config.TextColumn(width="small"),
            "タイトル": st.column_config.TextColumn(width="large"),
            "クリック率(%)": st.column_config.NumberColumn(min_value=0.0, max_value=100.0, step=0.1, format="%.1f"),
            "平均再生率(%)": st.column_config.NumberColumn(min_value=0.0, max_value=100.0, step=0.1, format="%.1f"),
            "視聴回数": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
            "長さ(分)": st.column_config.NumberColumn(disabled=True, format="%.1f"),
            "離脱が大きかった場所": st.column_config.TextColumn(
                help="視聴維持率のグラフで大きく落ちている場所（例: 導入の寸劇、2枚目の黒板の説明）"),
        },
    )
    if st.button("💾 数字を保存", key="analytics_save", type="primary"):
        for i, row in zip(indexes, edited):
            entries[i].update({
                "date": row.get("公開日") or entries[i].get("date", ""),
                "title": row.get("タイトル") or entries[i].get("title", ""),
                "ctr": row.get("クリック率(%)"), "avg_view": row.get("平均再生率(%)"), "views": row.get("視聴回数"),
                "comments": row.get("気になったコメント") or "", "note": row.get("メモ") or "",
                "drop": row.get("離脱が大きかった場所") or "",
            })
        video_history.save(entries)
        st.success("保存しました。")

    kind = "english" if group == "english" else "book"
    top = [e for e in video_history.ranked() if e.get("kind", "book") in kinds][:2]
    filled = sum(1 for i in indexes if _metric(entries[i].get("ctr")) and _metric(entries[i].get("avg_view")))
    st.markdown(f"**数字が入っている動画: {filled}本**" + ("（10本たまったら、次の方向を決めましょう）" if filled < 10 else ""))
    if top:
        st.markdown("**🏆 上位2本（クリック率 × 平均再生率）**\n" + "\n".join(
            f"{n}. {e.get('title', '')}（クリック率 {e.get('ctr')}%・平均再生率 {e.get('avg_view')}%）"
            for n, e in enumerate(top, start=1)))

    st.markdown("**💡 次に作る動画を考える**")
    prompt = video_history.next_ideas_prompt(kind)
    if book_ai.api_key_configured():
        if st.button("🤖 数字とコメントから、次の動画の案を出す（Claude）", key="analytics_ideas", disabled=not filled):
            with st.status("分析しています…", expanded=True) as status:
                try:
                    result = book_ai._call("あなたはYouTubeチャンネルの分析担当です。日本語で答えます。",
                                           [{"type": "text", "text": prompt}], _IDEAS_SCHEMA, 8000, status.write)
                except book_ai.BookAIError as e:
                    status.update(label="分析できませんでした", state="error")
                    st.error(str(e))
                    return
                status.update(label="分析しました", state="complete")
            st.session_state[f"analytics_result_{group}"] = result.data
    data = st.session_state.get(f"analytics_result_{group}")
    if data:
        st.markdown(data.get("analysis", ""))
        for idea in data.get("ideas", []):
            st.markdown(f"- **{idea.get('title', '')}**　サムネ「{idea.get('thumbnail', '')}」　— {idea.get('why', '')}")
        if data.get("from_comments"):
            st.markdown("**コメントから作れるテーマ**\n" + "\n".join(f"- {c}" for c in data["from_comments"]))
    with st.expander("APIを使わない場合（Claudeのチャット画面に貼るプロンプト）"):
        prompt_box(prompt, "次の動画を考えるプロンプト")

    _render_channel_settings(group, kind)


def _render_channel_settings(group: str, kind: str) -> None:
    """連続ドラマの軸と、視聴維持率の振り返りから決めた台本のルール（次の台本から反映）。"""
    st.divider()
    st.markdown("**📺 連続ドラマの軸と、振り返りから決めた台本のルール**（次に作る台本から反映されます）")
    channel = video_history.load_channel()
    arc = st.text_input(
        "シリーズの軸（ずんだもんの目標。毎回の動画はそのための1歩になり、前回の続きから始まります）",
        value=channel[group].get("arc", ""), key=f"analytics_arc_{group}",
    )
    st.caption(
        "5本ごとに、視聴維持率のグラフで大きく落ちている場所を上の表の「離脱が大きかった場所」にメモし、"
        "その原因を台本のルールとして下に書きます。次の5本で本当に改善したかを確かめます。"
    )
    rules = st.text_area("振り返りから決めた台本のルール（1行に1つ）", value=channel[group].get("rules", ""),
                         key=f"analytics_rules_{group}", height=140,
                         placeholder="- 導入の寸劇は20秒以内にする\n- 黒板の説明は1枚30秒以内で、途中にずんだもんのリアクションを挟む")
    if st.button("💾 シリーズの軸とルールを保存", key=f"analytics_channel_save_{group}"):
        channel[group] = {"arc": arc.strip(), "rules": rules.strip()}
        video_history.save_channel(channel)
        st.success("保存しました。次の台本から反映されます。")
    with st.expander("離脱のメモから、ルールを考えてもらう（Claudeのチャット画面に貼るプロンプト）"):
        prompt_box(video_history.rules_prompt(kind), "ルールを考えるプロンプト")
