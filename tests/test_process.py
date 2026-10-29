import os
import subprocess
import sys
import time

from tunnel_keeper import process
from tunnel_keeper.sshcmd import Command


def test_launch_logs_and_terminates_whole_group(tmp_path):
    log = tmp_path / "logs" / "t.log"
    # parent spawns a child, mimicking sshpass -> ssh
    script = "import subprocess,time,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print('hi', flush=True); time.sleep(60)"
    handle = process.launch(Command(argv=(sys.executable, "-c", script), env={"X": "1"}), log)
    deadline = time.time() + 5
    while "\nhi\n" not in log.read_text() and time.time() < deadline:
        time.sleep(0.05)
    assert handle.poll() is None
    pgid = os.getpgid(handle.pid)
    handle.terminate()
    assert handle.poll() is not None
    time.sleep(0.2)
    out = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True).stdout
    assert out.strip() == ""
    assert process.last_log_line(log) == "hi"
    handle.terminate()  # second call is a no-op


def test_last_log_line_skips_markers_and_missing(tmp_path):
    log = tmp_path / "x.log"
    assert process.last_log_line(log) == ""
    log.write_text("--- start\nssh: connect to host x port 22: Connection refused\n\n")
    assert process.last_log_line(log) == "ssh: connect to host x port 22: Connection refused"
    log.write_text("--- run 1\n#### old banner ####\n--- run 2\n\n")
    assert process.last_log_line(log) == ""  # never report a previous run's output


def test_rotate_large_log(tmp_path, monkeypatch):
    monkeypatch.setattr(process, "MAX_LOG_BYTES", 10)
    log = tmp_path / "x.log"
    log.write_text("x" * 50)
    process._rotate(log)
    assert not log.exists() and (tmp_path / "x.log.1").exists()


def test_kill_pids(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    process.kill_pids([proc.pid, 999999])
    assert proc.wait(timeout=5) is not None
