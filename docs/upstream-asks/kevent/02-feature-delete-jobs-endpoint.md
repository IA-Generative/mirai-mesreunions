# [Feature] Add `DELETE /jobs/{service_type}/{job_id}` endpoint

**Type** : feature request — small PR
**Priority** : P2 — unblocks client-side fix for issue #1
**Component** : gateway / REST API
**Depends on** : issue
[01-issue-job-leak-when-client-republishes.md](01-issue-job-leak-when-client-republishes.md)

## Motivation

Today the gateway exposes :

- `POST /jobs/{service_type}` — submit job, returns 202 + `{job_id, status: pending}`
- `GET /jobs/{service_type}/{id}` — get status + result
- (implicit DELETE on result pickup after `status=completed`)

There is **no way for a client to abandon a job before completion**.
When the client decides not to wait for the result anymore (timeout,
retry, user cancellation), the GPU keeps churning until the job
finishes — and the result is then discarded because the client has
moved on. Cf issue #1 for the production incident this caused.

## Proposed API

```http
DELETE /jobs/{service_type}/{job_id}
Authorization: Bearer <token>

204 No Content       → cancelled (or already terminal — idempotent)
404 Not Found        → unknown job_id
401 / 403            → standard auth response
409 Conflict         → job in non-cancellable state (e.g., result upload in progress)
```

Semantics :

- **Idempotent** : repeated `DELETE` returns 204 if the job is in any
  terminal or cancelled state. Only unknown ids return 404.
- **Best-effort** : the gateway emits a graceful stop (SIGTERM or
  worker-internal cancel token) to the worker. If the worker doesn't
  cooperate within `CANCEL_TIMEOUT_S` (default 5s), force-kill and
  free the slot.
- **State transition** : whatever the current status, after a
  successful DELETE the job becomes `cancelled`. Subsequent GETs
  return `{status: cancelled, cancelled_at: …}`.
- **Storage** : cancellation events are stored briefly (until normal
  job TTL) so a client polling after cancellation gets a clear
  answer instead of 404. After TTL, falls back to standard 404.

## Why a new verb instead of POST /jobs/{id}/cancel

REST convention. Cancel = "stop and discard", which maps to DELETE
semantically. The body would be empty either way. No state to
transmit.

Examples in the wild :
- [Kubernetes API : DELETE pod](https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.29/#-strong-write-operations-pod-v1-core-strong-) (graceful termination)
- [GCP AI Platform jobs.cancel](https://cloud.google.com/ai-platform/training/docs/reference/rest/v1/projects.jobs/cancel)
  — POST, but motivated by the need to return a body (we don't)
- [AWS Batch CancelJob](https://docs.aws.amazon.com/batch/latest/APIReference/API_CancelJob.html)
  — POST, same reason

DELETE is more idiomatic for a no-body cancellation.

## Client-side impact

In `mirai-mesreunions/services/dmz-to-internal-bridge/app/pipeline_watchdog.py`,
the `_reset_and_resubmit_kevent_pipeline` path would call DELETE on
the previous `kevent_job_id` (if any) before re-submitting. Estimated
GPU savings on production loops : ~40 GPU-hours per pathological
incident (cf metrics in issue #1).

## Sketch of implementation (gateway side)

```go
// cmd/gateway/main.go
func (g *Gateway) handleDeleteJob(w http.ResponseWriter, r *http.Request) {
    serviceType, jobID := parsePath(r)
    job, err := g.store.Get(serviceType, jobID)
    if errors.Is(err, ErrNotFound) {
        http.Error(w, "", 404); return
    }
    if job.IsTerminal() {
        w.WriteHeader(204); return  // idempotent
    }
    if !job.CanCancel() {
        http.Error(w, "", 409); return
    }
    if err := g.worker.Cancel(serviceType, jobID, CancelTimeout); err != nil {
        // log + force release slot
    }
    g.store.MarkCancelled(serviceType, jobID)
    w.WriteHeader(204)
}
```

Estimated effort : ~1 day for endpoint + worker cancellation + tests.

## Out of scope for this PR

- The lease-based auto-cancellation (cf
  [03-feature-lease-based-job-cancellation.md](03-feature-lease-based-job-cancellation.md))
  is a separate proposal. The two are independent : DELETE is for
  explicit cancellation, the lease is for crashed-client defense.
- Per-job hard timeout based on audio duration (also separate, cf
  ADR `0001-pipeline-liveness-vs-progress.md` in the client repo).
