"""Bugcrowd checker — LIVE source (public bugcrowd.com JSON endpoints).

The old Osb0rn3 feed froze in Sep 2024. Live pipeline (no auth needed):

  directory  /engagements.json?page=N          (24/page) -> name, briefUrl,
             logoUrl, rewardSummary (bounty strings), accessStatus, endsAt
  changelog  {briefUrl}/changelog.json         -> revision list, newest
             first, each with publishedAt + changelogShowUrl
  revision   {changelogShowUrl}.json           -> full brief incl. the
             complete scope at that revision (data.scope groups)

Per ~15-min cycle:
  1. directory scan (13-14 requests): new / removed programs, bounty
     range, type, access status
  2. rolling changelog sweep: only the K least-recently-checked programs
     are polled for a newer changelog revision; a new revision refetches
     the full scope and diffs in/out assets precisely. The revision's
     publishedAt is the change date shown in alerts.
  3. brief-text-only revisions (no scope/reward change) still produce a
     generic "program updated" note — nothing is silently swallowed.
First run silently reseeds (directory + newest revision per program).
"""
import datetime
import time

import modules.notifier.discord as discord_notifier
from modules.platforms.functions import (check_send_notification,
                                         find_program, generate_program_key,
                                         save_data)
from modules.platforms.live_common import (alert_fetch_error, api_get,
                                           finish_reseed, fmt_iso, mark_checked,
                                           needs_reseed, now_cairo_str,
                                           pick_stale)

BC_BASE = "https://bugcrowd.com"
ROLLING_K = 30
_PAGE_DELAY_S = 0.3
_PAGE_SIZE = 24


def _fetch_directory():
    programs, page = [], 1
    while True:
        data = api_get(f"{BC_BASE}/engagements.json", params={"page": page})
        items = data.get("engagements") or []
        programs.extend(items)
        total = (data.get("paginationMeta") or {}).get("totalCount") or 0
        if page * _PAGE_SIZE >= total or not items:
            return programs
        page += 1
        time.sleep(_PAGE_DELAY_S)


def _scopes_from_groups(groups):
    in_scope, out_scope = [], []
    for group in groups or []:
        for target in group.get("targets") or []:
            name = target.get("name") or ""
            if not name:
                continue
            if group.get("inScope", group.get("in_scope", True)):
                in_scope.append(name)
            else:
                out_scope.append(name)
    return in_scope, out_scope


def _latest_revision(brief_url):
    """(publishedAt, showUrl) of the newest changelog revision, or None."""
    changelogs = (api_get(f"{BC_BASE}{brief_url}/changelog.json")
                  .get("changelogs") or [])
    if not changelogs:
        return None, None
    latest = changelogs[0]
    return latest.get("publishedAt"), latest.get("changelogShowUrl")


def _revision_scope(show_url):
    detail = api_get(f"{BC_BASE}{show_url}.json")
    groups = (detail.get("data") or {}).get("scope") or []
    in_scope, out_scope = _scopes_from_groups(groups)
    return in_scope, out_scope, detail.get("statusLabel") or ""


def _base_message(name, ptype, url, logo, extra):
    data = {
        "programName": name, "programType": ptype, "programURL": url,
        "logo": logo, "platformName": "Bugcrowd", "isRemoved": False,
        "isNewProgram": False, "color": 14584064,
        "newType": "", "reward": [], "newInScope": [], "removeInScope": [],
        "newOutOfScope": [], "removeOutOfScope": [], "note": "",
        "updated_at": "", "detected_at": now_cairo_str(),
    }
    data.update(extra)
    return data


def _notify(db, config, first_time, data, watcher, is_update, mUrl):
    if check_send_notification(first_time, is_update, data, watcher,
                               config["monitor"], config["notifications"]):
        discord_notifier.send_notification(data, mUrl)


# checking bugcrowd
def check_bugcrowd(tmp_dir, mUrl, first_time, db, config):
    try:
        live = _fetch_directory()
    except Exception as exc:
        print(f"[bugcrowd] live fetch failed: {exc}")
        alert_fetch_error(db, "bugcrowd", exc)
        return
    live = [p for p in live if not p.get("isDemo") and not p.get("isBanned")]
    print(f"[bugcrowd] live API: {len(live)} engagements")
    reseed = needs_reseed(db, "bugcrowd")
    json_programs_key = []

    for item in live:
        name = item.get("name") or ""
        brief_url = item.get("briefUrl") or ""
        if not name or not brief_url:
            continue
        program_url = BC_BASE + brief_url
        program_key = generate_program_key(name, program_url)
        json_programs_key.append(program_key)

        reward_summary = item.get("rewardSummary") or None
        program_type = "rdp" if reward_summary else "vdp"
        reward = ({"min": reward_summary.get("minReward") or "",
                   "max": reward_summary.get("maxReward") or ""}
                  if reward_summary else {})
        access = item.get("accessStatus") or ""
        base_doc = {
            "programName": name, "programURL": program_url,
            "programType": program_type, "reward": reward,
            "accessStatus": access,
            "logo": item.get("logoUrl") or "",
        }
        watcher = find_program(db, "bugcrowd", program_key)

        if reseed:
            doc = dict(base_doc, inScope=[], outOfScope=[],
                       lastChangelogAt=None, statusLabel="")
            try:
                published_at, show_url = _latest_revision(brief_url)
                doc["lastChangelogAt"] = published_at
                if show_url:
                    in_scope, out_scope, status_label = _revision_scope(show_url)
                    doc["inScope"], doc["outOfScope"] = in_scope, out_scope
                    doc["statusLabel"] = status_label
                time.sleep(_PAGE_DELAY_S)
            except Exception as exc:
                print(f"[bugcrowd] revision fetch failed for {name}: {exc}")
            doc["lastCheckedAt"] = datetime.datetime.utcnow()
            save_data(db, "bugcrowd", program_key, doc)
            continue

        if watcher is None:
            in_scope, out_scope = [], []
            updated_at = ""
            try:
                published_at, show_url = _latest_revision(brief_url)
                if show_url:
                    in_scope, out_scope, _ = _revision_scope(show_url)
                    updated_at = fmt_iso(published_at) if published_at else ""
            except Exception as exc:
                print(f"[bugcrowd] revision fetch failed for {name}: {exc}")
            watcher_doc = dict(base_doc, inScope=in_scope, outOfScope=out_scope,
                               lastChangelogAt=None, statusLabel="")
            watcher_doc["lastCheckedAt"] = datetime.datetime.utcnow()
            save_data(db, "bugcrowd", program_key, watcher_doc)
            data = _base_message(name, program_type, program_url,
                                 base_doc["logo"],
                                 {"isNewProgram": True, "reward": reward,
                                  "newInScope": in_scope,
                                  "updated_at": updated_at})
            _notify(db, config, first_time, data, watcher_doc, True, mUrl)
            continue

        data = _base_message(name, program_type, program_url,
                             base_doc["logo"], {})
        new_doc = dict(watcher)
        new_doc.update(base_doc)
        is_update = False
        notes = []

        if watcher.get("programType") != program_type:
            if config["notifications"]["new_type"]:
                data["newType"] = program_type
                data["type_old"] = watcher.get("programType")
                is_update = True
        if watcher.get("reward") != reward:
            if config["notifications"]["new_bounty_table"]:
                data["reward"] = reward
                data["reward_old"] = watcher.get("reward")
                is_update = True
        if watcher.get("accessStatus", "open") != access:
            notes.append(f"access status: "
                         f"{watcher.get('accessStatus', '?')} -> {access}")
            is_update = True

        if notes:
            data["note"] = "; ".join(notes)
        if is_update:
            save_data(db, "bugcrowd", program_key, new_doc)
            _notify(db, config, first_time, data, watcher, True, mUrl)

    db_programs_key = db["bugcrowd"].distinct("programKey")
    removed_keys = set(db_programs_key) - set(json_programs_key)
    for program_key in removed_keys:
        program = find_program(db, "bugcrowd", program_key)
        if reseed:
            db["bugcrowd"].delete_many({"programKey": program_key})
            continue
        data = _base_message(program["programName"],
                             program.get("programType") or "",
                             program.get("programURL") or "", "",
                             {"isRemoved": True})
        data["color"] = 14584064
        if config["notifications"]["removed_program"] and not first_time:
            discord_notifier.send_notification(data, mUrl)
        db["bugcrowd"].delete_many({"programKey": program_key})

    if reseed:
        finish_reseed(db, "bugcrowd", len(live))

    # rolling changelog sweep: scope-level changes + change dates
    for program_key in pick_stale(db, "bugcrowd", ROLLING_K):
        doc = find_program(db, "bugcrowd", program_key)
        if not doc:
            continue
        program_url = doc.get("programURL") or ""
        if not program_url.startswith(BC_BASE):
            mark_checked(db, "bugcrowd", program_key)
            continue
        brief_url = program_url[len(BC_BASE):]
        try:
            published_at, show_url = _latest_revision(brief_url)
            if not show_url:
                mark_checked(db, "bugcrowd", program_key)
                continue
            if doc.get("lastChangelogAt") == published_at:
                mark_checked(db, "bugcrowd", program_key)
                continue
            in_scope, out_scope, status_label = _revision_scope(show_url)
        except Exception as exc:
            print(f"[bugcrowd] changelog sweep failed for "
                  f"{doc.get('programName')}: {exc}")
            mark_checked(db, "bugcrowd", program_key)
            continue

        new_doc = dict(doc)
        new_doc["lastChangelogAt"] = published_at
        new_doc["statusLabel"] = status_label
        is_update = False
        notes = []
        data = _base_message(doc["programName"],
                             doc.get("programType") or "", program_url,
                             doc.get("logo") or {},
                             {"updated_at": fmt_iso(published_at)
                              if published_at else ""})

        if doc.get("statusLabel") and \
                doc["statusLabel"] != status_label and \
                status_label != "In progress":
            notes.append(f"status: {doc['statusLabel']} -> {status_label}")
            is_update = True

        old_in = doc.get("inScope") or []
        old_out = doc.get("outOfScope") or []
        new_in_scope = [s for s in in_scope if s not in set(old_in)]
        rem_in_scope = [s for s in old_in if s not in set(in_scope)]
        new_out_scope = [s for s in out_scope if s not in set(old_out)]
        rem_out_scope = [s for s in old_out if s not in set(out_scope)]
        if new_in_scope and config["notifications"]["new_inscope"]:
            data["newInScope"] = new_in_scope
            is_update = True
        if rem_in_scope and config["notifications"]["removed_inscope"]:
            data["removeInScope"] = rem_in_scope
            is_update = True
        if new_out_scope and config["notifications"]["new_out_of_scope"]:
            data["newOutOfScope"] = new_out_scope
            is_update = True
        if rem_out_scope and config["notifications"]["removed_out_of_scope"]:
            data["removeOutOfScope"] = rem_out_scope
            is_update = True
        new_doc["inScope"], new_doc["outOfScope"] = in_scope, out_scope

        if not is_update:
            notes.append("brief updated (no scope/bounty change detected)")
            is_update = True
        if notes:
            data["note"] = "; ".join(notes)
        save_data(db, "bugcrowd", program_key, new_doc)
        _notify(db, config, first_time, data, doc, True, mUrl)
        mark_checked(db, "bugcrowd", program_key)
