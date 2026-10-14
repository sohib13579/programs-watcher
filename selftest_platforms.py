"""Offline self-test for the live Bugcrowd / HackerOne / Intigriti checkers.

Synthetic ground truth only — no network, no real Mongo (mongomock).
Run: python selftest_platforms.py
"""
import datetime
import sys

import mongomock

import modules.platforms.bugcrowd as bc
import modules.platforms.hackerone as h1
import modules.platforms.intigriti as ig

FAILURES = []


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILURES.append(label)


CONFIG = {
    "monitor": {"rdp": True, "vdp": True,
                "specific_programs": [], "excluded_programs": []},
    "notifications": {"new_program": True, "removed_program": True,
                      "new_inscope": True, "removed_inscope": True,
                      "new_out_of_scope": True, "removed_out_of_scope": True,
                      "new_scope": True, "removed_scope": True,
                      "changed_scope": True,
                      "new_type": True, "new_bounty_table": True},
}


class Recorder:
    def __init__(self):
        self.sent = []
        self.infos = []

    def reset(self):
        self.sent.clear()
        self.infos.clear()


REC = Recorder()


def install_common():
    import modules.platforms.live_common as lc
    lc.send_info = lambda text: REC.infos.append(text)
    bc.discord_notifier.send_notification = lambda d, u: REC.sent.append(d)
    h1.discord_notifier.send_notification = lambda d, u: REC.sent.append(d)
    ig.discord_notifier.send_notification = lambda d, u: REC.sent.append(d)
    bc.now_cairo_str = lambda: "2026-10-13 02:00 Cairo"
    h1.now_cairo_str = lambda: "2026-10-13 02:00 Cairo"
    ig.now_cairo_str = lambda: "2026-10-13 02:00 Cairo"
    ig.fmt_epoch = lambda ts: "2026-10-12 21:43 Cairo"
    bc.fmt_iso = lambda iso: "2026-10-12 21:43 Cairo"
    h1.fmt_iso = lambda iso: "2026-10-12 21:43 Cairo"


def fresh_db():
    return mongomock.MongoClient()["programs_watcher"]


def by_name(name):
    hits = [d for d in REC.sent if d.get("programName") == name]
    return hits[0] if hits else None


def msg_looks_clean(data):
    """The user-facing formatting rules, applied to the telegram text."""
    from modules.notifier.telegram import build_message_text
    text = build_message_text(data)
    ok_name = f"📌 <b>{data['programName']}</b>" in text
    ok_date = ("🕒 <b>Updated:</b>" in text) or ("🕒 <b>Detected:</b>" in text)
    # the program link lives in the inline keyboard button now
    ok_no_inline_link = '🔗 <a href=' not in text
    return ok_name and ok_date and ok_no_inline_link, text


# ---------------------------------------------------------------- bugcrowd
def bc_scenario():
    print("== Bugcrowd: reseed, bounty diff, changelog sweep, removed ==")
    db = fresh_db()
    dir_items = [
        {"name": "Prog A", "briefUrl": "/engagements/prog-a", "logoUrl": "l",
         "rewardSummary": {"minReward": "$100", "maxReward": "$5,000"},
         "accessStatus": "open", "isDemo": False, "isBanned": False},
        {"name": "Prog B", "briefUrl": "/engagements/prog-b", "logoUrl": "l",
         "rewardSummary": None, "accessStatus": "open",
         "isDemo": False, "isBanned": False},
    ]
    revisions = {
        "/engagements/prog-a": ("2026-10-06T19:56:36.194Z",
                                "/engagements/prog-a/cl-1",
                                ["a.com"], ["bad.com"], "In progress",
                                "brief-v1"),
        "/engagements/prog-b": (None, None, [], [], "", ""),
    }
    bc._fetch_directory = lambda: (list(dir_items), True)
    bc._latest_revision = lambda brief: revisions[brief][:2] + ("brief",)

    def rev_scope(show_url):
        for brief, r in revisions.items():
            if r[1] == show_url:
                return r[2], r[3], r[4], r[5], ["Grp A: $100 - $5,000"]
        raise RuntimeError("unknown revision")

    bc._revision_scope = rev_scope
    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    check("BC reseed: no notifications", len(REC.sent) == 0)
    check("BC reseed: one info", len(REC.infos) == 1)
    check("BC reseed: 2 docs", db["bugcrowd"].count_documents({}) == 2)
    doc_a = db["bugcrowd"].find_one({"programName": "Prog A"})
    check("BC seed scopes", doc_a["inScope"] == ["a.com"]
          and doc_a["outOfScope"] == ["bad.com"])
    check("BC seed changelog date",
          doc_a["lastChangelogAt"] == "2026-10-06T19:56:36.194Z")
    REC.reset()

    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    check("BC idempotent", len(REC.sent) == 0 and len(REC.infos) == 0)

    # bounty change via directory
    dir_items[0]["rewardSummary"] = {"minReward": "$200", "maxReward": "$9,000"}
    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    a = by_name("Prog A")
    check("BC bounty change alerted", a is not None
          and a["reward"]["max"] == "$9,000")
    ok, text = msg_looks_clean(a)
    check("BC message format (name+date+link)", ok)
    REC.reset()

    # changelog sweep: scope change on Prog A
    revisions["/engagements/prog-a"] = ("2026-10-13T10:00:00.000Z",
                                        "/engagements/prog-a/cl-2",
                                        ["a.com", "b.com"], [], "In progress",
                                        "brief-v1")
    bc.pick_stale = lambda db, p, k: [db[p].find_one(
        {"programName": "Prog A"})["programKey"]]
    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    a = by_name("Prog A")
    check("BC scope added via sweep", a is not None
          and a["newInScope"] == ["b.com"])
    check("BC sweep date from changelog",
          a["updated_at"] == "2026-10-12 21:43 Cairo")
    REC.reset()

    # sweep with brief-text change -> desc_changed, no vague note
    revisions["/engagements/prog-a"] = ("2026-10-13T11:00:00.000Z",
                                        "/engagements/prog-a/cl-3",
                                        ["a.com", "b.com"], [], "In progress",
                                        "brief-v2")
    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    a = by_name("Prog A")
    check("BC description change detected", a is not None
          and a.get("desc_changed") is True and a["newInScope"] == [])
    REC.reset()

    # removed: changelog probe confirms the program is really gone
    orig_api_get = bc.api_get
    bc.api_get = lambda url, retries=2, headers=None, timeout=30, params=None:         {"errors": [{"detail": "Not found"}]}  # changelog 404 = really gone
    bc._latest_revision = lambda brief: (None, None, "")
    dir_items.pop(1)
    bc.pick_stale = lambda db, p, k: []
    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    b = by_name("Prog B")
    check("BC confirmed removal alerted", b is not None and b["isRemoved"])
    bc.api_get = orig_api_get
    REC.reset()

    # flake: directory lost the program but its changelog still answers
    # -> removal deferred, doc kept
    db2 = fresh_db()
    dir_items2 = [dict(dir_items[0])]
    bc._fetch_directory = lambda: (list(dir_items2), True)
    revisions2 = {"/engagements/prog-a": ("2026-10-06T19:56:36.194Z",
                                          "/engagements/prog-a/cl-1",
                                          ["a.com"], [], "In progress",
                                          "b")}
    bc._latest_revision = lambda brief: revisions2[brief][:2] + ("brief",)
    bc._revision_scope = lambda show: (["a.com"], [], "In progress", "b", [])
    # seed: two programs, live scan only returns one
    db2["bugcrowd"].insert_many([
        {"programKey": "key-b", "programName": "Ghost B",
         "programURL": "https://bugcrowd.com/engagements/ghost-b",
         "programType": "rdp", "inScope": [], "outOfScope": [],
         "reward": {}, "statusLabel": "", "briefText": "b",
         "rewardRanges": [], "lastChangelogAt": "2026-10-01T00:00:00.000Z"},
    ])
    _saved = bc.api_get
    bc.api_get = lambda url, retries=2, headers=None, timeout=30, params=None:         {"changelogs": [{"id": "x"}]}  # still answers = directory flake
    db2["_meta"].insert_one({"programKey": "bugcrowd_live_resynced",
                             "programs": 2})  # not a reseed
    bc.check_bugcrowd("./tmp/", "hook", False, db2, CONFIG)
    bc.api_get = _saved
    check("BC flake removal deferred (no alert)",
          by_name("Ghost B") is None)
    check("BC flake doc kept", db2["bugcrowd"].count_documents({}) == 2)
    REC.reset()

    # None-baseline sweep: silent re-baseline, no months-old alert
    db3 = fresh_db()
    dir_items3 = [{"name": "Old Prog", "briefUrl": "/engagements/old-p",
                   "logoUrl": "l",
                   "rewardSummary": {"minReward": "$1", "maxReward": "$2"},
                   "accessStatus": "open", "isDemo": False,
                   "isBanned": False}]
    bc._fetch_directory = lambda: (list(dir_items3), True)
    bc._latest_revision = lambda brief: ("2026-05-27T11:26:00.000Z",
                                         "/engagements/old-p/cl-x", "brief")
    bc._revision_scope = lambda show: (["z.com"], [], "In progress", "b", [])
    db3["bugcrowd"].insert_one({
        "programKey": bc.generate_program_key(
            "Old Prog", "https://bugcrowd.com/engagements/old-p"),
        "programName": "Old Prog",
        "programURL": "https://bugcrowd.com/engagements/old-p",
        "programType": "rdp", "inScope": ["z.com"], "outOfScope": [],
        "reward": {"min": "$1", "max": "$2"}, "statusLabel": "In progress",
        "briefText": "b", "rewardRanges": [], "lastChangelogAt": None})
    bc.check_bugcrowd("./tmp/", "hook", False, db3, CONFIG)
    check("BC None-baseline stays silent", len(REC.sent) == 0)
    doc = db3["bugcrowd"].find_one({"programName": "Old Prog"})
    check("BC None-baseline stored silently",
          doc["lastChangelogAt"] == "2026-05-27T11:26:00.000Z")
    REC.reset()


# --------------------------------------------------------------- hackerone
def h1_scenario():
    print("== HackerOne: reseed, state change, scope sweep, removed ==")
    db = fresh_db()
    teams = [
        {"name": "Prog X", "handle": "progx", "offers_bounties": True,
         "submission_state": "open"},
        {"name": "Prog Y", "handle": "progy", "offers_bounties": False,
         "submission_state": "paused"},
    ]
    scopes = {
        "progx": ({"id1": "x.com (InScope)\n", "id2": "y.com (OutOfScope)\n"},
                  "2026-10-10T12:00:00.000Z",
                  {"id1": "2026-10-10T12:00:00.000Z",
                   "id2": "2026-10-10T12:00:00.000Z"}),
        "progy": ({"id3": "z.com (InScope)\n"}, "", {"id3": ""}),
    }
    h1._fetch_directory = lambda: list(teams)
    h1._fetch_scopes = lambda h: scopes[h]
    h1.check_hackerone("./tmp/", "hook", False, db, CONFIG)
    check("H1 reseed: no notifications", len(REC.sent) == 0)
    check("H1 reseed: one info", len(REC.infos) == 1)
    check("H1 reseed: 2 docs", db["hackerone"].count_documents({}) == 2)
    REC.reset()

    h1.check_hackerone("./tmp/", "hook", False, db, CONFIG)
    check("H1 idempotent", len(REC.sent) == 0 and len(REC.infos) == 0)

    # submission state change (paused -> open)
    teams[1]["submission_state"] = "open"
    h1.check_hackerone("./tmp/", "hook", False, db, CONFIG)
    y = by_name("Prog Y")
    check("H1 state change structured", y is not None
          and y["status_old"] == "paused" and y["status_new"] == "open")
    REC.reset()

    # scope sweep: add + text change on Prog X
    scopes["progx"] = ({"id1": "x.com (InScope)\nCHANGED",
                        "id3": "new.com (InScope)\n"},
                       "2026-10-13T09:00:00.000Z",
                       {"id1": "2026-10-13T09:00:00.000Z",
                        "id3": "2026-10-13T09:00:00.000Z"})
    h1.pick_stale = lambda db, p, k: [db[p].find_one(
        {"programName": "Prog X"})["programKey"]]
    h1.check_hackerone("./tmp/", "hook", False, db, CONFIG)
    x = by_name("Prog X")
    check("H1 scope added", x is not None
          and len(x["newScope"]) == 1 and "new.com" in x["newScope"][0])
    check("H1 scope changed detected", len(x["changedScope"]) == 1)
    check("H1 scope removed detected", len(x["removedScope"]) == 1)
    check("H1 date from scope updated_at",
          x["updated_at"] == "2026-10-12 21:43 Cairo")
    ok, text = msg_looks_clean(x)
    check("H1 message format (name+date+link)", ok)
    REC.reset()

    # timestamp-only touch -> names the touched asset, no vague note
    scopes["progx"] = ({"id1": "x.com (InScope)\nCHANGED",
                        "id3": "new.com (InScope)\n"},
                       "2026-10-13T11:00:00.000Z",
                       {"id1": "2026-10-13T11:00:00.000Z",
                        "id3": "2026-10-13T09:00:00.000Z"})
    h1.check_hackerone("./tmp/", "hook", False, db, CONFIG)
    x = by_name("Prog X")
    check("H1 touched-asset note", x is not None
          and "H1 logged updates on: x.com" in x["note"])
    REC.reset()

    # removed (state -> disabled, still exists)
    h1.pick_stale = lambda db, p, k: []
    teams[1]["submission_state"] = "disabled"
    h1.check_hackerone("./tmp/", "hook", False, db, CONFIG)
    y = by_name("Prog Y")
    check("H1 closed-not-deleted note", y is not None and y["isRemoved"]
          and "closed/disabled" in y["note"])
    REC.reset()


# ---------------------------------------------------------------- intigriti
def ig_scenario():
    print("== Intigriti: reseed, bounty, table-diff, updated-note, removed ==")
    db = fresh_db()
    programs = [
        {"name": "IG One", "handle": "igone", "companyHandle": "co1",
         "programId": "pid-1",
         "maxBounty": {"value": 4000.0, "currency": "EUR"},
         "minBounty": {"value": 50.0, "currency": "EUR"}, "status": 3,
         "lastUpdatedAt": 1791822135},
        {"name": "IG Two", "handle": "igtwo", "companyHandle": "co2",
         "maxBounty": {"value": 0, "currency": "EUR"},
         "minBounty": {"value": 0, "currency": "EUR"}, "status": 3,
         "lastUpdatedAt": 1791820097},
    ]
    previews = {
        "co1/igone": {"bountyTable": {"content": {"currency": "EUR",
                      "bountyRows": [{"bountyRanges": [
                          {"minScore": 1, "maxScore": 4,
                           "minBounty": {"value": 150, "currency": "EUR"},
                           "maxBounty": {"value": 150, "currency": "EUR"}}]}]}},
                      "description": "desc-v1"},
    }
    domains = {"pid-1": [
        {"id": "d1", "endpoint": "one.com", "description": "main"},
        {"id": "d2", "endpoint": "two.com", "description": ""},
    ]}
    preview_broken = [False]

    def fake_get(url, retries=2, headers=None, timeout=30, params=None):
        if "/external/researcher" in url:
            pid = url.rsplit("/", 1)[1]
            return {"domains": {"content": domains.get(pid, [])}}
        if "/preview" in url:
            if preview_broken[0]:
                raise RuntimeError("preview down")
            key = "/".join(url.split("/")[-3:-1])
            p = previews.get(key) or {"bountyTable": None,
                                      "description": "d-" + key}
            return dict(p)
        return list(programs)

    ig.api_get = fake_get
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    check("IG reseed: no notifications", len(REC.sent) == 0)
    check("IG reseed: one info", len(REC.infos) == 1)
    doc_one = db["intigriti"].find_one({"programName": "IG One"})
    check("IG reseed stores bountyTable baseline",
          doc_one.get("bountyTable") is not None
          and doc_one.get("description") == "desc-v1")
    check("IG reseed stores domains baseline",
          doc_one.get("domains") is not None
          and set(doc_one["domains"]) == {"d1", "d2"})
    REC.reset()

    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    check("IG idempotent", len(REC.sent) == 0 and len(REC.infos) == 0)

    # bounty table change on timestamp bump -> precise diff, no vague note
    previews["co1/igone"]["bountyTable"]["content"]["bountyRows"][0][
        "bountyRanges"][0]["maxBounty"]["value"] = 300
    programs[0]["lastUpdatedAt"] = 1791908535
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    one = by_name("IG One")
    check("IG table change detected", one is not None
          and one.get("bounty_table_new") == ["Score 1-4: 300 EUR"])
    from modules.notifier.telegram import build_message_text
    text = build_message_text(one)
    check("IG table rendered as +/- lines",
          "<code>- Score 1-4: 150 EUR</code>" in text
          and "<code>+ Score 1-4: 300 EUR</code>" in text)
    check("IG no vague note when diff known", one["note"] == "")
    REC.reset()

    # domain add/remove/edit on timestamp bump -> precise scope diff
    REC.reset()
    domains["pid-1"] = [
        {"id": "d1", "endpoint": "one.com", "description": "main EDITED"},
        {"id": "d3", "endpoint": "three.com", "description": "new"},
    ]
    programs[0]["lastUpdatedAt"] = 1792000000
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    one = by_name("IG One")
    check("IG domain added", one is not None and len(one["newScope"]) == 1
          and "three.com" in one["newScope"][0])
    check("IG domain removed", len(one["removedScope"]) == 1
          and "two.com" in one["removedScope"][0])
    check("IG domain edited", len(one["changedScope"]) == 1
          and "EDITED" in one["changedScope"][0]["new"])
    ok, text = msg_looks_clean(one)
    check("IG domain card format", ok)
    REC.reset()

    # timestamp bump with nothing visibly changed -> clear generic note
    REC.reset()
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    check("IG no re-alert loop (same timestamp)", len(REC.sent) == 0)
    previews["co1/igone"]["description"] = "desc-v2"
    programs[0]["lastUpdatedAt"] = 1791994935
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    one = by_name("IG One")
    check("IG description change detected", one is not None
          and one.get("desc_changed") is True)
    REC.reset()
    previews["co1/igone"]["description"] = "desc-v2"
    programs[0]["lastUpdatedAt"] = 1792081335
    preview_broken[0] = True  # preview endpoint down
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    one = by_name("IG One")
    check("IG internal-update note when nothing visible",
          one is not None and "internal update" in one["note"])
    REC.reset()

    # removed
    programs.pop(1)
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    two = by_name("IG Two")
    check("IG removed alerted", two is not None and two["isRemoved"])
    REC.reset()


def telegram_scenario():
    print("== Telegram: channel routing + old->new rendering ==")
    import modules.notifier.telegram as tg
    orig_channel_chat_id = tg.channel_chat_id
    posts = []  # (chat_id, reply_markup)
    tg._post = lambda text, token, chat_id, reply_markup=None: \
        posts.append((chat_id, reply_markup))
    data = {
        "platformName": "YesWeHack", "isRemoved": False,
        "isNewProgram": False, "programName": "Test Program",
        "programType": "rdp", "programURL": "https://yeswehack.com/programs/t",
        "reward": {"min": "200 EUR", "max": "9000 EUR"},
        "reward_old": {"min": "100 EUR", "max": "5000 EUR"},
        "newType": "rdp", "type_old": "vdp",
        "newInScope": ["new.com"], "removeInScope": ["old.com"],
        "note": "", "updated_at": "2026-10-13 02:00 Cairo",
        "detected_at": "2026-10-13 02:00 Cairo",
    }
    text = tg.build_message_text(data)
    check("bounty old->new rendered",
          "Bounty</b>: 100 EUR – 5000 EUR ➜ 200 EUR – 9000 EUR" in text)
    check("type old->new rendered", "Type</b>: vdp ➜ rdp" in text)
    check("date line present", "🕒 <b>Updated:</b>" in text)
    check("WHAT CHANGED section present", "WHAT CHANGED" in text)

    ig_data = dict(data, platformName="Intigriti", status_old=4,
                   status_new=3, reward_old=None, newReward=None,
                   reward=[], newType="", type_old=None)
    ig_text = tg.build_message_text(ig_data)
    check("intigriti status words rendered",
          "Status</b>: Paused ➜ Open" in ig_text)

    tg.channel_chat_id = lambda: "-100channel"
    tg.send(data)
    check("alert goes to private + channel",
          len([p for p in posts if p[0] == "-100channel"]) == 1
          and len(posts) == 2)
    check("inline button carries the program URL",
          all(p[1] and p[1]["inline_keyboard"][0][0]["url"]
              == data["programURL"] for p in posts))
    posts.clear()
    tg.channel_chat_id = lambda: None
    tg.quiet(100)
    check("quiet message stays private only", len(posts) == 1
          and posts[0][0] != "-100channel" and posts[0][1] is None)
    tg.channel_chat_id = lambda: None
    posts.clear()
    tg.send(data)
    check("disabled channel = private only", len(posts) == 1)

    tg.channel_chat_id = orig_channel_chat_id
    resolved = orig_channel_chat_id()
    check("real channel_chat_id: resolves live channel or disabled",
          resolved is None or str(resolved).startswith("-100"))


def listener_scenario():
    print("== Listener: owner answered, strangers silenced ==")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "bot_listener", ".github/bot_listener.py")
    bl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bl)
    db = fresh_db()
    for name, _ in bl.PLATFORMS:
        db[name].insert_one({"programKey": "x"})
    cfg = {"telegram": {"bot_token": "T", "chat_id": "111",
                        "channel_chat_id": "-100ch"},
           "mongoDB": {"uri": "x", "database": "x"}}
    posted = []
    owner_msg = {"chat": {"id": 111}, "text": "/start"}
    stranger_msg = {"chat": {"id": 999}, "text": "/start"}
    bl._handle(owner_msg, "T", "111", db, cfg,
               lambda t, tok, cid: posted.append((cid, t)))
    check("owner /start answered", len(posted) == 1
          and "PEBO Watcher" in posted[0][1] and posted[0][0] == "111")
    bl._handle(stranger_msg, "T", "111", db, cfg,
               lambda t, tok, cid: posted.append((cid, t)))
    check("stranger /start ignored (no reply)", len(posted) == 1)
    posted.clear()
    bl._handle({"chat": {"id": 111}, "text": "hello"}, "T", "111", db, cfg,
               lambda t, tok, cid: posted.append((cid, t)))
    check("owner non-command gets hint", len(posted) == 1
          and "/status" in posted[0][1])


def main():
    install_common()
    bc_scenario()
    h1_scenario()
    ig_scenario()
    telegram_scenario()
    listener_scenario()
    print()
    if FAILURES:
        print(f"SELFTEST FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("SELFTEST PASSED (all scenarios)")


if __name__ == "__main__":
    main()
