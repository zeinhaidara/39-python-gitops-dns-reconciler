import subprocess
from pathlib import Path

from dnsrecon.cli import main
from dnsrecon.providers import Route53Provider
from tests.fakes import FakeRoute53, rrset, zone_apex

RECORDS = str(Path(__file__).parent.parent / "dns" / "records.yaml")


def drifted():
    return zone_apex() + [
        rrset("app.example.com.", "A", 300, "192.0.2.10"),
        rrset("old.app.example.com.", "A", 300, "192.0.2.99"),
        rrset("mail.example.com.", "MX", 300, "10 mx.example.com."),
    ]


def run(args, fake, **kw):
    lines = []
    code = main(
        ["--records", RECORDS, *args], provider=Route53Provider(fake), out=lines.append, **kw
    )
    return code, lines


def test_dry_run_is_default_and_changes_nothing():
    fake = FakeRoute53(drifted())
    before = list(fake.rrsets)
    code, lines = run([], fake)
    assert code == 0
    assert fake.batches == []
    assert fake.rrsets == before
    assert lines[-1] == "dry-run: nothing changed (use --apply)"


def test_apply_sends_one_batch_then_converges():
    fake = FakeRoute53(drifted())
    code, lines = run(["--apply"], fake)
    assert code == 0
    assert len(fake.batches) == 1
    actions = sorted(c["Action"] for c in fake.batches[0]["Changes"])
    assert actions == ["CREATE", "CREATE", "DELETE", "UPSERT"]
    # Second run: nothing left to do, and NS/SOA/MX are untouched.
    code, lines = run(["--apply"], fake)
    assert code == 0
    assert lines == ["example.com.: in sync"]
    assert len(fake.batches) == 1
    names = {(r["Name"], r["Type"]) for r in fake.rrsets}
    assert {("example.com.", "NS"), ("example.com.", "SOA"), ("mail.example.com.", "MX")} <= names
    assert ("old.app.example.com.", "A") not in names


def mass_delete():
    return zone_apex() + [rrset(f"h{i}.app.example.com.", "A", 60, "192.0.2.1") for i in range(3)]


def test_deletion_guard_blocks_apply(capsys):
    fake = FakeRoute53(mass_delete())
    code, _ = run(["--apply", "--max-deletions", "2"], fake)
    assert code == 3
    assert fake.batches == []
    assert "--allow-mass-delete" in capsys.readouterr().err


def test_deletion_guard_can_be_overridden():
    fake = FakeRoute53(mass_delete())
    code, _ = run(["--apply", "--max-deletions", "2", "--allow-mass-delete"], fake)
    assert code == 0
    assert sum(c["Action"] == "DELETE" for c in fake.batches[0]["Changes"]) == 3


def test_deletions_at_the_limit_are_allowed():
    fake = FakeRoute53(mass_delete())
    assert run(["--apply", "--max-deletions", "3"], fake)[0] == 0


def test_loop_runs_and_sleeps_between_runs():
    fake, sleeps = FakeRoute53(drifted()), []
    code, lines = run(
        ["--apply", "--loop", "--interval", "60"], fake, sleep=sleeps.append, max_runs=3
    )
    assert code == 0
    assert sleeps == [60, 60]
    assert len(fake.batches) == 1  # later runs found nothing to do
    assert lines.count("example.com.: in sync") == 2


def test_loop_stops_on_error(capsys):
    class Broken(FakeRoute53):
        def list_resource_record_sets(self, **kw):
            raise RuntimeError("throttled")

    code, _ = run(["--loop"], Broken([]), sleep=lambda s: None, max_runs=5)
    assert code == 1
    assert "throttled" in capsys.readouterr().err


def test_git_pull_runs_before_each_reconcile():
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["check"]))
        return subprocess.CompletedProcess(cmd, 0)

    run(["--git-pull"], FakeRoute53(drifted()), run=fake_run)
    assert calls[0][0][-2:] == ["pull", "--ff-only"]
    assert calls[0][1] is True


def test_git_pull_failure_stops_the_run(capsys):
    def failing(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="not a fast-forward")

    fake = FakeRoute53(drifted())
    code, _ = run(["--git-pull", "--apply"], fake, run=failing)
    assert code == 1
    assert fake.batches == []
    assert "CalledProcessError" in capsys.readouterr().err


def test_azure_provider_flag_is_not_implemented(capsys):
    assert main(["--records", RECORDS, "--provider", "azure"]) == 1
    assert "Azure DNS is not implemented" in capsys.readouterr().err
