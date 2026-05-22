"""
Brevo drip automation — Supabase backend, Brevo sending.
Supports: send days, multiple sending accounts, per-domain limits,
          global unsubscribe, step priority, daily limits.
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


def _headers(key):
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=representation"}

def sget(base, key, table, qs=""):
    r = requests.get(f"{base}/rest/v1/{table}{qs}", headers=_headers(key), timeout=30)
    r.raise_for_status(); return r.json()

def spost(base, key, table, payload):
    r = requests.post(f"{base}/rest/v1/{table}", json=payload, headers=_headers(key), timeout=30)
    r.raise_for_status(); result = r.json(); return result[0] if isinstance(result, list) else result

def spatch(base, key, table, qs, payload):
    r = requests.patch(f"{base}/rest/v1/{table}{qs}", json=payload, headers=_headers(key), timeout=30)
    r.raise_for_status()

def parse_val(v):
    if isinstance(v, str):
        s = v.strip()
        if s.startswith('"') and s.endswith('"'):
            try: return json.loads(s)
            except: pass
    return v


def send_email(api_key, to_email, to_name, from_email, from_name, subject, body, tags, site_url=""):
    name = to_name or to_email.split("@")[0]
    text = body.replace("{{name}}", name).replace("{{email}}", to_email)
    unsub_url = f"{(site_url or 'https://aaronjp722.github.io/blu-balences').rstrip('/')}/unsubscribe.html?email={to_email}"
    footer = f"\n\n---\nTo unsubscribe: {unsub_url}\nBlu Balence | 6545 Market Avenue N. STE 100, North Canton, OH 44721"
    html = (("<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto'>"
             + text.replace("\n", "<br>")
             + "<br><br><hr><p style='font-size:11px;color:#9ca3af'>"
             + footer.replace("\n", "<br>")
             + "</p></body></html>"))
    payload = {
        "sender": {"email": from_email, "name": from_name or from_email},
        "to": [{"email": to_email, "name": to_name or to_email}],
        "subject": subject, "htmlContent": html,
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

    BASE = os.environ.get("SUPABASE_URL", "").rstrip("/")
    KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not BASE or not KEY: log.error("Set SUPABASE_URL and SUPABASE_SERVICE_KEY"); return 1

    def get(t, qs=""): return sget(BASE, KEY, t, qs)
    def post(t, p):    return spost(BASE, KEY, t, p)
    def patch(t, qs, p): spatch(BASE, KEY, t, qs, p)

    raw = get("settings", "?select=key,value")
    cfg = {r["key"]: parse_val(r["value"]) for r in raw}

    # ── Check send days ──
    send_days = [d.strip().lower() for d in (cfg.get("send_days") or "mon,tue,wed,thu,fri").split(",") if d.strip()]
    today_abbr = dt.datetime.now(dt.timezone.utc).strftime("%a").lower()
    if send_days and today_abbr not in send_days:
        log.info("Today (%s) not in send_days (%s). Exiting.", today_abbr, ",".join(send_days))
        return 0

    # ── Default sending credentials ──
    brevo_api_key = cfg.get("brevo_api_key", "")
    from_email    = cfg.get("from_email", "")
    from_name     = cfg.get("from_name", "")
    site_url      = cfg.get("site_url", "https://aaronjp722.github.io/blu-balences")
    if not brevo_api_key: log.error("brevo_api_key not set"); return 1
    if not from_email: log.error("from_email not set"); return 1
    m = re.match(r'^(.+?)\s*<(.+?)>\s*$', from_email)
    if m: from_name = from_name or m.group(1).strip(); from_email = m.group(2).strip()

    global_epm   = float(cfg.get("emails_per_minute", "2"))
    global_interval = 60.0 / global_epm
    daily_limit  = int(cfg.get("daily_limit", "50"))

    # ── Load sending accounts ──
    accs_raw = get("sending_accounts", "?active=eq.true&select=*")
    accounts = {a["id"]: a for a in (accs_raw or [])}

    # ── Load domain limits ──
    dlims_raw = get("domain_limits", "?active=eq.true&select=domain,daily_limit")
    domain_limits = {r["domain"].lower(): int(r["daily_limit"]) for r in (dlims_raw or [])}

    # ── Load unsubscribes ──
    try:
        unsub_rows = get("unsubscribes", "?select=email")
        unsubscribed = {r["email"].strip().lower() for r in unsub_rows}
    except:
        unsubscribed = set()
    log.info("Unsubscribed: %d | Domain limits: %d | Accounts: %d", len(unsubscribed), len(domain_limits), len(accounts))

    # ── Count sent today ──
    today_start = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_logs  = get("send_log", f"?sent_at=gte.{today_start}&select=id,sending_account_id,enrollments(email)")
    sent_today  = len(today_logs)

    # Per-account counts
    acc_sent = {}
    domain_sent = {}
    for entry in today_logs:
        aid = entry.get("sending_account_id")
        if aid: acc_sent[aid] = acc_sent.get(aid, 0) + 1
        email = (entry.get("enrollments") or {}).get("email", "")
        if email and "@" in email:
            dom = email.split("@")[1].lower()
            domain_sent[dom] = domain_sent.get(dom, 0) + 1

    log.info("Sent today: %d / %d", sent_today, daily_limit)
    if sent_today >= daily_limit:
        log.info("Daily limit reached. Exiting."); return 0

    # ── Process sequences ──
    sequences = get("sequences", "?active=eq.true&select=id,name,step_priority,sending_account_id")
    log.info("%d active sequence(s)", len(sequences))

    for seq in sequences:
        if sent_today >= daily_limit: log.info("Daily limit hit. Stopping."); break

        steps = get("steps", f"?sequence_id=eq.{seq['id']}&order=step_number.asc")
        total_steps = len(steps)
        if not total_steps: continue

        # Resolve sending account
        acc = accounts.get(seq.get("sending_account_id"))
        api_key_use  = acc["brevo_api_key"] if acc else brevo_api_key
        from_email_u = acc["from_email"]    if acc else from_email
        from_name_u  = (acc.get("from_name") or "") if acc else from_name
        acc_daily    = int(acc["daily_limit"]) if acc else daily_limit
        acc_id       = acc["id"] if acc else None

        if acc_id and acc_sent.get(acc_id, 0) >= acc_daily:
            log.info("Sequence '%s': account '%s' daily limit reached. Skipping.", seq["name"], acc["name"]); continue

        priority = seq.get("step_priority") or "later_first"
        steps_ordered = sorted(steps, key=lambda s: s["step_number"], reverse=(priority=="later_first"))
        log.info("Sequence '%s' — %d steps — %s", seq["name"], total_steps, priority)

        for step in steps_ordered:
            if sent_today >= daily_limit: break
            if acc_id and acc_sent.get(acc_id, 0) >= acc_daily: break

            snum = step["step_number"]
            delay_mins = step["delay_days"]*1440 + step["delay_hours"]*60 + step["delay_minutes"]
            send_interval = (step.get("batch_interval_minutes") or 0)*60 or global_interval
            batch_limit = int(step.get("batch_limit") or 0)
            step_body = step.get("body") or ""
            step_tags = [t.strip() for t in (step.get("tags") or "").split(",") if t.strip()]

            if snum == 1:
                due = get("enrollments", f"?sequence_id=eq.{seq['id']}&current_step=eq.0&status=eq.active&select=id,email,name")
            else:
                cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=delay_mins)).isoformat()
                due = get("enrollments", f"?sequence_id=eq.{seq['id']}&current_step=eq.{snum-1}&status=eq.active&last_sent_at=lte.{cutoff}&select=id,email,name")

            if not due: continue

            # Filter unsubscribed
            due = [e for e in due if e["email"].strip().lower() not in unsubscribed]
            if not due: continue

            # Filter already sent this step
            already = {r["enrollment_id"] for r in get("send_log", f"?step_id=eq.{step['id']}&select=enrollment_id")}
            due = [e for e in due if e["id"] not in already]
            if not due: continue

            # Cap to limits
            remaining = daily_limit - sent_today
            if acc_id: remaining = min(remaining, acc_daily - acc_sent.get(acc_id, 0))
            if batch_limit > 0: remaining = min(remaining, batch_limit)
            if len(due) > remaining: due = due[:remaining]

            log.info("  Step %d: %d due", snum, len(due))
            is_last = snum >= total_steps

            for i, enr in enumerate(due):
                if sent_today >= daily_limit: break
                if acc_id and acc_sent.get(acc_id, 0) >= acc_daily: break

                # Check domain limit
                dom = enr["email"].split("@")[1].lower() if "@" in enr["email"] else ""
                if dom and dom in domain_limits and domain_sent.get(dom, 0) >= domain_limits[dom]:
                    log.info("  Domain limit @%s reached. Skipping %s.", dom, enr["email"]); continue

                try:
                    msg_id = send_email(api_key_use, enr["email"], enr.get("name") or "",
                                        from_email_u, from_name_u, step["subject"], step_body, step_tags, site_url)
                    sent_today += 1
                    if acc_id: acc_sent[acc_id] = acc_sent.get(acc_id, 0) + 1
                    if dom: domain_sent[dom] = domain_sent.get(dom, 0) + 1

                    now = dt.datetime.now(dt.timezone.utc).isoformat()
                    post("send_log", {"enrollment_id": enr["id"], "step_id": step["id"],
                                      "step_number": snum, "brevo_message_id": msg_id,
                                      "status": "sent", "sent_at": now,
                                      "sending_account_id": acc_id})
                    patch("enrollments", f"?id=eq.{enr['id']}", {
                        "current_step": snum, "last_sent_at": now,
                        "status": "completed" if is_last else "active"})
                    log.info("  [%d/%d] → %s [today:%d/%d]", i+1, len(due), enr["email"], sent_today, daily_limit)
                except Exception:
                    log.exception("  Failed for %s", enr["email"])

                if i < len(due) - 1: time.sleep(send_interval)

    log.info("Done. Sent this run: %d", sent_today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
