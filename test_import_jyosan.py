import json
import unittest
import os
import sys
import tempfile
import types
from datetime import datetime, time as datetime_time
from decimal import Decimal
from pathlib import Path

from c1_browser import C1WarehouseBrowser
from genu_browser import parse_json_from_result, save_windows_credential
from notifier import build_notification, load_notify_config
from import_jyosan import (
    ExtractedData,
    JST,
    RtcmConfig,
    build_row,
    insert_into_oracle,
    load_config_env,
    otmkmsdbmn_for_seiban,
    profit_rate,
    validate_extracted,
)
from watch_folder import (
    ProcessingState,
    archive_pdf,
    direct_candidates,
    env_bool,
    latest_weekday_batch_cutoff,
    parse_batch_time,
    pdf_upload_name,
    process_c1_only,
    queue_copy_path,
    xdw_print_name,
)


class ImportJyosanTest(unittest.TestCase):
    def setUp(self):
        self.config = RtcmConfig("MMC", "MMC", "MMC", "80209", "AIJYSN01")
        self.customer = {
            "demand_customer_code": "109155004",
            "demand_customer_name": "太陽誘電",
            "demand_customer_name_kana": "ﾀｲﾖｳﾕｳﾃﾞﾝ",
            "demand_customer_postal_code": "3701196",
            "demand_customer_address": "佐波郡玉村町川井1796-1",
            "demand_customer_phone": "0270-65-7121",
        }
        self.extracted = ExtractedData(
            "V3579", 0, "20261023", 3_216_000, *self.customer.values()
        )
        self.now = datetime(2026, 9, 2, 10, 35, 25, tzinfo=JST)

    def test_validation(self):
        actual = validate_extracted(
            {
                "seiban": " v3579 ",
                "pdf_hansu": 0,
                "otkmkmnoki": "20261023",
                "otkmkmcost": 3_216_000,
                **self.customer,
            }
        )
        self.assertEqual(actual, self.extracted)

    def test_pdf_hansu_must_be_non_negative_integer(self):
        with self.assertRaises(ValueError):
            validate_extracted(
                {
                    "seiban": "V3579",
                    "pdf_hansu": -1,
                    "otkmkmnoki": "20261023",
                    "otkmkmcost": 3_216_000,
                    **self.customer,
                }
            )

    def test_browser_result_with_code_fence(self):
        actual = parse_json_from_result(
            '```json\n{"seiban":"V3579","pdf_hansu":0,'
            '"otkmkmnoki":"20261023","otkmkmcost":3216000,'
            '"demand_customer_code":"109155004","demand_customer_name":"太陽誘電",'
            '"demand_customer_name_kana":"ﾀｲﾖｳﾕｳﾃﾞﾝ",'
            '"demand_customer_postal_code":"3701196",'
            '"demand_customer_address":"佐波郡玉村町川井1796-1",'
            '"demand_customer_phone":"0270-65-7121"}\n```'
        )
        self.assertEqual(actual, self.extracted)

    def test_browser_result_after_explanation(self):
        actual = parse_json_from_result(
            'PDFから以下を読み取りました。\n\n'
            '{"seiban":"V3579","pdf_hansu":0,'
            '"otkmkmnoki":"20261023","otkmkmcost":3216000,'
            '"demand_customer_code":"109155004","demand_customer_name":"太陽誘電",'
            '"demand_customer_name_kana":"ﾀｲﾖｳﾕｳﾃﾞﾝ",'
            '"demand_customer_postal_code":"3701196",'
            '"demand_customer_address":"佐波郡玉村町川井1796-1",'
            '"demand_customer_phone":"0270-65-7121"}'
        )
        self.assertEqual(actual, self.extracted)

    def test_incomplete_streaming_json_is_not_accepted(self):
        with self.assertRaises(ValueError):
            parse_json_from_result(
                'PDFから以下を読み取りました。\n\n'
                '{"seiban":"V3579","pdf_hansu":0,"otkmkmnoki":"202610'
            )

    def test_pdf_upload_name_removes_spaces(self):
        self.assertEqual(pdf_upload_name(Path("V3579 R1 DIC.pdf")), "V3579R1DIC.pdf")
        self.assertEqual(pdf_upload_name(Path("V3579　R1\tDIC.xdw")), "V3579R1DIC.pdf")

    def test_xdw_print_name_is_unique_and_has_no_spaces(self):
        name = xdw_print_name(Path("U9007884 R0 瓢屋.xdw"), "abcdef1234567890")
        self.assertEqual(name, "U9007884R0瓢屋_abcdef123456.xdw")

    def test_watch_folder_does_not_scan_subfolders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            direct = root / "direct.pdf"
            direct.write_bytes(b"pdf")
            nested = root / "2026"
            nested.mkdir()
            (nested / "nested.pdf").write_bytes(b"pdf")
            self.assertEqual(direct_candidates(root), [direct])

    def test_weekday_batch_cutoff(self):
        batch_time = parse_batch_time("08:15")
        monday_before = datetime(2026, 10, 5, 8, 14, tzinfo=JST)
        monday_after = datetime(2026, 10, 5, 8, 15, tzinfo=JST)
        saturday = datetime(2026, 10, 3, 12, 0, tzinfo=JST)
        self.assertEqual(
            latest_weekday_batch_cutoff(monday_before, batch_time),
            datetime(2026, 10, 2, 8, 15, tzinfo=JST),
        )
        self.assertEqual(
            latest_weekday_batch_cutoff(monday_after, batch_time),
            datetime(2026, 10, 5, 8, 15, tzinfo=JST),
        )
        self.assertEqual(
            latest_weekday_batch_cutoff(saturday, batch_time),
            datetime(2026, 10, 2, 8, 15, tzinfo=JST),
        )

    def test_batch_cutoff_keeps_later_file_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = root / "before.pdf"
            after = root / "after.pdf"
            before.write_bytes(b"before")
            after.write_bytes(b"after")
            cutoff = datetime(2026, 10, 1, 8, 15, tzinfo=JST)
            os.utime(before, (cutoff.timestamp() - 1, cutoff.timestamp() - 1))
            os.utime(after, (cutoff.timestamp() + 1, cutoff.timestamp() + 1))
            self.assertEqual(direct_candidates(root, cutoff), [before])

    def test_sqlite_queue_survives_original_file_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watch = root / "inbox"
            work = root / "work"
            watch.mkdir()
            source = watch / "V3579.pdf"
            source.write_bytes(b"pdf")
            digest = "a" * 64
            queued_copy = queue_copy_path(work, source, digest)
            queued_copy.write_bytes(source.read_bytes())
            state = ProcessingState(work / "processing_state.sqlite3")
            try:
                self.assertTrue(state.enqueue(digest, source, queued_copy))
                self.assertFalse(state.enqueue(digest, source, queued_copy))
                archive = watch / "2026"
                archive.mkdir()
                source.rename(archive / source.name)
                ready = state.ready_items(datetime.now(JST))
                self.assertEqual(len(ready), 1)
                self.assertEqual(Path(ready[0]["queue_path"]), queued_copy)
                self.assertTrue(queued_copy.exists())
            finally:
                state.connection.close()

    def test_archive_pdf_enabled_property(self):
        key = "RTCM_ARCHIVE_PDF_ENABLED"
        previous = os.environ.get(key)
        try:
            os.environ[key] = "true"
            self.assertTrue(env_bool(key))
            os.environ[key] = "false"
            self.assertFalse(env_bool(key, True))
            os.environ[key] = "invalid"
            with self.assertRaises(ValueError):
                env_bool(key)
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    def test_archive_pdf_moves_to_execution_year(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.pdf"
            source.write_bytes(b"pdf")
            archived = archive_pdf(source, source, root, "abcdef123456", self.now)
            self.assertEqual(archived, root / "2026" / "input.pdf")
            self.assertTrue(archived.exists())
            self.assertFalse(source.exists())

    def test_archive_xdw_moves_source_and_converted_pdf_to_execution_year(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.xdw"
            converted = root / "converted.pdf"
            source.write_bytes(b"xdw")
            converted.write_bytes(b"pdf")
            archived = archive_pdf(source, converted, root, "abcdef123456", self.now)
            self.assertEqual(archived, root / "2026" / "converted.pdf")
            self.assertTrue(archived.exists())
            self.assertTrue((root / "2026" / "input.xdw").exists())
            self.assertFalse(source.exists())
            self.assertFalse(converted.exists())

    def test_notify_config_and_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notify.json"
            path.write_text(
                '{"notify":{"smtp_host":"mail.local","smtp_port":25,'
                '"smtp_auth_enabled":false,"smtp_use_tls":false,"smtp_use_ssl":false,'
                '"from_addr":"sender@example.com","subject_prefix":"[RTCM]",'
                '"recipients":[{"email":"on@example.com","enabled":true},'
                '{"email":"off@example.com","enabled":false}]}}',
                encoding="utf-8",
            )
            config = load_notify_config(path)
            self.assertIsNotNone(config)
            self.assertEqual(config.recipients, ("on@example.com",))
            subject, body = build_notification(
                "success",
                Path("A2433.pdf"),
                {
                    "extracted": {"seiban": "A2433"},
                    "database": {"hansu": 37},
                },
            )
            self.assertIn("成功", subject)
            self.assertIn("製番: A2433", body)

    def test_missing_order_notification_adds_c1_guidance(self):
        message = "T_JUCHUZAN_CTLに対象製番がありません: A2433"
        subject, body = build_notification("error", Path("A2433.pdf"), None, message)
        self.assertIn("失敗", subject)
        self.assertIn(message, body)
        guidance = "C1に受注情報、製番が登録されていることを確認してください。"
        self.assertIn(guidance, body)
        retry_guidance = (
            "C1に当日登録済みの場合は、夜間バッチでC1からRTCMへ連携されるため、"
            "翌日の自動再実行で正常終了する見込みです。"
        )
        self.assertIn(retry_guidance, body)
        self.assertLess(body.index(message), body.index(guidance))
        self.assertLess(body.index(guidance), body.index(retry_guidance))

    def test_u_seiban_is_skipped_without_database_access(self):
        extracted = ExtractedData(
            "U9007885", 0, "20261030", 988_000, *self.customer.values()
        )
        result = insert_into_oracle(extracted, self.config, dry_run=False)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["committed"])
        self.assertIn("U製番", result["skip_reason"])

    def test_revised_pdf_is_skipped_without_database_access(self):
        extracted = ExtractedData(
            "V3579", 1, "20261023", 3_216_000, *self.customer.values()
        )
        result = insert_into_oracle(extracted, self.config, dry_run=False)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["committed"])
        self.assertEqual(result["pdf_hansu"], 1)
        self.assertIn("初版（0版）以外", result["skip_reason"])

    def test_revised_pdf_notification_reports_skip(self):
        result = {
            "extracted": {"seiban": "V3579", "pdf_hansu": 2},
            "database": {
                "skipped": True,
                "pdf_hansu": 2,
                "skip_reason": "PDFの版数が2版のため、初版（0版）以外はRTCM登録対象外です。",
            },
            "pdf": r"C:\RTCM_AI\work\V3579.pdf",
        }
        subject, body = build_notification("skipped", Path("V3579.pdf"), result)
        self.assertIn("スキップ", subject)
        self.assertIn("PDF版数: 2", body)
        self.assertIn("初版（0版）以外", body)

    def test_u_seiban_notification_reports_skip(self):
        result = {
            "extracted": {"seiban": "U9007885", "pdf_hansu": 0},
            "database": {
                "skipped": True,
                "skip_reason": "U製番のためRTCM登録対象外です。",
            },
            "archived_pdf": r"C:\RTCM_AI\inbox\2026\U9007885.pdf",
        }
        subject, body = build_notification("skipped", Path("U9007885.pdf"), result)
        self.assertIn("スキップ", subject)
        self.assertIn("処理結果: スキップ", body)
        self.assertIn("製番: U9007885", body)
        self.assertIn("RTCMへの登録は行っていません。", body)

    def test_config_env_is_loaded_without_dotenv(self):
        key = "RTCM_TEST_WATCH_FOLDER"
        os.environ.pop(key, None)
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "config.env"
            env_file.write_text(f"\ufeff{key}=C:\\RTCM_AI\\inbox\n", encoding="utf-8")
            load_config_env(env_file)
        self.assertEqual(os.environ.pop(key), r"C:\RTCM_AI\inbox")

    def test_profit_rate_is_truncated(self):
        self.assertEqual(profit_rate(Decimal(1_584_000), Decimal(4_800_000)), Decimal("33.00"))
        self.assertEqual(profit_rate(Decimal(1_783_863), Decimal(4_800_000)), Decimal("37.16"))

    def test_error_state_can_be_retried_with_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ProcessingState(Path(directory) / "state.sqlite3")
            try:
                state.record("digest", Path("failed.pdf"), "error", error="test")
                self.assertFalse(state.should_process("digest", retry_errors=False))
                self.assertTrue(state.should_process("digest", retry_errors=True))
                self.assertFalse(
                    state.should_process("digest", retry_errors=True, retry_after_seconds=300)
                )
            finally:
                state.connection.close()

    def test_error_auto_retry_is_disabled_by_default(self):
        key = "RTCM_AUTO_RETRY_ERRORS"
        previous = os.environ.pop(key, None)
        try:
            self.assertFalse(env_bool(key, False))
        finally:
            if previous is not None:
                os.environ[key] = previous

    def test_existing_hansu_2_creates_hansu_3(self):
        latest = {
            "KAICD": "MMC",
            "KYOTENCD": "MMC",
            "KOJCD": "MMC",
            "SEIBAN": "V3579",
            "HANSU": 2,
            "JKYSNCOST": Decimal(3_016_137),
            "OTKMKMSDBMN": "361220",
            "BIKO": " ",
        }
        row = build_row(
            latest,
            self.extracted,
            self.config,
            Decimal(4_800_000),
            148065,
            self.now,
        )
        self.assertEqual(row["HANSU"], 3)
        self.assertEqual(row["GETUDO"], " ")
        self.assertEqual(row["RECNO"], 148065)
        self.assertEqual(row["JKYSNHANKG"], Decimal(4_800_000))
        self.assertEqual(row["JKYSNEKIKG"], Decimal(1_783_863))
        self.assertEqual(row["JKYSNEKIRT"], Decimal("37.16"))
        self.assertEqual(row["OTKMKMCOST"], Decimal(3_216_000))
        self.assertEqual(row["OTKMKMEKI"], Decimal(1_584_000))
        self.assertEqual(row["OTKMKMEKIRT"], Decimal("33.00"))
        self.assertEqual(row["OTKMKMSDBMN"], "361220")

    def test_no_existing_row_creates_hansu_0(self):
        row = build_row(
            None,
            self.extracted,
            self.config,
            Decimal(4_800_000),
            148065,
            self.now,
        )
        self.assertEqual(row["HANSU"], 0)
        self.assertEqual(row["GETUDO"], " ")
        self.assertEqual(row["OTMKMSDBMN"], "361220")
        # 初版の未指定列（JKYSNCOST等）はINSERT時にテーブル既定値を使用する。
        self.assertNotIn("JKYSNCOST", row)
        self.assertEqual(row["JKYSNEKIRT"], Decimal("100.00"))

    def test_c1_dry_run_builds_registered_record_shape_without_browser(self):
        result = C1WarehouseBrowser().register(self.extracted, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["registered"])
        self.assertEqual(result["warehouse_code"], "EG746")
        self.assertEqual(result["display_name"], "V3579 太陽誘電")
        self.assertEqual(result["name_kana"], "V3579 ﾀｲﾖｳﾕｳﾃﾞﾝ")

    def test_c1_browser_does_not_stop_shared_playwright(self):
        class FakePlaywright:
            def __init__(self):
                self.stop_called = False

            def stop(self):
                self.stop_called = True

        shared_playwright = FakePlaywright()
        browser = C1WarehouseBrowser(playwright=shared_playwright)
        browser.close()
        self.assertFalse(shared_playwright.stop_called)
        self.assertIs(browser._playwright, shared_playwright)

    def test_windows_credential_is_saved_as_unicode(self):
        captured = {}

        def cred_write(credential, flags):
            captured.update(credential)
            captured["flags"] = flags

        fake_win32cred = types.SimpleNamespace(
            CRED_TYPE_GENERIC=1,
            CRED_PERSIST_LOCAL_MACHINE=2,
            CredWrite=cred_write,
        )
        original = sys.modules.get("win32cred")
        sys.modules["win32cred"] = fake_win32cred
        try:
            save_windows_credential("C1_Auto_Fill: test", "user", "日本語pass")
        finally:
            if original is None:
                sys.modules.pop("win32cred", None)
            else:
                sys.modules["win32cred"] = original

        self.assertEqual(captured["CredentialBlob"], "日本語pass")
        self.assertIsInstance(captured["CredentialBlob"], str)

    def test_exe_build_includes_win32_credential_dependencies(self):
        build_script = (Path(__file__).parent / "build_exe.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("--hidden-import win32cred", build_script)
        self.assertIn("--hidden-import win32timezone", build_script)
        self.assertIn("--hidden-import pywintypes", build_script)

    def test_hidden_c1_warehouse_menu_is_activated_by_dom_click(self):
        class FakeLocator:
            def __init__(self, count, calls):
                self._count = count
                self.calls = calls

            @property
            def first(self):
                return self

            def count(self):
                return self._count

            def evaluate(self, script):
                self.calls.append(("evaluate", script))

        class FakePage:
            def __init__(self):
                self.calls = []

            def locator(self, selector):
                self.calls.append(("locator", selector))
                if ":visible" in selector:
                    return FakeLocator(0, self.calls)
                return FakeLocator(1, self.calls)

            def wait_for_timeout(self, timeout):
                self.calls.append(("wait", timeout))

            def goto(self, *args, **kwargs):
                self.calls.append(("goto", args, kwargs))

        browser = C1WarehouseBrowser()
        browser.page = FakePage()
        browser._activate_warehouse_menu()
        self.assertTrue(any(call[0] == "evaluate" for call in browser.page.calls))
        self.assertFalse(any(call[0] == "goto" for call in browser.page.calls))

    def test_c1_logged_in_is_detected_by_logout_on_main_page(self):
        class FakeLocator:
            def __init__(self, count=0, value=""):
                self._count = count
                self._value = value

            def count(self):
                return self._count

            def input_value(self):
                return self._value

        class FakePage:
            def locator(self, selector):
                if selector == "#logout":
                    return FakeLocator(count=1)
                return FakeLocator()

        browser = C1WarehouseBrowser()
        browser.page = FakePage()
        self.assertTrue(browser._logged_in())

    def test_c1_code_search_switches_to_inquiry_mode_only_once(self):
        browser = C1WarehouseBrowser()
        switched = []
        inquiries = []
        browser._switch_mode = switched.append

        def inquire(code):
            inquiries.append(code)
            if len(inquiries) == 1:
                return {
                    "warehouse_code": code,
                    "name": "別需要家",
                    "display_name": "V0000 別需要家",
                    "name_kana": "V0000 ﾍﾞﾂｼﾞｭﾖｳｶ",
                }
            return None

        browser._inquire = inquire
        code, already_registered = browser._find_code(self.extracted)
        self.assertEqual(switched, ["#mode_inquire"])
        self.assertEqual(inquiries, ["EG746", "EG747"])
        self.assertEqual(code, "EG747")
        self.assertFalse(already_registered)

    def test_c1_clear_confirmation_is_accepted(self):
        calls = []

        class FakeButton:
            def click(self, timeout):
                calls.append(("click", timeout))

        class FakeDialog:
            def filter(self, has_text):
                calls.append(("filter", has_text))
                return self

            def count(self):
                return 1

            @property
            def first(self):
                return self

            def get_by_role(self, role, name, exact):
                calls.append(("role", role, name, exact))
                return FakeButton()

        class FakePage:
            def locator(self, selector):
                calls.append(("locator", selector))
                return FakeDialog()

            def wait_for_timeout(self, timeout):
                calls.append(("wait", timeout))

        browser = C1WarehouseBrowser()
        browser.page = FakePage()
        self.assertTrue(browser._accept_clear_confirmation())
        self.assertTrue(any(call[0] == "click" for call in calls))

    def test_customer_address_length_is_checked_for_c1_field(self):
        with self.assertRaises(ValueError):
            validate_extracted(
                {
                    "seiban": "V3579",
                    "pdf_hansu": 0,
                    "otkmkmnoki": "20261023",
                    "otkmkmcost": 3_216_000,
                    **{**self.customer, "demand_customer_address": "あ" * 33},
                }
            )

    def test_c1_only_extracts_pdf_and_never_updates_rtcm(self):
        class FakeGenuBrowser:
            def __init__(self, extracted):
                self.extracted = extracted
                self.uploaded_pdf = None

            def extract(self, pdf_path):
                self.uploaded_pdf = pdf_path
                return self.extracted

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "V 3579.pdf"
            source_pdf.write_bytes(b"%PDF-1.4\n")
            fake_browser = FakeGenuBrowser(self.extracted)
            result = process_c1_only(
                source_pdf,
                fake_browser,
                root / "work",
                C1WarehouseBrowser(),
                dry_run=True,
            )
            self.assertEqual(result["mode"], "c1_only")
            self.assertEqual(result["input_type"], "pdf")
            self.assertEqual(result["database"]["status"], "not_run")
            self.assertFalse(result["database"]["committed"])
            self.assertEqual(result["c1_warehouse"]["status"], "dry_run")
            self.assertEqual(fake_browser.uploaded_pdf.name, "V3579.pdf")
            self.assertTrue(Path(result["extracted_json"]).is_file())
            result_path = Path(result["result_json"])
            self.assertTrue(result_path.is_file())
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["extracted"]["seiban"], "V3579")

    def test_c1_only_reads_json_without_generative_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_json = root / "M7604.json"
            source_json.write_text(
                json.dumps(self.extracted.__dict__, ensure_ascii=False),
                encoding="utf-8",
            )
            result = process_c1_only(
                source_json,
                None,
                root / "work",
                C1WarehouseBrowser(),
                dry_run=True,
            )
            self.assertEqual(result["input_type"], "json")
            self.assertEqual(result["source_json"], str(source_json.resolve()))
            self.assertNotIn("upload_pdf", result)
            self.assertEqual(result["extracted"]["seiban"], "V3579")
            self.assertEqual(result["c1_warehouse"]["status"], "dry_run")

    def test_c1_only_rejects_unsupported_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "extracted.txt"
            source.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PDFまたはJSONファイル"):
                process_c1_only(
                    source,
                    object(),
                    Path(directory) / "work",
                    C1WarehouseBrowser(),
                    dry_run=True,
                )

    def test_otmkmsdbmn_is_derived_from_seiban_prefix(self):
        for seiban in ("A0907", "G1234", "H1234", "K1234", "L1801", "M0123"):
            with self.subTest(seiban=seiban):
                self.assertEqual(otmkmsdbmn_for_seiban(seiban), "361210")
        for seiban in ("M1123", "M9123", "V3579"):
            with self.subTest(seiban=seiban):
                self.assertEqual(otmkmsdbmn_for_seiban(seiban), "361220")
        with self.assertRaisesRegex(ValueError, "決定できない製番"):
            otmkmsdbmn_for_seiban("B1234")


if __name__ == "__main__":
    unittest.main()
