from http import HTTPStatus
from os import environ
from typing import Callable, Generator
from unittest.mock import Mock

import pytest
import requests
import v3io.dataplane

import mlrun.api.utils.singletons.db
import mlrun.api.utils.singletons.projects_manager
import mlrun.config
from mlrun.api.db.sqldb.db import SQLDB
from tests.conftest import init_sqldb, root_path, rundb_path, logs_path

session_maker: Callable


@pytest.fixture(autouse=True)
# if we'll just call it config it may be overridden by other fixtures with the same name
def config_do_not_override():
    environ["PYTHONPATH"] = root_path
    environ["MLRUN_DBPATH"] = rundb_path
    environ["MLRUN_httpdb__dirpath"] = rundb_path
    environ["MLRUN_httpdb__logs_path"] = logs_path
    environ["MLRUN_httpdb__projects__periodic_sync_interval"] = "0 seconds"
    environ["MLRUN_log_level"] = "DEBUG"
    # reload config so that values overridden by tests won't pass to other tests
    mlrun.config.config.reload()


@pytest.fixture
def db():
    global session_maker
    dsn = "sqlite:///:memory:?check_same_thread=false"
    db_session = None
    try:
        session_maker = init_sqldb(dsn)
        db_session = session_maker()
        db = SQLDB(dsn)
        db.initialize(db_session)
    finally:
        if db_session is not None:
            db_session.close()
    mlrun.api.utils.singletons.db.initialize_db(db)
    mlrun.api.utils.singletons.projects_manager.initialize_projects_manager()
    return db


@pytest.fixture()
def db_session() -> Generator:
    db_session = None
    try:
        db_session = session_maker()
        yield db_session
    finally:
        if db_session is not None:
            db_session.close()


@pytest.fixture
def patch_file_forbidden(monkeypatch):
    class MockV3ioClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_container_contents(self, *args, **kwargs):
            raise RuntimeError("Permission denied")

    mock_get = mock_failed_get_func(HTTPStatus.FORBIDDEN.value)

    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(requests, "head", mock_get)
    monkeypatch.setattr(v3io.dataplane, "Client", MockV3ioClient)


@pytest.fixture
def patch_file_not_found(monkeypatch):
    class MockV3ioClient:
        def __init__(self, *args, **kwargs):
            pass

        def get_container_contents(self, *args, **kwargs):
            raise FileNotFoundError

    mock_get = mock_failed_get_func(HTTPStatus.NOT_FOUND.value)

    monkeypatch.setattr(requests, "get", mock_get)
    monkeypatch.setattr(requests, "head", mock_get)
    monkeypatch.setattr(v3io.dataplane, "Client", MockV3ioClient)


def mock_failed_get_func(status_code: int):
    def mock_get(*args, **kwargs):
        mock_response = Mock()
        mock_response.status_code = status_code
        mock_response.raise_for_status = Mock(
            side_effect=requests.HTTPError("Error", response=mock_response)
        )
        return mock_response

    return mock_get
