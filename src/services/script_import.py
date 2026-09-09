"""
台本（テキスト）から複数シーンを一括生成するユーティリティ。

1行 = 1シーンとして解析する。行の先頭が「話者名:」または「話者名：」の形式であれば
その話者を明示的に使用する（例: "ずんだもん: こんにちは"、"zundamon: こんにちは" どちらも可）。
話者名は表示名・内部キーのほか、config/characters.jsonの"aliases"に登録されたニックネーム
（既定では"めたん"＝四国めたん、"ずんだ"＝ずんだもん）でも指定できる。認識できない名前が
書かれていた場合はエラーにはせず、その部分を読み上げテキストから取り除いたうえで
（話者名がそのまま音声で読み上げられてしまわないように）、話者は直前のまま据え置き、
その旨を警告として伝える。
話者名の直後に括弧で表情を書くと、その行の表情も指定できる
（例: "ずんだもん(喜び): こんにちは"、半角/全角の括弧どちらも可。開き括弧・閉じ括弧で
半角/全角が混ざっていても認識できるようにしてある）。
表情は日本語ラベル（config/characters.jsonのexpressionsで定義されたもの。例: "喜び"）でも、
内部キー（例: "happy"）でも指定できる。話者に見つからない表情名を指定した場合は、
警告を追加したうえでその話者の既定表情にフォールバックする（エラーにはしない）。

話者指定の無い行は、直前に指定されていた話者・表情がそのまま続けて話す扱いになる
（自動で相手のキャラに交互切り替えすることはしない）。話者を交互に切り替えたい掛け合い
台本の場合は、話者が変わる行にだけ明示的に「話者名:」を書けばよい。

話者・表情が直前の行と変わらない行は、新しいシーンを作らず、直前のシーンの読み上げ
テキストに改行で連結される（＝1つのシーン内の複数行として扱われる。シーン編集画面で
手動でEnterキーによる改行を入力した場合と同じ扱いになる）。台本中で単に文章を読みやすく
改行しただけの行が、意図せず別カット（別シーン）として切り出されてしまうのを防ぐため。
話者または表情が変わる行にきたときだけ、そこで新しいシーンになる。

空行は読み飛ばす。テキストファイル(.txt)のアップロード・直接貼り付けのどちらでも
同じ関数(parse_script)で処理できる。
"""
from __future__ import annotations

from typing import Optional

from src.models import Scene
from src.services.voicevox_client import estimate_duration
from src.utils.asset_loader import (
    get_available_expressions,
    get_character_display_name,
    get_default_expression,
    get_expression_label,
    list_characters,
    load_character_config,
)

DEFAULT_DURATION_FALLBACK = 3.0
_SPEAKER_SEPARATORS = ("：", ":")
_OPEN_PARENS = ("(", "（")
_CLOSE_PARENS = (")", "）")
# 話者名らしき文字列かどうかの判定に使う、通常の文章では使われにくい記号
# （これらを含む/長すぎる場合は「話者指定ではなく普通の文章に偶然コロンが含まれただけ」と判断する）
_SENTENCE_LIKE_CHARS = "。、！？!?,　 "
_SPEAKER_LABEL_MAX_LENGTH = 12


def _build_speaker_label_map() -> dict[str, str]:
    """"ずんだもん" や "zundamon" のような表記から character_key を引けるようにする対応表。

    config/characters.json の "aliases"（例: 四国めたんの "めたん"、ずんだもんの "ずんだ"）も
    ニックネームとして認識する。ここに無いニックネームで書かれた場合は認識できないため、
    その旨を警告する仕組みは _looks_like_speaker_label() 側にある。
    """
    label_map: dict[str, str] = {}
    cfg = load_character_config()
    for key in list_characters():
        label_map[key] = key
        label_map[get_character_display_name(key)] = key
        for alias in cfg.get(key, {}).get("aliases", []):
            label_map[alias] = key
    return label_map


def _looks_like_speaker_label(text: str) -> bool:
    """カッコの中身（話者名部分）が「話者名の書き間違い」として妥当な長さ・形かを判定する。

    句読点を含む・長すぎる場合は話者名らしくないと判断する（誤検知を減らすための
    簡易ヒューリスティック）。呼び出し側では、これに加えて「話者名(表情):」のように
    カッコで表情らしき指定が伴っている行だけを対象にすることで、"注意: ..." のような、
    話者指定を意図していない普通の文章の先頭にたまたまコロンが含まれるだけのケースを
    誤って警告・改変してしまわないようにしている（カッコ付きの「話者名(表情):」という
    書き方は通常の文章にはまず登場しない、かなり特徴的な構文であるため）。
    """
    if not text or len(text) > _SPEAKER_LABEL_MAX_LENGTH:
        return False
    return not any(ch in _SENTENCE_LIKE_CHARS for ch in text)


def _build_expression_label_maps(characters: list[str]) -> dict[str, dict[str, str]]:
    """character_key -> {日本語ラベルまたは内部キー: 表情キー} の対応表を作る。

    例えば zundamon については {"喜び": "happy", "happy": "happy", ...} のような辞書になる。
    """
    maps: dict[str, dict[str, str]] = {}
    for char_key in characters:
        label_map: dict[str, str] = {}
        for expr_key in get_available_expressions(char_key):
            label_map[expr_key] = expr_key
            label_map[get_expression_label(char_key, expr_key)] = expr_key
        maps[char_key] = label_map
    return maps


def _split_expression_suffix(prefix: str) -> tuple[str, Optional[str]]:
    """"四国めたん(困り)" のような表記から (話者名部分, 表情ラベル) を取り出す。

    括弧が無い、あるいは括弧の中身/前が空の場合は (prefix, None) を返す
    （＝表情指定なしの通常の話者名として扱う）。

    日本語入力（IME）だと開き括弧だけ全角になる・閉じ括弧だけ半角になる、といった
    半角/全角の混在が起きやすいため、開き括弧・閉じ括弧はそれぞれ独立に半角/全角どちらでも
    認識する（"ずんだもん（喜び)" のような組み合わせでも表情指定として認識できるようにし、
    認識できずに話者名や括弧ごと読み上げテキストとして扱われてしまう事故を減らすため）。
    """
    if not prefix or prefix[-1] not in _CLOSE_PARENS:
        return prefix, None
    open_idx = max(prefix.rfind(open_p) for open_p in _OPEN_PARENS)
    if open_idx == -1:
        return prefix, None
    base = prefix[:open_idx].strip()
    label = prefix[open_idx + 1 : -1].strip()
    if base and label:
        return base, label
    return prefix, None


def parse_script(
    text: str,
    default_start_speaker: str = "zundamon",
    default_background: Optional[str] = None,
    warnings: Optional[list[str]] = None,
) -> list[Scene]:
    """台本テキストを解析し、Sceneのリストを生成する。

    Args:
        text: 台本本文（複数行）。
        default_start_speaker: 話者指定のない最初の行に使う話者（以降、明示的に話者が
            指定されるまでは同じ話者が続けて話す扱いになる）。
        default_background: 生成する各シーンに設定する背景画像/動画パス（Noneなら背景なし）。
        warnings: 表情名が見つからない等、処理を継続しつつ利用者に伝えたい注意事項を
            追記するためのリスト（省略可。渡さない場合は内部で使い捨てる）。

    テキストが空、あるいは全行が空行のみの場合は空リストを返す（エラーにはしない）。
    """
    if warnings is None:
        warnings = []
    if not text:
        return []

    label_map = _build_speaker_label_map()
    characters = list_characters() or ["zundamon", "shikoku_metan"]
    expression_maps = _build_expression_label_maps(characters)
    # 話者指定の無い行に使う「現在の話者・表情」。最初はdefault_start_speaker/その既定表情で、
    # 明示的に話者・表情が指定された行に出会うたびに更新される（＝それ以降の無指定行にも引き継がれる）。
    # 以前は行ごとに相手キャラへ自動で交互切り替えしていたが、掛け合いでない台本（同じキャラが
    # 何行も続けて話す場合）に不便なうえ、交互切り替えを避けるために毎行「話者名:」を書く必要があり
    # 表記ミスで話者名や表情名がそのまま読み上げテキストに混入する事故が増えていたため、
    # 「指定があるまで同じ話者・表情が続く」方式に変更した。
    current_speaker = default_start_speaker if default_start_speaker in characters else characters[0]
    current_expression = get_default_expression(current_speaker)

    scenes: list[Scene] = []

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        speaker: Optional[str] = None
        expression: Optional[str] = None
        unrecognized_label: Optional[str] = None  # 話者名らしいのに認識できなかった場合の警告用

        for sep in _SPEAKER_SEPARATORS:
            if sep not in line:
                continue
            prefix, _, rest = line.partition(sep)
            base_prefix, expr_label = _split_expression_suffix(prefix.strip())
            candidate = label_map.get(base_prefix)
            if candidate:
                speaker = candidate
                line = rest.strip()
                if expr_label:
                    resolved = expression_maps.get(speaker, {}).get(expr_label)
                    if resolved:
                        expression = resolved
                    else:
                        display_name = get_character_display_name(speaker)
                        warnings.append(
                            f"{line_no}行目: 表情「{expr_label}」が話者「{display_name}」に見つからなかったため、"
                            "既定の表情を使用しました。"
                        )
            elif expr_label is not None and _looks_like_speaker_label(base_prefix):
                # 「話者名(表情):」というカッコ付きの書き方をしているのに話者名が認識できない
                # ＝未登録のニックネーム等の書き間違いである可能性が高いケースに限定して扱う
                # （カッコの無い単なる「なにか:」は日常的な文章でも普通に登場するため対象外）。
                # 話者の切り替えはしないが、話者名部分が読み上げテキストに混入してしまわない
                # よう、コロンより後ろだけを残す。
                unrecognized_label = base_prefix
                line = rest.strip()
            break

        if speaker is None:
            # 話者指定なし → 直前の話者・表情をそのまま引き継ぐ（単なる改行として扱う）
            speaker = current_speaker
            expression = current_expression
            if unrecognized_label is not None:
                display_name = get_character_display_name(current_speaker)
                warnings.append(
                    f"{line_no}行目: 話者名「{unrecognized_label}」を認識できなかったため、"
                    f"話者指定なしの通常の行として扱いました（そのまま「{display_name}」が続けて話します）。"
                    "「ずんだもん」「四国めたん」など、認識できる話者名で書くと切り替わります。"
                )
        else:
            if expression is None:
                # 表情の指定は無かった → 話者が変わった場合はその話者の既定表情、
                # 同じ話者をあえて重ねて書いただけの場合は直前の表情をそのまま維持する
                expression = current_expression if speaker == current_speaker else get_default_expression(speaker)
            # 明示的に指定された話者・表情は、以降の無指定行にも引き継がれる
            current_speaker = speaker
            current_expression = expression

        if not line:
            # "ずんだもん:" のように話者名(+表情)だけでテキストが空の行は読み飛ばす
            continue

        # 直前のシーンと話者・表情が同じなら、新しいシーンを作らず改行で連結する
        # （台本中の単なる読みやすさのための改行が、意図せず別カットになってしまうのを防ぐ）
        if scenes and scenes[-1].speaker == speaker and scenes[-1].expression == expression:
            merged_scene = scenes[-1]
            merged_scene.text = f"{merged_scene.text}\n{line}"
            merged_scene.duration = estimate_duration(merged_scene.text) or DEFAULT_DURATION_FALLBACK
            continue

        scenes.append(
            Scene(
                background_path=default_background,
                speaker=speaker,
                expression=expression,
                text=line,
                duration=estimate_duration(line) or DEFAULT_DURATION_FALLBACK,
            )
        )

    return scenes
