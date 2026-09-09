"""
ずんだもん・四国めたん 解説動画自動生成アプリ

Streamlit エントリーポイント。
画像・読み上げテキスト・表示秒数を入力するだけで、
ずんだもん / 四国めたんの合成音声付き解説動画を自動生成するアプリのUI土台。

実行方法:
    streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from src.state import init_session_state
from src.ui.preview import render_export_section
from src.ui.scene_editor import render_scene_editor
from src.ui.sidebar import render_sidebar
from src.utils import license_check

st.set_page_config(
    page_title="ずんだもん解説動画ジェネレーター",
    page_icon="🎬",
    layout="wide",
)


def _ensure_licensed() -> bool:
    """有料配布（実行ファイル化）する場合のライセンスキー認証ゲート。

    license_check.LICENSE_ENFORCEMENT_ENABLED が False
    （環境変数 ZUNDA_APP_DISABLE_LICENSE=1 を設定した場合）のときは、
    開発・動作確認用にこのチェック自体をスキップする。
    保存済みの有効なキーがあれば自動的に認証済み扱いにし、無ければ
    キー入力画面を表示してアプリ本体（シーン編集等）の描画をブロックする。
    """
    if not license_check.LICENSE_ENFORCEMENT_ENABLED:
        return True

    if not license_check.is_configured():
        # 販売者がまだ tools/generate_keypair.py で鍵ペアを生成し、
        # public_key.pem の中身を license_check.PUBLIC_KEY_PEM に貼り付けていない
        # （＝プレースホルダーのまま）状態。この状態で認証を強制すると、
        # どんなキーを入力しても検証に失敗し、開発者自身も含めて誰もアプリを
        # 使えなくなってしまうため、警告を表示した上で認証をスキップする。
        # 販売用に配布する前に、必ず SELLING_GUIDE.md の手順で鍵ペアを設定すること。
        st.warning(
            "⚠️ ライセンスキーの認証が未設定です（開発モード）。"
            "このまま配布するとライセンスキーによる制限がかかりません。"
            "有料配布する際は SELLING_GUIDE.md の手順に従って鍵ペアを設定してください。",
            icon="⚠️",
        )
        return True

    if st.session_state.get("_license_ok"):
        return True

    saved = license_check.load_saved_license()
    if saved is not None:
        st.session_state["_license_ok"] = True
        st.session_state["_license_holder"] = saved.holder
        return True

    st.header("🔑 ライセンスキーの入力")
    st.write("このアプリのご利用には、購入時に発行されたライセンスキーが必要です。")
    key_input = st.text_input("ライセンスキー", type="password", key="_license_key_input")
    if st.button("認証する", type="primary"):
        info = license_check.verify_license_key(key_input)
        if info is None:
            st.error("ライセンスキーが無効です。購入時にお渡ししたキーを、コピー＆ペーストで正確にご入力ください。")
        else:
            license_check.save_license_key(key_input)
            st.session_state["_license_ok"] = True
            st.session_state["_license_holder"] = info.holder
            st.rerun()
    st.caption("ライセンスキーをお持ちでない場合は、購入元にお問い合わせください。")
    return False


def main() -> None:
    init_session_state()

    if not _ensure_licensed():
        return

    # st.title()だと見出しが大きすぎるため、一段階小さいst.headerを使用する
    st.header("🎬 ずんだもん・四国めたん 解説動画ジェネレーター")
    st.caption("画像・テキスト・表示秒数を入力するだけで、合成音声付きの解説動画を自動生成します。")

    render_sidebar()
    render_scene_editor()
    st.divider()
    render_export_section()


if __name__ == "__main__":
    main()
