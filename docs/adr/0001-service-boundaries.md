# ADR 0001: Service boundaries

## Context

ApprovalFlow has several responsibilities: receiving submissions, managing the approval workflow, deciding the approval route, and handling payment.                                                    Keeping everything in one service would be simpler at first, but it would mix parts of the system that have different responsibilities and failure modes.

## Decision

Split the backend into four FastAPI services:

- `api-gateway` for the external API.
- `workflow-service` for invoice state and workflow progress.
- `decision-service` for policy checks, agent recommendation, and guardrails.
- `payment-service` for payment execution and compensation.

## Consequences

This creates clear ownership for each part of the system and makes the approval and payment logic easier to reason about separately.

The tradeoff is that local development has more moving parts, so Docker Compose and correlation IDs are needed from the start.