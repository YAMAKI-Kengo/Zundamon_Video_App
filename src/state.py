"""
st.session_state 周りのヘルパー。

Streamlitはスクリプトを再実行する仕組みのため、プロジェクトの状態
(Project オブジェクト)は st.session_state に保持し、各UIコンポーネントは
ここ経由で読み書きする。
"""
from __future__ import annotations

import streamlit as st

from src.models import Project, Scene

PROJECT_KEY = "project"


def init_session_state() -> None:
    """初回アクセス時にProjectを初期化する。"""
    if PROJECT_KEY not in st.session_state:
        project = Project()
        project.add_scene(
            Scene(
                speaker="zundamon",
                expression="normal",
                text="ずんだもんなのだ！このアプリで解説動画を作るのだ。",
                duration=3.0,
            )
        )
        st.session_state[PROJECT_KEY] = project


def get_project() -> Project:
    init_session_state()
    return st.session_state[PROJECT_KEY]


def forget_scene_widgets(scenes) -> None:
    """シーンの入力欄が覚えている値を捨てて、次の描画でシーンの今の値から表示し直させる。

    Streamlit の入力欄は、一度操作すると自分の値を覚えていて、描画のたびにその値でシーンを上書きする。
    そのため、アプリ側でシーンを書き換えたとき（置かれたイラスト・ネイティブ音声の自動反映など）は、
    入力欄の値を捨てないと、すぐに元の値へ戻されてしまう。シーンの入力欄の key は「名前_シーンID」の形。
    """
    suffixes = tuple(f"_{scene.id}" for scene in scenes)
    if not suffixes:
        return
    for key in [k for k in st.session_state.keys() if str(k).endswith(suffixes)]:
        del st.session_state[key]


def set_project(project: Project) -> None:
    """プロジェクト全体を丸ごと置き換える（プロジェクトファイルの読み込み時に使用）。"""
    st.session_state[PROJECT_KEY] = project
    # シーンIDを含まない（プロジェクト単位の）keyつきウィジェットは、前のプロジェクトの選択状態が
    # 残って新しいプロジェクトの値を上書きしてしまうため、ここで破棄して新しい値から描画し直させる
    for key in [
        k for k in st.session_state.keys()
        if str(k).startswith(("bgm_section_", "meta_", "motion_", "thumb_")) or k in ("bgm_project_select", "background_blur", "speech_speed", "reading_dict_editor", "show_chapter_label",
                "board_pause")
    ]:
        del st.session_state[key]
