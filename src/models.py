"""
アプリ全体で使うデータモデル定義。

Project (プロジェクト全体) > Scene (1カット) という階層構造で、
Streamlit の st.session_state にはこの Project オブジェクトを1つだけ保持する想定。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class VideoFormat(str, Enum):
    """出力フォーマット（画面比率）"""
    LANDSCAPE = "landscape"  # 横画面 1920x1080
    PORTRAIT = "portrait"    # 縦画面 1080x1920


# フォーマットごとの解像度 (幅, 高さ)
FORMAT_RESOLUTIONS: dict[VideoFormat, tuple[int, int]] = {
    VideoFormat.LANDSCAPE: (1920, 1080),
    VideoFormat.PORTRAIT: (1080, 1920),
}

FORMAT_LABELS: dict[VideoFormat, str] = {
    VideoFormat.LANDSCAPE: "横画面 (1920x1080)",
    VideoFormat.PORTRAIT: "縦画面 (1080x1920)",
}


@dataclass
class Scene:
    """動画内の1シーン（1カット）を表す。

    要件定義の「シーン編集」に対応:
      - background_path: 背景画像（未設定の場合、Projectの共通背景があればそちらを使う）
      - content_media_path: 資料として画面中央に表示する写真/動画（任意。背景とは独立したレイヤー）
      - before_image_path / after_image_path: ビフォーアフター画像（任意。両方そろっている場合のみ
        画面を左右に分割して表示し、content_media_pathより優先される）
      - speaker / expression: 話者と表情
      - text: 読み上げるテキスト（テロップにもそのまま使う）
      - duration: 表示秒数（この秒数で強制的にカットされる）
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    background_path: Optional[str] = None
    content_media_path: Optional[str] = None
    before_image_path: Optional[str] = None
    after_image_path: Optional[str] = None
    speaker: str = "zundamon"          # "zundamon" | "shikoku_metan"
    expression: str = "normal"
    text: str = ""
    duration: float = 3.0              # 秒

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Scene":
        return cls(**data)


@dataclass
class Project:
    """プロジェクト全体（フォーマット + シーンのリスト + BGM設定）"""
    name: str = "新しい動画プロジェクト"
    format: VideoFormat = VideoFormat.LANDSCAPE
    scenes: list[Scene] = field(default_factory=list)
    bgm_path: Optional[str] = None   # 背景音楽ファイルのパス（未設定ならBGMなし）
    bgm_volume: float = 0.25         # BGMの音量（0.0〜1.0）。ナレーションを聞き取りやすくするため控えめが既定
    common_background_path: Optional[str] = None  # 全シーン共通の背景（画像/動画）。各シーン側で未設定の場合のみ使われる
    pr_label_enabled: bool = False   # PR/広告表記のテロップを動画全体に常時表示するか
    pr_label_text: str = "PR"        # PR/広告表記のテロップに表示する文言
    reading_dict: list[dict] = field(default_factory=list)
    # 読み方辞書（VOICEVOXの誤読修正用）。[{"word": "二人", "reading": "ふたり"}, ...] の形式。
    # テロップの表示テキスト(Scene.text)はそのままに、VOICEVOXへ渡す読み上げテキストの中だけ
    # word→readingの単純な文字列置換を行う（voicevox_client.apply_reading_dict）。

    @property
    def resolution(self) -> tuple[int, int]:
        return FORMAT_RESOLUTIONS[self.format]

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.scenes)

    def add_scene(self, scene: Optional[Scene] = None) -> Scene:
        scene = scene or Scene()
        self.scenes.append(scene)
        return scene

    def remove_scene(self, scene_id: str) -> None:
        self.scenes = [s for s in self.scenes if s.id != scene_id]

    def move_scene(self, scene_id: str, delta: int) -> None:
        """シーンの並び順を delta だけ移動する（-1: 上へ, +1: 下へ）"""
        idx = next((i for i, s in enumerate(self.scenes) if s.id == scene_id), None)
        if idx is None:
            return
        new_idx = idx + delta
        if 0 <= new_idx < len(self.scenes):
            self.scenes[idx], self.scenes[new_idx] = self.scenes[new_idx], self.scenes[idx]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "format": self.format.value,
            "scenes": [s.to_dict() for s in self.scenes],
            "bgm_path": self.bgm_path,
            "bgm_volume": self.bgm_volume,
            "common_background_path": self.common_background_path,
            "pr_label_enabled": self.pr_label_enabled,
            "pr_label_text": self.pr_label_text,
            "reading_dict": self.reading_dict,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(
            name=data.get("name", "新しい動画プロジェクト"),
            format=VideoFormat(data.get("format", VideoFormat.LANDSCAPE.value)),
            scenes=[Scene.from_dict(s) for s in data.get("scenes", [])],
            bgm_path=data.get("bgm_path"),
            bgm_volume=data.get("bgm_volume", 0.25),
            common_background_path=data.get("common_background_path"),
            pr_label_enabled=data.get("pr_label_enabled", False),
            pr_label_text=data.get("pr_label_text", "PR"),
            reading_dict=data.get("reading_dict", []),
        )
