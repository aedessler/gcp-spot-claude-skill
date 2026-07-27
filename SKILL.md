---
name: gcp-spot-batch-job
description: Deploy and manage a long-running batch download or compute job on a Google Cloud spot/preemptible VM, resilient to preemption (auto-resumes from cached progress) and self-stopping the instant it finishes (so nobody has to remember to turn it off). Use this whenever the user wants to run something on GCP/Google Cloud/a Compute Engine VM that will take more than a few minutes, processes many independent items (files, models, API calls, records), or explicitly mentions spot/preemptible instances, cost control, or "don't let it run up a bill" — even if they never say the word "resumable." Also use it when the user asks to check on, restart, "ping," download from, or delete a VM that a job like this was deployed to. The one thing this skill insists on: restarting a stopped VM, downloading results, and deleting the VM are three separate actions that always wait for the user's explicit go-ahead, never bundled or assumed.
---

# GCP spot batch job

## The shape of the problem

A user wants to process a large, parallelizable batch of work — download every file
in a dataset, run a model over many inputs, hit a rate-limited API thousands of
times — and run it on a Google Cloud spot VM to save money. Spot VMs are ~60-90%
cheaper than on-demand, but Google can reclaim one at any moment with only a short
warning. Two failure modes matter:

1. **Losing hours of progress to a preemption**, if the job isn't built to survive one.
2. **A finished VM billing all night** because nobody was watching when it completed.

This skill exists to make both of those non-issues, using a pattern proven across
two real runs (a 30-model climate-data download and a 33-model aggregation job) —
one preempted twice in one zone before migrating; the other ran for hours unattended
and stopped itself the moment it was done, with zero data lost either way.

The mechanism is simple once you see it: **make the smallest unit of work small
enough that losing one in-flight unit is cheap, cache each finished unit to disk
immediately, and never do anything to the VM (restart, download, delete) that the
user didn't just ask for.**

## Step 1 — Size the job honestly

Before touching `gcloud`, figure out what the job is actually bottlenecked on —
this decides both the VM size and how much `--workers` parallelism helps at all.
Three shapes come up constantly:

- **Bandwidth-bound** (downloading many independent files from object storage):
  the bottleneck is network throughput, and more parallel downloaders directly
  helps. A large VM (`n2-standard-32` or similar) is justified — spend on vCPUs to
  run many workers at once, one item-source per worker.
- **CPU/memory-bound** (heavy local computation per item): size to the actual
  compute, not the item count.
- **Externally throttled** (a rate-limited API, a queue-based service like a
  climate-data portal): the bottleneck is the remote service's concurrency limit,
  not local resources. A large VM here is wasted money — a small one
  (`n2-standard-4` or `-8`) with modest `--workers` (4-8) is correct; most
  "workers" will just be waiting in the remote queue regardless of local CPU.

See [references/sizing-and-zones.md](references/sizing-and-zones.md) for the
region/zone heuristic (prefer proximity to the data source; watch for a zone that
preempts repeatedly and be ready to migrate — this happened once already and the
new zone ran preemption-free afterward) and full VM-size guidance.

Always provision as **SPOT** with `--instance-termination-action=STOP` (never the
default `DELETE` — a preempted VM must keep its disk).

## Step 2 — Design the worker to be resumable by construction

The single idea that makes everything else possible: **break the job into units
small enough that losing one to a preemption barely matters, and persist each
finished unit to disk before moving to the next.**

- Pick a natural unit — one file, one year, one 5-item API batch, whatever the
  data's own granularity suggests. If a "member" of the ensemble (a model, a
  device, a customer) needs many units, cache each unit separately under that
  member's own folder.
- Write each finished unit with an **atomic rename**: write to `name.tmp`, then
  `os.replace(tmp, final)`. This is the detail that actually delivers the
  guarantee — without it, a crash mid-write can leave a corrupt file that *looks*
  finished on the next run and gets silently trusted.
- On every startup, list which units already have a final (non-`.tmp`) cache file
  and skip them. A worker that gets killed mid-unit loses at most that one unit,
  not the job.
- Once every unit for a member is cached, concatenate them into that member's
  final deliverable, and skip the member entirely if its final file already
  exists — so re-running the whole worker after a full restart is cheap even for
  members that finished long ago.
- Parallelize across members (not units) with a `--workers N` flag over a process
  pool — one member per worker keeps the resumable-unit logic simple and isolates
  crashes.
- Log concretely as you go (`"[member] downloaded 40/81 (unit=2039)"`) — this is
  what makes progress observable from outside without instrumenting anything
  extra.

[scripts/worker_template.py](scripts/worker_template.py) is a working skeleton of
this exact pattern — copy it in, replace the `TODO`-marked fetch/compute logic
with the real per-unit work, and the resumable cache/skip/concat/parallelism
scaffolding is already correct. Don't rebuild this from scratch each time.

## Step 3 — Ship the code as metadata, not a baked image

Put the worker script's contents into an instance metadata key (e.g. `job-code`)
rather than a custom image. This one choice is what makes the rest of the pattern
cheap to iterate on:

```bash
gcloud compute instances add-metadata JOB_NAME --zone=ZONE \
  --metadata-from-file job-code=worker.py
```

updates the code on a stopped *or running* VM instantly — it takes effect on the
next boot with no image rebuild, no redeploy pipeline.

The **startup script** is what turns this into automatic preemption recovery: GCP
runs it on *every* boot, preemption-triggered restarts included. It should:
install dependencies once (guarded by a `.setup_done` marker so a restart doesn't
reinstall everything), pull the latest code from metadata, clear any scratch left
by an interrupted run, and — unless a `.complete` marker already exists or the
worker is already running — launch it detached (`setsid ... &`) with output
redirected to a log.

[scripts/startup_script.sh.template](scripts/startup_script.sh.template) is this
script, parametrized. If the job needs a credential (an API key, a service token),
pass it as its own metadata key and have the startup script write it to the right
config file — call this out explicitly to the user as a secret, and don't let it
end up committed to a repo.

## Step 4 — Make the VM stop itself the instant it's done

Don't rely on a human noticing completion — a spot VM idling overnight after the
job finished is pure waste. Deploy a tiny watcher that polls for the worker's
completion marker and shuts the machine down the moment it appears.

**Use two markers, not one.** This is the detail that decides whether the cost
guard actually holds:

- `.complete` — the run succeeded *fully*. Strict, and worth keeping strict
  (a partial run must never look done).
- `.finished` — the run reached a **terminal** state, success or not.

Watch for **either**, plus an absolute wall-clock backstop. If the watcher keys
only off `.complete`, then a run that *fails* writes no marker at all, the
watcher waits forever, and the VM bills indefinitely — precisely the failure this
step exists to prevent. The strictness of `.complete` is what creates the hazard,
so `.finished` is what defuses it without loosening `.complete`.

**Ordering constraint, silently fatal if reversed:** the startup script must
delete a stale `.finished` **before** arming the watcher, because the watcher
tests its markers *before* its first sleep and will otherwise stop the VM seconds
into a fresh retry. Never clear `.complete` on boot — a genuinely finished job
should stop again rather than redo its work.

**Retrieval deadlock — the flip side of doing this right.** Because the watcher
tests before sleeping, every boot of an already-`.complete` VM shuts it down
within seconds. That is correct, and it makes the machine impossible to SSH into
to collect results: Step 5 says to download "once the VM is running and
`.complete` is present," which is a state you otherwise can't hold. The template
therefore honours a `retrieval-hold=1` instance-metadata key that suppresses the
watcher and worker for one boot. It is a deliberate opt-out of the cost guard —
a VM booted with it set runs until stopped by hand — so clear it as soon as the
download is verified.

**The watcher must be (re-)armed by the startup script itself, on every boot** —
not launched once by hand over SSH. This isn't a style preference: a preemption
*is* a reboot, and a one-time manual deploy doesn't survive one. A real toy-job
test caught this directly — `/tmp` is cleared on reboot and a transient systemd
unit doesn't persist across one either, so after a simulated preemption the
watcher was simply gone, and a finished job's VM sat running (billing) for
several extra minutes with nothing watching it, until the gap was noticed and
fixed by hand. [scripts/startup_script.sh.template](scripts/startup_script.sh.template)
already builds this in correctly: it writes the watcher to the **persistent
disk** (`$JOBDIR/selfstop.sh`, not `/tmp`) and relaunches it via
`systemd-run --unit=... --collect` unconditionally near the top of every boot —
the exact same "runs every boot, including after preemption" property that
already makes the worker itself resilient. Use that template rather than
deploying [scripts/selfstop_watcher.sh](scripts/selfstop_watcher.sh) by hand;
the standalone file is kept mainly as a readable reference and explains this
in its own header.

**A gotcha worth knowing before it costs you a debugging session:** don't reset a
previous watcher with `pkill -f selfstop.sh` from inside an SSH command whose own
command line also happens to contain the string `selfstop.sh` — it matches and
kills the SSH session's own shell, and the whole command silently dies before
anything else runs. Use `systemctl stop <unit>` (or a PID file) instead of
`pkill -f` for anything you might invoke from the same command you're trying to
protect. See [references/troubleshooting.md](references/troubleshooting.md) for
this and other gotchas (the watcher-persistence issue above, flaky SSH under
heavy load, orphaned disks).

Once this is running, completion and preemption converge on the same safe state
either way: **VM stopped, disk and every cached/final file intact, billing
halted.** Neither one requires a human to be watching.

## Step 5 — Three actions that are never automatic

This is the part worth being disciplined about, because it's exactly where a
well-meaning shortcut turns into a bad afternoon. Three actions in this whole
lifecycle require the user's explicit, in-the-moment go-ahead — not a standing
"yes you can do this kind of thing" from earlier, and not an inference from
context. Each one is separately gated:

1. **Restarting a stopped VM.** A VM that's stopped — whether because the job
   finished or because it was preempted mid-run — stays stopped until the user
   asks to check on it ("ping the machine," "check on the download," etc.). Don't
   poll a stopped VM and restart it proactively; that's the user's call, made when
   *they* want to know the status, not when convenient for you.

   The one exception, and it's a real exception because it isn't a new decision:
   if the VM unexpectedly goes to `TERMINATED` *while the job is still running*
   (i.e. `.complete` doesn't exist yet) — that's a preemption mid-job, not a
   finished job waiting to be checked on. Auto-restarting there is just resuming
   work already authorized, the same as the worker itself resuming from cache. A
   monitoring loop doing this is reasonable and was used successfully this way.

2. **Downloading the results.** Only once the VM is confirmed running and
   `.complete` is present.

   To reach that state at all you must set the retrieval hold *before* starting
   the VM — otherwise the watcher stops it within seconds of boot and your SSH
   attempts just time out on a machine that is already shutting down:

   ```bash
   gcloud compute instances add-metadata JOB_NAME --zone=ZONE \
     --metadata retrieval-hold=1
   gcloud compute instances start JOB_NAME --zone=ZONE
   ```

   Then use [scripts/retrieve_and_verify.sh](scripts/retrieve_and_verify.sh):
   it SSHes in, tars every output file together with a `sha256sum` manifest,
   `scp`s the single tarball down (far more reliable than `scp --recurse` over
   many small files), extracts locally, and verifies every file's checksum
   against the manifest before calling anything successful. A raw file count is
   not verification — a truncated transfer can still produce the right count.
   **Check its file glob** (`*.nc *.csv` by default) actually covers this job's
   outputs — a run manifest or JSON sidecar is easy to leave behind, and losing
   it costs you the record of which gaps were expected.

   Checksums prove transport, not correctness. Follow with a content sanity
   check before trusting the data.

   Clear the hold (`--metadata retrieval-hold=0`) once verified, unless the VM
   is about to be deleted anyway.

3. **Deleting the VM.** Only when the user explicitly asks, and only after a
   **verified** download. If verification fails for any reason — a checksum
   mismatch, a missing file, a wrong count — stop, report exactly what failed, and
   leave the VM alone. Do not delete "since the download basically worked" or "to
   be safe" — the failure mode of an extra $2 of idle spot billing is trivially
   recoverable; the failure mode of deleting an unverified dataset is not. A
   successful, fully verified download still does **not** license deletion on its
   own — that's always a separate, later ask, even in the same conversation.

   After deleting, list every instance *and every disk* in the project (not just
   describe the one instance you deleted) — a detached persistent disk keeps
   billing even with no VM attached, and it's easy to leave one behind by mistake.

## Step 6 — Watching it run (optional, but genuinely useful)

For a job that'll take hours, a lightweight monitor is worth setting up: poll
instance state every few minutes; if it's unexpectedly `TERMINATED` with no
`.complete`, that's the one auto-restart case from Step 5; otherwise SSH in
occasionally and report concrete progress (`"finals=12/30 units_cached=812/2430"`)
by counting cache files or tailing the log.

Two things that look like real problems but usually aren't: a single SSH timeout
under heavy load (a fully-loaded 32-vCPU box can starve its own `sshd` handshake
for a few seconds — retry with backoff before concluding anything is wrong), and
`systemctl`/`pkill` interactions during that same load — see
[references/troubleshooting.md](references/troubleshooting.md).

## Quick end-to-end checklist

1. Size the VM to the actual bottleneck (Step 1); pick a zone near the data.
2. Build the worker from `scripts/worker_template.py` (Step 2).
3. Ship it via metadata + `scripts/startup_script.sh.template` (Step 3).
4. Deploy the self-stop watcher (Step 4).
5. Launch, then leave it alone except for optional progress heartbeats (Step 6).
6. Wait for the user to ask about it before restarting, downloading
   (`scripts/retrieve_and_verify.sh`), or deleting anything (Step 5).
