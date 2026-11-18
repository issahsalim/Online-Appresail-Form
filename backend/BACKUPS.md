# Backups

The app creates a password-encrypted ZIP containing a PostgreSQL custom-format dump and the contents of `MEDIA_ROOT`. Backup runs are scheduled for Saturday at 2:00 AM Africa/Accra by Celery Beat. HR users and superusers can start backups from the Backups page.

## Configuration

Set these values in `backend/.env` or the server environment:

```text
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
BACKUP_ENCRYPTION_PASSWORD=use-a-long-random-secret
BACKUP_ROOT=/private/path/outside-the-web-root
BACKUP_RETENTION_DAYS=90
BACKUP_PG_DUMP=pg_dump
```

Keep the encryption password in a password manager or secret store separate from the backups. Without it, the archive cannot be opened. `BACKUP_ROOT` must be private and outside `MEDIA_ROOT` and the public web root. The default is `backend/backups`; that is local storage, not off-site disaster protection. Configure a protected off-site copy for production.

The machine running the Celery worker needs PostgreSQL's `pg_dump` client installed and on `PATH` (or set `BACKUP_PG_DUMP` to its full path). The PostgreSQL user must have permission to dump the configured database.

## Start locally on Windows

From the project root, start Redis in Docker if it is not already running:

```powershell
docker run --name appraisal-redis -p 127.0.0.1:6379:6379 -d redis:7-alpine
```

Install dependencies and apply migrations:

```powershell
.\appvenv\Scripts\python.exe -m pip install -r backend\requirements.txt
Set-Location backend
..\appvenv\Scripts\python.exe manage.py migrate
```

Run these in separate terminals from the `backend` directory:

```powershell
..\appvenv\Scripts\celery.exe -A project worker --pool=solo --loglevel=info
```

```powershell
..\appvenv\Scripts\celery.exe -A project beat --loglevel=info
```

The worker and Beat process must stay running. The Saturday schedule uses Ghana time; manual backups are available from the Backups page.

## Restore reminder

Test restoration in an isolated environment, never over the live database. Open the AES-encrypted ZIP with a compatible tool such as 7-Zip and the backup password. Restore `database.dump` with `pg_restore`, and restore the `media/` directory to the matching media root. Test both records and uploaded files before relying on the backup.