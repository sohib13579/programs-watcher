"""Chain guard for the self-retriggering watcher.

guard: exit 9 if a watcher chain checked in recently (a live chain exists);
       exit 0 otherwise (caller should start a new chain).
touch: record that a chain check just happened.

State lives in Mongo `_meta` so it survives across runners.
Usage: python chain_guard.py guard|touch
"""
import sys
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
        last = meta.find_one({"programKey": "chain_last_check"})
        alive = last and (now - last["at"]) < timedelta(
            minutes=MAX_CHAIN_SILENCE_MIN)
        if alive:
            print("chain already alive (last check %s) — not starting another"
                  % last["at"])
            sys.exit(9)
        print("no live chain — starting")
        return
    raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
