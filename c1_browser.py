from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from genu_browser import read_windows_credential, save_windows_credential
from import_jyosan import ExtractedData


DEFAULT_LOGIN_URL = "http://192.168.9.98/c1web/initialize.ini"
DEFAULT_WAREHOUSE_URL = (
    "http://192.168.9.98/c1web/ax040010/uiax040010.render"
    "?QuiqIdentity=1791265309754x648290619"
)


class C1WarehouseBrowser:
    """C1倉庫登録を行う。ブラウザは最初の登録時まで起動しない。"""

    def __init__(self) -> None:
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
        self._playwright = None
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
        from playwright.sync_api import sync_playwright

        self.profile.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
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
        if self._playwright is not None:
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

    def _warehouse_available(self, page=None) -> bool:
        target = page or self.page
        return bool(target and target.locator("#mode_new").count() and target.locator("#txstr_suk_cd").count())

    def ensure_logged_in(self) -> None:
        self._start()
        assert self.page is not None
        self.page.goto(self.login_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        if self._menu_available():
            return

        username = self.page.locator("#txstr_userid")
        password = self.page.locator("#txstr_password")
        if not username.count() or not password.count():
            self.page.wait_for_selector(
                'a[_c1_target="AX040010"], #c1_menu_AX040010', timeout=self.timeout_ms
            )
            return

        credential = read_windows_credential(self.credential_target)
        if credential:
            username.fill(credential.username)
            password.fill(credential.password)
            self._click_and_settle(self.page.locator("#set"))
            try:
                self.page.wait_for_selector(
                    'a[_c1_target="AX040010"], #c1_menu_AX040010', timeout=60_000
                )
                return
            except Exception:
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
            if self._menu_available():
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

    def open_warehouse(self) -> None:
        self.ensure_logged_in()
        assert self.context is not None and self.page is not None
        if self._warehouse_available():
            return

        menu = self.page.locator('a[_c1_target="AX040010"]').first
        if not menu.count():
            menu = self.page.locator("#c1_menu_AX040010")
        before_pages = list(self.context.pages)
        if menu.count():
            menu.click()
        else:
            self.page.goto(self.warehouse_url, wait_until="domcontentloaded", timeout=self.timeout_ms)

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

    def _inquire(self, warehouse_code: str) -> dict[str, str] | None:
        assert self.page is not None
        self._click_and_settle(self.page.locator("#mode_inquire"))
        self.page.locator("#txstr_suk_cd").fill(warehouse_code)
        self._click_and_settle(self.page.locator("#search"))
        name = self.page.locator("#txstr_suk_mei").input_value().strip()
        if not name:
            return None
        return {
            "warehouse_code": self.page.locator("#txstr_suk_cd").input_value().strip(),
            "name": name,
            "display_name": self.page.locator("#txstr_suk_hyuj_mei").input_value().strip(),
            "name_kana": self.page.locator("#txstr_suk_mei_kana").input_value().strip(),
        }

    def _candidate_codes(self):
        width = 5 - len(self.code_prefix)
        for number in range(self.code_start, 10**width):
            yield f"{self.code_prefix}{number:0{width}d}"

    @staticmethod
    def _display_name(data: ExtractedData) -> str:
        value = f"{data.seiban} {data.demand_customer_name}"
        if len(value) > 20:
            raise ValueError(f"C1表示名称が20文字を超えます: {value!r}")
        return value

    @staticmethod
    def _kana_name(data: ExtractedData) -> str:
        value = f"{data.seiban} {data.demand_customer_name_kana}"
        if len(value) > 40:
            raise ValueError(f"C1名称カナが40文字を超えます: {value!r}")
        return value

    def _find_code(self, data: ExtractedData) -> tuple[str, bool]:
        expected_display = self._display_name(data)
        for code in self._candidate_codes():
            existing = self._inquire(code)
            if existing is None:
                return code, False
            if (
                existing["name"] == data.demand_customer_name
                and existing["display_name"] == expected_display
            ):
                return code, True
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
            return {**base_result, "status": "already_registered", "registered": True}

        self._click_and_settle(self.page.locator("#mode_new"))
        values = {
            "#txstr_suk_cd": warehouse_code,
            "#txstr_suk_mei": data.demand_customer_name,
            "#txstr_suk_hyuj_mei": self._display_name(data),
            "#txstr_suk_mei_kana": self._kana_name(data),
            "#txstr_y_no_b": data.demand_customer_postal_code[:3],
            "#txstr_y_no_a": data.demand_customer_postal_code[3:],
            "#txstr_skgcs_mei": data.demand_customer_address,
            "#txstr_tel": data.demand_customer_phone,
            "#txstr_szit_ssk": self.location_org,
        }
        for selector, value in values.items():
            self.page.locator(selector).fill(value)
        # 添付された登録済レコードと同じ初期値を明示する。
        select_values = {
            "#ch_tdfk_cd": "00",
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

        self.page.once("dialog", lambda dialog: dialog.accept())
        self._click_and_settle(self.page.locator("#execute"))
        verified = self._inquire(warehouse_code)
        if verified is None or verified["display_name"] != self._display_name(data):
            raise RuntimeError(
                "C1倉庫登録後の照会確認に失敗しました: " + self._server_messages()
            )
        return {**base_result, "status": "registered", "registered": True}

