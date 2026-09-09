"""
サイドバー: プロジェクト名 / フォーマット選択 / BGM設定 / プロジェクトの保存・読み込み / シーン一覧サマリ
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from src.models import FORMAT_LABELS, Project, VideoFormat
from src.state import get_project, set_project
from src.utils.asset_loader import ASSETS_DIR, is_video_path

BGM_DIR = ASSETS_DIR / "bgm"
BACKGROUND_DIR = ASSETS_DIR / "backgrounds"
_LAST_BGM_UPLOAD_KEY = "_last_bgm_upload_identity"
_LAST_COMMON_BG_UPLOAD_KEY = "_last_common_bg_upload_identity"


def _save_uploaded_bgm(uploaded_file) -> str:
    BGM_DIR.mkdir(parents=True, exist_ok=True)
    save_path = BGM_DIR / uploaded_file.name
    with open(save_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return str(save_path)


def _render_bgm_section(project: Project) -> None:
    with st.sidebar.expander("🎵 BGM（背景音楽）", expanded=False):
        uploaded_bgm = st.file_uploader(
            "BGMファイルをアップロード（mp3 / wav / m4a）",
            type=["mp3", "wav", "m4a"],
            key="bgm_uploader",
        )
        if uploaded_bgm is not None:
            identity = (uploaded_bgm.name, uploaded_bgm.size)
            if st.session_state.get(_LAST_BGM_UPLOAD_KEY) != identity:
                project.bgm_path = _save_uploaded_bgm(uploaded_bgm)
                st.session_state[_LAST_BGM_UPLOAD_KEY] = identity

        if project.bgm_path:
            st.caption(f"設定中のBGM: {Path(project.bgm_path).name}")
            project.bgm_volume = st.slider(
                "BGMの音量",
                min_value=0.0,
                max_value=1.0,
                value=float(project.bgm_volume),
                step=0.05,
                help="ナレーションが聞き取りにくくならないよう、控えめな値がおすすめです。",
            )
            if st.button("🗑 BGMを削除", use_container_width=True):
                project.bgm_path = None
                st.rerun()
        else:
            st.caption(
                "BGM未設定です。設定すると、動画全体の長さに合わせて自動でループ／トリミングされ、"
                "ナレーションの音量を邪魔しない程度の音量でミックスされます。"
            )


def _save_uploaded_common_background(uploaded_file) -> str:
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    save_path = BACKGROUND_DIR / f"common_{uploaded_file.name}"
    with open(save_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return str(save_path)


def _render_common_background_section(project: Project) -> None:
    with st.sidebar.expander("🖼 共通の背景画像", expanded=False):
        st.caption(
            "全シーンで共通して使う背景（画像/動画）です。シーンごとに個別の背景を"
            "設定した場合は、そのシーンでは個別の背景が優先されます。"
        )
        uploaded_bg = st.file_uploader(
            "共通の背景をアップロード（画像/動画）",
            type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "m4v"],
            key="common_background_uploader",
        )
        if uploaded_bg is not None:
            identity = (uploaded_bg.name, uploaded_bg.size)
            if st.session_state.get(_LAST_COMMON_BG_UPLOAD_KEY) != identity:
                project.common_background_path = _save_uploaded_common_background(uploaded_bg)
                st.session_state[_LAST_COMMON_BG_UPLOAD_KEY] = identity

        if project.common_background_path:
            label = Path(project.common_background_path).name
            if is_video_path(project.common_background_path):
                st.caption(f"設定中の共通背景（動画）: {label}")
            else:
                st.caption(f"設定中の共通背景（画像）: {label}")
            if st.button("🗑 共通の背景を削除", use_container_width=True):
                project.common_background_path = None
                st.rerun()
        else:
            st.caption("共通の背景は未設定です。")


def _render_pr_label_section(project: Project) -> None:
    with st.sidebar.expander("📢 PR/広告表記", expanded=False):
        st.caption(
            "アフィリエイト・PR案件の動画で、広告であることを示すバッジを動画全体に常時表示します"
            "（2023年10月施行のステマ規制対応。画面右上に固定表示され、シーンごとにON/OFFはできません）。"
        )
        project.pr_label_enabled = st.checkbox(
            "PR/広告表記を表示する",
            value=project.pr_label_enabled,
            key="pr_label_enabled",
        )
        if project.pr_label_enabled:
            project.pr_label_text = st.text_input(
                "表示する文言",
                value=project.pr_label_text or "PR",
                key="pr_label_text",
                help='例:「PR」「広告」「〇〇社から商品提供を受けています」など。空欄にすると表示されません。',
            ).strip() or "PR"


def _render_reading_dict_section(project: Project) -> None:
    with st.sidebar.expander("🗣 読み方辞書（誤読の修正）", expanded=False):
        st.caption(
            "VOICEVOXが特定の単語を誤読する場合、ここに「単語」と「読み（ひらがな/カタカナ）」を"
            "登録すると、テロップの表示はそのままに、読み上げ音声だけ登録した読みに置き換わります。"
        )
        st.caption(
            "⚠️ 単純な文字列の置換のため、「人」のように1文字だけ登録すると「日本人」などの"
            "一部まで置き換わってしまうことがあります。実際に誤読された単語・フレーズ単位"
            "（例:「二人」→「ふたり」）で登録するのがおすすめです。"
        )
        edited = st.data_editor(
            project.reading_dict if project.reading_dict else [{"word": "", "reading": ""}],
            column_config={
                "word": st.column_config.TextColumn("単語", help="例: 二人"),
                "reading": st.column_config.TextColumn("読み（ひらがな/カタカナ）", help="例: ふたり"),
            },
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key="reading_dict_editor",
        )
        # 「単語」だけ入力して「読み」をまだ入力していない行など、未完成の行もあえてそのまま
        # 保持する（ここで除外すると、入力途中の行が再描画のたびに消えてしまうため）。
        # 実際の読み上げに使う際は voicevox_client.apply_reading_dict 側で
        # 未入力の行を自動的に無視する。
        project.reading_dict = [
            {"word": str(row.get("word") or ""), "reading": str(row.get("reading") or "")}
            for row in edited
        ]


def _render_project_io_section(project: Project) -> None:
    with st.sidebar.expander("💾 プロジェクトの保存・読み込み", expanded=False):
        st.caption("作業内容（シーン・テキスト・BGM設定など）をファイルに保存し、あとで続きから再開できます。")

        project_json = json.dumps(project.to_dict(), ensure_ascii=False, indent=2)
        st.download_button(
            "💾 プロジェクトを保存 (.json)",
            data=project_json.encode("utf-8"),
            file_name=f"{project.name or 'project'}.json",
            mime="application/json",
            use_container_width=True,
        )

        uploaded_project = st.file_uploader(
            "プロジェクトファイル(.json)を選択",
            type=["json"],
            key="project_json_uploader",
        )
        if uploaded_project is not None:
            if st.button("📂 このプロジェクトを読み込む（現在の内容は上書きされます）", use_container_width=True):
                try:
                    data = json.loads(uploaded_project.getvalue().decode("utf-8"))
                    new_project = Project.from_dict(data)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError, KeyError) as e:
                    st.error(f"プロジェクトファイルの読み込みに失敗しました。ファイルが壊れている可能性があります（{e}）")
                else:
                    set_project(new_project)
                    st.success("プロジェクトを読み込みました。")
                    st.rerun()


def render_sidebar() -> None:
    project = get_project()

    st.sidebar.header("プロジェクト設定")
    project.name = st.sidebar.text_input("プロジェクト名", value=project.name)

    format_options = list(VideoFormat)
    labels = [FORMAT_LABELS[f] for f in format_options]
    current_index = format_options.index(project.format)

    selected_label = st.sidebar.radio(
        "出力フォーマット",
        options=labels,
        index=current_index,
        help="横画面(1920x1080) / 縦画面(1080x1920) を切り替えます",
    )
    project.format = format_options[labels.index(selected_label)]

    st.sidebar.caption(f"解像度: {project.resolution[0]} x {project.resolution[1]}")

    _render_common_background_section(project)
    _render_pr_label_section(project)
    _render_reading_dict_section(project)
    _render_bgm_section(project)
    _render_project_io_section(project)

    st.sidebar.divider()
    st.sidebar.subheader(f"シーン一覧（{len(project.scenes)}件）")
    if not project.scenes:
        st.sidebar.caption("シーンがありません。下の「+ シーンを追加」から作成してください。")
    else:
        for i, scene in enumerate(project.scenes):
            preview_text = scene.text[:14] + ("…" if len(scene.text) > 14 else "")
            st.sidebar.caption(f"{i + 1}. [{scene.speaker}] {preview_text or '(未入力)'} / {scene.duration:.1f}秒")

    st.sidebar.divider()
    st.sidebar.metric("合計尺（目安）", f"{project.total_duration:.1f} 秒")
