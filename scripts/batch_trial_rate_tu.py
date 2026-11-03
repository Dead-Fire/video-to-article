from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from video_to_gpt.batch_integrator import integrate_batch_results
from video_to_gpt.processor import APP_ROOT, process_video


AV_IDS = [
    "Av1605985004",
    "Av1052472961",
    "Av1804864348",
    "Av536565215",
    "Av965622125",
]


def main() -> None:
    download_dir = Path(r"E:\application\jjdownload\Download")
    output_root = (
        APP_ROOT
        / "output"
        / "批量任务"
        / f"批量任务_率土试跑_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    paths: list[Path] = []

    for av_id in AV_IDS:
        matches = sorted(download_dir.glob(f"*{av_id}*.mp3"))
        if not matches:
            raise FileNotFoundError(f"找不到音频：{av_id}")
        paths.append(matches[0])

    results = []

    def log(message: str) -> None:
        print(message, flush=True)

    log(f"准备批量处理 {len(paths)} 个音频。")
    log(f"本次多文件任务输出目录：{output_root}")
    for index, path in enumerate(paths, start=1):
        log("")
        log(f"[{index}/{len(paths)}] {path.name}")
        try:
            result = process_video(path, root=APP_ROOT, output_root=output_root, log_callback=log)
            results.append(result)
        except Exception as exc:
            log(f"处理失败：{path.name} - {exc}")

    if len(results) >= 2:
        integrate_batch_results(results, output_root=output_root, log=log)
    else:
        log("成功文件少于 2 个，跳过批量内容整合。")

    log("批量试跑结束。")


if __name__ == "__main__":
    main()
