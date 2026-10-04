"""Live-only process output fanout; bounded delivery, no replay."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import re
import threading
from fastapi import WebSocketDisconnect

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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


class LiveLogs:
    def __init__(self, source):
        self.source = source
        self.queues = set()
        self.lock = threading.Lock()
        self.closed = False

    def subscribe(self):
        with self.lock:
            queue = LogMailbox()
            if self.closed:
                queue.put_nowait(None)
            else:
                self.queues.add(queue)
            return queue

    def unsubscribe(self, queue):
        with self.lock:
            self.queues.discard(queue)

    def publish(self, line):
        message = {"type": "log", "source": self.source,
                   "received_at": datetime.now(timezone.utc).isoformat(),
                   "message": ANSI.sub("", line.decode("utf-8", errors="replace").rstrip("\r\n"))[:8192]}
        with self.lock:
            if not self.closed:
                for queue in self.queues:
                    queue.put_nowait(message)

    def close(self, discard=True):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for queue in self.queues:
                if discard:
                    queue.clear()
                queue.put_nowait(None)
            if discard:
                self.queues.clear()


class LineDecoder:
    def __init__(self, logs):
        self.logs = logs
        self.pending = b""

    def feed(self, chunk):
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            for offset in range(0, max(1, len(line)), 8192):
                self.logs.publish(line[offset:offset + 8192])
        # Bound incomplete lines too; split oversized output into chunks.
        while len(self.pending) >= 8192:
            self.logs.publish(self.pending[:8192])
            self.pending = self.pending[8192:]

    def finish(self):
        if self.pending:
            self.logs.publish(self.pending)
            self.pending = b""


async def serve_logs(websocket, logs):
    await websocket.accept()
    queue = logs.subscribe()
    async def send():
        while True:
            message = await queue.get()
            if message is None:
                return
            await websocket.send_json(message)
    async def disconnect():
        while True:
            if (await websocket.receive())["type"] == "websocket.disconnect":
                return
    async def ended():
        while not logs.closed:
            await asyncio.sleep(.05)
        # Give a fast subscriber time to receive final fatal output at EOF.
        # A blocked sender is still cancelled after this bounded grace period.
        await asyncio.sleep(.25)
    sender = asyncio.create_task(send())
    receiver = asyncio.create_task(disconnect())
    monitor = asyncio.create_task(ended())
    tasks = [sender, receiver, monitor]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        close_socket = logs.closed and receiver not in done
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if close_socket:
            try:
                await websocket.close(code=1012, reason="process log stream ended")
            except (RuntimeError, WebSocketDisconnect):
                pass
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logs.unsubscribe(queue)
