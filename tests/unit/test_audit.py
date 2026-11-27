"""Audit trail: bounded, rotated, and free of payloads."""

from __future__ import annotations

import json
from pathlib import Path

from palette.audit import AuditLog


class TestScrubbing:
    def test_input_text_never_reaches_the_file(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record(
            "tool.act",
            vm_id="desktop",
            action_type="type_text",
            text_length=11,
            text="hunter2secret",
        )
        line = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "hunter2secret" not in line
        assert json.loads(line)["text_length"] == 11

    def test_host_paths_are_dropped(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record("vm.started", vm_id="desktop", path=r"C:\Users\someone\.ssh\id_rsa")
        assert "id_rsa" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")

    def test_credentials_are_dropped(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record("x", token="abc", secret="def", password="ghi", ok=True)
        payload = json.loads((tmp_path / "audit.jsonl").read_text(encoding="utf-8"))
        assert "token" not in payload
        assert "secret" not in payload
        assert "password" not in payload
        assert payload["ok"] is True


class TestBounding:
    def test_rotation_keeps_the_file_count_bounded(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, max_bytes=400, max_files=3)
        for index in range(200):
            log.record("event", index=index)
        generations = sorted(tmp_path.glob("audit*.jsonl"))
        assert len(generations) <= 3
        # The newest records must still be present: dropping old generations is fine,
        # losing current ones would not be.
        assert log.tail(1)[0]["index"] == 199

    def test_a_single_generation_setting_still_rotates(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, max_bytes=200, max_files=1)
        for index in range(50):
            log.record("event", index=index)
        assert log.tail(1)[0]["index"] == 49

    def test_corrupt_lines_are_skipped_rather_than_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path)
        log.record("good", value=1)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json}\n")
        log.record("good", value=2)
        assert [entry["value"] for entry in log.tail()] == [1, 2]


class TestFailureIsolation:
    def test_a_write_failure_never_propagates(self, tmp_path: Path) -> None:
        """Losing an audit line must not become an outage for the agent."""
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory", encoding="utf-8")
        log = AuditLog(blocker / "audit.jsonl")
        log.record("event", value=1)  # must not raise

    def test_disabled_audit_skips_the_write(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, enabled=False)
        log.record("event")
        assert not path.exists()


class TestRecordShape:
    def test_non_serialisable_values_do_not_break_the_writer(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record("event", when=object())
        assert log.tail(1)[0]["event"] == "event"


class TestAsyncWriter:
    def test_async_records_scrub_rotate_and_flush_on_tail_and_close(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, max_bytes=300, max_files=3)
        for index in range(60):
            log.record_async(
                "operator.input",
                vm_id="desktop",
                count=index,
                secret="never-written",
            )
        entries = log.tail(100)
        assert entries[-1]["count"] == 59
        assert all("secret" not in entry for entry in entries)
        generations = sorted(tmp_path.glob("audit*.jsonl"))
        assert len(generations) <= 3
        log.close()
        assert log._handle is None

    def test_disabled_async_write_does_not_spawn_worker(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, enabled=False)
        log.record_async("operator.input", count=1)
        log.flush()
        log.close()
        assert log._worker is None
        assert not path.exists()
