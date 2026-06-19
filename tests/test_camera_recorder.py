"""Tests for the shot video recorder (camera/recorder.py)."""

import pytest

from openflight.camera import MockShotVideoRecorder, RecorderConfig


class TestRecorderConfig:
    def test_default_config(self):
        config = RecorderConfig()
        assert config.width == 1332
        assert config.height == 990
        assert config.framerate == 50
        assert config.pre_roll_s == 2.0
        assert config.post_roll_s == 2.0


class TestMockShotVideoRecorder:
    def test_start_sets_running(self):
        recorder = MockShotVideoRecorder()
        assert not recorder.is_running
        recorder.start()
        assert recorder.is_running

    def test_stop_clears_running(self):
        recorder = MockShotVideoRecorder()
        recorder.start()
        recorder.stop()
        assert not recorder.is_running

    def test_save_clip_writes_file_and_tracks_it(self, tmp_path):
        recorder = MockShotVideoRecorder()
        recorder.start()

        out_path = tmp_path / "videos" / "session1" / "shot_0001.mp4"
        result = recorder.save_clip(out_path)

        assert result == out_path
        assert out_path.exists()
        assert recorder.saved_clips == [out_path]

    def test_save_clip_raises_when_not_running(self, tmp_path):
        recorder = MockShotVideoRecorder()
        with pytest.raises(RuntimeError):
            recorder.save_clip(tmp_path / "shot_0001.mp4")

    def test_context_manager_starts_and_stops(self):
        recorder = MockShotVideoRecorder()
        with recorder:
            assert recorder.is_running
        assert not recorder.is_running
