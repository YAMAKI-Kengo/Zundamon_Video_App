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

from src.models import (
    CAMERA_LABELS,
    CHAR_MOTION_LABELS,
    FORMAT_RESOLUTIONS,
    GUEST_CHARACTERS,
    MOOD_LABELS,
    PHASE_LABELS,
    SECTION_LABELS,
    SHAKE_LABELS,
    Project,
    Scene,
    VideoFormat,
)
from src.services import book_script, motion, script_import, slide_renderer, telop, voicevox_client
from src.ui.preview_cache import scene_preview
from src.services.voicevox_client import (
    VoicevoxConnectionError,
    VoicevoxSynthesisError,
    estimate_duration,
)
from src.state import get_project
from src.ui.sidebar import bgm_selectbox, se_selectbox
from src.utils.asset_loader import (
    ASSETS_DIR,
    get_available_expressions,
    get_character_display_name,
    get_expression_label,
    is_video_path,
    background_label,
    list_backgrounds,
    list_characters,
    list_content_media,
    list_illustrations,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_AUDIO_DIR = PROJECT_ROOT / "tmp" / "preview_audio"

NO_BACKGROUND_LABEL = "(背景なし)"
NO_CONTENT_MEDIA_LABEL = "(なし)"
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
                speech_speed=project.speech_speed,
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
    if result.speed_scale - project.speech_speed > 0.01:
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

        if st.button("🪄 シーンを一括生成", width="stretch"):
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


SCENES_PER_PAGE = 5  # シーン編集で一度に表示するシーンの数
_PAGE_KEY = "scene_editor_page"


def _jump_to_scene() -> None:
    number = st.session_state.get("scene_editor_jump")
    if number:
        st.session_state[_PAGE_KEY] = (int(number) - 1) // SCENES_PER_PAGE + 1


def _render_page_selector(project: Project) -> tuple[int, int]:
    """シーン編集のページ切り替え。表示するシーンの範囲 [開始, 終了) を返す。"""
    total = len(project.scenes)
    pages = max(1, -(-total // SCENES_PER_PAGE))
    if st.session_state.get(_PAGE_KEY, 1) > pages:
        st.session_state[_PAGE_KEY] = pages
    if pages > 1:
        col_page, col_jump, col_info = st.columns([3, 1, 2])
        col_page.select_slider(
            "表示するページ", options=list(range(1, pages + 1)), key=_PAGE_KEY,
            format_func=lambda n: f"{(n - 1) * SCENES_PER_PAGE + 1}〜{min(n * SCENES_PER_PAGE, total)}",
        )
        col_jump.number_input(
            "シーン番号へ移動", min_value=1, max_value=total, value=None, step=1,
            key="scene_editor_jump", on_change=_jump_to_scene, placeholder="番号",
        )
        col_info.caption(
            f"全{total}シーン。動作を軽くするため、{SCENES_PER_PAGE}シーンずつ表示しています"
            "（「📚 書籍解説モード」の構成一覧で全体を確認できます）。"
        )
    page = int(st.session_state.get(_PAGE_KEY, 1))
    return (page - 1) * SCENES_PER_PAGE, min(page * SCENES_PER_PAGE, total)


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

    _render_bulk_script_import(project, characters)
    st.divider()

    motion_contexts = motion.build_contexts(project.scenes)
    page_start, page_end = _render_page_selector(project)
    for i, scene in enumerate(project.scenes):
        if not page_start <= i < page_end:
            continue  # 表示中のページ以外のシーンは描かない（シーンが多いと、操作のたびの描き直しが非常に重くなるため）
        header = f"シーン {i + 1}: " + (f"🎬 場面転換「{scene.card_text}」" if scene.card_text
                                          else scene.text[:24] or "(未入力)")
        with st.expander(header, expanded=True):
            col_form, col_preview = st.columns([2, 1])

            with col_form:
                bg_index = bg_options.index(scene.background_path) if scene.background_path in bg_options else 0
                bg_choice = st.selectbox(
                    "背景画像", options=bg_options, index=bg_index, key=f"bg_{scene.id}",
                    format_func=lambda v: v if v == NO_BACKGROUND_LABEL else f"{background_label(v)}（{Path(v).name}）",
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

                with st.expander("📝 重要な表現の解説カード（赤い下線と意味）", expanded=bool(scene.note_text)):
                    scene.note_text = st.text_input(
                        "文", value=scene.note_text, key=f"note_text_{scene.id}",
                        placeholder="例: Can I get a coffee?",
                        help="入力すると、黒板の代わりに解説カードが出ます（イラストがあるシーンはイラストが優先）。",
                    )
                    col_focus, col_meaning = st.columns([1, 2])
                    scene.note_focus = col_focus.text_input(
                        "赤線を引く部分", value=scene.note_focus, key=f"note_focus_{scene.id}", placeholder="例: Can I get",
                    )
                    scene.note_meaning = col_meaning.text_input(
                        "意味・使い方", value=scene.note_meaning, key=f"note_meaning_{scene.id}",
                        placeholder="例: 〜をもらえる？ お店で注文するときの定番",
                    )

                col_mood, col_card = st.columns(2)
                scene.mood = col_mood.selectbox(
                    "背景の雰囲気", options=list(MOOD_LABELS), format_func=MOOD_LABELS.get,
                    index=list(MOOD_LABELS).index(scene.mood) if scene.mood in MOOD_LABELS else 0,
                    key=f"mood_{scene.id}",
                    help="背景だけの色味を変えて、落ち込み・ショック・回想などの雰囲気を出します（キャラクターと黒板はそのまま）。",
                )
                scene.card_text = col_card.text_input(
                    "場面転換テロップ（入れると全画面の文字だけのシーンになります）", value=scene.card_text,
                    key=f"card_{scene.id}", placeholder="例: 3日後…",
                    help="「3日後…」「その夜」など、時間や場面が変わることを伝える画面です（セリフ・キャラクターは出ません）。",
                )

                col_section, col_bgm, col_se = st.columns(3)
                with col_section:
                    section_keys = list(SECTION_LABELS)
                    scene.section = st.selectbox(
                        "場面",
                        options=section_keys,
                        index=section_keys.index(scene.section) if scene.section in section_keys else 0,
                        format_func=lambda k: SECTION_LABELS[k],
                        key=f"section_{scene.id}",
                        help="サイドバーの「場面ごとのBGM」で、場面ごとに流す曲を切り替えられます。",
                    )
                    phases = PHASE_LABELS.get(scene.section, {})
                    if phases:
                        phase_keys = [""] + list(phases)
                        scene.phase = st.selectbox(
                            "段階",
                            options=phase_keys,
                            index=phase_keys.index(scene.phase) if scene.phase in phase_keys else 0,
                            format_func=lambda k: phases.get(k, "(指定なし)"),
                            key=f"phase_{scene.id}",
                            help="導入の段階ごとのBGM（サイドバー）を切り替えるのに使います。",
                        )
                    else:
                        scene.phase = ""
                with col_bgm:
                    scene.bgm_path = bgm_selectbox(
                        "このシーンのBGM", scene.bgm_path, key=f"scene_bgm_{scene.id}",
                        inherit_label="(場面・全体の設定に従う)",
                    )
                    resolved_bgm = project.resolve_bgm_path(scene)
                    st.caption(f"→ 流れる曲: {Path(resolved_bgm).stem if resolved_bgm else 'なし'}")
                with col_se:
                    scene.se_path = se_selectbox("効果音（セリフの頭で鳴る）", scene.se_path, key=f"se_{scene.id}")
                    if scene.se_path and Path(scene.se_path).exists():
                        st.audio(scene.se_path)

                with st.expander("🖼 イラスト（黒板より優先して画面中央に表示・任意）", expanded=bool(scene.illustration_path)):
                    illust_options = [None] + [str(p) for p in list_illustrations()]
                    if scene.illustration_path not in illust_options:
                        illust_options.append(scene.illustration_path)
                    col_pick, col_thumb = st.columns([3, 1])
                    scene.illustration_path = col_pick.selectbox(
                        "イラスト", options=illust_options, index=illust_options.index(scene.illustration_path),
                        format_func=lambda v: "（なし）" if v is None else Path(v).stem, key=f"illust_{scene.id}",
                        help="assets/illustrations/ の画像から選びます（サイドバーの「🖼 イラスト素材」で追加できます）。",
                    )
                    scene.illustration_caption = col_pick.text_input(
                        "イラストの下に添える説明（12字程度）", value=scene.illustration_caption,
                        key=f"illust_caption_{scene.id}", placeholder="例: 寝る90分前にお風呂",
                    )
                    if scene.illustration_path and Path(scene.illustration_path).exists():
                        col_thumb.image(scene.illustration_path, width="stretch")
                    elif scene.illustration_request or scene.illustration_name:
                        st.caption(
                            f"💡 欲しいイラスト: {scene.illustration_request or '（説明なし）'}"
                            f"　→ 「{scene.illustration_name}.png」の名前で assets/illustrations/ に置くと自動で表示されます。"
                        )

                with st.expander("🧑‍🏫 スライド（黒板・任意）", expanded=scene.has_slide):
                    st.caption(
                        "見出し・箇条書きを入力すると、黒板風のスライド画像を自動生成して画面中央に表示します"
                        "（上の「資料メディア」より優先されます）。箇条書き中の **言葉** は色を変えて強調されます。"
                    )
                    scene.slide_title = st.text_input(
                        "見出し", value=scene.slide_title, key=f"slide_title_{scene.id}",
                        placeholder="例: 第1章 睡眠の質は「最初の90分」で決まる",
                    )
                    bullets_text = st.text_area(
                        "箇条書き（1行 = 1項目）",
                        value="\n".join(scene.slide_bullets),
                        key=f"slide_bullets_{scene.id}",
                        height=110,
                        placeholder="眠り始めの90分が**最も深い睡眠**になる\n就寝90分前に入浴する",
                    )
                    scene.slide_bullets = [line for line in bullets_text.split("\n") if line.strip()]
                    col_show, col_num = st.columns(2)
                    scene.show_board = col_show.checkbox(
                        "このシーンで黒板を画面に出す", value=scene.show_board, key=f"show_board_{scene.id}",
                        help="オフにすると2人の会話だけの画面になります（見出しは左上の目次ラベルに使われます）。",
                    )
                    scene.slide_numbered = col_num.checkbox(
                        "箇条書きを 1. 2. 3. の番号付きにする", value=scene.slide_numbered,
                        key=f"slide_numbered_{scene.id}",
                        help="手順・順番・ランキングなどは、番号付きの方が見やすくなります。",
                    )
                    bullet_total = len(slide_renderer.normalize_bullets(scene.slide_bullets))
                    if bullet_total > 1:
                        reveal_options = [None] + list(range(1, bullet_total))
                        if scene.slide_reveal not in reveal_options:
                            scene.slide_reveal = None
                        scene.slide_reveal = st.selectbox(
                            "このシーンで見せる箇条書き", options=reveal_options,
                            index=reveal_options.index(scene.slide_reveal),
                            format_func=lambda n: "すべて" if n is None else f"{n}行目まで",
                            key=f"slide_reveal_{scene.id}",
                            help="前のシーンより行が増えると、増えた行が左から書かれていくように表示されます。",
                        )

                if project.source_kind == "english" or scene.lang == "en" or scene.reading or scene.audio_id:
                    with st.expander("🗣 英会話用（英語のセリフ・読み・ネイティブ音声）",
                                     expanded=scene.lang == "en" or bool(scene.reading)):
                        col_lang, col_silent = st.columns(2)
                        scene.lang = col_lang.selectbox(
                            "セリフの言語", ["ja", "en"], index=1 if scene.lang == "en" else 0,
                            format_func={"ja": "日本語", "en": "英語"}.get, key=f"lang_{scene.id}",
                        )
                        scene.silent = col_silent.checkbox(
                            "音声なし（リピート・回答の間）", value=scene.silent, key=f"silent_{scene.id}",
                        )
                        scene.translation = st.text_input(
                            "字幕の下に出す訳", value=scene.translation, key=f"translation_{scene.id}",
                        )
                        scene.reading = st.text_input(
                            "VOICEVOXに読ませる文（カタカナ英語など。空ならセリフのまま）", value=scene.reading,
                            key=f"reading_{scene.id}",
                        )
                        if scene.audio_id:
                            st.caption(
                                f"ネイティブ音声: `{scene.audio_id}` → "
                                + (f"✅ {Path(scene.voice_path).name}" if scene.voice_path else
                                   "⬜ まだありません（英会話モードの「③ ネイティブ音声」を参照）")
                            )
                show_cols = st.columns(len(characters))
                hidden, guests = [], []
                for col, char_key in zip(show_cols, characters):
                    is_guest = char_key in GUEST_CHARACTERS
                    shown = col.checkbox(
                        f"{get_character_display_name(char_key)}を表示" + ("（ゲスト）" if is_guest else ""),
                        value=char_key not in scene.render_hidden,
                        key=f"show_{char_key}_{scene.id}",
                        help="ゲストは、話すシーンか、ここで表示にしたシーンだけ画面に出ます。" if is_guest else None,
                    )
                    if is_guest:
                        if shown:
                            guests.append(char_key)
                    elif not shown:
                        hidden.append(char_key)
                scene.hidden_characters = hidden
                scene.guests = guests
                if scene.speaker in hidden:
                    st.caption("💡 話者を非表示にしているため、このシーンは声だけ（ナレーション）になります。")

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

                partner_options = [None] + [
                    e for e in dict.fromkeys(
                        e for c in characters if c != scene.speaker for e in get_available_expressions(c)
                    )
                ]
                if scene.partner_expression not in partner_options:
                    partner_options.append(scene.partner_expression)
                partner = next((c for c in characters if c != scene.speaker), scene.speaker)
                scene.partner_expression = st.selectbox(
                    "聞き役（話していない方）の表情",
                    options=partner_options,
                    index=partner_options.index(scene.partner_expression),
                    format_func=lambda e, c=partner: "（待機表情）" if e is None else get_expression_label(c, e),
                    key=f"partner_expr_{scene.id}",
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

                scene.show_telop = st.checkbox(
                    "字幕（テロップ）を表示する", value=scene.show_telop, key=f"show_telop_{scene.id}",
                    help="オフにすると、読み上げはしますが画面下の字幕は出しません。",
                )
                scene.headline = st.text_area(
                    "画面上部に大きく表示する文字（任意・改行で複数行）", value=scene.headline,
                    key=f"headline_{scene.id}", height=68,
                    placeholder="例: ご視聴ありがとうございました！",
                )

                with st.expander("🎥 動き（カメラ・揺れ・キャラクター）"):
                    col_cam, col_shake, col_motion = st.columns(3)
                    scene.camera = col_cam.selectbox(
                        "カメラ", options=list(CAMERA_LABELS), index=list(CAMERA_LABELS).index(scene.camera)
                        if scene.camera in CAMERA_LABELS else 0, format_func=CAMERA_LABELS.get, key=f"camera_{scene.id}",
                    )
                    scene.shake = col_shake.selectbox(
                        "画面の揺れ", options=list(SHAKE_LABELS), index=list(SHAKE_LABELS).index(scene.shake)
                        if scene.shake in SHAKE_LABELS else 0, format_func=SHAKE_LABELS.get, key=f"shake_{scene.id}",
                    )
                    scene.char_motion = col_motion.selectbox(
                        "話者の動き", options=list(CHAR_MOTION_LABELS),
                        index=list(CHAR_MOTION_LABELS).index(scene.char_motion)
                        if scene.char_motion in CHAR_MOTION_LABELS else 0,
                        format_func=CHAR_MOTION_LABELS.get, key=f"char_motion_{scene.id}",
                    )
                    effects = motion.describe_motion(project, scene, motion_contexts[i])
                    st.caption("このシーンの動き: " + ("　".join(effects) if effects else "なし（静止）"))

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
                effective_background_path = book_script.effective_background_path(project, scene)
                preview_pr_text = project.pr_label_text if project.pr_label_enabled else None
                try:
                    # スライドは本番と同じ解像度で生成（キャッシュを共有）し、プレビュー側で縮小表示する
                    preview_media_path = slide_renderer.resolve_scene_content_media(scene, project.resolution)
                except Exception as e:  # noqa: BLE001 - プレビューの失敗で編集画面全体を落とさない
                    st.warning(f"スライドの生成に失敗しました（{e}）")
                    preview_media_path = scene.content_media_path
                p1, p2 = st.columns(2)
                for col, mouth_open, caption in ((p1, False, "話者の口:閉"), (p2, True, "話者の口:開")):
                    col.image(
                        scene_preview(
                            scene.speaker, scene.expression, mouth_open, effective_background_path, preview_res,
                            content_media_path=preview_media_path,
                            pr_label_text=preview_pr_text,
                            hidden_characters=scene.render_hidden,
                            partner_expression=scene.partner_expression,
                            background_blur=project.background_blur,
                            mood=scene.mood,
                            card_text=scene.card_text,
                        ),
                        caption=caption,
                        width="stretch",
                    )

            btn_cols = st.columns(5)
            preview_voice_clicked = btn_cols[0].button(
                "🔊 音声を試聴", key=f"preview_voice_{scene.id}", width="stretch"
            )
            if btn_cols[1].button("↑ 上へ", key=f"up_{scene.id}", disabled=(i == 0), width="stretch"):
                project.move_scene(scene.id, -1)
                st.rerun()
            if btn_cols[2].button(
                "↓ 下へ", key=f"down_{scene.id}", disabled=(i == len(project.scenes) - 1), width="stretch"
            ):
                project.move_scene(scene.id, 1)
                st.rerun()
            if btn_cols[3].button("複製", key=f"dup_{scene.id}", width="stretch"):
                new_scene = replace(scene, id=uuid.uuid4().hex[:8])
                project.scenes.insert(i + 1, new_scene)
                st.rerun()
            if btn_cols[4].button("🗑 削除", key=f"del_{scene.id}", width="stretch"):
                project.remove_scene(scene.id)
                st.rerun()

            if preview_voice_clicked:
                _render_voice_preview(scene, project)

    st.divider()
    if st.button("+ シーンを追加", width="stretch"):
        project.add_scene(Scene(speaker=characters[0] if characters else "zundamon"))
        st.rerun()
