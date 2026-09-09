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


def set_project(project: Project) -> None:
    """プロジェクト全体を丸ごと置き換える（プロジェクトファイルの読み込み時に使用）。"""
    st.session_state[PROJECT_KEY] = project
