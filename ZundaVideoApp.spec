# -*- mode: python ; coding: utf-8 -*-
"""
ずんだもん・四国めたん 解説動画ジェネレーターを「完全なデスクトップアプリ」として
ビルドするための PyInstaller 仕様ファイル。

エントリーポイントは packaging/launcher.py（Streamlitサーバーを裏で起動し、
pywebview のネイティブウィンドウに表示する。ブラウザのタブは開かない）。

ビルド手順の詳細は packaging/BUILD.md を参照。
    pyinstaller ZundaVideoApp.spec --clean
"""

from PyInstaller.utils.hooks import collect_all

# アプリが実行時に参照するソース・設定・素材を同梱する。
# launcher.resource_path() は sys._MEIPASS（PyInstallerの展開先）を基準に
# "app.py" / "src" / "config" / "assets" を解決するため、展開先ルート('.')へ配置する。
datas = [
    ('app.py', '.'),
    ('src', 'src'),
    ('config', 'config'),
    ('assets', 'assets'),
]
binaries = []
hiddenimports = []

# moviepy が動画エンコードに使う ffmpeg バイナリ（imageio-ffmpeg 同梱）と、
# moviepy が依存する imageio 本体（import時に自身のパッケージメタデータを参照する）を
# 確実に含める。これが漏れると購入者の環境で動画生成・起動が失敗する。
for _pkg in ('imageio_ffmpeg', 'imageio', 'moviepy'):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h


a = Analysis(
    ['packaging/launcher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=['packaging/hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ZundaVideoApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['packaging/app_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ZundaVideoApp',
)
