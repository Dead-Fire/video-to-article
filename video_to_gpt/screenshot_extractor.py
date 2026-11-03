from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .text_utils import SubtitleEntry, normalize_space, write_text
from .resource import PROCESS_HEARTBEAT_SECONDS


LogCallback = Callable[[str], None]

SCENE_THRESHOLD = 0.35
SUBTITLE_TIME_OFFSET_SECONDS = 0.8
SCENE_ANCHOR_OFFSET_SECONDS = 0.6
SCENE_ANCHOR_LOOKAHEAD_SECONDS = 10.0
SCENE_ANCHOR_LOOKBEHIND_SECONDS = 1.0
SCENE_DEDUPE_SECONDS = 4.0
MERGE_WINDOW_SECONDS = 3.0
MIN_SCREENSHOT_INTERVAL_SECONDS = 16.0
MAX_SCREENSHOTS = 24
MAX_SCENE_ONLY_SCREENSHOTS = 6

VISUAL_TERMS = (
    "看这里",
    "这里可以看到",
    "可以看到",
    "能看到",
    "看到了",
    "如图",
    "图中",
    "画面",
    "展示",
    "演示",
    "截图",
    "图片",
    "图像",
    "屏幕",
    "页面",
    "这个地方",
    "这块",
)

SPATIAL_TERMS = (
    "左边",
    "右边",
    "上面",
    "下面",
    "界面",
    "菜单",
    "按钮",
    "面板",
    "窗口",
    "画板",
    "列表",
    "选项",
    "文件",
    "文档",
)

STRUCTURE_TERMS = (
    "流程",
    "阶段",
    "架构",
    "结构",
    "表格",
    "对比",
    "分为",
    "包括",
    "第一",
    "第二",
    "第三",
    "总结",
    "关键在于",
)

ACTION_TERMS = (
    "点击",
    "打开",
    "选择",
    "拖",
    "复制",
    "粘贴",
    "下载",
    "添加",
    "创建",
    "运行",
    "输入",
    "替换",
    "生成",
)

WEAK_LOOK_PATTERNS = (
    r"(?<![\u4e00-\u9fff])你看(?!法|起来)",
    r"(?<![\u4e00-\u9fff])来看",
    r"看看",
)

FALSE_VISUAL_PATTERNS = (
    r"看法",
    r"看起来",
    r"好看",
    r"难看",
)


@dataclass
class ScreenshotCandidate:
    time_seconds: float
    score: float
    reasons: list[str] = field(default_factory=list)
    subtitle_time: str = ""
    subtitle_text: str = ""
    screenshot_name: str = ""


@dataclass(frozen=True)
class ScreenshotResult:
    index_path: Path
    candidates_path: Path
    screenshot_dir: Path
    count: int
    index_text: str
    candidates: list[ScreenshotCandidate]


def extract_key_screenshots(
    *,
    video_path: Path,
    entries: list[SubtitleEntry],
    output_dir: Path,
    ffmpeg: Path,
    max_screenshots: int = MAX_SCREENSHOTS,
    max_scene_only_screenshots: int = MAX_SCENE_ONLY_SCREENSHOTS,
    scene_sample_fps: float | None = None,
    log: LogCallback,
) -> ScreenshotResult:
    log("开始分析关键截图候选点。")
    screenshot_dir = output_dir / "关键截图"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    scene_times = _detect_scene_changes(video_path, ffmpeg, log, sample_fps=scene_sample_fps)
    subtitle_candidates = _subtitle_candidates(entries, scene_times)
    log(f"字幕规则候选点：{len(subtitle_candidates)} 个。")

    scene_candidates = _scene_candidates(scene_times, entries)
    log(f"画面变化候选点：{len(scene_candidates)} 个。")

    selected = _select_candidates(
        subtitle_candidates + scene_candidates,
        max_screenshots=max_screenshots,
        max_scene_only_screenshots=max_scene_only_screenshots,
    )
    log(f"合并去重后准备截图：{len(selected)} 张。")

    for candidate in selected:
        candidate.screenshot_name = _screenshot_name(candidate.time_seconds)
        screenshot_path = screenshot_dir / candidate.screenshot_name
        _capture_frame(video_path, screenshot_path, candidate.time_seconds, ffmpeg, log)

    index_path = output_dir / "关键截图索引.md"
    candidates_path = output_dir / "关键截图候选.json"
    index_text = _format_index(selected)
    write_text(index_path, index_text)
    write_text(candidates_path, _format_candidates_json(selected))
    log(f"关键截图索引已生成：{index_path}")

    return ScreenshotResult(
        index_path=index_path,
        candidates_path=candidates_path,
        screenshot_dir=screenshot_dir,
        count=len(selected),
        index_text=index_text,
        candidates=selected,
    )


def _subtitle_candidates(entries: list[SubtitleEntry], scene_times: list[float]) -> list[ScreenshotCandidate]:
    candidates: list[ScreenshotCandidate] = []
    for entry in entries:
        text = normalize_space(entry.text)
        if not text:
            continue

        score, reasons = _score_subtitle_text(text)

        if score < 3.0:
            continue

        entry_start = srt_time_to_seconds(entry.start)
        screenshot_time = entry_start + SUBTITLE_TIME_OFFSET_SECONDS
        anchored_scene_time = _scene_anchor_after(entry_start, scene_times)
        candidate_reasons = reasons[:]
        candidate_score = score
        if anchored_scene_time is not None:
            screenshot_time = anchored_scene_time + SCENE_ANCHOR_OFFSET_SECONDS
            candidate_score += 1.0
            candidate_reasons.append("邻近画面切换")

        candidates.append(
            ScreenshotCandidate(
                time_seconds=screenshot_time,
                score=candidate_score,
                reasons=candidate_reasons,
                subtitle_time=entry.start,
                subtitle_text=text,
            )
        )
    return candidates


def _score_subtitle_text(text: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    has_visual = _contains_any(text, VISUAL_TERMS)
    has_spatial = _contains_any(text, SPATIAL_TERMS)
    has_structure = _contains_any(text, STRUCTURE_TERMS)
    has_action = _contains_any(text, ACTION_TERMS)
    has_weak_look = any(re.search(pattern, text) for pattern in WEAK_LOOK_PATTERNS)
    has_false_visual = any(re.search(pattern, text) for pattern in FALSE_VISUAL_PATTERNS)

    if has_visual:
        score += 4.0
        reasons.append("字幕视觉提示")

    if has_weak_look and (has_spatial or has_structure or has_action or len(text) >= 12):
        score += 2.0
        if "字幕视觉提示" not in reasons:
            reasons.append("字幕视觉提示")

    if has_spatial:
        score += 3.0
        reasons.append("空间/界面表达")

    if has_structure:
        score += 2.0
        reasons.append("阶段/流程表达")

    if has_action and (has_visual or has_spatial or has_weak_look):
        score += 1.5
        reasons.append("操作演示表达")

    if has_false_visual and not (has_spatial or has_structure or has_action):
        score -= 3.0

    if len(text) <= 3 and not (has_spatial or has_structure or has_action):
        score -= 3.0

    return score, reasons


def _scene_candidates(scene_times: list[float], entries: list[SubtitleEntry]) -> list[ScreenshotCandidate]:
    candidates: list[ScreenshotCandidate] = []
    for seconds in scene_times:
        entry = _nearest_entry(entries, seconds)
        candidates.append(
            ScreenshotCandidate(
                time_seconds=seconds + 0.4,
                score=1.5,
                reasons=["画面明显切换"],
                subtitle_time=entry.start if entry else "",
                subtitle_text=normalize_space(entry.text) if entry else "",
            )
        )
    return candidates


def _detect_scene_changes(
    video_path: Path,
    ffmpeg: Path,
    log: LogCallback,
    *,
    sample_fps: float | None = None,
) -> list[float]:
    video_filter = f"select='gt(scene,{SCENE_THRESHOLD})',showinfo"
    if sample_fps and sample_fps > 0:
        video_filter = f"fps={sample_fps:g},{video_filter}"
        log(f"画面变化检测使用抽样模式：每秒 {sample_fps:g} 帧。")

    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-i",
        str(video_path),
        "-vf",
        video_filter,
        "-an",
        "-f",
        "null",
        "-",
    ]
    log("$ " + " ".join(_quote(part) for part in cmd))
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    times: list[float] = []
    tail: list[str] = []
    line_queue: queue.Queue[str | None] = queue.Queue()
    assert process.stdout is not None
    reader = threading.Thread(
        target=_read_process_output,
        args=(process.stdout, line_queue),
        daemon=True,
    )
    reader.start()
    start_time = time.monotonic()
    last_heartbeat = start_time
    while True:
        try:
            line = line_queue.get(timeout=1.0)
        except queue.Empty:
            line = None

        if line:
            stripped = line.rstrip()
            if stripped:
                tail.append(stripped)
                if len(tail) > 40:
                    del tail[0]
            for match in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", stripped):
                seconds = float(match.group(1))
                if not times or seconds - times[-1] >= SCENE_DEDUPE_SECONDS:
                    times.append(seconds)

        now = time.monotonic()
        if now - last_heartbeat >= PROCESS_HEARTBEAT_SECONDS and process.poll() is None:
            log(f"画面变化检测仍在运行，已耗时 {_format_duration(now - start_time)}。")
            last_heartbeat = now

        if process.poll() is not None:
            break

    returncode = process.wait()
    reader.join(timeout=1.0)
    if returncode != 0:
        if tail:
            log("画面变化检测输出摘要：\n" + "\n".join(tail))
        raise RuntimeError(f"画面变化检测失败，退出码 {returncode}")

    return times


def _read_process_output(stream, line_queue: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            line_queue.put(line)
    finally:
        line_queue.put(None)


def _scene_anchor_after(start_seconds: float, scene_times: list[float]) -> float | None:
    earliest = start_seconds - SCENE_ANCHOR_LOOKBEHIND_SECONDS
    latest = start_seconds + SCENE_ANCHOR_LOOKAHEAD_SECONDS
    for scene_time in scene_times:
        if earliest <= scene_time <= latest:
            return scene_time
        if scene_time > latest:
            break
    return None


def _select_candidates(
    candidates: list[ScreenshotCandidate],
    *,
    max_screenshots: int = MAX_SCREENSHOTS,
    max_scene_only_screenshots: int = MAX_SCENE_ONLY_SCREENSHOTS,
) -> list[ScreenshotCandidate]:
    if not candidates:
        return []

    merged: list[ScreenshotCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.time_seconds):
        if merged and abs(candidate.time_seconds - merged[-1].time_seconds) <= MERGE_WINDOW_SECONDS:
            current = merged[-1]
            current.score += candidate.score
            for reason in candidate.reasons:
                if reason not in current.reasons:
                    current.reasons.append(reason)
            if not current.subtitle_text and candidate.subtitle_text:
                current.subtitle_time = candidate.subtitle_time
                current.subtitle_text = candidate.subtitle_text
            if candidate.score > current.score:
                current.time_seconds = candidate.time_seconds
            continue
        merged.append(candidate)

    chosen: list[ScreenshotCandidate] = []
    scene_only_count = 0
    for candidate in sorted(merged, key=lambda item: item.score, reverse=True):
        if any(abs(candidate.time_seconds - item.time_seconds) < MIN_SCREENSHOT_INTERVAL_SECONDS for item in chosen):
            continue
        if _is_scene_only(candidate):
            if scene_only_count >= max_scene_only_screenshots:
                continue
            scene_only_count += 1
        chosen.append(candidate)
        if len(chosen) >= max_screenshots:
            break

    return sorted(chosen, key=lambda item: item.time_seconds)


def _is_scene_only(candidate: ScreenshotCandidate) -> bool:
    return candidate.reasons == ["画面明显切换"]


def _capture_frame(
    video_path: Path,
    screenshot_path: Path,
    seconds: float,
    ffmpeg: Path,
    log: LogCallback,
) -> None:
    cmd = [
        str(ffmpeg),
        "-y",
        "-ss",
        f"{max(seconds, 0):.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(screenshot_path),
    ]
    log("$ " + " ".join(_quote(part) for part in cmd))
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        log((completed.stderr or completed.stdout or "").strip())
        raise RuntimeError(f"截图失败，退出码 {completed.returncode}")
    if not screenshot_path.exists() or screenshot_path.stat().st_size == 0:
        raise RuntimeError(f"截图文件未生成：{screenshot_path}")


def _format_index(candidates: list[ScreenshotCandidate]) -> str:
    lines = [
        "# 关键截图索引",
        "",
        "| 时间点 | 触发原因 | 字幕内容 | 截图 |",
        "|---|---|---|---|",
    ]

    if not candidates:
        lines.append("| - | 未发现候选点 | - | - |")
        return "\n".join(lines) + "\n"

    for candidate in candidates:
        timestamp = seconds_to_timestamp(candidate.time_seconds)
        image_path = f"关键截图/{candidate.screenshot_name}"
        lines.append(
            "| {time} | {reason} | {text} | ![]({image}) |".format(
                time=timestamp,
                reason=_escape_cell("、".join(candidate.reasons)),
                text=_escape_cell(candidate.subtitle_text or "-"),
                image=image_path,
            )
        )
    return "\n".join(lines) + "\n"


def _format_candidates_json(candidates: list[ScreenshotCandidate]) -> str:
    payload = [
        {
            "time_seconds": round(candidate.time_seconds, 3),
            "time": seconds_to_timestamp(candidate.time_seconds),
            "score": round(candidate.score, 2),
            "reasons": candidate.reasons,
            "subtitle_time": candidate.subtitle_time,
            "subtitle_text": candidate.subtitle_text,
            "screenshot": f"关键截图/{candidate.screenshot_name}" if candidate.screenshot_name else "",
        }
        for candidate in candidates
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _nearest_entry(entries: list[SubtitleEntry], seconds: float) -> SubtitleEntry | None:
    best: tuple[float, SubtitleEntry] | None = None
    for entry in entries:
        start = srt_time_to_seconds(entry.start)
        end = srt_time_to_seconds(entry.end)
        if start <= seconds <= end:
            return entry
        distance = min(abs(seconds - start), abs(seconds - end))
        if best is None or distance < best[0]:
            best = (distance, entry)
    if best and best[0] <= 6.0:
        return best[1]
    return None


def srt_time_to_seconds(value: str) -> float:
    match = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)", value.strip())
    if not match:
        return 0.0
    hours, minutes, seconds, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis[:3].ljust(3, "0")) / 1000


def seconds_to_timestamp(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _screenshot_name(seconds: float) -> str:
    return seconds_to_timestamp(seconds).replace(":", "-") + ".jpg"


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _dedupe_times(times: list[float], min_interval: float) -> list[float]:
    deduped: list[float] = []
    for seconds in sorted(times):
        if not deduped or seconds - deduped[-1] >= min_interval:
            deduped.append(seconds)
    return deduped


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _quote(value: str) -> str:
    if " " in value or "\t" in value:
        return f'"{value}"'
    return value


def _format_duration(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours:
        return f"{hours}小时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"
