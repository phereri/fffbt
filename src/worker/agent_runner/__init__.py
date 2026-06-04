"""MobileRun AI agent runner — primary proof_of_posting executor.

This package wraps Mobilerun's ``MobileAgent`` behind a small, testable
``MobileRunAgentRunner`` so the worker's ``MobileUIAutomationStep`` can
delegate Instagram navigation to an LLM-driven agent guided by the
Instagram AppCard, instead of hardcoded Python UI taps.

See ``docs/research/mobilerun-real-repo-task-map.md`` §1.2 / §1.9 for the
factory + scenario contract this is ported from.
"""

from src.worker.agent_runner.result import (
    AgentPostResult,
    AgentRunnerResult,
    ResultCategory,
)
from src.worker.agent_runner.mobilerun_agent_runner import (
    MobileRunAgentRunner,
    map_failure_reason,
)

__all__ = [
    "AgentPostResult",
    "AgentRunnerResult",
    "MobileRunAgentRunner",
    "ResultCategory",
    "map_failure_reason",
]
