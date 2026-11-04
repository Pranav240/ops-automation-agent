import os

# Set before any test imports mock_api.app, which reads the tokens when it starts.
TOKENS = {"read": "test-read", "write": "test-write", "admin": "test-admin"}
os.environ["MOCK_API_READ_TOKEN"] = TOKENS["read"]
os.environ["MOCK_API_WRITE_TOKEN"] = TOKENS["write"]
os.environ["MOCK_API_ADMIN_TOKEN"] = TOKENS["admin"]


class ScopedClient:
    """Sends the token each route needs, for tests that are not about auth."""

    def __init__(self, client):
        self.client = client

    def request(self, method: str, path: str, **kwargs):
        if path in ("/reset", "/actions"):
            scope = "admin"
        else:
            scope = "read" if method == "GET" else "write"
        headers = {"Authorization": f"Bearer {TOKENS[scope]}"}
        return self.client.request(method, path, headers=headers, **kwargs)

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs):
        return self.request("POST", path, **kwargs)
