#!/usr/bin/env python3
"""
cleanup.py — retention sweep for generated PDFs.

The /download route serves files from outputs/. Those PDFs are throwaway
artifacts that still contain personal data (name, contact, work history), so we
don't keep them indefinitely. This deletes outputs/*.pdf older than
OUTPUT_RETENTION_DAYS (default 7). Uploaded resumes in data/resumes/ are NOT
touched — those are user libraries, removable from the Settings panel.

Run from cron (see deploy/cleanup.crontab.example). Stdlib only, no deps.
"""
import os
import glob
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(_BASE, "outputs")
RETENTION_DAYS = int(os.environ.get("OUTPUT_RETENTION_DAYS", "7"))


def sweep() -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for path in glob.glob(os.path.join(OUTPUT_DIR, "*.pdf")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] cleanup: removed {removed} PDF(s) older than {RETENTION_DAYS}d")


if __name__ == "__main__":
    sweep()
