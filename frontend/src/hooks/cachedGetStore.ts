type Entry = { data: unknown };

export const cachedGetEntries = new Map<string, Entry>();
export const cachedGetInFlight = new Map<string, Promise<unknown>>();

export function clearCachedGets(): void {
  cachedGetEntries.clear();
  cachedGetInFlight.clear();
}
