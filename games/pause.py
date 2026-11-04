import asyncio

state = {"paused": False, "msg": None}
resume_event = asyncio.Event()
resume_event.set()


def is_paused() -> bool:
    return state["paused"]


def pause():
    state["paused"] = True
    resume_event.clear()


def resume():
    state["paused"] = False
    resume_event.set()


async def sleep(seconds: float):
    left = seconds
    while left > 0:
        await resume_event.wait()
        step = min(0.5, left)
        await asyncio.sleep(step)
        if not state["paused"]:
            left -= step


async def wait_for(event: asyncio.Event, timeout: float) -> bool:
    left = timeout
    while True:
        if event.is_set():
            return True
        if left <= 0:
            raise asyncio.TimeoutError
        await resume_event.wait()
        step = min(0.5, left)
        try:
            await asyncio.wait_for(event.wait(), step)
            return True
        except asyncio.TimeoutError:
            if not state["paused"]:
                left -= step
