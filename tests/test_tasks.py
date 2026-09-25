import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from status_dashboard.clients import tasks
from status_dashboard.task_store import StoreError, decode_document, encode_document
from status_dashboard.task_store_cli import csv_rows
from tests.test_task_store import FakeS3, row


class TestTasks(unittest.TestCase):
    today: dt.date = dt.date.today()
    fake: FakeS3 = FakeS3([])

    @override
    def setUp(self):
        self.today = dt.date.today()
        self.fake = FakeS3([])
        env = patch.dict(
            os.environ,
            {"TASKS_S3_URI": "s3://example-bucket/tasks.json", "TASKS_AWS_CLI": "aws"},
        )
        _ = cast(object, self.enterContext(env))
        runner = patch(
            "status_dashboard.task_store.subprocess.run", side_effect=self.fake.run
        )
        _ = runner.start()
        self.addCleanup(runner.stop)

    def test_due_filter_order_and_completed_history(self):
        for name, due in [
            ("Today", "today"),
            ("Tomorrow", "tomorrow"),
            ("Overdue", (self.today - dt.timedelta(days=1)).isoformat()),
            ("Done", "today"),
        ]:
            task_id = tasks.create_task(name, due)
            self.assertIsNotNone(task_id)
        initial = tasks.get_today_tasks()
        overdue = next(task for task in initial if task.content == "Overdue")
        done = next(task for task in initial if task.content == "Done")
        self.assertTrue(tasks.update_day_orders({overdue.id: -1}))
        self.assertTrue(tasks.complete_task(done.id))
        self.assertEqual(
            [t.content for t in tasks.get_today_tasks()], ["Overdue", "Today"]
        )
        self.assertEqual(
            [
                t.content
                for t in tasks.get_tasks_for_date(self.today + dt.timedelta(days=1))
            ],
            ["Tomorrow"],
        )
        completed = next(
            r
            for r in decode_document(self.fake.objects["tasks.json"])
            if r[0] == done.id
        )
        self.assertEqual(completed[7], "TRUE")
        self.assertTrue(completed[8].startswith(self.today.isoformat()))
        self.assertTrue(tasks.reopen_task(done.id))
        self.assertEqual(len(tasks.get_today_tasks()), 3)

    def test_edit_defer_delete_and_projects(self):
        task_id = tasks.create_task("Task", "today", "description")
        assert task_id is not None
        self.assertTrue(
            tasks.update_task(
                task_id,
                content="Edited",
                project_id="Work",
                description="new",
                due_string="tomorrow",
            )
        )
        payload = tasks.get_task(task_id)
        assert payload is not None
        self.assertEqual(payload["content"], "Edited")
        self.assertEqual(payload["description"], "new")
        self.assertEqual(tasks.get_projects(), [tasks.Project("Work", "Work")])
        self.assertTrue(tasks.reschedule_to_today(task_id))
        self.assertEqual(len(tasks.get_today_tasks()), 1)
        self.assertTrue(tasks.defer_task(task_id))
        self.assertEqual(tasks.get_today_tasks(), [])
        self.assertTrue(tasks.set_due_date(task_id, None))
        unscheduled = tasks.get_task(task_id)
        assert unscheduled is not None
        self.assertIsNone(unscheduled["due"])
        self.assertTrue(tasks.delete_task(task_id))
        self.assertFalse(tasks.delete_task(task_id))
        self.assertIsNone(tasks.get_task(task_id))

    def test_create_with_id_and_orders_matches_preview(self):
        existing = tasks.create_task("Existing")
        assert existing is not None
        now = dt.datetime.combine(self.today, dt.time(9))
        preview = tasks.new_task(
            "new-id", "New", "today 5pm", "notes", day_order=0, now=now
        )
        created = tasks.create_task(
            "New",
            "today 5pm",
            "notes",
            task_id="new-id",
            day_orders={"new-id": 0, existing: 1},
            now=now,
        )
        self.assertEqual(created, "new-id")
        stored = tasks.get_today_tasks()
        self.assertEqual([t.id for t in stored], ["new-id", existing])
        self.assertEqual(stored[0], preview)
        # A retried create (e.g. after a lost response) doesn't duplicate the row.
        self.assertEqual(tasks.create_task("New", task_id="new-id"), "new-id")
        self.assertEqual(len(tasks.get_today_tasks()), 2)

    def test_set_due_date_restores_stored_timestamp(self):
        task_id = tasks.create_task("Timed", "today 3pm")
        assert task_id is not None
        payload = tasks.get_task(task_id)
        assert payload is not None
        original = cast(dict[str, object], payload["due"])["date"]
        assert isinstance(original, str)
        self.assertTrue(tasks.defer_task(task_id))
        self.assertTrue(tasks.set_due_date(task_id, original))
        restored = tasks.get_task(task_id)
        assert restored is not None
        self.assertEqual(cast(dict[str, object], restored["due"])["date"], original)

    def test_recurring_completion_advances_without_done(self):
        task_id = tasks.create_task("Recurring", "every day at 10am")
        assert task_id is not None
        before = decode_document(self.fake.objects["tasks.json"])[0]
        self.assertTrue(tasks.complete_task(task_id))
        after = decode_document(self.fake.objects["tasks.json"])[0]
        self.assertGreater(after[4], before[4])
        self.assertEqual(after[5], "every day at 10am")
        self.assertEqual(after[7:], ["FALSE", ""])
        self.assertTrue(tasks.reschedule_to_today(task_id, True, "every day at 10am"))
        self.assertEqual(tasks.get_today_tasks()[0].due_time, "10:00")

    def test_mutation_reresolves_id_after_concurrent_delete(self):
        first = tasks.create_task("First")
        second = tasks.create_task("Second")
        assert first and second

        def delete_first():
            self.fake.objects["tasks.json"] = encode_document(
                [row(second, "Second", self.today.isoformat())]
            )
            self.fake.before_write = None

        self.fake.before_write = delete_first
        self.assertTrue(tasks.update_task(second, content="Updated"))
        self.assertEqual([t.content for t in tasks.get_today_tasks()], ["Updated"])

    def test_failure_is_not_empty_list(self):
        self.fake.fail = "ExpiredToken"
        with self.assertRaises(StoreError):
            _ = tasks.get_today_tasks()
        with self.assertRaises(StoreError):
            _ = tasks.create_task("Do not lose this")

    def test_csv_import_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tasks.csv"
            _ = path.write_text(
                'id,content,project,description,due,recurrence,order,done,completed_at\na,"A, B",,,2026-09-15,,0,FALSE,\n'
            )
            self.assertEqual(csv_rows(path)[0][1], "A, B")
            _ = path.write_text("wrong,header\n")
            with self.assertRaises(StoreError):
                _ = csv_rows(path)
