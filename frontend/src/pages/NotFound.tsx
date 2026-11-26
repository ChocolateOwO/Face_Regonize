import { Link, useLocation } from "react-router-dom";
import { Card } from "../components/ui";

/** Any address the app does not have (instead of an empty page). */
export default function NotFound() {
  const location = useLocation();
  return (
    <div className="max-w-lg mx-auto mt-10">
      <Card>
        <div className="text-center py-6">
          <div className="text-4xl mb-2" aria-hidden="true">🧭</div>
          <h1 className="text-xl font-bold text-gray-900">Page not found</h1>
          <p className="text-sm text-gray-500 mt-2 break-all">
            There is no page at <code>{location.pathname}</code>.
          </p>
          <Link
            to="/"
            className="inline-block mt-5 px-4 py-2 rounded-lg text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700"
          >
            Go to the Dashboard
          </Link>
        </div>
      </Card>
    </div>
  );
}
