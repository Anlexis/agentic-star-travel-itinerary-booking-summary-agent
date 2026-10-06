"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from other agents
#
# TRV-C2-001 — OutputGateNode
# Inner domain pipeline node 5 (of 5): the release boundary. Nothing leaves the
# pipeline without passing through here.
#
# Release checks, in this order and non-bypassable:
#   1. Residual personal-data scrub — mask any personal-data pattern that
#      survived ingestion's field strip. Defence in depth: BookingIngestionNode
#      already drops those fields; this catches anything the composition steps
#      reassembled.
#   2. Credential screen — refuse release when the composed summary carries a
#      credential-shaped value. This calls the framework's own detector rather
#      than a list maintained here, so what this gate refuses and what the
#      framework's mandatory output scan blocks are one set by construction. A
#      local list narrower than the framework's is a containment bypass, not a
#      smaller gate: a value this node missed makes the framework raise from
#      inside the node wrapper, and the wrapper's error result then replaces
#      this node's whole delta — the clearing below included.
#   3. Faithful-summary check — every released summary must carry at least one
#      [BK-<id>] reference back to a source booking record. A summary that
#      references nothing is not a summary of the caller's bookings.
#   4. Size ceiling — a released summary larger than the declared ceiling means
#      the pipeline is re-emitting the booking payload rather than summarising
#      it.
#   5. Hardcoded disclaimer append — non-configurable, non-suppressible.
#
# On any refusal the node returns ERROR **and clears every field that carries
# released text**, replacing formatted_output with a truthy refusal notice.
# Both halves matter. The graph resolves the released document as
# `formatted_output or result` with no status check, so an empty-string notice
# falls through to the ungated text it was written to withhold, and a refusal
# that only sets an error status ships the document inside the error envelope.
# Refusal messages carry fixed reason codes, never the matched value: the
# framework scans every value this node returns, so quoting the finding would
# make the framework raise and discard the clearing along with it.

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from shared.utils.audit_logger import emit_trace_event
from src.schemas.bounds import bound_int, read_bounds

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Residual personal-data patterns
# ---------------------------------------------------------------------------

# Defence in depth against personal data that survived the ingestion strip.
# These cover the personal-data shapes this domain carries; credential shapes
# are NOT listed here — they are delegated to the framework detector so the two
# sets cannot drift apart.
_PII_PATTERNS: List[re.Pattern[str]] = [
    # Passport number (one or two letters followed by 7-8 digits)
    re.compile(r"\b[A-Z]{1,2}[0-9]{7,8}\b"),
    # Payment card number (13-16 digits, with or without spaces or dashes)
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    # Domestic phone number
    re.compile(r"\b0[0-9]{1,4}[-\s]?[0-9]{1,4}[-\s]?[0-9]{4}\b"),
    # Email address
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    # Individual number (12 digits in sequence)
    re.compile(r"\b\d{12}\b"),
]

_PII_MASK = "[MASKED]"

# Non-configurable, non-suppressible.
_DISCLAIMER = (
    "\n---\n"
    "【免責事項】本サマリーはAI生成による参考要約です。"
    "正式な予約内容・料金・取消条件は予約原本（確定書面）でご確認ください。"
)

# Source-traceability marker written by the synthesis step: [BK-<booking_id>].
_BOOKING_REF_RE = re.compile(r"\[BK-[^\]]+\]")

# Fixed refusal reason codes. These are the only refusal detail that reaches the
# caller, and each is a constant chosen here — never a substring of the summary.
REASON_CREDENTIAL = "credential_detected"
REASON_NO_SOURCE_REFERENCE = "no_source_reference"
REASON_OVER_SIZE_CEILING = "over_size_ceiling"

# Notice published in place of the summary. Truthy on purpose: a falsy value
# here activates the `formatted_output or result` fallback and releases exactly
# the text this node refused.
_REFUSAL_NOTICE = (
    "【要約は公開されませんでした】この依頼の旅程サマリーは公開前検査で差し止められました。"
    "理由コード: {reason}。予約データを確認のうえ再送してください。"
)

# Every state field that can carry released text or a caller payload. The gate
# clears all of them on refusal. Kept as an explicit inventory so a field added
# to the pipeline later cannot quietly join the released set without appearing
# in this list — the inventory test reads it directly.
CLEARED_ON_REFUSAL: Tuple[str, ...] = (
    "result",
    "itinerary_body",
    "total_cost_jpy",
    "cancellation_terms",
    "segments",
    "parsed_bookings",
    "segment_types_found",
    "validated_input",
)


def _scrub_pii(text: str) -> Tuple[str, bool]:
    """Mask residual personal-data patterns in *text*.

    Returns (scrubbed_text, pii_found). Operates on the composed summary only.
    No matched value is written to a log or to state.
    """
    pii_found = False
    for pattern in _PII_PATTERNS:
        if pattern.search(text):
            pii_found = True
            text = pattern.sub(_PII_MASK, text)
    return text, pii_found


def screen_release(text: str, max_report_chars: int) -> Optional[str]:
    """Return a refusal reason code for *text*, or None when it may be released.

    Checked in severity order so the reason code the caller sees is the most
    serious finding rather than whichever check happened to run first.
    """
    if detect_credentials_in_value(text):
        return REASON_CREDENTIAL
    if not _BOOKING_REF_RE.search(text):
        return REASON_NO_SOURCE_REFERENCE
    if len(text) > max_report_chars:
        return REASON_OVER_SIZE_CEILING
    return None


def build_refusal(reason: str) -> Dict[str, Any]:
    """Build the refusal delta: error status, cleared fields, truthy notice.

    Every key in CLEARED_ON_REFUSAL is present in the returned mapping. Presence
    is the point: LangGraph merges partial deltas, so omitting a key leaves the
    previous value standing in state. A delta that simply drops the field looks
    identical to a cleared one from the caller's side of an assertion, and is
    not cleared at all.
    """
    delta: Dict[str, Any] = {
        "status": AgentStatus.ERROR.value,
        "formatted_output": _REFUSAL_NOTICE.format(reason=reason),
        "gate_violation": reason,
        "s3_gate_passed": False,
        "pii_scrubbed": True,
        "error_log": [f"OutputGateNode: summary withheld ({reason})"],
    }
    for field in CLEARED_ON_REFUSAL:
        delta[field] = ""
    return delta


class OutputGateNode(FunctionNode):
    """Release boundary for TRV-C2-001.

    Fifth and final node of the inner domain pipeline. Scrubs residual personal
    data, then refuses release when the composed summary carries a credential,
    references no source booking record, or exceeds the declared size ceiling.
    A refusal clears every field carrying released text and publishes a truthy
    refusal notice in place of the summary.

    Input state keys:
        formatted_output: str — composed summary from FormatterNode
        runtime_limits:   str — JSON bounds seeded into inner state

    Output state keys (partial dict), release path:
        formatted_output: str  — scrubbed summary with the disclaimer appended
        s3_gate_passed:   bool — True
        pii_scrubbed:     bool — True
        status:           AgentStatus.SUCCESS

    Output state keys (partial dict), refusal path:
        formatted_output: str  — truthy refusal notice carrying a reason code
        gate_violation:   str  — the reason code
        every field in CLEARED_ON_REFUSAL: ""
        status:           AgentStatus.ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on a payload that was already
        # declined. Without this the node reports its own precondition failure
        # and the specific, actionable reason is replaced by a vaguer one.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        output: str = state.get("formatted_output") or ""
        max_report_chars = bound_int(read_bounds(state), "max_report_chars")

        # ------------------------------------------------------------------
        # Step 1 — residual personal-data scrub
        # ------------------------------------------------------------------
        scrubbed, pii_was_found = _scrub_pii(output)
        if pii_was_found:
            logger.warning("OutputGateNode: residual personal data masked in the composed summary")

        # ------------------------------------------------------------------
        # Step 2 — release screen, on the text as it would actually ship
        #
        # The disclaimer is appended before the screen so the ceiling is
        # measured against the released document, not against a draft of it.
        # ------------------------------------------------------------------
        candidate = scrubbed + _DISCLAIMER
        reason = screen_release(candidate, max_report_chars)

        if reason is not None:
            logger.warning("OutputGateNode: release refused (%s)", reason)
            emit_trace_event(
                "output_gate_complete",
                {
                    "released": False,
                    "reason": reason,
                    "pii_residual_masked": pii_was_found,
                },
                state,
            )
            return build_refusal(reason)

        # ------------------------------------------------------------------
        # Step 3 — audit trace (outcomes only, never content)
        # ------------------------------------------------------------------
        emit_trace_event(
            "output_gate_complete",
            {
                "released": True,
                "pii_residual_masked": pii_was_found,
                "output_chars": len(candidate),
            },
            state,
        )

        logger.info(
            "OutputGateNode: released summary (%d chars); residual masked=%s",
            len(candidate),
            pii_was_found,
        )

        return {
            "formatted_output": candidate,
            "s3_gate_passed": True,
            "pii_scrubbed": True,
            "status": AgentStatus.SUCCESS.value,
        }
