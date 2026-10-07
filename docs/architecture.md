# Application structure

Chert has one project-based Discord gateway. Harnesses own their session behavior;
the gateway owns authorization, project selection, and Discord command routing.

```text
chert/
  application.py          application assembly and process lock
  __main__.py             python -m chert
  config.py, paths.py     configuration and deployment-relative paths
  projects.py             project records and directory matching
  persistence.py          atomic JSON writes
  provisioning.py         Discord setup and explicit channel resets
  discord/
    frontend.py           the single gateway and project/session routing
    commands.py           supported session command schema
    projects.py           project creation, archiving, and harness selection
    models.py, prompts.py model picker and native approval/input UI
  backends/
    base.py               small Harness interface
    claude.py             Claude launch, command, and thread adapter
    codex/
      backend.py          lifecycle and Harness implementation
      client.py           native app-server RPC transport
      discovery.py        directory-scoped session discovery
      events.py           event processing, journaling, and activity cards
      controls.py         session commands and interaction controls
      state.py            durable session records
      terminal.py         optional native terminal attachment
      storage.py          native transcript backup/restore
      presentation.py     normalization for upstream renderers
  web/
    bridge_api.py         authenticated loopback session-control API
    dashboard.py          dashboard routes
    proxy.py              optional authenticated reverse proxy
  vendor/
    bridge.py             audited upstream Claude/session utilities
    checkin.py            audited transcript parser and Claude dashboard
    storage.py            upstream disk/S3 utilities
    __init__.py           upstream import/path compatibility boundary
```

## Composition

The frontend routes through a harness registry. Each harness implements thread
ownership, message delivery, slash-command execution, and project launches.
Codex composes its RPC client, discovery service, event processor, command handler,
and terminal controller; these are separate objects, not an inheritance chain.

Project commands and the local HTTP API are also separate components. The
frontend subclasses the audited upstream bridge only to reuse its Discord
rendering and Claude session utilities. Task-local project context adapts those
upstream methods that expect one parent channel, without changing a shared global
channel when two projects receive messages concurrently.

## State and compatibility

State files remain in the deployment root: `.env`, `bot_state.json`, and `private/`.
The package's location does not determine the location of a session file or a
project directory. The vendor boundary adapts upstream's old import names and
root-relative paths, including static assets, templates, protected directories,
and backup inventory. Vendored source files remain byte-for-byte audited copies.

The small root `chert.py`, `dashboard.py`, `dashboard_proxy.py`, `setup_discord.py`,
and `ash_twin.py` files are intentional entrypoints for installed systemd units,
operator commands, and existing offload markers. They contain no application logic.
`hooks/` and `bin/` are separately installed executable helpers. Static assets and
templates retain their established paths so existing configuration keeps working.

There is no standalone Codex bot, exec queue, compatibility harness-channel
frontend, harness-wide broadcast/chat bridge, or duplicate command registry in
the application. Upstream-only implementations remain isolated in the vendor
reference and are not registered by the project frontend.

## Validation

Run unit tests with `.venv/bin/python -m unittest discover -s tests -v`.
`tests/live_codex_activity.py` exercises the real native runtime with an isolated
Codex home and in-memory Discord channels. `--model` checks model switching;
`--projects` checks project launches and thread ownership across harness changes.
Tests must not publish test threads or run prompts in production conversations.
