"""Discord controls for native backend approval and user-input requests."""
import json
import discord
from jsonschema import validators, ValidationError
from referencing import Registry


class PromptView(discord.ui.View):
    def __init__(self, adapter, key):
        super().__init__(timeout=None)
        self.adapter, self.key = adapter, key

    async def interaction_check(self, interaction):
        if not self.adapter.allowed(interaction.user.id, interaction.channel):
            await interaction.response.send_message('This session is restricted.', ephemeral=True)
            return False
        if self.key not in self.adapter.live.server_requests:
            await interaction.response.send_message('That prompt expired; use the newest prompt.', ephemeral=True)
            return False
        return True

    def decision(self, label, response, style):
        button = discord.ui.Button(label=label, style=style,
                                   custom_id=f'cx|{self.key}|{len(self.children)}')
        async def clicked(interaction):
            await interaction.response.defer()
            await self.adapter.live.answer(self.key, response)
            await interaction.message.edit(content=f'-# ✅ answered: {label}', view=None)
        button.callback = clicked
        self.add_item(button)


class InputModal(discord.ui.Modal):
    def __init__(self, adapter, key, questions):
        super().__init__(title='Codex needs input')
        self.adapter, self.key, self.questions = adapter, key, questions
        for question in questions[:5]:
            options = ', '.join(o.get('label', '') for o in question.get('options') or [])
            self.add_item(discord.ui.TextInput(label=(question.get('header') or question['question'])[:45],
                placeholder=(options or question['question'])[:100], style=discord.TextStyle.paragraph))

    async def on_submit(self, interaction):
        if not self.adapter.allowed(interaction.user.id, interaction.channel):
            return await interaction.response.send_message('This session is restricted.', ephemeral=True)
        answers = {q['id']: {'answers': [field.value]} for q, field in zip(self.questions, self.children)}
        await self.adapter.live.answer(self.key, {'answers': answers})
        await interaction.response.send_message('✅ Input sent.', ephemeral=True)


class FormModal(discord.ui.Modal):
    def __init__(self, adapter, key, schema, answers=None, offset=0):
        super().__init__(title='Tool needs input')
        self.adapter, self.key, self.schema = adapter, key, schema
        self.answers, self.offset = dict(answers or {}), offset
        properties = schema.get('properties', {}) if isinstance(schema, dict) else {}
        self.fields = list(properties.items())[offset:offset + 5]
        self.raw = not properties
        if self.raw:
            self.add_item(discord.ui.TextInput(label='Response (JSON)', style=discord.TextStyle.paragraph))
        for name, prop in self.fields:
            hint = prop.get('description') or str(prop.get('enum') or prop.get('type') or '')
            self.add_item(discord.ui.TextInput(label=(prop.get('title') or name)[:45],
                placeholder=hint[:100], required=name in schema.get('required', []),
                style=discord.TextStyle.paragraph))

    async def on_submit(self, interaction):
        if not self.adapter.allowed(interaction.user.id, interaction.channel):
            return await interaction.response.send_message('This session is restricted.', ephemeral=True)
        try:
            if self.raw:
                self.answers = json.loads(self.children[0].value)
            for (name, prop), field in zip(self.fields, self.children):
                if not field.value and name not in self.schema.get('required', []):
                    continue
                kind = prop.get('type', 'string')
                if isinstance(kind, list):
                    kind = next((v for v in kind if v != 'null'), 'string')
                self.answers[name] = field.value if kind == 'string' else json.loads(field.value)
            offset = self.offset + len(self.fields)
            if not self.raw and offset < len(self.schema.get('properties', {})):
                view = PromptView(self.adapter, self.key)
                button = discord.ui.Button(label='Continue', style=discord.ButtonStyle.primary)
                async def more(next_interaction):
                    await next_interaction.response.send_modal(FormModal(self.adapter, self.key, self.schema, self.answers, offset))
                button.callback = more
                view.add_item(button)
                return await interaction.response.send_message('More fields remain.', view=view, ephemeral=True)
            # An empty registry deliberately disables network retrieval of schema refs.
            validators.validator_for(self.schema)(self.schema, registry=Registry()).validate(self.answers)
            await self.adapter.live.answer(self.key, {'action': 'accept', 'content': self.answers})
        except Exception as exc:
            return await interaction.response.send_message(f'Invalid input: {str(exc)[:1000]}', ephemeral=True)
        await interaction.response.send_message('✅ Input sent.', ephemeral=True)


def request_view(adapter, key, request):
    method, params = request['method'], request.get('params') or {}
    view = PromptView(adapter, key)
    details = params.get('command') or params.get('reason') or params.get('message') or method
    body = f'🟠 **Codex needs your input**\n```\n{str(details)[:1400]}\n```'
    if method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
        view.decision('Allow once', {'decision': 'accept'}, discord.ButtonStyle.success)
        view.decision('Allow for session', {'decision': 'acceptForSession'}, discord.ButtonStyle.primary)
        view.decision('Deny', {'decision': 'decline'}, discord.ButtonStyle.secondary)
        view.decision('Cancel turn', {'decision': 'cancel'}, discord.ButtonStyle.danger)
        amendment = params.get('proposedExecpolicyAmendment')
        if amendment and len(json.dumps(amendment)) < 300:
            body += '\nProposed command rule: `' + json.dumps(amendment) + '`'
            view.decision('Allow matching commands', {'decision': {'acceptWithExecpolicyAmendment':
                          {'execpolicy_amendment': amendment}}}, discord.ButtonStyle.secondary)
    elif method == 'item/permissions/requestApproval':
        permissions = params.get('permissions') or {}
        body += '\nRequested permissions: `' + json.dumps(permissions)[:400] + '`'
        view.decision('Allow for this turn', {'permissions': permissions, 'scope': 'turn'}, discord.ButtonStyle.success)
        view.decision('Deny', {'permissions': {}, 'scope': 'turn'}, discord.ButtonStyle.danger)
    elif method in {'item/tool/requestUserInput', 'tool/requestUserInput'}:
        questions = params.get('questions') or []
        body = '🟠 **Codex needs your input**\n' + '\n'.join(q['question'] for q in questions)
        button = discord.ui.Button(label='Answer', style=discord.ButtonStyle.primary)
        async def answer(interaction):
            await interaction.response.send_modal(InputModal(adapter, key, questions))
        button.callback = answer
        view.add_item(button)
    elif method == 'mcpServer/elicitation/request':
        if params.get('mode') in {'form', 'openai/form', 'openaiForm'}:
            button = discord.ui.Button(label='Fill form', style=discord.ButtonStyle.primary)
            async def form(interaction):
                await interaction.response.send_modal(FormModal(adapter, key, params['requestedSchema']))
            button.callback = form
            view.add_item(button)
        elif params.get('mode') == 'url':
            url = params.get('url', '')
            if url.startswith(('https://', 'http://')):
                view.add_item(discord.ui.Button(label='Open requested page', url=url))
            view.decision('Continue after completing', {'action': 'accept'}, discord.ButtonStyle.success)
        view.decision('Decline', {'action': 'decline'}, discord.ButtonStyle.secondary)
        view.decision('Cancel', {'action': 'cancel'}, discord.ButtonStyle.danger)
    else:
        body += '\nThis request must be answered in the original Codex client.'
    return body[:1900], view
