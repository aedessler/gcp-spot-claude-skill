#!/bin/bash
# Download a finished job's output directory from a GCP VM and verify every
# file byte-for-byte before trusting the download. Run this FROM the local
# machine (not on the VM). Exits non-zero on ANY verification failure --
# treat that as "do not delete the VM," full stop.
#
# Usage:
#   ./retrieve_and_verify.sh VM_NAME ZONE REMOTE_OUTPUT_DIR LOCAL_DEST_DIR
#
# Example:
#   ./retrieve_and_verify.sh france-tmax us-west1-b /opt/france/tmax_france \
#       "./results"
#
# Why tar+sha256, not `scp --recurse` or a bare file-count check:
#   - scp --recurse over many small files is slow and has historically been
#     the flakiest step in this whole pipeline; one tarball is one transfer.
#   - a file count can match while a file is silently truncated (network
#     hiccup mid-transfer) -- only a checksum catches that.

set -u
VM="${1:?usage: retrieve_and_verify.sh VM_NAME ZONE REMOTE_OUTPUT_DIR LOCAL_DEST_DIR}"
ZONE="${2:?missing ZONE}"
REMOTE_DIR="${3:?missing REMOTE_OUTPUT_DIR}"
LOCAL_DIR="${4:?missing LOCAL_DEST_DIR}"

mkdir -p "$LOCAL_DIR"

echo "=== tar finals + sha256 manifest on the VM ==="
gcloud compute ssh "$VM" --zone="$ZONE" --ssh-flag="-o ConnectTimeout=30" --command="
  cd '$REMOTE_DIR' && \
  sha256sum *.nc *.csv 2>/dev/null > /tmp/retrieve.sha256 && \
  tar cf /tmp/retrieve.tar \$(cat /tmp/retrieve.sha256 | awk '{print \$2}') && \
  echo TARRED files=\$(wc -l < /tmp/retrieve.sha256)
" || { echo "FAIL: could not tar/checksum on the VM"; exit 1; }
# NOTE: the glob *.nc *.csv above is a common case -- adjust to match the
# real job's output file extensions before using this for a new pipeline.

echo "=== download tarball + manifest ==="
gcloud compute scp "$VM:/tmp/retrieve.tar"    "$LOCAL_DIR/" --zone="$ZONE" || { echo "FAIL: scp tarball"; exit 1; }
gcloud compute scp "$VM:/tmp/retrieve.sha256" "$LOCAL_DIR/" --zone="$ZONE" || { echo "FAIL: scp manifest"; exit 1; }

cd "$LOCAL_DIR"
if [ ! -f retrieve.tar ] || ! tar tf retrieve.tar >/dev/null 2>&1; then
  echo "FAIL: tarball missing or corrupt after download -- VM left untouched."
  exit 1
fi
tar xf retrieve.tar

echo "=== verify sha256 for every file ==="
fail=0
while read -r expected name; do
  [ -z "${expected:-}" ] && continue
  actual=$(shasum -a 256 "$name" 2>/dev/null | awk '{print $1}')
  if [ "$actual" != "$expected" ]; then
    echo "  MISMATCH: $name"
    fail=$((fail + 1))
  fi
done < retrieve.sha256

expected_count=$(wc -l < retrieve.sha256 | tr -d ' ')
actual_count=$(($(wc -l < retrieve.sha256 | tr -d ' ')))

if [ "$fail" -ne 0 ]; then
  echo "VERIFICATION FAILED: $fail file(s) mismatched. Do NOT delete the VM."
  echo "Partial/possibly-corrupt files are in $LOCAL_DIR -- treat with suspicion."
  exit 1
fi

rm -f retrieve.tar retrieve.sha256
echo "VERIFIED: $expected_count/$expected_count files, all sha256 match."
echo "Safe to consider deletion IF the user explicitly asks for it -- a"
echo "successful verified download does not by itself authorize deleting the VM."
exit 0
