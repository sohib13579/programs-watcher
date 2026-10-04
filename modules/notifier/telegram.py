"""Telegram side-channel: the Discord notifier calls send()/send_startup()
after every Discord message, so both platforms get the same alerts.
Silently does nothing unless config.yml `telegram:` has a real bot token.

Env overrides (used by GitHub Actions secrets): TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID. Messages are HTML with escapes; oversized text is split
under Telegram's 4096-char message limit.
"""
import html
import os

import requests
import yaml

MAX_MESSAGE_LEN = 4000


def load_telegram_config():
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.full_load(f)
    tg = cfg.get("telegram", {}) or {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", tg.get("chat_id", ""))
    return token, chat_id


def enabled():
    token, chat_id = load_telegram_config()
    return bool(token) and not token.startswith("<") and bool(chat_id) \
        and not str(chat_id).startswith("<")


def _esc(text):
    return html.escape(str(text), quote=False)


def _bounty_line(reward):
    if reward and reward.get("min") and reward.get("max"):
        return f"Bounty: {_esc(reward['min'])} -- {_esc(reward['max'])}\n"
    return ""


def build_message_text(data):
    lines = []
    if data["isRemoved"]:
        lines.append(f"🗑 <b>Program removed from {_esc(data['platformName'])}</b>")
    elif data["isNewProgram"]:
        lines.append(f"🆕 <b>New program on {_esc(data['platformName'])}</b>")
    else:
        lines.append(f"♻️ <b>Program changed on {_esc(data['platformName'])}</b>")
    lines.append("")
    lines.append(f"<b>{_esc(data['programName'])}</b> "
                 f"({_esc(data.get('programType') or 'unknown type')})")
    if data.get("programURL"):
        lines.append(f"<a href=\"{_esc(data['programURL'])}\">Open program page</a>")

    if not data["isRemoved"]:
        new_type = data.get("newProgramType") or data.get("newType")
        if new_type and not data["isNewProgram"]:
            lines.append(f"Program type changed to: <b>{_esc(new_type)}</b>")
        reward = data.get("newReward") or data.get("reward")
        bounty = _bounty_line(reward)
        if bounty:
            lines.append(bounty.rstrip())
        if data.get("newScope"):
            lines.append("\n<b>In scope added:</b>")
            lines.append(f"<code>{_esc(chr(10).join(data['newScope']))}</code>")
        if data.get("newInScope"):
            lines.append("\n<b>In scope added:</b>")
            lines.append(f"<code>{_esc(chr(10).join(data['newInScope']))}</code>")
        if data.get("removedScope"):
            lines.append("\n<b>Scope removed:</b>")
            lines.append(f"<code>{_esc(chr(10).join(data['removedScope']))}</code>")
        if data.get("removeInScope"):
            lines.append("\n<b>In scope removed:</b>")
            lines.append(f"<code>{_esc(chr(10).join(data['removeInScope']))}</code>")
        if data.get("newOutOfScope"):
            lines.append("\n<b>Out of scope added:</b>")
            lines.append(f"<code>{_esc(chr(10).join(data['newOutOfScope']))}</code>")
        if data.get("removeOutOfScope"):
            lines.append("\n<b>Out of scope removed:</b>")
            lines.append(f"<code>{_esc(chr(10).join(data['removeOutOfScope']))}</code>")
        for scope in data.get("changedScope", []):
            lines.append("\n<b>Scope changed:</b>")
            diff = []
            for line in scope["old"].splitlines():
                diff.append(f"- {_esc(line)}")
            for line in scope["new"].splitlines():
                diff.append(f"+ {_esc(line)}")
            lines.append(f"<code>{chr(10).join(diff)}</code>")

    return "\n".join(lines)


def _split_text(text, limit=MAX_MESSAGE_LEN):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _post(text, token, chat_id):
    for i, chunk in enumerate(_split_text(text)):
        if i > 0:
            chunk = f"(continued {i + 1})\n{chunk}"
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": chunk,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=30)
        if resp.status_code != 200:
            print(f"Telegram send error {resp.status_code}: {resp.text}")


def send(data):
    token, chat_id = load_telegram_config()
    _post(build_message_text(data), token, chat_id)


def heartbeat(now_utc, total_programs):
    token, chat_id = load_telegram_config()
    _post(
        "💓 <b>Heartbeat — PEBO Watcher is alive</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 Last full check: <b>{now_utc.strftime('%H:%M')} UTC</b>\n"
        f"📡 Monitoring <b>{total_programs}</b> programs on 4 platforms\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Real alerts arrive instantly when something drops.</i>",
        token, chat_id)


def quiet(total_programs):
    token, chat_id = load_telegram_config()
    _post(
        "📭 <b>No updates yet</b>\n"
        f"Checked all 4 platforms ({total_programs} programs) — "
        "no new programs or changes this round. Next check in ~30 min.",
        token, chat_id)


def send_startup():
    token, chat_id = load_telegram_config()
    _post(
        "🚀 <b>PEBO Watcher is Online</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Your personal bug-bounty radar is now active. 🌐\n\n"
        "📡 <b>Platforms under surveillance</b>\n"
        "    <code>HackerOne</code> · <code>Bugcrowd</code>\n"
        "    <code>Intigriti</code> · <code>YesWeHack</code>\n\n"
        "🔔 <b>You will be alerted when</b>\n"
        "    🆕 A <b>new program</b> drops\n"
        "    ♻️ The <b>scope changes</b> (added / removed / modified)\n"
        "    💰 A <b>bounty table</b> gets updated\n"
        "    🗑️ A program gets <b>removed</b>\n\n"
        "⚙️ <b>Operations</b>\n"
        "    ⏱ Checked every 30 minutes\n"
        "    ☁️ Runs 24/7 in the cloud — no laptop needed\n"
        "    📨 Delivered to Discord + Telegram\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ <i>Stay sharp — new targets wait for no one.</i>",
        token, chat_id)
