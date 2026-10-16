"""
Flaky-aware delta debugging (ddmin) over a recorded action/network journal.

Shrinks a reproducing sequence to the smallest subsequence that still
reproduces the failure signature. Each candidate is tested ``trials`` times
and must reproduce in a strict majority, so flakiness cannot fake a reduction.
Hermetic by design: the caller's predicate decides reproduction (normally a
stubbed replay), and write requests are never replayed at a live backend by
this module -- it only orders/tests items the caller supplies.
"""

from __future__ import annotations

from typing import Any, Callable, List, Sequence


def _reproduces(predicate: Callable[[List[Any]], bool], candidate: List[Any], trials: int) -> bool:
    if trials <= 1:
        return bool(predicate(candidate))
    hits = sum(1 for _ in range(trials) if predicate(candidate))
    return hits * 2 > trials  # strict majority


def ddmin(items: Sequence[Any], predicate: Callable[[List[Any]], bool], *, trials: int = 1) -> List[Any]:
    """
    Classic ddmin with granularity doubling, memoized by item identity tuple.
    Returns a 1-minimal subsequence (removing any single item breaks repro).
    """
    cache: dict = {}

    def test(candidate: List[Any]) -> bool:
        key = tuple(id(x) if not isinstance(x, (str, int, float, bool, type(None))) else x for x in candidate)
        # Items are typically dicts (unhashable); key on their repr for memoization.
        key = tuple(repr(x) for x in candidate)
        if key not in cache:
            cache[key] = _reproduces(predicate, candidate, trials)
        return cache[key]

    current = list(items)
    if not test(current):
        return current  # does not reproduce as-is; nothing to minimize
    n = 2
    while len(current) >= 2:
        chunk = max(1, len(current) // n)
        reduced = False
        i = 0
        while i < len(current):
            candidate = current[:i] + current[i + chunk:]
            if candidate and test(candidate):
                current = candidate
                n = max(2, n - 1)
                reduced = True
                break
            i += chunk
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)
    return current


def _cdd_chunk_size(r: int, p0: float) -> int:
    """CDD (ICSE 2025): probabilities behave as counters, p_r = p0 * 1.582^r."""
    import math

    p = p0 * (1.582 ** r)
    if p >= 1.0:
        return 1
    s_star = -1.0 / math.log(1.0 - p)
    lo, hi = max(1, math.floor(s_star)), max(1, math.ceil(s_star))
    return max((lo, hi), key=lambda s: s * (1.0 - p) ** s)


def cdd(
    items: Sequence[Any],
    predicate: Callable[[List[Any]], bool],
    *,
    trials: int = 1,
    p0: float = 0.1,
    one_minimal: bool = True,
) -> List[Any]:
    """
    Counter-based delta debugging: remove the lowest-prior prefix whose size
    maximizes the expected reduction, with far fewer predicate queries than
    ddmin (~52% fewer in the CDD evaluation). CDD alone does not guarantee
    1-minimality, so a final ddmin single-removal pass runs by default.
    Items may carry a ``prior`` (dict key or attribute, P(essential)); the
    removal order is lowest-prior first, so likely-irrelevant units (assets,
    pings) are tested for removal before likely-essential ones.
    """
    cache: dict = {}

    def test(candidate: List[Any]) -> bool:
        key = tuple(repr(x) for x in candidate)
        if key not in cache:
            cache[key] = _reproduces(predicate, candidate, trials)
        return cache[key]

    def prior_of(item: Any) -> float:
        if isinstance(item, dict):
            return float(item.get("prior", 0.25))
        return float(getattr(item, "prior", 0.25))

    current = sorted(items, key=prior_of)
    if not test(list(current)):
        return list(items)
    r = 0
    i = 0
    while current:
        s = min(_cdd_chunk_size(r, p0), len(current))
        if i >= len(current):
            if s <= 1:
                break
            r += 1
            i = 0
            continue
        candidate = current[:i] + current[i + s:]
        if candidate and test(candidate):
            current = candidate
            r += 1
            i = 0  # priors/order shifted; restart the scan
        else:
            i += s
            if i >= len(current) and s <= 1:
                break
    if one_minimal:
        current = ddmin(current, predicate, trials=trials)
    return current


async def ddmin_async(items, predicate, *, trials: int = 1):
    """Async ddmin (predicate is a coroutine function), 1-minimal result."""
    cache: dict = {}

    async def test(candidate) -> bool:
        key = tuple(repr(x) for x in candidate)
        if key not in cache:
            if trials <= 1:
                cache[key] = bool(await predicate(candidate))
            else:
                hits = 0
                for _ in range(trials):
                    hits += 1 if await predicate(candidate) else 0
                cache[key] = hits * 2 > trials
        return cache[key]

    current = list(items)
    if not await test(current):
        return current
    n = 2
    while len(current) >= 2:
        chunk = max(1, len(current) // n)
        reduced = False
        i = 0
        while i < len(current):
            candidate = current[:i] + current[i + chunk:]
            if candidate and await test(candidate):
                current = candidate
                n = max(2, n - 1)
                reduced = True
                break
            i += chunk
        if not reduced:
            if n >= len(current):
                break
            n = min(len(current), n * 2)
    return current


async def cdd_async(items, predicate, *, trials: int = 1, p0: float = 0.1, one_minimal: bool = True):
    """Async CDD: same ordering as cdd(), with a coroutine predicate."""
    cache: dict = {}

    async def test(candidate) -> bool:
        key = tuple(repr(x) for x in candidate)
        if key not in cache:
            if trials <= 1:
                cache[key] = bool(await predicate(candidate))
            else:
                hits = 0
                for _ in range(trials):
                    hits += 1 if await predicate(candidate) else 0
                cache[key] = hits * 2 > trials
        return cache[key]

    def prior_of(item) -> float:
        if isinstance(item, dict):
            return float(item.get("prior", 0.25))
        return float(getattr(item, "prior", 0.25))

    current = sorted(items, key=prior_of)
    if not await test(list(current)):
        return list(items)
    r = 0
    i = 0
    while current:
        s = min(_cdd_chunk_size(r, p0), len(current))
        if i >= len(current):
            if s <= 1:
                break
            r += 1
            i = 0
            continue
        candidate = current[:i] + current[i + s:]
        if candidate and await test(candidate):
            current = candidate
            r += 1
            i = 0
        else:
            i += s
            if i >= len(current) and s <= 1:
                break
    if one_minimal:
        current = await ddmin_async(current, predicate, trials=trials)
    return current


def minimize_journal(
    steps: Sequence[Any],
    predicate: Callable[[List[Any]], bool],
    *,
    trials: int = 3,
    algorithm: str = "ddmin",
) -> dict:
    """Minimize recorded steps; report the reduction ratio for the corpus gate."""
    original = list(steps)
    if algorithm == "cdd":
        minimal = cdd(original, predicate, trials=trials)
    else:
        minimal = ddmin(original, predicate, trials=trials)
    return {
        "minimal_steps": minimal,
        "original_count": len(original),
        "minimal_count": len(minimal),
        "reduction_ratio": round(1 - (len(minimal) / len(original)), 4) if original else 0.0,
        "algorithm": algorithm,
    }
