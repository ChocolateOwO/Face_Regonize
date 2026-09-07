import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import ProtectedRoute from "./components/ProtectedRoute";
import Layout from "./components/Layout";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import People from "./pages/People";
import PersonDetail from "./pages/PersonDetail";
import Recognition from "./pages/Recognition";
import Activities from "./pages/Activities";
import CameraNode from "./pages/CameraNode";
import Connect from "./pages/Connect";
import Attendees from "./pages/Attendees";
import Import from "./pages/Import";
import Uploads from "./pages/Uploads";
import UploadDetail from "./pages/UploadDetail";
import History from "./pages/History";
import Reports from "./pages/Reports";
import Settings from "./pages/Settings";
import Pdpa from "./pages/Pdpa";
import PdpaDetail from "./pages/PdpaDetail";
import PhotoBatches from "./pages/PhotoBatches";
import PhotoBatchDetail from "./pages/PhotoBatchDetail";
import Privacy from "./pages/Privacy";

function LoginRoute() {
  const { username } = useAuth();
  if (username) return <Navigate to="/" replace />;
  return <Login />;
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginRoute />} />
      {/* Public — a privacy policy must be readable without signing in. */}
      <Route path="/privacy" element={<Privacy />} />
      <Route
        path="/*"
        element={
          <ProtectedRoute>
            <Layout>
              <Routes>
                <Route path="/" element={<Dashboard />} />
                <Route path="/people" element={<People />} />
                <Route path="/people/:id" element={<PersonDetail />} />
                <Route path="/recognition" element={<Recognition />} />
                <Route path="/attendees" element={<Attendees />} />
                <Route path="/activities" element={<Activities />} />
                {/* The one user-facing camera-monitoring feature: local
                    multi-camera CCTV live view with recognition, per-camera
                    recording, and Activity-gated detection. /camera-node is
                    kept as a compatibility alias for QR deep links
                    (network.py's camera_node_url / mobile_recognition_url) —
                    both render the same page. */}
                <Route path="/cctv" element={<CameraNode />} />
                <Route path="/camera-node" element={<CameraNode />} />
                <Route path="/connect" element={<Connect />} />
                <Route path="/import" element={<Import />} />
                <Route path="/uploads" element={<Uploads />} />
                <Route path="/uploads/:id" element={<UploadDetail />} />
                <Route path="/history" element={<History />} />
                <Route path="/reports" element={<Reports />} />
                <Route path="/settings" element={<Settings />} />
                <Route path="/pdpa" element={<Pdpa />} />
                <Route path="/pdpa/:id" element={<PdpaDetail />} />
                <Route path="/photo-batches" element={<PhotoBatches />} />
                <Route path="/photo-batches/:id" element={<PhotoBatchDetail />} />
              </Routes>
            </Layout>
          </ProtectedRoute>
        }
      />
    </Routes>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
