import json
import tempfile
import threading
import unittest
from pathlib import Path

import duplicate_file_finder as finder


class DuplicateFinderTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
