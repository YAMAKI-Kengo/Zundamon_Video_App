"""
ライセンスキーの検証（note等での有料配布用）。

このアプリを実行ファイル化して有料配布する場合に、購入者ごとに発行したライセンス
キーが無いと使えないようにするための、シンプルなオフライン検証の仕組み。

【重要】これは「絶対に破られない」DRMではありません。オフラインで動くソフトウェアで
ある以上、実行ファイルを解析されたり、正規に発行されたキーそのものをコピーして
使い回されたりすることを完全に防ぐことは原理的にできません。ここでの目的は、
  - 通りすがりのコピー・横流しに対する現実的なハードルを一段上げること
  - 購入者ごとに異なるキーを発行することで、後から見て「誰に渡したキーか」を
    把握できるようにすること（明らかな不正配布が見つかった場合の抑止力・手がかり）
という「現実的な範囲での抑止力」です。販売にあたっては、これとセットで
SELLING_GUIDE.md の利用規約（EULA）をきちんと購入者に提示・同意してもらうことが
重要です（技術的な対策だけでなく、利用規約という「ルール」の両輪で運用してください）。

【方式：非対称暗号（RSA署名）】
販売者だけが持つ秘密鍵でライセンスキーに署名し、アプリ（配布物）側には公開鍵だけを
埋め込んで検証します。これにより、実行ファイルを解析されてこの公開鍵が読み取られても、
「新しい・別人名義の」有効なライセンスキーを偽造することはできません
（秘密鍵が無いと署名できないため）。既存の正規キーがコピーされて複数人で使い回される
こと自体は、完全なオフライン動作である以上どうしても防げませんが、それは
本人確認なしで販売するデジタル商品全般に共通する限界です。

セットアップの流れ:
  1. tools/generate_keypair.py を（販売者が）1回だけ実行し、
     private_key.pem（絶対に配布しない・厳重に保管する） と
     public_key.pem（アプリに埋め込んでよい） を生成する。
  2. public_key.pem の中身を、このファイルの PUBLIC_KEY_PEM にそのまま貼り付ける。
  3. 購入者ごとに tools/generate_license_key.py を実行してライセンスキー文字列を
     発行し、そのキー文字列だけを購入者に渡す（private_key.pem やこのスクリプト
     自体は購入者に渡さない）。
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# 販売者の公開鍵(PEM形式)をここに埋め込む。
# tools/generate_keypair.py で生成した public_key.pem の中身をそのまま貼り付けること。
# （公開鍵なので、これがそのまま配布物に含まれて漏れても問題はない。
#  絶対に秘密鍵(private_key.pem)の中身をここに書かないこと）
# NOTE: bytesリテラルはASCII文字しか書けないため、下のプレースホルダ文言は日本語ではなく
# 英語にしてある。実際にpublic_key.pemの中身（PEM形式のBase64文字列）を貼り付ければ、
# ASCIIのみで構成されるため問題なく動作する。
PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
PASTE the contents of tools/generate_keypair.py's public_key.pem output here.
-----END PUBLIC KEY-----
"""

# PUBLIC_KEY_PEMがまだ上記プレースホルダーのまま（＝販売者がまだ鍵ペアを設定していない）
# かどうかの判定に使うマーカー文字列。
_PUBLIC_KEY_PLACEHOLDER_MARKER = b"PASTE the contents"


def is_configured() -> bool:
    """PUBLIC_KEY_PEMに実際の公開鍵が設定済みか（プレースホルダーのままでないか）を返す。

    開発中（＝まだSELLING_GUIDE.mdの手順で鍵ペアを設定していない）状態のまま
    ライセンス認証を強制してしまうと、開発者自身が自分のアプリを使えなくなって
    しまうため、app.py側ではこれがFalseの間は認証をスキップしつつ警告を表示する
    運用にしている。
    """
    return _PUBLIC_KEY_PLACEHOLDER_MARKER not in PUBLIC_KEY_PEM

# 購入者ごとの認証済みライセンスキーを保存しておく場所。
# 一度認証に成功すれば、次回起動時からはここに保存されたキーで自動的に認証される
# （毎回キーを入力し直さなくてよいようにするため）。
LICENSE_STORE_PATH = Path.home() / ".zunda_video_app" / "license.key"

# 開発・動作確認中はライセンス認証をスキップしたい場合、環境変数
# ZUNDA_APP_DISABLE_LICENSE=1 を設定して起動すると認証画面をバイパスできる
# （販売用に実行ファイル化する際は、この環境変数を設定しない状態でビルドすること）。
LICENSE_ENFORCEMENT_ENABLED = os.environ.get("ZUNDA_APP_DISABLE_LICENSE") != "1"


@dataclass
class LicenseInfo:
    holder: str
    issued: Optional[str]
    raw_key: str


def _b64url_decode(data: str) -> bytes:
    """パディング("=")を省略したURL-safe Base64文字列を正しくデコードする。"""
    padding_needed = (-len(data)) % 4
    return base64.urlsafe_b64decode(data + "=" * padding_needed)


def _load_public_key():
    return serialization.load_pem_public_key(PUBLIC_KEY_PEM)


def verify_license_key(license_key: str) -> Optional[LicenseInfo]:
    """ライセンスキー文字列を検証する。有効なら LicenseInfo を、無効なら None を返す。

    キーの形式は "ペイロード部分(Base64).署名部分(Base64)" で、
    tools/generate_license_key.py が生成するものに対応している。
    改ざん・偽造・破損したキーはもちろん、公開鍵の設定漏れ（PUBLIC_KEY_PEM未設定）や
    ライブラリ由来の例外もすべてここで捕捉し、呼び出し側には常に None として返す
    （認証系の処理は、想定外のエラーで例外を漏らしてアプリを落とすのではなく、
    「認証失敗」として安全側に倒すのが原則のため）。
    """
    if not license_key or "." not in license_key:
        return None

    try:
        payload_b64, sig_b64 = license_key.strip().split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        signature = _b64url_decode(sig_b64)
        payload = json.loads(payload_bytes.decode("utf-8"))
        holder = payload["holder"]
        issued = payload.get("issued")
    except Exception:  # noqa: BLE001 - キーの形式が想定と違う場合は全て「無効なキー」として扱う
        return None

    try:
        public_key = _load_public_key()
        public_key.verify(
            signature,
            payload_bytes,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
    except InvalidSignature:
        return None
    except Exception:  # noqa: BLE001 - 公開鍵が未設定/不正な場合も含め、検証失敗はすべて「無効」として扱う
        return None

    return LicenseInfo(holder=holder, issued=issued, raw_key=license_key.strip())


def save_license_key(license_key: str) -> None:
    """認証に成功したライセンスキーを保存し、次回起動時から自動認証されるようにする。"""
    LICENSE_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LICENSE_STORE_PATH.write_text(license_key.strip(), encoding="utf-8")


def load_saved_license() -> Optional[LicenseInfo]:
    """保存済みのライセンスキーを読み込んで検証する。未保存/無効な場合は None を返す。"""
    if not LICENSE_STORE_PATH.exists():
        return None
    try:
        saved = LICENSE_STORE_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    return verify_license_key(saved)
