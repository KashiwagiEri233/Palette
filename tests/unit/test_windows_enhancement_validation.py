"""Reject invalid Windows guest requests before they can reach a guest transport."""

import pytest

from palette.agent_enhancements import validate_parameters
from palette.errors import ErrorCode, PaletteError


@pytest.mark.parametrize(
    ("operation", "parameters"),
    [
        ("powershell", {"command": "whoami", "timeout_seconds": 61}),
        ("powershell", {"command": "whoami", "cwd": "bad\0path"}),
        ("filesystem", {"mode": "write", "path": "file"}),
        ("filesystem", {"mode": "write", "path": "file", "content": "中" * 20000}),
        ("filesystem", {"mode": "copy", "path": "file"}),
        ("process", {"mode": "kill"}),
        ("process", {"mode": "kill", "pid": 3, "name": "editor"}),
        (
            "registry",
            {"mode": "set", "path": "HKCU:\\Test", "name": "v", "type": "DWord", "value": -1},
        ),
        (
            "registry",
            {"mode": "set", "path": "HKCU:\\Test", "name": "v", "type": "Binary", "value": [256]},
        ),
        ("app", {"mode": "launch_executable", "executable": "C:\\App.exe", "name": "App"}),
        ("app", {"mode": "switch", "args": ["bad"]}),
        ("multi_select", {"locs": [[1, 2]], "labels": [1], "snapshot_id": "s"}),
        ("multi_select", {"labels": [1]}),
        ("multi_select", {"locs": [[True, 2]]}),
        ("multi_edit", {"locs": [[1, 2, "text"], [3, 4, 123]]}),
        ("multi_edit", {"locs": [[1, 2, "中" * 20000]]}),
    ],
)
def test_invalid_arguments_are_rejected(operation, parameters):
    with pytest.raises(PaletteError) as caught:
        validate_parameters(operation, parameters)
    assert caught.value.code == ErrorCode.INVALID_ARGUMENT


def test_typed_registry_and_empty_field_text_are_preserved():
    registry = validate_parameters(
        "registry",
        {
            "mode": "set",
            "path": "HKCU:\\Software\\PaletteTest",
            "name": "",
            "type": "QWord",
            "value": 2**64 - 1,
        },
    )
    assert registry.model_dump()["value"] == 2**64 - 1
    batch = validate_parameters("multi_edit", {"labels": [[0, ""]], "snapshot_id": "snapshot"})
    assert batch.model_dump()["clear"] is True
