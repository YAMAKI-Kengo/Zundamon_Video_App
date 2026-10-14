"""
サイドバー: プロジェクト名 / フォーマット選択 / BGM設定 / プロジェクトの保存・読み込み / シーン一覧サマリ
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import streamlit as st

from src.models import (
    FORMAT_LABELS,
    LESSON_SECTIONS,
    NO_BGM,
    PHASE_LABELS,
    SECTION_LABELS,
    TRANSITION_LABELS,
    Project,
    VideoFormat,
)
from src.state import TAB_KEY, TABS, get_project, set_project
from src.services import bgm_library, video_metadata
from src.utils.asset_loader import ASSETS_DIR, is_video_path, list_bgm, list_se

BGM_DIR = ASSETS_DIR / "bgm"
SE_DIR = ASSETS_DIR / "se"
BACKGROUND_DIR = ASSETS_DIR / "backgrounds"
_LAST_BGM_UPLOAD_KEY = "_last_bgm_upload_identity"
_LAST_COMMON_BG_UPLOAD_KEY = "_last_common_bg_upload_identity"


def _save_uploaded_bgm(uploaded_file) -> str:
    BGM_DIR.mkdir(parents=True, exist_ok=True)
    save_path = BGM_DIR / uploaded_file.name
    with open(save_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return str(save_path)


def bgm_selectbox(label: str, current: Optional[str], key: str, inherit_label: Optional[str], help: str = "") -> Optional[str]:
    """assets/bgm/ の曲から1つ選ぶセレクトボックス。

    inherit_label を渡すと先頭に「上位の設定に従う（=None）」の選択肢を出し、
    続けて「BGMなし（=NO_BGM）」の選択肢を出す。inherit_label=None（プロジェクト全体のBGM）の
    場合は「BGMなし（=None）」だけを出す。
    """
    options: list[Optional[str]] = []
    labels: dict[Optional[str], str] = {}
    if inherit_label is not None:
        options.append(None)
        labels[None] = inherit_label
        options.append(NO_BGM)
        labels[NO_BGM] = "BGMなし"
    else:
        options.append(None)
        labels[None] = "BGMなし"
    for path in list_bgm():
        options.append(str(path))
        labels[str(path)] = _bgm_label(path)
    if current not in options:  # assets/bgm/ 以外の場所にある曲が設定済みの場合も選択肢に残す
        options.append(current)
        labels[current] = f"{Path(current).stem}"
    return st.selectbox(
        label, options=options, index=options.index(current), format_func=lambda v: labels[v], key=key, help=help or None
    )


def _bgm_label(path: Path) -> str:
    """選択肢の表示「〔曲の気分〕曲名」（気分のフォルダの外の曲は曲名だけ）。"""
    track = bgm_library.track_for_path(path)
    mood = bgm_library.FOLDERS.get(track.mood, "")
    return f"〔{mood}〕{track.title}" if mood else track.title


def _render_mood_bgm(project: Project) -> None:
    """場面の種類ごとに流す曲（自動で選んだ曲を利用者が変えられる）。"""
    try:
        added = bgm_library.refresh_library()
    except Exception:  # noqa: BLE001 - 登録できなくても画面は出す
        added = []
    if added:
        st.caption(f"新しいBGMを{len(added)}曲登録しました（作者名は assets/bgm/_library.json に記入してください）。")
    st.markdown("**場面の種類ごとのBGM（自動選択）**")
    st.caption(
        "台本に書かれた場面の種類（ふだんの会話・調子に乗る・ピンチ・トホホ・まとめなど）ごとに、その場面に一番合う曲を"
        "全部の曲から選び、場面が変わったところで切り替えます。曲を変えたい種類だけ選び直してください。"
    )
    if not bgm_library.uses_moods(project) and project.scenes:
        st.caption("今の台本には場面の種類の指定がないため、場面・表情・効果音から推定して流します"
                   "（下の「場面ごとのBGM」を設定した場面は、その曲が流れます）。")
    assigned: dict = {}
    if project.scenes:
        durations = [s.duration for s in project.scenes]
        assigned = bgm_library.assign_tracks(project, bgm_library.effective_moods(project, durations), durations)
    for mood in bgm_library.MOODS:
        tracks = bgm_library.candidates(mood)
        if not tracks:
            continue
        auto = assigned.get(mood) if mood in assigned and not bgm_library.user_choice(project, mood) \
            else bgm_library.best_track(mood)
        options: list[Optional[str]] = [None, NO_BGM] + [str(t.path) for t in tracks]
        labels = {None: f"自動（{bgm_library.track_for_path(auto).title if auto else 'なし'}）", NO_BGM: "BGMなし"}
        labels.update({str(t.path): _bgm_label(t.path) for t in tracks})
        current = project.mood_bgm.get(mood)
        if current not in options:
            current = None
        picked = st.selectbox(
            bgm_library.MOOD_LABELS[mood], options=options, index=options.index(current),
            format_func=lambda v, labels=labels: labels[v], key=f"mood_bgm_{mood}",
            help=bgm_library.MOODS[mood] + "。候補は合う順に並んでいます。",
        )
        if picked is None:
            project.mood_bgm.pop(mood, None)
        else:
            project.mood_bgm[mood] = picked
    busy = [t.title for t in bgm_library.list_tracks() if bgm_library.is_busy(t)]
    if busy:
        st.caption("音がせわしなく長く聞くと疲れるため、自動では選ばない曲: " + "、".join(busy))
    used = video_metadata.used_bgm_paths(project) if project.scenes else []
    if used:
        st.caption("この動画で流れる曲: " + "、".join(bgm_library.track_for_path(p).title for p in used))


def _render_bgm_section(project: Project) -> None:
    with st.sidebar.expander("BGM（背景音楽）", expanded=False):
        _render_mood_bgm(project)
        uploaded_bgm = st.file_uploader(
            "BGMファイルをアップロード（mp3 / wav / m4a）",
            type=["mp3", "wav", "m4a"],
            key="bgm_uploader",
            help="アップロードした曲は assets/bgm/ に保存され、下の各選択肢に追加されます。",
        )
        if uploaded_bgm is not None:
            identity = (uploaded_bgm.name, uploaded_bgm.size)
            if st.session_state.get(_LAST_BGM_UPLOAD_KEY) != identity:
                project.bgm_path = _save_uploaded_bgm(uploaded_bgm)
                st.session_state[_LAST_BGM_UPLOAD_KEY] = identity
                st.session_state.pop("bgm_project_select", None)  # 選択ボックスの表示を新しい曲に合わせる

        project.bgm_path = bgm_selectbox(
            "動画全体のBGM", project.bgm_path, key="bgm_project_select", inherit_label=None,
            help="場面ごと・シーンごとに指定が無いシーンでは、この曲が流れます。",
        )

        st.markdown("**場面ごとのBGM**")
        st.caption(
            "シーン編集で各シーンに設定した「場面」ごとに曲を切り替えます。曲が変わる箇所は自動でクロスフェードします。"
        )
        # 英会話の場面のBGMは、台本を読み込む前（英会話モードを開いた時点）から選べるようにする
        english = project.source_kind == "english" or st.session_state.get(TAB_KEY) == TABS["english"]
        for section, section_label in SECTION_LABELS.items():
            if not section:
                continue
            if section in LESSON_SECTIONS and not english:
                continue  # 英会話モードの場面は、英会話の動画のとき・英会話モードを開いているときだけ出す
            chosen = bgm_selectbox(
                section_label, project.section_bgm.get(section), key=f"bgm_section_{section}",
                inherit_label="(動画全体のBGMと同じ)",
            )
            if chosen is None:
                project.section_bgm.pop(section, None)
            else:
                project.section_bgm[section] = chosen
            if section == "intro":
                # 導入は「決意 → 失敗 → めたん登場」で雰囲気が変わるので、段階ごとに曲を変えられる
                with st.expander("導入の段階ごとのBGM（2〜3曲に切り替え）"):
                    for phase, phase_name in PHASE_LABELS["intro"].items():
                        key = f"{section}:{phase}"
                        picked = bgm_selectbox(
                            phase_name, project.section_bgm.get(key), key=f"bgm_section_{key}",
                            inherit_label="(導入のBGMと同じ)",
                        )
                        if picked is None:
                            project.section_bgm.pop(key, None)
                        else:
                            project.section_bgm[key] = picked

        project.bgm_volume = st.slider(
            "BGMの音量",
            min_value=0.0,
            max_value=1.0,
            value=float(project.bgm_volume),
            step=0.05,
            help="ナレーションが聞き取りにくくならないよう、控えめな値がおすすめです（全ての曲に共通）。"
                 "曲ごとの録音の音量の違いは自動でそろえてから、この音量を掛けます。",
        )


def se_selectbox(label: str, current: Optional[str], key: str) -> Optional[str]:
    """assets/se/ の効果音から1つ選ぶセレクトボックス（先頭は「なし」）。"""
    options: list[Optional[str]] = [None] + [str(p) for p in list_se()]
    if current not in options:  # assets/se/ 以外の場所にある効果音が設定済みの場合も選択肢に残す
        options.append(current)
    return st.selectbox(
        label, options=options, index=options.index(current), key=key,
        format_func=lambda v: "（なし）" if v is None else f"{Path(v).stem}",
    )


def _render_se_section(project: Project) -> None:
    with st.sidebar.expander("効果音", expanded=False):
        st.caption(
            "シーン編集で各シーンに効果音を設定すると、そのセリフの頭で鳴ります。"
            "台本JSONでは各セリフに \"se\": \"きらーん1\" のように名前で指定できます。"
            "効果音ごとの用途（Claudeが台本で選ぶときの目安）と、画面を揺らす効果音は config/se_guide.json で設定できます。"
        )
        uploaded = st.file_uploader(
            "効果音ファイルを追加（mp3 / wav / m4a / ogg）", type=["mp3", "wav", "m4a", "ogg"],
            accept_multiple_files=True, key="se_uploader",
            help="追加した効果音は assets/se/ に保存され、各シーンの選択肢に追加されます。",
        )
        for f in uploaded or []:
            SE_DIR.mkdir(parents=True, exist_ok=True)
            target = SE_DIR / f.name
            if not target.exists():
                target.write_bytes(f.getbuffer())
        names = [p.stem for p in list_se()]
        if names:
            st.caption("使える効果音: " + "、".join(names))
        else:
            st.info("効果音がまだありません。基本セット（ポン・キラーン・ドン など）を作成できます。")
            if st.button("基本の効果音セットを作成", width="stretch", key="se_generate"):
                from scripts import generate_sound_effects

                generate_sound_effects.main()
                st.rerun()
        project.se_volume = st.slider(
            "効果音の音量", min_value=0.0, max_value=1.0, value=float(project.se_volume), step=0.05,
            help="全シーンの効果音に共通の音量です。",
        )


def _render_motion_section(project: Project) -> None:
    with st.sidebar.expander("動き・演出", expanded=False):
        st.caption(
            "各シーンの動きを「自動」にしている場合の演出です。シーン編集の「動き」で1シーンずつ変更もできます。"
            "動きのあるシーンは1コマずつ合成するため、書き出しに時間がかかるようになります。"
        )
        project.auto_camera = st.checkbox(
            "カメラワーク（驚き系の表情で話者にアップ・解説中はゆっくりズームイン）",
            value=project.auto_camera, key="motion_auto_camera",
        )
        project.auto_shake = st.checkbox(
            "画面の揺れ（ショック・驚き系の効果音・ガーン顔のとき）", value=project.auto_shake, key="motion_auto_shake",
        )
        project.char_bob = st.checkbox(
            "話している方がゆらゆら揺れる（呼吸）", value=project.char_bob, key="motion_char_bob",
        )
        project.auto_jump = st.checkbox(
            "表情に合わせて体を動かす（喜びで跳ねる・焦りで走り回る・落ち込みで沈む など）", value=project.auto_jump, key="motion_auto_jump",
        )
        project.char_slide_in = st.checkbox(
            "非表示だったキャラクターが横から入ってくる", value=project.char_slide_in, key="motion_char_slide_in",
        )
        project.auto_stage = st.checkbox(
            "台本に指定が無くても、セリフに合わせてキャラクターを動かす（中央に寄って話す・はしゃいで走り出る・"
            "落ち込んで端で小さくなる・詰め寄る・逃げる など）",
            value=project.auto_stage, key="motion_auto_stage",
        )
        project.auto_fx = st.checkbox(
            "表情に合わせて漫画風の効果を付ける（驚き→「！」、ショック→集中線、怒り→怒りマーク、落ち込み→どんより線 など）",
            value=project.auto_fx, key="motion_auto_fx",
        )
        options = list(TRANSITION_LABELS)
        project.slide_transition = st.selectbox(
            "黒板が切り替わるときのトランジション", options=options,
            index=options.index(project.slide_transition) if project.slide_transition in options else 0,
            format_func=TRANSITION_LABELS.get, key="motion_slide_transition",
        )


def _render_illustration_section() -> None:
    from src.utils.asset_loader import ILLUSTRATION_DIR, list_illustrations

    with st.sidebar.expander("イラスト素材", expanded=False):
        st.caption(
            "assets/illustrations/ に置いたイラストを、Claudeが台本の内容に合わせてシーンごとに選び、"
            "画面中央に大きく表示します。ファイル名で何の絵か分かるようにしておくと選ばれやすくなります"
            "（例: 眠れない男の子.png、お風呂.png）。フォルダ分けしてもかまいません。"
        )
        st.caption(
            "いらすとや等の素材は各サイトの利用規約を確認してください（いらすとやは商用利用の場合、"
            "1つの作品に使えるのは20点までです。台本の自動生成では1本20種類までに抑えています）。"
        )
        uploaded = st.file_uploader(
            "イラストを追加（png / jpg / webp・複数可）", type=["png", "jpg", "jpeg", "webp"],
            accept_multiple_files=True, key="illustration_uploader",
        )
        for f in uploaded or []:
            ILLUSTRATION_DIR.mkdir(parents=True, exist_ok=True)
            target = ILLUSTRATION_DIR / f.name
            if not target.exists():
                target.write_bytes(f.getbuffer())
        st.caption(f"登録済みのイラスト: {len(list_illustrations())}枚")


def _save_uploaded_common_background(uploaded_file) -> str:
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    save_path = BACKGROUND_DIR / f"common_{uploaded_file.name}"
    with open(save_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return str(save_path)


def _render_common_background_section(project: Project) -> None:
    with st.sidebar.expander("共通の背景画像", expanded=False):
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

        read_time = st.slider(
            "黒板のあとの無音の間", min_value=0.0, max_value=2.0, value=float(project.board_pause), step=0.25,
            key="board_pause",
            help="黒板やイラストの説明に新しく文字が出たシーンで、セリフのあとに無音の間を足します"
                 "（文字数に応じて最大3秒×この倍率。0で足さない＝既定）。黒板は、一度出したらそのポイントを"
                 "話し終えるまで出したままになります。",
        )
        if read_time != project.board_pause:
            from src.services import book_script
            from src.state import forget_scene_widgets

            project.board_pause = read_time
            book_script.apply_board_hold(project.scenes, read_time)
            forget_scene_widgets(project.scenes)  # 表示秒数の入力欄に、新しい秒数を出し直す

        project.background_blur = st.slider(
            "背景のぼかし", min_value=0.0, max_value=8.0, value=float(project.background_blur), step=0.5,
            key="background_blur",
            help="背景を少しぼかして、キャラクターや黒板を引き立てます（0でぼかしなし）。全シーンの背景に効きます。",
        )

        if project.common_background_path:
            label = Path(project.common_background_path).name
            if is_video_path(project.common_background_path):
                st.caption(f"設定中の共通背景（動画）: {label}")
            else:
                st.caption(f"設定中の共通背景（画像）: {label}")
            if st.button(":material/delete: 共通の背景を削除", width="stretch"):
                project.common_background_path = None
                st.rerun()
        else:
            st.caption("共通の背景は未設定です。")


def _render_chapter_label_section(project: Project) -> None:
    with st.sidebar.expander("左上の目次ラベル", expanded=False):
        st.caption(
            "いま何の話をしているかが分かるよう、説明文の目次と同じ見出し（「失敗の理由1：〜」など）を"
            "画面左上に常に表示します。ショート動画とエンディングには出ません。"
        )
        project.show_chapter_label = st.checkbox(
            "目次ラベルを表示する", value=project.show_chapter_label, key="show_chapter_label",
        )


def _render_pr_label_section(project: Project) -> None:
    with st.sidebar.expander("PR/広告表記", expanded=False):
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


def _render_reading_check(project: Project) -> None:
    """全セリフのVOICEVOXの読みをClaudeに確認させ、読み間違いを読み方辞書に追加する。"""
    from src.services import book_ai, reading_check, voicevox_client

    if not project.scenes:
        return
    if not st.button("台本の読み間違いを自動チェック", width="stretch", key="reading_check",
                     help="VOICEVOXが実際にどう読むかを調べ、Claudeに読み間違いを探させて読み方辞書に追加します"
                          "（VOICEVOXの起動とClaude APIのキーが必要です。費用は1回数円程度）。"):
        return
    try:
        with st.spinner("VOICEVOXの読みを調べて、Claudeが読み間違いを探しています…"):
            result = reading_check.run_reading_check(project)
    except (voicevox_client.VoicevoxConnectionError, voicevox_client.VoicevoxSynthesisError, book_ai.BookAIError) as e:
        st.error(str(e))
        return
    st.session_state.pop("reading_dict_editor", None)  # 表の表示を更新後の辞書に合わせる
    if result.added:
        st.success(f"{len(result.added)}件の読み方を追加しました: "
                   + "、".join(f"{c['word']}→{c['reading']}" for c in result.added[:10]))
    else:
        st.info(f"読み間違いは見つかりませんでした（{result.checked_lines}セリフを確認）。")
    st.caption(f"約${result.cost_usd:.3f}")


def _render_reading_dict_section(project: Project) -> None:
    with st.sidebar.expander("話す速さ・読み方辞書", expanded=False):
        project.speech_speed = st.slider(
            "話す速さ", min_value=0.8, max_value=1.6, value=float(project.speech_speed), step=0.05,
            key="speech_speed", format="x%.2f",
            help="1.0がVOICEVOXの普通の速さです。解説動画はテンポよく1.2倍前後がおすすめです。"
                 "変えた場合は、台本を取り込み直すと表示秒数もこの速さに合わせて計算し直されます。",
        )
        project.voice_emotion = st.toggle(
            "表情に合わせて声色を変える", value=project.voice_emotion, key="voice_emotion",
            help="驚きは少し高く、呆れ顔は抑揚を抑える…のように、セリフの表情に合わせて声の高さ・抑揚・速さを少しだけ変えます。"
                 "VOICEVOXの別の声（なみだめ等）は感情が激しすぎるため、大泣きの場面だけに使います。",
        )
        _render_reading_check(project)
        st.caption(
            "VOICEVOXが特定の単語を誤読する場合、ここに「単語」と「読み（ひらがな/カタカナ）」を"
            "登録すると、テロップの表示はそのままに、読み上げ音声だけ登録した読みに置き換わります。"
        )
        st.caption(
            "単純な文字列の置換のため、「人」のように1文字だけ登録すると「日本人」などの"
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
            width="stretch",
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
    with st.sidebar.expander("プロジェクトの保存・読み込み", expanded=False):
        st.caption("作業内容（シーン・テキスト・BGM設定など）をファイルに保存し、あとで続きから再開できます。")

        project_json = json.dumps(project.to_dict(), ensure_ascii=False, indent=2)
        st.download_button(
            ":material/save: プロジェクトを保存 (.json)",
            data=project_json.encode("utf-8"),
            file_name=f"{project.name or 'project'}.json",
            mime="application/json",
            width="stretch",
        )

        uploaded_project = st.file_uploader(
            "プロジェクトファイル(.json)を選択",
            type=["json"],
            key="project_json_uploader",
        )
        if uploaded_project is not None:
            if st.button("このプロジェクトを読み込む（現在の内容は上書きされます）", width="stretch"):
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

    st.sidebar.header("設定")
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
    _render_project_io_section(project)

    # 似た設定ごとに見出しを付けてまとめる（探しやすくするため）
    st.sidebar.caption("映像")
    _render_common_background_section(project)
    _render_motion_section(project)
    _render_illustration_section()
    st.sidebar.caption("音声・音楽")
    _render_reading_dict_section(project)
    _render_bgm_section(project)
    _render_se_section(project)
    st.sidebar.caption("画面に出す表示")
    _render_chapter_label_section(project)
    _render_pr_label_section(project)

    st.sidebar.divider()
    with st.sidebar.expander(f"シーン一覧（{len(project.scenes)}件）", expanded=False):
        if not project.scenes:
            st.caption("シーンがありません。「シーン編集」タブで追加できます。")
        for i, scene in enumerate(project.scenes):
            preview_text = scene.text[:14] + ("…" if len(scene.text) > 14 else "")
            st.caption(f"{i + 1}. [{scene.speaker}] {preview_text or '(未入力)'} / {scene.duration:.1f}秒")

    st.sidebar.metric("合計尺（目安）", f"{project.total_duration:.1f} 秒")
