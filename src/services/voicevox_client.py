"""
VOICEVOX ENGINE との連携（音声合成）。

VOICEVOX ENGINEはローカルで起動するHTTPサーバー（デフォルト http://localhost:50021）で、
以下の2段階のリクエストで音声合成を行う。

  1. POST /audio_query?speaker={id}&text={text}
       -> 抑揚などのパラメータを含むクエリJSONを取得
  2. POST /synthesis?speaker={id}  (body: 上記クエリJSON)
       -> 合成された音声のwavバイナリを取得

このアプリはプログラミング知識のないユーザーへの配布を想定しているため、
「VOICEVOXを起動し忘れている」状態を最重要のエラーケースとして扱い、
アプリを落とさず分かりやすい日本語メッセージを返すようにしている。
"""
from __future__ import annotations

import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

# --- 設定（話者IDなど） -------------------------------------------------

VOICEVOX_BASE_URL = "http://localhost:50021"

# VOICEVOXの話者ID（「ノーマル」スタイル）。
# 他のスタイル（あまあま・ツンツン等）を使いたい場合は、VOICEVOXを起動した状態で
# ブラウザから http://localhost:50021/docs を開き、/speakers エンドポイントで
# 該当キャラクターの style.id を確認してここを書き換える。
VOICEVOX_SPEAKER_IDS: dict[str, int] = {
    "zundamon": 3,        # ずんだもん（ノーマル）
    "shikoku_metan": 2,   # 四国めたん（ノーマル）
}

CONNECT_TIMEOUT = 3.0     # 起動確認・クエリ生成用の接続タイムアウト（秒）
QUERY_TIMEOUT = 15.0      # /audio_query の応答タイムアウト（秒）
SYNTHESIS_TIMEOUT = 120.0  # /synthesis の応答タイムアウト（秒）。長文だと時間がかかるため長めに設定

# 指定秒数に収めるための話速自動調整（speedScale）の上限。
# これを超える速度が必要な場合は、聞き取れなくなるのを避けるためこの値で頭打ちにし、
# 収まりきらない分は video_builder.py 側の従来通りの強制カットに委ねる。
MAX_SPEED_SCALE = 2.0
MIN_SPEED_SCALE = 0.5  # 現状は速める方向にしか使わないが、将来の拡張用に下限も定義しておく

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIO_DIR = PROJECT_ROOT / "tmp" / "audio"


# --- 例外定義 -------------------------------------------------------------

class VoicevoxConnectionError(Exception):
    """VOICEVOXエンジンに接続できない場合の例外（未起動 / URL間違い / タイムアウト等）。

    UI側ではこの例外を捕まえて
    「VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。」
    という固定メッセージを表示する。
    """


class VoicevoxSynthesisError(Exception):
    """接続はできたが、音声合成リクエスト自体が失敗した場合の例外
    （不正な話者ID、テキストが長すぎる等、VOICEVOX側が4xx/5xxを返したケース）。
    """


# --- データクラス -----------------------------------------------------------

@dataclass
class SynthesisResult:
    audio_path: Optional[Path]   # 生成されたwavファイルのパス（無音時はNone）
    duration_sec: float          # 音声の長さ（秒）
    is_silent: bool = False      # True: テキスト未入力、または合成失敗時のフォールバックで無音扱いにした
    speed_scale: float = 1.0     # 実際に適用したspeedScale（自動調整が働かなければ1.0のまま）
    speed_capped: bool = False   # True: 指定秒数に収めるにはMAX_SPEED_SCALEでも足りず、頭打ちにした


# --- 公開関数 ---------------------------------------------------------------

def estimate_duration(text: str, chars_per_second: float = 7.0) -> float:
    """文字数から大まかな読み上げ秒数を見積もる（UIでの目安表示用。実際の合成前のヒューリスティック）。"""
    if not text:
        return 1.0
    return max(1.0, round(len(text) / chars_per_second, 1))


def apply_reading_dict(text: str, reading_dict: Optional[list[dict]]) -> str:
    """読み方辞書を使って、VOICEVOXに渡す直前のテキストだけを単純な文字列置換で補正する。

    テロップ表示用のテキスト(Scene.text)には一切手を加えず、この関数を通した後の
    文字列だけをVOICEVOXの/audio_queryに渡すことで、「人」を「じん」と誤読される、
    といったケースを個別に修正できるようにする。

    reading_dict の各要素は {"word": "二人", "reading": "ふたり"} の形式を想定。
    word・readingのどちらかが空の要素は無視する。1文字の単語など、意図せず他の単語の
    一部にもマッチしてしまうケース（例: 「人」だけを登録すると「日本人」の一部も
    置き換わってしまう）を避けるため、登録された単語は文字数が長いものから順に
    置換していく（より具体的なフレーズを優先する）。
    """
    if not text or not reading_dict:
        return text
    entries = [
        (str(item.get("word", "")).strip(), str(item.get("reading", "")).strip())
        for item in reading_dict
        if isinstance(item, dict)
    ]
    entries = [(word, reading) for word, reading in entries if word and reading]
    entries.sort(key=lambda pair: len(pair[0]), reverse=True)
    for word, reading in entries:
        text = text.replace(word, reading)
    return text


def get_speaker_id(character_key: str) -> int:
    """キャラクターキーからVOICEVOXの話者IDを取得する。未定義の場合は例外を送出する。"""
    speaker_id = VOICEVOX_SPEAKER_IDS.get(character_key)
    if speaker_id is None:
        raise VoicevoxSynthesisError(
            f"「{character_key}」のVOICEVOX話者IDが未設定です。"
            f"src/services/voicevox_client.py の VOICEVOX_SPEAKER_IDS に追加してください。"
        )
    return speaker_id


def ensure_engine_running(base_url: str = VOICEVOX_BASE_URL) -> None:
    """VOICEVOX ENGINEが起動しているかを確認する。起動していなければ例外を送出する。

    動画生成の最初に必ずこれを呼び出すことで、各シーンの処理に入る前に
    「VOICEVOXが起動していません」という分かりやすいエラーで早期に止められるようにする。
    """
    try:
        response = requests.get(f"{base_url}/version", timeout=CONNECT_TIMEOUT)
        response.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise VoicevoxConnectionError(
            "VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。"
        ) from e
    except requests.exceptions.Timeout as e:
        raise VoicevoxConnectionError(
            "VOICEVOXへの接続がタイムアウトしました。VOICEVOXが起動しているか確認してください。"
        ) from e
    except requests.exceptions.RequestException as e:
        raise VoicevoxConnectionError(
            "VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。"
        ) from e


def _estimate_base_duration(query_json: dict) -> float:
    """audio_queryのレスポンス(mora単位の長さ情報)から、speedScale=1.0のときの
    合成音声の長さを見積もる。実際に音声を生成せずに概算できるため、
    「まず生成してから測る」よりも高速に指定秒数への収まり具合を判定できる。
    """
    total = (query_json.get("prePhonemeLength") or 0.0) + (query_json.get("postPhonemeLength") or 0.0)
    for phrase in query_json.get("accent_phrases", []) or []:
        for mora in phrase.get("moras", []) or []:
            total += mora.get("consonant_length") or 0.0
            total += mora.get("vowel_length") or 0.0
        pause_mora = phrase.get("pause_mora")
        if pause_mora:
            total += pause_mora.get("vowel_length") or 0.0
    return total


def _apply_auto_speed(
    query_json: dict,
    target_duration: float,
    max_speed_scale: float = MAX_SPEED_SCALE,
) -> tuple[dict, float, bool]:
    """指定秒数(target_duration)に収まるよう、必要であればquery_jsonのspeedScaleを引き上げる。

    「読み上げが指定秒数を超えそうな場合だけ話速を上げる」という一方向の調整のみ行う
    （既に短い場合に、あえて話速を落とすことはしない）。

    Returns:
        (更新後のquery_json, 実際に適用したspeedScale, MAX_SPEED_SCALEで頭打ちになったか)
    """
    base_duration = _estimate_base_duration(query_json)
    current_speed = query_json.get("speedScale") or 1.0

    if base_duration <= 0 or target_duration <= 0:
        return query_json, current_speed, False

    required_speed = base_duration / target_duration
    if required_speed <= current_speed:
        # 現在の速度のままで指定秒数に収まる（＝速める必要はない）
        return query_json, current_speed, False

    applied_speed = min(required_speed, max_speed_scale)
    speed_capped = required_speed > max_speed_scale
    query_json["speedScale"] = applied_speed
    return query_json, applied_speed, speed_capped


def synthesize_voice(
    text: str,
    character_key: str,
    output_path: Optional[Path] = None,
    target_duration: Optional[float] = None,
    max_speed_scale: float = MAX_SPEED_SCALE,
    base_url: str = VOICEVOX_BASE_URL,
    reading_dict: Optional[list[dict]] = None,
) -> SynthesisResult:
    """指定テキストをVOICEVOXで音声合成し、wavファイルとして保存する。

    Args:
        text: 読み上げるテキスト。空文字列の場合は合成を行わず無音として扱う。
        character_key: "zundamon" | "shikoku_metan"
        output_path: 保存先のwavパス。省略時は tmp/audio/ 配下にランダムなファイル名で保存する。
        target_duration: シーンの表示秒数。指定した場合、読み上げがこの秒数を超えそうなときに
            話速(speedScale)を自動的に引き上げて収めようとする（上限はmax_speed_scale）。
            Noneの場合は話速調整を行わない（常にspeedScale=1.0）。
        max_speed_scale: 話速自動調整の上限。これを超える速度が必要な場合は、この値で頭打ちにし、
            それでも収まらない分は呼び出し側（video_builder）の強制カットに委ねる。
        base_url: VOICEVOX ENGINEのURL。
        reading_dict: 読み方辞書（Project.reading_dict）。指定した場合、audio_queryに渡す直前の
            テキストにだけ word→reading の置換を適用する（テロップ表示には影響しない）。

    Returns:
        SynthesisResult

    Raises:
        VoicevoxConnectionError: VOICEVOXに接続できない場合（未起動・タイムアウト等）。
        VoicevoxSynthesisError: 接続はできたが合成リクエスト自体が失敗した場合。
    """
    if not text or not text.strip():
        # テキスト未入力のシーンはエラーにせず、無音として扱う
        return SynthesisResult(audio_path=None, duration_sec=1.0, is_silent=True)

    speaker_id = get_speaker_id(character_key)
    query_text = apply_reading_dict(text, reading_dict)

    # 1. /audio_query
    try:
        query_resp = requests.post(
            f"{base_url}/audio_query",
            params={"speaker": speaker_id, "text": query_text},
            timeout=(CONNECT_TIMEOUT, QUERY_TIMEOUT),
        )
    except requests.exceptions.ConnectionError as e:
        raise VoicevoxConnectionError(
            "VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。"
        ) from e
    except requests.exceptions.Timeout as e:
        raise VoicevoxConnectionError(
            "VOICEVOXへの接続がタイムアウトしました。VOICEVOXが起動しているか確認してください。"
        ) from e

    if query_resp.status_code != 200:
        raise VoicevoxSynthesisError(
            f"VOICEVOXの音声クエリ生成に失敗しました（ステータスコード: {query_resp.status_code}）。"
            f"テキストの内容や話者IDを確認してください。"
        )
    query_json = query_resp.json()

    # 話速の自動調整（指定秒数が渡されている場合のみ）
    speed_scale = query_json.get("speedScale") or 1.0
    speed_capped = False
    if target_duration is not None:
        query_json, speed_scale, speed_capped = _apply_auto_speed(query_json, target_duration, max_speed_scale)

    # 2. /synthesis
    try:
        synth_resp = requests.post(
            f"{base_url}/synthesis",
            params={"speaker": speaker_id},
            json=query_json,
            timeout=(CONNECT_TIMEOUT, SYNTHESIS_TIMEOUT),
        )
    except requests.exceptions.ConnectionError as e:
        raise VoicevoxConnectionError(
            "VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。"
        ) from e
    except requests.exceptions.Timeout as e:
        raise VoicevoxConnectionError(
            "VOICEVOXへの接続がタイムアウトしました（音声合成に時間がかかりすぎています）。"
        ) from e

    if synth_resp.status_code != 200:
        raise VoicevoxSynthesisError(
            f"VOICEVOXでの音声合成に失敗しました（ステータスコード: {synth_resp.status_code}）。"
        )

    if output_path is None:
        DEFAULT_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        output_path = DEFAULT_AUDIO_DIR / f"{uuid.uuid4().hex}.wav"
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        output_path.write_bytes(synth_resp.content)
        duration_sec = _get_wav_duration(output_path)
    except (OSError, wave.Error) as e:
        raise VoicevoxSynthesisError(
            f"音声ファイルの保存に失敗しました: {output_path}"
        ) from e

    return SynthesisResult(
        audio_path=output_path,
        duration_sec=duration_sec,
        is_silent=False,
        speed_scale=speed_scale,
        speed_capped=speed_capped,
    )


def list_available_speakers(base_url: str = VOICEVOX_BASE_URL) -> list[dict]:
    """（任意の補助関数）現在起動中のVOICEVOXが持つ話者一覧を取得する。

    他のスタイル（あまあま・ツンツン等）のIDを調べたいときに使う。
    VOICEVOX_SPEAKER_IDS の値を決める際の参考用で、アプリ本体からは呼ばれない。
    """
    try:
        resp = requests.get(f"{base_url}/speakers", timeout=CONNECT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        raise VoicevoxConnectionError(
            "VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。"
        ) from e


def _get_wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate) if rate else 0.0
