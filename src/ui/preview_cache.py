"""
画面プレビュー（背景＋立ち絵＋黒板などを合成した縮小画像）の使い回し。

Streamlit は操作のたびに画面全体を描き直すため、シーンが多い（10分の動画で200シーン前後）と、
プレビュー画像を毎回合成し直すだけで数分かかってしまう。合成に使う値（素材のパスと更新日時を含む）が
同じなら、前に合成した画像をそのまま使う。
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import streamlit as st
from PIL import Image

from src.services import hook_text
from src.services import telop
from src.services.compositor import compose_dual_scene_frame, render_pr_label_overlay, render_transition_card


def _stamp(path: Optional[str]) -> tuple:
    """ファイルのパスと更新日時（同じ名前のまま差し替えた画像も、作り直しの対象にするため）。"""
    if not path:
        return (None, 0.0)
    try:
        return (str(path), Path(path).stat().st_mtime)
    except OSError:
        return (str(path), 0.0)


@st.cache_data(max_entries=600, show_spinner=False)
def _compose_jpeg(
    speaker: str, expression: str, mouth_open: bool, background: tuple, resolution: tuple[int, int],
    media: tuple, pr_label_text: Optional[str], hidden: tuple[str, ...],
    partner_expression: Optional[str], background_blur: float, headline: str, chapter_label: str,
    mood: str, card_text: str, headline_style: str = "",
) -> bytes:
    if card_text.strip():  # 場面転換テロップ（全画面の文字だけ）
        buf = io.BytesIO()
        render_transition_card(background[0], resolution, card_text).convert("RGB").save(buf, format="JPEG", quality=88)
        return buf.getvalue()
    frame = compose_dual_scene_frame(
        speaker, expression, mouth_open, background[0], resolution,
        content_media_path=media[0],
        pr_label_overlay=render_pr_label_overlay(resolution, pr_label_text) if pr_label_text else None,
        hidden_characters=hidden, partner_expression=partner_expression, background_blur=background_blur,
        mood=mood,
    ).convert("RGBA")
    if headline.strip():
        frame = Image.alpha_composite(frame, hook_text.render_hook_image(headline, frame.size)
                                      if headline_style == hook_text.HOOK_STYLE
                                      else telop.render_headline_image(headline, frame.size, telop.resolve_font_path()))
    if chapter_label.strip():
        frame = Image.alpha_composite(frame, telop.render_chapter_label(chapter_label, frame.size, telop.resolve_font_path()))
    buf = io.BytesIO()
    frame.convert("RGB").save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def scene_preview(
    speaker: str, expression: str, mouth_open: bool, background_path: Optional[str], resolution: tuple[int, int],
    content_media_path: Optional[str] = None, pr_label_text: Optional[str] = None,
    hidden_characters=(), partner_expression: Optional[str] = None, background_blur: float = 0.0,
    headline: str = "", chapter_label: str = "", mood: str = "", card_text: str = "", headline_style: str = "",
) -> bytes:
    """compose_dual_scene_frame() と同じ合成（＋画面上部の見出し・左上の目次ラベル）を、同じ内容なら使い回して
    JPEG のバイト列で返す（st.image に渡せる）。"""
    return _compose_jpeg(
        speaker, expression, bool(mouth_open), _stamp(background_path), tuple(resolution),
        _stamp(content_media_path),
        pr_label_text or None, tuple(sorted(hidden_characters or ())), partner_expression, float(background_blur),
        headline or "", chapter_label or "", mood or "", card_text or "", headline_style or "",
    )
