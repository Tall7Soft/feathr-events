import asyncio
import time

import pytest

from app.queue import InMemoryQueue


async def test_send_receive_roundtrip():
    q = InMemoryQueue("t")
    await q.send({"x": 1})
    msg = await q.receive()
    assert msg.body == {"x": 1}
    assert msg.receive_count == 1
    await q.ack(msg)
    assert q.in_flight == 0


async def test_receive_increments_receive_count_on_retry():
    q = InMemoryQueue("t")
    await q.send({"x": 1})
    msg = await q.receive()
    assert msg.receive_count == 1
    await q.nack(msg, delay=0.0)
    msg2 = await q.receive()
    assert msg2.message_id == msg.message_id
    assert msg2.receive_count == 2


async def test_delayed_send_respects_visibility():
    q = InMemoryQueue("t")
    start = time.monotonic()
    await q.send({"x": 1}, delay=0.2)
    msg = await q.receive()
    elapsed = time.monotonic() - start
    assert msg.body == {"x": 1}
    assert elapsed >= 0.18


async def test_join_blocks_until_acked():
    q = InMemoryQueue("t")
    await q.send({"x": 1})
    msg = await q.receive()

    join_done = asyncio.Event()

    async def waiter():
        await q.join()
        join_done.set()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not join_done.is_set()
    await q.ack(msg)
    await asyncio.wait_for(join_done.wait(), timeout=1.0)
    await task


async def test_join_times_out_when_unacked():
    q = InMemoryQueue("t")
    await q.send({"x": 1})
    _ = await q.receive()
    drained = await q.join(timeout=0.1)
    assert drained is False
