#!/usr/bin/env python3
"""Resumable batch-job worker skeleton.

Copy this into the new project and fill in the TODO-marked spots with the real
per-unit fetch/compute logic. The scaffolding around it — atomic per-unit
caching, skip-if-cached resume, per-member concat, --workers parallelism — is
already correct and shouldn't need to change for most jobs.

Vocabulary used throughout:
  MEMBER  a top-level thing that gets its own final output file and its own
          worker process (e.g. one model, one device, one customer).
  UNIT    the smallest resumable piece of work within a member (e.g. one file,
          one year, one 5-item API batch). Losing one in-flight unit to a
          preemption should be cheap -- that's the granularity to aim for.
"""
import argparse
import glob
import logging
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("worker")

# TODO: replace with the real member list (models, devices, shard ids, ...).
DEFAULT_MEMBERS = ["member-a", "member-b", "member-c"]


def _retry(fn, what, attempts=5, base_delay=2.0):
    """Retry transient failures with exponential backoff.

    Does NOT retry a clearly-permanent failure (e.g. the item genuinely doesn't
    exist) -- re-raise those immediately so callers can skip/log instead of
    burning a minute of backoff on something that will never succeed.
    """
    for i in range(attempts):
        try:
            return fn()
        except FileNotFoundError:
            raise
        except Exception as e:
            if i == attempts - 1:
                raise
            delay = base_delay * (2 ** i)
            log.warning("retry %s (%s): %s -- sleeping %.1fs", i + 1, what, e, delay)
            time.sleep(delay)


def list_units(member: str, start: int, end: int) -> list:
    """Return the ordered list of unit ids to process for this member.

    TODO: replace `range(start, end + 1)` with real unit discovery if the job
    isn't simply "an integer range" (e.g. discover available files on a remote
    listing, or generate fixed-size batches for an API).
    """
    return list(range(start, end + 1))


def fetch_and_process_unit(member: str, unit, tmpdir: str):
    """Do the actual work for one unit and return an in-memory result.

    TODO: replace this with the real fetch (download/API call) + any per-unit
    processing. Keep it side-effect-free on the *cache* -- writing the cache
    file atomically is process_member's job, not this function's, so a
    half-finished fetch never produces a file that looks done.
    """
    _retry(lambda: time.sleep(0.01), what=f"fetch {member}/{unit}")  # placeholder work
    return {"member": member, "unit": unit, "value": unit}  # TODO: real payload


def write_unit_cache(result, cache_path: str):
    """Atomic write: never leave a partially-written file that looks finished.

    TODO: replace this placeholder serialization with the real one (e.g.
    `ds.to_netcdf(tmp, encoding=...)` for NetCDF, `df.to_parquet(tmp)`, etc.)
    """
    tmp = cache_path + ".tmp"
    with open(tmp, "w") as f:
        f.write(repr(result))
    os.replace(tmp, cache_path)  # atomic on the same filesystem


def concat_member_output(member: str, unit_cache_paths: list, out_path: str):
    """Combine every cached unit for a member into the final deliverable.

    TODO: replace with the real combine step (e.g. xr.concat + to_netcdf, or a
    pandas concat + to_csv). Write atomically here too, for the same reason.
    """
    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        for p in unit_cache_paths:
            with open(p) as unit_f:
                f.write(unit_f.read() + "\n")
    os.replace(tmp, out_path)


def process_member(member: str, start: int, end: int, outdir: str,
                    overwrite: bool, dry_run: bool) -> str:
    out_path = os.path.join(outdir, f"{member}_{start}_{end}.out")
    if os.path.exists(out_path) and not overwrite and not dry_run:
        return f"[{member}] exists, skipped ({out_path})"

    units = list_units(member, start, end)
    if not units:
        return f"[{member}] no units in range, skipped"
    if dry_run:
        return f"[{member}] {len(units)} units {units[0]}-{units[-1]}"

    cache_dir = os.path.join(outdir, "units", member)
    os.makedirs(cache_dir, exist_ok=True)

    def cache_path(unit) -> str:
        return os.path.join(cache_dir, f"{member}_{unit}.cache")

    # Clean up any half-written file from an interrupted previous run -- a
    # ".tmp" is never trusted as finished, so it's always safe to remove.
    for stale in glob.glob(os.path.join(cache_dir, "*.tmp")):
        try:
            os.remove(stale)
        except OSError:
            pass

    todo = [u for u in units if overwrite or not os.path.exists(cache_path(u))]
    cached = len(units) - len(todo)
    log.info("[%s] %d/%d units already cached; processing %d",
              member, cached, len(units), len(todo))

    with tempfile.TemporaryDirectory(prefix=f"work_{member}_") as tmp:
        for i, unit in enumerate(todo, 1):
            result = fetch_and_process_unit(member, unit, tmp)
            write_unit_cache(result, cache_path(unit))
            if i % 10 == 0 or i == len(todo):
                log.info("[%s]   processed %d/%d (unit=%s)", member, i, len(todo), unit)

    # Every unit is now cached (either just now or from a previous run) --
    # combine them into the final per-member output.
    unit_cache_paths = [cache_path(u) for u in units]
    concat_member_output(member, unit_cache_paths, out_path)
    return f"[{member}] wrote {out_path} ({len(units)} units)"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--members", default="all",
                    help="comma-separated member list, or 'all' for the default set")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--end", type=int, default=9)
    p.add_argument("--outdir", default="./out")
    p.add_argument("--workers", type=int, default=1,
                    help="parallelism across MEMBERS, not units")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--list-members", action="store_true")
    args = p.parse_args(argv)

    members = DEFAULT_MEMBERS if args.members == "all" else args.members.split(",")
    if args.list_members:
        for m in members:
            print(m)
        return 0

    os.makedirs(args.outdir, exist_ok=True)
    t0 = time.time()
    results = []
    if args.workers <= 1:
        for m in members:
            results.append(process_member(m, args.start, args.end, args.outdir,
                                           args.overwrite, args.dry_run))
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_member, m, args.start, args.end, args.outdir,
                               args.overwrite, args.dry_run): m for m in members}
            for fut in as_completed(futs):
                m = futs[fut]
                try:
                    results.append(fut.result())
                except Exception as e:
                    results.append(f"[{m}] FAILED: {e!r}")

    log.info("===== Summary =====")
    for r in results:
        log.info(r)
    log.info("done in %.1fs", time.time() - t0)

    # Signal completion for the self-stop watcher (Step 4 of the skill) --
    # only write this if nothing failed, so a partial run never looks done.
    if not args.dry_run and not any("FAILED" in r for r in results):
        open(os.path.join(args.outdir, "..", ".complete"), "w").close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
