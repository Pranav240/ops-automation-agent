const LABELS: Record<string, string> = {
  pending_approval: "Needs approval",
  done: "Done",
  rejected: "Rejected",
  blocked: "Blocked",
  failed: "Failed",
  error: "Error",
};

export function statusLabel(status: string | null): string {
  return status ? (LABELS[status] ?? status) : "Unknown";
}

export function money(value: number | null | undefined): string {
  return value == null ? "—" : `$${value.toFixed(2)}`;
}

export function cost(value: number | null | undefined): string {
  return value == null ? "—" : `$${value.toFixed(4)}`;
}

export function time(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function seconds(value: number | null): string {
  return value == null ? "waiting" : `${value.toFixed(1)} s`;
}
