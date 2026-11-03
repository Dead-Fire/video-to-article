from __future__ import annotations

from pathlib import Path

from .text_utils import read_text, write_text


DEFAULT_TEMPLATE = """# 你的任务

你是一个“视频学习资料整理与 PDF 成稿排版助手”。

我会给你一份由语音识别或字幕轨提取得到的视频字幕稿。它可能存在错别字、同音词错误、断句错误、标点错误、重复口癖和少量识别不准确的问题。

你的目标不是简单摘要，也不是继续输出提纲，而是直接把素材整理成一篇可以保存、打印和长期阅读的完整文章。最终交付优先是一份排版清晰的 PDF 文件。

# 视频信息

- 视频文件：{video_name}
- 字幕来源：{source}

# 最终交付要求

1. 如果当前对话支持生成文件，请直接生成并提供一个 PDF 文件，文件名使用“视频学习总结.pdf”或与视频标题相关的中文文件名。
2. PDF 必须是一篇完整成稿，不要只输出聊天回复、提纲、项目符号摘要或“我将如何处理”的说明。
3. 如果当前对话暂时不能生成 PDF 文件，请直接输出一篇可复制到 Word、飞书文档或 Markdown 编辑器并导出 PDF 的完整排版稿，不要只给简短摘要。
4. 不要先问我要不要继续，不要要求我确认格式；请直接完成最终文章。

# 文章排版要求

1. 使用清晰标题、副标题和二级小标题。
2. 开头用 1-2 段说明本文解决什么问题、适合谁读。
3. 正文按主题重组字幕内容，写成连贯文章，不要逐句翻译字幕。
4. 每段尽量短，避免大段墙式文字。
5. 可以使用少量表格总结流程、对比、步骤，但不要滥用表格。
6. 文章末尾加入“核心结论”和“值得回看的时间点”。
7. 保留关键时间点，格式类似“00:12:35”。
8. 如果发现疑似识别错误，在正文中谨慎修复；无法确认时用括号标注“疑似识别错误”。

# 内容处理原则

1. 保留原意，不要添加视频中没有明确表达的新观点。
2. 可以修正明显的语音识别错误、错别字、同音词、标点和断句。
3. 去掉明显口癖，合并碎句，按主题重组。
4. 不要写成营销文、公众号标题党或过度扩写的鸡汤文。
5. 输出使用简体中文。

# 专有名词表

如果字幕中出现类似词，请优先按这些词理解和修复：

{glossary}

# 本地工具预先标记的疑似问题

{suspicious_table}

{screenshot_index_text}

# 原始字幕如下

```text
{subtitle_text}
```
"""


def load_template(template_path: Path) -> str:
    if template_path.exists():
        return read_text(template_path)
    return DEFAULT_TEMPLATE


def format_glossary(glossary_text: str) -> str:
    terms = [line.strip() for line in glossary_text.splitlines() if line.strip()]
    if not terms:
        return "- 暂无"
    return "\n".join(f"- {term.lstrip('-').strip()}" for term in terms)


def format_suspicious_table(items: list[dict[str, str]]) -> str:
    if not items:
        return "未发现明显异常。"

    rows = ["| 时间点 | 原字幕 | 可能问题 | 建议修复 |", "|---|---|---|---|"]
    for item in items:
        rows.append(
            "| {time} | {text} | {problem} | {suggestion} |".format(
                time=_escape_cell(item["time"]),
                text=_escape_cell(item["text"]),
                problem=_escape_cell(item["problem"]),
                suggestion=_escape_cell(item["suggestion"]),
            )
        )
    return "\n".join(rows)


def write_gpt_pack(
    *,
    output_path: Path,
    template_path: Path,
    video_name: str,
    source: str,
    glossary_text: str,
    subtitle_text: str,
    suspicious: list[dict[str, str]],
    screenshot_index_text: str = "",
) -> None:
    template = load_template(template_path)
    content = template.format(
        video_name=video_name,
        source=source,
        glossary=format_glossary(glossary_text),
        suspicious_table=format_suspicious_table(suspicious),
        screenshot_index_text=screenshot_index_text.strip(),
        subtitle_text=subtitle_text.strip(),
    )
    write_text(output_path, content)


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()
