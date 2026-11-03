from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Allow the script to be launched directly from the scripts directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from video_to_gpt.paths import APP_ROOT
from video_to_gpt.processor import process_video


WAIT_SECONDS = 30


def main() -> int:
    parser = argparse.ArgumentParser(description="Serially process audio files into separate GPT packs.")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()

    _lower_process_priority()
    log_dir = APP_ROOT / "output" / f"指定音频批处理_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "处理日志.txt"

    paths = [path.resolve() for path in args.paths]
    status_file = log_dir / "队列状态.txt"
    public_status_file = APP_ROOT / "output" / "队列状态.txt"
    state = {
        "total": len(paths),
        "current_index": 0,
        "current_name": "尚未开始",
        "current_path": "",
        "current_output": "",
        "phase": "初始化",
        "success": 0,
        "failed": 0,
        "started_at": datetime.now(),
        "current_started_at": None,
        "last_message": "",
        "done": False,
    }

    def write_status() -> None:
        now = datetime.now()
        elapsed = _format_duration((now - state["started_at"]).total_seconds())
        current_elapsed = "-"
        if state["current_started_at"] is not None:
            current_elapsed = _format_duration((now - state["current_started_at"]).total_seconds())
        remaining = max(state["total"] - state["success"] - state["failed"], 0)
        text = "\n".join(
            [
                "后台音频队列状态",
                "================",
                f"更新时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                f"当前状态：{state['phase']}",
                f"总进度：{state['success'] + state['failed']}/{state['total']} 已结束，剩余 {remaining} 个",
                f"成功：{state['success']} 个",
                f"失败：{state['failed']} 个",
                f"当前文件序号：{state['current_index']}/{state['total']}",
                f"当前文件：{state['current_name']}",
                f"当前文件路径：{state['current_path']}",
                f"当前输出目录：{state['current_output']}",
                f"队列已运行：{elapsed}",
                f"当前文件已运行：{current_elapsed}",
                f"最近消息：{state['last_message']}",
                "",
                "完成判断：",
                "- 当前状态显示为“全部完成”时，队列已经结束。",
                "- 每个完成任务的可拖给 GPT 文件在对应输出目录的 00_拖给GPT 文件夹里。",
                "",
                f"本次批处理日志：{log_file}",
            ]
        )
        for target in (status_file, public_status_file):
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text + "\n", encoding="utf-8-sig")
            except Exception:
                pass

    def log(message: str) -> None:
        line = message.rstrip() + "\n"
        state["last_message"] = message.rstrip()
        print(message, flush=True)
        with log_file.open("a", encoding="utf-8-sig", newline="\n") as stream:
            stream.write(line)
        write_status()

    log(f"已建立独立音频处理队列，共 {len(paths)} 个文件。")
    log("当前任务结束前不会启动新的 whisper，避免两个识别任务争抢资源。")

    success = 0
    failed = 0
    for index, path in enumerate(paths, start=1):
        state["current_index"] = index
        state["current_name"] = path.name
        state["current_path"] = str(path)
        state["current_output"] = ""
        state["phase"] = "检查文件"
        state["current_started_at"] = None
        write_status()

        if not path.is_file():
            failed += 1
            state["failed"] = failed
            state["phase"] = "文件不存在，已跳过"
            log(f"[{index}/{len(paths)}] 文件不存在，跳过：{path}")
            continue

        state["phase"] = "等待其他 whisper 任务结束"
        write_status()
        _wait_for_existing_whisper(log, write_status)
        state["phase"] = "处理中"
        state["current_started_at"] = datetime.now()
        write_status()
        log(f"[{index}/{len(paths)}] 开始处理：{path.name}")
        try:
            result = process_video(
                path,
                root=APP_ROOT,
                output_root=APP_ROOT / "output" / "单文件任务",
                resource_mode="normal",
                log_callback=log,
            )
            success += 1
            state["success"] = success
            state["current_output"] = str(result.output_dir)
            state["phase"] = "当前文件完成"
            log(f"[{index}/{len(paths)}] 完成，独立输出目录：{result.output_dir}")
        except Exception as exc:
            failed += 1
            state["failed"] = failed
            state["phase"] = "当前文件失败"
            log(f"[{index}/{len(paths)}] 处理失败：{path.name} - {exc}")

    state["done"] = True
    state["phase"] = "全部完成"
    log(f"队列结束：成功 {success} 个，失败 {failed} 个。")
    log(f"批处理日志：{log_file}")
    return 0 if failed == 0 else 1


def _wait_for_existing_whisper(log, write_status=None) -> None:
    announced = False
    while _whisper_running():
        if not announced:
            log("检测到已有 whisper 任务，队列等待中；当前任务结束后自动继续。")
            announced = True
        if write_status is not None:
            write_status()
        time.sleep(WAIT_SECONDS)


def _whisper_running() -> bool:
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq whisper-cli.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
    )
    return "whisper-cli.exe" in completed.stdout.lower()


def _lower_process_priority() -> None:
    if os.name != "nt":
        return
    try:
        below_normal = 0x00004000
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(),
            below_normal,
        )
    except Exception:
        pass


def _format_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"


if __name__ == "__main__":
    raise SystemExit(main())
