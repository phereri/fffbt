"""Host-side video preparation and device transfer tools."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from src.worker.tools._adb import push as adb_push, shell as adb_shell
from src.worker.tools._types import ToolResult


def _run_subprocess(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _ffmpeg_path() -> str:
    return os.environ.get("FFMPEG_PATH", "ffmpeg")


def _ffprobe_video_meta(source: Path) -> dict[str, Any]:
    ffmpeg = _ffmpeg_path()
    ffprobe = ffmpeg
    if Path(ffprobe).name.lower().startswith("ffmpeg"):
        ffprobe = str(
            Path(ffprobe).with_name(
                Path(ffprobe).name.lower().replace("ffmpeg", "ffprobe")
            )
        )
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,profile,pix_fmt,bits_per_raw_sample,width,height,r_frame_rate",
        "-of",
        "json",
        str(source),
    ]
    proc = _run_subprocess(cmd, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    payload = json.loads(proc.stdout or "{}")
    streams = payload.get("streams") or []
    return streams[0] if streams else {}


def _is_android_friendly(meta: dict[str, Any]) -> bool:
    """True if the video stream is safe for Android/Instagram (H.264+yuv420p+8bit)."""
    if (meta.get("codec_name") or "").lower() != "h264":
        return False
    pix_fmt = (meta.get("pix_fmt") or "").lower()
    if pix_fmt and pix_fmt != "yuv420p":
        return False
    bps = meta.get("bits_per_raw_sample")
    try:
        if bps and int(bps) > 8:
            return False
    except (TypeError, ValueError):
        return False
    return True


def prepare_video_for_android(
    source_path: str,
    target_path: str | None = None,
    *,
    overwrite: bool = False,
) -> ToolResult:
    """Ensure a video is H.264 / yuv420p / 8-bit / +faststart for Android.

    Idempotent: returns the source path untouched if already compliant.
    """
    src = Path(source_path).expanduser().resolve()
    if not src.is_file():
        return ToolResult.fail(f"video not found: {src}")

    try:
        meta = _ffprobe_video_meta(src)
    except Exception as e:
        return ToolResult.fail(f"ffprobe error: {e}")

    if _is_android_friendly(meta) and not overwrite:
        return ToolResult.ok(
            f"video already android-friendly: {src} "
            f"({meta.get('codec_name')}, {meta.get('pix_fmt')})"
        )

    dst = (
        Path(target_path).expanduser().resolve()
        if target_path
        else src.with_name(src.stem + "_h264_yuv420p" + src.suffix)
    )
    dst.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        _ffmpeg_path(),
        "-y" if overwrite or not dst.exists() else "-n",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-i",
        str(src),
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.0",
        "-movflags",
        "+faststart",
        "-c:a",
        "copy",
        str(dst),
    ]
    proc = _run_subprocess(cmd, timeout=900)
    if proc.returncode != 0:
        return ToolResult.fail(
            f"ffmpeg exit {proc.returncode}: {proc.stderr.strip()[:400]}"
        )
    return ToolResult.ok(f"transcoded -> {dst}")


MEDIA_REMOTE_DIR = "/sdcard/DCIM/Camera"


async def push_video_to_gallery(
    serial: str,
    local_path: str,
    remote_filename: str | None = None,
) -> ToolResult:
    """Push a video to the device gallery and trigger MediaScanner."""
    src = Path(local_path).expanduser().resolve()
    if not src.is_file():
        return ToolResult.fail(f"video not found: {src}")

    name = remote_filename or src.name
    remote = f"{MEDIA_REMOTE_DIR}/{name}"

    try:
        await adb_push(serial, str(src), remote)
    except Exception as e:
        return ToolResult.fail(f"adb push: {e}")

    scan_cmd = (
        "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
        f"-d {shlex.quote('file://' + remote)}"
    )
    try:
        await adb_shell(serial, scan_cmd)
    except Exception as e:
        return ToolResult.ok(f"pushed {remote} (media scan failed: {e})")
    return ToolResult.ok(f"pushed and scanned {remote}")
