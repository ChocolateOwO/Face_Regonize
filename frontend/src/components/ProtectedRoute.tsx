import { useEffect, useState, type ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { ensureFileSession } from "../auth/fileSession";

export default function ProtectedRoute({ children }: { children: ReactNode }) {
  const { username } = useAuth();
  // Wait for the file-read cookie (one quick request per page load) so the
  // first render's <img> tags are already authorized.
  const [filesReady, setFilesReady] = useState(false);
  useEffect(() => {
    if (!username) return;
    let live = true;
    ensureFileSession().then(() => {
      if (live) setFilesReady(true);
    });
    return () => {
      live = false;
    };
  }, [username]);
  if (!username) return <Navigate to="/login" replace />;
  if (!filesReady) return null;
  return <>{children}</>;
}
