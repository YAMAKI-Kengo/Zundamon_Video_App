# 実行ファイル(exe/app)としてビルドする手順

このアプリ（Streamlit製）を、購入者がPython環境を用意しなくてもダブルクリックだけで
使えるように、PyInstallerで実行ファイル化する手順です。

**このアプリは「完全なデスクトップアプリ」として動作します**（`packaging/launcher.py`）。
Streamlitのサーバーは裏側（別プロセス）で起動し、画面は
[pywebview](https://pywebview.flowrl.com/) が開くOSネイティブのウィンドウに表示されます。
ブラウザのタブは一切開きません（ただしネイティブウィンドウを表示できなかった場合の
フォールバックとして、既定のブラウザで開くようにしてあります）。

> **補足:** 新しめのStreamlitは起動時に `signal.signal()` を呼ぶため、Streamlitサーバーを
> バックグラウンド「スレッド」で動かすと `ValueError: signal only works in main thread`
> で落ちます。そのため `launcher.py` はStreamlitを別「プロセス」（＝別のメインスレッド）
> として起動し、メインスレッドは pywebview のウィンドウ表示に使っています。

**重要な前提**

- ビルドは **Windows向けならWindows上で、Mac向けならMac上で** 行ってください
  （PyInstallerはクロスコンパイル非対応。Windows用exeをMacで作ることはできません）。
- 実行ファイル化しても、**VOICEVOX ENGINEは別途購入者自身にインストールしてもらう必要があります**
  （このアプリはVOICEVOXにHTTPで接続するだけで、VOICEVOX自体は同梱していません）。
  ただし**手動での起動は不要**です。生成／試聴時にVOICEVOXが起動していなければ、標準インストール先
  （`%LOCALAPPDATA%\Programs\VOICEVOX` / `Program Files\VOICEVOX`）の `vv-engine\run.exe`
  （無ければ `VOICEVOX.exe`）を自動でバックグラウンド起動し、アプリ終了時に停止します。
  非標準の場所にインストールした購入者向けに、環境変数 `VOICEVOX_ENGINE_PATH` で実行ファイルを
  直接指定できることも案内に含めてください。VOICEVOX ENGINE自体をアプリに同梱したい場合は
  `vendor/voicevox_engine/`（または PyInstaller の `voicevox_engine/`）に置けば自動検出されます。
- **Windowsでネイティブウィンドウを表示するには Microsoft Edge WebView2 Runtime が
  必要です**。Windows 10 (2004以降) / Windows 11ではOS標準でほぼ入っていますが、
  古い/最小構成のWindowsでは入っていないことがあります。念のため購入者向けの説明にも
  「起動しない場合は [Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
  をインストールしてください」と一言添えておくと安心です（ビルド作業自体には影響しません）。
- Streamlit＋PyInstaller＋pywebviewの組み合わせは、いずれかのアップデートで動かなくなることが
  時々あります。ビルド後は必ず実際に生成された実行ファイルを別のPC（開発に使ったPCとは
  別の、まっさらな環境が理想）で動作確認してください。

## 推奨する配布方針（まずはWindows版のみでOK）

検討の結果、最初のリリースは **Windows版のみ** に絞ることをおすすめします。理由は次の通りです。

- Windowsは、未署名の実行ファイルでも「詳細情報→実行」で購入者自身が起動できます
  （SmartScreenの警告は出ますが、次のセクションの文言を商品説明に載せておけば
  対応可能です）。
- 一方Macは、macOS Sequoia以降で「右クリック→開く」による回避方法自体が廃止され、
  「システム設定→プライバシーとセキュリティ」からの多段階操作が必須になりました。
  実用的にはApple Developer Program（年$99）でのnotarization（公証）が事実上必須で、
  これにはMac実機と追加のセットアップが要ります。
- まずはWindows版で需要を確認し、必要になった時点でMac対応
  （Apple Developer Program登録＋notarization）に投資する方が効率的です。

### 商品説明・READMEに載せる文言（SmartScreen対策）

購入者が「ウイルスかも」と不安にならないよう、商品ページと同梱READMEの両方に
次のような注意書きを入れることを強くおすすめします。

```
※ Windowsで起動時に「WindowsによってPCが保護されました」という青い画面
（SmartScreen）が表示されることがあります。これは未署名の実行ファイルに対する
一般的な警告であり、ウイルスではありません。「詳細情報」をクリックし、
「実行」ボタンを押して起動してください。

※ 起動すると専用のアプリウィンドウが開きます（ブラウザは使いません）。
数秒待っても真っ白なままの場合は、Microsoft Edge WebView2 Runtimeが
インストールされているかご確認ください（多くのWindows PCには標準で
入っています）。
```

## 0. 事前準備（最初の1回だけ）

```bash
cd zunda_video_app
python -m venv .venv
# Windows: .venv\Scripts\activate
# Mac:     source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-build.txt
```

`requirements.txt` には `pywebview` も含まれているので、これだけでデスクトップ
ウィンドウ表示に必要なものも一緒に入ります。

ライセンスキー認証を有効にする場合は、`tools/generate_keypair.py` を実行して
鍵ペアを作り、`public_key.pem` の中身を `src/utils/license_check.py` の
`PUBLIC_KEY_PEM` に貼り付けておいてください（詳細は `SELLING_GUIDE.md` 参照）。
このステップを飛ばした場合、ライセンス認証は常に失敗するようになるので、
販売しない（無料配布・自分用）場合は `ZUNDA_APP_DISABLE_LICENSE=1` を使ってください。

## 1. ビルドする（`ZundaVideoApp.spec` は設定済み・バージョン管理対象）

プロジェクトのルート（`zunda_video_app/`）で、コミット済みの `ZundaVideoApp.spec` を
使ってビルドします。

```bash
pyinstaller ZundaVideoApp.spec --clean
```

`dist/ZundaVideoApp/` フォルダの中に実行ファイル一式が生成されます
（Windowsなら `ZundaVideoApp.exe`、Macなら `ZundaVideoApp` または `.app`）。
**このフォルダごと**が配布物になります（実行ファイル単体では動きません）。

`ZundaVideoApp.spec` には、以前は手動で追記していた設定が最初から入っています。

- **エントリーポイント**: `packaging/launcher.py`
- **フックディレクトリ**: `packaging/hooks/`（`hookspath` で指定済み）
  - `hook-streamlit.py`: Streamlitのメタデータ・静的ファイル一式に加え、
    `collect_submodules("streamlit")` で動的importされるサブモジュール
    （`streamlit.runtime.scriptrunner.magic_funcs` 等。これが漏れると起動しても
    画面が真っ白になる）も同梱する。
  - `hook-webview.py`: pywebviewのバックエンド（Windows: pythonnet 経由の
    Edge WebView2）関連ファイルを `collect_all("webview")` で同梱する。
- **アイコン**: `packaging/app_icon.ico`（`EXE(...)` の `icon=` で指定済み。
  差し替えたいときは同じファイル名で上書きする）
- **同梱データ (`datas`)**: `app.py` / `src` / `config` / `assets`
  （`assets` はプレースホルダー素材のみにすること。SELLING_GUIDE.md参照）。
  同梱フォルダを増減したいときは `.spec` の `datas` リストを編集する
  （タプル形式 `(元のパス, 展開先)` で書けるので、コマンドラインの `--add-data` の
  ような区切り文字 `;`/`:` の違いを気にする必要はない）。
- **moviepy / imageio 関連**: `collect_all("imageio_ffmpeg")` /
  `collect_all("imageio")` / `collect_all("moviepy")` を実行し、ffmpegバイナリと
  各パッケージのメタデータを同梱する（`imageio` は import 時に自分自身の
  パッケージメタデータを参照するため、これが無いと起動時に
  `PackageNotFoundError: No package metadata was found for imageio` で落ちる）。
- **`--onedir`**（`.spec` の `COLLECT(...)`）を採用（`--onefile` は起動が遅くなりがちで、
  Streamlitのような重い依存関係との相性トラブルも起きやすいため）。

### `.spec` を最初から作り直したい場合（通常は不要）

```bash
pyinstaller --name ZundaVideoApp --onedir --additional-hooks-dir packaging/hooks --icon packaging/app_icon.ico --clean packaging/launcher.py
```

で `ZundaVideoApp.spec` が再生成されますが、上記の `datas` / `collect_all(...)` の
追記が消えるため、コミット済みの内容を参照して手で戻してください。

## 4. 動作確認チェックリスト

- [ ] 生成された `dist/ZundaVideoApp/` フォルダを、開発に使ったPCとは別の場所
      （別のフォルダ、できれば別のPC）にコピーしてから実行ファイルを起動する
      （開発環境の設定に依存していないかを確認するため）。
- [ ] ダブルクリックすると、ブラウザではなく専用のアプリウィンドウ（ネイティブウィンドウ）
      が開き、アプリ画面が表示されるか。
- [ ] ライセンス認証を有効にしている場合、発行したキーで認証が通るか
      （`tools/generate_license_key.py` で発行したキーを使う）。
- [ ] VOICEVOXを**起動していない**状態で、実際にシーンを1つ作って動画生成まで完走するか
      （VOICEVOXが自動起動し、音声合成が行われることを確認。動画背景・BGM等も一通り試すのが理想）。
- [ ] VOICEVOXがインストールされていないPCで生成を押すと、クラッシュせず
      「VOICEVOXを起動できませんでした…」の警告が出るか。
- [ ] ウィンドウを閉じたときに、裏で動いていたプロセスがきちんと終了しているか
      （タスクマネージャーで `ZundaVideoApp.exe` と、アプリが自動起動した `run.exe` /
      VOICEVOXエンジンが残っていないか確認）。
- [ ] `dist/ZundaVideoApp/` フォルダの中に `tools/`（`generate_keypair.py` /
      `generate_license_key.py`）や `private_key.pem` が**含まれていないこと**
      （このBUILD.md通りに`datas`を指定していれば含まれないはずですが、必ず目視確認
      してください。秘密鍵が万が一にも購入者に渡ってしまうと、ライセンスキーの
      仕組みが無意味になります）。

## よくあるトラブルと対処

これらはStreamlit＋PyInstallerの組み合わせで広く報告されている既知の問題です
（参考: [OpenMS/streamlit-template](https://github.com/OpenMS/streamlit-template/blob/main/docs/win_exe_with_pyinstaller.md)、
[Ploomberのブログ記事](https://ploomber.io/blog/streamlit_exe/)）。

> 下記のうち **magic_funcs / imageio メタデータ / signal スレッドエラー / ffmpeg同梱**
> は、コミット済みの `ZundaVideoApp.spec`・`packaging/hooks/hook-streamlit.py`・
> `packaging/launcher.py` で既に対処済みです（`.spec` を作り直したり `launcher.py` の
> プロセス分離を戻したりすると再発します）。

- **`ValueError: signal only works in main thread of the main interpreter` で
  起動直後に落ちる**: Streamlitサーバーをバックグラウンド「スレッド」で起動すると
  発生します。`launcher.py` はStreamlitを別「プロセス」として起動することで回避
  しています（ファイル冒頭のコメント参照）。
- **`ModuleNotFoundError: No module named 'streamlit.runtime.scriptrunner.magic_funcs'`
  でアプリ画面がエラーになる**: Streamlitが動的importするサブモジュールが同梱漏れ
  している状態です。`hook-streamlit.py` の `collect_submodules("streamlit")` で
  対処しています。
- **`importlib.metadata.PackageNotFoundError: No package metadata was found for
  imageio` で起動に失敗する**: moviepyが読み込む `imageio` が import 時に自分の
  パッケージメタデータを参照するためです。`.spec` の `collect_all("imageio")` で
  対処しています。
- **`imageio_ffmpeg.binaries` 関連のエラーで動画のエンコードに失敗する**
  （moviepyが内部で使うffmpegバイナリが見つからない）:
  `pip install -U pyinstaller-hooks-contrib` で最新版にしてから、`--clean` 付きで
  ビルドし直してください。それでも解決しない場合は、初回ビルドのコマンドに
  `--collect-all imageio_ffmpeg` を追加して試してください。
- **`altair` のスキーマファイルが見つからないというエラー**: Streamlitが内部で
  altair（グラフ描画ライブラリ）を読み込むことがあり、そのスキーマJSONが
  正しく同梱されないことがあります。`hooks/hook-streamlit.py` の `datas` に
  `collect_data_files("altair")` を追加してみてください。
- **Windowsで `0.0.0.0` へのバインドに失敗する**: `packaging/launcher.py` では
  Streamlitのサーバーを `localhost` 固定で起動しているため通常は問題ありませんが、
  もし `.streamlit/config.toml` を追加で持ち込んでいる場合は、
  `server.address` を `localhost` にしておいてください。
- **起動は成功するがアプリの中身が真っ白/エラーになる**: `hook-streamlit.py` の
  `datas` / `hiddenimports` が正しく効いていない可能性があります。`ZundaVideoApp.spec`
  の `hookspath=['packaging/hooks']` が残っているか、`--clean` 付きでビルドし直したかを
  確認してください（フックの変更はキャッシュされることがあるため、`--clean` 必須）。
- **ウィンドウが開くが真っ白なまま/何も表示されない**: (1) Microsoft Edge WebView2
  Runtimeがインストールされているか確認してください（購入者のPCでも同様）。
  (2) Streamlitサーバーの起動に時間がかかっている可能性があります。数秒〜十数秒
  待っても変化がない場合は、開発環境で `python packaging/launcher.py` を直接実行し、
  コンソールにエラーが出ていないか確認してください。
- **専用ウィンドウではなくブラウザで開く**: `launcher.py` は、ネイティブウィンドウを
  表示できなかったとき（WebView2 Runtime未導入など、`webview.start()` が即座に戻る
  ケース）に、既定のブラウザで開くフォールバックを持っています。コンソールに
  「専用ウィンドウを表示できなかったため、既定のブラウザで開きます」と出ている場合は
  WebView2 Runtimeをインストールすると次回から専用ウィンドウで起動します。
- **アイコンを変更したのにexeの見た目が変わらない**: Windowsはexeアイコンをキャッシュする
  ため、エクスプローラー上で古いアイコンのまま見えることがあります。`--clean` を付けて
  再ビルドし、それでも変わらない場合はエクスプローラーを再起動するか、`dist/ZundaVideoApp/`
  を一度削除してから作り直してください。
- **`ModuleNotFoundError: No module named 'webview'` 等、pywebview関連のエラー**:
  ビルドしたマシンで `pip install -r requirements.txt` が完了しているか
  （`pywebview` が入っているか）、および `ZundaVideoApp.spec` の
  `hookspath=['packaging/hooks']` が残っているか（`hook-webview.py` が読み込まれないと
  関連ファイルが同梱されません）を確認してください。

## 補足: なぜ実行ファイル化しても完全な「コピー防止」にはならないか

PyInstallerでビルドした実行ファイルは、`pyinstxtractor` のようなツールで内部の
`.pyc`（コンパイル済みPythonバイトコード）を取り出し、さらにデコンパイラで
元のソースコードに近い形へ復元することが技術的には可能です。つまり
「実行ファイル化＝ソースコードが完全に読めなくなる」わけではありません。

現実的な位置づけとしては、
1. 素のPythonソース（.pyファイルそのまま）を配るより、明らかにハードルは上がる
2. `src/utils/license_check.py` によるライセンスキー認証と組み合わせることで、
   「キーを持たない第三者がそのまま起動して使う」ことへの抑止力にはなる
3. ただし「本気で解析しようとする人」を完全には止められない

という「現実的な範囲での対策」です。より強い保護が必要な場合は、Nuitka
（Pythonをネイティブコードにコンパイルする、より解析されにくい選択肢）や、
コードを一切配布しない「自分のサーバーでホストしてアカウントを売る」形態への
切り替えも検討してください。
