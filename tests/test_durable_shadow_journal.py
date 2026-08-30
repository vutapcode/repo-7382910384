import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loi_he_thong import durable_shadow_journal


class DurableShadowJournalTest(unittest.TestCase):
    def _shadow(self):
        calls = []

        def append(event, payload):
            calls.append((event, payload))
            return "ok"

        return SimpleNamespace(
            _append_event=append,
            EVENT_PATH=Path("/tmp/smc-events.jsonl"),
            calls=calls,
        )

    def test_only_entry_exit_are_fsynced(self):
        shadow = self._shadow()
        durable_shadow_journal.install(shadow)

        with patch.object(
            durable_shadow_journal.journal_segments, "prepare_append"
        ) as rotate, patch.object(
            durable_shadow_journal, "_fsync_path_and_parent"
        ) as sync:
            shadow._append_event("ENTRY_SKIPPED", {"x": 1})
            rotate.assert_called_once_with(shadow.EVENT_PATH)
            sync.assert_not_called()

            shadow._append_event("ENTRY", {"x": 2})
            sync.assert_called_once_with(shadow.EVENT_PATH)

            sync.reset_mock()
            shadow._append_event("EXIT", {"x": 3})
            sync.assert_called_once_with(shadow.EVENT_PATH)

    def test_sync_failure_is_not_swallowed(self):
        shadow = self._shadow()
        durable_shadow_journal.install(shadow)

        with patch.object(
            durable_shadow_journal.journal_segments, "prepare_append"
        ), patch.object(
            durable_shadow_journal,
            "_fsync_path_and_parent",
            side_effect=OSError("disk sync failed"),
        ):
            with self.assertRaises(OSError):
                shadow._append_event("ENTRY", {})


if __name__ == "__main__":
    unittest.main()
