from http import HTTPStatus
from typing import Callable
from typing import Generator
from unittest.mock import Mock

import pytest
import requests
import v3io.dataplane

import mlrun.k8s_utils
from mlrun.api.db.sqldb.db import SQLDB
from mlrun.config import config
from mlrun.k8s_utils import get_k8s_helper
from tests.conftest import init_sqldb


@pytest.fixture
def k8s_helper_mock(monkeypatch):
    class K8sHelperMock(Mock):
        def resolve_namespace(self, namespace=None):
            return namespace or config.namespace

        def is_running_inside_kubernetes_cluster(self):
            return False

    monkeypatch.setattr(mlrun.k8s_utils, "K8sHelper", K8sHelperMock)

    # call get_k8s_helper so that the mock instance will get into cache
    k8s_helper_mock_instance = get_k8s_helper()

    yield k8s_helper_mock_instance

    k8s_helper_mock_instance.reset_mock()


session_maker: Callable


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
