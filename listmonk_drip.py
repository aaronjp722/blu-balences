"""
Brevo drip automation — reads config from Supabase, sends via Brevo.

New in this version:
  - Skips globally unsubscribed emails (unsubscribes table)
  - Skips enrollments with status='unsubscribed'
  - Step priority per sequence: later_first (default) or sequential
  - Daily send limit respected across all sequences
  - Unsubscribe footer link appended to every email automatically

Secrets required in GitHub Actions:
  SUPABASE_URL          — https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  — service role key (not anon)

Settings stored in Supabase settings table:
  brevo_api_key, from_email, from_name,
  emails_per_minute, daily_limit, site_url
"""

import datetime as dt
import json
import logging
import os
import re
import sys
import time

import requests

log = logging.getLogger("drip")


# ── Supabase helpers ──────────────────────────────────────────────────────────

def _supa_headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def supa_get(base: str, key: str, table: str, qs: str = "") -> list:
    r = requests.get(f"{base}/rest/v1/{table}{qs}", headers=_supa_headers(key), timeout=30)
    r.raise_for_status()
    return r.json()

def supa_post(base: str, key: str, table: str, payload: dict) -> dict:
    r = requests.post(f"{base}/rest/v1/{table}", json=payload, headers=_supa_headers(key), timeout=30)
    r.raise_for_status()
    result = r.json()
    return result[0] if isinstance(result, list) else result

def supa_patch(base: str, key: str, table: str, qs: str, payload: dict) -> None:
    r = requests.patch(f"{base}/rest/v1/{table}{qs}", json=payload, headers=_supa_headers(key), timeout=30)
    r.raise_for_status()


def parse_setting(v):
    if isinstance(v, str):
        stripped = v.strip()
        if stripped.startswith('"') and stripped.endswith('"'):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
    return v


# ── Email sending ─────────────────────────────────────────────────────────────

def build_unsubscribe_footer(site_url: str, to_email: str) -> str:
    base = site_url.rstrip("/") if site_url else "https://aaronjp722.github.io/blu-balences"
    link = f"{base}/unsubscribe.html?email={to_email}"
    return (
        "\n\n---\n"
        f"To unsubscribe from future emails, click here: {link}\n"
        "Blu Balence | 6545 Market Avenue N. STE 100, North Canton, OH 44721"
    )


def send_email(api_key: str, to_email: str, to_name: str,
               from_email: str, from_name: str,
               subject: str, body: str, tags: list,
               site_url: str = "") -> str:

    display_name = to_name or to_email.split("@")[0]
    personalized = (
        body
        .replace("{{name}}", display_name)
        .replace("{{email}}", to_email)
    )

    footer = build_unsubscribe_footer(site_url, to_email)

    if "<" in personalized and "<br" in personalized.lower():
        # HTML email — append footer as HTML
        footer_html = (
            "<br><br><hr style='border:none;border-top:1px solid #e5e7eb;margin:24px 0'>"
            f"<p style='font-size:12px;color:#6b7280'>To unsubscribe, "
            f"<a href='{build_unsubscribe_footer(site_url, to_email).split(\":\", 2)[-1].strip().split()[0]}' "
            f"style='color:#6b7280'>click here</a>.<br>"
            "Blu Balence | 6545 Market Avenue N. STE 100, North Canton, OH 44721</p>"
        )
        # Simpler: just inject the text footer converted to HTML
        html_body = personalized + "<br><br><hr><p style='font-size:11px;color:#9ca3af'>" + \
                    footer.replace("\n", "<br>").replace("---", "") + "</p>"
    else:
        # Plain text — convert to HTML with footer
        full_text = personalized + footer
        html_body = (
            "<html><body style='font-family:Arial,sans-serif;font-size:15px;color:#111;max-width:600px;margin:0 auto'>"
            + full_text.replace("\n", "<br>")
            + "</body></html>"
        )

    payload = {
        "sender": {"email": from_email, "name": from_name or from_email},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if tags:
        payload["tags"] = tags

    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        json=payload,
        headers={"api-key": api_key, "Content-Type": "application/json"},
        timeout=30,
    )
    if not r.ok:
        log.error("Brevo %d: %s", r.status_code, r.text)
    r.raise_for_status()
    return r.json().get("messageId", "")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    SUPA_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
    SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPA_URL or not SUPA_KEY:
        log.error("Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars")
        return 1

    def get(table, qs=""): return supa_get(SUPA_URL, SUPA_KEY, table, qs)
    def post(table, payload): return supa_post(SUPA_URL, SUPA_KEY, table, payload)
    def patch(table, qs, payload): return supa_patch(SUPA_URL, SUPA_KEY, table, qs, payload)

    # ── Load settings ──
    raw_cfg = get("settings", "?select=key,value")
    cfg = {r["key"]: parse_setting(r["value"]) for r in raw_cfg}

    brevo_api_key = cfg.get("brevo_api_key", "")
    if not brevo_api_key:
        log.error("brevo_api_key not set in settings table")
        return 1

    from_email = cfg.get("from_email", "")
    from_name  = cfg.get("from_name", "")
    if not from_email:
        log.error("from_email not set in settings table")
        return 1

    m = re.match(r'^(.+?)\s*<(.+?)>\s*$', from_email)
    if m:
        from_name  = from_name or m.group(1).strip()
        from_email = m.group(2).strip()

    site_url              = cfg.get("site_url", "https://aaronjp722.github.io/blu-balences")
    global_epm            = float(cfg.get("emails_per_minute", "2"))
    global_send_interval  = 60.0 / global_epm
    daily_limit           = int(cfg.get("daily_limit", "50"))

    log.info("From: %s <%s>", from_name, from_email)
    log.info("Rate: %.1f emails/min | Daily limit: %d", global_epm, daily_limit)

    # ── Count emails already sent today ──
    today_start = dt.datetime.now(dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    today_sent_rows = get("send_log", f"?sent_at=gte.{today_start}&select=id")
    sent_today = len(today_sent_rows)
    log.info("Already sent today: %d / %d", sent_today, daily_limit)

    if sent_today >= daily_limit:
        log.info("Daily limit reached (%d). Exiting.", daily_limit)
        return 0

    # ── Load globally unsubscribed emails ──
    try:
        unsub_rows = get("unsubscribes", "?select=email")
        global_unsubscribed = {r["email"].strip().lower() for r in unsub_rows}
        log.info("Global unsubscribe list: %d email(s)", len(global_unsubscribed))
    except Exception:
        log.warning("Could not load unsubscribes table — continuing without it")
        global_unsubscribed = set()

    # ── Process sequences ──
    sequences = get("sequences", "?active=eq.true&select=id,name,step_priority")
    log.info("%d active sequence(s)", len(sequences))

    for seq in sequences:
        if sent_today >= daily_limit:
            log.info("Daily limit reached mid-run. Stopping.")
            break

        steps = get("steps", f"?sequence_id=eq.{seq['id']}&order=step_number.asc")
        total_steps = len(steps)
        if not total_steps:
            log.info("Sequence '%s': no steps, skipping", seq["name"])
            continue

        priority = seq.get("step_priority") or "later_first"
        if priority == "later_first":
            steps_to_process = sorted(steps, key=lambda s: s["step_number"], reverse=True)
            log.info("Sequence '%s' — %d steps — priority: LATER FIRST", seq["name"], total_steps)
        else:
            steps_to_process = steps  # ascending: step 1 first
            log.info("Sequence '%s' — %d steps — priority: SEQUENTIAL", seq["name"], total_steps)

        for step in steps_to_process:
            if sent_today >= daily_limit:
                log.info("Daily limit reached. Stopping.")
                break

            snum = step["step_number"]
            delay_mins = (
                step["delay_days"] * 1440
                + step["delay_hours"] * 60
                + step["delay_minutes"]
            )

            batch_limit            = int(step.get("batch_limit") or 0)
            batch_interval_minutes = int(step.get("batch_interval_minutes") or 0)
            send_interval = (batch_interval_minutes * 60) if batch_interval_minutes > 0 else global_send_interval

            step_body = step.get("body") or ""
            step_tags = [t.strip() for t in (step.get("tags") or "").split(",") if t.strip()]

            # Find due enrollments
            if snum == 1:
                due = get("enrollments",
                    f"?sequence_id=eq.{seq['id']}&current_step=eq.0"
                    f"&status=eq.active&select=id,email,name")
            else:
                cutoff = (
                    dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=delay_mins)
                ).isoformat()
                due = get("enrollments",
                    f"?sequence_id=eq.{seq['id']}&current_step=eq.{snum - 1}"
                    f"&status=eq.active&last_sent_at=lte.{cutoff}&select=id,email,name")

            if not due:
                log.info("  Step %d: nobody due", snum)
                continue

            # Filter out globally unsubscribed contacts
            before = len(due)
            due = [e for e in due if e["email"].strip().lower() not in global_unsubscribed]
            skipped_unsub = before - len(due)
            if skipped_unsub:
                log.info("  Step %d: skipped %d globally unsubscribed", snum, skipped_unsub)

            if not due:
                log.info("  Step %d: all due are unsubscribed", snum)
                continue

            # Filter out already-sent this step
            already_sent = get("send_log", f"?step_id=eq.{step['id']}&select=enrollment_id")
            already_sent_ids = {r["enrollment_id"] for r in already_sent}
            if already_sent_ids:
                before2 = len(due)
                due = [e for e in due if e["id"] not in already_sent_ids]
                if before2 - len(due):
                    log.info("  Step %d: skipped %d already-sent", snum, before2 - len(due))

            if not due:
                log.info("  Step %d: all due already sent", snum)
                continue

            # Respect daily limit when capping batch
            remaining_today = daily_limit - sent_today
            effective_cap = remaining_today
            if batch_limit > 0:
                effective_cap = min(batch_limit, remaining_today)

            if len(due) > effective_cap:
                log.info("  Step %d: %d due, capped to %d", snum, len(due), effective_cap)
                due = due[:effective_cap]
            else:
                log.info("  Step %d: %d due (delay %d min)", snum, len(due), delay_mins)

            is_last = snum >= total_steps

            for i, enr in enumerate(due):
                if sent_today >= daily_limit:
                    log.info("  Daily limit hit mid-step. Stopping.")
                    break

                try:
                    msg_id = send_email(
                        api_key=brevo_api_key,
                        to_email=enr["email"],
                        to_name=enr.get("name") or "",
                        from_email=from_email,
                        from_name=from_name,
                        subject=step["subject"],
                        body=step_body,
                        tags=step_tags,
                        site_url=site_url,
                    )
                    sent_today += 1
                    log.info("  [%d/%d] Sent → %s (msgId: %s) [today: %d/%d]",
                             i + 1, len(due), enr["email"], msg_id, sent_today, daily_limit)

                    now = dt.datetime.now(dt.timezone.utc).isoformat()
                    post("send_log", {
                        "enrollment_id": enr["id"],
                        "step_id": step["id"],
                        "step_number": snum,
                        "brevo_message_id": msg_id,
                        "status": "sent",
                        "sent_at": now,
                    })
                    patch("enrollments", f"?id=eq.{enr['id']}", {
                        "current_step": snum,
                        "last_sent_at": now,
                        "status": "completed" if is_last else "active",
                    })

                except Exception:
                    log.exception("  [%d/%d] Failed for %s", i + 1, len(due), enr["email"])

                if i < len(due) - 1:
                    time.sleep(send_interval)

    log.info("Done. Total sent this run: %d", sent_today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
