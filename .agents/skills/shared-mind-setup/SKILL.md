---
name: shared-mind-setup
description: Initialize, connect, verify, and resume Shared Mind for the current coding project. Use whenever the user asks in natural language to "Shared Mind 초기설정해", set up Shared Mind, enable Shared Mind, connect project memory, or restore Shared Mind in a new session.
---

# Shared Mind Setup

Run the deterministic setup surface from the current project directory:

```console
shared-mind setup
```

Parse its single JSON response. Continue only when all of these hold:

- `ok` is `true`;
- `code` is `SETUP_READY`;
- `data.integrity.valid` is `true`.

Use `data.context` as the session's current project context. Inspect its active
WorkItems, decisions, questions, conflicts, and evidence references before
changing project files. If setup created a new workspace, treat the returned
cold-start report as imported evidence, not as unquestionable truth.

Keep one canonical Shared State for every Agent and session. Never create an
Agent-specific memory, edit the SQLite database directly, or make a projection,
Scenario, Core Context, Wiki, or index authoritative. Send canonical factual and
project-state changes through validated Proposals only.

If `shared-mind` is unavailable, report that the one-time Shared Mind tool
installation is missing. Do not guess an installer source or fetch and execute a
remote script. If setup returns a failure code, preserve the response and fix
that explicit boundary instead of creating a second workspace.

If automatic observation capture was started for this session, finish with
`shared-mind-product observe finalize --session <same-session-id>`. A hook
failure is non-blocking; keep the pending buffer for retry through the same
DEV-081 canonical registration boundary.
