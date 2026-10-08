"""
PyInstaller用のカスタムフック: pywebview(webview)本体を確実にビルドへ含めるためのもの。

pywebviewはOSごとに異なるバックエンド（Windowsなら pythonnet 経由の
Edge WebView2、Macなら pyobjc 経由の WKWebView 等）を実行時に動的に読み込むため、
PyInstallerの自動解析だけでは一部のモジュールが漏れることがある。
collect_all() で関連する datas / binaries / hiddenimports をまとめて含める。

packaging/BUILD.md の手順で `--additional-hooks-dir packaging/hooks` を指定して
ビルドするとこのフックが読み込まれる。
"""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = collect_all("webview")
