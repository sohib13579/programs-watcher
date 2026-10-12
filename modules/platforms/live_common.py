"""Shared helpers for the LIVE platform checkers.

Every platform module (bugcrowd / hackerone / intigriti / yeswehack)
follows the same contract: fetch the program list from the platform's
public API, diff against Mongo state, notify on changes, SILENTLY reseed
once on deploy (state holds a frozen snapshot — a naive diff would flood
Telegram), and alert via Telegram when the live fetch itself fails
(rate-limited so a broken platform can't spam).

Rolling sweep: platforms whose scope details need one request per program
(Bugcrowd changelogs, HackerOne structured_scopes) refresh only the K
least-recently-checked programs per cycle, tracked via lastCheckedAt.
"""
import datetime
import html
import time

import requests

import modules.notifier.discord as discord_notifier
from modules.notifier.telegram import send_info

ERROR_ALERT_COOLDOWN_S = 6 * 3600
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) programs-watcher/1.0"}


def api_get(url, retries=2, headers=None, timeout=30, params=None):
    hdrs = dict(_UA)
    if headers:
        hdrs.update(headers)
    err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=hdrs, timeout=timeout,
                                params=params)
            if resp.status_code == 200:
                return resp.json()
            err = RuntimeError("HTTP %s for %s" % (resp.status_code, url))
        except requests.RequestException as exc:
            err = exc
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise err


def cairo_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Africa/Cairo"))
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=3)


def fmt_cairo(dt):
    return dt.strftime("%Y-%m-%d %H:%M") + " Cairo"


def now_cairo_str():
    return fmt_cairo(cairo_now())


def fmt_epoch(ts):
    """Intigriti-style epoch seconds -> Cairo string."""
    try:
        dt = datetime.datetime.fromtimestamp(int(ts),
                                             tz=datetime.timezone.utc)
        return fmt_cairo(dt.astimezone(_cairo_zone()))
    except Exception:
        return str(ts)


def _cairo_zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Africa/Cairo")


def fmt_iso(iso):
    """ISO8601 (maybe with offset) -> Cairo string."""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return fmt_cairo(dt.astimezone(_cairo_zone()))
    except Exception:
        return str(iso)


def alert_fetch_error(db, platform, exc):
    meta = db["_meta"]
    now = datetime.datetime.utcnow()
    key = f"{platform}_last_error_alert"
    last = meta.find_one({"programKey": key})
    if last and (now - last["at"]).total_seconds() < ERROR_ALERT_COOLDOWN_S:
        return
    meta.update_one({"programKey": key}, {"$set": {"at": now}}, upsert=True)
    send_info(
        f"⚠️ <b>{html.escape(platform)} live feed fetch FAILED</b>\n"
        f"<code>{html.escape(str(exc)[:300])}</code>\n"
        "No alerts for this platform until this is fixed — check the "
        "GitHub Actions run.")


def needs_reseed(db, platform):
    return db["_meta"].find_one(
        {"programKey": f"{platform}_live_resynced"}) is None


def finish_reseed(db, platform, count):
    db["_meta"].update_one(
        {"programKey": f"{platform}_live_resynced"},
        {"$set": {"at": datetime.datetime.utcnow(), "programs": count}},
        upsert=True)
    send_info(
        "🔌 <b>" + platform.capitalize() + " source switched to LIVE</b>\n\n"
        "The old third-party feed was frozen — that is why alerts never "
        "arrived.\n\n"
        f"📡 Reseeded: <b>{count}</b> live programs now tracked.\n"
        "🔔 From now on: new/removed programs, scope, bounty and status "
        "changes — every update gets reported.")
    discord_notifier.notifications_sent += 1


def pick_stale(db, platform, k):
    """programKeys of the k least-recently-checked docs (rolling sweep)."""
    docs = db[platform].find({}, {"programKey": 1, "lastCheckedAt": 1}) \
                       .sort("lastCheckedAt", 1).limit(k)
    return [d["programKey"] for d in docs if d.get("programKey")]


def mark_checked(db, platform, programKey, when=None):
    db[platform].update_one(
        {"programKey": programKey},
        {"$set": {"lastCheckedAt": when or datetime.datetime.utcnow()}})
