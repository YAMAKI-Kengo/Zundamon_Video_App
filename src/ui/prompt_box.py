"""
Claude のチャット画面に貼るプロンプトの表示（長いプロンプトでもスクロールせずにコピーできるように）。

プロンプトは数千〜2万字あるため、そのまま表示すると画面がとても長く重くなる。
高さを決めた小さな枠に入れ、枠の右上のボタンで全文をコピーできるようにする。
"""
from __future__ import annotations

import streamlit as st


def prompt_box(text: str, label: str = "プロンプト", height: int = 140) -> None:
    st.caption(f"📋 {label}（{len(text):,}字）— 枠の右上のボタンで全文をコピーできます")
    st.code(text, language=None, height=height, wrap_lines=True)
