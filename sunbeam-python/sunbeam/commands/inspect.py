# Copyright (c) 2022 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.table import Table
from snaphelpers import Snap

from sunbeam.clusterd.service import ConfigItemNotFoundException
from sunbeam.core.common import (
    FORMAT_TABLE,
    FORMAT_YAML,
    run_plan,
)
from sunbeam.core.deployment import Deployment
from sunbeam.core.juju import JujuHelper
from sunbeam.core.openstack import OPENSTACK_MODEL
from sunbeam.steps.juju import WriteCharmLogStep, WriteJujuStatusStep
from sunbeam.utils import argument_with_deprecated_option, click_option_show_hints

LOG = logging.getLogger(__name__)
console = Console()
snap = Snap()


@click.group(invoke_without_command=True)
@click_option_show_hints
@click.pass_context
def inspect(ctx: click.Context, show_hints: bool) -> None:
    """Inspect the sunbeam installation.

    This script will inspect your installation. It will report any issue
    it finds, and create a tarball of logs and traces which can be
    attached to an issue filed against the sunbeam project.
    """
    deployment: Deployment = ctx.obj

    if ctx.invoked_subcommand is not None:
        return
    jhelper = JujuHelper(deployment.get_connected_controller())

    time_stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_name = f"sunbeam-inspection-report-{time_stamp}.tar.gz"
    dump_file: Path = Path(snap.paths.user_common) / file_name

    plan = []
    with tempfile.TemporaryDirectory() as tmpdirname:
        for model in [deployment.openstack_machines_model, OPENSTACK_MODEL]:
            status_file = Path(tmpdirname) / f"juju_status_{model}.out"
            debug_file = Path(tmpdirname) / f"debug_log_{model}.out"
            plan.extend(
                [
                    WriteJujuStatusStep(jhelper, model, status_file),
                    WriteCharmLogStep(jhelper, model, debug_file),
                ]
            )

        run_plan(plan, console, show_hints)

        with console.status("[bold green]Copying logs..."):
            log_dir = snap.paths.user_common / "logs"
            if log_dir.exists():
                shutil.copytree(log_dir, Path(tmpdirname) / "logs")

        with (
            console.status("[bold green]Creating tarball..."),
            tarfile.open(dump_file, "w:gz") as tar,
        ):
            tar.add(tmpdirname, arcname="./")

    console.print(f"[green]Output file written to {dump_file}[/green]")


@inspect.command()
@click.option(
    "-f",
    "--format",
    type=click.Choice([FORMAT_TABLE, FORMAT_YAML]),
    default=FORMAT_TABLE,
    help="Output format.",
)
@click.pass_context
def plans(ctx: click.Context, format: str):
    """List terraform plans and their lock status."""
    deployment: Deployment = ctx.obj
    client = deployment.get_client()
    plans = client.cluster.list_terraform_plans()
    locks = client.cluster.list_terraform_locks()
    if format == FORMAT_TABLE:
        table = Table()
        table.add_column("Plan", justify="left")
        table.add_column("Locked", justify="center")
        for plan in plans:
            table.add_row(
                plan,
                "x" if plan in locks else "",
            )
        console.print(table)
    elif format == FORMAT_YAML:
        plan_states = {
            plan: "locked" if plan in locks else "unlocked" for plan in plans
        }
        console.print(yaml.dump(plan_states))


@inspect.command()
@argument_with_deprecated_option(
    "plan", type=str, help="Name of the terraform plan to unlock."
)
@click.option("--force", is_flag=True, default=False, help="Force unlock the plan.")
@click.pass_context
def unlock_plan(ctx: click.Context, plan: str, force: bool):
    """Unlock a terraform plan."""
    deployment: Deployment = ctx.obj
    client = deployment.get_client()
    try:
        lock = client.cluster.get_terraform_lock(plan)
    except ConfigItemNotFoundException as e:
        raise click.ClickException(f"Lock for {plan!r} not found") from e
    if not force:
        lock_creation_time = datetime.datetime.strptime(
            lock["Created"][:-4] + "Z", "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        if datetime.datetime.utcnow() - lock_creation_time < datetime.timedelta(
            hours=1
        ):
            click.confirm(
                f"Plan {plan!r} was locked less than an hour ago,"
                " are you sure you want to unlock it?",
                abort=True,
            )
    try:
        client.cluster.unlock_terraform_plan(plan, lock)
    except ConfigItemNotFoundException as e:
        raise click.ClickException(f"Lock for {plan!r} not found") from e
    console.print(f"Unlocked plan {plan!r}")
