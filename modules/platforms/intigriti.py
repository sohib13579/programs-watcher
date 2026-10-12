"""Intigriti checker — LIVE source (public app.intigriti.com core API).

The old Osb0rn3 feed froze in Dec 2024 (and its scraper needed a
researcher API token). Live pipeline, fully unauthenticated:

  list  GET /api/core/public/programs  -> ALL public programs in ONE
        request: name, handles, min/maxBounty {value,currency}, status,
        confidentialityLevel, lastUpdatedAt (epoch seconds)

Per ~15-min cycle (a single list request):
  new / removed programs, bounty range, type, and lastUpdatedAt changes.
  A lastUpdatedAt change with no bounty/type change still alerts — the
  message carries the platform's own update date and points to the
  program page (scope-level detail requires a researcher token, which
  the public API does not expose).
First run silently reseeds.
"""
import datetime

import modules.notifier.discord as discord_notifier
from modules.platforms.functions import (check_send_notification,
                                         find_program, generate_program_key,
                                         save_data)
from modules.platforms.live_common import (alert_fetch_error, api_get,
                                           finish_reseed, fmt_epoch,
                                           needs_reseed, now_cairo_str)

IG_API = "https://app.intigriti.com/api/core/public/programs"
_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://app.intigriti.com/programs",
}


def _bounty_line(program):
    max_b = program.get("maxBounty") or {}
    if (max_b.get("value") or 0) <= 0:
        return "vdp", {}
    currency = max_b.get("currency") or ""
    return "rdp", {
        "min": f"{(program.get('minBounty') or {}).get('value', 0)} {currency}",
        "max": f"{max_b.get('value', 0)} {currency}",
    }


def _base_message(name, ptype, url, extra):
    data = {
        "programName": name, "programType": ptype, "programURL": url,
        "logo": "https://secure.gravatar.com/avatar/8609b9cc1f1333c3632b2afdc8607f4d?s=500&d=identicon&r=g",
        "platformName": "Intigriti", "isRemoved": False,
        "isNewProgram": False, "color": 10858237,
        "newType": "", "newScope": [], "removedScope": [],
        "changedScope": [], "newProgramType": [], "newReward": [],
        "note": "", "updated_at": "", "detected_at": now_cairo_str(),
    }
    data.update(extra)
    return data


def _notify(db, config, first_time, data, watcher, is_update, mUrl):
    if check_send_notification(first_time, is_update, data, watcher,
                               config["monitor"], config["notifications"]):
        discord_notifier.send_notification(data, mUrl)


# checking intigriti
def check_intigriti(tmp_dir, mUrl, first_time, db, config):
    try:
        live = api_get(IG_API, headers=_HEADERS)
    except Exception as exc:
        print(f"[intigriti] live fetch failed: {exc}")
        alert_fetch_error(db, "intigriti", exc)
        return
    print(f"[intigriti] live API: {len(live)} programs")
    reseed = needs_reseed(db, "intigriti")
    json_programs_key = []

    for program in live:
        name = program.get("name") or ""
        handle = program.get("handle") or ""
        company = program.get("companyHandle") or ""
        if not name or not handle:
            continue
        program_url = f"https://app.intigriti.com/programs/{company}/{handle}"
        program_key = generate_program_key(name, program_url)
        json_programs_key.append(program_key)
        program_type, reward = _bounty_line(program)
        updated_at = program.get("lastUpdatedAt")
        base_doc = {
            "programName": name, "programURL": program_url,
            "programType": program_type, "reward": reward,
            "lastUpdatedAt": updated_at,
            "status": program.get("status"),
        }
        watcher = find_program(db, "intigriti", program_key)

        if reseed:
            base_doc["lastCheckedAt"] = datetime.datetime.utcnow()
            save_data(db, "intigriti", program_key, base_doc)
            continue

        if watcher is None:
            save_data(db, "intigriti", program_key,
                      dict(base_doc,
                           lastCheckedAt=datetime.datetime.utcnow()))
            data = _base_message(name, program_type, program_url,
                                 {"isNewProgram": True, "newReward": reward,
                                  "updated_at": fmt_epoch(updated_at)
                                  if updated_at else ""})
            _notify(db, config, first_time, data, base_doc, True, mUrl)
            continue

        data = _base_message(name, program_type, program_url, {})
        new_doc = dict(watcher)
        new_doc.update(base_doc)
        new_doc["lastCheckedAt"] = datetime.datetime.utcnow()
        is_update = False
        notes = []

        if watcher.get("programType") != program_type:
            if config["notifications"]["new_type"]:
                data["newProgramType"] = program_type
                is_update = True
        if watcher.get("reward") != reward:
            if config["notifications"]["new_bounty_table"]:
                data["newReward"] = reward
                is_update = True
        if watcher.get("status") != program.get("status"):
            notes.append(f"status: {watcher.get('status')} -> "
                         f"{program.get('status')}")
            is_update = True
        if watcher.get("lastUpdatedAt") != updated_at and not is_update:
            # platform timestamps an update we can't see anonymously
            # (scope edits need a researcher token) — report it anyway
            notes.append("program updated on Intigriti (details on the "
                         "program page)")
            is_update = True

        data["updated_at"] = fmt_epoch(updated_at) if updated_at else ""
        if notes:
            data["note"] = "; ".join(notes)
        if is_update:
            save_data(db, "intigriti", program_key, new_doc)
            _notify(db, config, first_time, data, watcher, True, mUrl)
        else:
            save_data(db, "intigriti", program_key, new_doc)

    db_programs_key = db["intigriti"].distinct("programKey")
    removed_keys = set(db_programs_key) - set(json_programs_key)
    for program_key in removed_keys:
        program = find_program(db, "intigriti", program_key)
        if reseed:
            db["intigriti"].delete_many({"programKey": program_key})
            continue
        data = _base_message(program["programName"],
                             program.get("programType") or "",
                             program.get("programURL") or "",
                             {"isRemoved": True})
        data["color"] = 10858237
        if config["notifications"]["removed_program"] and not first_time:
            discord_notifier.send_notification(data, mUrl)
        db["intigriti"].delete_many({"programKey": program_key})

    if reseed:
        finish_reseed(db, "intigriti", len(live))
