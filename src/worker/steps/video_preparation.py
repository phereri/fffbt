"""video_preparation step — download, validate, transcode, push to device."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from src.worker.session.types import (
    Artifact,
    StepContext,
    StepName,
    StepResult,
    StepStatus,
    Warning,
)
from src.worker.tools._adb import shell as adb_shell
from src.worker.tools.video import (
    MEDIA_REMOTE_DIR,
    prepare_video_for_android,
    push_video_to_gallery,
)

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".mp4"}
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500 MB
MIN_FILE_SIZE_BYTES = 1024  # 1 KB — anything smaller is corrupt/empty
DEFAULT_DOWNLOAD_TIMEOUT = 300
MAX_RETRIES_TRANSIENT = 2


class VideoPreparationStep:
    """Runtime step: download video to VPS, validate, transcode, push to device."""

    name = StepName.VIDEO_PREPARATION

    async def run(self, ctx: StepContext, *, video_url: str | None = None, local_video_path: str | None = None, device_serial: str) -> StepResult:
        """Execute the video_preparation step.

        Accepts either a download URL or local path. Downloads if URL provided,
        validates extension/size, transcodes for Android, pushes to device gallery,
        and verifies existence on device.
        """
        download_timeout = ctx.settings.get("video_download_timeout", DEFAULT_DOWNLOAD_TIMEOUT)
        warnings: list[Warning] = []
        cleanup_paths: list[Path] = []

        try:
            # 1. Resolve local video file
            local_path = await self._resolve_video(
                video_url=video_url,
                local_video_path=local_video_path,
                timeout=download_timeout,
                cleanup_paths=cleanup_paths,
            )
            if local_path is None:
                return self._fail("INFRA", "no video_url or local_video_path provided")

            # 2. Validate extension and size
            err = self._validate(local_path)
            if err:
                return self._fail("INFRA", err)

            # 3. Transcode for Android (idempotent — skips if already compliant)
            transcode_result = prepare_video_for_android(str(local_path))
            if not transcode_result.success:
                return self._fail("INFRA", f"transcode: {transcode_result.message}")

            push_source = local_path
            if "transcoded ->" in transcode_result.message:
                transcoded_path = Path(transcode_result.message.split("transcoded -> ")[1])
                cleanup_paths.append(transcoded_path)
                push_source = transcoded_path

            # 4. Push to device with retry
            push_err = await self._push_with_retry(device_serial, push_source)
            if push_err:
                return self._fail("device_offline", f"push failed: {push_err}", retryable=True)

            # 5. Verify file exists on device
            remote_path = f"{MEDIA_REMOTE_DIR}/{push_source.name}"
            verified = await self._verify_on_device(device_serial, remote_path)
            if not verified:
                return self._fail("device_offline", f"file not found on device after push: {remote_path}", retryable=True)

            return StepResult(
                step=StepName.VIDEO_PREPARATION,
                status=StepStatus.OK,
                message=f"video ready on device: {remote_path}",
                warnings=warnings,
                artifacts=[],
            )

        except Exception as e:
            return self._fail("UNKNOWN", f"unhandled: {e}")

        finally:
            self._cleanup(cleanup_paths)

    async def _resolve_video(
        self,
        *,
        video_url: str | None,
        local_video_path: str | None,
        timeout: int,
        cleanup_paths: list[Path],
    ) -> Path | None:
        if local_video_path:
            p = Path(local_video_path).expanduser().resolve()
            if p.is_file():
                return p
            return None

        if video_url:
            return await self._download(video_url, timeout=timeout, cleanup_paths=cleanup_paths)

        return None

    async def _download(self, url: str, *, timeout: int, cleanup_paths: list[Path]) -> Path | None:
        """Download video to a temp file with retries on transient failure."""
        last_err: str | None = None
        for attempt in range(MAX_RETRIES_TRANSIENT + 1):
            try:
                dst = await asyncio.to_thread(self._download_sync, url, timeout)
                cleanup_paths.append(dst)
                return dst
            except Exception as e:
                last_err = str(e)
                logger.warning("download attempt %d failed: %s", attempt + 1, e)
        logger.error("download exhausted retries: %s", last_err)
        return None

    def _download_sync(self, url: str, timeout: int) -> Path:
        download_dir = os.environ.get("VIDEO_DOWNLOAD_DIR", tempfile.gettempdir())
        Path(download_dir).mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp4", dir=download_dir)
        try:
            resp = urlopen(url, timeout=timeout)  # noqa: S310
            with os.fdopen(tmp_fd, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        except Exception:
            os.unlink(tmp_path)
            raise
        return Path(tmp_path)

    def _validate(self, path: Path) -> str | None:
        ext = path.suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            return f"invalid extension '{ext}', allowed: {ALLOWED_EXTENSIONS}"
        size = path.stat().st_size
        if size < MIN_FILE_SIZE_BYTES:
            return f"file too small ({size} bytes), minimum {MIN_FILE_SIZE_BYTES}"
        if size > MAX_FILE_SIZE_BYTES:
            return f"file too large ({size} bytes), maximum {MAX_FILE_SIZE_BYTES}"
        return None

    async def _push_with_retry(self, serial: str, local_path: Path) -> str | None:
        """Push file to device gallery with retry on transient failure."""
        last_err: str | None = None
        for attempt in range(MAX_RETRIES_TRANSIENT + 1):
            result = await push_video_to_gallery(serial, str(local_path))
            if result.success:
                return None
            last_err = result.message
            logger.warning("push attempt %d failed: %s", attempt + 1, last_err)
        return last_err

    async def _verify_on_device(self, serial: str, remote_path: str) -> bool:
        try:
            out = await adb_shell(serial, f"ls {remote_path}", timeout=10)
            return remote_path in out
        except Exception:
            return False

    def _cleanup(self, paths: list[Path]) -> None:
        for p in paths:
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                logger.warning("cleanup failed for %s: %s", p, e)

    def _fail(self, code: str, message: str, *, retryable: bool | None = None) -> StepResult:
        return StepResult(
            step=StepName.VIDEO_PREPARATION,
            status=StepStatus.FAILED,
            code=code,
            message=message,
            retryable=retryable,
            warnings=[],
            artifacts=[],
        )
