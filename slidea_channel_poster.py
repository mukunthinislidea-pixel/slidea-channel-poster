#!/usr/bin/env python3
"""
Slidea -> WhatsApp Channel auto-poster

Posts Slidea's interactive presentation templates to the Slidea WhatsApp
Channel, automatically, free (self-hosted WhatsApp Web via Baileys).

What the owner asked for (8 Sep 2026), and what this implements:

  * TEMPLATES ONLY. No blog posts, no other content.
  * 8 posts per day, every day.
  * New templates first. On a day with nothing new (or not enough new),
    fill the rest of the day's 8 from older templates that have never
    been posted - so the channel is never silent.
  * Check hourly.
  * Weekly report email; plus an alert email if posting breaks.
  * Captions are generated here (see TEMPLATE_CAPTION).

Sources (verified live against slidea.com on 8 Sep 2026):

  https://slidea.com/recent-templates/        page 1..SCAN_PAGES -> "new"
  https://slidea.com/recent-templates/page/N/ deeper pages       -> backfill

  Slidea publishes no dates for templates (not in meta tags, not in the
  sitemap, not over the REST API), so "new" means "not in state/seen.json".
  That is sound because the source page is explicitly newest-first.

Run modes:
    python slidea_channel_poster.py            # normal run
    python slidea_channel_poster.py --dry-run  # build everything, send nothing
"""

import base64
import csv
import json
import os
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

SITE = os.environ.get("SITE", "https://slidea.com")
RECENT_TEMPLATES_PATH = os.environ.get("RECENT_TEMPLATES_PATH", "/recent-templates/")

CHANNEL_INVITE = os.environ.get("WA_CHANNEL_INVITE", "0029VbBV0biDZ4LSqmhPiM2h")
SENDER = os.environ.get("SENDER", "baileys")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1" or "--dry-run" in sys.argv

DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "8"))      # posts per IST day
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "2"))        # 24 templates per page
NEW_PER_RUN = int(os.environ.get("NEW_PER_RUN", "8"))      # brand-new items may go out at once
BACKFILL_PER_RUN = int(os.environ.get("BACKFILL_PER_RUN", "1"))   # older ones trickle, 1 per hour
BACKFILL_MAX_PAGES = int(os.environ.get("BACKFILL_MAX_PAGES", "21"))  # /recent-templates/ has ~21

# Posting window in IST hours. 9-21 keeps posts inside waking hours - an
# hourly run then spreads the day's 8 across the day instead of dumping
# them at 3am. Set 0/24 for round the clock.
ACTIVE_FROM = int(os.environ.get("ACTIVE_FROM", "9"))
ACTIVE_TO = int(os.environ.get("ACTIVE_TO", "21"))

ALERT_AFTER_HOURS = int(os.environ.get("ALERT_AFTER_HOURS", "24"))
EXIT_ON_ERROR = os.environ.get("EXIT_ON_ERROR", "0") == "1"

IST = timezone(timedelta(hours=5, minutes=30))

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
SEEN_PATH = STATE_DIR / "seen.json"
DAILY_PATH = STATE_DIR / "daily.json"
POSTS_CSV_PATH = STATE_DIR / "posts.csv"
LAST_RUN_PATH = STATE_DIR / "last_run.json"
HEALTH_PATH = STATE_DIR / "health.json"

SEEN_CAP = 20000

USER_AGENT = "Mozilla/5.0 (compatible; SlideaChannelPoster/1.0)"
HTTP_TIMEOUT = 25

TEMPLATE_CAPTION = """✨ *New Template on Slidea*
*{title}*
{desc}
✅ Live polls, quizzes & word clouds
✅ Run it live with your audience
\U0001F449 Try it free:
{url}
#Slidea #InteractivePresentation #AudienceEngagement #Quiz"""

# Older templates are not "new", so they get their own opening line.
BACKFILL_CAPTION = """\U0001F4A1 *Template of the Day on Slidea*
*{title}*
{desc}
✅ Live polls, quizzes & word clouds
✅ Run it live with your audience
\U0001F449 Try it free:
{url}
#Slidea #InteractivePresentation #AudienceEngagement #Quiz"""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)


def today_str():
    return now_ist().strftime("%Y-%m-%d")


def http_get(url, **kwargs):
    headers = {"User-Agent": USER_AGENT}
    headers.update(kwargs.pop("headers", {}))
    return requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


def clean_text(s, limit=None):
    s = unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = unescape(re.sub(r"\s+", " ", s)).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def extract_meta(html, prop_or_name):
    for attr in ("property", "name"):
        m = re.search(
            rf'<meta[^>]+{attr}=["\']{re.escape(prop_or_name)}["\'][^>]+content=["\']([^"\']*)["\']',
            html, re.IGNORECASE)
        if m:
            return unescape(m.group(1))
        m = re.search(
            rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+{attr}=["\']{re.escape(prop_or_name)}["\']',
            html, re.IGNORECASE)
        if m:
            return unescape(m.group(1))
    return ""


_warnings = []


def log_warning(msg):
    _warnings.append(msg)
    print(f"WARN: {msg}", file=sys.stderr)


@dataclass
class Item:
    title: str
    url: str
    thumb: str = ""
    desc: str = ""
    author: str = ""
    is_backfill: bool = False


# --------------------------------------------------------------------------
# Scraping /recent-templates/
# --------------------------------------------------------------------------

def recent_templates_url(page):
    """Page 1 is the bare path; later pages use WordPress path pagination
    (/recent-templates/page/2/). Confirmed live - ?page=2 is not it."""
    base = f"{SITE}{RECENT_TEMPLATES_PATH}"
    return base if page <= 1 else f"{base.rstrip('/')}/page/{page}/"


def parse_recent_templates(html, page_url):
    """
    Card markup, confirmed in the raw server HTML on 8 Sep 2026:

        div.template-card
        ├── a.template-card-link            -> /templates/<slug>/
        │   └── div.template-preview > img  -> preview image (may be relative)
        └── div.template-info
            ├── a.template-title-link > h4  -> title
            ├── p.template-description      -> short blurb (truncated)
            └── div.template-author-stats
                └── p.template-author       -> "by <designer>"

    Returns [] if the markup changes - the run then reports
    "the template listing page came back empty" (see HANDOVER.md runbook).
    """
    soup = BeautifulSoup(html, "html.parser")
    items = []

    for card in soup.select("div.template-card"):
        link = card.select_one('a[href*="/templates/"]')
        if not link or not link.get("href"):
            continue
        url = urljoin(page_url, link["href"])

        heading = card.select_one(".template-info h4")
        img = card.select_one(".template-preview img")
        title = clean_text(heading.get_text()) if heading else clean_text(img.get("alt", "") if img else "")
        if not title:
            continue

        thumb = ""
        if img:
            raw = img.get("src") or img.get("data-src") or ""
            thumb = urljoin(page_url, raw) if raw else ""

        desc_el = card.select_one("p.template-description")
        author_el = card.select_one("p.template-author")

        items.append(Item(
            title=title,
            url=url,
            thumb=thumb,
            desc=clean_text(desc_el.get_text(), 200) if desc_el else "",
            author=clean_text(author_el.get_text()) if author_el else "",
        ))

    seen_urls, deduped = set(), []
    for it in items:
        if it.url not in seen_urls:
            seen_urls.add(it.url)
            deduped.append(it)
    return deduped


def fetch_page(page):
    url = recent_templates_url(page)
    try:
        resp = http_get(url)
        resp.raise_for_status()
    except requests.RequestException as e:
        log_warning(f"recent-templates page {page} failed: {e}")
        return []
    items = parse_recent_templates(resp.text, url)
    if not items:
        log_warning(f"the template listing page came back empty (page {page})")
    return items


def fetch_new_templates(seen):
    """Newest pages only. Anything here that isn't in seen.json is new."""
    found = []
    for page in range(1, SCAN_PAGES + 1):
        found.extend(it for it in fetch_page(page) if it.url not in seen)
    return dedupe(found)


def fetch_backfill_templates(seen, needed):
    """Walk deeper into /recent-templates/ for older templates that have
    never been posted. Stops as soon as `needed` are collected, so a quiet
    day costs one or two extra page fetches, not twenty."""
    if needed <= 0:
        return []
    picked = []
    for page in range(SCAN_PAGES + 1, BACKFILL_MAX_PAGES + 1):
        for it in fetch_page(page):
            if it.url not in seen:
                it.is_backfill = True
                picked.append(it)
                if len(picked) >= needed:
                    return dedupe(picked)
    if not picked:
        log_warning("backfill found nothing - every template on the site has been posted")
    return dedupe(picked)


def dedupe(items):
    seen_urls, out = set(), []
    for it in items:
        if it.url not in seen_urls:
            seen_urls.add(it.url)
            out.append(it)
    return out


def enrich(item: Item) -> Item:
    """Card blurbs are truncated; the template page's og:description is the
    full sentence. Best effort - the card text is a fine fallback."""
    try:
        resp = http_get(item.url)
        resp.raise_for_status()
    except requests.RequestException as e:
        log_warning(f"enrich failed ({item.url}): {e}")
        return item
    html = resp.text
    desc = extract_meta(html, "og:description") or extract_meta(html, "description")
    item.desc = clean_text(desc, 200) or item.desc
    item.thumb = extract_meta(html, "og:image") or item.thumb
    return item


def build_caption(item: Item) -> str:
    template = BACKFILL_CAPTION if item.is_backfill else TEMPLATE_CAPTION
    return template.format(title=item.title, desc=item.desc or "", url=item.url)


def fetch_thumbnail_data_uri(thumb_url):
    if not thumb_url:
        return None
    try:
        resp = http_get(thumb_url)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        return f"data:{ctype};base64," + base64.b64encode(resp.content).decode("ascii")
    except requests.RequestException as e:
        log_warning(f"thumbnail fetch failed ({thumb_url}): {e}")
        return None


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_seen():
    return set(load_json(SEEN_PATH, []))


def save_seen(seen_set):
    save_json(SEEN_PATH, list(seen_set)[-SEEN_CAP:])


def load_daily():
    d = load_json(DAILY_PATH, {"date": today_str(), "posted": 0, "limit": DAILY_LIMIT})
    if d.get("date") != today_str():
        d = {"date": today_str(), "posted": 0, "limit": DAILY_LIMIT}
    return d


def append_posts_csv(rows):
    POSTS_CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not POSTS_CSV_PATH.exists()
    with open(POSTS_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(["date", "time_ist", "type", "title", "url"])
        for row in rows:
            w.writerow(row)


def plan_run(new_items, daily, backfill_fetcher):
    """
    Decide what this run posts.

      budget          what is left of today's 8
      new templates   go out immediately, up to NEW_PER_RUN
      backfill        tops the day up towards 8, but only BACKFILL_PER_RUN
                      per run, so old templates trickle out across the day
                      instead of arriving in one burst

    backfill_fetcher(needed) is passed in so tests can drive this without
    touching the network.
    """
    budget = max(0, daily["limit"] - daily["posted"])
    if budget == 0:
        return []

    queue = new_items[:min(budget, NEW_PER_RUN)]
    remaining = budget - len(queue)
    if remaining > 0:
        want = min(remaining, BACKFILL_PER_RUN)
        queue.extend(backfill_fetcher(want))
    return queue


# --------------------------------------------------------------------------
# Baileys transport
# --------------------------------------------------------------------------

class BaileysSender:
    def __init__(self):
        self.proc = None

    def __enter__(self):
        if DRY_RUN or SENDER != "baileys":
            return self
        env = os.environ.copy()
        env["WA_CHANNEL_INVITE"] = CHANNEL_INVITE
        self.proc = subprocess.Popen(
            ["node", "baileys/send.js"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env, cwd=str(Path(__file__).parent))
        return self

    def send(self, caption, media_data_uri):
        if DRY_RUN or SENDER != "baileys":
            print(f"[DRY RUN] would post:\n{caption}\n(media: {'yes' if media_data_uri else 'no'})\n---")
            return {"ok": True, "dry_run": True}
        if self.proc is None or self.proc.poll() is not None:
            return {"ok": False, "error": "sender_process_not_running"}
        self.proc.stdin.write(json.dumps({"caption": caption, "media": media_data_uri}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            return {"ok": False, "error": f"sender_no_response: {self.proc.stderr.read()[-500:]}"}
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"sender_bad_response: {line[:500]}"}

    def __exit__(self, *exc):
        if self.proc is not None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=15)
            except Exception:
                self.proc.kill()


# --------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------

def watchdog(problem_from_run, health):
    if problem_from_run:
        return problem_from_run
    try:
        last_new = health.get("last_new_item_at")
        if last_new:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(last_new)).total_seconds() / 3600
            if age > ALERT_AFTER_HOURS:
                return f"nothing new for {int(age)} hours"
    except Exception as e:
        return f"watchdog itself failed: {e}"
    return None


def record_crash(exc):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    last_run = load_json(LAST_RUN_PATH, {})
    last_run["problem"] = f"the run crashed: {exc}"
    last_run["traceback"] = tb[-2000:]
    last_run["at"] = datetime.now(timezone.utc).isoformat()
    save_json(LAST_RUN_PATH, last_run)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    problem = None
    posted_count = 0

    hour = now_ist().hour
    if ACTIVE_FROM != ACTIVE_TO and not (ACTIVE_FROM <= hour < ACTIVE_TO):
        print(f"Outside posting hours ({ACTIVE_FROM}-{ACTIVE_TO} IST); nothing to do.")
        return

    seen = load_seen()
    daily = load_daily()

    new_items = fetch_new_templates(seen)
    queue = plan_run(new_items, daily, lambda n: fetch_backfill_templates(seen, n))

    posted_rows = []
    with BaileysSender() as sender:
        for item in queue:
            item = enrich(item)
            result = sender.send(build_caption(item), fetch_thumbnail_data_uri(item.thumb))
            if result.get("ok"):
                seen.add(item.url)
                posted_count += 1
                kind = "backfill" if item.is_backfill else "new"
                posted_rows.append([today_str(), now_ist().strftime("%H:%M"), kind, item.title, item.url])
            else:
                problem = problem or f"sender_error: {result.get('error', 'unknown')}"
                break   # stop rather than hammer a broken connection

    daily["posted"] += posted_count
    save_seen(seen)
    save_json(DAILY_PATH, daily)
    if posted_rows:
        append_posts_csv(posted_rows)

    health = load_json(HEALTH_PATH, {})
    health["last_checked_at"] = datetime.now(timezone.utc).isoformat()
    if new_items:
        health["last_new_item_at"] = datetime.now(timezone.utc).isoformat()
    save_json(HEALTH_PATH, health)

    if _warnings and not problem:
        problem = "; ".join(_warnings[:3])

    last_run = {
        "at": datetime.now(timezone.utc).isoformat(),
        "posted": posted_count,
        "new_found": len(new_items),
        "backfilled": sum(1 for r in posted_rows if r[2] == "backfill"),
        "posted_today": daily["posted"],
        "problem": watchdog(problem, health),
        "diagnostics": {"warnings": _warnings[:10]},
    }
    save_json(LAST_RUN_PATH, last_run)
    print(json.dumps(last_run, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        record_crash(exc)
        traceback.print_exc()
        if EXIT_ON_ERROR:
            sys.exit(1)
    sys.exit(0)   # normal runs always exit 0 - the alert email is the only alarm
