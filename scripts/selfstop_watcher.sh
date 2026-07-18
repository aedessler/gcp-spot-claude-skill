#!/bin/bash
# Self-stop watcher: waits for the worker's .complete marker, then shuts the
# VM down so a finished spot instance never bills overnight unattended.
#
# IMPORTANT: this file is kept as a readable standalone reference, but
# scripts/startup_script.sh.template already embeds and (re-)arms this exact
# logic on EVERY boot -- use that, don't deploy this file by hand over SSH.
# A one-time manual deploy (scp this file to /tmp, launch it once via SSH)
# was the original design and it does NOT survive a preemption: /tmp is
# cleared on reboot and a transient systemd unit does not persist across a
# reboot either, so after a preemption the watcher would simply be gone and
# never re-arm, silently leaving a finished job's VM running indefinitely.
# This was caught in a real toy-job test, not just reasoned about -- see
# references/troubleshooting.md for the full story. The fix is writing the
# watcher to the persistent disk (under JOBDIR, not /tmp) and having the
# startup script itself relaunch it every boot, exactly like the worker.
#
# If you do want to arm this by hand for a quick one-off (e.g. testing),
# the old approach still works for that narrow purpose:
#
#   gcloud compute scp selfstop_watcher.sh JOB_NAME:/tmp/selfstop.sh --zone=ZONE
#   gcloud compute ssh JOB_NAME --zone=ZONE --command='
#     sudo systemd-run --unit=JOB_NAME-selfstop --collect bash /tmp/selfstop.sh
#   '
#
# To replace a previous instance of this watcher (e.g. after pushing an
# updated version), use `sudo systemctl stop JOB_NAME-selfstop` (and
# `systemctl reset-failed` if it's in a failed state) -- NOT `pkill -f
# selfstop.sh`. If you kill-and-relaunch in the same SSH command, pkill -f
# will match that very SSH command's own argv (it contains the string
# "selfstop.sh" too) and kill your session before the relaunch runs. systemctl
# targets the unit by name, not by matching command-line text, so it doesn't
# have this problem.

set -u
JOBDIR="${JOBDIR:-/opt/JOB_NAME}"   # TODO: match the JOBDIR used in the startup script
LOG="$JOBDIR/selfstop.log"
exec >>"$LOG" 2>&1

echo "=== selfstop watcher armed $(date -u) ==="
while [ ! -f "$JOBDIR/.complete" ]; do
  sleep 120
done

echo "$(date -u) .complete detected; stopping VM now."
sync
sleep 5
/sbin/shutdown -h now
