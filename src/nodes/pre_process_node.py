"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from other agents
#
# TRV-C2-001 — PreProcessNode
# Backbone entry gate: validate the raw user_input, reject empty, oversized
# and injection payloads, and write validated_input.
#
# Controls:
#   Trust: required_trust_level = VERIFIED_EXTERNAL — the backbone entry gate
#   Input: length cap + injection screen
#   Audit: emit_trace_event once per execute()

import logging
import re
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

logger = logging.getLogger(__name__)

_MAX_INPUT_BYTES = 1_048_576  # 1 MiB — guard against oversized booking blobs

# Directive phrases. Each is a complete instruction rather than a keyword,
# because this domain's own text collides with the obvious keywords: an
# activity can be an "Alcatraz Jailbreak Tour", and a confirmation line can read
# "You are now checked in". A screen that refuses those refuses real bookings,
# which is the failure mode that actually stops work.
_INJECTION_SIGNALS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "ignore all instructions",
    "ignore all rules",
    "ignore the above instructions",
    "disregard previous instructions",
    "disregard all instructions",
    "you are now a",
    "your system prompt",
    "enter jailbreak mode",
)

# Chat-template control tokens, screened as a class rather than as phrases.
# A payload can carry a complete role switch without containing any directive
# phrase at all — "<|im_start|>system ..." is a control token followed by
# ordinary words — so a phrase list alone leaves the whole class open.
_CONTROL_TOKEN_RE = re.compile(
    r"<\|[^|>]{0,64}\|>"  # ChatML-style <|im_start|>, <|endoftext|>
    r"|\[/?INST\]"  # Llama-style [INST] / [/INST]
    r"|<</?SYS>>",  # Llama-style <<SYS>> / <</SYS>>
    re.IGNORECASE,
)


def screen_directives(text: str) -> Optional[str]:
    """Return a fixed reason code when *text* carries an injection attempt.

    Returns None when the text is clean. The reason code is a constant, never
    the matched substring: an error message that quotes what it rejected hands
    the caller's own payload back out through the error channel.
    """
    if _CONTROL_TOKEN_RE.search(text):
        return "control_token"
    lowered = text.lower()
    for signal in _INJECTION_SIGNALS:
        if signal in lowered:
            return "directive_phrase"
    return None


class PreProcessNode(FunctionNode):
    """Backbone input-validation gate for TRV-C2-001.

    Validates the raw user_input submitted by the caller:
      - Rejects empty or whitespace-only payloads
      - Rejects inputs exceeding _MAX_INPUT_BYTES
      - Screens for common prompt-injection signals in free-text fields
      - Writes validated_input (stripped, size-bounded) on success

    Trust level: VERIFIED_EXTERNAL — only verified external callers may
    invoke the outer backbone; anonymous callers are denied at this gate.

    Input state keys:
        user_input: str — raw booking records payload from the caller

    Output state keys (partial dict):
        validated_input: str  — validated and stripped input
        status: AgentStatus.SUCCESS | AgentStatus.ERROR
        error_log: list[str]  — populated on rejection
    """

    # Outer backbone gate — only VERIFIED_EXTERNAL callers may enter.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_input: str = state.get("user_input", "") or ""

        # ------------------------------------------------------------------
        # Check 1 — empty / whitespace-only input
        # ------------------------------------------------------------------
        stripped = user_input.strip()
        if not stripped:
            logger.warning("PreProcessNode: empty user_input")
            emit_trace_event(
                "pre_process_complete",
                {"accepted": False, "reason": "empty_input"},
                state,
            )
            # Nothing was sent. The caller can fix that, so the run COMPLETES
            # carrying the reason rather than terminating and leaving them only
            # an exception type.
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # ------------------------------------------------------------------
        # Check 2 — payload size cap
        # ------------------------------------------------------------------
        if len(stripped.encode("utf-8")) > _MAX_INPUT_BYTES:
            logger.warning("PreProcessNode: user_input exceeds %d bytes", _MAX_INPUT_BYTES)
            emit_trace_event(
                "pre_process_complete",
                {"accepted": False, "reason": "oversized_input"},
                state,
            )
            # The whole request is oversized, not one field of it; shortening
            # it is a change the caller can make.
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [f"PreProcessNode: input exceeds maximum size ({_MAX_INPUT_BYTES} bytes)"],
            }

        # ------------------------------------------------------------------
        # Check 3 — injection screen on free-text content
        #
        # Enforced here rather than left to the framework input gate: this node
        # owns the caller contract, and a guarantee that only holds where an
        # upstream gate happens to be active is not a guarantee. Calling
        # execute() directly is what proves it.
        # ------------------------------------------------------------------
        reason = screen_directives(stripped)
        if reason is not None:
            logger.warning("PreProcessNode: input rejected by the injection screen (%s)", reason)
            emit_trace_event(
                "pre_process_complete",
                {"accepted": False, "reason": reason},
                state,
            )
            # Terminal, unlike the two rejections above. Spliced instructions
            # are not a value the caller can correct by rewording, and completing
            # the run would make a refusal read like an ordinary declined value.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: user_input rejected by the injection screen ({reason})"],
            }

        # ------------------------------------------------------------------
        # Audit trace — non-sensitive size metadata only
        # ------------------------------------------------------------------
        emit_trace_event(
            "pre_process_complete",
            {
                "accepted": True,
                "input_bytes": len(stripped.encode("utf-8")),
            },
            state,
        )

        logger.info("PreProcessNode: input accepted (%d bytes)", len(stripped.encode("utf-8")))

        return {
            "validated_input": stripped,
            "status": AgentStatus.SUCCESS.value,
        }
