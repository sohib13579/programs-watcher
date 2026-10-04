from discord_webhook import DiscordWebhook, DiscordEmbed
import yaml

from modules.notifier.functions import generate_diff, split_text, get_platform_profile, shorten_string
from modules.notifier.telegram import enabled as telegram_enabled, send as telegram_send, send_startup as telegram_startup, heartbeat as telegram_heartbeat, quiet as telegram_quiet

# count of real alerts sent during this process run (0 = quiet round)
notifications_sent = 0


def discord_enabled():
    """Discord is optional; config.yml `discord.enabled: false` silences the
    webhook entirely while Telegram keeps working."""
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.full_load(f)
    return (cfg.get("discord") or {}).get("enabled", True)


def add_field(embed, data, message, diff=False):
    texts = split_text(data)
    isFirstTime = 0
    type = ""
    if diff:
        type = "diff\n"
    for text in texts:
        if isFirstTime == 0:
            embed.add_embed_field(
                name=message, value=f"```{type}{text}```", inline=False)
            isFirstTime = 1
        else:
            embed.add_embed_field(
                name=" ", value=f"```{type}{text}```", inline=False)


def changed_program_message(data):
    embed = DiscordEmbed(title=f"{data['programName']}",
                         description=f"** {data['programName']} ** has changed!\n** Program type: ** {data['programType']}\n\n** Program page: ** [Click here]({data['programURL']})", color=data['color'])
    embed.set_thumbnail(url=data['logo'])
    embed.set_footer(text='PEBO')
    if data["platformName"] in ["HackerOne", "Intigriti"]:
        if data["newProgramType"]:
            embed.add_embed_field(name="Program type changed:",
                                  value=f"```changed to {data['newProgramType']}```", inline=False)
        if data["platformName"] == "Intigriti":
            if data['newReward']:
                embed.add_embed_field(
                    name="Bounty Table changed:", value=f"```{data['newReward']['min']} -- {data['newReward']['max']}```", inline=False)
        if data["newScope"]:
            scope = '\n'.join(data['newScope'])
            add_field(embed, scope, "Following scope added:")
        if data["removedScope"]:
            removedScope = '\n'.join(data['removedScope'])
            add_field(embed, removedScope, "Following Scope removed:")
        if data["changedScope"]:
            for scope in data["changedScope"]:
                old = scope["old"]
                new = scope["new"]
                diff = generate_diff(old, new)
                add_field(embed, diff, "Following scope changed:", True)
    if data["platformName"] in ["Bugcrowd", "YesWeHack"]:
        if data['newType']:
            embed.add_embed_field(
                name="Program Type changed", value=f"```New Type: {data['newType']}```", inline=False)
        if data['reward']:
            embed.add_embed_field(name="Bounty Table changed:",
                                  value=f"```{data['reward']['min']} -- {data['reward']['max']}```", inline=False)
        if data['newInScope']:
            inscope = '\n'.join(data['newInScope'])
            add_field(embed, inscope, "Following inScope added:")
        if data['removeInScope']:
            removeInScope = '\n'.join(data['removeInScope'])
            add_field(embed, removeInScope, "Following Inscope removed:")

        if data["platformName"] == "Bugcrowd":
            if data['newOutOfScope']:
                newOutOfScope = '\n'.join(data['newOutOfScope'])
                add_field(embed, newOutOfScope,
                          "Following out of scope added:")
            if data['removeOutOfScope']:
                removeOutOfScope = '\n'.join(data['removeOutOfScope'])
                add_field(embed, removeOutOfScope,
                          "Following out of scope removed:")
    return embed


def new_program_message(data):
    embed = DiscordEmbed(title=f"{data['programName']}",
                         description=f"** New program ** named** {data['programName']} ** added to platform!\n** Program type: ** {data['programType']}\n\n** Program page: ** [Click here]({data['programURL']})", color=data['color'])
    embed.set_thumbnail(url=data['logo'])
    embed.set_footer(text='PEBO')
    if data["platformName"] in ["HackerOne", "Intigriti"]:
        if data["platformName"] == "Intigriti":
            if data['newReward']:
                embed.add_embed_field(
                    name="Bounty Table:", value=f"```{data['newReward']['min']} -- {data['newReward']['max']}```", inline=False)
        if data["newScope"]:
            scope = ""
            for newScope in data["newScope"]:
                scope += f"{shorten_string(newScope)}\n"
            add_field(embed, scope, "Scope:")
    if data["platformName"] in ["Bugcrowd", "YesWeHack"]:
        if data['reward']:
            embed.add_embed_field(
                name="Bounty Table:", value=f"```{data['reward']['min']} -- {data['reward']['max']}```", inline=False)
        if data['newInScope']:
            inscope = '\n'.join(data['newInScope'])
            add_field(embed, inscope, "InScope:")
        if data["platformName"] == "Bugcrowd":
            if data['newOutOfScope']:
                newOutOfScope = '\n'.join(data['newOutOfScope'])
                add_field(embed, newOutOfScope, "Out of scope:")

    return embed

def removed_program_message(data):
    embed = DiscordEmbed(title=f"{data['programName']}",
                         description=f"Program named ** {data['programName']} ** has removed from platform!\n** Program type: ** {data['programType']}", color=data['color'])
    embed.set_thumbnail(url=data['logo'])
    embed.set_footer(text='PEBO')

    return embed

def send_notification(data, webhook_url):
    global notifications_sent
    notifications_sent += 1

    if discord_enabled():
        webhook = DiscordWebhook(
            url=webhook_url, username=data['platformName'], avatar_url=get_platform_profile(data['platformName']))
        if data["isRemoved"]:
            embed = removed_program_message(data)
        elif data["isNewProgram"]:
            embed = new_program_message(data)
        else:
            embed = changed_program_message(data)
        webhook.add_embed(embed)
        response = webhook.execute()
        if response.status_code != 200:
            print(data["programName"])
            print("Error sending message:", response.content)
    if telegram_enabled():
        try:
            telegram_send(data)
        except Exception as e:
            print("Telegram send error:", e)

def send_startup_message(webhook_url):
    if discord_enabled():
        webhook = DiscordWebhook(
            url=webhook_url, username="PEBO Watcher", avatar_url="https://t3.ftcdn.net/jpg/00/91/64/62/360_F_91646202_b3K6ELfgM2E8QIwuNTlzco7K1r3mOJvp.jpg")
        embed = DiscordEmbed(
            title="🚀 PEBO Watcher is Online",
            description="Your personal bug-bounty radar is now active and watching the horizon. 🌐",
            color="23ff1f")
        embed.set_thumbnail(url="https://t3.ftcdn.net/jpg/00/91/64/62/360_F_91646202_b3K6ELfgM2E8QIwuNTlzco7K1r3mOJvp.jpg")
        embed.add_embed_field(
            name="📡 Platforms Under Surveillance",
            value="`HackerOne` · `Bugcrowd` · `Intigriti` · `YesWeHack`", inline=False)
        embed.add_embed_field(
            name="🔔 You will be alerted when",
            value="🆕 A **new program** drops\n"
                  "♻️ The **scope changes** (added / removed / modified)\n"
                  "💰 A **bounty table** gets updated\n"
                  "🗑️ A program gets **removed**", inline=False)
        embed.add_embed_field(
            name="⚙️ Operations",
            value="⏱ Checked **every 30 minutes**\n"
                  "☁️ Running **24/7** in the cloud — no laptop needed\n"
                  "📨 Delivered to **Telegram**", inline=False)
        embed.set_footer(text="PEBO · Programs Watcher")
        webhook.add_embed(embed)
        response = webhook.execute()
        if response.status_code != 200:
           print("There was an error sending the Discord message")
    if telegram_enabled():
        try:
            telegram_startup()
        except Exception as e:
            print("Telegram startup error:", e)


def send_quiet_message(webhook_url, total_programs):
    if discord_enabled():
        webhook = DiscordWebhook(
            url=webhook_url, username="PEBO Watcher",
            avatar_url="https://t3.ftcdn.net/jpg/00/91/64/62/360_F_91646202_b3K6ELfgM2E8QIwuNTlzco7K1r3mOJvp.jpg")
        embed = DiscordEmbed(
            title="📭 No updates yet",
            description=f"Checked all 4 platforms (**{total_programs}** programs) — "
                        "no new programs or changes this round.\n\n"
                        "⏱ Next check in ~30 minutes.",
            color="808080")
        embed.set_footer(text="PEBO · Programs Watcher")
        webhook.add_embed(embed)
        response = webhook.execute()
        if response.status_code != 200:
            print("Error sending quiet message:", response.status_code)
    if telegram_enabled():
        try:
            telegram_quiet(total_programs)
        except Exception as e:
            print("Telegram quiet error:", e)


def send_heartbeat_message(webhook_url, now_utc, total_programs):
    if discord_enabled():
        webhook = DiscordWebhook(
            url=webhook_url, username="PEBO Watcher",
            avatar_url="https://t3.ftcdn.net/jpg/00/91/64/62/360_F_91646202_b3K6ELfgM2E8QIwuNTlzco7K1r3mOJvp.jpg")
        embed = DiscordEmbed(
            title="💓 Heartbeat — PEBO Watcher is alive",
            description=f"🕒 Last full check: **{now_utc.strftime('%H:%M')} UTC**\n"
                        f"📡 Monitoring **{total_programs}** programs on 4 platforms",
            color="00b0f0")
        embed.set_footer(text="PEBO · Programs Watcher · 6-hour liveness confirmation")
        webhook.add_embed(embed)
        response = webhook.execute()
        if response.status_code != 200:
            print("Error sending heartbeat:", response.status_code)
    if telegram_enabled():
        try:
            telegram_heartbeat(now_utc, total_programs)
        except Exception as e:
            print("Telegram heartbeat error:", e)
