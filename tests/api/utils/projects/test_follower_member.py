import typing

import deepdiff
import pytest
import sqlalchemy.orm

import mlrun.api.schemas
import mlrun.api.utils.projects.follower
import mlrun.api.utils.projects.remotes.leader
import mlrun.api.utils.singletons.project_member
import mlrun.config
import mlrun.errors
from mlrun.utils import logger


@pytest.fixture()
async def projects_follower() -> typing.Generator[
    mlrun.api.utils.projects.follower.Member, None, None
]:
    logger.info("Creating projects follower")
    mlrun.config.config.httpdb.projects.leader = "nop"
    mlrun.config.config.httpdb.projects.periodic_sync_interval = "0 seconds"
    mlrun.api.utils.singletons.project_member.initialize_project_member()
    projects_follower = mlrun.api.utils.singletons.project_member.get_project_member()
    yield projects_follower
    logger.info("Stopping projects follower")
    projects_follower.shutdown()


@pytest.fixture()
async def nop_leader(
    projects_follower: mlrun.api.utils.projects.follower.Member,
) -> mlrun.api.utils.projects.remotes.leader.Member:
    return projects_follower._leader_client


def test_create_project(
    db: sqlalchemy.orm.Session,
    projects_follower: mlrun.api.utils.projects.follower.Member,
    nop_leader: mlrun.api.utils.projects.remotes.leader.Member,
):
    project = _generate_project()
    created_project, _ = projects_follower.create_project(None, project,)
    _assert_projects_equal(project, created_project)
    _assert_project_in_follower(projects_follower, project)


def test_store_project(
    db: sqlalchemy.orm.Session,
    projects_follower: mlrun.api.utils.projects.follower.Member,
    nop_leader: mlrun.api.utils.projects.remotes.leader.Member,
):
    project = _generate_project()

    # project doesn't exist - store will create
    created_project, _ = projects_follower.store_project(
        None, project.metadata.name, project,
    )
    _assert_projects_equal(project, created_project)
    _assert_project_in_follower(projects_follower, project)

    project_update = _generate_project(description="new description")
    # project exists - store will update
    updated_project, _ = projects_follower.store_project(
        None, project.metadata.name, project_update,
    )
    _assert_projects_equal(project_update, updated_project)
    _assert_project_in_follower(projects_follower, project_update)


def test_delete_project(
    db: sqlalchemy.orm.Session,
    projects_follower: mlrun.api.utils.projects.follower.Member,
    nop_leader: mlrun.api.utils.projects.remotes.leader.Member,
):
    project = _generate_project()
    projects_follower.create_project(
        None, project,
    )
    _assert_project_in_follower(projects_follower, project)
    projects_follower.delete_project(
        None, project.metadata.name,
    )
    _assert_project_not_in_follower(projects_follower, project.metadata.name)

    # make sure another delete doesn't fail
    projects_follower.delete_project(
        None, project.metadata.name,
    )


def test_get_project(
    db: sqlalchemy.orm.Session,
    projects_follower: mlrun.api.utils.projects.follower.Member,
    nop_leader: mlrun.api.utils.projects.remotes.leader.Member,
):
    project = _generate_project()
    projects_follower.create_project(
        None, project,
    )
    # this functions uses get_project to assert, second assert will verify we're raising not found error
    _assert_project_in_follower(projects_follower, project)
    _assert_project_not_in_follower(projects_follower, "name-doesnt-exist")


def test_list_project(
    db: sqlalchemy.orm.Session,
    projects_follower: mlrun.api.utils.projects.follower.Member,
    nop_leader: mlrun.api.utils.projects.remotes.leader.Member,
):
    project = _generate_project(name="name-1")
    archived_project = _generate_project(
        name="name-2",
        desired_state=mlrun.api.schemas.ProjectDesiredState.archived,
        state=mlrun.api.schemas.ProjectState.archived,
    )
    label_key = "key"
    label_value = "value"
    labeled_project = _generate_project(name="name-3", labels={label_key: label_value})
    archived_and_labeled_project = _generate_project(
        name="name-4",
        desired_state=mlrun.api.schemas.ProjectDesiredState.archived,
        state=mlrun.api.schemas.ProjectState.archived,
        labels={label_key: label_value},
    )
    all_projects = {
        _project.metadata.name: _project
        for _project in [
            project,
            archived_project,
            labeled_project,
            archived_and_labeled_project,
        ]
    }
    for _project in all_projects.values():
        projects_follower.create_project(
            None, _project,
        )
    # list all
    _assert_list_projects(projects_follower, list(all_projects.values()))

    # list archived
    _assert_list_projects(
        projects_follower,
        [archived_project, archived_and_labeled_project],
        state=mlrun.api.schemas.ProjectState.archived,
    )

    # list labeled - key existence
    _assert_list_projects(
        projects_follower,
        [labeled_project, archived_and_labeled_project],
        labels=[label_key],
    )

    # list labeled - key value match
    _assert_list_projects(
        projects_follower,
        [labeled_project, archived_and_labeled_project],
        labels=[f"{label_key}={label_value}"],
    )

    # list labeled - key value match and key existence
    _assert_list_projects(
        projects_follower,
        [labeled_project, archived_and_labeled_project],
        labels=[f"{label_key}={label_value}", label_key],
    )

    # list labeled - key value match and key existence
    _assert_list_projects(
        projects_follower,
        [labeled_project, archived_and_labeled_project],
        labels=[f"{label_key}={label_value}", label_key],
    )

    # list labeled and archived - key value match and key existence
    _assert_list_projects(
        projects_follower,
        [archived_and_labeled_project],
        state=mlrun.api.schemas.ProjectState.archived,
        labels=[f"{label_key}={label_value}", label_key],
    )


def _assert_list_projects(
    projects_follower: mlrun.api.utils.projects.follower.Member,
    expected_projects: typing.List[mlrun.api.schemas.Project],
    **kwargs,
):
    projects = projects_follower.list_projects(None, **kwargs)
    assert len(projects.projects) == len(expected_projects)
    expected_projects_map = {
        _project.metadata.name: _project for _project in expected_projects
    }
    for project in projects.projects:
        _assert_projects_equal(project, expected_projects_map[project.metadata.name])

    # assert again - with name only format
    projects = projects_follower.list_projects(
        None, format_=mlrun.api.schemas.Format.name_only, **kwargs
    )
    assert len(projects.projects) == len(expected_projects)
    assert (
        deepdiff.DeepDiff(
            projects.projects, list(expected_projects_map.keys()), ignore_order=True,
        )
        == {}
    )


def _generate_project(
    name="project-name",
    description="some description",
    desired_state=mlrun.api.schemas.ProjectDesiredState.online,
    state=mlrun.api.schemas.ProjectState.online,
    labels: typing.Optional[dict] = None,
):
    return mlrun.api.schemas.Project(
        metadata=mlrun.api.schemas.ProjectMetadata(name=name, labels=labels),
        spec=mlrun.api.schemas.ProjectSpec(
            description=description, desired_state=desired_state,
        ),
        status=mlrun.api.schemas.ProjectStatus(state=state,),
    )


def _assert_projects_equal(project_1, project_2):
    assert (
        deepdiff.DeepDiff(project_1.dict(), project_2.dict(), ignore_order=True,) == {}
    )


def _assert_project_not_in_follower(
    projects_follower: mlrun.api.utils.projects.follower.Member, project_name: str
):
    with pytest.raises(mlrun.errors.MLRunNotFoundError):
        projects_follower.get_project(None, project_name)


def _assert_project_in_follower(
    projects_follower: mlrun.api.utils.projects.follower.Member,
    project: mlrun.api.schemas.Project,
):
    follower_project = projects_follower.get_project(None, project.metadata.name)
    _assert_projects_equal(project, follower_project)
