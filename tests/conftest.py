import os, pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import get_db
from app.models import Base
from app.main import app

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="Set TEST_DATABASE_URL to a disposable PostgreSQL database")

@pytest.fixture()
def client():
    if not TEST_DATABASE_URL: pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL database")
    engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    Base.metadata.drop_all(engine); Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    def override_db():
        db = TestingSession()
        try: yield db
        finally: db.close()
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as c: yield c
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine); engine.dispose()

@pytest.fixture()
def make_user(client):
    def create(email):
        r = client.post('/users', json={'email': email, 'full_name': email.split('@')[0]})
        assert r.status_code == 201, r.text
        return r.json()['id']
    return create
