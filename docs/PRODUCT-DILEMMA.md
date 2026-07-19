# Product Dilemma — Automation vs. Human Control

## The decision

ApprovalFlow automatically approves only small, low-risk invoices.

An invoice can be considered for automatic approval only when:

- its normalized value is no more than USD 250
- the recommendation confidence is at least 0.80
- the vendor is present in the configured known-vendor registry
- the receipt and category-specific rules are satisfied
- no duplicate, FX, or other hard-stop rule applies

Gemini can recommend an outcome, but the deterministic router always makes the final decision.

## Why the autonomy ceiling is USD 250

The goal is to reduce manual work for routine expenses without allowing the automated path to create a large financial risk.

USD 250 is high enough to cover common expenses such as meals, taxis, office supplies, and small software purchases. At the same time, a wrong automatic approval is still limited to a relatively small amount.

Invoices above this ceiling are not automatically rejected. They are sent to human review, where an approver can consider business context that may not be available to the system.

## Why the confidence threshold is 0.80

A very low threshold would allow uncertain recommendations to influence automatic approval. A threshold that is too high would make the automated path almost unused.

The value 0.80 is a practical middle ground. It allows clear, routine cases to continue automatically while sending uncertain cases to a person.

Confidence alone is never enough. A recommendation with high confidence still cannot bypass the autonomy ceiling, an unknown vendor, a missing receipt, or a category-specific rule.

## Supporting guardrails

The autonomous path is also limited by additional policy rules:

- receipts are required for expenses above USD 25
- meals are limited to USD 75 per attendee
- SaaS expenses have a USD 200 monthly cap
- entertainment expenses above USD 500 require justification
- hardware expenses above USD 1,000 are treated as capital purchases
- travel expenses above USD 1,500 require manager approval
- foreign-currency expenses above USD 1,000 use a hard stop
- vendor trust is determined by the configured registry, not by the submitter

These rules reduce the chance that a low total amount hides a different business risk.

## Provider failures

Gemini is the normal runtime recommendation provider.

If the Gemini secret is missing, authentication fails, the request times out, the provider is rate limited, or the response is invalid, the system does not silently replace Gemini with the deterministic stub.

The recommendation receives confidence 0, the decision records `AI-PROVIDER-ERROR`, and the invoice is routed to human review.

The stub provider is used only when it is explicitly selected for automated tests, CI, or end-to-end verification.

## Trade-offs

This design intentionally favors safety over maximum automation.

It can send some legitimate invoices to human review, especially when a vendor is new or the model is uncertain. That creates additional reviewer work, but it is safer than automatically approving a risky or unclear expense.

The policy values are stored in `data/policy-config.json` and can be changed while the service is running. This allows the business to adjust the balance between automation and control without changing application code or rebuilding the service image.

## How the policy should evolve

The current thresholds are suitable for the project and demonstration data. In a production system, they should be reviewed using real approval outcomes, payment failures, duplicate rates, reviewer workload, and department-specific risk.

Any future increase in the autonomy ceiling should be based on evidence and should keep the deterministic guardrails in place.
