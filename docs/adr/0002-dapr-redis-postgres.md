# ADR 0002: Dapr with Redis and PostgreSQL

## Context

The system needs communication between services, asynchronous events, temporary
workflow state, and durable business records. These needs should not all be
handled by the same storage layer.

## Decision

Use Dapr HTTP APIs for service invocation, pub/sub, state, and local secret store configuration.

Use Redis as the local backing service for Dapr pub/sub and transient state.
Use PostgreSQL for durable business data such as invoices, decisions, payments, and audit records.

## Consequences

Dapr keeps the application code less tied to Redis directly. Redis can handle fast coordination and events, while PostgreSQL remains the source of truth for business data.

The tradeoff is that the local setup has more containers and requires clear documentation.