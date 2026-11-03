from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .gpt_pack import write_gpt_pack
from .output_layout import create_output_layout, write_single_next_steps
from .paths import APP_ROOT, ensure_runtime_dirs
from .resource import (
    LARGE_BATCH_PROFILE,
    NORMAL_PROFILE,
    PROCESS_HEARTBEAT_SECONDS,
    PROCESS_OUTPUT_TAIL_CHARS,
    PROCESS_OUTPUT_TAIL_LINES,
    ResourceProfile,
)
from .screenshot_extractor import extract_key_screenshots
from .text_utils import (
    cleaned_reading_text,
    parse_srt,
    read_text,
    srt_to_plain,
    suspicious_items,
    write_text,
)
from .visual_pack import write_visual_gpt_pack


LogCallback = Callable[[str], None]

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".flv",
    ".wmv",
    ".m4v",
    ".webm",
    ".ts",
}

AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
    ".wma",
}

MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

TEXT_SUBTITLE_CODECS = {
    "subrip",
    "ass",
    "ssa",
    "webvtt",
    "mov_text",
    "text",
}


@dataclass(frozen=True)
class ToolPaths:
    ffmpeg: Path | None
    ffprobe: Path | None
    whisper: Path | None
    model: Path | None


@dataclass(frozen=True)
class SubtitleStream:
    index: int
    codec: str
    language: str
    title: str


@dataclass(frozen=True)
class ProcessResult:
    video_path: Path
    output_dir: Path
    source: str
    raw_srt: Path
    raw_txt: Path
    cleaned_txt: Path
    gpt_pack: Path
    log_file: Path
    screenshot_index: Path | None = None
    visual_gpt_pack: Path | None = None


def discover_tools(root: Path = APP_ROOT) -> ToolPaths:
    ffmpeg = _find_tool(
        root,
        candidates=(
            "tools/ffmpeg/ffmpeg.exe",
            "tools/ffmpeg/bin/ffmpeg.exe",
        ),
        path_name="ffmpeg",
    )
    if not ffmpeg:
        ffmpeg = _find_named_tool_in_dir(root / "tools" / "ffmpeg", "ffmpeg.exe")
    ffprobe = _find_tool(
        root,
        candidates=(
            "tools/ffmpeg/ffprobe.exe",
            "tools/ffmpeg/bin/ffprobe.exe",
        ),
        path_name="ffprobe",
    )
    if not ffprobe:
        ffprobe = _find_named_tool_in_dir(root / "tools" / "ffmpeg", "ffprobe.exe")
    whisper = _find_tool(
        root,
        candidates=(
            "tools/whisper/whisper-cli.exe",
            "tools/whisper/bin/whisper-cli.exe",
            "tools/whisper/main.exe",
            "tools/whisper/bin/main.exe",
        ),
        path_name="whisper-cli",
    )
    if not whisper:
        whisper = _find_whisper_in_tools(root)
    model = _find_model(root)
    return ToolPaths(ffmpeg=ffmpeg, ffprobe=ffprobe, whisper=whisper, model=model)


def list_videos(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS
    )


def process_video(
    video_path: Path,
    *,
    root: Path = APP_ROOT,
    output_root: Path | None = None,
    resource_mode: str = "normal",
    log_callback: LogCallback | None = None,
) -> ProcessResult:
    ensure_runtime_dirs(root)
    video_path = video_path.resolve()
    output_root = (output_root or root / "output").resolve()
    output_dir = _unique_output_dir(output_root / _safe_name(video_path.stem))
    output_dir.mkdir(parents=True, exist_ok=True)
    layout = create_output_layout(output_dir)
    profile = _resource_profile(resource_mode)
    log_file = layout.log_dir / "处理日志.txt"
    write_text(log_file, "")

    def log(message: str) -> None:
        _append_text(log_file, message.rstrip() + "\n")
        if log_callback:
            log_callback(message)

    raw_srt = layout.subtitle_dir / "原始字幕.srt"
    raw_txt = layout.subtitle_dir / "原始字幕.txt"
    cleaned_txt = layout.subtitle_dir / "轻度清洗字幕.txt"
    gpt_md = layout.drag_pack_dir / "发给GPT.md"

    try:
        log(f"开始处理：{video_path}")
        if profile.name == LARGE_BATCH_PROFILE.name:
            log("当前为大批量省资源模式：减少截图数量，压缩子进程日志输出。")
        tools = discover_tools(root)
        source = _create_subtitles(video_path, raw_srt, layout.log_dir, tools, log)

        entries = parse_srt(read_text(raw_srt))
        if not entries:
            raise RuntimeError("字幕文件已生成，但无法解析为有效 SRT。")

        plain_text = srt_to_plain(entries)
        write_text(raw_txt, plain_text)

        cleaned_text = cleaned_reading_text(entries)
        write_text(cleaned_txt, cleaned_text)

        screenshot_index_text = ""
        screenshot_index_path: Path | None = None
        visual_gpt_pack_path: Path | None = None
        if video_path.suffix.lower() in VIDEO_EXTENSIONS:
            if tools.ffmpeg:
                try:
                    screenshot_result = extract_key_screenshots(
                        video_path=video_path,
                        entries=entries,
                        output_dir=layout.screenshot_dir,
                        ffmpeg=tools.ffmpeg,
                        max_screenshots=profile.screenshot_max,
                        max_scene_only_screenshots=profile.screenshot_scene_only_max,
                        scene_sample_fps=profile.scene_sample_fps,
                        log=log,
                    )
                    screenshot_index_path = screenshot_result.index_path
                    visual_pack = write_visual_gpt_pack(
                        output_dir=layout.drag_pack_dir,
                        video_name=video_path.name,
                        source=source,
                        subtitle_text=plain_text,
                        screenshot_result=screenshot_result,
                        ffmpeg=tools.ffmpeg,
                        max_images=profile.visual_pack_max_images,
                    )
                    if visual_pack:
                        visual_gpt_pack_path = visual_pack.pack_path
                        log(f"GPT 图文总结包已生成：{visual_pack.pack_path}")
                        log(f"精选图片压缩包：{visual_pack.archive_path}")
                    screenshot_index_text = _format_screenshot_section(screenshot_result, visual_gpt_pack_path)
                except Exception as exc:
                    log(f"关键截图生成失败，已跳过：{exc}")
            else:
                log("缺少 ffmpeg，已跳过关键截图。")
        elif video_path.suffix.lower() in AUDIO_EXTENSIONS:
            log("音频输入无画面，已跳过关键截图。")

        glossary = _read_glossary(root)
        suspicious = suspicious_items(entries)
        if visual_gpt_pack_path is None:
            write_gpt_pack(
                output_path=gpt_md,
                template_path=root / "templates" / "gpt_prompt.md",
                video_name=video_path.name,
                source=source,
                glossary_text=glossary,
                subtitle_text=plain_text,
                suspicious=suspicious,
                screenshot_index_text=screenshot_index_text,
            )

        write_single_next_steps(
            output_dir=output_dir,
            source_name=video_path.name,
            has_visual_pack=visual_gpt_pack_path is not None,
            has_screenshots=screenshot_index_path is not None,
        )

        log(f"完成：{output_dir}")
        return ProcessResult(
            video_path=video_path,
            output_dir=output_dir,
            source=source,
            raw_srt=raw_srt,
            raw_txt=raw_txt,
            cleaned_txt=cleaned_txt,
            gpt_pack=gpt_md,
            log_file=log_file,
            screenshot_index=screenshot_index_path,
            visual_gpt_pack=visual_gpt_pack_path,
        )
    except Exception as exc:
        log(f"失败：{exc}")
        raise


def tool_status(root: Path = APP_ROOT) -> list[str]:
    tools = discover_tools(root)
    lines = [
        f"ffmpeg：{tools.ffmpeg or '未找到'}",
        f"ffprobe：{tools.ffprobe or '未找到'}",
        f"whisper.cpp：{tools.whisper or '未找到'}",
        f"默认模型：{tools.model or '未找到'}",
    ]
    return lines


def _create_subtitles(
    video_path: Path,
    raw_srt: Path,
    output_dir: Path,
    tools: ToolPaths,
    log: LogCallback,
) -> str:
    if video_path.suffix.lower() in AUDIO_EXTENSIONS:
        log("检测到音频文件，跳过视频字幕轨检测。")
        return _create_subtitles_from_audio(video_path, raw_srt, output_dir, tools, log)

    if tools.ffprobe:
        stream = _first_text_subtitle(video_path, tools.ffprobe, log)
        if stream:
            if not tools.ffmpeg:
                raise RuntimeError("发现字幕轨，但缺少 ffmpeg，无法提取字幕。")
            log(
                f"发现文本字幕轨：index={stream.index}, codec={stream.codec}, "
                f"language={stream.language or 'unknown'}"
            )
            _extract_text_subtitle(video_path, raw_srt, stream, tools.ffmpeg, log)
            return "视频内置文本字幕轨"
        log("未发现可直接提取的文本字幕轨，准备语音识别。")
    else:
        log("未找到 ffprobe，跳过字幕轨检测，准备语音识别。")

    return _create_subtitles_from_audio(video_path, raw_srt, output_dir, tools, log)


def _create_subtitles_from_audio(
    source_path: Path,
    raw_srt: Path,
    output_dir: Path,
    tools: ToolPaths,
    log: LogCallback,
) -> str:
    if not tools.ffmpeg:
        raise RuntimeError("缺少 ffmpeg，无法把媒体音频转换为 whisper.cpp 需要的 WAV。")
    if not tools.whisper:
        raise RuntimeError("缺少 whisper.cpp 可执行文件：请放到 tools/whisper/whisper-cli.exe。")
    if not tools.model:
        raise RuntimeError("缺少 whisper.cpp 模型：请放到 tools/whisper/models/ggml-small.bin。")

    wav_path = output_dir / "临时音频.wav"
    _extract_audio(source_path, wav_path, tools.ffmpeg, log)
    try:
        _run_whisper(wav_path, raw_srt, output_dir, tools.whisper, tools.model, log)
    finally:
        try:
            wav_path.unlink()
        except FileNotFoundError:
            pass
    return "本地语音识别 whisper.cpp"


def _first_text_subtitle(
    video_path: Path,
    ffprobe: Path,
    log: LogCallback,
) -> SubtitleStream | None:
    cmd = [
        str(ffprobe),
        "-v",
        "error",
        "-select_streams",
        "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title",
        "-of",
        "json",
        str(video_path),
    ]
    completed = _run(cmd, log, capture_stdout=True)
    data = json.loads(completed.stdout or "{}")
    for stream in data.get("streams", []):
        codec = str(stream.get("codec_name") or "").lower()
        if codec not in TEXT_SUBTITLE_CODECS:
            continue
        tags = stream.get("tags") or {}
        return SubtitleStream(
            index=int(stream["index"]),
            codec=codec,
            language=str(tags.get("language") or ""),
            title=str(tags.get("title") or ""),
        )
    return None


def _extract_text_subtitle(
    video_path: Path,
    raw_srt: Path,
    stream: SubtitleStream,
    ffmpeg: Path,
    log: LogCallback,
) -> None:
    cmd = [
        str(ffmpeg),
        "-y",
        "-i",
        str(video_path),
        "-map",
        f"0:{stream.index}",
        "-c:s",
        "srt",
        str(raw_srt),
    ]
    _run(cmd, log)
    if not raw_srt.exists() or raw_srt.stat().st_size == 0:
        raise RuntimeError("ffmpeg 未生成有效字幕文件。")


def _extract_audio(source_path: Path, wav_path: Path, ffmpeg: Path, log: LogCallback) -> None:
    log("正在抽取 16kHz 单声道音频。")
    cmd = [
        str(ffmpeg),
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    _run(cmd, log)
    if not wav_path.exists() or wav_path.stat().st_size == 0:
        raise RuntimeError("音频抽取失败。")


def _run_whisper(
    wav_path: Path,
    raw_srt: Path,
    output_dir: Path,
    whisper: Path,
    model: Path,
    log: LogCallback,
) -> None:
    log("正在调用 whisper.cpp 识别字幕。")
    runtime_dir, runtime_model = _prepare_ascii_runtime(output_dir, wav_path, model, log)
    runtime_wav = runtime_dir / "audio.wav"
    output_base = runtime_dir / "whisper_result"
    cmd = [
        str(whisper),
        "-m",
        str(runtime_model),
        "-f",
        str(runtime_wav),
        "-l",
        "auto",
        "-osrt",
        "-otxt",
        "-of",
        str(output_base),
    ]
    try:
        _run(cmd, log)

        generated_srt = output_base.with_suffix(".srt")
        if not generated_srt.exists():
            raise RuntimeError("whisper.cpp 未生成 SRT，请确认 whisper-cli 版本支持 -osrt。")
        shutil.copyfile(generated_srt, raw_srt)

        generated_txt = output_base.with_suffix(".txt")
        if generated_txt.exists():
            shutil.copyfile(generated_txt, output_dir / "whisper_result.txt")
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        log(f"已清理 whisper 临时运行目录：{runtime_dir}")


def _prepare_ascii_runtime(
    output_dir: Path, wav_path: Path, model: Path, log: LogCallback
) -> tuple[Path, Path]:
    runtime_root = _ascii_runtime_root()
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime_dir = runtime_root / f"job_{uuid.uuid4().hex[:12]}"
    runtime_dir.mkdir(parents=True, exist_ok=False)
    runtime_model = runtime_root / "models" / model.name
    runtime_model.parent.mkdir(parents=True, exist_ok=True)

    log(f"为 whisper.cpp 准备纯英文临时路径：{runtime_dir}")
    shutil.copyfile(wav_path, runtime_dir / "audio.wav")
    if not runtime_model.exists() or runtime_model.stat().st_size != model.stat().st_size:
        log(f"缓存 whisper 模型到纯英文路径：{runtime_model}")
        shutil.copyfile(model, runtime_model)
    write_text(
        output_dir / "whisper运行目录.txt",
        f"本次 whisper 临时运行目录：{runtime_dir}\n处理结束后会自动清理该目录，只保留模型缓存。\n",
    )
    return runtime_dir, runtime_model


def _ascii_runtime_root() -> Path:
    drive = APP_ROOT.drive
    if drive:
        return Path(f"{drive}\\video_to_gpt_runtime")
    return APP_ROOT / "tools" / "runtime_ascii"


def _format_screenshot_section(screenshot_result, visual_gpt_pack_path: Path | None) -> str:
    if screenshot_result.count <= 0:
        return ""
    visual_pack_text = ""
    if visual_gpt_pack_path:
        visual_pack_text = (
            "\n\n如果我要生成图文并茂的总结性文章，优先使用这个专用文件，并同时上传精选截图：\n\n"
            f"```text\n{visual_gpt_pack_path}\n```"
        )
    return f"""# 关键截图索引

本地工具已生成 {screenshot_result.count} 张关键截图。截图索引如下。GPT 无法直接读取我电脑上的本地图片路径；如果没有随对话上传图片，请不要编造图片细节，只能根据字幕和截图时间点辅助整理。

关键截图索引文件：

```text
{screenshot_result.index_path}
```
{visual_pack_text}

{screenshot_result.index_text.strip()}
"""


def _resource_profile(resource_mode: str) -> ResourceProfile:
    if resource_mode == LARGE_BATCH_PROFILE.name:
        return LARGE_BATCH_PROFILE
    return NORMAL_PROFILE


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(text)


def _run(
    cmd: list[str],
    log: LogCallback,
    *,
    capture_stdout: bool = False,
) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(_quote(part) for part in cmd))
    if capture_stdout:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        _log_process_output(log, completed.stdout, "stdout")
        _log_process_output(log, completed.stderr, "stderr")
    else:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output_tail: list[str] = []
        line_count = 0
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
                line_count += 1
                output_tail.append(line.rstrip())
                _trim_tail(output_tail)

            now = time.monotonic()
            if now - last_heartbeat >= PROCESS_HEARTBEAT_SECONDS and process.poll() is None:
                log(f"命令仍在运行，已耗时 {_format_duration(now - start_time)}。")
                last_heartbeat = now

            if process.poll() is not None:
                while True:
                    try:
                        remaining = line_queue.get_nowait()
                    except queue.Empty:
                        break
                    if remaining:
                        line_count += 1
                        output_tail.append(remaining.rstrip())
                        _trim_tail(output_tail)
                break

        returncode = process.wait()
        reader.join(timeout=1.0)
        output_text = "\n".join(line for line in output_tail if line.strip())
        if output_text:
            prefix = "命令输出摘要"
            if line_count > len(output_tail):
                prefix += f"（仅保留最后 {len(output_tail)} 行，共 {line_count} 行）"
            log(f"{prefix}：\n{output_text}")
        completed = subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=output_text)

    if completed.returncode != 0:
        raise RuntimeError(f"命令执行失败，退出码 {completed.returncode}")
    return completed


def _read_process_output(stream, line_queue: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            line_queue.put(line)
    finally:
        line_queue.put(None)


def _log_process_output(log: LogCallback, text: str | None, stream_name: str) -> None:
    if not text or not text.strip():
        return
    stripped = text.strip()
    trimmed = _tail_text(stripped)
    suffix = ""
    if len(trimmed) < len(stripped):
        suffix = "（已截断，仅保留尾部）"
    log(f"{stream_name}{suffix}：\n{trimmed}")


def _trim_tail(lines: list[str]) -> None:
    while len(lines) > PROCESS_OUTPUT_TAIL_LINES:
        del lines[0]
    while sum(len(line) + 1 for line in lines) > PROCESS_OUTPUT_TAIL_CHARS and len(lines) > 1:
        del lines[0]


def _tail_text(text: str) -> str:
    lines = text.splitlines()
    while len(lines) > PROCESS_OUTPUT_TAIL_LINES:
        del lines[0]
    while sum(len(line) + 1 for line in lines) > PROCESS_OUTPUT_TAIL_CHARS and len(lines) > 1:
        del lines[0]
    return "\n".join(lines)


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


def _find_tool(root: Path, *, candidates: tuple[str, ...], path_name: str) -> Path | None:
    for candidate in candidates:
        path = root / candidate
        if path.exists():
            return path.resolve()

    found = shutil.which(path_name)
    if found:
        return Path(found).resolve()

    return None


def _find_whisper_in_tools(root: Path) -> Path | None:
    whisper_dir = root / "tools" / "whisper"
    return _find_named_tool_in_dir(whisper_dir, "whisper-cli.exe") or _find_named_tool_in_dir(
        whisper_dir, "main.exe"
    )


def _find_named_tool_in_dir(tool_dir: Path, name: str) -> Path | None:
    if not tool_dir.exists():
        return None
    matches = sorted(tool_dir.rglob(name))
    if matches:
        return matches[0].resolve()
    return None


def _find_model(root: Path) -> Path | None:
    preferred = root / "tools" / "whisper" / "models" / "ggml-small.bin"
    if preferred.exists():
        return preferred.resolve()

    model_dir = root / "tools" / "whisper" / "models"
    for name in ("ggml-medium.bin", "ggml-base.bin", "ggml-tiny.bin"):
        path = model_dir / name
        if path.exists():
            return path.resolve()
    return None


def _read_glossary(root: Path) -> str:
    path = root / "config" / "术语词库.txt"
    if not path.exists():
        return ""
    return read_text(path)


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned or "video"


def _unique_output_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.name}_{i}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("输出目录重名过多，请清理 output 目录。")


def _quote(value: str) -> str:
    if " " in value or "\t" in value:
        return f'"{value}"'
    return value
