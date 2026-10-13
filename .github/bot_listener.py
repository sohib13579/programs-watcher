"""Telegram inbound mini-handler: replies ONLY to the operator.

Long-polls getUpdates for the lifetime of the chain job (started in the
background by watcher.yml). The operator's chat_id comes from
config.yml `telegram.chat_id` (CI: TELEGRAM_CHAT_ID secret). Any other
chat — a stranger /start-ing the bot, groups it was dragged into — is
silently ignored: the update offset still advances so it is never
re-read, but NO reply is ever sent.

Commands (owner only): /start, /status -> live status summary.
The update offset persists in Mongo `_meta.tg_update_offset` so messages
sent while the job was down are answered on the next job.
"""
import datetime
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

# running as `.github/bot_listener.py` puts .github/ (not the repo root)
# on sys.path — fix it or the modules.* imports die instantly (silent
# listener death, the bug that killed 2026-10-13's first deploy)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

POLL_TIMEOUT_S = 25
ERROR_SLEEP_S = 15
PLATFORMS = (("bugcrowd", "🟢 Bugcrowd"), ("hackerone", "⚫ HackerOne"),
             ("intigriti", "🟣 Intigriti"), ("yeswehack", "🟠 YesWeHack"))


def _load_cfg():
    with open("config.yml", "r", encoding="utf-8") as f:
        return yaml.full_load(f)


def _owner_and_token(cfg):
    tg = cfg.get("telegram") or {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
    owner = str(os.environ.get("TELEGRAM_CHAT_ID", tg.get("chat_id", "")))
    return token, owner


def _mongo_db(cfg):
    import pymongo
    client = pymongo.MongoClient(cfg["mongoDB"]["uri"],
                                 serverSelectionTimeoutMS=20000)
    return client[cfg["mongoDB"]["database"]]


def _summary(db, cfg):
    counts = {name: db[name].count_documents({})
              for name, _ in PLATFORMS}
    total = sum(counts.values())
    stamp = db["_meta"].find_one({"programKey": "chain_last_check"})
    ago = "؟"
    if stamp:
        secs = (datetime.datetime.utcnow()
                - stamp["at"].replace(tzinfo=None)).total_seconds()
        ago = f"{int(secs // 60)} دقيقة" if secs < 7200 \
            else f"{int(secs // 3600)} ساعة"
    tg = cfg.get("telegram") or {}
    channel = (os.environ.get("TELEGRAM_CHANNEL_ID",
                              tg.get("channel_chat_id", "")) or "")
    ch_line = "✅ متفعلة" if channel and not str(channel).startswith("<") \
        else "❌ مش متفعلة"
    per_line = " · ".join(f"{label} {counts[name]}"
                          for name, label in PLATFORMS)
    return (
        "👋 <b>المراقب شغّال — PEBO Watcher</b>\n\n"
        f"📡 بيراقب <b>{total}</b> برنامج\n"
        f"<code>{per_line}</code>\n\n"
        f"🕒 آخر فحص كامل: <b>{ago} مضت</b>\n"
        f"📢 القناة: {ch_line}\n\n"
        "🔔 التنبيهات الحقيقية بتوصلك هنا + في القناة، "
        "ورسايل (مفيش تحديث) والأخطاء هنا بس.")


def _handle(msg, token, owner, db, cfg, post_fn):
    """One incoming Telegram message. Strangers get absolute silence."""
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    if not chat_id or chat_id != owner:
        return
    text = (msg.get("text") or "").strip()
    if text.startswith("/start") or text.startswith("/status"):
        post_fn(_summary(db, cfg), token, chat_id)
    elif text:
        post_fn("الأوامر المتاحة: /start أو /status", token, chat_id)


def main():
    cfg = _load_cfg()
    token, owner = _owner_and_token(cfg)
    db = None
    for attempt in range(5):
        try:
            db = _mongo_db(cfg)
            doc = db["_meta"].find_one({"programKey": "tg_update_offset"})
            offset = (doc or {}).get("offset", 0)
            break
        except Exception as exc:
            print(f"[listener] mongo startup failed ({attempt + 1}/5): "
                  f"{exc}", flush=True)
            time.sleep(15)
    if db is None:
        raise SystemExit("[listener] mongo unreachable — giving up")
    print(f"[listener] running — owner={owner}, offset={offset}", flush=True)

    from modules.notifier.telegram import _post
    while True:
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT_S,
                        "allowed_updates": json.dumps(["message"])},
                timeout=POLL_TIMEOUT_S + 15)
            body = resp.json()
            if not body.get("ok"):
                # e.g. 409 if another poller briefly exists — back off
                print("[listener] api not ok:",
                      str(body)[:200], flush=True)
                time.sleep(ERROR_SLEEP_S)
                continue
            updates = body.get("result") or []
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    _handle(upd.get("message") or {}, token, owner, db,
                            cfg, _post)
                except Exception as exc:
                    print("[listener] handle error:", exc, flush=True)
            if updates:
                db["_meta"].update_one(
                    {"programKey": "tg_update_offset"},
                    {"$set": {"offset": offset}}, upsert=True)
        except Exception as exc:
            print("[listener] poll error:", exc, flush=True)
            time.sleep(ERROR_SLEEP_S)


if __name__ == "__main__":
    main()
