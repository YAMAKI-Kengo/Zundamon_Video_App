"""
ずんだもん・四国めたん 解説動画自動生成アプリ

Streamlit エントリーポイント。
画像・読み上げテキスト・表示秒数を入力するだけで、
ずんだもん / 四国めたんの合成音声付き解説動画を自動生成するアプリのUI土台。

実行方法:
    streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from src.services import book_script, english_lesson
from src.state import (
    TAB_KEY, TABS, apply_pending_tab, forget_scene_widgets, get_project, init_session_state, keep_widget_values,
)
from src.ui.analytics import render_analytics
from src.ui.book_mode import render_book_mode, render_project_bar, render_review_tab
from src.ui.english_mode import render_english_mode
from src.ui.scene_editor import render_scene_editor
from src.ui.sidebar import render_sidebar
from src.utils.env_config import load_env

# .env（"# === Anthropic ===" の ANTHROPIC_API_KEY 等）を環境変数として読み込む
load_env()

st.set_page_config(
    page_title="ずんだもん解説動画ジェネレーター",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def _link_new_assets(project) -> None:
    """素材フォルダに新しく置かれたイラスト・ネイティブ音声を、依頼していたシーンに反映する（描画のたびに確認）。"""
    # 以前の設定で足していた黒板のあとの無音の間は、設定が 0（既定）なら取り除く
    held = [s for s in project.scenes if s.board_hold]
    if held and not getattr(project, "board_pause", 0.0):
        book_script.apply_board_hold(project.scenes, 0.0)
        forget_scene_widgets(held)
    illustrated = book_script.link_requested_illustrations(project.scenes)
    backgrounds = book_script.link_requested_backgrounds(project.scenes)
    voiced = english_lesson.link_native_audio(project.scenes, english_lesson.native_gap(project))
    # シーン編集の入力欄が覚えている古い値で、反映した内容が上書きされないようにする
    forget_scene_widgets(illustrated + backgrounds + voiced)
    if illustrated:
        st.toast(f"追加されたイラストを{len(illustrated)}シーンに反映しました。")
    if backgrounds:
        st.toast(f"追加された背景を{len(backgrounds)}シーンに反映しました。")
    if voiced:
        st.toast(f"ネイティブ音声を{len(voiced)}シーンに反映しました。")


def main() -> None:
    init_session_state()
    keep_widget_values()

    st.markdown("#### 🎬 ずんだもん解説動画メーカー")

    render_sidebar()
    _link_new_assets(get_project())
    # 書籍解説用の既定背景を使っている場合、サイドバーで切り替えた出力フォーマット（横/縦）に背景を追従させる
    if book_script.sync_background_to_format(get_project()):
        st.toast("出力フォーマットに合わせて背景を切り替えました。")

    render_project_bar()
    apply_pending_tab()
    # 開いているタブの中身だけを描く（全部のタブを毎回描くと重く、切り替えの途中で古い画面が残って見えるため）
    tab_book, tab_english, tab_review, tab_analytics, tab_edit = st.tabs(
        list(TABS.values()), key=TAB_KEY, on_change="rerun")
    if tab_book.open:
        with tab_book:
            render_book_mode()
    if tab_english.open:
        with tab_english:
            render_english_mode()
    if tab_review.open:
        with tab_review:
            render_review_tab()
    if tab_edit.open:
        with tab_edit:
            render_scene_editor()
    if tab_analytics.open:
        with tab_analytics:
            render_analytics()


if __name__ == "__main__":
    main()
