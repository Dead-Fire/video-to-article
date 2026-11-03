from __future__ import annotations

from dataclasses import dataclass


LARGE_BATCH_THRESHOLD = 20
GUI_LOG_MAX_LINES = 1200
PROCESS_OUTPUT_TAIL_LINES = 80
PROCESS_OUTPUT_TAIL_CHARS = 12000
PROCESS_HEARTBEAT_SECONDS = 60
BATCH_FUZZY_COMPARE_LIMIT = 3000


@dataclass(frozen=True)
class ResourceProfile:
    name: str
    screenshot_max: int
    screenshot_scene_only_max: int
    visual_pack_max_images: int
    scene_sample_fps: float | None
    enable_fuzzy_batch_dedupe: bool


NORMAL_PROFILE = ResourceProfile(
    name="normal",
    screenshot_max=24,
    screenshot_scene_only_max=6,
    visual_pack_max_images=14,
    scene_sample_fps=None,
    enable_fuzzy_batch_dedupe=True,
)

LARGE_BATCH_PROFILE = ResourceProfile(
    name="large_batch",
    screenshot_max=10,
    screenshot_scene_only_max=3,
    visual_pack_max_images=8,
    scene_sample_fps=1.0,
    enable_fuzzy_batch_dedupe=False,
)


def profile_for_count(count: int) -> ResourceProfile:
    if count >= LARGE_BATCH_THRESHOLD:
        return LARGE_BATCH_PROFILE
    return NORMAL_PROFILE
