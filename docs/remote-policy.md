# Remote adapter policy boundary

`shared_mind.remote_policy` is a deterministic, deny-by-default authorization
primitive for a future remote adapter transport. It compiles a local policy and
evaluates an already-authenticated binding against one request. It performs no
authentication, DNS lookup, socket connection, TLS verification, HTTP request,
credential loading, source read, Proposal submission, or canonical mutation.

There is currently no live remote vendor connector or credential support. This
module is not wired into the local external-source adapters described in
[External source adapters](adapters.md). Until a reviewed transport is added,
the implemented status is policy evaluation only and local/offline.

## Trust is out of band

The request's `claimed_actor_id` is untrusted input. It never establishes
identity. A future transport must authenticate its peer first and pass the
result separately through the keyword-only `authenticated_binding` argument:

```python
from shared_mind.remote_policy import compile_policy, evaluate_request

compiled = compile_policy(policy_document)
decision = evaluate_request(
    compiled,
    request_document,
    authenticated_binding=transport_authenticated_binding,
)
audit_document = decision.as_dict()
```

The authenticated binding must match a configured trust binding on
`binding_id`, `issuer`, `subject`, and `actor_id`. Omitting it yields
`MISSING_TRUST_BINDING`; a self-asserted actor in the request cannot substitute
for it. How a future transport derives those four values—such as mTLS, workload
identity, or a verified token—is intentionally outside this module and is not
implemented today.

Likewise, the policy requires an endpoint `origin`, but the pure evaluator does
not connect to or authenticate that origin. A future transport must bind its
verified origin to the configured `endpoint_id` before evaluation. The
evaluator compares the request's endpoint ID, protocol version, and adapter
version against their pins.

## Compilation and policy hash

`compile_policy(document)` requires:

- `policy_version: "remote-adapter-policy@1"`;
- `default_effect: "DENY"`;
- a registry version;
- an endpoint pin containing `endpoint_id`, `origin`, `protocol_version`, and
  `adapter_version`;
- at least one trust binding, source label, and capability scope.

Compilation makes a defensive canonical JSON copy and computes a SHA-256
`policy_hash`. Mapping order does not affect the hash, and the caller's input is
not mutated. The compiled value is immutable and every decision carries its
policy hash, making the exact policy revision auditable.

## Deny-default checks

Evaluation fails closed in a stable order. A request must pass every applicable
boundary:

1. `request_version` is exactly `remote-policy-request@1`;
2. an authenticated binding is present and exactly matches a configured trust
   binding;
3. the claimed actor matches the authenticated actor;
4. endpoint ID, protocol version, and remote adapter version match their pins;
5. the predicate-registry version matches the policy;
6. the capability exists;
7. the operation type and actor are allowed by that capability;
8. the source reference is safe, labeled, and beneath an allowed source root;
9. the derived sensitivity is allowed;
10. every requested disclosure field is allowlisted.

Unknown capabilities, operations, actors, sources, sensitivity levels, fields,
versions, and bindings are denied. Source labels use the most specific matching
root. Backslashes, literal dot segments, and percent-decoded dot-segment escapes
are rejected before labeling, so a broad prefix cannot authorize traversal.

The current stable denial codes are:

```text
ACTOR_BINDING_MISMATCH
DISCLOSURE_FIELD_DENIED
ENDPOINT_PIN_MISMATCH
MISSING_TRUST_BINDING
OPERATION_SCOPE_DENIED
REGISTRY_VERSION_MISMATCH
REMOTE_REQUEST_VERSION_MISMATCH
REMOTE_VERSION_PIN_MISMATCH
SENSITIVITY_DENIED
SOURCE_SCOPE_DENIED
TRUST_BINDING_NOT_ALLOWED
UNKNOWN_CAPABILITY
```

## Disclosure and audit output

An allowed decision returns only the requested fields that were allowlisted,
plus the configured redaction paths that apply to those field roots. A denied
decision returns empty `allowed_fields` and `redacted_paths`. This decision is
an authorization result; the policy module never reads, redacts, or transmits
the underlying content itself.

The canonical `remote-policy-decision@1` audit document contains:

- request ID, outcome, stable reason codes, policy hash, and deterministic
  decision ID;
- authenticated actor and trust-binding IDs;
- endpoint ID, protocol version, and adapter version, but not endpoint origin;
- registry version, capability, and operation type;
- a coarse policy-derived source label: source root, sensitivity, and data
  classes;
- allowed disclosure fields and redaction paths.

It does not echo the raw source reference, source content, requested secret
field names on denial, authentication secret material, credentials, or tokens.
Callers must still keep secrets out of identifiers such as `request_id`, because
identifiers intentionally appear in the audit record.

## Future transport integration

A future network transport must add its own conformance-tested boundary without
widening this pure policy API:

1. authenticate the remote peer and verify its network origin;
2. derive an out-of-band binding and pinned endpoint ID;
3. compile the reviewed local policy and verify its expected hash;
4. evaluate capability, operation, source label, sensitivity, registry,
   versions, and requested disclosure;
5. on denial, perform no fetch, disclosure, or Proposal submission;
6. on allow, disclose only allowlisted fields after applying every configured
   redaction;
7. convert any accepted response to an immutable source snapshot and use the
   normal adapter Proposal validation and atomic commit path.

Transport code must not put credentials into request documents, source
locators, Proposal metadata, adapter failures, or audit output. Adding live
network I/O, authentication, credential storage, vendor SDKs, or remote writes
is future work and requires a separate security review and failure-injection
suite.
