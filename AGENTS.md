# Agent Rules

## Model and provider

- All subagents for this project MUST run through the OpenAI Codex runtime using `gpt-5.6-luna` with reasoning effort `high`.
- Subagents MUST NOT use OpenRouter, Claude, Anthropic, Gemini, or any other external model/provider routing.
- Do not invoke external AI CLIs or provider APIs for implementation, review, testing, or research.
- If the requested Codex Luna model is unavailable, stop and report the blocker; do not silently fall back to another model.

## Delegation

- Every subagent dispatch MUST specify the model explicitly as `gpt-5.6-luna` and reasoning effort `high` when the dispatch surface supports those fields.
- Keep delegated work scoped to non-overlapping files and verify agent-reported changes independently before claiming completion.
