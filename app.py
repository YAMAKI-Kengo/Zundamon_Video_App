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
from src.state import forget_scene_widgets, get_project, init_session_state
from src.ui.book_mode import render_book_mode
from src.ui.english_mode import render_english_mode
from src.ui.preview import render_export_section
from src.ui.scene_editor import render_scene_editor
from src.ui.sidebar import render_sidebar
from src.utils.env_config import load_env

# .env（"# === Anthropic ===" の ANTHROPIC_API_KEY 等）を環境変数として読み込む
load_env()

st.set_page_config(
    page_title="ずんだもん解説動画ジェネレーター",
    page_icon="🎬",
    layout="wide",
)


def _link_new_assets(project) -> None:
    """素材フォルダに新しく置かれたイラスト・ネイティブ音声を、依頼していたシーンに反映する（描画のたびに確認）。"""
    # 以前の設定で足していた黒板のあとの無音の間は、設定が 0（既定）なら取り除く
    held = [s for s in project.scenes if s.board_hold]
    if held and not getattr(project, "board_pause", 0.0):
        book_script.apply_board_hold(project.scenes, 0.0)
        forget_scene_widgets(held)
    illustrated = book_script.link_requested_illustrations(project.scenes)
    voiced = english_lesson.link_native_audio(project.scenes)
    # シーン編集の入力欄が覚えている古い値で、反映した内容が上書きされないようにする
    forget_scene_widgets(illustrated + voiced)
    if illustrated:
        st.toast(f"追加されたイラストを{len(illustrated)}シーンに反映しました。")
    if voiced:
        st.toast(f"ネイティブ音声を{len(voiced)}シーンに反映しました。")


def main() -> None:
    init_session_state()

    # st.title()だと見出しが大きすぎるため、一段階小さいst.headerを使用する
    st.header("🎬 ずんだもん・四国めたん 解説動画ジェネレーター")
    st.caption(
        "画像・テキスト・表示秒数を入力するだけで、合成音声付きの解説動画を自動生成します。"
        "「📚 書籍解説モード」では、台本JSONから黒板スライド付きの書籍解説動画をまとめて作れます。"
    )

    render_sidebar()
    _link_new_assets(get_project())
    # 書籍解説用の既定背景を使っている場合、サイドバーで切り替えた出力フォーマット（横/縦）に背景を追従させる
    if book_script.sync_background_to_format(get_project()):
        st.toast("出力フォーマットに合わせて背景を切り替えました。")

    tab_book, tab_english, tab_edit = st.tabs(["📚 書籍解説モード", "🗣 英会話モード", "🎬 シーン編集・書き出し"])
    with tab_book:
        render_book_mode()
    with tab_english:
        render_english_mode()
    with tab_edit:
        render_scene_editor()
        st.divider()
        render_export_section()


if __name__ == "__main__":
    main()
