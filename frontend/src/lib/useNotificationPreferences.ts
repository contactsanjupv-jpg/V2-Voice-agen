"use client";

import { useCallback, useEffect, useReducer } from "react";
import { api } from "./api";
import { initialPrefsFor, prefsReducer, type NotificationPreference } from "./notificationPreferences";
import type { NotificationCategory } from "./notifications";

/** Loads the person's own preferences when `active` (the preferences view is open) and saves each switch optimistically. */
export function useNotificationPreferences(orgId: string | undefined, active: boolean) {
  const [raw, dispatch] = useReducer(prefsReducer, orgId ?? null, initialPrefsFor);
  const state = raw.orgId === (orgId ?? null) ? raw : initialPrefsFor(orgId ?? null);  // the render that sees a new organization is already empty
  if (raw.orgId !== (orgId ?? null)) dispatch({ type: "switch-org", orgId: orgId ?? null });

  const load = useCallback(async () => {
    if (!orgId) return;
    dispatch({ type: "load-start" });
    try {
      const items = await api.getNotificationPreferences(orgId);
      dispatch({ type: "loaded", org: orgId, items });
    } catch {
      dispatch({ type: "load-failed", org: orgId });
    }
  }, [orgId]);

  useEffect(() => {
    if (!active || !orgId) return;
    const t = setTimeout(() => void load(), 0);
    return () => clearTimeout(t);
  }, [active, orgId, load]);

  const setEnabled = useCallback(
    async (category: NotificationCategory, enabled: boolean) => {
      if (!orgId) return;
      const previous = state.items.find((p) => p.category === category)?.enabled ?? true;
      dispatch({ type: "toggle", org: orgId, category, enabled });
      try {
        const item: NotificationPreference = await api.setNotificationPreference(orgId, category, enabled);
        dispatch({ type: "saved", org: orgId, item });
      } catch {
        dispatch({ type: "save-failed", org: orgId, category, previous });
      }
    },
    [orgId, state.items],
  );

  return { state, setEnabled, retry: load };
}
