"""A small cooperative-cancellation primitive shared by pipeline.fetch_all
and the scrapers with a real pagination/retry loop (arxiv/openalex) — the
ones actually slow enough (a deep backfill, a rate-limit backoff wait) that
a "Cancel" click needs to stop something in progress, not just prevent the
next step from starting.

Python threads can't be killed from outside, so this is cooperative:
check_cancelled() raises FetchCancelled the moment a caller opts in to
check — always at a safe point (between scraper calls, between paginated
pages, between batch items), never in the middle of a network call or a
database write. Whatever a scraper already collected but hasn't returned
yet when it's interrupted is lost; anything from an earlier scraper in the
same run that already finished and got stored stays stored — cancelling
means "stop now," not "roll back everything so far."
"""
from __future__ import annotations

import threading


class FetchCancelled(Exception):
    """Raised by check_cancelled() when cancel_event is set. Meant to be
    caught once, near the top of the run (scheduler._run_fetch), not by
    every intermediate caller — let it propagate."""


def check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise FetchCancelled()
