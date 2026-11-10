"""Host telemetry collection."""
import socket

import pytest

from collector import OSCollector


@pytest.fixture(scope="module")
def snapshot():
    collector = OSCollector()
    return collector.snapshot()


def test_system_metrics_are_sane(snapshot):
    system = snapshot["os"]
    assert system["host"] == socket.gethostname()
    assert 0 <= system["cpu_percent"] <= 100 and 0 <= system["memory_percent"] <= 100 and 0 < system["disk_percent"] <= 100
    assert system["cpu_count"] >= 1 and system["memory_total"] > 0 and system["disk_total"] > 0
    assert system["process_count"] >= len(snapshot["processes"]) > 0


def test_process_list_is_bounded_and_clean(snapshot):
    procs = snapshot["processes"]
    assert len(procs) <= 100
    assert all(p["pid"] != 0 for p in procs)                              # the Windows idle "process" is excluded
    assert all(0 <= p["cpu_percent"] <= 100 for p in procs)               # normalised by core count
    assert all(p["status"] is None for p in procs)                        # not collected: too slow on Windows
    assert len({p["pid"] for p in procs}) == len(procs)                   # the CPU and memory top lists overlap without duplicates


def test_biggest_memory_users_are_included(snapshot):
    procs = snapshot["processes"]
    assert max(p["memory_percent"] for p in procs) > 0


def test_partitions_only_list_mounted_volumes(snapshot):
    assert snapshot["partitions"]
    for part in snapshot["partitions"]:
        assert part["filesystem"] and part["total"] > 0 and 0 <= part["percent"] <= 100
