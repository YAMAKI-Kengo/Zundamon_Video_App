"""
プレビュー・書き出しセクション。

「動画を生成する」ボタンから video_builder.build_video() を呼び出し、
進捗をリアルタイムに表示する。VOICEVOX未起動などの想定内のエラーは
分かりやすい日本語メッセージに変換してUI上に表示し、アプリ自体は
クラッシュさせない（プログラミング知識のないユーザーへの配布を想定）。
"""
from __future__ import annotations

import streamlit as st

from src.services import video_metadata
from src.services.video_builder import DEFAULT_SPEED_PRESET, VideoBuildError, build_video
from src.services.voicevox_client import VoicevoxConnectionError, VoicevoxSynthesisError
from src.state import get_project

_SPEED_PRESET_OPTIONS: dict[str, str] = {
    "fast": "⚡ 高速優先（下書き確認向け。ファイルサイズは大きめ）",
    "balanced": "⚖️ バランス（既定）",
    "quality": "🎬 高画質優先（時間がかかります）",
}


def render_export_section() -> None:
    project = get_project()
    st.subheader("動画生成")

    if not project.scenes:
        st.info("シーンを1つ以上追加してから生成してください。")
        return

    st.write(
        f"シーン数: {len(project.scenes)}　/　"
        f"合計 {project.total_duration:.1f} 秒　/　"
        f"{project.resolution[0]}x{project.resolution[1]}"
    )

    preset_keys = list(_SPEED_PRESET_OPTIONS.keys())
    speed_preset = st.selectbox(
        "書き出し速度",
        options=preset_keys,
        index=preset_keys.index(DEFAULT_SPEED_PRESET),
        format_func=lambda k: _SPEED_PRESET_OPTIONS[k],
        key="export_speed_preset",
        help=(
            "動画のエンコード（書き出し）速度と画質・ファイルサイズのバランスです。"
            "内容を素早く確認したいときは「高速優先」、SNS等に公開する最終版は"
            "「高画質優先」がおすすめです。背景・立ち絵の合成そのものの速度には影響しません"
            "（動画背景/資料動画を使うシーンが多いほど、そちらの合成に時間がかかります）。"
        ),
    )

    if st.button("🚀 動画を生成する", type="primary", use_container_width=True):
        _run_generation(project, speed_preset)


def _run_generation(project, speed_preset: str = DEFAULT_SPEED_PRESET) -> None:
    status_box = st.status("動画を生成しています…", expanded=True)

    def on_progress(message: str) -> None:
        status_box.write(message)

    try:
        result = build_video(project, progress_callback=on_progress, speed_preset=speed_preset)
    except VoicevoxConnectionError:
        status_box.update(label="VOICEVOXに接続できませんでした", state="error")
        st.error(
            "⚠️ VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。",
            icon="⚠️",
        )
        st.caption(
            "VOICEVOXアプリ（またはVOICEVOX ENGINE）を起動した状態で、"
            "もう一度「🚀 動画を生成する」ボタンを押してください。"
        )
        return
    except VoicevoxSynthesisError as e:
        status_box.update(label="音声合成に失敗しました", state="error")
        st.error(f"音声合成でエラーが発生しました: {e}")
        return
    except VideoBuildError as e:
        status_box.update(label="動画生成に失敗しました", state="error")
        st.error(f"動画の生成中にエラーが発生しました: {e}")
        return
    except Exception as e:  # noqa: BLE001 - 想定外の例外もアプリを落とさず表示する
        status_box.update(label="予期しないエラーが発生しました", state="error")
        st.error("予期しないエラーが発生しました。お手数ですが開発者にお問い合わせください。")
        with st.expander("エラーの詳細（開発者向け）"):
            st.exception(e)
        return

    status_box.update(label="動画の生成が完了しました 🎉", state="complete")

    if result.warnings:
        with st.expander(f"⚠️ 生成時の注意事項（{len(result.warnings)}件）", expanded=True):
            for w in result.warnings:
                st.warning(w)

    st.success(f"動画を生成しました: {result.output_path.name}")
    if project.video_title or project.video_description:
        # 投稿用のタイトル・説明文も動画と同じ場所にテキストで保存しておく
        info_path = result.output_path.with_name(result.output_path.stem + "_投稿用.txt")
        try:
            info_path.write_text(video_metadata.export_text(project), encoding="utf-8")
            st.caption(f"📝 タイトル・説明文を {info_path.name} に保存しました（書籍解説モードで編集できます）。")
        except OSError:
            pass
    st.video(str(result.output_path))
    with open(result.output_path, "rb") as f:
        st.download_button(
            "⬇️ MP4をダウンロード",
            data=f.read(),
            file_name=result.output_path.name,
            mime="video/mp4",
            use_container_width=True,
        )
