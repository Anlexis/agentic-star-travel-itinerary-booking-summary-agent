# Test Specification — TRV-C2-001 TravelItinerarySummarizationAgent

## Test Strategy
- Coverage target: key domain paths (happy path + error / boundary cases per node)
- Test types: Unit (node-level) / Proof-of-Boundary (backbone + framework)

---

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result |
|-------|------|----------------|
| TC-01 | State contract: flat TypedDict, all dict/list as `Optional[str]` | Type check pass; no Pydantic/dataclass |
| TC-02 | PreProcessNode stops on empty input | Run completes carrying the reason: SUCCESS + `error_code`, `error_log` populated, no `validated_input` |
| TC-03 | PreProcessNode stops on oversized input (>1 MiB) | Run completes carrying the reason: SUCCESS + `error_code`, no `validated_input` |
| TC-04 | PreProcessNode refuses directive phrases AND chat-template control tokens | Terminates: AgentStatus.ERROR returned, nothing carried forward |
| TC-05 | `emit_trace_event` present in every node's `execute()` | audit-trace check: 0 violations |
| TC-06 | `emit_trace_event` uses positional args (event, payload, state) | audit-trace check: 0 violations |
| TC-07 | No `@final` gate-hook override (class-method `_security_gate_input/output`) | gate-hook check: 0 violations |
| TC-08 | `required_trust_level` declared on every domain node | ANONYMOUS on inner; VERIFIED_EXTERNAL on pre_process |
| TC-09 | Personal-data fields absent from `parsed_bookings` after ingestion | PASS: no passenger_name / passport_number / contact_info / payment_info |
| TC-10 | Residual personal-data patterns masked with `[MASKED]` in the released summary | PASS |
| TC-11 | Faithful-summary check: `[BK-*]` refs present → summary released | `s3_gate_passed=True`, SUCCESS |
| TC-12 | Faithful-summary check: no `[BK-*]` refs → summary **withheld** | ERROR; every answer-bearing field cleared; truthy notice |
| TC-13 | Non-suppressible disclaimer always appended to a released summary | `免責事項` always present |
| TC-14 | No `.run()` calls, no platform-SDK imports | no-run + import-isolation checks: 0 violations |

---

## Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result |
|-------|----------|------|----------------|
| PB-2 | State serialization | `State` fields are flat primitives / `Optional[str]` JSON | No Pydantic/dataclass; msgpack-safe |
| PB-4 | Import isolation | No platform-SDK imports in src/ | AST scan: 0 violations |
| PB-5 | Checkpoint safety | No credential-like field names in `State` | credential scan: 0 violations |
| PB-6 | Backbone invoke order | Full `Graph().invoke(user_input=PAYLOAD, ctx=InvocationContext(VERIFIED_EXTERNAL))` → `node_history == [InitializeNode, PreProcessNode, ItineraryWorkflowGraphNode, PostProcessNode, FinalizeNode]` | Backbone order verified; AgentStatus.SUCCESS |
| PB-7 | HITL interrupt propagation | Skipped — `config/config.yaml` does not set `hitl.enabled` | N/A for this template |

> PB-6 uses `TrustLevel.VERIFIED_EXTERNAL` (not `for_internal()`). This exercises the real
> trust path a production caller uses and catches a trust mismatch in the inner nodes:
> inner nodes declare `ANONYMOUS`, so VERIFIED_EXTERNAL(1) >= ANONYMOUS(0) → ADMIT.
>
> PB-6 constructs the invocation context directly, which is exactly what it cannot see: a
> deployment where the HTTP adapter hands the graph an anonymous caller passes PB-6 and fails
> every real request. That is what the end-to-end suite below is for.

---

## End-to-End Tests (`tests/integration/test_invoke_e2e.py`)

Driven through the real ASGI `/invoke` entry point with bearer authentication, not through a
hand-constructed invocation context.

| TC-ID | Test | Expected Result |
|-------|------|----------------|
| E2E-01 | Authenticated caller, valid booking records | `status=success`; `[BK-*]` refs, computed total and disclaimer all present; backbone order verified |
| E2E-02 | A different payload produces a different total | Output tracks the caller data, not a fixed baseline |
| E2E-03 | No bearer token | HTTP 401; no document in the response |
| E2E-04 | Declared bounds load and match the shipped file | `config/config.yaml` resolves through `runtime_config()` |
| E2E-05 | The main slot forwards the declared bounds to the subgraph | A main slot handing over an empty mapping fails here |
| E2E-06 | A forwarded bound overrides the shipped file | The constructor channel drives the seeded value |
| E2E-07 | Bounds are seeded into **inner** state | The layer the ingestion cap and the size ceiling actually read |
| E2E-08 | `max_bookings` records accepted; one more stopped | The declared cap is enforced end to end; over the cap the run completes with the too-long sentence as the body and no document |
| E2E-09 | Changing the declared cap changes the behaviour | The cap is read, not hard-coded |
| E2E-10 | Credential-shaped value in the payload | HTTP 400 naming the field; the value is never echoed |
| E2E-11 | Credential that survives the framework's name mask | HTTP 400 — screened as submitted, before any masking |
| E2E-12 | Payload with no bookings (non-JSON, `[]`, `{"bookings": []}`) | `status=success` carrying the invalid-value sentence as the body; no document — a summary of no bookings is not a summary |
| E2E-13 | Composed summary over the declared size ceiling | `status=error`; nothing composed reaches the caller — the release gate's own check, not a caller value |
| E2E-14 | The inner graph publishes the refusal notice, not the summary | Truthy notice carrying the reason code; `OutputGateNode` in `node_history` |
| E2E-15 | Clean-path control on the same inner graph | A real summary is still released — a refuse-everything gate cannot pass |
| E2E-16 | Error envelope contents | No traceback, no source path, no `site-packages` |
| E2E-17 | `cost_jpy` = NaN / ±Infinity / 1e400 / 10^30 / `"abc"` / `true` | Stopped per field: `status=success` carrying one of the fixed reason sentences as the body; no document |
| E2E-18 | A valid cost still works | `¥1,234` rendered |
| E2E-19 | Injection attempts (control tokens and directive phrases) | Refused: `status=error`, no document |
| E2E-20 | Ordinary travel text ("Alcatraz Jailbreak Tour", "You are now checked in") | Accepted — the screen must not block real bookings |
| E2E-21 | A title-cased hotel name | Removed by the framework's own name mask before any template code runs; pinned so a change is visible |

---

## Release-Gate Tests (`tests/unit/test_release_gate.py`)

| TC-ID | Test | Expected Result |
|-------|------|----------------|
| RG-01 | Clean summary | Released, with source refs, total and disclaimer |
| RG-02 | Clean summary | No field is cleared — the clearing is specific to a refusal |
| RG-03 | Each credential shape the framework detector knows | Withheld; the value never appears in the notice |
| RG-04 | Each refusal reason | Every field in `CLEARED_ON_REFUSAL` is **present in the delta** and empty |
| RG-05 | The notice | Truthy; carries the reason code only, never the matched value or the withheld text |
| RG-06 | The whole refusal delta | No released text, no traceback, no source path anywhere in it |
| RG-07 | Cleared-field inventory | Matches the answer-bearing field set exactly |
| RG-08 | Detector parity, as a property | `refused_for_credential == bool(detect_credentials_in_value(text))` |
| RG-09 | Injection screen, via direct `execute()` | Attack forms refused by this node, with no framework wrapper in front |
| RG-10 | Injection screen, via direct `execute()` | Ordinary travel text passes |

> RG-04 asserts **presence and emptiness**, not falsiness. LangGraph merges partial deltas, so a
> gate that clears nothing returns a delta without the key and leaves the previous value standing
> in state — against which `assert not result.get(field)` passes on the exact defect it looks
> like it is testing.

---

## Business Logic Tests

### BookingIngestionNode (TC-BKG)

| TC-ID | Input | Expected Result |
|-------|-------|----------------|
| TC-BKG-001 | Valid JSON array with flight + hotel | SUCCESS; 2 records in `parsed_bookings` |
| TC-BKG-002 | Booking carrying personal-data fields | Stripped; `booking_id`, `segment_type` retained |
| TC-BKG-003 | Payload that is not a booking document | Completes carrying the reason: SUCCESS + `error_code`; no `parsed_bookings`; the payload is not echoed back |
| TC-BKG-003b | Well-formed payload carrying no records | Completes carrying the reason: SUCCESS + `error_code`; no `parsed_bookings` |
| TC-BKG-004 | `{"bookings": [...]}` wrapper shape | SUCCESS; records extracted from wrapper |

### SegmentExtractionNode (TC-SEG)

| TC-ID | Input | Expected Result |
|-------|-------|----------------|
| TC-SEG-001 | Two bookings on different dates | Segments ordered ascending by date |
| TC-SEG-002 | Flight booking | Summary contains `[BK-<booking_id>]` marker |
| TC-SEG-003 | Three bookings | `segment_count` == 3; len(segments) == 3 |
| TC-SEG-004 | Empty `parsed_bookings` | SUCCESS; `segment_count` == 0 |

### ItinerarySynthesisNode (TC-SYN)

| TC-ID | Input | Expected Result |
|-------|-------|----------------|
| TC-SYN-001 | One segment | `itinerary_body` contains `[BK-*]` reference |
| TC-SYN-002 | Two segments with costs | `total_cost_jpy` == sum of both costs |
| TC-SYN-003 | Segment with cancellation policy | `cancellation_terms` references booking_id |
| TC-SYN-004 | Two segments same date | Only one `## 2026-XX-XX` header in itinerary_body |
| TC-SYN-005 | Zero segments | Fallback message; SUCCESS |

### FormatterNode (TC-FMT)

| TC-ID | Input | Expected Result |
|-------|-------|----------------|
| TC-FMT-001 | Valid synthesis output | `formatted_output` contains `旅行日程サマリー` |
| TC-FMT-002 | Valid synthesis output | `formatted_output` contains `電子帳簿保存法` section |
| TC-FMT-003 | Total cost ¥85,000 | `formatted_output` contains `85,000` |
| TC-FMT-004 | Any valid input | Disclaimer (`免責事項`) NOT in FormatterNode output (appended by OutputGateNode) |

### OutputGateNode (TC-OGT)

| TC-ID | Input | Expected Result |
|-------|-------|----------------|
| TC-OGT-001 | Output with `[BK-G01]` | Released; `s3_gate_passed=True` |
| TC-OGT-002 | Output without any `[BK-*]` | **Withheld**: ERROR, `gate_violation=no_source_reference`, every answer-bearing field cleared, truthy notice |
| TC-OGT-003 | Valid output | `免責事項` + `AI生成` + `予約原本` present in `formatted_output` |
| TC-OGT-004 | Output containing an email address | Email replaced with `[MASKED]`; summary still released |
| TC-OGT-005 | Empty `formatted_output` | **Withheld**: ERROR, `gate_violation=no_source_reference` |
| TC-OGT-006 | Trust declaration | `required_trust_level == ANONYMOUS` |

### Audit Payload Masking (TC-AUD)

| TC-ID | Test | Expected Result |
|-------|------|----------------|
| TC-AUD-001 | BookingIngestionNode emits an audit event on input carrying personal data | Audit payload (`call.args[1]`) carries no raw personal data |

---

## Test Execution

- Runner: pytest against the installed `agenticstar-agentcore` wheel — a stub run is not a run
- Totals: 113 passed, 2 skipped (PB-7, not applicable — `hitl.enabled` is not set)

> The two stop paths are asserted apart throughout: a value the caller can correct
> is checked for SUCCESS **with** a reason code (asserting the status alone would
> pass on a run that quietly answered), while a refusal keeps asserting ERROR with
> nothing carried forward.
