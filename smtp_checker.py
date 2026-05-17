import os
import logging
import time
from datetime import datetime, timezone

import requests
import schedule
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)

SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]
TABLE           = os.getenv("SUPABASE_TABLE", "clean_leads")
INTERVAL_HOURS  = float(os.getenv("INTERVAL_HOURS", "24"))
BATCH_SIZE      = int(os.getenv("BATCH_SIZE", "50"))
VERIFY_BACKEND  = os.getenv("VERIFY_BACKEND", "https://backend-production-d43c8.up.railway.app")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


def verify_email(email):
    try:
        r = requests.post(
            f"{VERIFY_BACKEND}/verify",
            json={"email": email},
            timeout=15,
        )
        if r.ok:
            data = r.json()
            return data.get("valid", False), data.get("reason", "ok")
        return False, f"http_{r.status_code}"
    except requests.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def verify_batch(emails):
    """Try batch endpoint first, fall back to individual calls."""
    try:
        r = requests.post(
            f"{VERIFY_BACKEND}/verify-batch",
            json={"emails": emails},
            timeout=30,
        )
        if r.ok:
            return r.json().get("results", [])
    except Exception:
        pass
    # Fall back to individual
    return [{"email": e, **dict(zip(["valid","reason"], verify_email(e)))} for e in emails]


def fetch_batch(offset):
    resp = (
        supabase.table(TABLE)
        .select("id, email")
        .eq("smtp_checked", "false")
        .range(offset, offset + BATCH_SIZE - 1)
        .execute()
    )
    return resp.data or []


def update_row(row_id, valid):
    supabase.table(TABLE).update({
        "smtp_valid":      valid,
        "smtp_checked":    True,
        "smtp_checked_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row_id).execute()


def run_check():
    log.info("Starting SMTP check run...")
    total = checked = valid = failed = 0
    reason_counts = {}
    offset = 0

    while True:
        rows = fetch_batch(offset)
        if not rows:
            break

        emails = [r["email"] for r in rows if r.get("email")]
        log.info(f"Verifying batch of {len(rows)} (offset {offset})")

        results = verify_batch(emails)
        result_map = {r["email"]: r for r in results}

        for row in rows:
            email = (row.get("email") or "").strip().lower()
            if not email:
                update_row(row["id"], False)
                failed += 1
                continue
            res    = result_map.get(email, {})
            is_valid = res.get("valid", False)
            reason   = res.get("reason", "unknown")
            update_row(row["id"], is_valid)
            checked += 1; total += 1
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if is_valid: valid += 1
            else: failed += 1

        offset += BATCH_SIZE
        if len(rows) < BATCH_SIZE:
            break
        log.info("Batch done — sleeping 2s")
        time.sleep(2)

    log.info(f"Run complete — checked: {checked}, valid: {valid}, failed: {failed}")
    log.info(f"Reasons: {reason_counts}")


if __name__ == "__main__":
    log.info(f"SMTP checker starting — backend: {VERIFY_BACKEND}")
    run_check()
    schedule.every(INTERVAL_HOURS).hours.do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(60)
