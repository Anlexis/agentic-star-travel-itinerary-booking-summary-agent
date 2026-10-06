"""AgentCore Platform v1.0"""

# TRV-C2-001 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full travel itinerary summarization domain workflow:
#
#   START
#     → booking_ingest    (BookingIngestionNode)  validate + strip personal data
#     → segment_extract   (SegmentExtractionNode) date-order multi-type segments
#     → itinerary_synth   (ItinerarySynthesisNode) day-by-day compose + cost + terms
#     → formatter         (FormatterNode)          customer-facing + 電子帳簿保存法 format
#     → output_gate       (OutputGateNode)         release boundary + disclaimer
#   → END
#
# Called by ItineraryWorkflowGraphNode.get_subgraph() in graph.py.
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all 7 BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ All domain nodes instantiated with NO ctor args (SDK-v1 contract)
#   ✅ get_output() designed together with ItineraryWorkflowGraphNode.merge_output()
#   ❌ No platform SDK imports — framework.* only
#   ❌ Not placed under src/subagents/

import json
from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from src.nodes.booking_ingestion_node import BookingIngestionNode
from src.nodes.formatter_node import FormatterNode
from src.nodes.itinerary_synthesis_node import ItinerarySynthesisNode
from src.nodes.output_gate_node import OutputGateNode
from src.nodes.segment_extraction_node import SegmentExtractionNode
from src.schemas.state import State


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for TRV-C2-001.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by ItineraryWorkflowGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → booking_ingest   (BookingIngestionNode)
          → segment_extract  (SegmentExtractionNode)
          → itinerary_synth  (ItinerarySynthesisNode)
          → formatter        (FormatterNode)
          → output_gate      (OutputGateNode)
          → END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    All domain nodes declare required_trust_level = TrustLevel.ANONYMOUS, so a
    verified external caller admitted at the backbone is not denied here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "trv_itinerary_summarization_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Validate inner graph config before compilation.

        The bounds arrive from the outer graph, which resolves them from
        config/config.yaml and always supplies a complete mapping. An inner
        graph built directly — in a test, or by a host that instantiates the
        pipeline on its own — carries none, and falls back to the shipped
        values at seeding time rather than failing to compile.
        """
        configurable = self.config.get("configurable")
        if configurable is not None and not isinstance(configurable, dict):
            raise ValueError(
                "DomainWorkflowGraph: config['configurable'] must be a mapping "
                f"when present, got {type(configurable).__name__}."
            )

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the resolved itinerary bounds into INNER state.

        The framework hands a nested graph only the input string, so the inner
        nodes have no other route to the declared bounds. Seeding here — rather
        than relying on the outer state — is what makes the ingestion cap and
        the released-summary ceiling read a key that exists in their own scope;
        a layer reading an outer key from inner state would compare against an
        empty mapping on every real invocation and be silently dead.
        """
        from src.graph.graph import itinerary_bounds

        configurable = self.config.get("configurable")
        declared = configurable.get("itinerary") if isinstance(configurable, dict) else None
        bounds = declared if isinstance(declared, dict) and declared else itinerary_bounds({})
        return {"runtime_limits": json.dumps(bounds, ensure_ascii=False)}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 inner domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().

        SDK-v1 contract: nodes are instantiated with NO constructor arguments.
        Config flows per-call via execute(self, state, config=None).
        """
        self._nodes["booking_ingest"] = BookingIngestionNode()
        self._nodes["segment_extract"] = SegmentExtractionNode()
        self._nodes["itinerary_synth"] = ItinerarySynthesisNode()
        self._nodes["formatter"] = FormatterNode()
        self._nodes["output_gate"] = OutputGateNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear itinerary summarization domain topology.

        Flow:
          BookingIngest → SegmentExtract → ItinerarySynth → Formatter → OutputGate

        The topology is intentionally linear — no conditional branching
        between domain nodes. route() is implemented as required by the ABC
        but add_conditional_edges() is not used.
        """
        self._sg.add_edge(START, "booking_ingest")
        self._sg.add_edge("booking_ingest", "segment_extract")
        self._sg.add_edge("segment_extract", "itinerary_synth")
        self._sg.add_edge("itinerary_synth", "formatter")
        self._sg.add_edge("formatter", "output_gate")
        self._sg.add_edge("output_gate", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: AgentState) -> str:
        """Conditional routing — required by BaseGraph ABC.

        For this linear topology add_conditional_edges() is not used, so this
        method is never called at runtime. Returns END on error so an
        unexpected call does not re-enter a processing node.
        """
        return END

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by ItineraryWorkflowGraphNode.merge_output()
        in graph.py as the `sub_result` argument. Both methods are designed
        together to guarantee field-name consistency:

            Inner get_output()   emits: "output", "status", "trace_id",
                                        "correlation_id", "node_history"
            Outer merge_output() reads: sub_result.get("output") → writes "formatted_output"
                                        sub_result.get("status")

        "output" carries the final PII-scrubbed itinerary summary with
        disclaimer (written by OutputGateNode into state["formatted_output"]).
        """
        return {
            # The reason must leave the subgraph or the outer graph has no way
            # to tell a declined request from a produced-nothing one.
            "error_code": state.get("error_code"),
            "output": state.get("formatted_output"),
            "status": state.get("status"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
