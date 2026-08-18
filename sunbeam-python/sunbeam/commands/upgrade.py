# SPDX-FileCopyrightText: 2025 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Upgrade CLI commands.

Provides the ``sunbeam cluster upgrade`` command group with
subcommands for managing the upgrade lifecycle.

Current subcommands:
- abandon: mark the active hop as abandoned, release the lock, and
  print recovery guidance.
"""

from __future__ import annotations

import click
from rich.console import Console

from sunbeam.clusterd.service import UpgradeLockHeldException
from sunbeam.core.deployment import Deployment
from sunbeam.upgrades.coordinator import ReleaseUpgradeCoordinator
from sunbeam.upgrades.observability import UpgradeLogger

console = Console()


@click.group("upgrade", context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def upgrade(ctx: click.Context) -> None:
    """Manage cluster upgrades."""


@upgrade.command("abandon")
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
@click.pass_context
def abandon(ctx: click.Context, yes: bool) -> None:
    """Abandon the active upgrade hop.

    Marks the active hop as abandoned, releases the upgrade lock, and
    prints recovery guidance. The hop enters a terminal state — it
    cannot be resumed. Recovery is operator-driven via
    ``sunbeam restore`` and component-specific commands.
    """
    deployment: Deployment = ctx.obj
    client = deployment.get_client()

    coordinator = ReleaseUpgradeCoordinator(client, UpgradeLogger())
    coordinator.load_state()
    hop = coordinator.get_current_hop()
    if hop is None:
        raise click.ClickException("No active upgrade hop to abandon.")

    if not yes:
        click.echo(
            f"This will abandon the active upgrade hop "
            f"{hop.from_release} -> {hop.to_release}.\n"
            "The hop will enter a terminal state and cannot be resumed.\n"
            "Recovery is operator-driven via 'sunbeam restore'.\n"
        )
        click.confirm("Are you sure?", abort=True)

    try:
        coordinator.acquire_lock()
        coordinator.load_state()
        coordinator.abandon()
    except UpgradeLockHeldException:
        raise click.ClickException(
            "Cannot acquire the upgrade lock — it is held by another process. "
            "Wait for the lock to expire and retry, or identify and stop "
            "the process holding the lock."
        )
    finally:
        coordinator.release_lock()

    click.echo(
        f"\nUpgrade hop {hop.from_release} -> {hop.to_release} abandoned.\n\n"
        "Recovery steps:\n"
        "  1. Recover from failure using 'sunbeam restore' and\n"
        "     component-specific commands.\n"
        "  2. Once recovered, start a new upgrade with\n"
        "     'sunbeam cluster upgrade preflight'.\n"
    )
