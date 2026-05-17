import os
import socket
import smtplib
import logging
import time
from datetime import datetime, timezone

import dns.resolver
import schedule
from supabase import create_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

SUPABASE_URL   = os.environ["SUPABASE_URL"]
SUPABASE_KEY   = os.environ["SUPABASE_KEY"]
TABLE          = os.getenv("SUPABASE_TABLE", "clean_leads")
INTERVAL_HOURS = float(os.getenv("INTERVAL_HOURS", "24"))
SMTP_TIMEOUT   = int(os.getenv("SMTP_TIMEOUT", "10"))
BATCH_SIZE     = int(os.getenv("BATCH_SIZE", "50"))

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def get_mx_host(domain: str) -> str | None:
    try:
        records = dns.resolver.resolve(domain, "MX")
        return str(sorted(records, key=lambda r: r.preference)[0].exchange).rstrip(".")
    except Exception:
        return None


def smtp_verify(email: str) -> bool:
    domain = email.split("@")[-1]
    mx = get_mx_host(domain)
    if not mx:
        log.debug(f"No MX record for {domain}")
        return False
    try:
        with smtplib.SMTP(mx, 25, timeout=SMTP_TIMEOUT) as smtp:
            smtp.ehlo("check.local")
            smtp.mail("verify@check.local")
            code, _ = smtp.rcpt(email)
            return code == 250
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            socket.timeout, OSError) as e:
        log.debug(f"SMTP error for {email}: {e}")
        return False
    except smtplib.SMTPResponseException:
        return False


def fetch_batch(offset: int) -> list[dict]:
    resp = (
        supabase.table(TABLE)
        .select("id, email")
        .eq("smtp_checked", False)
        .range(offset, offset + BATCH_SIZE - 1)
        .execute()
    )
    return resp.data or []


def update_row(row_id, valid: bool) -> None:
    supabase.table(TABLE).update({
        "smtp_valid":      valid,
        "smtp_checked":    True,
        "smtp_checked_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row_id).execute()


def run_check() -> None:
    log.info("Starting SMTP check run...")
    total = checked = valid = failed = 0
    offset = 0

    while True:
        rows = fetch_batch(offset)
        if not rows:
            break

        log.info(f"Processing batch of {len(rows)} (offset {offset})")
        for row in rows:
            email = (row.get("email") or "").strip().lower()
            if not email:
                update_row(row["id"], False)
                failed += 1
                continue

            result = smtp_verify(email)
            update_row(row["id"], result)
            checked += 1
            total += 1
            if result:
                valid += 1
            else:
                failed += 1

        offset += BATCH_SIZE
        if len(rows) < BATCH_SIZE:
            break

        log.info("Batch done - sleeping 2s before next batch")
        time.sleep(2)

    log.info(
        f"Run complete - checked: {checked}, valid: {valid}, "
        f"failed: {failed}, total processed: {total}"
    )


if __name__ == "__main__":
    log.info(f"SMTP checker starting - interval: {INTERVAL_HOURS}h, "
             f"batch size: {BATCH_SIZE}, timeout: {SMTP_TIMEOUT}s")
    run_check()
    schedule.every(INTERVAL_HOURS).hours.do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(60)
