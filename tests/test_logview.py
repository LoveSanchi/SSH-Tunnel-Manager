from tunnel_keeper import logview, paths

JOURNAL = """2026-10-29T10:00:00+0530 host tunnel-keeperd[1]: INFO orders: up — SELECT 1 ok
2026-10-29T10:01:00+0530 host tunnel-keeperd[1]: INFO other: up — SELECT 1 ok
2026-10-29T10:02:00+0530 host tunnel-keeperd[1]: INFO tunnel orders stopped
2026-10-29T10:03:00+0530 host tunnel-keeperd[1]: INFO tunnel orders-2 saved: x
"""


def test_service_events_filters_by_exact_name():
    calls = []
    events = logview.service_events("orders", lambda args: calls.append(args) or JOURNAL)
    assert [e.split(" INFO ")[1] for e in events] == ["orders: up — SELECT 1 ok", "tunnel orders stopped"]
    assert calls[0][:4] == ["journalctl", "--user", "-u", "tunnel-keeper"]


def test_tail_file(tmp_path):
    f = tmp_path / "a.log"
    assert logview.tail_file(f) is None
    f.write_text("line1\nline2\nline3\n")
    assert logview.tail_file(f) == "line1\nline2\nline3\n"
    assert logview.tail_file(f, max_bytes=8) == "line3\n"


def test_report_explains_missing_ssh_log():
    text = logview.report("orders", lambda args: JOURNAL)
    assert "orders: up" in text and "No ssh log yet" in text


def test_report_includes_ssh_log():
    log = paths.log_file("orders")
    log.parent.mkdir(parents=True)
    log.write_text("--- start\nssh: Connection refused\n")
    text = logview.report("orders", lambda args: "")
    assert "(none yet)" in text and "ssh: Connection refused" in text


def test_journal_runner_handles_missing_binary():
    assert "could not read the journal" in logview._journal(["/nonexistent/journalctl"])
