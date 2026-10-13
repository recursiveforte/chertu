"""Project lifecycle and its Discord command UI."""

import asyncio
import os
import discord
from chert.projects import create_project_channel
from chert.worktrees import validate_repository


class ProjectCommands:
    def __init__(self, frontend, lock):
        self.frontend, self.lock = frontend, lock

    @property
    def registry(self):
        return self.frontend.projects

    @property
    def channels(self):
        return self.frontend.project_channels

    async def project_permission(self, interaction):
        if interaction.guild_id != self.registry.guild_id or not self.frontend.allowed_user(
            interaction.user
        ):
            await interaction.response.send_message(
                "This Chert instance is restricted.", ephemeral=True
            )
            return False
        return True

    async def category(self, archived=False):
        key = "archive_category_id" if archived else "category_id"
        category_id = getattr(self.registry, key)
        if category_id:
            return self.frontend.get_channel(category_id) or await self.frontend.fetch_channel(
                category_id
            )
        raise ValueError("Project categories are missing. Run setup_discord.py again.")

    async def create_project(self, name, directory, host="local"):
        async with self.lock:
            if host not in self.frontend.hosts:
                raise ValueError(f"Unknown host: {host}. Use /hosts to list configured hosts.")
            remote = self.frontend.codex_hosts[host].remote
            if remote:
                directory = await remote.run("directory", directory=directory)
            project = self.registry.prepare(
                name,
                directory,
                self.frontend.config.project_root,
                "codex" if remote else os.environ.get("DEFAULT_HARNESS") or "codex",
                host=host,
            )
            category = await self.category()
            channel = await create_project_channel(self.registry, project, category.guild, category)
            self.channels[channel.id] = channel
            if self.frontend._main_channel is None:
                self.frontend.main_channel = self.frontend.codex.main_channel = channel
            return project

    async def set_archived(self, project, archived):
        async with self.lock:
            channel = self.channels.get(project.channel_id) or await self.frontend.fetch_channel(
                project.channel_id
            )
            channel = await channel.edit(
                category=await self.category(archived),
                reason="Project archived" if archived else "Project reopened",
            )
            project.archived = archived
            self.channels[project.channel_id] = channel
            self.registry.save()

    async def set_harness(self, project, harness):
        if harness not in {"codex", "claude"}:
            raise ValueError("Choose codex or claude.")
        if project.host != "local" and harness != "codex":
            raise ValueError("Remote hosts currently support Codex.")
        async with self.lock:
            previous = project.harness
            project.harness = harness
            try:
                self.registry.save()
            except Exception:
                project.harness = previous
                raise

    async def sync_topic(self, project):
        channel = self.channels.get(project.channel_id) or await self.frontend.fetch_channel(
            project.channel_id
        )
        if getattr(channel, "topic", None) != project.topic:
            self.channels[project.channel_id] = await channel.edit(
                topic=project.topic, reason="Update project defaults"
            )

    async def set_fast_default(self, project, mode):
        if mode not in {"on", "off", "inherit"}:
            raise ValueError("Choose on, off, or inherit.")
        async with self.lock:
            previous = project.fast_default
            project.fast_default = {"on": True, "off": False, "inherit": None}[mode]
            try:
                self.registry.save()
            except Exception:
                project.fast_default = previous
                raise
            try:
                await self.sync_topic(project)
            except discord.HTTPException as exc:
                raise ValueError(
                    "The fast default was saved, but the channel topic could not be updated. "
                    "Retry /fast-default with the same setting to refresh it."
                ) from exc

    async def set_worktrees(self, project, enabled):
        async with self.lock:
            if enabled:
                remote = self.frontend.codex_for(project=project).remote
                if remote:
                    await remote.run("repository", directory=project.directory)
                else:
                    await asyncio.to_thread(validate_repository, project.directory)
            previous = project.worktrees
            project.worktrees = enabled
            try:
                self.registry.save()
            except Exception:
                project.worktrees = previous
                raise
            try:
                await self.sync_topic(project)
            except discord.HTTPException as exc:
                raise ValueError(
                    "The workspace default was saved, but the channel topic could not be updated. "
                    "Retry /worktrees with the same setting to refresh it."
                ) from exc

    def install(self):
        @self.frontend.tree.command(
            name="fast-default",
            description="View or set the fast-mode default for new Codex sessions in this project",
        )
        @discord.app_commands.choices(mode=[
            discord.app_commands.Choice(name="on", value="on"),
            discord.app_commands.Choice(name="off", value="off"),
            discord.app_commands.Choice(name="inherit", value="inherit"),
        ])
        async def fast_default_command(interaction: discord.Interaction, mode: str = ""):
            if not await self.project_permission(interaction):
                return
            project = self.registry.for_channel(interaction.channel)
            if project is None:
                return await interaction.response.send_message(
                    "Use /fast-default in a project channel or its thread.", ephemeral=True
                )
            if mode and interaction.user.id != self.frontend.owner:
                return await interaction.response.send_message(
                    "/fast-default changes are owner-only.", ephemeral=True
                )
            await interaction.response.defer(ephemeral=True)
            if mode:
                await self.set_fast_default(project, mode)
            setting = (
                "inherit" if project.fast_default is None
                else "on" if project.fast_default else "off"
            )
            inherited = ""
            if setting == "inherit":
                tier = self.frontend.codex_for(project=project).store.meta.get("service_tier")
                inherited = (
                    f" (host default: {'on' if tier == 'fast' else 'off'})"
                    if tier else " (Codex's own default)"
                )
            await interaction.followup.send(
                f"New Codex sessions in <#{project.channel_id}> use fast mode **{setting}**{inherited}. "
                "Set `/fast-default mode:on`, `mode:off`, or `mode:inherit` to change this. "
                "Existing threads keep their settings; use `/fast` inside a thread to change it.",
                ephemeral=True,
            )

        @self.frontend.tree.command(name="hosts", description="Show configured execution hosts and their connection status")
        async def hosts_command(interaction: discord.Interaction):
            if not await self.project_permission(interaction):
                return
            lines = [f"**{name}** · {'connected' if backend.live.connected else 'not connected'}"
                     for name, backend in self.frontend.codex_hosts.items()]
            await interaction.response.send_message(
                "\n".join(lines) + "\nChoose a host with `/project name:… dir:… host:…`.", ephemeral=True)

        @self.frontend.tree.command(
            name="worktrees",
            description="View or set whether new sessions in this project create worktrees",
        )
        async def worktrees_command(interaction: discord.Interaction, enabled: bool | None = None):
            if not await self.project_permission(interaction):
                return
            project = self.registry.for_channel(interaction.channel)
            if project is None:
                return await interaction.response.send_message(
                    "Use /worktrees in a project channel.", ephemeral=True
                )
            await interaction.response.defer(ephemeral=True)
            if enabled is not None:
                await self.set_worktrees(project, enabled)
            await interaction.followup.send(
                f"New sessions in <#{project.channel_id}> use "
                + ("**a fresh worktree**." if project.worktrees else "**the project directory**.")
                + " Set `/worktrees enabled:True` or `enabled:False` to change this."
                + " Override once with `/worktree prompt:…` or `/no-worktree prompt:…`."
                + " Existing threads keep their directories.",
                ephemeral=True,
            )

        @self.frontend.tree.command(
            name="project", description="Create a project channel for a directory on an execution host"
        )
        async def project_command(interaction: discord.Interaction, name: str, dir: str, host: str = "local"):
            if not await self.project_permission(interaction):
                return
            await interaction.response.defer(ephemeral=True)
            project = await self.create_project(name, dir, host)
            await interaction.followup.send(
                f"Created <#{project.channel_id}> · **{project.host}** · `{project.directory}` · {project.harness}",
                ephemeral=True,
            )

        @project_command.autocomplete("host")
        async def host_names(interaction, current):
            if not self.frontend.allowed_user(interaction.user):
                return []
            return [discord.app_commands.Choice(name=name, value=name)
                    for name in self.frontend.hosts if current.lower() in name][:25]

        @self.frontend.tree.command(
            name="harness",
            description="Choose the default harness for new sessions in this project",
        )
        @discord.app_commands.choices(
            name=[
                discord.app_commands.Choice(name="Codex", value="codex"),
                discord.app_commands.Choice(name="Claude", value="claude"),
            ]
        )
        async def harness_command(interaction: discord.Interaction, name: str = ""):
            if not await self.project_permission(interaction):
                return
            project = self.registry.for_channel(interaction.channel)
            if project is None:
                return await interaction.response.send_message(
                    "Use /harness in a project channel.", ephemeral=True
                )
            if not name:
                view = HarnessPicker(self, project, interaction.user.id)
                return await interaction.response.send_message(
                    f"Default harness: **{project.harness}**. Choose a harness for new sessions.",
                    view=view,
                    ephemeral=True,
                )
            await interaction.response.defer(ephemeral=True)
            await self.set_harness(project, name)
            await interaction.followup.send(
                f"New sessions in <#{project.channel_id}> use **{name}**. Existing threads keep their harness.",
                ephemeral=True,
            )

        async def archive_command(interaction, name, archived):
            if not await self.project_permission(interaction):
                return
            project = (
                self.registry.projects.get(name)
                if name
                else self.registry.for_channel(interaction.channel)
            )
            if project is None:
                return await interaction.response.send_message(
                    "Choose a project name or run this in its channel.", ephemeral=True
                )
            await interaction.response.defer(ephemeral=True)
            await self.set_archived(project, archived)
            await interaction.followup.send(
                f"{'Archived' if archived else 'Reopened'} <#{project.channel_id}>.", ephemeral=True
            )

        @self.frontend.tree.command(
            name="archive", description="Move this project to the archived section"
        )
        async def archive(interaction: discord.Interaction, name: str = ""):
            await archive_command(interaction, name, True)

        @self.frontend.tree.command(
            name="unarchive", description="Move a project back to the projects section"
        )
        async def unarchive(interaction: discord.Interaction, name: str = ""):
            await archive_command(interaction, name, False)

        for command in (archive, unarchive):

            @command.autocomplete("name")
            async def names(interaction, current):
                return [
                    discord.app_commands.Choice(name=p.name, value=p.name)
                    for p in self.registry.projects.values()
                    if current.lower() in p.name
                ][:25]


class HarnessPicker(discord.ui.View):
    def __init__(self, frontend, project, user_id):
        super().__init__(timeout=120)
        self.frontend, self.project, self.user_id = frontend, project, user_id
        select = discord.ui.Select(
            options=[
                discord.SelectOption(label=h.title(), value=h, default=h == project.harness)
                for h in ("codex", "claude")
            ]
        )
        select.callback = self.choose
        self.select = select
        self.add_item(select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Open your own /harness picker.", ephemeral=True
            )
            return False
        return await self.frontend.project_permission(interaction)

    async def choose(self, interaction):
        await interaction.response.defer()
        await self.frontend.set_harness(self.project, self.select.values[0])
        await interaction.edit_original_response(
            content=f"Default harness: **{self.project.harness}**. Existing threads keep their harness.",
            view=None,
        )
        self.stop()
