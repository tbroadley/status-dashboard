"""Fetch secrets from Bitwarden at startup, so none need to live on disk.

Resolution order for each secret:

1. An already-set environment variable wins, so `.env`, CI, and one-off
   overrides keep working unchanged.
2. Otherwise `bw get item <item>`, reading a named custom field (defaulting to
   the secret's own name) and falling back to the item's password, unlocking
   the vault first if needed.

Called from `main()` before the Textual app starts, while the terminal is
still free for `bw`'s password prompt. `bw unlock --raw` writes the prompt to
stderr and the session key to stdout, so stdout is captured while stdin and
stderr stay attached to the terminal.

A locked vault or a missing item is never fatal: the secret stays unset and the
affected panel degrades, matching how the API clients handle failure.
"""

import json
import logging
import os
import subprocess
import sys
from typing import TypeAlias, cast

logger = logging.getLogger(__name__)

JsonDict: TypeAlias = dict[str, object]

GET_TIMEOUT = 30  # seconds
UNLOCK_TIMEOUT = 300  # seconds; a human is typing

# secret env var -> (env var naming the vault item, env var naming the field).
# When the field var is unset, the item's own name is used as the field name,
# then the item's password as a last resort.
MANAGED_SECRETS = {
    "LINEAR_API_KEY": ("LINEAR_BW_ITEM", "LINEAR_BW_FIELD"),
}


def _bw(args: list[str], session: str | None = None) -> str | None:
    """Run a non-interactive `bw` command and return stdout."""
    env = {**os.environ, "BW_SESSION": session} if session else None
    try:
        result = subprocess.run(
            ["bw", *args],
            capture_output=True,
            text=True,
            timeout=GET_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.error("bw %s timed out", args[0])
        return None
    except FileNotFoundError:
        logger.warning("bw CLI not found; cannot fetch secrets from Bitwarden")
        return None

    if result.returncode != 0:
        logger.warning("bw %s failed: %s", args[0], result.stderr.strip()[:200])
        return None
    return result.stdout.strip()


def vault_status(session: str | None = None) -> str:
    """One of 'unlocked', 'locked', 'unauthenticated', or 'unknown'."""
    output = _bw(["status"], session)
    if not output:
        return "unknown"
    try:
        data = cast(JsonDict, json.loads(output))
    except json.JSONDecodeError:
        return "unknown"
    return str(data.get("status", "unknown"))


def unlock() -> str | None:
    """Prompt for the master password and return a session key.

    stdin and stderr stay attached to the terminal so the user sees and answers
    the prompt; only stdout is captured, which is where `--raw` writes the key.
    """
    if not sys.stdin.isatty():
        logger.warning("Bitwarden vault is locked and no terminal is available")
        return None

    print("Bitwarden vault is locked.", file=sys.stderr)
    try:
        result = subprocess.run(
            ["bw", "unlock", "--raw"],
            stdout=subprocess.PIPE,
            text=True,
            timeout=UNLOCK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print("Timed out waiting for the master password.", file=sys.stderr)
        return None
    except FileNotFoundError:
        logger.warning("bw CLI not found; cannot unlock Bitwarden")
        return None

    # `bw unlock` exits 0 even when the prompt is aborted, so check the output.
    session = result.stdout.strip()
    if not session:
        print("Unlock failed; continuing without Bitwarden secrets.", file=sys.stderr)
        return None
    return session


def get_secret(
    item: str, field: str | None = None, session: str | None = None
) -> str | None:
    """Read a secret out of a vault item.

    With `field`, reads that custom field — secrets are often stored as named
    fields on a shared "shell env" note rather than as an item's password.
    Without it, falls back to the item's login password.
    """
    output = _bw(["get", "item", item], session)
    if not output:
        logger.warning("Could not read Bitwarden item %r", item)
        return None

    try:
        data = cast(JsonDict, json.loads(output))
    except json.JSONDecodeError:
        logger.error("Bitwarden returned unparseable JSON for item %r", item)
        return None

    if field:
        entries = cast(list[JsonDict], data.get("fields") or [])
        for entry in entries:
            if entry.get("name") == field:
                value = cast(str, entry.get("value") or "").strip()
                if value:
                    return value
                logger.warning("Bitwarden field %r on item %r is empty", field, item)
                return None
        logger.warning(
            "No field %r on Bitwarden item %r (has: %s)",
            field,
            item,
            [entry.get("name") for entry in entries],
        )
        return None

    login = cast(JsonDict, data.get("login") or {})
    password = cast(str, login.get("password") or "").strip()
    if not password:
        logger.warning("No password on Bitwarden item %r", item)
        return None
    return password


def load_into_env() -> None:
    """Populate any managed secrets that aren't already in the environment.

    Unlocks the vault at most once, and only if something actually needs it.
    """
    # Default the field name to the secret's own name: a "shell env" note
    # typically holds a field literally called LINEAR_API_KEY.
    wanted = {
        secret: (os.environ[item_var], os.environ.get(field_var) or secret)
        for secret, (item_var, field_var) in MANAGED_SECRETS.items()
        if not os.environ.get(secret) and os.environ.get(item_var)
    }
    for secret, (item_var, _) in MANAGED_SECRETS.items():
        if not os.environ.get(secret) and not os.environ.get(item_var):
            logger.warning(
                "%s is unset and %s does not name a Bitwarden item", secret, item_var
            )

    if not wanted:
        return

    session = os.environ.get("BW_SESSION") or None
    status = vault_status(session)

    if status == "unauthenticated":
        print(
            "Bitwarden is not logged in; run `bw login`. Continuing without it.",
            file=sys.stderr,
        )
        return

    if status != "unlocked":
        session = unlock()
        if not session:
            return
        # Keep it for child processes and any later lookups this run.
        os.environ["BW_SESSION"] = session

    for secret, (item, field) in wanted.items():
        # Try the named field first, then the item's password.
        value = get_secret(item, field, session) or get_secret(item, None, session)
        if value:
            os.environ[secret] = value
            logger.info("Loaded %s from Bitwarden item %r", secret, item)
        else:
            print(
                f"Could not read {secret} from Bitwarden item {item!r}.",
                file=sys.stderr,
            )
