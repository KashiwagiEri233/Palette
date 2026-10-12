"""The QMP protocol layer: handshake, correlation, events, framing and failures."""

from __future__ import annotations

import asyncio
import json

import pytest

from palette.errors import ErrorCode, PaletteError, QmpCommandError, QmpDisconnectedError
from palette.qmp.client import ALLOWED_COMMANDS, QmpClient, qapi_declares_argument
from support.fake_qmp_server import GREETING, FakeQmpServer, connected_client


class TestHandshake:
    async def test_the_greeting_is_read_then_capabilities_are_negotiated(self) -> None:
        server = FakeQmpServer()
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            greeting = await client.start()
            assert greeting["QMP"]["version"]["qemu"]["major"] == 11
            assert client.connected is True
            assert [message["execute"] for message in server.received] == ["qmp_capabilities"]
            await client.close()

    async def test_a_silent_peer_fails_the_handshake_instead_of_hanging(self) -> None:
        server = FakeQmpServer(greeting=False)
        async with connected_client(server) as transport:
            client = QmpClient(transport, handshake_timeout=0.2)
            with pytest.raises(PaletteError) as info:
                await client.start()
            assert info.value.code is ErrorCode.QMP_UNAVAILABLE
            await client.close()

    async def test_a_peer_that_closes_immediately_is_reported_cleanly(self) -> None:
        server = FakeQmpServer(greeting=False, disconnect_after=0)
        async with connected_client(server) as transport:
            client = QmpClient(transport, handshake_timeout=0.5)
            with pytest.raises(PaletteError):
                await client.start()
            await client.close()


class TestCorrelation:
    async def test_responses_are_matched_by_id(self) -> None:
        server = FakeQmpServer()
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            result = await client.execute("query-status")
            assert result["status"] == "running"
            await client.close()

    async def test_out_of_order_responses_are_still_matched(self) -> None:
        """Two commands in flight, answered in reverse: the ids, not the arrival order,
        decide which caller gets which result."""
        server = FakeQmpServer(slow_commands={"query-version"})
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            first, second = await asyncio.gather(
                client.execute("query-version"),
                client.execute("query-status"),
            )
            assert first["package"] == "v11.1.2"
            assert second["status"] == "running"
            await client.close()

    async def test_events_interleaved_with_responses_do_not_disturb_them(self) -> None:
        server = FakeQmpServer(
            events=[
                {"event": "RESUME", "timestamp": {"seconds": 0, "microseconds": 0}},
                {"event": "STOP", "timestamp": {"seconds": 0, "microseconds": 0}},
            ],
            event_after_commands=2,
        )
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            seen: list[str] = []
            client.on_event(lambda name, _data: seen.append(name))
            await client.start()
            result = await client.execute("query-status")
            for _ in range(50):
                if len(seen) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert seen == ["RESUME", "STOP"]
            assert result["running"] is True
            await client.close()

    async def test_a_frame_split_across_reads_is_reassembled(self) -> None:
        server = FakeQmpServer(split_writes=True)
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            assert (await client.execute("query-status"))["status"] == "running"
            await client.close()

    async def test_a_response_for_an_unknown_id_is_ignored(self) -> None:
        server = FakeQmpServer()
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            # Push a stray response with an id nobody is waiting for, then a real command.
            transport.writer.write(b'{"return": {"stray": true}, "id": "not-a-real-id"}\n')
            await transport.writer.drain()
            assert (await client.execute("query-status"))["status"] == "running"
            await client.close()

    async def test_a_very_large_single_line_is_read(self) -> None:
        """query-qmp-schema is one enormous line; a too-small reader buffer would break it."""
        server = FakeQmpServer(schema_bytes=300_000)
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            schema = await client.execute("query-qmp-schema")
            assert len(json.dumps(schema)) > 300_000
            await client.close()

    async def test_an_oversized_frame_is_a_clean_disconnect(self) -> None:
        server = FakeQmpServer(schema_bytes=200_000, limit=64 * 1024)
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            with pytest.raises(PaletteError) as info:
                await client.execute("query-qmp-schema", timeout=2.0)
            assert info.value.code is ErrorCode.QMP_UNAVAILABLE
            await client.close()


class TestFailures:
    async def test_a_command_error_carries_the_qmp_class(self) -> None:
        server = FakeQmpServer(
            error_commands={"screendump": ("GenericError", "could not open file")}
        )
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            with pytest.raises(QmpCommandError) as info:
                await client.execute("screendump", {"filename": "x"})
            assert info.value.qmp_class == "GenericError"
            assert "could not open file" in info.value.description
            assert info.value.details["command"] == "screendump"
            await client.close()

    async def test_a_timeout_does_not_retry_the_command(self) -> None:
        server = FakeQmpServer(response_delay=0.3)
        async with connected_client(server) as transport:
            client = QmpClient(transport, command_timeout=0.1)
            await client.start()
            before = len(server.received)
            with pytest.raises(PaletteError) as info:
                await client.execute("screendump", {"filename": "x"})
            assert info.value.code is ErrorCode.TIMEOUT
            assert info.value.retryable is True
            await asyncio.sleep(0.35)
            after = len(server.received)
            # Exactly one command was issued: a timeout is never silently retried.
            assert after == before + 1
            await client.close()

    async def test_a_late_response_after_a_timeout_is_discarded(self) -> None:
        server = FakeQmpServer(response_delay=0.25)
        async with connected_client(server) as transport:
            client = QmpClient(transport, command_timeout=0.05)
            await client.start()
            with pytest.raises(PaletteError):
                await client.execute("query-status")
            await asyncio.sleep(0.3)
            # The client is still usable afterwards.
            client._command_timeout = 2.0
            assert (await client.execute("query-status"))["status"] == "running"
            await client.close()

    async def test_eof_fails_every_pending_command(self) -> None:
        server = FakeQmpServer(disconnect_after=1)
        async with connected_client(server) as transport:
            client = QmpClient(transport, command_timeout=2.0)
            await client.start()
            with pytest.raises(QmpDisconnectedError) as info:
                await client.execute("query-status")
            assert info.value.code is ErrorCode.QMP_UNAVAILABLE
            assert client.connected is False
            await client.close()

    async def test_a_disconnect_notifies_listeners(self) -> None:
        server = FakeQmpServer(disconnect_after=1)
        async with connected_client(server) as transport:
            client = QmpClient(transport, command_timeout=2.0)
            failures: list[object] = []
            client.on_disconnect(lambda failure: failures.append(failure))
            await client.start()
            with pytest.raises(PaletteError):
                await client.execute("query-status")
            for _ in range(50):
                if failures:
                    break
                await asyncio.sleep(0.01)
            assert len(failures) == 1
            await client.close()

    async def test_commands_after_a_disconnect_are_refused_immediately(self) -> None:
        server = FakeQmpServer(disconnect_after=1)
        async with connected_client(server) as transport:
            client = QmpClient(transport, command_timeout=0.5)
            await client.start()
            with pytest.raises(PaletteError):
                await client.execute("query-status")
            with pytest.raises(QmpDisconnectedError):
                await client.execute("query-status")
            await client.close()

    async def test_an_event_handler_that_raises_does_not_break_the_reader(self) -> None:
        server = FakeQmpServer(
            events=[{"event": "STOP", "timestamp": {"seconds": 0, "microseconds": 0}}],
            event_after_commands=2,
        )
        async with connected_client(server) as transport:
            client = QmpClient(transport)

            def explode(_name: str, _data: dict) -> None:
                raise RuntimeError("handler bug")

            client.on_event(explode)
            await client.start()
            assert (await client.execute("query-status"))["status"] == "running"
            await asyncio.sleep(0.05)
            assert (await client.execute("query-status"))["status"] == "running"
            await client.close()


class TestAllowList:
    async def test_a_command_outside_the_reviewed_list_is_refused_locally(self) -> None:
        """There is no generic passthrough: adding one requires editing a reviewed list,
        not just adding a call site."""
        server = FakeQmpServer()
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            with pytest.raises(PaletteError) as info:
                await client.execute("human-monitor-command", {"command-line": "info status"})
            assert info.value.code is ErrorCode.POLICY_DENIED
            # Nothing was sent to QEMU at all.
            assert all(
                message.get("execute") != "human-monitor-command" for message in server.received
            )
            await client.close()

    def test_the_allow_list_is_explicit(self) -> None:
        assert "input-send-event" in ALLOWED_COMMANDS
        assert "screendump" in ALLOWED_COMMANDS
        assert "system_powerdown" in ALLOWED_COMMANDS
        assert "human-monitor-command" not in ALLOWED_COMMANDS
        assert "migrate" not in ALLOWED_COMMANDS
        assert "device_add" not in ALLOWED_COMMANDS


class TestProbe:
    async def test_the_probe_reports_which_features_the_build_has(self) -> None:
        server = FakeQmpServer()
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            report = await client.probe()
            assert report["supports_screendump"] is True
            assert report["supports_input_send_event"] is True
            assert report["supports_png_screendump"] is True
            assert client.version is not None
            await client.close()

    async def test_an_older_build_without_png_is_detected_from_the_schema(self) -> None:
        """The schema is where the answer comes from — and its shape has to be the real one.

        The double emits a command whose ``arg-type`` is a type *name* pointing at a separate
        ``object`` entry, exactly as QEMU 11.1 does. An earlier version of this double inlined
        the members under ``arg-type``, which made the probe look correct while it raised
        ``AttributeError`` against a real QEMU.
        """
        server = FakeQmpServer(schema_advertises_png=False)
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            assert (await client.probe())["supports_png_screendump"] is False
            await client.close()

    async def test_png_support_is_detected_in_the_real_schema_shape(self) -> None:
        server = FakeQmpServer(schema_advertises_png=True)
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            assert (await client.probe())["supports_png_screendump"] is True
            await client.close()

    async def test_the_greeting_is_retained_for_diagnostics(self) -> None:
        server = FakeQmpServer()
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            assert client.greeting == GREETING
            await client.close()


class TestEventHistory:
    async def test_recent_events_are_kept_for_diagnosis(self) -> None:
        server = FakeQmpServer(
            events=[
                {"event": "RESET", "timestamp": {"seconds": 0, "microseconds": 0}},
                {"event": "RESUME", "timestamp": {"seconds": 0, "microseconds": 0}},
            ],
            event_after_commands=2,
        )
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            await client.execute("query-status")
            for _ in range(50):
                if len(client.history) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert [entry[1] for entry in client.history] == ["RESET", "RESUME"]
            await client.close()


class TestFramingDetail:
    async def test_non_json_lines_are_discarded_without_killing_the_session(self) -> None:
        server = FakeQmpServer()
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            transport.writer.write(b"this is not json\n")
            await transport.writer.drain()
            assert (await client.execute("query-status"))["status"] == "running"
            await client.close()

    async def test_a_json_array_is_not_a_valid_message(self) -> None:
        server = FakeQmpServer()
        async with connected_client(server) as transport:
            client = QmpClient(transport)
            await client.start()
            transport.writer.write(json.dumps([1, 2, 3]).encode() + b"\n")
            await transport.writer.drain()
            assert (await client.execute("query-status"))["status"] == "running"
            await client.close()


#: Verbatim from ``query-qmp-schema`` on QEMU 11.1.0. Kept at module level because it is a
#: recorded fact about another program, not state belonging to a test class.
REAL_QMP_SCHEMA: list[dict] = [
    {"name": "screendump", "ret-type": "0", "meta-type": "command", "arg-type": "132"},
    {
        "name": "132",
        "members": [
            {"name": "filename", "type": "str"},
            {"name": "device", "default": None, "type": "str"},
            {"name": "head", "default": None, "type": "int"},
            {"name": "format", "default": None, "type": "415"},
        ],
        "meta-type": "object",
    },
    {"name": "query-status", "ret-type": "1", "meta-type": "command", "arg-type": "0"},
]


class TestQapiSchemaShape:
    """``arg-type`` resolution, pinned against output captured from a real QEMU 11.1.

    This exists because the probe used to assume an inline member list and worked only
    against a double that made the same assumption. The fragment above is verbatim.
    """

    def by_name(self) -> dict:
        return {entry.get("name"): entry for entry in REAL_QMP_SCHEMA}

    def test_a_named_type_resolves_to_its_members(self) -> None:
        assert qapi_declares_argument("132", self.by_name(), "format") is True
        assert qapi_declares_argument("132", self.by_name(), "device") is True

    def test_a_missing_argument_is_false_not_an_error(self) -> None:
        assert qapi_declares_argument("132", self.by_name(), "nonsense") is False

    def test_the_builtin_no_argument_type_is_false(self) -> None:
        """``query-status`` spells "no arguments" as the builtin ``0``."""
        assert qapi_declares_argument("0", self.by_name(), "format") is False

    def test_an_inline_member_list_still_resolves(self) -> None:
        """Older outputs inlined the members; both spellings must work."""
        inline = [{"name": "filename", "type": "str"}, {"name": "format", "type": "str"}]
        assert qapi_declares_argument(inline, self.by_name(), "format") is True
        assert qapi_declares_argument({"members": inline}, self.by_name(), "format") is True

    def test_junk_answers_false_rather_than_raising(self) -> None:
        for value in (None, 132, "", [], {"members": "nope"}):
            assert qapi_declares_argument(value, self.by_name(), "format") is False

    def test_the_recorded_fragment_still_matches_the_shape_the_probe_expects(self) -> None:
        """A guard on the fixture itself: if QEMU changes the shape, this records what the
        probe was written against rather than letting it drift silently."""
        command = next(e for e in REAL_QMP_SCHEMA if e.get("name") == "screendump")
        assert isinstance(command["arg-type"], str)
        target = self.by_name()[command["arg-type"]]
        assert target["meta-type"] == "object"
        assert any(member["name"] == "format" for member in target["members"])
