"""Mindscape plugin entry point.

Registers the outbound toolset. Tools register unconditionally: a node with no
peers yet still needs ``mindscape_roster`` to say so.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def register(ctx) -> None:
    try:
        from .tools import register_tools

        register_tools(ctx)
    except Exception:  # never let a plugin failure break the host agent
        logger.warning("Mindscape: failed to register tools", exc_info=True)

    # Keep the ledger current without a human polling it. Without this, a run that
    # died looks identical to one working, which is the observability gap the tab
    # exists to close. Idempotent, a daemon thread, and free while the mesh is idle.
    try:
        from .reconcile import start_poller

        start_poller()
    except Exception:
        logger.warning("Mindscape: failed to start ledger reconciler", exc_info=True)

    # A tab in the desktop app comes from being a PLATFORM, not from tools. Registering
    # an inbound adapter is what puts Mindscape alongside Sessions, Telegram and Photon.
    try:
        from .adapter import (
            PLATFORM_HINT,
            MindscapeAdapter,
            check_requirements,
            interactive_setup,
            is_connected,
            validate_config,
        )

        ctx.register_platform(
            name="mindscape",
            label="Mindscape",
            adapter_factory=lambda cfg: MindscapeAdapter(cfg),
            check_fn=check_requirements,
            validate_config=validate_config,
            is_connected=is_connected,
            required_env=[],
            install_hint="No extra packages needed (stdlib only)",
            setup_fn=interactive_setup,
            emoji="\U0001f578",  # spider web
            allowed_users_env="MINDSCAPE_ALLOWED_USERS",
            allow_all_env="MINDSCAPE_ALLOW_ALL_USERS",
            cron_deliver_env_var="MINDSCAPE_HOME_LANE",
            allow_update_command=False,
            platform_hint=PLATFORM_HINT,
        )
    except Exception:
        logger.warning("Mindscape: failed to register platform adapter", exc_info=True)
