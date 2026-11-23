import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pyzipper
from django.conf import settings
from django.utils import timezone


class BackupCreationError(Exception):
    pass


def create_encrypted_backup(archive_path):
    password = settings.BACKUP_ENCRYPTION_PASSWORD
    if not password:
        raise BackupCreationError('BACKUP_ENCRYPTION_PASSWORD is not configured.')

    archive_path = Path(archive_path)
    media_root = Path(settings.MEDIA_ROOT).resolve()
    backup_root = Path(settings.BACKUP_ROOT).resolve()
    if backup_root == media_root or backup_root.is_relative_to(media_root):
        raise BackupCreationError('Backup storage must not be inside MEDIA_ROOT.')

    database = settings.DATABASES['default']
    command = [
        settings.BACKUP_PG_DUMP,
        '--format=custom',
        '--no-owner',
        '--no-privileges',
        '--no-password',
        '--dbname',
        str(database['NAME']),
    ]
    if database.get('HOST'):
        command.extend(['--host', str(database['HOST'])])
    if database.get('PORT'):
        command.extend(['--port', str(database['PORT'])])
    if database.get('USER'):
        command.extend(['--username', str(database['USER'])])

    process_environment = os.environ.copy()
    process_environment['PGPASSWORD'] = str(database.get('PASSWORD', ''))
    media_files = sorted(
        path for path in media_root.rglob('*')
        if path.is_file() and not path.is_symlink()
    ) if media_root.is_dir() else []

    with pyzipper.AESZipFile(
        archive_path,
        'w',
        compression=pyzipper.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        archive.setpassword(password.encode('utf-8'))
        archive.setencryption(pyzipper.WZ_AES, nbits=256)
        with tempfile.TemporaryFile() as error_stream:
            with subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=error_stream,
                env=process_environment,
            ) as process:
                with archive.open('database.dump', 'w') as database_stream:
                    shutil.copyfileobj(process.stdout, database_stream, 1024 * 1024)
                return_code = process.wait()
            if return_code:
                error_stream.seek(0)
                error = error_stream.read().decode('utf-8', errors='replace').strip()
                raise BackupCreationError(error or 'pg_dump failed.')

        for media_file in media_files:
            archive.write(
                media_file,
                f"media/{media_file.relative_to(media_root).as_posix()}",
            )

        archive.writestr(
            'manifest.json',
            json.dumps({
                'created_at': timezone.now().isoformat(),
                'database_name': str(database['NAME']),
                'database_dump': 'database.dump',
                'media_files': len(media_files),
                'media_directory': 'media/',
            }, indent=2),
        )

    digest = hashlib.sha256()
    with archive_path.open('rb') as archive_file:
        for chunk in iter(lambda: archive_file.read(1024 * 1024), b''):
            digest.update(chunk)
    return archive_path.stat().st_size, digest.hexdigest()