# [Bug] GPU job continues consuming resources after client republishes

**Type** : bug report (factual)
**Priority** : P1 — confirmed GPU resource leak in production
**Component** : gateway / job lifecycle

## Summary

When a client (un nous, `mirai-mesreunions/internal-ingester`)
re-submits the same audio for transcription because it considered the
previous job lost, **the previous job keeps running on the GPU until
its natural completion**, even though no client is polling for its
result. We observed up to **6 parallel jobs for the same audio**
during pathological loops, consuming up to ~30 minutes of L4 GPU
time for a single 2h-audio session that finally returned a single
result.

## Reproduction

1. Submit a long audio (e.g., 120 min) via `POST /jobs/audio` with
   `operation=transcription`.
2. Begin polling `GET /jobs/audio/{id}`.
3. Stop polling for > job TTL.
4. Submit the same audio again, obtain a new `job_id`.
5. Observe : the original `job_id` continues processing on the GPU
   (visible via `nvidia-smi` or via gateway metrics if exposed).
   The result is eventually produced but lost — the next `GET` on
   the old id returns 404 (TTL expired) or stale data.

## Evidence from prod-bêta (mai 2026)

DB inspection of 7 rows in `kevent_failed` with `reprocess_version`
between 49 and 61, all corresponding to long audios (3 472–7 589 s
= 58–126 min). For each row :

- `reprocess_history` shows ~60 watchdog-triggered re-submissions
  spaced ~5 minutes apart.
- Each re-submission obtained a new `kevent_job_id`.
- All previously-submitted `job_id`s eventually returned
  `{"error": "job not found"}` on probe (TTL expired).
- L4 GPU utilization on the prod-bêta diarization pool was
  saturated during the pathological window despite low logical
  throughput.

Estimated waste : 6 parallel ~6-min L4 jobs × 60 cycles × 7 rows ≈
**40+ hours of L4 GPU time wasted** to ultimately deliver 0
successful transcription (all 7 rows ended in `kevent_failed` once
the client-side cap kicked in).

The client-side cap is in place (commit
[3cb2ad7](https://github.com/.../commit/3cb2ad7), `MAX_AUTO_RETRIES=5`)
and a heartbeat fix is being deployed (this repo, commit `78abecb`)
to eliminate the false-positive re-submissions. But the underlying
GPU leak — that a long-running job has no way to know the client is
gone — remains.

## Proposed fix (light)

Add a way for the client to **explicitly abandon a job**, so the
gateway can immediately release the GPU :

```http
DELETE /jobs/{service_type}/{job_id}
→ 204 if cancelled
→ 404 if already terminal or unknown
→ 409 if cannot be cancelled (e.g., result already being uploaded)
```

Semantics : idempotent, best-effort. Gateway emits SIGTERM (or
equivalent) to the worker, frees the slot, marks the job as
`cancelled` in its store. Client confirms cancellation before
re-submitting.

See companion PR draft in
[02-feature-delete-jobs-endpoint.md](02-feature-delete-jobs-endpoint.md).

## Proposed fix (defensive)

Introduce a **client liveness lease** : if no `GET /jobs/{id}` is
received for `JOB_LEASE_SECONDS` (default 60s), the gateway
considers the client gone and cancels the job. The client-side
heartbeat is then implicit — every poll renews the lease. No new
endpoint, change of behavior only.

See companion design proposal in
[03-feature-lease-based-job-cancellation.md](03-feature-lease-based-job-cancellation.md).

## Why this matters for capacity planning

Without these fixes, every client-side bug that triggers a re-submit
loop (we just had one, others will come) becomes a GPU outage. The
heartbeat fix on our side closes the door for *this specific* bug,
but :

- Defense in depth requires the GPU pool to **protect itself**
  rather than trust clients.
- New tenants onboarding Kevent will likely reproduce the issue
  before they realize they need client-side heartbeats.
- Scaling the diarization pool to absorb the leak (current
  workaround) burns budget unnecessarily.

## Cross-reference

- Internal post-mortem : `docs/adr/0001-pipeline-liveness-vs-progress.md`
  (this repo, post-incident decision record).
- Related Kevent issue : #49 (job TTL too short for long audios) —
  this is a different problem (TTL on Redis store, not GPU lifecycle)
  but the two combine pathologically.
