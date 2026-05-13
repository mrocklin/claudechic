"""Entry point for claudechic CLI."""

import argparse
import os
import sys
from importlib.metadata import version

from claudechic.app import ChatApp
from claudechic.errors import setup_logging

# Set up file logging to ~/claudechic.log
setup_logging()


def positive_int(value: str) -> int:
    """Argparse type for positive integers."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}")
    if n <= 0:
        raise argparse.ArgumentTypeError(f"must be positive, got {n}")
    return n


def main():
    parser = argparse.ArgumentParser(description="Claude Chic")
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"claudechic {version('claudechic')}",
    )
    parser.add_argument(
        "--resume", "-r", action="store_true", help="Resume the most recent session"
    )
    parser.add_argument(
        "--session", "-s", type=str, help="Resume a specific session ID"
    )
    parser.add_argument(
        "--theme",
        "-t",
        nargs="?",
        const="__list__",
        default=None,
        help="Use a specific theme for this session, or pass without a value to list available themes",
    )
    parser.add_argument(
        "--remote-port",
        type=int,
        default=int(os.environ.get("CLAUDECHIC_REMOTE_PORT", "0")),
        help="Start HTTP server for remote control on this port",
    )
    parser.add_argument(
        "--dangerously-skip-permissions",
        "--yolo",
        action="store_true",
        help="Auto-approve all tool uses without prompting (use in sandboxed environments)",
    )
    parser.add_argument(
        "--width",
        "-w",
        type=positive_int,
        default=None,
        help="Set terminal width for chat column",
    )
    parser.add_argument("prompt", nargs="*", help="Initial prompt to send")
    args = parser.parse_args()

    initial_prompt = " ".join(args.prompt) if args.prompt else None

    # Pass resume flag or specific session ID - actual lookup happens in app
    resume_id = (
        args.session if args.session else ("__most_recent__" if args.resume else None)
    )

    # Validate theme name or list available themes
    if args.theme:
        from claudechic.theme import get_available_theme_names

        available = get_available_theme_names()
        if args.theme == "__list__":
            print("Available themes:")
            for name in sorted(available):
                print(f"  {name}")
            sys.exit(0)
        if args.theme not in available:
            print(f"Unknown theme '{args.theme}'. Available themes:")
            for name in sorted(available):
                print(f"  {name}")
            sys.exit(1)

    # Set terminal window title (before Textual takes over stdout)
    from pathlib import Path
    from rich.console import Console
    from rich.control import Control

    Console().control(Control.title(f"Claude Chic · {Path.cwd().name}"))

    try:
        app = ChatApp(
            resume_session_id=resume_id,
            initial_prompt=initial_prompt,
            remote_port=args.remote_port,
            skip_permissions=args.dangerously_skip_permissions,
            theme_override=args.theme,
            width=args.width,
        )
        app.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception:
        import tempfile
        import traceback

        crash_log = Path(tempfile.gettempdir()) / "claudechic-crash.log"
        with open(crash_log, "w", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        # Print standard traceback (not rich's fancy one) and exit
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Windows: suppress stderr during interpreter shutdown to silence asyncio
        # transport __del__ exceptions (ValueError from closed pipes). See issue #31.
        # This must happen after app.run() returns but before Python garbage collects.
        if sys.platform == "win32":
            sys.stderr = open(os.devnull, "w")


if __name__ == "__main__":
    main()
