"""Keep rapid project-channel prompts in the session their first message opens."""

import asyncio
from dataclasses import dataclass, field


@dataclass
class Burst:
    last_received: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: int = 0
    started: bool = False
    thread: object | None = None


class ProjectPromptBursts:
    # Allow a follow-up just after Discord finishes creating the first thread.
    window = 5.0

    def __init__(self):
        self.bursts = {}

    def forget(self, key):
        self.bursts.pop(key, None)

    async def deliver(self, key, launch, followup):
        now = asyncio.get_running_loop().time()
        for old_key, burst in list(self.bursts.items()):
            if not burst.pending and now - burst.last_received > self.window:
                self.forget(old_key)
        burst = self.bursts.setdefault(key, Burst(now))
        burst.last_received = now
        burst.pending += 1
        try:
            # Claim the destination before audio transcription, uploads, or launch
            # can yield. FIFO locking also preserves arrival order for follow-ups.
            async with burst.lock:
                if not burst.started:
                    burst.started = True
                    burst.thread = await launch()
                elif burst.thread is None:
                    raise ValueError("The first message could not start a session. Please retry.")
                else:
                    await followup(burst.thread)
                return burst.thread
        finally:
            burst.pending -= 1
            if burst.thread is None and self.bursts.get(key) is burst:
                self.forget(key)
