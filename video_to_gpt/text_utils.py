from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SubtitleEntry:
    index: int
    start: str
    end: str
    text: str


FILLER_PATTERNS = [
    r"(?<!\w)(嗯|呃|啊|额|呃嗯|嗯嗯)(?!\w)",
    r"(就是说|也就是说|然后呢|对吧|是吧|你知道吧)[，,、\s]*",
]


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig", newline="\n")


def parse_srt(text: str) -> list[SubtitleEntry]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    entries: list[SubtitleEntry] = []
    blocks = re.split(r"\n{2,}", normalized)
    fallback_index = 1

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue

        index = fallback_index
        time_line_pos = 0
        if lines[0].isdigit():
            index = int(lines[0])
            time_line_pos = 1

        if time_line_pos >= len(lines) or "-->" not in lines[time_line_pos]:
            continue

        start_end = [part.strip() for part in lines[time_line_pos].split("-->", 1)]
        if len(start_end) != 2:
            continue

        body = " ".join(lines[time_line_pos + 1 :]).strip()
        if body:
            entries.append(
                SubtitleEntry(
                    index=index,
                    start=start_end[0],
                    end=start_end[1].split()[0],
                    text=body,
                )
            )
            fallback_index += 1

    return entries


def srt_to_plain(entries: list[SubtitleEntry]) -> str:
    lines = []
    for entry in entries:
        lines.append(f"[{entry.start}] {normalize_space(entry.text)}")
    return "\n".join(lines).strip() + ("\n" if lines else "")


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def light_clean_text(text: str) -> str:
    cleaned = normalize_space(text)
    for pattern in FILLER_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned)
    cleaned = re.sub(r"([，。！？；：,.!?;:])\1+", r"\1", cleaned)
    cleaned = re.sub(r"\s+([，。！？；：,.!?;:])", r"\1", cleaned)
    return cleaned.strip()


def cleaned_reading_text(entries: list[SubtitleEntry]) -> str:
    if not entries:
        return ""

    paragraphs: list[str] = []
    current_parts: list[str] = []
    current_start = entries[0].start

    for entry in entries:
        text = light_clean_text(entry.text)
        if not text:
            continue

        if not current_parts:
            current_start = entry.start

        current_parts.append(text)
        merged = "".join(current_parts)
        should_flush = (
            len(merged) >= 120
            or text.endswith(("。", "！", "？", ".", "!", "?"))
            and len(merged) >= 40
        )

        if should_flush:
            paragraphs.append(f"[{current_start}] {merged}")
            current_parts = []

    if current_parts:
        paragraphs.append(f"[{current_start}] {''.join(current_parts)}")

    return "\n\n".join(paragraphs).strip() + "\n"


def suspicious_items(entries: list[SubtitleEntry]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in entries:
        text = normalize_space(entry.text)
        reasons: list[str] = []

        if len(text) <= 1:
            reasons.append("字幕过短")
        if len(text) > 140:
            reasons.append("字幕过长")
        if re.search(r"(.{1,4})\1{3,}", text):
            reasons.append("疑似重复识别")
        if "�" in text or "锟" in text:
            reasons.append("疑似乱码")
        if re.search(r"\d{7,}", text):
            reasons.append("连续数字较长")

        symbols = re.findall(r"[^\w\s\u4e00-\u9fff，。！？；：、,.!?;:（）()《》“”\"'-]", text)
        if len(text) >= 8 and len(symbols) / max(len(text), 1) > 0.25:
            reasons.append("异常符号较多")

        if reasons:
            items.append(
                {
                    "time": entry.start,
                    "text": text,
                    "problem": "、".join(reasons),
                    "suggestion": "请结合上下文或回看原视频确认",
                }
            )

    return items
