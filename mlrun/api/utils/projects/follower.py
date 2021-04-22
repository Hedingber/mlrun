import typing

import humanfriendly
import sqlalchemy.orm

import mlrun.api.db.session
import mlrun.api.schemas
import mlrun.api.utils.clients.iguazio
import mlrun.api.utils.clients.nuclio
import mlrun.api.utils.periodic
import mlrun.api.utils.projects.member
import mlrun.api.utils.projects.remotes.member
import mlrun.api.utils.projects.remotes.nop
import mlrun.config
import mlrun.errors
import mlrun.utils
import mlrun.utils.helpers
import mlrun.utils.regex
import mlrun.utils.singleton
from mlrun.utils import logger


class Member(
    mlrun.api.utils.projects.member.Member,
    metaclass=mlrun.utils.singleton.AbstractSingleton,
):
    def initialize(self):
        logger.info("Initializing projects follower")
        self._projects: typing.Dict[str, mlrun.api.schemas.Project] = {}
        if mlrun.config.config.httpdb.projects.leader == "iguazio":
            self._leader_client = mlrun.api.utils.clients.iguazio.Client()
            if not mlrun.config.config.httpdb.projects.iguazio_access_key:
                raise mlrun.errors.MLRunInvalidArgumentError(
                    "Iguazio access key must be configured when the leader is Iguazio"
                )
            self._iguazio_cookie = f'j:{{"sid": "{mlrun.config.config.httpdb.projects.iguazio_access_key}"}}'
        else:
            raise NotImplementedError("Unsupported project leader")
        self._periodic_sync_interval_seconds = humanfriendly.parse_timespan(
            mlrun.config.config.httpdb.projects.periodic_sync_interval
        )
        # run one sync to start off on the right foot
        self._sync_projects()
        self._start_periodic_sync()

    def shutdown(self):
        logger.info("Shutting down projects leader")
        self._stop_periodic_sync()

    def create_project(
        self,
        session: sqlalchemy.orm.Session,
        project: mlrun.api.schemas.Project,
        request_from_leader: bool = False,
    ) -> mlrun.api.schemas.Project:
        if request_from_leader:
            if project.metadata.name in self._projects:
                raise mlrun.errors.MLRunConflictError("Project already exists")
            self._projects[project.metadata.name] = project
            return project
        else:
            return self._leader_client.create_project(self._iguazio_cookie, project)

    def store_project(
        self,
        session: sqlalchemy.orm.Session,
        name: str,
        project: mlrun.api.schemas.Project,
        request_from_leader: bool = False,
    ):
        if request_from_leader:
            self._projects[project.metadata.name] = project
            return project
        else:
            return self._leader_client.store_project(
                self._iguazio_cookie, name, project
            )

    def patch_project(
        self,
        session: sqlalchemy.orm.Session,
        name: str,
        project: dict,
        patch_mode: mlrun.api.schemas.PatchMode = mlrun.api.schemas.PatchMode.replace,
        request_from_leader: bool = False,
    ):
        # TODO: think if we really want it
        raise NotImplementedError("Patch operation not supported in follower mode")

    def delete_project(
        self,
        session: sqlalchemy.orm.Session,
        name: str,
        deletion_strategy: mlrun.api.schemas.DeletionStrategy = mlrun.api.schemas.DeletionStrategy.default(),
        request_from_leader: bool = False,
    ):
        if request_from_leader:
            if name in self._projects:
                del self._projects[name]
        else:
            return self._leader_client.delete_project(
                self._iguazio_cookie, name, deletion_strategy
            )

    def get_project(
        self, session: sqlalchemy.orm.Session, name: str
    ) -> mlrun.api.schemas.Project:
        if name not in self._projects:
            raise mlrun.errors.MLRunNotFoundError(f"Project not found {name}")
        return self._projects[name]

    def list_projects(
        self,
        session: sqlalchemy.orm.Session,
        owner: str = None,
        format_: mlrun.api.schemas.Format = mlrun.api.schemas.Format.full,
        labels: typing.List[str] = None,
        state: mlrun.api.schemas.ProjectState = None,
    ) -> mlrun.api.schemas.ProjectsOutput:
        projects = self._projects.values()
        # filter projects
        if owner:
            raise NotImplementedError(
                "Filtering projects by owner is currently not supported in follower mode"
            )
        if state:
            projects = list(
                filter(lambda project: project.status.state == state, projects)
            )
        if labels:
            projects = list(
                filter(
                    lambda project: self._is_project_matching_labels(labels, project),
                    projects,
                )
            )
        # format output
        if format_ == mlrun.api.schemas.Format.name_only:
            projects = list(map(lambda project: project.metadata.name, projects))
        elif format_ == mlrun.api.schemas.Format.full:
            pass
        else:
            raise NotImplementedError(
                f"Provided format is not supported. format={format_}"
            )
        return mlrun.api.schemas.ProjectsOutput(projects=projects)

    def _start_periodic_sync(self):
        # the > 0 condition is to allow ourselves to disable the sync from configuration
        if self._periodic_sync_interval_seconds > 0:
            logger.info(
                "Starting periodic projects sync",
                interval=self._periodic_sync_interval_seconds,
            )
            mlrun.api.utils.periodic.run_function_periodically(
                self._periodic_sync_interval_seconds,
                self._sync_projects.__name__,
                False,
                self._sync_projects,
            )

    def _stop_periodic_sync(self):
        mlrun.api.utils.periodic.cancel_periodic_function(self._sync_projects.__name__)

    def _sync_projects(self):
        projects = self._leader_client.list_projects(self._iguazio_cookie)
        # This might cause some "concurrency" issues, might need to use some locking
        self._projects = {project.metadata.name: project for project in projects}

    @staticmethod
    def _is_project_matching_labels(
        labels: typing.List[str], project: mlrun.api.schemas.Project
    ):
        for label in labels:
            if "=" in label:
                name, value = [v.strip() for v in label.split("=", 1)]
                if name not in project.metadata.labels:
                    return False
                return value == project.metadata.labels[name]
            else:
                return label in project.metadata.labels