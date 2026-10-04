import yaml
from pymongo import MongoClient
import os
import shutil
import datetime
from modules.platforms.bugcrowd import check_bugcrowd
from modules.platforms.hackerone import check_hackerone
from modules.platforms.intigriti import check_intigriti
from modules.platforms.yeswehack import check_yeswehack
import modules.notifier.discord as discord_notifier


# load config file
with open("config.yml", "r") as ymlfile:
    cfg = yaml.full_load(ymlfile)
discord_webhook = cfg['discordWebhook']['programs_watcher']
platforms = {}
for platform in cfg['platforms']:
    platforms[platform['name']] = {
        'url': platform['url'],
        'notifications': platform['notifications'],
        'monitor': platform['monitor']
    }

# connect to MongoDB
client = MongoClient(cfg['mongoDB']['uri'])
dbName = cfg['mongoDB']['database']
db = client[dbName]
first_time = False
if dbName not in client.list_database_names():
    first_time = True

# Check ./tmp directory exists
tmp_dir = f"./tmp/"
if os.path.exists(tmp_dir):
    shutil.rmtree(tmp_dir)
    os.mkdir(tmp_dir)
else:
    os.mkdir(tmp_dir)

# checking bugcrowd
check_bugcrowd(tmp_dir, discord_webhook, first_time, db, platforms['bugcrowd'])

# checking hackerone
check_hackerone(tmp_dir, discord_webhook, first_time, db, platforms['hackerone'])

# checking intigriti
check_intigriti(tmp_dir, discord_webhook, first_time, db, platforms['intigriti'])

# checking yeswehack
check_yeswehack(tmp_dir, discord_webhook, first_time, db, platforms['yeswehack'])

# liveness: quiet notice when nothing changed, plus a 6-hour heartbeat
total_programs = sum(db[name].count_documents({}) for name in platforms)
meta = db['_meta']
now = datetime.datetime.utcnow()

if not first_time and discord_notifier.notifications_sent == 0:
    last_quiet = meta.find_one({'programKey': 'last_quiet'})
    quiet_gap = (now - last_quiet['at']).total_seconds() if last_quiet else 1e9
    if quiet_gap >= 25 * 60:  # extra schedule slots must not spam quiet msgs
        discord_notifier.send_quiet_message(discord_webhook, total_programs)
        meta.update_one({'programKey': 'last_quiet'},
                        {'$set': {'at': now}}, upsert=True)

heartbeat_cfg = cfg.get('heartbeat') or {}
if heartbeat_cfg.get('enabled', True):
    every_hours = float(heartbeat_cfg.get('every_hours', 6))
    last = meta.find_one({'programKey': 'last_heartbeat'})
    due = last is None or (now - last['at']).total_seconds() >= every_hours * 3600
    if due:
        discord_notifier.send_heartbeat_message(discord_webhook, now, total_programs)
        meta.update_one({'programKey': 'last_heartbeat'},
                        {'$set': {'at': now}}, upsert=True)

# Clean up resources and remove tmp_dir
client.close()
shutil.rmtree(tmp_dir)

if first_time:
    discord_notifier.send_startup_message(discord_webhook)