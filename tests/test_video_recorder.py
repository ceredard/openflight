"""Tests for shot replay video recording."""

from pathlib import Path
from unittest.mock import patch

import pytest

from openflight.camera.video_recorder import (
    PICAMERA_VIDEO_AVAILABLE,
    MockVideoRecorder,
    VideoRecorder,
    VideoRecorderConfig,
    _mux_to_mp4,
)


class TestVideoRecorderConfig:
    """Tests for VideoRecorderConfig dataclass."""

    def test_default_config(self):
        """Default config should match Camera Module 3 shot-replay settings."""
        config = VideoRecorderConfig()
        assert config.width == 1536
        assert config.height == 864
        assert config.framerate == 120
        assert config.pre_roll_seconds == 3.0
        assert config.post_roll_seconds == 2.0
        assert config.output_dir == Path.home() / "openflight_videos"

    def test_custom_config(self):
        """Custom config values should be respected."""
        config = VideoRecorderConfig(
            width=640, height=480, framerate=60, pre_roll_seconds=1.0, post_roll_seconds=0.5
        )
        assert config.width == 640
        assert config.height == 480
        assert config.framerate == 60

    def test_buffer_size_frames(self):
        """Buffer size should be pre-roll seconds * framerate."""
        config = VideoRecorderConfig(framerate=120, pre_roll_seconds=3.0)
        assert config.buffer_size_frames == 360

    def test_buffer_size_frames_minimum_one(self):
        """Buffer size should never be zero, even with a tiny pre-roll."""
        config = VideoRecorderConfig(framerate=120, pre_roll_seconds=0.0)
        assert config.buffer_size_frames == 1


class TestMuxToMp4:
    """Tests for the ffmpeg H.264 -> MP4 muxing helper."""

    def test_mux_success_removes_h264(self, tmp_path):
        """A successful mux should remove the intermediate .h264 file."""
        h264_path = tmp_path / "clip.h264"
        mp4_path = tmp_path / "clip.mp4"
        h264_path.write_bytes(b"fake-h264-data")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = None
            assert _mux_to_mp4(h264_path, mp4_path, framerate=120) is True

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "ffmpeg"
        assert "-r" in args
        assert str(h264_path) in args
        assert str(mp4_path) in args
        assert not h264_path.exists()

    def test_mux_failure_keeps_h264(self, tmp_path):
        """A failed mux should keep the .h264 file and return False."""
        import subprocess

        h264_path = tmp_path / "clip.h264"
        mp4_path = tmp_path / "clip.mp4"
        h264_path.write_bytes(b"fake-h264-data")

        with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")):
            assert _mux_to_mp4(h264_path, mp4_path, framerate=120) is False

        assert h264_path.exists()

    def test_mux_missing_ffmpeg(self, tmp_path):
        """A missing ffmpeg binary should be handled gracefully."""
        h264_path = tmp_path / "clip.h264"
        mp4_path = tmp_path / "clip.mp4"
        h264_path.write_bytes(b"fake-h264-data")

        with patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg not found")):
            assert _mux_to_mp4(h264_path, mp4_path, framerate=120) is False

        assert h264_path.exists()


class TestMockVideoRecorder:
    """Tests for MockVideoRecorder."""

    def test_start_stop(self):
        """Mock recorder should track running state."""
        recorder = MockVideoRecorder()
        assert recorder.is_running is False

        assert recorder.start() is True
        assert recorder.is_running is True

        recorder.stop()
        assert recorder.is_running is False

    def test_save_clip_async_returns_none(self):
        """Mock recorder has no footage, so saving a clip returns None."""
        recorder = MockVideoRecorder()
        recorder.start()

        assert recorder.save_clip_async("session_shot0001") is None

    def test_context_manager(self):
        """Mock recorder should support use as a context manager."""
        with MockVideoRecorder() as recorder:
            assert recorder.is_running is True
        assert recorder.is_running is False


@pytest.mark.skipif(PICAMERA_VIDEO_AVAILABLE, reason="picamera2 is available in this environment")
class TestVideoRecorderUnavailable:
    """Tests for VideoRecorder when picamera2 is not installed."""

    def test_init_raises_without_picamera(self):
        """Constructing a VideoRecorder without picamera2 should raise ImportError."""
        with pytest.raises(ImportError):
            VideoRecorder()


@pytest.mark.skipif(not PICAMERA_VIDEO_AVAILABLE, reason="picamera2 not available")
class TestVideoRecorderSaveClip:
    """Tests for VideoRecorder.save_clip_async (requires picamera2)."""

    def _make_recorder(self, tmp_path):
        config = VideoRecorderConfig(post_roll_seconds=0.01, output_dir=tmp_path)
        recorder = VideoRecorder(config)
        recorder._running = True  # pylint: disable=protected-access
        recorder._output = type(  # pylint: disable=protected-access
            "FakeOutput", (), {"fileoutput": None, "start": lambda self: None, "stop": lambda self: None}
        )()
        return recorder

    def test_save_clip_returns_mp4_filename(self, tmp_path):
        """Saving a clip should return the expected MP4 filename."""
        recorder = self._make_recorder(tmp_path)
        with patch("openflight.camera.video_recorder._mux_to_mp4", return_value=True):
            filename = recorder.save_clip_async("session_shot0001")
        assert filename == "session_shot0001.mp4"

    def test_concurrent_save_returns_none(self, tmp_path):
        """A second clip request while one is saving should be skipped."""
        recorder = self._make_recorder(tmp_path)
        recorder._save_lock.acquire()  # pylint: disable=protected-access

        assert recorder.save_clip_async("session_shot0002") is None
