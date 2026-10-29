import unittest
from typing import override
from unittest.mock import patch

import httpx

from status_dashboard.clients import linear


def _response(payload: object) -> httpx.Response:
    return httpx.Response(
        200, json=payload, request=httpx.Request("POST", "https://linear")
    )


STATES = {
    "data": {
        "workflowStates": {
            "nodes": [{"id": "s-done", "name": "Done", "type": "completed"}]
        }
    }
}
UPDATED = {"data": {"issueUpdate": {"success": True}}}
VIEWER = {"data": {"viewer": {"id": "me"}}}


class FakeClient:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses: list[httpx.Response | Exception] = list(responses)
        self.calls: int = 0

    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class TestLinearRequests(unittest.TestCase):
    @override
    def setUp(self):
        linear._cache.clear()  # pyright: ignore[reportPrivateUsage]
        self.addCleanup(linear._cache.clear)  # pyright: ignore[reportPrivateUsage]

    def test_state_changes_look_up_workflow_states_once_per_team(self):
        client = FakeClient(_response(STATES), _response(UPDATED), _response(UPDATED))
        with patch.object(linear, "_http", return_value=client):
            self.assertTrue(linear.set_issue_state("i1", "team", "done", "key"))
            self.assertTrue(linear.set_issue_state("i2", "team", "done", "key"))
        self.assertEqual(client.calls, 3)

    def test_viewer_is_fetched_once_but_failures_are_retried(self):
        client = FakeClient(httpx.ConnectError("offline"), _response(VIEWER))
        with patch.object(linear, "_http", return_value=client):
            self.assertIsNone(linear.get_viewer_id("key"))
            self.assertEqual(linear.get_viewer_id("key"), "me")
            self.assertEqual(linear.get_viewer_id("key"), "me")
        self.assertEqual(client.calls, 2)

    def test_requests_share_one_client(self):
        self.assertIs(linear._http(), linear._http())  # pyright: ignore[reportPrivateUsage]
