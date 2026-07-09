# ADR 0003: Agent recommends, code decides

## Context

The product uses an agent to help evaluate invoices and expenses, but approval decisions have financial risk. The system must not allow an agent recommendation to bypass company policy or hard stops.

## Decision

The agent recommendation is treated as one input to the decision service. The final route is always chosen by deterministic code.

The code must block auto-approval when policy conditions fail, including amount ceiling, missing receipt, low confidence, risky vendor, calculation mismatch, or fraud indicators.

## Consequences

This keeps the system safer and easier to audit. It also makes it clear where automation is allowed: simple safe cases can be automated, but risky cases go to human review.

The tradeoff is that some cases the agent thinks are safe may still be escalated because the code guardrails are stricter.