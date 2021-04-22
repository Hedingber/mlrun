import copy
import datetime
import http
import json
import typing

import requests.adapters
import urllib3

import mlrun.api.schemas
import mlrun.api.utils.projects.remotes.member
import mlrun.errors
import mlrun.utils.helpers
import mlrun.utils.singleton
from mlrun.utils import logger


class Client(metaclass=mlrun.utils.singleton.Singleton,):
    def __init__(self) -> None:
        super().__init__()
        http_adapter = requests.adapters.HTTPAdapter(
            max_retries=urllib3.util.retry.Retry(total=3, backoff_factor=1)
        )
        self._session = requests.Session()
        self._session.mount("http://", http_adapter)
        self._api_url = mlrun.mlconf.iguazio_api_url
        self._wait_for_job_completion_retry_interval = 5

    def try_get_grafana_service_url(self, session_cookie: str) -> typing.Optional[str]:
        """
        Try to find a ready grafana app service, and return its URL
        If nothing found, returns None
        """
        logger.debug("Getting grafana service url from Iguazio")
        response = self._send_request_to_api(
            "GET", "app_services_manifests", session_cookie
        )
        response_body = response.json()
        for app_services_manifest in response_body.get("data", []):
            for app_service in app_services_manifest.get("attributes", {}).get(
                "app_services", []
            ):
                if (
                    app_service.get("spec", {}).get("kind") == "grafana"
                    and app_service.get("status", {}).get("state") == "ready"
                    and len(app_service.get("status", {}).get("urls", [])) > 0
                ):
                    url_kind_to_url = {}
                    for url in app_service["status"]["urls"]:
                        url_kind_to_url[url["kind"]] = url["url"]
                    # precedence for https
                    for kind in ["https", "http"]:
                        if kind in url_kind_to_url:
                            return url_kind_to_url[kind]
        return None

    def create_project(
        self, session_cookie: str, project: mlrun.api.schemas.Project
    ) -> mlrun.api.schemas.Project:
        logger.debug("Creating project in Iguazio", project=project)
        body = self._generate_request_body(project)
        return self._post_project_to_iguazio(session_cookie, body)

    def store_project(
        self, session_cookie: str, name: str, project: mlrun.api.schemas.Project,
    ) -> mlrun.api.schemas.Project:
        logger.debug("Storing project in Iguazio", name=name, project=project)
        body = self._generate_request_body(project)
        try:
            self._get_project_from_iguazio(session_cookie, name)
        except requests.HTTPError as exc:
            if exc.response.status_code != http.HTTPStatus.NOT_FOUND.value:
                raise
            return self._post_project_to_iguazio(session_cookie, body)
        else:
            return self._put_project_to_iguazio(session_cookie, name, body)

    def delete_project(
        self,
        session_cookie: str,
        name: str,
        deletion_strategy: mlrun.api.schemas.DeletionStrategy = mlrun.api.schemas.DeletionStrategy.default(),
    ):
        logger.debug(
            "Deleting project in Iguazio",
            name=name,
            deletion_strategy=deletion_strategy,
        )
        body = self._generate_request_body(
            mlrun.api.schemas.Project(
                metadata=mlrun.api.schemas.ProjectMetadata(name=name)
            )
        )
        # TODO: verify header name and values
        headers = {
            "x-iguazio-delete-project-strategy": deletion_strategy.to_nuclio_deletion_strategy(),
        }
        try:
            response = self._send_request_to_api(
                "DELETE", "projects", session_cookie, headers=headers, json=body
            )
        except requests.HTTPError as exc:
            if exc.response.status_code != http.HTTPStatus.NOT_FOUND.value:
                raise
            logger.debug(
                "Project not found in Iguazio. Considering deletion as successful",
                name=name,
                deletion_strategy=deletion_strategy,
            )
        else:
            job_id = response.json()["data"]["id"]
            self._wait_for_job_completion(session_cookie, job_id)

    def list_projects(
        self, session_cookie: str,
    ) -> typing.List[mlrun.api.schemas.Project]:
        response = self._send_request_to_api("GET", "projects", session_cookie)
        response_body = response.json()
        projects = []
        for iguazio_project in response_body["data"]:
            projects.append(
                self._transform_iguazio_project_to_mlrun_project(iguazio_project)
            )
        return projects

    def _post_project_to_iguazio(
        self, session_cookie: str, body: dict
    ) -> mlrun.api.schemas.Project:
        response = self._send_request_to_api(
            "POST", "projects", session_cookie, json=body
        )
        return self._transform_iguazio_project_to_mlrun_project(response.json()["data"])

    def _put_project_to_iguazio(self, session_cookie: str, name: str, body: dict):
        response = self._send_request_to_api(
            "PUT", f"projects/{name}", session_cookie, json=body
        )
        return self._transform_iguazio_project_to_mlrun_project(response.json()["data"])

    def _get_project_from_iguazio(self, session_cookie: str, name):
        return self._send_request_to_api("GET", f"projects/{name}", session_cookie)

    def _wait_for_job_completion(self, session_cookie: str, job_id: str):
        def _verify_job_in_terminal_state():
            response = self._send_request_to_api(
                "GET", f"jobs/{job_id}", session_cookie
            )
            job_state = response.json()["data"]["attributes"]["state"]
            if job_state not in ["canceled", "failed", "completed"]:
                raise Exception(f"Job in progress. State: {job_state}")

        mlrun.utils.helpers.retry_until_successful(
            self._wait_for_job_completion_retry_interval,
            5,
            logger,
            True,
            _verify_job_in_terminal_state,
        )

    def _send_request_to_api(self, method, path, session_cookie=None, **kwargs):
        url = f"{self._api_url}/api/{path}"
        if session_cookie:
            cookies = kwargs.get("cookies", {})
            # in case some dev using this function for some reason setting cookies manually through kwargs + have a
            # cookie with "session" key there + filling the session cookie - explode
            if "session" in cookies and cookies["session"] != session_cookie:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    "Session cookie already set"
                )
            cookies["session"] = session_cookie
            kwargs["cookies"] = cookies
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = 20
        response = self._session.request(method, url, verify=False, **kwargs)
        if not response.ok:
            log_kwargs = copy.deepcopy(kwargs)
            log_kwargs.update({"method": method, "path": path})
            if response.content:
                try:
                    data = response.json()
                    ctx = data.get("meta", {}).get("ctx")
                    errors = data.get("errors", [])
                except Exception:
                    pass
                else:
                    log_kwargs.update({"ctx": ctx, "errors": errors})
            logger.warning("Request to iguazio failed", **log_kwargs)
            mlrun.errors.raise_for_status(response)
        return response

    @staticmethod
    def _generate_request_body(project: mlrun.api.schemas.Project):
        body = {
            "data": {
                "type": "project",
                "attributes": {
                    "name": project.metadata.name,
                    "description": project.spec.description,
                    "admin_status": project.spec.desired_state,
                    "mlrun_project": Client._transform_mlrun_project_to_iguazio_mlrun_project_attribute(
                        project
                    ),
                },
            }
        }
        if project.metadata.created:
            body["data"]["attributes"][
                "created_at"
            ] = project.metadata.created.isoformat()
        if project.metadata.labels:
            body["data"]["attributes"][
                "labels"
            ] = Client._transform_mlrun_labels_to_iguazio_labels(
                project.metadata.labels
            )
        if project.metadata.annotations:
            body["data"]["attributes"][
                "annotations"
            ] = Client._transform_mlrun_labels_to_iguazio_labels(
                project.metadata.annotations
            )
        return body

    @staticmethod
    def _transform_mlrun_project_to_iguazio_mlrun_project_attribute(
        project: mlrun.api.schemas.Project,
    ):
        return json.dumps(
            project.dict(
                exclude_unset=True,
                exclude={
                    "metadata": {"name", "created", "labels", "annotations"},
                    "spec": {"description", "desired_state"},
                    "status": {"state"},
                },
            )
        )

    @staticmethod
    def _transform_mlrun_labels_to_iguazio_labels(
        mlrun_labels: dict,
    ) -> typing.List[dict]:
        iguazio_labels = []
        for label_key, label_value in mlrun_labels.items():
            iguazio_labels.append({"name": label_key, "value": label_value})
        return iguazio_labels

    @staticmethod
    def _transform_iguazio_labels_to_mlrun_labels(
        iguazio_labels: typing.List[dict],
    ) -> dict:
        return {label["name"]: label["value"] for label in iguazio_labels}

    @staticmethod
    def _transform_iguazio_project_to_mlrun_project(
        iguazio_project,
    ) -> mlrun.api.schemas.Project:
        mlrun_project_without_common_fields = json.loads(
            iguazio_project["attributes"].get("mlrun_project", "{}")
        )
        # name is mandatory in the mlrun schema, without adding it the schema initialization will fail
        mlrun_project_without_common_fields.setdefault("metadata", {})[
            "name"
        ] = iguazio_project["attributes"]["name"]
        mlrun_project = mlrun.api.schemas.Project(**mlrun_project_without_common_fields)
        mlrun_project.metadata.created = datetime.datetime.fromisoformat(
            iguazio_project["attributes"]["created_at"]
        )
        mlrun_project.spec.desired_state = mlrun.api.schemas.ProjectDesiredState(
            iguazio_project["attributes"]["admin_status"]
        )
        mlrun_project.status.state = mlrun.api.schemas.ProjectState(
            iguazio_project["attributes"]["operational_status"]
        )
        if iguazio_project["attributes"].get("description"):
            mlrun_project.spec.description = iguazio_project["attributes"][
                "description"
            ]
        if iguazio_project["attributes"].get("labels"):
            mlrun_project.metadata.labels = Client._transform_iguazio_labels_to_mlrun_labels(
                iguazio_project["attributes"]["labels"]
            )
        if iguazio_project["attributes"].get("annotations"):
            mlrun_project.metadata.annotations = Client._transform_iguazio_labels_to_mlrun_labels(
                iguazio_project["attributes"]["annotations"]
            )
        return mlrun_project
