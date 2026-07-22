# Agent Rules

## Model and provider

- All subagents for this project MUST run through the OpenAI Codex runtime.
- Subagents MUST NOT use OpenRouter, Claude, Anthropic, Gemini, or any other external model/provider routing.
- Do not invoke external AI CLIs or provider APIs for implementation, review, testing, or research.

## Delegation

- Keep delegated work scoped to non-overlapping files and verify agent-reported changes independently before claiming completion.

## Product UI constraints

- NEVER add user-facing billing acknowledgements, billing-review checkboxes, cost-review gates, or billing-warning confirmation dialogs to Modelfiche UI or CLI flows.
- Provider billing and failure risk are system concerns; generation and queue actions MUST NOT require a user checkbox or acknowledgment phrase before submission.
