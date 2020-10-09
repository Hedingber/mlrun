import unittest.mock
from typing import List, Dict

from sqlalchemy.orm import Session

import mlrun.api.crud as crud
from mlrun.api.constants import LogSources
from mlrun.api.utils.singletons.db import get_db
from mlrun.api.utils.singletons.k8s import get_k8s
from mlrun.runtimes import get_runtime_handler
from mlrun.utils import create_logger
from tests.conftest import DictToK8sObjectWrapper

logger = create_logger(level="debug", name="test-runtime-handlers")


class TestRuntimeHandlerBase:
    def setup_method(self, method):
        self._logger = logger
        self._logger.info(
            f"Setting up test {self.__class__.__name__}::{method.__name__}"
        )

        self.project = "test_project"
        self.run_uid = "test_run_uid"

        self.custom_setup()

        self._logger.info(
            f"Finished setting up test {self.__class__.__name__}::{method.__name__}"
        )

    def teardown_method(self, method):
        self._logger.info(
            f"Tearing down test {self.__class__.__name__}::{method.__name__}"
        )

        self.custom_teardown()

        self._logger.info(
            f"Finished tearing down test {self.__class__.__name__}::{method.__name__}"
        )

    def custom_setup(self):
        pass

    def custom_teardown(self):
        pass

    @staticmethod
    def _assert_runtime_handler_list_resources(
        runtime_kind, expected_crds=None, expected_pods=None, expected_services=None,
    ):
        runtime_handler = get_runtime_handler(runtime_kind)
        resources = runtime_handler.list_resources()
        crd_group, crd_version, crd_plural = runtime_handler._get_crd_info()
        get_k8s().v1api.list_namespaced_pod.assert_called_once_with(
            get_k8s().resolve_namespace(),
            label_selector=runtime_handler._get_default_label_selector(),
        )
        if expected_crds:
            get_k8s().crdapi.list_namespaced_custom_object.assert_called_once_with(
                crd_group,
                crd_version,
                get_k8s().resolve_namespace(),
                crd_plural,
                label_selector=runtime_handler._get_default_label_selector(),
            )
        if expected_services:
            get_k8s().v1api.list_namespaced_service.assert_called_once_with(
                get_k8s().resolve_namespace(),
                label_selector=runtime_handler._get_default_label_selector(),
            )
        TestRuntimeHandlerBase._assert_list_resources_response(
            resources,
            expected_crds=expected_crds,
            expected_pods=expected_pods,
            expected_services=expected_services,
        )

    @staticmethod
    def _assert_list_resources_response(
        resources, expected_crds=None, expected_pods=None, expected_services=None
    ):
        expected_crds = expected_crds or []
        expected_pods = expected_pods or []
        expected_services = expected_services or []
        assert len(resources["crd_resources"]) == len(expected_crds)
        for index, crd in enumerate(expected_crds):
            assert resources["crd_resources"][index]["name"] == crd["metadata"]["name"]
            assert (
                resources["crd_resources"][index]["labels"] == crd["metadata"]["labels"]
            )
            assert resources["crd_resources"][index]["status"] == crd["status"]
        assert len(resources["pod_resources"]) == len(expected_pods)
        for index, pod in enumerate(expected_pods):
            pod_dict = pod.to_dict()
            assert (
                resources["pod_resources"][index]["name"]
                == pod_dict["metadata"]["name"]
            )
            assert (
                resources["pod_resources"][index]["labels"]
                == pod_dict["metadata"]["labels"]
            )
            assert resources["pod_resources"][index]["status"] == pod_dict["status"]
        if expected_services:
            assert len(resources["service_resources"]) == len(expected_services)
            for index, service in enumerate(expected_services):
                assert (
                    resources["service_resources"][index]["name"]
                    == service.metadata.name
                )
                assert (
                    resources["service_resources"][index]["labels"]
                    == service.metadata.labels
                )

    @staticmethod
    def _mock_list_namespaces_pods(pod_dicts_call_responses: List[List[Dict]]):
        calls = []
        for pod_dicts_call_response in pod_dicts_call_responses:
            pods = []
            for pod_dict in pod_dicts_call_response:
                pod = DictToK8sObjectWrapper(pod_dict)
                pods.append(pod)
            calls.append(DictToK8sObjectWrapper({"items": pods}))
        get_k8s().v1api.list_namespaced_pod = unittest.mock.Mock(side_effect=calls)
        return calls

    @staticmethod
    def _mock_list_namespaced_crds(crd_dicts_call_responses: List[List[Dict]]):
        calls = []
        for crd_dicts_call_response in crd_dicts_call_responses:
            calls.append({"items": crd_dicts_call_response})
        get_k8s().crdapi.list_namespaced_custom_object = unittest.mock.Mock(
            side_effect=calls
        )
        return calls

    @staticmethod
    def _mock_list_services(service_dicts):
        service_mocks = []
        for service_dict in service_dicts:
            service_mock = unittest.mock.Mock()
            service_mock.metadata.name.return_value = service_dict["metadata"]["name"]
            service_mock.metadata.labels.return_value = service_dict["metadata"][
                "labels"
            ]
            service_mocks.append(service_mock)
        services_mock = unittest.mock.Mock()
        services_mock.items = service_mocks
        get_k8s().v1api.list_namespaced_service = unittest.mock.Mock(
            return_value=services_mock
        )
        return service_mocks

    @staticmethod
    def _assert_list_namespaced_pods_calls(
        expected_number_of_calls: int, expected_label_selector: str
    ):
        assert (
            get_k8s().v1api.list_namespaced_pod.call_count == expected_number_of_calls
        )
        get_k8s().v1api.list_namespaced_pod.assert_any_call(
            get_k8s().resolve_namespace(), label_selector=expected_label_selector
        )

    @staticmethod
    def _assert_list_namespaced_crds_calls(
        runtime_handler, expected_number_of_calls: int, expected_label_selector: str
    ):
        crd_group, crd_version, crd_plural = runtime_handler._get_crd_info()
        assert (
            get_k8s().crdapi.list_namespaced_custom_object.call_count
            == expected_number_of_calls
        )
        get_k8s().crdapi.list_namespaced_custom_object.assert_any_call(
            crd_group,
            crd_version,
            get_k8s().resolve_namespace(),
            crd_plural,
            label_selector=expected_label_selector,
        )

    @staticmethod
    def _assert_run_logs(
        db: Session,
        project: str,
        uid: str,
        expected_log: str,
        logger_pod_name: str = None,
    ):
        if logger_pod_name is not None:
            get_k8s().v1api.read_namespaced_pod_log.assert_called_once_with(
                name=logger_pod_name, namespace=get_k8s().resolve_namespace(),
            )
        _, log = crud.Logs.get_logs(db, project, uid, source=LogSources.PERSISTENCY)
        assert log == expected_log.encode()

    @staticmethod
    def _assert_run_reached_state(
        db: Session, project: str, uid: str, expected_state: str
    ):
        run = get_db().read_run(db, uid, project)
        assert run["status"]["state"] == expected_state
