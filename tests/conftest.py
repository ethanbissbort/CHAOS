"""Shared pytest fixtures.

Every test runs against an in-memory SQLite database and the in-memory message
bus, so the whole platform is exercisable without PostgreSQL or Mosquitto.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from homestead_twin import db as db_module
from homestead_twin.config import Settings
from homestead_twin.models.base import Base
from homestead_twin.mqtt import InMemoryBus


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        database_url="sqlite://",
        mqtt_enabled=False,
        ems_enabled=False,
        alarm_engine_enabled=False,
        allow_physical_control=False,
        node_role="primary",
    )


@pytest.fixture()
def engine():
    """One shared in-memory SQLite database for the duration of a test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    import homestead_twin.models  # noqa: F401  (register mappers)

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture()
def session_factory(engine) -> sessionmaker[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    db_module.configure(engine)
    return factory


@pytest.fixture()
def db_session(session_factory) -> Session:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def bus() -> InMemoryBus:
    return InMemoryBus()


@pytest.fixture()
def app(settings, engine, session_factory, bus):
    from homestead_twin.api.app import create_app

    application = create_app(settings, bus=bus, start_services=False, init_db=False)
    # Bind the app to the test engine rather than the configured database.
    application.state.engine = engine
    application.state.session_factory = session_factory
    return application


@pytest.fixture()
def client(app) -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def operator_headers() -> dict[str, str]:
    return {"X-Operator": "test.operator", "X-Operator-Role": "operator"}


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"X-Operator": "test.admin", "X-Operator-Role": "administrator"}


@pytest.fixture()
def loaded_registry(db_session):
    """Load the v0.3 machine-readable package into the test database."""
    from homestead_twin.registry.loader import load_package

    result = load_package(db_session)
    db_session.commit()
    return result
