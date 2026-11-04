import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import type { RunDetail } from "./api";

const ATTACK = '<img src=x onerror="window.hacked=true"><script>window.hacked=true</script>';
const RUN = {
  run_id: "abc123",
  source: "api",
  request_id: "REQ-1",
  ticket_key: null,
  status: "pending_approval",
  action: "refund",
  amount: 720,
  sent_to_approval: true,
  approved_by: null,
  executed: false,
  llm_calls: 5,
  cost_usd: 0.0021,
  latency_s: 12,
  model: "claude-haiku-5-5",
  created_at: "2026-11-04T10:00:00+00:00",
};
const DETAIL: RunDetail = {
  run: RUN,
  trace_id: "feed",
  customer_email: "alice@example.com",
  request_text: ATTACK,
  steps: [
    { node: "planner", started_at: "t1", seconds: 2, output: { plan: { summary: ATTACK } } },
    { node: "approval_gate", started_at: "t2", seconds: null, output: {} },
  ],
  pending: {
    action: "refund",
    args: { order_id: "ORD-1033", amount: 720, reason: "lost" },
    reply: `Refunded. ${ATTACK}`,
    reasons: ["refund over $500"],
    critique: { ok: false, issues: [ATTACK] },
  },
  outcome: null,
};

interface Call {
  path: string;
  init?: RequestInit;
}

function serve(routes: { scope?: string; detail?: RunDetail; status?: number; error?: string }) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", (path: string, init?: RequestInit) => {
    calls.push({ path, init });
    let status = 200;
    let body: unknown = {};
    if (init?.method === "POST") {
      status = routes.status ?? 202;
      body = routes.error ? { detail: routes.error } : { sent: "approve" };
    } else if (path === "/api/me") {
      body = { scope: routes.scope ?? "approver" };
    } else if (path.startsWith("/api/runs/")) {
      body = routes.detail ?? DETAIL;
    } else {
      body = [RUN];
    }
    return Promise.resolve(new Response(JSON.stringify(body), { status }));
  });
  return calls;
}

function signedIn(hash = "") {
  sessionStorage.setItem("ops-dashboard-token", "secret");
  window.location.hash = hash;
  render(<App />);
}

const decisions = (calls: Call[]) => calls.filter((call) => call.init?.method === "POST");

test("asks for a token and shows when it is refused", async () => {
  vi.stubGlobal("fetch", () => Promise.resolve(new Response("{}", { status: 401 })));
  render(<App />);
  await userEvent.type(screen.getByLabelText("Access token"), "wrong");
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("alert")).toHaveProperty(
    "textContent",
    "That token was not accepted.",
  );
});

test("sends the token as a bearer header", async () => {
  const calls = serve({});
  signedIn();
  await screen.findByText("REQ-1");
  const headers = calls[0].init?.headers as Record<string, string>;
  expect(headers.Authorization).toBe("Bearer secret");
});

test("queue links a paused run to its page", async () => {
  serve({});
  signedIn();
  const link = await screen.findByRole("link", { name: "REQ-1" });
  expect(link.getAttribute("href")).toBe("#/runs/abc123");
  expect(screen.getByText("Needs approval")).toBeTruthy();
});

test("customer text from a run stays inert text everywhere it appears", async () => {
  serve({});
  signedIn("#/runs/abc123");
  await screen.findByText("Decision needed");
  const raw = document.body.innerHTML;
  expect(document.querySelector("img")).toBeNull();
  expect(document.querySelector("script")).toBeNull();
  expect(raw).toContain("&lt;img src=x");
  expect((window as unknown as { hacked?: boolean }).hacked).toBeUndefined();
});

test("approve sends only the decision", async () => {
  const calls = serve({});
  signedIn("#/runs/abc123");
  await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
  await screen.findByRole("status");
  expect(decisions(calls)[0].path).toBe("/api/runs/abc123/decision");
  expect(JSON.parse(decisions(calls)[0].init?.body as string)).toEqual({ decision: "approve" });
});

test("reject sends the rejection", async () => {
  const calls = serve({});
  signedIn("#/runs/abc123");
  await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
  await screen.findByRole("status");
  expect(JSON.parse(decisions(calls)[0].init?.body as string)).toEqual({ decision: "reject" });
});

test("edit sends the changed amount as a number", async () => {
  const calls = serve({});
  signedIn("#/runs/abc123");
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  const amount = screen.getByLabelText("Amount (USD)");
  await userEvent.clear(amount);
  await userEvent.type(amount, "450");
  await userEvent.click(screen.getByRole("button", { name: "Approve with edits" }));
  await screen.findByRole("status");
  expect(JSON.parse(decisions(calls)[0].init?.body as string)).toEqual({
    decision: "edit",
    args: { amount: 450 },
  });
});

test("a refused decision shows the reason and sends nothing else", async () => {
  const calls = serve({ status: 409, error: "REQ-1 is not waiting for approval" });
  signedIn("#/runs/abc123");
  await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toBe("REQ-1 is not waiting for approval");
  expect(decisions(calls)).toHaveLength(1);
});

test("a viewer sees the decision but has no buttons", async () => {
  serve({ scope: "viewer" });
  signedIn("#/runs/abc123");
  await screen.findByText("This token can view but not decide.");
  expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
});

test("a finished run shows its outcome and no decision", async () => {
  serve({
    detail: {
      ...DETAIL,
      run: { ...RUN, status: "done", approved_by: "dashboard-approver" },
      pending: null,
      outcome: { status: "done", reply: "Refunded.", executed: true, args: { amount: 720 } },
    },
  });
  signedIn("#/runs/abc123");
  await screen.findByText("Outcome");
  expect(screen.getByText("dashboard-approver")).toBeTruthy();
  expect(screen.queryByText("Decision needed")).toBeNull();
});

test("sign out clears the token", async () => {
  serve({});
  signedIn();
  await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
  await waitFor(() => expect(sessionStorage.getItem("ops-dashboard-token")).toBeNull());
  expect(screen.getByLabelText("Access token")).toBeTruthy();
});
