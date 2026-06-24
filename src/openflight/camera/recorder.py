"""
Per-shot video recording for a dedicated Raspberry Pi Camera Module 3 Wide.

This is a separate, dedicated recording camera from the Hough/YOLO ball-tracking
camera in capture.py/camera_tracker.py. It continuously records to an in-memory
H.264 circular buffer so that, on a shot trigger, the pre-roll (swing) and a
post-roll window (ball flight) can be flushed to a single mp4 file.
"""

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import CircularOutput

    PICAMERA_AVAILABLE = True
except ImportError:
    PICAMERA_AVAILABLE = False


def _mux_h264_to_mp4(raw_h264_path: Path, out_path: Path, framerate: int) -> None:
    """Remux a raw H.264 elementary stream into a real MP4 container.

    picamera2's CircularOutput writes the bare encoded bitstream with no
    container, headers, or timestamps - ffprobe identifies it as raw "h264"
    rather than "mp4", and standard players (VLC, Windows Media Player)
    reject it even though the encoded frames themselves are fine. This
    matches the `ffmpeg -i input.mp4 -c copy output.mp4` fix that confirms
    the underlying video data is valid; it just needs a container.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH - required to mux shot clips into a "
            "playable MP4. Install it with: sudo apt install ffmpeg"
        )

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-r",
            str(framerate),
            "-i",
            str(raw_h264_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mux to MP4 failed (exit {result.returncode}): {result.stderr}")


@dataclass
class RecorderConfig:
    """Configuration for the shot video recorder."""

    width: int = 1332
    height: int = 990
    framerate: int = 50

    # Pre-roll (swing) and post-roll (ball flight) clip duration.
    pre_roll_s: float = 2.0
    post_roll_s: float = 2.0

    bitrate: int = 8_000_000


class ShotVideoRecorder:
    """
    Continuously records H.264 video to a circular buffer using the hardware
    encoder, and saves a pre-roll + post-roll clip to disk on save_clip().

    Example:
        recorder = ShotVideoRecorder()
        recorder.start()

        # On shot detection:
        recorder.save_clip(Path("shot_0001.mp4"))

        recorder.stop()
    """

    def __init__(self, config: Optional[RecorderConfig] = None):
        self.config = config or RecorderConfig()
        self._camera: Optional["Picamera2"] = None
        self._encoder: Optional["H264Encoder"] = None
        self._output: Optional["CircularOutput"] = None
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> bool:
        """Start continuous recording into the circular buffer."""
        if not PICAMERA_AVAILABLE:
            raise RuntimeError("picamera2 not available. Install with: pip install picamera2")

        self._camera = Picamera2()
        video_config = self._camera.create_video_configuration(
            main={"size": (self.config.width, self.config.height)},
            controls={"FrameRate": self.config.framerate},
        )
        self._camera.configure(video_config)

        self._encoder = H264Encoder(bitrate=self.config.bitrate)
        buffer_size_s = self.config.pre_roll_s
        self._output = CircularOutput(
            buffersize=int(buffer_size_s * self.config.framerate)
        )

        self._camera.start_recording(self._encoder, self._output)
        self._running = True
        return True

    def stop(self):
        """Stop recording and release the camera."""
        if self._camera:
            self._camera.stop_recording()
            self._camera.close()
        self._camera = None
        self._encoder = None
        self._output = None
        self._running = False

    def save_clip(
        self,
        out_path: Path,
        post_roll_s: Optional[float] = None,
        impact_timestamp: Optional[float] = None,
    ) -> Path:
        """
        Flush the pre-roll buffer and continue recording for post_roll_s,
        saving the combined clip to out_path as a playable MP4.

        The pre-roll buffer is flushed relative to "now", not to the actual
        swing - callers normally invoke this only after upstream processing
        (FFT/spin/K-LD7/ballistics) has already spent some time since the
        physical impact. Pass impact_timestamp (the epoch time of impact) so
        that delay is subtracted from the post-roll sleep, keeping the
        impact anchored ~pre_roll_s seconds into the clip instead of
        drifting earlier as processing gets slower.

        Must be called off the main/socket thread - this blocks for the
        duration of the (possibly shortened) post-roll sleep, plus the
        remux step.
        """
        if not self._running or not self._output:
            raise RuntimeError("Recorder is not running")

        post_roll = self.config.post_roll_s if post_roll_s is None else post_roll_s
        if impact_timestamp is not None:
            elapsed_since_impact = time.time() - impact_timestamp
            if elapsed_since_impact > self.config.pre_roll_s:
                logger.warning(
                    "Shot video processing delay (%.2fs) exceeded pre_roll_s "
                    "(%.2fs) - impact frame may already be evicted from the "
                    "circular buffer",
                    elapsed_since_impact,
                    self.config.pre_roll_s,
                )
            post_roll = max(0.0, post_roll - elapsed_since_impact)

        out_path.parent.mkdir(parents=True, exist_ok=True)

        # CircularOutput only writes a raw H.264 elementary stream - capture
        # to a sibling .h264 file, then remux into a real MP4 container.
        raw_path = out_path.with_suffix(".h264")

        with self._lock:
            self._output.fileoutput = str(raw_path)
            self._output.start()
            time.sleep(post_roll)
            self._output.stop()

        try:
            _mux_h264_to_mp4(raw_path, out_path, framerate=self.config.framerate)
        finally:
            raw_path.unlink(missing_ok=True)

        return out_path

    @property
    def is_running(self) -> bool:
        """Whether continuous recording is active."""
        return self._running

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


class MockShotVideoRecorder:
    """Mock recorder for testing/dev without a Camera Module 3 attached."""

    def __init__(self, config: Optional[RecorderConfig] = None):
        self.config = config or RecorderConfig()
        self._running = False
        self.saved_clips: list = []

    def start(self) -> bool:
        self._running = True
        return True

    def stop(self):
        self._running = False

    def save_clip(
        self,
        out_path: Path,
        post_roll_s: Optional[float] = None,
        impact_timestamp: Optional[float] = None,
    ) -> Path:
        if not self._running:
            raise RuntimeError("Recorder is not running")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"")
        self.saved_clips.append(out_path)
        return out_path

    @property
    def is_running(self) -> bool:
        return self._running

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False
