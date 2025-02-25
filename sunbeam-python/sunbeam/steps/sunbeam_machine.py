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

import logging

from sunbeam.clusterd.client import Client
from sunbeam.core.deployment import Deployment, Networks
from sunbeam.core.juju import JujuHelper
from sunbeam.core.manifest import Manifest
from sunbeam.core.steps import (
    AddMachineUnitsStep,
    DeployMachineApplicationStep,
    DestroyMachineApplicationStep,
    RemoveMachineUnitsStep,
)
from sunbeam.core.terraform import TerraformHelper

LOG = logging.getLogger(__name__)
CONFIG_KEY = "TerraformVarsSunbeamMachine"
APPLICATION = "sunbeam-machine"
SUNBEAM_MACHINE_APP_TIMEOUT = 180  # 3 minutes, managing the application should be fast
SUNBEAM_MACHINE_UNIT_TIMEOUT = (
    1200  # 20 minutes, adding / removing units can take a long time
)


class DeploySunbeamMachineApplicationStep(DeployMachineApplicationStep):
    """Deploy openstack-hyervisor application using Terraform cloud."""

    def __init__(
        self,
        deployment: Deployment,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
        refresh: bool = False,
        proxy_settings: dict = {},
    ):
        super().__init__(
            deployment,
            client,
            tfhelper,
            jhelper,
            manifest,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Deploy sunbeam-machine",
            "Deploying Sunbeam Machine",
            refresh,
        )
        self.proxy_settings = proxy_settings

    def get_application_timeout(self) -> int:
        """Return application timeout."""
        return SUNBEAM_MACHINE_APP_TIMEOUT

    def extra_tfvars(self) -> dict:
        """Extra terraform vars to pass to terraform apply."""
        return {
            "endpoint_bindings": [
                {"space": self.deployment.get_space(Networks.MANAGEMENT)},
            ],
            "charm_config": {
                "http_proxy": self.proxy_settings.get("HTTP_PROXY", ""),
                "https_proxy": self.proxy_settings.get("HTTPS_PROXY", ""),
                "no_proxy": self.proxy_settings.get("NO_PROXY", ""),
            },
        }


class AddSunbeamMachineUnitsStep(AddMachineUnitsStep):
    """Add Sunbeam machine Units."""

    def __init__(
        self,
        client: Client,
        names: list[str] | str,
        jhelper: JujuHelper,
        model: str,
    ):
        super().__init__(
            client,
            names,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Add Sunbeam-machine unit(s)",
            "Adding Sunbeam Machine unit to machine(s)",
        )

    def get_unit_timeout(self) -> int:
        """Return unit timeout in seconds."""
        return SUNBEAM_MACHINE_UNIT_TIMEOUT


class RemoveSunbeamMachineUnitsStep(RemoveMachineUnitsStep):
    """Remove Sunbeam machine Unit."""

    def __init__(
        self, client: Client, names: list[str] | str, jhelper: JujuHelper, model: str
    ):
        super().__init__(
            client,
            names,
            jhelper,
            CONFIG_KEY,
            APPLICATION,
            model,
            "Remove sunbeam-machine unit",
            f"Removing sunbeam-machine unit from machine(s) {names}",
        )

    def get_unit_timeout(self) -> int:
        """Return unit timeout in seconds."""
        return SUNBEAM_MACHINE_UNIT_TIMEOUT


class DestroySunbeamMachineApplicationStep(DestroyMachineApplicationStep):
    """Destroy Sunbeam Machine application using Terraform."""

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        jhelper: JujuHelper,
        manifest: Manifest,
        model: str,
    ):
        super().__init__(
            client,
            tfhelper,
            jhelper,
            manifest,
            CONFIG_KEY,
            [APPLICATION],
            model,
            "Destroy Sunbeam Machine",
            "Destroying Sunbeam Machine",
        )

    def get_application_timeout(self) -> int:
        """Return application timeout in seconds."""
        return SUNBEAM_MACHINE_APP_TIMEOUT
