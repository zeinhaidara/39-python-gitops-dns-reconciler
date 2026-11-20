"""DNS providers. Route 53 is implemented; Azure DNS is a stub."""

from __future__ import annotations

from typing import Protocol

from dnsrecon.records import Record, fqdn

MAX_CHANGES_PER_BATCH = 1000  # Route 53 limit; re-verify if you hit it


class ProviderError(RuntimeError):
    pass


class DnsProvider(Protocol):
    def list_records(self, zone_id: str) -> list[dict]: ...
    def apply(self, zone_id: str, changes: list[dict], comment: str) -> str: ...


class Route53Provider:
    def __init__(self, client):
        self.client = client

    def list_records(self, zone_id: str) -> list[dict]:
        """Every record set in the zone, raw. Follows NextRecordName/Type/Identifier."""
        kwargs: dict = {"HostedZoneId": zone_id}
        out: list[dict] = []
        while True:
            page = self.client.list_resource_record_sets(**kwargs)
            out.extend(page.get("ResourceRecordSets", []))
            if not page.get("IsTruncated"):
                return out
            kwargs["StartRecordName"] = page["NextRecordName"]
            kwargs["StartRecordType"] = page["NextRecordType"]
            if "NextRecordIdentifier" in page:
                kwargs["StartRecordIdentifier"] = page["NextRecordIdentifier"]
            else:
                kwargs.pop("StartRecordIdentifier", None)

    def apply(self, zone_id: str, changes: list[dict], comment: str) -> str:
        if len(changes) > MAX_CHANGES_PER_BATCH:
            # One batch keeps the change atomic; splitting it would not be.
            raise ProviderError(
                f"{len(changes)} changes exceed one batch of {MAX_CHANGES_PER_BATCH}; "
                "split the change"
            )
        resp = self.client.change_resource_record_sets(
            HostedZoneId=zone_id, ChangeBatch={"Comment": comment, "Changes": changes}
        )
        return resp["ChangeInfo"]["Id"]


class AzureDnsProvider:
    """Interface placeholder only."""

    def list_records(self, zone_id: str) -> list[dict]:
        raise NotImplementedError("Azure DNS is not implemented")

    def apply(self, zone_id: str, changes: list[dict], comment: str) -> str:
        raise NotImplementedError("Azure DNS is not implemented")


def to_record(rrset: dict) -> Record:
    return Record(
        name=fqdn(rrset["Name"]),
        type=rrset["Type"],
        ttl=int(rrset["TTL"]),
        values=tuple(sorted(r["Value"] for r in rrset["ResourceRecords"])),
    )


def to_rrset(rec: Record) -> dict:
    return {
        "Name": rec.name,
        "Type": rec.type,
        "TTL": rec.ttl,
        "ResourceRecords": [{"Value": v} for v in rec.values],
    }
