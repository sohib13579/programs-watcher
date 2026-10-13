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


def _bounty_line(reward, old=None):
    if not (reward and reward.get("min") and reward.get("max")):
        return ""
    new = f"{_esc(reward['min'])} – {_esc(reward['max'])}"
    if old and old.get("min") and old.get("max") and old != reward:
        old_s = f"{_esc(old['min'])} – {_esc(old['max'])}"
        return f"💰 <b>Bounty</b>: {old_s} ➜ {new}"
    return f"💰 <b>Bounty</b>: {new}"


PLATFORM_ICONS = {
    "Bugcrowd": "🟢",
    "HackerOne": "⚫",
    "Intigriti": "🟣",
    "YesWeHack": "🟠",
}
MAX_SCOPE_ITEMS = 12


def _scope_list(items):
    shown = items[:MAX_SCOPE_ITEMS]
    lines = [f"• {_esc(i)}" for i in shown]
    if len(items) > MAX_SCOPE_ITEMS:
        lines.append(f"… and {len(items) - MAX_SCOPE_ITEMS} more")
    return lines


def _date_line(data):
    # the platform's own update date wins; otherwise when we detected it
    when = data.get("updated_at") or ""
    if when:
        return f"🕒 <b>Updated:</b> {_esc(when)}"
    return f"🕒 <b>Detected:</b> {_esc(data.get('detected_at') or '')}"


def build_message_text(data):
    icon = PLATFORM_ICONS.get(data.get("platformName"), "📡")
    platform = _esc(data["platformName"])

    if data["isRemoved"]:
        lines = [f"{icon} {platform} · 🗑 <b>PROGRAM REMOVED</b>"]
    elif data["isNewProgram"]:
        lines = [f"{icon} {platform} · 🆕 <b>NEW PROGRAM</b>"]
    else:
        lines = [f"{icon} {platform} · ♻️ <b>PROGRAM CHANGED</b>"]
    lines.append("")
    lines.append(f"📌 <b>{_esc(data['programName'])}</b> "
                 f"<i>({data.get('programType') or 'unknown type'})</i>")
    lines.append(_date_line(data))
    if data.get("programURL"):
        lines.append(f"🔗 <a href=\"{_esc(data['programURL'])}\">Open program page</a>")

    if not data["isRemoved"]:
        new_type = data.get("newProgramType") or data.get("newType")
        if new_type and not data["isNewProgram"]:
            lines.append("")
            old_type = data.get("type_old")
            if old_type:
                lines.append(f"🔄 <b>Type</b>: {_esc(old_type)} ➜ "
                             f"{_esc(new_type)}")
            else:
                lines.append(f"🔄 <b>Type changed:</b> {_esc(new_type)}")
        reward = data.get("newReward") or data.get("reward")
        bounty = _bounty_line(reward, data.get("reward_old"))
        if bounty:
            lines.append("")
            lines.append(bounty.rstrip())
        if data.get("newScope"):
            lines.append("")
            lines.append(f"➕ <b>Scope added</b> ({len(data['newScope'])})")
            lines.extend(_scope_list(data["newScope"]))
        if data.get("newInScope"):
            lines.append("")
            lines.append(f"➕ <b>In scope added</b> ({len(data['newInScope'])})")
            lines.extend(_scope_list(data["newInScope"]))
        if data.get("removedScope"):
            lines.append("")
            lines.append(f"➖ <b>Scope removed</b> ({len(data['removedScope'])})")
            lines.extend(_scope_list(data["removedScope"]))
        if data.get("removeInScope"):
            lines.append("")
            lines.append(f"➖ <b>In scope removed</b> ({len(data['removeInScope'])})")
            lines.extend(_scope_list(data["removeInScope"]))
        if data.get("newOutOfScope"):
            lines.append("")
            lines.append(f"🚫 <b>Out of scope added</b> ({len(data['newOutOfScope'])})")
            lines.extend(_scope_list(data["newOutOfScope"]))
        if data.get("removeOutOfScope"):
            lines.append("")
            lines.append(f"✅ <b>Out of scope removed</b> ({len(data['removeOutOfScope'])})")
            lines.extend(_scope_list(data["removeOutOfScope"]))
        for scope in data.get("changedScope", []):
            lines.append("")
            lines.append("<b>✏️ Scope changed</b>")
            for line in scope["old"].splitlines():
                if line.strip():
                    lines.append(f"<code>- {_esc(line)}</code>")
            for line in scope["new"].splitlines():
                if line.strip():
                    lines.append(f"<code>+ {_esc(line)}</code>")

    if data.get("note"):
        lines.append("")
        lines.append(f"ℹ️ {_esc(data['note'])}")

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


def channel_chat_id():
    """Public channel for polished program updates only — quiet messages,
    heartbeats and error notices stay private. Disabled while unset
    ("<...>" placeholder). Env override: TELEGRAM_CHANNEL_ID."""
    with open("config.yml", "r", encoding="utf-8") as f:
        cfg = yaml.full_load(f)
    tg = cfg.get("telegram", {}) or {}
    cid = os.environ.get("TELEGRAM_CHANNEL_ID", tg.get("channel_chat_id", ""))
    if not cid or str(cid).startswith("<"):
        return None
    return str(cid)


def send(data):
    """Real program alerts: private chat + public channel (if set)."""
    token, chat_id = load_telegram_config()
    text = build_message_text(data)
    _post(text, token, chat_id)
    channel = channel_chat_id()
    if channel and channel != str(chat_id):
        try:
            _post(text, token, channel)
        except Exception as e:
            # channel trouble must never block the private alert
            print("Channel send error:", e)


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


def send_info(html_text):
    """Free-form one-off notice (reseeds, feed errors). Caller escapes HTML."""
    token, chat_id = load_telegram_config()
    _post(html_text, token, chat_id)
