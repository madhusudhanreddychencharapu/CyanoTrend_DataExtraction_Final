"""Durations and live transfer counters accessible without querying SQLite."""

from contextlib import contextmanager
import os
import threading
import time
from .common import utcnow


class Telemetry:
    def __init__(self, ledger, sid, attempt):
        self.ledger = ledger
        self.sid = sid
        self.attempt = attempt
        self.lock = threading.Lock()
        self.bytes = 0
        self.last = 0
        self.start = time.monotonic()

    @contextmanager
    def stage(self, name, details=None):
        start = time.monotonic()
        stamp = utcnow()
        ok = False
        self.ledger.update(self.sid, stage=name)
        try:
            yield
            ok = True
        finally:
            self.ledger.timing(
                self.sid,
                self.attempt,
                name,
                stamp,
                time.monotonic() - start,
                ok,
                details,
            )

    def download(self, n):
        with self.lock:
            self.bytes += n
            now = time.monotonic()
            if now - self.last >= 1:
                self.ledger.update(
                    self.sid,
                    bytes_downloaded=self.bytes,
                    download_seconds=now - self.start,
                )
                self.last = now

    def finish_download(self):
        self.ledger.update(
            self.sid,
            bytes_downloaded=self.bytes,
            download_seconds=time.monotonic() - self.start,
        )


def redact(text):
    for key in (
        "CDSE_USERNAME",
        "CDSE_PASSWORD",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        value = os.environ.get(key)
        if value:
            text = text.replace(value, "[redacted]")
    return text
