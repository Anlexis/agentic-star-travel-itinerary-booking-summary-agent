"""AgentCore Platform v1.0"""

# TRV-C2-001 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (ItineraryWorkflowGraphNode) that
#   delegates the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#
# Class-name alignment:
#   class TravelItinerarySummarizationAgent(AgentBaseGraph)  ← real class
#   config/agent.yaml `class: TravelItinerarySummarizationAgent`   ← must match
#   src/api/server.py `from src.graph.graph import Graph`    ← resolved via alias below
#   Graph = TravelItinerarySummarizationAgent                ← back-compat alias
#
# Rules enforced:
#   ✅ TravelItinerarySummarizationAgent inherits AgentBaseGraph (L1 Base)
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ ItineraryWorkflowGraphNode assigned to self._nodes["main"]
#   ✅ merge_output() returns only changed keys
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform SDK imports — framework.* only

import json
from pathlib import Path
from typing import Any, ClassVar, Dict, cast

from framework.schemas.agent_status import AgentStatus
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.graph.base_graph import BaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.utils.config_loader import load_agent_config
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State

# Repo root: src/graph/graph.py -> parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors the itinerary block of config/config.yaml so the bounds are never
# empty in a deployment layout where the file cannot be read.
_FALLBACK_ITINERARY: Dict[str, Any] = {
    "max_bookings": 500,
    "max_field_chars": 400,
    "max_report_chars": 120000,
    "min_cost_jpy": 0,
    "max_cost_jpy": 100000000,
}


def runtime_config() -> Dict[str, Any]:
    """Load config/config.yaml — the live runtime parameters.

    The agent registry loads this file and hands it to the graph constructor;
    the standalone HTTP entry point does the same, so the retry budget and the
    itinerary bounds are live in both deployments rather than declared and
    ignored.

    Reading config/agent.yaml here instead would return nothing usable: that
    file carries identity and compile-time requirements only, and a reader
    pointed at it degrades silently to hard-coded defaults.
    """
    loaded = load_agent_config(_REPO_ROOT)
    return dict(loaded) if isinstance(loaded, dict) else {}


def itinerary_bounds(config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the itinerary bounds from a graph config, falling back to file.

    A graph constructed with no config at all still runs against the shipped
    values rather than against numbers hard-coded here that could drift from
    config/config.yaml.
    """
    source = config if config else runtime_config()
    declared = source.get("itinerary")
    merged = dict(_FALLBACK_ITINERARY)
    if isinstance(declared, dict):
        merged.update(declared)
    return merged


class ItineraryWorkflowGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of TravelItinerarySummarizationAgent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()    — instantiate and return DomainWorkflowGraph (no ctor args)
      extract_input()   — pull validated_input / user_input from outer state
      merge_output()    — map sub_result fields into outer state delta (changed keys only)
      error_strategy    — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (default — fail fast).
    error_strategy: ClassVar[str] = "propagate"

    # False: HITL interrupts are handled inside the inner graph only.
    propagate_hitl: ClassVar[bool] = False

    def _parent_config(self) -> Dict[str, Any]:
        """Runtime bounds handed down to the inner graph.

        The inner graph is constructed per invocation and has no other route to
        the declared configuration, so the resolved bounds travel through its
        constructor. Without this the inner graph would build against an empty
        mapping and every value in config/config.yaml would be inert.
        """
        return {"configurable": {"itinerary": itinerary_bounds(runtime_config())}}

    def execute(self, state: AgentState) -> Dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A declined request has no validated payload to ingest, so running the
        workflow would only reach the first domain node, fail its own
        precondition, and replace the specific, actionable reason already
        settled with a vaguer one.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        return cast(Dict[str, Any], super().execute(state))

    def get_subgraph(self) -> "BaseGraph":
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time. The only constructor argument
        is the graph configuration the base class already accepts.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the input string passed into the inner graph invoke().

        PreProcessNode validates the raw user_input and writes the result to
        validated_input. Prefer validated_input over raw user_input.
        """
        value = state.get("validated_input") or state.get("user_input", "")
        return str(value) if value else ""

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output()   emits: "output", "status", "trace_id",
                                      "correlation_id", "node_history"
          This merge_output() reads: sub_result.get("output") → writes "formatted_output"
                                     sub_result.get("status")

        formatted_output (str | None): final itinerary summary text (PII-scrubbed
          + disclaimer-appended by OutputGateNode).
        status (str | None): terminal AgentStatus value from the inner graph run.
        """
        return {
            "formatted_output": sub_result.get("output"),
            "status": sub_result.get("status"),
            # A reason settled OUTSIDE the subgraph (PreProcessNode) is the real
            # one: reading sub_result alone would overwrite it with the inner
            # blank, since the inner graph never ran.
            "error_code": state.get("error_code") or sub_result.get("error_code"),
        }


class TravelItinerarySummarizationAgent(AgentBaseGraph):
    """Outer graph for TRV-C2-001 (Cat 2).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in ItineraryWorkflowGraphNode (main slot), which
    delegates to DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode  (trust + input gate, VERIFIED_EXTERNAL)
      - main:        ItineraryWorkflowGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (forwards formatted_output to result)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "TravelItinerarySummarizationAgent"

    @property
    def state_schema(self) -> type:
        return State

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the live runtime bounds into the outer state.

        Nodes take no constructor arguments, so a backbone node that needs a
        declared bound can only read it from state. Structured state fields are
        stored as JSON strings, so the mapping is serialized here.
        """
        return {"runtime_limits": json.dumps(itinerary_bounds(self.config), ensure_ascii=False)}

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ItineraryWorkflowGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Back-compat alias for callers that import the shorter name.
Graph = TravelItinerarySummarizationAgent
