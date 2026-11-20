"""Compare desired and live records and build a change plan."""

from __future__ import annotations

from dataclasses import dataclass

from dnsrecon.providers import to_record, to_rrset
from dnsrecon.records import NEVER_MANAGED, Desired, Record, fqdn


class PlanError(RuntimeError):
    pass


@dataclass(frozen=True)
class Change:
    action: str  # CREATE, UPSERT, DELETE
    record: Record
    old: Record | None = None

    def to_api(self) -> dict:
        return {"Action": self.action, "ResourceRecordSet": to_rrset(self.record)}

    def describe(self) -> str:
        r = self.record
        line = f"{self.action:<6} {r.name} {r.type} ttl={r.ttl} {list(r.values)}"
        if self.old is not None:
            line += f"  (was ttl={self.old.ttl} {list(self.old.values)})"
        return line


@dataclass(frozen=True)
class Plan:
    changes: list[Change]

    @property
    def deletions(self) -> int:
        return sum(1 for c in self.changes if c.action == "DELETE")


def managed_live(desired: Desired, rrsets: list[dict]) -> dict[tuple[str, str], Record]:
    """Live records we are allowed to touch. Everything else is ignored."""
    out = {}
    for rr in rrsets:
        name = fqdn(rr["Name"])
        if rr["Type"] in NEVER_MANAGED or not desired.in_scope(name):
            continue
        if "AliasTarget" in rr or "SetIdentifier" in rr:
            # Can't round-trip these safely, so refuse rather than guess.
            raise PlanError(f"{name} {rr['Type']} is an alias or routing-policy record in scope")
        rec = to_record(rr)
        out[rec.key] = rec
    return out


def build_plan(desired: Desired, rrsets: list[dict]) -> Plan:
    live = managed_live(desired, rrsets)
    changes: list[Change] = []
    for key in sorted(desired.records.keys() | live.keys()):
        want, have = desired.records.get(key), live.get(key)
        if want and not have:
            changes.append(Change("CREATE", want))
        elif have and not want:
            # DELETE must match the live record exactly, so send the live one.
            changes.append(Change("DELETE", have))
        elif want != have:
            changes.append(Change("UPSERT", want, old=have))
    return Plan(changes)
