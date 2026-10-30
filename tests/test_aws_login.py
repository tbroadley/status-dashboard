import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from status_dashboard import aws_login


class SsoLoginTests(unittest.IsolatedAsyncioTestCase):
    def fake_aws(self, script: str) -> str:
        directory = tempfile.mkdtemp()
        path = Path(directory) / "aws"
        _ = path.write_text(f"#!/bin/sh\n{script}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return directory

    async def test_runs_aws_sso_login(self):
        directory = self.fake_aws('[ "$1 $2" = "sso login" ] || exit 3')
        with patch.dict(os.environ, {"PATH": directory}):
            self.assertTrue(await aws_login.sso_login())

    async def test_failure_returns_false(self):
        directory = self.fake_aws("exit 1")
        with patch.dict(os.environ, {"PATH": directory}):
            self.assertFalse(await aws_login.sso_login())

    async def test_missing_cli_returns_false(self):
        with patch.dict(os.environ, {"PATH": tempfile.mkdtemp()}):
            self.assertFalse(await aws_login.sso_login())

    async def test_timeout_kills_the_login(self):
        directory = self.fake_aws("exec sleep 30")
        with (
            patch.dict(os.environ, {"PATH": directory + os.pathsep + "/bin"}),
            patch.object(aws_login, "LOGIN_TIMEOUT_SECONDS", 0.1),
        ):
            self.assertFalse(await asyncio.wait_for(aws_login.sso_login(), 5))

    def test_enabled_by_default(self):
        for value, expected in (
            (None, True),
            ("false", False),
            ("0", False),
            ("1", True),
        ):
            env = {} if value is None else {"AWS_AUTO_SSO_LOGIN": value}
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                self.assertEqual(aws_login.enabled(), expected)
