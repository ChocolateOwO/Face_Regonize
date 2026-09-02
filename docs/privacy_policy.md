# Reconize — Privacy Policy

**Last updated:** 25 August 2026

Reconize is an event management and face recognition system used to register
event participants, handle check-in, and organise event photographs. This
policy explains what data the application handles and how Google user data is
used.

---

## 1. Who operates this application

Reconize is operated by the event organiser who installs and runs it. The
application runs locally on the organiser's own computer or server. It is not
offered as a hosted public service.

**Contact:** nicotinnee@gmail.com

---

## 2. What data the application processes

| Data | Purpose | Where it is stored |
|---|---|---|
| Participant name and participant ID | Identifying attendees at check-in | Locally, on the organiser's machine |
| Participant email (optional) | Contacting the participant | Locally |
| Participant reference photograph | Producing a face signature for recognition | Locally |
| Face signature (numeric embedding) | Matching a detected face to a registered participant | Locally |
| Check-in records | Recording event attendance | Locally |
| Consent records (PDPA) | Recording whether a participant agreed to the use of their image | Locally |
| Event photographs | Sorting photographs by participant | Locally, and in Google Drive where the organiser enables it |

The application stores data on the organiser's own machine. It does not
transmit participant data to the application's developers, and does not sell,
rent, or share participant data with third parties.

Face signatures are numeric representations used only for matching. They are
never displayed in the interface and are never sent to any external service.

---

## 3. How Google user data is used

Reconize requests access to Google Drive for two distinct, limited purposes.

### 3.1 Reading event photographs

Where the organiser chooses to process a photographer's Google Drive folder,
the application reads the image files in that folder in order to detect and
recognise faces within them.

This access uses a Google service account. The photographer grants access by
sharing their folder with that service account; view access is sufficient,
because this access is read-only. The application does not modify, move, or
delete the photographer's original files under any circumstance.

### 3.2 Writing processed results

Where the organiser connects their own Google account, the application creates
a new folder in that account's Google Drive and uploads the processed results
into it — photographs sorted by participant, photographs containing no faces,
photographs requiring review, and a media version with non-consented faces
blurred.

This access uses the `drive.file` scope. That scope permits the application to
access **only the files and folders it creates itself**. It does not grant the
application the ability to read, modify, or delete any other file in the
connected Google Drive account.

### 3.3 Limits on Google user data

- Google user data is used solely to provide the photo processing feature
  described above.
- Google user data is not used for advertising.
- Google user data is not sold or transferred to third parties.
- Google user data is not used to train any machine learning model. The face
  recognition model used by this application is a pre-existing model and is
  never modified or retrained by data processed here.
- The application stores a Google refresh token locally so that the organiser
  does not need to sign in repeatedly. It is stored on the organiser's own
  machine and is not transmitted elsewhere.

Reconize's use of information received from Google APIs adheres to the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

---

## 4. Consent and image use (PDPA)

Participants are asked whether they consent to the use of their personal and
facial data for the event. Each consent decision is recorded, together with the
time it was made and whether it was made by the participant or by an
administrator. Earlier decisions are never overwritten, so a complete consent
history is retained.

When event photographs are processed, a separate "media" copy of each
photograph is produced. In that copy, the face of any person who has not
consented is blurred. Faces that cannot be identified are also blurred. Faces
of people who have consented remain visible. The original photograph is never
altered.

Consent is applied at the moment a set of photographs is processed. If a
participant changes their decision afterwards, previously produced media copies
are not altered retrospectively; the new decision applies to any photographs
processed after that point.

---

## 5. Data retention

Each set of processed event photographs is given a retention period of between
one and seven days, chosen by the organiser. When that period expires, the
application automatically deletes its local copies of those photographs and the
associated processing records.

Results already uploaded to Google Drive are not deleted automatically; they
remain in the organiser's Drive account until the organiser removes them.

Participant records, check-in records, and consent records are retained until
the organiser deletes them.

---

## 6. Requesting deletion of your data

To have your personal data, photographs, or consent records removed, contact
the event organiser who operates the installation, or write to
nicotinnee@gmail.com. Administrators can delete an individual participant along
with their photograph, face signature, check-in history, and consent records.

---

## 7. Security

The application stores its data on the organiser's own machine. Administrative
access requires a username and password. Access to participant records,
consent information, and processing controls requires an authenticated
administrator account.

---

## 8. Changes to this policy

Any change to this policy will be published on this page with an updated date.
