# Travel Itinerary & Booking Summary Agent

AI agent for summarizing travel itineraries and bookings, built with Agentic Star.

> **Category**: Cat 2 (domain-specific document generation pipeline)
> **Industry**: Travel & Hospitality
> **Template ID**: TRV-C2-001

## Overview

Takes a batch of booking records for one trip — flights, hotels, transfers and
activities, arriving in mixed shapes and spread across several dates — and returns one
day-by-day itinerary summary: the key details of each segment in date order, the total
cost of the trip, and the cancellation terms reconciled across the individual fare rules.
Every line in the summary carries the identifier of the booking record it came from, so a
reader can check any statement against the source booking rather than taking it on trust.

The pipeline is deterministic: it composes the summary from the records themselves rather
than generating prose about them. A summary that references no source record is withheld
instead of released, on the view that a plausible itinerary for a trip nobody booked is
worse than no answer.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >= 3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
