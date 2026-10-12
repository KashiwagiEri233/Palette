"""Audit trail: bounded, rotated, and free of payloads."""

from __future__ import annotations

import json
from pathlib import Path

from palette.audit import AuditLog, NullAuditLog


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

    def test_tail_of_a_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert AuditLog(tmp_path / "nothing.jsonl").tail() == []

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

    def test_null_audit_writes_nothing(self, tmp_path: Path) -> None:
        log = NullAuditLog()
        log.record("event", value=1)
        assert log.tail() == []
        assert log.enabled is False

    def test_disabled_audit_skips_the_write(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        log = AuditLog(path, enabled=False)
        log.record("event")
        assert not path.exists()


class TestRecordShape:
    def test_records_carry_a_timestamp_and_event_name(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record("vm.started", vm_id="desktop")
        entry = log.tail(1)[0]
        assert entry["event"] == "vm.started"
        assert entry["vm_id"] == "desktop"
        assert "T" in entry["ts"]

    def test_non_serialisable_values_do_not_break_the_writer(self, tmp_path: Path) -> None:
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record("event", when=object())
        assert log.tail(1)[0]["event"] == "event"
