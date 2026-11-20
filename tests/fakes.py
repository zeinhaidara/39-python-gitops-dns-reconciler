"""Hand-written fake Route 53 client with paging and atomic change batches."""

from __future__ import annotations

import copy

from botocore.exceptions import ClientError


def rrset(name, rtype, ttl, *values, **extra):
    rr = {
        "Name": name,
        "Type": rtype,
        "TTL": ttl,
        "ResourceRecords": [{"Value": v} for v in values],
    }
    rr.update(extra)
    return rr


def zone_apex():
    return [
        rrset("example.com.", "NS", 172800, "ns-1.awsdns-00.com.", "ns-2.awsdns-00.net."),
        rrset(
            "example.com.",
            "SOA",
            900,
            "ns-1.awsdns-00.com. hostmaster.example.com. 1 7200 900 1209600 86400",
        ),
    ]


class FakeRoute53:
    def __init__(self, rrsets: list[dict], page_size: int = 2):
        self.rrsets = copy.deepcopy(rrsets)
        self.page_size = page_size
        self.list_calls: list[dict] = []
        self.batches: list[dict] = []

    def list_resource_record_sets(self, HostedZoneId, StartRecordName=None, StartRecordType=None):
        self.list_calls.append(
            {"StartRecordName": StartRecordName, "StartRecordType": StartRecordType}
        )
        start = 0
        if StartRecordName is not None:
            start = next(
                i
                for i, r in enumerate(self.rrsets)
                if (r["Name"], r["Type"]) == (StartRecordName, StartRecordType)
            )
        chunk = self.rrsets[start : start + self.page_size]
        page = {"ResourceRecordSets": copy.deepcopy(chunk), "IsTruncated": False}
        nxt = start + self.page_size
        if nxt < len(self.rrsets):
            page.update(
                IsTruncated=True,
                NextRecordName=self.rrsets[nxt]["Name"],
                NextRecordType=self.rrsets[nxt]["Type"],
            )
        return page

    def _find(self, rr):
        for i, r in enumerate(self.rrsets):
            if (r["Name"], r["Type"]) == (rr["Name"], rr["Type"]):
                return i
        return None

    def change_resource_record_sets(self, HostedZoneId, ChangeBatch):
        self.batches.append(copy.deepcopy(ChangeBatch))
        staged = copy.deepcopy(self.rrsets)
        backup, self.rrsets = self.rrsets, staged
        try:
            for change in ChangeBatch["Changes"]:
                rr, action, i = change["ResourceRecordSet"], change["Action"], None
                i = self._find(rr)
                if action == "CREATE":
                    if i is not None:
                        raise self._invalid(f"{rr['Name']} already exists")
                    self.rrsets.append(copy.deepcopy(rr))
                elif action == "DELETE":
                    if i is None or self._norm(self.rrsets[i]) != self._norm(rr):
                        raise self._invalid(f"{rr['Name']} delete does not match")
                    del self.rrsets[i]
                elif action == "UPSERT":
                    if i is None:
                        self.rrsets.append(copy.deepcopy(rr))
                    else:
                        self.rrsets[i] = copy.deepcopy(rr)
        except ClientError:
            self.rrsets = backup  # all or nothing, like Route 53
            raise
        return {"ChangeInfo": {"Id": f"/change/C{len(self.batches):04d}", "Status": "PENDING"}}

    @staticmethod
    def _norm(rr):
        return (
            rr["Name"],
            rr["Type"],
            rr["TTL"],
            sorted(v["Value"] for v in rr["ResourceRecords"]),
        )

    @staticmethod
    def _invalid(msg):
        return ClientError(
            {"Error": {"Code": "InvalidChangeBatch", "Message": msg}}, "ChangeResourceRecordSets"
        )
