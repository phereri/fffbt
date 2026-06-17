"""mobile_ui_automation step — drive Instagram Trial Reel posting flow.

Uses MobilerunWorker.run_goal() for multi-step navigation and MobileRun TCP
driver methods for deterministic operations (caption entry, caption
verification, share confirmation).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from src.worker.agent_runner import (
    AgentRunnerResult,
    MobileRunAgentRunner,
    ResultCategory,
)
from src.worker.session.mobilerun_adapter import MobilerunRouteMissingError, MobilerunWorker
from src.worker.session.types import (
    Artifact,
    StepContext,
    StepName,
    StepResult,
    StepStatus,
)
from src.worker.tools._ui import walk_plain_ui
from src.worker.tools._ui import node_text, parse_bounds
from src.worker.tools._types import ToolResult
from src.worker.tools.instagram import verify_caption_text

EXECUTOR_MOBILERUN_AGENT = "mobilerun_agent"
EXECUTOR_DETERMINISTIC = "deterministic"
_DEFAULT_EXECUTOR = EXECUTOR_MOBILERUN_AGENT
_VALID_EXECUTORS = (EXECUTOR_MOBILERUN_AGENT, EXECUTOR_DETERMINISTIC)

logger = logging.getLogger(__name__)

_HARD_STOP_PATTERNS: dict[str, list[str]] = {
    "action_blocked": [
        "action blocked",
        "we restrict certain activity",
        "try again later",
    ],
    "logged_out": [
        "log in to instagram",
        "create new account",
    ],
    "login_challenge": [
        "two-factor",
        "enter the code",
        "verify your identity",
        "confirm your identity",
        "security code",
    ],
    "account_suspended": [
        "account suspended",
        "account has been disabled",
        "your account has been suspended",
    ],
    "unexpected_destructive_dialog": [
        "log out of all accounts",
        "delete your account",
    ],
}

_GOAL_OPEN_INSTAGRAM = (
    "Open Instagram app (com.instagram.android). "
    "If an account-switching dialog appears, select the account "
    "matching the current session. Wait for the app to be fully loaded."
)

_GOAL_NAVIGATE_TO_SHARE = (
    "Navigate to the Trial Reel Share screen following this exact path:\n"
    "1. Go to the Profile tab.\n"
    "2. Tap 'Professional dashboard' (may say 'Professional Tools' or 'Pro dashboard').\n"
    "3. Inside the dashboard, tap the 'Trial Reels' tile (may be 'Trial reel', 'Trial', or under 'Tools to grow').\n"
    "4. Tap the create entry: 'Create', 'Try it', 'Get started', or a centred '+'.\n"
    "5. In the composer, switch to gallery tab if needed. Select the MOST RECENT video.\n"
    "6. Tap 'Next' / arrow forward through editor screens. Do NOT enter 'Edit cover'.\n"
    "Stop when you reach the Share screen (you'll see 'Write a caption' and a Share button).\n\n"
    "IMPORTANT: Do NOT use the bottom-nav '+' button. Do NOT look for a Trial toggle on the normal Share screen. "
    "Always use the Professional dashboard path."
)


def _parse_xml_ui(xml_str: str) -> list[dict[str, Any]]:
    """Parse uiautomator dump XML into flat node dicts."""
    _ATTR_MAP = {
        "resource-id": "resourceId",
        "class": "className",
        "content-desc": "contentDescription",
        "long-clickable": "longClickable",
    }
    nodes: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return nodes
    for elem in root.iter():
        node: dict[str, Any] = {}
        for key, val in elem.attrib.items():
            mapped = _ATTR_MAP.get(key, key)
            node[mapped] = val
        if node:
            nodes.append(node)
    return nodes


def _parse_page_source(source: str) -> list[dict[str, Any]]:
    """Parse page source (XML or JSON) into flat ui_nodes list."""
    if not source or not source.strip():
        return []
    stripped = source.strip()
    if stripped.startswith("<") or stripped.startswith("<?"):
        return _parse_xml_ui(stripped)
    try:
        data = json.loads(stripped)
        return walk_plain_ui(data)
    except (json.JSONDecodeError, TypeError):
        return []


def _parse_activity_dump(source: str) -> list[dict[str, Any]]:
    """Parse a dumpsys activity view dump into node-like dicts.

    This is a fallback for Instagram screens where uiautomator returns a stale
    Launcher tree while dumpsys still exposes resource ids and bounds.
    """
    nodes: list[dict[str, Any]] = []
    for line in (source or "").splitlines():
        if "app:id/" not in line:
            continue
        bounds_match = re.search(r"(?<![\w-])(-?\d+),(-?\d+)-(-?\d+),(-?\d+)", line)
        id_match = re.search(r"app:id/([A-Za-z0-9_]+)", line)
        class_match = re.search(r"([A-Za-z0-9_.$]+)\{[0-9a-f]+", line)
        if not bounds_match or not id_match:
            continue
        x1, y1, x2, y2 = (int(v) for v in bounds_match.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        nodes.append(
            {
                "resourceId": f"com.instagram.android:id/{id_match.group(1)}",
                "className": class_match.group(1) if class_match else "",
                "bounds": f"[{x1},{y1}][{x2},{y2}]",
                "text": "",
            }
        )
    return nodes


def _normalize_hashtags(value: Any) -> list[str]:
    """Coerce a ``hashtags`` setting into a list of complete tags.

    Accepts a ``list``/``tuple`` of tags (preferred) or a whitespace/comma
    separated string (legacy). A string is split on separators, never into
    individual characters — guarding against ``list("a b")`` producing
    ``['a', ' ', 'b']``.
    """
    if not value:
        return []
    if isinstance(value, str):
        return [tok for tok in re.split(r"[\s,]+", value.strip()) if tok]
    if isinstance(value, (list, tuple)):
        return [str(tok) for tok in value if str(tok).strip()]
    return []


def _detect_hard_stop(ui_nodes: list[dict[str, Any]]) -> tuple[str, str] | None:
    all_text = " ".join(
        str(n.get("text") or n.get("contentDescription") or "")
        for n in ui_nodes
    ).lower()
    for code, patterns in _HARD_STOP_PATTERNS.items():
        for pattern in patterns:
            if pattern in all_text:
                return code, pattern
    return None


def _goal_succeeded(result: dict[str, Any]) -> bool:
    status = str(result.get("status", "")).lower()
    return status in ("success", "completed", "ok", "done")


def _has_resource(ui_nodes: list[dict[str, Any]], suffix: str) -> bool:
    return any(str(n.get("resourceId") or "").endswith(suffix) for n in ui_nodes)


def _route_missing(result: dict[str, Any]) -> bool:
    text = " ".join(str(v) for v in result.values()).lower()
    return "route missing" in text or "404" in text or "/automation/run" in text


def _node_bounds(node: dict[str, Any]) -> tuple[int, int, int, int] | None:
    bounds = parse_bounds(
        node.get("bounds")
        or node.get("boundsInScreen")
        or node.get("bounds_in_screen")
    )
    if bounds:
        return bounds
    raw = node.get("boundsInScreen") or node.get("bounds_in_screen")
    if isinstance(raw, dict):
        try:
            return (
                int(raw["left"]),
                int(raw["top"]),
                int(raw["right"]),
                int(raw["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _bottom_right_next(ui_nodes: list[dict[str, Any]]) -> tuple[int, int] | None:
    """Find the visible bottom-right editor Next button, ignoring stale top controls."""
    candidates: list[tuple[int, int, int, int]] = []
    for node in ui_nodes:
        text = node_text(node).strip().lower()
        if text not in {"next", "next →", "next ->"} and "next" not in text:
            continue
        if node.get("isEnabled") is False or node.get("enabled") is False:
            continue
        bounds = _node_bounds(node)
        if not bounds:
            continue
        x1, y1, x2, y2 = bounds
        if x2 < 700 or y2 < 1200:
            continue
        candidates.append(bounds)
    if not candidates:
        return None
    x1, y1, x2, y2 = max(candidates, key=lambda b: (b[3], b[2]))
    return (x1 + x2) // 2, (y1 + y2) // 2


def _text_center(
    ui_nodes: list[dict[str, Any]],
    *needles: str,
    min_y: int = 0,
) -> tuple[int, int] | None:
    lowered = [needle.lower() for needle in needles if needle]
    candidates: list[tuple[int, int, int, int]] = []
    for node in ui_nodes:
        text = node_text(node).strip().lower()
        if not text:
            continue
        if not any(needle in text for needle in lowered):
            continue
        if node.get("isEnabled") is False or node.get("enabled") is False:
            continue
        bounds = _node_bounds(node)
        if not bounds:
            continue
        if bounds[1] < min_y:
            continue
        candidates.append(bounds)
    if not candidates:
        return None
    x1, y1, x2, y2 = max(candidates, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    return (x1 + x2) // 2, (y1 + y2) // 2


class MobileUIAutomationStep:
    """Drive Instagram app to publish a Trial Reel.

    Two executors are supported:

    * ``mobilerun_agent`` (default) — primary path. Builds a Mobilerun
      ``MobileAgent`` for the device and lets the agent + Instagram AppCard
      drive the publish flow. UI control goes through Mobilerun TCP tools
      end-to-end; no raw ADB UI actions are issued from this step.
    * ``deterministic`` (legacy fallback) — the original hardcoded
      ``run_goal()`` + per-coordinate TCP path. Kept behind
      ``MOBILE_UI_EXECUTOR=deterministic`` (or ``ctx.settings["mobile_ui_executor"]``)
      until the agent path is validated on real devices.
    """

    name = StepName.MOBILE_UI_AUTOMATION

    def __init__(
        self,
        *,
        genfarmer_url: str | None = None,
        artifacts_dir: str | None = None,
        agent_runner_factory: "Callable[..., MobileRunAgentRunner] | None" = None,
    ) -> None:
        self._genfarmer_url = genfarmer_url
        self._artifacts_dir = artifacts_dir
        self._job_id: str | None = None
        self._captured_artifacts: list[Artifact] = []
        self._agent_runner_factory = agent_runner_factory or MobileRunAgentRunner

    async def run(
        self,
        ctx: StepContext,
        *,
        device_serial: str | None = None,
        caption_text: str | None = None,
    ) -> StepResult:
        serial = device_serial or ctx.settings.get("device_serial")
        caption = caption_text or ctx.settings.get("caption_text", "")

        if not serial:
            return self._fail("INFRA", "no device_serial provided")
        if not caption:
            return self._fail("INFRA", "no caption_text provided")

        self._job_id = ctx.job_id
        self._captured_artifacts = []
        ctx_settings_dir = ctx.settings.get("artifacts_dir")
        if ctx_settings_dir and not self._artifacts_dir:
            self._artifacts_dir = ctx_settings_dir

        executor = self._select_executor(ctx)
        if executor not in _VALID_EXECUTORS:
            return self._fail(
                "INFRA",
                f"unknown MOBILE_UI_EXECUTOR={executor!r}; "
                f"expected one of {_VALID_EXECUTORS}",
            )
        if executor == EXECUTOR_MOBILERUN_AGENT:
            return await self._run_agent_executor(ctx, serial, caption)
        return await self._run_deterministic_executor(ctx, serial, caption)

    @staticmethod
    def _select_executor(ctx: StepContext) -> str:
        ctx_value = ctx.settings.get("mobile_ui_executor")
        if ctx_value:
            return str(ctx_value).strip().lower()
        env_value = os.environ.get("MOBILE_UI_EXECUTOR", "").strip().lower()
        if env_value:
            return env_value
        return _DEFAULT_EXECUTOR

    async def _run_deterministic_executor(
        self,
        ctx: StepContext,
        serial: str,
        caption: str,
    ) -> StepResult:
        gf_url = (
            self._genfarmer_url
            or ctx.settings.get("genfarmer_url", "http://127.0.0.1:55554")
        )
        worker = MobilerunWorker(
            device_serial=serial,
            genfarmer_url=gf_url,
            adb_fallback=False,
            use_tcp=True,
        )
        try:
            await asyncio.to_thread(worker.connect)
        except Exception as e:
            return self._fail("INFRA", f"GenFarmer connect failed: {e}")

        try:
            result = await self._execute(worker, serial, caption)
            if self._has_disallowed_fallback(worker):
                result = self._needs_review(
                    "unknown_screen",
                    "MobileRun UI control used disallowed ADB fallback",
                )
            self._attach_driver_details(result, worker, executor=EXECUTOR_DETERMINISTIC)
            self._attach_captured_artifacts(result)
            return result
        except Exception as e:
            await self._capture_hard_stop_artifacts(worker, "on_error")
            result = self._fail("UNKNOWN", f"unhandled: {e}")
            self._attach_driver_details(result, worker, executor=EXECUTOR_DETERMINISTIC)
            self._attach_captured_artifacts(result)
            return result
        finally:
            try:
                await asyncio.to_thread(worker.disconnect)
            except Exception:
                pass

    async def _run_agent_executor(
        self,
        ctx: StepContext,
        serial: str,
        caption: str,
    ) -> StepResult:
        # The agent goal renders ``caption_base`` + ``hashtags`` (appended once).
        # ``caption`` (== caption_text) already folds the tags in for the
        # deterministic path, so prefer the body to avoid duplicating them.
        agent_caption = ctx.settings.get("caption_base") or caption
        runner_kwargs = dict(
            device_serial=serial,
            job_id=ctx.job_id,
            caption=agent_caption,
            hashtags=_normalize_hashtags(ctx.settings.get("hashtags")),
            expected_username=ctx.settings.get("expected_username")
            or ctx.settings.get("account_username"),
            video_id=ctx.video_id,
            local_video_path=ctx.settings.get("local_video_path"),
            host_video_in_gallery=ctx.settings.get("host_video_in_gallery"),
            mode="proof_of_posting",
            config_path=ctx.settings.get("mobilerun_config_path"),
            app_cards_dir=ctx.settings.get("mobilerun_app_cards_dir"),
            trajectories_dir=ctx.settings.get("mobilerun_trajectories_dir"),
            model_overrides=ctx.settings.get("mobilerun_overrides") or {},
            timeout_seconds=int(ctx.settings.get("mobilerun_timeout_seconds") or 1500),
        )
        # Only pass preferred_path when set, so existing runner factories / test
        # stubs that don't accept the kwarg are unaffected.
        if ctx.settings.get("preferred_trial_path"):
            runner_kwargs["preferred_path"] = ctx.settings["preferred_trial_path"]
        try:
            runner = self._agent_runner_factory(**runner_kwargs)
        except Exception as exc:
            return self._fail("INFRA", f"agent runner construction failed: {exc}")

        try:
            agent_result = await runner.run()
        except Exception as exc:
            return self._fail("UNKNOWN", f"agent runner raised: {exc}")

        return self._agent_result_to_step(agent_result)

    def _agent_result_to_step(self, agent_result: AgentRunnerResult) -> StepResult:
        artifacts = [
            Artifact(
                artifact_id=path,
                artifact_type="mobilerun_trajectory",
                label="trajectory",
            )
            for path in agent_result.trajectory_paths
        ]
        details: dict[str, Any] = {
            "mobile_driver": {
                "primary": "mobilerun_agent",
                "executor": EXECUTOR_MOBILERUN_AGENT,
                "use_tcp": True,
                "adb_fallback_used": False,
                "agent_status": agent_result.agent_status,
                "failure_reason": agent_result.failure_reason,
                "path_used": (
                    agent_result.structured.path_used
                    if agent_result.structured is not None
                    else None
                ),
            }
        }
        if agent_result.category is ResultCategory.OK:
            result = StepResult(
                step=StepName.MOBILE_UI_AUTOMATION,
                status=StepStatus.OK,
                message=agent_result.message,
                artifacts=artifacts,
                details=details,
            )
            return result
        if agent_result.category is ResultCategory.HARD_STOP:
            result = StepResult(
                step=StepName.MOBILE_UI_AUTOMATION,
                status=StepStatus.FAILED,
                code=agent_result.error_code,
                message=agent_result.message,
                retryable=False,
                artifacts=artifacts,
                details=details,
            )
            return result
        if agent_result.category is ResultCategory.INFRA:
            return StepResult(
                step=StepName.MOBILE_UI_AUTOMATION,
                status=StepStatus.FAILED,
                code=agent_result.error_code or "INFRA",
                message=agent_result.message,
                retryable=True,
                artifacts=artifacts,
                details=details,
            )
        return StepResult(
            step=StepName.MOBILE_UI_AUTOMATION,
            status=StepStatus.NEEDS_REVIEW,
            code=agent_result.error_code or "unknown_screen",
            message=agent_result.message,
            artifacts=artifacts,
            details=details,
        )

    async def _execute(
        self,
        worker: MobilerunWorker,
        serial: str,
        caption: str,
    ) -> StepResult:
        # --- Open Instagram ---
        result = await self._open_instagram(worker)
        if not _goal_succeeded(result):
            ui = await self._read_ui(worker)
            stop = _detect_hard_stop(ui)
            if stop:
                await self._capture_hard_stop_artifacts(worker, f"hard_stop_{stop[0]}")
                return self._fail(stop[0], f"hard stop: {stop[1]}")
            return self._fail(
                "INFRA",
                f"open_instagram failed: {result.get('error', result.get('status', 'unknown'))}",
                retryable=True,
            )

        await self._screenshot(worker, "after_instagram_launch")

        ui = await self._read_ui(worker)
        stop = _detect_hard_stop(ui)
        if stop:
            await self._capture_hard_stop_artifacts(worker, f"hard_stop_{stop[0]}")
            return self._fail(stop[0], f"hard stop after launch: {stop[1]}")

        # --- Navigate to Share screen ---
        result = await self._run_goal(worker, _GOAL_NAVIGATE_TO_SHARE, timeout=180)
        if not _goal_succeeded(result) and _route_missing(result):
            result = await self._navigate_trial_reel_share_with_mobilerun(worker, serial)
        if not _goal_succeeded(result):
            ui = await self._read_ui(worker)
            stop = _detect_hard_stop(ui)
            if stop:
                await self._capture_hard_stop_artifacts(worker, f"hard_stop_{stop[0]}")
                return self._fail(stop[0], f"hard stop: {stop[1]}")
            err = (
                result.get("error")
                or result.get("failure_reason")
                or result.get("status", "unknown")
            )
            error_code = str(result.get("error_code") or "")
            if error_code in {
                "editor_next_not_reached",
                "share_screen_not_reached",
                "next_button_inactive",
                "trial_reels_gallery_not_reached",
            }:
                return self._needs_review(error_code, str(err))
            err_lower = str(err).lower()
            if "dashboard" in err_lower or "trial" in err_lower:
                return self._fail("trial_reels_unavailable", str(err))
            return self._needs_review("unknown_screen", f"navigation failed: {err}")

        await self._screenshot(worker, "share_screen")

        ui = await self._read_ui(worker)
        stop = _detect_hard_stop(ui)
        if stop:
            await self._capture_hard_stop_artifacts(worker, f"hard_stop_{stop[0]}")
            return self._fail(stop[0], f"hard stop on Share screen: {stop[1]}")

        # --- Fill caption (local tools) ---
        paste_result = await self._type_caption(worker, ui, caption)
        if not paste_result.success:
            return self._needs_review(
                "unknown_screen", f"caption paste failed: {paste_result.message}"
            )

        await asyncio.sleep(0.8)

        # --- Verify caption ---
        ui = await self._read_ui(worker)
        verify_result = verify_caption_text(caption, ui_nodes=ui)
        if not verify_result.success:
            return self._needs_review(
                "caption_mismatch", f"caption verification failed: {verify_result.message}"
            )

        await self._screenshot(worker, "caption_filled")

        # --- Share ---
        share_result = await self._tap_share_and_confirm(worker)
        if not share_result.success:
            ui = await self._read_ui(worker)
            stop = _detect_hard_stop(ui)
            if stop:
                await self._capture_hard_stop_artifacts(worker, f"hard_stop_{stop[0]}")
                return self._fail(stop[0], f"hard stop during share: {stop[1]}")
            code = (
                "final_ok_did_not_register"
                if "final_ok_did_not_register" in share_result.message
                else "share_did_not_register"
            )
            return self._needs_review(code, f"share failed: {share_result.message}")

        await self._screenshot(worker, "post_result")

        return StepResult(
            step=StepName.MOBILE_UI_AUTOMATION,
            status=StepStatus.OK,
            message=f"Trial Reel published: {share_result.message}",
        )

    async def _open_instagram(self, worker: MobilerunWorker) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                worker.open_app,
                "com.instagram.android",
                activity="com.instagram.mainactivity.InstagramMainActivity",
                force_stop=True,
                wait_seconds=3.0,
            )
        except Exception as e:
            logger.error("open_app error: %s", e)
            return {"status": "error", "error": str(e)}

    async def _navigate_trial_reel_share_with_mobilerun(
        self,
        worker: MobilerunWorker,
        serial: str,
    ) -> dict[str, Any]:
        """MobileRun TCP fallback for the deployed GenFarmer build without /automation/run.

        Coordinates are scoped to the validated 1080x1920 happy-path phone.
        The routine checks screen state between phases and returns needs-review
        style errors instead of continuing from ambiguous screens.
        """
        del serial  # serial is kept for test readability and future branching.

        async def tap(x: int, y: int, delay: float = 1.4) -> None:
            await asyncio.to_thread(worker.tap, x, y)
            await asyncio.sleep(delay)

        async def swipe() -> None:
            await asyncio.to_thread(worker.swipe, 540, 1650, 540, 650, 500)
            await asyncio.sleep(1.2)

        # Profile -> dashboard -> Trial Reels -> Create trial reel.
        await tap(970, 1728, 2.0)
        ui = await self._read_ui(worker)
        stop = _detect_hard_stop(ui)
        if stop:
            return {"status": "failed", "error": f"hard stop: {stop[1]}"}

        await tap(520, 870, 2.0)
        ui = await self._read_ui(worker)
        trial_coords = _text_center(ui, "trial reels", "trial reel")
        if trial_coords is None:
            await swipe()
            ui = await self._read_ui(worker)
            trial_coords = _text_center(ui, "trial reels", "trial reel")
        if trial_coords is None:
            await self._screenshot(worker, "trial_reels_tile_not_found")
            return {
                "status": "failed",
                "error": "Trial Reels tile not found in Professional dashboard",
            }

        await tap(trial_coords[0], trial_coords[1], 2.0)
        ui = await self._read_ui(worker)
        if not (
            _has_resource(ui, "gallery_recycler_view")
            or _has_resource(ui, "clips_next_button")
            or _has_resource(ui, "feed_gallery_fragment_holder")
        ):
            create_coords = _text_center(ui, "create trial reel", "create", min_y=1200)
            if create_coords:
                await tap(create_coords[0], create_coords[1], 2.5)
            else:
                await tap(540, 1710, 2.5)

        # Select the newest non-camera media cell in the Trial Reel gallery.
        ui = await self._read_ui(worker)
        if not (
            _has_resource(ui, "gallery_recycler_view")
            or _has_resource(ui, "gallery_grid_item_thumbnail")
        ):
            await self._screenshot(worker, "trial_reels_gallery_not_reached")
            return {
                "status": "failed",
                "error_code": "trial_reels_gallery_not_reached",
                "error": "Trial Reels gallery not detected after create",
            }

        await tap(540, 850, 2.5)

        # Advance through editor screens until the Share screen appears. Prefer
        # the visible bottom-right Next button; ignore the legacy top
        # clips_next_button, which can be present while disabled/inactive.
        ui = await self._read_ui(worker)
        next_coords = _bottom_right_next(ui) or (920, 1620)
        await tap(next_coords[0], next_coords[1], 2.5)
        for _ in range(5):
            ui = await self._read_ui(worker)
            if _has_resource(ui, "caption_input_text_view") and _has_resource(
                ui, "share_button"
            ):
                return {"status": "success", "method": "mobilerun_tcp_trial_reels_path"}
            if _has_resource(ui, "post_capture_button_share_container"):
                next_coords = _bottom_right_next(ui) or (920, 1620)
                await tap(next_coords[0], next_coords[1], 2.0)
            else:
                await tap(700, 145, 2.0)

        ui = await self._read_ui(worker)
        if _has_resource(ui, "caption_input_text_view") and _has_resource(
            ui, "share_button"
        ):
            return {"status": "success", "method": "mobilerun_tcp_trial_reels_path"}
        await self._screenshot(worker, "share_screen_not_reached")
        return {
            "status": "failed",
            "error_code": "share_screen_not_reached",
            "error": "Share screen not reached after editor Next fallback",
        }

    async def _run_goal(
        self, worker: MobilerunWorker, goal: str, timeout: int = 300
    ) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                worker.run_goal, goal, timeout_seconds=timeout
            )
        except MobilerunRouteMissingError as e:
            logger.error("run_goal route missing: %s", e)
            return {"status": "error", "error": str(e)}
        except Exception as e:
            logger.error("run_goal error: %s", e)
            return {"status": "error", "error": str(e)}

    async def _read_ui(self, worker: MobilerunWorker) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        try:
            source = await asyncio.to_thread(worker.page_source)
            nodes = _parse_page_source(source)
        except Exception:
            nodes = []
        return nodes

    async def _tap_by_resource_id(
        self,
        worker: MobilerunWorker,
        resource_id: str,
        *,
        ui_nodes: list[dict[str, Any]],
        class_name_contains: str | None = None,
    ) -> ToolResult:
        suffix = resource_id.split("/")[-1] if "/" in resource_id else resource_id
        suffix = suffix.split(":")[-1]
        class_needle = (class_name_contains or "").lower()
        matches: list[dict[str, Any]] = []
        for node in ui_nodes:
            rid = str(node.get("resourceId") or node.get("resource-id") or "")
            if rid != resource_id and not rid.endswith(suffix):
                continue
            if class_needle:
                class_name = str(node.get("className") or node.get("class_name") or "").lower()
                if class_needle not in class_name:
                    continue
            if _node_bounds(node):
                matches.append(node)

        if not matches:
            return ToolResult.fail(f"tap_by_resource_id: no node with resource_id={resource_id!r}")

        def area(node: dict[str, Any]) -> int:
            x1, y1, x2, y2 = _node_bounds(node) or (0, 0, 0, 0)
            return max(0, x2 - x1) * max(0, y2 - y1)

        target = max(matches, key=area)
        x1, y1, x2, y2 = _node_bounds(target) or (0, 0, 0, 0)
        await asyncio.to_thread(worker.tap, (x1 + x2) // 2, (y1 + y2) // 2)
        return ToolResult.ok(f"tapped {resource_id} via MobileRun TCP")

    async def _focus_caption_field(
        self,
        worker: MobilerunWorker,
        ui_nodes: list[dict[str, Any]],
    ) -> ToolResult:
        result = await self._tap_by_resource_id(
            worker,
            "com.instagram.android:id/caption_input_text_view",
            ui_nodes=ui_nodes,
            class_name_contains="AutoComplete",
        )
        if result.success:
            return result
        coords = _text_center(ui_nodes, "write a caption", "add a caption")
        if coords:
            await asyncio.to_thread(worker.tap, coords[0], coords[1])
            return ToolResult.ok("focused caption field via MobileRun TCP")
        return ToolResult.fail("focus_caption_field: caption input not found")

    async def _type_caption(
        self,
        worker: MobilerunWorker,
        ui_nodes: list[dict[str, Any]],
        caption: str,
    ) -> ToolResult:
        focus = await self._focus_caption_field(worker, ui_nodes)
        if not focus.success:
            return focus
        await asyncio.sleep(0.55)
        try:
            await asyncio.to_thread(worker.type_text, caption)
        except Exception as exc:
            return ToolResult.fail(f"type_caption: MobileRun input_text failed: {exc}")
        return ToolResult.ok(f"typed {len(caption)} chars via MobileRun TCP")

    async def _tap_share_and_confirm(self, worker: MobilerunWorker) -> ToolResult:
        """Publish the Trial Reel.

        On the Trial Reel "New reel" final screen, the publish action is the
        top-right OK button — there may be no separate bottom Share button.
        Detect that screen and tap OK via MobileRun TCP, preferring the
        accessible OK node and falling back to dynamic top-right bounds.

        For legacy Reels share screens (no "New reel" title + Trial banner),
        keep dismissing the caption editor via OK then tapping share_button.
        """
        ui = await self._read_ui(worker)

        if self._on_new_reel_screen(ui):
            if self._keyboard_or_suggestions_active(ui):
                await self._dismiss_keyboard_safely(worker)
                ui = await self._read_ui(worker)
                if not self._on_new_reel_screen(ui):
                    await self._screenshot(worker, "final_ok_left_new_reel")
                    return ToolResult.fail(
                        "final_ok_did_not_register: New reel screen left during "
                        "keyboard dismiss; cannot confirm publish target"
                    )
            return await self._tap_final_ok(worker, ui)

        await self._dismiss_caption_keyboard(worker)
        return await self._tap_share_button_loop(worker)

    async def _tap_share_button_loop(self, worker: MobilerunWorker) -> ToolResult:
        deadline = asyncio.get_running_loop().time() + 22.0
        while asyncio.get_running_loop().time() < deadline:
            ui = await self._read_ui(worker)
            coords = self._share_button_center(ui)
            if coords:
                await asyncio.to_thread(worker.tap, coords[0], coords[1])
                await asyncio.sleep(2.0)
                after = await self._read_ui(worker)
                if not self._share_button_center(after):
                    return ToolResult.ok("share confirmed: share button disappeared")
            await asyncio.sleep(1.0)
        return ToolResult.fail("share did not register before timeout")

    _FINAL_OK_CONFIRM_TIMEOUT_S: float = 18.0

    async def _tap_final_ok(
        self, worker: MobilerunWorker, ui: list[dict[str, Any]]
    ) -> ToolResult:
        """Tap the top-right OK on the New reel screen and confirm transition."""
        ok_coords = self._final_ok_center(ui)
        method = "accessible_node"
        if ok_coords is None:
            ok_coords = self._top_right_fallback_coords(ui)
            method = "top_right_fallback"
        if ok_coords is None:
            await self._screenshot(worker, "final_ok_not_found")
            await self._read_ui(worker)  # dump UI tree to actions log
            return ToolResult.fail(
                "final_ok_did_not_register: OK button not found and screen "
                "size could not be inferred"
            )

        await asyncio.to_thread(worker.tap, ok_coords[0], ok_coords[1])
        await asyncio.sleep(1.8)

        max_polls = max(1, int(self._FINAL_OK_CONFIRM_TIMEOUT_S))
        deadline = asyncio.get_running_loop().time() + self._FINAL_OK_CONFIRM_TIMEOUT_S
        polls = 0
        while polls < max_polls and asyncio.get_running_loop().time() < deadline:
            polls += 1
            after = await self._read_ui(worker)
            if not after:
                break  # exhausted UI source; treat as no-transition
            if not self._on_new_reel_screen(after) or self._post_publish_screen(after):
                return ToolResult.ok(
                    f"Trial Reel published via {method} OK at {ok_coords}"
                )
            await asyncio.sleep(1.0)

        await self._screenshot(worker, "final_ok_did_not_register")
        await self._read_ui(worker)  # dump UI tree to actions log
        return ToolResult.fail(
            "final_ok_did_not_register: New reel screen still present after "
            f"OK tap at {ok_coords} via {method}"
        )

    async def _dismiss_keyboard_safely(self, worker: MobilerunWorker) -> None:
        """Hide IME / clear caption focus without tapping the publish OK.

        Order: (1) worker.hide_ime() if exposed by the MobileRun driver,
        (2) tap the Trial Reel banner / non-input area to clear focus,
        (3) ADB keyevent BACK only if it keeps us on the New reel screen.
        """
        hide_ime = getattr(worker, "hide_ime", None)
        if callable(hide_ime):
            try:
                await asyncio.to_thread(hide_ime)
                await asyncio.sleep(0.6)
                ui = await self._read_ui(worker)
                if not self._keyboard_or_suggestions_active(ui):
                    return
            except Exception:
                pass

        ui = await self._read_ui(worker)
        banner = _text_center(
            ui, "non-followers", "non followers", "trial reel and will"
        )
        if banner is not None:
            await asyncio.to_thread(worker.tap, banner[0], banner[1])
            await asyncio.sleep(0.6)
            after = await self._read_ui(worker)
            if (
                not self._keyboard_or_suggestions_active(after)
                and self._on_new_reel_screen(after)
            ):
                return

        try:
            from src.worker.tools._adb import shell as adb_shell

            await adb_shell(worker.device_serial, "input keyevent 4", timeout=10)
            await asyncio.sleep(0.6)
        except Exception:
            return

    async def _dismiss_caption_keyboard(self, worker: MobilerunWorker) -> None:
        """Legacy dismiss: close caption edit mode on the non-Trial share screen.

        On the legacy Reels share screen, OK only confirms the caption edit and
        the user still needs to tap the bottom Share button to publish.
        """
        for _ in range(2):
            ui = await self._read_ui(worker)
            ok = self._action_bar_ok_center(ui)
            if ok is None:
                return
            if not (
                self._caption_field_focused(ui)
                or _has_resource(ui, "caption_add_on_recyclerview")
            ):
                return
            await asyncio.to_thread(worker.tap, ok[0], ok[1])
            await asyncio.sleep(1.2)

    def _on_new_reel_screen(self, ui_nodes: list[dict[str, Any]]) -> bool:
        """True iff UI is the Trial Reel "New reel" final share screen."""
        has_title = False
        has_banner = False
        has_caption_input = False
        for node in ui_nodes:
            text = node_text(node).strip().lower()
            rid = str(node.get("resourceId") or node.get("resource-id") or "")
            if text == "new reel" or "new reel" in text:
                has_title = True
            if (
                "trial reel" in text
                or "non-followers" in text
                or "non followers" in text
            ):
                has_banner = True
            if rid.endswith("caption_input_text_view"):
                has_caption_input = True
        return has_caption_input and (has_title or has_banner)

    def _keyboard_or_suggestions_active(
        self, ui_nodes: list[dict[str, Any]]
    ) -> bool:
        if _has_resource(ui_nodes, "caption_add_on_recyclerview"):
            return True
        for node in ui_nodes:
            rid = str(node.get("resourceId") or node.get("resource-id") or "")
            if rid.endswith("caption_input_text_view") and bool(node.get("isFocused")):
                return True
        return False

    def _post_publish_screen(self, ui_nodes: list[dict[str, Any]]) -> bool:
        text_blob = " ".join(
            str(n.get("text") or n.get("contentDescription") or "")
            for n in ui_nodes
        ).lower()
        markers = (
            "posting",
            "uploading",
            "your trial reel",
            "trial reels",
            "trial_thumbnail_image",
            "your reel",
            "home",
            "for you",
        )
        return any(marker in text_blob for marker in markers)

    def _final_ok_center(
        self, ui_nodes: list[dict[str, Any]]
    ) -> tuple[int, int] | None:
        """Find the New reel top-right OK button in the accessibility tree.

        Accepts a node whose visible text is exactly "OK" OR whose resource-id
        ends with action_bar_button_text, provided its bounds sit in the
        top-right region of the screen.
        """
        size = self._screen_size(ui_nodes)
        width = size[0] if size else 1080
        height = size[1] if size else 1920
        right_threshold = int(width * 0.6)
        top_limit = int(height * 0.20)
        candidates: list[tuple[int, int, int, int]] = []
        for node in ui_nodes:
            text = node_text(node).strip().lower()
            rid = str(node.get("resourceId") or node.get("resource-id") or "")
            if text != "ok" and not rid.endswith("action_bar_button_text"):
                continue
            bounds = _node_bounds(node)
            if not bounds:
                continue
            x1, y1, x2, y2 = bounds
            if x2 < right_threshold or y1 > top_limit:
                continue
            candidates.append(bounds)
        if not candidates:
            return None
        x1, y1, x2, y2 = max(candidates, key=lambda b: (b[2], -b[1]))
        return (x1 + x2) // 2, (y1 + y2) // 2

    def _screen_size(
        self, ui_nodes: list[dict[str, Any]]
    ) -> tuple[int, int] | None:
        max_x = 0
        max_y = 0
        for node in ui_nodes:
            bounds = _node_bounds(node)
            if not bounds:
                continue
            if bounds[2] > max_x:
                max_x = bounds[2]
            if bounds[3] > max_y:
                max_y = bounds[3]
        if max_x <= 0 or max_y <= 0:
            return None
        return max_x, max_y

    def _top_right_fallback_coords(
        self, ui_nodes: list[dict[str, Any]]
    ) -> tuple[int, int] | None:
        size = self._screen_size(ui_nodes)
        if size is None:
            return None
        w, h = size
        return int(w * 0.925), int(h * 0.07)

    def _share_button_center(self, ui_nodes: list[dict[str, Any]]) -> tuple[int, int] | None:
        candidates: list[tuple[int, int, int, int]] = []
        for node in ui_nodes:
            rid = str(node.get("resourceId") or node.get("resource-id") or "")
            if not rid.endswith("share_button"):
                continue
            bounds = _node_bounds(node)
            if bounds:
                candidates.append(bounds)
        if not candidates:
            return None
        x1, y1, x2, y2 = max(candidates, key=lambda b: b[3])
        return (x1 + x2) // 2, (y1 + y2) // 2

    def _action_bar_ok_center(self, ui_nodes: list[dict[str, Any]]) -> tuple[int, int] | None:
        candidates: list[tuple[int, int, int, int]] = []
        for node in ui_nodes:
            text = node_text(node).strip().lower()
            rid = str(node.get("resourceId") or node.get("resource-id") or "")
            if text != "ok" and not rid.endswith("action_bar_button_text"):
                continue
            bounds = _node_bounds(node)
            if not bounds:
                continue
            x1, y1, x2, y2 = bounds
            if x1 < 800 or y2 > 260:
                continue
            candidates.append(bounds)
        if not candidates:
            return None
        x1, y1, x2, y2 = max(candidates, key=lambda b: b[2])
        return (x1 + x2) // 2, (y1 + y2) // 2

    def _caption_field_focused(self, ui_nodes: list[dict[str, Any]]) -> bool:
        for node in ui_nodes:
            rid = str(node.get("resourceId") or node.get("resource-id") or "")
            if rid.endswith("caption_input_text_view") and bool(node.get("isFocused")):
                return True
        return False

    async def _screenshot(self, worker: MobilerunWorker, label: str) -> None:
        try:
            await asyncio.to_thread(worker.screenshot, label)
        except Exception:
            pass

    def _resolve_artifacts_dir(self) -> Path | None:
        """Return the per-job artifacts directory, creating it on demand."""
        base = self._artifacts_dir or os.environ.get("ARTIFACTS_DIR", "./.artifacts")
        job_id = self._job_id or "no_job"
        try:
            target = Path(base) / "mobile_ui" / job_id
            target.mkdir(parents=True, exist_ok=True)
            return target
        except OSError:
            return None

    async def _capture_hard_stop_artifacts(
        self, worker: MobilerunWorker, label: str
    ) -> None:
        """Persist a screenshot + UI dump to ARTIFACTS_DIR for the current job.

        Hard stops like logged_out / action_blocked / login_challenge /
        account_suspended produce StepResult with code only — to debug them,
        we need the screen state the moment the hard stop fired. Failures
        here are swallowed: artifact capture must never raise during the
        already-failing path.
        """
        directory = self._resolve_artifacts_dir()
        if directory is None:
            await self._screenshot(worker, label)
            return

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base_name = f"{label}_{stamp}"
        png_path = directory / f"{base_name}.png"
        ui_path = directory / f"{base_name}.ui.json"

        try:
            raw = await asyncio.to_thread(worker.screenshot, label)
            if raw:
                png_path.write_bytes(raw)
                self._captured_artifacts.append(
                    Artifact(
                        artifact_id=str(png_path),
                        artifact_type="screenshot",
                        label=label,
                    )
                )
        except Exception:
            pass

        try:
            source = await asyncio.to_thread(worker.page_source)
            if source:
                ui_path.write_text(source, encoding="utf-8")
                self._captured_artifacts.append(
                    Artifact(
                        artifact_id=str(ui_path),
                        artifact_type="ui_dump",
                        label=label,
                    )
                )
        except Exception:
            pass

    def _attach_captured_artifacts(self, result: StepResult) -> None:
        if not self._captured_artifacts:
            return
        result.artifacts = list(result.artifacts) + list(self._captured_artifacts)

    def _attach_driver_details(
        self,
        result: StepResult,
        worker: MobilerunWorker,
        *,
        executor: str = EXECUTOR_DETERMINISTIC,
    ) -> None:
        actions = worker.actions_log
        details = dict(result.details or {})
        details["mobile_driver"] = {
            "primary": "mobilerun_tcp",
            "executor": executor,
            "use_tcp": True,
            "adb_fallback_used": any(a.get("action") == "adb_fallback" for a in actions),
            "actions": actions,
        }
        result.details = details

    def _has_disallowed_fallback(self, worker: MobilerunWorker) -> bool:
        return any(action.get("action") == "adb_fallback" for action in worker.actions_log)

    def _fail(
        self, code: str, message: str, *, retryable: bool | None = None
    ) -> StepResult:
        return StepResult(
            step=StepName.MOBILE_UI_AUTOMATION,
            status=StepStatus.FAILED,
            code=code,
            message=message,
            retryable=retryable,
        )

    def _needs_review(self, code: str, message: str) -> StepResult:
        return StepResult(
            step=StepName.MOBILE_UI_AUTOMATION,
            status=StepStatus.NEEDS_REVIEW,
            code=code,
            message=message,
        )
