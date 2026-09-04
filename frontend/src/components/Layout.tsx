import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { apiGet } from "../api/client";

const NAV = [
  { to: "/", label: "Dashboard", icon: "📊" },
  { to: "/people", label: "People", icon: "👥" },
  { to: "/recognition", label: "Face Recognition", icon: "🔍" },
  { to: "/attendees", label: "Attendees", icon: "✅" },
  { to: "/activities", label: "Activities", icon: "🎪" },
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

export default function Layout({ children }: { children: ReactNode }) {
  const { username, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
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
    <div className="h-screen flex overflow-hidden" style={{ background: "var(--color-bg)" }}>
      <aside className="w-64 shrink-0 bg-white border-r border-gray-200 flex flex-col h-full">
        <div className="px-5 py-4 border-b border-gray-200">
          <div className="text-lg font-bold text-indigo-600">Reconize</div>
          <div className="text-xs text-gray-500">Event Management &amp; AI Recognition</div>
        </div>
        <UpdateBanner />
        <nav className="flex-1 py-3 px-2 space-y-1 overflow-y-auto">
          {NAV.map((item) => (
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
              {item.label}
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
      <main className="flex-1 min-w-0 overflow-y-auto">
        <div className="max-w-6xl mx-auto p-6">{children}</div>
      </main>
    </div>
  );
}
