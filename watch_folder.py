from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import datetime, time as datetime_time, timedelta
from pathlib import Path

from c1_browser import C1WarehouseBrowser
from genu_browser import GenuBrowser
from import_jyosan import (
    JST,
    RtcmConfig,
    application_dir,
    insert_into_oracle,
    required_env,
    validate_extracted,
)
from notifier import build_notification, load_notify_config, send_notification
from xdw_converter import convert_xdw_to_pdf


SUPPORTED_EXTENSIONS = {".pdf", ".xdw"}


class C1WarehouseRegistrationError(RuntimeError):
    """RTCM committed successfully, but the subsequent C1 registration failed."""

    def __init__(self, message, partial_result):
        super().__init__(message)
        self.partial_result = partial_result


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} は true/false で指定してください: {value}")


def pdf_upload_name(source: Path) -> str:
    """生成AIへ渡すPDF名から半角・全角を含む空白文字を除去する。"""
    stem = re.sub(r"\s+", "", source.stem, flags=re.UNICODE)
    if not stem:
        stem = "upload"
    return f"{stem}.pdf"


def rename_source_pdf(source: Path, payload: dict, digest: str, dry_run: bool = False) -> dict:
    """Rename the original PDF only when all three naming fields are readable."""
    seiban = payload.get("seiban")
    revision = payload.get("pdf_hansu")
    customer = payload.get("demand_customer_name")
    if (
        not isinstance(seiban, str)
        or not re.fullmatch(r"[A-Z0-9_-]{1,25}", seiban.strip().upper())
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 0 <= revision <= 999
        or not isinstance(customer, str)
        or not customer.strip()
        or re.search(r"ERROR|エラー|読取不可|読み取れ|不明|記載なし", customer, re.IGNORECASE)
    ):
        return {"status": "not_renamed", "reason": "製番・版数・需要家名を読み取れません。"}
    if source.suffix.lower() != ".pdf":
        return {"status": "not_renamed", "reason": "元ファイルがPDFではありません。"}
    customer = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", customer.strip())[:80].rstrip(" .")
    if not customer:
        return {"status": "not_renamed", "reason": "需要家名をファイル名に使用できません。"}
    if not source.is_file() or file_hash(source) != digest:
        return {"status": "not_renamed", "reason": "元PDFが移動または変更されています。"}
    filename = f"{seiban.strip().upper()}_R{revision}_{customer}.PDF"
    target = source.with_name(filename)
    if source.name == filename:
        return {"status": "already_named", "path": str(source)}
    if source.resolve() != target.resolve():
        counter = 1
        while target.exists():
            target = source.with_name(f"{Path(filename).stem}_{counter}.PDF")
            counter += 1
    if not dry_run:
        source.rename(target)
    return {
        "status": "dry_run" if dry_run else "renamed",
        "original_path": str(source),
        "path": str(target),
    }


def xdw_print_name(source: Path, digest: str) -> str:
    """固定出力先で同名競合しない、空白なしの印刷用XDW名を作る。"""
    stem = re.sub(r"\s+", "", source.stem, flags=re.UNICODE) or "upload"
    return f"{stem}_{digest[:12]}.xdw"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_stable(path: Path, stable_seconds: int) -> bool:
    try:
        first = path.stat()
        if first.st_size == 0 or time.time() - first.st_mtime < stable_seconds:
            return False
        time.sleep(1)
        second = path.stat()
        return first.st_size == second.st_size and first.st_mtime_ns == second.st_mtime_ns
    except (FileNotFoundError, PermissionError):
        return False


class ProcessingState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_files (
                sha256 TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error_text TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        existing_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(processed_files)")
        }
        for column_name, column_type in (
            ("queued_at", "TEXT"),
            ("source_mtime", "TEXT"),
            ("queue_path", "TEXT"),
        ):
            if column_name not in existing_columns:
                self.connection.execute(
                    f"ALTER TABLE processed_files ADD COLUMN {column_name} {column_type}"
                )
        self.connection.commit()

    def enqueue(self, digest: str, source: Path, queue_path: Path) -> bool:
        now = datetime.now(JST).isoformat(timespec="seconds")
        source_mtime = datetime.fromtimestamp(source.stat().st_mtime, tz=JST).isoformat(
            timespec="seconds"
        )
        existing = self.connection.execute(
            "SELECT status, source_mtime, queue_path FROM processed_files WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        if existing is not None:
            # 旧版でerror/processingまで記録済みの行にはキュー情報がないため、
            # 同じファイルを検出できた時点で移行情報を補完する。
            if existing[0] in {"error", "processing"} and (
                existing[1] is None or existing[2] is None
            ):
                self.connection.execute(
                    """
                    UPDATE processed_files
                       SET source_path = ?,
                           queued_at = COALESCE(queued_at, ?),
                           source_mtime = ?,
                           queue_path = ?,
                           updated_at = ?
                     WHERE sha256 = ?
                    """,
                    (str(source), now, source_mtime, str(queue_path), now, digest),
                )
                self.connection.commit()
                return True
            return False
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO processed_files(
                sha256, source_path, status, result_json, error_text, updated_at,
                queued_at, source_mtime, queue_path
            ) VALUES (?, ?, 'queued', NULL, NULL, ?, ?, ?, ?)
            """,
            (digest, str(source), now, now, source_mtime, str(queue_path)),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def ready_items(self, cutoff: datetime) -> list[dict[str, str]]:
        rows = self.connection.execute(
            """
            SELECT sha256, source_path, queue_path, status, source_mtime
              FROM processed_files
             WHERE status IN ('queued', 'processing', 'error', 'extracting')
               AND source_mtime IS NOT NULL
               AND source_mtime <= ?
             ORDER BY source_mtime, queued_at
            """,
            (cutoff.isoformat(timespec="seconds"),),
        ).fetchall()
        return [
            {
                "sha256": row[0],
                "source_path": row[1],
                "queue_path": row[2],
                "status": row[3],
                "source_mtime": row[4],
            }
            for row in rows
        ]

    def should_process(
        self,
        digest: str,
        retry_errors: bool,
        retry_after_seconds: int = 0,
    ) -> bool:
        row = self.connection.execute(
            "SELECT status, updated_at FROM processed_files WHERE sha256 = ?", (digest,)
        ).fetchone()
        # 前回プロセスが異常終了してprocessingのままなら、次回起動時に再開する。
        if row is None or row[0] in {"queued", "processing", "extracting"}:
            return True
        if row[0] != "error" or not retry_errors:
            return False
        if retry_after_seconds <= 0:
            return True
        updated_at = datetime.fromisoformat(row[1])
        return (datetime.now(JST) - updated_at).total_seconds() >= retry_after_seconds

    def record(self, digest: str, source: Path, status: str, result=None, error=None) -> None:
        now = datetime.now(JST).isoformat(timespec="seconds")
        self.connection.execute(
            """
            INSERT INTO processed_files(sha256, source_path, status, result_json, error_text, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                source_path=excluded.source_path,
                status=excluded.status,
                result_json=excluded.result_json,
                error_text=excluded.error_text,
                updated_at=excluded.updated_at
            """,
            (
                digest,
                str(source),
                status,
                json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
                str(error) if error is not None else None,
                now,
            ),
        )
        self.connection.commit()


def append_log(log_path: Path, event: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": datetime.now(JST).isoformat(timespec="seconds"),
        **event,
    }
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")


def parse_batch_time(value: str) -> datetime_time:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError as exc:
        raise ValueError(f"RTCM_BATCH_TIME は HH:MM 形式で指定してください: {value}") from exc
    return datetime_time(parsed.hour, parsed.minute)


def latest_weekday_batch_cutoff(now: datetime, batch_time: datetime_time) -> datetime:
    """現在以前で直近となる月～金曜日のバッチ時刻を返す。"""
    cutoff = now.replace(
        hour=batch_time.hour,
        minute=batch_time.minute,
        second=0,
        microsecond=0,
    )
    if now < cutoff:
        cutoff -= timedelta(days=1)
    while cutoff.weekday() >= 5:
        cutoff -= timedelta(days=1)
    return cutoff


def direct_candidates(watch_folder: Path, cutoff: datetime | None = None) -> list[Path]:
    """監視フォルダ直下だけを対象にし、年別保管フォルダ等は再帰しない。"""
    return sorted(
        (
            path
            for path in watch_folder.iterdir()
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
            and (
                cutoff is None
                or datetime.fromtimestamp(path.stat().st_mtime, tz=JST) <= cutoff
            )
        ),
        key=lambda path: path.stat().st_mtime,
    )


def queue_copy_path(work_folder: Path, source: Path, digest: str) -> Path:
    queue_folder = work_folder / "queue" / digest[:16]
    queue_folder.mkdir(parents=True, exist_ok=True)
    return queue_folder / source.name


def unique_archive_path(folder: Path, filename: str, digest: str) -> Path:
    destination = folder / filename
    if not destination.exists():
        return destination
    return folder / f"{Path(filename).stem}_{digest[:12]}{Path(filename).suffix}"


def archive_pdf(source: Path, pdf_path: Path, watch_folder: Path, digest: str, now: datetime) -> Path:
    archive_folder = watch_folder / now.strftime("%Y")
    archive_folder.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".pdf":
        destination = unique_archive_path(archive_folder, source.name, digest)
        return Path(shutil.move(str(source), str(destination)))

    # XDW入力では生成AIに渡した変換後PDFに加え、監視フォルダにある元XDWも
    # 年別フォルダへ移動し、正常終了後のファイルを監視フォルダ直下に残さない。
    pdf_destination = unique_archive_path(archive_folder, pdf_path.name, digest)
    archived_pdf = Path(shutil.move(str(pdf_path), str(pdf_destination)))
    source_destination = unique_archive_path(archive_folder, source.name, digest)
    shutil.move(str(source), str(source_destination))
    return archived_pdf


def notify_safely(notify_config, status: str, source: Path, result=None, error="") -> str | None:
    if notify_config is None:
        return None
    try:
        subject, body = build_notification(status, source, result, error)
        send_notification(notify_config, subject, body)
        return None
    except Exception as exc:
        return f"メール通知に失敗しました: {exc}"


def rtcm_config() -> RtcmConfig:
    return RtcmConfig(
        kaicd=os.getenv("RTCM_KAICD", "MMC").strip(),
        kyotencd=os.getenv("RTCM_KYOTENCD", "MMC").strip(),
        kojcd=os.getenv("RTCM_KOJCD", "MMC").strip(),
        user_id=required_env("RTCM_USER_ID"),
        pgid=required_env("RTCM_PGID"),
    )


def prepare_upload_pdf(source: Path, digest: str, work_folder: Path) -> Path:
    job_folder = work_folder / digest[:16]
    job_folder.mkdir(parents=True, exist_ok=True)
    upload_pdf = job_folder / pdf_upload_name(source)
    if source.suffix.lower() == ".xdw":
        print_xdw = job_folder / xdw_print_name(source, digest)
        if not print_xdw.exists():
            shutil.copy2(source, print_xdw)
        return convert_xdw_to_pdf(print_xdw, upload_pdf)
    if source.resolve() != upload_pdf.resolve():
        shutil.copy2(source, upload_pdf)
    return upload_pdf


def prepare_queue_item(item: dict, state: ProcessingState, browser: GenuBrowser,
                       work_folder: Path, dry_run: bool = False) -> dict:
    """Extract and rename at intake; persist results before registration is scheduled."""
    digest = item["sha256"]
    original = Path(item["source_path"])
    queued = Path(item["queue_path"])
    if not queued.is_file():
        renamed_copy = queued.with_name(original.name)
        if renamed_copy.is_file() and file_hash(renamed_copy) == digest:
            queued = renamed_copy
    job_folder = work_folder / digest[:16]
    job_folder.mkdir(parents=True, exist_ok=True)
    cache = job_folder / "extracted.json"
    intake_path = job_folder / "intake.json"
    rename_info = {"status": "not_renamed", "reason": "命名情報を取得できません。"}

    def metadata(payload):
        nonlocal original
        rename_info.update(rename_source_pdf(original, payload, digest, dry_run))
        if rename_info["status"] in {"renamed", "already_named"}:
            original = Path(rename_info["path"])
        state.record(digest, original, "extracting")
        intake_path.write_text(json.dumps({"source": str(original), "pdf_rename": rename_info}, ensure_ascii=False, indent=2), encoding="utf-8")

    state.record(digest, original, "extracting")
    try:
        if cache.is_file():
            extracted = validate_extracted(json.loads(cache.read_text(encoding="utf-8")))
            metadata(extracted.__dict__)
        else:
            upload_pdf = prepare_upload_pdf(queued, digest, work_folder)
            print(f"受付時の生成AI抽出: {original.name}", flush=True)
            extracted = browser.extract(upload_pdf, on_metadata=metadata)
            temporary_cache = cache.with_suffix(".json.tmp")
            temporary_cache.write_text(json.dumps(extracted.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary_cache.replace(cache)
        if not dry_run and original.suffix.lower() == ".pdf" and rename_info["status"] in {"renamed", "already_named"}:
            target = queued.with_name(original.name)
            if queued != target:
                if target.exists():
                    raise FileExistsError(f"キューのリネーム先が既に存在します: {target}")
                queued.rename(target)
                queued = target
        state.connection.execute(
            "UPDATE processed_files SET source_path = ?, queue_path = ? WHERE sha256 = ?",
            (str(original), str(queued), digest),
        )
        state.record(digest, original, "queued", result={"pdf_rename": rename_info})
        return {**item, "source_path": str(original), "queue_path": str(queued), "pdf_rename": rename_info}
    except Exception:
        state.record(digest, original, "error", error=traceback.format_exc())
        raise


def process_file(
    source: Path,
    digest: str,
    browser: GenuBrowser,
    work_folder: Path,
    config: RtcmConfig,
    dry_run: bool,
    watch_folder: Path,
    archive_pdf_enabled: bool,
    c1_browser: C1WarehouseBrowser,
    c1_enabled: bool,
    on_metadata=None,
    pdf_rename: dict | None = None,
) -> dict:
    job_folder = work_folder / digest[:16]
    job_folder.mkdir(parents=True, exist_ok=True)
    pdf_path = prepare_upload_pdf(source, digest, work_folder)
    extracted_cache = job_folder / "extracted.json"
    if extracted_cache.is_file():
        extracted = validate_extracted(json.loads(extracted_cache.read_text(encoding="utf-8")))
        print(f"保存済み生成AI抽出結果を再利用: {extracted.seiban}", flush=True)
        if on_metadata is not None:
            on_metadata(extracted.__dict__)
    else:
        print(f"処理中: 生成AIへアップロードします: {pdf_path.name}", flush=True)
        extracted = (
            browser.extract(pdf_path, on_metadata=on_metadata)
            if on_metadata is not None
            else browser.extract(pdf_path)
        )
    print(f"生成AI抽出完了: {json.dumps(extracted.__dict__, ensure_ascii=False)}", flush=True)
    (job_folder / "extracted.json").write_text(
        json.dumps(extracted.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # RTCMコミット後にC1で失敗した場合、再実行でT_JYOSANを重複登録しないよう
    # RTCM成功結果を直ちにチェックポイントへ保存する。
    rtcm_checkpoint = job_folder / "rtcm_result.json"
    db_result = None
    if not dry_run and rtcm_checkpoint.is_file():
        checkpoint_value = json.loads(rtcm_checkpoint.read_text(encoding="utf-8"))
        if (
            checkpoint_value.get("committed") is True
            and checkpoint_value.get("seiban") == extracted.seiban
        ):
            db_result = checkpoint_value
            print(
                f"RTCM登録済みチェックポイントを再利用します: {extracted.seiban}",
                flush=True,
            )
    if db_result is None:
        db_result = insert_into_oracle(extracted, config, dry_run)
        if db_result.get("committed") is True:
            checkpoint_temp = rtcm_checkpoint.with_suffix(".json.tmp")
            checkpoint_temp.write_text(
                json.dumps(db_result, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            checkpoint_temp.replace(rtcm_checkpoint)

    if db_result.get("skipped"):
        c1_result = {
            "status": "not_run",
            "registered": False,
            "reason": "RTCM登録がスキップされたためC1倉庫登録も実行していません。",
        }
    elif dry_run:
        c1_result = c1_browser.register(extracted, dry_run=True) if c1_enabled else {
            "status": "disabled",
            "registered": False,
        }
    elif db_result.get("committed") and c1_enabled:
        print(f"処理中: RTCM登録成功後のC1倉庫登録: {extracted.seiban}", flush=True)
        try:
            c1_result = c1_browser.register(extracted)
        except Exception as exc:
            partial_result = {
                "source": str(source),
                "pdf": str(pdf_path),
                "extracted": extracted.__dict__,
                "database": db_result,
                "pdf_rename": dict(pdf_rename or {}),
                "c1_warehouse": {
                    "status": "error",
                    "registered": False,
                    "error": str(exc),
                },
            }
            (job_folder / "result.json").write_text(
                json.dumps(partial_result, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            raise C1WarehouseRegistrationError(
                "RTCM登録は成功しましたが、C1倉庫登録に失敗しました。"
                f"再実行時はRTCM登録済みチェックポイントを再利用します: {exc}",
                partial_result,
            ) from exc
        print(f"C1倉庫登録完了: {json.dumps(c1_result, ensure_ascii=False)}", flush=True)
    else:
        c1_result = {"status": "disabled", "registered": False}
    result = {
        "source": str(source),
        "pdf": str(pdf_path),
        "extracted": extracted.__dict__,
        "database": db_result,
        "c1_warehouse": c1_result,
        "pdf_rename": dict(pdf_rename or {}),
    }
    result["archive_pdf_enabled"] = archive_pdf_enabled
    if not dry_run and archive_pdf_enabled:
        try:
            archive_source = (
                Path(pdf_rename["path"])
                if pdf_rename and pdf_rename.get("status") in {"renamed", "already_named"}
                else source
            )
            archived_pdf = archive_pdf(archive_source, pdf_path, watch_folder, digest, datetime.now(JST))
            result["archived_pdf"] = str(archived_pdf)
            result["pdf"] = str(archived_pdf)
        except Exception as exc:
            # DBは既にコミット済み。ここで処理全体をerrorにすると再試行時に新版を
            # 重複登録するため、保管エラーを結果に残してDB更新自体は成功扱いにする。
            result["archive_error"] = str(exc)
    (job_folder / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return result


def process_c1_only(
    source: Path,
    browser: GenuBrowser | None,
    work_folder: Path,
    c1_browser: C1WarehouseBrowser,
    dry_run: bool,
) -> dict:
    """PDFまたは抽出済みJSONから、Oracleを使用せずC1だけを登録する。"""
    source_file = source.expanduser().resolve()
    if not source_file.is_file():
        raise FileNotFoundError(f"C1倉庫登録対象ファイルが見つかりません: {source_file}")
    input_type = source_file.suffix.lower()
    if input_type not in {".pdf", ".json"}:
        raise ValueError(
            f"--c1-only にはPDFまたはJSONファイルを指定してください: {source_file}"
        )

    digest = file_hash(source_file)
    job_folder = work_folder / "c1-only" / digest[:16]
    job_folder.mkdir(parents=True, exist_ok=True)
    upload_pdf = None
    if input_type == ".pdf":
        if browser is None:
            raise RuntimeError("PDF入力には生成AIブラウザが必要です")
        upload_pdf = job_folder / pdf_upload_name(source_file)
        if source_file != upload_pdf.resolve():
            shutil.copy2(source_file, upload_pdf)
        print(f"C1専用実行: 生成AIへアップロードします: {upload_pdf.name}", flush=True)
        extracted = browser.extract(upload_pdf)
        print(f"C1専用実行: 生成AI抽出完了: {extracted.seiban}", flush=True)
    else:
        print(f"C1専用実行: JSONを読み込みます: {source_file.name}", flush=True)
        try:
            payload = json.loads(source_file.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"C1専用実行のJSON形式が不正です: {exc}") from exc
        extracted = validate_extracted(payload)
        print(f"C1専用実行: JSON検証完了: {extracted.seiban}", flush=True)

    extracted_path = job_folder / "extracted.json"
    extracted_path.write_text(
        json.dumps(extracted.__dict__, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    c1_result = c1_browser.register(extracted, dry_run=dry_run)
    result = {
        "mode": "c1_only",
        "input_type": input_type.removeprefix("."),
        "source_file": str(source_file),
        "extracted_json": str(extracted_path),
        "extracted": extracted.__dict__,
        "database": {
            "status": "not_run",
            "committed": False,
            "reason": "C1専用実行のためRTCM更新は行っていません。",
        },
        "c1_warehouse": c1_result,
    }
    if input_type == ".pdf":
        result["source_pdf"] = str(source_file)
        result["upload_pdf"] = str(upload_pdf)
    else:
        result["source_json"] = str(source_file)
    output_path = job_folder / "c1_only_result.json"
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    result["result_json"] = str(output_path)
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PDF/XDW監視フォルダからRTCM落付見込を自動登録します")
    parser.add_argument("--once", action="store_true", help="現在あるファイルを処理して終了")
    parser.add_argument("--dry-run", action="store_true", help="DB登録をROLLBACK")
    parser.add_argument("--retry-errors", action="store_true", help="前回エラーの同一ファイルを再実行")
    parser.add_argument(
        "--c1-only",
        type=Path,
        metavar="INPUT_FILE",
        help=(
            "PDFを生成AIへアップロードするか抽出済みJSONを読み込み、"
            "RTCMを更新せずC1倉庫登録だけを即時実行"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.c1_only is not None:
        work_folder = Path(required_env("RTCM_WORK_FOLDER"))
        work_folder.mkdir(parents=True, exist_ok=True)
        log_path = work_folder / "logs" / "c1-only.jsonl"
        browser = None
        c1_browser = None
        try:
            input_suffix = args.c1_only.suffix.lower()
            if input_suffix == ".pdf":
                browser = GenuBrowser()
                c1_browser = C1WarehouseBrowser(playwright=browser.playwright)
            else:
                # JSON入力では生成AIを一切起動せず、C1専用ブラウザだけを使用する。
                c1_browser = C1WarehouseBrowser()
            result = process_c1_only(
                args.c1_only, browser, work_folder, c1_browser, args.dry_run
            )
            append_log(
                log_path,
                {
                    "status": "dry_run" if args.dry_run else "success",
                    "source": str(args.c1_only),
                    **result,
                },
            )
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0
        except Exception as exc:
            append_log(
                log_path,
                {
                    "status": "error",
                    "source": str(args.c1_only),
                    "error": "".join(traceback.format_exception(exc)),
                },
            )
            raise
        finally:
            if c1_browser is not None:
                c1_browser.close()
            if browser is not None:
                browser.close()

    watch_folder = Path(required_env("RTCM_WATCH_FOLDER"))
    work_folder = Path(required_env("RTCM_WORK_FOLDER"))
    poll_seconds = int(os.getenv("RTCM_POLL_SECONDS", "3"))
    stable_seconds = int(os.getenv("RTCM_FILE_STABLE_SECONDS", "5"))
    batch_time = parse_batch_time(os.getenv("RTCM_BATCH_TIME", "08:15"))
    archive_pdf_enabled = env_bool("RTCM_ARCHIVE_PDF_ENABLED", True)
    c1_enabled = env_bool("C1_WAREHOUSE_ENABLED", False)
    auto_retry_errors = env_bool("RTCM_AUTO_RETRY_ERRORS", False)
    error_retry_seconds = int(os.getenv("RTCM_ERROR_RETRY_SECONDS", "300"))
    if error_retry_seconds < 0:
        raise ValueError("RTCM_ERROR_RETRY_SECONDS は0以上で指定してください")
    watch_folder.mkdir(parents=True, exist_ok=True)
    work_folder.mkdir(parents=True, exist_ok=True)
    state = ProcessingState(work_folder / "processing_state.sqlite3")
    log_path = work_folder / "logs" / "watcher.jsonl"
    config = rtcm_config()
    notify_path = Path(os.getenv("RTCM_NOTIFY_CONFIG", str(application_dir() / "notify.json")))
    notify_config = load_notify_config(notify_path)

    browser = GenuBrowser()
    c1_browser = C1WarehouseBrowser(playwright=browser.playwright)
    try:
        while True:
            now = datetime.now(JST)
            # 監視フォルダへの格納を検出した時点で、元ファイルが後から移動されても
            # 処理できるよう作業フォルダへコピーし、SQLiteとJSONLへqueued登録する。
            for discovered_source in direct_candidates(watch_folder):
                if not is_stable(discovered_source, stable_seconds):
                    continue
                discovered_digest = file_hash(discovered_source)
                queued_copy = queue_copy_path(work_folder, discovered_source, discovered_digest)
                if not queued_copy.exists():
                    shutil.copy2(discovered_source, queued_copy)
                if state.enqueue(discovered_digest, discovered_source, queued_copy):
                    state.record(discovered_digest, discovered_source, "extracting")
                    queued_event = {
                        "status": "extracting",
                        "sha256": discovered_digest,
                        "source": str(discovered_source),
                        "queue_path": str(queued_copy),
                        "source_mtime": datetime.fromtimestamp(
                            discovered_source.stat().st_mtime, tz=JST
                        ).isoformat(timespec="seconds"),
                    }
                    append_log(log_path, queued_event)
                    print(
                        f"抽出待ち受付: {discovered_source} -> {queued_copy}",
                        flush=True,
                    )

            # Extraction is immediate for every received file, independent of the
            # registration cutoff. A durable copy/state exists before the AI call.
            failed_intakes = set()
            for pending in state.ready_items(datetime.max.replace(tzinfo=JST)):
                digest = pending["sha256"]
                job_folder = work_folder / digest[:16]
                if (job_folder / "intake.json").is_file() and (job_folder / "extracted.json").is_file() and pending["status"] != "extracting":
                    continue
                retry_errors = args.retry_errors or args.once or auto_retry_errors
                retry_delay = 0 if (args.retry_errors or args.once) else error_retry_seconds
                if not state.should_process(digest, retry_errors, retry_delay):
                    failed_intakes.add(digest)
                    continue
                try:
                    prepared = prepare_queue_item(pending, state, browser, work_folder, args.dry_run)
                    append_log(log_path, {"status": "queued", **prepared})
                    print(f"キュー登録: {prepared['source_path']} -> {prepared['queue_path']}", flush=True)
                except Exception as exc:
                    failed_intakes.add(digest)
                    detail = traceback.format_exc()
                    append_log(log_path, {"status": "error", "phase": "intake", "sha256": digest, "source": pending["source_path"], "error": detail})
                    print(f"受付時抽出エラー: {exc}", file=sys.stderr)
                    if not args.dry_run:
                        notify_error = notify_safely(notify_config, "error", Path(pending["source_path"]), error=detail)
                        if notify_error:
                            append_log(log_path, {"status": "notification_error", "sha256": digest, "error": notify_error})

            now = datetime.now(JST)
            # --once は従来どおり手動即時実行。常駐時は、直近の平日バッチ時刻
            # 以前に格納されたファイルだけを処理し、それ以降の追加分は次回へ回す。
            cutoff = now if args.once else latest_weekday_batch_cutoff(now, batch_time)
            for queued_item in state.ready_items(cutoff):
                source = Path(queued_item["queue_path"])
                original_source = Path(queued_item["source_path"])
                digest = queued_item["sha256"]
                if digest in failed_intakes:
                    continue
                # 明示指定と単発実行は即時、常駐監視は設定した待機時間後に再試行する。
                retry_errors = args.retry_errors or args.once or auto_retry_errors
                retry_after_seconds = 0 if (args.retry_errors or args.once) else error_retry_seconds
                if not state.should_process(digest, retry_errors, retry_after_seconds):
                    continue
                state.record(digest, original_source, "processing")
                print(f"処理開始: {original_source}", flush=True)
                rename_result = {"status": "not_renamed", "reason": "生成AIから命名情報を取得できません。"}

                def on_metadata(payload):
                    nonlocal original_source
                    try:
                        outcome = rename_source_pdf(original_source, payload, digest, args.dry_run)
                        rename_result.clear()
                        rename_result.update(outcome)
                        if rename_result["status"] in {"renamed", "already_named"}:
                            original_source = Path(rename_result["path"])
                            state.record(digest, original_source, "processing")
                        print(f"PDFリネーム: {json.dumps(rename_result, ensure_ascii=False)}", flush=True)
                    except OSError as exc:
                        rename_result.clear()
                        rename_result.update({"status": "error", "error": str(exc)})

                try:
                    if not source.is_file():
                        raise FileNotFoundError(f"作業用キューファイルが見つかりません: {source}")
                    result = process_file(
                        source,
                        digest,
                        browser,
                        work_folder,
                        config,
                        args.dry_run,
                        watch_folder,
                        archive_pdf_enabled,
                        c1_browser,
                        c1_enabled,
                        on_metadata=on_metadata,
                        pdf_rename=rename_result,
                    )
                    result["source"] = str(original_source)
                    result["pdf_rename"] = rename_result
                    result["queue_source"] = str(source)
                    process_status = (
                        "skipped" if result.get("database", {}).get("skipped") else "success"
                    )
                    state.record(digest, original_source, process_status, result=result)
                    append_log(log_path, {"status": process_status, "sha256": digest, **result})
                    print(json.dumps(result, ensure_ascii=False, default=str))
                    notify_error = None if args.dry_run else notify_safely(
                        notify_config, process_status, original_source, result=result
                    )
                    if notify_error:
                        append_log(log_path, {"status": "notification_error", "sha256": digest, "error": notify_error})
                        print(notify_error, file=sys.stderr)
                    if not args.dry_run:
                        source.unlink(missing_ok=True)
                except Exception as exc:
                    error_detail = "".join(traceback.format_exception(exc))
                    notification_result = (
                        exc.partial_result
                        if isinstance(exc, C1WarehouseRegistrationError)
                        else None
                    )
                    state.record(digest, original_source, "error", error=error_detail)
                    append_log(
                        log_path,
                        {
                            "status": "error",
                            "sha256": digest,
                            "source": str(original_source),
                            "queue_path": str(source),
                            "pdf_rename": rename_result,
                            "error": error_detail,
                        },
                    )
                    print(f"ERROR {original_source}: {exc}", file=sys.stderr)
                    notify_error = None if args.dry_run else notify_safely(
                        notify_config,
                        "error",
                        original_source,
                        result=notification_result,
                        error=error_detail,
                    )
                    if notify_error:
                        append_log(log_path, {"status": "notification_error", "sha256": digest, "error": notify_error})
                        print(notify_error, file=sys.stderr)
            if args.once:
                return 0
            time.sleep(poll_seconds)
    finally:
        c1_browser.close()
        browser.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
