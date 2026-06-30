import asyncio
import threading
import time
import pytest
from ag_core.utils.rate_limiter import TokenBucketRateLimiter


@pytest.mark.asyncio
async def test_rate_limiter_lock_bypass():
    """Verify that the TokenBucketRateLimiter's async_lock can be bypassed due to loop-switching."""
    limiter = TokenBucketRateLimiter(rate=0.0, capacity=2.0)  # capacity 2, no refill

    loop_a = asyncio.get_running_loop()

    # 1. Acquire the async lock of the limiter directly in Loop A
    lock_a = limiter.async_lock
    await lock_a.acquire()
    assert lock_a.locked()

    # 2. Start Task A1 which tries to consume. It should block because lock_a is locked.
    task_a1_started = asyncio.Event()
    task_a1_done = asyncio.Event()
    task_a1_result = []

    async def task_a1():
        task_a1_started.set()
        res = await limiter.consume_async(1.0)
        task_a1_result.append(res)
        task_a1_done.set()

    asyncio.create_task(task_a1())
    await asyncio.sleep(0.05)  # Let task A1 run and block on the lock

    # 3. Simulate Thread B (with Loop B) calling consume_async.
    # This will overwrite the limiter's _async_lock with a lock for Loop B.
    def thread_b_worker():
        loop_b = asyncio.new_event_loop()
        asyncio.set_event_loop(loop_b)
        try:
            # This call will overwrite the shared _async_lock
            loop_b.run_until_complete(limiter.consume_async(1.0))
        finally:
            loop_b.close()

    t = threading.Thread(target=thread_b_worker)
    t.start()
    t.join()

    # 4. Now start Task A2 in Loop A.
    # Because the lock is loop-safe, Task A2 should also block on lock_a
    # rather than bypassing Task A1.
    task_a2_started = asyncio.Event()
    task_a2_done = asyncio.Event()
    task_a2_result = []

    async def task_a2():
        task_a2_started.set()
        res = await limiter.consume_async(1.0)
        task_a2_result.append(res)
        task_a2_done.set()

    asyncio.create_task(task_a2())
    await asyncio.sleep(0.05)  # Let task A2 run and block on the lock

    # Release the original lock to let the waiting tasks continue
    lock_a.release()
    await task_a1_done.wait()
    await task_a2_done.wait()

    print(f"\nTask A1 result (was waiting): {task_a1_result}")
    print(f"Task A2 result (waited): {task_a2_result}")

    # Verify loop safety: Task A1 must run first and return True, and Task A2 must return False.
    assert (
        task_a1_result[0] is True
    ), "Task A1 should have consumed the token after lock release"
    assert (
        task_a2_result[0] is False
    ), "Task A2 should get False because Task A1 consumed the last token"
    print("Lock safety confirmed! TokenBucketRateLimiter async_lock is loop-safe.")
