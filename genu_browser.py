from __future__ import annotations

import getpass
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from import_jyosan import ExtractedData, strip_json_fence, validate_extracted


DEFAULT_URL = "https://genu.n-coke.com/use-case-builder/execute/7ef1d4ea-837d-4870-baf4-28e7cd35bba9"


@dataclass(frozen=True)
class WindowsCredential:
    username: str
    password: str


def _decode_credential_blob(blob: Any) -> str:
    if isinstance(blob, str):
        return blob
    if not isinstance(blob, (bytes, bytearray)):
        return str(blob or "")
    raw = bytes(blob)
    for encoding in ("utf-16-le", "utf-8"):
        try:
            value = raw.decode(encoding).rstrip("\x00")
            if value:
                return value
        except UnicodeDecodeError:
            pass
    raise ValueError("資格情報マネージャーのパスワードを復号できません")


def read_windows_credential(target: str) -> WindowsCredential | None:
    import pywintypes
    import win32cred

    try:
        credential = win32cred.CredRead(target, win32cred.CRED_TYPE_GENERIC, 0)
    except pywintypes.error as exc:
        # ERROR_NOT_FOUND
        if getattr(exc, "winerror", None) == 1168:
            return None
        raise
    return WindowsCredential(
        username=str(credential.get("UserName") or ""),
        password=_decode_credential_blob(credential.get("CredentialBlob")),
    )


def save_windows_credential(target: str, username: str, password: str) -> None:
    import win32cred

    win32cred.CredWrite(
        {
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": target,
            "UserName": username,
            "CredentialBlob": password.encode("utf-16-le"),
            "Comment": "RTCM AI取込 - genu.n-coke.com",
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        },
        0,
    )


def parse_json_from_result(text: str) -> ExtractedData:
    raw = strip_json_fence(text)
    try:
        return validate_extracted(json.loads(raw))
    except json.JSONDecodeError:
        # 画面側が前後に説明を付けた場合でも、最初のJSONオブジェクトだけを候補にする。
        match = re.search(r"\{[\s\S]*?\}", raw)
        if not match:
            raise ValueError(f"生成AIの画面結果からJSONを取得できません: {raw[:500]}")
        return validate_extracted(json.loads(match.group(0)))


class GenuBrowser:
    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright

        self.url = os.getenv("GENAI_WEB_URL", DEFAULT_URL).strip()
        self.target = os.getenv("GENAI_CREDENTIAL_TARGET", "genu.n-coke.com").strip()
        profile = Path(os.getenv("GENAI_BROWSER_PROFILE", r"C:\RTCM_AI\browser-profile"))
        profile.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            str(profile),
            channel="msedge",
            headless=False,
            accept_downloads=False,
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.ensure_logged_in()

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:
            # ユーザーが先にEdgeを閉じていても、元の処理結果を終了エラーで上書きしない。
            pass
        try:
            self._playwright.stop()
        except Exception:
            pass

    def _upload_input_visible(self) -> bool:
        return self.page.locator('input[type="file"][accept*=".pdf"]').count() > 0

    def _wait_for_upload_page(self, timeout_ms: int = 180_000) -> None:
        self.page.locator('input[type="file"][accept*=".pdf"]').wait_for(
            state="attached", timeout=timeout_ms
        )

    def ensure_logged_in(self) -> None:
        self.page.goto(self.url, wait_until="domcontentloaded", timeout=120_000)
        if self._upload_input_visible():
            return

        username = self.page.locator('input[name="username"]')
        password = self.page.locator('input[name="password"]')
        if username.count() == 0 or password.count() == 0:
            self._wait_for_upload_page()
            return

        credential = read_windows_credential(self.target)
        if credential:
            username.fill(credential.username)
            password.fill(credential.password)
            self.page.locator('button[type="submit"]').click()
            try:
                self._wait_for_upload_page(timeout_ms=60_000)
                return
            except Exception:
                print("保存済み資格情報でログインできませんでした。ブラウザで手動ログインしてください。")
        else:
            print(f"汎用資格情報 {self.target!r} がありません。ブラウザで手動ログインしてください。")

        # 初回または資格情報失効時は、ユーザーがブラウザでログインするまで待つ。
        self._wait_for_upload_page(timeout_ms=10 * 60_000)
        answer = input("ログインに使用した資格情報をWindows資格情報マネージャーへ保存しますか? [y/N]: ").strip()
        if answer.lower() == "y":
            entered_username = input("ユーザー名: ").strip()
            entered_password = getpass.getpass("パスワード（画面には表示されません）: ")
            if not entered_username or not entered_password:
                raise ValueError("資格情報を保存するにはユーザー名とパスワードの両方が必要です")
            save_windows_credential(self.target, entered_username, entered_password)
            print(f"汎用資格情報 {self.target!r} に保存しました。")

    def extract(self, pdf_path: Path) -> ExtractedData:
        if not self._upload_input_visible():
            self.ensure_logged_in()

        file_input = self.page.locator('input[type="file"][accept*=".pdf"]').last
        result_box = self.page.locator("div.prose.max-w-full").last
        attachment_list = self.page.locator("div.my-2.flex.flex-wrap.gap-2").last
        clear_button = self.page.get_by_role("button", name="クリア", exact=True)
        if clear_button.count():
            clear_button.last.click()
            clear_deadline = time.monotonic() + 10
            while time.monotonic() < clear_deadline:
                result_is_clear = not result_box.count() or not result_box.inner_text().strip()
                attachments_are_clear = (
                    not attachment_list.count() or not attachment_list.inner_text().strip()
                )
                if result_is_clear and attachments_are_clear:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("生成AIの前回結果または添付ファイルをクリアできませんでした")

        upload_timeout_ms = int(os.getenv("GENAI_UPLOAD_TIMEOUT_SECONDS", "180")) * 1000
        file_input.set_input_files(str(pdf_path.resolve()))
        print(f"生成AI: PDFアップロード完了を待機します: {pdf_path.name}", flush=True)
        uploaded_file = self.page.get_by_text(pdf_path.name, exact=True).last
        try:
            uploaded_file.wait_for(state="visible", timeout=upload_timeout_ms)
        except Exception as exc:
            raise TimeoutError(
                f"生成AI画面でPDFのアップロード完了を確認できませんでした: {pdf_path.name}"
            ) from exc

        # ファイル名はアップロード開始直後から表示される。添付HTMLでは処理中だけ
        # 添付ボタンとファイルカード内に svg.animate-spin が出るため、その消滅を
        # 一定時間連続して確認してから実行する。
        upload_spinners = self.page.locator("svg.animate-spin")
        upload_deadline = time.monotonic() + (upload_timeout_ms / 1000)
        spinner_free_since: float | None = None
        while time.monotonic() < upload_deadline:
            visible_spinner_count = 0
            for index in range(upload_spinners.count()):
                if upload_spinners.nth(index).is_visible():
                    visible_spinner_count += 1
            if visible_spinner_count == 0:
                if spinner_free_since is None:
                    spinner_free_since = time.monotonic()
                elif time.monotonic() - spinner_free_since >= 1.0:
                    break
            else:
                spinner_free_since = None
            time.sleep(0.2)
        else:
            raise TimeoutError(
                f"生成AI画面のPDFアップロード中スピナーが消えませんでした: {pdf_path.name}"
            )
        print(f"生成AI: PDFアップロード完了: {pdf_path.name}", flush=True)

        execute_button = self.page.get_by_role("button", name="実行", exact=True).last
        execute_button.wait_for(state="visible", timeout=30_000)
        deadline_enabled = time.monotonic() + 30
        while time.monotonic() < deadline_enabled:
            if execute_button.is_enabled():
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("生成AIの実行ボタンが有効になりませんでした")
        execute_button.click()

        result_timeout = int(os.getenv("GENAI_RESULT_TIMEOUT_SECONDS", "300"))
        deadline = time.monotonic() + result_timeout
        last_result = ""
        while time.monotonic() < deadline:
            current = result_box.inner_text().strip() if result_box.count() else ""
            if current:
                last_result = current
                try:
                    # 回答はストリーミング表示されるため、説明文や途中までのJSONを
                    # 最終結果とみなさず、3項目が揃った妥当なJSONになるまで待つ。
                    return parse_json_from_result(current)
                except (ValueError, TypeError):
                    pass
            time.sleep(0.5)
        preview = last_result[:500].replace("\r", " ").replace("\n", " ")
        if preview:
            raise TimeoutError(
                f"生成AIの実行結果が{result_timeout}秒以内に完全なJSONになりませんでした: {preview}"
            )
        raise TimeoutError(
            f"生成AIの実行結果が{result_timeout}秒以内に表示されませんでした"
        )

