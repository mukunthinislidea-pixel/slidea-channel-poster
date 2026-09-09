#!/usr/bin/env python3
"""
Email side of the Slidea channel poster.

Two emails only, both to MAIL_TO_DEFAULT (below), matching the standing
orders in HANDOVER.md section 9:

  --weekly   Monday 09:15 IST. The last 7 days of state/posts.csv as a
             plain table. Sent even if the week was empty.
  --alert    Runs after every scheduled autopost run. Sends only when
             something is actually wrong, with a 24h cooldown per distinct
             problem so a broken run can't flood the inbox. Sends one
             "working again" email when a problem clears.

Manual (workflow_dispatch) runs should not trigger --alert - gate that in
the workflow YAML, not here, so `report.py --alert --dry-run` can still be
run by hand for testing.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

MAIL_TO_DEFAULT = "admin@slideegg.com"  # hardcoded on purpose - see HANDOVER.md: the
                                         # recipient should be visible in source, not
                                         # hidden behind a secret.
MAIL_FROM = os.environ.get("MAIL_FROM", "")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
MAIL_APP_PASSWORD = os.environ.get("MAIL_APP_PASSWORD", "")  # unused Gmail SMTP fallback

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
POSTS_CSV_PATH = STATE_DIR / "posts.csv"
LAST_RUN_PATH = STATE_DIR / "last_run.json"
HEALTH_PATH = STATE_DIR / "health.json"
ALERT_PATH = STATE_DIR / "alert.json"

ALERT_COOLDOWN_HOURS = 24
STALE_RUN_HOURS = 6


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


def send_email(subject, html_body, dry_run=False):
    if dry_run:
        print(f"[DRY RUN] would email {MAIL_TO_DEFAULT}\nSubject: {subject}\n\n{html_body}\n")
        return True
    if not BREVO_API_KEY or not MAIL_FROM:
        print("BREVO_API_KEY / MAIL_FROM missing - cannot send email.", file=sys.stderr)
        return False
    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
        json={
            "sender": {"email": MAIL_FROM},
            "to": [{"email": MAIL_TO_DEFAULT}],
            "subject": subject,
            "htmlContent": html_body,
        },
        timeout=20,
    )
    if resp.status_code >= 300:
        print(f"Brevo send failed: {resp.status_code} {resp.text[:300]}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------
# Weekly list
# --------------------------------------------------------------------------

def weekly_rows(days=7):
    if not POSTS_CSV_PATH.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    with open(POSTS_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                d = datetime.strptime(row["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if d >= cutoff:
                rows.append(row)
    return rows


def render_weekly_html(rows):
    if not rows:
        body = "<p>No posts went out this week.</p>"
    else:
        trs = "".join(
            f"<tr><td>{r['date']}</td><td>{r['time_ist']}</td><td>{r['type']}</td>"
            f"<td><a href=\"{r['url']}\">{r['title']}</a></td></tr>"
            for r in rows
        )
        body = (
            "<table cellpadding='6' style='border-collapse:collapse;width:100%'>"
            "<tr style='text-align:left;border-bottom:1px solid #ccc'>"
            "<th>Date</th><th>Time (IST)</th><th>Type</th><th>Title</th></tr>"
            f"{trs}</table>"
        )
    return f"<h2>Slidea Channel — this week's posts</h2>{body}"


def run_weekly(dry_run):
    rows = weekly_rows()
    html = render_weekly_html(rows)
    subject = f"Slidea Channel — {len(rows)} post(s) this week"
    send_email(subject, html, dry_run=dry_run)


# --------------------------------------------------------------------------
# Alert
# --------------------------------------------------------------------------

def health_note():
    """Return a problem string, or None if everything looks fine."""
    last_run = load_json(LAST_RUN_PATH, None)
    if last_run is None:
        return "last_run.json is missing entirely"

    if last_run.get("problem"):
        return last_run["problem"]

    try:
        at = datetime.fromisoformat(last_run["at"])
        age_hours = (datetime.now(timezone.utc) - at).total_seconds() / 3600
        if age_hours > STALE_RUN_HOURS:
            return f"the workflow has not run for over {int(age_hours)} hours"
    except (KeyError, ValueError):
        pass

    return None


def run_alert(dry_run):
    problem = health_note()
    alert_state = load_json(ALERT_PATH, {"last_problem": None, "last_sent_at": None})

    if problem is None:
        if alert_state.get("last_problem"):
            send_email("Slidea Channel — posting is working again",
                        "<p>The previous issue has cleared. Posting is back to normal.</p>",
                        dry_run=dry_run)
        if not dry_run:
            save_json(ALERT_PATH, {"last_problem": None, "last_sent_at": None})
        return

    same_problem = problem == alert_state.get("last_problem")
    if same_problem and alert_state.get("last_sent_at"):
        last_sent = datetime.fromisoformat(alert_state["last_sent_at"])
        if (datetime.now(timezone.utc) - last_sent) < timedelta(hours=ALERT_COOLDOWN_HOURS):
            print(f"Alert suppressed (cooldown): {problem}")
            return

    sent = send_email("Slidea Channel — posting needs attention",
                       f"<p>{problem}</p><p>Check state/last_run.json in the repo for details.</p>",
                       dry_run=dry_run)
    if sent and not dry_run:
        save_json(ALERT_PATH, {"last_problem": problem, "last_sent_at": datetime.now(timezone.utc).isoformat()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument("--alert", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.weekly:
        run_weekly(args.dry_run)
    if args.alert:
        run_alert(args.dry_run)
    if not args.weekly and not args.alert:
        parser.print_help()


if __name__ == "__main__":
    main()
