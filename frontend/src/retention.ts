// Event Photo retention choices, shared by Settings (default for new
// batches), the New Batch form and a batch's Change Retention dialog.
// "never" is a real policy the backend stores on the batch - no deadline at
// all - not a long number of days.

export const RETENTION_NEVER = "never";
export const RETENTION_NEVER_LABEL = "Never / ไม่ลบอัตโนมัติ";
export const DEFAULT_RETENTION_CHOICE = "7";

export const RETENTION_CHOICES: { value: string; label: string }[] = [
  ...[1, 2, 3, 4, 5, 6, 7].map((d) => ({ value: String(d), label: `${d} Day${d === 1 ? "" : "s"}` })),
  { value: RETENTION_NEVER, label: RETENTION_NEVER_LABEL },
];

export function isRetentionChoice(value: unknown): value is string {
  return typeof value === "string" && RETENTION_CHOICES.some((c) => c.value === value);
}

/** What the API expects for a choice: an explicit policy, plus days when finite. */
export function retentionPayload(choice: string): { retention_policy: "never" } | { retention_policy: "days"; retention_days: number } {
  return choice === RETENTION_NEVER
    ? { retention_policy: "never" }
    : { retention_policy: "days", retention_days: Number(choice) };
}

/** The choice a saved batch currently has. */
export function retentionChoiceOf(batch: { retention_policy?: string; retention_days: number; delete_at: string | null }): string {
  return batch.retention_policy === RETENTION_NEVER || batch.delete_at === null ? RETENTION_NEVER : String(batch.retention_days);
}

export function retentionLabel(choice: string): string {
  return RETENTION_CHOICES.find((c) => c.value === choice)?.label ?? choice;
}
