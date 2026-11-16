"""Helpers for the push client tests."""

import hashlib

import pandas as pd

from resink_catalog.cloud.push import Pusher


def make_pusher(catalog, device, cloud, sleeps=None, **kw) -> Pusher:
    return Pusher(
        catalog,
        device,
        transport=cloud.transport(),
        out=lambda s: None,
        sleep=(sleeps if sleeps is not None else []).append,
        **kw,
    )


def rows(n: int, start: int = 0) -> pd.DataFrame:
    """Rows that compress badly, so a data file spans several chunks of the fake's 256 B to 1 KiB limits."""
    ids = range(start, start + n)
    return pd.DataFrame({"id": ids, "name": [hashlib.sha256(str(i).encode()).hexdigest() for i in ids]})
