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
import re
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


DEFAULT_SPEECH_SPEED = 1.2   # 話す速さの既定値（VOICEVOXの等速1.0だと解説動画としてはテンポが遅いため）
PHONEME_EDGE_SECONDS = 0.05  # セリフの前後の無音（VOICEVOXの既定は0.1秒ずつ）
PAUSE_LENGTH_SCALE = 0.8     # 文中の「、」「。」の間の長さ（VOICEVOXの既定は1.0）

_BRACKETS = str.maketrans("", "", "『』「」【】〈〉《》")  # 読み上げでは括弧を外す（「『書名』は」の前後に不自然な間が入るため）
# カタカナとカタカナの間のスペース・中黒（カタカナ英語「キャン アイ ゲット」の単語の区切り）
_KATAKANA_GAP = re.compile(r"(?<=[ァ-ヶー])[ 　・]+(?=[ァ-ヶー])")
_RANGE_PATTERN = re.compile(r"(\d)\s*[〜～~]\s*(\d)")
_SEVEN_PATTERN = re.compile(r"(?<![\d.])7(?=(時間|人|時|分|日間|週間|か月|ヶ月|年|歳|回|個|冊|点|割|倍|才))")


def normalize_for_speech(text: str) -> str:
    """VOICEVOXが読み間違える表記を、読み上げ用にだけ直す（字幕の表示には影響しない）。

    実際にVOICEVOXで読ませて確認した誤読への対処:
      - 数字の範囲「1〜2時間」→ 「〜」が「、」と読まれる → 「1から2時間」
      - 単独の「7」+単位（7時間・7人など）→「しち」と読まれる → 「なな」
      - カタカナ英語の単語の間のスペース・「・」（「キャン アイ ゲット」）→ 1語ごとに「、」の間が入って
        カタコトがとても遅くなる → 詰めて一続きに読ませる（句読点での区切りは残す）
    """
    text = _KATAKANA_GAP.sub("", text)
    text = text.translate(_BRACKETS)
    text = _RANGE_PATTERN.sub(r"\1から\2", text)
    return _SEVEN_PATTERN.sub("なな", text)


def to_katakana(text: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)


def _is_katakana_reading(text: str) -> bool:
    return bool(text) and all("ァ" <= c <= "ヴ" or c in "ーヵヶ" for c in text)


def _zenkaku(text: str) -> str:
    """VOICEVOXのユーザー辞書は表記を全角で保存するため、半角英数字・記号を全角にそろえる。"""
    return "".join(
        "\u3000" if c == " " else chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in text
    )


_user_dict_cache: dict[tuple, frozenset] = {}


def _accent_type(pronunciation: str, base_url: str) -> int:
    """読み（カタカナ）を VOICEVOX に読ませたときのアクセント位置を、辞書登録用のアクセント型にする。"""
    try:
        query = requests.post(
            f"{base_url}/audio_query", params={"speaker": 1, "text": pronunciation},
            timeout=(CONNECT_TIMEOUT, QUERY_TIMEOUT),
        ).json()
        phrases = query.get("accent_phrases") or []
        return int(phrases[0]["accent"]) if len(phrases) == 1 else 0
    except (requests.exceptions.RequestException, ValueError, KeyError, IndexError):
        return 0


def sync_user_dict(reading_dict: Optional[list[dict]], base_url: str = VOICEVOX_BASE_URL) -> frozenset:
    """読み方辞書の単語を、VOICEVOX本体のユーザー辞書に登録する（登録できた単語の集合を返す）。

    文字列の置き換えだと「ぐっすりねむるためのかがくは」のように平仮名が続いて単語の区切りが崩れ、
    直後の助詞「は」を「ハ」と読むなどの誤読が起きる。ユーザー辞書に登録すれば、表記は漢字のまま
    VOICEVOXが単語として正しく区切って読むため、前後の助詞やアクセントも自然になる。
    読みがカタカナ/ひらがなでない語や、ユーザー辞書が使えない古いVOICEVOXでは登録せず、
    speech_text() で従来どおり文字列の置き換えをする。同じ辞書の内容では1回だけ同期する。
    """
    entries = tuple(sorted(
        (str(item.get("word", "")).strip(), to_katakana(str(item.get("reading", "")).strip().replace(" ", "")))
        for item in (reading_dict or []) if isinstance(item, dict)
    ))
    entries = tuple((w, r) for w, r in entries if w and r and _is_katakana_reading(r))
    if not entries:
        return frozenset()
    if entries in _user_dict_cache:
        return _user_dict_cache[entries]
    registered = set()
    try:
        existing = requests.get(f"{base_url}/user_dict", timeout=(CONNECT_TIMEOUT, QUERY_TIMEOUT)).json()
        by_surface = {v.get("surface"): (uuid, v.get("pronunciation"), v.get("priority")) for uuid, v in existing.items()}
        for word, pronunciation in entries:
            surface = _zenkaku(word)
            found = by_surface.get(surface)
            if found and found[1] == pronunciation and found[2] == 10:
                registered.add(word)
                continue
            params = {
                "surface": word, "pronunciation": pronunciation,
                "accent_type": _accent_type(pronunciation, base_url),
                "word_type": "PROPER_NOUN", "priority": 10,  # 最優先（VOICEVOX標準の辞書の読みより優先させる）
            }
            if found:
                resp = requests.put(f"{base_url}/user_dict_word/{found[0]}", params=params,
                                    timeout=(CONNECT_TIMEOUT, QUERY_TIMEOUT))
            else:
                resp = requests.post(f"{base_url}/user_dict_word", params=params,
                                     timeout=(CONNECT_TIMEOUT, QUERY_TIMEOUT))
            if resp.status_code in (200, 204):
                registered.add(word)
    except (requests.exceptions.RequestException, ValueError, AttributeError):
        pass  # ユーザー辞書が使えない場合は、文字列の置き換えで対応する
    result = frozenset(registered)
    _user_dict_cache[entries] = result
    return result


def speech_text(text: str, reading_dict: Optional[list[dict]] = None) -> str:
    """VOICEVOXに渡す読み上げ用のテキスト（読み方辞書 → 表記の正規化）。

    読み方辞書の単語は、まずVOICEVOXのユーザー辞書への登録を試み（sync_user_dict）、登録できなかった
    単語だけ文字列を置き換える。置き換える読みはカタカナにする（平仮名だと前後と続いて単語の区切りが崩れやすい）。
    """
    registered = sync_user_dict(reading_dict)
    fallback = [
        {"word": item.get("word", ""), "reading": to_katakana(str(item.get("reading", "")))}
        for item in (reading_dict or [])
        if isinstance(item, dict) and str(item.get("word", "")).strip() not in registered
    ]
    return normalize_for_speech(apply_reading_dict(text, fallback))


def _apply_tempo(query_json: dict, speech_speed: float) -> dict:
    """話す速さと、セリフ前後・文中の間の長さを調整する（テンポを速めにする）。"""
    query_json["speedScale"] = max(MIN_SPEED_SCALE, min(MAX_SPEED_SCALE, speech_speed))
    query_json["prePhonemeLength"] = min(query_json.get("prePhonemeLength") or 0.1, PHONEME_EDGE_SECONDS)
    query_json["postPhonemeLength"] = min(query_json.get("postPhonemeLength") or 0.1, PHONEME_EDGE_SECONDS)
    if "pauseLengthScale" in query_json:
        query_json["pauseLengthScale"] = PAUSE_LENGTH_SCALE
    return query_json


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


def measure_natural_duration(
    text: str,
    character_key: str,
    base_url: str = VOICEVOX_BASE_URL,
    reading_dict: Optional[list[dict]] = None,
    speech_speed: float = DEFAULT_SPEECH_SPEED,
) -> float:
    """話す速さ speech_speed で読み上げたときの秒数を、音声を合成せずに求める。

    /audio_query だけを呼び、その長さ情報から計算する（_estimate_base_duration）。
    台本からシーンを一括生成するときに、表示秒数を実際の読み上げ時間に合わせるために使う
    （文字数からの概算 estimate_duration() だと短すぎて、話速が不自然に上がることがあるため）。

    Raises:
        VoicevoxConnectionError / VoicevoxSynthesisError: synthesize_voice() と同じ。
    """
    if not text or not text.strip():
        return 0.0
    try:
        resp = requests.post(
            f"{base_url}/audio_query",
            params={"speaker": get_speaker_id(character_key), "text": speech_text(text, reading_dict)},
            timeout=(CONNECT_TIMEOUT, QUERY_TIMEOUT),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        raise VoicevoxConnectionError(
            "VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。"
        ) from e
    if resp.status_code != 200:
        raise VoicevoxSynthesisError(f"VOICEVOXの音声クエリ生成に失敗しました（ステータスコード: {resp.status_code}）。")
    query_json = _apply_tempo(resp.json(), speech_speed)
    return _estimate_base_duration(query_json) / query_json["speedScale"]


def get_kana(text: str, character_key: str, reading_dict: Optional[list[dict]] = None,
             base_url: str = VOICEVOX_BASE_URL) -> str:
    """VOICEVOXが実際に読む予定の読み（AquesTalk風のカタカナ表記）を返す（読み間違いのチェック用）。"""
    try:
        resp = requests.post(
            f"{base_url}/audio_query",
            params={"speaker": get_speaker_id(character_key), "text": speech_text(text, reading_dict)},
            timeout=(CONNECT_TIMEOUT, QUERY_TIMEOUT),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        raise VoicevoxConnectionError(
            "VOICEVOXが起動していません。VOICEVOXを起動してから再度実行してください。"
        ) from e
    if resp.status_code != 200:
        raise VoicevoxSynthesisError(f"VOICEVOXの音声クエリ生成に失敗しました（ステータスコード: {resp.status_code}）。")
    return resp.json().get("kana", "")


def synthesize_voice(
    text: str,
    character_key: str,
    output_path: Optional[Path] = None,
    target_duration: Optional[float] = None,
    max_speed_scale: float = MAX_SPEED_SCALE,
    base_url: str = VOICEVOX_BASE_URL,
    reading_dict: Optional[list[dict]] = None,
    speech_speed: float = DEFAULT_SPEECH_SPEED,
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
        speech_speed: 話す速さ（1.0=VOICEVOXの等速）。表示秒数に収まらない場合は、ここから自動で引き上げる。

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
    query_text = speech_text(text, reading_dict)

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
    query_json = _apply_tempo(query_resp.json(), speech_speed)

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
