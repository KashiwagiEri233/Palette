"""Acknowledged real-time batches preserve the final state of repeated inputs."""

import pytest

from palette.input.events import button_event, key_event
from palette.input.ledger import InputLedger


@pytest.mark.parametrize("down", [(False, True), (True, False, True), (True, False)])
@pytest.mark.parametrize("kind", ["key", "button"])
def test_acknowledged_batch_tracks_last_transition(down, kind):
    ledger = InputLedger()
    event = key_event if kind == "key" else button_event
    name = "a" if kind == "key" else "left"
    events = [event(name, value) for value in down]
    ledger.note_presses(events)
    # Before ACK, retain every possibly delivered press for emergency cleanup.
    assert not ledger.is_clean
    ledger.note_releases(events)
    snapshot = ledger.snapshot()
    assert snapshot["keys" if kind == "key" else "buttons"] == ([name] if down[-1] else [])
