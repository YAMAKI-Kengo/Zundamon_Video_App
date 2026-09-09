"""
メインエリア: シーンの追加・編集・削除・並べ替えUI。

各シーンで入力する項目（要件定義「シーン編集」に対応）:
  - 背景画像 / 話者 / 表情 / 読み上げテキスト / 表示秒数
また、立ち絵は compositor.compose_dual_scene_frame() を使って、実際の動画と同じ
「左:ずんだもん・右:四国めたん常時表示」レイアウトでその場でプレビュー表示する
（話者側のみ口を開けた状態・閉じた状態の両方を確認できる）。

このほか、以下の便利機能もここで提供する:
  - 台本（複数行のテキスト）から一括でシーンを生成（src.services.script_import）
  - 動画全体を生成する前に、そのシーンの声だけをVOICEVOXで試聴するボタン
"""
from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path

import streamlit as st

from src.models import FORMAT_RESOLUTIONS, Project, VideoFormat, Scene
from src.services import script_import, telop, voicevox_client
from src.services.compositor import compose_dual_scene_frame, render_pr_label_overlay
from src.services.voicevox_client import (
    VoicevoxConnectionError,
    VoicevoxSynthesisError,
    estimate_duration,
)
from src.state import get_project
from src.utils.asset_loader import (
    ASSETS_DIR,
    get_available_expressions,
    get_character_display_name,
    get_expression_label,
    is_video_path,
    list_backgrounds,
    list_characters,
    list_content_images,
    list_content_media,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_AUDIO_DIR = PROJECT_ROOT / "tmp" / "preview_audio"

NO_BACKGROUND_LABEL = "(背景なし)"
NO_CONTENT_MEDIA_LABEL = "(なし)"
NO_BEFORE_AFTER_LABEL = "(なし)"
PREVIEW_MAX_DIM = 480  # プレビュー画像の最大辺（px）。動作を軽くするため実際の出力解像度より縮小する

# テロップ1行あたりの文字数の目安（横画面・縦画面それぞれ）。
# 実際に使うフォントで計測するため、フォントが見つからない環境でも落ちないようにしてある。
_TELOP_FONT_PATH = telop.resolve_font_path()
CHARS_PER_LINE_GUIDE: dict[VideoFormat, int] = {
    fmt: telop.recommended_chars_per_line(resolution, _TELOP_FONT_PATH)
    for fmt, resolution in FORMAT_RESOLUTIONS.items()
}


def _preview_resolution(resolution: tuple[int, int]) -> tuple[int, int]:
    """アスペクト比を保ったまま、長辺が PREVIEW_MAX_DIM 以下になる解像度を返す。"""
    width, height = resolution
    scale = PREVIEW_MAX_DIM / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _save_uploaded_background(uploaded_file, scene_id: str) -> Path:
    bg_dir = ASSETS_DIR / "backgrounds"
    bg_dir.mkdir(parents=True, exist_ok=True)
    save_path = bg_dir / f"uploaded_{scene_id}_{uploaded_file.name}"
    with open(save_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return save_path


def _save_uploaded_content_media(uploaded_file, scene_id: str) -> Path:
    media_dir = ASSETS_DIR / "content_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    save_path = media_dir / f"uploaded_{scene_id}_{uploaded_file.name}"
    with open(save_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return save_path


def _save_uploaded_before_after_image(uploaded_file, scene_id: str, slot: str) -> Path:
    """ビフォーアフター画像（beforeまたはafter）を保存する。

    通常の資料メディアと同じ assets/content_media/ フォルダに保存する
    （list_content_images() で画像のみに絞って選択肢に出す）。
    """
    media_dir = ASSETS_DIR / "content_media"
    media_dir.mkdir(parents=True, exist_ok=True)
    save_path = media_dir / f"uploaded_{scene_id}_{slot}_{uploaded_file.name}"
    with open(save_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return save_path


def _render_voice_preview(scene: Scene, project: Project) -> None:
    """動画全体を生成する前に、そのシーンの声だけをVOICEVOXで試聴できるようにする。"""
    if not scene.text or not scene.text.strip():
        st.info("読み上げテキストが空のため、試聴できません。")
        return

    with st.spinner("VOICEVOXで音声を生成しています…"):
        try:
            PREVIEW_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            result = voicevox_client.synthesize_voice(
                scene.text,
                scene.speaker,
                output_path=PREVIEW_AUDIO_DIR / f"preview_{scene.id}.wav",
                target_duration=scene.duration,
                reading_dict=project.reading_dict,
            )
        except VoicevoxConnectionError:
            st.error(
                "⚠️ VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。",
                icon="⚠️",
            )
            return
        except VoicevoxSynthesisError as e:
            st.error(f"音声合成に失敗しました: {e}")
            return
        except Exception as e:  # noqa: BLE001 - 想定外の失敗でもアプリを落とさず表示する
            st.error("予期しないエラーが発生しました。")
            with st.expander("エラーの詳細（開発者向け）"):
                st.exception(e)
            return

    if result.audio_path is None:
        st.info("テキストが空のため無音として扱われました。")
        return

    st.audio(str(result.audio_path))
    if abs(result.speed_scale - 1.0) > 0.01:
        st.caption(
            f"※ 表示秒数({scene.duration:.1f}秒)に収めるため、話速をx{result.speed_scale:.2f}に"
            "調整した場合の音声です（実際の動画生成時と同じ調整です）。"
        )


_BULK_IMPORT_FLASH_KEY = "_bulk_import_flash"


def _render_bulk_script_import(project: Project, characters: list[str]) -> None:
    """台本（複数行のテキスト）から、まとめて複数シーンを生成する。"""
    # ボタン押下 → シーン生成 → st.rerun() という流れになるため、生成直後のメッセージは
    # session_stateに一時保存し、rerun後にこの関数の先頭で表示してから消す
    # （そうしないと、st.rerun()で画面が再構築される際にメッセージが一瞬で消えてしまうため）。
    flash = st.session_state.get(_BULK_IMPORT_FLASH_KEY)
    if flash is not None:
        del st.session_state[_BULK_IMPORT_FLASH_KEY]

    with st.expander("📄 台本から一括でシーンを作成", expanded=flash is not None):
        if flash is not None:
            st.success(flash["success"])
            for w in flash["warnings"]:
                st.warning(w)

        st.caption(
            "「ずんだもん: ◯◯」のように行頭に話者名を書くとその話者に切り替わり、"
            "話者名を書かない行は指定があるまで同じ話者が続けて話す扱いになります"
            "（掛け合い台本にしたい場合は、話者が変わる行にだけ「話者名:」を書いてください）。"
            "話者・表情が直前の行と同じ行は新しいシーンにはならず、直前のシーンに改行として"
            "連結されます（話者・表情が変わる行だけが新しいシーンになります）。"
            "話者名は表示名（ずんだもん・四国めたん）のほか、「めたん」「ずんだ」のようなニックネーム"
            "（config/characters.jsonの\"aliases\"で追加可能）でも指定できます。"
            "「ずんだもん(喜び): ◯◯」のように話者名の直後に括弧で表情を書くと、その行の表情も指定できます"
            "（表情名は日本語ラベル・内部キーのどちらでも可）。"
        )
        script_text = st.text_area(
            "台本を貼り付け",
            key="bulk_script_text",
            height=150,
            placeholder=(
                "ずんだもん(喜び): このアプリの使い方を紹介するのだ！\n"
                "四国めたん(困り): えっと、よろしくお願いしますわ。\n"
                "..."
            ),
        )
        uploaded_script = st.file_uploader("または、台本のテキストファイル(.txt)を選択", type=["txt"], key="bulk_script_file")
        if uploaded_script is not None:
            script_text = uploaded_script.getvalue().decode("utf-8", errors="replace")

        col_start, col_replace = st.columns(2)
        start_speaker = col_start.selectbox(
            "最初の話者（話者指定の無い1行目に使う）",
            options=characters,
            format_func=get_character_display_name,
            key="bulk_script_start_speaker",
        )
        replace_existing = col_replace.checkbox(
            "既存のシーンを置き換える（オフの場合は末尾に追加）",
            value=False,
            key="bulk_script_replace",
        )

        if st.button("🪄 シーンを一括生成", use_container_width=True):
            if not script_text or not script_text.strip():
                st.warning("台本のテキストが入力されていません。")
            else:
                default_background = project.scenes[-1].background_path if project.scenes else None
                import_warnings: list[str] = []
                new_scenes = script_import.parse_script(
                    script_text,
                    default_start_speaker=start_speaker,
                    default_background=default_background,
                    warnings=import_warnings,
                )
                if not new_scenes:
                    st.warning("シーンとして読み込める行が見つかりませんでした。")
                else:
                    if replace_existing:
                        project.scenes = new_scenes
                    else:
                        project.scenes.extend(new_scenes)
                    st.session_state[_BULK_IMPORT_FLASH_KEY] = {
                        "success": f"{len(new_scenes)}件のシーンを生成しました。",
                        "warnings": import_warnings,
                    }
                    st.rerun()


def render_scene_editor() -> None:
    project = get_project()
    st.subheader("シーン編集")

    characters = list_characters()
    if not characters:
        st.warning(
            "assets/ 配下にキャラクター素材([表情名]_open.png / [表情名]_close.png)が"
            "見つかりません。現在は動作確認用のダミー素材で表示しています。"
        )
        characters = ["zundamon", "shikoku_metan"]

    bg_options = [NO_BACKGROUND_LABEL] + [str(p) for p in list_backgrounds()]
    content_media_options = [NO_CONTENT_MEDIA_LABEL] + [str(p) for p in list_content_media()]
    before_after_options = [NO_BEFORE_AFTER_LABEL] + [str(p) for p in list_content_images()]

    _render_bulk_script_import(project, characters)
    st.divider()

    for i, scene in enumerate(project.scenes):
        header = f"シーン {i + 1}: {scene.text[:24] or '(未入力)'}"
        with st.expander(header, expanded=True):
            col_form, col_preview = st.columns([2, 1])

            with col_form:
                bg_index = bg_options.index(scene.background_path) if scene.background_path in bg_options else 0
                bg_choice = st.selectbox(
                    "背景画像", options=bg_options, index=bg_index, key=f"bg_{scene.id}"
                )
                scene.background_path = None if bg_choice == NO_BACKGROUND_LABEL else bg_choice
                if is_video_path(scene.background_path):
                    st.caption("🎬 動画背景として扱われます（表示秒数に合わせて自動でループ／トリミングされます）。")
                elif not scene.background_path and project.common_background_path:
                    st.caption("背景未設定のため、サイドバーで設定した共通の背景を使用します。")

                uploaded_bg = st.file_uploader(
                    "背景画像/動画をアップロード（任意）",
                    type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "m4v"],
                    key=f"bg_upload_{scene.id}",
                    help=(
                        "動画ファイルを背景にすると、表示秒数に合わせて自動でループ／トリミングされます"
                        "（動画自体の音声は使われません）。"
                    ),
                )
                if uploaded_bg is not None:
                    saved_path = _save_uploaded_background(uploaded_bg, scene.id)
                    scene.background_path = str(saved_path)

                media_index = (
                    content_media_options.index(scene.content_media_path)
                    if scene.content_media_path in content_media_options
                    else 0
                )
                media_choice = st.selectbox(
                    "資料メディア（写真/動画・画面中央に表示）",
                    options=content_media_options,
                    index=media_index,
                    key=f"media_{scene.id}",
                    help="背景とは別のレイヤーとして、画面上部〜中央にcontainフィットで表示されます。",
                )
                scene.content_media_path = None if media_choice == NO_CONTENT_MEDIA_LABEL else media_choice
                if is_video_path(scene.content_media_path):
                    st.caption("🎬 資料動画として扱われます（表示秒数に合わせて自動でループ／トリミングされます）。")

                uploaded_media = st.file_uploader(
                    "資料メディア（写真/動画）をアップロード（任意）",
                    type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "m4v"],
                    key=f"media_upload_{scene.id}",
                    help="スクリーンショットや資料動画などを、画面中央に見やすく表示したい場合に使用します。",
                )
                if uploaded_media is not None:
                    saved_media_path = _save_uploaded_content_media(uploaded_media, scene.id)
                    scene.content_media_path = str(saved_media_path)

                with st.expander("📸 ビフォーアフター画像（任意）", expanded=bool(scene.before_image_path or scene.after_image_path)):
                    st.caption(
                        "Before/After の2枚の画像を指定すると、画面を左右に分割して同時表示します"
                        "（両方そろっている場合のみ有効になり、上の「資料メディア」より優先されます。"
                        "静止画のみ対応・動画は選べません）。"
                    )
                    col_before, col_after = st.columns(2)

                    with col_before:
                        before_index = (
                            before_after_options.index(scene.before_image_path)
                            if scene.before_image_path in before_after_options
                            else 0
                        )
                        before_choice = st.selectbox(
                            "Before画像", options=before_after_options, index=before_index, key=f"before_{scene.id}"
                        )
                        scene.before_image_path = None if before_choice == NO_BEFORE_AFTER_LABEL else before_choice
                        uploaded_before = st.file_uploader(
                            "Before画像をアップロード", type=["png", "jpg", "jpeg", "webp"], key=f"before_upload_{scene.id}"
                        )
                        if uploaded_before is not None:
                            saved_before_path = _save_uploaded_before_after_image(uploaded_before, scene.id, "before")
                            scene.before_image_path = str(saved_before_path)

                    with col_after:
                        after_index = (
                            before_after_options.index(scene.after_image_path)
                            if scene.after_image_path in before_after_options
                            else 0
                        )
                        after_choice = st.selectbox(
                            "After画像", options=before_after_options, index=after_index, key=f"after_{scene.id}"
                        )
                        scene.after_image_path = None if after_choice == NO_BEFORE_AFTER_LABEL else after_choice
                        uploaded_after = st.file_uploader(
                            "After画像をアップロード", type=["png", "jpg", "jpeg", "webp"], key=f"after_upload_{scene.id}"
                        )
                        if uploaded_after is not None:
                            saved_after_path = _save_uploaded_before_after_image(uploaded_after, scene.id, "after")
                            scene.after_image_path = str(saved_after_path)

                speaker_index = characters.index(scene.speaker) if scene.speaker in characters else 0
                scene.speaker = st.selectbox(
                    "話者",
                    options=characters,
                    format_func=get_character_display_name,
                    index=speaker_index,
                    key=f"speaker_{scene.id}",
                )

                expressions = get_available_expressions(scene.speaker)
                if scene.expression not in expressions:
                    scene.expression = expressions[0]
                scene.expression = st.selectbox(
                    "表情",
                    options=expressions,
                    format_func=lambda e, spk=scene.speaker: get_expression_label(spk, e),
                    index=expressions.index(scene.expression),
                    key=f"expr_{scene.id}",
                )

                scene.text = st.text_area(
                    "読み上げテキスト（テロップにもそのまま使用）",
                    value=scene.text,
                    key=f"text_{scene.id}",
                    height=100,
                    help=(
                        "Enterキーで改行すると、その位置でテロップも改行されます"
                        "（改行しなくても、画面幅に収まらない場合は自動的に折り返されます）。\n"
                        f"1行の文字数の目安: 横画面(1920x1080)は約{CHARS_PER_LINE_GUIDE[VideoFormat.LANDSCAPE]}文字、"
                        f"縦画面(1080x1920)は約{CHARS_PER_LINE_GUIDE[VideoFormat.PORTRAIT]}文字です。"
                    ),
                )

                # 現在のフォーマットの目安文字数を超えている行があれば、事前にやんわり知らせる
                # （自動折り返しされるので生成自体は失敗しないが、狙った位置で改行したい場合の目安になる）
                guide_count = CHARS_PER_LINE_GUIDE[project.format]
                long_lines = [
                    (idx, len(line))
                    for idx, line in enumerate(scene.text.split("\n"), start=1)
                    if len(line) > guide_count
                ]
                if long_lines:
                    detail = "、".join(f"{idx}行目({length}文字)" for idx, length in long_lines)
                    st.caption(
                        f"💡 {detail} が目安（約{guide_count}文字/行）を超えています。"
                        "自動で折り返されますが、狙った位置で改行したい場合はここでEnterを押してください。"
                    )

                suggested = estimate_duration(scene.text)
                scene.duration = st.number_input(
                    "表示秒数（この秒数で強制カットされます）",
                    min_value=0.5,
                    max_value=60.0,
                    value=float(scene.duration),
                    step=0.5,
                    key=f"dur_{scene.id}",
                    help=f"テキストの文字数からの目安: 約{suggested}秒（VOICEVOX連携後は実際の音声長に置き換わります）",
                )

            with col_preview:
                st.caption("プレビュー（2人常時表示レイアウト・話者のみ口パク）")
                preview_res = _preview_resolution(project.resolution)
                effective_background_path = scene.background_path or project.common_background_path
                preview_pr_overlay = (
                    render_pr_label_overlay(preview_res, project.pr_label_text)
                    if project.pr_label_enabled
                    else None
                )
                p1, p2 = st.columns(2)
                p1.image(
                    compose_dual_scene_frame(
                        scene.speaker, scene.expression, False, effective_background_path, preview_res,
                        content_media_path=scene.content_media_path,
                        before_image_path=scene.before_image_path,
                        after_image_path=scene.after_image_path,
                        pr_label_overlay=preview_pr_overlay,
                    ),
                    caption="話者の口:閉",
                    use_container_width=True,
                )
                p2.image(
                    compose_dual_scene_frame(
                        scene.speaker, scene.expression, True, effective_background_path, preview_res,
                        content_media_path=scene.content_media_path,
                        before_image_path=scene.before_image_path,
                        after_image_path=scene.after_image_path,
                        pr_label_overlay=preview_pr_overlay,
                    ),
                    caption="話者の口:開",
                    use_container_width=True,
                )

            btn_cols = st.columns(5)
            preview_voice_clicked = btn_cols[0].button(
                "🔊 音声を試聴", key=f"preview_voice_{scene.id}", use_container_width=True
            )
            if btn_cols[1].button("↑ 上へ", key=f"up_{scene.id}", disabled=(i == 0), use_container_width=True):
                project.move_scene(scene.id, -1)
                st.rerun()
            if btn_cols[2].button(
                "↓ 下へ", key=f"down_{scene.id}", disabled=(i == len(project.scenes) - 1), use_container_width=True
            ):
                project.move_scene(scene.id, 1)
                st.rerun()
            if btn_cols[3].button("複製", key=f"dup_{scene.id}", use_container_width=True):
                new_scene = replace(scene, id=uuid.uuid4().hex[:8])
                project.scenes.insert(i + 1, new_scene)
                st.rerun()
            if btn_cols[4].button("🗑 削除", key=f"del_{scene.id}", use_container_width=True):
                project.remove_scene(scene.id)
                st.rerun()

            if preview_voice_clicked:
                _render_voice_preview(scene, project)

    st.divider()
    if st.button("+ シーンを追加", use_container_width=True):
        project.add_scene(Scene(speaker=characters[0] if characters else "zundamon"))
        st.rerun()
