"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from other agents
#
# TRV-C2-001 — PostProcessNode
# Outer backbone post-process slot.
# Reads state["formatted_output"] (set by ItineraryWorkflowGraphNode.merge_output()
# which maps the inner graph's OutputGateNode result to the outer formatted_output).
# Forwards it to state["result"] for the framework FinalizeNode to return
# to the caller.
#
# Controls:
#   Trust: required_trust_level = ANONYMOUS — the caller was already admitted
#          at the backbone entry gate
#   Audit: emit_trace_event once after the forwarding step

import logging
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

logger = logging.getLogger(__name__)


# Reason code -> the sentence the caller reads. A code with no entry falls back
# to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES: Dict[str, str] = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Outer backbone post-process node for TRV-C2-001.

    Reads formatted_output (the final, PII-scrubbed + disclaimer-appended
    itinerary summary produced by OutputGateNode, surfaced by
    ItineraryWorkflowGraphNode.merge_output()) and writes it to result
    so that the framework FinalizeNode can return it to the caller.

    Also emits a mandatory audit trace event.

    Input state keys:
        formatted_output: str — final itinerary summary from inner graph

    Output state keys (partial dict):
        result:  str — the formatted output forwarded to FinalizeNode
        status:  AgentStatus.SUCCESS
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # A declined request produced no summary. Without this branch the node
        # forwards an empty string as the result, and the caller receives an
        # empty envelope with no reason in it.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_not_produced", {"reason": marker}, state)
            return {
                "formatted_output": message,
                "result": message,
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
            }

        output: str = state.get("formatted_output") or ""

        # Audit trace — never include the output content
        emit_trace_event(
            "post_process_complete",
            {"has_output": bool(output and output.strip())},
            state,
        )

        logger.info(
            "PostProcessNode: forwarding formatted_output to result (%d chars)",
            len(output) if output else 0,
        )

        return {
            "result": output,
            "status": AgentStatus.SUCCESS.value,
        }
