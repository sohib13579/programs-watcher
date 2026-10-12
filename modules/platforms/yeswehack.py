"""YesWeHack checker — LIVE source (public api.yeswehack.com).

The old source (Osb0rn3/bugbounty-targets programs/yeswehack.json) froze
in Apr 2025, so the watcher compared a dead snapshot to itself and could
never fire on real YWH changes. This module queries the public API
directly (no auth needed):

    list   /programs?page=N      -> title, slug, bounty, currency, status,
                                    last_update_at, scopes_count
                                    (scopes come back null here)
    detail /programs/{slug}      -> full scopes[] with asset_value etc.

Detection layers per ~15-min cycle:
  1. structural: new / removed program, programType, bounty range, status
  2. lastUpdateAt / scopesCount changed -> fetch the detail endpoint and
     diff scopes precisely; if that fetch fails, still send a generic
     "program was updated" note (never silently swallow a change)
First run after deploy does a SILENT reseed + one info message: the Mongo
collection still holds the frozen Apr-2025 snapshot and a naive diff
would flood Telegram with dozens of stale alerts. Reseed state lives in
_meta.ywh_live_resynced. Feed-fetch failures send an alert rate-limited
to one per 6h (_meta.ywh_last_error_alert).
"""
import datetime
import html
import json
import time

import requests

import modules.notifier.discord as discord_notifier
from modules.notifier.telegram import send_info
from modules.platforms.functions import (check_send_notification,
                                         find_program, generate_program_key,
                                         save_data)
from modules.platforms.live_common import fmt_iso, now_cairo_str

API_BASE = "https://api.yeswehack.com/programs"
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) programs-watcher/1.0"}
_PAGE_DELAY_S = 0.4
_DETAIL_DELAY_S = 0.4
_ERROR_ALERT_COOLDOWN_S = 6 * 3600


def _api_get(url, retries=2):
    err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=_UA, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            err = RuntimeError("HTTP %s for %s" % (resp.status_code, url))
        except requests.RequestException as exc:
            err = exc
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    raise err


def _fetch_live_list():
    programs, page = [], 1
    while True:
        data = _api_get(f"{API_BASE}?page={page}&paginated_backend=true")
        items = data.get("items") or []
        programs.extend(items)
        nb_pages = (data.get("pagination") or {}).get("nb_pages") or 1
        if page >= nb_pages or not items:
            return programs
        page += 1
        time.sleep(_PAGE_DELAY_S)


def _fetch_detail(slug):
    time.sleep(_DETAIL_DELAY_S)
    return _api_get(f"{API_BASE}/{slug}")


def _scopes_of(detail):
    return [s.get("scope", "") for s in (detail.get("scopes") or [])]


def _alert_fetch_error(db, exc):
    meta = db["_meta"]
    now = datetime.datetime.utcnow()
    last = meta.find_one({"programKey": "ywh_last_error_alert"})
    if last and (now - last["at"]).total_seconds() < _ERROR_ALERT_COOLDOWN_S:
        return
    meta.update_one({"programKey": "ywh_last_error_alert"},
                    {"$set": {"at": now}}, upsert=True)
    send_info(
        "⚠️ <b>YesWeHack live feed fetch FAILED</b>\n"
        f"<code>{html.escape(str(exc)[:300])}</code>\n"
        "No YWH alerts until this is fixed — check the GitHub Actions run.")


def _base_message(title, programType, programURL, logo, extra):
    data = {
        "programName": title, "programType": programType,
        "programURL": programURL, "logo": logo,
        "platformName": "YesWeHack", "isRemoved": False,
        "isNewProgram": False, "color": 16270147,
        "newType": [], "reward": [], "removeInScope": [],
        "newInScope": [], "note": "",
        "updated_at": "", "detected_at": now_cairo_str(),
    }
    data.update(extra)
    return data


# checking yeswehack
def check_yeswehack(tmp_dir, mUrl, first_time, db, config):
    meta = db["_meta"]
    try:
        live = _fetch_live_list()
    except Exception as exc:
        print(f"[yeswehack] live fetch failed: {exc}")
        _alert_fetch_error(db, exc)
        return
    print(f"[yeswehack] live API: {len(live)} programs")
    try:
        with open(f"{tmp_dir}yeswehack_live.json", "w", encoding="utf-8") as f:
            json.dump(live, f)
    except OSError:
        pass

    need_reseed = meta.find_one({"programKey": "ywh_live_resynced"}) is None
    notifications = config["notifications"]
    json_programs_key = []

    for item in live:
        title = item.get("title") or item.get("slug", "")
        slug = item.get("slug", "")
        programURL = f"https://yeswehack.com/programs/{slug}"
        programKey = generate_program_key(title, programURL)
        json_programs_key.append(programKey)

        programType = "rdp" if item.get("bounty") else "vdp"
        currency = (item.get("business_unit") or {}).get("currency") or ""
        reward = ({"min": f"{item.get('bounty_reward_min')} {currency}",
                   "max": f"{item.get('bounty_reward_max')} {currency}"}
                  if item.get("bounty") else {})
        base_doc = {
            "programName": title,
            "programURL": programURL,
            "programType": programType,
            "reward": reward,
            "status": item.get("status") or "",
            "lastUpdateAt": item.get("last_update_at") or "",
            "scopesCount": item.get("scopes_count") or 0,
            "logo": (item.get("thumbnail") or {}).get("url", ""),
        }
        watcher = find_program(db, "yeswehack", programKey)

        if need_reseed:
            doc = dict(base_doc)
            try:
                doc["inScope"] = _scopes_of(_fetch_detail(slug))
            except Exception as exc:
                print(f"[yeswehack] detail fetch failed for {slug}: {exc}")
                doc["inScope"] = []
            save_data(db, "yeswehack", programKey, doc)
            continue

        if watcher is None:
            scopes = []
            try:
                scopes = _scopes_of(_fetch_detail(slug))
            except Exception as exc:
                print(f"[yeswehack] detail fetch failed for {slug}: {exc}")
            watcher_doc = dict(base_doc, inScope=scopes)
            save_data(db, "yeswehack", programKey, watcher_doc)
            data = _base_message(title, programType, programURL,
                                 base_doc["logo"],
                                 {"isNewProgram": True, "reward": reward,
                                  "newInScope": scopes,
                                  "updated_at": fmt_iso(base_doc["lastUpdateAt"])
                                  if base_doc["lastUpdateAt"] else ""})
            if check_send_notification(first_time, True, data, watcher_doc,
                                       config["monitor"], notifications):
                discord_notifier.send_notification(data, mUrl)
            continue

        # existing program: structural diffs + metadata-change detection
        data = _base_message(title, programType, programURL,
                             base_doc["logo"], {})
        new_doc = dict(base_doc)
        new_doc["inScope"] = watcher.get("inScope", [])
        is_update = False
        notes = []

        if watcher.get("programType") != programType:
            if notifications["new_type"]:
                data["newType"] = programType
                is_update = True
        if watcher.get("reward") != reward:
            if notifications["new_bounty_table"]:
                data["reward"] = reward
                is_update = True
        if watcher.get("status", "V") != base_doc["status"]:
            notes.append(f"status: {watcher.get('status', '?')} -> "
                         f"{base_doc['status']}")
            is_update = True

        metadata_changed = (
            watcher.get("lastUpdateAt") != base_doc["lastUpdateAt"]
            or watcher.get("scopesCount") != base_doc["scopesCount"])
        if metadata_changed or not watcher.get("inScope"):
            try:
                scopes = _scopes_of(_fetch_detail(slug))
            except Exception as exc:
                print(f"[yeswehack] detail fetch failed for {slug}: {exc}")
                scopes = None
            if scopes is None:
                # keep the old scope list, but record the new timestamp so
                # this change is not re-alerted every cycle
                notes.append("platform reports an update (detail fetch "
                             "failed — check the program page)")
                is_update = True
            else:
                new_doc["inScope"] = scopes
                old_set, new_set = set(watcher.get("inScope", [])), set(scopes)
                newInScope = [s for s in scopes if s not in old_set]
                removedInScope = [s for s in watcher.get("inScope", [])
                                  if s not in new_set]
                if newInScope and notifications["new_inscope"]:
                    data["newInScope"] = newInScope
                    is_update = True
                if removedInScope and notifications["removed_inscope"]:
                    data["removeInScope"] = removedInScope
                    is_update = True
                if not is_update:
                    # timestamp moved with no scope/type/reward change:
                    # still report it — the user wants every small update
                    notes.append("platform reports an update (no scope/"
                                 "bounty change detected)")
                    is_update = True

        if notes:
            data["note"] = "; ".join(notes)
        data["updated_at"] = fmt_iso(base_doc["lastUpdateAt"]) \
            if base_doc["lastUpdateAt"] else ""
        if is_update:
            save_data(db, "yeswehack", programKey, new_doc)
            if check_send_notification(first_time, is_update, data, watcher,
                                       config["monitor"], notifications):
                discord_notifier.send_notification(data, mUrl)

    db_programs_key = db["yeswehack"].distinct("programKey")
    removed_programs_key = set(db_programs_key) - set(json_programs_key)
    for program_key in removed_programs_key:
        program = find_program(db, "yeswehack", program_key)
        if need_reseed:
            db["yeswehack"].delete_many({"programKey": program_key})
            continue
        data = {
            "color": 16270147,
            "logo": "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcTwToiI8YA0eLclDkd-vJ0xXs7bun5LdHfTrgJucvI&s",
            "platformName": "YesWeHack",
            "isRemoved": True,
            "programName": program["programName"],
            "programType": program["programType"],
            "note": "",
            "updated_at": "",
            "detected_at": now_cairo_str(),
        }
        if notifications["removed_program"] and not first_time:
            discord_notifier.send_notification(data, mUrl)
        db["yeswehack"].delete_many({"programKey": program_key})

    if need_reseed:
        meta.update_one({"programKey": "ywh_live_resynced"},
                        {"$set": {"at": datetime.datetime.utcnow(),
                                  "programs": len(live)}}, upsert=True)
        send_info(
            "🔌 <b>YesWeHack source switched to LIVE</b>\n\n"
            "The old third-party feed was frozen since <b>Apr 2025</b> — "
            "that is why YWH alerts never arrived.\n\n"
            f"📡 Reseeded: <b>{len(live)}</b> live programs now tracked.\n"
            "🔔 From now on you get: new/removed programs, scope changes, "
            "bounty changes, status changes — and <b>any</b> platform-side "
            "update, even small ones.")
        discord_notifier.notifications_sent += 1
