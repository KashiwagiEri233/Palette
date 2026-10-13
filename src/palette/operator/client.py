"""One-shot CLI client for the already-running owner service."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..errors import ErrorCode, PaletteError
from .paths import IS_WINDOWS, RuntimePaths
from .protocol import (
    CONNECT_TIMEOUT,
    Channel,
    Request,
    Response,
    decode_json,
    receive_frame,
    send_frame,
)
from .unix import UnixChannel


class OperatorClient:
    def __init__(self, runtime_root: Path) -> None:
        self.paths = RuntimePaths(runtime_root)

    async def call(self, method: str, **parameters: Any) -> dict[str, Any]:
        try:
            endpoint = self.paths.read_endpoint()
        except (OSError, ValueError) as exc:
            raise PaletteError(
                ErrorCode.PRECONDITION_FAILED,
                "no usable palette serve owns this runtime; start palette serve with the same --root",
            ) from exc
        try:
            request = Request.model_validate(
                {
                    "generation": endpoint.generation,
                    "token": endpoint.token,
                    "request_id": uuid.uuid4().hex,
                    "command": {"method": method, **parameters},
                }
            )
        except ValidationError as exc:
            raise PaletteError(
                ErrorCode.INVALID_ARGUMENT, "invalid operator command arguments"
            ) from exc
        channel: Channel
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                if IS_WINDOWS:
                    from .windows import connect

                    channel = await connect(endpoint.address)
                else:
                    open_unix: Any = getattr(asyncio, "open_unix_connection", None)
                    reader, writer = await open_unix(endpoint.address)
                    channel = UnixChannel(reader, writer)
        except (OSError, TimeoutError) as exc:
            raise PaletteError(
                ErrorCode.PRECONDITION_FAILED,
                "palette serve is unavailable; start it with the same --root and retry the connection",
            ) from exc
        try:
            try:
                await send_frame(channel, request)
                response = Response.model_validate(
                    decode_json(
                        await receive_frame(channel, timeout=endpoint.operation_timeout_seconds + 5)
                    )
                )
                if (
                    response.generation != endpoint.generation
                    or response.request_id != request.request_id
                ):
                    raise ValueError("operator response does not match this request")
            except (
                OSError,
                EOFError,
                TimeoutError,
                ValueError,
                asyncio.IncompleteReadError,
            ) as exc:
                raise PaletteError(
                    ErrorCode.ACTION_OUTCOME_UNKNOWN,
                    "the operator connection ended before its result was confirmed; query status before retrying",
                    retryable=False,
                ) from exc
            if response.error is not None:
                try:
                    code = ErrorCode(response.error.code)
                except ValueError:
                    code = ErrorCode.INTERNAL_ERROR
                raise PaletteError(
                    code,
                    response.error.message,
                    details=response.error.details,
                    retryable=response.error.retryable,
                )
            return response.result or {}
        finally:
            await channel.close()
