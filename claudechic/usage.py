"""Claude API usage fetching from OAuth endpoint."""

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class UsageLimit:
    """A single usage limit with utilization percentage and reset time."""

    utilization: float  # 0-100
    resets_at: datetime | None


@dataclass
class UsageInfo:
    """Usage information from the API."""

    five_hour: UsageLimit | None
    seven_day: UsageLimit | None
    seven_day_sonnet: UsageLimit | None
    error: str | None = None


def get_oauth_token() -> str | None:
    """Get OAuth access token from system credential store.

    Supports macOS Keychain and Linux credentials file.
    """
    if sys.platform == "darwin":
        return _get_oauth_token_macos()
    else:
        # Linux and other platforms use credentials file
        return _get_oauth_token_file()


def _get_oauth_token_macos() -> str | None:
    """Get OAuth token from macOS Keychain."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        creds = json.loads(result.stdout)
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def _get_oauth_token_file() -> str | None:
    """Get OAuth token from credentials file (Linux/other platforms)."""
    try:
        creds_path = Path.home() / ".claude" / ".credentials.json"
        if not creds_path.exists():
            return None
        creds = json.loads(creds_path.read_text())
        return creds.get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def parse_reset_time(iso_str: str | None) -> datetime | None:
    """Parse ISO timestamp to datetime."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


async def fetch_usage() -> UsageInfo:
    """Fetch usage data from Anthropic API.

    Returns UsageInfo with error field set if fetch fails.
    """
    import asyncio

    token = get_oauth_token()
    if not token:
        if sys.platform == "darwin":
            return UsageInfo(None, None, None, error="No OAuth token found in keychain")
        else:
            return UsageInfo(
                None,
                None,
                None,
                error="No OAuth token found in ~/.claude/.credentials.json",
            )

    # Run curl in subprocess to avoid adding httpx/aiohttp dependency
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            "https://api.anthropic.com/api/oauth/usage",
            "-H",
            "Accept: application/json",
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "anthropic-beta: oauth-2025-04-20",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            return UsageInfo(None, None, None, error="API request failed")

        data = json.loads(stdout.decode())

        def parse_limit(key: str) -> UsageLimit | None:
            limit = data.get(key)
            if not limit or limit.get("utilization") is None:
                return None
            return UsageLimit(
                utilization=limit["utilization"],
                resets_at=parse_reset_time(limit.get("resets_at")),
            )

        return UsageInfo(
            five_hour=parse_limit("five_hour"),
            seven_day=parse_limit("seven_day"),
            seven_day_sonnet=parse_limit("seven_day_sonnet"),
        )
    except json.JSONDecodeError:
        return UsageInfo(None, None, None, error="Invalid API response")
    except Exception as e:
        return UsageInfo(None, None, None, error=str(e))


def format_reset_time(dt: datetime | None) -> str:
    """Format reset time for display."""
    if not dt:
        return ""

    # Convert to local time
    local_dt = dt.astimezone()
    now = datetime.now().astimezone()

    # Format hour without leading zero (cross-platform)
    hour = local_dt.hour % 12 or 12
    ampm = "am" if local_dt.hour < 12 else "pm"
    if local_dt.minute:
        time_str = f"{hour}:{local_dt.minute:02d}{ampm}"
    else:
        time_str = f"{hour}{ampm}"

    # If same day, just show time
    if local_dt.date() == now.date():
        return f"Resets {time_str}"
    else:
        return f"Resets {local_dt.strftime('%b')} {local_dt.day} at {time_str}"
