// The consumer app talks only to the VelcrowAI service. It never calls either
// shop directly: cross-shop querying happens server-side (spec 2, 8), so the
// buyer's mandate and keys stay on the buyer's own service.
const AGENT = "http://127.0.0.1:8003";

export class ApiError extends Error {
  constructor(payload, status) {
    super(payload.why || `request failed (${status})`);
    this.code = payload.code || "UNKNOWN";
    this.why = payload.why || "";
    this.availableActions = payload.available_actions || [];
    this.status = status;
  }
}

async function request(path, { method = "GET", body } = {}) {
  let resp;
  try {
    resp = await fetch(AGENT + path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(
      {
        code: "UNREACHABLE",
        why:
          `Could not reach the VelcrowAI service at ${AGENT}. Start it with: ` +
          `python -m uvicorn agent.app:create_app --factory --port 8003`,
      },
      0,
    );
  }
  const text = await resp.text();
  const payload = text ? JSON.parse(text) : {};
  if (!resp.ok) throw new ApiError(payload, resp.status);
  return payload;
}

export const buyer = {
  start: (goal) => request("/buyer/run", { method: "POST", body: { goal } }),
  get: (runId) => request(`/buyer/run/${runId}`),
  choose: (runId, optionId, contact) =>
    request(`/buyer/run/${runId}/choose`, {
      method: "POST",
      body: { option_id: optionId, contact },
    }),
  approve: (runId) => request(`/buyer/run/${runId}/approve`, { method: "POST", body: {} }),
  history: () => request("/buyer/history"),
  trust: () => request("/buyer/trust"),
};

// The evidence room (spec 9). Read-only apart from the tamper button, which
// exists so the chain can be seen breaking rather than described as breaking.
export const audit = {
  chains: (limit = 30) => request(`/audit/chains?limit=${limit}`),
  verify: () => request("/audit/verify"),
  tamper: (actor, index) =>
    request("/audit/tamper", { method: "POST", body: { actor, index } }),
  dispute: (txnRef) => request(`/audit/dispute/${encodeURIComponent(txnRef)}`),
  traces: () => request("/audit/traces"),
  revenueLab: () => request("/audit/revenue-lab"),
};

export { AGENT };
