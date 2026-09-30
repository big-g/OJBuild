"""Real-session clients for authenticated server behavior tests."""

from fastapi.testclient import TestClient


def authenticated_client(app):
    """Authenticate against the app's real store without mocking identity."""
    store = app.state.auth_store
    if store.get_user("test-user") is None:
        store.create_user("test-user", "test-user", "test-password")
    token = store.create_session("test-user")
    return TestClient(app, headers={"X-OpenJarvis-Session": token})
