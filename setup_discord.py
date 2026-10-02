#!/usr/bin/env python3
"""Provision Chert's Discord category and channels from a bot token.

  ./setup_discord.py            # interactive: invite link, create channels, write ids to .env
  ./setup_discord.py --check    # just log in and show what the bot can see
  ./setup_discord.py --guild "My Server"   # pick a server when the bot is in several

What it does
  1. Reads DISCORD_BOT_TOKEN from .env (or prompts for it, hidden) and decodes the bot's
     application id from the token itself, so it can print the exact invite URL — with the
     permissions chert needs and the `bot` + `applications.commands` scopes (slash commands
     silently fail without the second one).
  2. Logs in. If the bot is in no server yet, it prints the invite URL and waits for you to
     click it. If it's in several, it asks which one (or use --guild).
  3. Finds or creates a private `chert` category. Both (default) uses #codex and
     #claude, plus each backend's ask-all and shared-chat channels.
     Codex-only uses #codex.
     With --backend claude, it creates three text channels —
       #claudes      one thread per live claude (the main channel)
       #claude-chat  two-way bridge to the claude↔claude bus
       #all-claudes  ask every claude at once; a summarizer claude answers
     — sets their topics, and writes DISCORD_CHANNEL_ID / DISCORD_CHAT_CHANNEL_ID /
     DISCORD_BROADCAST_CHANNEL_ID (and DISCORD_OWNER_ID if blank) into .env.
Idempotent: reuses channels within the category and preserves existing permissions.

Needs: `pip install -r requirements.txt` (discord.py, python-dotenv). The bot must have the
Message Content intent enabled in the Developer Portal → Bot → Privileged Gateway Intents.
"""
import argparse
import asyncio
import base64
import getpass
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values

try:
    import discord
except ImportError:
    sys.exit("discord.py missing — run:  .venv/bin/pip install -r requirements.txt  (or ./setup.sh)")

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
CLAUDE_CHANNELS = [
    ("claudes", "DISCORD_CHANNEL_ID",
     "🔭 chert's signalscope — one thread per live claude on the box. Reply in a thread to talk to that "
     "claude; /claude <prompt> or @chert to launch one; !help for everything."),
    ("claude-chat", "DISCORD_CHAT_CHANNEL_ID",
     "two-way bridge to the claude↔claude bus (cchat.py) — type here to talk to all listening claudes"),
    ("all-claudes", "DISCORD_BROADCAST_CHANNEL_ID",
     "🧠 ask EVERY claude at once: a plain message is fanned out, replies are collected and a summarizer "
     "claude answers here. Reply to it / !hub for follow-ups. !all <msg> = plain broadcast."),
]
CODEX_CHANNELS = [
    ('codex', 'DISCORD_CHANNEL_ID',
     'Chert · type a prompt here to start a session. Existing Codex sessions appear automatically; reply in their threads to talk to them.'),
]
SHARED_CHANNELS = [
    ('codex', 'DISCORD_CODEX_CHANNEL_ID', CODEX_CHANNELS[0][2]),
    ('claude', 'DISCORD_CLAUDE_CHANNEL_ID',
     'Chert · type a prompt to start Claude. Reply in a session thread to continue.'),
    ('all-codex', 'DISCORD_CODEX_BROADCAST_CHANNEL_ID',
     'Ask all Codex sessions; Chert collects their replies and summarizes them. !all sends without a summary.'),
    ('codex-chat', 'DISCORD_CODEX_CHAT_CHANNEL_ID', 'Shared Codex session chat bus.'),
    *CLAUDE_CHANNELS[1:],
]


def channels_for(backend):
    if backend == 'both':
        return SHARED_CHANNELS
    if backend not in {'codex', 'claude'}:
        raise ValueError('Backend must be both, codex, or claude')
    return CODEX_CHANNELS if backend == 'codex' else CLAUDE_CHANNELS


def read_env():
    return dict(dotenv_values(ENV)) if ENV.exists() else {}


def existing_channel(guild, category, name, key, env):
    """Configured IDs outrank names/categories: users may reorganize their server."""
    channel_id = env.get(key)
    if not channel_id and key == 'DISCORD_CODEX_CHANNEL_ID' and env.get('DISCORD_CHANNEL_ID'):
        legacy = guild.get_channel(int(env['DISCORD_CHANNEL_ID']))
        if isinstance(legacy, discord.TextChannel) and legacy.name == 'codex':
            channel_id = legacy.id
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            raise ValueError(f'Configured {key} is missing or inaccessible; update the ID before provisioning.')
        return channel
    return discord.utils.get(category.text_channels, name=name)


def write_env(updates):
    """Set KEY=value lines in .env in place (keeps comments/order; appends missing keys)."""
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    done = set()
    out = []
    for line in lines:
        m = re.match(r"^\s*([A-Z_0-9]+)\s*=\s*(.*?)\s*(#.*)?$", line)
        if m and m.group(1) in updates and not line.lstrip().startswith("#"):
            comment = f"   {m.group(3)}" if m.group(3) else ""
            out.append(f"{m.group(1)}={updates[m.group(1)]}{comment}")
            done.add(m.group(1))
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in done:
            out.append(f"{k}={v}")
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write("\n".join(out) + "\n")
    os.chmod(ENV, 0o600)


def app_id_from_token(token):
    """Bot tokens are `base64(application_id).timestamp.hmac`; the first part is public."""
    try:
        first = token.split(".")[0]
        first += "=" * (-len(first) % 4)
        return int(base64.urlsafe_b64decode(first).decode())
    except Exception:  # noqa: BLE001
        return None


def needed_permissions():
    return discord.Permissions(
        view_channel=True, send_messages=True, send_messages_in_threads=True,
        create_public_threads=True, manage_threads=True, manage_messages=True,   # pin the board
        manage_channels=True,          # create the three channels / rename threads
        manage_webhooks=True,          # one webhook per channel: each claude posts under its own name
        embed_links=True, attach_files=True, add_reactions=True, read_message_history=True,
        use_application_commands=True, mention_everyone=False,
    )


def invite_url(app_id):
    return discord.utils.oauth_url(app_id, permissions=needed_permissions(),
                                   scopes=("bot", "applications.commands"))


async def build(token, want_guild, check_only, backend='codex'):
    channels = channels_for(backend)
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    client = discord.Client(intents=intents)
    result = {}

    @client.event
    async def on_ready():
        try:
            app_id = client.user.id
            guilds = list(client.guilds)
            print(f"logged in as {client.user} (application id {app_id})")
            if not guilds:
                print("\nThe bot is not in any server yet. Invite it with this link (it has exactly the\n"
                      "permissions chert needs + the applications.commands scope for slash commands):\n\n"
                      f"  {invite_url(app_id)}\n")
                if check_only:
                    return
                print('waiting up to 10 minutes for the bot to join a server…')
                for _ in range(120):
                    await asyncio.sleep(5)
                    guilds = list(client.guilds)
                    if guilds:
                        break
                if not guilds:
                    print("still not in a server — run me again after inviting it.")
                    return
            if want_guild:
                g = next((x for x in guilds if x.name == want_guild or str(x.id) == want_guild), None)
                if not g:
                    print(f"no server named/id {want_guild!r}; the bot is in: " + ", ".join(f"{x.name} ({x.id})" for x in guilds))
                    return
            elif len(guilds) == 1:
                g = guilds[0]
            else:
                print("the bot is in several servers:")
                for i, x in enumerate(guilds, 1):
                    print(f"  {i}. {x.name}  ({x.id})")
                pick = input("which one? [number] ").strip()
                g = guilds[int(pick) - 1]
            print(f"server: {g.name} ({g.id}), owner {g.owner_id}")
            if check_only:
                for name, key, _ in channels:
                    ch = discord.utils.get(g.text_channels, name=name)
                    print(f"  #{name:<12} {'exists ' + str(ch.id) if ch else 'missing'}")
                me = g.me
                missing = [p for p, v in needed_permissions() if v and not getattr(me.guild_permissions, p)]
                print("  permissions:", "ok" if not missing else "MISSING " + ", ".join(missing))
                result['checked'] = not missing and all(
                    discord.utils.get(g.text_channels, name=name) is not None for name, _, _ in channels)
                return
            cat = discord.utils.get(g.categories, name="chert")
            if cat is None:
                # Server administrators retain access to private channels.
                overwrites = {
                    g.default_role: discord.PermissionOverwrite(view_channel=False),
                    g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                     send_messages_in_threads=True),
                }
                env = read_env()
                user_ids = {int(env.get('DISCORD_OWNER_ID') or g.owner_id)}
                user_ids.update(int(x.strip()) for x in (env.get('SPAWN_ALLOW_USERS') or '').split(',')
                                if x.strip() and x.strip() != '0')
                for user_id in user_ids:
                    member = g.get_member(user_id) or await g.fetch_member(user_id)
                    overwrites[member] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, send_messages_in_threads=True,
                        read_message_history=True, use_application_commands=True)
                cat = await g.create_category('chert', overwrites=overwrites)
            updates = {}
            env = read_env()
            for name, key, topic in channels:
                ch = existing_channel(g, cat, name, key, env)
                if ch is None:
                    ch = await g.create_text_channel(name, category=cat, topic=topic)
                    print(f"  created #{name} ({ch.id})")
                else:
                    print(f"  found   #{name} ({ch.id})")
                    if not ch.topic:
                        try:
                            await ch.edit(topic=topic)
                        except discord.HTTPException:
                            pass
                updates[key] = str(ch.id)
            env = read_env()
            updates['CHERT_BACKEND'] = backend
            if backend == 'both':
                updates['DISCORD_CHANNEL_ID'] = updates['DISCORD_CODEX_CHANNEL_ID']
            if not env.get("DISCORD_OWNER_ID"):
                updates["DISCORD_OWNER_ID"] = str(g.owner_id)
            write_env(updates)
            result.update(updates)
            print("\nwrote to .env: " + ", ".join(f"{k}={v}" for k, v in updates.items()))
            if backend == 'both':
                print('\nstart the bridge and type a prompt in #codex or #claude 🔭')
            else:
                command = 'codex' if backend == 'codex' else 'claude'
                print(f"\nstart the bridge and type  /{command} hello  in #{channels[0][0]}  🔭")
        except discord.Forbidden as e:
            print(f"\nthe bot lacks a permission: {e}. Re-invite it with:\n  {invite_url(client.user.id)}")
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.PrivilegedIntentsRequired:
        print("\n❌ Message Content intent is OFF. Developer Portal → your app → Bot → Privileged Gateway\n"
              "   Intents → enable MESSAGE CONTENT INTENT, save, then run me again.")
    except discord.LoginFailure:
        print("\n❌ Discord rejected the token. Copy it again from Developer Portal → Bot → Reset Token.")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only log in and report; change nothing")
    ap.add_argument("--guild", help="server name or id when the bot is in several")
    ap.add_argument("--token", help="bot token (else .env / hidden prompt)")
    ap.add_argument('--backend', choices=('both', 'codex', 'claude'), help='default: CHERT_BACKEND or both')
    a = ap.parse_args()
    env = read_env()
    backend = a.backend or os.environ.get('CHERT_BACKEND') or env.get('CHERT_BACKEND') or 'both'
    token = a.token or os.environ.get('DISCORD_BOT_TOKEN') or env.get("DISCORD_BOT_TOKEN") or ""
    if not token:
        token = getpass.getpass("Discord bot token (hidden; Developer Portal → Bot → Reset Token): ").strip()
        if not token:
            sys.exit("no token")
        if not a.check:
            write_env({"DISCORD_BOT_TOKEN": token})
            print("saved the token to .env (chmod 600)")
    app_id = app_id_from_token(token)
    if app_id:
        print(f"invite link (also shown if the bot turns out not to be in a server yet):\n  {invite_url(app_id)}\n")
    result = asyncio.run(build(token, a.guild, a.check, backend))
    if (a.check and not result.get('checked')) or not result:
        sys.exit(1)


if __name__ == "__main__":
    main()
