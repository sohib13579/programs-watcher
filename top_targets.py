"""Daily Top-10 targets per platform, scored from the state already stored
in MongoDB (bounty value + scope size + program type).

HackerOne enrichment (live, via the site's own GraphQL):
  average bounty, reports received in the last 90 days (competition
  pressure per asset), and response/triage times when a team publishes them.
  Any GraphQL failure degrades gracefully to the base score.

Response-speed stats for Bugcrowd/Intigriti/YesWeHack exist on their pages
but those pages are bot-protected (Cloudflare), so they are not fetched.

Score (transparent):
  +20  rdp (real money)          +0  vdp
  +up to 30  bounty: reward_usd / 333, capped at 30
  +up to 40  scope: in-scope asset count x 2, capped at 40
  H1 only:  +up to 15 speed (published first-response/triage time)
            -up to 15 competition: reports_90d / assets pressure

Sends one Discord embed (one field per platform) and a Telegram message.
Run: python top_targets.py   (uses the same config.yml / secrets as main.py)
Self-test: python top_targets.py --selftest
"""
import os
import re
import sys
import time

import yaml

PLATFORMS = ["bugcrowd", "hackerone", "intigriti", "yeswehack"]
LABELS = {
    "bugcrowd": "Bugcrowd",
    "hackerone": "HackerOne",
    "intigriti": "Intigriti",
    "yeswehack": "YesWeHack",
}
TOP_N = 10
H1_ENRICH_CANDIDATES = 15  # enrich only the top base-scored H1 programs


def parse_money_max(reward):
    """Extract the max bounty in USD from stored reward shapes like
    {"min": "$100", "max": "$3,500"} / {"min": "50 USD", "max": "5000 USD"} / {}."""
    if not isinstance(reward, dict):
        return 0.0
    raw = str(reward.get("max", "") or "")
    digits = re.sub(r"[^\d.]", "", raw)
    try:
        return float(digits) if digits else 0.0
    except ValueError:
        return 0.0


def in_scope_count(platform, doc):
    if "inScope" in doc:
        return len(doc.get("inScope") or [])
    return len(doc.get("scope") or {})


def score_doc(platform, doc):
    is_rdp = doc.get("programType") == "rdp"
    bounty_max = parse_money_max(doc.get("reward")) if is_rdp else 0.0
    bounty_points = min(30.0, bounty_max / 333.0)
    scope_points = min(40.0, in_scope_count(platform, doc) * 2.0)
    total = (20.0 if is_rdp else 0.0) + bounty_points + scope_points
    return total, is_rdp, bounty_max


# ---------- HackerOne live enrichment (site GraphQL, graceful fallback) -----

H1_QUERY = ("query($handle:String!){team(handle:$handle){name "
            "reports_received_last_90_days first_response_time triage_time "
            "bounty_time resolution_time average_bounty_lower_amount "
            "average_bounty_upper_amount}}")


def h1_handle(doc):
    m = re.match(r"https://hackerone\.com/([^/?]+)", doc.get("programURL") or "")
    return m.group(1) if m else None


def fetch_h1_metrics(handles, delay=1.5):
    """{handle: metrics dict or None}. Never raises."""
    out = {}
    try:
        import requests
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        s.get("https://hackerone.com/", timeout=30)  # session cookies
    except Exception as e:
        print("H1 enrichment unavailable:", e)
        return {h: None for h in handles}
    for h in handles:
        try:
            r = s.post("https://hackerone.com/graphql",
                       json={"query": H1_QUERY, "variables": {"handle": h}},
                       timeout=30)
            team = (r.json().get("data") or {}).get("team")
            out[h] = team if r.status_code == 200 and team else None
        except Exception:
            out[h] = None
        time.sleep(delay)
    return out


UNITS = {"second": 1 / 86400, "minute": 1 / 1440, "hour": 1 / 24,
         "day": 1.0, "week": 7.0, "month": 30.0, "year": 365.0}


def parse_duration_days(value):
    """Accepts None, seconds (number), or humanized strings like
    '4 days, 18 hours' / 'about 1 month'. Returns float days or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) / 86400.0
    days = 0.0
    found = False
    for num, unit in re.findall(r"([\d.]+)\s*(second|minute|hour|day|week|month|year)",
                                str(value), re.I):
        days += float(num) * UNITS[unit.lower()]
        found = True
    return days if found else None


def speed_points(days):
    if days is None:
        return 0.0
    if days <= 1:
        return 15.0
    if days <= 3:
        return 12.0
    if days <= 7:
        return 9.0
    if days <= 30:
        return 4.0
    return 0.0


def competition_penalty(reports_90d, assets):
    """Crowded program = your duplicate risk. Reports per asset in 90d."""
    if not reports_90d:
        return 0.0
    pressure = reports_90d / max(assets, 1)
    if pressure >= 10:
        return 15.0
    if pressure >= 5:
        return 8.0
    if pressure >= 2:
        return 4.0
    return 0.0


def enrich_score(platform, doc, base_total, metrics):
    """Re-score an H1 doc with live metrics. Returns (total, extras dict)."""
    extras = {"avg_bounty": None, "reports_90d": None, "speed_days": None}
    if not metrics:
        return base_total, extras
    lo, hi = metrics.get("average_bounty_lower_amount"), metrics.get(
        "average_bounty_upper_amount")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > 0:
        avg = (lo + hi) / 2.0
        extras["avg_bounty"] = avg
        is_rdp = doc.get("programType") == "rdp"
        base_total = (20.0 if is_rdp else 0.0) + min(30.0, avg / 333.0) \
            + min(40.0, in_scope_count(platform, doc) * 2.0)
    extras["reports_90d"] = metrics.get("reports_received_last_90_days")
    speed = min(filter(None, (
        parse_duration_days(metrics.get("first_response_time")),
        parse_duration_days(metrics.get("triage_time")))), default=None)
    extras["speed_days"] = speed
    total = base_total + speed_points(speed) - competition_penalty(
        extras["reports_90d"], in_scope_count(platform, doc))
    return max(total, 0.0), extras


def build_ranking(platform, docs, h1_metrics=None):
    scored = []
    for doc in docs:
        total, is_rdp, bounty_max = score_doc(platform, doc)
        extras = {"avg_bounty": None, "reports_90d": None, "speed_days": None}
        if platform == "hackerone" and h1_metrics is not None:
            handle = h1_handle(doc)
            total, extras = enrich_score(
                platform, doc, total,
                h1_metrics.get(handle) if handle else None)
        scored.append((total, doc, is_rdp, bounty_max, extras))
    # ties (e.g. capped scores): larger real scope wins, then name
    scored.sort(key=lambda t: (-t[0], -in_scope_count(platform, t[1]),
                               t[1].get("programName", "")))
    # same program name can appear as several engagement entries — keep best
    seen_names = set()
    unique = []
    for item in scored:
        name = item[1].get("programName", "")
        if name in seen_names:
            continue
        seen_names.add(name)
        unique.append(item)
    return unique[:TOP_N]


def _short(name, limit=42):
    name = str(name or "?").strip()
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _money(bounty_max):
    if bounty_max >= 1000:
        return f"${bounty_max/1000:.1f}k"
    if bounty_max > 0:
        return f"${int(bounty_max)}"
    return "—"


def format_platform_lines(platform, ranking):
    lines = []
    for i, (total, doc, is_rdp, bounty_max, extras) in enumerate(ranking, 1):
        badge = "💰" if is_rdp else "🆓"
        assets = in_scope_count(platform, doc)
        money = _money(extras["avg_bounty"] or bounty_max)
        seg = (f"{i}. {badge} {doc.get('programName', '?')} — "
               f"{money} · {assets} assets")
        if extras.get("reports_90d"):
            seg += f" · {extras['reports_90d']}r/90d"
        if extras.get("speed_days") is not None:
            seg += f" · ⚡{_short_speed(extras['speed_days'])}"
        lines.append(seg)
    return lines


def _short_speed(days):
    if days < 1 / 24:
        return f"{int(days * 1440)}m"
    if days < 1:
        return f"{days * 24:.0f}h"
    return f"{days:.0f}d"


def send_report(rankings, discord_webhook, tg_module):
    from discord_webhook import DiscordEmbed, DiscordWebhook

    from modules.notifier.discord import discord_enabled
    if discord_enabled():
        webhook = DiscordWebhook(
            url=discord_webhook, username="PEBO Watcher",
            avatar_url="https://t3.ftcdn.net/jpg/00/91/64/62/360_F_91646202_b3K6ELfgM2E8QIwuNTlzco7K1r3mOJvp.jpg")
        embed = DiscordEmbed(
            title="🏆 Daily Top-10 Targets",
            description="Ranked by bounty value + scope size + program type.\n"
                        "HackerOne lines add live stats where published: "
                        "avg bounty · reports/90d (competition) · ⚡response speed.\n"
                        "Bugcrowd/Intigriti/YWH hide stats from bots (Cloudflare).",
            color="ffd700")
        for platform in PLATFORMS:
            lines = format_platform_lines(platform, rankings[platform])
            value = "\n".join(lines) if lines else "_no programs_"
            if len(value) > 1024:
                value = value[:1020] + "…"
            embed.add_embed_field(name=f"📡 {LABELS[platform]}", value=value, inline=False)
        embed.set_footer(text="PEBO · Programs Watcher · auto-generated daily")
        webhook.add_embed(embed)
        resp = webhook.execute()
        if resp.status_code != 200:
            print("Discord send error:", resp.status_code, resp.content)

    if tg_module.enabled():
        tg_lines = ["🏆 <b>Daily Top-10 Targets</b>",
                    "Ranked by bounty + scope + type",
                    "<i>HackerOne lines include live stats where published "
                    "(avg bounty · reports/90d · ⚡speed)</i>\n"]
        for platform in PLATFORMS:
            tg_lines.append(f"\n📡 <b>{LABELS[platform]}</b>")
            for i, (total, doc, is_rdp, bounty_max, extras) in enumerate(
                    rankings[platform], 1):
                badge = "💰" if is_rdp else "🆓"
                assets = in_scope_count(platform, doc)
                url = doc.get("programURL", "")
                name = _short(doc.get("programName", "?"), 34)
                link = f'<a href="{url}">{name}</a>' if url else name
                money = _money(extras["avg_bounty"] or bounty_max)
                line = f"{i}. {badge} {link} — {money} · {assets}a"
                if extras.get("reports_90d"):
                    line += f" · {extras['reports_90d']}r/90d"
                if extras.get("speed_days") is not None:
                    line += f" · ⚡{_short_speed(extras['speed_days'])}"
                tg_lines.append(line)
        tg_module._post("\n".join(tg_lines),
                        *tg_module.load_telegram_config())


def run():
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.full_load(f)
    from pymongo import MongoClient
    client = MongoClient(cfg["mongoDB"]["uri"], serverSelectionTimeoutMS=20000)
    db = client[cfg["mongoDB"]["database"]]

    rankings = {}
    h1_docs = list(db["hackerone"].find({}))
    pre = build_ranking("hackerone", h1_docs)
    candidates = [h1_handle(doc) for _, doc, _, _, _ in pre]
    candidates = [h for h in candidates if h][:H1_ENRICH_CANDIDATES]
    print(f"H1 enrichment: {len(candidates)} handles -> {candidates}")
    h1_metrics = fetch_h1_metrics(candidates)

    rankings["hackerone"] = build_ranking("hackerone", h1_docs,
                                          h1_metrics=h1_metrics)
    for platform in ["bugcrowd", "intigriti", "yeswehack"]:
        rankings[platform] = build_ranking(
            platform, list(db[platform].find({})))
    client.close()
    send_report(rankings, cfg["discordWebhook"]["programs_watcher"],
                sys.modules.get("modules.notifier.telegram")
                or __import__("modules.notifier.telegram", fromlist=["x"]))


def _selftest():
    docs = [
        {"programName": "BigBounty", "programType": "rdp",
         "reward": {"min": "$100", "max": "$3,500"}, "inScope": [f"a{i}" for i in range(15)]},
        {"programName": "VdpOnly", "programType": "vdp", "reward": {},
         "inScope": ["x.com"]},
        {"programName": "YwhStyle", "programType": "rdp",
         "reward": {"min": "50 USD", "max": "5000 USD"}, "inScope": [f"y{i}" for i in range(5)]},
        {"programName": "H1Wide", "programType": "rdp", "reward": None,
         "scope": {str(i): "host (InScope)" for i in range(30)}},
        {"programName": "Malformed", "programType": "rdp",
         "reward": {"min": "", "max": "n/a"}, "inScope": []},
        {"programName": "HugeScope", "programType": "vdp", "reward": {},
         "inScope": [f"z{i}" for i in range(50)]},
    ]
    m = parse_money_max({"min": "$100", "max": "$3,500"})
    assert m == 3500.0, m
    assert parse_money_max({"max": "5000 USD"}) == 5000.0
    assert parse_money_max({}) == 0.0
    assert parse_money_max({"max": "n/a"}) == 0.0
    assert parse_money_max(None) == 0.0

    assert parse_duration_days(None) is None
    assert parse_duration_days(86400) == 1.0
    assert abs(parse_duration_days("4 days, 18 hours") - 4.75) < 1e-9
    assert parse_duration_days("about 1 month") == 30.0
    assert parse_duration_days("1 hour") == 1 / 24
    assert parse_duration_days("garbage") is None
    assert speed_points(None) == 0.0 and speed_points(0.5) == 15.0
    assert speed_points(20) == 4.0 and speed_points(400) == 0.0
    assert competition_penalty(None, 10) == 0.0
    assert competition_penalty(0, 10) == 0.0
    assert competition_penalty(100, 5) == 15.0   # 20 r/asset
    assert competition_penalty(10, 5) == 4.0     # 2.0 r/asset
    assert competition_penalty(30, 5) == 8.0     # 6 r/asset
    assert competition_penalty(3, 5) == 0.0      # 0.6 r/asset

    r = build_ranking("bugcrowd", docs)
    names = [d["programName"] for _, d, _, _, _ in r]
    # BigBounty: 20+10.5+30=60.5 ; H1Wide: 20+0+40=60 ; YwhStyle: 20+15+10=45
    assert names[0] == "BigBounty" and names[1] == "H1Wide" and names[2] == "YwhStyle", names
    # H1-shaped docs only: H1Wide 20+40=60 beats HugeScope 0+40=40 (rdp bonus)
    r2 = build_ranking("hackerone", [docs[3], docs[5]])
    assert r2[0][1]["programName"] == "H1Wide", r2  # 20 + min(60,40) = 60
    lines = format_platform_lines("hackerone", r2)
    assert lines[0].startswith("1. 💰 H1Wide — — · 30 assets")
    assert build_ranking("bugcrowd", []) == []
    # duplicate engagement names collapse to the best entry
    dupes = [{"programName": "Same", "programType": "rdp", "reward": {"max": "$100"},
              "inScope": ["a"]},
             {"programName": "Same", "programType": "rdp", "reward": {"max": "$9,000"},
              "inScope": ["a", "b", "c"]}]
    r3 = build_ranking("bugcrowd", dupes)
    assert len(r3) == 1 and r3[0][3] == 9000.0, r3
    # enrichment: avg bounty replaces empty feed reward, speed adds, pressure cuts
    h1doc = {"programName": "Enriched", "programType": "rdp",
             "programURL": "https://hackerone.com/enriched?type=team",
             "scope": {str(i): "h (InScope)" for i in range(20)}}
    base = score_doc("hackerone", h1doc)[0]  # 20+0+40 = 60
    metrics = {"average_bounty_lower_amount": 600,
               "average_bounty_upper_amount": 1009,
               "reports_received_last_90_days": 10,
               "first_response_time": "1 hour", "triage_time": None}
    total, extras = enrich_score("hackerone", h1doc, base, metrics)
    assert extras["avg_bounty"] == 804.5 and extras["reports_90d"] == 10, extras
    # bounty 804.5/333=2.4 -> 20+2.4+40=62.4 ; +15 speed ; pressure 0.5 -> -0
    assert abs(total - (62.415 + 15)) < 0.01, total
    # no metrics -> unchanged base
    total2, extras2 = enrich_score("hackerone", h1doc, base, None)
    assert total2 == 60 and extras2["avg_bounty"] is None
    # brutal pressure: 200 reports on 20 assets = -15
    total3, _ = enrich_score("hackerone", h1doc, base,
                             {"reports_received_last_90_days": 200})
    assert total3 == 45.0, total3
    print("top_targets self-test: ALL PASS")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        run()
