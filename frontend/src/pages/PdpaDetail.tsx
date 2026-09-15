import { Navigate, useParams } from "react-router-dom";

/** `/pdpa/:id` used to be a second detail page for the same person, showing
 *  only their consent. Consent now lives on the participant page itself, so
 *  there is one canonical place for a person rather than two half-views.
 *
 *  This route stays as a redirect purely so existing bookmarks and any link
 *  already sent to someone keep working — `replace` keeps it out of the back
 *  history, so Back does not bounce the user through it again.
 */
export default function PdpaDetail() {
  const { id } = useParams();
  return <Navigate to={id ? `/people/${id}` : "/pdpa"} replace />;
}
