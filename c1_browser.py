from __future__ import annotations

import csv
import io
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from genu_browser import read_windows_credential, save_windows_credential
from import_jyosan import ExtractedData, PREFECTURES


DEFAULT_LOGIN_URL = "http://192.168.9.98/c1web/initialize.ini"
DEFAULT_WAREHOUSE_URL = (
    "http://192.168.9.98/c1web/ax040010/uiax040010.render"
    "?QuiqIdentity=1791265309754x648290619"
)


class C1WarehouseBrowser:
    """C1倉庫登録を行う。ブラウザは最初の登録時まで起動しない。"""

    def __init__(self, playwright=None) -> None:
        self.login_url = os.getenv("C1_LOGIN_URL", DEFAULT_LOGIN_URL).strip()
        self.warehouse_url = os.getenv("C1_WAREHOUSE_URL", DEFAULT_WAREHOUSE_URL).strip()
        self.credential_target = os.getenv(
            "C1_CREDENTIAL_TARGET", "C1_Auto_Fill: 192.168.9.98"
        ).strip()
        self.profile = Path(os.getenv("C1_BROWSER_PROFILE", r"C:\RTCM_AI\c1-browser-profile"))
        self.location_org = os.getenv("C1_WAREHOUSE_LOCATION_ORG", "361000").strip()
        self.code_prefix = os.getenv("C1_WAREHOUSE_CODE_PREFIX", "EG").strip().upper()
        self.code_start = int(os.getenv("C1_WAREHOUSE_CODE_START", "746"))
        self.timeout_ms = int(os.getenv("C1_TIMEOUT_SECONDS", "180")) * 1000
        # sync_playwright().start() を同一スレッドで二重に呼ぶと、既存の
        # Playwright同期APIが動作中であっても asyncio loop 内実行と判定される。
        # 生成AIブラウザと併用する場合は、その実行基盤を共有する。
        self._playwright = playwright
        self._owns_playwright = playwright is None
        self.context = None
        self.page = None
        self._validate_settings()

    def _validate_settings(self) -> None:
        if not re.fullmatch(r"[A-Z0-9]{1,4}", self.code_prefix):
            raise ValueError("C1_WAREHOUSE_CODE_PREFIX は英数字1～4文字で指定してください")
        digits = 5 - len(self.code_prefix)
        if digits <= 0 or not (0 <= self.code_start <= (10**digits - 1)):
            raise ValueError(
                "C1_WAREHOUSE_CODE_START は倉庫コード全体が5文字以内になる数値で指定してください"
            )
        if not re.fullmatch(r"[A-Z0-9]{1,6}", self.location_org, re.IGNORECASE):
            raise ValueError("C1_WAREHOUSE_LOCATION_ORG は1～6文字で指定してください")

    def _start(self) -> None:
        if self.context is not None:
            return
        self.profile.mkdir(parents=True, exist_ok=True)
        if self._playwright is None:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._owns_playwright = True
        self.context = self._playwright.chromium.launch_persistent_context(
            str(self.profile),
            channel="msedge",
            headless=False,
            accept_downloads=False,
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def close(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None
        if self._owns_playwright and self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _menu_available(self) -> bool:
        assert self.page is not None
        return (
            self.page.locator('a[_c1_target="AX040010"]').count() > 0
            or self.page.locator("#c1_menu_AX040010").count() > 0
        )

    def _logged_in(self) -> bool:
        """C1の認証済みメイン画面または倉庫登録画面かを判定する。"""
        assert self.page is not None
        if self._warehouse_available() or self._menu_available():
            return True
        if self.page.locator("#logout").count() > 0:
            return True
        user_id = self.page.locator("#__az_user_id")
        return bool(user_id.count() and user_id.input_value().strip())

    def _wait_for_logged_in(self, timeout_ms: int) -> None:
        deadline = time.monotonic() + (timeout_ms / 1000)
        while time.monotonic() < deadline:
            try:
                if self._logged_in():
                    return
            except Exception:
                # 画面遷移中は一時的にDOMへアクセスできないことがある。
                pass
            time.sleep(0.25)
        raise TimeoutError("C1のログイン完了を確認できませんでした")

    def _warehouse_available(self, page=None) -> bool:
        target = page or self.page
        if not target:
            return False
        has_form = bool(
            target.locator("#mode_new").count()
            and target.locator("#txstr_suk_cd").count()
        )
        if not has_form:
            return False
        # C1は送信開始時に旧DOMを残したまま#shimで画面を覆う。
        # この状態を操作可能な倉庫登録画面と判定すると、次のclickが待ち続ける。
        blocking_layer = target.locator(
            "#shim:visible, .ui-widget-overlay:visible"
        )
        return blocking_layer.count() == 0

    def ensure_logged_in(self) -> None:
        self._start()
        assert self.page is not None
        self.page.goto(self.login_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if self._logged_in():
            return

        username = self.page.locator("#txstr_userid")
        password = self.page.locator("#txstr_password")
        if not username.count() or not password.count():
            self._wait_for_logged_in(self.timeout_ms)
            return

        credential = read_windows_credential(self.credential_target)
        if credential:
            username.fill(credential.username)
            password.fill(credential.password)
            self._click_and_settle(self.page.locator("#set"))
            try:
                self._wait_for_logged_in(60_000)
                return
            except TimeoutError:
                print(
                    "C1の保存済み資格情報でログインできませんでした。"
                    "ブラウザで手動ログインしてください。",
                    flush=True,
                )
                self.page.goto(self.login_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        else:
            print(
                f"汎用資格情報 {self.credential_target!r} がありません。"
                "C1ブラウザで手動ログインしてください。",
                flush=True,
            )

        # 手入力中の値をプロセス側で保持し、ログイン成功後に汎用資格情報へ保存する。
        last_username = ""
        last_password = ""
        deadline = time.monotonic() + (self.timeout_ms / 1000)
        while time.monotonic() < deadline:
            if self._logged_in():
                if last_username and last_password:
                    save_windows_credential(
                        self.credential_target, last_username, last_password
                    )
                    print(
                        f"C1認証情報を汎用資格情報 {self.credential_target!r} に保存しました。",
                        flush=True,
                    )
                return
            if self.page.locator("#txstr_userid").count():
                last_username = self.page.locator("#txstr_userid").input_value() or last_username
                last_password = self.page.locator("#txstr_password").input_value() or last_password
            time.sleep(0.25)
        raise TimeoutError("C1の手動ログインが時間内に完了しませんでした")

    def _click_and_settle(self, locator, timeout_ms: int | None = None) -> None:
        timeout = timeout_ms or self.timeout_ms
        locator.click(timeout=timeout)
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=min(timeout, 5_000))
        except Exception:
            # C1の一部操作は同一ページ内で完了するため、ナビゲーション未発生は許容する。
            pass
        self.page.wait_for_timeout(300)

    def _accept_c1_html_dialog(self, page, message: str) -> None:
        """jQuery UIで表示されるC1の確認ダイアログをOKで閉じる。"""
        dialog = page.locator(".ui-dialog:visible").filter(has_text=message).first
        dialog.wait_for(state="visible", timeout=self.timeout_ms)
        dialog.get_by_role("button", name="OK", exact=True).click(
            timeout=self.timeout_ms
        )
        # 次の操作がオーバーレイの背後へ送られないよう、消えるまで待つ。
        dialog.wait_for(state="hidden", timeout=self.timeout_ms)
        try:
            page.wait_for_load_state(
                "domcontentloaded", timeout=min(self.timeout_ms, 5_000)
            )
        except Exception:
            # 一覧コピーは画面遷移せず、登録実行時だけ画面遷移する。
            pass
        page.wait_for_timeout(300)

    def _accept_c1_html_dialog_if_visible(
        self, page, message: str, timeout_ms: int = 3_000
    ) -> bool:
        dialog = page.locator(".ui-dialog:visible").filter(has_text=message).first
        try:
            dialog.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            return False
        dialog.get_by_role("button", name="OK", exact=True).click(
            timeout=self.timeout_ms
        )
        dialog.wait_for(state="hidden", timeout=self.timeout_ms)
        page.wait_for_timeout(300)
        return True

    def _accept_any_c1_html_dialog_if_visible(
        self, page, timeout_ms: int = 3_000
    ) -> str | None:
        """直前の操作で表示されたC1確認ダイアログをOKで閉じる。"""
        dialog = page.locator(".ui-dialog:visible").first
        try:
            dialog.wait_for(state="visible", timeout=timeout_ms)
        except Exception:
            return None
        message = dialog.locator(".ui-dialog-content").inner_text().strip()
        ok_button = dialog.get_by_role("button", name="OK", exact=True)
        if not ok_button.count():
            raise RuntimeError(
                "C1ダイアログを閉じるOKボタンが見つかりません: " + message
            )
        ok_button.click(timeout=self.timeout_ms)
        dialog.wait_for(state="hidden", timeout=self.timeout_ms)
        page.wait_for_timeout(300)
        return message

    def _activate_warehouse_menu(self) -> None:
        """表示中の倉庫登録リンクを優先し、非表示メニューにも対応する。"""
        assert self.page is not None
        visible_menu = self.page.locator(
            'a[_c1_target="AX040010"]:visible, #c1_menu_AX040010:visible'
        ).first
        if visible_menu.count():
            self._click_and_settle(visible_menu)
            return

        # C1は画面幅やメニュー状態により、同じリンクを非表示要素として持つ。
        # Playwrightの通常clickは非表示要素を待ち続けるため、DOM clickでonclickを実行する。
        hidden_menu = self.page.locator(
            'a[_c1_target="AX040010"], #c1_menu_AX040010'
        ).first
        if hidden_menu.count():
            hidden_menu.evaluate("element => element.click()")
            self.page.wait_for_timeout(300)
            return

        self.page.goto(
            self.warehouse_url,
            wait_until="domcontentloaded",
            timeout=self.timeout_ms,
        )

    def open_warehouse(self) -> None:
        self.ensure_logged_in()
        assert self.context is not None and self.page is not None
        if self._warehouse_available():
            return

        before_pages = list(self.context.pages)
        self._activate_warehouse_menu()

        deadline = time.monotonic() + (self.timeout_ms / 1000)
        while time.monotonic() < deadline:
            for candidate in reversed(self.context.pages):
                if self._warehouse_available(candidate):
                    self.page = candidate
                    return
            if self.page not in before_pages and self._warehouse_available(self.page):
                return
            time.sleep(0.25)
        raise TimeoutError("C1の倉庫登録画面を開けませんでした")

    def _candidate_codes(self):
        prefix = self.code_prefix
        width = 5 - len(prefix)
        first_number = self.code_start
        while prefix is not None:
            for number in range(first_number, 10**width):
                yield f"{prefix}{number:0{width}d}"
            prefix = self._next_alpha_prefix(prefix)
            first_number = 1

    @staticmethod
    def _next_alpha_prefix(prefix: str) -> str | None:
        """同じ桁数の英字接頭辞を EG -> EH -> ... の順に繰り上げる。"""
        if not re.fullmatch(r"[A-Z]+", prefix):
            return None
        chars = list(prefix)
        for index in range(len(chars) - 1, -1, -1):
            if chars[index] != "Z":
                chars[index] = chr(ord(chars[index]) + 1)
                return "".join(chars)
            chars[index] = "A"
        return None

    @staticmethod
    def _parse_warehouse_clipboard(text: str) -> list[dict[str, str]]:
        """C1の一覧コピー（タブ区切り）を倉庫コード・名称へ変換する。"""
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter="\t"))
        header_index = next(
            (
                index
                for index, row in enumerate(rows)
                if row and row[0].strip() == "倉庫コード"
            ),
            None,
        )
        if header_index is None:
            raise ValueError("C1倉庫一覧のクリップボードに見出しがありません")

        header = [cell.strip() for cell in rows[header_index]]
        required = ("倉庫コード", "倉庫名称", "倉庫表示名称")
        missing = [name for name in required if name not in header]
        if missing:
            raise ValueError("C1倉庫一覧に必要な列がありません: " + ", ".join(missing))
        positions = {name: header.index(name) for name in required}
        records: list[dict[str, str]] = []
        for row in rows[header_index + 1 :]:
            if len(row) <= max(positions.values()):
                continue
            code = row[positions["倉庫コード"]].strip().upper()
            if not code:
                continue
            records.append(
                {
                    "warehouse_code": code,
                    "name": row[positions["倉庫名称"]].strip(),
                    "display_name": row[positions["倉庫表示名称"]].strip(),
                }
            )
        if not records:
            raise ValueError("C1倉庫一覧の明細をクリップボードから取得できませんでした")
        return records

    @staticmethod
    def _clear_windows_clipboard() -> None:
        import win32clipboard

        deadline = time.monotonic() + 5
        while True:
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                finally:
                    win32clipboard.CloseClipboard()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Windowsクリップボードを初期化できませんでした")
                time.sleep(0.1)

    def _read_windows_clipboard(self) -> str:
        import win32clipboard

        deadline = time.monotonic() + (self.timeout_ms / 1000)
        while time.monotonic() < deadline:
            try:
                win32clipboard.OpenClipboard()
                try:
                    if win32clipboard.IsClipboardFormatAvailable(
                        win32clipboard.CF_UNICODETEXT
                    ):
                        text = win32clipboard.GetClipboardData(
                            win32clipboard.CF_UNICODETEXT
                        )
                        if text and "倉庫コード" in text:
                            return text
                finally:
                    win32clipboard.CloseClipboard()
            except OSError:
                pass
            time.sleep(0.1)
        raise TimeoutError("C1倉庫一覧がクリップボードへコピーされませんでした")

    @staticmethod
    def _warehouse_lookup_available(page) -> bool:
        try:
            program_id = page.locator("#c1-program-id")
            return bool(
                program_id.count()
                and program_id.first.inner_text().strip() == "AXCM0120"
            )
        except Exception:
            return False

    def _recover_stale_submit_shim(self, page) -> None:
        """登録画面へ戻った後も残るC1の送信中shimを再読込で解消する。"""
        shim = page.locator("#shim:visible")
        if not shim.count():
            return
        try:
            shim.wait_for(state="hidden", timeout=min(self.timeout_ms, 10_000))
            return
        except Exception:
            pass
        if page.locator("#mode_new").count() and page.locator("#txstr_suk_cd").count():
            page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
            page.wait_for_timeout(300)

    def _open_warehouse_lookup(self):
        assert self.context is not None and self.page is not None
        registration_page = self.page
        registration_page.locator("#lb_suk_cd").click(timeout=self.timeout_ms)
        deadline = time.monotonic() + (self.timeout_ms / 1000)
        while time.monotonic() < deadline:
            for candidate in reversed(self.context.pages):
                if self._warehouse_lookup_available(candidate):
                    return registration_page, candidate
            time.sleep(0.1)
        raise TimeoutError("C1の倉庫名称検索画面を開けませんでした")

    def _return_to_registration(self, registration_page, lookup_page) -> None:
        assert self.context is not None
        if lookup_page is not registration_page:
            try:
                lookup_page.close()
            except Exception:
                pass
        else:
            cancel = lookup_page.locator("#cancel")
            if cancel.count():
                self._click_and_settle(cancel)
                self._recover_stale_submit_shim(lookup_page)

        deadline = time.monotonic() + (self.timeout_ms / 1000)
        while time.monotonic() < deadline:
            for candidate in reversed(self.context.pages):
                if self._warehouse_available(candidate):
                    self.page = candidate
                    return
            time.sleep(0.1)
        raise TimeoutError("C1の倉庫登録画面へ戻れませんでした")

    @staticmethod
    def _list_result_count(text: str) -> int | None:
        match = re.search(r"([0-9,]+)件中", text)
        return int(match.group(1).replace(",", "")) if match else None

    def _warehouse_records_from_lookup(
        self, search_value: str = ""
    ) -> list[dict[str, str]]:
        """新規画面の倉庫リンクから検索結果一覧をコピーして取得する。"""
        registration_page, lookup_page = self._open_warehouse_lookup()
        try:
            self._configure_warehouse_lookup(lookup_page, search_value)
            lookup_page.locator("#search").click(timeout=self.timeout_ms)
            if self._accept_c1_html_dialog_if_visible(
                lookup_page, "該当するデータはありません"
            ):
                return []
            exporter = lookup_page.locator("img.c1-list-exporter").first
            exporter.wait_for(state="visible", timeout=self.timeout_ms)
            result_count = self._list_result_count(
                lookup_page.locator(".list-info").first.inner_text()
            )
            self._clear_windows_clipboard()
            exporter.click(timeout=self.timeout_ms)
            self._accept_c1_html_dialog(
                lookup_page, "クリップボードコピーを行います"
            )
            text = self._read_windows_clipboard()
            records = self._parse_warehouse_clipboard(text)
            if result_count is not None and len(records) != result_count:
                target = search_value or "全件"
                raise RuntimeError(
                    f"C1倉庫一覧コピーが不完全です（{target}: "
                    f"画面{result_count}件／コピー{len(records)}件）"
                )
            print(
                f"C1倉庫一覧取得: {search_value or '全件'} {len(records)}件",
                flush=True,
            )
            return records
        finally:
            self._return_to_registration(registration_page, lookup_page)

    def _configure_warehouse_lookup(self, lookup_page, search_value: str) -> None:
        """倉庫検索を栃木工場・コード検索へ固定する。"""
        lookup_page.locator("#ch_jgs").select_option(self.location_org)
        lookup_page.locator("#cmb0115").select_option("1")
        lookup_page.locator("#tx_kensakuti").fill(search_value)

    def _warehouse_code_exists(self, warehouse_code: str) -> bool:
        """候補コードを検索画面で直接照会し、登録済みか最終確認する。"""
        registration_page, lookup_page = self._open_warehouse_lookup()
        try:
            self._configure_warehouse_lookup(lookup_page, warehouse_code)
            lookup_page.locator("#search").click(timeout=self.timeout_ms)
            if self._accept_c1_html_dialog_if_visible(
                lookup_page, "該当するデータはありません"
            ):
                return False
            lookup_page.wait_for_timeout(300)
            code_cells = lookup_page.locator('input[id^="m1_suk_cd-"]')
            return any(
                code_cells.nth(index).input_value().strip().upper()
                == warehouse_code.upper()
                for index in range(code_cells.count())
            )
        finally:
            self._return_to_registration(registration_page, lookup_page)

    @staticmethod
    def _c1_text_width(value: str) -> int:
        """C1の桁数規則（半角1桁、全角2桁）で文字列幅を返す。"""
        return sum(
            2 if unicodedata.east_asian_width(character) in {"W", "F", "A"} else 1
            for character in value
        )

    @classmethod
    def _truncate_c1_text(cls, value: str, max_width: int) -> str:
        """文字を途中で壊さず、C1換算桁数以内に切り詰める。"""
        result: list[str] = []
        width = 0
        for character in value:
            character_width = cls._c1_text_width(character)
            if width + character_width > max_width:
                break
            result.append(character)
            width += character_width
        return "".join(result).rstrip()

    @classmethod
    def _display_name(cls, data: ExtractedData) -> str:
        value = f"{data.seiban} {data.demand_customer_name}"
        if cls._c1_text_width(value) <= 20:
            return value

        # 表示名称は半角20桁（全角10桁）制限のため、名称が長い場合は
        # 生成AIが抽出した半角カナを使う。さらに長い値も登録画面で
        # エラーにならないよう、C1換算20桁で安全に切り詰める。
        shorter_name = data.demand_customer_name_kana or data.demand_customer_name
        return cls._truncate_c1_text(f"{data.seiban} {shorter_name}", 20)

    @staticmethod
    def _kana_name(data: ExtractedData) -> str:
        if not data.demand_customer_name_kana:
            # C1 requires this field. Preserve the known seiban without inventing
            # a pronunciation for a foreign customer name.
            return data.seiban
        value = f"{data.seiban} {data.demand_customer_name_kana}"
        if len(value) > 40:
            raise ValueError(f"C1名称カナが40文字を超えます: {value!r}")
        return value

    @staticmethod
    def _prefecture_code(data: ExtractedData) -> str:
        if data.demand_customer_prefecture == "国外":
            return "00"
        try:
            return f"{PREFECTURES.index(data.demand_customer_prefecture) + 1:02d}"
        except ValueError as exc:
            raise ValueError(
                f"C1へ設定できない都道府県です: {data.demand_customer_prefecture!r}"
            ) from exc

    @classmethod
    def _address_parts(cls, data: ExtractedData) -> tuple[str, str, str, str]:
        """Split an address using C1 widths (half-width 1, full-width 2)."""
        remaining = data.demand_customer_address
        parts: list[str] = []
        for limit in (32, 44, 32, 44):
            part: list[str] = []
            width = 0
            consumed = 0
            for character in remaining:
                character_width = cls._c1_text_width(character)
                if width + character_width > limit:
                    break
                part.append(character)
                width += character_width
                consumed += 1
            parts.append("".join(part))
            remaining = remaining[consumed:]
        if remaining:
            raise ValueError(
                "C1住所が入力可能な合計152桁（半角換算）を超えます: "
                f"{data.demand_customer_address!r}"
            )
        return parts[0], parts[1], parts[2], parts[3]

    def _update_existing_prefecture(
        self, warehouse_code: str, data: ExtractedData
    ) -> bool:
        """既存倉庫の都道府県を照合し、異なる場合だけ変更登録する。"""
        assert self.page is not None
        self._click_and_settle(self.page.locator("#mode_edit"))
        # 新規画面から変更画面へ切り替える際、C1が確認ダイアログを表示する
        # 場合がある。閉じずに検索するとoverlayがクリックを遮断する。
        self._accept_any_c1_html_dialog_if_visible(self.page)
        self.page.locator("#txstr_suk_cd").fill(warehouse_code)
        self._click_and_settle(self.page.locator("#search"))

        prefecture = self.page.locator("#ch_tdfk_cd")
        expected_code = self._prefecture_code(data)
        if prefecture.input_value() == expected_code:
            return False

        prefecture.select_option(expected_code)
        self._click_and_settle(self.page.locator("#execute"))
        self._accept_c1_html_dialog(self.page, "よろしいですか？")
        return True

    def _find_code(self, data: ExtractedData) -> tuple[str, bool]:
        records = self._warehouse_records_from_lookup()
        for existing in records:
            if existing["display_name"].startswith(data.seiban):
                return existing["warehouse_code"], True

        used_codes = {record["warehouse_code"] for record in records}
        checked_prefixes: set[str] = set()
        for code in self._candidate_codes():
            prefix = code[: len(self.code_prefix)]
            if prefix not in checked_prefixes:
                prefix_records = self._warehouse_records_from_lookup(prefix)
                used_codes.update(
                    record["warehouse_code"] for record in prefix_records
                )
                checked_prefixes.add(prefix)
            if code in used_codes:
                continue
            if self._warehouse_code_exists(code):
                raise RuntimeError(
                    f"C1倉庫コード {code} は直接照会では登録済みですが、"
                    "一覧コピーに含まれていません。登録を中止しました。"
                )
            return code, False
        raise RuntimeError("C1倉庫コードの空きがありません")

    def _server_messages(self) -> str:
        assert self.page is not None
        try:
            messages: Any = self.page.evaluate(
                "() => window.com?.fsol?.quiqpro?.messages?.serverMessages || []"
            )
            if messages:
                return str(messages)
        except Exception:
            pass
        return self.page.locator("body").inner_text()[-1000:]

    def register(self, data: ExtractedData, dry_run: bool = False) -> dict[str, Any]:
        planned_code = next(self._candidate_codes())
        base_result: dict[str, Any] = {
            "warehouse_code": planned_code,
            "demand_customer_code": data.demand_customer_code,
            "name": data.demand_customer_name,
            "display_name": self._display_name(data),
            "name_kana": self._kana_name(data),
            "postal_code": data.demand_customer_postal_code,
            "prefecture": data.demand_customer_prefecture,
            "address": data.demand_customer_address,
            "phone": data.demand_customer_phone,
            "location_org": self.location_org,
        }
        if dry_run:
            return {**base_result, "status": "dry_run", "registered": False}

        self.open_warehouse()
        assert self.page is not None
        warehouse_code, already_registered = self._find_code(data)
        base_result["warehouse_code"] = warehouse_code
        if already_registered:
            return {
                **base_result,
                "status": "already_registered",
                "registered": True,
            }

        city, oaza, azatyo, banchi = self._address_parts(data)
        values = {
            "#txstr_suk_cd": warehouse_code,
            "#txstr_suk_mei": data.demand_customer_name,
            "#txstr_suk_hyuj_mei": self._display_name(data),
            "#txstr_suk_mei_kana": self._kana_name(data),
            "#txstr_y_no_b": data.demand_customer_postal_code[:3],
            "#txstr_y_no_a": data.demand_customer_postal_code[3:],
            "#txstr_skgcs_mei": city,
            "#txstr_oaza_mei": oaza,
            "#txstr_azatyo_mei": azatyo,
            "#txstr_banchi_mei": banchi,
            "#txstr_tel": data.demand_customer_phone,
            "#txstr_szit_ssk": self.location_org,
        }
        for selector, value in values.items():
            self.page.locator(selector).fill(value)
        # 添付された登録済レコードと同じ初期値を明示する。
        select_values = {
            "#ch_tdfk_cd": self._prefecture_code(data),
            "#ch_suk_k": "1",
            "#ch_suk_yoto_k": "1",
            "#ch_hoz_k": "0",
            "#ch_nsyuko_rykn_cal_k": "0",
            "#ch_sav_rykn_k": "0",
            "#ch_sav_rykn_cal_k": "0",
            "#ch_suk_yory_mana_k": "0",
            "#ch_gauge_stn_hsyr_k": "1",
            "#ch_gauge_knsn_hyo_stei_k": "0",
            "#ch_std_kesu_stei_k": "00",
            "#ch_suk_ssz_yoh_k": "0",
            "#ch_jsk_jdo_ses_k": "0",
            "#ch_ktai_tmt_knsn_hyo_stei_k": "1",
        }
        for selector, value in select_values.items():
            if self.page.locator(selector).count():
                self.page.locator(selector).select_option(value)

        self._click_and_settle(self.page.locator("#execute"))
        self._accept_c1_html_dialog(
            self.page, "新規登録を行います。よろしいですか？"
        )
        verified_records = self._warehouse_records_from_lookup()
        verified = next(
            (
                record
                for record in verified_records
                if record["warehouse_code"] == warehouse_code
            ),
            None,
        )
        if verified is None or verified["display_name"] != self._display_name(data):
            raise RuntimeError(
                "C1倉庫登録後の一覧確認に失敗しました: " + self._server_messages()
            )
        return {**base_result, "status": "registered", "registered": True}

