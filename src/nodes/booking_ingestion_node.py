"""AgentCore Platform v1.0"""

# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return the status as AgentStatus.<X>.value — the enum's string value,
#    not the enum object itself
#  - Never import from other agents
#
# TRV-C2-001 — BookingIngestionNode
# Inner domain pipeline node 1 (of 5): parse, normalize, validate and strip
# personal data from the booking records arriving from PreProcessNode. This
# node owns the caller contract — every bound the records are held to is
# enforced here, against the values declared in config/config.yaml.
#
# Input:  state["validated_input"] — raw JSON string of booking records
# Output: state["parsed_bookings"]    — JSON-serialized list of sanitized records
#         state["segment_types_found"] — JSON-serialized list of segment types
#
# Controls:
#   Input: refuses an unparsable payload, a payload with no records, a record
#          list over the declared cap, a missing required field, and a cost
#          that is not a finite number inside the declared range
#   Personal data: strips passenger_name, passport_number, dob, contact_info
#          and payment_info before any downstream processing
#   Audit: emit_trace_event once per execute() (non-sensitive counts only)
#   Credentials: none written to state

import json
import logging
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Set

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress
from src.schemas.bounds import bound_int, finite_in_range, read_bounds

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Fields required in every booking record.
_REQUIRED_BOOKING_FIELDS: FrozenSet[str] = frozenset(
    {
        "booking_id",
        "segment_type",
        "date",
    }
)

# Personal-data fields stripped from booking records.
_PII_FIELDS: FrozenSet[str] = frozenset(
    {
        "passenger_name",
        "passenger_names",
        "passport_number",
        "date_of_birth",
        "dob",
        "contact_info",
        "email",
        "phone",
        "payment_info",
        "payment_card",
        "credit_card",
        "address",
    }
)

# Recognised segment types.
_VALID_SEGMENT_TYPES: FrozenSet[str] = frozenset(
    {
        "flight",
        "hotel",
        "transfer",
        "activity",
        "car_rental",
        "cruise",
        "rail",
    }
)

# Non-PII fields that are safe to retain after sanitization.
_SAFE_FIELDS: FrozenSet[str] = frozenset(
    {
        "booking_id",
        "segment_type",
        "date",
        "date_end",
        "origin",
        "origin_iata",
        "destination",
        "destination_iata",
        "airline_code",
        "flight_number",
        "hotel_name",
        "hotel_class",
        "room_type",
        "cost_jpy",
        "currency",
        "cancellation_policy",
        "departure_time",
        "arrival_time",
        "check_in",
        "check_out",
        "transfer_type",
        "activity_name",
        "notes",
        "confirmation_number",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Caller-supplied fields that are rendered verbatim into the summary. Each is
# truncated to the declared per-field ceiling so a single record cannot inflate
# the released document.
_RENDERED_TEXT_FIELDS: FrozenSet[str] = frozenset(
    {
        "origin",
        "origin_iata",
        "destination",
        "destination_iata",
        "airline_code",
        "flight_number",
        "hotel_name",
        "hotel_class",
        "room_type",
        "transfer_type",
        "activity_name",
        "cancellation_policy",
        "departure_time",
        "arrival_time",
        "check_in",
        "check_out",
        "notes",
        "confirmation_number",
        "currency",
    }
)


def _strip_pii(record: Dict[str, Any]) -> Dict[str, Any]:
    """Remove the personal-data fields; retain only _SAFE_FIELDS keys."""
    return {k: v for k, v in record.items() if k in _SAFE_FIELDS}


def _bound_text(record: Dict[str, Any], max_field_chars: int) -> Dict[str, Any]:
    """Truncate every rendered caller string to the declared ceiling."""
    bounded: Dict[str, Any] = {}
    for key, value in record.items():
        if key in _RENDERED_TEXT_FIELDS and isinstance(value, str) and len(value) > max_field_chars:
            bounded[key] = value[:max_field_chars]
        else:
            bounded[key] = value
    return bounded


def _validate_record(
    record: Dict[str, Any],
    idx: int,
    min_cost: int,
    max_cost: int,
) -> List[str]:
    """Return a list of validation error strings for a booking record.

    Messages name the record position and the field, never the rejected value —
    a caller-supplied value echoed into an error is caller-controlled output on
    the error channel.
    """
    errors: List[str] = []
    for field in sorted(_REQUIRED_BOOKING_FIELDS):
        if not record.get(field):
            errors.append(f"record[{idx}]: missing required field {field!r}")
    seg = record.get("segment_type", "")
    if seg and seg not in _VALID_SEGMENT_TYPES:
        logger.debug(
            "BookingIngestionNode: unrecognised segment_type at record[%d]; keeping as-is",
            idx,
        )
    if "cost_jpy" in record and record.get("cost_jpy") is not None:
        if finite_in_range(record["cost_jpy"], min_cost, max_cost) is None:
            errors.append(
                f"record[{idx}]: field 'cost_jpy' must be a finite number " f"between {min_cost} and {max_cost}"
            )
    return errors


def _parse_records(raw: str) -> List[Dict[str, Any]]:
    """Parse raw JSON string to a list of booking record dicts.

    Accepts:
      - JSON array: [{"booking_id": ...}, ...]
      - JSON object with "bookings" key: {"bookings": [...]}
    """
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        records = parsed.get("bookings", parsed.get("records", []))
        if isinstance(records, list):
            return records
    return []


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


class BookingIngestionNode(FunctionNode):
    """Parse, validate, bound, and strip personal data from booking records.

    First inner domain node for TRV-C2-001 TravelItinerarySummarizationAgent.

    Input state keys:
        validated_input: str — JSON booking records payload from PreProcessNode

    Output state keys (partial dict):
        parsed_bookings:     str — JSON list of sanitized booking dicts
        segment_types_found: str — JSON list of unique segment types present
        status: AgentStatus.SUCCESS
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw: str = state.get("validated_input") or state.get("user_input", "") or ""

        bounds = read_bounds(state)
        max_bookings = bound_int(bounds, "max_bookings")
        max_field_chars = bound_int(bounds, "max_field_chars")
        min_cost = bound_int(bounds, "min_cost_jpy")
        max_cost = bound_int(bounds, "max_cost_jpy")

        # ------------------------------------------------------------------
        # Step 1 — JSON parse
        #
        # A payload that does not parse carries no bookings, and a summary
        # composed from no bookings is a document that describes a trip the
        # caller never booked. It is refused rather than answered.
        # ------------------------------------------------------------------
        try:
            raw_records: List[Dict[str, Any]] = _parse_records(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("BookingIngestionNode: input is not a booking-records document")
            emit_trace_event(
                "booking_ingestion_complete",
                {"record_count": 0, "accepted": False, "reason": "unparsable_payload"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [
                    "BookingIngestionNode: user_input is not a booking-records document "
                    "(expected a JSON array of records, or an object with a 'bookings' array)"
                ],
            }

        if not isinstance(raw_records, list) or not raw_records:
            logger.warning("BookingIngestionNode: no booking records in the payload")
            emit_trace_event(
                "booking_ingestion_complete",
                {"record_count": 0, "accepted": False, "reason": "no_records"},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["BookingIngestionNode: user_input contains no booking records"],
            }

        # ------------------------------------------------------------------
        # Step 2 — structural cap
        #
        # The record list is sized by the caller. The cap is the declared
        # max_bookings, read from the state this node runs in.
        # ------------------------------------------------------------------
        if len(raw_records) > max_bookings:
            logger.warning(
                "BookingIngestionNode: %d records exceeds the declared cap of %d",
                len(raw_records),
                max_bookings,
            )
            emit_trace_event(
                "booking_ingestion_complete",
                {"record_count": len(raw_records), "accepted": False, "reason": "too_many_records"},
                state,
            )
            # The REQUEST is oversized, not one field of it: the fix is to send
            # fewer records, which is what the too-long sentence asks for.
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "QUESTION_TOO_LONG",
                "error_log": [
                    f"BookingIngestionNode: user_input carries more than {max_bookings} "
                    "booking records; split the request"
                ],
            }

        # ------------------------------------------------------------------
        # Step 3 — validate, bound, and strip personal data from each record
        # ------------------------------------------------------------------
        sanitized_records: List[Dict[str, Any]] = []
        segment_types: Set[str] = set()
        field_errors: List[str] = []

        for idx, record in enumerate(raw_records):
            if not isinstance(record, dict):
                field_errors.append(f"record[{idx}]: expected an object")
                continue

            # Validate BEFORE stripping — required fields are checked on the
            # record as submitted.
            field_errors.extend(_validate_record(record, idx, min_cost, max_cost))

            safe_record = _bound_text(_strip_pii(record), max_field_chars)
            sanitized_records.append(safe_record)

            seg_type = record.get("segment_type", "")
            if seg_type:
                segment_types.add(seg_type)

        # A record that fails validation is not summarised around: the totals
        # and the cancellation terms are the deliverable, and composing them
        # from a payload we have already found malformed publishes a figure
        # nobody can stand behind. Fail closed, naming the fields.
        if field_errors:
            logger.warning("BookingIngestionNode: %d record validation error(s)", len(field_errors))
            emit_trace_event(
                "booking_ingestion_complete",
                {
                    "record_count": len(raw_records),
                    "accepted": False,
                    "reason": "record_validation_failed",
                    "error_count": len(field_errors),
                },
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"BookingIngestionNode: {err}" for err in field_errors[:20]],
            }

        # ------------------------------------------------------------------
        # Step 4 — audit trace (non-sensitive counts only)
        # ------------------------------------------------------------------
        emit_trace_event(
            "booking_ingestion_complete",
            {
                "record_count": len(sanitized_records),
                "accepted": True,
                "segment_types": sorted(segment_types),
            },
            state,
        )

        logger.info(
            "BookingIngestionNode: ingested %d booking records; segment types: %s",
            len(sanitized_records),
            sorted(segment_types),
        )

        return {
            "parsed_bookings": json.dumps(sanitized_records),
            "segment_types_found": json.dumps(sorted(segment_types)),
            "status": AgentStatus.SUCCESS.value,
        }
