"""
Brevo drip automation — reads config from Supabase, sends transactional emails via Brevo.

Setup:
  export SUPABASE_URL=https://xxxx.supabase.co
  export SUPABASE_SERVICE_KEY=your-service-role-key
  python listmonk_drip.py
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


def _supa_headers(key: str) -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def supa_get(base, key, table, qs=""):
    r = requests.get(f"{base}/rest/v1/{table}{qs}", headers=_supa_headers(key), timeout=30)
    r.raise_for_status()
    return r.json()

def supa_post(base, key, table, payload):
    r = requests.post(f"{base}/rest/v1/{table}", json=payload, headers=_supa_headers(key), timeout=30)
    r.raise_for_status()
    result = r.json()
    return result[0] if isinstance(result, list) else result

def supa_patch(base, key, table, qs, payload):
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


def personalize(body: str, to_email: str, to_name: str) -> str:
    display_name = to_name or to_email.split("@")[0]
    first_name   = display_name.split()[0] if display_name else to_email.split("@")[0]
    return (
        body
        .replace("{{ .Subscriber.FirstName }}", first_name)
        .replace("{{.Subscriber.FirstName}}",   first_name)
        .replace("{{ .Subscriber.Name }}",       display_name)
        .replace("{{.Subscriber.Name}}",         display_name)
        .replace("{{ .Subscriber.Email }}",      to_email)
        .replace("{{.Subscriber.Email}}",        to_email)
        .replace("{{name}}",                     display_name)
        .replace("{{first_name}}",               first_name)
        .replace("{{email}}",                    to_email)
    )


def send_email(api_key, to_email, to_name, from_email, from_name, subject, body, tags):
    text = personalize(body, to_email, to_name or "")
    html = text if "<" in text else "<html><body>" + text.replace("\n", "<br>") + "</body></html>"

    payload = {
        "sender": {"email": from_email, "name": from_name or from_email},
        "to":     [{"email": to_email,  "name": to_name  or to_email}],
        "subject": subject,
        "htmlContent": html,
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
        log.error("Brevo %d error: %s", r.status_code, r.text)
    r.raise_for_status()
    return r.json().get("messageId", "")


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

    def get(table, qs=""):    return supa_get(SUPA_URL, SUPA_KEY, table, qs)
    def post(table, payload): return supa_post(SUPA_URL, SUPA_KEY, table, payload)
    def patch(table, qs, pl): return supa_patch(SUPA_URL, SUPA_KEY, table, qs, pl)

    raw_cfg = get("settings", "?select=key,value")
    cfg = {r["key"]: parse_setting(r["value"]) for r in raw_cfg}

    brevo_api_key = cfg.get("brevo_api_key", "")
    if not brevo_api_key:
        log.error("brevo_api_key not set in settings table")
        return 1
    log.info("Brevo key loaded: length=%d, prefix=%s", len(brevo_api_key), brevo_api_key[:12])

    from_email = cfg.get("from_email", "")
    from_name  = cfg.get("from_name", "")
    if not from_email:
        log.error("from_email not set in settings table")
        return 1

    m = re.match(r'^(.+?)\s*<(.+?)>\s*$', from_email)
    if m:
        from_name  = from_name or m.group(1).strip()
        from_email = m.group(2).strip()
    log.info("Sending from: %s <%s>", from_name, from_email)

    global_emails_per_minute = float(cfg.get("emails_per_minute", "2"))
    global_send_interval     = 60.0 / global_emails_per_minute

    sequences = get("sequences", "?active=eq.true&select=id,name")
    log.info("%d active sequence(s)", len(sequences))

    for seq in sequences:
        steps = get("steps", f"?sequence_id=eq.{seq['id']}&order=step_number.asc")
        total_steps = len(steps)
        if not total_steps:
            log.info("  Sequence '%s' has no steps, skipping", seq["name"])
            continue

        log.info("Sequence '%s' — %d steps", seq["name"], total_steps)

        for step in steps:
            snum       = step["step_number"]
            delay_mins = step["delay_days"] * 1440 + step["delay_hours"] * 60 + step["delay_minutes"]

            batch_limit              = int(step.get("batch_limit") or 0)
            batch_interval_minutes   = int(step.get("batch_interval_minutes") or 0)
            send_interval            = (batch_interval_minutes * 60) if batch_interval_minutes > 0 else global_send_interval

            step_body = step.get("body") or ""
            step_tags = [t.strip() for t in (step.get("tags") or "").split(",") if t.strip()]

            if snum == 1:
                due = get("enrollments",
                    f"?sequence_id=eq.{seq['id']}&current_step=eq.0"
                    f"&status=eq.active&select=id,email,name")
            else:
                cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=delay_mins)).isoformat()
                due = get("enrollments",
                    f"?sequence_id=eq.{seq['id']}&current_step=eq.{snum - 1}"
                    f"&status=eq.active&last_sent_at=lte.{cutoff}&select=id,email,name")

            if not due:
                log.info("  Step %d: nobody due", snum)
                continue

            already_sent_ids = {
                r["enrollment_id"]
                for r in get("send_log", f"?step_id=eq.{step['id']}&select=enrollment_id")
            }
            if already_sent_ids:
                before = len(due)
                due = [e for e in due if e["id"] not in already_sent_ids]
                if before - len(due):
                    log.info("  Step %d: skipped %d already-sent", snum, before - len(due))

            if not due:
                log.info("  Step %d: all due already sent", snum)
                continue

            if batch_limit > 0 and len(due) > batch_limit:
                log.info("  Step %d: %d due, capped to %d", snum, len(due), batch_limit)
                due = due[:batch_limit]
            else:
                log.info("  Step %d: %d due (delay=%d min)", snum, len(due), delay_mins)

            is_last = snum >= total_steps

            for i, enr in enumerate(due):
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
                    )
                    log.info("  [%d/%d] Sent → %s (msgId: %s)", i + 1, len(due), enr["email"], msg_id)

                    now = dt.datetime.now(dt.timezone.utc).isoformat()
                    post("send_log", {
                        "enrollment_id":    enr["id"],
                        "step_id":          step["id"],
                        "step_number":      snum,
                        "brevo_message_id": msg_id,
                        "status":           "sent",
                    })
                    patch("enrollments", f"?id=eq.{enr['id']}", {
                        "current_step": snum,
                        "last_sent_at": now,
                        "status":       "completed" if is_last else "active",
                    })

                except Exception:
                    log.exception("  [%d/%d] Failed for %s", i + 1, len(due), enr["email"])

                if i < len(due) - 1:
                    log.info("  Waiting %.0f seconds…", send_interval)
                    time.sleep(send_interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
