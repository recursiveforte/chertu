"""The small interface required by the Discord gateway from every harness."""

from typing import Protocol


class Harness(Protocol):
    name: str
    enabled: bool

    @property
    def active_count(self) -> int: ...

    def session_lines(self, project) -> list[str]: ...

    def owns_thread(self, channel_id: int) -> bool: ...

    async def message(self, message): ...

    async def command(self, name, original, interaction, arguments): ...

    async def launch(self, project, prompt, user, respond, source=None, worktree=None): ...
