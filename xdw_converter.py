from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def wait_until_stable(path: Path, timeout_seconds: int, stable_seconds: int = 3) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_signature: tuple[int, int] | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        if path.exists():
            stat = path.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if stat.st_size > 0 and signature == last_signature:
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= stable_seconds:
                    return
            else:
                last_signature = signature
                stable_since = time.monotonic()
        time.sleep(0.5)
    raise TimeoutError(f"PDF変換結果が完成しませんでした: {path}")


def _printer_properties(printer_name: str) -> tuple[str, str]:
    import win32print

    handle = win32print.OpenPrinter(printer_name)
    try:
        properties = win32print.GetPrinter(handle, 2)
        return str(properties["pDriverName"]), str(properties["pPortName"])
    finally:
        win32print.ClosePrinter(handle)


def convert_xdw_to_pdf(xdw_path: Path, output_pdf: Path) -> Path:
    viewer = Path(
        os.getenv(
            "DOCUWORKS_VIEWER",
            r"C:\Program Files (x86)\FUJIFILM\DocuWorks\bin\dwviewer.exe",
        )
    )
    printer = os.getenv("DOCUWORKS_PDF_PRINTER", "DocuWorks PDF").strip()
    printer_output_folder = Path(
        os.getenv("DOCUWORKS_PDF_OUTPUT_FOLDER", r"C:\DocuWorksPdfOut")
    )
    timeout_seconds = int(os.getenv("XDW_CONVERT_TIMEOUT_SECONDS", "180"))
    if not viewer.is_file():
        raise FileNotFoundError(f"DocuWorks Viewerが見つかりません: {viewer}")
    if not printer_output_folder.is_dir():
        raise FileNotFoundError(
            f"DocuWorks PDFの固定出力フォルダが見つかりません: {printer_output_folder}"
        )
    if output_pdf.exists():
        output_pdf.unlink()
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    # DocuWorks PDFは入力XDWのファイル名を引き継いで固定フォルダへ出力する。
    printer_pdf = printer_output_folder / f"{xdw_path.stem}.pdf"
    if printer_pdf.is_file():
        # 印刷用XDW名には内容ハッシュが付いているため、既存PDFは同じ入力の変換結果として再利用できる。
        wait_until_stable(printer_pdf, min(timeout_seconds, 30))
        shutil.copy2(printer_pdf, output_pdf)
        print(f"XDW変換済みPDFを再利用: {printer_pdf}", flush=True)
        return output_pdf

    driver, port = _printer_properties(printer)
    print(
        f"XDW変換開始: {xdw_path.name} / プリンタ={printer} / ドライバ={driver} / ポート={port}",
        flush=True,
    )
    process = subprocess.Popen(
        [str(viewer), "/pt", str(xdw_path.resolve()), printer, driver, port],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until_stable(printer_pdf, timeout_seconds)
        shutil.copy2(printer_pdf, output_pdf)
        wait_until_stable(output_pdf, min(timeout_seconds, 30))
        print(f"XDW変換完了: {printer_pdf} -> {output_pdf}", flush=True)
        return output_pdf
    finally:
        if process.poll() is None:
            process.terminate()

