import importlib
import sys
from unittest.mock import Mock

import pytest


@pytest.fixture(scope="session")
def app_module(monkeypatch_session):
    monkeypatch_session.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch_session.setenv("AUTH_SERVICE_URL", "http://auth-service")

    import psycopg2.pool

    monkeypatch_session.setattr(psycopg2.pool, "SimpleConnectionPool", Mock(return_value=Mock()))
    sys.modules.pop("app", None)
    return importlib.import_module("app")


@pytest.fixture(scope="session")
def monkeypatch_session():
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield monkeypatch


@pytest.fixture
def pool(app_module, monkeypatch):
    mocked_pool = Mock()
    monkeypatch.setattr(app_module, "pool", mocked_pool)
    return mocked_pool


@pytest.fixture
def client(app_module, pool, monkeypatch):
    app_module.app.config.update(TESTING=True)
    auth_response = Mock(status_code=200)
    monkeypatch.setattr(app_module.requests, "get", Mock(return_value=auth_response))
    return app_module.app.test_client()


@pytest.fixture
def database(pool):
    connection = Mock()
    cursor = Mock()
    connection.cursor.return_value = cursor
    pool.getconn.return_value = connection
    return connection, cursor


@pytest.fixture
def auth_headers():
    return {"Authorization": "Bearer valid-key"}
