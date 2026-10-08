"""
【販売者専用・最初に1回だけ実行するスクリプト】
ライセンスキーの署名・検証に使うRSA鍵ペア（秘密鍵・公開鍵）を生成する。

使い方:
    python tools/generate_keypair.py

実行すると、このスクリプトと同じフォルダに以下の2つのファイルが作られます。

  - private_key.pem … 秘密鍵。【絶対に配布物に含めない・外部に漏らさない・
      GitHub等に絶対にpushしない】こと。この鍵が漏れると、誰でも好きな名前で
      「本物として検証が通ってしまう」ライセンスキーを作れるようになってしまいます。
      パスワードマネージャや暗号化ドライブなど、安全な場所に保管してください。
  - public_key.pem  … 公開鍵。中身をそのまま
      src/utils/license_check.py の PUBLIC_KEY_PEM に貼り付けて、
      アプリと一緒に配布して問題ありません（公開鍵なので漏れても偽造はできません）。

このスクリプト自体（tools/フォルダ）は購入者に配布しないでください。
"""
from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

OUTPUT_DIR = Path(__file__).resolve().parent
PRIVATE_KEY_PATH = OUTPUT_DIR / "private_key.pem"
PUBLIC_KEY_PATH = OUTPUT_DIR / "public_key.pem"


def main() -> None:
    if PRIVATE_KEY_PATH.exists() or PUBLIC_KEY_PATH.exists():
        answer = input(
            f"{PRIVATE_KEY_PATH.name} または {PUBLIC_KEY_PATH.name} が既に存在します。"
            "上書きすると、これまで発行した既存のライセンスキーが全て無効になります。"
            "本当に上書きしますか？ (yes と入力すると続行): "
        )
        if answer.strip().lower() != "yes":
            print("中止しました。")
            return

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    PRIVATE_KEY_PATH.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    PUBLIC_KEY_PATH.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    print(f"鍵ペアを生成しました:\n  {PRIVATE_KEY_PATH}  ← 絶対に配布・公開しないこと\n  {PUBLIC_KEY_PATH}  ← アプリに埋め込んで配布してよい\n")
    print("次の手順: public_key.pem の中身を、")
    print("src/utils/license_check.py の PUBLIC_KEY_PEM にそのままコピー＆ペーストしてください。")


if __name__ == "__main__":
    main()
