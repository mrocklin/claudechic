"""Entry point for claudechic CLI."""

import argparse
import os
from importlib.metadata import version

from claudechic.app import ChatApp
from claudechic.errors import setup_logging

# Set up file logging to ~/claudechic.log
setup_logging()


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
        "--remote-port",
        type=int,
        default=int(os.environ.get("CLAUDECHIC_REMOTE_PORT", "0")),
        help="Start HTTP server for remote control on this port",
    )
    parser.add_argument("prompt", nargs="*", help="Initial prompt to send")
    args = parser.parse_args()

    initial_prompt = " ".join(args.prompt) if args.prompt else None

    # Pass resume flag or specific session ID - actual lookup happens in app
    resume_id = (
        args.session if args.session else ("__most_recent__" if args.resume else None)
    )

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
        raise


if __name__ == "__main__":
    main()
