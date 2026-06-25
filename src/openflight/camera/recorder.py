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


def _mux_h264_to_mp4(
    raw_h264_path: Path,
    out_path: Path,
    framerate: int,
    trim_start_s: Optional[float] = None,
    clip_duration_s: Optional[float] = None,
) -> None:
    """Remux a raw H.264 elementary stream into a real MP4 container.

    picamera2's CircularOutput writes the bare encoded bitstream with no
    container, headers, or timestamps - ffprobe identifies it as raw "h264"
    rather than "mp4", and standard players (VLC, Windows Media Player)
    reject it even though the encoded frames themselves are fine. This
    matches the `ffmpeg -i input.mp4 -c copy output.mp4` fix that confirms
    the underlying video data is valid; it just needs a container.

    trim_start_s/clip_duration_s optionally cut the raw stream down to a
    fixed window (used to anchor the saved clip on the actual impact when
    the circular buffer is sized larger than pre_roll_s + post_roll_s - see
    ShotVideoRecorder.save_clip). Frame timestamps are synthesized from
    `-r framerate` on the raw stream, so the trim is deterministic even
    though the elementary stream itself carries no timestamps.

    -ss/-t are placed AFTER -i (output seeking), not before (input seeking).
    ffmpeg's raw h264 demuxer has no index to seek against - on a real
    Pi-encoded stream, input seeking fails outright ("could not seek to
    position ...") and silently produces an empty output file, even though
    the input itself is perfectly valid. Output seeking instead demuxes
    sequentially from the start and discards everything before trim_start_s,
    which works regardless of the demuxer's seek support - the only cost is
    reading through the whole buffer dump rather than fast-seeking into it,
    which is negligible at these clip sizes (tens of MB).

    With -c copy, output seeking can only cut at a keyframe - and unlike
    input seeking (which snaps to the keyframe at or before the target),
    output seeking snaps to the keyframe at or after it, so the clip can
    start a little later than requested but never earlier. H264Encoder is
    configured with a 1-second keyframe interval (see ShotVideoRecorder)
    specifically to keep that snap small.
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH - required to mux shot clips into a "
            "playable MP4. Install it with: sudo apt install ffmpeg"
        )

    cmd = ["ffmpeg", "-y", "-r", str(framerate), "-i", str(raw_h264_path)]
    if trim_start_s:
        cmd += ["-ss", str(trim_start_s)]
    if clip_duration_s:
        cmd += ["-t", str(clip_duration_s)]
    cmd += ["-c", "copy", "-movflags", "+faststart", str(out_path)]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg mux to MP4 failed (exit {result.returncode}): {result.stderr}")


@dataclass
class RecorderConfig:
    """Configuration for the shot video recorder."""

    # 1536x864 is the largest sensor mode confirmed (via rpicam-vid, on the
    # actual Pi 4 test rig) to sustain 50fps through the hardware H.264
    # encoder. The IMX708 Wide's 2304x1296 binned mode captures fine at the
    # ISP/sensor level, but the Pi 4's hardware encoder can't keep up with
    # it at 50fps - rpicam-vid itself fails with "failed to start output
    # streaming" at that resolution, independent of anything in this repo.
    width: int = 1536
    height: int = 864
    framerate: int = 50

    # Pre-roll (swing) and post-roll (ball flight) clip duration, anchored
    # on the shot's actual impact timestamp (see save_clip).
    pre_roll_s: float = 2.0
    post_roll_s: float = 2.0

    # Extra circular-buffer capacity beyond pre_roll_s, to survive the delay
    # between the physical impact and save_clip() actually being called
    # (FFT/spin/K-LD7/ballistics processing all run before that). Without
    # this cushion, a slow shot can scroll the swing right out of the
    # buffer before it's ever flushed to disk.
    max_processing_delay_s: float = 8.0

    bitrate: int = 14_000_000

    @property
    def buffer_capacity_s(self) -> float:
        """Total circular-buffer span: pre-roll plus the processing-delay cushion."""
        return self.pre_roll_s + self.max_processing_delay_s


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
        self._started_at: Optional[float] = None

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

        # repeat=True re-sends SPS/PPS (and forces a keyframe) periodically
        # rather than only once at the start of the live encode session.
        # Without it, a circular-buffer dump starting mid-session has no
        # parseable sequence header at all - ffmpeg's raw h264 demuxer then
        # can't identify any frames, producing a structurally valid but
        # entirely empty MP4 (zero streams, Duration: N/A) on mux. iperiod
        # (keyframe interval, in frames) is set to ~1s so save_clip()'s
        # output-seek trim (see _mux_h264_to_mp4) never has to snap more
        # than ~1s past the requested start.
        self._encoder = H264Encoder(
            bitrate=self.config.bitrate,
            repeat=True,
            iperiod=self.config.framerate,
        )
        self._output = CircularOutput(
            buffersize=int(self.config.buffer_capacity_s * self.config.framerate)
        )

        # Start the camera pipeline itself before attaching the encoder,
        # rather than the start_recording() convenience (which starts both
        # together) - this matches picamera2's own circular-buffer example.
        # Telling the V4L2 M2M encoder to stream on before the camera is
        # actually producing frames can fail at the ioctl level.
        self._camera.start()
        self._camera.start_encoder(self._encoder, self._output)
        self._running = True
        self._started_at = time.time()
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
        self._started_at = None

    def save_clip(
        self,
        out_path: Path,
        post_roll_s: Optional[float] = None,
        impact_timestamp: Optional[float] = None,
        keep_raw: bool = False,
    ) -> Path:
        """
        Flush the circular buffer and continue recording briefly, saving a
        pre_roll_s + post_roll_s clip anchored on the actual impact to
        out_path as a playable MP4.

        The buffer is flushed relative to "now", not to the actual swing -
        callers normally invoke this only after upstream processing
        (FFT/spin/K-LD7/ballistics) has already spent some time since the
        physical impact. Pass impact_timestamp (the epoch time of impact)
        so that delay can be located within the buffer and trimmed out,
        keeping the clip consistently framed around the impact instead of
        drifting later as processing gets slower. The buffer is sized with
        a cushion (config.max_processing_delay_s) specifically so this
        delay doesn't scroll the swing out of the buffer entirely.

        Must be called off the main/socket thread - this blocks for the
        duration of the (possibly shortened) post-roll sleep, plus the
        remux step.

        keep_raw: diagnostic-only - keeps the intermediate sibling .h264
        file instead of deleting it, so it can be inspected directly with
        ffprobe (e.g. to check whether it has a parseable sequence header)
        independent of the mux step.
        """
        if not self._running or not self._output:
            raise RuntimeError("Recorder is not running")

        post_roll = self.config.post_roll_s if post_roll_s is None else post_roll_s
        trim_start_s = None
        clip_duration_s = None
        if impact_timestamp is not None:
            now = time.time()
            elapsed_since_impact = now - impact_timestamp
            # The ring buffer only holds buffer_capacity_s once the recorder
            # has actually been running that long - right after start(), or
            # in a short test script, it holds less. Sizing the trim off the
            # theoretical max here would seek -ss past the end of the much
            # shorter real .h264 file, which ffmpeg copies as a silently
            # empty (but structurally valid) MP4 - zero streams, Duration N/A.
            recording_elapsed_s = (
                now - self._started_at if self._started_at is not None else self.config.buffer_capacity_s
            )
            available_buffer_s = min(self.config.buffer_capacity_s, recording_elapsed_s)
            if elapsed_since_impact > available_buffer_s - self.config.pre_roll_s:
                logger.warning(
                    "Shot video processing delay (%.2fs) exceeded the available "
                    "buffer cushion (%.2fs) - impact frame may already be evicted "
                    "from the circular buffer",
                    elapsed_since_impact,
                    available_buffer_s - self.config.pre_roll_s,
                )
            trim_start_s = max(0.0, available_buffer_s - elapsed_since_impact - self.config.pre_roll_s)
            clip_duration_s = self.config.pre_roll_s + post_roll
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
            _mux_h264_to_mp4(
                raw_path,
                out_path,
                framerate=self.config.framerate,
                trim_start_s=trim_start_s,
                clip_duration_s=clip_duration_s,
            )
        finally:
            if not keep_raw:
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
        keep_raw: bool = False,
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
