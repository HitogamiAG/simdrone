import asyncio
import pytest
from app.live_logs import LiveLogs, LineDecoder, LogMailbox


def test_live_only_fanout_bounds_and_gap():
    async def run():
        logs = LiveLogs("px4")
        logs.publish(b"before subscribe")
        first, second = logs.subscribe(), logs.subscribe()
        logs.publish(b"\x1b[31mhello\x1b[0m\n")
        assert (await first.get())["message"] == "hello"
        assert (await second.get())["message"] == "hello"
        for i in range(140):
            logs.publish(str(i).encode())
        assert len(first.values) == 128
        assert await first.get() == {"type": "gap", "dropped": 12}
        assert (await first.get())["message"] == "12"
        logs.unsubscribe(first)
        logs.close()
        logs.close()
        assert await second.get() is None
        assert not logs.queues
    asyncio.run(run())


def test_decoder_bounds_unterminated_output_and_replaces_invalid_utf8():
    async def run():
        logs = LiveLogs("gazebo")
        queue = logs.subscribe()
        decoder = LineDecoder(logs)
        decoder.feed(b"x" * 20000)
        assert len(decoder.pending) < 8192
        assert len((await queue.get())["message"]) == 8192
        decoder.feed(b"\xff\n")
        decoder.finish()
        assert len(queue.values) == 2
        assert "\ufffd" in (await queue.get())["message"] or "\ufffd" in (await queue.get())["message"]
    asyncio.run(run())


def test_px4_reader_drains_output_without_subscribers(tmp_path):
    import sys
    from types import SimpleNamespace
    from app.adapters import Px4Adapter
    async def run():
        adapter = Px4Adapter(None)
        logs = LiveLogs("px4")
        process = await asyncio.create_subprocess_exec(sys.executable, "-u", "-c",
            "import sys; sys.stdout.write('x' * 200000); sys.stderr.write('error\\n')",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        reader = asyncio.create_task(adapter._read_output(process, logs, tmp_path / "px4.log"))
        await asyncio.wait_for(process.wait(), 10)
        await reader
        assert logs.closed
        assert (tmp_path / "px4.log").stat().st_size >= 200000
    asyncio.run(run())


def test_socket_cleanup_cancels_slow_sender_on_process_exit():
    from app.live_logs import serve_logs
    class Socket:
        def __init__(self):
            self.sending = asyncio.Event()
            self.cancelled = False
            self.code = None
        async def accept(self): pass
        async def send_json(self, message):
            self.sending.set()
            try:
                await asyncio.Future()
            finally:
                self.cancelled = True
        async def receive(self): return await asyncio.Future()
        async def close(self, code, reason):
            assert self.cancelled
            self.code = code
    async def run():
        logs, socket = LiveLogs("px4"), Socket()
        task = asyncio.create_task(serve_logs(socket, logs))
        await asyncio.sleep(.03)
        logs.publish(b"pending")
        await asyncio.wait_for(socket.sending.wait(), 1)
        logs.close()
        await asyncio.wait_for(task, 1)
        assert socket.code == 1012
        assert not logs.queues
    asyncio.run(run())
