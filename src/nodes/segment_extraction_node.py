"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from other agents
#
# TRV-C2-001 — SegmentExtractionNode
# Inner domain graph node 2 (of 5): extract multi-type booking segments
# from parsed_bookings and date-order them into a coherent cross-date
# timeline. Each segment carries its source booking_id, so every line of the
# released summary can be checked against the booking it came from.
#
# Input:  state["parsed_bookings"] — JSON list of sanitized booking records
# Output: state["segments"]        — JSON list of date-ordered segment dicts
#         state["segment_count"]   — integer count of extracted segments
#
# Audit: emit_trace_event once per execute() (non-sensitive counts only)

import json
import logging
from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_segment_summary(record: Dict[str, Any]) -> str:
    """Build a human-readable one-line segment summary from a booking record.

    The summary is bound to the source booking_id. No personal-data field is
    referenced — those were stripped by BookingIngestionNode.
    """
    seg_type = record.get("segment_type", "unknown")
    booking_id = record.get("booking_id", "—")
    origin = record.get("origin") or record.get("origin_iata", "")
    destination = record.get("destination") or record.get("destination_iata", "")
    hotel = record.get("hotel_name", "")
    activity = record.get("activity_name", "")
    flight_num = record.get("flight_number", "")
    departure = record.get("departure_time", "")
    arrival = record.get("arrival_time", "")
    check_in = record.get("check_in", "")
    check_out = record.get("check_out", "")

    if seg_type == "flight":
        parts = [f"フライト [BK-{booking_id}]"]
        if flight_num:
            parts.append(f"便名: {flight_num}")
        if origin and destination:
            parts.append(f"{origin} → {destination}")
        if departure:
            parts.append(f"出発: {departure}")
        if arrival:
            parts.append(f"到着: {arrival}")
        return " | ".join(parts)

    if seg_type == "hotel":
        parts = [f"ホテル [BK-{booking_id}]"]
        if hotel:
            parts.append(hotel)
        if destination:
            parts.append(destination)
        if check_in:
            parts.append(f"チェックイン: {check_in}")
        if check_out:
            parts.append(f"チェックアウト: {check_out}")
        return " | ".join(parts)

    if seg_type == "transfer":
        parts = [f"送迎・乗り継ぎ [BK-{booking_id}]"]
        transfer_type = record.get("transfer_type", "")
        if transfer_type:
            parts.append(transfer_type)
        if origin and destination:
            parts.append(f"{origin} → {destination}")
        return " | ".join(parts)

    if seg_type == "activity":
        parts = [f"アクティビティ [BK-{booking_id}]"]
        if activity:
            parts.append(activity)
        if destination:
            parts.append(destination)
        return " | ".join(parts)

    # Fallback for other segment types
    parts = [f"{seg_type} [BK-{booking_id}]"]
    if origin:
        parts.append(origin)
    if destination:
        parts.append(destination)
    return " | ".join(parts)


def _date_label(date_str: str) -> str:
    """Derive a display-friendly date label from an ISO 8601 date string."""
    if not date_str:
        return "日付不明"
    # date_str may be "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS..."
    return date_str[:10]


def _extract_segments(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build date-ordered segment dicts from sanitized booking records.

    Each segment dict carries:
      booking_id, segment_type, date, date_label, summary,
      cost_jpy, cancellation_policy, source_field="booking_id"
    The "source_field" key records where the traceability marker came from.
    """
    segments: List[Dict[str, Any]] = []
    for rec in records:
        booking_id = rec.get("booking_id", "UNKNOWN")
        date_str = rec.get("date", "")
        cost = rec.get("cost_jpy", 0)
        # The numeric contract is owned by BookingIngestionNode, which refuses
        # a record whose cost is non-finite or out of range. This conversion is
        # the arithmetic step, not a second gate: it must not raise on a value
        # that reached it anyway, so the whole conversion error set is caught.
        try:
            cost_int = int(cost) if cost is not None else 0
        except (ValueError, TypeError, OverflowError):
            cost_int = 0

        segment: Dict[str, Any] = {
            "booking_id": booking_id,
            "segment_type": rec.get("segment_type", "unknown"),
            "date": date_str,
            "date_label": _date_label(date_str),
            "summary": _build_segment_summary(rec),
            "cost_jpy": cost_int,
            "cancellation_policy": rec.get("cancellation_policy", ""),
            # Traceability: every segment is bound to its source booking_id
            "source_field": "booking_id",
        }
        segments.append(segment)

    # Sort by date ascending (ISO 8601 strings sort lexicographically)
    segments.sort(key=lambda s: (s["date"] or "", s["booking_id"]))
    return segments


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class SegmentExtractionNode(FunctionNode):
    """Extract and date-order multi-type travel segments.

    Second inner domain node for TRV-C2-001 TravelItinerarySummarizationAgent.

    Reads parsed_bookings (JSON list from BookingIngestionNode) and produces
    a date-ordered sequence of segment dicts, each bound to its source
    booking_id, which is what the release gate checks for.

    Input state keys:
        parsed_bookings: str — JSON list of sanitized booking records

    Output state keys (partial dict):
        segments:      str — JSON list of date-ordered segment dicts
        segment_count: int — number of segments extracted
        status:        AgentStatus.SUCCESS
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

        raw: str = state.get("parsed_bookings") or "[]"

        # ------------------------------------------------------------------
        # Step 1 — parse bookings
        # ------------------------------------------------------------------
        try:
            records: List[Dict[str, Any]] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("SegmentExtractionNode: could not parse parsed_bookings")
            emit_trace_event(
                "segment_extraction_complete",
                {"segment_count": 0, "error": "parse_failure"},
                state,
            )
            return {
                "segments": json.dumps([]),
                "segment_count": 0,
                "status": AgentStatus.SUCCESS.value,
            }

        if not isinstance(records, list):
            records = []

        # ------------------------------------------------------------------
        # Step 2 — extract and date-order segments
        # ------------------------------------------------------------------
        segments = _extract_segments(records)
        segment_count = len(segments)

        # ------------------------------------------------------------------
        # Step 3 — audit trace (non-sensitive counts only)
        # ------------------------------------------------------------------
        type_counts: Dict[str, int] = {}
        for seg in segments:
            st = seg["segment_type"]
            type_counts[st] = type_counts.get(st, 0) + 1

        emit_trace_event(
            "segment_extraction_complete",
            {
                "segment_count": segment_count,
                "type_breakdown": type_counts,
            },
            state,
        )

        logger.info(
            "SegmentExtractionNode: extracted %d segments; breakdown: %s",
            segment_count,
            type_counts,
        )

        return {
            "segments": json.dumps(segments),
            "segment_count": segment_count,
            "status": AgentStatus.SUCCESS.value,
        }
