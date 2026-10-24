Duplicate File Finder
=====================

This is a lightweight Windows duplicate-file finder using only Python's
standard library and Tkinter. It does not scan automatically.

Run
---

Double-click run_duplicate_file_finder.bat. Python 3 with Tkinter must be
installed. The script itself is duplicate_file_finder.py.

1. Add one or more folders, or click All local drives.
2. Click Scan Now. Scanning runs in the background.
3. Expand a duplicate group to inspect its files.
4. Double-click file rows to select them, or use Select safe files.
5. Click Delete selected and confirm. Files are moved to the Windows Recycle
   Bin, and the final copy in each group is protected.

Cached results
--------------

The last completed scan is saved at:

%LOCALAPPDATA%\DuplicateFileFinder\scan_cache.json

The app loads this result at startup and never starts a scan by itself. A new
scan leaves the cached rows visible and usable while it runs. Deletions update
the cache without triggering a rescan. If the cache is unavailable, the app
still works and can create a new one after the next completed scan.

Safety and behavior
-------------------

- Files are grouped by size before SHA-256 content hashing, so unique-size
  files are not hashed.
- Selected files are rechecked for size and SHA-256 content immediately before
  deletion. Changed or missing files are skipped.
- The final currently existing file in every duplicate group is protected.
- Rescanning is explicit: use Scan Now whenever you want fresh results.
- Sort the tree by largest/smallest group size, number of copies, or path.

This source package is intentionally not a compiled executable. If a portable
single-file executable is wanted later, build it in a separate release step
with the user's chosen Python packager.
