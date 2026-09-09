"""
PyInstaller用のカスタムフック: streamlit本体のメタデータ・静的ファイル(フロントエンド一式)を
確実にビルドへ含めるためのもの。streamlitはパッケージメタデータや同梱データファイルを
実行時に参照する箇所があり、PyInstallerの自動解析だけでは漏れることがあるため明示している。

packaging/BUILD.md の手順で `--additional-hooks-dir packaging/hooks` を指定してビルドすると
このフックが読み込まれる。
"""
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

datas = copy_metadata("streamlit")
datas += collect_data_files("streamlit")
