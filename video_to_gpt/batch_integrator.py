from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable

from .output_layout import write_batch_next_steps
from .processor import ProcessResult
from .resource import BATCH_FUZZY_COMPARE_LIMIT, LARGE_BATCH_PROFILE
from .text_utils import read_text, write_text


LogCallback = Callable[[str], None]

SIMILARITY_THRESHOLD = 0.97
BATCH_GPT_IMAGE_ZIP_MAX_IMAGES = 180


@dataclass
class IntegratedSegment:
    source_name: str
    source_path: str
    text: str


@dataclass
class DuplicateSegment:
    source_name: str
    text: str
    matched_source_name: str
    matched_text: str
    similarity: float


@dataclass(frozen=True)
class BatchIntegrationResult:
    output_dir: Path
    integrated_markdown: Path
    integrated_text: Path
    gpt_pack: Path
    duplicates_json: Path
    manifest_json: Path
    kept_count: int
    duplicate_count: int


def integrate_batch_results(
    results: Iterable[ProcessResult],
    *,
    output_root: Path,
    resource_mode: str = "normal",
    log: LogCallback,
) -> BatchIntegrationResult | None:
    successful = [result for result in results if result.cleaned_txt.exists()]
    if len(successful) < 2:
        log("本次成功文件少于 2 个，跳过批量内容整合。")
        return None

    output_dir = _unique_dir(output_root / "整合结果")
    output_dir.mkdir(parents=True, exist_ok=False)
    gpt_dir = output_dir / "00_拖给GPT"
    screenshot_dir = output_dir / "03_截图素材"
    text_dir = output_dir / "02_整合稿"
    log_dir = output_dir / "04_日志与调试"
    for folder in (gpt_dir, screenshot_dir, text_dir, log_dir):
        folder.mkdir(parents=True, exist_ok=True)

    kept: list[IntegratedSegment] = []
    duplicates: list[DuplicateSegment] = []
    normalized_kept: list[tuple[str, IntegratedSegment]] = []
    exact_index: dict[str, IntegratedSegment] = {}
    enable_fuzzy = resource_mode != LARGE_BATCH_PROFILE.name
    fuzzy_limit_reached_logged = False

    if not enable_fuzzy:
        log("大批量省资源模式：批量整合只跳过完全重复片段，跳过高成本模糊去重。")

    for result in successful:
        source_name = result.video_path.name
        for paragraph in _paragraphs(read_text(result.cleaned_txt)):
            normalized = _normalize_for_compare(paragraph)
            if not normalized:
                continue

            duplicate = None
            exact_match = exact_index.get(normalized)
            if exact_match:
                duplicate = _make_duplicate(
                    source_name=source_name,
                    original_text=paragraph,
                    matched_segment=exact_match,
                    similarity=1.0,
                )
            elif enable_fuzzy and len(normalized_kept) <= BATCH_FUZZY_COMPARE_LIMIT:
                duplicate = _find_duplicate(normalized, paragraph, normalized_kept, source_name)
            elif enable_fuzzy and not fuzzy_limit_reached_logged:
                log(
                    f"整合片段超过 {BATCH_FUZZY_COMPARE_LIMIT} 段，后续自动关闭模糊去重以节省资源。"
                )
                fuzzy_limit_reached_logged = True

            if duplicate:
                duplicates.append(duplicate)
                continue

            segment = IntegratedSegment(
                source_name=source_name,
                source_path=str(result.cleaned_txt),
                text=paragraph,
            )
            kept.append(segment)
            normalized_kept.append((normalized, segment))
            exact_index[normalized] = segment

    integrated_text = text_dir / "整合字幕.txt"
    integrated_markdown = text_dir / "整合阅读稿.md"
    gpt_pack = gpt_dir / "发给GPT.md"
    duplicates_json = log_dir / "重复片段.json"
    manifest_json = log_dir / "整合清单.json"

    screenshot_overview_text = _prepare_batch_screenshot_assets(successful, screenshot_dir, gpt_dir)

    write_text(integrated_text, _format_integrated_text(kept))
    write_text(integrated_markdown, _format_integrated_markdown(kept, duplicates))
    write_text(
        gpt_pack,
        _format_gpt_pack(
            kept,
            duplicates,
            screenshot_overview_text=screenshot_overview_text,
        ),
    )
    write_text(duplicates_json, _json_dumps([duplicate.__dict__ for duplicate in duplicates]))
    write_text(manifest_json, _json_dumps(_manifest(successful, kept, duplicates)))
    write_batch_next_steps(
        output_dir=output_dir,
        source_count=len(successful),
        kept_count=len(kept),
        duplicate_count=len(duplicates),
    )

    log(f"批量内容整合完成：保留 {len(kept)} 段，跳过重复 {len(duplicates)} 段。")
    log(f"批量整合输出目录：{output_dir}")

    return BatchIntegrationResult(
        output_dir=output_dir,
        integrated_markdown=integrated_markdown,
        integrated_text=integrated_text,
        gpt_pack=gpt_pack,
        duplicates_json=duplicates_json,
        manifest_json=manifest_json,
        kept_count=len(kept),
        duplicate_count=len(duplicates),
    )


def _paragraphs(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    parts = re.split(r"\n{2,}", normalized)
    if len(parts) == 1:
        parts = normalized.splitlines()
    return [part.strip() for part in parts if part.strip()]


def _find_duplicate(
    normalized: str,
    original_text: str,
    existing: list[tuple[str, IntegratedSegment]],
    source_name: str,
) -> DuplicateSegment | None:
    for existing_normalized, segment in existing:
        if normalized == existing_normalized:
            similarity = 1.0
        elif len(normalized) < 20 or len(existing_normalized) < 20:
            continue
        else:
            similarity = SequenceMatcher(None, normalized, existing_normalized).ratio()

        if similarity >= SIMILARITY_THRESHOLD:
            return _make_duplicate(
                source_name=source_name,
                original_text=original_text,
                matched_segment=segment,
                similarity=similarity,
            )
    return None


def _make_duplicate(
    *,
    source_name: str,
    original_text: str,
    matched_segment: IntegratedSegment,
    similarity: float,
) -> DuplicateSegment:
    return DuplicateSegment(
        source_name=source_name,
        text=original_text,
        matched_source_name=matched_segment.source_name,
        matched_text=matched_segment.text,
        similarity=round(similarity, 4),
    )


def _normalize_for_compare(text: str) -> str:
    text = re.sub(r"\[[0-9:,.\s]+\]", "", text)
    text = re.sub(r"\s+", "", text).lower()
    text = re.sub(r"[，。！？；：、,.!?;:（）()《》“”\"'`~\-_\[\]【】]", "", text)
    return text.strip()


def _format_integrated_text(segments: list[IntegratedSegment]) -> str:
    lines: list[str] = []
    for segment in segments:
        lines.append(f"【来源：{segment.source_name}】")
        lines.append(segment.text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _format_integrated_markdown(
    segments: list[IntegratedSegment],
    duplicates: list[DuplicateSegment],
) -> str:
    lines = [
        "# 批量整合阅读稿",
        "",
        f"- 保留片段：{len(segments)}",
        f"- 跳过重复：{len(duplicates)}",
        "",
    ]

    current_source = ""
    for segment in segments:
        if segment.source_name != current_source:
            current_source = segment.source_name
            lines.extend(["", f"## {current_source}", ""])
        lines.append(segment.text)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _format_gpt_pack(
    segments: list[IntegratedSegment],
    duplicates: list[DuplicateSegment],
    *,
    screenshot_overview_text: str = "",
) -> str:
    body = _format_integrated_text(segments)
    screenshot_section = ""
    if screenshot_overview_text:
        screenshot_section = f"""
# 批量截图素材

我同时上传了 `图文总结图片.zip`。这些图片来自各个源视频的关键截图，文件名里包含来源序号和时间点。请在生成 PDF 文章时按内容相关性选择合适图片穿插到正文附近，并为图片添加图号和图注。

如果你无法直接读取压缩包图片，请不要编造图片细节；可以根据下方截图总览和字幕内容生成文字文章，并在合适位置保留图片占位。

{screenshot_overview_text.strip()}
"""
    return f"""# 你的任务

你是一个“多视频知识整合与 PDF 成稿排版助手”。

我会给你一份由本地工具从多个相关视频/音频字幕中合并后的稿件。它们属于同一系列或同一主题，工具只跳过了完全重复的片段，因此内容可能仍有重复观点、识别错误和顺序不够清晰的问题。

请你直接把它整理成一篇完整、连贯、适合快速阅读和学习吸收的知识文章。最终交付优先是一份排版清晰的 PDF 文件。

# 最终交付要求

1. 如果当前对话支持生成文件，请直接生成并提供一个 PDF 文件，文件名使用“多视频知识整合总结.pdf”。
2. PDF 必须是一篇完整成稿，不要只输出聊天回复、提纲、项目符号摘要或处理说明。
3. 如果当前对话暂时不能生成 PDF 文件，请直接输出一篇可复制到 Word、飞书文档或 Markdown 编辑器并导出 PDF 的完整排版稿。
4. 如果我同时上传了图片压缩包，请把图片穿插到正文对应位置，并为每张图片写图号和图注。
5. 不要先问我要不要继续，不要要求我确认格式；请直接完成最终文章。

# 文章排版要求

1. 使用清晰标题、副标题和二级小标题。
2. 开头用 1-2 段说明主题、适合谁读，以及整合后最重要的结论。
3. 合并重复观点，但不要删除互相补充的细节。
4. 正文按主题重组，写成连贯文章，不要按视频文件机械罗列。
5. 每段尽量短，必要时使用少量表格整理流程、对比或步骤。
6. 保留来源文件名和关键时间点，方便必要时回看。
7. 末尾加入“核心结论”“实践建议”和“疑似识别错误或需要回看处”。
8. 输出使用简体中文。

# 本地整合统计

- 保留片段：{len(segments)}
- 跳过重复：{len(duplicates)}

{screenshot_section}

# 请输出

## 文章结构建议

## 一、主题概览

## 二、完整知识文章

## 三、核心结论与实践建议

## 四、疑似识别错误或需要回看处

# 已去重整合稿如下

```text
{body.strip()}
```
"""


def _manifest(
    results: list[ProcessResult],
    kept: list[IntegratedSegment],
    duplicates: list[DuplicateSegment],
) -> dict:
    return {
        "source_count": len(results),
        "kept_count": len(kept),
        "duplicate_count": len(duplicates),
        "similarity_threshold": SIMILARITY_THRESHOLD,
        "sources": [
            {
                "media": str(result.video_path),
                "output_dir": str(result.output_dir),
                "cleaned_txt": str(result.cleaned_txt),
            }
            for result in results
        ],
    }


def _prepare_batch_screenshot_assets(
    results: list[ProcessResult],
    screenshot_dir: Path,
    gpt_dir: Path,
) -> str:
    copied_dirs = _copy_screenshot_assets(results, screenshot_dir)
    overview_text = _format_screenshot_overview(copied_dirs)
    write_text(screenshot_dir / "截图总览.md", overview_text)
    archive_count = _write_batch_screenshot_archive(
        screenshot_dir=screenshot_dir,
        archive_path=gpt_dir / "图文总结图片.zip",
        copied_dirs=copied_dirs,
    )
    if archive_count <= 0:
        return ""
    return overview_text + f"\n`00_拖给GPT/图文总结图片.zip` 已打包 {archive_count} 张批量精选截图。\n"


def _copy_screenshot_assets(results: list[ProcessResult], screenshot_dir: Path) -> list[tuple[int, str, str]]:
    copied_dirs: list[tuple[int, str, str]] = []
    for index, result in enumerate(results, start=1):
        source_screenshot_dir = result.output_dir / "03_截图素材"
        if not source_screenshot_dir.exists():
            continue
        target_name = f"{index:02d}_{_safe_name(result.video_path.stem)}"
        target_dir = screenshot_dir / target_name
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        shutil.copytree(source_screenshot_dir, target_dir)
        copied_dirs.append((index, result.video_path.name, target_name))
    return copied_dirs


def _format_screenshot_overview(copied_dirs: list[tuple[int, str, str]]) -> str:
    lines = [
        "# 批量截图总览",
        "",
        "这个目录保留了每个源视频自己的截图原件和索引，避免批量整合后丢失中间截图。",
        "",
        "| 序号 | 来源文件 | 截图目录 |",
        "|---|---|---|",
    ]
    for index, source_name, target_name in copied_dirs:
        lines.append(
            f"| {index:02d} | {_escape_cell(source_name)} | {target_name} |"
        )
    if not copied_dirs:
        lines.append("| - | 未找到截图素材 | - |")
    return "\n".join(lines) + "\n"


def _write_batch_screenshot_archive(
    *,
    screenshot_dir: Path,
    archive_path: Path,
    copied_dirs: list[tuple[int, str, str]],
) -> int:
    image_paths: list[Path] = []
    for _index, _source_name, target_name in copied_dirs:
        source_dir = screenshot_dir / target_name
        image_paths.extend(sorted(source_dir.rglob("*.jpg")))

    if not image_paths:
        if archive_path.exists():
            archive_path.unlink()
        return 0

    selected = _select_batch_images(image_paths, copied_dirs, screenshot_dir)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for image_path in selected:
            zf.write(image_path, arcname=str(image_path.relative_to(screenshot_dir)).replace("\\", "/"))
    return len(selected)


def _select_batch_images(
    image_paths: list[Path],
    copied_dirs: list[tuple[int, str, str]],
    screenshot_dir: Path,
) -> list[Path]:
    if len(image_paths) <= BATCH_GPT_IMAGE_ZIP_MAX_IMAGES:
        return image_paths

    grouped: list[list[Path]] = []
    for _index, _source_name, target_name in copied_dirs:
        group = sorted((screenshot_dir / target_name).rglob("*.jpg"))
        if group:
            grouped.append(group)

    per_source_limit = max(1, BATCH_GPT_IMAGE_ZIP_MAX_IMAGES // max(len(grouped), 1))
    selected: list[Path] = []
    selected_set: set[Path] = set()
    for group in grouped:
        for image_path in group[:per_source_limit]:
            selected.append(image_path)
            selected_set.add(image_path)
            if len(selected) >= BATCH_GPT_IMAGE_ZIP_MAX_IMAGES:
                return selected

    for image_path in image_paths:
        if image_path in selected_set:
            continue
        selected.append(image_path)
        if len(selected) >= BATCH_GPT_IMAGE_ZIP_MAX_IMAGES:
            break
    return selected


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.name}_{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("整合结果目录重名过多，请清理输出目录。")


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "video"


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()
