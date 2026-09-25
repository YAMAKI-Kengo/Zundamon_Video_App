"""
本のテキストデータ（txt / PDF）の読み込み。

- txt: 文字コードを UTF-8（BOM付き含む）→ UTF-16 → Shift_JIS(cp932) の順に試して読む
  （Windowsのメモ帳や青空文庫のテキストは Shift_JIS のことが多いため）。
- PDF: pypdf でページごとにテキストを取り出してつなげる。画像だけのPDF（スキャンした本など）は
  文字を取り出せないため、その旨をエラーで知らせる。

読み込んだテキストはそのまま Claude（src.services.book_ai）に渡す。Claudeは100万トークンまでの
入力を一度に読めるため、一般的な本1冊なら章ごとに分割せずに丸ごと渡せる。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

MIN_TEXT_CHARS = 200  # これより短い場合は「本文を読み取れていない」とみなす


class BookLoadError(Exception):
    """本のファイルを読み込めなかった（利用者向けのメッセージ付き）。"""


@dataclass
class BookText:
    title_guess: str      # ファイル名から推測したタイトル
    text: str
    pages: int = 0        # PDFのページ数（txtは0）

    @property
    def char_count(self) -> int:
        return len(self.text)


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_pdf(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise BookLoadError("PDFを読むには pypdf が必要です。`pip install -r requirements.txt` を実行してください。") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:  # noqa: BLE001
                raise BookLoadError("パスワード付きのPDFは読み込めません。") from e
        pages = [page.extract_text() or "" for page in reader.pages]
    except BookLoadError:
        raise
    except Exception as e:  # noqa: BLE001 - 壊れたPDF等
        raise BookLoadError(f"PDFを読み込めませんでした（{e}）") from e
    return "\n\n".join(p.strip() for p in pages if p.strip()), len(pages)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("　", "　")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_book(filename: str, data: bytes) -> BookText:
    """アップロードされた本のファイルを読み込む。"""
    suffix = Path(filename).suffix.lower()
    title_guess = Path(filename).stem
    if suffix == ".pdf":
        text, pages = _read_pdf(data)
    elif suffix in (".txt", ".md", ".text"):
        text, pages = _decode_text(data), 0
    else:
        raise BookLoadError("対応していないファイル形式です（.txt / .pdf に対応しています）。")
    text = _clean(text)
    if len(text) < MIN_TEXT_CHARS:
        hint = "画像だけのPDF（スキャンした本など）は文字を取り出せません。" if suffix == ".pdf" else ""
        raise BookLoadError(f"本文をほとんど読み取れませんでした（{len(text)}文字）。{hint}")
    return BookText(title_guess=title_guess, text=text, pages=pages)


def from_pasted_text(text: str, title: str = "") -> BookText:
    text = _clean(text or "")
    if len(text) < MIN_TEXT_CHARS:
        raise BookLoadError(f"本文が短すぎます（{len(text)}文字）。本のテキストを貼り付けてください。")
    return BookText(title_guess=title, text=text)
