"""
Brevo drip automation — reads config from Supabase, sends via Brevo.

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


def _supa_headers(key):
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=representation"}

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
        s = v.strip()
        if s.startswith('"') and s.endswith('"'):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                pass
    return v


def personalize(body, to_email, to_name, company=""):
    display = to_name or to_email.split("@")[0]
    parts   = display.split()
    first   = parts[0]  if parts          else to_email.split("@")[0]
    last    = parts[-1] if len(parts) > 1 else ""
    now        = dt.datetime.now()
    today      = now.strftime("%B %d, %Y")
    this_month = now.strftime("%B")
    return (
        body
        .replace("{{ .Subscriber.FirstName }}", first)
        .replace("{{.Subscriber.FirstName}}",   first)
        .replace("{{ .Subscriber.LastName }}",  last)
        .replace("{{.Subscriber.LastName}}",    last)
        .replace("{{ .Subscriber.Name }}",      display)
        .replace("{{.Subscriber.Name}}",        display)
        .replace("{{ .Subscriber.Email }}",     to_email)
        .replace("{{.Subscriber.Email}}",       to_email)
        .replace("{{name}}",                    display)
        .replace("{{first_name}}",              first)
        .replace("{{last_name}}",               last)
        .replace("{{email}}",                   to_email)
        .replace("{{today}}",                   today)
        .replace("{{this_month}}",              this_month)
        .replace("{{company}}",                 company)
    )


def send_email(api_key, to_email, to_name, from_email, from_name, subject, body, tags, company="", in_reply_to=""):
    text = personalize(body, to_email, to_name or "", company=company)
    html = text if "<" in text else "<html><body>" + text.replace("\n", "<br>") + "</body></html>"
    payload = {
        "sender": {"email": from_email, "name": from_name or from_email},
        "to":     [{"email": to_email,  "name": to_name  or to_email}],
        "subject": subject,
        "htmlContent": html,
    }
    if tags:
        payload["tags"] = tags
    if in_reply_to:
        payload["headers"] = {"In-Reply-To": in_reply_to, "References": in_reply_to}
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


def load_seq_inboxes(get_fn, seq_id):
    """Return active inboxes assigned to this sequence, with their limit settings."""
    rows = get_fn("sequence_inboxes", f"?sequence_id=eq.{seq_id}&active=eq.true&select=*,inboxes(*)")
    result = []
    for r in rows:
        inbox = r.get("inboxes") or {}
        if not inbox.get("id") or inbox.get("active") is False:
            continue
        api_key = inbox.get("brevo_api_key") or ""
        if not api_key:
            continue
        result.append({
            "inbox_id":        inbox["id"],
            "name":            inbox.get("name", ""),
            "email":           inbox.get("email", ""),
            "api_key":         api_key,
            "daily_limit":     r.get("daily_limit") or 0,
            "use_inbox_limit": r.get("use_inbox_limit") or False,
        })
    return result


def pick_inbox(seq_inboxes, sends_today):
    """Pick the inbox with fewest sends today that hasn't hit its daily limit."""
    available = []
    for ib in seq_inboxes:
        count = sends_today.get(ib["inbox_id"], 0)
        if ib["use_inbox_limit"] and ib["daily_limit"] > 0 and count >= ib["daily_limit"]:
            continue
        available.append((count, ib))
    if not available:
        return None
    available.sort(key=lambda x: x[0])
    return available[0][1]


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])

    SUPA_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
    SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPA_URL or not SUPA_KEY:
        log.error("Set SUPABASE_URL and SUPABASE_SERVICE_KEY")
        return 1

    def get(table, qs=""):    return supa_get(SUPA_URL, SUPA_KEY, table, qs)
    def post(table, payload): return supa_post(SUPA_URL, SUPA_KEY, table, payload)
    def patch(table, qs, pl): return supa_patch(SUPA_URL, SUPA_KEY, table, qs, pl)

    cfg = {r["key"]: parse_setting(r["value"]) for r in get("settings", "?select=key,value")}

    brevo_api_key = cfg.get("brevo_api_key", "")
    if not brevo_api_key:
        log.error("brevo_api_key not set")
        return 1
    log.info("Brevo key: len=%d prefix=%s", len(brevo_api_key), brevo_api_key[:12])

    from_email = cfg.get("from_email", "")
    from_name  = cfg.get("from_name", "")
    if not from_email:
        log.error("from_email not set")
        return 1
    m = re.match(r'^(.+?)\s*<(.+?)>\s*$', from_email)
    if m:
        from_name  = from_name or m.group(1).strip()
        from_email = m.group(2).strip()
    log.info("From: %s <%s>", from_name, from_email)

    send_days = [d.strip() for d in cfg.get("send_days", "mon,tue,wed,thu,fri").split(",")]
    today_abbr = dt.datetime.now().strftime("%a").lower()
    if today_abbr not in send_days:
        log.info("Today (%s) not in send_days (%s) — skipping", today_abbr, send_days)
        return 0

    global_rate     = float(cfg.get("emails_per_minute", "2"))
    global_interval = 60.0 / global_rate

    sequences = get("sequences", "?active=eq.true&select=id,name,same_thread")
    log.info("%d active sequence(s)", len(sequences))

    for seq in sequences:
        steps = get("steps", f"?sequence_id=eq.{seq['id']}&order=step_number.asc")
        if not steps:
            log.info("  '%s' has no steps", seq["name"])
            continue
        total_steps = len(steps)
        log.info("Sequence '%s' — %d steps", seq["name"], total_steps)
        same_thread = seq.get("same_thread", False)

        # Load inboxes assigned to this sequence and seed today's send counts
                seq_inboxes = load_seq_inboxes(get, seq["id"])
        today_start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        sends_today = {}
        if seq_inboxes:
            try:
                sends_today = {
                    ib["inbox_id"]: len(get("send_log", f"?inbox_id=eq.{ib['inbox_id']}&sent_at=gte.{today_start}&select=id"))
                    for ib in seq_inboxes
                }
            except Exception:
                log.warning("  send_log missing inbox_id/sent_at columns — defaulting counts to 0")
                sends_today = {ib["inbox_id"]: 0 for ib in seq_inboxes}
            log.info("  %d inbox(es) assigned for rotation", len(seq_inboxes))

        for step in steps:
            snum        = step["step_number"]
            delay_mins  = step["delay_days"]*1440 + step["delay_hours"]*60 + step["delay_minutes"]
            batch_limit = int(step.get("batch_limit") or 0)
            batch_iv    = int(step.get("batch_interval_minutes") or 0)
            interval    = batch_iv * 60 if batch_iv > 0 else global_interval
            step_body   = step.get("body") or ""
            step_tags   = [t.strip() for t in (step.get("tags") or "").split(",") if t.strip()]

            if snum == 1:
                due = get("enrollments", f"?sequence_id=eq.{seq['id']}&current_step=eq.0&status=eq.active&select=id,email,name,company,thread_message_id,assigned_inbox_id")
            else:
                cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=delay_mins)).isoformat()
                due = get("enrollments", f"?sequence_id=eq.{seq['id']}&current_step=eq.{snum-1}&status=eq.active&last_sent_at=lte.{cutoff}&select=id,email,name,company,thread_message_id,assigned_inbox_id")

            if not due:
                log.info("  Step %d: nobody due", snum)
                continue

            sent_ids = {r["enrollment_id"] for r in get("send_log", f"?step_id=eq.{step['id']}&select=enrollment_id")}
            if sent_ids:
                before = len(due)
                due = [e for e in due if e["id"] not in sent_ids]
                if before - len(due):
                    log.info("  Step %d: skipped %d already-sent", snum, before - len(due))

            if not due:
                log.info("  Step %d: all done", snum)
                continue

            if batch_limit > 0 and len(due) > batch_limit:
                log.info("  Step %d: %d due → capped to %d", snum, len(due), batch_limit)
                due = due[:batch_limit]
            else:
                log.info("  Step %d: %d due", snum, len(due))

            is_last = snum >= total_steps

            for i, enr in enumerate(due):
                try:
                    # Resolve which inbox to use
                    if snum == 1 and seq_inboxes:
                        inbox_info = pick_inbox(seq_inboxes, sends_today)
                        if not inbox_info:
                            log.warning("  [%d/%d] All inboxes at daily limit — skipping %s", i+1, len(due), enr["email"])
                            continue
                        sends_today[inbox_info["inbox_id"]] = sends_today.get(inbox_info["inbox_id"], 0) + 1
                    elif snum > 1 and seq_inboxes and enr.get("assigned_inbox_id"):
                        inbox_info = next((ib for ib in seq_inboxes if ib["inbox_id"] == enr["assigned_inbox_id"]), None)
                    else:
                        inbox_info = None

                    use_api_key    = inbox_info["api_key"]  if inbox_info else brevo_api_key
                    use_from_email = inbox_info["email"]    if inbox_info else from_email
                    use_from_name  = inbox_info["name"]     if inbox_info else from_name
                    use_inbox_id   = inbox_info["inbox_id"] if inbox_info else None

                    in_reply_to = (enr.get("thread_message_id") or "") if (same_thread and snum > 1) else ""
                    msg_id = send_email(
                        api_key=use_api_key, to_email=enr["email"],
                        to_name=enr.get("name") or "", from_email=use_from_email,
                        from_name=use_from_name, subject=step["subject"],
                        body=step_body, tags=step_tags,
                        company=enr.get("company") or "",
                        in_reply_to=in_reply_to,
                    )
                    log.info("  [%d/%d] → %s via %s (msg: %s)", i+1, len(due), enr["email"], use_from_email, msg_id)
                    now = dt.datetime.now(dt.timezone.utc).isoformat()
                    post("send_log", {
                        "enrollment_id": enr["id"], "step_id": step["id"],
                        "step_number": snum, "brevo_message_id": msg_id,
                        "status": "sent", "inbox_id": use_inbox_id,
                    })
                    update_payload = {"current_step": snum, "last_sent_at": now, "status": "completed" if is_last else "active"}
                    if same_thread and snum == 1 and msg_id:
                        update_payload["thread_message_id"] = msg_id
                    if snum == 1 and use_inbox_id:
                        update_payload["assigned_inbox_id"] = use_inbox_id
                    patch("enrollments", f"?id=eq.{enr['id']}", update_payload)
                except Exception:
                    log.exception("  [%d/%d] failed for %s", i+1, len(due), enr["email"])

                if i < len(due) - 1:
                    log.info("  Waiting %.0fs…", interval)
                    time.sleep(interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
