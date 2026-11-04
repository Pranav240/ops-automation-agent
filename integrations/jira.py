"""Jira Service Management client (Jira Cloud REST API, basic auth with an API token).

Reads new customer requests, posts replies as comments (public or internal) and moves tickets
to a status by name. Settings come from .env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
JIRA_PROJECT_KEY.
"""

import os
from dataclasses import dataclass

import httpx

PROCESSED_LABEL = "ops-agent"


@dataclass
class Ticket:
    key: str
    customer_email: str
    summary: str
    description: str
    source: str = "jira"  # "api" requests have no ticket, so nothing is posted to Jira

    @property
    def text(self) -> str:
        return f"{self.summary}\n\n{self.description}".strip()


class JiraClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 20,
    ):
        self.project_key = project_key
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(email, api_token),
            headers={"Accept": "application/json"},
            transport=transport,
            timeout=timeout_s,
        )

    @classmethod
    def from_env(cls) -> "JiraClient":
        return cls(
            os.environ["JIRA_BASE_URL"],
            os.environ["JIRA_EMAIL"],
            os.environ["JIRA_API_TOKEN"],
            os.environ["JIRA_PROJECT_KEY"],
        )

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        response = await self.client.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    async def new_tickets(self, max_results: int = 20) -> list[Ticket]:
        """Open requests in the project that the agent has not picked up yet, oldest first."""
        jql = (
            f'project = "{self.project_key}" AND statusCategory != Done '
            f'AND (labels IS EMPTY OR labels NOT IN ("{PROCESSED_LABEL}")) ORDER BY created ASC'
        )
        response = await self._send(
            "GET",
            "/rest/api/3/search/jql",
            params={"jql": jql, "fields": "summary", "maxResults": max_results},
        )
        return [await self.ticket(issue["key"]) for issue in response.json().get("issues", [])]

    async def ticket(self, key: str) -> Ticket:
        """The service desk view gives plain-text fields and the customer's email."""
        body = (await self._send("GET", f"/rest/servicedeskapi/request/{key}")).json()
        fields = {f["fieldId"]: f.get("value") for f in body.get("requestFieldValues", [])}
        return Ticket(
            key=key,
            customer_email=body["reporter"].get("emailAddress", ""),
            summary=fields.get("summary") or "",
            description=fields.get("description") or "",
        )

    async def mark_processed(self, key: str) -> None:
        await self._send(
            "PUT",
            f"/rest/api/3/issue/{key}",
            json={"update": {"labels": [{"add": PROCESSED_LABEL}]}},
        )

    async def comment(self, key: str, body: str, public: bool) -> None:
        """Public comments are sent to the customer; internal ones are seen by agents only."""
        await self._send(
            "POST",
            f"/rest/servicedeskapi/request/{key}/comment",
            json={"body": body, "public": public},
        )

    async def move_to(self, key: str, status: str) -> bool:
        """Apply the transition that leads to `status`. False if the workflow has none."""
        response = await self._send("GET", f"/rest/api/3/issue/{key}/transitions")
        for transition in response.json().get("transitions", []):
            if transition["to"]["name"].lower() == status.lower():
                await self._send(
                    "POST",
                    f"/rest/api/3/issue/{key}/transitions",
                    json={"transition": {"id": transition["id"]}},
                )
                return True
        return False

    async def aclose(self) -> None:
        await self.client.aclose()
