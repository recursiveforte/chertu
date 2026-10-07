#!/usr/bin/env python3
"""Provision private project channels and an archived category.

Normal setup preserves existing channels. --project NAME DIR registers a project.
--reset-channels --guild NAME explicitly deletes every channel in that server,
including their messages and threads, before creating the project layout.
"""

import argparse
import asyncio
import base64
import getpass
import json
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values
from chert.projects import ProjectStore, create_project_channel

try:
    import discord
except ImportError:
    sys.exit(
        "discord.py missing — run:  .venv/bin/pip install -r requirements.txt  (or ./setup.sh)"
    )

from chert.paths import ROOT

HERE = ROOT
ENV = HERE / ".env"


def read_env():
    return dict(dotenv_values(ENV)) if ENV.exists() else {}


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
    with os.fdopen(fd, "w") as file:
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
        view_channel=True,
        send_messages=True,
        send_messages_in_threads=True,
        create_public_threads=True,
        manage_threads=True,
        manage_messages=True,  # pin the board
        manage_channels=True,  # create the three channels / rename threads
        manage_webhooks=True,  # one webhook per channel: each claude posts under its own name
        embed_links=True,
        attach_files=True,
        add_reactions=True,
        read_message_history=True,
        use_application_commands=True,
        mention_everyone=False,
    )


def invite_url(app_id):
    return discord.utils.oauth_url(
        app_id, permissions=needed_permissions(), scopes=("bot", "applications.commands")
    )


async def provision_projects(guild, env, reset=False, initial_projects=()):
    """Only the explicit reset option deletes channels; normal setup is idempotent."""
    store = ProjectStore(env.get("PROJECT_STATE_FILE") or HERE / "private/projects.json")
    if store.guild_id and store.guild_id != guild.id:
        raise ValueError("The project registry belongs to a different server.")
    root = Path(env.get("PROJECT_ROOT") or Path.home() / "projects").expanduser()
    seeds = list(initial_projects)
    if not seeds and not store.projects:
        seeds = [("chert", str(HERE))]
    # Validate every directory before any destructive operation.
    planned = []
    from copy import copy

    validator = copy(store)
    validator.projects = dict(store.projects)
    for name, directory in seeds:
        project = validator.prepare(name, directory, root, env.get("DEFAULT_HARNESS") or "codex")
        validator.projects[project.name] = project
        planned.append(project)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, send_messages_in_threads=True
        ),
    }
    user_ids = {int(env.get("DISCORD_OWNER_ID") or guild.owner_id)}
    user_ids.update(
        int(x.strip()) for x in (env.get("SPAWN_ALLOW_USERS") or "").split(",") if x.strip()
    )
    for user_id in user_ids:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
            read_message_history=True,
            use_application_commands=True,
        )
    if reset:
        # Preserve configuration and old IDs for diagnosis; this is not a message backup.
        from datetime import datetime, timezone

        folder = HERE / "private/channel-resets"
        folder.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "guild_id": guild.id,
            "guild_name": guild.name,
            "channels": [
                {
                    "id": ch.id,
                    "name": ch.name,
                    "type": str(ch.type),
                    "category_id": getattr(ch, "category_id", None),
                }
                for ch in guild.channels
            ],
        }
        target = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
        target.write_text(json.dumps(snapshot, indent=2))
        # Save the Discord mappings before invalidating them. Native agent histories
        # stay in their original homes and can be rediscovered in project channels.
        import shutil

        state_paths = [
            store.path,
            Path(env.get("CODEX_STATE_FILE") or HERE / "private/codex-state.json"),
            HERE / "bot_state.json",
        ]
        for path in state_paths:
            if path.exists():
                shutil.copy2(path, folder / f"{target.stem}-{path.name}")
        # Children before categories, using the exact selected guild's channel inventory.
        for channel in sorted(guild.channels, key=lambda c: isinstance(c, discord.CategoryChannel)):
            await channel.delete(reason="Owner requested reset to project channels")
            print(f"  deleted {channel.name} ({channel.id})")
        store.category_id = store.archive_category_id = 0
        for project in store.projects.values():
            project.channel_id = 0
        codex_path = state_paths[1]
        if codex_path.exists():
            from chert.backends.codex.state import SessionStore

            sessions = SessionStore(codex_path)
            sessions.sessions.clear()
            for key in ("board_msg", "board_body", "hub"):
                sessions.meta.pop(key, None)
            sessions.save()
        claude_path = state_paths[2]
        if claude_path.exists():
            data = json.loads(claude_path.read_text())
            meta = data.get("_meta", {})
            for key in ("board_msg", "board_body"):
                meta.pop(key, None)
            temp = claude_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps({"_meta": meta}))
            temp.replace(claude_path)
    store.guild_id = guild.id
    categories = {}
    for name, key in [("projects", "category_id"), ("archived", "archive_category_id")]:
        category = guild.get_channel(getattr(store, key))
        if category is None and not reset:
            category = discord.utils.get(guild.categories, name=name)
        if category is None:
            category = await guild.create_category(name, overwrites=overwrites)
        setattr(store, key, category.id)
        categories[name] = category
    store.projects.update({p.name: p for p in planned})
    store.save()
    for project in store.projects.values():
        channel = guild.get_channel(project.channel_id) if project.channel_id else None
        if channel is None:
            category = categories["archived" if project.archived else "projects"]
            channel = await create_project_channel(store, project, guild, category)
            print(f"  created project #{project.name} ({channel.id})")
    updates = {
        "DISCORD_GUILD_ID": str(guild.id),
        "CHERT_BACKEND": env.get("CHERT_BACKEND") or "both",
        "DISCORD_OWNER_ID": str(env.get("DISCORD_OWNER_ID") or guild.owner_id),
        "PROJECT_STATE_FILE": json.dumps(str(store.path)),
    }
    # No harness-specific channel may be fetched or recreated on restart.
    for key in (
        "DISCORD_CHANNEL_ID",
        "DISCORD_CODEX_CHANNEL_ID",
        "DISCORD_CLAUDE_CHANNEL_ID",
        "DISCORD_CODEX_BROADCAST_CHANNEL_ID",
        "DISCORD_CODEX_CHAT_CHANNEL_ID",
        "DISCORD_CHAT_CHANNEL_ID",
        "DISCORD_BROADCAST_CHANNEL_ID",
        "CODEX_PROMPT_CHANNEL_ID",
        "PROMPT_CHANNEL_ID",
    ):
        updates[key] = "0"
    write_env(updates)
    return updates


async def build(
    token, want_guild, check_only, backend="codex", reset_channels=False, initial_projects=()
):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    client = discord.Client(intents=intents)
    result = {}
    started = False

    @client.event
    async def on_ready():
        nonlocal started
        if started:
            return
        started = True
        try:
            app_id = client.user.id
            guilds = list(client.guilds)
            print(f"logged in as {client.user} (application id {app_id})")
            if not guilds:
                print(
                    "\nThe bot is not in any server yet. Invite it with this link (it has exactly the\n"
                    "permissions chert needs + the applications.commands scope for slash commands):\n\n"
                    f"  {invite_url(app_id)}\n"
                )
                if check_only:
                    return
                print("waiting up to 10 minutes for the bot to join a server…")
                for _ in range(120):
                    await asyncio.sleep(5)
                    guilds = list(client.guilds)
                    if guilds:
                        break
                if not guilds:
                    print("still not in a server — run me again after inviting it.")
                    return
            if want_guild:
                matches = [x for x in guilds if x.name == want_guild or str(x.id) == want_guild]
                if len(matches) > 1:
                    raise ValueError(
                        "Multiple servers have that name. Select the exact server ID with --guild."
                    )
                g = next(iter(matches), None)
                if not g:
                    print(
                        f"no server named/id {want_guild!r}; the bot is in: "
                        + ", ".join(f"{x.name} ({x.id})" for x in guilds)
                    )
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
                store = ProjectStore(
                    read_env().get("PROJECT_STATE_FILE") or HERE / "private/projects.json"
                )
                for project in store.projects.values():
                    ch = g.get_channel(project.channel_id)
                    print(f"  #{project.name:<20} {'exists ' + str(ch.id) if ch else 'missing'}")
                me = g.me
                missing = [
                    p for p, v in needed_permissions() if v and not getattr(me.guild_permissions, p)
                ]
                print("  permissions:", "ok" if not missing else "MISSING " + ", ".join(missing))
                result["checked"] = (
                    not missing
                    and store.guild_id == g.id
                    and all(
                        g.get_channel(p.channel_id) is not None for p in store.projects.values()
                    )
                )
                return
            missing = [
                p
                for p, needed in needed_permissions()
                if needed and not getattr(g.me.guild_permissions, p)
            ]
            if missing:
                raise ValueError(
                    "The bot needs these permissions before provisioning: " + ", ".join(missing)
                )
            env = {**read_env(), "CHERT_BACKEND": backend}
            result.update(await provision_projects(g, env, reset_channels, initial_projects))
            print("Project channels configured. Use /project <name> <dir> and /harness.")
            return
        except discord.Forbidden as e:
            print(
                f"\nthe bot lacks a permission: {e}. Re-invite it with:\n  {invite_url(client.user.id)}"
            )
        finally:
            await client.close()

    try:
        await client.start(token)
    except discord.PrivilegedIntentsRequired:
        print(
            "\n❌ Message Content intent is OFF. Developer Portal → your app → Bot → Privileged Gateway\n"
            "   Intents → enable MESSAGE CONTENT INTENT, save, then run me again."
        )
    except discord.LoginFailure:
        print(
            "\n❌ Discord rejected the token. Copy it again from Developer Portal → Bot → Reset Token."
        )
    return result


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--check", action="store_true", help="only log in and report; change nothing")
    ap.add_argument("--guild", help="server name or id when the bot is in several")
    ap.add_argument("--token", help="bot token (else .env / hidden prompt)")
    ap.add_argument(
        "--backend", choices=("both", "codex", "claude"), help="default: CHERT_BACKEND or both"
    )
    ap.add_argument(
        "--reset-channels",
        action="store_true",
        help="Delete ALL channels in --guild before provisioning projects",
    )
    ap.add_argument(
        "--project",
        nargs=2,
        action="append",
        default=[],
        metavar=("NAME", "DIR"),
        help="Seed a project (repeatable)",
    )
    a = ap.parse_args()
    if a.reset_channels and (not a.guild or a.check):
        ap.error(
            "--reset-channels requires an explicit --guild and cannot be combined with --check"
        )
    env = read_env()
    backend = a.backend or os.environ.get("CHERT_BACKEND") or env.get("CHERT_BACKEND") or "both"
    token = a.token or os.environ.get("DISCORD_BOT_TOKEN") or env.get("DISCORD_BOT_TOKEN") or ""
    if not token:
        token = getpass.getpass(
            "Discord bot token (hidden; Developer Portal → Bot → Reset Token): "
        ).strip()
        if not token:
            sys.exit("no token")
        if not a.check:
            write_env({"DISCORD_BOT_TOKEN": token})
            print("saved the token to .env (chmod 600)")
    app_id = app_id_from_token(token)
    if app_id:
        print(
            f"invite link (also shown if the bot turns out not to be in a server yet):\n  {invite_url(app_id)}\n"
        )
    result = asyncio.run(build(token, a.guild, a.check, backend, a.reset_channels, a.project))
    if (a.check and not result.get("checked")) or not result:
        sys.exit(1)


if __name__ == "__main__":
    main()
