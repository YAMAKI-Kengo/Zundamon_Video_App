"""
「🗣 英会話モード」タブ。

毎日更新する英会話レッスン動画（1週間ごとにテーマを変える）を作る流れを1画面にまとめる:
  1. 1週間の計画: 第N週のテーマ・1〜6日目のフレーズを Claude で作る（lessons/week_XX.json に保存）
  2. 毎日の台本: 計画から、その日（1〜6日目のレッスン / 7日目のまとめ）の台本を作ってシーンにする
  3. ネイティブ音声: お手本の英文の一覧（ファイル名付き）を出し、assets/english_audio/ に置いた音声を紐付ける
  4. 構成の確認（書籍解説モードと同じ一覧）→ 書き出しは「🎬 シーン編集・書き出し」タブ
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from src.ui.prompt_box import prompt_box
from src.services import book_ai, english_lesson, english_tts
from src.state import forget_scene_widgets, get_project, go_to_tab
from src.ui import book_mode

_EN_FLASH_KEY = "_en_import_flash"
DAY_LABELS = {d: f"{d}日目" for d in english_lesson.LESSON_DAYS} | {english_lesson.REVIEW_DAY: "7日目（1週間のまとめ）"}


def _render_week_plan(week: int, level: str, use_api: bool) -> dict | None:
    """① 1週間の計画を作る・表示する。保存済みの計画を返す（無ければ None）。"""
    plan = english_lesson.load_week_plan(week)
    with st.expander(f"① 第{week}週の計画", expanded=plan is None):
        theme_hint = st.text_input(
            "今週のテーマ（任意）", key="en_theme_hint", placeholder="例: カフェで注文する英語 / 空いていればおまかせ",
        )
        previous = english_lesson.previous_themes(week)
        if previous:
            st.caption("これまでのテーマ: " + "、".join(previous))
        if use_api:
            if st.button("🗓 1週間の計画を作る（Claude）", type="primary", key="en_plan_btn", width="stretch"):
                with st.status("1週間の計画を作っています…", expanded=True) as status:
                    try:
                        result = english_lesson.plan_week(week, theme_hint, level, previous, progress=status.write)
                    except book_ai.BookAIError as e:
                        status.update(label="計画の作成に失敗しました", state="error")
                        st.error(str(e))
                        return plan
                    status.update(label="計画ができました", state="complete")
                english_lesson.save_week_plan(result.data)
                st.session_state["_en_plan_cost"] = book_mode._format_cost(result)
                st.session_state.pop("en_plan_json", None)
                st.rerun()
        else:
            st.caption("Claude（claude.ai）のチャットに次のプロンプトを貼り付け、返ってきたJSONを下の欄に貼って保存してください。")
            prompt_box(english_lesson.week_plan_manual_prompt(week, theme_hint, level, previous), "1週間の計画づくりのプロンプト")
        if st.session_state.get("_en_plan_cost"):
            st.caption(f"💰 {st.session_state['_en_plan_cost']}")

        if plan is not None:
            st.markdown(f"**テーマ: {plan.get('theme', '')}**（{plan.get('theme_en', '')}）　{plan.get('goal', '')}")
            for d in plan.get("days", []):
                phrases = " / ".join(f"`{p.get('en', '')}`（{p.get('ja', '')}）" for p in d.get("phrases", []))
                st.markdown(f"- **{d.get('day')}日目 {d.get('title', '')}**：{phrases}")
            st.markdown(f"- **7日目**：1週間のまとめ（来週の予告: {plan.get('next_theme_idea', '')}）")
        edited = st.text_area(
            "計画のJSON（修正・貼り付けして保存できます）", key="en_plan_json", height=160,
            value=json.dumps(plan, ensure_ascii=False, indent=2) if plan else "",
        )
        if st.button("💾 計画を保存", key="en_plan_save"):
            try:
                data = json.loads(edited)
            except json.JSONDecodeError as e:
                st.error(f"JSONの書式が正しくありません（{e.lineno}行目: {e.msg}）")
            else:
                data["week"] = week
                data.setdefault("level", level)
                path = english_lesson.save_week_plan(data)
                st.success(f"保存しました（{path.name}）")
                st.rerun()
    return plan


def _check_readings_after_import(project) -> None:
    """取り込んだ台本の日本語の読み間違いを自動でチェックし、読み方辞書に追加する（VOICEVOXとAPIキーが必要）。"""
    from src.services import reading_check, voicevox_client

    if not st.session_state.get("en_reading_check", True) or not book_ai.api_key_configured():
        return
    try:
        with st.spinner("日本語の読み間違いをチェックしています…"):
            result = reading_check.run_reading_check(project)
    except (voicevox_client.VoicevoxConnectionError, voicevox_client.VoicevoxSynthesisError, book_ai.BookAIError) as e:
        st.session_state["_en_reading_note"] = f"⚠️ 読み間違いのチェックができませんでした（{e}）"
        return
    st.session_state.pop("reading_dict_editor", None)
    st.session_state["_en_reading_note"] = (
        f"🔍 読み方を{len(result.added)}件、辞書に追加しました: "
        + "、".join(f"{c['word']}→{c['reading']}" for c in result.added[:10])
        if result.added else f"🔍 日本語の読み間違いは見つかりませんでした（{result.checked_lines}セリフを確認）"
    ) + f"（約${result.cost_usd:.3f}）"


def _import_lesson(project, data: dict) -> None:
    ok = book_mode._import_script(
        project, json.dumps(data, ensure_ascii=False),
        use_voicevox_timing=st.session_state.get("en_timing", True),
        replace_existing=True, add_ending=st.session_state.get("en_ending", True), reveal_bullets=True,
    )
    if ok:
        english_lesson.link_native_audio(project.scenes, english_lesson.native_gap(project))
        if st.session_state.get("en_auto_tts", True) and english_tts.is_available():
            _generate_missing_audio(project)  # 足りないお手本の音声を、読み上げAIで自動で作る
        _check_readings_after_import(project)
        st.rerun()


def _render_daily_script(project, plan: dict, week: int, level: str, use_api: bool) -> None:
    """② その日の台本を作って、シーンにする。"""
    with st.expander("② 毎日の台本", expanded=True):
        day = st.select_slider("作る日", options=list(DAY_LABELS), format_func=DAY_LABELS.get, key="en_day")
        col_a, col_b, col_c = st.columns(3)
        col_a.checkbox("エンディングを付ける", value=True, key="en_ending")
        col_b.checkbox("日本語のセリフの秒数をVOICEVOXで測る", value=True, key="en_timing",
                       help="英語のセリフの秒数は、ネイティブ音声を紐付けたときに音声の長さに合わせます。")
        if english_tts.is_available():
            st.checkbox("足りないお手本の英語の音声を、読み上げAIで自動で作る", value=True, key="en_auto_tts",
                        help="台本を読み込んだあと、まだ音声が無い英文を読み上げAI（Kokoro）で作って紐付けます。")
        col_c.checkbox("日本語の読み間違いを自動チェック", value=True, key="en_reading_check",
                       help="台本を読み込んだあと、VOICEVOXが実際にどう読むかを調べ、Claudeが読み間違いを探して"
                            "読み方辞書に追加します（VOICEVOXの起動とAPIキーが必要。1回数円程度）。")
        if use_api:
            if st.button(f"✍ {DAY_LABELS[day]}の台本を作る（Claude）", type="primary", key="en_lesson_btn",
                         width="stretch"):
                with st.status("台本を書いています…", expanded=True) as status:
                    try:
                        result = english_lesson.generate_lesson(plan, day, level, project.speech_speed,
                                                               progress=status.write)
                    except book_ai.BookAIError as e:
                        status.update(label="台本の作成に失敗しました", state="error")
                        st.error(str(e))
                        return
                    status.update(label="台本ができました", state="complete")
                st.session_state["_en_lesson_cost"] = book_mode._format_cost(result)
                st.session_state["_en_lesson_json"] = json.dumps(result.data, ensure_ascii=False, indent=2)
                _import_lesson(project, result.data)
            if st.session_state.get("_en_lesson_cost"):
                st.caption(f"💰 {st.session_state['_en_lesson_cost']}")
            if st.session_state.get("_en_lesson_json"):
                st.download_button(
                    "⬇️ 台本JSONを保存", data=st.session_state["_en_lesson_json"], key="en_lesson_download",
                    file_name=f"week{week:02d}_day{day}_台本.json", mime="application/json",
                )
        else:
            st.caption("Claude（claude.ai）のチャットに次のプロンプトを貼り付け、返ってきたJSONを下の欄に貼って読み込んでください。")
            prompt_box(english_lesson.lesson_manual_prompt(plan, day, level, project.speech_speed), "台本づくりのプロンプト")
        pasted = st.text_area("台本JSONを貼り付けて読み込む", key="en_lesson_paste", height=120)
        if st.button("📥 貼り付けた台本を読み込む", key="en_lesson_import", disabled=not pasted.strip()):
            try:
                from src.services.book_script import BookScriptError, load_book_script

                data = load_book_script(pasted)
            except BookScriptError as e:
                st.error(str(e))
                return
            _import_lesson(project, english_lesson.prepare_pasted_lesson(data, plan, day))


TTS_SETTINGS_PATH = english_lesson.ROOT / "config" / "english_tts.json"


def _tts_settings() -> dict:
    """読み上げAIの声・速さの設定（毎日同じ声にするため、ファイルに保存しておく）。"""
    settings = {"voice_a": english_tts.DEFAULT_VOICE_A, "voice_b": english_tts.DEFAULT_VOICE_B,
                "speed": english_tts.DEFAULT_SPEED}
    try:
        settings.update(json.loads(TTS_SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return settings


def _save_tts_settings(settings: dict) -> None:
    TTS_SETTINGS_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def _generate_missing_audio(project, overwrite: bool = False) -> None:
    """足りないネイティブ音声を読み上げAIで作り、シーンに紐付ける。"""
    items = english_lesson.native_audio_items(project.scenes)
    if not overwrite and all(i["ready"] for i in items):
        return
    settings = _tts_settings()
    with st.status("お手本の音声を作っています…（初回は準備に1分ほどかかります）", expanded=True) as status:
        result = english_tts.generate_native_audio(
            items, settings["voice_a"], settings["voice_b"], settings["speed"], overwrite=overwrite,
            progress=status.write,
        )
        status.update(label=f"音声を{len(result.created)}件作りました", state="error" if result.failed else "complete")
    for message in result.failed:
        st.error(message)
    forget_scene_widgets(english_lesson.link_native_audio(project.scenes, english_lesson.native_gap(project)))


def _render_tts_controls(project, items: list[dict]) -> None:
    """読み上げAI（Kokoro）で音声を自動で作るための設定とボタン。"""
    if not english_tts.is_available():
        st.info("読み上げAI（Kokoro）が入っていないため、音声は手作業で用意してください"
                "（コマンドで `pip install kokoro soundfile` を実行すると、ここで自動で作れるようになります）。")
        return
    settings = _tts_settings()
    voice_keys = list(english_tts.VOICES)
    col_a, col_b, col_speed = st.columns([2, 2, 1])
    voice_a = col_a.selectbox("声A（めたん役）", voice_keys, index=voice_keys.index(settings["voice_a"])
                              if settings["voice_a"] in voice_keys else 0, format_func=english_tts.VOICES.get,
                              key="en_tts_voice_a")
    voice_b = col_b.selectbox("声B（会話の相手役）", voice_keys, index=voice_keys.index(settings["voice_b"])
                              if settings["voice_b"] in voice_keys else 0, format_func=english_tts.VOICES.get,
                              key="en_tts_voice_b")
    speed = col_speed.slider("話す速さ", 0.7, 1.2, float(settings["speed"]), 0.05, key="en_tts_speed",
                             help="1.0が標準。学習用に少しゆっくりめ（0.9）が既定です。")
    new_settings = {"voice_a": voice_a, "voice_b": voice_b, "speed": speed}
    if new_settings != {k: settings[k] for k in new_settings}:
        _save_tts_settings(new_settings)

    missing = sum(1 for i in items if not i["ready"])
    col_make, col_redo, col_try = st.columns([2, 2, 1])
    if col_make.button(f"🤖 足りない音声をAIで作る（{missing}件）", type="primary", key="en_tts_make",
                       disabled=missing == 0, width="stretch"):
        _generate_missing_audio(project)
        st.rerun()
    if col_redo.button("♻️ この動画の音声をすべて作り直す", key="en_tts_redo", width="stretch",
                       help="声や速さを変えたときに使います（手作業で置いた音声も、AIの音声で置き換えます）。"):
        _generate_missing_audio(project, overwrite=True)
        st.rerun()
    if col_try.button("▶ 試し聞き", key="en_tts_try", width="stretch"):
        with st.spinner("試し聞きの音声を作っています…"):
            try:
                st.audio(english_tts.sample_audio(voice_a, speed), format="audio/wav")
                st.audio(english_tts.sample_audio(voice_b, speed, "Hi, I'm Alex. Nice to meet you, too."),
                         format="audio/wav")
            except english_tts.EnglishTTSError as e:
                st.error(str(e))
    st.caption("読み上げAI「Kokoro」（Apache 2.0ライセンス・商用利用可）で、このPCの中で音声を作ります（登録・料金なし）。")


def _render_native_audio(project) -> None:
    """③ ネイティブ音声の一覧と紐付け。"""
    items = english_lesson.native_audio_items(project.scenes)
    if not items:
        return
    ready = sum(1 for i in items if i["ready"])
    with st.expander(f"③ ネイティブ音声（{ready}/{len(items)} 用意済み）", expanded=ready < len(items)):
        _render_tts_controls(project, items)
        st.caption(
            f"手作業で用意する場合は、お手本の英文を音声にして、表のファイル名で `{english_lesson.AUDIO_DIR}` に"
            "保存してください（mp3 / wav / m4a など。1文 = 1ファイル）。声A はめたん役（女性）、声B は会話の相手役です。"
            "同じ英文は前の日・前の週と同じファイル名になるので、作り直す必要はありません。"
        )
        st.dataframe(
            [{"": "✅" if i["ready"] else "⬜", "ファイル名": Path(i["path"]).name if i["ready"] else f"{i['id']}.mp3",
              "声": i["voice"], "英文": i["text"]}
             for i in items],
            hide_index=True, width="stretch",
        )
        missing = english_lesson.audio_script_text(items, only_missing=True)
        col_dl, col_link = st.columns(2)
        if missing:
            col_dl.download_button(
                "⬇️ まだ無い音声の一覧（ファイル名・声・英文）", data=missing, key="en_audio_list",
                file_name="native_audio_list.txt", mime="text/plain", width="stretch",
            )
        if col_link.button("🔄 置いた音声を読み込む", key="en_audio_link", width="stretch"):
            linked = english_lesson.link_native_audio(project.scenes, english_lesson.native_gap(project))
            forget_scene_widgets(linked)
            st.toast(f"{len(linked)}シーンに音声を紐付けました。" if linked else "新しく置かれた音声はありませんでした。")
            st.rerun()
        if missing:
            with st.popover("英文だけをコピー"):
                st.code("\n".join(i["text"] for i in items if not i["ready"]), language="text")


def render_english_mode() -> None:
    project = get_project()
    st.caption(
        "毎日更新の英会話レッスン動画を作ります。1週間ごとにテーマを決め、1〜6日目はレッスン"
        "（会話を聞く → フレーズ解説 → リピート → 瞬発トレーニング）、7日目は1週間のまとめです。"
        "お手本の英語はネイティブ音声（自分で用意した音声ファイル）、ずんだもんの英語はカタカナ読みで話します。"
    )
    col_week, col_level, col_mode = st.columns([1, 2, 2])
    week = int(col_week.number_input("第何週", min_value=1, max_value=520, value=int(
        (project.lesson or {}).get("week") or 1), step=1, key="en_week"))
    level = col_level.selectbox("レベル", list(english_lesson.LEVELS), format_func=english_lesson.LEVELS.get,
                                key="en_level")
    use_api = col_mode.radio("作り方", ["Claude APIで自動生成", "手動（チャット画面・API料金なし）"],
                             horizontal=True, key="en_mode").startswith("Claude")
    if use_api and not book_ai.api_key_configured():
        st.warning("Claude APIのキーが設定されていません（`.env` の `ANTHROPIC_API_KEY`）。手動モードなら使えます。")

    plan = _render_week_plan(week, level, use_api)
    if plan is None:
        st.info("まず「① 第N週の計画」で1週間の計画を作ってください。")
        return
    _render_daily_script(project, plan, week, level, use_api)

    flash = st.session_state.pop(_EN_FLASH_KEY, None)
    if flash is not None:
        st.success(flash["success"])
        for w in flash["warnings"]:
            st.warning(w)
    note = st.session_state.pop("_en_reading_note", None)
    if note:
        st.info(note)

    if project.source_kind == "english" and project.scenes:
        _render_native_audio(project)
        st.button("✅ 仕上げ・投稿へ（構成の確認・タイトル・サムネイル）", key="en_go_review", on_click=go_to_tab,
                  args=("review",))
