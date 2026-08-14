# Shared Mind self-dogfooding bootstrap seed

이 파일은 DEV-080 첫 self cold-start를 위한 deterministic bootstrap source다.
Shared Mind의 local extractor가 아래 directive를 읽어 핵심 Decision, Question, WorkItem, Skill 후보를 staging한다.

DECISION: One Shared State | 모든 AI client와 session은 하나의 동일한 canonical project state를 관찰하고 task 차이는 context view만 바꾼다. | Agent별 memory partition은 Shared Mind가 해결하려는 knowledge fragmentation을 다시 만든다. | AgentProfile;Agent Loadout;Fixed Asset Binding

DECISION: Task-aware Context instead of Agent Loadout | Context selection은 Agent identity가 아니라 task, query, explicit reference, depth, budget을 입력으로 사용한다. | 동일 state와 동일 ContextRequest는 호출 client와 무관하게 동일한 context 결과를 만들어야 한다. | Agent-specific loadout

DECISION: Core Context is a projection | Core Context는 canonical Shared State에서 재생성되는 derived projection이며 독립적인 authoritative memory가 아니다. | stale summary가 두 번째 truth가 되는 것을 막기 위해서다. | authoritative L3 Core memory

DECISION: Factual and project state uses the kernel Proposal boundary | Source, Claim, Evidence, Conflict, Decision, Question, WorkItem 변경은 validated kernel Proposal과 append-only ledger를 통해서만 적용한다. | evidence authority, conflict preservation, stale-write rejection, deterministic replay를 보존해야 한다. | direct database mutation

DECISION: Skills are shared procedural state | Skill은 versioned ProductMutationProposal, receipt, audit hash chain, version guard, review, replay로 관리하며 Agent별로 복사하지 않는다. | procedural memory는 lifecycle이 필요하지만 Agent identity에 따라 fork되면 안 된다. | Agent-owned Skill copies

DECISION: Derived views are disposable | Scenario, Wiki, Core Context, retrieval index, CodeGraph, context pack은 source와 Shared State에서 재생성 가능해야 한다. | 검색과 요약 편의 기능이 truth authority가 되어서는 안 된다. | authoritative derived index

DECISION: Local-first and provider-neutral | Shared Mind는 mandatory LLM, embedding provider, vector database, TencentDB 또는 특정 vendor runtime 없이 동작해야 한다. | deterministic local path를 correctness baseline으로 유지하기 위해서다. | mandatory vendor dependency

QUESTION: 완전히 새로운 AI 세션이 사용자의 재설명 없이 Shared Mind 개발을 정확하게 이어갈 수 있는가? | DEV-080의 핵심 dogfooding 질문이다.

QUESTION: 실제 코딩 세션에서 Wrong Memory Rate는 얼마인가? | 관련 없는 검색 결과보다 확신을 가진 잘못된 기억이 다음 의사결정을 오염시킬 위험이 더 크다.

QUESTION: 어떤 기억을 historical evidence는 보존하면서 stale 또는 superseded 상태로 자동 전환해야 하는가? | DEV-084 Memory Lifecycle의 핵심 질문이다.

QUESTION: 정확한 next-action 선택을 유지하면서 context를 얼마나 줄일 수 있는가? | DEV-086 Context Quality Benchmark와 향후 compaction 연구의 핵심 질문이다.

WORK: P0 | DEV-080: Shared Mind 저장소 자체를 cold-start하고 첫 실제 self-dogfooding workspace로 사용한다.

WORK: P0 | DEV-081: 실제 Codex, Claude, GPT 개발 세션을 immutable task trace로 capture한다.

WORK: P0 | DEV-082: 설명 없는 fresh session이 Shared Mind context만으로 프로젝트를 이어가는 Zero-Relearning 평가를 자동화한다.

WORK: P0 | DEV-083: duplicate, irrelevant memory, stale memory, confidently wrong memory를 측정하는 Memory Pollution 평가를 구현한다.

WORK: P0 | DEV-084: stale, superseded, completed, still-current 상태를 구분하는 Memory Lifecycle 규칙을 구현한다.

WORK: P0 | DEV-085: 원래 conflicting Claims와 해결 rationale을 보존하는 explicit Conflict Resolution Workflow를 구현한다.

WORK: P0 | DEV-086: relevant recall, missing critical memory, irrelevant context, evidence traceability, context cost를 측정하는 Context Quality Benchmark를 구현한다.

SKILL: Continue Shared Mind development from a fresh session | continue implementation,new session,shared mind development | read AGENTS.md and DEV-080 runbook; verify the local Shared Mind workspace; request Core plus task-aware Context; inspect active WorkItems and open Questions; retrieve relevant Decisions and Conflicts; drill down to evidence when needed; implement the highest-priority unblocked task; run targeted tests and regression tests; capture the resulting task trace before ending the session | NON_EMPTY
