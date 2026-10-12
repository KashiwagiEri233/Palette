"""Palette: observable, operable, resettable QEMU virtual desktops for visual agents.

The package is layered so that no layer trusts the one above it:

``server``      MCP tool contracts and image/structured results
``service``     orchestration: policy, dispatch, audit, lifecycle
``policy``      authorization, path confinement, capability checks
``qmp``         the private QEMU management channel
``vm``          process, image and lifecycle management
``capture``     framebuffer acquisition
``input``       coordinate transforms, keymaps, bounded action transactions
``guest_tools`` the retained, disabled-by-default in-guest enhancement channel
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
