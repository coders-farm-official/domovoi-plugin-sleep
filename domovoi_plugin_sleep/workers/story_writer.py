"""StoryWriter — writes Storytime stories a passage at a time.

Each tick runs ``stories.tick``: every live story whose listener is
getting close to the end of what's written gets its next passage
(written by the house's Q&A model, voiced in the room's voice, queued in
the room). Starting a story also kicks a tick at once, so the first
passage doesn't wait for the interval.
"""

from __future__ import annotations

from domovoi.sdk import PluginSDK, Worker

from domovoi_plugin_sleep import stories


class StoryWriter(Worker):
    name = "story_writer"
    enabled_setting = "story_enabled"
    interval_setting = "story_interval_sec"
    stub_suppressed = True          # tests drive stories.tick() directly
    requires_online = False         # the model and the voices are local

    def __init__(self, sdk: PluginSDK) -> None:
        self.sdk = sdk

    async def tick(self) -> None:
        await stories.tick(self.sdk)
