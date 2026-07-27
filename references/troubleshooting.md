# Troubleshooting

## The self-stop watcher not surviving a preemption

If the watcher is deployed the "obvious" way — scp a script to `/tmp`, then
launch it once by hand with `systemd-run --unit=... --collect bash
/tmp/selfstop.sh` over SSH — it will quietly stop working after the very
event it exists to handle. This was caught in a real end-to-end test, not
just reasoned about: a toy job was deliberately stopped and restarted mid-run
to simulate a preemption, and the watcher never came back afterward. The VM
sat `RUNNING` (billing) for several extra minutes after the job actually
finished, with nothing watching for `.complete`, until the gap was noticed by
checking `systemctl status` (unit gone) and the `/tmp` script (also gone).

Two independent things caused this, and both matter:
- `/tmp` is cleared on reboot on Debian (and most distros configure it this
  way), so a script placed there doesn't survive one.
- A **transient** systemd unit (`systemd-run --collect`) is explicitly
  ephemeral by design — it's meant for one-off ad hoc commands, and it does
  not persist across a reboot either, even though it does survive the launching
  SSH session closing (which is the problem it *was* solving).

A GCP spot preemption is a full reboot of the guest OS, so both of those
"doesn't survive a reboot" properties turn into "doesn't survive a
preemption" — precisely the scenario the watcher is supposed to be resilient
to. The fix is to stop treating the watcher as a one-time manual deploy step
and instead have it (re-)armed by the **startup script itself**, on every
boot, the exact same way the worker is already relaunched on every boot:
write the watcher's content to the persistent disk (e.g. `$JOBDIR/selfstop.sh`,
not `/tmp`) and call `systemd-run` for it unconditionally near the top of the
startup script, guarded only against double-launching if it's already active.
`scripts/startup_script.sh.template` in this skill does this correctly —
don't deploy `scripts/selfstop_watcher.sh` by hand as the primary mechanism;
it's kept mainly as a readable reference for what the watcher itself does.

## `pkill -f <script-name>` killing its own SSH session

If you ever need to reset a previous background watcher or worker process by
name, the tempting one-liner is:

```bash
gcloud compute ssh VM --command='pkill -f selfstop.sh; sudo setsid bash /tmp/selfstop.sh &'
```

This can silently fail every time, for a non-obvious reason: `pkill -f` matches
against the full command line of every process, and the SSH command *you just
ran* has `selfstop.sh` sitting right there in its own argv (because you typed
it). `pkill -f` matches your own shell and kills it before the relaunch line
executes. The symptom is maddening — the command appears to run, produces no
error, but nothing changes on the VM, and repeating it doesn't help either.

**Fix:** target the process by something that isn't a substring of the command
you're currently running — a systemd unit name (`systemctl stop UNIT_NAME`), a
PID file, or `pkill -f` with a more specific pattern that doesn't appear in the
invoking command (e.g. match the full path `/tmp/selfstop.sh` if your SSH
command only references the unit name, or vice versa). Using `systemd-run
--unit=NAME` for the watcher from the start (as `selfstop_watcher.sh` in this
skill does) sidesteps the whole problem — `systemctl stop NAME` can't
accidentally self-match.

## SSH banner timeouts under heavy load

A VM running many parallel workers can peg its CPU hard enough (load average
near or above its vCPU count) that `sshd` is slow to complete the initial
handshake, and `gcloud compute ssh` fails with something like:

```
Connection timed out during banner exchange
```

This is not usually a real connectivity problem — it's the VM being too busy to
answer promptly. Retry with a longer `--ssh-flag="-o ConnectTimeout=45"` and a
short backoff (a handful of retries a few seconds apart) before concluding
anything is actually wrong. It's normal to see this intermittently on a fully
loaded box and have every other attempt succeed.

## Orphaned disks after deleting a VM

`gcloud compute instances delete` removes the VM but — depending on how the disk
was created and its `autoDelete` setting — can occasionally leave the persistent
disk behind, still billing with nothing attached to it. After any deletion,
don't just check that the one instance is gone; list everything in the project:

```bash
gcloud compute instances list
gcloud compute disks list
```

Both should show nothing left over from the job. If a disk is still there,
delete it explicitly (`gcloud compute disks delete NAME --zone=ZONE`) — but only
after confirming it isn't attached to something else you still need.

## A `.complete` marker that shouldn't have been written

If the worker writes its completion marker unconditionally at the end of a run
rather than only on full success, a run that finished with some members failed
or skipped can trigger the self-stop watcher and shut the VM down mid-problem,
making it harder to debug. Guard the `.complete` write on the run actually
having succeeded for everything expected (see `worker_template.py`'s `main()` --
it only touches `.complete` when no result string contains `"FAILED"`).

## A restarted, finished VM that stops before you can SSH in

Symptom: the job completed successfully, you start the VM to download the
results, and every `gcloud compute ssh` attempt times out or is refused. Checking
the instance shows it back at `TERMINATED` — often before you got a single shell.
It looks like a networking or sshd problem. It isn't.

The watcher tests its markers *before* its first `sleep`, and the startup script
arms it near the top of every boot. So on a VM where `.complete` already exists,
the sequence is: boot → startup script arms watcher → watcher sees `.complete`
immediately → `shutdown -h now` about five seconds later. sshd frequently isn't
ready to accept a connection that early, so the machine is gone before you can
reach it. Retrying just repeats the same race.

This is the cost guard working exactly as designed, colliding with the retrieval
step that needs the opposite. The fix is not to weaken the watcher but to opt out
of it explicitly for the one boot where you need a shell — set the metadata key
**before** starting the VM:

```bash
gcloud compute instances add-metadata JOB_NAME --zone=ZONE --metadata retrieval-hold=1
gcloud compute instances start JOB_NAME --zone=ZONE
```

`startup_script.sh.template` checks that key first and exits without arming the
watcher or relaunching the worker, leaving the VM up indefinitely. That is a real
opt-out of the billing protection, so clear it (`retrieval-hold=0`) as soon as
the download verifies, unless you're deleting the VM straight after.

## A failed run that leaves the VM billing forever

The mirror image of the spurious-`.complete` problem below. If the watcher waits
only on `.complete`, and the worker correctly withholds it because the run did
*not* fully succeed, then nothing ever stops the VM. The stricter the completion
guard, the worse this gets — a job that fails in the first minute can bill for
days.

Have the worker (and, belt-and-braces, the run wrapper) also touch a `.finished`
marker on any terminal outcome, and have the watcher stop on `.complete` **or**
`.finished`. Add an absolute wall-clock backstop for the case where the worker
dies hard enough to write neither. Keep `.complete` strict — `.finished` is what
makes strictness safe to have.

## Verification passed but the numbers "feel wrong"

A checksum match only proves the bytes downloaded intact — it says nothing
about whether the *content* is sane (e.g. a file that's technically valid but
truncated at the source, or a unit that silently produced garbage upstream of
the atomic write). For anything going into real analysis, it's worth a light
content sanity check after checksum verification passes — expected file count,
a plausible value range, no unexpected NaNs/nulls — not just "the bytes
matched."

## Don't build anything important in a session-scoped cache directory

If you're deploying this skill itself (not the jobs it manages), build it
somewhere durable -- a real, persistent config location, not a path that lives
under a session ID or a temp/cache directory tied to the current run. A
session-scoped location can be cleaned up by a janitor process the moment the
session it belongs to is considered stale, silently deleting work that looked
saved. This bit the skill's own first build: it was drafted under a
session-scoped skills cache and was garbage-collected mid-evaluation, along
with most of the eval output files, before the actual result was safely
delivered. If a build location's directory listing shows other entries with
UUID-like names or it's nested under anything resembling
`.../sessions/<id>/...`, treat that as a signal to check for a more permanent
home before investing real work there.
