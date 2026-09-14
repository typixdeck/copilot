import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Copilot · TypixDeck 板载协处理器固件管理")
    parser.add_argument("--preview", action="store_true", help="启动本机模拟模式")
    parser.add_argument("--fullscreen", action="store_true", help="全屏显示")
    args = parser.parse_args()
    try:
        if args.preview:
            from .app import CopilotApplication
        else:
            from .live_app import LiveCopilotApplication as CopilotApplication
    except (ImportError, ValueError) as exc:
        print(f"GTK3 环境不可用：{exc}\n请检查 GTK3 与 PyGObject 安装。", file=sys.stderr)
        return 1
    return CopilotApplication(fullscreen=args.fullscreen).run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
