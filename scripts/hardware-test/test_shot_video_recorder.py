#!/usr/bin/env python3
"""
Camera Module 3 Wide shot-video-recorder hardware test.

Starts the real ShotVideoRecorder (picamera2 + the actual circular H.264
buffer), waits for the buffer to fill, then simulates one or more shots by
calling save_clip() with a synthetic impact timestamp - exactly the flow
server.py uses in production, just without needing a radar/sound trigger.

After each clip, runs ffprobe on the result and prints whether it found a
real video stream - this is the quickest way to confirm a fresh recorder.py
change didn't reintroduce the "valid MP4 container, zero video streams"
bug (missing SPS/PPS) or the "trim seeks past EOF" bug (buffer not yet
full), both of which previously produced an empty-looking clip.

Prerequisites:
    Raspberry Pi with Camera Module 3 (Wide) on a CSI/MIPI port.
    python3-picamera2 + ffmpeg installed (scripts/setup/setup.sh does this).

Usage:
    # Single shot, default settings
    python scripts/hardware-test/test_shot_video_recorder.py

    # Simulate a slow shot (3s of "processing" between impact and save_clip)
    python scripts/hardware-test/test_shot_video_recorder.py --processing-delay 3

    # Take 3 shots in a row, 5s apart, keep the intermediate .h264 files
    python scripts/hardware-test/test_shot_video_recorder.py --shots 3 --keep-raw

    # Trigger immediately after start() instead of waiting for the buffer
    # to fill - reproduces the "buffer not yet full" scenario directly
    python scripts/hardware-test/test_shot_video_recorder.py --warmup 0
"""

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from openflight.camera.recorder import RecorderConfig, ShotVideoRecorder


def probe_clip(path: Path) -> None:
    """Print ffprobe's verdict on whether the clip has a real video stream."""
    if shutil.which("ffprobe") is None:
        print("  (ffprobe not on PATH - skipping verification)")
        return

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout.strip()
    if "codec_type=video" in output:
        print("  ffprobe: OK - real video stream found")
        for line in output.splitlines():
            print(f"    {line}")
    else:
        print("  ffprobe: ** NO VIDEO STREAM FOUND - clip is empty/broken **")
        if output:
            print(f"    {output}")
        if result.stderr.strip():
            print(f"    stderr: {result.stderr.strip()}")


def main():
    parser = argparse.ArgumentParser(
        description="Hardware test for ShotVideoRecorder (Camera Module 3 Wide)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
    python scripts/hardware-test/test_shot_video_recorder.py
    python scripts/hardware-test/test_shot_video_recorder.py --processing-delay 3
    python scripts/hardware-test/test_shot_video_recorder.py --shots 3 --keep-raw
        """,
    )
    parser.add_argument(
        "--width", type=int, default=None, help="Override RecorderConfig.width"
    )
    parser.add_argument(
        "--height", type=int, default=None, help="Override RecorderConfig.height"
    )
    parser.add_argument(
        "--fps", type=int, default=None, help="Override RecorderConfig.framerate"
    )
    parser.add_argument(
        "--pre-roll", type=float, default=None, help="Override RecorderConfig.pre_roll_s"
    )
    parser.add_argument(
        "--post-roll", type=float, default=None, help="Override RecorderConfig.post_roll_s"
    )
    parser.add_argument(
        "--warmup",
        type=float,
        default=12.0,
        help="Seconds to let the circular buffer fill before the first shot "
        "(default: 12, i.e. past the default 10s buffer_capacity_s). Use 0 "
        "to reproduce the 'buffer not yet full' scenario.",
    )
    parser.add_argument(
        "--processing-delay",
        type=float,
        default=0.5,
        help="Simulated seconds between 'impact' and save_clip() being called "
        "(default: 0.5, like a fast real pipeline)",
    )
    parser.add_argument(
        "--shots", type=int, default=1, help="Number of shots to simulate (default: 1)"
    )
    parser.add_argument(
        "--shot-interval",
        type=float,
        default=5.0,
        help="Seconds to wait between simulated shots (default: 5)",
    )
    parser.add_argument(
        "--keep-raw",
        action="store_true",
        help="Keep the intermediate .h264 file instead of deleting it",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Output directory (default: ~/openflight_sessions/videos/test_<timestamp>/)",
    )
    args = parser.parse_args()

    config_kwargs = {}
    if args.width is not None:
        config_kwargs["width"] = args.width
    if args.height is not None:
        config_kwargs["height"] = args.height
    if args.fps is not None:
        config_kwargs["framerate"] = args.fps
    if args.pre_roll is not None:
        config_kwargs["pre_roll_s"] = args.pre_roll
    if args.post_roll is not None:
        config_kwargs["post_roll_s"] = args.post_roll
    config = RecorderConfig(**config_kwargs)

    if args.output_dir:
        output_dir = Path(args.output_dir).expanduser().resolve()
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path.home() / "openflight_sessions" / "videos" / f"test_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  Shot Video Recorder Hardware Test")
    print("=" * 60)
    print(f"  Resolution:       {config.width}x{config.height} @ {config.framerate}fps")
    print(f"  Pre/post-roll:    {config.pre_roll_s}s / {config.post_roll_s}s")
    print(f"  Buffer capacity:  {config.buffer_capacity_s}s")
    print(f"  Warmup:           {args.warmup}s")
    print(f"  Processing delay: {args.processing_delay}s (simulated)")
    print(f"  Shots:            {args.shots}")
    print(f"  Output dir:       {output_dir}")
    print()

    recorder = ShotVideoRecorder(config)

    print("Starting recorder...")
    try:
        recorder.start()
    except Exception as e:
        print(f"Error starting recorder: {e}")
        sys.exit(1)
    print("  Recorder running.")
    print()

    try:
        if args.warmup > 0:
            print(f"Warming up ({args.warmup}s, letting the circular buffer fill)...")
            time.sleep(args.warmup)

        for shot_num in range(1, args.shots + 1):
            print(f"--- Shot {shot_num}/{args.shots} ---")
            impact_timestamp = time.time()
            if args.processing_delay > 0:
                print(f"  Simulating {args.processing_delay}s of upstream processing...")
                time.sleep(args.processing_delay)

            out_path = output_dir / f"shot_{shot_num:04d}.mp4"
            print(f"  Calling save_clip() -> {out_path}")
            try:
                recorder.save_clip(
                    out_path,
                    impact_timestamp=impact_timestamp,
                    keep_raw=args.keep_raw,
                )
            except Exception as e:
                print(f"  ERROR: save_clip() failed: {e}")
                continue

            size_kb = out_path.stat().st_size / 1024 if out_path.exists() else 0
            print(f"  Saved: {out_path} ({size_kb:.1f} KB)")
            if args.keep_raw:
                raw_path = out_path.with_suffix(".h264")
                if raw_path.exists():
                    print(f"  Raw stream kept: {raw_path} ({raw_path.stat().st_size / 1024:.1f} KB)")
            probe_clip(out_path)
            print()

            if shot_num < args.shots:
                time.sleep(args.shot_interval)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        print("Stopping recorder...")
        recorder.stop()
        print("Done.")


if __name__ == "__main__":
    main()
