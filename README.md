# gcp-spot-batch-job

A [Claude Code skill](https://docs.anthropic.com/en/docs/claude-code/skills) for deploying and managing long-running batch jobs on Google Cloud spot/preemptible VMs. Resilient to preemption by design — jobs auto-resume from cached progress on restart — and self-stopping the instant they finish, so nothing bills unattended overnight.

## What problem this solves

Spot (preemptible) VMs on GCP are 60–90% cheaper than on-demand, but two things routinely go wrong:

1. **Hours of progress lost to a preemption** when the job wasn't built to survive one.
2. **A finished VM billing all night** because nobody was watching when it completed.

This skill handles both. It teaches Claude the exact pattern — atomic per-unit caching, startup-script-driven auto-recovery, self-stopping watcher — and wires in the discipline to never restart, download from, or delete a VM without the user's explicit go-ahead.

## How it works

Claude reads `SKILL.md` (the skill definition) and follows its six-step playbook when you ask it to set up a batch job on GCP:

1. **Size the VM to the actual bottleneck** — bandwidth-bound, CPU-bound, or externally throttled jobs each have the right VM type and worker count.
2. **Build a resumable worker** from `scripts/worker_template.py` — atomic per-unit cache writes, skip-if-cached resume, per-member parallelism.
3. **Ship code via instance metadata**, not a baked image — update the running job with one `gcloud` command, no image rebuild needed.
4. **Deploy a self-stop watcher** that shuts the VM down the moment `.complete` appears — armed by the startup script on every boot so it survives preemptions.
5. **Gate three actions** on explicit user confirmation: restarting a stopped VM, downloading results, and deleting the VM. Each is a separate ask, never bundled.
6. **Optionally monitor progress** with concrete log tailing and an auto-restart path for mid-job preemptions.

## Files

```
SKILL.md                          # Skill definition Claude reads and follows
scripts/
  worker_template.py              # Resumable batch worker skeleton (copy + fill TODOs)
  startup_script.sh.template      # GCP startup script with self-stop watcher embedded
  selfstop_watcher.sh             # Standalone watcher reference (readable; use template instead)
  retrieve_and_verify.sh          # Download results + sha256 verify before any deletion
references/
  sizing-and-zones.md             # VM sizing guide and zone selection heuristics
  troubleshooting.md              # Known gotchas: watcher persistence, pkill self-kill, etc.
```

## Installation

Copy this skill into your Claude Code skills directory:

```bash
# User-level (available in all projects)
cp -r gcp-spot-batch-job ~/.claude/skills/

# Or project-level (scoped to one repo)
cp -r gcp-spot-batch-job /path/to/project/.claude/skills/
```

Claude Code picks up skills in `~/.claude/skills/` and `.claude/skills/` automatically — no configuration needed. Once installed, Claude will use this skill whenever you ask it to run something on a GCP spot VM.

## Usage

Just describe what you want to run. Claude will invoke this skill automatically when the request involves:

- A long-running batch job on GCP / Google Cloud
- Spot or preemptible instances, cost control, or "don't let it run up a bill"
- Processing many independent items (files, models, API calls, records)
- Checking on, restarting, downloading from, or deleting a VM a previous job was deployed to

**Examples of things you can say:**

```
Download all 30 models from the climate dataset to a GCP spot VM.
I want to run this Python script over 10,000 API calls cheaply on Google Cloud.
Check on the download job — did it finish?
The VM preempted again. Can you restart it?
Download the results and clean up the VM.
```

Claude will size the VM, adapt `worker_template.py` to your job, generate the startup script, and walk you through the `gcloud` commands — stopping to get your explicit approval before restarting, downloading, or deleting anything.

## Key design decisions

**Atomic per-unit caching** — every unit of work is written via `name.tmp` → `os.replace(tmp, name)`. A crash mid-write never leaves a corrupt file that looks finished on the next run.

**Startup-script-driven recovery** — the startup script runs on every boot, including after a preemption. It re-fetches the latest worker code from instance metadata, clears interrupted scratch, and relaunches the job. This is why code updates are a single `gcloud compute instances add-metadata` call.

**Watcher embedded in the startup script** — the self-stop watcher is written to the persistent disk and armed by the startup script on every boot. A one-time manual deploy doesn't survive a preemption (both `/tmp` and transient systemd units are cleared on reboot); building it into the startup script means the watcher is always present, including after recovery.

**Terminate on preemption, never delete** — VMs are always provisioned with `--instance-termination-action=STOP`, not the default `DELETE`. The disk — and all cached progress — survives.

**Verified download before deletion** — `retrieve_and_verify.sh` tars outputs with a sha256 manifest, transfers the single tarball (more reliable than `scp --recurse` over many files), and verifies every checksum. A file-count match is not verification; a truncated transfer can still produce the right count.

## Tested patterns

The design comes from two real jobs: a 30-model climate-data download (preempted twice in one zone before migrating) and a 33-model aggregation job that ran unattended and stopped itself exactly when it finished. The troubleshooting guide in `references/troubleshooting.md` covers the specific bugs that were caught during those runs — including a watcher that silently disappeared after a simulated preemption and a `pkill -f` that killed its own SSH session.
