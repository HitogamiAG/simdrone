"""Live-only process output fanout; bounded delivery, no replay."""
import asyncio
from collections import deque
import threading


class LogMailbox:
    def __init__(self, capacity=128):
        self.capacity = capacity
        self.values = deque()
        self.dropped = 0
        self.lock = threading.Lock()

    def put_nowait(self, value):
        with self.lock:
            if len(self.values) >= self.capacity:
                self.values.popleft()
                self.dropped += 1
            self.values.append(value)

    def clear(self):
        with self.lock:
            self.values.clear()
            self.dropped = 0

    async def get(self):
        while True:
            with self.lock:
                if self.dropped:
                    dropped, self.dropped = self.dropped, 0
                    return {"type": "gap", "dropped": dropped}
                if self.values:
                    return self.values.popleft()
            await asyncio.sleep(.02)
