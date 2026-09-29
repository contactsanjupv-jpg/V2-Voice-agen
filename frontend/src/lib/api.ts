/**
 * Thin fetch wrapper for the Atla backend. Credentials are always included
 * so the session cookie round-trips — the backend is what enforces auth,
 * this file just makes sure the browser sends what it has.
 */
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });

    if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body.detail)) {
        detail = body.detail.map((e: { msg?: string }) => e.msg).filter(Boolean).join("; ") || detail;
      } else if (body.detail) {
        detail = JSON.stringify(body.detail);
      }
    } catch {
      // response wasn't JSON — fall back to statusText
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return res.json();
}

export interface Organization {
  id: string;
  name: string;
  role: string;
}

export interface StructuredBusinessInfo {
  business_name: string | null;
  description: string | null;
  industry: string | null;
  services: string[];
  address: string | null;
  phone: string | null;
  hours: Record<string, string> | null;
  faqs: { question: string; answer: string }[];
  policies: string[];
}

export interface Voice {
  id: string;
  retell_voice_id: string;
  name: string;
  provider: string;
  gender: string | null;
  accent: string | null;
  age_style: string | null;
  preview_url: string | null;
}

export interface PhoneNumberOut {
  id: string;
  number: string;
  area_code: string | null;
  country: string;
  monthly_cost_cents: number | null;
  status: string;
}

export interface AgentOut {
  id: string;
  name: string;
  status: string;
  retell_agent_id: string | null;
}

export interface CallOut {
  id: string;
  direction: string;
  caller_number: string | null;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  status: string | null;
  disconnect_reason: string | null;
  summary: string | null;
  sentiment: string | null;
  cost_cents: number | null;
}

export interface LeadOut {
  id: string;
  name: string | null;
  phone: string | null;
  email: string | null;
  reason: string | null;
  summary: string | null;
  status: string;
  source: string;
  created_at: string;
}

export const api = {
  signup: (data: { email: string; password: string; organization_name: string }) =>
    request<{ id: string; email: string }>("/api/v1/auth/signup", { method: "POST", body: JSON.stringify(data) }),
  login: (data: { email: string; password: string }) =>
    request<{ id: string; email: string }>("/api/v1/auth/login", { method: "POST", body: JSON.stringify(data) }),
  logout: () => request<void>("/api/v1/auth/logout", { method: "POST" }),
  me: () => request<{ id: string; email: string }>("/api/v1/auth/me"),

  myOrganizations: () => request<Organization[]>("/api/v1/orgs/me"),

  importWebsite: (orgId: string, url: string) =>
    request<{ business_id: string; pages_fetched: string[]; structured_info: StructuredBusinessInfo }>(
      `/api/v1/orgs/${orgId}/businesses/import-website`,
      { method: "POST", body: JSON.stringify({ url }) }
    ),
  createManualBusiness: (orgId: string, name: string) =>
    request<{ id: string; name: string; status: string }>(`/api/v1/orgs/${orgId}/businesses/create-manual`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  getBusiness: (orgId: string, businessId: string) =>
    request<Record<string, unknown>>(`/api/v1/orgs/${orgId}/businesses/${businessId}`),
  approveBusiness: (
    orgId: string,
    businessId: string,
    data: {
      name: string;
      industry: string | null;
      address: string | null;
      description: string | null;
      hours: Record<string, string> | null;
      phone: string | null;
      services: string[];
      faqs: { question: string; answer: string }[];
      policies: string[];
    }
  ) =>
    request<Record<string, unknown>>(`/api/v1/orgs/${orgId}/businesses/${businessId}/approve`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  listVoices: (params?: { gender?: string; accent?: string; search?: string }) => {
    const qs = new URLSearchParams(params as Record<string, string>).toString();
    return request<Voice[]>(`/api/v1/voices${qs ? `?${qs}` : ""}`);
  },

    purchasePhoneNumber: (orgId: string, country: string, areaCode?: string) =>
    request<PhoneNumberOut>(`/api/v1/orgs/${orgId}/phone-numbers`, {
      method: "POST",
      body: JSON.stringify({ country, area_code: areaCode || null }),
    }),

  listPhoneNumbers: (orgId: string) => request<PhoneNumberOut[]>(`/api/v1/orgs/${orgId}/phone-numbers`),

  upsertAgent: (
    orgId: string,
    businessId: string,
    data: {
      name: string;
      greeting: string | null;
      personality: string;
      language: string;
      voice_id: string;
      tasks: Record<string, boolean>;
      transfer_number: string | null;
      business_hours: Record<string, string> | null;
      after_hours_behavior: string | null;
    }
  ) => request<AgentOut>(`/api/v1/orgs/${orgId}/agents/${businessId}`, { method: "PUT", body: JSON.stringify(data) }),

  listAgents: (orgId: string) => request<AgentOut[]>(`/api/v1/orgs/${orgId}/agents`),

  startTestCall: (orgId: string, agentId: string) =>
    request<{ call_id: string; access_token: string }>(`/api/v1/orgs/${orgId}/agents/${agentId}/test-call`, {
      method: "POST",
    }),

  activate: (orgId: string, agentId: string, phoneNumberId: string) =>
    request<{ phone_number_id: string; agent_id: string; status: string }>(`/api/v1/orgs/${orgId}/activate`, {
      method: "POST",
      body: JSON.stringify({ agent_id: agentId, phone_number_id: phoneNumberId }),
    }),

  listCalls: (orgId: string, params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams(params as unknown as Record<string, string>).toString();
    return request<CallOut[]>(`/api/v1/orgs/${orgId}/calls${qs ? `?${qs}` : ""}`);
  },
  getCall: (orgId: string, callId: string) => request<CallOut>(`/api/v1/orgs/${orgId}/calls/${callId}`),
  getCallTranscript: (orgId: string, callId: string) =>
    request<{ call_id: string; transcript: unknown[] }>(`/api/v1/orgs/${orgId}/calls/${callId}/transcript`),

  listLeads: (orgId: string, params?: { status?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams(params as unknown as Record<string, string>).toString();
    return request<LeadOut[]>(`/api/v1/orgs/${orgId}/leads${qs ? `?${qs}` : ""}`);
  },
  updateLeadStatus: (orgId: string, leadId: string, status: string) =>
    request<LeadOut>(`/api/v1/orgs/${orgId}/leads/${leadId}`, { method: "PATCH", body: JSON.stringify({ status }) }),
};