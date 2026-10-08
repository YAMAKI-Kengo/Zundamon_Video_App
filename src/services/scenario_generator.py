"""
台本（シナリオ）の自動生成。

ローカルで動く [Ollama](https://ollama.com/) の HTTP API（既定 http://localhost:11434）を
使って、「ずんだもん」と「四国めたん」の掛け合い解説台本を生成する。生成結果は
src/services/script_import.py の parse_script() がそのまま解釈できる

    ずんだもん: セリフ
    四国めたん: セリフ
    ずんだもん(喜び): セリフ

という行区切りのテキストで返す。UI 側はこれを「台本を貼り付け」欄に流し込み、
ユーザーが内容を確認・修正してから「シーンを一括生成」する想定。

VOICEVOX と同じく「プログラミング知識のないユーザーへの配布」を前提にしているため、
- Ollama を手動で起動していなくても、インストール済みなら自動でバックグラウンド起動する
- モデル未取得・接続不可などは落ちずに分かりやすい日本語メッセージにする
という方針をとる（voicevox_client.py と対になる作り）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterator, Optional

import requests

# --- 設定 -------------------------------------------------------------------

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "").strip() or "http://localhost:11434"

# 既定で使うモデル。環境変数 ZUNDA_SCENARIO_MODEL で上書きできる。
DEFAULT_MODEL = os.environ.get("ZUNDA_SCENARIO_MODEL", "").strip() or "qwen2.5:3b"

# インストール済みモデルから自動選択するときの優先順位（軽い順に日本語がある程度書けるもの）。
PREFERRED_MODELS = [
    DEFAULT_MODEL,
    "qwen2.5:3b",
    "qwen2.5:7b",
    "llama3.2:3b",
    "gemma2:2b",
    "qwen2.5:1.5b",
]

CONNECT_TIMEOUT = 3.0          # 起動確認・一覧取得の接続タイムアウト（秒）
GENERATE_READ_TIMEOUT = 600.0  # 生成の応答タイムアウト（秒）。CPU実行だと数分かかるため長め
STARTUP_POLL_INTERVAL = 0.5
try:
    STARTUP_WAIT_TIMEOUT = float(os.environ.get("OLLAMA_STARTUP_TIMEOUT", "") or 40.0)
except ValueError:
    STARTUP_WAIT_TIMEOUT = 40.0

# Some NVIDIA driver / CUDA runner combinations can make Ollama return HTTP 500
# before a model starts generating.  Keep this fallback opt-out so advanced users
# can manage the runner themselves, but make the app usable out of the box.
CPU_FALLBACK_ENABLED = os.environ.get("ZUNDA_OLLAMA_CPU_FALLBACK", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
CPU_FALLBACK_LIBRARY = os.environ.get("ZUNDA_OLLAMA_CPU_LIBRARY", "cpu").strip() or "cpu"

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 自動起動した Ollama サーバーのPID。アプリ終了時に launcher.py が読んで後始末する
# （Streamlitサーバーとは別プロセスのため、共有できる一時ディレクトリ上の固定パスで受け渡す）。
OLLAMA_PID_FILE = Path(tempfile.gettempdir()) / "zundavideoapp_ollama.pid"

_ollama_process: Optional[subprocess.Popen] = None
_auto_start_attempted = False

# 表情タグに使える値（config/characters.json の expressions 日本語ラベルと合わせる）。
_ALLOWED_EXPRESSION_LABELS = ["普通", "喜び", "悲しみ", "怒り", "驚き", "困り"]


# --- 例外 -----------------------------------------------------------------

class ScenarioGeneratorError(Exception):
    """台本自動生成まわりの基底例外。"""


class OllamaConnectionError(ScenarioGeneratorError):
    """Ollama に接続できない（未インストール / 起動できない / タイムアウト）。"""


class OllamaModelMissingError(ScenarioGeneratorError):
    """指定（または既定）のモデルが Ollama に取り込まれていない。"""


class ScenarioGenerationError(ScenarioGeneratorError):
    """接続はできたが、生成リクエスト自体が失敗した / 出力が想定形式でなかった。"""


# --- Ollama の起動確認・自動起動 -------------------------------------------

def _ollama_is_up(base_url: str = OLLAMA_BASE_URL) -> bool:
    """Ollama サーバーが応答するかどうかを返す（例外は投げない）。"""
    for path in ("/api/version", "/api/tags"):
        try:
            resp = requests.get(f"{base_url}{path}", timeout=CONNECT_TIMEOUT)
            if resp.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            continue
    return False


def _candidate_ollama_executables() -> list[Path]:
    """インストール済み Ollama の実行ファイル候補を優先度順で返す。

    環境変数 OLLAMA_PATH で直接指定も可能。
    """
    candidates: list[Path] = []

    env_override = os.environ.get("OLLAMA_PATH")
    if env_override:
        candidates.append(Path(env_override))

    which = shutil.which("ollama")
    if which:
        candidates.append(Path(which))

    roots: list[Path] = []
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        roots.append(Path(localappdata) / "Programs" / "Ollama")
    for pf_env in ("ProgramFiles", "ProgramFiles(x86)"):
        pf = os.environ.get(pf_env)
        if pf:
            roots.append(Path(pf) / "Ollama")
    # macOS / Linux の一般的な配置
    roots.append(Path("/usr/local/bin"))
    roots.append(Path("/opt/homebrew/bin"))
    roots.append(Path.home() / ".local" / "bin")

    for root in roots:
        candidates.append(root / ("ollama.exe" if os.name == "nt" else "ollama"))
        candidates.append(root / "ollama app.exe")

    result: list[Path] = []
    seen: set[Path] = set()
    for c in candidates:
        try:
            if c not in seen and c.is_file():
                seen.add(c)
                result.append(c)
        except OSError:
            pass
    return result


def _spawn_ollama(llm_library: Optional[str] = None) -> bool:
    """インストール済み Ollama をバックグラウンドで起動する。見つからなければ False。"""
    global _ollama_process

    exe = next(iter(_candidate_ollama_executables()), None)
    if exe is None:
        return False

    # `ollama serve` が API サーバー本体。デスクトップアプリ本体（"ollama app.exe"）は
    # 引数なしで起動するとサーバーも立ち上がる。
    if exe.name.lower() == "ollama app.exe":
        args = [str(exe)]
    else:
        args = [str(exe), "serve"]

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    env = os.environ.copy()
    if llm_library:
        env["OLLAMA_LLM_LIBRARY"] = llm_library
    try:
        _ollama_process = subprocess.Popen(
            args,
            cwd=str(exe.parent),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError:
        return False

    try:
        OLLAMA_PID_FILE.write_text(str(_ollama_process.pid), encoding="ascii")
    except OSError:
        pass
    return True


def _is_cuda_backend_failure(response: requests.Response) -> bool:
    """Return True only for the GPU runner failures that CPU fallback can fix."""
    if response.status_code != 500:
        return False
    try:
        detail = response.text.lower()
    except Exception:  # noqa: BLE001 - an unreadable response is not a fallback signal
        return False
    return "cuda error" in detail and (
        "kernel image" in detail
        or "llama-server process has terminated" in detail
        or "invalid device" in detail
    )


def _stop_ollama_for_cpu_fallback() -> None:
    """Stop the broken Windows GUI/server pair after a confirmed CUDA crash."""
    if os.name != "nt":
        return
    for image_name in ("ollama app.exe", "ollama.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", image_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            pass


def _restart_ollama_on_cpu(base_url: str) -> bool:
    """Restart Ollama with a CPU runner after a confirmed GPU-only failure."""
    global _ollama_process, _auto_start_attempted

    _stop_ollama_for_cpu_fallback()
    _ollama_process = None
    _auto_start_attempted = True
    if not _spawn_ollama(CPU_FALLBACK_LIBRARY):
        return False
    return _wait_until_up(base_url, STARTUP_WAIT_TIMEOUT)


def _wait_until_up(base_url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ollama_is_up(base_url):
            return True
        if _ollama_process is not None and _ollama_process.poll() is not None:
            # 自分で起動したサーバーが即終了した場合（＝別のサーバーが既に 11434 を
            # 使っている等）は、そのサーバーが応答するなら成功、しないなら打ち切る。
            return _ollama_is_up(base_url)
        time.sleep(STARTUP_POLL_INTERVAL)
    return False


def ensure_ollama_running(base_url: str = OLLAMA_BASE_URL) -> None:
    """Ollama を使える状態にする。未起動なら自動起動し、それでも駄目なら例外を送出する。"""
    global _auto_start_attempted

    if _ollama_is_up(base_url):
        return

    if not _auto_start_attempted:
        _auto_start_attempted = True
        if _spawn_ollama():
            _wait_until_up(base_url, STARTUP_WAIT_TIMEOUT)
    elif _ollama_process is not None and _ollama_process.poll() is None:
        _wait_until_up(base_url, STARTUP_WAIT_TIMEOUT)

    if _ollama_is_up(base_url):
        return

    raise OllamaConnectionError(
        "Ollamaを起動できませんでした。Ollama（https://ollama.com/）がインストールされているか"
        "確認してください。インストール済みの場合は Ollama を起動してから再度お試しください。"
    )


# --- モデル一覧・取得 ----------------------------------------------------

def list_models(base_url: str = OLLAMA_BASE_URL) -> list[str]:
    """Ollama に取り込み済みのモデル名一覧を返す。

    UI から毎回呼ばれるため、ここでは自動起動は行わない（起動していなければ例外）。
    """
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=CONNECT_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise OllamaConnectionError("Ollamaに接続できませんでした。") from e
    try:
        models = resp.json().get("models", []) or []
        return [m.get("name", "") for m in models if m.get("name")]
    except (ValueError, AttributeError) as e:
        raise OllamaConnectionError("Ollamaの応答を解釈できませんでした。") from e


def _resolve_model(model: Optional[str], base_url: str) -> str:
    """使うモデル名を決める。指定があればそれを、無ければインストール済みから選ぶ。"""
    if model and model.strip():
        return model.strip()

    try:
        installed = list_models(base_url)
    except OllamaConnectionError:
        installed = []

    if installed:
        for preferred in PREFERRED_MODELS:
            if preferred in installed:
                return preferred
        return installed[0]

    raise OllamaModelMissingError(
        f"使用できるモデルがOllamaにありません。ターミナルで `ollama pull {DEFAULT_MODEL}` を"
        "実行してモデルを取得するか、UIの「モデルを取得する」ボタンを押してください。"
    )


def pull_model(model: str, base_url: str = OLLAMA_BASE_URL) -> Iterator[tuple[str, int, int]]:
    """`ollama pull` 相当のダウンロードを実行し、進捗を (status, completed, total) で逐次返す。

    呼び出し側で for ループしながら進捗バーを更新する想定。完了時に正常終了する。
    """
    ensure_ollama_running(base_url)
    try:
        resp = requests.post(
            f"{base_url}/api/pull",
            json={"name": model, "stream": True},
            stream=True,
            timeout=(CONNECT_TIMEOUT, None),
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise ScenarioGenerationError(f"モデルの取得を開始できませんでした: {e}") from e

    for raw in resp.iter_lines():
        if not raw:
            continue
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if payload.get("error"):
            raise ScenarioGenerationError(str(payload["error"]))
        status = str(payload.get("status", ""))
        completed = int(payload.get("completed", 0) or 0)
        total = int(payload.get("total", 0) or 0)
        yield status, completed, total


# --- プロンプト構築 ----------------------------------------------------

_SYSTEM_PROMPT = (
    "あなたは日本語のYouTube解説動画の台本作家です。"
    "「ずんだもん」と「四国めたん」の2人が掛け合いで解説する台本を書きます。\n"
    "厳守するルール:\n"
    "1. 出力は台本本文だけ。前置き・後書き・見出し・箇条書き記号・Markdown・英語での説明は一切書かない。\n"
    "2. すべての行を `ずんだもん: セリフ` または `四国めたん: セリフ` の形式にする（行頭に必ず話者名とコロン）。\n"
    "3. ずんだもんの口調は語尾が「〜のだ」「〜なのだ」。無邪気で元気。一人称は「ボク」。\n"
    "4. 四国めたんの口調は落ち着いた丁寧な女性口調（「〜ですわ」「〜かしら」など）。\n"
    "5. 2人が交互に話すのを基本に、自然で分かりやすい会話にする。1行は1〜2文程度で短く。\n"
    "6. 専門用語は噛み砕く。最初の行で話題を紹介し、最後の行で軽くまとめる。\n"
)


def _estimate_line_count(target_seconds: Optional[int], scene_count: Optional[int]) -> Optional[int]:
    if scene_count and scene_count > 0:
        return int(scene_count)
    if target_seconds and target_seconds > 0:
        # 1行 ≒ 3秒（読み上げ）を目安にする。少なめに寄せて冗長になりすぎないようにする。
        return max(4, round(target_seconds / 3))
    return None


def _build_user_prompt(
    topic: str,
    target_seconds: Optional[int],
    scene_count: Optional[int],
    tone: Optional[str],
    zundamon_role: Optional[str],
    metan_role: Optional[str],
    start_speaker_label: str,
    use_expressions: bool,
) -> str:
    lines = [f"テーマ・題材: {topic.strip()}"]

    line_count = _estimate_line_count(target_seconds, scene_count)
    if scene_count and scene_count > 0:
        lines.append(f"シーン数の目安: {int(scene_count)}カット前後（＝台本{int(scene_count)}行前後）")
    elif target_seconds and target_seconds > 0:
        lines.append(f"動画の尺の目安: 約{target_seconds}秒（台本はだいたい{line_count}行前後）")
    else:
        lines.append("長さの指定はなし（10〜16行程度でまとめる）")

    if tone and tone.strip():
        lines.append(f"口調・雰囲気: {tone.strip()}")
    if zundamon_role and zundamon_role.strip():
        lines.append(f"ずんだもんの役割: {zundamon_role.strip()}")
    if metan_role and metan_role.strip():
        lines.append(f"四国めたんの役割: {metan_role.strip()}")
    lines.append(f"最初に話すのは「{start_speaker_label}」から。")

    if use_expressions:
        allowed = " / ".join(_ALLOWED_EXPRESSION_LABELS)
        lines.append(
            "感情がはっきりしている行に限り、`ずんだもん(喜び): セリフ` のように"
            f"話者名の直後に丸括弧で表情を書いてよい。使える表情は {allowed} のいずれか。"
            "迷ったら表情は付けなくてよい。"
        )

    lines.append("では台本を書いてください。1行目から台本本文を始めること。")
    return "\n".join(lines)


# --- 生成結果のクリーニング -------------------------------------------

def _known_speaker_labels() -> set[str]:
    """台本行の行頭として認める話者名の集合（表示名・内部キー・エイリアス）。

    config/characters.json をベースにするが、読み込めない環境でも動くよう
    ずんだもん・四国めたんの既定表記をフォールバックとして必ず含める。
    """
    labels = {"ずんだもん", "四国めたん", "ずんだ", "めたん", "zundamon", "shikoku_metan"}
    try:
        from src.utils.asset_loader import (
            get_character_display_name,
            list_characters,
            load_character_config,
        )

        cfg = load_character_config()
        for key in list_characters():
            labels.add(key)
            labels.add(get_character_display_name(key))
            for alias in cfg.get(key, {}).get("aliases", []):
                labels.add(str(alias))
    except Exception:  # noqa: BLE001 - 設定が読めなくてもフォールバックで続行
        pass
    return {lb.strip() for lb in labels if lb and lb.strip()}


def _clean_scenario_text(raw: str) -> str:
    """モデル出力から「ずんだもん / 四国めたん の台本行」だけを取り出す。

    - コードフェンス(```) を除去
    - 行頭の箇条書き記号（- , * , ・ , 数字.）を除去
    - 行頭が既知の話者名（表情タグ付き可）でない行（前置き・地の文・見出し・
      「ナレーション:」等）は捨てる
    """
    known = _known_speaker_labels()
    cleaned: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        # 行頭の箇条書き記号・番号（"- " "* " "・" "1. " "1) "）を剥がす
        for bullet in ("- ", "* ", "・", "> "):
            if line.startswith(bullet):
                line = line[len(bullet):].strip()
        line = re.sub(r"^\d{1,2}[.)]\s*", "", line)

        # 行頭 "話者名:" または "話者名(表情):" を取り出す（半角/全角のコロン・括弧に対応）
        m = re.match(r"^\s*([^:：(（]{1,10})\s*(?:[(（][^)）]{0,8}[)）])?\s*[:：]\s*(.+)$", line)
        if not m:
            continue
        speaker_head = m.group(1).strip()
        if speaker_head not in known:
            continue
        cleaned.append(line)

    return "\n".join(cleaned).strip()


# --- 公開関数：台本生成 ----------------------------------------------

def generate_scenario(
    topic: str,
    *,
    target_seconds: Optional[int] = None,
    scene_count: Optional[int] = None,
    tone: Optional[str] = None,
    zundamon_role: Optional[str] = None,
    metan_role: Optional[str] = None,
    start_speaker: str = "zundamon",
    use_expressions: bool = True,
    model: Optional[str] = None,
    base_url: str = OLLAMA_BASE_URL,
) -> str:
    """ローカルの Ollama で掛け合い台本を生成し、parse_script が解釈できる形式の文字列で返す。

    Raises:
        OllamaConnectionError: Ollama に接続できない（未インストール等）。
        OllamaModelMissingError: 使えるモデルが無い。
        ScenarioGenerationError: 生成に失敗、または出力が想定形式でなかった。
    """
    if not topic or not topic.strip():
        raise ScenarioGenerationError("テーマ・題材が入力されていません。")

    ensure_ollama_running(base_url)
    resolved_model = _resolve_model(model, base_url)

    start_label = "四国めたん" if start_speaker == "shikoku_metan" else "ずんだもん"
    user_prompt = _build_user_prompt(
        topic, target_seconds, scene_count, tone,
        zundamon_role, metan_role, start_label, use_expressions,
    )

    chat_payload = {
        "model": resolved_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.8, "top_p": 0.9, "num_predict": 1400},
    }

    try:
        resp = requests.post(
            f"{base_url}/api/chat",
            json=chat_payload,
            timeout=(CONNECT_TIMEOUT, GENERATE_READ_TIMEOUT),
        )
    except requests.exceptions.Timeout as e:
        raise ScenarioGenerationError(
            "台本の生成がタイムアウトしました。より小さいモデルを使うか、"
            "シーン数を減らして再度お試しください。"
        ) from e
    except requests.exceptions.RequestException as e:
        raise OllamaConnectionError("Ollamaへの生成リクエストに失敗しました。") from e

    # Ollama can advertise a GPU successfully but fail only while loading the
    # model. Retry once on CPU, but only after a response explicitly identifies
    # a CUDA runner crash so unrelated server errors are not masked.
    if CPU_FALLBACK_ENABLED and _is_cuda_backend_failure(resp):
        if not _restart_ollama_on_cpu(base_url):
            raise ScenarioGenerationError(
                "Ollama GPU backend failed and the automatic CPU fallback could not start."
            )
        try:
            resp = requests.post(
                f"{base_url}/api/chat",
                json=chat_payload,
                timeout=(CONNECT_TIMEOUT, GENERATE_READ_TIMEOUT),
            )
        except requests.exceptions.Timeout as e:
            raise ScenarioGenerationError(
                "Ollama was switched to CPU mode, but scenario generation timed out."
            ) from e
        except requests.exceptions.RequestException as e:
            raise OllamaConnectionError(
                "Could not reconnect to Ollama after switching to CPU mode."
            ) from e

    if resp.status_code == 404:
        raise OllamaModelMissingError(
            f"モデル「{resolved_model}」がOllamaにありません。"
            f"ターミナルで `ollama pull {resolved_model}` を実行するか、"
            "UIの「モデルを取得する」ボタンを押してください。"
        )
    if resp.status_code != 200:
        raise ScenarioGenerationError(
            f"Ollamaが台本生成に失敗しました（ステータスコード: {resp.status_code}）。"
        )

    try:
        content = resp.json().get("message", {}).get("content", "") or ""
    except (ValueError, AttributeError) as e:
        raise ScenarioGenerationError("Ollamaの応答を解釈できませんでした。") from e

    scenario = _clean_scenario_text(content)
    dialogue_lines = [ln for ln in scenario.splitlines() if ln.strip()]
    if len(dialogue_lines) < 2:
        raise ScenarioGenerationError(
            "台本を生成できませんでした（モデルの出力が想定した形式ではありませんでした）。"
            "もう一度お試しいただくか、別のモデルをお使いください。"
        )
    return scenario
