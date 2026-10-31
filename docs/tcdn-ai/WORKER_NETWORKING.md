# Worker networking: inbound today, outbound-connection mode next

Status: **design only.** No code implements the outbound mode. The checklist row
is **3.4** — `P2`, `MISSING`. Staging (`STAGING.md` §5) works around it with a
reachable worker.

## 1. What happens today

Every job travels over a connection the **coordinator** opens:

1. The worker registers (`POST /api/ai/v1/nodes/register`) with an `endpoint`
   field — the HTTPS URL it will serve.
2. It heartbeats every 30 s (`POST /api/ai/v1/nodes/heartbeat`), signed with its
   identity key. Heartbeats are outbound, but they carry liveness only.
3. Placement filters out nodes whose last heartbeat is older than 120 s
   (`HEARTBEAT_TIMEOUT_SECONDS`), then scores the rest.
4. The gateway dials the winner: `POST {endpoint}/v1/jobs`
   (`gateway.http_worker_transport`), encrypts the prompt into frames, and
   streams the worker's frames back **on that same response**.
5. The worker posts its signed receipt to the coordinator
   (`POST /api/ai/v1/jobs/{job_id}/receipt`), which is what bills the job.

So: registration and heartbeats are peer-to-peer outbound, but **the work itself
is inbound**. `NODE_HEARTBEAT.relay_reachable` exists and nothing reads it; there
is no relay in the codebase.

## 2. Consequence

A worker behind NAT, CGNAT, a home router, a corporate firewall, or a laptop on
mobile data:

- registers successfully (outbound works),
- looks **healthy** on `/api/ai/v1/status` (heartbeats land),
- and fails every placement with `worker_rejected_job` /
  `worker_unreachable`, because the coordinator cannot open a TCP connection to
  it.

That is worse than an obviously offline worker: the fleet shows green while
every request fails. Tunnels (cloudflared, Tailscale Funnel, ngrok) are the only
way to contribute a machine today, which excludes the "share your idle GPU"
story the product is built on.

## 3. Requirements for the outbound mode

1. No inbound port required on the worker; it only dials the coordinator.
2. Same end-to-end encryption: the coordinator never sends the worker's private
   keys anywhere new, and the job envelope stays AEAD-sealed for the node's
   session key.
3. Same billing authority: the worker's signed receipt still bills the job; the
   gateway's own token count still outranks an overclaim.
4. Liveness must be observable in seconds, and a dead worker must stop receiving
   jobs without operator action.
5. At-most-once delivery per job, and a lost connection must not wedge a job or
   an account's hold.
6. It has to work behind a proxy that buffers and has a request timeout (Render
   sits behind Cloudflare), so no server-initiated connections and no
   "connection stays open forever".

## 4. Design: a pull channel (long-poll) plus a frame upload

The worker inverts the direction. It parks a request on the coordinator; the
coordinator answers that request with a job when it has one for that node.

```
worker                                   coordinator
  │  POST /api/ai/v1/nodes/{id}/jobs/pull   (signed, wait ≤ 25 s)
  ├───────────────────────────────────────►│  parks an asyncio waiter keyed by node id
  │                                        │  … placement picks this node …
  │◄───────────────────────────────────────┤  200 {job_id, ticket, sealed_envelope} | 204
  │  POST /api/ai/v1/nodes/{id}/jobs/{job}/frames   (streamed request body)
  ├───────────────────────────────────────►│  frames ARE the existing worker stream
  │  POST /api/ai/v1/jobs/{job}/receipt     │  (unchanged; this is what bills)
  └───────────────────────────────────────►│
```

Why long-poll rather than a WebSocket:

- a plain HTTPS POST request survives every proxy in the path (Render →
  Cloudflare) and needs no protocol upgrade;
- 25 s is under every idle timeout in the chain (Cloudflare 100 s, Render ~100 s);
- the coordinator side is an ordinary ASGI request, so it inherits the existing
  rate limiting, body-size guards and auth paths;
- backpressure is trivial: the worker has exactly one parked request; if it is
  not parked, placement does not consider it.

The frames upload is a second request whose **request body** is the worker's
existing encrypted frame stream. The coordinator pipes it through the same
`decode_frames` path the gateway already uses, so the crypto, the receipt and the
verification flow do not change at all.

### 4.1 Data flow, concretely

1. `Gate.alive(node_id)` — a process-local `dict[node_id, asyncio.Future]` plus
   a Redis key `ai:pull:{node_id}` with a 30 s TTL, so a coordination across
   coordinator instances is visible and a dead instance's park expires.
2. Placement (`select_worker`) gains a transport filter: `node.transport in
   {"push", "pull"}`; the gateway picks `worker_transport(node)` accordingly.
3. For a pull node, `Gateway.open` does not dial. It writes the sealed envelope
   to `ai:pull-jobs:{node_id}` (a short list, TTL 60 s), then wakes the parked
   waiter. The parked request returns the envelope.
4. The worker executes and uploads frames; the gateway's stream consumer is fed
   from the upload request instead of a response body. The uploader is
   authenticated by the same node signature as the receipt — no new secret.
5. Settlement is unchanged: the worker's receipt arrives, the gateway compares
   its own count, the split is applied, the hold is released.

### 4.2 Delivery, timeouts and failure

| Event | Behaviour |
|---|---|
| No job within 25 s | `204 No Content`; the worker re-parks immediately (jittered 0–250 ms to avoid a thundering herd) |
| Worker disconnects mid-park | waiter cancelled; node considered not-parked; placement skips it |
| Job dispatched, no upload within 60 s | job `expired` via the existing stale-job sweep; hold released |
| Upload dies mid-stream | partial frames discarded, no settlement; sweep releases the hold |
| Duplicate pull for the same node | the older waiter is cancelled (one slot per node) |
| Coordinator restarts between dispatch and upload | the job row is `placed` with a TTL; the sweep expires it; the client sees `worker_timeout` and is not billed |

At-most-once is enforced by `ai_jobs.status`: a dispatch moves `placed →
dispatched`, and a second dispatch for the same job is refused (`409`). A lost
response after dispatch therefore fails the job rather than running it twice —
duplicate execution is the one outcome the design refuses, because it costs the
node real compute.

### 4.3 Security notes

- The node signature over the pull request (its identity key, same envelope
  shape as the heartbeat) is the authentication; no API key is required for the
  pull path beyond the existing worker-scoped key the registration uses.
- The envelope stays sealed for `node.session_public_key`; a coordinator that
  was compromised still cannot read a confidential-tier job (that tier remains
  SDK-mode only and is out of scope here).
- The upload body limit reuses `max_body_bytes` on the *decoded* frame sum, not
  the wire size, so a worker cannot stream unbounded data.
- A node may declare `transport: pull` only at registration; switching requires
  a re-registration signed with the same identity key, which is audited.

## 5. Alternatives considered

| Option | Why not |
|---|---|
| A relay service the worker connects to and the coordinator dials | one more deployment, one more trusted party that sees ciphertext and metadata, and the coordinator still needs an inbound connection — to the relay. It solves NAT by moving it. |
| WebSocket from worker to coordinator | works, but adds a protocol upgrade through Cloudflare, a second auth path, and framing/backpressure code that long-poll gets for free. Consider later for latency-heavy streaming. |
| Requiring Tailscale/cloudflared on every worker | what staging does today. Fine for operators, unacceptable as the documented way to contribute a home GPU. |
| Making the worker poll for work over the existing heartbeat response | couples liveness to work, and a 30 s beat bounds dispatch latency to 30 s. |

## 6. Implementation plan (when it is scheduled)

1. `models.py`: `NodeRegistrationPayload.transport` (`push` default, `pull`).
2. `routes.py`: `POST /nodes/{node_id}/jobs/pull`, `POST /nodes/{node_id}/jobs/{job_id}/frames`.
3. `service.py`: park registry with the Redis visibility key; dispatch for pull nodes.
4. `gateway.py`: `poll_worker_transport` implementing `WorkerTransport` against the
   parked queue; `http_worker_transport` stays for push nodes.
5. `scheduler.py`: filter by transport; a pull node with no parked request is
   `not_parked` (a rejection reason, not a silence).
6. `worker.py`/`ai_worker.py`: `--transport pull` loop with jittered re-park.
7. Tests: a NAT'd worker (no listener at all) runs two completions through the
   gateway; a worker that dies mid-park; a duplicate dispatch; a hold released
   by the sweep; the frame upload tampered with.
8. Checklist: 3.4 moves to `VERIFIED` only with those tests and a staging run.

## 7. Open questions

- Should the pull channel carry the redundant-execution (shadow) job as well?
  It must, or verification silently stops for pull nodes.
- Long-poll concurrency: one parked request per node is simple; a node with
  `max_worker_concurrency > 1` needs N parked requests or a worker-side queue.
  Start with one and lower the advertised concurrency for pull nodes.
- Charges for time parked vs time executing: today billing is per token only,
  which stays correct.
