"""Helpers for the push client tests."""

import hashlib
import time

import pandas as pd

from resink_catalog.cloud.push import Pusher


class Sleeps(list):
    """Records fake sleeps; `clock` is a monotonic clock that moves forward by them (and by real time)."""

    def __init__(self):
        super().__init__()
        self.offset = 0.0

    def append(self, seconds: float) -> None:
        super().append(seconds)
        self.offset += seconds

    def clock(self) -> float:
        return time.monotonic() + self.offset


def make_pusher(catalog, device, cloud, sleeps=None, **kw) -> Pusher:
    return Pusher(
        catalog,
        device,
        transport=cloud.transport(),
        out=lambda s: None,
        sleep=(sleeps := sleeps if sleeps is not None else Sleeps()).append,
        clock=getattr(sleeps, "clock", time.monotonic),
        **kw,
    )


def rows(n: int, start: int = 0) -> pd.DataFrame:
    """Rows that compress badly, so a data file spans several chunks of the fake's 256 B to 1 KiB limits."""
    ids = range(start, start + n)
    return pd.DataFrame({"id": ids, "name": [hashlib.sha256(str(i).encode()).hexdigest() for i in ids]})
