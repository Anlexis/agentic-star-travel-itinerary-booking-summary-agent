"""PB-6: Backbone Invoke-Order Verification — TRV-C2-001

Full Graph().invoke() over a SUCCESS-yielding payload with an EXTERNAL caller,
asserting the backbone node_history order matches:
  [InitializeNode, PreProcessNode, ItineraryWorkflowGraphNode, PostProcessNode, FinalizeNode]

Rules:
- Uses InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL), NOT for_internal().
  An internal context masks a trust mismatch in the inner nodes: a real external
  VERIFIED_EXTERNAL(1) caller would be denied by an INTERNAL(2) inner gate, and a test
  built on an internal context would never see it.
- Asserts result["output"] (not result["formatted_output"]).
- Compares result["status"] == AgentStatus.SUCCESS.value (the enum's string value).
- invoke(user_input=..., ctx=...) — not text= or input_context=.
- Do NOT assert raw booking IDs in result["output"] — FinalizeNode may mask identifiers.
"""

import json
import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

# The main-slot GraphNode class (from src/graph/graph.py)
from src.graph.graph import ItineraryWorkflowGraphNode as _MAIN_SLOT_NODE

# A SUCCESS-yielding payload: valid booking records with required fields.
# - booking_id is required for the [BK-*] source references the release gate checks.
# - cost_jpy is required for total cost summation (ItinerarySynthesisNode).
# - cancellation_policy is required for cancellation terms section.
# No PII fields — APPI-clean by design.
_VALID_PAYLOAD = json.dumps(
    [
        {
            "booking_id": "TRV001",
            "segment_type": "flight",
            "date": "2026-08-15",
            "origin": "HND",
            "destination": "SIN",
            "flight_number": "SQ637",
            "departure_time": "09:30",
            "arrival_time": "15:45",
            "cost_jpy": 85000,
            "cancellation_policy": "出発72時間前まで無料",
        },
        {
            "booking_id": "TRV002",
            "segment_type": "hotel",
            "date": "2026-08-15",
            "hotel_name": "Marina Bay Hotel",
            "destination": "Singapore",
            "check_in": "2026-08-15",
            "check_out": "2026-08-18",
            "cost_jpy": 120000,
            "cancellation_policy": "チェックイン48時間前まで無料",
        },
    ]
)


class TestBackboneInvokeOrder:
    """PB-6: Full backbone invoke order with a real external caller."""

    @pytest.fixture(autouse=True)
    def build_graph(self):
        """Construct the agent once per test."""
        from src.graph.graph import TravelItinerarySummarizationAgent

        self.graph = TravelItinerarySummarizationAgent()
        self.ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)

    def test_backbone_invoke_order(self):
        """PB-6 core: backbone node_history == [Initialize, PreProcess, Main, PostProcess, Finalize]."""
        result = self.graph.invoke(user_input=_VALID_PAYLOAD, ctx=self.ctx)

        # 1. Overall status must be SUCCESS for the full backbone to run
        assert result["status"] == AgentStatus.SUCCESS.value, (
            f"Expected AgentStatus.SUCCESS.value, got {result['status']!r}. " f"error_log: {result.get('error_log')}"
        )

        # 2. node_history must contain exactly the 5 backbone nodes in order
        node_history = result.get("node_history", [])
        expected_backbone = [
            "InitializeNode",
            "PreProcessNode",
            _MAIN_SLOT_NODE.__name__,  # "ItineraryWorkflowGraphNode"
            "PostProcessNode",
            "FinalizeNode",
        ]
        assert len(node_history) == len(expected_backbone), (
            f"Expected {len(expected_backbone)} backbone nodes, got {len(node_history)}.\n"
            f"node_history: {node_history}"
        )
        assert node_history == expected_backbone, (
            f"Backbone execution order mismatch.\n" f"Expected : {expected_backbone}\n" f"Actual   : {node_history}"
        )

    def test_output_is_populated(self):
        """PB-6: result['output'] is a non-empty string (not None, not 'formatted_output')."""
        result = self.graph.invoke(user_input=_VALID_PAYLOAD, ctx=self.ctx)

        # Assert result["output"], NOT result.get("formatted_output")
        assert result.get("output"), "result['output'] must be a non-empty string. " f"Got: {result.get('output')!r}"
        assert isinstance(result["output"], str), f"result['output'] must be str, got {type(result['output'])}"

    def test_output_contains_itinerary_header(self):
        """PB-6: itinerary header is present in the final output."""
        result = self.graph.invoke(user_input=_VALID_PAYLOAD, ctx=self.ctx)
        assert result["status"] == AgentStatus.SUCCESS.value
        # The formatter always writes the 旅行日程サマリー header
        assert "旅行日程サマリー" in result["output"], (
            "Expected itinerary header in output. " f"Output (first 400 chars): {result['output'][:400]}"
        )

    def test_output_contains_disclaimer(self):
        """PB-6: non-suppressible AI disclaimer is always present (OutputGateNode hardcodes it)."""
        result = self.graph.invoke(user_input=_VALID_PAYLOAD, ctx=self.ctx)
        assert result["status"] == AgentStatus.SUCCESS.value
        # The disclaimer is hardcoded and non-suppressible
        assert "免責事項" in result["output"], (
            "Non-suppressible 免責事項 disclaimer must appear in every itinerary output. "
            f"Output (first 400 chars): {result['output'][:400]}"
        )

    def test_trust_level_external_path_succeeds(self):
        """PB-6: VERIFIED_EXTERNAL caller is admitted by ANONYMOUS inner nodes (trust order ANONYMOUS <= all)."""
        # A VERIFIED_EXTERNAL(1) caller satisfies ANONYMOUS(0) gate: 0 <= 1 → ADMIT.
        # This test catches the trust-trap: if inner nodes were INTERNAL(2), VERIFIED_EXTERNAL(1)
        # would be denied (1 < 2) → SubgraphError → backbone short-circuits before PostProcess.
        result = self.graph.invoke(user_input=_VALID_PAYLOAD, ctx=self.ctx)
        assert result["status"] == AgentStatus.SUCCESS.value, (
            "VERIFIED_EXTERNAL caller must be admitted by ANONYMOUS inner domain nodes. "
            "A non-SUCCESS status here indicates an inner trust-level misconfiguration. "
            f"Got: {result['status']!r}"
        )

    def test_result_keys_present(self):
        """PB-6: invoke result dict has the expected top-level keys."""
        result = self.graph.invoke(user_input=_VALID_PAYLOAD, ctx=self.ctx)
        for key in ("output", "status", "trace_id", "correlation_id", "node_history"):
            assert key in result, f"Expected key '{key}' in invoke result, got keys: {list(result.keys())}"
