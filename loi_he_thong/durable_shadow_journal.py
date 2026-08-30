"""Durability and bounded-segment hook for the active shadow journal."""
import os

from loi_he_thong import journal_segments

VERSION = "DURABLE_SHADOW_JOURNAL_V2_BOUNDED_SEGMENTS"
_CRITICAL = {"ENTRY", "EXIT"}


def _fsync_path_and_parent(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def install(shadow):
    if getattr(shadow, "_durable_shadow_journal_installed", False):
        return VERSION

    original = shadow._append_event

    def append_event_durable(event, payload):
        journal_segments.prepare_append(shadow.EVENT_PATH)
        out = original(event, payload)
        if str(event).upper() in _CRITICAL:
            _fsync_path_and_parent(shadow.EVENT_PATH)
        return out

    shadow._append_event = append_event_durable
    shadow._durable_shadow_journal_installed = True
    return VERSION
