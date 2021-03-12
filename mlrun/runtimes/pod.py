# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import typing
import uuid
from copy import deepcopy

from kfp.dsl import ContainerOp
from kubernetes import client

import mlrun.utils.regex

from ..utils import logger, normalize_name, update_in, verify_field_regex
from .base import BaseRuntime, FunctionSpec
from .utils import (
    apply_kfp,
    generate_resources,
    get_item_name,
    get_resource_labels,
    set_named_item,
)


class KubeResourceSpec(FunctionSpec):
    def __init__(
        self,
        command=None,
        args=None,
        image=None,
        mode=None,
        volumes=None,
        volume_mounts=None,
        env=None,
        resources=None,
        default_handler=None,
        entry_points=None,
        description=None,
        workdir=None,
        replicas=None,
        image_pull_policy=None,
        service_account=None,
        build=None,
        image_pull_secret=None,
        node_name=None,
        node_selector=None,
        affinity=None,
    ):
        super().__init__(
            command=command,
            args=args,
            image=image,
            mode=mode,
            build=build,
            entry_points=entry_points,
            description=description,
            workdir=workdir,
            default_handler=default_handler,
        )
        self._volumes = {}
        self._volume_mounts = {}
        self.volumes = volumes or []
        self.volume_mounts = volume_mounts or []
        self.env = env or []
        self.resources = resources or {}
        self.replicas = replicas
        self.image_pull_policy = image_pull_policy
        self.service_account = service_account
        self.image_pull_secret = image_pull_secret
        self.node_name = node_name
        self.node_selector = node_selector
        self.affinity = affinity

    @property
    def volumes(self) -> list:
        return list(self._volumes.values())

    @volumes.setter
    def volumes(self, volumes):
        self._volumes = {}
        if volumes:
            for vol in volumes:
                set_named_item(self._volumes, vol)

    @property
    def volume_mounts(self) -> list:
        return list(self._volume_mounts.values())

    @volume_mounts.setter
    def volume_mounts(self, volume_mounts):
        self._volume_mounts = {}
        if volume_mounts:
            for volume_mount in volume_mounts:
                self._set_volume_mount(volume_mount)

    def update_vols_and_mounts(self, volumes, volume_mounts):
        if volumes:
            for vol in volumes:
                set_named_item(self._volumes, vol)

        if volume_mounts:
            for volume_mount in volume_mounts:
                self._set_volume_mount(volume_mount)

    def _get_affinity_as_k8s_class_instance(self):
        api = client.ApiClient()
        affinity = self.affinity
        if isinstance(self.affinity, dict):
            affinity = api.__deserialize(self.affinity, "V1Affinity")
        return affinity

    def _get_sanitized_affinity(self):
        """
        When using methods like to_dict() on kubernetes class instances we're getting the attributes in snake_case
        Which is ok if we're using the kubernetes python package but not if for example we're creating CRDs that we
        apply directly. For that we need the sanitized (CamelCase) version
        """
        api = client.ApiClient()
        # When the client send the function to the API it already serialized it by using to_dict() which uses snake_case
        # So we need to first turn it back into a k8s class instance
        affinity = self._get_affinity_as_k8s_class_instance()
        return api.sanitize_for_serialization(affinity)

    def _set_volume_mount(self, volume_mount):
        # calculate volume mount hash
        volume_name = get_item_name(volume_mount, "name")
        volume_sub_path = get_item_name(volume_mount, "subPath")
        volume_mount_path = get_item_name(volume_mount, "mountPath")
        volume_mount_key = hash(f"{volume_name}-{volume_sub_path}-{volume_mount_path}")
        self._volume_mounts[volume_mount_key] = volume_mount


class KubeResource(BaseRuntime):
    kind = "job"
    _is_nested = True

    def __init__(self, spec=None, metadata=None):
        super().__init__(metadata, spec)
        self._cop = ContainerOp("name", "image")
        self.verbose = False

    @property
    def spec(self) -> KubeResourceSpec:
        return self._spec

    @spec.setter
    def spec(self, spec):
        self._spec = self._verify_dict(spec, "spec", KubeResourceSpec)

    def to_dict(self, fields=None, exclude=None, strip=False):
        struct = super().to_dict(fields, exclude, strip=strip)
        api = client.ApiClient()
        struct = api.sanitize_for_serialization(struct)
        if strip:
            spec = struct["spec"]
            for attr in ["volumes", "volume_mounts"]:
                if attr in spec:
                    del spec[attr]
            if "env" in spec and spec["env"]:
                for ev in spec["env"]:
                    if ev["name"].startswith("V3IO_"):
                        ev["value"] = ""
        return struct

    def apply(self, modify):
        return apply_kfp(modify, self._cop, self)

    def set_env_from_secret(self, name, secret=None, secret_key=None):
        """set pod environment var from secret"""
        secret_key = secret_key or name
        value_from = client.V1EnvVarSource(
            secret_key_ref=client.V1SecretKeySelector(name=secret, key=secret_key)
        )
        return self._set_env(name, value_from=value_from)

    def set_env(self, name, value):
        """set pod environment var from value"""
        return self._set_env(name, value=str(value))

    def _set_env(self, name, value=None, value_from=None):
        new_var = client.V1EnvVar(name=name, value=value, value_from=value_from)
        i = 0
        for v in self.spec.env:
            if get_item_name(v) == name:
                self.spec.env[i] = new_var
                return self
            i += 1
        self.spec.env.append(new_var)
        return self

    def set_envs(self, env_vars):
        """set pod environment var key/value dict"""
        for name, value in env_vars.items():
            self.set_env(name, value)
        return self

    def gpus(self, gpus, gpu_type="nvidia.com/gpu"):
        update_in(self.spec.resources, ["limits", gpu_type], gpus)

    def with_limits(self, mem=None, cpu=None, gpus=None, gpu_type="nvidia.com/gpu"):
        """set pod cpu/memory/gpu limits"""
        if mem:
            verify_field_regex(
                "function.limits.memory",
                mem,
                mlrun.utils.regex.k8s_resource_quantity_regex,
            )
        if cpu:
            verify_field_regex(
                "function.limits.cpu",
                cpu,
                mlrun.utils.regex.k8s_resource_quantity_regex,
            )
        if gpus:
            verify_field_regex(
                "function.limits.gpus",
                gpus,
                mlrun.utils.regex.k8s_resource_quantity_regex,
            )
        update_in(
            self.spec.resources,
            "limits",
            generate_resources(mem=mem, cpu=cpu, gpus=gpus, gpu_type=gpu_type),
        )

    def with_requests(self, mem=None, cpu=None):
        """set requested (desired) pod cpu/memory/gpu resources"""
        if mem:
            verify_field_regex(
                "function.requests.memory",
                mem,
                mlrun.utils.regex.k8s_resource_quantity_regex,
            )
        if cpu:
            verify_field_regex(
                "function.requests.cpu",
                cpu,
                mlrun.utils.regex.k8s_resource_quantity_regex,
            )
        update_in(
            self.spec.resources, "requests", generate_resources(mem=mem, cpu=cpu),
        )

    def with_node_name(self, node_name: str = None):
        """set node_name to schedule the pod on"""
        if node_name:
            self.spec.node_name = node_name

    def with_node_selector(self, node_selector: typing.Dict[str, str] = None):
        """set node_name to schedule the pod on"""
        if node_selector:
            self.spec.node_selector = node_selector

    def with_affinity(self, affinity: client.V1Affinity = None):
        """set node_name to schedule the pod on"""
        if affinity:
            self.spec.affinity = affinity

    def _get_meta(self, runobj, unique=False):
        namespace = self._get_k8s().resolve_namespace()

        labels = get_resource_labels(self, runobj, runobj.spec.scrape_metrics)
        new_meta = client.V1ObjectMeta(namespace=namespace, labels=labels)

        name = runobj.metadata.name or "mlrun"
        norm_name = f"{normalize_name(name)}-"
        if unique:
            norm_name += uuid.uuid4().hex[:8]
            new_meta.name = norm_name
            runobj.set_label("mlrun/job", norm_name)
        else:
            new_meta.generate_name = norm_name
        return new_meta

    def copy(self):
        self._cop = None
        fn = deepcopy(self)
        self._cop = ContainerOp("name", "image")
        fn._cop = ContainerOp("name", "image")
        return fn

    def _add_vault_params_to_spec(self, runobj=None, project=None):
        from ..config import config as mlconf

        project_name = project or runobj.metadata.project
        if project_name is None:
            logger.warning("No project provided. Cannot add vault parameters")
            return

        service_account_name = mlconf.secret_stores.vault.project_service_account_name.format(
            project=project_name
        )

        project_vault_secret_name = self._get_k8s().get_project_vault_secret_name(
            project_name, service_account_name
        )
        if project_vault_secret_name is None:
            logger.info(f"No vault secret associated with project {project_name}")
            return

        volumes = [
            {
                "name": "vault-secret",
                "secret": {"defaultMode": 420, "secretName": project_vault_secret_name},
            }
        ]
        # We cannot use expanduser() here, since the user in question is the user running in the pod
        # itself (which is root) and not where this code is running. That's why this hacky replacement is needed.
        token_path = mlconf.secret_stores.vault.token_path.replace("~", "/root")

        volume_mounts = [{"name": "vault-secret", "mountPath": token_path}]

        self.spec.update_vols_and_mounts(volumes, volume_mounts)
        self.spec.env.append(
            {
                "name": "MLRUN_SECRET_STORES__VAULT__ROLE",
                "value": f"project:{project_name}",
            }
        )
        # In case remote URL is different than local URL, use it. Else, use the local URL
        vault_url = mlconf.secret_stores.vault.remote_url
        if vault_url == "":
            vault_url = mlconf.secret_stores.vault.url

        self.spec.env.append(
            {"name": "MLRUN_SECRET_STORES__VAULT__URL", "value": vault_url}
        )


def kube_resource_spec_to_pod_spec(
    kube_resource_spec: KubeResourceSpec, container: client.V1Container
):
    if kube_resource_spec.affinity and isinstance(kube_resource_spec.affinity, dict):
        affinity = kube_resource_spec._get_affinity_as_k8s_class_instance()
    elif kube_resource_spec.affinity and isinstance(
        kube_resource_spec.affinity, client.V1Affinity
    ):
        affinity = kube_resource_spec.affinity
    else:
        affinity = None
    return client.V1PodSpec(
        containers=[container],
        restart_policy="Never",
        volumes=kube_resource_spec.volumes,
        service_account=kube_resource_spec.service_account,
        node_name=kube_resource_spec.node_name,
        node_selector=kube_resource_spec.node_selector,
        affinity=affinity,
    )
