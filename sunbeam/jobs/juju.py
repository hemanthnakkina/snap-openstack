# Copyright (c) 2023 Canonical Ltd.
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

import asyncio
import logging
import json
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path
from typing import Awaitable, Dict, List, Optional, TypeVar, cast

import yaml
from juju.application import Application
from juju.controller import Controller
from juju.model import Model
from juju.unit import Unit

from sunbeam.clusterd.client import Client as clusterClient

LOG = logging.getLogger(__name__)
CONTROLLER_MODEL = "admin/controller"
# Note(gboutry): pylibjuju get_model does not support user/model
MODEL = CONTROLLER_MODEL.split("/")[1]
CONTROLLER = "sunbeam-controller"
JUJU_CONTROLLER_KEY = "JujuController"
ACCOUNT_FILE = "account.yaml"


T = TypeVar("T")


def run_sync(coro: Awaitable[T]) -> T:
    """Helper to run coroutines synchronously."""
    result = asyncio.get_event_loop().run_until_complete(coro)
    return cast(T, result)


class JujuException(Exception):
    """Main juju exception, to be subclassed."""

    pass


class ControllerNotFoundException(JujuException):
    """Raised when controller is missing."""

    pass


class ModelNotFoundException(JujuException):
    """Raised when model is missing."""

    pass


class MachineNotFoundException(JujuException):
    """Raised when machine is missing from model."""

    pass


class JujuAccountNotFound(JujuException):
    """Raised when account in snap's user_data is missing."""

    pass


class ApplicationNotFoundException(JujuException):
    """Raised when application is missing from model."""

    pass


class UnitNotFoundException(JujuException):
    """Raised when unit is missing from model."""

    pass


class TimeoutException(JujuException):
    """Raised when a query timed out"""

    pass


@dataclass
class JujuAccount:
    user: str
    password: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def load(cls, data_location: Path) -> "JujuAccount":
        data_file = data_location / ACCOUNT_FILE
        try:
            with data_file.open() as file:
                return JujuAccount(**yaml.safe_load(file))
        except FileNotFoundError as e:
            raise JujuAccountNotFound() from e

    def write(self, data_location: Path):
        data_file = data_location / ACCOUNT_FILE
        if not data_file.exists():
            data_file.touch()
        data_file.chmod(0o660)
        with data_file.open("w") as file:
            yaml.safe_dump(self.to_dict(), file)


@dataclass
class JujuController:
    api_endpoints: List[str]
    ca_cert: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def load(cls, client: clusterClient) -> "JujuController":
        controller = client.cluster.get_config(JUJU_CONTROLLER_KEY)
        return JujuController(**json.loads(controller))

    def write(self, client: clusterClient):
        client.cluster.update_config(JUJU_CONTROLLER_KEY, json.dumps(self.to_dict()))


def controller(func):
    """Automatically set up controller."""

    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        if self.controller is None:
            client = clusterClient()
            juju_controller = JujuController.load(client)

            account = JujuAccount.load(self.data_location)

            self.controller = Controller()
            await self.controller.connect(
                endpoint=juju_controller.api_endpoints,
                cacert=juju_controller.ca_cert,
                username=account.user,
                password=account.password,
            )
        return await func(self, *args, **kwargs)

    return wrapper


class JujuHelper:
    """Helper function to manage Juju apis through pylibjuju."""

    def __init__(self, data_location: Path):
        self.data_location = data_location
        self.controller = None

    @controller
    async def get_model(self, model: str) -> Model:
        """Fetch model.

        :model: Name of the model
        """
        try:
            return await self.controller.get_model(model)
        except Exception as e:
            if "HTTP 400" in str(e):
                raise ModelNotFoundException
            raise e

    @controller
    async def get_application(self, name: str, model: str) -> Application:
        """Fetch application in model.

        :name: Application name
        :model: Name of the model where the application is located
        """
        model_impl = await self.get_model(model)
        application = model_impl.applications.get(name)
        if application is None:
            raise ApplicationNotFoundException(
                f"Application missing from model: {model!r}"
            )
        return application

    @controller
    async def get_unit(self, name: str, model: str) -> Unit:
        """Fetch an application's unit in model.

        :name: Name of the unit to wait for, name format is application/id
        :model: Name of the model where the unit is located"""
        parts = name.split("/")
        if len(parts) != 2:
            raise ValueError(
                f"Name {name!r} has invalid format, "
                "should be a valid unit of format application/id"
            )
        model_impl = await self.get_model(model)

        unit = model_impl.units.get(name)

        if unit is None:
            raise UnitNotFoundException(
                f"Unit {name!r} is missing from model {model!r}"
            )
        return unit

    @controller
    async def add_unit(
        self,
        name: str,
        model: str,
        machine: Optional[str] = None,
    ) -> Unit:
        """Add unit to application, can be optionnally placed on a machine.

        :name: Application name
        :model: Name of the model where the application is located
        :machine: Machine ID to place the unit on, optional
        """

        model_impl = await self.get_model(model)

        application = model_impl.applications.get(name)

        if application is None:
            raise ApplicationNotFoundException(
                f"Application {name!r} is missing from model {model!r}"
            )

        # Note(gboutry): add_unit waits for unit to be added to model,
        # but does not check status
        # we add only one unit, so it's ok to get the first result
        return (await application.add_unit(1, machine))[0]

    @controller
    async def remove_unit(self, name: str, unit: str, model: str):
        """Remove unit from application.

        :name: Application name
        :unit: Unit tag
        :model: Name of the model where the application is located
        """

        model_impl = await self.get_model(model)

        application = model_impl.applications.get(name)

        if application is None:
            raise ApplicationNotFoundException(
                f"Application {name!r} is missing from model {model!r}"
            )

        await application.destroy_unit(unit)

    @controller
    async def wait_application_ready(
        self,
        name: str,
        model: str,
        accepted_status: Optional[List[str]] = None,
        timeout: Optional[int] = None,
    ):
        """Block execution until application is ready
        The function early exits if the application is missing from the model

        :name: Name of the application to wait for
        :model: Name of the model where the application is located
        :accepted status: List of status acceptable to exit the waiting loop, default:
            ["active"]
        :timeout: Waiting timeout in seconds
        """
        if accepted_status is None:
            accepted_status = ["active"]

        model_impl = await self.get_model(model)

        try:
            application = await self.get_application(name, model)
        except ApplicationNotFoundException as e:
            LOG.debug(str(e))
            return

        if application is None:
            LOG.debug(f"Application {name!r} is missing from model {model!r}")
            return

        LOG.debug(f"Application {name!r} is in status: {application.status!r}")

        try:
            await model_impl.block_until(
                lambda: model_impl.applications[name].status in accepted_status,
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise TimeoutException(
                f"Timed out while waiting for application {name!r} to be ready"
            ) from e

    @controller
    async def wait_unit_ready(
        self,
        name: str,
        model: str,
        accepted_status: Optional[Dict[str, List[str]]] = None,
        timeout: Optional[int] = None,
    ):
        """Block execution until unit is ready
        The function early exits if the unit is missing from the model

        :name: Name of the unit to wait for, name format is application/id
        :model: Name of the model where the unit is located
        :accepted status: map of accepted statuses for "workload" and "agent"
        :timeout: Waiting timeout in seconds
        """

        if accepted_status is None:
            accepted_status = {}

        agent_accepted_status = accepted_status.get("agent", ["idle"])
        workload_accepted_status = accepted_status.get("workload", ["active"])

        model_impl = await self.get_model(model)

        try:
            unit = await self.get_unit(name, model)
        except UnitNotFoundException as e:
            LOG.debug(str(e))
            return

        LOG.debug(
            f"Unit {name!r} is in status: "
            f"agent={unit.agent_status!r}, workload={unit.workload_status!r}"
        )

        def condition() -> bool:
            """Computes readiness for unit"""
            unit = model_impl.units[name]
            agent_ready = unit.agent_status in agent_accepted_status
            workload_ready = unit.workload_status in workload_accepted_status
            return agent_ready and workload_ready

        try:
            await model_impl.block_until(
                condition,
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise TimeoutException(
                f"Timed out while waiting for unit {name!r} to be ready"
            ) from e

    @controller
    async def wait_until_active(
        self,
        model: str,
        timeout: Optional[int] = None,
    ) -> None:
        """Wait for all agents in model to reach idle status

        :model: Name of the model to wait for readiness
        :timeout: Waiting timeout in seconds
        """
        model_impl = await self.get_model(model)

        try:
            await model_impl.block_until(
                lambda: model_impl.all_units_idle(),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise TimeoutException(
                f"Timed out while waiting for model {model!r} to be ready"
            ) from e
