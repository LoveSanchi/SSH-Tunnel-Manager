import dataclasses

import pytest

from tests.fakes import World
from tunnel_keeper import supervisor as sv
from tunnel_keeper.config import Config
from tunnel_keeper.supervisor import Supervisor


@pytest.fixture
def world():
    return World()


@pytest.fixture
def sup(world, tunnel):
    world._current_port = tunnel.local_port
    return Supervisor(Config(interval_seconds=120).with_tunnel(tunnel), world.deps())


def test_initial_snapshot_before_any_check(sup):
    (status,) = sup.snapshot()
    assert status.state == "down" and status.detail == "not checked yet"


def test_opens_tunnel_when_port_closed(sup, world):
    status = sup.heal("orders")
    assert status.state == "up" and status.pid == world.launched[0].pid
    assert not status.external


def test_healthy_owned_tunnel_is_left_alone(sup, world):
    sup.heal("orders")
    status = sup.heal("orders")
    assert status.state == "up" and len(world.launched) == 1


def test_half_dead_owned_tunnel_is_restarted(sup, world):
    sup.heal("orders")
    world.healthy = False
    status = sup.heal("orders", force=True)
    assert world.terminated == [world.launched[0].pid]
    assert len(world.launched) == 2
    assert status.state == "error" and "check failed" in status.detail
    assert status.pid == world.launched[1].pid  # ssh kept running: the far side is the problem


def test_restart_reason_is_reported(sup, world):
    sup.heal("orders")
    world.healthy = False
    sup.heal("orders", force=True)
    world.healthy = True
    world.launched[-1].die()
    status = sup.heal("orders", force=True)
    assert status.state == "up" and "ssh exited" in status.detail


def test_adopts_healthy_external_tunnel(sup, world, tunnel):
    world.external[tunnel.forward_spec] = [4242]
    world.open_ports.add(tunnel.local_port)
    status = sup.heal("orders")
    assert status.state == "up" and status.external and status.pid == 4242
    assert world.launched == []


def test_replaces_unhealthy_external_tunnel(sup, world, tunnel):
    world.external[tunnel.forward_spec] = [4242]
    world.open_ports.add(tunnel.local_port)
    world.healthy = False
    sup.heal("orders")
    assert world.killed == [4242]
    assert len(world.launched) == 1


def test_port_held_by_someone_else_is_a_conflict(sup, world, tunnel):
    world.open_ports.add(tunnel.local_port)  # e.g. a docker container on 3307
    status = sup.heal("orders")
    assert status.state == "conflict" and "another program" in status.detail
    assert world.launched == []


def test_ssh_that_exits_backs_off(sup, world):
    world.launch_exits = True
    first = sup.heal("orders")
    assert first.state == "error" and "Connection refused" in first.detail and first.failures == 1

    second = sup.heal("orders")  # inside the 15s backoff window: no new launch
    assert second.state == "down" and "waiting to retry" in second.detail
    assert second.next_retry_at == world.now + 15  # a field, not text: the menu stays stable
    assert len(world.launched) == 1

    world.now += 16
    third = sup.heal("orders")
    assert third.failures == 2 and len(world.launched) == 2

    forced = sup.heal("orders", force=True)  # refresh ignores backoff
    assert len(world.launched) == 3 and forced.failures == 3


def test_backoff_is_capped_at_interval(sup, world):
    world.launch_exits = True
    for _ in range(8):
        sup.heal("orders", force=True)
    rt = sup._rt("orders")
    assert rt.next_attempt_at - world.now <= 120


def test_port_that_never_opens(sup, world):
    world.launch_opens_port = False
    status = sup.heal("orders")
    assert status.state == "error" and "port never opened" in status.detail
    assert world.launched[0].poll() is not None


def test_success_resets_failures(sup, world):
    world.launch_exits = True
    sup.heal("orders", force=True)
    world.launch_exits = False
    assert sup.heal("orders", force=True).state == "up"
    assert sup.heal("orders").failures == 0


def test_watch_processes_reacts_to_exit(sup, world):
    sup.heal("orders")
    world.launched[0].die()
    sup.watch_processes()
    assert len(world.launched) == 2
    assert sup.snapshot()[0].state == "up"


def test_stop_kills_owned_and_external(sup, world, tunnel):
    sup.heal("orders")
    world.external[tunnel.forward_spec] = [777]
    status = sup.stop("orders")
    assert status.state == "stopped"
    assert world.terminated == [world.launched[0].pid] and world.killed == [777]


def test_disabled_tunnel_is_stopped(world, tunnel):
    world._current_port = tunnel.local_port
    sup = Supervisor(Config().with_tunnel(dataclasses.replace(tunnel, enabled=False)), world.deps())
    assert sup.snapshot()[0].state == "stopped"
    assert sup.heal_all()[0].state == "stopped"
    assert world.launched == []


def test_apply_config_stops_removed_and_changed(sup, world, tunnel):
    sup.heal("orders")
    sup.apply_config(Config())
    assert world.terminated == [world.launched[0].pid]
    assert sup.snapshot() == ()

    sup.apply_config(Config().with_tunnel(tunnel))
    sup.heal("orders")
    sup.apply_config(Config().with_tunnel(dataclasses.replace(tunnel, remote_port=3307)))
    assert world.launched[1].poll() is not None


def test_unknown_tunnel_raises(sup):
    with pytest.raises(KeyError):
        sup.heal("ghost")


def test_shutdown_terminates_everything(sup, world):
    sup.heal("orders")
    sup.shutdown()
    assert world.launched[0].poll() is not None


def test_status_dict_roundtrip():
    status = sv.TunnelStatus("a", "up", "ok", pid=1)
    assert status.to_dict()["pid"] == 1


def test_one_failed_probe_does_not_kill_a_healthy_tunnel(sup, world):
    sup.heal("orders")
    results = iter([False, True])
    real = world.deps().probe
    sup._deps = dataclasses.replace(
        sup._deps, probe=lambda t: real(t) if next(results, True) else sv.health.Probe(False, "slow"))
    assert sup.heal("orders").state == "up"
    assert world.terminated == []


def test_mysql_error_from_server_never_restarts(sup, world):
    sup.heal("orders")
    sup._deps = dataclasses.replace(
        sup._deps, probe=lambda t: sv.health.Probe(False, "MySQL answered with an error: Access denied", True))
    status = sup.heal("orders", force=True)
    assert status.state == "error" and "Access denied" in status.detail
    assert world.terminated == [] and len(world.launched) == 1


def test_external_tunnel_with_server_error_is_not_killed(sup, world, tunnel):
    world.external[tunnel.forward_spec] = [4242]
    world.open_ports.add(tunnel.local_port)
    sup._deps = dataclasses.replace(
        sup._deps, probe=lambda t: sv.health.Probe(False, "MySQL answered with an error: too many", True))
    status = sup.heal("orders")
    assert status.state == "error" and status.external and world.killed == []


def test_unexpected_error_marks_only_that_tunnel(sup, world):
    def boom(cmd, log_path):
        raise FileNotFoundError("sshpass")
    sup._deps = dataclasses.replace(sup._deps, launch=boom)
    (status,) = sup.heal_all()
    assert status.state == "error" and "internal error" in status.detail and status.failures == 1


def test_heal_of_tunnel_removed_meanwhile(sup):
    sup.apply_config(Config())
    assert sup._heal_safely("orders", force=False).detail == "removed"


def test_slow_tunnel_does_not_block_another(world, tunnel):
    """Per-tunnel locks: a status/heal for B proceeds while A is mid-probe."""
    import threading
    other = dataclasses.replace(tunnel, name="b", local_port=3400)
    gate, entered = threading.Event(), threading.Event()
    real = world.deps().probe

    def probe(t):
        if t.name == "orders":
            entered.set()
            gate.wait(5)
        return real(t)

    world._current_port = tunnel.local_port
    world.open_ports.update({3390, 3400})
    world.external = {tunnel.forward_spec: [1], other.forward_spec: [2]}
    sup = Supervisor(Config().with_tunnel(tunnel).with_tunnel(other),
                     dataclasses.replace(world.deps(), probe=probe))
    slow = threading.Thread(target=sup.heal, args=("orders",))
    slow.start()
    assert entered.wait(5)
    assert sup.heal("b").state == "up"  # would deadlock-wait on one global lock
    gate.set()
    slow.join(5)
