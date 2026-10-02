"""One camera's bounded raw-frame to FFmpeg publishing session."""
import logging
import queue
import subprocess
import threading

log = logging.getLogger("gazebo-service")

class EncoderSession:
    def __init__(self, client, sensor, image_type, command, width, height, pixel_format, sample_callback, *, popen=subprocess.Popen):
        self.client = client
        self.sensor = sensor
        self.image_type = image_type
        self.command = command
        self.width = width
        self.height = height
        self.pixel_format = pixel_format
        self.sample_callback = sample_callback
        self._popen = popen
        self.frames = queue.Queue(maxsize=2)
        self.stop_event = threading.Event()
        self.process = None
        self.writer = None
        self._subscribed = False
        self._closed = False

    @staticmethod
    def frame_bytes(message, width, height, pixel_format):
        raw = bytes(message.data)
        row_bytes = width * 3
        stride = message.step or row_bytes
        if len(raw) < (height - 1) * stride + row_bytes:
            raise ValueError("Gazebo image payload is shorter than its declared dimensions and stride")
        frame = b"".join(raw[y * stride:y * stride + row_bytes] for y in range(height))
        if pixel_format == 3:
            frame = b"".join(frame[i:i + 3][::-1] for i in range(0, len(frame), 3))
        return frame

    def start(self):
        self.process = self._popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
        try:
            if not self.client.subscribe(self.image_type, self.sensor.topic, self._on_frame):
                raise RuntimeError(f"Cannot subscribe to {self.sensor.topic}")
            self._subscribed = True
            self.writer = threading.Thread(target=self._write_frames, daemon=True, name=f"camera-{self.sensor.id}")
            self.writer.start()
        except Exception:
            self.stop()
            raise
        return self.process

    def _on_frame(self, message):
        try:
            self.sample_callback(self.sensor, message)
            frame = self.frame_bytes(message, self.width, self.height, self.pixel_format)
            if self.frames.full():
                try: self.frames.get_nowait()
                except queue.Empty: pass
            self.frames.put_nowait(frame)
        except Exception:
            log.exception("Camera frame callback failed")

    def _write_frames(self):
        try:
            while not self.stop_event.is_set() and self.process and self.process.poll() is None:
                try: frame = self.frames.get(timeout=.25)
                except queue.Empty: continue
                self.process.stdin.write(frame)
        except (BrokenPipeError, OSError, AttributeError):
            pass

    def stop(self):
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        if self._subscribed:
            try: self.client.unsubscribe(self.sensor.topic)
            except Exception: pass
            self._subscribed = False
        process = self.process
        if process:
            try:
                if process.stdin: process.stdin.close()
                process.wait(timeout=3)
            except Exception:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired: process.kill()
        if self.writer:
            self.writer.join(timeout=1)
