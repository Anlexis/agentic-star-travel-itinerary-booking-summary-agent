# Template Design Specification — TRV-C2-001 TravelItinerarySummarizationAgent

## Template metadata

| Field | Value |
|---|---|
| Template ID | TRV-C2-001 |
| Agent class | `TravelItinerarySummarizationAgent` |
| Category | Cat 2 — multi-step domain workflow (document-generation pattern) |
| Industry | TRV (Travel) |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Pattern | Two-layer nested: a GraphNode in the `main` slot wraps the itinerary workflow |
| Generation mode | Deterministic — the summary is composed from the booking records; no model is invoked |

**Three-layer separation**

- **State**: flat `TypedDict` composition (no Pydantic — msgpack incompatible)
- **Node**: framework inheritance (`FunctionNode`; override `execute(self, state) -> dict` only)
- **Graph**: composition (`register_nodes()` for node substitution; outer `AgentBaseGraph` + inner `BaseGraph`)

---

## Architecture Overview

TRV-C2-001 uses the **Cat 2 nested DocGen architecture**: an outer `AgentBaseGraph` backbone with a `GraphNode` subclass (`ItineraryWorkflowGraphNode`) in the `main` slot, which delegates the full domain pipeline to an inner `DomainWorkflowGraph(BaseGraph)` at `src/graph/domain_workflow_graph.py`.

```
Outer backbone (AgentBaseGraph):
  START → initialize → pre_process → main → {route} → post_process → finalize → END
                                        ↓ (RETRY, max 3)
                                     pre_process

  main slot = ItineraryWorkflowGraphNode(GraphNode)
              → get_subgraph() → DomainWorkflowGraph

Inner graph (DomainWorkflowGraph ← BaseGraph):
  START
    → booking_ingest   (BookingIngestionNode)
    → segment_extract  (SegmentExtractionNode)
    → itinerary_synth  (ItinerarySynthesisNode)
    → formatter        (FormatterNode)
    → output_gate      (OutputGateNode)
  → END
```

---

## Node Configuration

### Outer Backbone Nodes (AgentBaseGraph slots)

| Slot | Node | Trust Level | Key Responsibility |
|------|------|-------------|-------------------|
| `initialize` | `InitializeNode` (framework default) | — | Sets schema_version, session_id, trust_level |
| `pre_process` | `PreProcessNode` | `VERIFIED_EXTERNAL` | Input gate: empty / size / injection check; writes `validated_input`. An empty or oversized request stops the run carrying its reason; spliced directives terminate it (see "Two ways a request stops") |
| `main` | `ItineraryWorkflowGraphNode` | (delegates to inner) | GraphNode wrapper; calls `DomainWorkflowGraph.invoke()` |
| `post_process` | `PostProcessNode` | `ANONYMOUS` | Forwards `formatted_output` → `result`; audit trace. For a run stopped upstream on a correctable value, renders that reason's sentence as the body instead of forwarding an empty document |
| `finalize` | `FinalizeNode` (framework default) | — | Builds `response_metadata`, `total_time_ms` |

### Inner Domain Nodes (DomainWorkflowGraph; all `TrustLevel.ANONYMOUS`)

| Step | Node | Key Logic |
|------|------|-----------|
| 1 | `BookingIngestionNode` | Owns the caller contract: parses and normalizes the booking records, enforces every declared bound (record cap, per-field character ceiling, finite cost range), and strips the personal-data fields (`passenger_name`, `passport_number`, `dob`, `contact_info`, `payment_info`) before any downstream processing. Fails closed, naming the field. Every bound it enforces is a value the caller can correct, so a breach stops the run carrying the reason rather than terminating it |
| 2 | `SegmentExtractionNode` | Extract flight/hotel/transfer/activity segments; date-order ascending; every segment carries `[BK-<booking_id>]` back to its source record |
| 3 | `ItinerarySynthesisNode` | Compose day-by-day itinerary body; sum `total_cost_jpy`; reconcile `cancellation_terms` from per-segment fare rules; each line bound to source `booking_id` |
| 4 | `FormatterNode` | Structure for customer-facing presentation + 電子帳簿保存法 digital-retention compliance; embeds UTC timestamp + document metadata |
| 5 | `OutputGateNode` | The release boundary: residual personal-data scrub, credential screen (delegated to the framework detector), faithful-summary check, size ceiling, and the hardcoded non-suppressible disclaimer. A refusal clears every field carrying released text and publishes a truthy notice carrying a fixed reason code |

---

## Data Flow

```
user_input (JSON booking records)
    │
    ▼ PreProcessNode (size + injection check)
validated_input
    │
    ▼ BookingIngestionNode (bounds + schema + personal-data strip)
parsed_bookings (JSON str of sanitized records)
segment_types_found
    │
    ▼ SegmentExtractionNode (date-order)
segments (JSON str of date-ordered segment dicts, each with [BK-*])
segment_count
    │
    ▼ ItinerarySynthesisNode (compose)
itinerary_body (day-by-day text with [BK-*] references)
total_cost_jpy
cancellation_terms
    │
    ▼ FormatterNode (structure)
formatted_output (customer-facing + 電子帳簿保存法 format, no disclaimer yet)
    │
    ▼ OutputGateNode (release gate + disclaimer)
formatted_output (PII-scrubbed + 【免責事項】disclaimer)
s3_gate_passed
pii_scrubbed
    │
    ▼ merge_output() → PostProcessNode
result (→ FinalizeNode → caller)
```

---

## State Schema (`src/schemas/state.py`)

Flat `TypedDict` extending `AgentState`. All dict/list structured fields are serialized to `Optional[str]` (JSON strings) for msgpack safety.

| Field | Type | Producer | Consumer | Notes |
|-------|------|----------|----------|-------|
| `validated_input` | `Optional[str]` | PreProcessNode | BookingIngestionNode | Validated and stripped input |
| `parsed_bookings` | `Optional[str]` | BookingIngestionNode | SegmentExtractionNode | JSON list of sanitized records |
| `segment_types_found` | `Optional[str]` | BookingIngestionNode | (audit) | JSON list of type strings |
| `segments` | `Optional[str]` | SegmentExtractionNode | ItinerarySynthesisNode | JSON list of date-ordered segment dicts |
| `segment_count` | `Optional[int]` | SegmentExtractionNode | ItinerarySynthesisNode | Convenience count |
| `itinerary_body` | `Optional[str]` | ItinerarySynthesisNode | FormatterNode | Day-by-day text |
| `total_cost_jpy` | `Optional[str]` | ItinerarySynthesisNode | FormatterNode | Formatted ¥ string |
| `cancellation_terms` | `Optional[str]` | ItinerarySynthesisNode | FormatterNode | Per-segment terms |
| `formatted_output` | `Optional[str]` | FormatterNode → OutputGateNode → merge_output() | PostProcessNode | Final itinerary |
| `s3_gate_passed` | `Optional[bool]` | OutputGateNode | (audit) | Faithful-summary check result |
| `pii_scrubbed` | `Optional[bool]` | OutputGateNode | (audit) | Residual-scrub completion flag |
| `runtime_limits` | `Optional[str]` | `_extra_initial_state()` on both graphs | BookingIngestionNode, OutputGateNode | JSON bounds resolved from `config/config.yaml` |
| `gate_violation` | `Optional[str]` | OutputGateNode | (audit) | Fixed refusal reason code; never caller text |
| `error_code` | `Optional[str]` | PreProcessNode, BookingIngestionNode | PostProcessNode, every downstream node | Reason marker for a run stopped on a correctable value; read by PostProcessNode to pick the sentence, never published in the caller envelope |

---

## Two ways a request stops

Nothing composed reaches the caller on either path — no itinerary, no totals, no
cancellation terms. The paths differ only in how the stop is REPORTED back.

| Path | What takes it | How the run ends | What the caller reads |
|---|---|---|---|
| completes carrying a reason | a value the caller can correct: an empty request, one over the payload size cap, a payload that is not a booking document, one carrying no records, one over the declared record cap, or a record field that breaks its bound (missing required field, non-finite or out-of-range cost) | `status = success`, a reason code held in State | the sentence for that reason, as the whole body |
| terminates | content this agent refuses, and a failure of its own release checks: spliced directives or chat-template control tokens in the input, and a release-gate refusal (credential, no source reference, over the size ceiling) | `status = error`, no document | no document |

A correctable value completes rather than terminating because terminating ends
the calling surface's turn and surfaces only a status, leaving the reason
reachable solely from the audit trail; completing with a sentence lets the caller
fix the value and resubmit on the same conversation. Spliced instructions are not
a value to correct — completing the run would make a refusal read like an ordinary
declined value. The release-gate refusals are not caller values at all: they are
this agent's own checks on a composed document, and there is nothing for the
caller to correct.

| Reason code | The sentence names |
|---|---|
| `EMPTY_INPUT` | that nothing was received to work on |
| `QUESTION_TOO_LONG` | that the request is too long and should be shortened or split |
| `INVALID_REQUEST` | that a value could not be accepted and should be checked against the documented format |

The code itself never leaves the process. It is a State marker: `PostProcessNode`
reads it to pick the sentence, and the caller envelope carries the sentence as the
body and no reason code — so neither a field path nor an internal log reaches the
caller. A code with no entry in that table falls back to the generic sentence
rather than leaking the code.

Once a reason is settled, the main slot does not run the inner graph, and each
inner node passes the marker through untouched instead of reporting its own
precondition failure — so a second, vaguer reason can never replace the specific,
actionable one.

---

## Security Controls

| Layer | Node | Implementation |
|-------|------|----------------|
| **Caller authentication** | `src/api/server.py` | When `INVOKE_AUTH_TOKEN` is set, a caller that no upstream middleware vouched for must present it as a bearer token and then runs as `VERIFIED_EXTERNAL`. Nothing else sets the trust level in a standalone deployment, and `PreProcessNode` admits verified external callers only — without this boundary every request is denied |
| **Trust** | `PreProcessNode` | `required_trust_level = VERIFIED_EXTERNAL`; inner domain nodes are `ANONYMOUS` so an admitted caller is never denied deeper in |
| **Input screen** | `PreProcessNode` | Size cap plus an injection screen covering directive phrases AND chat-template control tokens (`<\|…\|>`, `[INST]`, `<<SYS>>`) as a class. Enforced in the node that owns the caller contract, not delegated to the framework gate, and proven by calling `execute()` directly |
| **Bounds** | `BookingIngestionNode` | Record cap, per-field character ceiling, and a finite+range check on every caller-controlled number. NaN and the infinities parse through `float()` and compare False against any range, so they are rejected explicitly rather than left to a comparison that silently admits them |
| **Credential screen** | `src/api/server.py` | The raw payload is screened with the framework's own `detect_credentials_in_value` before `invoke()`. It runs at the adapter because the framework's input gate masks personal-data patterns first, and a name-shaped match spanning a token prefix rewrites `Hotel Bearer <token>` to `[MASKED] <token>` — removing what made the value recognisable and forwarding the rest into the summary |
| **Personal data (input)** | `BookingIngestionNode` | Strips `passenger_name`, `passport_number`, `date_of_birth`, `contact_info`, `payment_info` before state |
| **Release gate** | `OutputGateNode` | Residual personal-data scrub, credential screen (the framework detector again, so the two block sets cannot drift), faithful-summary check, size ceiling. On refusal: `ERROR`, every answer-bearing field cleared, and a **truthy** notice carrying a fixed reason code. Truthy matters: the graph resolves the released document as `formatted_output or result` with no status check, so an empty notice falls through to the text the gate refused |
| **Audit** | All nodes | `emit_trace_event(event, payload, state)` — positional args, non-sensitive payloads only |
| **Credentials** | All nodes | None written to state; the manifest declares `secrets: []` because no code path calls `ctx.secrets.require()` |

### Non-suppressible Hardcoded Disclaimer

```
【免責事項】本サマリーはAI生成による参考要約です。正式な予約内容・料金・取消条件は予約原本（確定書面）でご確認ください。
```

Appended by `OutputGateNode` once the release checks pass. Cannot be disabled by config or by the caller.

### Numeric precision in the released summary

The summary renders exactly one monetary aggregate — the trip total — and it is a faithful sum
of the caller's own declared per-segment costs, returned to the caller who supplied them. No
third party reads a figure they did not provide, and no raw line-item amounts are rendered. A
rounding grid is therefore not applied: rounding the total would make the customer-facing cost
wrong without withholding anything the reader does not already hold. What is enforced instead is
the invariant this template actually states — every rendered line traces to a source booking
record — plus a finite+range check on each cost before it enters the sum, so an unbounded or
non-finite value cannot reach the total at all.

### Runtime configuration

`config/agent.yaml` is the static manifest: identity and compile-time requirements, read at root
level with no `agent:` block. Runtime parameters live in `config/config.yaml` and reach the graph
as `Graph(config=...)` — the registry supplies it, and `src/api/server.py` does the same through
`runtime_config()`, so the declared values are live in both deployments.

| Key | Consumer |
|---|---|
| `max_retry` | the framework backbone's retry routing |
| `timeout_s` | framework service clients |
| `itinerary.max_bookings` | `BookingIngestionNode` — record cap |
| `itinerary.max_field_chars` | `BookingIngestionNode` — per-field character ceiling |
| `itinerary.min_cost_jpy` / `max_cost_jpy` | `BookingIngestionNode` — cost range |
| `itinerary.max_report_chars` | `OutputGateNode` — released-summary ceiling |

The itinerary bounds are forwarded to the inner graph by
`ItineraryWorkflowGraphNode._parent_config()` and seeded into **inner** state by
`DomainWorkflowGraph._extra_initial_state()`. The layer matters: the ingestion cap and the size
ceiling run inside the inner graph, and a check reading an outer-graph key from inner state would
compare against an empty mapping on every real invocation — dead, healthy-looking, and green in
its own tests.

---

## GraphNode Contract (`ItineraryWorkflowGraphNode`)

| Method | Implementation |
|--------|----------------|
| `get_subgraph()` | `DomainWorkflowGraph(config=self._parent_config())` — the forwarded runtime bounds |
| `extract_input(state)` | `state.get("validated_input") or state.get("user_input", "")` |
| `merge_output(state, sub_result)` | `{"formatted_output": sub_result.get("output"), "status": sub_result.get("status")}` |
| `error_strategy` | `"propagate"` — re-raise inner errors as `SubgraphError` (fail-fast) |
| `propagate_hitl` | `False` — HITL handled inside inner graph |

---

## Class-Name Alignment

| Artifact | Value |
|----------|-------|
| `src/graph/graph.py` class | `TravelItinerarySummarizationAgent(AgentBaseGraph)` |
| `config/agent.yaml` `class:` | `TravelItinerarySummarizationAgent` |
| `src/api/server.py` import | `from src.graph.graph import Graph` (alias: `Graph = TravelItinerarySummarizationAgent`) |

---

## Dependencies

| # | Dependency | Status |
|---|-----------|--------|
| 1 | Booking-record schema / source connectors — deterministic rule-based impl (no real LLM/API) | In-scope: synthetic rule-based processing used in this template |
| 2 | `shared.utils.audit_logger.emit_trace_event` — provided by the installed framework wheel | Provided by `agenticstar-agentcore==1.0.2` |
| 3 | `framework.*` imports only — never the platform SDK | Enforced; verified by the import-isolation check |
