"""
PyInstallerで実行ファイル化する際のエントリーポイント。

Streamlitアプリは通常 `streamlit run app.py` というコマンドで起動するが、
実行ファイル(exe/app)ではダブルクリックだけで起動できる必要があるため、
Streamlit自身のCLI(streamlit.web.cli)をこのスクリプトの中から直接呼び出す
（購入者はPythonのコマンドラインを一切触らなくてよい）。

ビルド方法の詳細は packaging/BUILD.md を参照。
"""
from __future__ import annotations

import os
import sys


def resource_path(relative_path: str) -> str:
    """開発時・PyInstallerでの実行時のどちらでも、同梱データファイルの実パスを解決する。

    PyInstallerで --onefile / --onedir ビルドした場合、同梱したデータは実行時に
    一時展開先(sys._MEIPASS)に配置されるため、それを優先的に参照する。
    通常のPython実行時（開発中）はこのファイルからの相対パスで解決する。
    """
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path is None:
        base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base_path, relative_path)


def main() -> None:
    # Streamlitの起動オプションはコマンドライン引数(sys.argv)経由で渡す必要があるため、
    # このプロセス自身のsys.argvを書き換えてからStreamlitのCLIエントリーポイントを呼ぶ。
    import streamlit.web.cli as stcli

    app_path = resource_path("app.py")
    sys.argv = [
        "streamlit",
        "run",
        app_path,
        "--global.developmentMode=false",
        "--browser.gatherUsageStats=false",
        "--server.headless=false",  # ローカルPCで使うツールのため、ブラウザを自動で開かせる
    ]
    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
