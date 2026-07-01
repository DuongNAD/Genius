"""Tests for gather_or_raise (M4): concurrent fan-out that never orphans a
sibling when one branch fails, while preserving fail-fast error propagation."""

import asyncio

import pytest

from orchestrator import gather_or_raise


@pytest.mark.asyncio
async def test_returns_all_results_in_order_on_success():
    async def ok(v):
        return v

    results = await gather_or_raise(ok(1), ok(2), ok(3))
    assert results == [1, 2, 3]


@pytest.mark.asyncio
async def test_reraises_the_branch_exception():
    async def boom():
        raise ValueError("kaboom")

    async def ok():
        return "ok"

    with pytest.raises(ValueError, match="kaboom"):
        await gather_or_raise(boom(), ok())


@pytest.mark.asyncio
async def test_all_siblings_complete_even_when_one_fails():
    # The core M4 property: a fast-failing branch must NOT orphan/skip the
    # slower siblings. With a bare asyncio.gather they would still be pending
    # (orphaned) when the exception propagates; gather_or_raise awaits them all.
    completed = []

    async def slow_ok(tag):
        await asyncio.sleep(0.02)
        completed.append(tag)
        return tag

    async def fail_fast():
        raise RuntimeError("early")

    with pytest.raises(RuntimeError, match="early"):
        await gather_or_raise(fail_fast(), slow_ok("a"), slow_ok("b"))

    assert set(completed) == {"a", "b"}


@pytest.mark.asyncio
async def test_first_exception_in_argument_order_wins():
    async def boom_a():
        raise ValueError("A")

    async def boom_b():
        raise KeyError("B")

    # Both fail; the helper re-raises the first in argument order deterministically.
    with pytest.raises(ValueError, match="A"):
        await gather_or_raise(boom_a(), boom_b())


@pytest.mark.asyncio
async def test_single_awaitable():
    async def ok():
        return 42

    assert await gather_or_raise(ok()) == [42]
