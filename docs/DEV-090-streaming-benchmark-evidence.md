# DEV-090 — Streaming benchmark evidence hashing

## Problem

DEV-089 made current-schema 100k certification reproducible, but its final
database-evidence step called `Path.read_bytes()` for the source and replay
files. Each certified hot-active database is 527,572,992 bytes, so hashing
allocated memory proportional to database size even though the certification
otherwise uses bounded inputs and output.

## Contract

- SHA-256 is updated using fixed 1,048,576-byte reads from one open descriptor.
- The file must be a regular file.
- Device, inode, size, mtime, and ctime are sampled from the same descriptor
  before and after streaming; drift or a byte-count mismatch fails closed with
  `DATABASE_CHANGED_DURING_HASH`.
- A directory/special file returns `DATABASE_NOT_REGULAR`; an unreadable file
  returns `DATABASE_EVIDENCE_UNAVAILABLE`.
- The result shape and SHA values remain exactly compatible with
  `context-benchmark-certification@1`.

The opened descriptor, not a second path lookup, is the evidence source. This
change is confined to derived benchmark evidence and does not alter the kernel,
ledger, replay, context, or Shared State.

## Acceptance

- Tests fail if `Path.read_bytes()` is used or any read request exceeds the
  fixed chunk size.
- A deterministic mid-hash append is rejected with the stable drift code.
- Non-regular files fail with the stable type code.
- The full DEV-089 certification and projection regression remains green.
- Streaming both preserved 503MiB DEV-089 hot-active files reproduces the
  checked-in SHA/size exactly with bounded Python allocation.

RED/GREEN and real-file measurements are recorded in
[`testing/dev-090-streaming-benchmark-evidence.tdd.md`](testing/dev-090-streaming-benchmark-evidence.tdd.md).

