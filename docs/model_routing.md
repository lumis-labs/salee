# Salee model routing

Salee uses one OpenRouter budget through three narrow roles:

- `worker`: repetitive support, sales replies, and fulfillment drafts. Set `OPENROUTER_WORKER_MODEL` to a cheap or free model when one is available.
- `planner`: growth strategy, landing-page changes, and content proposals. Set `OPENROUTER_PLANNER_MODEL` to the stronger model.
- `qa`: final growth-copy review. Set `OPENROUTER_QA_MODEL` to the stronger model and keep `GROWTH_QA_ENABLED=true`.

The supervisor caps calls per cycle with `MAX_LLM_CALLS_PER_CYCLE`, records each role/model in the audit log, and keeps quality control deterministic where possible. A larger model is used for judgment-heavy work; small models handle volume. The agent does not spend tokens on hidden deliberation or duplicate passes when local checks are sufficient.

Future outreach adapters should use official APIs or approved account integrations, only contact opted-in or platform-permitted audiences, honor opt-outs, and record every send, failure, and rate-limit decision.
