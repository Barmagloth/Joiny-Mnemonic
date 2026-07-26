# Finalization dogfood

For this repository, use post-factum finalization tags only for outcomes that
were actually resolved in the current interaction.

Emit each outcome as a standalone, first-column line in the final answer:

    [TYPE] STATUS: self-contained outcome

TYPE must be one of GOAL, DECISION, FACT, CONSTRAINT, TODO, PREFERENCE,
FAILURE, LESSON. STATUS must be CONFIRMED, REJECTED, or DEFERRED.

Use CONFIRMED only after the outcome is genuinely selected or established.
Use REJECTED or DEFERRED only when that disposition was explicitly resolved.
Do not emit any finalization tag for an unanswered question, an unselected
proposal, intermediate reasoning, or an assumption. If no outcome was
resolved, emit no tag. Never put a real tag in a quote or code block.

If the agent invents a new durable outcome rather than completing an outcome
the user already selected, ask whether it should be recorded. No answer means
no tag. Installed host hooks materialize valid tags; do not duplicate them
through a memory tool.
