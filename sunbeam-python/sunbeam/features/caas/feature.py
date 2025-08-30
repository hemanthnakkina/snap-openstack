# SPDX-FileCopyrightText: 2023 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import click
import httpx
import yaml
from packaging.version import Version
from pydantic import Field
from rich.console import Console
from rich.status import Status
from snaphelpers import Snap

from sunbeam.clusterd.client import Client
from sunbeam.clusterd.service import ConfigItemNotFoundException
from sunbeam.commands.configure import retrieve_admin_credentials
from sunbeam.core.common import (
    BaseStep,
    Result,
    ResultType,
    RiskLevel,
    read_config,
    run_plan,
)
from sunbeam.core.deployment import Deployment
from sunbeam.core.juju import JujuHelper
from sunbeam.core.k8s import K8S_APP_NAME, K8SHelper
from sunbeam.core.manifest import (
    AddManifestStep,
    CharmManifest,
    FeatureConfig,
    Manifest,
    SoftwareConfig,
    TerraformManifest,
)
from sunbeam.core.openstack import OPENSTACK_MODEL
from sunbeam.core.terraform import (
    TerraformException,
    TerraformHelper,
    TerraformInitStep,
    TerraformStateLockedException,
)
from sunbeam.features.interface.v1.base import ConfigType, FeatureRequirement
from sunbeam.features.interface.v1.openstack import (
    DisableOpenStackApplicationStep,
    EnableOpenStackApplicationStep,
    OpenStackControlPlaneFeature,
    TerraformPlanLocation,
)
from sunbeam.lazy import LazyImport
from sunbeam.steps.k8s import KubeClientError, get_kube_client
from sunbeam.utils import click_option_show_hints, pass_method_obj
from sunbeam.versions import CLUSTER_API_VERSIONS, OPENSTACK_CHANNEL

if TYPE_CHECKING:
    import lightkube.core.exceptions as l_exceptions
    from lightkube.core import selector
    from lightkube.resources import apiextensions_v1, core_v1, rbac_authorization_v1
else:
    l_exceptions = LazyImport("lightkube.core.exceptions")
    core_v1 = LazyImport("lightkube.resources.core_v1")
    apiextensions_v1 = LazyImport("lightkube.resources.apiextensions_v1")
    selector = LazyImport("lightkube.core.selector")
    rbac_authorization_v1 = LazyImport("lightkube.resources.rbac_authorization_v1")


LOG = logging.getLogger(__name__)
console = Console()

PROVIDER_WAIT_TIMEOUT = 300  # 5 minutes for each provider


class CaasConfig(FeatureConfig):
    image_name: str | None = Field(default=None, description="CAAS Image name")
    image_url: str | None = Field(
        default=None, description="CAAS Image URL to upload to glance"
    )
    container_format: str | None = Field(
        default=None, description="Image container format"
    )
    disk_format: str | None = Field(default=None, description="Image disk format")
    properties: dict = Field(
        default={}, description="Properties to set for image in glance"
    )


class CaasConfigureStep(BaseStep):
    """Configure CaaS service."""

    def __init__(
        self,
        client: Client,
        tfhelper: TerraformHelper,
        manifest: Manifest,
        tfvar_map: dict,
    ):
        super().__init__(
            "Configure Container as a Service",
            "Configure Cloud for Container as a Service use",
        )
        self.client = client
        self.tfhelper = tfhelper
        self.manifest = manifest
        self.tfvar_map = tfvar_map

    def run(self, status: Status | None = None) -> Result:
        """Execute configuration using terraform."""
        try:
            override_tfvars = {}
            try:
                feature_manifest = self.manifest.get_feature("caas")
                if not feature_manifest:
                    raise ValueError("No caas feature found in manifest")
                manifest_caas_config = feature_manifest.config
                if not manifest_caas_config:
                    raise ValueError("No caas configuration found in manifest")
                manifest_caas_config_dict = manifest_caas_config.model_dump(
                    by_alias=True
                )
                for caas_config_attribute, tfvar_name in self.tfhelper.tfvar_map.get(
                    "caas_config", {}
                ).items():
                    if caas_config_attribute_ := manifest_caas_config_dict.get(
                        caas_config_attribute
                    ):
                        override_tfvars[tfvar_name] = caas_config_attribute_
            except AttributeError:
                # caas_config not defined in manifest, ignore
                pass

            self.tfhelper.update_tfvars_and_apply_tf(
                self.client, self.manifest, override_tfvars=override_tfvars
            )
        except (TerraformException, TerraformStateLockedException) as e:
            LOG.exception("Error configuring Container as a Service feature.")
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class SetupClusterAPI(BaseStep):
    """Setup Management Cluster for Cluster API."""

    def __init__(
        self, client: Client, jhelper: JujuHelper, snap: Snap, machine_model: str
    ):
        super().__init__(
            "Setup Cluster API components",
            "Setup Cluster API components to transform into management cluster",
        )
        self.client = client
        self.jhelper = jhelper
        self.snap = snap
        self.machine_model = self.jhelper.get_model_name_with_owner(machine_model)
        self.micro_version_changed = False

    def is_skip(self, status: Status | None = None) -> Result:
        """Determines if the step should be skipped or not.

        :return: ResultType.SKIPPED if the Step should be skipped,
                 ResultType.COMPLETED or ResultType.FAILED otherwise
        """
        try:
            self.kubeconfig = read_config(self.client, K8SHelper.get_kubeconfig_key())
        except ConfigItemNotFoundException:
            LOG.debug("K8S kubeconfig not found", exc_info=True)
            return Result(ResultType.FAILED, "K8S kubeconfig not found")

        try:
            self.kube = get_kube_client(self.client)
        except KubeClientError as e:
            LOG.debug("Failed to create k8s client", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        try:
            providers = list(
                self.kube.list(
                    K8SHelper.get_provider_resource(),
                    namespace="*",
                    labels={"clusterctl.cluster.x-k8s.io/core": "inventory"},
                )
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                providers = []
            else:
                LOG.debug("Failed to retrieve providers", exc_info=True)
                return Result(ResultType.FAILED, str(e))
        except l_exceptions.ApiError as e:
            if e.status.code == 404:
                providers = []
            else:
                LOG.debug("Failed to retrieve providers", exc_info=True)
                return Result(ResultType.FAILED, str(e))

        if not providers:
            return Result(ResultType.COMPLETED)

        for p in providers:
            provider_name = p.get("metadata", {}).get("name")
            provider_installed_version = Version(p.get("version", "0.0.0"))
            provider_required_version = Version(
                CLUSTER_API_VERSIONS.get(provider_name, "0.0.0")
            )
            LOG.warning(
                f"{provider_name}: {provider_installed_version}"
                f" {provider_required_version}"
            )
            if (
                provider_installed_version.major != provider_required_version.major
                or provider_installed_version.minor != provider_required_version.minor
            ):
                return Result(
                    ResultType.FAILED,
                    "Only micro version upgrade is supported for Cluster API component "
                    f"{provider_name}. Installed version: {provider_installed_version},"
                    f" Required version: {provider_required_version}",
                )

            if provider_installed_version.micro != provider_required_version.micro:
                self.micro_version_changed = True

        if self.micro_version_changed:
            return Result(ResultType.COMPLETED)

        return Result(ResultType.SKIPPED)

    def _install_orc_crd(self) -> None:
        timeout = 180  # 3 minutes to install ORC CRD
        crd_filename = "crd_orc.yaml"
        source: Path = (
            self.snap.paths.snap / "etc" / "cluster-api-resources" / crd_filename
        )
        destination = "/var/lib/juju/"
        k8s_leader_unit = self.jhelper.get_leader_unit(K8S_APP_NAME, self.machine_model)

        # juju scp orc crd file to k8s/leader unit in /tmp location
        scp_command_args = [
            "scp",
            "--model",
            self.machine_model,
            str(source),
            f"{k8s_leader_unit}:{destination}",
        ]
        self.jhelper._juju.cli(*scp_command_args)

        # Install the orc crd on k8s leader unit
        install_cmd = [
            "sudo",
            "k8s",
            "kubectl",
            "apply",
            "-f",
            f"{destination}{crd_filename}",
        ]
        self.jhelper.run_cmd_on_machine_unit_payload(
            k8s_leader_unit, self.machine_model, " ".join(install_cmd), timeout
        )

    def _initialize_or_upgrade_capi(self) -> None:
        cmd = ["clusterctl", "init"]
        # Upgrade fails due to https://github.com/canonical/cluster-api-k8s/issues/181
        if self.micro_version_changed:
            cmd = ["clusterctl", "upgrade", "apply"]

        with tempfile.NamedTemporaryFile(mode="w") as kubeconfig_file:
            kubeconfig_file.write(yaml.safe_dump(self.kubeconfig))
            kubeconfig_file.flush()
            cmd.extend(
                [
                    "--core",
                    f"cluster-api:{CLUSTER_API_VERSIONS.get('cluster-api')}",
                    "--bootstrap",
                    f"canonical-kubernetes:{CLUSTER_API_VERSIONS.get('bootstrap-canonical-kubernetes')}",
                    "--control-plane",
                    f"canonical-kubernetes:{CLUSTER_API_VERSIONS.get('control-plane-canonical-kubernetes')}",
                    "--infrastructure",
                    f"openstack:{CLUSTER_API_VERSIONS.get('infrastructure-openstack')}",
                    "--addon",
                    f"helm:{CLUSTER_API_VERSIONS.get('addon-helm')}",
                    "--wait-provider-timeout",
                    str(PROVIDER_WAIT_TIMEOUT),
                    "--wait-providers",
                    "--kubeconfig",
                    kubeconfig_file.name,
                ]
            )
            subprocess.run(cmd, check=True, timeout=360, capture_output=True)

    def run(self, status: Status | None = None) -> Result:
        """Execute clusterctl init command."""
        # Install ORC CRDs. This is required for CAPO to be running, otherwise the
        # pod will be in crashloopbackoff
        # https://github.com/kubernetes-sigs/cluster-api-provider-openstack/blob/v0.12.4/docs/book/src/clusteropenstack/configuration.md#orc
        # https://github.com/kubernetes-sigs/cluster-api-provider-openstack/releases/tag/v0.12.0
        try:
            self._install_orc_crd()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            message = f"Error in installing CAPO ORC CRD: {str(e)}"
            return Result(ResultType.FAILED, message)

        # Initialize clusterctl
        # https://github.com/kubernetes-sigs/cluster-api/blob/v1.10.5/docs/book/src/clusterctl/commands/init.md
        # https://github.com/kubernetes-sigs/cluster-api/blob/v1.10.5/docs/book/src/clusterctl/commands/upgrade.md
        # Only micro version upgrades are supported and so always pass the specific
        # versions of the components to be upgraded. The reason being there can be
        # breaking changes in minor versions as well and so have to handle properly
        # case by case.
        try:
            self._initialize_or_upgrade_capi()
        except subprocess.CalledProcessError as e:
            message = f"Error in installing Cluster API components: {str(e)}"
            return Result(ResultType.FAILED, message)
        except subprocess.TimeoutExpired as e:
            message = f"Timed out initiating Cluster API components: {str(e)}"
            return Result(ResultType.FAILED, message)

        return Result(ResultType.COMPLETED)


class DeleteClusterAPI(BaseStep):
    """Delete Cluster API components."""

    def __init__(self, client: Client):
        super().__init__(
            "Delete Cluster API components",
            "Delete Cluster API components from management cluster",
        )
        self.client = client

    def _get_workload_clusters(self) -> list:
        return list(self.kube.list(K8SHelper.get_cluster_resource(), namespace="*"))

    def _delete_capi_components(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w") as kubeconfig_file:
            kubeconfig_file.write(yaml.safe_dump(self.kubeconfig))
            kubeconfig_file.flush()
            cmd = [
                "clusterctl",
                "delete",
                "--all",
                "--kubeconfig",
                kubeconfig_file.name,
            ]
            subprocess.run(cmd, check=True, timeout=300, capture_output=True)

    def _delete_capi_namespaces(self) -> None:
        capi_namespaces = [
            "caaph-system",
            "cabpck-system",
            "cacpck-system",
            "capo-system",
            "capi-system",
            "cert-manager",
            "orc-system",
        ]

        namespaces_in_cluster = self.kube.list(core_v1.Namespace)
        namespaces_in_cluster_list = [
            ns.metadata.name for ns in namespaces_in_cluster if ns.metadata
        ]
        LOG.debug(f"Namespaces in the management cluster: {namespaces_in_cluster_list}")

        namespaces_to_delete = set(capi_namespaces).intersection(
            namespaces_in_cluster_list
        )
        LOG.debug(
            f"Namespaces to delete in the management cluster: {namespaces_to_delete}"
        )

        for ns in namespaces_to_delete:
            LOG.debug(f"Deleting namespace {ns}")
            self.kube.delete(core_v1.Namespace, name=ns)

    def _delete_capi_crds(self) -> None:
        # Delete CAPI CRDs using selector label
        # lightkube client delete does not support labelSelector
        # so workaround by using client request
        # client request expects resource name for delete operation,
        # as a workaround pass empty string
        capi_label_selector = {"clusterctl.cluster.x-k8s.io": None}
        self.kube._client.request(
            "delete",
            res=apiextensions_v1.CustomResourceDefinition,
            name="",
            params={"labelSelector": selector.build_selector(capi_label_selector)},
        )

        # Delete ORC CRD, ignore if delete fails
        try:
            self.kube._client.request(
                "delete",
                res=apiextensions_v1.CustomResourceDefinition,
                name="images.openstack.k-orc.cloud",
            )
        except l_exceptions.ApiError as e:
            LOG.debug(f"Error in deleting ORC CRD: {str(e)}")

        # Delete Provider CRD, ignore if delete fails
        try:
            self.kube._client.request(
                "delete",
                res=apiextensions_v1.CustomResourceDefinition,
                name="providers.clusterctl.cluster.x-k8s.io",
            )
        except l_exceptions.ApiError as e:
            LOG.debug(f"Error in deleting provider CRD: {str(e)}")

    def _delete_orc_resources(self) -> None:
        orc_cluster_roles = [
            "orc-image-editor-role",
            "orc-image-viewer-role",
            "orc-manager-role",
            "orc-metrics-auth-role",
            "orc-metrics-reader",
        ]
        orc_cluster_role_bindings = [
            "orc-manager-rolebinding",
            "orc-metrics-auth-rolebinding",
        ]

        # Delete ORC ClusterRoles
        clusterroles_in_cluster = self.kube.list(rbac_authorization_v1.ClusterRole)
        clusterroles_in_cluster_list = [
            role.metadata.name for role in clusterroles_in_cluster if role.metadata
        ]
        clusterroles_to_delete = set(orc_cluster_roles).intersection(
            clusterroles_in_cluster_list
        )
        LOG.debug(
            "ClusterRoles to delete in the management cluster: "
            f"{clusterroles_to_delete}"
        )

        for clusterrole in clusterroles_to_delete:
            LOG.debug(f"Deleting ClusterRole {clusterrole}")
            self.kube.delete(rbac_authorization_v1.ClusterRole, clusterrole)

        # Delete ORC ClusterRoleBindings
        clusterrolebindings_in_cluster = self.kube.list(
            rbac_authorization_v1.ClusterRoleBinding
        )
        clusterrolebindings_in_cluster_list = [
            binding.metadata.name
            for binding in clusterrolebindings_in_cluster
            if binding.metadata
        ]
        clusterrolebindings_to_delete = set(orc_cluster_role_bindings).intersection(
            clusterrolebindings_in_cluster_list
        )
        LOG.debug(
            "ClusterRoleBindings to delete in the management cluster: "
            f"{clusterrolebindings_to_delete}"
        )

        for clusterrolebinding in clusterrolebindings_to_delete:
            LOG.debug(f"Deleting ClusterRoleBinding {clusterrolebinding}")
            self.kube.delete(
                rbac_authorization_v1.ClusterRoleBinding, clusterrolebinding
            )

    def run(self, status: Status | None = None) -> Result:
        """Delete Cluster API components."""
        try:
            self.kube = get_kube_client(self.client)
        except KubeClientError as e:
            LOG.debug("Failed to create k8s client", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        try:
            self.kubeconfig = read_config(self.client, K8SHelper.get_kubeconfig_key())
        except ConfigItemNotFoundException:
            LOG.debug("K8S kubeconfig not found", exc_info=True)
            return Result(ResultType.FAILED, "K8S kubeconfig not found")

        # 1. Error out if any workload clusters are still managed.
        try:
            clusters = self._get_workload_clusters()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                LOG.debug("No clusters exists")
            else:
                LOG.debug("Failed to retrieve clusters", exc_info=True)
                return Result(ResultType.FAILED, str(e))
        except l_exceptions.ApiError as e:
            if e.status.code == 404:
                LOG.debug("No clusters exists")
            else:
                LOG.debug("Failed to retrieve clusters", exc_info=True)
                return Result(ResultType.FAILED, str(e))

        if clusters:
            message = (
                f"Cannot delete Cluster API components as {len(clusters)} Workload "
                "cluster(s) are still managed."
            )
            return Result(ResultType.FAILED, message)

        # 2. Delete CAPI components using clusterctl
        # This step will delete CAPI, CAPO, CABPCK, CACPCK deployments
        try:
            self._delete_capi_components()
        except subprocess.CalledProcessError as e:
            LOG.debug(f"Error from clusterctl delete: {e.stderr}")
            # If CRDs are already deleted, the command results in following error
            # Error: failed to check Cluster API version:
            # customresourcedefinitions.apiextensions.k8s.io "clusters.cluster.x-k8s.io"
            # not found
            if "not found" not in str(e.stderr):
                LOG.debug("Error in deleting capi components", exc_info=True)
                message = f"Error in deleting Cluster API components: {str(e)}"
                return Result(ResultType.FAILED, message)
            else:
                LOG.debug("CRDs already deleted and so ignore clusterctl delete error")
        except subprocess.TimeoutExpired as e:
            message = f"Timed out deleting Cluster API components: {str(e)}"
            return Result(ResultType.FAILED, message)

        # 3. Delete Cluster API namespaces
        # This also deletes any cert-manager resources and ORC resources
        try:
            self._delete_capi_namespaces()
        except l_exceptions.ApiError as e:
            LOG.debug("Failed to delete capi namespace", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        # 4. Delete Cluster API CRDs
        try:
            self._delete_capi_crds()
        except l_exceptions.ApiError as e:
            LOG.debug("Failed to delete capi crds", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        # 5. Delete ORC resources
        # Not only ClusterRoles and ClusterRoleBindings need to be deleted
        # Rest of the resources are deleted during namespace deletion stage
        try:
            self._delete_orc_resources()
        except l_exceptions.ApiError as e:
            LOG.debug("Failed to delete orc resources", exc_info=True)
            return Result(ResultType.FAILED, str(e))

        return Result(ResultType.COMPLETED)


class CaasFeature(OpenStackControlPlaneFeature):
    version = Version("0.0.1")
    requires = {
        FeatureRequirement("secrets"),
        FeatureRequirement("loadbalancer", optional=True),
    }

    name = "caas"
    risk_availability = RiskLevel.BETA

    tf_plan_location = TerraformPlanLocation.SUNBEAM_TERRAFORM_REPO
    configure_plan = "caas-setup"

    def config_type(self) -> type | None:
        """Feature configuration model."""
        return CaasConfig

    def default_software_overrides(self) -> SoftwareConfig:
        """Feature software configuration."""
        return SoftwareConfig(
            charms={"magnum-k8s": CharmManifest(channel=OPENSTACK_CHANNEL)},
            terraform={
                self.configure_plan: TerraformManifest(
                    source=Path(__file__).parent / "etc" / self.configure_plan
                ),
            },
        )

    def manifest_attributes_tfvar_map(self) -> dict:
        """Manifest attributes terraformvars map."""
        return {
            self.tfplan: {
                "charms": {
                    "magnum-k8s": {
                        "channel": "magnum-channel",
                        "revision": "magnum-revision",
                        "config": "magnum-config",
                    }
                }
            },
            self.configure_plan: {
                "caas_config": {
                    "image_name": "image-name",
                    "image_url": "image-source-url",
                    "container_format": "image-container-format",
                    "disk_format": "image-disk-format",
                    "properties": "image-properties",
                }
            },
        }

    def set_application_names(self, deployment: Deployment) -> list:
        """Application names handled by the terraform plan."""
        apps = ["magnum", "magnum-mysql-router"]
        if self.get_database_topology(deployment) == "multi":
            apps.extend(["magnum-mysql"])

        return apps

    def set_tfvars_on_enable(
        self, deployment: Deployment, config: FeatureConfig
    ) -> dict:
        """Set terraform variables to enable the application."""
        return {
            "enable-magnum": True,
            **self.add_horizon_plugin_to_tfvars(deployment, "magnum"),
        }

    def set_tfvars_on_disable(self, deployment: Deployment) -> dict:
        """Set terraform variables to disable the application."""
        return {
            "enable-magnum": False,
            **self.remove_horizon_plugin_from_tfvars(deployment, "magnum"),
        }

    def set_tfvars_on_resize(
        self, deployment: Deployment, config: FeatureConfig
    ) -> dict:
        """Set terraform variables to resize the application."""
        return {}

    def get_database_charm_processes(self) -> dict[str, dict[str, int]]:
        """Returns the database processes accessing this service."""
        return {
            "magnum": {"magnum-k8s": 10},
        }

    @click.command()
    @click_option_show_hints
    @pass_method_obj
    def enable_cmd(self, deployment: Deployment, show_hints: bool) -> None:
        """Enable Container as a Service feature."""
        self.enable_feature(deployment, CaasConfig(), show_hints)

    @click.command()
    @click_option_show_hints
    @pass_method_obj
    def disable_cmd(self, deployment: Deployment, show_hints: bool) -> None:
        """Disable Container as a Service feature."""
        self.disable_feature(deployment, show_hints)

    @click.command()
    @click_option_show_hints
    @pass_method_obj
    def configure(self, deployment: Deployment, show_hints: bool) -> None:
        """Configure Cloud for Container as a Service use."""
        jhelper = JujuHelper(deployment.juju_controller)
        admin_credentials = retrieve_admin_credentials(jhelper, OPENSTACK_MODEL)

        tfhelper = deployment.get_tfhelper(self.configure_plan)
        tfhelper.env = (tfhelper.env or {}) | admin_credentials
        plan = [
            TerraformInitStep(tfhelper),
            CaasConfigureStep(
                deployment.get_client(),
                tfhelper,
                self.manifest,
                self.manifest_attributes_tfvar_map(),
            ),
        ]

        run_plan(plan, console, show_hints)

    def enabled_commands(self) -> dict[str, list[dict]]:
        """Dict of clickgroup along with commands.

        Return the commands available once the feature is enabled.
        """
        return {
            "configure": [{"name": "caas", "command": self.configure}],
        }

    def run_enable_plans(
        self, deployment: Deployment, config: ConfigType, show_hints: bool
    ) -> None:
        """Run plans to enable feature."""
        tfhelper = deployment.get_tfhelper(self.tfplan)
        jhelper = JujuHelper(deployment.juju_controller)

        plan: list[BaseStep] = []
        if self.user_manifest:
            plan.append(AddManifestStep(deployment.get_client(), self.user_manifest))
        plan.extend(
            [
                SetupClusterAPI(
                    deployment.get_client(),
                    jhelper,
                    self.snap,
                    deployment.openstack_machines_model,
                ),
                TerraformInitStep(deployment.get_tfhelper(self.tfplan)),
                EnableOpenStackApplicationStep(
                    deployment, config, tfhelper, jhelper, self
                ),
            ]
        )

        run_plan(plan, console, show_hints)
        click.echo(f"OpenStack {self.display_name} application enabled.")

    def run_disable_plans(self, deployment: Deployment, show_hints: bool) -> None:
        """Run plans to disable the feature."""
        tfhelper = deployment.get_tfhelper(self.tfplan)
        jhelper = JujuHelper(deployment.juju_controller)
        plan = [
            DeleteClusterAPI(deployment.get_client()),
            TerraformInitStep(tfhelper),
            DisableOpenStackApplicationStep(deployment, tfhelper, jhelper, self),
        ]

        run_plan(plan, console, show_hints)
        click.echo(f"OpenStack {self.display_name} application disabled.")
