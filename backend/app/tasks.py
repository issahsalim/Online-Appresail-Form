import logging
import smtplib
from datetime import timedelta
from email.mime.image import MIMEImage
from pathlib import Path

import redis
from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.contrib.staticfiles import finders
from django.db import OperationalError
from django.template.loader import render_to_string
from django.utils import timezone

from .backup_service import BackupCreationError, create_encrypted_backup
from .models import BackupRun, PasswordResetCode, RegistrationCode

logger = logging.getLogger(__name__)


@shared_task(
    name='app.tasks.send_registration_code',
    autoretry_for=(OperationalError, smtplib.SMTPException, ConnectionError, TimeoutError),
    retry_backoff=True,
    max_retries=3,
)
def send_registration_code(registration_code_id, code):
    registration_code = RegistrationCode.objects.select_related('staff_id').filter(
        pk=registration_code_id,
    ).first()
    if registration_code is None:
        logger.warning(
            'Skipping registration email: registration code %s no longer exists.',
            registration_code_id,
        )
        return 'missing'
    if registration_code.verified_at or registration_code.expires_at <= timezone.now():
        return 'expired'

    text_body = (
        'Hello,\n\n'
        f'Your staff account verification code is {code}. It expires in 10 minutes.\n\n'
        'For your security, do not share this code with anyone. '
        'If you did not request an account, you can ignore this email.'
    )
    html_body = render_to_string('emails/registration_code.html', {'code': code})
    logo_path = finders.find('images/mathias-catholic-hospital-logo.png')
    if not logo_path:
        raise FileNotFoundError('Registration email logo was not found in static files.')

    email = EmailMultiAlternatives(
        'Your staff account verification code',
        text_body,
        settings.DEFAULT_FROM_EMAIL,
        [registration_code.staff_id.email],
    )
    email.attach_alternative(html_body, 'text/html')
    with open(logo_path, 'rb') as logo_file:
        logo = MIMEImage(logo_file.read(), _subtype='png')
    logo.add_header('Content-ID', '<registration-code-logo>')
    logo.add_header(
        'Content-Disposition',
        'inline',
        filename='mathias-catholic-hospital-logo.png',
    )
    email.attach(logo)
    email.send(fail_silently=False)
    return 'sent'


@shared_task(
    name='app.tasks.send_password_reset_code',
    autoretry_for=(OperationalError, smtplib.SMTPException, ConnectionError, TimeoutError),
    retry_backoff=True,
    max_retries=3,
)
def send_password_reset_code(password_reset_code_id, code):
    reset_code = PasswordResetCode.objects.select_related(
        'staff_id__registered_user',
    ).filter(pk=password_reset_code_id).first()
    if reset_code is None:
        logger.warning(
            'Skipping password reset email: reset code %s no longer exists.',
            password_reset_code_id,
        )
        return 'missing'
    if reset_code.verified_at or reset_code.expires_at <= timezone.now():
        return 'expired'

    user = reset_code.staff_id.registered_user
    if not user or not reset_code.staff_id.email:
        return 'invalid'

    name = user.first_name or user.username
    text_body = (
        f'Hello {name},\n\n'
        f'Your password reset code is {code}. It expires in 10 minutes.\n\n'
        'If you did not request this code, you can ignore this email.'
    )
    html_body = render_to_string('emails/password_reset_code.html', {
        'name': name,
        'code': code,
    })
    logo_path = finders.find('images/mathias-catholic-hospital-logo.png')
    if not logo_path:
        raise FileNotFoundError('Password reset email logo was not found in static files.')

    email = EmailMultiAlternatives(
        'Your password reset code',
        text_body,
        settings.DEFAULT_FROM_EMAIL,
        [reset_code.staff_id.email],
    )
    email.attach_alternative(html_body, 'text/html')
    with open(logo_path, 'rb') as logo_file:
        logo = MIMEImage(logo_file.read(), _subtype='png')
    logo.add_header('Content-ID', '<password-reset-logo>')
    logo.add_header(
        'Content-Disposition',
        'inline',
        filename='mathias-catholic-hospital-logo.png',
    )
    email.attach(logo)
    email.send(fail_silently=False)
    return 'sent'


@shared_task(name='app.tasks.create_backup', acks_late=True, reject_on_worker_lost=True)
def create_backup(backup_run_id=None):
    if backup_run_id is None:
        backup_run = BackupRun.objects.create(trigger_type='scheduled')
    else:
        backup_run = BackupRun.objects.get(pk=backup_run_id)

    if backup_run.status != 'queued':
        return {'status': backup_run.status, 'backup_run_id': backup_run.pk}

    lock = None
    acquired = False
    try:
        lock = redis.Redis.from_url(settings.CELERY_BROKER_URL).lock(
            'appraisal-backup-lock',
            timeout=86400,
        )
        acquired = lock.acquire(blocking=False)
        if not acquired:
            backup_run.status = 'failed'
            backup_run.completed_at = timezone.now()
            backup_run.error = 'Another backup is already running.'
            backup_run.save(update_fields=['status', 'completed_at', 'error'])
            return {'status': backup_run.status, 'backup_run_id': backup_run.pk}

        backup_run.status = 'running'
        backup_run.started_at = timezone.now()
        backup_run.save(update_fields=['status', 'started_at'])

        backup_root = Path(settings.BACKUP_ROOT)
        backup_root.mkdir(parents=True, exist_ok=True)
        try:
            backup_root.chmod(0o700)
        except OSError:
            logger.warning('Could not restrict permissions on backup directory %s', backup_root)

        timestamp = timezone.now().strftime('%Y%m%dT%H%M%SZ')
        archive_name = f'appraisal-backup-{timestamp}-{backup_run.pk}.zip'
        archive_path = backup_root / archive_name
        partial_path = backup_root / f'.{archive_name}.partial'

        try:
            size, checksum = create_encrypted_backup(partial_path)
            try:
                partial_path.chmod(0o600)
            except OSError:
                logger.warning('Could not restrict permissions on backup archive %s', partial_path)
            partial_path.replace(archive_path)
        finally:
            partial_path.unlink(missing_ok=True)

        backup_run.status = 'succeeded'
        backup_run.completed_at = timezone.now()
        backup_run.archive_name = archive_name
        backup_run.archive_size_bytes = size
        backup_run.sha256 = checksum
        backup_run.error = ''
        backup_run.save(update_fields=[
            'status', 'completed_at', 'archive_name', 'archive_size_bytes', 'sha256', 'error',
        ])
        try:
            _remove_expired_backups(backup_root, backup_run.pk)
        except Exception:
            logger.exception('Could not complete backup retention cleanup.')
        return {'status': backup_run.status, 'backup_run_id': backup_run.pk}
    except Exception as error:
        logger.exception('Backup run %s failed.', backup_run.pk)
        backup_run.status = 'failed'
        backup_run.completed_at = timezone.now()
        if isinstance(error, FileNotFoundError):
            backup_run.error = 'pg_dump was not found. Install PostgreSQL tools or set BACKUP_PG_DUMP.'
        elif isinstance(error, BackupCreationError):
            backup_run.error = str(error)[:2000]
        else:
            backup_run.error = 'Backup failed. Check the Celery worker log.'
        backup_run.save(update_fields=['status', 'completed_at', 'error'])
        raise
    finally:
        if acquired:
            try:
                lock.release()
            except redis.exceptions.LockError:
                logger.warning('Backup lock expired before it could be released.')


def _remove_expired_backups(backup_root, current_run_id):
    retention_days = max(1, settings.BACKUP_RETENTION_DAYS)
    cutoff = timezone.now() - timedelta(days=retention_days)
    expired_runs = BackupRun.objects.filter(
        status='succeeded',
        completed_at__lt=cutoff,
    ).exclude(pk=current_run_id)

    for expired_run in expired_runs:
        if expired_run.archive_name:
            archive_path = backup_root / Path(expired_run.archive_name).name
            try:
                archive_path.unlink(missing_ok=True)
            except OSError:
                logger.exception('Could not remove expired backup %s.', archive_path)
                continue
        expired_run.delete()
