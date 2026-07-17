"""SleepTimer — timer enforcement + session reconciliation.

Every tick runs ``sessions.enforce``: due timers get their fade-out and
stop, and rows whose room is no longer playing our file (user stopped
it another way, the file reached its natural end, something else took
the room) are deactivated so the dashboard and voice status stay
honest. The row is the durable truth — a core restart mid-nap loses
nothing, and a finite sound file is the fail-safe even if this worker
is disabled.
"""

from __future__ import annotations

import logging

from domovoi.sdk import PluginSDK, Worker

from domovoi_plugin_sleep import sessions

log = logging.getLogger(__name__)


class SleepTimer(Worker):
    name = "sleep_timer"
    enabled_setting = "timer_enabled"
    interval_setting = "timer_interval_sec"
    stub_suppressed = True          # deterministic test runs drive tick() directly
    requires_online = False

    def __init__(self, sdk: PluginSDK) -> None:
        self.sdk = sdk

    async def tick(self) -> None:
        await sessions.enforce(self.sdk)
