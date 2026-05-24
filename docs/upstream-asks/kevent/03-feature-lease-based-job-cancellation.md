# [RFC] Lease-based job cancellation (defense in depth)

**Type** : design proposal — RFC, no PR yet
**Priority** : P3 — improvement, blocked by client adoption
**Component** : gateway / job lifecycle
**Depends on** : issue #1 acknowledged, ideally PR #2 merged first

## Motivation

[`02-feature-delete-jobs-endpoint.md`](02-feature-delete-jobs-endpoint.md)
adds an explicit cancellation verb that **well-behaved clients can
use** to free the GPU. But not all clients are well-behaved :

- A client process can crash between submission and the next poll →
  no DELETE ever issued.
- A network partition can cut off the client → same outcome.
- A client bug can forget to DELETE in some code path → silent leak.

The gateway today has **no way to detect a gone client**. The
proposed lease mechanism makes the gateway autonomous : if a client
stops polling, the gateway assumes it's gone and reclaims the GPU.

This is the pattern used by SQS (visibility timeout), Celery
(visibility timeout + acks_late), Temporal (activity heartbeats),
and Kubernetes (lease objects). It is the industry standard for
asynchronous compute with abandonment recovery.

## Proposed mechanism

Each job carries a **lease deadline** updated on every successful
`GET /jobs/{id}` :

```
on POST /jobs/{type}:
    lease_until = NOW() + JOB_LEASE_SECONDS  (default 60s)

on GET /jobs/{type}/{id}:
    lease_until = NOW() + JOB_LEASE_SECONDS  (renewal)
    return current status/result

on background tick (every 5s) in gateway:
    for each running job where lease_until < NOW():
        send cancel signal to worker
        mark status = abandoned_by_client
        free GPU slot
```

**No new endpoint.** The poll IS the heartbeat. Existing clients
that poll regularly (every < `JOB_LEASE_SECONDS`) are unaffected.

## Default value of `JOB_LEASE_SECONDS`

Trade-off :

- **Too short** : well-intended clients with bursty network or slow
  polls get killed legitimately. Bad UX.
- **Too long** : a crashed client wastes GPU for `LEASE_SECONDS`
  before reclamation. Defeats the purpose.

Suggested default : **60 seconds**, which is 20× the typical poll
interval (3s) used by `mirai-mesreunions`. Configurable via
env / gateway config. Documented as : "your client must poll at
least every 60s, or use heartbeat via GET on the job id".

If multiple clients want different defaults, expose a per-submission
override :
```http
POST /jobs/{type}
Content-Type: multipart/form-data
…
lease_seconds: 120     ← optional, capped by gateway max (e.g. 600)
```

## Per-job hard budget (orthogonal)

In parallel, introduce a **hard ceiling per job** computed from the
submitted audio duration :

```
budget_seconds = ceil(audio_duration_seconds × MAX_RTF × SAFETY_FACTOR)
               = e.g. audio_duration × 0.6 × 2 = 1.2 × audio_duration
```

After `budget_seconds`, the gateway kills the job regardless of
client polling, returning `status=budget_exceeded`. This protects
against worker bugs (infinite loop on a corrupted audio) where the
client would happily poll forever.

This is **defense in depth** : the lease handles "client gone", the
budget handles "model stuck". They cover different failure modes.

## Client migration plan

To avoid breaking existing clients :

1. **Phase 1** : add the mechanism, but `JOB_LEASE_SECONDS=∞` by
   default (no behavior change). Add metric "would-be-cancelled
   jobs" so we can size the cap based on real traffic.
2. **Phase 2** : announce a deprecation date. Document the
   heartbeat requirement. Clients add poll-on-poll heartbeat if
   they don't already.
3. **Phase 3** : reduce default to 60s. Operators can override per
   instance.

For `mirai-mesreunions` specifically : Phase 6 of the
client-side fix (commit `78abecb`) already implements the heartbeat
via `on_poll` callback. We're Phase-2-ready today.

## Why not just polling RAM/GPU utilization

Tempting alternative : "if the worker is still using GPU memory, it's
still working, keep the slot. Else free it." Doesn't work :

- Whisper/pyannote hold GPU memory throughout inference even when
  blocked on I/O.
- A crashed Python worker can leak GPU memory until process exit.
- "Is the worker using GPU" doesn't distinguish "actively
  transcribing" from "stuck in infinite loop".

The **client liveness signal** (does someone still want this
result?) is fundamentally different from the **worker progress
signal** (is the model still making progress?). Conflating them
gives bad heuristics. The lease answers the first question
cleanly ; the budget answers the second.

## Prior art

- SQS visibility timeout :
  [https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)
- Celery `visibility_timeout` + `acks_late` :
  [https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-broker_transport_options](https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-broker_transport_options)
- Temporal activity heartbeats :
  [https://docs.temporal.io/dev-guide/python/features#activity-heartbeats](https://docs.temporal.io/dev-guide/python/features#activity-heartbeats)
- Kubernetes coordination.k8s.io/Lease objects :
  [https://kubernetes.io/docs/concepts/architecture/leases/](https://kubernetes.io/docs/concepts/architecture/leases/)

All four converge on the same model : the consumer must prove
liveness, the broker reclaims silently. We don't need to invent
anything.

## Open questions for the Kevent team

1. Is there appetite for a multi-phase rollout, or do you prefer to
   ship lease + DELETE + budget together as one breaking change?
2. Storage backend (Redis) — TTL on lease entries vs separate
   background sweeper — preference?
3. Metrics : add `kevent_jobs_cancelled_by_lease_total` /
   `_by_delete_total` / `_by_budget_total` — OK to add to the
   existing Prometheus exporter?
4. Compatibility with sync endpoints (`POST /v1/audio/transcriptions`) :
   N/A by design (no job_id to lease), or do you want the same
   protection (request-context cancellation)?
