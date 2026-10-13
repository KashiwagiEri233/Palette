"""Authenticated, local operator access to the service that owns the VMs."""

from .client import OperatorClient
from .server import OperatorServer

__all__ = ["OperatorClient", "OperatorServer"]
