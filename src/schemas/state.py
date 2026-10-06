"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never a Pydantic BaseModel. LangGraph
# checkpoints use msgpack serialization, and Pydantic objects corrupt
# silently through it. Extend AgentState with agent-specific fields only;
# never add credentials, secrets, or Pydantic models.
#
# TRV-C2-001 — TravelItinerarySummarizationAgent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover the full node pipeline:
#   PreProcessNode (outer) → BookingIngestionNode → SegmentExtractionNode
#   → ItinerarySynthesisNode → FormatterNode → OutputGateNode
#   → PostProcessNode (outer)
#
# This state must never hold passport numbers, payment card numbers, or any
# other personal data. Those fields are stripped by BookingIngestionNode;
# only non-sensitive booking metadata survives.
# dict/list fields are serialized to JSON strings (Optional[str]) for msgpack
# safety; nodes use json.loads/json.dumps when reading and writing them.

from typing import Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for TRV-C2-001.

    All shared fields (user_input, status, result, error_log, session_id,
    correlation_id, trace_id, node_history, hitl_*, etc.) are inherited
    from AgentState.  Only TRV-C2-001-specific fields are declared here.

    Producer / consumer alignment
    ──────────────────────────────
    validated_input       PreProcessNode (outer)     → BookingIngestionNode
    parsed_bookings       BookingIngestionNode        → SegmentExtractionNode
    segment_types_found   BookingIngestionNode        → SegmentExtractionNode
    segments              SegmentExtractionNode       → ItinerarySynthesisNode
    segment_count         SegmentExtractionNode       → ItinerarySynthesisNode
    itinerary_body        ItinerarySynthesisNode      → FormatterNode
    total_cost_jpy        ItinerarySynthesisNode      → FormatterNode
    cancellation_terms    ItinerarySynthesisNode      → FormatterNode
    formatted_output      FormatterNode / OutputGateNode → merge_output() → PostProcessNode
    s3_gate_passed        OutputGateNode              → PostProcessNode (audit)
    pii_scrubbed          OutputGateNode              → PostProcessNode (audit)
    """

    # ------------------------------------------------------------------
    # Input / validated input — PreProcessNode (outer backbone)
    # ------------------------------------------------------------------

    # Validated booking-records string as submitted by the caller.
    # Written by PreProcessNode after input validation.
    validated_input: Optional[str]

    # ------------------------------------------------------------------
    # Domain: BookingIngestionNode
    # ------------------------------------------------------------------

    # JSON-serialized list of normalized, PII-scrubbed booking records.
    # Shape: [{"booking_id": str, "segment_type": str, "date": str,
    #          "origin": str, "destination": str, "cost_jpy": int, ...}]
    # The personal-data fields (passenger_name, passport_number, dob,
    # contact, payment_info) have been stripped.
    parsed_bookings: Optional[str]

    # JSON-serialized list of segment type strings found in the input.
    # e.g. ["flight", "hotel", "transfer", "activity"]
    segment_types_found: Optional[str]

    # ------------------------------------------------------------------
    # Domain: SegmentExtractionNode
    # ------------------------------------------------------------------

    # JSON-serialized list of segments ordered by date (ascending).
    # Each segment is a dict: {"booking_id": str, "segment_type": str,
    # "date": str, "date_label": str, "summary": str, "cost_jpy": int,
    # "cancellation_policy": str, "source_field": str}
    segments: Optional[str]

    # Count of extracted segments (convenience field).
    segment_count: Optional[int]

    # ------------------------------------------------------------------
    # Domain: ItinerarySynthesisNode
    # ------------------------------------------------------------------

    # Day-by-day itinerary body text.
    # Each line references the source booking_id [BK-XXX].
    itinerary_body: Optional[str]

    # Total trip cost in JPY as a formatted string (e.g. "¥245,000").
    total_cost_jpy: Optional[str]

    # Consolidated cancellation terms text, reconciled from per-segment
    # fare/policy rules and bound to source booking_id references.
    cancellation_terms: Optional[str]

    # ------------------------------------------------------------------
    # Domain: FormatterNode / OutputGateNode
    # ------------------------------------------------------------------

    # Formatted itinerary output string (customer-facing + 電子帳簿保存法
    # retention format).  Updated first by FormatterNode, then by
    # OutputGateNode (after PII final-scrub + disclaimer append).
    formatted_output: Optional[str]

    # ------------------------------------------------------------------
    # OutputGateNode gate flags
    # ------------------------------------------------------------------

    # True when the faithful-summary check passed — every itinerary item
    # references a real source booking_id from segments.
    s3_gate_passed: Optional[bool]

    # True when the residual-PII scrub completed without detecting
    # residual PII in formatted_output.
    pii_scrubbed: Optional[bool]

    # ------------------------------------------------------------------
    # Runtime bounds — seeded from config/config.yaml
    # ------------------------------------------------------------------

    # JSON-serialized mapping of the itinerary bounds declared in
    # config/config.yaml (max_bookings, max_field_chars, max_report_chars,
    # min_cost_jpy, max_cost_jpy). Seeded into inner state by
    # DomainWorkflowGraph._extra_initial_state() and into outer state by
    # TravelItinerarySummarizationAgent._extra_initial_state(), so a node can
    # read the declared value from the state it actually runs in.
    runtime_limits: Optional[str]

    # Fixed reason code for the release refusal, when the output gate refuses
    # to release a summary. Never carries caller text or a matched value.
    gate_violation: Optional[str]
    # Set when a run COMPLETES without carrying out the request, because the
    # caller sent a value they can correct. A closed set of codes, never caller
    # content. Nodes downstream of the one that set it do no work and pass it on.
    error_code: Optional[str]
