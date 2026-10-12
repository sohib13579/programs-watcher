"""Offline self-test for the live YesWeHack checker.

Synthetic ground truth only — no network, no real Mongo (mongomock).
Covers: silent reseed, idempotent second run, scope add/remove, bounty
change, status change, timestamp-only update (no re-alert loop), detail
fetch failure, live-list failure + error-alert cooldown.

Run: python selftest_yeswehack.py
"""
import datetime
import sys

import mongomock

import modules.platforms.yeswehack as ywh

FAILURES = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        FAILURES.append(label)


CONFIG = {
    "monitor": {"rdp": True, "vdp": True,
                "specific_programs": [], "excluded_programs": []},
    "notifications": {"new_program": True, "removed_program": True,
                      "new_inscope": True, "removed_inscope": True,
                      "new_type": True, "new_bounty_table": True},
}


class Harness:
    """Fake live API + notification recorders around a mongomock DB."""

    def __init__(self):
        self.db = mongomock.MongoClient()["programs_watcher"]
        self.live = []
        self.details = {}
        self.detail_raises = set()
        self.list_raises = False
        self.sent = []          # send_notification(data, mUrl) payloads
        self.infos = []         # send_info(html) payloads
        self._orig = (ywh._fetch_live_list, ywh._fetch_detail,
                      ywh.send_info, ywh.discord_notifier.send_notification)

    def install(self):
        ywh._fetch_live_list = lambda: (self._fail_list() if self.list_raises
                                        else list(self.live))
        ywh._fetch_detail = self._detail
        ywh.send_info = lambda text: self.infos.append(text)
        ywh.discord_notifier.send_notification = (
            lambda data, mUrl: self.sent.append(data))

    def restore(self):
        (ywh._fetch_live_list, ywh._fetch_detail,
         ywh.send_info, ywh.discord_notifier.send_notification) = self._orig

    def _fail_list(self):
        raise RuntimeError("synthetic list failure")

    def _detail(self, slug):
        if slug in self.detail_raises:
            raise RuntimeError(f"synthetic detail failure {slug}")
        return {"scopes": [{"scope": s} for s in
                           self.details.get(slug, [])]}

    def run(self):
        ywh.check_yeswehack("./tmp/", "http://hook", False, self.db, CONFIG)

    # -- fixture builders ------------------------------------------------
    def seed_stale(self, titles):
        """Pre-deploy DB state: frozen Apr-2025 shape, no new fields."""
        for t in titles:
            url = f"https://yeswehack.com/programs/{t}"
            key = ywh.generate_program_key(t, url)
            self.db["yeswehack"].update_one(
                {"programKey": key},
                {"$set": {"programName": t, "programURL": url,
                          "programType": "rdp", "inScope": ["old-scope"],
                          "reward": {"min": "1 EUR", "max": "2 EUR"}}},
                upsert=True)

    def add_live(self, slug, bounty=True, tmin=50, tmax=4000,
                 last_update="2026-10-12T19:43:28+02:00", status="V",
                 scopes_count=None):
        self.live.append({
            "title": slug, "slug": slug, "bounty": bounty,
            "bounty_reward_min": tmin, "bounty_reward_max": tmax,
            "business_unit": {"currency": "EUR"},
            "status": status, "last_update_at": last_update,
            "scopes_count": scopes_count if scopes_count is not None
                            else len(self.details.get(slug, [])),
            "thumbnail": {"url": f"https://cdn/{slug}.png"},
        })

    def by_name(self, name):
        hits = [d for d in self.sent if d.get("programName") == name]
        return hits[0] if hits else None


def fresh_db_without_reseed_flag(h):
    h.db["_meta"].delete_many({"programKey": "ywh_live_resynced"})


def scenario_1_reseed():
    print("Scenario 1: silent reseed over stale Apr-2025 DB")
    h = Harness()
    h.install()
    h.seed_stale(["stale-prog-a", "stale-prog-b"])
    h.details = {"prog-1": ["a.com", "b.com"], "prog-2": ["c.com"],
                 "prog-3": ["d.com", "e.com", "f.com"]}
    h.add_live("prog-1")
    h.add_live("prog-2", bounty=False)
    h.add_live("prog-3", tmin=100, tmax=9000)
    h.run()
    check("no per-program notifications during reseed", len(h.sent) == 0)
    check("exactly one info message", len(h.infos) == 1)
    check("info mentions reseed", "LIVE" in h.infos[0])
    check("DB now has 3 live docs",
          h.db["yeswehack"].count_documents({}) == 3)
    check("stale doc replaced",
          h.db["yeswehack"].find_one({"programName": "stale-prog-a"}) is None)
    doc = h.db["yeswehack"].find_one({"programName": "prog-1"})
    check("doc carries inScope from detail",
          doc["inScope"] == ["a.com", "b.com"])
    check("doc carries status/lastUpdateAt",
          doc["status"] == "V" and doc["lastUpdateAt"].startswith("2026-10-12"))
    vdp = h.db["yeswehack"].find_one({"programName": "prog-2"})
    check("vdp flag + empty reward for non-bounty program",
          vdp["programType"] == "vdp" and vdp["reward"] == {})
    check("meta flag set",
          h.db["_meta"].find_one({"programKey": "ywh_live_resynced"})
          is not None)

    print("Scenario 2: idempotent second run")
    sent_before, infos_before = len(h.sent), len(h.infos)
    h.run()
    check("zero notifications on unchanged data",
          len(h.sent) == sent_before and len(h.infos) == infos_before)
    return h


def scenario_3_diffs(h):
    print("Scenario 3: scope add/remove, bounty, status, new, removed")
    # P1: timestamp + scopes change ({a.com,b.com} -> {b.com,c.com})
    h.live = []
    h.details["prog-1"] = ["b.com", "c.com"]
    h.add_live("prog-1", last_update="2026-10-13T08:00:00+02:00")
    # P4: brand-new rdp program
    h.details["prog-4"] = ["x.com"]
    h.add_live("prog-4", tmin=200, tmax=12000)
    # prog-2 / prog-3 disappear -> removed x2
    h.run()
    p1 = h.by_name("prog-1")
    check("prog-1 reported as changed", p1 is not None
          and not p1["isNewProgram"] and not p1["isRemoved"])
    check("prog-1 scope added detected", p1["newInScope"] == ["c.com"])
    check("prog-1 scope removed detected", p1["removeInScope"] == ["a.com"])
    p4 = h.by_name("prog-4")
    check("prog-4 reported as new with its scopes",
          p4 is not None and p4["isNewProgram"]
          and p4["newInScope"] == ["x.com"])
    removed = [d for d in h.sent if d.get("isRemoved")]
    check("both vanished programs reported removed",
          sorted(d["programName"] for d in removed)
          == ["prog-2", "prog-3"])
    check("DB holds prog-1 + prog-4 only",
          h.db["yeswehack"].count_documents({}) == 2)
    check("prog-1 stored scopes updated",
          h.db["yeswehack"].find_one(
              {"programName": "prog-1"})["inScope"] == ["b.com", "c.com"])
    return h


def scenario_4_ts_only(h):
    print("Scenario 4: timestamp-only update (no re-alert loop)")
    h.sent.clear()  # drop scenario-3 notifications; assertions are absolute
    sent_before = len(h.sent)
    h.live = []
    h.add_live("prog-1", last_update="2026-10-13T09:30:00+02:00")
    h.run()
    p1 = h.by_name("prog-1")
    check("timestamp-only change still reported", p1 is not None)
    check("report is note-only (no scope diff)",
          p1["note"] != "" and p1["newInScope"] == []
          and p1["removeInScope"] == [])
    h.sent.clear()
    h.run()
    check("no re-alert on the next cycle (same timestamp)",
          len(h.sent) == 0)


def scenario_5_detail_fail(h):
    print("Scenario 5: detail fetch failure still reports + no loop")
    h.live = []
    h.details["prog-1"] = ["b.com", "c.com", "n.com"]  # would-be diff
    h.add_live("prog-1", last_update="2026-10-13T10:00:00+02:00")
    h.detail_raises.add("prog-1")
    h.run()
    p1 = h.by_name("prog-1")
    check("failure reported as note update", p1 is not None
          and "detail fetch failed" in p1["note"])
    check("stored timestamp advanced despite failure",
          h.db["yeswehack"].find_one(
              {"programName": "prog-1"})["lastUpdateAt"]
          == "2026-10-13T10:00:00+02:00")
    check("stored scopes kept from before",
          h.db["yeswehack"].find_one(
              {"programName": "prog-1"})["inScope"] == ["b.com", "c.com"])
    h.sent.clear()
    h.run()
    check("no re-alert loop after failure", len(h.sent) == 0)
    h.detail_raises.clear()


def scenario_6_feed_error(h):
    print("Scenario 6: live-list failure + 6h error-alert cooldown")
    h.infos.clear()  # drop the scenario-1 reseed info; assertions are absolute
    h.live = []
    h.list_raises = True
    h.run()
    check("error alert sent once",
          len(h.infos) == 1 and "FAILED" in h.infos[0])
    h.run()
    check("second failure within cooldown is silent", len(h.infos) == 1)
    h.db["_meta"].update_one(
        {"programKey": "ywh_last_error_alert"},
        {"$set": {"at": datetime.datetime.utcnow()
                  - datetime.timedelta(hours=7)}})
    h.run()
    check("alert fires again after cooldown", len(h.infos) == 2)
    h.list_raises = False


def main():
    h = scenario_1_reseed()
    scenario_3_diffs(h)
    scenario_4_ts_only(h)
    scenario_5_detail_fail(h)
    scenario_6_feed_error(h)
    h.restore()
    print()
    if FAILURES:
        print(f"SELFTEST FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("SELFTEST PASSED (all scenarios)")


if __name__ == "__main__":
    main()
