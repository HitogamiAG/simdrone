"""Owns the Gazebo process group and its log file."""
import logging
import os
import signal
import subprocess

log = logging.getLogger("gazebo-service")

class GazeboProcess:
    def __init__(self, log_path: str):
        self.log_path = log_path
        self.process = None
        self._log = None

    def start(self, command):
        if self.process is not None:
            raise RuntimeError("Gazebo process is already started")
        self._log = open(self.log_path, "a", buffering=1)
        try:
            self.process = subprocess.Popen(command, start_new_session=True, stdout=self._log, stderr=subprocess.STDOUT)
        except Exception:
            self._log.close()
            self._log = None
            raise
        return self.process

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        process, self.process = self.process, None
        try:
            if process is not None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=8)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process is not None:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired: log.error("Gazebo process group did not exit")
        finally:
            if self._log:
                self._log.close()
                self._log = None
