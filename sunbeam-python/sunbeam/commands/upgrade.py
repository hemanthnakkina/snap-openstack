# SPDX-FileCopyrightText: 2025 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Upgrade CLI commands.

Provides the ``sunbeam cluster upgrade`` command group with
subcommands for managing the upgrade lifecycle.

Current subcommands:
- preflight: run pre-flight checks and create the active hop.
- abandon: mark the active hop as abandoned, release the lock, and
  print recovery guidance.
"""

from __future__ import annotations

import click
from rich.console import Console

from sunbeam.clusterd.service import UpgradeLockHeldException
from sunbeam.core.deployment import Deployment
from sunbeam.core.openstack import OPENSTACK_MODEL
from sunbeam.upgrades.coordinator import ReleaseUpgradeCoordinator
from sunbeam.upgrades.observability import UpgradeLogger
from sunbeam.upgrades.preflight.checks import (
    CheckContext,
    build_preflight_checks,
    run_upgrade_preflight_checks,
)
from sunbeam.upgrades.preflight.hop import create_hop_after_preflight
from sunbeam.versions import detect_deployed_release, detect_snap_release

console = Console()


def _detect_from_release(deployment: Deployment) -> str:
    """Auto-detect the source release from deployed charm channels.

    Reads charm channels from the openstack model and matches against
    RELEASE_TRACKS. Falls back to prompting the operator if detection
    fails.
    """
    jhelper = deployment.get_juju_helper()
    status = jhelper.get_model_status(OPENSTACK_MODEL)
    charm_channels: dict[str, str] = {}
    for app_name, app in status.apps.items():
        if app.charm_channel:
            charm_channels[app_name] = app.charm_channel
    release = detect_deployed_release(charm_channels)
    if release is None:
        raise click.ClickException(
            "Cannot auto-detect the deployed OpenStack release from charm "
            "channels. Specify --from explicitly."
        )
    return release


@click.group("upgrade", context_settings={"help_option_names": ["-h", "--help"]})
@click.pass_context
def upgrade(ctx: click.Context) -> None:
    """Manage cluster upgrades."""


@upgrade.command("preflight")
@click.option(
    "--from",
    "from_release",
    help="Source release (auto-detected if omitted).",
    default=None,
)
@click.option(
    "--capacity-policy-override",
    is_flag=True,
    default=False,
    help="Skip the capacity policy check.",
)
@click.pass_context
def preflight(
    ctx: click.Context,
    from_release: str | None,
    capacity_policy_override: bool,
) -> None:
    """Run pre-flight checks and create the active upgrade hop.

    Validates cluster health, capacity, and metadata before starting
    an upgrade. If all checks pass, creates the active hop with status
    pending.

    Exit codes:
      0 — all checks passed, active hop created
      1 — operational failure (unhealthy cluster, capacity, MySQL)
      2 — invalid hop or missing metadata
    """
    deployment: Deployment = ctx.obj
    client = deployment.get_client()

    to_release = detect_snap_release()
    if from_release is None:
        from_release = _detect_from_release(deployment)

    click.echo(f"\nRunning pre-flight checks for {from_release} -> {to_release}...\n")

    ctx_obj = CheckContext(
        deployment=deployment,
        from_release=from_release,
        to_release=to_release,
    )
    checks = build_preflight_checks(ctx_obj, capacity_override=capacity_policy_override)

    try:
        run_upgrade_preflight_checks(checks, console)
    except click.ClickException:
        raise

    click.echo("  All checks passed.\n")
    click.echo(
        "  NOTE: Ensure backups are taken before proceeding (sunbeam backup).\n"
        "  The upgrade engine does not create or verify backups.\n"
    )

    metadata = ctx_obj.metadata
    if metadata is None:
        raise click.ClickException("Metadata not loaded after preflight checks.")

    try:
        hop = create_hop_after_preflight(client, from_release, to_release, metadata)
    except UpgradeLockHeldException:
        raise click.ClickException(
            "Cannot acquire the upgrade lock — it is held by another process. "
            "Wait for the lock to expire and retry."
        )
    except RuntimeError as e:
        raise click.ClickException(str(e))

    click.echo(f"Active hop created: {hop.from_release} -> {hop.to_release}")
    click.echo("Next: sunbeam cluster upgrade control-plane --auto")


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
