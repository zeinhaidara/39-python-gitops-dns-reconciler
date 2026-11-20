"""Reconcile Route 53 with dns/records.yaml. Dry-run unless --apply."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from dnsrecon.plan import build_plan
from dnsrecon.providers import AzureDnsProvider, DnsProvider, Route53Provider
from dnsrecon.records import load_desired

EXIT_OK, EXIT_ERROR, EXIT_GUARD = 0, 1, 3


class GuardTripped(RuntimeError):
    pass


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="GitOps reconciler for Route 53 records.")
    p.add_argument("--records", type=Path, default=Path("dns/records.yaml"))
    p.add_argument("--provider", choices=["route53", "azure"], default="route53")
    p.add_argument("--apply", action="store_true", help="make changes (default is dry-run)")
    p.add_argument("--max-deletions", type=int, default=5)
    p.add_argument(
        "--allow-mass-delete",
        action="store_true",
        help="apply even if deletions exceed --max-deletions",
    )
    p.add_argument("--loop", action="store_true", help="keep reconciling every --interval")
    p.add_argument("--interval", type=int, default=300, help="seconds between loop runs")
    p.add_argument(
        "--git-pull",
        action="store_true",
        help="git pull --ff-only in the records repo before each run",
    )
    args = p.parse_args(argv)
    if args.max_deletions < 0 or args.interval < 10:
        p.error("--max-deletions must be >= 0 and --interval >= 10")
    return args


def git_pull(records: Path, run=subprocess.run) -> None:
    # check=True: a failed pull must stop us, not reconcile stale state.
    run(
        ["git", "-C", str(records.parent), "pull", "--ff-only"],
        check=True,
        capture_output=True,
        text=True,
    )


def reconcile_once(args, provider: DnsProvider, out=print, run=subprocess.run) -> int:
    if args.git_pull:
        git_pull(args.records, run)
    desired = load_desired(args.records)
    plan = build_plan(desired, provider.list_records(desired.zone_id))

    if not plan.changes:
        out(f"{desired.zone_name}: in sync")
        return 0
    for c in plan.changes:
        out(c.describe())
    out(f"{len(plan.changes)} change(s), {plan.deletions} deletion(s)")

    if plan.deletions > args.max_deletions and not args.allow_mass_delete:
        raise GuardTripped(
            f"plan deletes {plan.deletions} records, more than --max-deletions "
            f"{args.max_deletions}; re-run with --allow-mass-delete if intended"
        )
    if not args.apply:
        out("dry-run: nothing changed (use --apply)")
        return len(plan.changes)

    change_id = provider.apply(
        desired.zone_id, [c.to_api() for c in plan.changes], comment="gitops-dns reconcile"
    )
    out(f"applied as change {change_id}")
    return len(plan.changes)


def main(
    argv=None,
    provider: DnsProvider | None = None,
    sleep: Callable = time.sleep,
    max_runs: int | None = None,
    out=print,
    run=subprocess.run,
) -> int:
    args = parse_args(argv)
    runs = 0
    try:
        if provider is None:
            if args.provider == "azure":
                provider = AzureDnsProvider()
            else:
                import boto3

                provider = Route53Provider(boto3.client("route53"))
        while True:
            reconcile_once(args, provider, out=out, run=run)
            runs += 1
            if not args.loop or (max_runs is not None and runs >= max_runs):
                return EXIT_OK
            sleep(args.interval)
    except GuardTripped as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_GUARD
    except Exception as exc:
        # In loop mode we stop too; let systemd or Kubernetes restart us.
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
