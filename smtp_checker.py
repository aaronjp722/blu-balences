import os
import logging
import time
from datetime import datetime, timezone

import dns.resolver
import requests
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
BATCH_SIZE     = int(os.getenv("BATCH_SIZE", "50"))
REACHER_URL    = os.getenv("REACHER_URL", "https://backend-production-d43c8.up.railway.app")
REACHER_SECRET = os.getenv("RCH_HEADER_SECRET", "blubalences2024")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

REACHER_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": REACHER_SECRET,
}

REACHABLE = {"safe", "risky", "unknown"}

GOOGLE_DOMAINS = {"gmail.com", "googlemail.com"}


def get_mx(domain: str) -> str:
    try:
        records = dns.resolver.resolve(domain, "MX")
        return str(sorted(records, key=lambda r: r.preference)[0].exchange).rstrip(".").lower()
    except Exception:
        return ""


def is_google_hosted(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    if domain in GOOGLE_DOMAINS:
        return True
    mx = get_mx(domain)
    return "google.com" in mx or "googlemail.com" in mx


def verify_via_reacher(email: str) -> tuple[bool, str]:
    try:
        r = requests.post(
            f"{REACHER_URL}/v0/check_email",
            json={"to_email": email},
            headers=REACHER_HEADERS,
            timeout=30,
        )
        if r.ok:
            data = r.json()
            reachable = data.get("is_reachable", "unknown")
            return reachable in REACHABLE, reachable
        return False, f"http_{r.status_code}"
    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def verify_email(email: str) -> tuple[bool, str]:
    if is_google_hosted(email):
        return True, "google"
    return verify_via_reacher(email)


def fetch_batch(offset: int) -> list[dict]:
    resp = (
        supabase.table(TABLE)
        .select("id, email")
        .eq("smtp_checked", "false")
        .range(offset, offset + BATCH_SIZE - 1)
        .execute()
    )
    return resp.data or []


def update_row(row_id, valid: bool, reason: str = "") -> None:
    supabase.table(TABLE).update({
        "smtp_valid":      valid,
        "smtp_checked":    True,
        "smtp_checked_at": datetime.now(timezone.utc).isoformat(),
        "smtp_reason":     reason,
    }).eq("id", row_id).execute()


def run_check() -> None:
    log.info("Starting SMTP check run…")
    checked = valid_count = failed = google_count = 0
    offset = 0

    while True:
        rows = fetch_batch(offset)
        if not rows:
            break

        log.info(f"Processing batch of {len(rows)} (offset {offset})")
        for row in rows:
            email = (row.get("email") or "").strip().lower()
            if not email:
                update_row(row["id"], False, "empty")
                failed += 1
                continue

            valid, reason = verify_email(email)
            update_row(row["id"], valid, reason)
            checked += 1

            if reason == "google":
                google_count += 1
            if valid:
                valid_count += 1
            else:
                failed += 1

            log.debug(f"{'✓' if valid else '✗'} {email} ({reason})")

            if reason != "google":
                time.sleep(0.1)

        offset += BATCH_SIZE
        if len(rows) < BATCH_SIZE:
            break

        log.info("Batch done — sleeping 2s before next batch")
        time.sleep(2)

    log.info(
        f"Run complete — checked: {checked}, valid: {valid_count}, "
        f"failed: {failed}, google-verified: {google_count}"
    )


if __name__ == "__main__":
    log.info(f"SMTP checker starting — reacher: {REACHER_URL}, interval: {INTERVAL_HOURS}h")
    run_check()
    schedule.every(INTERVAL_HOURS).hours.do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(60)
