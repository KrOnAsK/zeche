"""Push a finished bill into an existing Tricount as one custom-split expense.

Tricount has no official write API. This goes through `tricount-api` on PyPI,
an unofficial client reverse-engineered from the Android app, so it can break
whenever bunq ships an update. Every failure here is non-fatal: the bill stays
in Zeche and the edit page falls back to a copy-paste summary.
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass

# Sharing links look like https://tricount.com/tMjbqgwJxaikhUbkNz — the trailing
# segment is the key. Accept a bare key too, since people paste both.
KEY_RE = re.compile(r"(?:^|/)([A-Za-z0-9_-]{8,40})/?(?:[?#].*)?$")

DEFAULT_KEY = os.environ.get("ZECHE_TRICOUNT_KEY", "").strip()

_lock = threading.Lock()
_client = None


def available() -> bool:
    try:
        import tricount  # noqa: F401
    except ImportError:
        return False
    return True


def parse_key(link_or_key: str) -> str:
    m = KEY_RE.search((link_or_key or "").strip())
    if not m:
        raise ValueError("That doesn't look like a Tricount sharing link")
    return m.group(1)


def _get_client():
    """One client per process. Credentials land in the data volume."""
    global _client
    with _lock:
        if _client is None:
            from tricount import load_client

            # The library writes tricount_credentials.json to the working
            # directory; main.py chdirs into the data volume at startup so the
            # device registration survives a rebuild.
            _client = load_client()
        return _client


@dataclass
class Member:
    id: str
    name: str


def _members(t) -> list[Member]:
    return [Member(id=str(m.id), name=m.display_name) for m in t.members]


def fetch(key: str) -> tuple[str, list[Member]]:
    """Join the tricount and return its title plus members."""
    client = _get_client()
    t = client.join_tricount(key)
    return getattr(t, "title", "") or "", _members(t)


def push(
    key: str,
    description: str,
    total_cents: int,
    payer_id: str,
    allocations: list[tuple[str, int]],
) -> str:
    """Create one expense. `allocations` is [(member_id, cents_owed), ...]."""
    if total_cents <= 0:
        raise ValueError("Nothing to push — the bill totals zero")
    charged = sum(c for _, c in allocations if c > 0)
    if charged != total_cents:
        raise ValueError(
            f"Shares add up to {charged / 100:.2f} but the bill is "
            f"{total_cents / 100:.2f} — refusing to push a mismatched expense"
        )

    client = _get_client()
    t = client.join_tricount(key)
    by_id = {str(m.id): m for m in t.members}

    missing = [mid for mid, c in allocations if c > 0 and mid not in by_id]
    if missing or payer_id not in by_id:
        raise ValueError("Someone on this bill is no longer in the tricount")

    tx = client.create_transaction_custom_split(
        tricount=t,
        description=description[:100],
        amount=total_cents,
        payer=by_id[payer_id],
        # Members who owe nothing are left off the expense entirely rather than
        # sent as a zero, which Tricount renders as a participant with no share.
        allocations=[(by_id[mid], cents) for mid, cents in allocations if cents > 0],
    )
    return str(tx)


def match_names(people: list[dict], members: list[Member]) -> dict[str, str]:
    """Best-effort person -> member mapping. Exact first, then case-folded."""
    out: dict[str, str] = {}
    taken: set[str] = set()
    for pas in (lambda s: s, lambda s: s.strip().casefold()):
        index = {pas(m.name): m.id for m in members if m.id not in taken}
        for p in people:
            if p["id"] in out:
                continue
            hit = index.get(pas(p["name"]))
            if hit and hit not in taken:
                out[p["id"]] = hit
                taken.add(hit)
    return out
