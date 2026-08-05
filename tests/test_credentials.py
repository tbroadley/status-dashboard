import contextlib
import json
import os
import unittest
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

from status_dashboard import credentials

ITEM = "00000000-0000-4000-8000-000000000000"


def _item(
    field_value: str | None = None,
    field_name: str = "LINEAR_API_KEY",
    password: str | None = None,
) -> dict[str, object]:
    """A Bitwarden item as `bw get item` returns it."""
    item: dict[str, object] = {"id": ITEM, "name": "shell env"}
    if field_value is not None:
        item["fields"] = [{"name": field_name, "value": field_value}]
    if password is not None:
        item["login"] = {"password": password}
    return item


@contextlib.contextmanager
def _env(**values: str | None) -> Iterator[None]:
    """Set env vars for the duration of the block; None removes a var."""
    originals = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            _ = os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in originals.items():
            if value is None:
                _ = os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _bw(
    status: str = "unlocked",
    item_json: dict[str, object] | None = None,
    unlock_session: str | None = "new-session",
) -> Iterator[list[list[str]]]:
    """Patch subprocess so `bw` responds without touching the real vault."""
    if item_json is None:
        item_json = _item(field_value="secret-key")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> MagicMock:
        del kwargs
        commands.append(command)
        if command[:2] == ["bw", "status"]:
            return MagicMock(
                returncode=0, stdout=json.dumps({"status": status}), stderr=""
            )
        if command[:3] == ["bw", "get", "item"]:
            if item_json is None:
                return MagicMock(returncode=1, stdout="", stderr="Not found.")
            return MagicMock(returncode=0, stdout=json.dumps(item_json), stderr="")
        if command[:2] == ["bw", "unlock"]:
            return MagicMock(
                returncode=0, stdout=(unlock_session or "") + "\n", stderr=""
            )
        return MagicMock(returncode=1, stdout="", stderr="unexpected")

    with (
        patch("status_dashboard.credentials.subprocess.run", side_effect=fake_run),
        patch("status_dashboard.credentials.sys.stdin.isatty", return_value=True),
    ):
        yield commands


class LoadIntoEnv(unittest.TestCase):
    def test_existing_env_var_is_left_alone(self):
        with _env(LINEAR_API_KEY="already-set", LINEAR_BW_ITEM=ITEM), _bw() as commands:
            credentials.load_into_env()
            self.assertEqual(os.environ["LINEAR_API_KEY"], "already-set")
        self.assertEqual(commands, [])

    def test_fetches_from_bitwarden_when_unset(self):
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM), _bw() as commands:
            credentials.load_into_env()
            self.assertEqual(os.environ["LINEAR_API_KEY"], "secret-key")

        self.assertIn(["bw", "get", "item", ITEM], commands)

    def test_does_nothing_without_an_item_name(self):
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=None), _bw() as commands:
            credentials.load_into_env()
            self.assertIsNone(os.environ.get("LINEAR_API_KEY"))
        self.assertEqual(commands, [])

    def test_locked_vault_triggers_unlock_then_fetch(self):
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM, BW_SESSION=None):
            with _bw(status="locked") as commands:
                credentials.load_into_env()
                self.assertEqual(os.environ["LINEAR_API_KEY"], "secret-key")
                self.assertEqual(os.environ["BW_SESSION"], "new-session")

        issued = [c[1] for c in commands]
        self.assertEqual(issued, ["status", "unlock", "get"])

    def test_aborted_unlock_leaves_secret_unset(self):
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM, BW_SESSION=None):
            with _bw(status="locked", unlock_session=None):
                credentials.load_into_env()
                self.assertIsNone(os.environ.get("LINEAR_API_KEY"))

    def test_unauthenticated_vault_does_not_prompt(self):
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM):
            with _bw(status="unauthenticated") as commands:
                credentials.load_into_env()
                self.assertIsNone(os.environ.get("LINEAR_API_KEY"))

        self.assertEqual([c[1] for c in commands], ["status"])

    def test_missing_item_leaves_secret_unset(self):
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM), _bw(item_json=_item()):
            credentials.load_into_env()
            self.assertIsNone(os.environ.get("LINEAR_API_KEY"))

    def test_missing_bw_cli_is_not_fatal(self):
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM):
            with patch(
                "status_dashboard.credentials.subprocess.run",
                side_effect=FileNotFoundError,
            ):
                credentials.load_into_env()
                self.assertIsNone(os.environ.get("LINEAR_API_KEY"))

    def test_existing_session_is_reused_without_unlocking(self):
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM, BW_SESSION="existing"):
            with _bw() as commands:
                credentials.load_into_env()

        self.assertNotIn("unlock", [c[1] for c in commands])


class VaultStatus(unittest.TestCase):
    def test_reports_status(self):
        with _bw(status="locked"):
            self.assertEqual(credentials.vault_status(), "locked")

    def test_unparseable_output_is_unknown(self):
        with patch(
            "status_dashboard.credentials.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="not json", stderr=""),
        ):
            self.assertEqual(credentials.vault_status(), "unknown")


class Unlock(unittest.TestCase):
    def test_returns_session_key(self):
        with _bw(status="locked"):
            self.assertEqual(credentials.unlock(), "new-session")

    def test_empty_output_is_treated_as_failure(self):
        """`bw unlock` exits 0 even when the prompt is aborted."""
        with _bw(status="locked", unlock_session=None):
            self.assertIsNone(credentials.unlock())

    def test_no_tty_does_not_prompt(self):
        with (
            patch("status_dashboard.credentials.sys.stdin.isatty", return_value=False),
            patch("status_dashboard.credentials.subprocess.run") as run,
        ):
            self.assertIsNone(credentials.unlock())
            run.assert_not_called()

    def test_stdout_is_captured_but_stderr_is_not(self):
        """The prompt goes to stderr; capturing it would hide it from the user."""
        with _bw(status="locked"):
            with patch(
                "status_dashboard.credentials.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="sess\n"),
            ) as run:
                _ = credentials.unlock()

        kwargs = run.call_args.kwargs
        self.assertNotIn("stderr", kwargs)
        self.assertIn("stdout", kwargs)


class GetSecret(unittest.TestCase):
    def test_reads_named_custom_field(self):
        with _bw(item_json=_item(field_value="from-field")):
            self.assertEqual(
                credentials.get_secret(ITEM, "LINEAR_API_KEY"), "from-field"
            )

    def test_falls_back_to_password_when_no_field_requested(self):
        with _bw(item_json=_item(password="from-password")):
            self.assertEqual(credentials.get_secret(ITEM), "from-password")

    def test_missing_field_returns_none(self):
        with _bw(item_json=_item(field_value="x", field_name="OTHER_KEY")):
            self.assertIsNone(credentials.get_secret(ITEM, "LINEAR_API_KEY"))

    def test_empty_field_returns_none(self):
        with _bw(item_json=_item(field_value="   ")):
            self.assertIsNone(credentials.get_secret(ITEM, "LINEAR_API_KEY"))

    def test_field_takes_precedence_over_password(self):
        with _bw(item_json=_item(field_value="field", password="password")):
            self.assertEqual(credentials.get_secret(ITEM, "LINEAR_API_KEY"), "field")

    def test_load_falls_back_to_password_when_field_absent(self):
        item = _item(password="pw-only")
        with _env(LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM), _bw(item_json=item):
            credentials.load_into_env()
            self.assertEqual(os.environ["LINEAR_API_KEY"], "pw-only")

    def test_field_name_can_be_overridden(self):
        item = _item(field_value="custom", field_name="LINEAR_TOKEN")
        with (
            _env(
                LINEAR_API_KEY=None, LINEAR_BW_ITEM=ITEM, LINEAR_BW_FIELD="LINEAR_TOKEN"
            ),
            _bw(item_json=item),
        ):
            credentials.load_into_env()
            self.assertEqual(os.environ["LINEAR_API_KEY"], "custom")


if __name__ == "__main__":
    _ = unittest.main()
