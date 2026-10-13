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
import json
import time

import yaml

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


def _ig_token():
    import os
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.full_load(f)
    tok = os.environ.get("INTIGRITI_TOKEN", cfg.get("intigriti_token", ""))
    if not tok or str(tok).startswith("<"):
        return None
    return str(tok)


def _fetch_domains(program_id):
    """{domain_id: 'endpoint
description
'} via the researcher API.
    None = token missing/invalid (caller falls back gracefully)."""
    import os
    token = _ig_token()
    if not token:
        return None
    headers = dict(_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    try:
        detail = api_get(f"https://api.intigriti.com/external/researcher/"
                         f"v1/programs/{program_id}", headers=headers)
        content = ((detail.get("domains") or {}).get("content")) or []
    except Exception as exc:
        print(f"[intigriti] domain fetch failed for {program_id}: {exc}")
        return None
    domains = {}
    for d in content:
        text = f"{d.get('endpoint', '')}\n"
        if d.get("description"):
            text += f"{d['description']}\n"
        domains[d.get("id")] = text
    return domains


def _table_lines(bounty_table):
    """Compact 'Score lo-hi: amount CUR' lines for a bountyTable."""
    content = (bounty_table or {}).get("content") or {}
    currency = content.get("currency") or ""
    lines = []
    for row in content.get("bountyRows") or []:
        for r in row.get("bountyRanges") or []:
            bounty = r.get("maxBounty") or r.get("minBounty") or {}
            amount = bounty.get("value", "?")
            cur = bounty.get("currency") or currency
            lines.append(f"Score {r.get('minScore')}-{r.get('maxScore')}: "
                         f"{amount} {cur}".strip())
    return lines


def _fetch_preview(company, handle):
    """Public program preview (needs browser-ish headers, else 403)."""
    headers = dict(_HEADERS,
                   Referer=f"https://app.intigriti.com/programs/{company}/{handle}",
                   **{"sec-fetch-dest": "empty", "sec-fetch-mode": "cors",
                      "sec-fetch-site": "same-origin"})
    preview = api_get(f"{IG_API}/{company}/{handle}/preview", headers=headers)
    return (json.dumps(preview.get("bountyTable"), sort_keys=True),
            preview.get("description") or "")


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
            try:
                table_json, description = _fetch_preview(company, handle)
                base_doc["bountyTable"] = table_json
                base_doc["description"] = description
                time.sleep(0.2)
            except Exception as exc:
                print(f"[intigriti] preview fetch failed for {name}: {exc}")
            if program.get("programId"):
                doms = _fetch_domains(program["programId"])
                if doms is not None:
                    base_doc["domains"] = doms
                    time.sleep(0.2)
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
                data["type_old"] = watcher.get("programType")
                is_update = True
        if watcher.get("reward") != reward:
            if config["notifications"]["new_bounty_table"]:
                data["newReward"] = reward
                data["reward_old"] = watcher.get("reward")
                is_update = True
        if watcher.get("status") != program.get("status"):
            data["status_old"] = watcher.get("status")
            data["status_new"] = program.get("status")
            is_update = True
        if watcher.get("lastUpdatedAt") != updated_at:
            # the list timestamp moved: diff the public preview (bounty
            # table + description) to say WHAT changed
            try:
                table_json, description = _fetch_preview(company, handle)
            except Exception as exc:
                print(f"[intigriti] preview fetch failed for {name}: {exc}")
                table_json, description = None, None
            if table_json is not None:
                old_table = watcher.get("bountyTable")
                old_desc = watcher.get("description")
                if old_table is not None and table_json != old_table:
                    data["bounty_table_old"] = _table_lines(
                        json.loads(old_table))
                    data["bounty_table_new"] = _table_lines(
                        json.loads(table_json))
                    is_update = True
                if old_desc is not None and description != old_desc:
                    data["desc_changed"] = True
                    is_update = True
                new_doc["bountyTable"] = table_json
                new_doc["description"] = description
            if program.get("programId"):
                doms = _fetch_domains(program["programId"])
                if doms is not None:
                    old_doms = watcher.get("domains")
                    if old_doms is not None and doms != old_doms:
                        old_ids, new_ids = set(old_doms), set(doms)
                        data["newScope"] = [doms[i] for i in new_ids
                                            - old_ids]
                        data["removedScope"] = [old_doms[i]
                                                for i in old_ids - new_ids]
                        data["changedScope"] = [
                            {"old": old_doms[i], "new": doms[i]}
                            for i in (old_ids & new_ids)
                            if doms[i] != old_doms[i]]
                        if (data["newScope"] or data["removedScope"]
                                or data["changedScope"]):
                            is_update = True
                    new_doc["domains"] = doms
            if not is_update:
                notes.append("platform logged an internal update "
                             "(no visible scope/bounty change) — check "
                             "the program page")
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
