import { Link } from "react-router-dom";

// Public page — deliberately outside ProtectedRoute. A privacy policy has to
// be readable without signing in, both because that is the point of one and
// because Google's OAuth review requires the linked policy to be reachable
// without authentication.
//
// Source of truth for this text is docs/privacy_policy.md — keep the two in
// sync when either changes.

const UPDATED = "25 August 2026";
const CONTACT = "nicotinnee@gmail.com";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="text-lg font-semibold text-gray-900 mb-2">{title}</h2>
      <div className="space-y-3 text-sm text-gray-700 leading-relaxed">{children}</div>
    </section>
  );
}

export default function Privacy() {
  return (
    <div className="min-h-screen" style={{ background: "var(--color-bg)" }}>
      <div className="max-w-3xl mx-auto px-6 py-10">
        <div className="flex items-baseline justify-between flex-wrap gap-3 mb-2">
          <div>
            <div className="text-2xl font-bold text-indigo-600">Reconize</div>
            <div className="text-sm text-gray-500">Event Management &amp; Face Recognition</div>
          </div>
          <Link to="/login" className="text-sm text-indigo-600 hover:underline">
            ← Back to sign in
          </Link>
        </div>

        <div className="bg-white border border-gray-200 rounded-xl p-8 shadow-sm mt-6">
          <h1 className="text-2xl font-bold text-gray-900">Privacy Policy</h1>
          <p className="text-xs text-gray-400 mt-1">Last updated: {UPDATED}</p>

          <p className="text-sm text-gray-700 leading-relaxed mt-5">
            Reconize is an event management and face recognition system used to register event participants, handle
            check-in, and organise event photographs. This policy explains what data the application handles and how
            Google user data is used.
          </p>

          <Section title="1. Who operates this application">
            <p>
              Reconize is operated by the event organiser who installs and runs it. The application runs locally on the
              organiser's own computer or server. It is not offered as a hosted public service.
            </p>
            <p>
              Contact: <a className="text-indigo-600 hover:underline" href={`mailto:${CONTACT}`}>{CONTACT}</a>
            </p>
          </Section>

          <Section title="2. What data the application processes">
            <div className="overflow-x-auto">
              <table className="w-full text-sm border border-gray-200 rounded-lg">
                <thead className="bg-gray-50 text-gray-500 text-xs uppercase">
                  <tr>
                    <th className="text-left px-3 py-2">Data</th>
                    <th className="text-left px-3 py-2">Purpose</th>
                    <th className="text-left px-3 py-2">Stored</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {[
                    ["Name and participant ID", "Identifying attendees at check-in", "Locally"],
                    ["Email (optional)", "Contacting the participant", "Locally"],
                    ["Reference photograph", "Producing a face signature for recognition", "Locally"],
                    ["Face signature (numeric)", "Matching a detected face to a participant", "Locally"],
                    ["Check-in records", "Recording event attendance", "Locally"],
                    ["Consent records (PDPA)", "Recording agreement to use of their image", "Locally"],
                    ["Event photographs", "Sorting photographs by participant", "Locally, and Google Drive if enabled"],
                  ].map(([a, b, c]) => (
                    <tr key={a}>
                      <td className="px-3 py-2 text-gray-900">{a}</td>
                      <td className="px-3 py-2 text-gray-600">{b}</td>
                      <td className="px-3 py-2 text-gray-600">{c}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p>
              The application stores data on the organiser's own machine. It does not transmit participant data to the
              application's developers, and does not sell, rent, or share participant data with third parties.
            </p>
            <p>
              Face signatures are numeric representations used only for matching. They are never displayed in the
              interface and are never sent to any external service.
            </p>
          </Section>

          <Section title="3. How Google user data is used">
            <p className="font-medium text-gray-900">3.1 Reading event photographs</p>
            <p>
              Where the organiser chooses to process a photographer's Google Drive folder, the application reads the
              image files in that folder in order to detect and recognise faces within them. This access uses a Google
              service account; the photographer grants access by sharing their folder with it, and view access is
              sufficient because this access is read-only. The application does not modify, move, or delete the
              photographer's original files under any circumstance.
            </p>

            <p className="font-medium text-gray-900 pt-2">3.2 Writing processed results</p>
            <p>
              Where the organiser connects their own Google account, the application creates a new folder in that
              account's Google Drive and uploads the processed results into it — photographs sorted by participant,
              photographs containing no faces, photographs requiring review, and a media version with non-consented
              faces blurred.
            </p>
            <p>
              This access uses the <code className="bg-gray-100 px-1 rounded">drive.file</code> scope, which permits the
              application to access <strong>only the files and folders it creates itself</strong>. It does not grant the
              ability to read, modify, or delete any other file in the connected Google Drive account.
            </p>

            <p className="font-medium text-gray-900 pt-2">3.3 Limits on Google user data</p>
            <ul className="list-disc pl-5 space-y-1">
              <li>Google user data is used solely to provide the photo processing feature described above.</li>
              <li>Google user data is not used for advertising.</li>
              <li>Google user data is not sold or transferred to third parties.</li>
              <li>
                Google user data is not used to train any machine learning model. The face recognition model used is a
                pre-existing model and is never modified or retrained by data processed here.
              </li>
              <li>
                A Google refresh token is stored locally so the organiser need not sign in repeatedly. It stays on the
                organiser's own machine and is not transmitted elsewhere.
              </li>
            </ul>
            <p>
              Reconize's use of information received from Google APIs adheres to the{" "}
              <a
                className="text-indigo-600 hover:underline"
                href="https://developers.google.com/terms/api-services-user-data-policy"
                target="_blank"
                rel="noreferrer"
              >
                Google API Services User Data Policy
              </a>
              , including the Limited Use requirements.
            </p>
          </Section>

          <Section title="4. Consent and image use (PDPA)">
            <p>
              Participants are asked whether they consent to the use of their personal and facial data for the event.
              Each decision is recorded with the time it was made and whether it was made by the participant or by an
              administrator. Earlier decisions are never overwritten, so a complete consent history is retained.
            </p>
            <p>
              When event photographs are processed, a separate media copy of each photograph is produced. In that copy,
              the face of any person who has not consented is blurred, as is any face that cannot be identified. Faces
              of people who have consented remain visible. The original photograph is never altered.
            </p>
            <p>
              Consent is applied at the moment a set of photographs is processed. If a participant changes their
              decision afterwards, previously produced media copies are not altered retrospectively; the new decision
              applies to photographs processed after that point.
            </p>
          </Section>

          <Section title="5. Data retention">
            <p>
              Each set of processed event photographs is given a retention period of between one and seven days, chosen
              by the organiser. When that period expires, the application automatically deletes its local copies of
              those photographs and the associated processing records.
            </p>
            <p>
              Results already uploaded to Google Drive are not deleted automatically; they remain in the organiser's
              Drive account until the organiser removes them.
            </p>
            <p>Participant records, check-in records, and consent records are retained until the organiser deletes them.</p>
          </Section>

          <Section title="6. Requesting deletion of your data">
            <p>
              To have your personal data, photographs, or consent records removed, contact the event organiser who
              operates the installation, or write to{" "}
              <a className="text-indigo-600 hover:underline" href={`mailto:${CONTACT}`}>{CONTACT}</a>. Administrators can
              delete an individual participant along with their photograph, face signature, check-in history, and
              consent records.
            </p>
          </Section>

          <Section title="7. Security">
            <p>
              The application stores its data on the organiser's own machine. Administrative access requires a username
              and password. Access to participant records, consent information, and processing controls requires an
              authenticated administrator account.
            </p>
          </Section>

          <Section title="8. Changes to this policy">
            <p>Any change to this policy will be published on this page with an updated date.</p>
          </Section>
        </div>

        <div className="text-xs text-gray-400 text-center mt-6">Reconize — Event Management &amp; Face Recognition</div>
      </div>
    </div>
  );
}
