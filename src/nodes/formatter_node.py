"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from other agents
#
# TRV-C2-001 — FormatterNode
# Inner domain graph node 4 (of 5): structure the synthesized itinerary for
# (a) customer-facing presentation and (b) 電子帳簿保存法 digital-retention
# compliance. The hardcoded disclaimer is appended by OutputGateNode.
#
# Input:  state["itinerary_body"]     — day-by-day itinerary text
#         state["total_cost_jpy"]     — formatted total cost string
#         state["cancellation_terms"] — consolidated cancellation terms
# Output: state["formatted_output"]  — structured itinerary text ready for
#                                       OutputGateNode (release gate)
#
# Audit: emit_trace_event once per execute() (non-sensitive metrics only)

import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Template header and section separators.
_HEADER_TEMPLATE = """\
# 旅行日程サマリー（AI生成要約）
## TravelItinerarySummarizationAgent — TRV-C2-001
---
"""

_TOTAL_COST_SECTION = "## 旅行合計金額\n{total_cost_jpy}\n"

_CANCELLATION_SECTION = "## 取消条件サマリー\n{cancellation_terms}\n"

# 電子帳簿保存法 metadata section template (non-PII metadata only).
_CHOCHOSOHN_TEMPLATE = """\
---
### 電子帳簿保存法 保管メタデータ
- 文書種別: 旅程サマリー（AI生成）
- 生成日時: {timestamp_utc}
- テンプレートID: TRV-C2-001
- 保管形式: テキスト / JSON互換
---"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_timestamp() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_itinerary(
    itinerary_body: str,
    total_cost_jpy: str,
    cancellation_terms: str,
    timestamp_utc: str,
) -> str:
    """Assemble the customer-facing + 電子帳簿保存法 formatted output.

    The hardcoded disclaimer is NOT appended here — OutputGateNode appends it
    once the release checks have passed.
    """
    sections = [
        _HEADER_TEMPLATE.rstrip(),
        "",
        "## 日程詳細",
        itinerary_body or "（日程情報なし）",
        "",
        _TOTAL_COST_SECTION.format(total_cost_jpy=total_cost_jpy or "¥0"),
        _CANCELLATION_SECTION.format(cancellation_terms=cancellation_terms or "（取消条件なし）"),
        _CHOCHOSOHN_TEMPLATE.format(timestamp_utc=timestamp_utc),
    ]
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class FormatterNode(FunctionNode):
    """Structure the synthesized itinerary for customer-facing + 電子帳簿保存法 output.

    Fourth inner domain node for TRV-C2-001 TravelItinerarySummarizationAgent.

    Assembles the formatted output string from itinerary_body, total_cost_jpy,
    and cancellation_terms produced by ItinerarySynthesisNode. The
    non-suppressible disclaimer is appended by the subsequent OutputGateNode,
    so this node does NOT append it here.

    Input state keys:
        itinerary_body:     str — day-by-day itinerary text (each line [BK-XXX])
        total_cost_jpy:     str — formatted total trip cost
        cancellation_terms: str — reconciled per-segment cancellation terms

    Output state keys (partial dict):
        formatted_output: str — structured itinerary (pre-gate; no disclaimer yet)
        status: AgentStatus.SUCCESS
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

        itinerary_body: str = state.get("itinerary_body") or "（日程情報なし）"
        total_cost_jpy: str = state.get("total_cost_jpy") or "¥0"
        cancellation_terms: str = state.get("cancellation_terms") or "（取消条件なし）"

        timestamp_utc = _utc_timestamp()

        # ------------------------------------------------------------------
        # Step 1 — assemble formatted output
        # ------------------------------------------------------------------
        formatted = _format_itinerary(
            itinerary_body=itinerary_body,
            total_cost_jpy=total_cost_jpy,
            cancellation_terms=cancellation_terms,
            timestamp_utc=timestamp_utc,
        )

        # ------------------------------------------------------------------
        # Step 2 — audit trace (length metric only — no content)
        # ------------------------------------------------------------------
        emit_trace_event(
            "formatter_complete",
            {
                "output_chars": len(formatted),
                "has_itinerary": bool(itinerary_body and "日程情報なし" not in itinerary_body),
                "has_cost": bool(total_cost_jpy and total_cost_jpy != "¥0"),
            },
            state,
        )

        logger.info(
            "FormatterNode: formatted itinerary (%d chars); cost=%s",
            len(formatted),
            total_cost_jpy,
        )

        return {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }
