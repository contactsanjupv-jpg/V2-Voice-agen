"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import { API_BASE, api } from "./api";
import { POLL_MS, backoffMs, initialStateFor, parseStateEvent, reducer, type Filter, type NotificationItem } from "./notifications";

/**
 * Notification centre state for the dashboard Overview. Real time = one Server-Sent Events stream (the session cookie authenticates it
 * like any request); the server pushes only when the unread count or newest notification changes. If the stream cannot be kept open the hook
 * says "reconnecting" and falls back to a light poll of the same state endpoint, so the badge is never silently stale.
 */
export function useNotifications(orgId: string | undefined) {
  const [raw, dispatch] = useReducer(reducer, orgId ?? null, initialStateFor);
  // Organization switch: the very render that sees a new orgId already shows an empty state (never the previous organization's
  // items, badge or cursor), and the stored state is reset. Late results from the old organization are dropped by the reducer.
  const state = raw.orgId === (orgId ?? null) ? raw : initialStateFor(orgId ?? null);
  if (raw.orgId !== (orgId ?? null)) dispatch({ type: "switch-org", orgId: orgId ?? null });
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  });

  const fetchFirstPage = useCallback(async () => {
    if (!orgId) return;
    dispatch({ type: "load-start" });
    try {
      const page = await api.listNotifications(orgId, { unreadOnly: stateRef.current.filter === "unread" });
      dispatch({ type: "loaded", org: orgId, page });
    } catch {
      dispatch({ type: "load-failed", org: orgId });
    }
  }, [orgId]);

  // The badge: initial state, then live updates.
  useEffect(() => {
    if (!orgId) return;
    let closed = false;
    let source: EventSource | null = null;
    let poll: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const pollOnce = async () => {
      try {
        { const st = await api.getNotificationState(orgId); if (!closed) dispatch({ type: "server-state", org: orgId, state: st }); };
        attempt = 0;
      } catch {
        attempt += 1;
      }
      if (!closed) poll = setTimeout(pollOnce, state_isLive() ? POLL_MS * 2 : Math.max(POLL_MS, backoffMs(attempt)));
    };
    const state_isLive = () => stateRef.current.connection === "live";

    const connect = () => {
      if (typeof EventSource === "undefined") {
        dispatch({ type: "connection", org: orgId, connection: "offline" });
        void pollOnce();
        return;
      }
      source = new EventSource(`${API_BASE}/api/v1/orgs/${orgId}/notifications/stream`, { withCredentials: true });
      source.addEventListener("state", (e) => {
        const parsed = parseStateEvent((e as MessageEvent<string>).data);
        if (closed) return;
        if (parsed) dispatch({ type: "server-state", org: orgId, state: parsed });
        dispatch({ type: "connection", org: orgId, connection: "live" });
      });
      source.addEventListener("closed", () => {
        source?.close();  // the server ended the stream (max age or membership change): reconnect, which re-checks the session
        if (!closed) setTimeout(() => !closed && connect(), 1_000);
      });
      source.onerror = () => !closed && dispatch({ type: "connection", org: orgId, connection: "reconnecting" });  // the browser retries by itself
    };

    void api.getNotificationState(orgId).then((st) => !closed && dispatch({ type: "server-state", org: orgId, state: st })).catch(() => undefined);
    connect();
    poll = setTimeout(pollOnce, POLL_MS);  // safety net while the stream is down; slows to a trickle when it is live
    const onVisible = () => document.visibilityState === "visible" && void api.getNotificationState(orgId).then((st) => !closed && dispatch({ type: "server-state", org: orgId, state: st })).catch(() => undefined);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      closed = true;
      source?.close();
      if (poll) clearTimeout(poll);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [orgId]);

  // The list: loaded when the drawer opens (or the filter changes) and again if something newer arrived while it is open.
  useEffect(() => {
    if (state.open && (state.load === "idle" || state.stale)) void fetchFirstPage();
  }, [state.open, state.load, state.stale, fetchFirstPage]);

  const loadMore = useCallback(async () => {
    const cursor = stateRef.current.nextCursor;
    if (!orgId || !cursor || stateRef.current.loadingMore) return;
    dispatch({ type: "more-start" });
    try {
      dispatch({ type: "more-loaded", org: orgId, page: await api.listNotifications(orgId, { before: cursor, unreadOnly: stateRef.current.filter === "unread" }) });
    } catch {
      dispatch({ type: "more-failed", org: orgId });
    }
  }, [orgId]);

  const markRead = useCallback(async (item: NotificationItem) => {
    if (!orgId || item.is_read) return;
    try {
      const st = await api.markNotificationRead(orgId, item.id);
      dispatch({ type: "marked-read", org: orgId, id: item.id, unread: st.unread_count });
    } catch {
      /* the badge and the row stay as they were; the next state event corrects them */
    }
  }, [orgId]);

  const markAllRead = useCallback(async () => {
    if (!orgId) return;
    try {
      const r = await api.markAllNotificationsRead(orgId);
      dispatch({ type: "marked-all", org: orgId, unread: r.unread_count });
    } catch {
      /* unchanged on failure */
    }
  }, [orgId]);

  return {
    state,
    open: () => dispatch({ type: "open" }),
    close: () => dispatch({ type: "close" }),
    setFilter: (filter: Filter) => dispatch({ type: "filter", filter }),
    retry: fetchFirstPage,
    loadMore,
    markRead,
    markAllRead,
  };
}
