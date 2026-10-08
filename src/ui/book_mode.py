"""
「📚 書籍解説モード」タブ。

書籍解説の掛け合い動画を作る流れを1画面にまとめる:
  1. 台本を用意する
     - 本のテキスト(txt/PDF)を Claude で分析（要点抽出）→ 台本を自動生成（src.services.book_ai）。
     - 本の代わりに、論文・ネット記事を Claude にWebで調べさせて題材にすることもできる（出典付き）
       APIを使わず、Claudeのチャット画面に貼るプロンプトを表示する手動モードもある
     - 台本JSONを貼り付け / ファイルから読み込み（src.services.book_script）
  2. 構成の確認: 台本のブロック（場面 + 黒板スライド）ごとに、実際の画面プレビュー・セリフ・秒数・BGMを一覧表示
  3. 書き出しは「🎬 シーン編集・書き出し」タブで行う（細かい修正もそちらで1シーンずつできる）
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from src.models import MOOD_LABELS, Project

from src.services import (
    book_ai, book_loader, book_script, motion, promo_short, slide_renderer, thumbnail, video_history, video_metadata,
)
from src.ui.preview_cache import scene_preview
from src.state import forget_scene_widgets, get_project, go_to_tab, set_project
from src.utils.asset_loader import get_available_expressions, get_character_display_name, get_expression_label

_BOOK_IMPORT_FLASH_KEY = "_book_import_flash"
_PROMO_PAIR_KEY = "_promo_pair"
_PROMO_FLASH_KEY = "_promo_flash"
USD_TO_JPY = 150  # 費用の目安表示用のレート
BOOK_COVER_DIR = Path(__file__).resolve().parents[2] / "assets" / "books"
STYLE_LABELS = {"normal": "通常（横画面・数分）", "short": "ショート（縦画面・約1分）"}
SOURCE_LABELS = {"book": "📖 本（txt / PDF）", "research": "🔎 論文・ネット記事（Claudeに調べてもらう）"}
OVERVIEW_PREVIEW_MAX_DIM = 400  # 構成一覧のプレビュー画像の最大辺（px）

_SECTION_ICONS = {"intro": "😣", "explain": "💡", "summary": "📝", "ending": "🙏", "": "🎬"}


def _fill_book_script_sample() -> None:
    # ウィジェットのkeyに対応するsession_stateは、そのウィジェットの描画前（=コールバック内）でしか書き換えられない
    st.session_state["book_script_text"] = book_script.sample_script_text()


def _import_script(project: Project, script_text: str, use_voicevox_timing: bool, replace_existing: bool,
                   add_ending: bool, reveal_bullets: bool) -> bool:
    """台本JSONからシーンを作ってプロジェクトに反映し、結果のメッセージを次の再描画で表示できるよう保存する。"""
    project = _script_target(project)
    try:
        with st.spinner("台本を読み込んでいます…"):
            data = book_script.load_book_script(script_text)
            result = book_script.build_scenes(
                data, use_voicevox_timing=use_voicevox_timing, reading_dict=project.reading_dict,
                add_ending=add_ending, reveal_bullets=reveal_bullets, speech_speed=project.speech_speed,
                video_format=project.format, board_pause=project.board_pause,
            )
    except book_script.BookScriptError as e:
        st.error(str(e))
        return False
    book_script.apply_to_project(project, result, replace=replace_existing)
    book_script.sync_background_to_format(project)
    video_history.record(project)  # 次の台本で「最近の動画と被らせない」ため・振り返りで数字を書き込むため
    pair = st.session_state.get(_PROMO_PAIR_KEY)
    if pair and pair["main"] is project:
        st.session_state.pop(_PROMO_PAIR_KEY, None)  # 台本が変わったので、前のショートは使わない
    for key in ("meta_title", "meta_description", "meta_tags", "meta_title_candidate", "meta_x_post", "reading_dict_editor"):
        st.session_state.pop(key, None)
    for key in [k for k in st.session_state.keys() if str(k).startswith("thumb_") and not str(k).startswith("thumb_dl_")]:
        del st.session_state[key]
    timing = "VOICEVOXの読み上げ時間に合わせました" if result.used_voicevox_timing else "文字数からの概算です"
    st.session_state[_BOOK_IMPORT_FLASH_KEY] = {
        "success": (
            f"{len(result.scenes)}件のシーンを生成しました（合計 約{sum(s.duration for s in result.scenes):.0f}秒・"
            f"表示秒数は{timing}）。下の「構成の確認」で内容を確認してください。"
            + ("（ショート用の台本なので、出力フォーマットを縦画面に切り替えました）" if result.style == "short" else "")
            + ("本編紹介ショートの台本も読み込みました（構成の確認の最後から作れます）。" if result.promo_short else "")
        ),
        "warnings": result.warnings,
    }
    return True


def _script_target(project: Project) -> Project:
    """台本の取り込み先。紹介ショートを表示中でも、ショートを上書きせず本編の側に取り込む
    （どの動画を表示しているかと、どの動画を作るかを切り離す）。"""
    pair = _promo_pair()
    if pair and project is pair["short"]:
        project = pair["main"]
    elif project.promo_of:  # 本編と組になっていない紹介ショート（プロジェクトファイルから開いたもの等）も上書きしない
        fresh = Project()
        for attr in book_script._PROMO_INHERITED:
            setattr(fresh, attr, json.loads(json.dumps(getattr(project, attr))))
        project = fresh
    if project is not get_project():
        set_project(project)
    return project


def _format_cost(result: book_ai.AIResult) -> str:
    yen = result.cost_usd * USD_TO_JPY
    cache = f"・キャッシュ読込 {result.cache_read_tokens:,}" if result.cache_read_tokens else ""
    searches = f"・Web検索 {result.searches}回" if result.searches else ""
    return (
        f"{result.model}：入力 {result.input_tokens + result.cache_write_tokens:,}{cache}・"
        f"出力 {result.output_tokens:,} トークン{searches} → 約${result.cost_usd:.2f}（約{yen:,.0f}円）"
    )


def _load_uploaded_book(uploaded, pasted: str, title_hint: str):
    """アップロード/貼り付けされた本を読み込む（同じファイルは読み直さないよう session_state にキャッシュ）。"""
    if uploaded is not None:
        identity = (uploaded.name, uploaded.size)
        cached = st.session_state.get("_ai_book_cache")
        if cached and cached[0] == identity:
            return cached[1]
        book = book_loader.load_book(uploaded.name, uploaded.getvalue())
        st.session_state["_ai_book_cache"] = (identity, book)
        return book
    if pasted.strip():
        return book_loader.from_pasted_text(pasted, title_hint)
    return None


def _render_ai_generation(project: Project) -> None:
    has_ai_state = "ai_analysis_text" in st.session_state
    with st.expander("🤖 台本を自動生成（Claude）", expanded=not project.scenes or has_ai_state):
        mode = st.radio(
            "作り方", ["Claude APIで自動生成", "手動（Claudeのチャット画面を使う・API料金なし）"],
            horizontal=True, key="ai_mode",
        )
        style_label = st.radio(
            "動画の種類", list(STYLE_LABELS.values()), horizontal=True, key="ai_style",
            help="ショートは縦画面・約1分で、本の一番意外なポイント1つだけを速いテンポで紹介します（エンディングは付きません）。",
        )
        style = next(k for k, v in STYLE_LABELS.items() if v == style_label)
        source_label = st.radio(
            "題材", list(SOURCE_LABELS.values()), horizontal=True, key="ai_source",
            help="論文・ネット記事は、ClaudeがWeb検索で論文・公的機関の資料・信頼できる記事を調べ、"
                 "その内容を出典付きで解説する台本にします。",
        )
        source_kind = next(k for k, v in SOURCE_LABELS.items() if v == source_label)
        focus, urls = "", []
        if source_kind == "research":
            title_hint = st.text_input(
                "調べるテーマ", key="ai_topic", placeholder="例: 勉強した内容を忘れない方法",
                help="動画のテーマ名にもなります。",
            )
            author_hint = ""
            focus = st.text_area(
                "特に知りたいこと・観点（任意）", key="ai_focus", height=68,
                placeholder="例: 復習のタイミングについての研究。社会人でもできる方法",
            )
            urls = st.text_area(
                "必ず読んでほしい論文・記事のURL（任意・1行に1つ）", key="ai_urls", height=68,
                placeholder="https://...",
            ).splitlines()
        else:
            col_title, col_author = st.columns(2)
            title_hint = col_title.text_input("本のタイトル（任意）", key="ai_title_hint")
            author_hint = col_author.text_input("著者（任意）", key="ai_author_hint")
        worry_hint = st.text_input(
            "ずんだもんの悩み（任意）", key="ai_worry_hint",
            placeholder="例: 夜なかなか寝つけなくて、朝もすっきり起きられない",
            help="空欄なら、本の内容から自動で決めます。",
        )
        if style == "short":
            target_minutes, structure_hint = book_ai.SHORT_TARGET_SECONDS / 60, ""
            st.caption(f"ショート: 縦画面・約{book_ai.SHORT_TARGET_SECONDS}秒・ポイント1つ（冒頭1〜2秒のつかみ → 答え → オチ → ループ）")
        else:
            st.caption(
                "構成: 導入（ずんだもんが張り切って始める → 失敗・勘違い → めたん登場「そんなんじゃだめよ」）→ 解説 → まとめ。"
                "解説の組み立て（失敗と成功・思い込みと真実・ランキング・Q&A・ステップ・ストーリーなど）とポイントの数は、"
                "本の内容と動画の長さに合わせてAIが選びます。"
            )
            col_len, col_hint = st.columns([1, 2])
            target_minutes = col_len.select_slider(
                "動画の長さの目安（エンディング除く）", options=[3, 5, 8, 10, 12, 15, 20], value=8,
                format_func=lambda m: f"約{m}分", key="ai_target_minutes",
            )
            structure_hint = col_hint.text_input(
                "構成の希望（任意）", key="ai_structure_hint",
                placeholder="例: ランキング形式で / Q&A形式で / 失敗の理由と成功のコツに分けて（空欄ならおまかせ）",
            )

        if mode.startswith("手動"):
            st.caption(
                "① Claude（claude.ai）のチャットに本のファイル（PDF・テキスト）を添付し、下のプロンプトを貼り付けて送信します。"
                "② 返ってきたJSONを、下の「📋 台本JSONを読み込む」に貼り付けて「台本からシーンを一括生成」を押します。"
            )
            st.code(
                book_ai.manual_script_prompt(
                    style, target_minutes, title_hint, author_hint, worry_hint, project.speech_speed,
                    source_kind=source_kind, focus=focus, urls=urls, structure_hint=structure_hint,
                ),
                language="markdown",
            )
            return

        model = book_ai.current_model()
        if not book_ai.api_key_configured():
            st.warning(
                "Claude APIのキーが設定されていません。プロジェクト直下の `.env` ファイルの "
                "`# === Anthropic ===` の下にある `ANTHROPIC_API_KEY=` にキーを書いて保存し、アプリを再起動してください。"
                "（APIキーは https://platform.claude.com/ で発行できます）"
            )
        else:
            st.caption(f"使用モデル: `{model}`（`.env` の ANTHROPIC_MODEL で変更できます）")

        if source_kind == "research":
            _render_research_button(title_hint, focus, urls, worry_hint)
        else:
            _render_book_analysis(model, title_hint, author_hint, worry_hint)
        _render_script_generation(project, style, target_minutes, structure_hint, worry_hint)


def _render_research_button(topic: str, focus: str, urls: list[str], worry_hint: str) -> None:
    """① 論文・ネット記事をClaudeに調べさせ、分析結果（本の分析と同じ形式）を作る。"""
    uploaded = st.file_uploader(
        "手元の論文・記事のファイル（任意・.txt / .pdf）", type=["txt", "pdf"], key="ai_research_file",
        help="あれば調査の材料に加えます。",
    )
    material = ""
    if uploaded is not None:
        try:
            material = book_loader.load_book(uploaded.name, uploaded.getvalue()).text
        except book_loader.BookLoadError as e:
            st.error(str(e))
    st.caption(
        f"費用の目安: 約$1〜3（Web検索 最大{book_ai.RESEARCH_MAX_SEARCHES}回 × ${book_ai.WEB_SEARCH_PRICE_USD} ＋ "
        "読み込んだ論文・記事の量によるトークン代）。調べ終わるまで数分かかります。"
    )
    if st.button("① Webで調べる（論文・記事）", type="primary", key="ai_research", use_container_width=True,
                 disabled=not topic.strip()):
        with st.status("論文・記事を調べています…", expanded=True) as status:
            try:
                result = book_ai.research_topic(topic, focus, urls, material, worry_hint, progress=status.write)
            except book_ai.BookAIError as e:
                status.update(label="調査に失敗しました", state="error")
                st.error(str(e))
                return
            status.update(label="調査が完了しました", state="complete")
        data = dict(result.data)
        st.session_state["ai_research_report"] = data.pop("research_report", "")
        st.session_state["ai_analysis_text"] = json.dumps(data, ensure_ascii=False, indent=2)
        st.session_state["_ai_costs"] = {"analysis": _format_cost(result), "notes": result.notes}
        st.rerun()


def _render_book_analysis(model: str, title_hint: str, author_hint: str, worry_hint: str) -> None:
    """① 本のファイルを読み込んで、Claudeに分析させる。"""
    st.session_state.pop("ai_research_report", None)
    uploaded = st.file_uploader("本のファイル（.txt / .pdf）", type=["txt", "pdf"], key="ai_book_file")
    pasted = st.text_area("または、本のテキストを貼り付け", key="ai_book_paste", height=100)
    try:
        book = _load_uploaded_book(uploaded, pasted, title_hint)
    except book_loader.BookLoadError as e:
        st.error(str(e))
        book = None

    if book is not None:
        pages = f"・{book.pages}ページ" if book.pages else ""
        st.caption(f"📖 読み込んだ本: {book.title_guess or '（貼り付け）'}　{book.char_count:,}文字{pages}")
        col_est, col_analyze = st.columns(2)
        if col_est.button("📏 費用を見積もる", key="ai_estimate", use_container_width=True):
            try:
                tokens = book_ai.count_book_tokens(book.text)
                cost = book_ai.estimate_cost(model, tokens, 6000)
                st.info(
                    f"① 本の分析: 入力 約{tokens:,}トークン → 約${cost:.2f}（約{cost * USD_TO_JPY:,.0f}円）"
                    "　② 台本の生成: 約$0.1〜0.3（分析結果だけを使うため安価）"
                )
            except book_ai.BookAIError as e:
                st.error(str(e))
        if col_analyze.button("① 本を分析する（要点の抽出）", type="primary", key="ai_analyze", use_container_width=True):
            with st.status("本を分析しています…", expanded=True) as status:
                try:
                    result = book_ai.analyze_book(
                        book.text, title_hint or book.title_guess, author_hint, worry_hint, progress=status.write,
                    )
                except book_ai.BookAIError as e:
                    status.update(label="分析に失敗しました", state="error")
                    st.error(str(e))
                else:
                    status.update(label="分析が完了しました", state="complete")
                    st.session_state["ai_analysis_text"] = json.dumps(result.data, ensure_ascii=False, indent=2)
                    st.session_state["_ai_costs"] = {"analysis": _format_cost(result), "notes": result.notes}
                    st.rerun()


def _render_script_generation(project: Project, style: str, target_minutes: float, structure_hint: str,
                              worry_hint: str) -> None:
    """② 分析結果（本 / 調べた論文・記事）から台本を作る。"""
    costs = st.session_state.get("_ai_costs", {})
    if "ai_analysis_text" in st.session_state:
        _render_analysis_preview(st.session_state["ai_analysis_text"])
        if costs.get("analysis"):
            st.caption(f"💰 ①分析: {costs['analysis']}")
        st.text_area(
            "分析結果（JSON・必要なら修正してから台本を生成できます）", key="ai_analysis_text", height=220,
        )
        apply_now = st.checkbox("生成した台本をそのままシーンに反映する", value=True, key="ai_apply_now")
        button_label = "② ショートの台本を生成する" if style == "short" else "② 台本を生成する"
        if st.button(button_label, type="primary", key="ai_generate_script", use_container_width=True):
            try:
                analysis = json.loads(st.session_state["ai_analysis_text"])
            except json.JSONDecodeError as e:
                st.error(f"分析結果のJSONの書式が正しくありません（{e.lineno}行目: {e.msg}）")
                return
            with st.status("台本を書いています…", expanded=True) as status:
                try:
                    result = book_ai.generate_script(
                        analysis, style, target_minutes, worry_hint, progress=status.write,
                        speech_speed=project.speech_speed, structure_hint=structure_hint,
                    )
                except book_ai.BookAIError as e:
                    status.update(label="台本の生成に失敗しました", state="error")
                    st.error(str(e))
                    return
                status.update(label="台本ができました", state="complete")
            script_json = json.dumps(result.data, ensure_ascii=False, indent=2)
            costs["script"] = _format_cost(result)
            costs["notes"] = costs.get("notes", []) + result.notes
            st.session_state["_ai_costs"] = costs
            st.session_state["_ai_generated_script"] = script_json
            if apply_now:
                _import_script(
                    project, script_json,
                    use_voicevox_timing=st.session_state.get("book_script_timing", True),
                    replace_existing=st.session_state.get("book_script_replace", True),
                    add_ending=st.session_state.get("book_script_ending", True),
                    reveal_bullets=st.session_state.get("book_script_reveal", True),
                )
            st.rerun()
        if costs.get("script"):
            st.caption(f"💰 ②台本: {costs['script']}")
        for note in costs.get("notes", []):
            st.caption(f"ℹ️ {note}")
        if st.session_state.get("_ai_generated_script"):
            st.download_button(
                "⬇️ 生成した台本JSONを保存", data=st.session_state["_ai_generated_script"],
                file_name=f"{project.book_title or 'script'}_台本.json", mime="application/json", key="ai_script_download",
            )


def _render_analysis_preview(analysis_text: str) -> None:
    try:
        analysis = json.loads(analysis_text)
    except json.JSONDecodeError:
        return
    title = analysis.get("book_title") or "（タイトル不明）"
    author = f"／{analysis['author']}" if analysis.get("author") else ""
    st.markdown(f"**📘 {title}{author}**　{analysis.get('one_line_summary', '')}")
    if analysis.get("source_kind") == "research":
        report = st.session_state.get("ai_research_report", "")
        sources = analysis.get("sources") or []
        if sources:
            st.markdown(f"**📚 調べた出典（{len(sources)}件）**\n" + "\n".join(
                f"{i}. {s.get('title', '')}（{s.get('publisher', '')} {s.get('year', '')}）"
                + (f" [{s.get('kind')}]" if s.get("kind") else "") + (f" — {s['url']}" if s.get("url") else "")
                for i, s in enumerate(sources, start=1)
            ))
        if report:
            with st.expander("📝 調査レポートを読む"):
                st.markdown(report)
    col_left, col_right = st.columns(2)
    with col_left:
        if analysis.get("worries"):
            st.markdown("**こんな悩みに**\n" + "\n".join(f"- {w}" for w in analysis["worries"]))
        if analysis.get("key_takeaways"):
            st.markdown("**実践ポイント**\n" + "\n".join(f"- {t}" for t in analysis["key_takeaways"]))
    with col_right:
        chapters = analysis.get("chapters", [])
        if chapters:
            st.markdown(f"**章ごとの要点（{len(chapters)}章）**")
            for chapter in chapters:
                st.markdown(f"- {chapter.get('title', '')}：" + " / ".join(chapter.get("key_points", [])[:3]))


def _render_script_import(project: Project) -> None:
    flash = st.session_state.get(_BOOK_IMPORT_FLASH_KEY)
    if flash is not None:
        del st.session_state[_BOOK_IMPORT_FLASH_KEY]
        st.success(flash["success"])
        for w in flash["warnings"]:
            st.warning(w)

    with st.expander("📋 台本JSONを読み込む", expanded=not project.book_title):
        st.caption(
            "「ブロック（場面 + 黒板スライド + セリフのリスト）」を並べたJSONを貼り付けると、"
            "1セリフ = 1シーンとして、場面（BGM切り替え用）・黒板スライドを設定した状態でまとめて作成します。"
            "AIチャットの回答を ```json ～ ``` ごと貼り付けても読み込めます。"
            "まずは「サンプル台本を入れる」で書き方を確認してください。"
        )
        st.button("📝 サンプル台本を入れる", on_click=_fill_book_script_sample, key="book_script_sample")
        script_text = st.text_area(
            "台本JSONを貼り付け", key="book_script_text", height=260,
            placeholder='{"book_title": "...", "blocks": [{"section": "intro", "slide": {...}, "lines": [...]}]}',
        )
        uploaded = st.file_uploader(
            "または、台本JSONファイル(.json / .txt)を選択", type=["json", "txt"], key="book_script_file"
        )
        if uploaded is not None:
            script_text = uploaded.getvalue().decode("utf-8-sig", errors="replace")

        col_timing, col_replace, col_ending = st.columns(3)
        use_voicevox_timing = col_timing.checkbox(
            "VOICEVOXで読み上げ時間を測って表示秒数を合わせる", value=True, key="book_script_timing",
            help="VOICEVOXが起動していない場合は、文字数からの概算になります。",
        )
        replace_existing = col_replace.checkbox(
            "既存のシーンを置き換える（オフの場合は末尾に追加）", value=True, key="book_script_replace",
        )
        add_ending = col_ending.checkbox(
            "最後にエンディングを付ける", value=True, key="book_script_ending",
            help=(
                "最後に、2人が笑顔で「ご視聴ありがとうございました！／チャンネル登録＆高評価よろしくお願いします！」と"
                "挨拶するエンディング（字幕なし・約12秒）を付けます（台本にエンディングのブロックがある場合は付けません）。"
                "内容は config/ending.json で変更できます。"
            ),
        )

        reveal_bullets = st.checkbox(
            "黒板の箇条書きをセリフに合わせて1行ずつ書き足す", value=True, key="book_script_reveal",
            help="オフにすると、黒板の箇条書きは各ブロックの最初から全部表示されます。",
        )

        if st.button("🪄 台本からシーンを一括生成", type="primary", use_container_width=True, key="book_script_generate"):
            if _import_script(project, script_text, use_voicevox_timing, replace_existing, add_ending, reveal_bullets):
                st.rerun()


def _preview_resolution(resolution: tuple[int, int]) -> tuple[int, int]:
    scale = OVERVIEW_PREVIEW_MAX_DIM / max(resolution)
    return max(1, round(resolution[0] * scale)), max(1, round(resolution[1] * scale))


def _render_group_preview(project: Project, group: book_script.SceneGroup) -> None:
    """ブロック先頭のシーンを、実際の動画と同じレイアウトで縮小プレビューする。"""
    scene = group.scenes[0]
    try:
        media = slide_renderer.resolve_scene_content_media(scene, project.resolution)
        show_label = project.show_chapter_label and not video_metadata.is_short(project) and scene.section != "ending"
        st.image(scene_preview(
            scene.speaker, scene.expression, False,
            book_script.effective_background_path(project, scene),
            _preview_resolution(project.resolution),
            content_media_path=media,
            hidden_characters=scene.render_hidden,
            partner_expression=scene.partner_expression,
            background_blur=project.background_blur,
            headline=scene.headline,
            chapter_label=video_metadata.chapter_label(scene) if show_label else "",
            mood=scene.mood, card_text=scene.card_text,
        ), use_container_width=True)
    except Exception as e:  # noqa: BLE001 - プレビューの失敗で一覧全体を落とさない
        st.warning(f"プレビューを作成できませんでした（{e}）")


def _render_book_cover(project: Project) -> None:
    """本の表紙画像（本を紹介するシーンで画面中央に表示する）のアップロード。"""
    col_upload, col_preview = st.columns([3, 1])
    with col_upload:
        uploaded = st.file_uploader(
            "📕 本の表紙画像（本を紹介するシーンで画面に表示します）", type=["png", "jpg", "jpeg", "webp"],
            key="book_cover_file",
            help="無い場合は、書名と著者を書いた黒板で代わりに見せます。",
        )
        if uploaded is not None:
            identity = (uploaded.name, uploaded.size)
            if st.session_state.get("_book_cover_identity") != identity:
                BOOK_COVER_DIR.mkdir(parents=True, exist_ok=True)
                path = BOOK_COVER_DIR / uploaded.name
                path.write_bytes(uploaded.getbuffer())
                project.book_cover_path = str(path)
                st.session_state["_book_cover_identity"] = identity
                book_script.apply_book_cover(project)
                forget_scene_widgets([s for s in project.scenes if s.show_book_cover])
        if project.book_cover_path and st.button("表紙画像を外す", key="book_cover_remove"):
            project.book_cover_path = None
            book_script.apply_book_cover(project)
            forget_scene_widgets([s for s in project.scenes if s.show_book_cover])
            st.rerun()
    with col_preview:
        if project.book_cover_path and Path(project.book_cover_path).exists():
            st.image(project.book_cover_path, width=110)


def _render_overview(project: Project) -> None:
    st.subheader("構成の確認")
    if not project.scenes:
        st.info("まだシーンがありません。上の「台本JSONを読み込む」から台本を読み込んでください。")
        return
    if project.source_kind not in ("research", "english"):
        _render_book_cover(project)

    groups = book_script.group_scenes(project.scenes)
    total = project.total_duration
    if project.book_title:
        st.markdown(f"#### 📘 {project.book_title}")
    c1, c2, c3 = st.columns(3)
    c1.metric("シーン数", f"{len(project.scenes)}")
    c2.metric("合計の長さ", f"{int(total // 60)}分{int(total % 60):02d}秒")
    is_landscape = project.resolution[0] >= project.resolution[1]
    c3.metric(
        "画面の向き", "横画面" if is_landscape else "縦画面",
        help="横画面も縦画面（ショート動画）も黒板のスライドになります。",
    )
    st.caption(
        "アイコンは各シーンの動き（🎥カメラ 📳揺れ 🐸ぴょん 〰ゆらゆら 🚶登場 🔀黒板切り替え ✍書き足し）です。"
        "背景・BGM・動き・出力フォーマットはサイドバーで変更できます。セリフの修正や1シーンずつの細かい調整、"
        "動画の書き出しは「🎬 シーン編集・書き出し」タブで行います。"
    )

    # 縦画面では、プレビュー画像が縦長になるため画像列を細めにする
    image_ratio = 1.2 if project.resolution[0] >= project.resolution[1] else 0.6
    scene_no = 1
    contexts = motion.build_contexts(project.scenes)
    for group_no, group in enumerate(groups, start=1):
        icon = _SECTION_ICONS.get(group.section, "🎬")
        label = group.label
        title = group.slide_title or "（スライドなし）"
        bgm = project.resolve_bgm_path(group.scenes[0])
        with st.container(border=True):
            st.markdown(f"**{icon} ブロック{group_no}：{label}**　—　{title}")
            col_img, col_lines = st.columns([image_ratio, 2])
            with col_img:
                _render_group_preview(project, group)
            with col_lines:
                for scene in group.scenes:
                    if scene.card_text:
                        st.markdown(f"`{scene_no:>2}` 🎬 **場面転換「{scene.card_text}」**　<small>{scene.duration:.1f}秒</small>",
                                    unsafe_allow_html=True)
                        scene_no += 1
                        continue
                    if not scene.text.strip():
                        st.markdown(f"`{scene_no:>2}` （セリフなし）　<small>{scene.duration:.1f}秒</small>", unsafe_allow_html=True)
                        scene_no += 1
                        continue
                    speaker = get_character_display_name(scene.speaker)
                    expression = get_expression_label(scene.speaker, scene.expression)
                    se = f"　🔔{Path(scene.se_path).stem}" if scene.se_path else ""
                    if not scene.show_telop:
                        se += "　（字幕なし）"
                    if scene.has_slide and scene.show_board and not scene.illustration_path:
                        se += "　🟩黒板" + ("（番号付き）" if scene.slide_numbered else "")
                    if scene.note_text:
                        se += f"　📝解説「{scene.note_focus or scene.note_text}」"
                    if scene.background_path and project.common_background_path != scene.background_path:
                        se += f"　🏞{Path(scene.background_path).stem}"
                    elif scene.background_name:
                        se += f"　🏞（用意待ち: {scene.background_name}）"
                    if scene.board_hold:
                        se += f"　📖+{scene.board_hold:.1f}秒"
                    if scene.mood:
                        se += f"　🌗{MOOD_LABELS.get(scene.mood, scene.mood).split('（')[0]}"
                    if scene.pause_style == "shadow":
                        se += f"　🗣 3・2・1 → もう一度音声（{scene.headline}）"
                    elif scene.silent:
                        label = {"repeat": "3・2・1 → ", "think": "タイマー "}.get(scene.pause_style, "")
                        se += f"　🔁 無音の間 {label}「{scene.headline or 'リピート'}」"
                    elif scene.audio_id:
                        se += f"　🎧{scene.audio_id}" + ("" if scene.voice_path else "（音声未設定）")
                    elif scene.reading:
                        se += f"　🗣読み: {scene.reading}"
                    if scene.translation and not scene.silent:
                        se += f"<br>　　訳: {scene.translation}"
                    if not scene.illustration_path and scene.illustration_name:
                        se += f"　🖼（用意待ち: {scene.illustration_name}）"
                    if scene.illustration_path:
                        se += f"　🖼{Path(scene.illustration_path).stem}"
                        if scene.illustration_caption:
                            se += f"「{scene.illustration_caption}」"
                    effects = motion.describe_motion(project, scene, contexts[scene_no - 1])
                    if effects:
                        se += "　" + " ".join(e.split(" ")[0] for e in effects)  # アイコンだけ並べる
                    st.markdown(
                        f"`{scene_no:>2}` **{speaker}**（{expression}）{scene.text}　<small>{scene.duration:.1f}秒{se}</small>",
                        unsafe_allow_html=True,
                    )
                    scene_no += 1
                st.caption(f"⏱ {group.duration:.1f}秒　🎵 {Path(bgm).stem if bgm else 'BGMなし'}")


def _render_illustration_requests(project: Project) -> None:
    """合うイラストが無かったシーンで、Claudeが「こういうイラストが欲しい」と書いたものの一覧。"""
    requests = book_script.illustration_requests(project.scenes)
    if not requests:
        return
    with st.expander(f"🖼 欲しいイラスト一覧（{len(requests)}件・まだ用意されていません）", expanded=True):
        st.caption(
            "手元に合うイラストが無かった場面です。下の「ファイル名」の名前で画像を assets/illustrations/ に置くと"
            "（サイドバーの「🖼 イラスト素材」からも追加できます）、そのシーンに自動で表示されます。"
            "用意できないものは、そのシーンだけイラスト無し（黒板など）のまま書き出されます。"
        )
        rows = [
            {
                "ファイル名": req.name,
                "欲しいイラスト": req.description,
                "画面の説明": " / ".join(req.captions),
                "シーン": "、".join(str(n) for n in req.scene_numbers),
            }
            for req in requests
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        text = "\n".join(
            f"{r['ファイル名']}.png\t{r['欲しいイラスト']}\t（シーン {r['シーン']}）" for r in rows
        )
        col_dl, col_link = st.columns(2)
        col_dl.download_button(
            "⬇️ 一覧をテキストで保存", data=text, file_name=f"{project.book_title or 'video'}_欲しいイラスト.txt",
            mime="text/plain", key="illust_requests_download", use_container_width=True,
        )
        if col_link.button("🔄 追加したイラストを反映", key="illust_requests_link", use_container_width=True):
            st.rerun()


def _render_background_requests(project: Project) -> None:
    """回想・寸劇などで場所が変わる場面で、Claudeが「こういう背景が欲しい」と書いたものの一覧。"""
    requests = book_script.background_requests(project.scenes)
    if not requests:
        return
    with st.expander(f"🏞 欲しい背景一覧（{len(requests)}件・まだ用意されていません）", expanded=True):
        st.caption(
            "回想や寸劇で、学校・職場など部屋以外の場所の話をしている場面です。下の「ファイル名」の名前で画像"
            "（横長 16:9 がおすすめ）を assets/backgrounds/ に置くと、その場面の背景に自動で反映されます。"
            "用意できないものは、いつもの部屋の背景のまま書き出されます。"
        )
        rows = [{"ファイル名": req.name, "欲しい背景": req.description,
                 "シーン": "、".join(str(n) for n in req.scene_numbers)} for req in requests]
        st.dataframe(rows, use_container_width=True, hide_index=True)
        text = "\n".join(f"{r['ファイル名']}.png\t{r['欲しい背景']}\t（シーン {r['シーン']}）" for r in rows)
        col_dl, col_link = st.columns(2)
        col_dl.download_button(
            "⬇️ 一覧をテキストで保存", data=text, file_name=f"{project.book_title or 'video'}_欲しい背景.txt",
            mime="text/plain", key="bg_requests_download", use_container_width=True,
        )
        if col_link.button("🔄 追加した背景を反映", key="bg_requests_link", use_container_width=True):
            st.rerun()


def _render_ending_button(project: Project) -> None:
    if not project.scenes or book_script.has_ending(project.scenes):
        return
    if st.button("🙏 エンディング（ご視聴ありがとう・チャンネル登録＆高評価のお願い）を最後に追加", key="book_add_ending"):
        result = book_script.build_ending_scenes()
        project.scenes.extend(result.scenes)
        st.session_state[_BOOK_IMPORT_FLASH_KEY] = {
            "success": "エンディングのシーンを追加しました。",
            "warnings": result.warnings,
        }
        st.rerun()


def _regenerate_metadata() -> None:
    project = get_project()
    video_metadata.apply_generated_metadata(project, overwrite=True)
    # keyつきウィジェットの表示内容を、生成し直した値に合わせる（コールバック内でのみ書き換え可能）
    st.session_state["meta_title"] = project.video_title
    st.session_state["meta_description"] = project.video_description
    st.session_state["meta_tags"] = ", ".join(project.video_tags)
    st.session_state["meta_x_post"] = project.x_post
    st.session_state.pop("meta_title_candidate", None)


def _use_title_candidate() -> None:
    choice = st.session_state.get("meta_title_candidate")
    if choice:
        st.session_state["meta_title"] = choice


def _render_metadata(project: Project) -> None:
    """投稿用のタイトル・説明文・タグ（自動生成 → その場で編集できる）。"""
    if not project.scenes:
        return
    st.subheader("📝 動画のタイトル・説明文")
    meta = video_metadata.generate_metadata(project)
    if not project.video_title and not project.video_description:
        video_metadata.apply_generated_metadata(project, overwrite=False)
    # 別のプロジェクトを読み込んだ等で、表示中の入力欄の内容がプロジェクトとずれていたら合わせる
    for key, value in (
        ("meta_title", project.video_title),
        ("meta_description", project.video_description),
        ("meta_tags", ", ".join(project.video_tags)),
        ("meta_x_post", project.x_post or video_metadata.build_x_post(project)),
    ):
        st.session_state.setdefault(key, value)
    # BGMや config/bgm_credits.json を変えたら、説明文のクレジット欄だけ最新にする（他の部分の手直しは残す）
    fresh = video_metadata.refresh_credits(st.session_state["meta_description"], project)
    if fresh != st.session_state["meta_description"]:
        st.session_state["meta_description"] = fresh

    st.caption(
        "台本の内容（本のタイトル・悩み・ポイント・まとめ・各シーンの秒数）から自動で作っています。"
        "自由に書き換えられます。台本やシーンを変えたあとは「🔄 自動生成し直す」で作り直せます"
        "（目次の時刻も最新の秒数で計算し直されます）。"
    )
    st.button("🔄 自動生成し直す", on_click=_regenerate_metadata, key="meta_regenerate")

    st.selectbox(
        "タイトル案（選ぶと下のタイトル欄に入ります）", options=meta.title_candidates, index=None,
        placeholder="タイトル案から選ぶ…", key="meta_title_candidate", on_change=_use_title_candidate,
    )
    project.video_title = st.text_input("タイトル", key="meta_title", max_chars=video_metadata.TITLE_MAX_CHARS)
    project.video_description = st.text_area("説明文", key="meta_description", height=380)
    tags_text = st.text_area(
        "タグ（カンマ区切り）", key="meta_tags", height=100,
        help="タグは、検索したときの表記ゆれ・変換ミス・打ち間違い（例: えいかいわ、トーイック、ズンダモン）でも"
             "動画が見つかるようにするためのものです。動画と関係のない言葉は入れないでください。",
    )
    project.video_tags = [t.strip() for t in tags_text.replace("、", ",").split(",") if t.strip()]
    used = video_metadata._tags_length(project.video_tags)
    st.caption(f"{'✅' if used <= video_metadata.TAGS_MAX_CHARS else '⚠️'} タグ {len(project.video_tags)}個・"
               f"{used} / {video_metadata.TAGS_MAX_CHARS}字（YouTubeの上限）")

    if meta.short_chapters:
        names = "、".join(f"「{c.label}」({c.duration:.0f}秒)" for c in meta.short_chapters)
        st.caption(
            f"💡 YouTubeの目次（チャプター）は各区間が10秒以上必要です。{names} が10秒未満のため、"
            "このままだとチャプターとして表示されない場合があります。"
        )
    _render_x_post(project)
    st.download_button(
        "⬇️ タイトル・説明文・タグをテキストで保存", data=video_metadata.export_text(project),
        file_name=f"{project.book_title or 'video'}_投稿用.txt", mime="text/plain", key="meta_download",
    )


def _render_x_post(project: Project) -> None:
    """X（旧Twitter）に投稿する文章（編集でき、下の枠の右上のボタンでコピーできる）。"""
    st.markdown("**𝕏 X（旧Twitter）の投稿文**")
    project.x_post = st.text_area(
        "X（旧Twitter）の投稿文", key="meta_x_post", height=200, label_visibility="collapsed",
        help="「（ここに動画のURLを貼る）」を、投稿した動画のURLに置き換えてください。",
    )
    length = video_metadata.x_post_length(project.x_post)
    ok = length <= video_metadata.X_MAX_WEIGHT
    st.caption(
        f"{'✅' if ok else '⚠️'} 文字数 {length} / {video_metadata.X_MAX_WEIGHT}"
        "（Xの数え方: 日本語は1文字=2、英数字は1、URLは23）。"
        + ("" if ok else "長すぎるので、少し削ってください。")
        + "下の枠の右上のボタンでコピーできます。"
    )
    st.code(project.x_post, language=None)


@st.cache_data(max_entries=60, show_spinner=False)
def _thumbnail_png(spec_json: str, stamps: tuple) -> bytes:
    import io

    img = thumbnail.render_thumbnail(json.loads(spec_json))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_thumbnail(project: Project) -> None:
    """サムネイル（1280×720）をいくつかのレイアウトで作り、選んで保存する。"""
    if not project.scenes:
        return
    st.subheader("🖼 サムネイル")
    spec = thumbnail.default_spec(project)
    english = project.source_kind == "english"
    st.caption(
        "台本のAIが考えた文言（無ければタイトルから）で作っています。上の大きな文字は2行・1行8字くらいまでが見やすく、"
        "文字の色は、何も付けなければ白、**語** で囲むと赤（大きく）、==語== で囲むと黄色になります。"
        "下のプレビューは、スマホでの見え方に近い大きさです。"
    )
    images = [None] + ([project.book_cover_path] if project.book_cover_path else []) + list(dict.fromkeys(
        s.illustration_path for s in project.scenes if s.illustration_path))
    current_image = spec.get("image") if spec.get("image") in images else (images[1] if len(images) > 1 else None)
    z_opts, m_opts = get_available_expressions("zundamon"), get_available_expressions("shikoku_metan")
    panel_images = [None] + thumbnail.video_images(project)

    def pick(value, options, fallback):
        return value if value in options else (fallback if fallback in options else options[0])

    # 入力欄の値は session_state に入れてから表示する（value= で渡すと、台本を読み込み直したときに
    # ブラウザ側に残っていた前の文字が、次の操作で送り返されて上書きしてしまうため）
    defaults = {
        "thumb_text": spec.get("text", ""),
        "thumb_sub": spec.get("sub", ""),
        "thumb_shout": spec["shout"] if spec.get("shout") is not None
        else thumbnail.DEFAULT_SHOUTS.get(spec.get("zundamon"), ""),
        "thumb_before": spec.get("before", ""),
        "thumb_after": spec.get("after", ""),
        "thumb_scene": spec.get("scene", ""),
        "thumb_phrase": spec.get("phrase", ""),
        "thumb_zundamon": pick(spec.get("zundamon"), z_opts, "surprised"),
        "thumb_metan": pick(spec.get("metan"), m_opts, "point"),
        "thumb_before_face": pick(spec.get("before_face"), z_opts, "gloomy"),
        "thumb_after_face": pick(spec.get("after_face"), z_opts, "happy"),
        "thumb_image": current_image,
        "thumb_before_image": spec.get("before_image"),
        "thumb_after_image": spec.get("after_image"),
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    for key, options in (("thumb_zundamon", z_opts), ("thumb_before_face", z_opts), ("thumb_after_face", z_opts),
                         ("thumb_metan", m_opts), ("thumb_image", images), ("thumb_before_image", panel_images),
                         ("thumb_after_image", panel_images)):
        if st.session_state[key] not in options:
            st.session_state[key] = defaults[key] if defaults[key] in options else options[0]

    col_text, col_opt = st.columns([3, 2])
    spec["text"] = col_text.text_area("一番大きく出す一言（改行で行を分ける）", key="thumb_text", height=90)
    if english:
        col_scene, col_phrase = col_text.columns([1, 2])
        spec["scene"] = col_scene.text_input("使える場面（「〇〇で使える！」）", key="thumb_scene")
        spec["phrase"] = col_phrase.text_input("英語のフレーズ（吹き出し）", key="thumb_phrase")
    else:
        col_before, col_after = col_text.columns(2)
        spec["before"] = col_before.text_input("見る前（悩み・失敗）", key="thumb_before")
        spec["after"] = col_after.text_input("見た後（解決・変化）", key="thumb_after")
        spec["before_face"] = col_before.selectbox(
            "見る前の表情", z_opts, format_func=lambda e: get_expression_label("zundamon", e), key="thumb_before_face")
        spec["after_face"] = col_after.selectbox(
            "見た後の表情", z_opts, format_func=lambda e: get_expression_label("zundamon", e), key="thumb_after_face")
        image_label = lambda v: "（なし）" if not v else Path(v).stem  # noqa: E731
        spec["before_image"] = col_before.selectbox("見る前の画面に敷く画像（動画で使った画像）", panel_images,
                                                    format_func=image_label, key="thumb_before_image")
        spec["after_image"] = col_after.selectbox("見た後の画面に敷く画像（動画で使った画像）", panel_images,
                                                  format_func=image_label, key="thumb_after_image")
    spec["sub"] = col_opt.text_input("左上の帯", key="thumb_sub")
    spec["shout"] = col_opt.text_input("ずんだもんの吹き出し（空欄なら出さない）", key="thumb_shout")
    spec["zundamon"] = col_opt.selectbox(
        "ずんだもんの表情", z_opts, format_func=lambda e: get_expression_label("zundamon", e), key="thumb_zundamon")
    spec["metan"] = col_opt.selectbox(
        "めたんの表情（2人のレイアウト）", m_opts, format_func=lambda e: get_expression_label("shikoku_metan", e),
        key="thumb_metan")
    spec["image"] = col_opt.selectbox("画像（2人・使える場面のレイアウト）", images,
                                      format_func=lambda v: "（なし）" if not v else Path(v).stem, key="thumb_image")
    project.thumbnail = {k: v for k, v in spec.items() if k != "image"} | ({"image": spec["image"]} if spec["image"] else {})

    stamps = tuple(Path(p).stat().st_mtime if p and Path(p).exists() else 0
                   for p in (spec.get("image"), spec.get("before_image"), spec.get("after_image")))
    layouts = [k for k in thumbnail.LAYOUTS if k != ("before_after" if english else "scene")]
    for row in range(0, len(layouts), 2):
        cols = st.columns(2)
        for col, layout in zip(cols, layouts[row:row + 2]):
            png = _thumbnail_png(json.dumps(spec | {"layout": layout}, ensure_ascii=False), stamps)
            chosen = spec.get("layout") == layout
            col.image(png, caption=("⭐ AIのおすすめ・" if chosen else "") + thumbnail.LAYOUTS[layout],
                      use_container_width=True)
            col.download_button(
                "⬇️ このサムネイルを保存", data=png, key=f"thumb_dl_{layout}", use_container_width=True,
                file_name=f"{project.book_title or 'video'}_サムネイル_{layout}.png", mime="image/png",
                type="primary" if chosen else "secondary",
            )


def render_book_mode() -> None:
    project = get_project()
    st.subheader("台本の準備")
    _render_ai_generation(project)
    _render_script_import(project)
    st.divider()
    if project.source_kind == "english":
        # 英会話の動画の構成・投稿用の文章は、英会話モードのタブで表示する（同じ画面を2か所に出さない）
        st.info("いまのプロジェクトは英会話の動画です。構成の確認・投稿用の文章は「🗣 英会話モード」タブで行ってください。")
        return
    render_project_review(project)


def render_project_review(project: Project) -> None:
    """構成の確認・イラストの依頼・エンディング・投稿用のタイトルと説明文（英会話モードのタブからも使う）。"""
    _render_overview(project)
    _render_illustration_requests(project)
    _render_background_requests(project)
    _render_ending_button(project)
    st.divider()
    _render_metadata(project)
    _render_thumbnail(project)
    _render_promo_short(project)


# ---------------------------------------------------------------------------
# 本編紹介ショート（本編の要点をまとめて「詳しくは本編で」と締める縦型ショート）
# ---------------------------------------------------------------------------

def _promo_pair() -> dict | None:
    """いま作業中の本編とそのショートの組 {"main": Project, "short": Project}（本編とショートを行き来するため）。"""
    return st.session_state.get(_PROMO_PAIR_KEY)


def _promo_for(main: Project, rebuild: bool = False) -> tuple[Project, list[str]]:
    """本編の紹介ショート（作りかけがあればそれ、無ければ台本から作る）。本編とショートの組として覚えておく。"""
    pair = _promo_pair()
    short = pair["short"] if pair and pair["main"] is main and not rebuild else None
    warnings: list[str] = []
    if short is None:
        with st.spinner("本編紹介ショートを作っています…"):
            short, warnings = book_script.build_promo_project(
                main, use_voicevox_timing=st.session_state.get("book_script_timing", True),
            )
    st.session_state[_PROMO_PAIR_KEY] = {"main": main, "short": short}
    return short, warnings


def _open_promo_short(main: Project, rebuild: bool = False) -> None:
    short, warnings = _promo_for(main, rebuild)
    set_project(short)
    st.session_state[_PROMO_FLASH_KEY] = {
        "success": f"本編紹介ショート（{len(short.scenes)}シーン・約{short.total_duration:.0f}秒・縦画面）に切り替えました。"
                   "上のバーの「🎬 本編 / 📱 紹介ショート」でいつでも切り替えられます。「🎬 書き出しへ」で書き出せます。",
        "warnings": warnings,
    }


def export_targets(project: Project) -> tuple[Project | None, bool]:
    """「動画を生成する」で書き出す本編（表示中が紹介ショートでも本編を返す）と、紹介ショートも書き出せるか。
    本編と組でない単独のショートは (そのショート, False)。"""
    pair = _promo_pair()
    if pair and (project is pair["main"] or project is pair["short"]):
        return pair["main"], True
    if project.promo_of or video_metadata.is_short(project):
        return project, False
    return project, bool(project.promo_short.get("blocks"))


def promo_for_export(main: Project) -> tuple[Project, list[str]]:
    """書き出し用に、本編の紹介ショートを用意する（表示中の動画は切り替えない）。"""
    return _promo_for(main)


def _switch_pair_view() -> None:
    """プロジェクトバーの「本編 / 本編紹介ショート」の切り替え。"""
    pair = _promo_pair()
    choice = st.session_state.get("pair_view")
    if not pair or choice not in ("main", "short"):
        return
    target = pair[choice]
    if target is not get_project():
        set_project(target)


def render_project_bar() -> None:
    """画面上部のバー: いま編集中の動画（名前・縦横・長さ）と、本編⇔本編紹介ショートの切り替え・書き出しへの移動。

    いつも同じ場所に同じ形で出す（出したり消したりすると、描き直しの途中で古いボタンが残って見えるため）。
    """
    project = get_project()
    flash = st.session_state.pop(_PROMO_FLASH_KEY, None)
    pair = _promo_pair()
    in_pair = bool(pair and (project is pair["main"] or project is pair["short"]))
    with st.container(border=True, key="project_bar"):
        col_info, col_switch, col_go = st.columns([5, 3, 2], vertical_alignment="center")
        portrait = project.resolution[1] > project.resolution[0]
        minutes, seconds = divmod(int(round(project.total_duration)), 60)
        kind = "📱 本編紹介ショート" if project.promo_of else ("📱 ショート" if project.video_style == "short" else "🎬 本編")
        col_info.markdown(
            f"**{project.book_title or project.name}**　{kind}・{'縦' if portrait else '横'}画面・"
            f"{len(project.scenes)}シーン・{minutes}分{seconds:02d}秒"
        )
        if in_pair:
            st.session_state["pair_view"] = "short" if project is pair["short"] else "main"
            col_switch.segmented_control(
                "表示する動画", ["main", "short"], format_func={"main": "🎬 本編", "short": "📱 紹介ショート"}.get,
                key="pair_view", on_change=_switch_pair_view, required=True, label_visibility="collapsed",
            )
        elif project.promo_of:
            col_switch.caption(f"本編「{project.promo_of}」の紹介ショート")
        col_go.button("🎬 書き出しへ", key="bar_go_export", on_click=go_to_tab, args=("edit",),
                      use_container_width=True, disabled=not project.scenes)
    if flash:
        st.success(flash["success"])
        for w in flash["warnings"]:
            st.warning(w)


def _render_promo_short(project: Project) -> None:
    """本編の構成確認の最後に出す、本編紹介ショートの確認・作成。"""
    if project.video_style == "short" or project.promo_of or not project.scenes:
        return
    st.divider()
    st.subheader("📱 本編紹介ショート")
    st.caption(
        f"本編の要点をまとめて「詳しくは本編で」と締める、縦画面・約{book_ai.PROMO_SHORT_SECONDS}秒のショートです。"
        "投稿したら、ショートの説明欄のリンクと「関連動画」に本編を設定すると、本編に誘導できます。"
    )
    promo = project.promo_short
    if not promo.get("blocks"):
        st.info("この台本には、本編紹介ショートの台本が入っていません。本編の台本から作れます。")
        _render_promo_generation(project)
        return
    lines = [(line.get("speaker", ""), str(line.get("text") or "").strip())
             for block in promo["blocks"] if isinstance(block, dict)
             for line in block.get("lines", []) if isinstance(line, dict) and str(line.get("text") or "").strip()]
    chars = sum(len(t) for _, t in lines)
    title = promo.get("video_title") or (promo.get("title_candidates") or [""])[0]
    st.markdown(f"**{title}**　セリフ{len(lines)}個・約{chars / (5.5 * project.speech_speed):.0f}秒の見込み")
    with st.expander("ショートのセリフを見る"):
        st.markdown("\n".join(f"- **{get_character_display_name(sp)}**：{tx}" for sp, tx in lines))
    pair = _promo_pair()
    opened = bool(pair and pair["main"] is project)
    col_open, col_rebuild = st.columns(2)
    if col_open.button("📱 作りかけのショートに切り替える" if opened else "📱 ショートを作って開く（編集・書き出し）",
                       type="primary", key="promo_open", use_container_width=True):
        _open_promo_short(project)
        st.rerun()
    if opened and col_rebuild.button("🔄 台本からショートを作り直す（ショートで直した内容は消えます）",
                                     key="promo_rebuild", use_container_width=True):
        _open_promo_short(project, rebuild=True)
        st.rerun()
    with st.expander("台本を作り直す（Claude）"):
        _render_promo_generation(project)


def _render_promo_generation(project: Project) -> None:
    """本編のセリフの一覧から、本編紹介ショートの台本だけを作る（API / チャット画面）。"""
    if book_ai.api_key_configured():
        if st.button("🤖 本編の台本からショートの台本を作る（Claude API）", key="promo_generate"):
            with st.status("ショートの台本を書いています…", expanded=True) as status:
                try:
                    result = promo_short.generate(project, progress=status.write)
                except (book_ai.BookAIError, book_script.BookScriptError) as e:
                    status.update(label="ショートの台本を作れませんでした", state="error")
                    st.error(str(e))
                    return
                status.update(label="ショートの台本ができました", state="complete")
            st.session_state.pop(_PROMO_PAIR_KEY, None)
            st.session_state[_PROMO_FLASH_KEY] = {"success": f"ショートの台本を作りました（{_format_cost(result)}）。",
                                                  "warnings": []}
            st.rerun()
    st.caption("APIを使わない場合: 下のプロンプトを Claude のチャットに貼り付け、返ってきたJSONを下に貼って読み込みます。")
    st.code(promo_short.manual_prompt(project), language="markdown")
    pasted = st.text_area("返ってきたJSON", key="promo_paste", height=120)
    if st.button("📥 ショートの台本を読み込む", key="promo_import", disabled=not pasted.strip()):
        try:
            promo_short.attach(project, book_script.load_book_script(pasted))
        except book_script.BookScriptError as e:
            st.error(str(e))
            return
        st.session_state.pop(_PROMO_PAIR_KEY, None)
        st.session_state[_PROMO_FLASH_KEY] = {"success": "ショートの台本を読み込みました。", "warnings": []}
        st.rerun()
