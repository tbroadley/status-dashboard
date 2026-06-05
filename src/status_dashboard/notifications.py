import json
import logging
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)


def send_desktop_notification(
    title: str, message: str, sound: str | None = None
) -> bool:
    """Send a desktop notification. Returns True on success, False otherwise.

    Prefers the `notify` helper script (handles both host and dev containers),
    falling back to `osascript` on macOS. Failures are logged, never raised.
    """
    notify_path = shutil.which("notify")
    if notify_path:
        cmd = [notify_path, "-t", title]
        if sound:
            cmd += ["-s", sound]
        cmd.append(message)
        return _run(cmd)

    if sys.platform == "darwin":
        script = (
            f"display notification {json.dumps(message)} with title {json.dumps(title)}"
        )
        if sound:
            script += f" sound name {json.dumps(sound)}"
        return _run(["/usr/bin/osascript", "-e", script])

    logger.warning("No desktop notification mechanism available on this platform")
    return False


def _run(cmd: list[str]) -> bool:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            logger.warning("Notification command failed: %s", result.stderr.strip())
            return False
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("Failed to send desktop notification: %s", e)
        return False
