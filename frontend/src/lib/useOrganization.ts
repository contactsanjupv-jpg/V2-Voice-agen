"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, Organization } from "./api";

export function useOrganization() {
  const router = useRouter();
  const [org, setOrg] = useState<Organization | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .myOrganizations()
      .then((orgs) => {
        if (orgs.length === 0) {
          setError("No organization found on your account.");
          return;
        }
        setOrg(orgs[0]);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          router.push("/get-started");
          return;
        }
        setError("Couldn't reach the server. Is the backend running?");
      })
      .finally(() => setLoading(false));
  }, [router]);

  return { org, loading, error };
}