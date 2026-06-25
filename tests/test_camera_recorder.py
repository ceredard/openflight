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
        assert config.width == 1536
        assert config.height == 864
        assert config.framerate == 50
        assert config.pre_roll_s == 2.0
        assert config.post_roll_s == 2.0
        assert config.max_processing_delay_s == 8.0

    def test_buffer_capacity_is_pre_roll_plus_processing_delay_cushion(self):
        config = RecorderConfig(pre_roll_s=2.0, max_processing_delay_s=8.0)
        assert config.buffer_capacity_s == 10.0


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
        assert "-ss" not in cmd
        assert "-t" not in cmd

    def test_omits_trim_flags_when_trim_start_is_zero(self, tmp_path, monkeypatch):
        """A trim_start_s of exactly 0.0 means no trim is needed - don't
        emit a no-op -ss 0."""
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        _mux_h264_to_mp4(
            tmp_path / "in.h264", tmp_path / "out.mp4", framerate=50, trim_start_s=0.0
        )

        assert "-ss" not in captured["cmd"]

    def test_includes_trim_flags_when_trim_start_and_duration_given(self, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/ffmpeg")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        raw_path = tmp_path / "in.h264"
        _mux_h264_to_mp4(
            raw_path,
            tmp_path / "out.mp4",
            framerate=50,
            trim_start_s=7.3,
            clip_duration_s=4.0,
        )

        cmd = captured["cmd"]
        # -ss after -i (output seeking) - ffmpeg's raw h264 demuxer can't
        # input-seek at all ("could not seek to position ..."), confirmed
        # against a real Pi-encoded stream, and silently produces an empty
        # file rather than erroring.
        assert cmd.index("-ss") > cmd.index("-i")
        assert cmd[cmd.index("-ss") + 1] == "7.3"
        assert cmd[cmd.index("-t") + 1] == "4.0"

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

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
    def test_real_ffmpeg_trim_produces_a_non_empty_clip(self, tmp_path):
        """Regression test for the actual bug hit on hardware: input
        seeking (-ss before -i) on a real raw h264 stream fails outright
        ("could not seek to position ...") and silently writes an empty
        but structurally valid MP4 - camera/encoder/buffer all working
        perfectly, only the trim step broken. Generates a longer stream
        with periodic keyframes (like the real encoder's iperiod) and
        confirms a trimmed mux actually produces playable video, not just
        a valid-looking empty container."""
        raw_path = tmp_path / "raw.h264"
        out_path = tmp_path / "out.mp4"

        gen = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=64x64:rate=10:duration=3",
                "-c:v",
                "libx264",
                "-g",
                "10",  # keyframe every 1s at 10fps, like iperiod=framerate
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

        _mux_h264_to_mp4(raw_path, out_path, framerate=10, trim_start_s=1.5, clip_duration_s=1.0)

        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        duration = float(probe.stdout.strip() or 0)
        assert duration > 0, (
            "trimmed clip has zero duration - this is the exact 'valid "
            "container, no playable content' bug from input-seek failing"
        )


class TestShotVideoRecorderStart:
    """start() must size the circular buffer off buffer_capacity_s (the
    processing-delay cushion), not just pre_roll_s, or a slow shot can
    scroll the swing out of the buffer before save_clip() ever runs. It
    must also configure the encoder to repeat SPS/PPS headers, or a
    circular-buffer dump starting mid-session has no parseable sequence
    header at all (real-world symptom: a structurally valid MP4 with zero
    video streams, since ffmpeg's raw h264 demuxer can't identify any
    frames without it)."""

    def test_start_sizes_circular_output_from_buffer_capacity(self, monkeypatch):
        import openflight.camera.recorder as recorder_module

        captured = {}

        class FakeCamera:
            def create_video_configuration(self, **kwargs):
                return {}

            def configure(self, video_config):
                pass

            def start(self):
                pass

            def start_encoder(self, encoder, output):
                pass

        class FakeCircularOutput:
            def __init__(self, buffersize):
                captured["buffersize"] = buffersize

        monkeypatch.setattr(recorder_module, "PICAMERA_AVAILABLE", True)
        monkeypatch.setattr(recorder_module, "Picamera2", FakeCamera)
        monkeypatch.setattr(
            recorder_module,
            "H264Encoder",
            lambda **kwargs: captured.setdefault("encoder_kwargs", kwargs),
        )
        monkeypatch.setattr(recorder_module, "CircularOutput", FakeCircularOutput)

        config = RecorderConfig(framerate=50, pre_roll_s=2.0, max_processing_delay_s=8.0)
        recorder = ShotVideoRecorder(config)
        recorder.start()

        # buffer_capacity_s (10.0) * framerate (50) = 500 frames, not just
        # pre_roll_s (2.0) * framerate (50) = 100 frames.
        assert captured["buffersize"] == 500

    def test_start_configures_encoder_to_repeat_sps_pps(self, monkeypatch):
        import openflight.camera.recorder as recorder_module

        captured = {}

        class FakeCamera:
            def create_video_configuration(self, **kwargs):
                return {}

            def configure(self, video_config):
                pass

            def start(self):
                pass

            def start_encoder(self, encoder, output):
                pass

        monkeypatch.setattr(recorder_module, "PICAMERA_AVAILABLE", True)
        monkeypatch.setattr(recorder_module, "Picamera2", FakeCamera)
        monkeypatch.setattr(
            recorder_module,
            "H264Encoder",
            lambda **kwargs: captured.setdefault("encoder_kwargs", kwargs),
        )
        monkeypatch.setattr(recorder_module, "CircularOutput", lambda buffersize: None)

        recorder = ShotVideoRecorder(RecorderConfig(framerate=50))
        recorder.start()

        assert captured["encoder_kwargs"]["repeat"] is True
        # Frequent keyframes keep save_clip()'s output-seek trim (which can
        # only cut at a keyframe) close to the requested start time.
        assert captured["encoder_kwargs"]["iperiod"] == 50

    def test_start_starts_camera_before_attaching_encoder(self, monkeypatch):
        """Regression test: starting the encoder (VIDIOC_STREAMON on the
        V4L2 M2M hardware encoder) before the camera pipeline is actually
        producing frames can fail at the ioctl level (observed as
        ProcessLookupError: [Errno 3] No such process on a Pi 4). picamera2's
        own circular-buffer example calls start() before start_encoder()
        rather than the combined start_recording() - this must too."""
        import openflight.camera.recorder as recorder_module

        calls = []

        class FakeCamera:
            def create_video_configuration(self, **kwargs):
                return {}

            def configure(self, video_config):
                pass

            def start(self):
                calls.append("start")

            def start_encoder(self, encoder, output):
                calls.append("start_encoder")

        monkeypatch.setattr(recorder_module, "PICAMERA_AVAILABLE", True)
        monkeypatch.setattr(recorder_module, "Picamera2", FakeCamera)
        monkeypatch.setattr(recorder_module, "H264Encoder", lambda **kwargs: None)
        monkeypatch.setattr(recorder_module, "CircularOutput", lambda buffersize: None)

        recorder = ShotVideoRecorder(RecorderConfig())
        recorder.start()

        assert calls == ["start", "start_encoder"]


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
        # Long-running session by default, so the ring buffer is already
        # fully populated to buffer_capacity_s - matches what most tests
        # below are exercising. Tests of the "buffer not yet full" case
        # override this explicitly.
        recorder._started_at = 0.0
        return recorder

    def test_save_clip_writes_to_temp_h264_then_muxes_and_cleans_up(self, tmp_path, monkeypatch):
        recorder = self._running_recorder()
        mux_calls = []

        def fake_mux(raw_path, out_path, framerate, trim_start_s=None, clip_duration_s=None):
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

        def failing_mux(raw_path, out_path, framerate, trim_start_s=None, clip_duration_s=None):
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

    def test_save_clip_shortens_post_roll_by_processing_delay_since_impact(
        self, tmp_path, monkeypatch
    ):
        """If save_clip always slept the full post_roll_s from "now", the
        impact moment would drift later into the live recording the slower
        upstream processing (FFT/spin/K-LD7/ballistics) got. Passing
        impact_timestamp lets save_clip subtract the elapsed delay from the
        post-roll sleep instead, since that span is already sitting in the
        buffer from when it was recorded live the first time around."""
        recorder = self._running_recorder()
        recorder.config = RecorderConfig(
            framerate=50, post_roll_s=2.0, pre_roll_s=2.0, max_processing_delay_s=8.0
        )
        monkeypatch.setattr("openflight.camera.recorder._mux_h264_to_mp4", lambda *a, **k: None)

        sleep_calls = []
        monkeypatch.setattr(
            "openflight.camera.recorder.time.sleep", lambda s: sleep_calls.append(s)
        )
        # 0.7s of processing already elapsed between impact and this call.
        monkeypatch.setattr("openflight.camera.recorder.time.time", lambda: 1000.7)

        recorder.save_clip(tmp_path / "shot_0001.mp4", impact_timestamp=1000.0)

        assert sleep_calls == [pytest.approx(1.3)]

    def test_save_clip_clamps_post_roll_to_zero_when_delay_exceeds_post_roll(
        self, tmp_path, monkeypatch
    ):
        recorder = self._running_recorder()
        recorder.config = RecorderConfig(
            framerate=50, post_roll_s=2.0, pre_roll_s=2.0, max_processing_delay_s=8.0
        )
        monkeypatch.setattr("openflight.camera.recorder._mux_h264_to_mp4", lambda *a, **k: None)

        sleep_calls = []
        monkeypatch.setattr(
            "openflight.camera.recorder.time.sleep", lambda s: sleep_calls.append(s)
        )
        # 5s of processing delay - already well past the post-roll window,
        # but still well inside the buffer's processing-delay cushion.
        monkeypatch.setattr("openflight.camera.recorder.time.time", lambda: 1005.0)

        recorder.save_clip(tmp_path / "shot_0001.mp4", impact_timestamp=1000.0)

        assert sleep_calls == [0.0]

    def test_save_clip_trims_buffer_to_a_clip_anchored_on_impact(self, tmp_path, monkeypatch):
        """This is the fix for clips drifting ~6s behind the actual swing:
        the buffer holds buffer_capacity_s (pre_roll_s + cushion) of
        history, so the impact sits buried inside it once processing has
        eaten into the cushion. trim_start_s/clip_duration_s must locate
        the impact within that buffer and cut a consistent pre_roll_s +
        post_roll_s window around it, regardless of how slow processing
        was for this particular shot."""
        recorder = self._running_recorder()
        recorder.config = RecorderConfig(
            framerate=50, post_roll_s=2.0, pre_roll_s=2.0, max_processing_delay_s=8.0
        )
        mux_calls = []
        monkeypatch.setattr(
            "openflight.camera.recorder._mux_h264_to_mp4",
            lambda *a, **k: mux_calls.append(k),
        )
        monkeypatch.setattr("openflight.camera.recorder.time.sleep", lambda _s: None)
        # 6s of processing delay - the scenario reported as "video is ~6s
        # behind". buffer_capacity_s is 10s (2 + 8 cushion), so the impact
        # is still inside the buffer, 6s back from "now".
        monkeypatch.setattr("openflight.camera.recorder.time.time", lambda: 1006.0)

        recorder.save_clip(tmp_path / "shot_0001.mp4", impact_timestamp=1000.0)

        assert len(mux_calls) == 1
        assert mux_calls[0]["trim_start_s"] == pytest.approx(2.0)
        assert mux_calls[0]["clip_duration_s"] == pytest.approx(4.0)

    def test_save_clip_clamps_trim_start_when_delay_exceeds_buffer_cushion(
        self, tmp_path, monkeypatch
    ):
        """If processing took longer than the buffer can hold at all, the
        earliest available frame is the best we can do - clamp to 0 rather
        than requesting a negative seek."""
        recorder = self._running_recorder()
        recorder.config = RecorderConfig(
            framerate=50, post_roll_s=2.0, pre_roll_s=2.0, max_processing_delay_s=8.0
        )
        mux_calls = []
        monkeypatch.setattr(
            "openflight.camera.recorder._mux_h264_to_mp4",
            lambda *a, **k: mux_calls.append(k),
        )
        monkeypatch.setattr("openflight.camera.recorder.time.sleep", lambda _s: None)
        # 15s of delay - beyond the 10s buffer capacity entirely.
        monkeypatch.setattr("openflight.camera.recorder.time.time", lambda: 1015.0)

        recorder.save_clip(tmp_path / "shot_0001.mp4", impact_timestamp=1000.0)

        assert len(mux_calls) == 1
        assert mux_calls[0]["trim_start_s"] == 0.0
        assert mux_calls[0]["clip_duration_s"] == pytest.approx(4.0)

    def test_save_clip_clamps_trim_start_when_buffer_not_yet_full(self, tmp_path, monkeypatch):
        """Regression test: right after start() (or in a short test script),
        the ring buffer holds less than buffer_capacity_s of real footage.
        Sizing trim_start_s off the theoretical max buffer_capacity_s would
        seek -ss past the end of the much shorter real .h264 file - ffmpeg
        then writes a structurally valid but empty MP4 (no video stream,
        Duration: N/A), even though the encoded frames are otherwise fine."""
        recorder = self._running_recorder()
        recorder.config = RecorderConfig(
            framerate=50, post_roll_s=2.0, pre_roll_s=2.0, max_processing_delay_s=8.0
        )
        # Recorder has only been running 3s - buffer_capacity_s is 10s, so
        # it's nowhere near full yet.
        recorder._started_at = 1000.0
        mux_calls = []
        monkeypatch.setattr(
            "openflight.camera.recorder._mux_h264_to_mp4",
            lambda *a, **k: mux_calls.append(k),
        )
        monkeypatch.setattr("openflight.camera.recorder.time.sleep", lambda _s: None)
        # "Now" is 3s after start(), 0.5s after impact.
        monkeypatch.setattr("openflight.camera.recorder.time.time", lambda: 1003.0)

        recorder.save_clip(tmp_path / "shot_0001.mp4", impact_timestamp=1002.5)

        assert len(mux_calls) == 1
        # available_buffer_s = min(10, 3) = 3 -> trim_start_s = 3 - 0.5 - 2 = 0.5,
        # safely within the ~3s of real footage. The old buffer_capacity_s=10
        # assumption would have produced 7.5, well past EOF.
        assert mux_calls[0]["trim_start_s"] == pytest.approx(0.5)

    def test_save_clip_uses_buffer_capacity_once_recorder_has_run_long_enough(
        self, tmp_path, monkeypatch
    ):
        """Once the recorder has been running at least buffer_capacity_s,
        the ring buffer really is full - trim_start_s should use the full
        theoretical span again, matching test_save_clip_trims_buffer_to_a_clip_anchored_on_impact."""
        recorder = self._running_recorder()
        recorder.config = RecorderConfig(
            framerate=50, post_roll_s=2.0, pre_roll_s=2.0, max_processing_delay_s=8.0
        )
        recorder._started_at = 0.0
        mux_calls = []
        monkeypatch.setattr(
            "openflight.camera.recorder._mux_h264_to_mp4",
            lambda *a, **k: mux_calls.append(k),
        )
        monkeypatch.setattr("openflight.camera.recorder.time.sleep", lambda _s: None)
        monkeypatch.setattr("openflight.camera.recorder.time.time", lambda: 1006.0)

        recorder.save_clip(tmp_path / "shot_0001.mp4", impact_timestamp=1000.0)

        assert len(mux_calls) == 1
        assert mux_calls[0]["trim_start_s"] == pytest.approx(2.0)


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
