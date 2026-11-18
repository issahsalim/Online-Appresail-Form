# First-time Employee Registration

HR users and superusers manage approved staff IDs at `/hr/staff-ids/`. Add each staff ID with the employee's work email, or upload a CSV/XLSX file with `staff_id` and `email` columns. Keep staff IDs formatted as text in Excel so leading zeroes are preserved. Registered IDs cannot be claimed again.

Employees choose **Create your staff account** on the sign-in page, enter their staff ID, and verify a six-digit code sent to the pre-approved work email. The code expires after 10 minutes and allows up to five attempts. After verification, the employee sets a password, profile details, and a secret question and answer. The account role is always `Employee`; the user cannot choose a role or email address.

## Email setup

Locally, Django uses the console mailer by default. The verification code appears in the Celery worker terminal. For real email delivery, add SMTP settings to `backend/.env` and restart the web process and Celery worker:

```text
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.org
EMAIL_PORT=587
EMAIL_USE_TLS=true
EMAIL_HOST_USER=your-mailbox
EMAIL_HOST_PASSWORD=your-mailbox-password
DEFAULT_FROM_EMAIL=Appraisal System <noreply@example.org>
```

Use your organization's SMTP provider and keep its password secret. Redis and the Celery worker must be running because the email is sent as a background task.

Apply the new database migration before using registration:

```powershell
cd backend
..\appvenv\Scripts\python.exe manage.py migrate
```

Forgotten-password recovery is not included in this feature yet.
