# SPDX-FileCopyrightText: 2025 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

"""Tests for the control-plane upgrade handler."""

from unittest.mock import MagicMock

import pytest

from sunbeam.clusterd.models import AcquireUpgradeLockResponse
from sunbeam.core.terraform import TerraformException
from sunbeam.upgrades.control_plane.groups import (
    ControlPlaneHandler,
    _terraform_targets_for_charms,
)
from sunbeam.upgrades.coordinator import ReleaseUpgradeCoordinator
from sunbeam.upgrades.errors import UpgradeErrorCode
from sunbeam.upgrades.metadata import HopMetadata
from sunbeam.upgrades.state import Group, HopStatus, PhaseStatus, UpgradeState

FROM = "2025.1"
TO = "2026.1"

_TF_TARGETS = {
    "keystone-k8s": ["module.keystone"],
    "glance-k8s": ["module.glance"],
    "ceilometer-k8s": ["juju_application.ceilometer"],
    "heat-k8s": ["module.heat"],
}


class TestTerraformTargetsForCharms:
    """Tests for _terraform_targets_for_charms."""

    def test_single_charm(self):
        targets = _terraform_targets_for_charms(["keystone-k8s"], _TF_TARGETS)
        assert targets == ["-target=module.keystone"]

    def test_ceilometer_maps_to_juju_application(self):
        targets = _terraform_targets_for_charms(["ceilometer-k8s"], _TF_TARGETS)
        assert targets == ["-target=juju_application.ceilometer"]

    def test_multiple_charms(self):
        targets = _terraform_targets_for_charms(
            ["keystone-k8s", "glance-k8s", "ceilometer-k8s"], _TF_TARGETS
        )
        assert targets == [
            "-target=module.keystone",
            "-target=module.glance",
            "-target=juju_application.ceilometer",
        ]

    def test_empty_list(self):
        targets = _terraform_targets_for_charms([], _TF_TARGETS)
        assert targets == []

    def test_unknown_charm_raises_keyerror(self):
        with pytest.raises(KeyError):
            _terraform_targets_for_charms(["unknown-k8s"], _TF_TARGETS)


def _make_metadata(groups=None):
    if groups is None:
        groups = [
            {
                "name": "identity-core",
                "apps": ["keystone-k8s"],
                "ready_timeout_sec": 600,
                "terraform_targets": {"keystone-k8s": ["module.keystone"]},
            },
            {
                "name": "image",
                "apps": ["glance-k8s"],
                "ready_timeout_sec": 600,
                "terraform_targets": {"glance-k8s": ["module.glance"]},
            },
        ]
    return HopMetadata.model_validate(
        {
            "from": FROM,
            "to": TO,
            "control_plane_groups": groups,
        }
    )


def _make_state():
    state = UpgradeState()
    state.hop_history.append(
        type(
            "Hop",
            (),
            {
                "from": FROM,
                "to": TO,
                "status": HopStatus.IN_PROGRESS,
                "phase": "control_plane",
                "phases": state.__class__().phases,
            },
        )()
    )
    return state


@pytest.fixture
def mock_deployment():
    deployment = MagicMock()
    tfhelper = MagicMock()
    manifest = MagicMock()
    jhelper = MagicMock()
    client = MagicMock()
    client.cluster.acquire_upgrade_lock.return_value = AcquireUpgradeLockResponse(
        token=1
    )
    deployment.get_tfhelper.return_value = tfhelper
    deployment.get_manifest.return_value = manifest
    deployment.get_juju_helper.return_value = jhelper
    deployment.get_client.return_value = client
    return deployment


@pytest.fixture
def coordinator(mock_deployment):
    client = mock_deployment.get_client.return_value
    client.cluster.get_upgrade_state.return_value = None
    coord = ReleaseUpgradeCoordinator(client)
    coord.acquire_lock()
    coord.load_state()
    # Create a hop manually
    hop = coord.create_hop(FROM, TO, "123")
    hop.status = HopStatus.IN_PROGRESS
    hop.phase = "control_plane"
    coord.persist_state()
    return coord


class TestControlPlaneHandler:
    def test_all_groups_succeed(self, mock_deployment, coordinator):
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        result = handler.run(coordinator, metadata, state)

        assert result.success is True
        cp = state.current_hop.phases.control_plane
        assert cp.groups["identity-core"].status == PhaseStatus.COMPLETED
        assert cp.groups["image"].status == PhaseStatus.COMPLETED

    def test_terraform_apply_called_per_group(self, mock_deployment, coordinator):
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        handler.run(coordinator, metadata, state)

        tfhelper = mock_deployment.get_tfhelper.return_value
        assert tfhelper.update_partial_tfvars_and_apply_tf.call_count == 2

    def test_convergence_wait_called_per_group(self, mock_deployment, coordinator):
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        handler.run(coordinator, metadata, state)

        jhelper = mock_deployment.get_juju_helper.return_value
        assert jhelper.wait_until_desired_status.call_count == 2

    def test_terraform_failure_fails_group(self, mock_deployment, coordinator):
        tfhelper = mock_deployment.get_tfhelper.return_value
        tfhelper.update_partial_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed"
        )
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        result = handler.run(coordinator, metadata, state)

        assert result.success is False
        assert result.error_code == UpgradeErrorCode.CONTROL_PLANE_APPLY_FAILED
        cp = state.current_hop.phases.control_plane
        assert cp.groups["identity-core"].status == PhaseStatus.FAILED
        # Second group not attempted
        assert "image" not in cp.groups

    def test_convergence_timeout_fails_group(self, mock_deployment, coordinator):
        jhelper = mock_deployment.get_juju_helper.return_value
        jhelper.wait_until_desired_status.side_effect = TimeoutError("timed out")
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        result = handler.run(coordinator, metadata, state)

        assert result.success is False
        assert result.error_code == UpgradeErrorCode.CONTROL_PLANE_CONVERGENCE_TIMEOUT
        cp = state.current_hop.phases.control_plane
        assert cp.groups["identity-core"].status == PhaseStatus.FAILED

    def test_skips_completed_groups_on_resume(self, mock_deployment, coordinator):
        metadata = _make_metadata()
        state = coordinator.state

        # Mark first group as completed
        handler = ControlPlaneHandler(mock_deployment)
        cp = state.current_hop.phases.control_plane
        cp.groups["identity-core"] = Group(
            status=PhaseStatus.COMPLETED,
            started_at="2025-01-01T00:00:00Z",
            completed_at="2025-01-01T00:01:00Z",
        )

        result = handler.run(coordinator, metadata, state)

        assert result.success is True
        tfhelper = mock_deployment.get_tfhelper.return_value
        # Only image group should be upgraded
        assert tfhelper.update_partial_tfvars_and_apply_tf.call_count == 1

    def test_no_metadata_returns_failure(self, mock_deployment, coordinator):
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        result = handler.run(coordinator, None, state)

        assert result.success is False
        assert result.error_code == UpgradeErrorCode.METADATA_MISSING

    def test_persists_state_after_each_group(self, mock_deployment, coordinator):
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        handler.run(coordinator, metadata, state)

        client = mock_deployment.get_client.return_value
        # 2 groups × (start persist + complete persist) = 4 calls minimum
        # plus initial state creation
        assert client.cluster.update_upgrade_state.call_count >= 4

    def test_group_state_has_timestamps(self, mock_deployment, coordinator):
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        handler.run(coordinator, metadata, state)

        cp = state.current_hop.phases.control_plane
        group = cp.groups["identity-core"]
        assert group.started_at is not None
        assert group.completed_at is not None

    def test_failed_group_has_last_error(self, mock_deployment, coordinator):
        tfhelper = mock_deployment.get_tfhelper.return_value
        tfhelper.update_partial_tfvars_and_apply_tf.side_effect = TerraformException(
            "apply failed"
        )
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        handler.run(coordinator, metadata, state)

        cp = state.current_hop.phases.control_plane
        group = cp.groups["identity-core"]
        assert group.last_error is not None
        assert (
            group.last_error.code == UpgradeErrorCode.CONTROL_PLANE_APPLY_FAILED.value
        )

    def test_apply_passes_target_args(self, mock_deployment, coordinator):
        """Verify -target args are passed to terraform apply per group."""
        metadata = _make_metadata()
        state = coordinator.state
        handler = ControlPlaneHandler(mock_deployment)

        handler.run(coordinator, metadata, state)

        tfhelper = mock_deployment.get_tfhelper.return_value
        assert tfhelper.update_partial_tfvars_and_apply_tf.call_count == 2
        # First call: identity-core group with keystone-k8s
        first_call = tfhelper.update_partial_tfvars_and_apply_tf.call_args_list[0]
        assert first_call.kwargs["tf_apply_extra_args"] == ["-target=module.keystone"]
        # Second call: image group with glance-k8s
        second_call = tfhelper.update_partial_tfvars_and_apply_tf.call_args_list[1]
        assert second_call.kwargs["tf_apply_extra_args"] == ["-target=module.glance"]

    def test_run_application_passes_target_args(self, mock_deployment, coordinator):
        """Verify -target args are passed when upgrading a single app."""
        metadata = _make_metadata()
        handler = ControlPlaneHandler(mock_deployment)

        handler.run_application(coordinator, metadata, "keystone-k8s")

        tfhelper = mock_deployment.get_tfhelper.return_value
        call = tfhelper.update_partial_tfvars_and_apply_tf.call_args
        assert call.kwargs["tf_apply_extra_args"] == ["-target=module.keystone"]

    def test_plan_group_passes_target_args(self, mock_deployment):
        """Verify -target args are passed to terraform plan in dry-run."""
        metadata = _make_metadata()
        handler = ControlPlaneHandler(mock_deployment)
        group_meta = metadata.control_plane_groups[0]

        handler.plan_group(group_meta)

        tfhelper = mock_deployment.get_tfhelper.return_value
        plan_call = tfhelper.update_partial_tfvars_and_plan_tf.call_args
        assert plan_call.kwargs["tf_plan_extra_args"] == ["-target=module.keystone"]
        text_call = tfhelper.terraform_plan_text.call_args
        assert text_call.kwargs["extra_args"] == ["-target=module.keystone"]
