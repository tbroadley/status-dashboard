import subprocess
import unittest
from typing import cast
from unittest.mock import patch

from status_dashboard import notifications


def _completed(
    returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stderr=stderr)


class SendDesktopNotificationTests(unittest.TestCase):
    def test_uses_notify_script_when_available(self) -> None:
        with (
            patch("shutil.which", return_value="/bin/notify"),
            patch("subprocess.run", return_value=_completed()) as run,
        ):
            result = notifications.send_desktop_notification("Title", "Body", "Glass")

        self.assertTrue(result)
        run.assert_called_once_with(
            ["/bin/notify", "-t", "Title", "-s", "Glass", "Body"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_omits_sound_flag_when_no_sound(self) -> None:
        with (
            patch("shutil.which", return_value="/bin/notify"),
            patch("subprocess.run", return_value=_completed()) as run,
        ):
            result = notifications.send_desktop_notification("Title", "Body")

        self.assertTrue(result)
        run.assert_called_once_with(
            ["/bin/notify", "-t", "Title", "Body"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_falls_back_to_osascript_on_macos(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch("sys.platform", "darwin"),
            patch("subprocess.run", return_value=_completed()) as run,
        ):
            result = notifications.send_desktop_notification("Title", "Body", "Glass")

        self.assertTrue(result)
        args = cast(list[str], run.call_args.args[0])
        self.assertEqual(args[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn('display notification "Body"', args[2])
        self.assertIn('with title "Title"', args[2])
        self.assertIn('sound name "Glass"', args[2])

    def test_returns_false_when_no_mechanism(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch("sys.platform", "linux"),
        ):
            self.assertFalse(notifications.send_desktop_notification("Title", "Body"))

    def test_returns_false_on_nonzero_exit(self) -> None:
        with (
            patch("shutil.which", return_value="/bin/notify"),
            patch(
                "subprocess.run", return_value=_completed(returncode=1, stderr="boom")
            ),
        ):
            self.assertFalse(notifications.send_desktop_notification("Title", "Body"))

    def test_returns_false_on_timeout(self) -> None:
        with (
            patch("shutil.which", return_value="/bin/notify"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="notify", timeout=10),
            ),
        ):
            self.assertFalse(notifications.send_desktop_notification("Title", "Body"))


if __name__ == "__main__":
    _ = unittest.main()
