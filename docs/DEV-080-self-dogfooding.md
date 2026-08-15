# DEV-080 — Shared Mind Self-Dogfooding Cold Start

> **Project has state. Agents come and go.**

이 문서는 Shared Mind 저장소 자체를 Shared Mind의 첫 실제 dogfooding 대상으로 사용하는 실행 가이드다.
새 Codex 세션에서는 이 문서를 먼저 읽고, 사용자의 장황한 재설명 없이 여기 적힌 절차를 따라 DEV-080을 시작한다.

## 1. Cold-start가 무엇인가

Cold-start는 새 프로젝트를 만드는 작업이 아니다.
이미 존재하는 프로젝트의 문서, 코드, 대화 기록을 **빈 Shared Mind workspace에 처음 등록**해서 다음 AI 세션이 프로젝트를 다시 설명받지 않고 시작할 수 있도록 만드는 초기화 단계다.

```text
기존 프로젝트
    ↓
immutable SourceRevision 등록
    ↓
Decision / Question / WorkItem / Skill 후보 추출
    ↓
검토 가능한 DraftProposal
    ↓
Shared State 갱신
    ↓
Scenario / Core / Retrieval / Code views 생성
    ↓
Task-aware Context
    ↓
새 AI 세션이 설명 없이 작업 재개
```

Cold-start는 Agent별 memory를 만들지 않는다. 모든 Agent와 세션은 하나의 동일한 Shared State를 관찰한다.

```text
Agent A memory != Agent B memory      # 금지
Shared Mind(A) == Shared Mind(B)      # 필수
Context(task A) != Context(task B)    # 허용
```

## 2. DEV-080의 목적

DEV-080의 질문은 하나다.

> **완전히 새로운 AI 세션이 사용자의 재설명 없이 Shared Mind의 현재 상태를 이해하고 다음 개발 작업을 정확하게 이어갈 수 있는가?**

Shared Mind 자체를 첫 대상 프로젝트로 사용한다.

새 세션이 최소한 다음을 Shared Mind에서 복원할 수 있어야 한다.

- 프로젝트가 Personal LLM Wiki 아이디어에서 출발했다는 것
- 핵심 철학이 `Project has state. Agents come and go.`라는 것
- AgentProfile / Agent Loadout / Fixed Asset Binding을 의도적으로 버린 이유
- One Shared State와 Task-aware Context의 차이
- Core Context가 canonical truth가 아니라 derived projection이라는 것
- factual/project mutation과 Skill mutation의 권위 경계
- evidence, conflict, ledger, replay가 보존되어야 한다는 것
- 현재 DEV-080 이후의 P0 작업이 무엇인지

## 3. 현재 deterministic extractor의 한계

현재 provider-free deterministic extractor는 일반 Markdown 문장을 LLM처럼 자유롭게 이해해서 자동으로 구조화하지 않는다.
다음 directive를 명시적으로 인식한다.

```text
FACT:
DECISION:
QUESTION:
WORK:
SKILL:
```

README, SRS, ROADMAP, Python 코드는 모두 immutable SourceRevision으로 보존되지만, 일반 문장 전체가 자동으로 Decision/Question/WorkItem으로 변환되지는 않는다.

따라서 첫 self-dogfooding cold-start에는 [`self-dogfooding-bootstrap.md`](self-dogfooding-bootstrap.md)를 함께 ingest한다.
이 파일은 Shared Mind의 핵심 결정과 DEV-080 이후 작업을 deterministic directive 형식으로 제공한다.

Model-backed extractor는 별도 명시적 remote-disclosure authorization이 있어야 하며, DEV-080의 첫 correctness baseline에는 필수가 아니다.

## 4. 권장 로컬 디렉터리 구조

Shared Mind의 기억 workspace를 Git repository **밖에** 둔다.

```text
~/projects/
├── shared-mind/          # Git repository
└── shared-mind-memory/   # local Shared Mind workspace
```

이렇게 해야 Shared Mind가 자기 SQLite DB와 projection을 다시 source로 ingest하는 재귀를 피할 수 있다.

`.shared-mind`와 build/cache 디렉터리는 ingest 시 기본 제외되지만, self-dogfooding workspace 자체를 repository 외부에 두는 것이 더 명확한 운영 경계다.

## 5. 처음 한 번 실행

### 5.1 Repository clone

```bash
git clone https://github.com/ArthurCore/shared-mind.git
cd shared-mind
```

이미 clone되어 있다면 최신 `main`으로 맞춘다.

```bash
git switch main
git pull --ff-only
```

### 5.2 개발 설치

```bash
uv tool install --editable '.[mcp]'
```

### 5.3 Shared Mind memory workspace 생성

repository root에서 실행한다.

```bash
shared-mind init ../shared-mind-memory \
  --purpose "Preserve Shared Mind project reasoning and work state so any AI session can continue without re-explanation."
```

이 명령은 `../shared-mind-memory`를 Shared Mind의 실제 local memory workspace로 만든다.
Git repository와는 별개다.

### 5.4 Self cold-start 실행

memory workspace로 이동한다.

```bash
cd ../shared-mind-memory
```

그리고 repository 전체를 첫 source set으로 넣는다.

```bash
shared-mind-product cold-start ../shared-mind \
  --task "Continue Shared Mind development starting with DEV-080 self-dogfooding" \
  --budget-bytes 65536
```

이때 repository 안의 `docs/self-dogfooding-bootstrap.md`도 함께 ingest되므로 핵심 Decision, Question, WorkItem, Skill 후보가 deterministic하게 생성된다.

## 6. Cold-start 직후 검증

다음 명령은 모두 성공해야 한다.

```bash
shared-mind-product verify
shared-mind-product catalog
shared-mind-product review-queue
```

확인할 사항:

- kernel ledger verification 성공
- product audit/replay verification 성공
- bootstrap source가 SourceRevision으로 존재
- DEV-080~086 WorkItem 후보가 존재
- One Shared State 관련 Decision이 존재
- open Questions가 존재
- derived Scenario/Core/index가 재생성 가능

## 7. 첫 Zero-Relearning Context 생성

cold-start 후 다음 context를 요청한다.

```bash
shared-mind-product context \
  --task "Continue the highest-priority unblocked Shared Mind development work" \
  --query "project purpose current decisions architecture invariants open questions active work conflicts evidence" \
  --depth EVIDENCE \
  --budget-bytes 65536
```

이 결과가 새로운 Codex 세션의 첫 실제 handoff다.

## 8. Codex에서 DEV-080을 시작하는 방법

새 Codex 세션에서는 프로젝트 역사를 다시 길게 설명하지 않는다.
다음 정도의 지시만 준다.

```text
Read AGENTS.md and docs/DEV-080-self-dogfooding.md first.
Use the local Shared Mind workspace at ../shared-mind-memory to recover the current project state.
Do not rely on a long explanatory prompt from me.
Verify the workspace, request task-aware context, identify the highest-priority unblocked WorkItem, and continue implementation.
Preserve the One Shared State architecture invariants.
Run targeted tests and the relevant regression suite after changes.
Capture the completed task trace back into Shared Mind before ending the session.
```

이 지시만으로 Codex가 프로젝트 목적, 핵심 결정, 열린 질문, 현재 작업을 Shared Mind에서 찾아야 한다.

## 9. DEV-080 성공 기준

다음 질문을 **사용자의 추가 설명 없이** 새 세션이 정확하게 답해야 한다.

1. Shared Mind의 핵심 제품 목적은 무엇인가?
2. 왜 Agent Loadout을 제거했는가?
3. One Shared State와 Task-aware Context는 무엇이 다른가?
4. Core Context가 별도 truth가 아닌 이유는 무엇인가?
5. factual/project state와 Skill state의 mutation boundary는 어떻게 다른가?
6. 현재 가장 우선순위가 높은 다음 작업은 무엇인가?
7. 답의 근거 SourceRevision 또는 Decision/WorkItem으로 drill-down할 수 있는가?

그리고 다음 지표를 기록한다.

- Continuity Accuracy
- Decision Recall
- Open Question Recall
- Conflict Recall
- Evidence Traceability
- Wrong Memory Rate
- Missing Critical Memory Rate
- Irrelevant Context Rate
- Context Bytes / Tokens
- Time To Productive Action

## 10. 작업 종료 시 반드시 capture

실제 개발 작업이 끝나면 해당 세션의 trace를 다시 Shared Mind에 넣는다.

```bash
shared-mind-product capture <task-id> <task-trace.json> --auto-commit
```

DEV-081부터 실제 session trace는
[`TASK_TRACE` strict contract](DEV-081-real-session-capture.md)를 사용한다.

그 후 다시 검증한다.

```bash
shared-mind-product verify
```

다음 새 세션은 방금 작업한 Agent의 private memory가 아니라 **갱신된 동일 Shared State**를 읽어야 한다.

## 11. 하지 말아야 할 것

DEV-080과 이후 dogfooding에서 다음을 금지한다.

- Agent별 canonical memory workspace 생성
- Codex용, Claude용, GPT용 별도 project memory 생성
- AgentProfile 또는 fixed memory loadout 도입
- Core Context를 별도 authoritative memory로 저장
- LLM이 직접 canonical DB 수정
- conflict를 한쪽 Claim 삭제로 해결
- test를 통과시키기 위해 evidence/ledger/replay invariant 약화

## 12. DEV-080 이후 바로 이어지는 작업

첫 self cold-start가 정상적으로 작동하면 다음 순서로 진행한다.

```text
DEV-080  Shared Mind self cold-start / dogfooding
DEV-081  실제 AI 개발 세션 capture
DEV-082  Zero-Relearning 평가 자동화
DEV-083  Memory Pollution / Wrong Memory 측정
DEV-084  Memory Lifecycle
DEV-085  Conflict Resolution Workflow
DEV-086  Context Quality Benchmark
```

DEV-080의 목적은 기능 하나를 더 추가하는 것이 아니다.
Shared Mind가 **자기 자신을 실제로 기억하고 다음 AI에게 넘겨줄 수 있는지 증명하는 것**이다.
