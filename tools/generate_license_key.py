"""
【販売者専用・購入者ごとに実行するスクリプト】
購入者に渡すライセンスキーを1件発行する。

事前準備: tools/generate_keypair.py を1回実行し、このスクリプトと同じフォルダに
private_key.pem がある状態にしておくこと。

使い方:
    python tools/generate_license_key.py "購入者名やメールアドレスなど識別子"

例:
    python tools/generate_license_key.py "yamada_taro@example.com"

出力されたライセンスキー文字列（1行の長い英数字）だけを購入者に渡してください。
このスクリプトファイル自体や private_key.pem は購入者に渡さないでください。

購入者ごとに識別子（名前・メールアドレス・note注文番号など）を変えて実行すれば、
それぞれ別々の、しかし全て正規に検証が通るライセンスキーが発行できます。
発行したキーと識別子の対応は、このスクリプトでは記録しないため、
必要であれば販売者側で別途（表計算ソフト等に）控えておいてください
（購入者からの問い合わせ対応や、後から見て「誰に渡したキーか」を追跡するため）。
"""
from __future__ import annotations

import base64
import json
import sys
from datetime import date, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

DEFAULT_PRIVATE_KEY_PATH = Path(__file__).resolve().parent / "private_key.pem"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_license_key(holder: str, private_key_path: Path = DEFAULT_PRIVATE_KEY_PATH) -> str:
    if not private_key_path.exists():
        raise FileNotFoundError(
            f"{private_key_path} が見つかりません。先に tools/generate_keypair.py を実行して、"
            "秘密鍵を作成してください。"
        )

    private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)

    payload = {"holder": holder, "issued": date.today().isoformat()}
    payload_bytes = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

    signature = private_key.sign(
        payload_bytes,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )

    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print('使い方: python tools/generate_license_key.py "購入者名やメールアドレスなど識別子"')
        sys.exit(1)

    holder = sys.argv[1].strip()
    try:
        key = generate_license_key(holder)
    except FileNotFoundError as e:
        print(f"エラー: {e}")
        sys.exit(1)

    print(f"購入者「{holder}」向けのライセンスキーを発行しました。このキーだけを購入者に渡してください:\n")
    print(key)


if __name__ == "__main__":
    main()
