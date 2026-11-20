"""Desired state: load and validate the records file kept in Git."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

NEVER_MANAGED = {"NS", "SOA"}
SUPPORTED_TYPES = {"A", "AAAA", "CNAME", "TXT", "MX", "SRV", "CAA"}
MAX_TTL = 604800  # one week


class RecordsError(ValueError):
    pass


def fqdn(name: str) -> str:
    """Lowercase with a trailing dot. Route 53 escapes '*' as \\052."""
    name = name.strip().lower().replace("\\052", "*")
    return name if name.endswith(".") else name + "."


@dataclass(frozen=True)
class Record:
    name: str
    type: str
    ttl: int
    values: tuple[str, ...]  # sorted, so order in YAML doesn't matter

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.type)


@dataclass(frozen=True)
class Desired:
    zone_id: str
    zone_name: str
    scope: tuple[str, ...]
    records: dict[tuple[str, str], Record]

    def in_scope(self, name: str) -> bool:
        return any(name == s or name.endswith("." + s) for s in self.scope)


def load_desired(path: Path) -> Desired:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RecordsError(f"{path}: expected a mapping")
    try:
        zone_id = str(raw["zone_id"])
        zone_name = fqdn(raw["zone_name"])
        scope = tuple(fqdn(s) for s in raw["managed_scope"])
        entries = raw["records"] or []
    except KeyError as exc:
        raise RecordsError(f"{path}: missing key {exc}") from exc

    if not scope:
        raise RecordsError("managed_scope must list at least one name")
    for s in scope:
        if not (s == zone_name or s.endswith("." + zone_name)):
            raise RecordsError(f"scope {s} is not inside zone {zone_name}")

    desired = Desired(zone_id, zone_name, scope, {})
    for i, e in enumerate(entries):
        rec = _record(e, f"{path}: records[{i}]")
        if rec.type in NEVER_MANAGED:
            raise RecordsError(f"{rec.name} {rec.type}: NS and SOA are never managed")
        if not desired.in_scope(rec.name):
            raise RecordsError(f"{rec.name} is outside managed_scope {list(scope)}")
        if rec.key in desired.records:
            raise RecordsError(f"{rec.name} {rec.type} is listed twice")
        desired.records[rec.key] = rec
    return desired


def _record(e: dict, where: str) -> Record:
    try:
        name, rtype, ttl, values = e["name"], str(e["type"]).upper(), int(e["ttl"]), e["values"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordsError(f"{where}: needs name, type, ttl, values ({exc})") from exc
    if rtype not in SUPPORTED_TYPES | NEVER_MANAGED:
        raise RecordsError(f"{where}: unsupported type {rtype}")
    if not 0 < ttl <= MAX_TTL:
        raise RecordsError(f"{where}: ttl must be 1..{MAX_TTL}")
    if not isinstance(values, list) or not values:
        raise RecordsError(f"{where}: values must be a non-empty list")
    if rtype == "CNAME" and len(values) != 1:
        raise RecordsError(f"{where}: CNAME takes exactly one value")
    return Record(fqdn(name), rtype, ttl, tuple(sorted(str(v) for v in values)))
