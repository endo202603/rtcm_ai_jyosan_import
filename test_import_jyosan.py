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
    C1WarehouseRegistrationError,
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
            "demand_customer_prefecture": "群馬県",
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

    def test_missing_c1_optional_values_are_accepted(self):
        actual = validate_extracted(
            {
                "seiban": "A2470",
                "pdf_hansu": 0,
                "otkmkmnoki": "20261030",
                "otkmkmcost": 1_620_000,
                "demand_customer_code": "115112001",
                "demand_customer_name": "パナソニックインダストリー",
                "demand_customer_name_kana": "ﾊﾟﾅｿﾆｯｸｲﾝﾀﾞｽﾄﾘｰ",
                "demand_customer_postal_code": "5100883",
                "demand_customer_prefecture": "三重県",
                "demand_customer_address": "四日市市泊小柳町２－３",
                "demand_customer_phone": "",
            }
        )
        self.assertEqual(actual.demand_customer_phone, "")
        a2469 = validate_extracted(
            {
                "seiban": "A2469",
                "pdf_hansu": 0,
                "otkmkmnoki": "20270331",
                "otkmkmcost": 8_076_000,
                "demand_customer_code": "102284007",
                "demand_customer_name": "住友金属鉱山",
                "demand_customer_name_kana": "ｽﾐﾄﾓｷﾝｿﾞｸｺｳｻﾞﾝ",
                "demand_customer_postal_code": "1988601",
                "demand_customer_prefecture": "東京都",
                "demand_customer_address": "青梅市末広町１－６－１",
                "demand_customer_phone": "",
            }
        )
        self.assertEqual(a2469.demand_customer_phone, "")

    def test_overseas_customer_without_kana_postal_or_phone_is_accepted(self):
        address = "中国淮安市淮安区経済開発区経十九路、江蘇達康智能装備産業園１～５号"
        actual = validate_extracted(
            {
                "seiban": "A2468",
                "pdf_hansu": 0,
                "otkmkmnoki": "20270831",
                "otkmkmcost": 77_048_000,
                "demand_customer_code": "114618001",
                "demand_customer_name": "江蘇芯声微電子科技有限公司",
                "demand_customer_name_kana": "",
                "demand_customer_postal_code": "",
                "demand_customer_prefecture": "国外",
                "demand_customer_address": address,
                "demand_customer_phone": "",
            }
        )
        self.assertEqual(actual.demand_customer_prefecture, "国外")
        self.assertEqual(actual.demand_customer_postal_code, "")
        self.assertEqual(actual.demand_customer_name_kana, "")
        self.assertEqual(C1WarehouseBrowser._prefecture_code(actual), "00")
        address_parts = C1WarehouseBrowser._address_parts(actual)
        self.assertEqual("".join(address_parts), address)
        self.assertEqual(
            [C1WarehouseBrowser._c1_text_width(part) for part in address_parts],
            [32, 34, 0, 0],
        )
        self.assertEqual(C1WarehouseBrowser._kana_name(actual), "A2468")
        self.assertLessEqual(
            C1WarehouseBrowser._c1_text_width(
                C1WarehouseBrowser._display_name(actual)
            ),
            20,
        )

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
            '"demand_customer_prefecture":"群馬県",'
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
            '"demand_customer_prefecture":"群馬県",'
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

    def test_c1_failure_notification_reports_rtcm_success(self):
        result = {
            "extracted": {
                "seiban": "V3667",
                "pdf_hansu": 0,
                "demand_customer_code": "110413200",
                "demand_customer_name": "トヨタバッテリー",
            },
            "database": {"committed": True, "hansu": 3, "recno": 119061},
            "c1_warehouse": {
                "status": "error",
                "registered": False,
                "error": "検索画面の操作に失敗しました。",
            },
        }
        subject, body = build_notification(
            "error", Path("V3667-00-S.pdf"), result, "Traceback: C1 error"
        )
        self.assertIn("C1倉庫登録失敗（RTCM登録成功）", subject)
        self.assertIn("RTCM登録結果: 成功", body)
        self.assertIn("C1倉庫登録結果: 失敗", body)
        self.assertIn("C1エラー: 検索画面の操作に失敗しました。", body)
        self.assertIn("RTCMを重複登録せず", body)
        self.assertIn("エラー詳細:", body)

    def test_c1_failure_exception_keeps_partial_result_for_notification(self):
        partial_result = {"database": {"committed": True}}
        error = C1WarehouseRegistrationError("C1 failed", partial_result)
        self.assertIs(error.partial_result, partial_result)

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

    def test_c1_display_name_uses_half_width_kana_when_full_width_exceeds_limit(self):
        extracted = ExtractedData(
            "V3667",
            0,
            "20270115",
            265_600,
            "104132001",
            "トヨタバッテリー",
            "ﾄﾖﾀﾊﾞｯﾃﾘｰ",
            "4310452",
            "静岡県",
            "湖西市境宿５５５",
            "053-577-3461",
        )
        self.assertEqual(
            C1WarehouseBrowser._display_name(extracted),
            "V3667 ﾄﾖﾀﾊﾞｯﾃﾘｰ",
        )
        self.assertLessEqual(
            C1WarehouseBrowser._c1_text_width(
                C1WarehouseBrowser._display_name(extracted)
            ),
            20,
        )

    def test_c1_display_name_truncates_oversized_kana_by_c1_width(self):
        extracted = ExtractedData(
            "V0001",
            0,
            "20270115",
            1,
            "123456789",
            "非常に長い需要家名称テスト",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "1234567",
            "東京都",
            "住所",
            "03-1234-5678",
        )
        display_name = C1WarehouseBrowser._display_name(extracted)
        self.assertEqual(display_name, "V0001 ABCDEFGHIJKLMN")
        self.assertEqual(C1WarehouseBrowser._c1_text_width(display_name), 20)

    def test_c1_prefecture_code_uses_japanese_prefecture_order(self):
        self.assertEqual(C1WarehouseBrowser._prefecture_code(self.extracted), "10")
        extracted = ExtractedData(
            "V3667",
            0,
            "20270115",
            265_600,
            "104132001",
            "トヨタバッテリー",
            "ﾄﾖﾀﾊﾞｯﾃﾘｰ",
            "4310452",
            "静岡県",
            "湖西市境宿５５５",
            "053-577-3461",
        )
        self.assertEqual(C1WarehouseBrowser._prefecture_code(extracted), "22")

    def test_c1_existing_warehouse_updates_wrong_prefecture(self):
        events = []

        class FakeLocator:
            def __init__(self, selector):
                self.selector = selector

            def fill(self, value):
                events.append(("fill", self.selector, value))

            def input_value(self):
                return "00"

            def select_option(self, value):
                events.append(("select", self.selector, value))

        class FakePage:
            def locator(self, selector):
                return FakeLocator(selector)

        browser = C1WarehouseBrowser()
        browser.page = FakePage()
        browser._click_and_settle = lambda locator: events.append(
            ("click", locator.selector)
        )
        browser._accept_any_c1_html_dialog_if_visible = lambda page: events.append(
            ("transition_dialog",)
        )
        browser._accept_c1_html_dialog = lambda page, message: events.append(
            ("dialog", message)
        )

        self.assertTrue(browser._update_existing_prefecture("EH212", self.extracted))
        self.assertIn(("transition_dialog",), events)
        self.assertIn(("fill", "#txstr_suk_cd", "EH212"), events)
        self.assertIn(("select", "#ch_tdfk_cd", "10"), events)
        self.assertIn(("dialog", "よろしいですか？"), events)

    def test_validation_rejects_missing_or_invalid_prefecture(self):
        valid = {
            "seiban": "V3579",
            "pdf_hansu": 0,
            "otkmkmnoki": "20261023",
            "otkmkmcost": 3_216_000,
            **self.customer,
        }
        missing = dict(valid)
        missing.pop("demand_customer_prefecture")
        with self.assertRaisesRegex(ValueError, "demand_customer_prefecture"):
            validate_extracted(missing)

        invalid = {**valid, "demand_customer_prefecture": "海外"}
        with self.assertRaisesRegex(ValueError, "47都道府県"):
            validate_extracted(invalid)

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
        self.assertIn("--hidden-import win32clipboard", build_script)
        self.assertIn("--hidden-import win32timezone", build_script)
        self.assertIn("--hidden-import pywintypes", build_script)
        self.assertIn("CreateShortcut", build_script)
        self.assertIn('Arguments = "--retry-errors"', build_script)
        self.assertIn('Arguments = "--c1-only"', build_script)

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

    def test_c1_warehouse_is_not_ready_while_submit_shim_is_visible(self):
        class FakeLocator:
            def __init__(self, count):
                self._count = count

            def count(self):
                return self._count

        class FakePage:
            def locator(self, selector):
                if selector in {"#mode_new", "#txstr_suk_cd"}:
                    return FakeLocator(1)
                if selector == "#shim:visible, .ui-widget-overlay:visible":
                    return FakeLocator(1)
                return FakeLocator(0)

        browser = C1WarehouseBrowser()
        self.assertFalse(browser._warehouse_available(FakePage()))

    def test_c1_stale_submit_shim_reloads_registration_page(self):
        calls = []

        class FakeLocator:
            def __init__(self, selector):
                self.selector = selector

            def count(self):
                return 1

            def wait_for(self, **kwargs):
                calls.append(("wait_for", self.selector, kwargs))
                raise TimeoutError("shim remained visible")

        class FakePage:
            def locator(self, selector):
                return FakeLocator(selector)

            def reload(self, **kwargs):
                calls.append(("reload", kwargs))

            def wait_for_timeout(self, timeout):
                calls.append(("timeout", timeout))

        browser = C1WarehouseBrowser()
        browser._recover_stale_submit_shim(FakePage())
        self.assertTrue(any(call[0] == "reload" for call in calls))

    def test_c1_html_dialog_is_accepted_before_continuing(self):
        calls = []

        class FakeButton:
            def click(self, **kwargs):
                calls.append(("click", kwargs))

        class FakeDialog:
            @property
            def first(self):
                return self

            def filter(self, **kwargs):
                calls.append(("filter", kwargs))
                return self

            def wait_for(self, **kwargs):
                calls.append(("wait_for", kwargs))

            def get_by_role(self, *args, **kwargs):
                calls.append(("get_by_role", args, kwargs))
                return FakeButton()

        class FakePage:
            def locator(self, selector):
                calls.append(("locator", selector))
                return FakeDialog()

            def wait_for_load_state(self, *args, **kwargs):
                calls.append(("load", args, kwargs))

            def wait_for_timeout(self, timeout):
                calls.append(("timeout", timeout))

        browser = C1WarehouseBrowser()
        browser._accept_c1_html_dialog(
            FakePage(), "新規登録を行います。よろしいですか？"
        )

        waits = [call[1]["state"] for call in calls if call[0] == "wait_for"]
        self.assertEqual(waits, ["visible", "hidden"])
        self.assertTrue(any(call[0] == "click" for call in calls))
        self.assertIn(
            (
                "filter",
                {"has_text": "新規登録を行います。よろしいですか？"},
            ),
            calls,
        )

    def test_c1_any_visible_dialog_is_accepted_and_message_is_returned(self):
        calls = []

        class FakeContent:
            def inner_text(self):
                return "変更モードへ切り替えます。よろしいですか？"

        class FakeButton:
            def count(self):
                return 1

            def click(self, **kwargs):
                calls.append(("click", kwargs))

        class FakeDialog:
            @property
            def first(self):
                return self

            def wait_for(self, **kwargs):
                calls.append(("wait_for", kwargs))

            def locator(self, selector):
                self.selector = selector
                return FakeContent()

            def get_by_role(self, *args, **kwargs):
                return FakeButton()

        class FakePage:
            def locator(self, selector):
                self.selector = selector
                return FakeDialog()

            def wait_for_timeout(self, timeout):
                calls.append(("timeout", timeout))

        message = C1WarehouseBrowser()._accept_any_c1_html_dialog_if_visible(
            FakePage()
        )
        self.assertEqual(message, "変更モードへ切り替えます。よろしいですか？")
        self.assertTrue(any(call[0] == "click" for call in calls))
        waits = [call[1]["state"] for call in calls if call[0] == "wait_for"]
        self.assertEqual(waits, ["visible", "hidden"])

    def test_c1_code_search_uses_copied_list_without_inquiry_mode(self):
        browser = C1WarehouseBrowser()
        records = [
            {
                "warehouse_code": "EG746",
                "name": "別需要家",
                "display_name": "V0000 別需要家",
            }
        ]
        browser._warehouse_records_from_lookup = lambda search_value="": records
        browser._warehouse_code_exists = lambda code: False
        code, already_registered = browser._find_code(self.extracted)
        self.assertEqual(code, "EG747")
        self.assertFalse(already_registered)

    def test_c1_code_search_returns_matching_existing_record(self):
        browser = C1WarehouseBrowser()
        browser._warehouse_records_from_lookup = lambda search_value="": [
            {
                "warehouse_code": "EH212",
                "name": "旧需要家名称",
                "display_name": "V3579 省略された旧表示",
            }
        ]
        code, already_registered = browser._find_code(self.extracted)
        self.assertEqual(code, "EH212")
        self.assertTrue(already_registered)

    def test_c1_existing_seiban_returns_already_registered_without_update(self):
        browser = C1WarehouseBrowser()
        browser.page = object()
        browser.open_warehouse = lambda: None
        browser._warehouse_records_from_lookup = lambda search_value="": [
            {"warehouse_code": "EH212", "name": "旧名称", "display_name": "V3579旧表記"}
        ]

        def unexpected_update(*args):
            self.fail("登録済み倉庫は更新しないこと")

        browser._update_existing_prefecture = unexpected_update
        result = browser.register(self.extracted)
        self.assertEqual(result["status"], "already_registered")
        self.assertEqual(result["warehouse_code"], "EH212")
        self.assertTrue(result["registered"])

    def test_c1_code_candidates_roll_over_from_eg999_to_eh001(self):
        browser = C1WarehouseBrowser()
        browser.code_start = 999
        candidates = browser._candidate_codes()
        self.assertEqual(next(candidates), "EG999")
        self.assertEqual(next(candidates), "EH001")
        self.assertEqual(next(candidates), "EH002")

    def test_c1_clipboard_tsv_is_parsed_by_header_name(self):
        text = (
            "倉庫コード\t倉庫名称\t倉庫表示名称\t郵便番号\t住所\n"
            "EG999\t既存倉庫\t既存表示\t1000001\t東京都\n"
            "EH001\t新倉庫\t新表示\t1000002\t大阪府\n"
        )
        records = C1WarehouseBrowser._parse_warehouse_clipboard(text)
        self.assertEqual(
            records[1],
            {
                "warehouse_code": "EH001",
                "name": "新倉庫",
                "display_name": "新表示",
            },
        )

    def test_c1_code_search_uses_first_gap_after_prefix_rollover(self):
        browser = C1WarehouseBrowser()
        browser.code_start = 999
        records = [
            {"warehouse_code": "EG999", "name": "A", "display_name": "A"},
            {"warehouse_code": "EH001", "name": "B", "display_name": "B"},
            {"warehouse_code": "EH003", "name": "C", "display_name": "C"},
        ]
        browser._warehouse_records_from_lookup = lambda search_value="": [
            record
            for record in records
            if not search_value or record["warehouse_code"].startswith(search_value)
        ]
        browser._warehouse_code_exists = lambda code: False
        code, already_registered = browser._find_code(self.extracted)
        self.assertEqual(code, "EH002")
        self.assertFalse(already_registered)

    def test_c1_code_search_selects_eh212_for_supplied_range(self):
        browser = C1WarehouseBrowser()
        records = [
            {"warehouse_code": f"EG{number:03d}", "name": "A", "display_name": "A"}
            for number in range(746, 1000)
        ]
        records.extend(
            {
                "warehouse_code": f"EH{number:03d}",
                "name": "A",
                "display_name": "A",
            }
            for number in range(1, 212)
        )
        browser._warehouse_records_from_lookup = lambda search_value="": [
            record
            for record in records
            if not search_value or record["warehouse_code"].startswith(search_value)
        ]
        browser._warehouse_code_exists = lambda code: False
        code, already_registered = browser._find_code(self.extracted)
        self.assertEqual(code, "EH212")
        self.assertFalse(already_registered)

    def test_c1_candidate_is_rejected_when_direct_lookup_finds_it(self):
        browser = C1WarehouseBrowser()
        browser._warehouse_records_from_lookup = lambda search_value="": []
        browser._warehouse_code_exists = lambda code: code == "EG746"
        with self.assertRaisesRegex(RuntimeError, "EG746.*登録済み"):
            browser._find_code(self.extracted)

    def test_c1_list_result_count_is_parsed(self):
        self.assertEqual(
            C1WarehouseBrowser._list_result_count("7,897件中　1 - 15 件を表示"),
            7897,
        )

    def test_c1_lookup_is_fixed_to_configured_location_org(self):
        calls = []

        class FakeLocator:
            def __init__(self, selector):
                self.selector = selector

            def select_option(self, value):
                calls.append(("select", self.selector, value))

            def fill(self, value):
                calls.append(("fill", self.selector, value))

        class FakePage:
            def locator(self, selector):
                return FakeLocator(selector)

        browser = C1WarehouseBrowser()
        browser.location_org = "361000"
        browser._configure_warehouse_lookup(FakePage(), "EG")

        self.assertIn(("select", "#ch_jgs", "361000"), calls)
        self.assertIn(("select", "#cmb0115", "1"), calls)
        self.assertIn(("fill", "#tx_kensakuti", "EG"), calls)

    def test_customer_address_length_is_checked_for_all_c1_address_fields(self):
        with self.assertRaises(ValueError):
            validate_extracted(
                {
                    "seiban": "V3579",
                    "pdf_hansu": 0,
                    "otkmkmnoki": "20261023",
                    "otkmkmcost": 3_216_000,
                    **{**self.customer, "demand_customer_address": "あ" * 153},
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
