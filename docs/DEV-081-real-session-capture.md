# DEV-081 — Real Session Capture

> **Project has state. Agents come and go.**

DEV-081은 실제 개발 세션의 작업 과정과 결과를 다음 세션이 검증 가능한 raw
evidence로 읽도록 보존한다. capture 대상은 Agent별 memory가 아니라 하나의 동일한
Shared Mind workspace에 등록되는 immutable `SourceRevision`이다.

## Contract

`TASK_TRACE` (`task-trace@1`)는 다음을 가진다.

- stable `trace_id`, `task_id`, Agent 비종속 `session_id`
- 원본 `started_at`, `ended_at`
- 관련 canonical object ID
- 1부터 연속하는 ordered events

각 `TASK_TRACE_EVENT` (`task-trace-event@1`)은 `TASK`, `TOOL`, `RESULT`,
`DECISION`, `FAILURE`, `TEST` 중 하나이며 stable event ID, 원 timestamp, 요약과
structured details를 보존한다. Event는 raw evidence이며 그 자체가 canonical
Decision/WorkItem을 직접 변경하지 않는다.

## Mutation boundary

```text
strict validation
    → no-clobber atomic local trace file
    → IngestBatch
    → REGISTER_SOURCE_REVISION Proposal
    → append-only kernel ledger/receipt
    → optional Draft extraction
    → disposable view/index consolidation
```

Task trace capture는 kernel DB나 product DB를 직접 수정하는 별도 Agent memory 경로를
만들지 않는다. factual/project state 변경은 계속 별도의 validated kernel Proposal을
요구하고 Skill 변경은 ProductMutationProposal을 요구한다.

## Idempotency and failure semantics

- 같은 `trace_id`와 같은 canonical bytes는 `UNCHANGED`로 반환하며 ledger와 audit를
  늘리지 않는다.
- 같은 `trace_id`와 다른 bytes는 `TASK_TRACE_IMMUTABLE_CONFLICT`로 거부한다.
- malformed JSON, schema 위반, task mismatch, duplicate event ID, sequence gap은 file
  또는 canonical mutation 전에 거부한다.
- file write는 temporary file을 완성·fsync한 뒤 no-clobber link로 게시한다.
- source registration 뒤 extraction/consolidation이 실패해도 retry는 같은 file,
  IngestBatch, SourceRevision을 재사용한다.
- successful capture receipt는 product audit hash chain에 한 번 기록된다.

## Acceptance tests

1. 여섯 event type을 가진 trace가 contract validation과 capture를 통과한다.
2. fresh `ProductService`가 source span에서 동일 trace를 복원하고 검색할 수 있다.
3. exact duplicate는 ledger/source/receipt/product audit count를 변경하지 않는다.
4. identity conflict, malformed JSON/schema, duplicate ID와 sequence gap은 mutation 0이다.
5. atomic write failure는 partial file과 canonical mutation을 남기지 않는다.
6. post-registration failure retry는 ledger entry를 중복 생성하지 않는다.
7. source `captured_at`, trace timestamp, event order는 입력과 동일하다.
8. contract validator, targeted tests, full branch coverage, 실제 dogfooding capture와
   `PRODUCT_INTEGRITY_VALID`가 모두 통과해야 DEV-081을 DONE으로 바꿀 수 있다.

## CLI

```bash
shared-mind-product capture DEV-081 ./dev-081-task-trace.json
```

기존 plain directive text capture는 DEV-073 호환 경로로 유지한다. JSON처럼 시작하는
입력은 DEV-081 strict contract로 처리되므로 malformed structured trace가 legacy text로
우회되지 않는다.
