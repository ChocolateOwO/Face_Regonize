import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { apiPostJson } from "../api/client";

interface AuthState {
  username: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [username, setUsername] = useState<string | null>(localStorage.getItem("username"));

  async function login(username: string, password: string) {
    const data = await apiPostJson("/api/auth/login", { username, password });
    localStorage.setItem("token", data.access_token);
    localStorage.setItem("username", data.username);
    setUsername(data.username);
  }

  function logout() {
    localStorage.removeItem("token");
    localStorage.removeItem("username");
    setUsername(null);
  }

  // Fired by api/client.ts whenever any request comes back 401 (expired/
  // invalid token). Clearing state here — instead of a hard page reload —
  // lets ProtectedRoute redirect to /login via normal client-side routing,
  // with no risk of a reload loop.
  useEffect(() => {
    window.addEventListener("auth:unauthorized", logout);
    return () => window.removeEventListener("auth:unauthorized", logout);
  }, []);

  return <AuthContext.Provider value={{ username, login, logout }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
