import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest import mock

import duplicate_file_finder as finder


class DuplicateFinderTests(unittest.TestCase):
    def make_pair(self, root: Path):
        first = root / "image.jpg"
        second = root / "image (1).jpg"
        first.write_bytes(b"identical fixture contents")
        second.write_bytes(first.read_bytes())
        return first, second

    def app_state(self, cache, cache_path):
        state = SimpleNamespace(
            cache=cache,
            cache_path=cache_path,
            groups=list(cache["groups"]),
            selected_paths=set(),
            deleted_during_scan=set(),
            latest_scan=None,
            cancel_event=threading.Event(),
            scan_thread=object(),
            status_text=mock.Mock(),
            cache_text=mock.Mock(),
            details_button=mock.Mock(),
            _render_tree=mock.Mock(),
            _finish_scan=mock.Mock(),
        )
        state._accept_scan_result = MethodType(finder.DuplicateFileFinderApp._accept_scan_result, state)
        return state

    def test_finds_groups_and_round_trips_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.txt").write_text("same", encoding="utf-8")
            nested = root / "nested"
            nested.mkdir()
            (nested / "copy.txt").write_text("same", encoding="utf-8")
            (root / "empty-a.bin").write_bytes(b"")
            (nested / "empty-b.bin").write_bytes(b"")
            (root / "unique.txt").write_text("unique", encoding="utf-8")

            result = finder.find_duplicate_groups([str(root)], threading.Event())
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(2, len(result["groups"]))
            self.assertEqual({2}, {len(group["paths"]) for group in result["groups"]})

            cache = root / "cache.json"
            finder.save_cache(result, cache)
            loaded = finder.load_cache(cache)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(2, len(loaded["groups"]))
            self.assertEqual(result["scan"], loaded["scan"])

    def test_safe_selection_preserves_one_copy_and_deletes_directly_for_test(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_text("same", encoding="utf-8")
            second.write_text("same", encoding="utf-8")
            result = finder.find_duplicate_groups([str(root)], threading.Event())
            assert result is not None
            selected = {str(first), str(second)}
            safe = finder.select_safe_paths(result["groups"], selected)
            self.assertEqual(1, len(safe))
            deleted, failures = finder.delete_paths(safe, recycle=False)
            self.assertEqual(safe, deleted)
            self.assertEqual([], failures)
            self.assertTrue(first.exists() or second.exists())
            self.assertFalse(first.exists() and second.exists())

    def test_invalid_cache_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache.json"
            cache.write_text(json.dumps({"version": 999, "groups": []}), encoding="utf-8")
            self.assertIsNone(finder.load_cache(cache))

    def test_cancellation_before_enumeration_never_returns_a_completed_scan(self):
        cancel = threading.Event()
        cancel.set()
        self.assertIsNone(finder.find_duplicate_groups(["unused"], cancel))

    def test_cancellation_at_enumeration_end_is_not_lost(self):
        cancel = threading.Event()

        def cancelled_walk(*_args, **_kwargs):
            cancel.set()
            yield from ()

        with mock.patch.object(finder, "iter_regular_files", cancelled_walk):
            self.assertIsNone(finder.find_duplicate_groups(["unused"], cancel))

    def test_cancellation_during_last_hash_does_not_publish_a_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            cancel = threading.Event()

            def progress(value):
                if value.hashed >= 2:
                    cancel.set()

            self.assertIsNone(finder.find_duplicate_groups([temporary], cancel, progress))

    def test_missing_root_is_reported_as_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = str(Path(temporary) / "missing")
            result = finder.find_duplicate_groups([missing])
            self.assertFalse(result["scan"]["complete"])
            self.assertEqual(1, result["scan"]["errors"])
            self.assertEqual(missing, result["scan"]["issues"][0]["path"])

    def test_denied_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            def denied_walk(root, **kwargs):
                kwargs["onerror"](PermissionError(13, "Access denied", root))
                yield from ()

            with mock.patch.object(finder.os, "walk", denied_walk):
                result = finder.find_duplicate_groups([temporary])
            self.assertFalse(result["scan"]["complete"])
            self.assertEqual(1, result["scan"]["errors"])

    def test_valid_root_still_finds_pairs_when_another_root_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            result = finder.find_duplicate_groups([temporary, str(Path(temporary) / "missing")])
            self.assertEqual(1, len(result["groups"]))
            self.assertEqual(2, result["scan"]["files_seen"])
            self.assertFalse(result["scan"]["complete"])

    def test_failed_hashes_are_not_reported_as_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            with mock.patch.object(finder, "hash_file", return_value=None):
                result = finder.find_duplicate_groups([temporary])
            self.assertEqual(2, result["scan"]["errors"])
            self.assertFalse(result["scan"]["complete"])

    def test_same_size_file_change_during_hashing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, _second = self.make_pair(Path(temporary))
            before = first.stat()
            after = SimpleNamespace(
                st_dev=before.st_dev,
                st_ino=before.st_ino,
                st_size=before.st_size,
                st_mtime_ns=before.st_mtime_ns + 1,
                st_ctime_ns=before.st_ctime_ns,
            )
            with mock.patch.object(finder.os, "fstat", side_effect=[before, after]):
                self.assertIsNone(finder.hash_file(str(first)))

    def test_windows_stat_and_fstat_creation_time_difference_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, _second = self.make_pair(Path(temporary))
            info = first.stat()
            handle_info = SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns,
                st_ctime_ns=info.st_ctime_ns + 100,
            )
            with mock.patch.object(finder.os, "fstat", return_value=handle_info):
                result = finder.hash_file(str(first))
            self.assertIsNotNone(result)
            self.assertEqual(info.st_size, result[1])

    def test_disappearing_duplicate_does_not_crash_group_assembly(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = self.make_pair(Path(temporary))
            original_hash = finder.hash_file
            hashed = []

            def remove_first_after_second_hash(path, cancel=None):
                result = original_hash(path, cancel)
                hashed.append(path)
                if len(hashed) == 2:
                    Path(hashed[0]).unlink()
                return result

            with mock.patch.object(finder, "hash_file", side_effect=remove_first_after_second_hash):
                result = finder.find_duplicate_groups([str(first), str(second)])
            self.assertIsNotNone(result)
            self.assertEqual([], result["groups"])
            self.assertFalse(result["scan"]["complete"])

    def test_worker_does_not_write_cache_before_ui_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            path = Path(temporary) / "saved.json"
            path.write_bytes(b"previous cache bytes")
            state = SimpleNamespace(queue=queue.Queue(), cancel_event=threading.Event(), cache_path=path)
            finder.DuplicateFileFinderApp._scan_worker(state, [temporary])
            self.assertEqual(b"previous cache bytes", path.read_bytes())
            self.assertIn("result", [kind for kind, _payload in state.queue.queue])

    def test_cancel_after_result_was_queued_preserves_rows_and_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            saved = finder.find_duplicate_groups([temporary])
            path = Path(temporary) / "saved.json"
            finder.save_cache(saved, path)
            before = path.read_bytes()
            state = self.app_state(saved, path)
            state.cancel_event.set()
            state._accept_scan_result({**saved, "groups": []})
            self.assertEqual(saved["groups"], state.groups)
            self.assertEqual(before, path.read_bytes())

    def test_failed_root_keeps_cached_rows_and_cache_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            saved = finder.find_duplicate_groups([temporary])
            path = Path(temporary) / "saved.json"
            finder.save_cache(saved, path)
            before = path.read_bytes()
            state = self.app_state(saved, path)
            result = finder.find_duplicate_groups([str(Path(temporary) / "missing")])
            state._accept_scan_result(result)
            self.assertEqual(saved["groups"], state.groups)
            self.assertEqual(before, path.read_bytes())

    def test_partial_results_are_labelled_without_replacing_complete_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            saved = finder.find_duplicate_groups([temporary])
            path = Path(temporary) / "saved.json"
            finder.save_cache(saved, path)
            before = path.read_bytes()
            state = self.app_state(saved, path)
            result = finder.find_duplicate_groups([temporary, str(Path(temporary) / "missing")])
            state._accept_scan_result(result)
            self.assertEqual(1, len(state.groups))
            self.assertIn("incomplete", state.status_text.set.call_args[0][0].lower())
            self.assertEqual(before, path.read_bytes())
            restored = finder.load_cache(path)
            self.assertFalse(restored["scan"]["complete"])
            self.assertEqual(state.groups, restored["groups"])

    def test_newer_complete_scan_takes_precedence_over_partial_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            completed = finder.find_duplicate_groups([temporary])
            path = Path(temporary) / "saved.json"
            partial = {
                **completed,
                "scanned_at": completed["scanned_at"] + 1,
                "scan": {**completed["scan"], "complete": False, "errors": 1},
            }
            finder.save_cache(partial, finder.partial_cache_path(path))
            completed["scanned_at"] += 2
            finder.save_cache(completed, path)
            self.assertTrue(finder.load_cache(path)["scan"]["complete"])

    def test_deletions_are_pruned_from_both_saved_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            _first, second = self.make_pair(Path(temporary))
            completed = finder.find_duplicate_groups([temporary])
            path = Path(temporary) / "saved.json"
            finder.save_cache(completed, path)
            partial = {
                **completed,
                "scanned_at": completed["scanned_at"] + 1,
                "scan": {**completed["scan"], "complete": False, "errors": 1},
            }
            finder.save_cache(partial, finder.partial_cache_path(path))
            errors = finder.update_saved_deletions(partial, path, {finder.canonical_path(str(second))})
            self.assertEqual([], errors)
            self.assertEqual([], finder.load_cache(path)["groups"])
            for saved_path in (path, finder.partial_cache_path(path)):
                self.assertEqual([], json.loads(saved_path.read_text(encoding="utf-8"))["groups"])

    def test_scan_result_does_not_restore_files_deleted_during_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            _first, second = self.make_pair(Path(temporary))
            saved = finder.find_duplicate_groups([temporary])
            state = self.app_state(saved, Path(temporary) / "saved.json")
            state.deleted_during_scan.add(finder.canonical_path(str(second)))
            state._accept_scan_result(saved)
            self.assertEqual([], state.groups)
            self.assertEqual([], finder.load_cache(state.cache_path)["groups"])

    def test_cache_write_failure_stays_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.make_pair(Path(temporary))
            saved = finder.find_duplicate_groups([temporary])
            state = self.app_state(saved, Path(temporary) / "saved.json")
            with mock.patch.object(finder, "save_cache", side_effect=OSError("fixture cache locked")):
                state._accept_scan_result(saved)
            status = state.status_text.set.call_args[0][0].lower()
            self.assertIn("cache", status)
            self.assertIn("fixture cache locked", status)


if __name__ == "__main__":
    unittest.main()
