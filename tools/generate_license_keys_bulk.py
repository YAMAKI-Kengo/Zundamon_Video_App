"""
【販売者専用スクリプト】
Payhip等の「事前生成キーをアップロードする」機能向けに、ライセンスキーを
まとめてN件発行し、1行1キーのテキストファイルに書き出す。

tools/generate_license_key.py は購入者ごとに都度手動で実行する想定だが、
Payhipの「Software License Keys」機能は、事前にまとめて生成したキーの
リストをアップロードしておき、購入が発生するたびに自動で1つずつ割り当てる
運用ができる。このスクリプトはそのキーリストを作るためのもの。

事前準備: tools/generate_keypair.py を1回実行し、このスクリプトと同じフォルダに
private_key.pem がある状態にしておくこと。

使い方:
    python tools/generate_license_keys_bulk.py <発行件数> [出力ファイル名]

例（100件発行し、license_keys.csv に書き出す場合）:
    python tools/generate_license_keys_bulk.py 100
    python tools/generate_license_keys_bulk.py 100 license_keys.csv

出力されるファイルは1行1キーのプレーンテキスト（拡張子は.csvでも中身はただの
テキスト）。Payhipの商品設定画面でこのファイルをアップロードする形を想定している
（アップロード形式の詳細は都度Payhip側の最新の仕様を確認すること）。

このスクリプトで発行したキーの「識別子(holder)」は "bulk-XXXXXX"
という連番になる（Payhipの事前生成キー機能では、アップロード時点で
購入者が誰かはまだ分からないため）。誰にどのキーが渡ったかは、
Payhip側の購入履歴とキー発行順の対応で追跡することになる。

出力ファイルには実際に使える正規のライセンスキーが平文で並ぶため、
生成後は他人に見られない場所に保管し、アップロードが終わったら
安全に削除・移動すること。
"""
from __future__ import annotations

import sys
from pathlib import Path

from generate_license_key import generate_license_key

DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "license_keys.csv"


def generate_bulk(count: int, output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    lines = []
    for i in range(1, count + 1):
        holder = f"bulk-{i:06d}"
        lines.append(generate_license_key(holder))

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip().isdigit():
        print("使い方: python tools/generate_license_keys_bulk.py <発行件数> [出力ファイル名]")
        sys.exit(1)

    count = int(sys.argv[1])
    if count <= 0:
        print("発行件数は1以上の整数を指定してください。")
        sys.exit(1)

    output_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else DEFAULT_OUTPUT_PATH

    try:
        generate_bulk(count, output_path)
    except FileNotFoundError as e:
        print(f"エラー: {e}")
        sys.exit(1)

    print(f"{count}件のライセンスキーを発行し、{output_path} に書き出しました。")
    print("このファイルの中身（キーの一覧）は、Payhipの事前生成キーアップロード機能に")
    print("そのまま読み込ませてください。アップロードが終わったら、このファイルは")
    print("安全な場所に移すか削除することをおすすめします。")


if __name__ == "__main__":
    main()
