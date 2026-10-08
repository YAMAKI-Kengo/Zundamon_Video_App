"""
PyInstallerで実行ファイル化する際のエントリーポイント。

「完全なデスクトップアプリ」として動作するように、Streamlitのサーバーを
別プロセスで起動しつつ、pywebview でOSネイティブのウィンドウを開いてその画面を
表示する（ブラウザのタブは一切開かない）。

ビルド方法の詳細は packaging/BUILD.md を参照。

前提（購入者側のPCにも必要）:
- Windows: Microsoft Edge WebView2 Runtime
  （Windows 10 2004以降 / Windows 11ならOS標準でほぼ入っている。
  入っていない場合は https://developer.microsoft.com/microsoft-edge/webview2/
  から Evergreen Bootstrapper をダウンロードしてインストールしてもらう必要がある）

【なぜStreamlitをスレッドではなく別プロセスで動かすか】
新しめのStreamlit (1.4x以降) は起動時に signal.signal() でSIGTERM/SIGINT等の
ハンドラを登録するが、signal.signal() は「メインスレッド」でしか呼べず、
バックグラウンドスレッドから呼ぶと ValueError: signal only works in main thread
で即座に落ちる。一方 pywebview のウィンドウ表示（webview.start()）もメインスレッドで
動かす必要がある。両者は同じプロセスのメインスレッドを取り合ってしまうため、
Streamlitサーバーは subprocess で別プロセス（＝別のメインスレッド）に分離している。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser

# voicevox_client が自動起動したVOICEVOXエンジンのPIDを書き出すファイル
# （src/services/voicevox_client.py の ENGINE_PID_FILE と同じパスにすること）。
_ENGINE_PID_FILE = os.path.join(tempfile.gettempdir(), "zundavideoapp_voicevox_engine.pid")

# 子プロセス（Streamlitサーバー役）として起動されたことを示す環境変数。
_SERVER_ENV_FLAG = "ZUNDA_RUN_STREAMLIT_SERVER"
_SERVER_ENV_PORT = "ZUNDA_STREAMLIT_PORT"


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


def _find_free_port() -> int:
    """空いているTCPポートを1つ探して返す。

    購入者のPCで他のアプリ（別のStreamlitアプリ等）が既定ポート(8501)を
    使っている可能性があるため、固定ポートにせずOSに割り当てさせる。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_streamlit_server(app_path: str, port: int) -> int:
    """Streamlitのサーバーを「このプロセスのメインスレッド」で起動する。

    この関数は子プロセス側でのみ呼ばれる（メインスレッドなので signal.signal() が
    問題なく使える）。server.headless=true にすることで、Streamlit自身がブラウザを
    自動で開く挙動を止めている（画面表示は親プロセスの pywebview ウィンドウで行うため）。
    """
    import streamlit.web.cli as stcli

    sys.argv = [
        "streamlit",
        "run",
        app_path,
        "--global.developmentMode=false",
        "--browser.gatherUsageStats=false",
        "--server.headless=true",
        f"--server.port={port}",
        "--server.address=localhost",
    ]
    try:
        return int(stcli.main() or 0)
    except SystemExit as e:  # stcli.main() は終了時に sys.exit() を呼ぶ
        return int(e.code or 0)


def _spawn_server_process(port: int) -> subprocess.Popen:
    """Streamlitサーバー役の子プロセスを起動する。

    - PyInstallerで凍結された実行ファイルの場合: 自分自身（同じ .exe）を
      環境変数フラグ付きで再実行する。フラグを見た子プロセス側は
      pywebviewウィンドウを開かず、Streamlitサーバーの起動だけを行う。
    - 通常のPython実行（開発中）の場合: `python launcher.py` を同様に再実行する。
    """
    env = os.environ.copy()
    env[_SERVER_ENV_FLAG] = "1"
    env[_SERVER_ENV_PORT] = str(port)

    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, os.path.abspath(__file__)]

    return subprocess.Popen(cmd, env=env)


def _wait_for_server(url: str, proc: subprocess.Popen, timeout_seconds: float = 60.0) -> bool:
    """Streamlitサーバーが応答するようになるまで待つ（ウィンドウを開く前に必要）。

    子プロセスが途中で異常終了した場合は待ち続けても無駄なので、そこで打ち切る。
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def _terminate_server_process(proc: subprocess.Popen) -> None:
    """Streamlitサーバーの子プロセスを確実に終了させる。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _terminate_autostarted_voicevox() -> None:
    """このアプリが「VOICEVOX未起動」を検知して自動起動したエンジンを終了させる。

    ユーザーが自分で起動していたVOICEVOXには触れない（PIDファイルは自動起動時のみ作られる）。
    アプリ終了後にエンジンだけが裏に残り続けるのを防ぐ。
    """
    try:
        with open(_ENGINE_PID_FILE, "r", encoding="ascii") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return

    try:
        if os.name == "nt":
            # /T で子プロセス（エンジンが生成するワーカー）ごと終了させる
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            os.kill(pid, 15)
    except Exception:  # noqa: BLE001 - 後始末の失敗でアプリ終了を妨げない
        pass
    finally:
        try:
            os.remove(_ENGINE_PID_FILE)
        except OSError:
            pass


def _run_as_server() -> int:
    """子プロセスとして起動されたときのエントリー（Streamlitサーバーのみ起動）。"""
    app_path = resource_path("app.py")
    try:
        port = int(os.environ[_SERVER_ENV_PORT])
    except (KeyError, ValueError):
        port = _find_free_port()
    return _run_streamlit_server(app_path, port)


def main() -> None:
    # 子プロセス（サーバー役）として呼ばれた場合は、ウィンドウを開かずにサーバーだけ起動する。
    if os.environ.get(_SERVER_ENV_FLAG) == "1":
        raise SystemExit(_run_as_server())

    import webview

    # 前回異常終了時などに残ったPIDファイルを消しておく。以降このファイルは
    # 「今回のアプリが自動起動したエンジン」だけを指すようにする。
    try:
        os.remove(_ENGINE_PID_FILE)
    except OSError:
        pass

    port = _find_free_port()
    url = f"http://localhost:{port}"

    server_proc = _spawn_server_process(port)

    try:
        # サーバーが規定時間内に立ち上がらなかった場合も、一応ウィンドウは開く
        # （真っ白な画面になるが、購入者が手動でリロードできる可能性を残すため）。
        server_ready = _wait_for_server(url, server_proc)

        # ネイティブウィンドウが「実際に表示されたか」を shown イベントで記録する。
        # 表示されずに webview.start() が戻った場合（WebView2 Runtime未導入など）と、
        # 表示された後にユーザーが閉じた場合とを区別するために必要。
        window_shown = {"ok": False}
        window_error: Exception | None = None
        try:
            window = webview.create_window(
                "ずんだもん・四国めたん 解説動画ジェネレーター",
                url,
                width=1360,
                height=900,
                min_size=(1000, 700),
                fullscreen=True,
            )
            try:
                window.events.shown += lambda: window_shown.__setitem__("ok", True)
            except Exception:  # noqa: BLE001 - 古いpywebviewでイベントAPIが違う場合も起動自体は続ける
                window_shown["ok"] = True
            webview.start()
        except Exception as e:  # noqa: BLE001 - どんな失敗でもブラウザにフォールバックする
            window_error = e

        # ネイティブウィンドウを一度も表示できなかった場合（Windows: Microsoft Edge
        # WebView2 Runtime 未導入など）は、何も出ないまま終了するより、既定のブラウザで
        # 開いて使える状態にする。表示された後に閉じられた場合は普通に終了する。
        window_failed = window_error is not None or not window_shown["ok"]
        if window_failed and server_ready and server_proc.poll() is None:
            if window_error is not None:
                print(f"[ZundaVideoApp] 専用ウィンドウの表示に失敗しました: {window_error}", flush=True)
            print(
                "[ZundaVideoApp] 専用ウィンドウを表示できなかったため、既定のブラウザで開きます。\n"
                "  Windowsをお使いの場合、Microsoft Edge WebView2 Runtime をインストールすると\n"
                "  次回から専用ウィンドウで起動します: "
                "https://developer.microsoft.com/microsoft-edge/webview2/\n"
                f"  ブラウザが開かない場合はこのURLを開いてください: {url}\n"
                "  （このウィンドウ／ターミナルを閉じるとアプリが終了します）",
                flush=True,
            )
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
            try:
                server_proc.wait()
            except KeyboardInterrupt:
                pass
    finally:
        # ウィンドウが閉じられたら、裏で動いていたStreamlitサーバーを終了する。
        _terminate_server_process(server_proc)
        # アプリが自動起動したVOICEVOXエンジンも一緒に終了させる（残留防止）。
        _terminate_autostarted_voicevox()

    # Tornado等の後片付けに時間がかかりプロセスが残ることがあるため、明示的に強制終了する。
    os._exit(0)


if __name__ == "__main__":
    main()
