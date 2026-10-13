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
    ok_link = "Open program page" in text
    return ok_name and ok_date and ok_link, text


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
                                ["a.com"], ["bad.com"], "In progress"),
        "/engagements/prog-b": (None, None, [], [], ""),
    }
    bc._fetch_directory = lambda: list(dir_items)
    bc._latest_revision = lambda brief: revisions[brief][:2]

    def rev_scope(show_url):
        for brief, r in revisions.items():
            if r[1] == show_url:
                return r[2], r[3], r[4]
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
                                        ["a.com", "b.com"], [], "In progress")
    bc.pick_stale = lambda db, p, k: [db[p].find_one(
        {"programName": "Prog A"})["programKey"]]
    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    a = by_name("Prog A")
    check("BC scope added via sweep", a is not None
          and a["newInScope"] == ["b.com"])
    check("BC sweep date from changelog",
          a["updated_at"] == "2026-10-12 21:43 Cairo")
    REC.reset()

    # sweep with no scope change -> generic note
    revisions["/engagements/prog-a"] = ("2026-10-13T11:00:00.000Z",
                                        "/engagements/prog-a/cl-3",
                                        ["a.com", "b.com"], [], "In progress")
    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    a = by_name("Prog A")
    check("BC brief-only update note", a is not None
          and "brief updated" in a["note"])
    REC.reset()

    # removed
    dir_items.pop(1)
    bc.pick_stale = lambda db, p, k: []
    bc.check_bugcrowd("./tmp/", "hook", False, db, CONFIG)
    b = by_name("Prog B")
    check("BC removed alerted", b is not None and b["isRemoved"])
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
                  "2026-10-10T12:00:00.000Z"),
        "progy": ({"id3": "z.com (InScope)\n"}, ""),
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
    check("H1 state change note", y is not None
          and "submission state: paused -> open" in y["note"])
    REC.reset()

    # scope sweep: add + text change on Prog X
    scopes["progx"] = ({"id1": "x.com (InScope)\nCHANGED",
                        "id3": "new.com (InScope)\n"},
                       "2026-10-13T09:00:00.000Z")
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
    print("== Intigriti: reseed, bounty, updated-note, removed ==")
    db = fresh_db()
    programs = [
        {"name": "IG One", "handle": "igone", "companyHandle": "co1",
         "maxBounty": {"value": 4000.0, "currency": "EUR"},
         "minBounty": {"value": 50.0, "currency": "EUR"}, "status": 3,
         "lastUpdatedAt": 1791822135},
        {"name": "IG Two", "handle": "igtwo", "companyHandle": "co2",
         "maxBounty": {"value": 0, "currency": "EUR"},
         "minBounty": {"value": 0, "currency": "EUR"}, "status": 3,
         "lastUpdatedAt": 1791820097},
    ]
    ig.api_get = lambda url, retries=2, headers=None, timeout=30, params=None: \
        list(programs)
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    check("IG reseed: no notifications", len(REC.sent) == 0)
    check("IG reseed: one info", len(REC.infos) == 1)
    check("IG reseed: 2 docs", db["intigriti"].count_documents({}) == 2)
    REC.reset()

    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    check("IG idempotent", len(REC.sent) == 0 and len(REC.infos) == 0)

    # bounty change
    programs[0]["maxBounty"]["value"] = 7000.0
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    one = by_name("IG One")
    check("IG bounty change alerted", one is not None
          and one["newReward"]["max"] == "7000.0 EUR")
    ok, text = msg_looks_clean(one)
    check("IG message format (name+date+link)", ok)
    REC.reset()

    # timestamp-only update (no re-alert loop)
    programs[0]["lastUpdatedAt"] = 1791908535
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    one = by_name("IG One")
    check("IG updated note", one is not None
          and "program updated" in one["note"])
    REC.reset()
    ig.check_intigriti("./tmp/", "hook", False, db, CONFIG)
    check("IG no re-alert loop", len(REC.sent) == 0)
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
    posts = []
    tg._post = lambda text, token, chat_id: posts.append(chat_id)
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

    tg.channel_chat_id = lambda: "-100channel"
    tg.send(data)
    check("alert goes to private + channel", posts.count("-100channel") == 1
          and len(posts) == 2)
    posts.clear()
    tg.channel_chat_id = lambda: None
    tg.quiet(100)
    check("quiet message stays private only", len(posts) == 1
          and "-100channel" not in posts)
    # a placeholder resolves to None inside the real channel_chat_id,
    # so send() sees None and stays private-only
    tg.channel_chat_id = lambda: None
    posts.clear()
    tg.send(data)
    check("placeholder channel = disabled", len(posts) == 1)

    # restore the REAL channel_chat_id: local config.yml holds the
    # placeholder, so it must resolve to None (disabled)
    tg.channel_chat_id = orig_channel_chat_id
    check("real channel_chat_id: placeholder -> None",
          orig_channel_chat_id() is None)


def main():
    install_common()
    bc_scenario()
    h1_scenario()
    ig_scenario()
    telegram_scenario()
    print()
    if FAILURES:
        print(f"SELFTEST FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("SELFTEST PASSED (all scenarios)")


if __name__ == "__main__":
    main()
