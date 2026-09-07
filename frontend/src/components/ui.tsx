import type { InputHTMLAttributes, ReactNode } from "react";

export function PageHeader({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-6">
      <div>
        <h1 className="text-xl font-bold text-gray-900">{title}</h1>
        {subtitle && <p className="text-sm text-gray-500 mt-0.5">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`bg-white border border-gray-200 rounded-xl p-5 ${className}`}>{children}</div>;
}

export function StatCard({ label, value, tone = "default" }: { label: string; value: string | number; tone?: "default" | "good" | "warn" | "bad" }) {
  const toneClass = {
    default: "text-gray-900",
    good: "text-green-600",
    warn: "text-amber-600",
    bad: "text-red-600",
  }[tone];
  return (
    <Card>
      <div className="text-sm text-gray-500">{label}</div>
      <div className={`text-3xl font-bold mt-1 ${toneClass}`}>{value}</div>
    </Card>
  );
}

export function Badge({ children, tone = "default" }: { children: ReactNode; tone?: "default" | "good" | "warn" | "bad" }) {
  const toneClass = {
    default: "bg-gray-100 text-gray-700",
    good: "bg-green-100 text-green-700",
    warn: "bg-amber-100 text-amber-700",
    bad: "bg-red-100 text-red-700",
  }[tone];
  return <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${toneClass}`}>{children}</span>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-gray-500 text-sm py-8 justify-center">
      <div className="w-4 h-4 border-2 border-gray-300 border-t-indigo-600 rounded-full animate-spin" />
      {label || "Loading..."}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="text-center text-gray-400 text-sm py-12">{children}</div>;
}

export function Button({
  children,
  onClick,
  variant = "primary",
  type = "button",
  disabled,
  className = "",
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "secondary" | "danger";
  type?: "button" | "submit";
  disabled?: boolean;
  className?: string;
}) {
  const variantClass = {
    primary: "bg-indigo-600 text-white hover:bg-indigo-700 disabled:bg-gray-300",
    secondary: "bg-white border border-gray-300 text-gray-700 hover:bg-gray-50",
    danger: "bg-red-600 text-white hover:bg-red-700",
  }[variant];
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:cursor-not-allowed ${variantClass} ${className}`}
    >
      {children}
    </button>
  );
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 ${props.className || ""}`}
    />
  );
}

const DEFAULT_PAGE_SIZE_OPTIONS = [20, 50, 100, 200, 500, 1000];

/** Shared list-pagination bar — page-size selector + Prev/Next — for any
 *  page backed by a server-paginated `{items, total, page, page_size}`
 *  endpoint (Recognition History, Upload History). Page-size options match
 *  what the backend accepts; passing a size it does not recognize falls
 *  back to the backend's own default, so this never silently disagrees. */
export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = DEFAULT_PAGE_SIZE_OPTIONS,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  pageSizeOptions?: number[];
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, total);
  return (
    <div className="flex items-center justify-between flex-wrap gap-3 px-4 py-3 border-t border-gray-100 text-sm">
      <div className="text-gray-500">{total === 0 ? "No results" : `Showing ${start}–${end} of ${total}`}</div>
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-2 text-gray-600">
          Per page
          <select
            value={pageSize}
            onChange={(e) => onPageSizeChange(Number(e.target.value))}
            className="px-2 py-1.5 border border-gray-300 rounded-lg text-sm"
          >
            {pageSizeOptions.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <div className="flex items-center gap-1">
          <button
            onClick={() => onPageChange(page - 1)}
            disabled={page <= 1}
            className="px-3 py-1.5 border border-gray-300 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed hover:bg-gray-50"
          >
            Prev
          </button>
          <span className="text-gray-500 px-2">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => onPageChange(page + 1)}
            disabled={page >= totalPages}
            className="px-3 py-1.5 border border-gray-300 rounded-lg disabled:opacity-40 disabled:cursor-not-allowed hover:bg-gray-50"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
