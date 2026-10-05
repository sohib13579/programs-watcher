"""Daily Top-10 fired from INSIDE the watch chain — scheduler-independent.

Runs top_targets.run() once per UTC day (after 07:00 so it lands ~10:23
Cairo), recording the date in _meta.last_top10. Fails open: if sending
fails the date is not recorded and the next cycle retries.
"""
import sys
from datetime import datetime, timezone

import yaml
from pymongo import MongoClient

sys.path.insert(0, ".")


def main():
    now = datetime.now(timezone.utc)
    if now.hour < 7:
        return
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.full_load(f)
    client = MongoClient(cfg["mongoDB"]["uri"], serverSelectionTimeoutMS=20000)
    meta = client[cfg["mongoDB"]["database"]]["_meta"]
    today = now.date().isoformat()
    doc = meta.find_one({"programKey": "last_top10"})
    if doc and doc.get("date") == today:
        client.close()
        return
    client.close()

    import top_targets
    top_targets.run()  # raises on failure -> caller retries next cycle

    client = MongoClient(cfg["mongoDB"]["uri"], serverSelectionTimeoutMS=20000)
    meta = client[cfg["mongoDB"]["database"]]["_meta"]
    meta.update_one({"programKey": "last_top10"},
                    {"$set": {"date": today}}, upsert=True)
    client.close()
    print("daily top-10 delivered for", today)


if __name__ == "__main__":
    main()
