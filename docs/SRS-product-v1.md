# Shared Mind Product Layer SRS

| 항목 | 값 |
|---|---|
| 문서 버전 | 1.0.0 |
| 구현 패키지 | 0.3.0 |
| 기준일 | 2026-08-14 |
| 상태 | 구현 및 CI 검증 기준선 |
| 상위 문서 | `docs/SRS.md`, `ROADMAP.md` |

## 1. 목적

본 문서는 기존 Shared Mind 커널 위에 추가되는 자동 수집, 검토, 파생
메모리, 공유 Skill, Task-aware Context, 검색, 운영 및 평가 계층의 요구사항과
합격 기준을 정의한다. 기존 커널의 evidence authority, Proposal-only mutation,
FACT_CONFLICT/TRANSACTION_CONFLICT, ledger/replay 원칙은 변경하지 않는다.

## 2. 제품 불변조건

1. Agent, 모델, 세션별 canonical memory partition을 만들지 않는다.
2. 모든 클라이언트는 하나의 canonical Shared State를 관찰한다.
3. task가 다르면 context view는 달라질 수 있지만 underlying state는 같다.
4. 동일 state/request/selector version/budget은 동일 context hash를 만든다.
5. 모델 추출 결과는 DraftProposal이며 직접 canonical write를 하지 않는다.
6. Scenario/Core/Wiki/retrieval/code index는 삭제·재생성 가능한 view다.
7. factual Claim은 검증 가능한 source byte span을 요구한다.
8. open conflict는 context와 검색에서 숨기지 않는다.
9. Skill은 Agent 소유물이 아니라 shared identity/version을 가진다.
10. Skill 승격에는 explicit passing evidence와 approval이 필요하다.

## 3. 기능 요구사항

### PFR-01 Trusted ingest and extraction

- 파일, 디렉터리, UTF-8 문서, Python 코드, JSONL 대화를 batch manifest로 수집한다.
- symlink escape, oversized input, 과도한 operation, timeout을 fail closed 처리한다.
- 동일 bytes 재수집은 idempotent하고 변경 bytes는 새 SourceRevision을 만든다.
- deterministic/model-backed extractor는 공통 후보 계약을 사용한다.
- model-backed 실행은 remote disclosure policy `ALLOW`를 요구한다.
- 모든 후보에 extractor/model/prompt/parameter/source hash provenance를 남긴다.

### PFR-02 Review staging

- DraftProposal은 canonical DB와 분리되어 DRAFT/REVIEWED/REJECTED/COMMITTED/
  EXPIRED/FAILED lifecycle을 가진다.
- edit는 expected version을 요구한다.
- reject/failure는 kernel ledger head를 전진시키지 않는다.
- factual Draft commit은 evidence byte/hash 검증을 통과해야 한다.

### PFR-03 Derived shared-memory views

- kernel 객체를 common atomic envelope로 읽는다.
- project/subject/decision/incident/workstream Scenario를 생성한다.
- Core Context는 canonical state에서 계산하며 새로운 fact authority가 아니다.
- member dependency digest로 무관한 Scenario 재생성을 피한다.
- 상위 view에서 member/evidence/source/proposal/receipt로 drill down한다.
- verify는 managed READY view를 canonical state에서 재구축해 비교한다.

### PFR-04 Shared Skills

- SkillRecord는 purpose, trigger, precondition, steps, resources, expected output,
  validation rule, provenance, status, version을 가진다.
- create/revise/mark-tested/approve/deprecate는 product Proposal/receipt로 기록한다.
- stale version은 transaction conflict로 거부한다.
- failed evidence는 TESTED로 승격할 수 없다.
- package export/import는 identity/version/resource hashes를 보존한다.
- Agent별 Skill 복사본을 만들지 않는다.

### PFR-05 Task-aware Context

- ContextRequest는 task/purpose/query/references/depth/budget을 받는다.
- Shared Core와 task-relevant atomic/Scenario/Skill을 stable ranking으로 선택한다.
- serialized byte/token hard cap, omission metadata, selection trace를 제공한다.
- explicit reference를 보존할 수 없으면 fail closed 한다.
- Agent/model partition hint를 거부한다.

### PFR-06 Cold start

- repo/docs/conversation을 한 흐름으로 ingest/extract/review-commit/build/context한다.
- report는 imported/unchanged/failed/draft/committed/stale/conflict 수치를 제공한다.
- first handoff는 source map, current state, recommended next actions를 포함한다.
- 변경 없는 재실행은 동일 handoff hash를 생성한다.

### PFR-07 Retrieval and code understanding

- FTS5/BM25 또는 deterministic fallback을 기본 제공한다.
- vector/RRF는 optional이며 없더라도 correctness가 유지된다.
- link graph와 검색 결과는 source/evidence/provenance를 보존한다.
- Python code index는 module/class/function/method와 CALLS/REFERENCES edge를 만든다.
- caller/callee/impact path와 source-span on-demand tool을 제공한다.
- 같은 state에서 시간 차를 두고 rebuild해도 같은 fingerprint를 생성한다.

### PFR-08 Governance and recovery

- unified catalog와 review queue를 제공한다.
- product mutation을 hash-chained audit에 기록한다.
- local web surface는 loopback에만 bind하고 service 경계를 재사용한다.
- backup/restore는 duplicate, undeclared, symlink, traversal, directory, oversized,
  hash mismatch를 거부한다.
- restore 후 kernel state root와 product state hash가 일치해야 한다.

### PFR-09 Continuous compounding and evaluation

- post-task trace를 source로 보존하고 후보를 staging한다.
- dependency가 바뀐 view/index만 갱신한다.
- telemetry는 raw task/query를 저장하지 않고 hashed/minimal metadata를 사용한다.
- evidence validity, conflict recall, staleness, duplicates, provenance, routing,
  Skill reuse, cold-start cost/accuracy를 분리 평가한다.

## 4. 인터페이스 요구사항

- `shared-mind-product`는 JSON-only CLI를 제공한다.
- `shared-mind-product-mcp`는 kernel MCP와 분리된 optional stdio surface다.
- `shared-mind-web`는 loopback-only HTTP surface다.
- `shared-mind context --task`는 동일 ContextRequest semantics를 사용한다.
- CLI/service/MCP/Web는 동일 ProductService 및 stable error codes를 재사용한다.

## 5. 비기능 요구사항

| ID | 요구사항 |
|---|---|
| PNFR-01 | local/provider-neutral deterministic mode가 항상 존재한다. |
| PNFR-02 | derived rebuild와 context selection은 같은 input/version에서 동일 hash를 만든다. |
| PNFR-03 | rejected Draft/Proposal/Skill operation은 부분 상태를 남기지 않는다. |
| PNFR-04 | product audit와 Skill replay는 corruption을 탐지한다. |
| PNFR-05 | web/MCP path와 backup ZIP path는 workspace/package 경계를 벗어나지 못한다. |
| PNFR-06 | base wheel은 MCP dependency 없이 동작한다. |
| PNFR-07 | Python 3.11~3.13과 Linux/macOS/Windows determinism subset을 CI에서 검증한다. |
| PNFR-08 | 전체 branch coverage 80% 이상을 유지한다. |

## 6. DEV-029~079 추적표

| 작업 | 주요 구현 | 주요 시험 |
|---|---|---|
| DEV-029~035 | `product_ingest.py`, `product_store.py`, product contract | `test_product_ingest.py`, `test_product_contract.py` |
| DEV-036~042 | `memory_views.py`, canonical rebuild verification | `test_memory_views_product.py`, governance tamper tests |
| DEV-043~048 | `skills.py`, product Proposal/receipt/replay, Skill package | `test_shared_skills.py` |
| DEV-049~054 | `ContextRouter`, kernel CLI compatibility, product MCP | `test_memory_views_product.py`, `test_product_interfaces.py` |
| DEV-055~060 | `ProductService.cold_start`, handoff/source map/report | ingest/governance cold-start tests |
| DEV-061~067 | `retrieval.py`, FTS/RRF/link/code/on-demand tools | `test_product_retrieval.py`, interface tool tests |
| DEV-068~072 | catalog/queue/web/verify/backup/restore | `test_product_governance_eval.py`, interface tests |
| DEV-073~079 | capture/consolidation/telemetry/metrics/benchmarks | governance and product retrieval evaluations |

## 7. Definition of Done

작업은 다음을 모두 만족할 때 완료다.

- kernel/product JSON Schema와 fixtures가 검증된다.
- accepted/rejected/idempotent/stale/tampered 경로가 자동 시험된다.
- kernel Proposal authority와 One Shared State invariant를 우회하지 않는다.
- derived view/index는 재생성 가능하고 provenance/dependency digest를 가진다.
- 동일 request의 cross-client context hash parity가 확인된다.
- package metadata와 wheel data files/entrypoints가 검증된다.
- 전체 unittest/coverage와 CI 품질·보안·OS 결정성 job이 통과한다.
- README/SRS/ROADMAP이 실제 구현 및 CI 증거와 일치한다.
