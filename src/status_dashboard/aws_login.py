"""Automatic `aws sso login` when task storage reports expired AWS credentials."""

import asyncio
import contextlib
import logging
import os
import shutil

logger = logging.getLogger(__name__)

# `aws sso login` waits for the user to approve the sign-in in the browser.
LOGIN_TIMEOUT_SECONDS = 300
# After a sign-in that failed or was ignored, wait this long before opening
# the browser again, so a dashboard left alone doesn't keep opening tabs.
RETRY_AFTER_SECONDS = 15 * 60


def enabled() -> bool:
    """Whether to run `aws sso login` automatically (`AWS_AUTO_SSO_LOGIN`, default on)."""
    raw = os.environ.get("AWS_AUTO_SSO_LOGIN", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


async def sso_login() -> bool:
    """Run `aws sso login` for the current profile; return whether it succeeded.

    It opens the sign-in page in the browser and returns once the user
    approves it. Output is discarded; failures are logged, never raised.
    Cancelling the call (e.g. quitting the app) stops the login.
    """
    aws = shutil.which("aws")
    if aws is None:
        logger.warning("AWS sign-in required, but the aws CLI is not installed")
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            aws,
            "sso",
            "login",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as error:
        logger.warning("Could not run aws sso login: %s", type(error).__name__)
        return False
    try:
        returncode = await asyncio.wait_for(process.wait(), LOGIN_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("aws sso login was not approved in time")
        return False
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    if returncode != 0:
        logger.warning("aws sso login failed with exit code %d", returncode)
        return False
    return True
