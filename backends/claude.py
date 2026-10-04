"""Claude adapter: delegate behavior to the unmodified upstream implementation."""
import re

import discord_bot as upstream


class ClaudeBackend:
    name = 'claude'

    def __init__(self, frontend):
        self.frontend = frontend

    async def start(self):
        await upstream.Bridge.setup_hook(self.frontend)

    async def message(self, message):
        host = self.frontend
        content = (message.content or '').strip()
        control = content.split(maxsplit=1)[0].lower() if content else ''
        if not host.claude_enabled and control not in {'!help', '!sessions', '!status', '!threads', '!ls',
                                                       '!disk', '!s3', '!backup', '!offload', '!restore', '!astra'}:
            return await host.say(message.channel, 'Claude is disabled. Use #codex for now.')
        if message.channel.id == host.claude_channel_id and not content.startswith('!'):
            text = re.sub(rf'<@!?{host.user.id}>', '', content).strip()
            if text or message.attachments:
                return await host.summon(message, text)
        return await upstream.Bridge.on_message(host, message)

    async def execute(self, name, original, interaction, kwargs):
        host = self.frontend
        if not host.claude_enabled and name not in {'help', 'sessions', 'disk', 's3', 'backup', 'offload', 'restore'}:
            return await interaction.response.send_message('Claude is disabled. Use #codex for now.', ephemeral=True)
        if name == 'claude' and interaction.channel_id != host.claude_channel_id:
            from shared_frontend import ChannelInteraction
            interaction = ChannelInteraction(interaction, host.main_channel)
        if name == 'fork' and kwargs.get('to') == 'codex':
            kwargs = {**kwargs, 'to': 'astra'}
        return await original(interaction, **kwargs)
