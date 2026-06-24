"""Tests for the shot video recorder (camera/recorder.py)."""

import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from openflight.camera import MockShotVideoRecorder, RecorderConfig, ShotVideoRecorder
from openflight.camera.recorder import _mux_h264_to_mp4


class TestRecorderConfig:
    def test_default_config(self):
        config = RecorderConfig()
        assert config.width == 1332
        assert config.height == 990
        assert config.framerate == 50
        assert config.pre_roll_s == 2.0
        assert config.post_roll_s == 2.0


class TestMuxH264ToMp4:
    """save_clip() must remux the raw H.264 elementary stream into a real
    MP4 container - CircularOutput on its own writes a bare bitstream that
    ffprobe identifies as "h264", not "mp4", and most players reject."""

    def test_raises_when_ffmpeg_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(RuntimeError, match="ffmpeg not found"):
            _mux_h264_to_mp4(tmp_path / "in.h264", tmp_path / "out.mp4", framerate=50)

    def test_invokes_ffmpeg_with_copy_and_faststart(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        raw_path = tmp_path / "shot_0001.h264"
        out_path = tmp_path / "shot_0001.mp4"
        _mux_h264_to_mp4(raw_path, out_path, framerate=50)

        cmd = captured["cmd"]
        assert cmd[0] == "ffmpeg"
        assert "-r" in cmd and cmd[cmd.index("-r") + 1] == "50"
        assert "-i" in cmd and cmd[cmd.index("-i") + 1] == str(raw_path)
        assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
        assert "-movflags" in cmd and cmd[cmd.index("-movflags") + 1] == "+faststart"
        assert cmd[-1] == str(out_path)

    def test_raises_on_ffmpeg_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="bad input")

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(RuntimeError, match="bad input"):
            _mux_h264_to_mp4(tmp_path / "in.h264", tmp_path / "out.mp4", framerate=50)

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_real_ffmpeg_produces_an_actual_mp4_container(self, tmp_path):
        """End-to-end regression check: feed a real raw H.264 elementary
        stream through the mux step and confirm the output is a real MP4
        container (starts with an ftyp box), not a renamed raw stream."""
        raw_path = tmp_path / "raw.h264"
        out_path = tmp_path / "out.mp4"

        gen = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=64x64:rate=10:duration=0.3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "h264",
                str(raw_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if gen.returncode != 0:
            pytest.skip(f"ffmpeg couldn't generate a test H.264 stream: {gen.stderr}")

        _mux_h264_to_mp4(raw_path, out_path, framerate=10)

        header = out_path.read_bytes()[:32]
        assert b"ftyp" in header, "output should be a real MP4 container, not a raw stream"


class TestShotVideoRecorderSaveClip:
    """Exercise save_clip()'s remux flow without requiring real picamera2
    hardware - stub the circular-buffer output and the mux step."""

    class _FakeCircularOutput:
        def __init__(self):
            self.fileoutput = None
            self.started = False

        def start(self):
            # Simulate CircularOutput actually writing the raw stream.
            Path(self.fileoutput).write_bytes(b"\x00\x00\x00\x01\x65fake-nal")
            self.started = True

        def stop(self):
            self.started = False

    def _running_recorder(self) -> ShotVideoRecorder:
        recorder = ShotVideoRecorder.__new__(ShotVideoRecorder)
        recorder.config = RecorderConfig(framerate=50, post_roll_s=0.0)
        recorder._lock = threading.Lock()
        recorder._running = True
        recorder._output = self._FakeCircularOutput()
        recorder._camera = None
        recorder._encoder = None
        return recorder

    def test_save_clip_writes_to_temp_h264_then_muxes_and_cleans_up(self, tmp_path, monkeypatch):
        recorder = self._running_recorder()
        mux_calls = []

        def fake_mux(raw_path, out_path, framerate):
            mux_calls.append((raw_path, out_path, framerate))
            out_path.write_bytes(b"fake mp4 bytes")

        monkeypatch.setattr("openflight.camera.recorder._mux_h264_to_mp4", fake_mux)
        monkeypatch.setattr("openflight.camera.recorder.time.sleep", lambda _s: None)

        out_path = tmp_path / "videos" / "session1" / "shot_0001.mp4"
        result = recorder.save_clip(out_path)

        assert result == out_path
        assert out_path.exists()
        assert mux_calls == [(out_path.with_suffix(".h264"), out_path, 50)]
        # The intermediate raw .h264 file must not be left behind.
        assert not out_path.with_suffix(".h264").exists()

    def test_save_clip_cleans_up_temp_file_even_if_mux_fails(self, tmp_path, monkeypatch):
        recorder = self._running_recorder()

        def failing_mux(raw_path, out_path, framerate):
            raise RuntimeError("ffmpeg exploded")

        monkeypatch.setattr("openflight.camera.recorder._mux_h264_to_mp4", failing_mux)
        monkeypatch.setattr("openflight.camera.recorder.time.sleep", lambda _s: None)

        out_path = tmp_path / "shot_0002.mp4"
        with pytest.raises(RuntimeError, match="ffmpeg exploded"):
            recorder.save_clip(out_path)

        assert not out_path.with_suffix(".h264").exists()

    def test_save_clip_raises_when_not_running(self, tmp_path):
        recorder = ShotVideoRecorder.__new__(ShotVideoRecorder)
        recorder._running = False
        recorder._output = None
        with pytest.raises(RuntimeError, match="not running"):
            recorder.save_clip(tmp_path / "shot_0001.mp4")


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
