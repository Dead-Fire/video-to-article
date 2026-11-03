from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .text_utils import write_text


@dataclass(frozen=True)
class OutputLayout:
    root: Path
    drag_pack_dir: Path
    subtitle_dir: Path
    screenshot_dir: Path
    log_dir: Path


def create_output_layout(output_dir: Path) -> OutputLayout:
    layout = OutputLayout(
        root=output_dir,
        drag_pack_dir=output_dir / "00_拖给GPT",
        subtitle_dir=output_dir / "02_字幕稿",
        screenshot_dir=output_dir / "03_截图素材",
        log_dir=output_dir / "04_日志与调试",
    )
    for folder in (
        layout.drag_pack_dir,
        layout.subtitle_dir,
        layout.screenshot_dir,
        layout.log_dir,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    return layout


def write_single_next_steps(
    *,
    output_dir: Path,
    source_name: str,
    has_visual_pack: bool,
    has_screenshots: bool,
) -> Path:
    path = output_dir / "00_先看我_使用说明.md"
    content = f"""# 先看我

来源文件：{source_name}

直接把这个文件夹拖给 GPT：

```text
00_拖给GPT/
```

## 这个文件夹里有什么

- `发给GPT.md`
- 如果是视频任务并且生成了截图，还会有 `图文总结图片.zip`

把整个 `00_拖给GPT/` 上传给 GPT 后，要求 GPT 直接生成一篇排版清晰的 PDF 文章；如果当前对话不能生成 PDF 文件，它会输出可导出 PDF 的完整排版稿。

## 目录说明

- `00_拖给GPT/`：直接拖给 GPT 的入口。
- `02_字幕稿/`：原始字幕、纯文本字幕、轻度清洗稿。
- `03_截图素材/`：完整关键截图、截图索引、候选数据。
- `04_日志与调试/`：处理日志、whisper 运行信息、调试文件。
"""
    write_text(path, content)
    return path


def write_batch_next_steps(
    *,
    output_dir: Path,
    source_count: int,
    kept_count: int,
    duplicate_count: int,
) -> Path:
    path = output_dir / "00_先看我_使用说明.md"
    content = f"""# 先看我

这是一次多文件整合结果。

直接把这个文件夹拖给 GPT：

```text
00_拖给GPT/
```

## 本地整合统计

- 来源文件：{source_count}
- 保留片段：{kept_count}
- 跳过重复：{duplicate_count}

把整个 `00_拖给GPT/` 上传给 GPT 后，要求 GPT 直接生成一篇多文件整合后的 PDF 文章；如果当前对话不能生成 PDF 文件，它会输出可导出 PDF 的完整排版稿。

## 目录说明

- `00_拖给GPT/`：多文件整合后要拖给 GPT 的入口。
- `00_拖给GPT/图文总结图片.zip`：如果本批次有视频截图，会自动打包批量精选截图。
- `02_整合稿/`：整合字幕和整合阅读稿，适合自己直接查看。
- `03_截图素材/`：保留每个源视频自己的关键截图、截图索引和候选数据。
- `04_日志与调试/`：重复片段、整合清单等调试数据。
"""
    write_text(path, content)
    return path
