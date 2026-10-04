"""CI helper: overwrites the Discord webhook and Mongo URI in config.yml
from environment variables, leaving the platform blocks untouched."""
import os

import yaml

with open("config.yml", "r", encoding="utf-8") as f:
    cfg = yaml.full_load(f)

cfg["discordWebhook"]["programs_watcher"] = os.environ["DISCORD_WEBHOOK"]
cfg["mongoDB"]["uri"] = os.environ["MONGO_URI"]

# optional Telegram side-channel (secrets may be empty -> left as placeholder)
tg = cfg.setdefault("telegram", {})
if os.environ.get("TELEGRAM_BOT_TOKEN"):
    tg["bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
if os.environ.get("TELEGRAM_CHAT_ID"):
    tg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"]

with open("config.yml", "w", encoding="utf-8") as f:
    yaml.dump(cfg, f, allow_unicode=True)
