import asyncio
import os
import shutil
import signal
from pathlib import Path


async def stop_process(process: asyncio.subprocess.Process | None, timeout: float):
    if process is None or process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


def prepare_rootfs(template: Path, target: Path):
    shutil.copytree(template, target, symlinks=True, dirs_exist_ok=True)
