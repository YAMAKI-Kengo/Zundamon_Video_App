"""
PyInstaller用のカスタムフック: streamlit本体のメタデータ・静的ファイル(フロントエンド一式)・
サブモジュールを確実にビルドへ含めるためのもの。

streamlitはパッケージメタデータや同梱データファイルを実行時に参照するほか、
スクリプト実行時に `streamlit.runtime.scriptrunner.magic_funcs` などのサブモジュールを
動的import（遅延import）する。PyInstallerの静的解析だけではこれらが漏れ、
実行時に ModuleNotFoundError で画面が真っ白になるため、collect_submodules() で
streamlit配下のサブモジュールをまとめてhiddenimportsに含めている。

packaging/BUILD.md の手順で `--additional-hooks-dir packaging/hooks` を指定してビルドすると
このフックが読み込まれる。
"""
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

datas = copy_metadata("streamlit")
datas += collect_data_files("streamlit")

# 動的importされるサブモジュール（magic_funcs 等）を取りこぼさないように全部入れる
hiddenimports = collect_submodules("streamlit")
