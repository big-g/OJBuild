"""Real-session clients for authenticated server behavior tests."""

from fastapi.testclient import TestClient


def authenticated_client(app, *, admin=False):
    """Authenticate against the app's real store without mocking identity."""
    store = app.state.auth_store
    user_id = "test-admin" if admin else "test-user"
    if store.get_user(user_id) is None:
        store.create_user(user_id, user_id, "test-password", is_admin=admin)
    token = store.create_session(user_id)
    return TestClient(app, headers={"X-OpenJarvis-Session": token})
