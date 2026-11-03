from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .batch_integrator import integrate_batch_results
from .gpt_pack import DEFAULT_TEMPLATE
from .paths import APP_ROOT, ensure_runtime_dirs
from .processor import MEDIA_EXTENSIONS, list_videos, process_video, tool_status
from .resource import GUI_LOG_MAX_LINES, profile_for_count
from .text_utils import write_text

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    TkinterDnD = None


BaseWindow = TkinterDnD.Tk if TkinterDnD else tk.Tk


@dataclass
class QueueState:
    total: int = 0
    current_index: int = 0
    current_name: str = "尚未开始"
    current_path: str = ""
    current_output: str = ""
    phase: str = "空闲"
    success: int = 0
    failed: int = 0
    started_at: datetime | None = None
    current_started_at: datetime | None = None
    last_message: str = ""


class VideoToGptApp(BaseWindow):
    def __init__(self) -> None:
        super().__init__()
        self.title("视频转字幕与 GPT 阅读稿处理包")
        self.geometry("980x680")
        self.minsize(840, 560)

        self.selected_videos: list[Path] = []
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.output_dirs_by_media: dict[Path, Path] = {}
        self.last_output_dir: Path | None = None
        self._log_line_count = 0
        self.queue_state = QueueState()
        self.queue_status_path = APP_ROOT / "output" / "队列状态.txt"

        initialize_runtime_files()
        self._build_ui()
        self._enable_file_drop()
        self._refresh_tool_status()
        self.after(100, self._poll_events)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        header = ttk.Frame(self, padding=(14, 12, 14, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text="视频转字幕与 GPT 阅读稿处理包", font=("Microsoft YaHei UI", 15, "bold"))
        title.grid(row=0, column=0, sticky="w")

        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, sticky="e")

        ttk.Button(actions, text="选择视频", command=self._select_videos).grid(row=0, column=0, padx=4)
        ttk.Button(actions, text="选择文件夹", command=self._select_folder).grid(row=0, column=1, padx=4)
        ttk.Button(actions, text="清空", command=self._clear_selection).grid(row=0, column=2, padx=4)

        status_frame = ttk.LabelFrame(self, text="本地依赖状态", padding=10)
        status_frame.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        status_frame.columnconfigure(0, weight=1)

        self.tool_status_var = tk.StringVar()
        ttk.Label(status_frame, textvariable=self.tool_status_var, justify="left").grid(row=0, column=0, sticky="w")
        ttk.Button(status_frame, text="刷新", command=self._refresh_tool_status).grid(row=0, column=1, sticky="e")

        queue_frame = ttk.LabelFrame(self, text="处理队列", padding=(10, 7))
        queue_frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))
        queue_frame.columnconfigure(1, weight=1)
        self.queue_summary_var = tk.StringVar(value="空闲，尚未开始处理。")
        self.queue_current_var = tk.StringVar(value="当前文件：-")
        ttk.Label(queue_frame, textvariable=self.queue_summary_var).grid(row=0, column=0, sticky="w", padx=(0, 18))
        ttk.Label(queue_frame, textvariable=self.queue_current_var).grid(row=0, column=1, sticky="w")
        ttk.Button(queue_frame, text="打开状态文件", command=self._open_queue_status).grid(
            row=0, column=2, sticky="e"
        )

        body = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        body.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 10))

        left = ttk.Frame(body, padding=(0, 0, 10, 0))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        body.add(left, weight=1)

        ttk.Label(left, text="待处理视频/音频").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.video_list = tk.Listbox(left, height=12, activestyle="none")
        self.video_list.grid(row=1, column=0, sticky="nsew")
        video_scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.video_list.yview)
        video_scroll.grid(row=1, column=1, sticky="ns")
        self.video_list.configure(yscrollcommand=video_scroll.set)

        self.drop_hint_var = tk.StringVar(
            value=f"可以把视频、音频或文件夹拖到窗口；也可以放入 {APP_ROOT / 'input'}。"
        )
        ttk.Label(left, textvariable=self.drop_hint_var, wraplength=360, foreground="#555").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )

        right = ttk.Frame(body)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        body.add(right, weight=2)

        ttk.Label(right, text="处理日志").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.log_text = tk.Text(right, height=18, wrap="word", state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(right, orient=tk.VERTICAL, command=self.log_text.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        footer = ttk.Frame(self, padding=(14, 0, 14, 14))
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(footer, text="开始处理", command=self._start_processing)
        self.start_button.grid(row=0, column=0, padx=(0, 8))

        self.progress = ttk.Progressbar(footer, mode="determinate", maximum=1, value=0)
        self.progress.grid(row=0, column=1, sticky="ew", padx=8)

        ttk.Button(footer, text="打开输出目录", command=self._open_output_dir).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(footer, text="打开 GPT 文件夹", command=self._open_gpt_dir).grid(
            row=0, column=3, padx=(8, 0)
        )

    def _select_videos(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(MEDIA_EXTENSIONS))
        paths = filedialog.askopenfilenames(
            title="选择视频或音频文件",
            filetypes=(("媒体文件", patterns), ("所有文件", "*.*")),
        )
        if paths:
            self._add_videos(Path(path) for path in paths)

    def _select_folder(self) -> None:
        folder = filedialog.askdirectory(title="选择包含视频的文件夹")
        if folder:
            videos = list_videos(Path(folder))
            if not videos:
                messagebox.showinfo("未找到媒体", "这个文件夹下没有发现支持的视频或音频文件。")
                return
            self._add_videos(videos)

    def _add_videos(self, paths) -> None:
        existing = {path.resolve() for path in self.selected_videos}
        added = 0
        for path in paths:
            resolved = Path(path).resolve()
            if resolved in existing:
                continue
            if resolved.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            self.selected_videos.append(resolved)
            existing.add(resolved)
            self.video_list.insert(tk.END, str(resolved))
            added += 1
        if added:
            self._append_log(f"已加入 {added} 个媒体文件。")

    def _enable_file_drop(self) -> None:
        if not DND_FILES or not hasattr(self, "drop_target_register"):
            self.drop_hint_var.set("拖放不可用：缺少 tkinterdnd2。请使用选择视频或选择文件夹。")
            self._append_log("拖放未启用：缺少 tkinterdnd2。")
            return

        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self._on_drop_event)
        self._append_log("已启用文件拖放。")

    def _on_drop_event(self, event) -> None:
        try:
            raw_paths = self.tk.splitlist(event.data)
            paths = [Path(path) for path in raw_paths]
        except Exception as exc:
            self._append_log(f"解析拖入内容失败：{exc}")
            return
        self._handle_dropped_paths(paths)

    def _handle_dropped_paths(self, paths: list[Path]) -> None:
        try:
            media_paths: list[Path] = []
            for path in paths:
                if path.is_dir():
                    media_paths.extend(list_videos(path))
                elif path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
                    media_paths.append(path)
        except Exception as exc:
            self._append_log(f"读取拖入文件失败：{exc}")
            return

        if not media_paths:
            self._append_log("拖入内容里没有发现支持的视频或音频文件。")
            return

        self._add_videos(media_paths)

    def _clear_selection(self) -> None:
        self.selected_videos.clear()
        self.output_dirs_by_media.clear()
        self.video_list.delete(0, tk.END)

    def _refresh_tool_status(self) -> None:
        self.tool_status_var.set("\n".join(tool_status(APP_ROOT)))

    def _start_processing(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        videos = self.selected_videos[:]
        if not videos:
            videos = list_videos(APP_ROOT / "input")

        if not videos:
            messagebox.showinfo("没有媒体", "请先选择视频/音频，或把文件放到 input 目录。")
            return

        self.output_dirs_by_media.clear()
        self.last_output_dir = None
        profile = profile_for_count(len(videos))
        self.queue_state = QueueState(
            total=len(videos),
            phase="准备处理",
            started_at=datetime.now(),
        )
        self._refresh_queue_ui()
        self._write_queue_status()
        self._append_log(f"准备处理 {len(videos)} 个媒体文件。")
        if profile.name == "large_batch":
            self._append_log(
                "已启用大批量省资源模式：单文件串行处理，降低截图数量，并使用轻量整合去重。"
            )
        self.start_button.configure(state="disabled")
        self.progress.configure(maximum=len(videos), value=0)
        self.worker = threading.Thread(target=self._run_processing, args=(videos, profile.name), daemon=True)
        self.worker.start()

    def _run_processing(self, videos: list[Path], resource_mode: str) -> None:
        successful_results = []
        success_count = 0
        failed_count = 0
        batch_output_root = APP_ROOT / "output" / "单文件任务"
        if len(videos) >= 2:
            batch_output_root = (
                APP_ROOT / "output" / "批量任务" / f"批量任务_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            batch_output_root.mkdir(parents=True, exist_ok=False)
            self.events.put(("output_dir", str(batch_output_root)))
            self.events.put(("log", f"本次多文件任务输出目录：{batch_output_root}"))

        for index, video in enumerate(videos, start=1):
            self.events.put(
                (
                    "queue_state",
                    _queue_state_message(
                        total=len(videos),
                        current_index=index,
                        current_name=video.name,
                        current_path=video,
                        phase="处理中",
                        success=success_count,
                        failed=failed_count,
                        current_output="",
                        started_at=self.queue_state.started_at,
                        current_started_at=datetime.now(),
                    ),
                )
            )
            self.events.put(("log", f"\n[{index}/{len(videos)}] {video.name}"))
            try:
                result = process_video(
                    video,
                    root=APP_ROOT,
                    output_root=batch_output_root,
                    resource_mode=resource_mode,
                    log_callback=lambda message: self.events.put(("log", message)),
                )
                successful_results.append(result)
                success_count += 1
                self.events.put(("result_dir", f"{video.resolve()}\t{result.output_dir}"))
                self.events.put(
                    (
                        "queue_state",
                        _queue_state_message(
                            total=len(videos),
                            current_index=index,
                            current_name=video.name,
                            current_path=video,
                            phase="当前文件完成",
                            success=success_count,
                            failed=failed_count,
                            current_output=result.output_dir,
                            started_at=self.queue_state.started_at,
                            current_started_at=None,
                        ),
                    )
                )
            except Exception as exc:
                failed_count += 1
                self.events.put(
                    (
                        "queue_state",
                        _queue_state_message(
                            total=len(videos),
                            current_index=index,
                            current_name=video.name,
                            current_path=video,
                            phase="当前文件失败",
                            success=success_count,
                            failed=failed_count,
                            current_output="",
                            started_at=self.queue_state.started_at,
                            current_started_at=None,
                            last_message=str(exc),
                        ),
                    )
                )
                self.events.put(("log", f"处理失败：{video.name} - {exc}"))

        if len(successful_results) >= 2:
            try:
                integration_result = integrate_batch_results(
                    successful_results,
                    output_root=batch_output_root,
                    resource_mode=resource_mode,
                    log=lambda message: self.events.put(("log", message)),
                )
                if integration_result:
                    self.events.put(("output_dir", str(batch_output_root)))
            except Exception as exc:
                self.events.put(("log", f"批量内容整合失败：{exc}"))
        else:
            self.events.put(("log", "成功文件少于 2 个，跳过批量内容整合。"))
            if len(successful_results) == 1:
                self.events.put(("output_dir", str(successful_results[0].output_dir)))
        self.events.put(
            (
                "queue_state",
                _queue_state_message(
                    total=len(videos),
                    current_index=len(videos),
                    current_name="全部任务",
                    current_path="",
                    phase="全部完成",
                    success=success_count,
                    failed=failed_count,
                    current_output=batch_output_root if len(videos) >= 2 else (
                        successful_results[-1].output_dir if successful_results else ""
                    ),
                    started_at=self.queue_state.started_at,
                    current_started_at=None,
                ),
            )
        )
        self.events.put(("done", "全部处理结束。"))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "log":
                    self._append_log(message)
                elif kind == "output_dir":
                    self.last_output_dir = Path(message)
                elif kind == "result_dir":
                    media_text, output_text = message.split("\t", 1)
                    self.output_dirs_by_media[Path(media_text).resolve()] = Path(output_text)
                    self.last_output_dir = Path(output_text)
                elif kind == "queue_state":
                    self._apply_queue_state_message(message)
                elif kind == "done":
                    self._append_log(message)
                    self.start_button.configure(state="normal")
                    self._refresh_tool_status()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _append_log(self, message: str) -> None:
        line_count = max(message.count("\n") + 1, 1)
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, message.rstrip() + "\n")
        self._log_line_count += line_count
        if self._log_line_count > GUI_LOG_MAX_LINES:
            delete_to = max(self._log_line_count - GUI_LOG_MAX_LINES, 1)
            self.log_text.delete("1.0", f"{delete_to + 1}.0")
            self._log_line_count = GUI_LOG_MAX_LINES
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _open_output_dir(self) -> None:
        output_dir = self._selected_output_dir() or self.last_output_dir or APP_ROOT / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(output_dir)

    def _open_gpt_dir(self) -> None:
        output_dir = self._selected_output_dir() or self.last_output_dir
        if output_dir and (output_dir / "整合结果" / "00_拖给GPT").is_dir():
            output_dir = output_dir / "整合结果"
        if output_dir:
            gpt_dir = output_dir / "00_拖给GPT"
        else:
            gpt_dir = APP_ROOT / "output"
        gpt_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(gpt_dir)

    def _open_queue_status(self) -> None:
        self.queue_status_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.queue_status_path.exists():
            self._write_queue_status()
        os.startfile(self.queue_status_path)

    def _apply_queue_state_message(self, message: str) -> None:
        fields = {}
        for line in message.splitlines():
            if "\t" in line:
                key, value = line.split("\t", 1)
                fields[key] = value
        self.queue_state.total = int(fields.get("total", self.queue_state.total))
        self.queue_state.current_index = int(fields.get("current_index", self.queue_state.current_index))
        self.queue_state.current_name = fields.get("current_name", self.queue_state.current_name)
        self.queue_state.current_path = fields.get("current_path", self.queue_state.current_path)
        self.queue_state.current_output = fields.get("current_output", self.queue_state.current_output)
        self.queue_state.phase = fields.get("phase", self.queue_state.phase)
        self.queue_state.success = int(fields.get("success", self.queue_state.success))
        self.queue_state.failed = int(fields.get("failed", self.queue_state.failed))
        self.queue_state.last_message = fields.get("last_message", self.queue_state.last_message)
        self.queue_state.current_started_at = None
        self._refresh_queue_ui()
        self._write_queue_status()

    def _refresh_queue_ui(self) -> None:
        state = self.queue_state
        remaining = max(state.total - state.success - state.failed, 0)
        self.queue_summary_var.set(
            f"{state.phase} | {state.success + state.failed}/{state.total} 已结束 | "
            f"成功 {state.success} | 失败 {state.failed} | 剩余 {remaining}"
        )
        self.queue_current_var.set(f"当前文件：{state.current_name}")
        if state.total:
            self.progress.configure(maximum=state.total, value=state.success + state.failed)

    def _write_queue_status(self) -> None:
        state = self.queue_state
        now = datetime.now()
        remaining = max(state.total - state.success - state.failed, 0)
        elapsed = _format_duration(
            (now - state.started_at).total_seconds()
        ) if state.started_at else "-"
        status = "\n".join(
            [
                "视频转文章处理队列状态",
                "======================",
                f"更新时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                f"当前状态：{state.phase}",
                f"总进度：{state.success + state.failed}/{state.total} 已结束，剩余 {remaining} 个",
                f"成功：{state.success} 个",
                f"失败：{state.failed} 个",
                f"当前文件序号：{state.current_index}/{state.total}",
                f"当前文件：{state.current_name}",
                f"当前文件路径：{state.current_path}",
                f"当前输出目录：{state.current_output}",
                f"队列已运行：{elapsed}",
                f"最近消息：{state.last_message or '-'}",
                "",
                "完成判断：当前状态显示为“全部完成”时，本轮任务已经结束。",
                "GPT 入口：每个任务输出目录下的 00_拖给GPT 文件夹。",
            ]
        )
        try:
            write_text(self.queue_status_path, status + "\n")
        except OSError:
            pass

    def _selected_output_dir(self) -> Path | None:
        selection = self.video_list.curselection()
        if not selection:
            return None
        try:
            media_path = Path(self.video_list.get(selection[0])).resolve()
        except Exception:
            return None
        return self.output_dirs_by_media.get(media_path)


def initialize_runtime_files() -> None:
    ensure_runtime_dirs(APP_ROOT)

    template_path = APP_ROOT / "templates" / "gpt_prompt.md"
    if not template_path.exists():
        write_text(template_path, DEFAULT_TEMPLATE)

    glossary_path = APP_ROOT / "config" / "术语词库.txt"
    if not glossary_path.exists():
        write_text(
            glossary_path,
            "# 每行一个术语。GPT 处理包会自动引用这里的词。\n"
            "# 示例：\n"
            "大语言模型\n"
            "提示词工程\n"
            "知识蒸馏\n",
        )


def _queue_state_message(
    *,
    total: int,
    current_index: int,
    current_name: str,
    current_path: Path | str,
    phase: str,
    success: int,
    failed: int,
    current_output: Path | str,
    started_at: datetime | None,
    current_started_at: datetime | None,
    last_message: str = "",
) -> str:
    values = {
        "total": str(total),
        "current_index": str(current_index),
        "current_name": current_name,
        "current_path": str(current_path),
        "current_output": str(current_output),
        "phase": phase,
        "success": str(success),
        "failed": str(failed),
        "started_at": started_at.isoformat() if started_at else "",
        "current_started_at": current_started_at.isoformat() if current_started_at else "",
        "last_message": last_message.replace("\n", " "),
    }
    return "\n".join(f"{key}\t{value}" for key, value in values.items())


def _format_duration(seconds: float) -> str:
    total = max(int(seconds), 0)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{secs:02d}秒"
    if minutes:
        return f"{minutes}分{secs:02d}秒"
    return f"{secs}秒"


def main() -> None:
    app = VideoToGptApp()
    app.mainloop()
