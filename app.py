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
    page_icon=":material/movie:",
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


_STYLE = """
<style>
/* 上の余白を詰め、横幅を読みやすい広さに */
.block-container { padding-top: 3.6rem; padding-bottom: 4rem; max-width: 1280px; }
/* 見出し */
.app-header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; margin: 0 0 10px; }
.app-header h1 { font-size: 1.45rem; font-weight: 700; margin: 0; padding: 0; letter-spacing: .01em; }
.app-header .app-steps { font-size: .85rem; opacity: .62; }
/* タブ: 文字を少し大きく、選択中を太く */
.stTabs [data-baseweb="tab-list"] { gap: 6px; }
.stTabs [data-baseweb="tab"] { font-size: .95rem; padding: 6px 12px; }
.stTabs [aria-selected="true"] { font-weight: 600; }
/* 枠付きの箱・折りたたみに、ごく薄い影を付けてカードのように見せる */
[data-testid="stExpander"] details, .st-key-project_bar {
  box-shadow: 0 1px 2px rgba(16, 24, 40, .05);
}
.st-key-project_bar { padding: 4px 6px; }
/* ボタンの文字を少し太く */
.stButton button, .stDownloadButton button, [data-testid="stPopover"] button { font-weight: 600; }
</style>
"""


def _render_header() -> None:
    st.markdown(_STYLE, unsafe_allow_html=True)
    st.markdown(
        '<div class="app-header"><h1>ずんだもん解説動画メーカー</h1>'
        '<span class="app-steps">台本を作る → シーンを確認・修正 → 動画を作る → 投稿の準備</span></div>',
        unsafe_allow_html=True,
    )


def main() -> None:
    init_session_state()
    keep_widget_values()

    _render_header()

    render_sidebar()
    _link_new_assets(get_project())
    # 書籍解説用の既定背景を使っている場合、サイドバーで切り替えた出力フォーマット（横/縦）に背景を追従させる
    if book_script.sync_background_to_format(get_project()):
        st.toast("出力フォーマットに合わせて背景を切り替えました。")

    render_project_bar()
    apply_pending_tab()
    # 開いているタブの中身だけを描く（全部のタブを毎回描くと重く、切り替えの途中で古い画面が残って見えるため）
    tabs = dict(zip(TABS, st.tabs(list(TABS.values()), key=TAB_KEY, on_change="rerun")))
    renderers = {"book": render_book_mode, "english": render_english_mode, "edit": render_scene_editor,
                 "review": render_review_tab, "analytics": render_analytics}
    for name, tab in tabs.items():
        if tab.open:
            with tab:
                renderers[name]()


if __name__ == "__main__":
    main()
