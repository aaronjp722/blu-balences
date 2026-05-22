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


def _supa_headers(key):
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json", "Prefer": "return=representation"}

def supa_get(base, key, table, qs=""):
    r = requests.get(f"{base}/rest/v1/{table}{qs}", headers=_supa_headers(key), timeout=30)
    r.raise_for_status(); return r.json()

def supa_post(base, key, table, payload):
    r = requests.post(f"{base}/rest/v1/{table}", json=payload, headers=_supa_headers(key), timeout=30)
    r.raise_for_status(); result = r.json()
    return result[0] if isinstance(result, list) else result

def supa_patch(base, key, table, qs, payload):
    r = requests.patch(f"{base}/rest/v1/{table}{qs}", json=payload, headers=_supa_headers(key), timeout=30)
    r.raise_for_status()

def parse_setting(v):
    if isinstance(v, str):
        s = v.strip()
        if s.startswith('"') and s.endswith('"'):
            try: return json.loads(s)
            except json.JSONDecodeError: pass
    return v

def send_email(api_key, to_email, to_name, from_email, from_name, subject, body, tags):
    display_name = to_name or to_email.split("@")[0]
    personalized = body.replace("{{name}}", display_name).replace("{{email}}", to_email)
    if "<" not in personalized:
        html_body = "<html><body>" + personalized.replace("\n", "<br>") + "</body></html>"
    else:
        html_body = personalized
    payload = {
        "sender": {"email": from_email, "name": from_name or from_email},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject, "htmlContent": html_body,
    }
    if tags: payload["tags"] = tags
    r = requests.post("https://api.brevo.com/v3/smtp/email", json=payload,
                      headers={"api-key": api_key, "Content-Type": "application/json"}, timeout=30)
    if not r.ok: log.error("Brevo %d: %s", r.status_code, r.text)
    r.raise_for_status()
    return r.json().get("messageId", "")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    SUPA_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
    SUPA_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not SUPA_URL or not SUPA_KEY:
        log.error("Set SUPABASE_URL and SUPABASE_SERVICE_KEY"); return 1

    def get(table, qs=""): return supa_get(SUPA_URL, SUPA_KEY, table, qs)
    def post(table, payload): return supa_post(SUPA_URL, SUPA_KEY, table, payload)
    def patch(table, qs, payload): return supa_patch(SUPA_URL, SUPA_KEY, table, qs, payload)

    raw_cfg = get("settings", "?select=key,value")
    cfg = {r["key"]: parse_setting(r["value"]) for r in raw_cfg}

    # Check send days
    send_days = [d.strip().lower() for d in (cfg.get("send_days") or "mon,tue,wed,thu,fri").split(",") if d.strip()]
    today_abbr = dt.datetime.now(dt.timezone.utc).strftime("%a").lower()
    if send_days and today_abbr not in send_days:
        log.info("Today (%s) not in send_days %s — exiting.", today_abbr, send_days); return 0

    brevo_api_key = cfg.get("brevo_api_key", "")
    if not brevo_api_key: log.error("brevo_api_key not set"); return 1
    log.info("Brevo key: length=%d prefix=%s", len(brevo_api_key), brevo_api_key[:12])

    from_email = cfg.get("from_email", "")
    from_name  = cfg.get("from_name", "")
    if not from_email: log.error("from_email not set"); return 1
    m = re.match(r'^(.+?)\s*<(.+?)>\s*$', from_email)
    if m: from_name = from_name or m.group(1).strip(); from_email = m.group(2).strip()
    log.info("Default sender: %s <%s>", from_name, from_email)

    global_emails_per_minute = float(cfg.get("emails_per_minute", "2"))
    global_send_interval     = 60.0 / global_emails_per_minute
    global_daily_limit       = int(cfg.get("daily_limit", "300") or "300")
    site_url                 = (cfg.get("site_url") or "").rstrip("/")

    # Sending accounts
    accs_raw = get("sending_accounts", "?active=eq.true&select=*")
    accounts = {a["id"]: a for a in (accs_raw or [])}
    log.info("%d sending account(s)", len(accounts))

    # Domain limits
    dl_raw = get("domain_limits", "?active=eq.true&select=domain,daily_limit")
    domain_limits = {r["domain"].lower(): int(r["daily_limit"]) for r in (dl_raw or [])}
    if domain_limits: log.info("Domain limits: %s", domain_limits)

    # Unsubscribes
    unsub_raw = get("unsubscribes", "?select=email")
    unsubscribed = {r["email"].lower() for r in (unsub_raw or [])}
    log.info("%d unsubscribed", len(unsubscribed))

    # Today's send counts
    today_start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    todays_logs = get("send_log", f"?sent_at=gte.{today_start}&select=sending_account_id,enrollments(email)")
    account_sends = {}  # acc_id -> count
    domain_sends  = {}  # domain  -> count
    for row in (todays_logs or []):
        aid = row.get("sending_account_id")
        if aid: account_sends[aid] = account_sends.get(aid, 0) + 1
        email = (row.get("enrollments") or {}).get("email", "")
        if email:
            dom = email.lower().split("@")[-1]
            domain_sends[dom] = domain_sends.get(dom, 0) + 1

    sequences = get("sequences", "?active=eq.true&select=id,name,step_priority,sending_account_id,list_id")
    log.info("%d active sequence(s)", len(sequences))

    for seq in sequences:
        steps = get("steps", f"?sequence_id=eq.{seq['id']}&order=step_number.asc")
        total_steps = len(steps)
        if not total_steps:
            log.info("  Sequence '%s': no steps", seq["name"]); continue

        step_priority = seq.get("step_priority") or "later_first"
        log.info("Sequence '%s' — %d steps (priority: %s)", seq["name"], total_steps, step_priority)

        # Resolve sending account
        acc = accounts.get(seq.get("sending_account_id"))
        if acc:
            api_key_use   = acc["brevo_api_key"]
            from_email_use = acc["from_email"]
            from_name_use  = acc.get("from_name") or from_name
            acc_daily      = int(acc.get("daily_limit") or global_daily_limit)
            acc_id_use     = acc["id"]
            log.info("  Inbox: %s <%s> (limit %d/day)", from_name_use, from_email_use, acc_daily)
        else:
            api_key_use    = brevo_api_key
            from_email_use = from_email
            from_name_use  = from_name
            acc_daily      = global_daily_limit
            acc_id_use     = None

        # Order steps by priority
        if step_priority == "later_first":
            steps_ordered = sorted(steps, key=lambda s: s["step_number"], reverse=True)
        else:
            steps_ordered = sorted(steps, key=lambda s: s["step_number"])

        is_last = {s["step_number"]: s["step_number"] >= total_steps for s in steps}
        seq_sent = 0

        for step in steps_ordered:
            snum = step["step_number"]
            delay_mins = step["delay_days"] * 1440 + step["delay_hours"] * 60 + step["delay_minutes"]
            batch_limit = int(step.get("batch_limit") or 0)
            batch_interval_minutes = int(step.get("batch_interval_minutes") or 0)
            send_interval = (batch_interval_minutes * 60) if batch_interval_minutes > 0 else global_send_interval

            if snum == 1:
                due = get("enrollments",
                    f"?sequence_id=eq.{seq['id']}&current_step=eq.0&status=eq.active&select=id,email,name")
            else:
                cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=delay_mins)).isoformat()
                due = get("enrollments",
                    f"?sequence_id=eq.{seq['id']}&current_step=eq.{snum-1}"
                    f"&status=eq.active&last_sent_at=lte.{cutoff}&select=id,email,name")

            if not due: log.info("  Step %d: nobody due", snum); continue

            # Filter unsubscribed
            before = len(due)
            due = [e for e in due if e["email"].lower() not in unsubscribed]
            if len(due) < before:
                log.info("  Step %d: skipped %d unsubscribed", snum, before - len(due))

            # Filter already-sent this step
            already_sent_ids = {r["enrollment_id"] for r in get("send_log", f"?step_id=eq.{step['id']}&select=enrollment_id")}
            if already_sent_ids:
                before2 = len(due)
                due = [e for e in due if e["id"] not in already_sent_ids]
                if len(due) < before2:
                    log.info("  Step %d: skipped %d already-sent", snum, before2 - len(due))

            if not due: continue

            if batch_limit > 0 and len(due) > batch_limit:
                log.info("  Step %d: capped to batch limit %d", snum, batch_limit)
                due = due[:batch_limit]
            else:
                log.info("  Step %d: %d due (delay %d min)", snum, len(due), delay_mins)

            step_body = step.get("body") or ""
            step_tags = [t.strip() for t in (step.get("tags") or "").split(",") if t.strip()]

            # Unsubscribe footer
            if site_url:
                unsub_url = f"{site_url}/unsubscribe.html?email={{{{email}}}}"
                footer_html = (f'<p style="font-size:12px;color:#999;margin-top:32px;border-top:1px solid #eee;'
                               f'padding-top:12px">Don\'t want these? '
                               f'<a href="{unsub_url}" style="color:#999">Unsubscribe</a></p>')
                footer_text = f"\n\n---\nUnsubscribe: {unsub_url}"
                if "<" in step_body:
                    step_body = re.sub(r'</body>', footer_html + '</body>', step_body, flags=re.IGNORECASE) \
                                if "</body>" in step_body.lower() else step_body + footer_html
                else:
                    step_body += footer_text

            for i, enr in enumerate(due):
                # Account daily limit check
                sends_so_far = account_sends.get(acc_id_use, 0) if acc_id_use else sum(account_sends.values())
                if sends_so_far >= acc_daily:
                    log.info("  Daily limit (%d) reached — stopping", acc_daily); break

                # Domain limit check
                dom = enr["email"].lower().split("@")[-1]
                if dom in domain_limits and domain_sends.get(dom, 0) >= domain_limits[dom]:
                    log.info("  Domain @%s limit reached, skipping %s", dom, enr["email"]); continue

                try:
                    body_for_send = step_body.replace("{{email}}", enr["email"])
                    msg_id = send_email(
                        api_key=api_key_use, to_email=enr["email"],
                        to_name=enr.get("name") or "", from_email=from_email_use,
                        from_name=from_name_use, subject=step["subject"],
                        body=body_for_send, tags=step_tags,
                    )
                    seq_sent += 1
                    log.info("  [%d] Step %d → %s (msgId: %s)", seq_sent, snum, enr["email"], msg_id)
                    now = dt.datetime.now(dt.timezone.utc).isoformat()
                    post("send_log", {
                        "enrollment_id": enr["id"], "step_id": step["id"],
                        "step_number": snum, "brevo_message_id": msg_id,
                        "sending_account_id": acc_id_use, "status": "sent",
                    })
                    patch("enrollments", f"?id=eq.{enr['id']}", {
                        "current_step": snum, "last_sent_at": now,
                        "status": "completed" if is_last.get(snum) else "active",
                    })
                    if acc_id_use: account_sends[acc_id_use] = account_sends.get(acc_id_use, 0) + 1
                    domain_sends[dom] = domain_sends.get(dom, 0) + 1

                except Exception:
                    log.exception("  Failed for %s step %d", enr["email"], snum)

                if i < len(due) - 1:
                    log.info("  Waiting %.0f s…", send_interval)
                    time.sleep(send_interval)

        log.info("  Sequence '%s': sent %d total", seq["name"], seq_sent)

    return 0


if __name__ == "__main__":
    sys.exit(main())
