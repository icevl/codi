// Thin wrapper around fetch. All requests carry session cookies via
// `credentials: "include"`. The server returns JSON for /api/* endpoints
// and the proxy in vite.config.ts forwards them to the Python backend in
// dev mode.

export interface SessionSummary {
  window_id: string;
  name: string;
  tmux_name: string;
  cwd: string;
  runtime: "codex" | "claude" | string;
  session_id: string | null;
  pane_command: string;
  last_activity: number | null;
  pinned: boolean;
}

export interface SessionMessage {
  role: string;
  text: string;
  content_type: string;
  timestamp?: string;
  tool_name?: string | null;
  tool_input?: Record<string, unknown> | null;
  tool_use_id?: string | null;
}

export interface SessionMessagesResponse {
  messages: SessionMessage[];
  session_id: string | null;
  has_more: boolean;
  oldest_timestamp?: string | null;
  newest_timestamp?: string | null;
  history_version?: string;
}

export interface RuntimeInfo {
  name: string;
  display_name: string;
  emoji: string;
}

export interface DirectoryEntry {
  name: string;
  path: string;
}

export interface DirectoryListing {
  path: string;
  parent: string | null;
  entries: DirectoryEntry[];
}

export interface ResumeSession {
  session_id: string;
  summary: string;
  message_count: number;
}

async function request<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const { json, headers, ...rest } = init;
  const opts: RequestInit = {
    credentials: "include",
    headers: {
      "Content-Type": json !== undefined ? "application/json" : "application/json",
      Accept: "application/json",
      ...(headers ?? {}),
    },
    ...rest,
  };
  if (json !== undefined) {
    opts.body = JSON.stringify(json);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    const err = new Error("unauthorized");
    (err as Error & { code?: number }).code = 401;
    throw err;
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (res.status === 204) {
    return undefined as unknown as T;
  }
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

export const api = {
  me: () =>
    request<{
      authenticated: boolean;
      enabled: boolean;
      totp_required: boolean;
    }>("/api/me"),
  login: (password: string, totpCode?: string) =>
    request<{ ok: boolean }>("/api/login", {
      method: "POST",
      json: { password, totp_code: totpCode || null },
    }),
  logout: () => request<{ ok: boolean }>("/api/logout", { method: "POST" }),

  listSessions: () =>
    request<{ sessions: SessionSummary[] }>("/api/sessions"),
  createSession: (body: {
    cwd: string;
    runtime: string;
    resume_session_id?: string | null;
    name?: string | null;
  }) => request<SessionSummary>("/api/sessions", { method: "POST", json: body }),
  killSession: (windowId: string) =>
    request<{ ok: boolean }>(`/api/sessions/${encodeURIComponent(windowId)}`, {
      method: "DELETE",
    }),
  renameSession: (windowId: string, name: string) =>
    request<{ ok: boolean; name: string }>(
      `/api/sessions/${encodeURIComponent(windowId)}`,
      { method: "PATCH", json: { name } },
    ),
  setSessionPinned: (windowId: string, pinned: boolean) =>
    request<{ ok: boolean; pinned: boolean }>(
      `/api/sessions/${encodeURIComponent(windowId)}`,
      { method: "PATCH", json: { pinned } },
    ),
  getMessages: (
    windowId: string,
    opts?: { before?: string; after?: string; limit?: number },
  ) => {
    const params = new URLSearchParams();
    params.set("limit", String(opts?.limit ?? 500));
    if (opts?.before) params.set("before", opts.before);
    if (opts?.after) params.set("after", opts.after);
    return request<SessionMessagesResponse>(
      `/api/sessions/${encodeURIComponent(windowId)}/messages?${params.toString()}`,
    );
  },
  sendText: (windowId: string, text: string, enter = true) =>
    request<{ ok: boolean }>(
      `/api/sessions/${encodeURIComponent(windowId)}/text`,
      { method: "POST", json: { text, enter } },
    ),
  sendKey: (windowId: string, key: string) =>
    request<{ ok: boolean }>(
      `/api/sessions/${encodeURIComponent(windowId)}/keys`,
      { method: "POST", json: { key } },
    ),
  sendCommand: (windowId: string, command: string) =>
    request<{ ok: boolean }>(
      `/api/sessions/${encodeURIComponent(windowId)}/command`,
      { method: "POST", json: { command } },
    ),
  screenshotUrl: (windowId: string) =>
    `/api/sessions/${encodeURIComponent(windowId)}/screenshot.png?t=${Date.now()}`,
  uploadImage: async (
    windowId: string,
    file: File,
  ): Promise<{ ok: boolean; path: string }> => {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(
        windowId,
      )}/upload?filename=${encodeURIComponent(file.name || "image")}`,
      {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": file.type || "application/octet-stream",
        },
        body: file,
      },
    );
    if (res.status === 401) {
      const err = new Error("unauthorized");
      (err as Error & { code?: number }).code = 401;
      throw err;
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        if (typeof body?.detail === "string") detail = body.detail;
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    return (await res.json()) as { ok: boolean; path: string };
  },

  listRuntimes: () =>
    request<{ runtimes: RuntimeInfo[] }>("/api/runtimes"),
  listSkills: (runtime: string) =>
    request<{ skills: string[]; runtime: string }>(
      `/api/skills?runtime=${encodeURIComponent(runtime)}`,
    ),
  listDirectories: (path: string) =>
    request<DirectoryListing>(
      `/api/directories?path=${encodeURIComponent(path)}`,
    ),
  listResumeSessions: (cwd: string) =>
    request<{ sessions: ResumeSession[] }>(
      `/api/resume-sessions?cwd=${encodeURIComponent(cwd)}`,
    ),
  getGitInfo: (windowId: string) =>
    request<{ is_repo: boolean; branch: string | null }>(
      `/api/sessions/${encodeURIComponent(windowId)}/git`,
    ),
  listBranches: (windowId: string) =>
    request<{
      is_repo: boolean;
      current: string | null;
      branches: string[];
    }>(`/api/sessions/${encodeURIComponent(windowId)}/branches`),
  switchBranch: (windowId: string, branch: string) =>
    request<{ ok: boolean; branch: string; stdout: string }>(
      `/api/sessions/${encodeURIComponent(windowId)}/switch-branch`,
      { method: "POST", json: { branch } },
    ),
  getOfficeState: () =>
    request<{
      catalog: Record<string, unknown>;
      layout: { cols: number; rows: number; placements: unknown[] } | null;
    }>("/api/office/state"),
  putOfficeState: (body: {
    catalog: Record<string, unknown>;
    layout: { cols: number; rows: number; placements: unknown[] } | null;
  }) =>
    request<{ ok: boolean; path: string }>("/api/office/state", {
      method: "PUT",
      json: body,
    }),

  getDiff: async (
    windowId: string,
    etag?: string | null,
  ): Promise<
    | {
        status: 304;
        etag: string | null;
        data: null;
      }
    | {
        status: 200;
        etag: string | null;
        data: {
          is_repo: boolean;
          diff: string;
          additions: number;
          deletions: number;
          file_count: number;
          untracked: string[];
        };
      }
  > => {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (etag) headers["If-None-Match"] = etag;
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(windowId)}/diff`,
      { credentials: "include", headers },
    );
    if (res.status === 401) {
      const err = new Error("unauthorized");
      (err as Error & { code?: number }).code = 401;
      throw err;
    }
    const newEtag = res.headers.get("ETag");
    if (res.status === 304) {
      return { status: 304, etag: newEtag, data: null };
    }
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    return { status: 200, etag: newEtag, data };
  },

  getUpdateStatus: () =>
    request<{
      enabled: boolean;
      current_sha: string | null;
      latest_sha: string | null;
      has_update: boolean;
      dirty: boolean;
      subject: string | null;
    }>("/api/update/status"),

  runUpdate: () =>
    request<{ started: boolean }>("/api/update/run", { method: "POST" }),
};

export type WsEvent =
  | { type: "hello"; ts: number }
  | {
      type: "message";
      window_id: string;
      session_id: string;
      role: string;
      text: string;
      content_type: string;
      is_complete: boolean;
      tool_name: string | null;
      tool_input: Record<string, unknown> | null;
      tool_use_id: string | null;
      turn_id: number | null;
      ts: number;
    }
  | {
      type: "completion";
      window_id: string;
      session_id: string;
      turn_id: number | null;
      ts: number;
    }
  | {
      type: "stream";
      window_id: string;
      session_id: string | null;
      text: string;
      status: string;
      ts: number;
    }
  | {
      type: "stream_end";
      window_id: string;
      session_id: string | null;
      ts: number;
    }
  | { type: "sessions_changed"; ts: number }
  | {
      type: "update_available";
      current_sha: string;
      latest_sha: string;
      subject: string;
      ts: number;
    };
