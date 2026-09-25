"""
プロジェクト直下の .env ファイルから設定（APIキー等）を読み込む。

python-dotenv 等の追加ライブラリは使わず、"KEY=VALUE" 形式の行だけを読む簡易版。
# で始まる行（"# === Anthropic ===" のような見出しを含む）と空行は無視する。
既に環境変数として設定されている値は上書きしない（OSの環境変数 > .env の優先順）。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


def load_env(path: Path = ENV_PATH) -> None:
    """.env の内容を os.environ に読み込む（未設定のキーだけ）。ファイルが無ければ何もしない。"""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # 値の後ろの「 # コメント」を取り除き、引用符で囲まれていれば外す
        if value and value[0] in "\"'" and value[-1:] == value[0]:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and value and not os.environ.get(key):
            os.environ[key] = value
