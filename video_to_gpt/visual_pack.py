from __future__ import annotations

import shutil
import statistics
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .screenshot_extractor import ScreenshotCandidate, ScreenshotResult
from .text_utils import write_text


MAX_VISUAL_PACK_IMAGES = 14
MIN_VISUAL_SIGNAL_SCORE = 14.0
HIGH_SUBTITLE_SCORE = 10.0


@dataclass(frozen=True)
class VisualGptPackResult:
    pack_path: Path
    image_dir: Path
    archive_path: Path
    image_count: int


def write_visual_gpt_pack(
    *,
    output_dir: Path,
    video_name: str,
    source: str,
    subtitle_text: str,
    screenshot_result: ScreenshotResult,
    ffmpeg: Path | None = None,
    max_images: int = MAX_VISUAL_PACK_IMAGES,
) -> VisualGptPackResult | None:
    if screenshot_result.count <= 0:
        return None

    image_dir = output_dir
    image_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / "图文总结图片.zip"

    for old_image in image_dir.glob("fig-*.jpg"):
        old_image.unlink()
    if archive_path.exists():
        archive_path.unlink()

    selected = _select_for_visual_pack(
        screenshot_result.candidates,
        screenshot_result.screenshot_dir,
        max_images,
        ffmpeg,
    )
    image_rows: list[dict[str, str]] = []

    for index, candidate in enumerate(selected, start=1):
        source_image = screenshot_result.screenshot_dir / candidate.screenshot_name
        if not source_image.exists():
            continue

        figure_id = f"图{index:02d}"
        copied_name = f"fig-{index:02d}_{candidate.screenshot_name}"
        copied_path = image_dir / copied_name
        shutil.copyfile(source_image, copied_path)
        image_rows.append(
            {
                "figure_id": figure_id,
                "time": _timestamp(candidate.time_seconds),
                "reason": "、".join(candidate.reasons),
                "subtitle": candidate.subtitle_text or "-",
                "path": copied_name,
            }
        )

    if not image_rows:
        return None

    written_images = sorted(image_dir.glob("fig-*.jpg"))
    _write_zip_archive(archive_path, written_images)
    for image_path in written_images:
        image_path.unlink(missing_ok=True)

    pack_path = output_dir / "发给GPT.md"

    write_text(
        pack_path,
        _format_visual_pack(
            video_name=video_name,
            source=source,
            image_rows=image_rows,
            subtitle_text=subtitle_text,
        ),
    )

    return VisualGptPackResult(
        pack_path=pack_path,
        image_dir=image_dir,
        archive_path=archive_path,
        image_count=len(image_rows),
    )


def _select_for_visual_pack(
    candidates: list[ScreenshotCandidate],
    screenshot_dir: Path,
    max_images: int,
    ffmpeg: Path | None,
) -> list[ScreenshotCandidate]:
    if len(candidates) <= max_images:
        return candidates

    scored = [
        (candidate, _visual_signal_score(screenshot_dir / candidate.screenshot_name, ffmpeg))
        for candidate in candidates
    ]
    rich_pool = [
        (candidate, signal)
        for candidate, signal in scored
        if signal >= MIN_VISUAL_SIGNAL_SCORE or candidate.score >= HIGH_SUBTITLE_SCORE
    ]
    pool = rich_pool if len(rich_pool) >= min(max_images, 8) else scored

    ranked = sorted(
        pool,
        key=lambda item: item[0].score + min(item[1], 30.0) / 6.0,
        reverse=True,
    )
    chosen = [candidate for candidate, _signal in ranked[:max_images]]
    return sorted(chosen, key=lambda item: item.time_seconds)


def _visual_signal_score(image_path: Path, ffmpeg: Path | None) -> float:
    if not ffmpeg or not image_path.exists():
        return 0.0

    width = 96
    height = 54
    cmd = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(image_path),
        "-vf",
        f"scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-",
    ]
    completed = subprocess.run(cmd, capture_output=True, check=False)
    data = completed.stdout
    if completed.returncode != 0 or len(data) != width * height:
        return 0.0

    values = list(data)
    stdev = statistics.pstdev(values)
    diff_total = 0
    diff_count = 0
    for y in range(height):
        row = y * width
        for x in range(width - 1):
            diff_total += abs(values[row + x] - values[row + x + 1])
            diff_count += 1
    for y in range(height - 1):
        row = y * width
        next_row = (y + 1) * width
        for x in range(width):
            diff_total += abs(values[row + x] - values[next_row + x])
            diff_count += 1

    edge_score = diff_total / max(diff_count, 1)
    return edge_score + stdev * 0.15


def _format_visual_pack(
    *,
    video_name: str,
    source: str,
    image_rows: list[dict[str, str]],
    subtitle_text: str,
) -> str:
    return f"""# 你的任务

你是一个“视频图文学习稿整理与 PDF 成稿排版助手”。

我会给你一份由本地工具生成的视频字幕稿，并同时上传一组从视频中截取的关键截图。请把它整理成一篇图文并茂、排版清晰、可以保存和打印的完整文章，目标是让我不看完整视频也能掌握核心内容。

# 使用素材

- 视频文件：{video_name}
- 字幕来源：{source}
- 截图数量：{len(image_rows)}

截图图片已经打包成 `图文总结图片.zip`。如果你能读取压缩包内容，请结合压缩包里的图片一起理解，并把合适的图片插入正文；如果不能直接插入图片，请在正文相应位置使用“【插图：图03】”这种占位，并写清楚图注，不要编造图片细节。

# 最终交付要求

1. 如果当前对话支持生成文件，请直接生成并提供一个 PDF 文件，文件名使用“视频图文总结.pdf”或与视频标题相关的中文文件名。
2. PDF 必须是一篇完整成稿，不要只输出聊天回复、提纲、项目符号摘要或“我将如何处理”的说明。
3. 如果当前对话暂时不能生成 PDF 文件，请直接输出一篇可复制到 Word、飞书文档或 Markdown 编辑器并导出 PDF 的完整图文排版稿。
4. 不要先问我要不要继续，不要要求我确认格式；请直接完成最终文章。

# 文章排版要求

1. 使用清晰标题、副标题和二级小标题。
2. 开头用 1-2 段说明本文解决什么问题、适合谁读。
3. 正文按主题重组字幕内容，写成连贯文章，不要逐句翻译字幕。
4. 图片要穿插在对应内容附近，不要全部堆在开头或结尾。
5. 每张被引用的图片都要有图号和一句图注，例如“图03：展示了某流程中的关键界面”。
6. 每段尽量短，必要时使用表格整理流程、对比或步骤。
7. 保留关键时间点，方便我必要时回看。
8. 文章末尾加入“核心结论”和“最值得回看的时间点”。
9. 修复明显语音识别错误，但不要添加视频没有表达的新观点。
10. 输出使用简体中文。

# 图片引用索引

| 图号 | 时间点 | 触发原因 | 字幕上下文 | 本地图片路径 |
|---|---|---|---|---|
{_format_image_rows(image_rows)}

# 原始字幕如下

```text
{subtitle_text.strip()}
```
"""


def _format_image_rows(rows: list[dict[str, str]]) -> str:
    return "\n".join(
        "| {figure_id} | {time} | {reason} | {subtitle} | {path} |".format(
            figure_id=_escape_cell(row["figure_id"]),
            time=_escape_cell(row["time"]),
            reason=_escape_cell(row["reason"]),
            subtitle=_escape_cell(row["subtitle"]),
            path=_escape_cell(row["path"]),
        )
        for row in rows
    )


def _timestamp(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _write_zip_archive(archive_path: Path, files: list[Path]) -> None:
    if not files:
        return
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for file_path in files:
            zf.write(file_path, arcname=file_path.name)
