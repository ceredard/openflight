"""
Shot replay video recording using a circular pre-trigger buffer.

Designed for the Raspberry Pi Camera Module 3 (IMX708). The camera
continuously records H.264 video into an in-memory circular buffer. When a
shot is detected, the buffer (pre-roll) plus a short post-roll window is
flushed to disk and muxed into an MP4 for browser playback.
"""

import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import CircularOutput

    PICAMERA_VIDEO_AVAILABLE = True
except ImportError:
    PICAMERA_VIDEO_AVAILABLE = False


logger = logging.getLogger(__name__)


@dataclass
class VideoRecorderConfig:
    """Configuration for shot replay video recording."""

    width: int = 1536
    height: int = 864
    framerate: int = 120

    # How much pre-trigger footage to retain in the circular buffer.
    pre_roll_seconds: float = 3.0

    # How long to keep recording after a shot before saving the clip.
    post_roll_seconds: float = 2.0

    bitrate: int = 10_000_000

    output_dir: Path = field(default_factory=lambda: Path.home() / "openflight_videos")

    @property
    def buffer_size_frames(self) -> int:
        """Number of frames to retain in the circular pre-trigger buffer."""
        return max(1, int(self.pre_roll_seconds * self.framerate))


def _mux_to_mp4(h264_path: Path, mp4_path: Path, framerate: int) -> bool:
    """Mux a raw H.264 elementary stream into an MP4 container via ffmpeg."""
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-r",
                str(framerate),
                "-i",
                str(h264_path),
                "-c",
                "copy",
                str(mp4_path),
            ],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
        logger.warning("[VIDEO] Failed to mux %s to MP4: %s", h264_path, e)
        return False

    h264_path.unlink(missing_ok=True)
    return True


class VideoRecorder:
    """
    Records shot replay clips from the Pi Camera Module 3.

    Continuously records to an in-memory circular buffer. When
    save_clip_async() is called, the buffered pre-roll plus a post-roll
    window is written to disk and muxed into an MP4 in a background thread.
    """

    def __init__(self, config: Optional[VideoRecorderConfig] = None):
        if not PICAMERA_VIDEO_AVAILABLE:
            raise ImportError("picamera2 required: pip install picamera2")

        self.config = config or VideoRecorderConfig()
        self._picam2: Optional["Picamera2"] = None
        self._encoder: Optional["H264Encoder"] = None
        self._output: Optional["CircularOutput"] = None
        self._running = False
        self._save_lock = threading.Lock()

    def start(self) -> bool:
        """Start the camera and begin recording to the circular buffer."""
        try:
            self._picam2 = Picamera2()
            video_config = self._picam2.create_video_configuration(
                main={"size": (self.config.width, self.config.height)},
                controls={"FrameRate": self.config.framerate},
            )
            self._picam2.configure(video_config)

            self._encoder = H264Encoder(bitrate=self.config.bitrate)
            self._output = CircularOutput(buffersize=self.config.buffer_size_frames)

            self._picam2.start_recording(self._encoder, self._output)
            self._running = True
            return True
        except Exception as e:
            self._running = False
            raise RuntimeError(f"Failed to start video recorder: {e}") from e

    def stop(self):
        """Stop recording and release the camera."""
        if self._picam2 and self._running:
            try:
                self._picam2.stop_recording()
            except Exception:
                logger.warning("[VIDEO] Error stopping recording", exc_info=True)
        if self._picam2:
            try:
                self._picam2.close()
            except Exception:
                logger.warning("[VIDEO] Error closing camera", exc_info=True)

        self._picam2 = None
        self._encoder = None
        self._output = None
        self._running = False

    def save_clip_async(self, filename_stem: str) -> Optional[str]:
        """
        Save the current circular buffer plus a post-roll window as an MP4.

        Returns the MP4 filename (relative to config.output_dir) that will
        exist once the background save/mux completes, or None if no clip
        could be started (recorder not running, or a previous clip is still
        being saved).
        """
        if not self._running or not self._output:
            return None

        if not self._save_lock.acquire(blocking=False):  # pylint: disable=consider-using-with
            logger.warning(
                "[VIDEO] Skipping clip for %s: previous clip still saving", filename_stem
            )
            return None

        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        mp4_filename = f"{filename_stem}.mp4"
        h264_path = self.config.output_dir / f"{filename_stem}.h264"
        mp4_path = self.config.output_dir / mp4_filename

        self._output.fileoutput = str(h264_path)
        self._output.start()

        def _finish_clip():
            try:
                time.sleep(self.config.post_roll_seconds)
                try:
                    self._output.stop()
                except Exception:
                    logger.warning("[VIDEO] Error stopping clip output", exc_info=True)
                _mux_to_mp4(h264_path, mp4_path, self.config.framerate)
            finally:
                self._save_lock.release()

        threading.Thread(target=_finish_clip, daemon=True).start()
        return mp4_filename

    @property
    def is_running(self) -> bool:
        """Whether the camera is currently recording."""
        return self._running

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
        return False


class MockVideoRecorder:
    """Mock video recorder for testing/mock mode without camera hardware."""

    def __init__(self, config: Optional[VideoRecorderConfig] = None):
        """Initialize mock video recorder."""
        self.config = config or VideoRecorderConfig()
        self._running = False

    def start(self) -> bool:
        """Start mock recording."""
        self._running = True
        return True

    def stop(self):
        """Stop mock recording."""
        self._running = False

    def save_clip_async(self, filename_stem: str) -> Optional[str]:  # pylint: disable=unused-argument
        """Mock recorder has no footage to save."""
        return None

    @property
    def is_running(self) -> bool:
        """Whether the mock recorder is currently running."""
        return self._running

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.stop()
        return False
