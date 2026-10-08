"""
プレビュー・書き出しセクション。

「動画を生成する」ボタンから video_builder.build_video() を呼び出し、
進捗をリアルタイムに表示する。VOICEVOX未起動などの想定内のエラーは
分かりやすい日本語メッセージに変換してUI上に表示し、アプリ自体は
クラッシュさせない（プログラミング知識のないユーザーへの配布を想定）。
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.services import video_metadata
from src.services.video_builder import DEFAULT_SPEED_PRESET, VideoBuildError, build_video
from src.services.voicevox_client import VoicevoxConnectionError, VoicevoxSynthesisError
from src.state import get_project

_LAST_EXPORT_KEY = "_last_export"  # 最後に書き出した動画 {"names": [本編・ショートの名前], "items": [{"label", "path", "warnings", "notes"}]}

_SPEED_PRESET_OPTIONS: dict[str, str] = {
    "fast": "⚡ 高速優先（下書き確認向け。ファイルサイズは大きめ）",
    "balanced": "⚖️ バランス（既定）",
    "quality": "🎬 高画質優先（時間がかかります）",
}


SPEED_PRESET_OPTIONS = _SPEED_PRESET_OPTIONS
_JUST_MADE_KEY = "_export_just_made"


def render_speed_setting() -> str:
    """書き出し速度の設定（上部のバーの ⚙ の中に出す）。"""
    preset_keys = list(_SPEED_PRESET_OPTIONS.keys())
    st.session_state.setdefault("export_speed_preset", DEFAULT_SPEED_PRESET)
    return st.selectbox(
        "書き出し速度", options=preset_keys, format_func=lambda k: _SPEED_PRESET_OPTIONS[k], key="export_speed_preset",
        help="内容を素早く確認したいときは「高速優先」、公開する最終版は「高画質優先」がおすすめです。",
    )


def run_generation(project, speed_preset: str = DEFAULT_SPEED_PRESET) -> None:
    """動画を書き出す（上部のバーの「🚀 動画を作る」から呼ぶ）。本編と紹介ショートの両方を書き出す。"""
    from src.ui.book_mode import export_targets  # book_mode がこのモジュールを読み込むため、ここで読み込む

    main, with_short = export_targets(project)
    _run_generation(main, with_short, speed_preset)


def render_last_export(project) -> None:
    """最後に書き出した動画（このプロジェクトのもの）を、上部のバーのすぐ下に折りたたんで出す。"""
    last = st.session_state.get(_LAST_EXPORT_KEY)
    if not last or project.name not in last.get("names", []):
        return
    paths = [Path(item["path"]) for item in last["items"] if Path(item["path"]).exists()]
    if not paths:
        return
    just_made = st.session_state.pop(_JUST_MADE_KEY, False)
    with st.expander(f"🎬 できた動画: {'、'.join(p.name for p in paths)}", expanded=just_made):
        _render_last_export(project)


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

    from src.ui.book_mode import export_targets  # book_mode がこのモジュールを読み込むため、ここで読み込む

    main, with_short = export_targets(project)
    kind = "本編＋紹介ショート" if with_short else ("ショート" if video_metadata.is_short(main) else "本編")
    if st.button(f"🚀 動画を生成する（{kind}）", type="primary", width="stretch", key="export_generate"):
        _run_generation(main, with_short, speed_preset)
    _render_last_export(project)


def _export_label(project) -> str:
    return "本編紹介ショート" if project.promo_of else ("ショート" if video_metadata.is_short(project) else "本編")


def _render_last_export(project) -> None:
    """最後に書き出した動画（このプロジェクトのもの。本編と紹介ショートの両方）を表示する。ほかの操作をしても消えない。"""
    last = st.session_state.get(_LAST_EXPORT_KEY)
    if not last or project.name not in last.get("names", []):
        return
    for i, item in enumerate(last["items"]):
        path = Path(item["path"])
        if not path.exists():
            continue
        st.success(f"{item['label']}を生成しました: {path.name}（保存先: {path.parent}）")
        for note in item.get("notes", []):
            st.caption(note)
        if item.get("warnings"):
            with st.expander(f"⚠️ 生成時の注意事項（{len(item['warnings'])}件）"):
                for w in item["warnings"]:
                    st.warning(w)
        st.video(str(path))
        st.download_button(
            f"⬇️ MP4をダウンロード（{item['label']}）", data=lambda p=path: p.read_bytes(), file_name=path.name,
            mime="video/mp4", width="stretch", key=f"export_download_{i}",
        )


def _run_generation(main, with_short: bool, speed_preset: str = DEFAULT_SPEED_PRESET) -> None:
    """本編（と紹介ショート）を続けて書き出す。どのタブ・どちらの動画を表示しているかには左右されない。"""
    status_box = st.status("動画を生成しています…", expanded=True)
    used: list[str] = []
    items: list[dict] = []
    targets: list[tuple] = [(main, [])]
    short = None
    failed = False
    while targets:
        project, extra_warnings = targets.pop(0)
        label = _export_label(project)
        status_box.write(f"▶ {label}の書き出しを始めます")
        result = _build_one(project, label, status_box, speed_preset, used)
        if result is None:
            failed = True
            break
        items.append({"label": label, "path": str(result.output_path),
                      "warnings": extra_warnings + list(result.warnings),
                      "notes": _save_post_texts(project, result.output_path)})
        if with_short and project is main:
            try:
                from src.ui.book_mode import promo_for_export

                short, warnings = promo_for_export(main)
            except Exception as e:  # noqa: BLE001 - ショートが作れなくても、書き出した本編はそのまま残す
                st.warning(f"本編紹介ショートを作れなかったため、本編だけを書き出しました（{e}）")
            else:
                targets.append((short, warnings))
    if not failed:
        status_box.update(label="動画の生成が完了しました 🎉", state="complete", expanded=False)
    if items:
        names = [main.name] + ([short.name] if short is not None else [])
        st.session_state[_LAST_EXPORT_KEY] = {"names": names, "items": items}
        st.session_state[_JUST_MADE_KEY] = True


def _build_one(project, label: str, status_box, speed_preset: str, used: list[str]):
    """1本を書き出す（ファイル名は動画のタイトル）。想定内のエラーは分かりやすく表示して None を返す（アプリは落とさない）。"""
    stem = video_metadata.safe_filename(project.video_title or project.name)
    if stem in used:  # 本編とショートのタイトルが同じでも、上書きしない
        stem = video_metadata.safe_filename(f"{stem}_{label}")
    used.append(stem)
    try:
        return build_video(project, progress_callback=status_box.write, speed_preset=speed_preset,
                           output_filename=stem + ".mp4")
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
    except VoicevoxSynthesisError as e:
        status_box.update(label="音声合成に失敗しました", state="error")
        st.error(f"{label}の音声合成でエラーが発生しました: {e}")
    except VideoBuildError as e:
        status_box.update(label="動画生成に失敗しました", state="error")
        st.error(f"{label}の生成中にエラーが発生しました: {e}")
    except Exception as e:  # noqa: BLE001 - 想定外の例外もアプリを落とさず表示する
        status_box.update(label="予期しないエラーが発生しました", state="error")
        st.error("予期しないエラーが発生しました。お手数ですが開発者にお問い合わせください。")
        with st.expander("エラーの詳細（開発者向け）"):
            st.exception(e)
    return None


def _save_post_texts(project, output_path: Path) -> list[str]:
    """投稿用のタイトル・説明文を動画と同じ場所にテキストで保存する（縦動画は TikTok・Instagram 用も）。"""
    if not (project.video_title or project.video_description):
        return []
    saved = []
    for suffix, text in video_metadata.export_files(project).items():
        info_path = output_path.with_name(output_path.stem + suffix + ".txt")
        try:
            info_path.write_text(text, encoding="utf-8")
        except OSError:
            continue
        saved.append(info_path.name)
    return [f"📝 投稿用の文章を保存しました: {'、'.join(saved)}（書籍解説モードで編集できます）"] if saved else []
