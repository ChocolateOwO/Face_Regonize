import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../api/client";
import { cachedGetEntries, cachedGetInFlight } from "./cachedGetStore";

function keyFor(path: string): string {
  return `${localStorage.getItem("token") ?? "anonymous"}:${path}`;
}

async function request<T>(key: string, path: string): Promise<T> {
  const existing = cachedGetInFlight.get(key);
  if (existing) return existing as Promise<T>;
  const promise = apiGet(path)
    .then((data) => {
      cachedGetEntries.set(key, { data });
      return data;
    })
    .finally(() => cachedGetInFlight.delete(key));
  cachedGetInFlight.set(key, promise);
  return promise as Promise<T>;
}

/** Shows the previous read result on return navigation and always revalidates. */
export function useCachedGet<T>(path: string) {
  const key = keyFor(path);
  const [snapshot, setSnapshot] = useState<{ key: string; data: T | null }>(() => ({
    key,
    data: (cachedGetEntries.get(key)?.data as T | undefined) ?? null,
  }));
  const refresh = useCallback(async () => {
    const data = await request<T>(key, path);
    setSnapshot({ key, data });
    return data;
  }, [key, path]);

  useEffect(() => {
    const cached = cachedGetEntries.get(key)?.data as T | undefined;
    setSnapshot({ key, data: cached ?? null });
    void refresh();
  }, [key, refresh]);

  return { data: snapshot.key === key ? snapshot.data : ((cachedGetEntries.get(key)?.data as T | undefined) ?? null), refresh };
}
