"""
Listmonk drip automation — reads config from Supabase, fires campaigns, logs sends.

Setup:
  export SUPABASE_URL=https://xxxx.supabase.co
  export SUPABASE_SERVICE_KEY=your-service-role-key   # NOT the anon key
  python listmonk_drip.py

Cron (daily 9am):
  0 9 * * * cd /path/to/repo && python listmonk_drip.py >> drip.log 2>&1
"""

import datetime as dt
import logging
import os
import sys
import time

import requests

log = logging.getLogger("drip")


# ── Supabase REST helpers ────────────────────────────────────

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


# ── Listmonk API client ──────────────────────────────────────

class Listmonk:
    def __init__(self, url: str, username: str, password: str):
        self.base = url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (username, password)
        self.s.headers["Content-Type"] = "application/json"

    def _get(self, path: str, **params):
        r = self.s.get(f"{self.base}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()["data"]

    def _post(self, path: str, payload: dict):
        r = self.s.post(f"{self.base}{path}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()["data"]

    def _put(self, path: str, payload: dict):
        r = self.s.put(f"{self.base}{path}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json().get("data")

    def create_list(self, name: str) -> int:
        return self._post("/api/lists", {"name": name, "type": "private", "optin": "single"})["id"]

    def delete_list(self, list_id: int) -> None:
        self.s.delete(f"{self.base}/api/lists/{list_id}", timeout=30)

    def find_subscriber(self, email: str) -> int | None:
        data = self._get("/api/subscribers", query=f"subscribers.email = '{email}'")
        results = data.get("results", [])
        return results[0]["id"] if results else None

    def upsert_subscriber(self, email: str, name: str, list_id: int) -> int:
        sub_id = self.find_subscriber(email)
        if sub_id:
            self._put("/api/subscribers/lists", {
                "ids": [sub_id], "action": "add",
                "target_list_ids": [list_id], "status": "confirmed",
            })
        else:
            data = self._post("/api/subscribers", {
                "email": email, "name": name or email, "status": "enabled",
                "lists": [list_id], "preconfirm_subscriptions": True,
            })
            sub_id = data["id"]
        return sub_id

    def create_campaign(self, name: str, subject: str, from_email: str,
                        list_ids: list[int], template_id: int,
                        body: str = " ", tags: list[str] = None) -> int:
        data = self._post("/api/campaigns", {
            "name": name,
            "subject": subject,
            "from_email": from_email,
            "lists": list_ids,
            "template_id": template_id,
            "type": "regular",
            "content_type": "richtext",
            "messenger": "email",
            "body": body if body and body.strip() else " ",
            "tags": tags or [],
        })
        return data["id"]

    def start_campaign(self, campaign_id: int) -> None:
        self._put(f"/api/campaigns/{campaign_id}/status", {"status": "running"})


# ── Main ─────────────────────────────────────────────────────

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

    cfg = {r["key"]: r["value"] for r in get("settings", "?select=key,value")}
    lm = Listmonk(
        cfg.get("listmonk_url", "http://localhost:9000"),
        cfg.get("listmonk_username", "admin"),
        cfg.get("listmonk_password", ""),
    )
    global_emails_per_minute = float(cfg.get("emails_per_minute", "2"))
    global_send_interval = 60.0 / global_emails_per_minute

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
            snum = step["step_number"]
            delay_mins = (
                step["delay_days"] * 1440
                + step["delay_hours"] * 60
                + step["delay_minutes"]
            )

            batch_limit = int(step.get("batch_limit") or 0)
            batch_interval_minutes = int(step.get("batch_interval_minutes") or 0)
            send_interval = (batch_interval_minutes * 60) if batch_interval_minutes > 0 else global_send_interval

            step_body = step.get("body") or " "
            step_tags = [t.strip() for t in (step.get("tags") or "").split(",") if t.strip()]

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

            already_sent = get("send_log", f"?step_id=eq.{step['id']}&select=enrollment_id")
            already_sent_ids = {r["enrollment_id"] for r in already_sent}
            if already_sent_ids:
                before = len(due)
                due = [e for e in due if e["id"] not in already_sent_ids]
                skipped = before - len(due)
                if skipped:
                    log.info("  Step %d: skipped %d already-sent enrollment(s)", snum, skipped)

            if not due:
                log.info("  Step %d: all due already sent, skipping", snum)
                continue

            if batch_limit > 0 and len(due) > batch_limit:
                log.info("  Step %d: %d due, capped to batch limit of %d", snum, len(due), batch_limit)
                due = due[:batch_limit]
            else:
                log.info("  Step %d: %d due (delay was %d min)", snum, len(due), delay_mins)

            if batch_interval_minutes > 0:
                log.info("  Step %d: interval %d min between emails", snum, batch_interval_minutes)
            else:
                log.info("  Step %d: using global rate %.1f emails/min", snum, global_emails_per_minute)

            if step_tags:
                log.info("  Step %d: tags %s", snum, step_tags)

            list_name = f"_drip_{seq['name'][:24]}_s{snum}_{dt.date.today()}"
            temp_list_id = lm.create_list(list_name)

            try:
                for enr in due:
                    lm.upsert_subscriber(enr["email"], enr.get("name") or "", temp_list_id)
                    time.sleep(send_interval)

                campaign_id = lm.create_campaign(
                    name=f"{seq['name']} Step {snum} {dt.date.today()}",
                    subject=step["subject"],
                    from_email=step["from_email"],
                    list_ids=[temp_list_id],
                    template_id=step["template_id"],
                    body=step_body,
                    tags=step_tags,
                )
                lm.start_campaign(campaign_id)
                log.info("  Campaign #%d started (tags: %s)", campaign_id, step_tags or "none")

                now = dt.datetime.now(dt.timezone.utc).isoformat()
                is_last = snum >= total_steps

                for enr in due:
                    post("send_log", {
                        "enrollment_id": enr["id"],
                        "step_id": step["id"],
                        "step_number": snum,
                        "listmonk_campaign_id": campaign_id,
                        "status": "sent",
                    })
                    patch("enrollments", f"?id=eq.{enr['id']}", {
                        "current_step": snum,
                        "last_sent_at": now,
                        "status": "completed" if is_last else "active",
                    })

            except Exception:
                log.exception("  Step %d failed — removing temp list", snum)
                lm.delete_list(temp_list_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
