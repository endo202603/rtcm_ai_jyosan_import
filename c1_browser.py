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
        return bool(target and target.locator("#mode_new").count() and target.locator("#txstr_suk_cd").count())

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

    def _accept_clear_confirmation(self) -> bool:
        """モード切替時に表示されるC1のクリア確認を承認する。"""
        assert self.page is not None
        dialog = self.page.locator(".ui-dialog:visible").filter(
            has_text="クリアしてもよろしいですか"
        )
        if not dialog.count():
            return False
        dialog.first.get_by_role("button", name="OK", exact=True).click(
            timeout=self.timeout_ms
        )
        self.page.wait_for_timeout(300)
        return True

    def _switch_mode(self, selector: str) -> None:
        assert self.page is not None
        self._click_and_settle(self.page.locator(selector))
        self._accept_clear_confirmation()

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

    def _inquire(self, warehouse_code: str) -> dict[str, str] | None:
        assert self.page is not None
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
        # 照会モードへの切替は採番処理の開始時に1回だけ行う。
        # 候補ごとに押すと、前回の照会結果を消す確認ダイアログが毎回表示される。
        self._switch_mode("#mode_inquire")
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

        self._switch_mode("#mode_new")
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

