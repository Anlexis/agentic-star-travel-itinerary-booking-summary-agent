"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from other agents
#
# TRV-C2-001 — ItinerarySynthesisNode
# Inner domain graph node 3 (of 5): compose the day-by-day itinerary body,
# compute total trip cost, and reconcile cancellation terms from the
# date-ordered segments produced by SegmentExtractionNode.
#
# Every itinerary line is bound to its source [BK-<booking_id>], which is what
# the release gate in OutputGateNode checks for.
#
# Input:  state["segments"]       — JSON list of date-ordered segment dicts
#         state["segment_count"]  — integer count
# Output: state["itinerary_body"]     — day-by-day itinerary text
#         state["total_cost_jpy"]     — total trip cost as formatted string
#         state["cancellation_terms"] — consolidated cancellation terms text
#
# Audit: emit_trace_event once per execute() (non-sensitive aggregates only)

import json
import logging
from collections import defaultdict
from typing import Any, ClassVar, Dict, List, Optional, Set

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _group_by_date(segments: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group segments by date_label (preserving order via insertion order of dict)."""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for seg in segments:
        label = seg.get("date_label") or seg.get("date") or "日付不明"
        groups[label].append(seg)
    return dict(groups)


def _compose_itinerary_body(groups: Dict[str, List[Dict[str, Any]]]) -> str:
    """Build the day-by-day itinerary body text.

    Each segment line is bound to its source booking_id in [BK-XXX] format.
    """
    lines: List[str] = []
    for day_label, day_segs in groups.items():
        lines.append(f"## {day_label}")
        for seg in day_segs:
            # The summary already embeds the [BK-booking_id] reference
            # (built by SegmentExtractionNode._build_segment_summary)
            lines.append(f"- {seg['summary']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _compute_total_cost(segments: List[Dict[str, Any]]) -> str:
    """Sum all segment costs and return a formatted JPY string."""
    total = sum(int(s.get("cost_jpy") or 0) for s in segments)
    return f"¥{total:,.0f}"


def _reconcile_cancellation_terms(segments: List[Dict[str, Any]]) -> str:
    """Reconcile per-segment cancellation policies into a consolidated summary.

    Each term is bound to its source [BK-<booking_id>].
    """
    lines: List[str] = []
    seen: Set[str] = set()
    for seg in segments:
        policy = (seg.get("cancellation_policy") or "").strip()
        booking_id = seg.get("booking_id", "UNKNOWN")
        seg_type = seg.get("segment_type", "")
        if not policy:
            policy = "条件要確認（予約原本参照）"
        # Deduplicate identical policies while keeping traceability
        key = f"[BK-{booking_id}]"
        if key not in seen:
            seen.add(key)
            lines.append(f"- {seg_type} [BK-{booking_id}]: {policy}")
    if not lines:
        lines.append("- 取消条件は予約原本（確定書面）でご確認ください。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class ItinerarySynthesisNode(FunctionNode):
    """Compose day-by-day itinerary from date-ordered segments.

    Third inner domain node for TRV-C2-001 TravelItinerarySummarizationAgent.

    Produces:
      itinerary_body     — day-by-day narrative, each line bound to [BK-XXX]
      total_cost_jpy     — summed trip cost as a formatted ¥ string
      cancellation_terms — consolidated cancellation terms per segment

    All three outputs are shaped so the release gate in OutputGateNode can
    check them: every itinerary item, cost figure and cancellation term
    traces back to a real input booking_id from segments.

    Input state keys:
        segments:      str — JSON list of date-ordered segment dicts
        segment_count: int — count (for quick zero-check)

    Output state keys (partial dict):
        itinerary_body:     str — day-by-day itinerary text
        total_cost_jpy:     str — formatted total cost (e.g. "¥245,000")
        cancellation_terms: str — reconciled per-segment cancellation terms
        status:             AgentStatus.SUCCESS
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

        segments_raw: str = state.get("segments") or "[]"

        # ------------------------------------------------------------------
        # Step 1 — parse segments
        # ------------------------------------------------------------------
        try:
            segments: List[Dict[str, Any]] = json.loads(segments_raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("ItinerarySynthesisNode: could not parse segments")
            emit_trace_event(
                "itinerary_synthesis_complete",
                {"segment_count": 0, "error": "parse_failure"},
                state,
            )
            return {
                "itinerary_body": "（予約データの解析に失敗しました）",
                "total_cost_jpy": "¥0",
                "cancellation_terms": "（取消条件の取得に失敗しました）",
                "status": AgentStatus.SUCCESS.value,
            }

        if not isinstance(segments, list):
            segments = []

        # ------------------------------------------------------------------
        # Step 2 — zero-segment fallback
        # ------------------------------------------------------------------
        if not segments:
            logger.info("ItinerarySynthesisNode: no segments to synthesize")
            emit_trace_event(
                "itinerary_synthesis_complete",
                {"segment_count": 0, "days": 0},
                state,
            )
            return {
                "itinerary_body": "（予約セグメントが見つかりませんでした）",
                "total_cost_jpy": "¥0",
                "cancellation_terms": "- 取消条件は予約原本（確定書面）でご確認ください。",
                "status": AgentStatus.SUCCESS.value,
            }

        # ------------------------------------------------------------------
        # Step 3 — compose day-by-day itinerary body
        # ------------------------------------------------------------------
        groups = _group_by_date(segments)
        itinerary_body = _compose_itinerary_body(groups)

        # ------------------------------------------------------------------
        # Step 4 — compute total cost
        # ------------------------------------------------------------------
        total_cost_jpy = _compute_total_cost(segments)

        # ------------------------------------------------------------------
        # Step 5 — reconcile cancellation terms
        # ------------------------------------------------------------------
        cancellation_terms = _reconcile_cancellation_terms(segments)

        # ------------------------------------------------------------------
        # Step 6 — audit trace (non-sensitive aggregates only)
        # ------------------------------------------------------------------
        emit_trace_event(
            "itinerary_synthesis_complete",
            {
                "segment_count": len(segments),
                "days": len(groups),
                "total_cost_computed": bool(total_cost_jpy),
            },
            state,
        )

        logger.info(
            "ItinerarySynthesisNode: synthesized %d segments across %d days; total=%s",
            len(segments),
            len(groups),
            total_cost_jpy,
        )

        return {
            "itinerary_body": itinerary_body,
            "total_cost_jpy": total_cost_jpy,
            "cancellation_terms": cancellation_terms,
            "status": AgentStatus.SUCCESS.value,
        }
