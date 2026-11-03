from video_to_gpt.processor import tool_status


if __name__ == "__main__":
    print("本地依赖检查")
    print("=" * 30)
    print("\n".join(tool_status()))
    print("=" * 30)
    print("如果仍显示未找到，请确认文件放置位置是否正确。")
