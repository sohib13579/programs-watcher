"""Chain guard for the self-retriggering watcher.

guard: exit 9 if a watcher chain checked in recently (a live chain exists);
       exit 0 otherwise (caller should start a new chain).
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
        # wait up to ~18 min for a dead owner's stamp to go stale; a live
        # owner checks every ~15 min so its stamp never goes stale
        for attempt in range(18):
            last = meta.find_one({"programKey": "chain_last_check"})
            if not last or (now - last["at"]) >= timedelta(
                    minutes=MAX_CHAIN_SILENCE_MIN):
                print("no live chain — starting (waited %d min)" % attempt)
                return
            print(f"chain looks alive ({last['at']}) — waiting... "
                  f"({attempt + 1}/18)")
            time.sleep(60)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
        print("owner still alive after 18 min — it will re-arm itself")
        sys.exit(9)
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
