import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { apiGet } from "../api/client";
import { applyLanguage, helpUrl } from "../language";

const NAV: { to: string; label: string; icon: string; adminOnly?: boolean }[] = [
  { to: "/", label: "Dashboard", icon: "📊" },
  { to: "/people", label: "People", icon: "👥" },
  { to: "/recognition", label: "Face Recognition", icon: "🔍" },
  { to: "/attendees", label: "Attendees", icon: "✅" },
  { to: "/activities", label: "Activities", icon: "🎪" },
  { to: "/cctv", label: "CCTV", icon: "📡" },
  { to: "/local-video-experiment", label: "Local Video Experiment", icon: "🎬", adminOnly: true },
  { to: "/connect", label: "Connect", icon: "📱" },
  { to: "/import", label: "Import Participants", icon: "📥" },
  { to: "/uploads", label: "Upload History", icon: "🖼️" },
  { to: "/history", label: "Recognition History", icon: "🕘" },
  { to: "/reports", label: "Reports", icon: "📈" },
  { to: "/pdpa", label: "PDPA", icon: "🛡️" },
  { to: "/photo-batches", label: "Event Photos", icon: "📷" },
  { to: "/settings", label: "Settings", icon: "⚙️" },
];

// Sidebar-only notice. This renders inside the admin chrome, which the kiosk
// path returns before ever reaching — so the check-in screen is never
// interrupted by an update prompt.
function UpdateBanner() {
  const [version, setVersion] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    apiGet("/api/update/status")
      .then((s) => {
        if (s.update_available) setVersion(s.latest_version);
      })
      .catch(() => {});
  }, []);

  if (!version || dismissed) return null;

  return (
    <div className="mx-2 mt-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2">
      <div className="text-xs font-medium text-amber-900">Update available</div>
      <div className="text-xs text-amber-700 mb-1">v{version}</div>
      <div className="flex gap-2">
        <Link to="/settings" className="text-xs font-medium text-indigo-600 hover:underline">
          View
        </Link>
        <button onClick={() => setDismissed(true)} className="text-xs text-gray-500 hover:text-gray-700">
          Later
        </button>
      </div>
    </div>
  );
}

// Admins only: this install still uses the public default sign-in secret or
// admin password (yes/no from /api/system/security; never a value).
function SecurityBanner({ isAdmin }: { isAdmin: boolean }) {
  const [state, setState] = useState<{ jwt_secret_default: boolean; admin_password_default: boolean; fix: string[] } | null>(null);
  useEffect(() => {
    if (!isAdmin) return;
    apiGet("/api/system/security").then(setState).catch(() => {});
  }, [isAdmin]);
  if (!isAdmin || !state || (!state.jwt_secret_default && !state.admin_password_default)) return null;
  const what = [state.jwt_secret_default && "sign-in secret (JWT_SECRET)", state.admin_password_default && "admin password"]
    .filter(Boolean).join(" and ");
  return (
    <div className="mx-2 mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2" role="alert">
      <div className="text-xs font-semibold text-red-800">Security: public default {what}</div>
      <ol className="text-xs text-red-700 mt-1 list-decimal pl-4 space-y-0.5">
        {state.fix.map((step) => <li key={step}>{step}</li>)}
      </ol>
    </div>
  );
}

export default function Layout({ children }: { children: ReactNode }) {
  const { username, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [isAdmin, setIsAdmin] = useState(false);
  // Below 768 px the menu is a drawer opened from the top bar.
  const [menuOpen, setMenuOpen] = useState(false);
  const language = applyLanguage(location.search);
  useEffect(() => {
    let current = true;
    setIsAdmin(false);
    apiGet("/api/auth/me")
      .then(user => { if (current) setIsAdmin(user.role === "admin"); })
      .catch(() => {});
    return () => { current = false; };
  }, [username]);
  useEffect(() => setMenuOpen(false), [location.pathname]);
  // The Face Recognition screen is a dedicated event check-in kiosk, not a
  // dashboard panel — it gets the entire viewport, with no sidebar, header,
  // or page chrome around it at all.
  const isKiosk = location.pathname.startsWith("/recognition");

  if (isKiosk) {
    return (
      <div className="h-screen w-screen overflow-hidden" style={{ background: "#000" }}>
        {children}
      </div>
    );
  }

  return (
    // h-screen + overflow-hidden pins the shell to the viewport so the page
    // itself never scrolls; scrolling is delegated to the main column below.
    // That is what keeps the sidebar fixed on long pages like Settings.
    <div className="h-screen flex flex-col md:flex-row overflow-hidden" style={{ background: "var(--color-bg)" }}>
      <header className="md:hidden flex items-center justify-between px-4 py-2 bg-white border-b border-gray-200 shrink-0">
        <div className="text-base font-bold text-indigo-600">Reconize</div>
        <button
          type="button"
          onClick={() => setMenuOpen((v) => !v)}
          aria-label={menuOpen ? "Close menu" : "Open menu"}
          aria-expanded={menuOpen}
          aria-controls="reconize-nav"
          className="px-3 py-2 rounded-lg text-sm font-medium text-gray-700 hover:bg-gray-100 border border-gray-200"
        >
          {menuOpen ? "✕" : "☰"} Menu
        </button>
      </header>
      {menuOpen && (
        <div className="md:hidden fixed inset-0 z-30 bg-black/30" aria-hidden="true" onClick={() => setMenuOpen(false)} />
      )}
      <aside
        id="reconize-nav"
        className={`w-64 shrink-0 bg-white border-r border-gray-200 flex flex-col h-full
          fixed inset-y-0 left-0 z-40 transition-transform md:static md:translate-x-0 md:z-auto
          ${menuOpen ? "translate-x-0" : "-translate-x-full"}`}
      >
        <div className="px-5 py-4 border-b border-gray-200">
          <div className="text-lg font-bold text-indigo-600">Reconize</div>
          <div className="text-xs text-gray-500">Event Management &amp; AI Recognition</div>
          <a
            href={helpUrl(language)}
            target="_blank"
            rel="noreferrer"
            className="mt-2 inline-flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium text-indigo-700 bg-indigo-50 hover:bg-indigo-100"
          >
            <span aria-hidden="true">❓</span>
            Help
          </a>
        </div>
        <SecurityBanner isAdmin={isAdmin} />
        <UpdateBanner />
        <nav className="flex-1 py-3 px-2 space-y-1 overflow-y-auto">
          {NAV.filter(item => !item.adminOnly || isAdmin).map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive ? "bg-indigo-600 text-white" : "text-gray-700 hover:bg-gray-100"
                }`
              }
            >
              <span>{item.icon}</span>
              <span>{item.label}{item.adminOnly && <span className="block text-xs font-normal opacity-80">Admin experiment</span>}</span>
            </NavLink>
          ))}
        </nav>
        <div className="p-3 border-t border-gray-200">
          <div className="text-xs text-gray-500 px-2 mb-2">Signed in as {username}</div>
          <button
            onClick={() => {
              logout();
              navigate("/login");
            }}
            className="w-full text-left px-3 py-2 rounded-lg text-sm font-medium text-red-600 hover:bg-red-50"
          >
            🚪 Logout
          </button>
        </div>
      </aside>
      <main className="flex-1 min-w-0 min-h-0 overflow-y-auto">
        <div className="max-w-6xl mx-auto p-4 md:p-6">{children}</div>
      </main>
    </div>
  );
}
