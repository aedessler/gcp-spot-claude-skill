# VM sizing and zone selection

## Sizing: match the VM to the actual bottleneck

The instinct to reach for a big VM "to be safe" is usually wrong for this kind of
job — the bottleneck almost never turns out to be raw local CPU. Work out which
of these three shapes the job is before picking anything:

| Bottleneck | Signal | VM | `--workers` |
|---|---|---|---|
| **Bandwidth** — downloading many independent files from S3/GCS/HTTP | Each unit is a fetch of a file that's already sitting somewhere; the "work" per file is mostly I/O wait | Large (`n2-standard-16`/`-32`) — more parallel downloaders = more throughput | High (16-32), one member per worker |
| **CPU/memory** — heavy local computation per unit | Each unit involves real number-crunching, image processing, model inference, etc. | Sized to the actual compute (profile one unit locally first if unsure) | Match to vCPU count |
| **Externally throttled** — a rate-limited API, a queued request system (e.g. a data portal that processes one request at a time per user) | The remote service, not local resources, decides how fast things move; a `curl` to the service returns in queue-time, not download-time | Small (`n2-standard-4`/`-8`) — a bigger VM just idles | Low (4-8) — matches the service's real concurrency limit, not vCPU count |

Getting this wrong doesn't break anything — it just wastes money (oversized VM
mostly idle) or leaves easy performance on the table (undersized VM for a
bandwidth job that could've used more parallel workers). When genuinely unsure,
size down and watch the CPU/network utilization once it's running — it's cheap to
resize a *stopped* spot VM (`gcloud compute instances set-machine-type`) if the
first guess was off.

## Provisioning

Always:
```bash
--provisioning-model=SPOT --instance-termination-action=STOP
```
`STOP` (not the default `DELETE`) is what makes preemption non-destructive — the
disk, and everything the resumable worker has cached to it, survives.

## Zone selection

Prefer the zone/region physically closest to the data source or service being
called — it reduces latency for a throttled/API-bound job and can matter for
egress cost on some clouds (though most public open-data buckets have free
egress regardless of requester region).

**But treat the first zone choice as provisional.** Spot capacity varies zone to
zone and hour to hour in ways that aren't visible in advance. If a VM gets
preempted repeatedly in a short window — this happened once already: a job was
preempted twice within about 30 minutes in one zone — that's a real signal to
migrate rather than keep restarting in place. Because the worker's progress
lives on the resumable disk cache (not the VM), migrating is cheap: create a
fresh VM of the same spec in a different zone (same region if possible, or a
nearby one with more headroom), point it at the same job code via metadata, and
let it re-download only what's still marked incomplete. In the one case this
happened, the new zone ran the rest of the job with zero further preemptions.

If you don't know which nearby zone has more headroom, a reasonable default is
to try the "sibling" zones in the same region first (e.g. `us-west1-a` /
`us-west1-c` if `us-west1-b` is struggling) before jumping regions entirely.
