Duplicate File Finder
=====================

Version 1.0.1

This is a lightweight Windows duplicate-file finder using only Python's
standard library and Tkinter. It does not scan automatically.

Run
---

Double-click run_duplicate_file_finder.bat. Python 3 with Tkinter must be
installed. The script itself is duplicate_file_finder.py.

1. Add one or more folders, or click All fixed drives. Add removable drives or
   network folders separately with Add folder.
2. Click Scan Now. Scanning runs in the background.
3. Expand a duplicate group to inspect its files.
4. Double-click file rows to select them, or use Select safe files.
5. Click Delete selected and confirm. Files are moved to the Windows Recycle
   Bin, and the final copy in each group is protected.

Cached results
--------------

The last completed scan is saved at:

%LOCALAPPDATA%\DuplicateFileFinder\scan_cache.json

Incomplete results are saved separately at:

%LOCALAPPDATA%\DuplicateFileFinder\scan_cache.partial.json

The app loads the newest valid scan at startup, labels incomplete results, and
never starts a scan by itself. A new scan leaves the cached rows visible and
usable while it runs. Deletions update
the cache without triggering a rescan. If the cache is unavailable, the app
still works and can create a new one after the next completed scan.

Scan coverage and cancellation
------------------------------

Scan details shows the selected locations, files listed and hashed, skipped
symbolic links, and read/location failures (with the first 50 paths and reasons).
Missing folders, access-denied folders, unreadable files, and files that change
while being read are reported instead of silently appearing to be a full scan.

An incomplete scan can show duplicates from the files it could read, but does
not overwrite the last completed cache. Its partial results are saved separately
and restored on launch if they are newer. If no files could be checked, the
previous result stays visible. Cancel also keeps the current result and cache,
including when the worker finishes just before you click Cancel. Closing the
app cannot let a background scan write over the cache afterward.

The app compares exact file contents, regardless of the filename. A browser's
"photo (1).jpg" copy matches "photo.jpg" if their bytes are identical. Pictures
that merely look alike but have different encodings or metadata are not exact
file duplicates.

Safety and behavior
-------------------

- Files are grouped by size before SHA-256 content hashing, so unique-size
  files are not hashed.
- Selected files are rechecked for size and SHA-256 content immediately before
  deletion. Changed or missing files are skipped.
- The final currently existing file in every duplicate group is protected.
- Rescanning is explicit: use Scan Now whenever you want fresh results.
- Sort the tree by largest/smallest group size, number of copies, or path.
- Files deleted from cached results during a scan are removed from the incoming
  result as well, so the scan does not bring those deleted rows back.

Verification
------------

From the GitHub source checkout (which includes test_duplicate_file_finder.py),
run the standard-library regression suite:

python -B -m unittest discover -s . -p "test_*.py" -v

It covers cancellation boundaries, cache preservation and write failures,
missing/unreadable locations, files changing/disappearing during scanning,
Windows file metadata differences, duplicate grouping, and deletion selection.

This source package is intentionally not a compiled executable. If a portable
single-file executable is wanted later, build it in a separate release step
with the user's chosen Python packager.
