"""Owns the Gazebo process group and its log file."""
import logging
import os
import signal
import subprocess
import threading
from ..live_logs import LiveLogs, LineDecoder

log = logging.getLogger("gazebo-service")

class GazeboProcess:
    def __init__(self, log_path: str):
        self.log_path = log_path
        self.process = None
        self._log = None
        self.logs = LiveLogs("gazebo")
        self._reader = None

    def start(self, command):
        if self.process is not None:
            raise RuntimeError("Gazebo process is already started")
        self.logs = LiveLogs("gazebo")
        self._log = open(self.log_path, "ab", buffering=0)
        try:
            self.process = subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except Exception:
            self._log.close()
            self._log = None
            self.logs.close()
            raise
        self._reader = threading.Thread(target=self._read_output, args=(self.process, self.logs, self._log), daemon=True)
        self._reader.start()
        return self.process

    def _read_output(self, process, logs, logfile):
        decoder = LineDecoder(logs)
        try:
            while chunk := os.read(process.stdout.fileno(), 4096):
                try:
                    logfile.write(chunk)
                except OSError:
                    pass
                decoder.feed(chunk)
            decoder.finish()
        finally:
            process.stdout.close()
            logs.close()

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
            self.logs.close()
            if self._reader:
                self._reader.join(timeout=3)
                self._reader = None
            if self._log:
                self._log.close()
                self._log = None
