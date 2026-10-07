from __future__ import annotations

import json
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NotifyConfig:
    smtp_host: str
    smtp_port: int
    smtp_auth_enabled: bool
    smtp_user: str
    smtp_password: str
    smtp_use_tls: bool
    smtp_use_ssl: bool
    from_addr: str
    subject_prefix: str
    recipients: tuple[str, ...]


def load_notify_config(path: Path) -> NotifyConfig | None:
    if not path.exists():
        return None
    root = json.loads(path.read_text(encoding="utf-8-sig"))
    value = root.get("notify", root)
    recipients = tuple(
        str(item.get("email", "")).strip()
        for item in value.get("recipients", [])
        if item.get("enabled", False) and str(item.get("email", "")).strip()
    )
    host = str(value.get("smtp_host", "")).strip()
    from_addr = str(value.get("from_addr", "")).strip()
    if not host or not from_addr or not recipients:
        return None
    return NotifyConfig(
        smtp_host=host,
        smtp_port=int(value.get("smtp_port", 25)),
        smtp_auth_enabled=bool(value.get("smtp_auth_enabled", False)),
        smtp_user=str(value.get("smtp_user", "")),
        smtp_password=str(value.get("smtp_password", "")),
        smtp_use_tls=bool(value.get("smtp_use_tls", False)),
        smtp_use_ssl=bool(value.get("smtp_use_ssl", False)),
        from_addr=from_addr,
        subject_prefix=str(value.get("subject_prefix", "【RTCM更新結果通知】")),
        recipients=recipients,
    )


def build_notification(status: str, source: Path, result: dict[str, Any] | None, error: str = "") -> tuple[str, str]:
    success = status == "success"
    skipped = status == "skipped"
    status_label = "成功" if success else "スキップ" if skipped else "失敗"
    subject = f"{status_label}: {source.name}"
    lines = [
        f"処理結果: {status_label}",
        f"対象ファイル: {source}",
    ]
    if success and result:
        extracted = result.get("extracted", {})
        database = result.get("database", {})
        c1_warehouse = result.get("c1_warehouse", {})
        lines.extend(
            [
                f"製番: {extracted.get('seiban', '')}",
                f"PDF版数: {extracted.get('pdf_hansu', '')}",
                f"落付見込納期: {extracted.get('otkmkmnoki', '')}",
                f"落付見込原価: {extracted.get('otkmkmcost', '')}",
                f"RTCM登録版数: {database.get('hansu', '')}",
                f"RECNO: {database.get('recno', '')}",
                f"落付見込販価: {database.get('otkmkmhnkg', '')}",
                f"落付見込益金: {database.get('otkmkmeki', '')}",
                f"落付見込益率: {database.get('otkmkmekirt', '')}",
                f"需要家コード: {extracted.get('demand_customer_code', '')}",
                f"需要家名称: {extracted.get('demand_customer_name', '')}",
                f"C1倉庫登録結果: {c1_warehouse.get('status', '')}",
                f"C1倉庫コード: {c1_warehouse.get('warehouse_code', '')}",
                f"PDF保管先: {result.get('archived_pdf', result.get('pdf', ''))}",
            ]
        )
        if result.get("archive_error"):
            lines.append(f"PDF保管エラー: {result['archive_error']}")
    elif skipped and result:
        extracted = result.get("extracted", {})
        database = result.get("database", {})
        lines.extend(
            [
                f"製番: {extracted.get('seiban', '')}",
                f"PDF版数: {extracted.get('pdf_hansu', '')}",
                f"スキップ理由: {database.get('skip_reason', 'RTCM登録対象外です。')}",
                "RTCMへの登録は行っていません。",
                f"PDF保管先: {result.get('archived_pdf', result.get('pdf', ''))}",
            ]
        )
        if result.get("archive_error"):
            lines.append(f"PDF保管エラー: {result['archive_error']}")
    elif error:
        lines.extend(["", "エラー内容:", error])
        if "T_JUCHUZAN_CTLに対象製番がありません" in error:
            lines.append("C1に受注情報、製番が登録されていることを確認してください。")
            lines.append(
                "C1に当日登録済みの場合は、夜間バッチでC1からRTCMへ連携されるため、"
                "翌日の自動再実行で正常終了する見込みです。"
            )
    return subject, "\n".join(lines)


def send_notification(config: NotifyConfig, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"] = config.from_addr
    message["To"] = ", ".join(config.recipients)
    message["Subject"] = f"{config.subject_prefix}{subject}"
    message.set_content(body)

    smtp_class = smtplib.SMTP_SSL if config.smtp_use_ssl else smtplib.SMTP
    kwargs = {"context": ssl.create_default_context()} if config.smtp_use_ssl else {}
    with smtp_class(config.smtp_host, config.smtp_port, timeout=30, **kwargs) as smtp:
        if config.smtp_use_tls and not config.smtp_use_ssl:
            smtp.starttls(context=ssl.create_default_context())
        if config.smtp_auth_enabled:
            smtp.login(config.smtp_user, config.smtp_password)
        smtp.send_message(message)
