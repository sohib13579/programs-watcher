"""Chain guard for the self-retriggering watcher.

guard: exit 0 to start/keep a chain; exit 9 to stand down (a live chain
       owns the watch). Takes over when the owner's stamp goes stale.
touch: record that a chain check just happened.

State lives in Mongo `_meta` so it survives across runners.
Usage: python chain_guard.py guard|touch
"""
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml
from pymongo import MongoClient

MAX_CHAIN_SILENCE_MIN = 20  # a live chain checks every ~15 min
TAKEOVER_AT_MIN = 23        # must exceed MAX + guard startup lag
MAX_WAIT_MIN = 30


def _meta():
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.full_load(f)
    client = MongoClient(cfg["mongoDB"]["uri"], serverSelectionTimeoutMS=20000)
    meta = client[cfg["mongoDB"]["database"]]["_meta"]
    return meta


def main():
    mode = sys.argv[1]
    meta = _meta()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if mode == "touch":
        meta.update_one({"programKey": "chain_last_check"},
                        {"$set": {"at": now}}, upsert=True)
        return
    if mode == "guard":
        first = meta.find_one({"programKey": "chain_last_check"})
        first_at = first["at"] if first else None
        deadline = now + timedelta(minutes=MAX_WAIT_MIN)
        while now < deadline:
            last = meta.find_one({"programKey": "chain_last_check"})
            at = last["at"] if last else None
            if at != first_at:
                # owner touched the stamp AFTER we started waiting: alive,
                # and it (or its re-arm) owns the chain — stand down
                print("owner touched stamp while we waited — alive, standing down")
                sys.exit(9)
            if at is None or (now - at) >= timedelta(minutes=TAKEOVER_AT_MIN):
                print("no live chain (stamp %s) — taking over" % at)
                return
            print(f"stamp unchanged ({at}) — waiting... now={now:%H:%M}")
            time.sleep(60)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        print("waited out the window with a fresh stamp — owner alive")
        sys.exit(9)
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
