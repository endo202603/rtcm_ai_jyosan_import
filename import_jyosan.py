from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any


def application_dir() -> Path:
    """通常実行時はソース、EXE時はEXE本体のある外部設定フォルダを返す。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_config_env(path: Path) -> None:
    """config.envを外部ライブラリなしで読み込み、未設定の環境変数へ反映する。"""
    if not path.is_file():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"config.envの{line_number}行目に '=' がありません")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"config.envの{line_number}行目の変数名が不正です: {name!r}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(name, value)


load_config_env(application_dir() / "config.env")


MAX_COST = 999_999_999_999_999
JST = timezone(timedelta(hours=9), "JST")
JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "seiban": {"type": "string", "minLength": 1, "maxLength": 25},
        "pdf_hansu": {"type": "integer", "minimum": 0, "maximum": 999},
        "otkmkmnoki": {"type": "string", "pattern": "^[0-9]{8}$"},
        "otkmkmcost": {"type": "integer", "minimum": 0, "maximum": MAX_COST},
        "demand_customer_code": {"type": "string", "pattern": "^[0-9]{9}$"},
        "demand_customer_name": {"type": "string", "minLength": 1, "maxLength": 40},
        "demand_customer_name_kana": {"type": "string", "minLength": 1, "maxLength": 40},
        "demand_customer_postal_code": {"type": "string", "pattern": "^[0-9]{7}$"},
        "demand_customer_address": {"type": "string", "minLength": 1, "maxLength": 32},
        "demand_customer_phone": {"type": "string", "minLength": 1, "maxLength": 16},
    },
    "required": [
        "seiban",
        "pdf_hansu",
        "otkmkmnoki",
        "otkmkmcost",
        "demand_customer_code",
        "demand_customer_name",
        "demand_customer_name_kana",
        "demand_customer_postal_code",
        "demand_customer_address",
        "demand_customer_phone",
    ],
}


@dataclass(frozen=True)
class ExtractedData:
    seiban: str
    pdf_hansu: int
    otkmkmnoki: str
    otkmkmcost: int
    demand_customer_code: str
    demand_customer_name: str
    demand_customer_name_kana: str
    demand_customer_postal_code: str
    demand_customer_address: str
    demand_customer_phone: str


@dataclass(frozen=True)
class RtcmConfig:
    kaicd: str
    kyotencd: str
    kojcd: str
    user_id: str
    pgid: str


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"環境変数 {name} が設定されていません")
    return value


def validate_extracted(value: dict[str, Any]) -> ExtractedData:
    expected_keys = {
        "seiban",
        "pdf_hansu",
        "otkmkmnoki",
        "otkmkmcost",
        "demand_customer_code",
        "demand_customer_name",
        "demand_customer_name_kana",
        "demand_customer_postal_code",
        "demand_customer_address",
        "demand_customer_phone",
    }
    if set(value) != expected_keys:
        raise ValueError(
            "生成AIのJSON項目が不正です。必要項目: " + ", ".join(sorted(expected_keys))
        )

    seiban = str(value["seiban"]).strip().upper()
    if not seiban or len(seiban) > 25 or not re.fullmatch(r"[A-Z0-9_-]+", seiban):
        raise ValueError(f"製番の形式が不正です: {seiban!r}")

    pdf_hansu = value["pdf_hansu"]
    if isinstance(pdf_hansu, bool) or not isinstance(pdf_hansu, int) or not (0 <= pdf_hansu <= 999):
        raise ValueError(f"PDF版数は0～999の整数で指定してください: {pdf_hansu!r}")

    noki = str(value["otkmkmnoki"]).strip()
    if not re.fullmatch(r"\d{8}", noki):
        raise ValueError(f"落付見込納期はYYYYMMDDの8桁で指定してください: {noki!r}")
    try:
        datetime.strptime(noki, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"落付見込納期が実在する日付ではありません: {noki}") from exc

    cost = value["otkmkmcost"]
    if isinstance(cost, bool) or not isinstance(cost, int) or not (0 <= cost <= MAX_COST):
        raise ValueError(f"落付見込原価は0以上15桁以内の整数で指定してください: {cost!r}")

    customer_code = str(value["demand_customer_code"]).strip()
    if not re.fullmatch(r"\d{9}", customer_code):
        raise ValueError(f"需要家コードは9桁の数字で指定してください: {customer_code!r}")

    customer_name = str(value["demand_customer_name"]).strip()
    customer_name_kana = str(value["demand_customer_name_kana"]).strip()
    postal_code = re.sub(r"[-ー－\s]", "", str(value["demand_customer_postal_code"]).strip())
    customer_address = str(value["demand_customer_address"]).strip()
    customer_phone = str(value["demand_customer_phone"]).strip()
    for label, text, limit in (
        ("需要家名称", customer_name, 40),
        ("需要家名称カナ", customer_name_kana, 40),
        ("需要家住所", customer_address, 32),
        ("需要家電話番号", customer_phone, 16),
    ):
        if not text or len(text) > limit:
            raise ValueError(f"{label}は1～{limit}文字で指定してください: {text!r}")
    if not re.fullmatch(r"\d{7}", postal_code):
        raise ValueError(f"需要家郵便番号は7桁の数字で指定してください: {postal_code!r}")
    if not re.fullmatch(r"[0-9()（）+\-ー－\s]+", customer_phone):
        raise ValueError(f"需要家電話番号の形式が不正です: {customer_phone!r}")

    return ExtractedData(
        seiban=seiban,
        pdf_hansu=pdf_hansu,
        otkmkmnoki=noki,
        otkmkmcost=cost,
        demand_customer_code=customer_code,
        demand_customer_name=customer_name,
        demand_customer_name_kana=customer_name_kana,
        demand_customer_postal_code=postal_code,
        demand_customer_address=customer_address,
        demand_customer_phone=customer_phone,
    )


def strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def extract_ai_text(response: dict[str, Any], api_style: str) -> str:
    if api_style == "chat_completions":
        return str(response["choices"][0]["message"]["content"])
    if response.get("output_text"):
        return str(response["output_text"])
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                return str(content["text"])
    raise ValueError("生成AIレスポンスからJSON本文を取得できませんでした")


def build_ai_request(pdf_path: Path, prompt: str, api_style: str, model: str) -> dict[str, Any]:
    pdf_data = base64.b64encode(pdf_path.read_bytes()).decode("ascii")
    file_data = f"data:application/pdf;base64,{pdf_data}"
    if api_style == "chat_completions":
        return {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "添付PDFを解析し、指定JSONだけを返してください。"},
                        {
                            "type": "file",
                            "file": {"filename": pdf_path.name, "file_data": file_data},
                        },
                    ],
                },
            ],
        }
    if api_style != "responses":
        raise ValueError("GENAI_API_STYLE は responses または chat_completions を指定してください")
    return {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": prompt}]},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "添付PDFを解析し、指定JSONだけを返してください。"},
                    {"type": "input_file", "filename": pdf_path.name, "file_data": file_data},
                ],
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "rtcm_jyosan_extract",
                "strict": True,
                "schema": JSON_SCHEMA,
            }
        },
    }


def call_internal_ai(pdf_path: Path, prompt_path: Path) -> ExtractedData:
    endpoint = required_env("GENAI_ENDPOINT")
    model = required_env("GENAI_MODEL")
    api_style = os.getenv("GENAI_API_STYLE", "responses").strip().lower()
    prompt = prompt_path.read_text(encoding="utf-8")
    payload = build_ai_request(pdf_path, prompt, api_style, model)

    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("GENAI_API_KEY", "").strip()
    if api_key:
        header_name = os.getenv("GENAI_AUTH_HEADER", "Authorization").strip()
        prefix = os.getenv("GENAI_AUTH_PREFIX", "Bearer").strip()
        headers[header_name] = f"{prefix} {api_key}".strip()

    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"生成AI APIエラー HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"生成AI APIへ接続できません: {exc.reason}") from exc

    raw_text = extract_ai_text(response_json, api_style)
    try:
        return validate_extracted(json.loads(strip_json_fence(raw_text)))
    except json.JSONDecodeError as exc:
        raise ValueError(f"生成AIの出力がJSONではありません: {raw_text[:500]}") from exc


def profit_rate(profit: Decimal, price: Decimal) -> Decimal:
    if price == 0:
        return Decimal("0.00")
    rate = (profit / price * Decimal(100)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    return max(Decimal("-999.99"), min(Decimal("999.99"), rate))


def audit_values(now: datetime, config: RtcmConfig) -> dict[str, Any]:
    return {
        "KAITEIDATE": now.strftime("%Y%m%d"),
        "KAITEUSRID": config.user_id,
        "CRTDATE": now.strftime("%Y%m%d"),
        "CRTTIME": now.strftime("%H%M%S"),
        "CRTPGID": config.pgid,
        "CRTUSRID": config.user_id,
        "UPDDATE": now.strftime("%Y%m%d"),
        "UPDTIME": now.strftime("%H%M%S"),
        "UPDPGID": config.pgid,
        "UPDUSRID": config.user_id,
    }


def build_row(
    latest: dict[str, Any] | None,
    extracted: ExtractedData,
    config: RtcmConfig,
    juchukg: Decimal,
    recno: int,
    now: datetime,
) -> dict[str, Any]:
    row = dict(latest or {})
    old_hansu = int(row["HANSU"]) if latest else -1
    new_hansu = old_hansu + 1
    if new_hansu > 999:
        raise ValueError("HANSUが上限999に達しているため、新しい版を登録できません")

    jkysn_cost = Decimal(row.get("JKYSNCOST") or 0)
    otkmkm_cost = Decimal(extracted.otkmkmcost)
    jkysn_profit = juchukg - jkysn_cost
    otkmkm_profit = juchukg - otkmkm_cost

    row.update(
        {
            "KAICD": config.kaicd,
            "KYOTENCD": config.kyotencd,
            "KOJCD": config.kojcd,
            "SEIBAN": extracted.seiban,
            "HANSU": new_hansu,
            "GETUDO": now.strftime("%Y%m"),
            "RECNO": recno,
            "JKYSNHANKG": juchukg,
            "JKYSNEKIKG": jkysn_profit,
            "JKYSNEKIRT": profit_rate(jkysn_profit, juchukg),
            "OTKMKMNOKI": extracted.otkmkmnoki,
            "OTKMKMHNKG": juchukg,
            "OTKMKMCOST": otkmkm_cost,
            "OTKMKMEKI": otkmkm_profit,
            "OTKMKMEKIRT": profit_rate(otkmkm_profit, juchukg),
        }
    )
    row.update(audit_values(now, config))
    return row


def fetch_one_dict(cursor: Any) -> dict[str, Any] | None:
    record = cursor.fetchone()
    if record is None:
        return None
    columns = [str(column[0]).upper() for column in cursor.description]
    return dict(zip(columns, record))


def insert_into_oracle(extracted: ExtractedData, config: RtcmConfig, dry_run: bool) -> dict[str, Any]:
    if extracted.pdf_hansu != 0:
        return {
            "seiban": extracted.seiban,
            "pdf_hansu": extracted.pdf_hansu,
            "skipped": True,
            "skip_reason": (
                f"PDFの版数が{extracted.pdf_hansu}版のため、"
                "初版（0版）以外はRTCM登録対象外です。"
            ),
            "committed": False,
        }

    if extracted.seiban.upper().startswith("U"):
        return {
            "seiban": extracted.seiban,
            "pdf_hansu": extracted.pdf_hansu,
            "skipped": True,
            "skip_reason": "U製番のためRTCM登録対象外です。",
            "committed": False,
        }

    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("oracledbが未導入です。requirements.txtをインストールしてください") from exc

    connection = oracledb.connect(
        user=required_env("ORACLE_USER"),
        password=required_env("ORACLE_PASSWORD"),
        dsn=required_env("ORACLE_DSN"),
    )
    try:
        cursor = connection.cursor()
        keys = {
            "kaicd": config.kaicd,
            "kyotencd": config.kyotencd,
            "kojcd": config.kojcd,
            "seiban": extracted.seiban,
        }

        # 受注行をロックして、同一製番の同時取込によるHANSU競合を防ぐ。
        cursor.execute(
            """
            SELECT JUCHUKG
              FROM T_JUCHUZAN_CTL
             WHERE KAICD = :kaicd
               AND KYOTENCD = :kyotencd
               AND KOJCD = :kojcd
               AND SEIBAN = :seiban
             FOR UPDATE
            """,
            keys,
        )
        order_row = cursor.fetchone()
        if order_row is None:
            raise ValueError(f"T_JUCHUZAN_CTLに対象製番がありません: {extracted.seiban}")
        juchukg = Decimal(order_row[0] or 0)

        cursor.execute(
            """
            SELECT MAX(HANSU)
              FROM T_JYOSAN
             WHERE KAICD = :kaicd
               AND KYOTENCD = :kyotencd
               AND KOJCD = :kojcd
               AND SEIBAN = :seiban
            """,
            keys,
        )
        max_hansu = cursor.fetchone()[0]
        latest = None
        if max_hansu is not None:
            cursor.execute(
                """
                SELECT *
                  FROM T_JYOSAN
                 WHERE KAICD = :kaicd
                   AND KYOTENCD = :kyotencd
                   AND KOJCD = :kojcd
                   AND SEIBAN = :seiban
                   AND HANSU = :hansu
                 FOR UPDATE
                """,
                {**keys, "hansu": max_hansu},
            )
            latest = fetch_one_dict(cursor)

        cursor.execute("SELECT SQ_T_JYOSAN01.NEXTVAL FROM DUAL")
        recno = int(cursor.fetchone()[0])
        now = datetime.now(JST)
        row = build_row(latest, extracted, config, juchukg, recno, now)

        # 初版は指定列以外をテーブル定義の既定値に委ねる。改訂版は最新行を全列引き継ぐ。
        columns = list(row)
        bind_names = [f"b{i}" for i in range(len(columns))]
        sql = (
            f"INSERT INTO T_JYOSAN ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + name for name in bind_names)})"
        )
        binds = {name: row[column] for name, column in zip(bind_names, columns)}
        cursor.execute(sql, binds)

        result = {
            "kaicd": config.kaicd,
            "kyotencd": config.kyotencd,
            "kojcd": config.kojcd,
            "seiban": extracted.seiban,
            "pdf_hansu": extracted.pdf_hansu,
            "hansu": row["HANSU"],
            "recno": recno,
            "otkmkmnoki": extracted.otkmkmnoki,
            "otkmkmhnkg": int(juchukg),
            "otkmkmcost": extracted.otkmkmcost,
            "otkmkmeki": int(row["OTKMKMEKI"]),
            "otkmkmekirt": str(row["OTKMKMEKIRT"]),
            "committed": not dry_run,
        }
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def parse_args() -> argparse.Namespace:
    base_dir = application_dir()
    parser = argparse.ArgumentParser(description="PDFを社内生成AIで解析し、T_JYOSANへ新版登録します")
    parser.add_argument("pdf", type=Path, nargs="?", help="入力PDF")
    parser.add_argument("--prompt", type=Path, default=base_dir / "prompt.txt")
    parser.add_argument("--json-file", type=Path, help="AIを呼ばず、このJSONを入力として使用")
    parser.add_argument("--expected-seiban", help="PDF取違い防止用の期待製番")
    parser.add_argument("--dry-run", action="store_true", help="INSERT後にROLLBACKして登録内容だけ表示")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.json_file:
        extracted = validate_extracted(json.loads(args.json_file.read_text(encoding="utf-8")))
    else:
        if args.pdf is None:
            raise ValueError("入力PDFまたは --json-file を指定してください")
        if not args.pdf.is_file():
            raise FileNotFoundError(f"PDFが見つかりません: {args.pdf}")
        extracted = call_internal_ai(args.pdf, args.prompt)

    if args.expected_seiban and extracted.seiban != args.expected_seiban.strip().upper():
        raise ValueError(
            f"抽出製番 {extracted.seiban} が期待製番 {args.expected_seiban.strip().upper()} と一致しません"
        )

    config = RtcmConfig(
        kaicd=os.getenv("RTCM_KAICD", "MMC").strip(),
        kyotencd=os.getenv("RTCM_KYOTENCD", "MMC").strip(),
        kojcd=os.getenv("RTCM_KOJCD", "MMC").strip(),
        user_id=required_env("RTCM_USER_ID"),
        pgid=required_env("RTCM_PGID"),
    )
    for name, value, limit in (
        ("RTCM_KAICD", config.kaicd, 12),
        ("RTCM_KYOTENCD", config.kyotencd, 12),
        ("RTCM_KOJCD", config.kojcd, 12),
        ("RTCM_USER_ID", config.user_id, 10),
        ("RTCM_PGID", config.pgid, 10),
    ):
        if not value or len(value) > limit:
            raise ValueError(f"{name} は1～{limit}文字で指定してください")

    print("AI抽出結果:", json.dumps(extracted.__dict__, ensure_ascii=False))
    result = insert_into_oracle(extracted, config, args.dry_run)
    print("DB処理結果:", json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
