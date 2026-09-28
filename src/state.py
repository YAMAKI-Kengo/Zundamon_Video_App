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


# --- 画面上部のタブ（開いているタブだけを描いて軽くする。どのタブを開いているかは覚えておく） ---
TAB_KEY = "main_tab"
TABS = {"book": "📚 書籍解説モード", "english": "🗣 英会話モード", "edit": "🎬 シーン編集・書き出し",
        "analytics": "📈 振り返り"}
_GOTO_TAB_KEY = "_goto_tab"


def go_to_tab(name: str) -> None:
    """次の描画でタブを切り替える（ボタンのコールバックや、処理のあとの st.rerun() の前に呼ぶ）。"""
    st.session_state[_GOTO_TAB_KEY] = TABS[name]


def apply_pending_tab() -> None:
    """go_to_tab() で予約したタブを開く（タブを描く前に呼ぶ）。"""
    target = st.session_state.pop(_GOTO_TAB_KEY, None)
    if target:
        st.session_state[TAB_KEY] = target


# 開いていないタブの入力欄は描かれないため、そのままだとStreamlitが入力内容を捨ててしまう。
# 台本の設定・貼り付けた台本・分析結果などの入力内容は、タブを行き来しても残す
_KEEP_PREFIXES = ("ai_", "book_script_", "en_", "promo_", "meta_", "thumb_", "export_", "scene_editor_", "analytics_")
_KEEP_VALUE_TYPES = {
    "bool_value", "int_value", "double_value", "string_value",
    "string_array_value", "int_array_value", "double_array_value",
}


def keep_widget_values() -> None:
    """開いていないタブの入力欄の値を残す（文字・数値・選択・チェックの入力欄だけ。ボタンやファイルは対象外）。"""
    try:
        from streamlit.runtime.state import get_session_state

        state = get_session_state()._state
        mapper = state._key_id_mapper
        metadata = state._new_widget_state.widget_metadata
    except Exception:  # noqa: BLE001 - Streamlitの内部が変わっていたら、何もしない（入力内容が消えるだけ）
        return
    for key in list(st.session_state.keys()):
        if not str(key).startswith(_KEEP_PREFIXES):
            continue
        meta = metadata.get(mapper.get_id_from_key(key, key))
        if meta is None or meta.value_type not in _KEEP_VALUE_TYPES:
            continue
        try:
            st.session_state[key] = st.session_state[key]
        except Exception:  # noqa: BLE001
            pass
