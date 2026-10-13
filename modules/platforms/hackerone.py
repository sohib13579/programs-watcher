"""HackerOne checker — LIVE source (public hackerone.com GraphQL).

The old Osb0rn3 feed froze in Nov 2024 (and its REST scraper needed API
credentials anyway). Live pipeline, fully unauthenticated:

  directory  GraphQL `teams(first:100, where:{_ilike:"%"})` -> name,
             handle, offers_bounties, submission_state (~6-8 pages)
  scopes     GraphQL `team(handle){structured_scopes{nodes{id
             asset_identifier eligible_for_submission instruction
             max_severity updated_at}}}`

Per ~15-min cycle:
  1. directory scan: new / removed programs (tracked set = teams with
     submission_state open|paused), bounty (rdp/vdp) and submission
     state changes
  2. rolling scope sweep: only the K least-recently-checked programs are
     re-polled; scope add/remove/change diffs carry the scope's own
     updated_at as the change date.
A tracked team that disappears from the open/paused set but still exists
with another state is reported as closed rather than deleted.
First run silently reseeds (directory + scopes per program).
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

H1_GRAPHQL = "https://hackerone.com/graphql"
TRACKED_STATES = ("open", "paused")
ROLLING_K = 10
_DIR_PAGE = 100

TEAMS_QUERY = """
query GetTeamsQuery($team_search: String, $after: String) {
  teams(first: %d, after: $after, where: {_or: [
    {handle: {_ilike: $team_search}}, {name: {_ilike: $team_search}}]}) {
    pageInfo { hasNextPage endCursor }
    edges { node { name handle offers_bounties submission_state } }
  }
}
""" % _DIR_PAGE

SCOPES_QUERY = """
query TeamScopes($handle: String!) {
  team(handle: $handle) {
    name
    handle
    structured_scopes(first: 100) {
      nodes { id asset_identifier eligible_for_submission instruction
              max_severity updated_at }
    }
  }
}
"""


def _graphql(query, variables):
    data = {"query": query, "variables": variables}
    import requests
    resp = requests.post(H1_GRAPHQL, json=data,
                         headers={"User-Agent": "Mozilla/5.0 (X11; "
                                  "Linux x86_64) programs-watcher/1.0",
                                  "Content-Type": "application/json"},
                         timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} from H1 GraphQL")
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError("H1 GraphQL error: "
                           + str(body["errors"])[:200])
    return body["data"]


def _fetch_directory():
    nodes, after = [], None
    while True:
        data = _graphql(TEAMS_QUERY, {"team_search": "%", "after": after})
        teams = data["teams"]
        nodes.extend(edge["node"] for edge in teams["edges"])
        if not teams["pageInfo"]["hasNextPage"]:
            return nodes
        after = teams["pageInfo"]["endCursor"]
        time.sleep(0.3)


def _fetch_scopes(handle):
    team = _graphql(SCOPES_QUERY, {"handle": handle}).get("team")
    if not team:
        return None
    scopes, stamps, latest = {}, {}, ""
    for node in (team.get("structured_scopes") or {}).get("nodes") or []:
        kind = "(InScope)" if node.get("eligible_for_submission") \
            else "(OutOfScope)"
        instruction = node.get("instruction")
        text = f"{node.get('asset_identifier', '')} {kind}\n"
        if instruction:
            text += f"{instruction}\n"
        scopes[node.get("id")] = text
        updated = node.get("updated_at") or ""
        stamps[node.get("id")] = updated
        if updated and updated > latest:
            latest = updated
    return scopes, latest, stamps


def _base_message(name, ptype, url, logo, extra):
    data = {
        "programName": name, "programType": ptype, "programURL": url,
        "logo": logo, "platformName": "HackerOne", "isRemoved": False,
        "isNewProgram": False, "color": 16777215,
        "newType": "", "newScope": [], "removedScope": [],
        "changedScope": [], "newProgramType": [], "note": "",
        "updated_at": "", "detected_at": now_cairo_str(),
    }
    data.update(extra)
    return data


def _notify(db, config, first_time, data, watcher, is_update, mUrl):
    if check_send_notification(first_time, is_update, data, watcher,
                               config["monitor"], config["notifications"]):
        discord_notifier.send_notification(data, mUrl)


# checking hackerone
def check_hackerone(tmp_dir, mUrl, first_time, db, config):
    try:
        live_nodes = _fetch_directory()
    except Exception as exc:
        print(f"[hackerone] live fetch failed: {exc}")
        alert_fetch_error(db, "hackerone", exc)
        return
    live = [n for n in live_nodes
            if (n.get("submission_state") or "") in TRACKED_STATES]
    by_handle = {n["handle"]: n for n in live_nodes}
    print(f"[hackerone] live GraphQL: {len(live_nodes)} teams, "
          f"{len(live)} tracked")
    reseed = needs_reseed(db, "hackerone")
    json_programs_key = []

    for node in live:
        name = node.get("name") or node["handle"]
        handle = node["handle"]
        program_url = f"https://hackerone.com/{handle}?type=team"
        program_key = generate_program_key(name, program_url)
        json_programs_key.append(program_key)
        program_type = "rdp" if node.get("offers_bounties") else "vdp"
        base_doc = {
            "programName": name, "programURL": program_url,
            "programType": program_type,
            "handle": handle, "submissionState": node.get("submission_state"),
            "logo": "",
        }
        watcher = find_program(db, "hackerone", program_key)

        if reseed:
            doc = dict(base_doc, scope={})
            try:
                scopes, latest, stamps = _fetch_scopes(handle)
                if scopes is not None:
                    doc["scope"], doc["lastScopeUpdatedAt"] = scopes, latest
                    doc["scopeTimestamps"] = stamps
                time.sleep(0.3)
            except Exception as exc:
                print(f"[hackerone] scope fetch failed for {handle}: {exc}")
            doc["lastCheckedAt"] = datetime.datetime.utcnow()
            save_data(db, "hackerone", program_key, doc)
            continue

        if watcher is None:
            scopes, latest, stamps = {}, "", {}
            try:
                scopes, latest, stamps = _fetch_scopes(handle)
                if scopes is None:
                    scopes = {}
            except Exception as exc:
                print(f"[hackerone] scope fetch failed for {handle}: {exc}")
            watcher_doc = dict(base_doc, scope=scopes,
                               lastScopeUpdatedAt=latest,
                               scopeTimestamps=stamps,
                               lastCheckedAt=datetime.datetime.utcnow())
            save_data(db, "hackerone", program_key, watcher_doc)
            data = _base_message(name, program_type, program_url, "",
                                 {"isNewProgram": True,
                                  "newScope": list(scopes.values()),
                                  "updated_at": fmt_iso(latest)
                                  if latest else ""})
            _notify(db, config, first_time, data, watcher_doc, True, mUrl)
            continue

        data = _base_message(name, program_type, program_url, "", {})
        new_doc = dict(watcher)
        new_doc.update(base_doc)
        is_update = False
        notes = []

        if watcher.get("programType") != program_type:
            if config["notifications"]["new_type"]:
                data["newProgramType"] = program_type
                data["type_old"] = watcher.get("programType")
                is_update = True
        old_state = watcher.get("submissionState") or "open"
        if old_state != node.get("submission_state"):
            data["status_old"] = old_state
            data["status_new"] = node.get("submission_state")
            is_update = True

        if notes:
            data["note"] = "; ".join(notes)
        if is_update:
            save_data(db, "hackerone", program_key, new_doc)
            _notify(db, config, first_time, data, watcher, True, mUrl)

    db_programs_key = db["hackerone"].distinct("programKey")
    removed_keys = set(db_programs_key) - set(json_programs_key)
    for program_key in removed_keys:
        program = find_program(db, "hackerone", program_key)
        handle = program.get("handle")
        node = by_handle.get(handle)
        if reseed:
            db["hackerone"].delete_many({"programKey": program_key})
            continue
        if node is not None and (node.get("submission_state") or "") \
                not in TRACKED_STATES:
            # still exists — submission closed/disabled, not deleted
            data = _base_message(program["programName"],
                                 program.get("programType") or "",
                                 program.get("programURL") or "", "",
                                 {"note": "submissions closed/disabled — "
                                          "removed from tracking",
                                  "isRemoved": True})
        else:
            data = _base_message(program["programName"],
                                 program.get("programType") or "",
                                 program.get("programURL") or "", "",
                                 {"isRemoved": True})
        data["color"] = 16777215
        if config["notifications"]["removed_program"] and not first_time:
            discord_notifier.send_notification(data, mUrl)
        db["hackerone"].delete_many({"programKey": program_key})

    if reseed:
        finish_reseed(db, "hackerone", len(live))

    # rolling scope sweep
    for program_key in pick_stale(db, "hackerone", ROLLING_K):
        doc = find_program(db, "hackerone", program_key)
        if not doc or not doc.get("handle"):
            continue
        try:
            scopes, latest, stamps = _fetch_scopes(doc["handle"])
            if scopes is None:
                mark_checked(db, "hackerone", program_key)
                continue
            time.sleep(0.3)
        except Exception as exc:
            print(f"[hackerone] scope sweep failed for "
                  f"{doc.get('programName')}: {exc}")
            mark_checked(db, "hackerone", program_key)
            continue

        new_doc = dict(doc)
        new_doc["scope"] = scopes
        new_doc["lastScopeUpdatedAt"] = latest
        new_doc["scopeTimestamps"] = stamps
        old_scopes = doc.get("scope") or {}
        is_update = False
        data = _base_message(doc["programName"],
                             doc.get("programType") or "",
                             doc.get("programURL") or "",
                             {}, {"updated_at": fmt_iso(latest)
                                  if latest else ""})
        new_ids, old_ids = set(scopes), set(old_scopes)
        added = [scopes[i] for i in new_ids - old_ids]
        removed = [old_scopes[i] for i in old_ids - new_ids]
        changed = [{"old": old_scopes[i], "new": scopes[i]}
                   for i in (new_ids & old_ids)
                   if scopes[i] != old_scopes[i]]
        if added and config["notifications"]["new_scope"]:
            data["newScope"] = added
            is_update = True
        if removed and config["notifications"]["removed_scope"]:
            data["removedScope"] = removed
            is_update = True
        if changed and config["notifications"]["changed_scope"]:
            data["changedScope"] = changed
            is_update = True
        if not is_update and doc.get("lastScopeUpdatedAt") != latest:
            old_stamps = doc.get("scopeTimestamps") or {}
            new_ids = set(stamps) & set(scopes)
            touched = sorted({scopes[i].split(" ", 1)[0] for i in new_ids
                              if stamps.get(i)
                              and stamps[i] > (old_stamps.get(i) or "")})
            if touched:
                data["note"] = ("H1 logged updates on: "
                                + ", ".join(touched[:8])
                                + " (no scope-text change detected)")
            else:
                data["note"] = ("platform logged an internal update (no "
                                "visible scope change) — check the page")
            is_update = True
        new_doc["scopeTimestamps"] = stamps
        save_data(db, "hackerone", program_key, new_doc)
        if is_update:
            _notify(db, config, first_time, data, doc, True, mUrl)
        mark_checked(db, "hackerone", program_key)
