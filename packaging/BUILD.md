# 実行ファイル(exe/app)としてビルドする手順

このアプリ（Streamlit製）を、購入者がPython環境を用意しなくてもダブルクリックだけで
使えるように、PyInstallerで実行ファイル化する手順です。

**重要な前提**

- ビルドは **Windows向けならWindows上で、Mac向けならMac上で** 行ってください
  （PyInstallerはクロスコンパイル非対応。Windows用exeをMacで作ることはできません）。
- 実行ファイル化しても、**VOICEVOX ENGINEは別途購入者自身にインストール・起動して
  もらう必要があります**（このアプリはVOICEVOXにHTTPで接続するだけで、VOICEVOX自体は
  同梱していません）。購入者向けの説明に必ず明記してください。
- Streamlit＋PyInstallerの組み合わせは、Streamlit側のアップデートで動かなくなることが
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

ライセンスキー認証を有効にする場合は、`tools/generate_keypair.py` を実行して
鍵ペアを作り、`public_key.pem` の中身を `src/utils/license_check.py` の
`PUBLIC_KEY_PEM` に貼り付けておいてください（詳細は `SELLING_GUIDE.md` 参照）。
このステップを飛ばした場合、ライセンス認証は常に失敗するようになるので、
販売しない（無料配布・自分用）場合は `ZUNDA_APP_DISABLE_LICENSE=1` を使ってください。

## 1. 初回ビルド（.specファイルの生成）

プロジェクトのルート（`zunda_video_app/`）で実行します。

```bash
pyinstaller --name ZundaVideoApp --onedir --additional-hooks-dir packaging/hooks --clean packaging/launcher.py
```

- `--onedir` を推奨しています（`--onefile` は起動が遅くなりがちで、Streamlitのような
  重い依存関係との相性トラブルも起きやすいため）。
- 実行すると `ZundaVideoApp.spec` がプロジェクトルートに生成されます。次のステップで
  このファイルを編集してから、もう一度ビルドし直します。

## 2. .specファイルを編集してデータを同梱する

生成された `ZundaVideoApp.spec` を開き、`Analysis(...)` の `datas=[...]` に、
アプリが実行時に必要とするフォルダを追加します。

```python
datas=[
    ('app.py', '.'),
    ('src', 'src'),
    ('config', 'config'),
    ('assets', 'assets'),   # プレースホルダー素材のみ同梱すること（SELLING_GUIDE.md参照）
],
```

（Windows/Mac共通で、.specファイル内はタプル形式 `(元のパス, 展開先)` で書けるので、
コマンドラインの `--add-data` のような区切り文字`;`/`:`の違いを気にする必要はありません）

## 3. ビルドし直す

```bash
pyinstaller ZundaVideoApp.spec --clean
```

`dist/ZundaVideoApp/` フォルダの中に実行ファイル一式が生成されます
（Windowsなら `ZundaVideoApp.exe`、Macなら `ZundaVideoApp` または `.app`）。
**このフォルダごと**が配布物になります（実行ファイル単体では動きません）。

## 4. 動作確認チェックリスト

- [ ] 生成された `dist/ZundaVideoApp/` フォルダを、開発に使ったPCとは別の場所
      （別のフォルダ、できれば別のPC）にコピーしてから実行ファイルを起動する
      （開発環境の設定に依存していないかを確認するため）。
- [ ] ダブルクリックしてブラウザが自動で開き、アプリ画面が表示されるか。
- [ ] ライセンス認証を有効にしている場合、発行したキーで認証が通るか
      （`tools/generate_license_key.py` で発行したキーを使う）。
- [ ] VOICEVOXを起動した状態で、実際にシーンを1つ作って動画生成まで完走するか
      （音声合成・動画背景・BGM等、使う機能ごとに一通り試すのが理想）。
- [ ] `dist/ZundaVideoApp/` フォルダの中に `tools/`（`generate_keypair.py` /
      `generate_license_key.py`）や `private_key.pem` が**含まれていないこと**
      （このBUILD.md通りに`datas`を指定していれば含まれないはずですが、必ず目視確認
      してください。秘密鍵が万が一にも購入者に渡ってしまうと、ライセンスキーの
      仕組みが無意味になります）。

## よくあるトラブルと対処

これらはStreamlit＋PyInstallerの組み合わせで広く報告されている既知の問題です
（参考: [OpenMS/streamlit-template](https://github.com/OpenMS/streamlit-template/blob/main/docs/win_exe_with_pyinstaller.md)、
[Ploomberのブログ記事](https://ploomber.io/blog/streamlit_exe/)）。

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
  Streamlitの既定（localhost）のまま起動しているため通常は問題ありませんが、
  もし `.streamlit/config.toml` を追加で持ち込んでいる場合は、
  `server.address` を `localhost` にしておいてください。
- **起動は成功するがアプリの中身が真っ白/エラーになる**: `hook-streamlit.py` の
  `datas` が正しく効いていない可能性があります。`--additional-hooks-dir packaging/hooks`
  を指定し忘れていないか確認してください。

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
