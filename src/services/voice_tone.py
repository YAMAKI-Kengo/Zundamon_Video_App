"""
セリフの表情（感情）に合わせて、VOICEVOXの声色（スタイル）と話し方（高さ・抑揚・速さ・音量）を変える。

同じキャラクターでも、泣いているときは「なみだめ」、ひそひそ話は「ささやき」、めたんの冷めた一言は
「ツンツン」で抑揚を小さく…のように、VOICEVOXのスタイルと audio_query のパラメータを変えて、
声だけでも感情が伝わるようにする（「ノーマル」の声だけだと、泣いても怒っても同じ声色で臨場感が出ない）。

- スタイルのIDは VOICEVOX のバージョンで変わることがあるため、ここには「スタイル名」だけを書き、
  実際のIDは起動中の VOICEVOX の /speakers から調べる（style_id()）。
  そのスタイルが無い（古いVOICEVOX・つむぎのようにスタイルが1つだけ）場合は「ノーマル」の声のまま、
  高さ・抑揚・速さ・音量の調整だけを効かせる。
- 表情が一覧に無いとき（普通・説明など）は、何も変えない。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# VOICEVOX の /speakers での話者名（キャラクターキー → 話者名）
VOICEVOX_SPEAKER_NAMES: dict[str, str] = {
    "zundamon": "ずんだもん",
    "shikoku_metan": "四国めたん",
    "kasukabe_tsumugi": "春日部つむぎ",
}

# audio_query のパラメータの範囲（VOICEVOXの仕様。外れると合成が失敗するため丸める）
PITCH_RANGE = (-0.15, 0.15)
INTONATION_RANGE = (0.0, 2.0)
VOLUME_RANGE = (0.0, 2.0)


@dataclass(frozen=True)
class VoiceTone:
    style: str = ""          # VOICEVOXのスタイル名（空ならノーマル）
    pitch: float = 0.0       # 声の高さ（pitchScale に足す。+0.05 で少し高く）
    intonation: float = 1.0  # 抑揚（intonationScale に掛ける。1.3 で大げさに、0.8 で平坦・冷めた感じ）
    speed: float = 1.0       # 話す速さ（speedScale に掛ける）
    volume: float = 1.0      # 音量（volumeScale に掛ける。ささやき系は小さいので上げる）


NEUTRAL = VoiceTone()

# 表情 → 声色。config/characters.json の表情キーに合わせる
EXPRESSION_TONES: dict[str, dict[str, VoiceTone]] = {
    "zundamon": {
        "happy": VoiceTone(pitch=0.02, intonation=1.2),
        "excited": VoiceTone(pitch=0.04, intonation=1.35, speed=1.08),
        "smug": VoiceTone(pitch=0.02, intonation=1.3, speed=0.97),
        "surprised": VoiceTone(pitch=0.05, intonation=1.35),
        "shock": VoiceTone(pitch=0.06, intonation=1.45, speed=0.95),
        "panic": VoiceTone(pitch=0.05, intonation=1.3, speed=1.12),
        "angry": VoiceTone(style="ツンツン", intonation=1.2),
        "sad": VoiceTone(style="なみだめ", speed=0.96),
        "cry": VoiceTone(style="なみだめ", intonation=1.2, speed=0.94),
        "gloomy": VoiceTone(style="ヘロヘロ", speed=0.94),
        "sleepy": VoiceTone(style="ヘロヘロ", speed=0.9),
        "troubled": VoiceTone(intonation=0.9, speed=0.97),
        "think": VoiceTone(intonation=0.9, speed=0.95),
        "idea": VoiceTone(pitch=0.03, intonation=1.25),
        "whisper": VoiceTone(style="ヒソヒソ", volume=1.5, speed=0.97),
    },
    "shikoku_metan": {
        "happy": VoiceTone(style="あまあま", intonation=1.1),
        "laugh": VoiceTone(style="あまあま", pitch=0.02, intonation=1.25),
        "blush": VoiceTone(style="あまあま", intonation=1.1, speed=0.97),
        "angry": VoiceTone(style="ツンツン", intonation=1.2),
        # 呆れ・冷めたツッコミ: ツンツンで抑揚を落とすと「…はぁ」という冷たさが出る
        "troubled": VoiceTone(style="ツンツン", intonation=0.8, speed=0.97),
        "confident": VoiceTone(style="ツンツン", intonation=1.05),
        "surprised": VoiceTone(pitch=0.04, intonation=1.3),
        "sad": VoiceTone(pitch=-0.02, intonation=0.8, speed=0.95),
        "worried": VoiceTone(pitch=-0.01, intonation=0.85, speed=0.97),
        "think": VoiceTone(intonation=0.9, speed=0.96),
        "whisper": VoiceTone(style="ささやき", volume=1.5, speed=0.97),
    },
    "kasukabe_tsumugi": {
        "happy": VoiceTone(pitch=0.02, intonation=1.2),
        "laugh": VoiceTone(pitch=0.03, intonation=1.3),
        "surprised": VoiceTone(pitch=0.05, intonation=1.3),
        "smug": VoiceTone(intonation=1.25),
        "sad": VoiceTone(pitch=-0.02, intonation=0.85, speed=0.96),
        "troubled": VoiceTone(intonation=0.9),
        "angry": VoiceTone(intonation=1.2),
    },
}


def tone_for(character_key: str, expression: str) -> VoiceTone:
    """キャラクターと表情に合う声色（一覧に無ければ変えない）。"""
    return EXPRESSION_TONES.get(character_key, {}).get(expression or "", NEUTRAL)


def style_id(speakers: list[dict], character_key: str, style_name: str) -> Optional[int]:
    """/speakers の一覧から、キャラクターのスタイル名に当たるIDを探す（無ければ None）。"""
    name = VOICEVOX_SPEAKER_NAMES.get(character_key)
    if not name or not style_name:
        return None
    for speaker in speakers or []:
        if speaker.get("name") != name:
            continue
        for style in speaker.get("styles") or []:
            if style.get("name") == style_name and isinstance(style.get("id"), int):
                return style["id"]
    return None


def _clamp(value: float, low_high: tuple[float, float]) -> float:
    return max(low_high[0], min(low_high[1], value))


def apply_tone(query_json: dict, tone: VoiceTone) -> dict:
    """audio_query の結果に、声の高さ・抑揚・速さ・音量の調整をかける（話す速さの自動調整の前に呼ぶ）。"""
    if tone == NEUTRAL:
        return query_json
    query_json["pitchScale"] = _clamp((query_json.get("pitchScale") or 0.0) + tone.pitch, PITCH_RANGE)
    base_intonation = query_json.get("intonationScale")
    query_json["intonationScale"] = _clamp((1.0 if base_intonation is None else base_intonation) * tone.intonation,
                                           INTONATION_RANGE)
    base_volume = query_json.get("volumeScale")
    query_json["volumeScale"] = _clamp((1.0 if base_volume is None else base_volume) * tone.volume, VOLUME_RANGE)
    query_json["speedScale"] = (query_json.get("speedScale") or 1.0) * tone.speed
    return query_json
