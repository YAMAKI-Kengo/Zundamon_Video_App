"""
アプリ全体で使うデータモデル定義。

Project (プロジェクト全体) > Scene (1カット) という階層構造で、
Streamlit の st.session_state にはこの Project オブジェクトを1つだけ保持する想定。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields, asdict
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


# 書籍解説動画の「場面（セクション）」。場面ごとにBGMを切り替えるために使う。
# キーは台本JSON・プロジェクトファイルに保存される内部値、値はUI表示用ラベル。
SECTION_LABELS: dict[str, str] = {
    "": "(指定なし)",
    "intro": "導入（悩み・問題提起）",
    "explain": "解説（本の紹介・要点）",
    "summary": "まとめ",
    "ending": "エンディング（ご視聴ありがとう）",
    # 英会話モード（src.services.english_lesson）の場面
    "dialog": "ダイアログ（会話を聞く）",
    "phrase": "フレーズ解説",
    "repeat": "リピート練習",
    "quiz": "瞬発トレーニング",
    "review": "1週間のふりかえり",
}
LESSON_SECTIONS = ("dialog", "phrase", "repeat", "quiz", "review")  # 英会話モードだけで使う場面


# 場面をさらに細かく分けた段階（導入の流れ・解説の前半/後半）。段階ごとにBGMを変えられる
# （Project.section_bgm に "intro:hype" のようなキーで保存する）。
PHASE_LABELS: dict[str, dict[str, str]] = {
    "intro": {"hype": "導入①：決意・ワクワク", "fail": "導入②：失敗", "rescue": "導入③：めたん登場"},
    "explain": {"why": "解説：なぜ失敗したのか", "how": "解説：成功させるには"},
}


def phase_label(section: str, phase: str) -> str:
    return PHASE_LABELS.get(section, {}).get(phase, "")


# 視聴者がリピート・回答する間（台本の "pause"）の見せ方。
#   repeat … 「3・2・1」のカウントダウン（合図の音つき）→「リピート！」と残り時間のバー（音声なし）
#   shadow … 「3・2・1」のカウントダウン → お手本の音声をもう一度流す（視聴者は音声に重ねて言う）
#   think  … 見出し（「考えてみて！」など）と残り秒数のタイマー（音声なし）
PAUSE_STYLE_LABELS: dict[str, str] = {
    "repeat": "リピート（3・2・1 → リピート！）",
    "shadow": "シャドーイング（3・2・1 → お手本の音声をもう一度）",
    "think": "考える・答える（残り秒数のタイマー）",
}
COUNTDOWN_FROM = 3             # カウントダウンの数（3・2・1）
COUNTDOWN_STEP_SECONDS = 0.6   # カウントダウンの1つ分の秒数
COUNTDOWN_SECONDS = COUNTDOWN_FROM * COUNTDOWN_STEP_SECONDS


# シーンの雰囲気（背景だけの色味を変える。src.services.compositor.apply_mood）。
MOOD_LABELS: dict[str, str] = {
    "": "通常",
    "gloomy": "どんより（暗く・青っぽく。落ち込み）",
    "shock": "ガーン（暗く・色あせ。ショック）",
    "dark": "暗く（夜・不安）",
    "sepia": "回想（セピア）",
    "bright": "ぱっと明るく（うれしい・ひらめき）",
}


# 動画の動き（src.services.motion）の選択肢。"auto" はプロジェクトの自動演出の設定に従う。
CAMERA_LABELS: dict[str, str] = {
    "auto": "自動",
    "none": "動かさない",
    "zoom_speaker": "話者にパッとアップ",
    "slow_zoom_speaker": "話者にゆっくりアップ",
    "slow_zoom": "ゆっくりズームイン（全体）",
}
SHAKE_LABELS: dict[str, str] = {"auto": "自動", "on": "揺らす", "off": "揺らさない"}
CHAR_MOTION_LABELS: dict[str, str] = {
    "auto": "自動",
    "none": "動かさない",
    "bob": "ゆらゆら（呼吸）",
    "jump": "ぴょんと跳ねる",
}
TRANSITION_LABELS: dict[str, str] = {
    "slide": "スライド（横に入れ替わる）",
    "fade": "フェード",
    "wipe": "ワイプ（左から書き換わる）",
    "none": "なし（パッと切り替え）",
}

# Scene.bgm_path / Project.section_bgm に入れると「このシーン（場面）はBGMを流さない」を意味する値。
# None（未設定＝上位の設定に従う）と区別するために使う。
NO_BGM = "__none__"


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

    書籍解説動画用の追加項目:
      - section: 場面（SECTION_LABELSのキー）。場面ごとのBGM切り替えに使う
      - bgm_path: このシーンだけ使うBGM（未設定なら場面→プロジェクト全体のBGMの順で決まる）
      - slide_title / slide_bullets: 黒板風スライドの見出し・箇条書き。どちらかが入力されていれば
        slide_renderer で黒板スライド画像を生成し、資料メディアの位置に表示する
        （content_media_path より優先。ビフォーアフターがそろっている場合はそちらが優先）
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
    section: str = ""
    phase: str = ""                   # 場面の中の段階（PHASE_LABELS のキー。導入の決意/失敗/めたん登場など）
    show_board: bool = True           # このシーンで黒板（スライド）を画面に出すか（False なら2人の会話だけ。見出しは目次ラベル用に残す）
    bgm_path: Optional[str] = None
    slide_title: str = ""
    slide_bullets: list[str] = field(default_factory=list)
    se_path: Optional[str] = None      # シーンの頭で鳴らす効果音（未設定なら鳴らさない）
    hidden_characters: list[str] = field(default_factory=list)  # このシーンで画面に表示しないキャラクター
    partner_expression: Optional[str] = None  # 話していない方（聞き役）の表情。未設定なら待機表情
    show_telop: bool = True           # 読み上げテキストを字幕（テロップ）として表示するか
    headline: str = ""                # 画面上部に大きく表示する文字（エンディングの挨拶など。改行可）
    slide_reveal: Optional[int] = None  # 黒板に表示する箇条書きの数（Noneなら全部。1行ずつ書き足す演出用）
    camera: str = "auto"               # カメラワーク（CAMERA_LABELS のキー）
    shake: str = "auto"                # 画面の揺れ（SHAKE_LABELS のキー）
    char_motion: str = "auto"          # 話者の動き（CHAR_MOTION_LABELS のキー）
    show_book_cover: bool = False      # 本の表紙画像（Project.book_cover_path）を画面中央に表示するシーン（本の紹介の場面）
    illustration_path: Optional[str] = None  # 画面中央にカードで表示するイラスト（黒板より優先。assets/illustrations/）
    illustration_caption: str = ""   # イラストの下に添える短い説明（何の話か一目で分かるように）
    # 合うイラストが手元に無かった場合に、Claudeが「こういうイラストが欲しい」と書いたもの。
    # illustration_name の名前で assets/illustrations/ に画像を置くと、そのシーンに表示される（link_requested_illustrations）
    illustration_request: str = ""
    illustration_name: str = ""
    # 英会話モード用（src.services.english_lesson）
    lang: str = "ja"                  # セリフの言語: "ja" / "en"（英語のセリフは字幕を単語単位で折り返す）
    translation: str = ""             # 字幕の下に小さく出す訳（英語のセリフの日本語訳）
    reading: str = ""                 # VOICEVOXに読ませる文（空なら text。ずんだもんの英語をカタカナで読ませる等）
    audio_id: str = ""                # ネイティブ音声のID（assets/english_audio/<ID>.mp3 等を置くと voice_path に紐付く）
    voice_path: Optional[str] = None  # VOICEVOXの代わりに使う録音済みの音声ファイル（ネイティブ音声）
    silent: bool = False              # 音声を鳴らさないシーン（視聴者がリピートする間。字幕・見出しは表示する）
    pause_style: str = ""             # リピート・回答の間の見せ方（PAUSE_STYLE_LABELS のキー。空なら普通のシーン）
    lead_in: float = 0.0              # 声の前に置く秒数（カウントダウンの間。duration に含む）
    # 演出
    mood: str = ""                    # 背景の雰囲気（MOOD_LABELS のキー。落ち込み・回想など）
    card_text: str = ""               # 場面転換テロップ（「3日後…」など）。入っていれば、このシーンは全画面の文字だけを出す
    slide_numbered: bool = False      # 黒板の箇条書きを「・」ではなく 1. 2. 3. の番号付きにする
    # 重要な表現の解説カード（文の大事な部分に赤い下線 → 意味・使い方）。note_text があれば黒板の代わりに出す
    note_text: str = ""               # 文（例: "Can I get a coffee?"）
    note_focus: str = ""              # 赤い下線を引く部分（note_text の中の語句。例: "Can I get"）
    note_meaning: str = ""            # その部分の意味・使い方（例: 「〜をもらえる？ 注文の定番」）
    bullet_ref: int = 0               # このセリフで初めて話す黒板の行の番号（1から。台本の "bullet"。0なら指定なし）
    board_hold: float = 0.0           # 黒板に新しく書いた文字を読む時間として、セリフのあとに足している秒数（duration に含む）

    @property
    def has_slide(self) -> bool:
        return bool(self.slide_title.strip() or any(b.strip() for b in self.slide_bullets))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Scene":
        # 新しいバージョンで追加された項目を持たない古いプロジェクトファイルは既定値で補い、
        # 逆に未知の項目は無視する（バージョン違いのファイルでも読み込めるようにする）
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Project:
    """プロジェクト全体（フォーマット + シーンのリスト + BGM設定）"""
    name: str = "新しい動画プロジェクト"
    format: VideoFormat = VideoFormat.LANDSCAPE
    scenes: list[Scene] = field(default_factory=list)
    bgm_path: Optional[str] = None   # 背景音楽ファイルのパス（未設定ならBGMなし）
    bgm_volume: float = 0.15         # BGMの音量（0.0〜1.0）。ナレーションを聞き取りやすくするため控えめが既定
    se_volume: float = 0.6           # 効果音の音量（0.0〜1.0。全シーン共通）
    common_background_path: Optional[str] = None  # 全シーン共通の背景（画像/動画）。各シーン側で未設定の場合のみ使われる
    pr_label_enabled: bool = False   # PR/広告表記のテロップを動画全体に常時表示するか
    pr_label_text: str = "PR"        # PR/広告表記のテロップに表示する文言
    reading_dict: list[dict] = field(default_factory=list)
    # 読み方辞書（VOICEVOXの誤読修正用）。[{"word": "二人", "reading": "ふたり"}, ...] の形式。
    # テロップの表示テキスト(Scene.text)はそのままに、VOICEVOXへ渡す読み上げテキストの中だけ
    # word→readingの単純な文字列置換を行う（voicevox_client.apply_reading_dict）。
    section_bgm: dict[str, str] = field(default_factory=dict)
    # 場面ごとのBGM。{"intro": "assets/bgm/xxx.mp3", ...} の形式（Scene.sectionのキーに対応）。
    book_title: str = ""  # 書籍解説モードで扱っている本のタイトル（表示・スライド生成用）
    book_author: str = ""  # 本の著者名（説明文・タグに使う）
    book_cover_path: Optional[str] = None  # 本の表紙画像（本を紹介するシーンで画面中央に表示する）
    speech_speed: float = 1.2  # 話す速さ（1.0=VOICEVOXの等速。解説動画はテンポよく1.2倍が既定）
    # 動画の動き（src.services.motion）の自動演出の設定
    auto_camera: bool = True      # 驚き系の表情で話者にアップ・解説中はゆっくりズームイン
    auto_shake: bool = True       # 驚き系の効果音（config/se_guide.json の shake）・ガーン顔で画面を揺らす
    char_bob: bool = True         # 話している方がゆっくり上下に揺れる（呼吸）
    auto_jump: bool = True        # 喜び・驚き系の表情で話者がぴょんと跳ねる
    char_slide_in: bool = True    # 非表示だったキャラクターが画面の横から入ってくる
    slide_transition: str = "slide"  # 黒板の内容が変わるときの切り替え方（TRANSITION_LABELS のキー）
    background_blur: float = 2.0  # 背景のぼかしの強さ（0=なし。1080pでのぼかし半径px。解像度に合わせて換算する）
    # 投稿用のタイトル・説明文・タグ（src.services.video_metadata で自動生成し、UIで編集できる）
    video_title: str = ""
    video_description: str = ""
    video_tags: list[str] = field(default_factory=list)
    video_style: str = "normal"  # 動画の種類: "normal"（通常・横画面） / "short"（ショート・縦画面・約1分）
    title_candidates: list[str] = field(default_factory=list)  # AIが提案したタイトル案（説明文の自動生成でも使う）
    description_lead: str = ""  # 説明文の冒頭2〜3行（AIが書いた、動画の見どころの紹介文）
    hashtags: list[str] = field(default_factory=list)  # 説明文に入れるハッシュタグ（先頭3つがタイトル上に表示される）
    show_chapter_label: bool = True  # 画面左上に、いま話している項目（説明文の目次と同じ見出し）を常に表示する
    board_pause: float = 0.0         # 黒板に新しく文字が書かれたシーンで、セリフのあとに足す無音の間の量（0=足さない。1=文字数に応じて最大3秒）
    # 動画の題材: "book"（本の解説） / "research"（Claudeが調べた論文・記事の解説）
    source_kind: str = "book"  # "book" / "research" / "english"（英会話レッスン）
    thumbnail: dict = field(default_factory=dict)  # サムネイルの指定 {"text", "sub", "layout", "zundamon", "metan", "image"}
    lesson: dict = field(default_factory=dict)  # 英会話モードの情報 {"week", "day", "theme", "level", "phrases": [...]}
    sources: list[dict] = field(default_factory=list)  # 調べた論文・記事の出典 [{"title","publisher","year","url","kind"}]

    def resolve_bgm_path(self, scene: Scene) -> Optional[str]:
        """シーンで流すBGMを「シーン個別 → 場面の段階ごと → 場面ごと → プロジェクト全体」の優先順で決める。

        途中で NO_BGM が指定されていれば、そこで「BGMなし」に確定する（下位の設定は見ない）。
        """
        phase_bgm = self.section_bgm.get(f"{scene.section}:{scene.phase}") if scene.phase else None
        for candidate in (scene.bgm_path, phase_bgm, self.section_bgm.get(scene.section), self.bgm_path):
            if candidate == NO_BGM:
                return None
            if candidate:
                return candidate
        return None

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
            "se_volume": self.se_volume,
            "common_background_path": self.common_background_path,
            "pr_label_enabled": self.pr_label_enabled,
            "pr_label_text": self.pr_label_text,
            "reading_dict": self.reading_dict,
            "section_bgm": self.section_bgm,
            "book_title": self.book_title,
            "book_author": self.book_author,
            "book_cover_path": self.book_cover_path,
            "speech_speed": self.speech_speed,
            "auto_camera": self.auto_camera,
            "auto_shake": self.auto_shake,
            "char_bob": self.char_bob,
            "auto_jump": self.auto_jump,
            "char_slide_in": self.char_slide_in,
            "slide_transition": self.slide_transition,
            "background_blur": self.background_blur,
            "video_title": self.video_title,
            "video_description": self.video_description,
            "video_tags": self.video_tags,
            "video_style": self.video_style,
            "title_candidates": self.title_candidates,
            "description_lead": self.description_lead,
            "hashtags": self.hashtags,
            "show_chapter_label": self.show_chapter_label,
            "board_pause": self.board_pause,
            "source_kind": self.source_kind,
            "sources": self.sources,
            "lesson": self.lesson,
            "thumbnail": self.thumbnail,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(
            name=data.get("name", "新しい動画プロジェクト"),
            format=VideoFormat(data.get("format", VideoFormat.LANDSCAPE.value)),
            scenes=[Scene.from_dict(s) for s in data.get("scenes", [])],
            bgm_path=data.get("bgm_path"),
            bgm_volume=data.get("bgm_volume", 0.15),
            se_volume=data.get("se_volume", 0.6),
            common_background_path=data.get("common_background_path"),
            pr_label_enabled=data.get("pr_label_enabled", False),
            pr_label_text=data.get("pr_label_text", "PR"),
            reading_dict=data.get("reading_dict", []),
            section_bgm=data.get("section_bgm", {}),
            book_title=data.get("book_title", ""),
            book_author=data.get("book_author", ""),
            book_cover_path=data.get("book_cover_path"),
            speech_speed=data.get("speech_speed", 1.2),
            auto_camera=data.get("auto_camera", True),
            auto_shake=data.get("auto_shake", True),
            char_bob=data.get("char_bob", True),
            auto_jump=data.get("auto_jump", True),
            char_slide_in=data.get("char_slide_in", True),
            slide_transition=data.get("slide_transition", "slide"),
            background_blur=data.get("background_blur", 2.0),
            video_title=data.get("video_title", ""),
            video_description=data.get("video_description", ""),
            video_tags=data.get("video_tags", []),
            video_style=data.get("video_style", "normal"),
            title_candidates=data.get("title_candidates", []),
            description_lead=data.get("description_lead", ""),
            hashtags=data.get("hashtags", []),
            show_chapter_label=data.get("show_chapter_label", True),
            board_pause=data.get("board_pause", 0.0),
            source_kind=data.get("source_kind", "book"),
            sources=data.get("sources", []),
            lesson=data.get("lesson", {}),
            thumbnail=data.get("thumbnail", {}),
        )
