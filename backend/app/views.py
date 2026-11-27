import csv
import io
import logging
from datetime import timedelta
from pathlib import Path
import secrets
from urllib.parse import urlencode

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.forms import SetPasswordForm
from django.conf import settings
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, Count, F, Q, Value, When
from PIL import Image
from django.db import transaction
from django.db.models import Avg, Case, CharField, Count, F, Q, Value, When
from django.urls import reverse
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.views.decorators.http import require_POST

from .models import (
    Appraisal,
    BackupRun,
    AppraisalCompetencyStatementScore,
    AppraisalCycle,
    AppNotification,
    AuditLog,
    AppraisalReturn,
    AppraisalReturnRequest,
    AssignDepartmentHod,
    DepartmentSupervisor,
    Competency,
    Department,
    EmployeeProfile,
    PasswordResetCode,
    PreviousAppraisal,
    RegistrationCode,
    StaffId,
    SupervisorReporting,
    get_assigned_supervisor,
)
from .forms import (
    AppraiserReviewForm,
    AppraisalEmployeeInfoForm,
    AppraisalReturnForm,
    AppraisalReturnDecisionForm,
    AppraisalReturnRequestForm,
    AppraisalTrainingFormSet,
    EmployeeCompetencySelectionForm,
    HODCommentForm,
    HODAppraisalTargetScoringFormSet,
    HRCommentForm,
    HRDepartmentHodAssignmentForm,
    HRSupervisorAssignmentForm,
    ProfilePhotoForm,
    SupervisorReportingForm,
    SupervisorCompetencyFormSet,
    SupervisorCompetencyStatementFormSet,
    SupervisorTargetFormSet,
    TargetFormSet,
    UserProfileForm,
    AppraisalCycleForm,
    PreviousAppraisalUploadForm,
    EmployeeRegistrationForm,
    PasswordResetRequestForm,
    PasswordResetAnswerForm,
    PasswordResetCodeForm,
    RegistrationCodeForm,
    RegistrationStaffIdForm,
    StaffIdEntryForm,
    StaffIdUploadForm,
)
from .notifications import mark_as_read, notify_users
from .notification_cache import (
    get_unread_notification_count,
    invalidate_notification_cache,
)
from .audit import record_auth_event
from .tasks import create_backup, send_password_reset_code, send_registration_code

logger = logging.getLogger(__name__)
User = get_user_model()


def _process_hr_supervisor_assignment(request):
    form = HRSupervisorAssignmentForm(request.POST)
    if not form.is_valid():
        return form, None, None

    department = form.cleaned_data['department']
    supervisor = form.cleaned_data['supervisor']
    employees = EmployeeProfile.objects.filter(
        role='Employee',
        department=department,
    )
    appraisals_to_transfer = Appraisal.objects.filter(
        employee__in=employees,
        status__in=['pending_manager', 'returned_supervisor'],
    ).exclude(supervisor=supervisor)

    if appraisals_to_transfer.exists() and request.POST.get('confirm_transfer') != '1':
        confirmation = {
            'department_id': department.id,
            'supervisor_id': supervisor.id,
            'department_name': department.name,
            'supervisor_name': supervisor.user.get_full_name() or supervisor.user.username,
            'appraisal_count': appraisals_to_transfer.count(),
        }
        return form, confirmation, None

    previous_assignment = DepartmentSupervisor.objects.filter(
        department=department,
    ).select_related('supervisor__user').first()
    previous_supervisor = previous_assignment.supervisor if previous_assignment else None
    supervisor_changed = (
        previous_assignment is None
        or previous_assignment.supervisor_id != supervisor.id
    )

    with transaction.atomic():
        DepartmentSupervisor.objects.update_or_create(
            department=department,
            defaults={'supervisor': supervisor},
        )
        draft_appraisals = Appraisal.objects.filter(
            employee__in=employees,
            status='draft',
        )
        for appraisal in draft_appraisals.iterator():
            appraisal.supervisor = supervisor
            appraisal.save(update_fields=['supervisor'])
        transferred_count = 0
        if request.POST.get('confirm_transfer') == '1':
            for appraisal in appraisals_to_transfer.iterator():
                appraisal.supervisor = supervisor
                appraisal.save(update_fields=['supervisor'])
                transferred_count += 1

        if supervisor_changed:
            notified_users = [
                employee.user
                for employee in employees.select_related('user')
            ]
            notified_users.append(supervisor.user)
            if previous_supervisor and previous_supervisor.pk != supervisor.pk:
                notified_users.append(previous_supervisor.user)
            previous_name = (
                previous_supervisor.user.get_full_name() or previous_supervisor.user.username
                if previous_supervisor and previous_supervisor.pk != supervisor.pk
                else 'no previous supervisor'
            )
            notify_users(
                notified_users,
                title='Department reviewer assignment updated',
                message=(
                    f"Reviewer for the {department.name} department changed from "
                    f"{previous_name} to "
                    f"{supervisor.user.get_full_name() or supervisor.user.username}."
                ),
                url=reverse('dashboard_router'),
            )

    return form, None, (
        department.name,
        supervisor.user.username,
        transferred_count,
    )


def _process_hr_supervisor_reporting(request):
    form = SupervisorReportingForm(request.POST, prefix='reporting')
    if not form.is_valid():
        return form, None, None
 
    supervisor = form.cleaned_data['supervisor']
    manager = form.cleaned_data['manager']
    appraisals_to_transfer = Appraisal.objects.filter(
        employee=supervisor,
        status__in=['pending_manager', 'returned_supervisor'],
    ).exclude(supervisor=manager)
    if appraisals_to_transfer.exists() and request.POST.get('confirm_transfer') != '1':
        return form, {
            'supervisor_id': supervisor.id,
            'manager_id': manager.id,
            'supervisor_name': supervisor.user.get_full_name() or supervisor.user.username,
            'manager_name': manager.user.get_full_name() or manager.user.username,
            'appraisal_count': appraisals_to_transfer.count(),
            'reporting': True,
        }, None

    previous = SupervisorReporting.objects.filter(
        supervisor=supervisor,
    ).select_related('manager__user').first()
    changed = previous is None or previous.manager_id != manager.id
    with transaction.atomic():
        SupervisorReporting.objects.update_or_create(
            supervisor=supervisor,
            defaults={'manager': manager},
        )
        draft_appraisals = Appraisal.objects.filter(
            employee=supervisor,
            status='draft',
        )
        for appraisal in draft_appraisals.iterator():
            appraisal.supervisor = manager
            appraisal.save(update_fields=['supervisor'])
        transferred_count = 0
        if request.POST.get('confirm_transfer') == '1':
            for appraisal in appraisals_to_transfer.iterator():
                appraisal.supervisor = manager
                appraisal.save(update_fields=['supervisor'])
                transferred_count += 1

        if changed:
            notify_users(
                [supervisor.user, manager.user]
                + ([previous.manager.user] if previous and previous.manager_id != manager.id else []),
                title='Supervisor reporting line updated',
                message=(
                    f"{supervisor.user.get_full_name() or supervisor.user.username} "
                    f"now reports to {manager.user.get_full_name() or manager.user.username}."
                ),
                url=reverse('dashboard_router'),
            )
    return form, None, (
        supervisor.user.username,
        manager.user.username,
        transferred_count,
    )


def _process_hr_department_hod_assignment(request):
    form = HRDepartmentHodAssignmentForm(request.POST)
    if not form.is_valid():
        return form, None

    department = form.cleaned_data['department']
    hod = form.cleaned_data['hod']
    with transaction.atomic():
        assignment = AssignDepartmentHod.objects.filter(
            department=department,
        ).select_related('hod__user').order_by('pk').first()
        previous_hod = assignment.hod if assignment else None
        if assignment:
            assignment.hod = hod
            assignment.save(update_fields=['hod'])
        else:
            AssignDepartmentHod.objects.create(
                department=department,
                hod=hod,
            )

        if not previous_hod or previous_hod.pk != hod.pk:
            recipients = [hod.user]
            if previous_hod:
                recipients.append(previous_hod.user)
            notify_users(
                recipients,
                title='Department HOD assignment updated',
                message=(
                    f"{hod.user.get_full_name() or hod.user.username} is assigned as "
                    f"the HOD for {department.name}."
                ),
                url=reverse('dashboard_router'),
            )

    return form, (department.name, hod.user.username)


def _notify_hr_of_submission(appraisal):
    hr_users = EmployeeProfile.objects.filter(
        role='HR',
    ).select_related('user')
    notify_users(
        [hr_profile.user for hr_profile in hr_users],
        title='Appraisal awaiting HR approval',
        message=(
            f"{appraisal.employee.user.get_full_name() or appraisal.employee.user.username}'s "
            f"{appraisal.cycle.year} appraisal was submitted for final approval."
        ),
        url=reverse('hr_dashboard'),
    )


def _submit_appraisal_to_next_stage(appraisal, profile):
    if not appraisal.reference_number:
        appraisal.reference_number = f"MCHY-{appraisal.cycle.year}-{appraisal.pk:06d}"

    if profile.role == 'HOD':
        appraisal.status = 'pending_hr'
        appraisal.hod_signed_at = timezone.now()
        appraisal.employee_signed_at = appraisal.employee_signed_at or timezone.now()
        appraisal.save(update_fields=[
            'status',
            'hod_signed_at',
            'employee_signed_at',
            'reference_number',
        ])
        _notify_hr_of_submission(appraisal)
        return 'HR'

    assigned_reviewer = (
        appraisal.supervisor
        if appraisal.status == 'returned_employee' and appraisal.supervisor_id
        else get_assigned_supervisor(profile)
    )
    if not assigned_reviewer:
        return None

    appraisal.supervisor = assigned_reviewer
    appraisal.status = 'pending_manager'
    appraisal.employee_signed_at = timezone.now()
    appraisal.save(update_fields=[
        'supervisor',
        'status',
        'employee_signed_at',
        'reference_number',
    ])
    notify_users(
        [assigned_reviewer.user],
        title='Appraisal awaiting your review',
        message=(
            f"{profile.user.get_full_name() or profile.user.username} "
            f"submitted the {appraisal.cycle.year} appraisal for your review."
        ),
        url=reverse('supervisor_review', args=[appraisal.id]),
    )
    return 'your assigned reviewer'


def _can_review_as_supervisor(profile, appraisal):
    if appraisal.supervisor_id != profile.id:
        return False
    if appraisal.employee.role == 'Employee':
        return profile.role != 'HR'
    return profile.role == 'HOD' and appraisal.employee.role == 'Supervisor'


def _return_request_context(appraisal):
    return_requests = list(
        appraisal.return_requests.select_related('requester', 'reviewer')
    )
    return {
        'pending_return_request': next(
            (item for item in return_requests if item.status == 'pending'),
            None,
        ),
        'return_requests': return_requests,
        'return_request_form': AppraisalReturnRequestForm(),
        'return_decision_form': AppraisalReturnDecisionForm(),
    }


def _return_request_actor_profile(request):
    profile = EmployeeProfile.objects.filter(user=request.user).first()
    if profile is None:
        messages.error(request, "A staff profile is required for appraisal return requests.")
    return profile


def _return_request_context_for_user(appraisal, profile):
    stage, _ = _return_request_stage_and_destination(appraisal, profile)
    context = _return_request_context(appraisal)
    return {
        **context,
        'can_request_return': (
            stage is not None
            and context['pending_return_request'] is None
        ),
        'can_decide_return_request': _can_decide_return_request(profile, appraisal),
    }


def _return_request_stage_and_destination(appraisal, profile):
    if (
        appraisal.status == 'pending_manager'
        and appraisal.employee_id == profile.id
        and profile.role in ['Employee', 'Supervisor']
    ):
        return 'supervisor', 'returned_employee'
    if appraisal.status == 'pending_hod' and appraisal.supervisor_id == profile.id:
        return 'hod', 'returned_supervisor'
    if (
        appraisal.status == 'pending_hr'
        and profile.role == 'HOD'
        and (
            appraisal.hod_id == profile.id
            or appraisal.employee_id == profile.id
        )
    ):
        destination = (
            'returned_employee'
            if appraisal.employee_id == profile.id
            else 'returned_hod'
        )
        return 'hr', destination
    return None, None


def _can_decide_return_request(profile, appraisal):
    if appraisal.status == 'pending_manager':
        return _can_review_as_supervisor(profile, appraisal)
    if appraisal.status == 'pending_hod':
        return profile.role == 'HOD' and appraisal.hod_id == profile.id
    if appraisal.status == 'pending_hr':
        return profile.role == 'HR'
    return False


def _return_request_destination_url(appraisal, status, requester=None):
    if status == 'returned_employee':
        return reverse('fill_targets_year', args=[appraisal.cycle.year])
    if status == 'returned_supervisor':
        return reverse('supervisor_review', args=[appraisal.id])
    if status == 'returned_hod':
        return reverse('hod_review', args=[appraisal.id])
    if status == 'pending_manager':
        return reverse('fill_targets_year', args=[appraisal.cycle.year])
    if status == 'pending_hod':
        return reverse('supervisor_view_appraisal', args=[appraisal.id])
    if status == 'pending_hr':
        if requester and appraisal.employee.user_id == requester.pk:
            return reverse('fill_targets_year', args=[appraisal.cycle.year])
        return reverse('hod_view_appraisal', args=[appraisal.id])
    return reverse('dashboard_router')


def _can_manage_staff_ids(user):
    return user.is_superuser or EmployeeProfile.objects.filter(
        user=user,
        role='HR',
    ).exists()


def _parse_staff_id_upload(uploaded_file):
    if uploaded_file.name.lower().endswith('.csv'):
        try:
            rows = list(csv.reader(io.StringIO(
                uploaded_file.read().decode('utf-8-sig'),
            )))
        except UnicodeDecodeError as error:
            raise ValidationError('The CSV file must use UTF-8 encoding.') from error
    else:
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(uploaded_file, read_only=True, data_only=True)
            try:
                rows = list(workbook.active.iter_rows(values_only=True))
            finally:
                workbook.close()
        except Exception as error:
            raise ValidationError('The Excel file could not be read.') from error

    if not rows:
        raise ValidationError('The file is empty.')
    headers = [str(value or '').strip().lower().replace(' ', '_') for value in rows[0]]
    staff_id_column = next(
        (index for index, value in enumerate(headers) if value in {'staff_id', 'name'}),
        None,
    )
    email_column = next(
        (index for index, value in enumerate(headers) if value == 'email'),
        None,
    )
    if staff_id_column is None or email_column is None:
        raise ValidationError('The first row must contain staff_id and email columns.')
    if len(rows) - 1 > 5000:
        raise ValidationError('Upload no more than 5,000 staff IDs at a time.')

    staff_rows = []
    seen_ids = set()
    seen_emails = set()
    for row_number, row in enumerate(rows[1:], start=2):
        values = list(row)
        staff_id = str(values[staff_id_column] or '').strip() if staff_id_column < len(values) else ''
        email = str(values[email_column] or '').strip().lower() if email_column < len(values) else ''
        if not staff_id and not email:
            continue
        if not staff_id or not email:
            raise ValidationError(f'Row {row_number} needs both a staff ID and email.')
        if len(staff_id) > 100:
            raise ValidationError(f'Staff ID on row {row_number} is too long.')
        try:
            validate_email(email)
        except ValidationError as error:
            raise ValidationError(f'Email on row {row_number} is invalid.') from error

        normalized_id = staff_id.casefold()
        normalized_email = email.casefold()
        if normalized_id in seen_ids:
            raise ValidationError(f'Staff ID on row {row_number} duplicates an earlier row.')
        if normalized_email in seen_emails:
            raise ValidationError(f'Email on row {row_number} is already used in this file.')
        seen_ids.add(normalized_id)
        seen_emails.add(normalized_email)
        staff_rows.append((staff_id, email))
    return staff_rows


def _save_staff_id_rows(staff_rows):
    added = 0
    staff_ids_seen = set()
    emails_seen = set()
    with transaction.atomic():
        chunk_size = 200
        for start in range(0, len(staff_rows), chunk_size):
            rows = staff_rows[start:start + chunk_size]
<<<<<<< HEAD
            normalized_names = []
            normalized_emails = []
            for staff_id, email in rows:
                normalized_name = staff_id.upper()
                normalized_email = email.casefold()
                if normalized_name in staff_ids_seen:
                    raise ValidationError('The upload contains a duplicate Staff ID.')
                if normalized_email in emails_seen:
                    raise ValidationError('The upload contains a duplicate email address.')
                staff_ids_seen.add(normalized_name)
                emails_seen.add(normalized_email)
                normalized_names.append(normalized_name)
                normalized_emails.append(normalized_email)

            existing_staff = StaffId.objects.annotate(
                normalized_name=Upper('name'),
                normalized_email=Lower('email'),
            ).filter(
                Q(normalized_name__in=normalized_names)
                | Q(normalized_email__in=normalized_emails)
            )
            if existing_staff.exists():
                raise ValidationError(
                    'At least one Staff ID or email is already in the staff directory. '
                    'Correct the duplicate entries and try again.'
                )

            existing_usernames = User.objects.annotate(
                normalized_username=Upper('username'),
            ).filter(normalized_username__in=normalized_names)
            if existing_usernames.exists():
                raise ValidationError(
                    'At least one Staff ID is already used by an account. '
                    'Correct the duplicate entry and try again.'
                )

            new_staff_ids = [
                StaffId(name=staff_id, email=email)
                for staff_id, email in rows
            ]
            try:
                # Keep a savepoint so a concurrent insert can be handled without
                # leaving partially imported rows if an ID or email races.
=======
            name_match = Q()
            for staff_id, _email in rows:
                name_match |= Q(name__iexact=staff_id)

            existing_by_name = {}
            for existing in StaffId.objects.filter(name_match).order_by('pk'):
                existing_by_name.setdefault(existing.name.casefold(), existing)

            new_staff_ids = []
            changed_staff_ids = []
            for staff_id, email in rows:
                existing = existing_by_name.get(staff_id.casefold())
                if existing:
                    if existing.registered_user_id:
                        continue
                    if existing.email != email:
                        existing.email = email
                        changed_staff_ids.append(existing)
                    continue
                new_staff_ids.append(StaffId(name=staff_id, email=email))

            if changed_staff_ids:
                StaffId.objects.bulk_update(
                    changed_staff_ids,
                    ['email'],
                    batch_size=chunk_size,
                )
                updated += len(changed_staff_ids)

            if not new_staff_ids:
                continue
            try:
                # Keep a savepoint so a concurrent insert can be handled without
                # rolling back the other rows in this upload chunk.
>>>>>>> c5922a9 (db optimazation fixed)
                with transaction.atomic():
                    StaffId.objects.bulk_create(
                        new_staff_ids,
                        batch_size=chunk_size,
                    )
                added += len(new_staff_ids)
            except IntegrityError:
<<<<<<< HEAD
                raise ValidationError(
                    'A Staff ID or email was added at the same time. '
                    'Refresh the page and check for duplicates.'
                )
    return added, 0
=======
                # Preserve the previous behavior for a rare unique-name race:
                # import each row independently and skip only conflicting rows.
                for staff_id in new_staff_ids:
                    try:
                        with transaction.atomic():
                            staff_id.save(force_insert=True)
                        added += 1
                    except IntegrityError:
                        continue
    return added, updated
>>>>>>> c5922a9 (db optimazation fixed)


def _staff_ids_csv_response(staff_ids, filename):
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow(['staff_id', 'email', 'account_status', 'registered_username'])
    for staff in staff_ids:
        values = (
            staff.name,
            staff.email,
            'Registered' if staff.registered_user_id else 'Not registered',
            staff.registered_user.username if staff.registered_user_id else '',
        )
        writer.writerow([
            f"'{value}" if str(value).lstrip()[:1] in {'=', '+', '-', '@'} else value
            for value in values
        ])
    return response


def _staff_id_upload_template_response():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = 'Staff IDs'
    worksheet.append(['staff_id', 'email'])
    worksheet.freeze_panes = 'A2'
    worksheet.column_dimensions['A'].width = 24
    worksheet.column_dimensions['B'].width = 38
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill(fill_type='solid', fgColor='16845D')

    output = io.BytesIO()
    workbook.save(output)
    response = HttpResponse(
        output.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = 'attachment; filename="staff_id_upload_template.xlsx"'
    return response


def _staff_ids_redirect(request):
    params = {}
    query = request.POST.get('q', request.GET.get('q', '')).strip()
    page = request.POST.get('page', request.GET.get('page'))
    if query:
        params['q'] = query
    if page:
        params['page'] = page
    query_string = urlencode(params)
    return redirect(f"{reverse('manage_staff_ids')}{'?' + query_string if query_string else ''}")


@login_required
def manage_staff_ids(request):
    if not _can_manage_staff_ids(request.user):
        return HttpResponseForbidden('Only HR and superusers can manage staff IDs.')

    staff_queryset = StaffId.objects.select_related('registered_user').order_by('name')
    if request.GET.get('download') == 'template':
        return _staff_id_upload_template_response()
    if request.GET.get('download') == 'all':
        return _staff_ids_csv_response(staff_queryset, 'approved_staff_ids.csv')
    download_id = request.GET.get('download_id')
    if download_id:
        staff = get_object_or_404(staff_queryset, pk=download_id)
        return _staff_ids_csv_response([staff], f'staff_id_{staff.pk}.csv')

    entry_form = StaffIdEntryForm()
    upload_form = StaffIdUploadForm()
    action = request.POST.get('action') if request.method == 'POST' else ''
    if action == 'update':
        staff = get_object_or_404(StaffId.objects.select_related('registered_user'), pk=request.POST.get('staff_pk'))
        edit_form = StaffIdEntryForm(
            request.POST,
            exclude_pk=staff.pk,
            exclude_user_pk=staff.registered_user_id,
        )
        if edit_form.is_valid():
            staff_id = edit_form.cleaned_data['staff_id']
            email = edit_form.cleaned_data['email']
            try:
                with transaction.atomic():
                    staff.name = staff_id
                    staff.email = email
                    staff.save(update_fields=['name', 'email'])
                    if staff.registered_user_id:
                        staff.registered_user.username = staff_id
                        staff.registered_user.email = email
                        staff.registered_user.save(update_fields=['username', 'email'])
            except IntegrityError:
                messages.error(
                    request,
                    'That Staff ID or email was just assigned to another staff member. Refresh and check the directory.',
                )
            else:
                messages.success(request, f'Staff record {staff_id} updated.')
        else:
            errors = [
                str(message)
                for field_errors in edit_form.errors.values()
                for message in field_errors
            ]
            messages.error(request, ' '.join(errors) or 'Enter a valid Staff ID and work email before saving.')
        return _staff_ids_redirect(request)
    elif action == 'delete':
        staff = get_object_or_404(StaffId.objects.select_related('registered_user'), pk=request.POST.get('staff_pk'))
        if staff.registered_user_id:
            messages.error(request, 'This staff ID is linked to an account. Update it or freeze the account instead of deleting it.')
        else:
            staff.delete()
            messages.success(request, 'Staff ID entry deleted.')
        return _staff_ids_redirect(request)
    elif action == 'toggle_account':
        staff = get_object_or_404(StaffId.objects.select_related('registered_user'), pk=request.POST.get('staff_pk'))
        if not staff.registered_user_id:
            messages.error(request, 'There is no registered account linked to this staff ID.')
        else:
            requested_status = request.POST.get('account_status')
            if requested_status not in {'freeze', 'activate'}:
                messages.error(request, 'Choose Freeze or Reactivate to change this account status.')
            else:
                staff.registered_user.is_active = requested_status == 'activate'
                staff.registered_user.save(update_fields=['is_active'])
                state = 'reactivated' if staff.registered_user.is_active else 'frozen'
                messages.success(request, f"The account for {staff.name} was {state}.")
        return _staff_ids_redirect(request)
    elif action == 'add':
        entry_form = StaffIdEntryForm(request.POST)
        if entry_form.is_valid():
            try:
                added, _updated = _save_staff_id_rows([(
                    entry_form.cleaned_data['staff_id'],
                    entry_form.cleaned_data['email'],
                )])
            except ValidationError as error:
                entry_form.add_error(None, error)
            else:
                if added:
                    messages.success(request, 'Staff ID and work email saved.')
                    return _staff_ids_redirect(request)
    elif action == 'upload':
        upload_form = StaffIdUploadForm(request.POST, request.FILES)
        if upload_form.is_valid():
            try:
                staff_rows = _parse_staff_id_upload(upload_form.cleaned_data['file'])
                added, _updated = _save_staff_id_rows(staff_rows)
            except ValidationError as error:
                upload_form.add_error('file', error)
            else:
                messages.success(
                    request,
                    f'Import complete: {added} added.',
                )
                return _staff_ids_redirect(request)

    search_query = request.GET.get('q', '').strip()
    staff_total = staff_queryset.count()
    registered_total = staff_queryset.filter(registered_user__isnull=False).count()
    unregistered_total = staff_total - registered_total
    if search_query:
        staff_queryset = staff_queryset.filter(
            Q(name__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(registered_user__username__icontains=search_query)
            | Q(registered_user__first_name__icontains=search_query)
            | Q(registered_user__last_name__icontains=search_query)
        )
    paginator = Paginator(staff_queryset, 15)
    page_obj = paginator.get_page(request.GET.get('page', 1))
    return render(request, 'app/manage_staff_ids.html', {
        'entry_form': entry_form,
        'upload_form': upload_form,
        'staff_ids': page_obj.object_list,
        'page_obj': page_obj,
        'page_range': paginator.get_elided_page_range(page_obj.number),
        'page_ellipsis': paginator.ELLIPSIS,
        'search_query': search_query,
        'filtered_total': paginator.count,
        'staff_total': staff_total,
        'registered_total': registered_total,
        'unregistered_total': unregistered_total,
    })


def employee_registration(request):
    form = RegistrationStaffIdForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        request.session.pop('registration_code_id', None)
        staff_id = form.approved_staff_id
        now = timezone.now()
        recent_code_count = RegistrationCode.objects.filter(
            staff_id=staff_id,
            created_at__gte=now - timedelta(hours=1),
        ).count()
        if recent_code_count >= 5:
            form.add_error(
                None,
                'Too many verification codes were requested for this Staff ID. Try again later.',
            )
        else:
            RegistrationCode.objects.filter(
                staff_id=staff_id,
                verified_at__isnull=True,
            ).update(expires_at=now)
            code = f'{secrets.randbelow(1_000_000):06d}'
            registration_code = RegistrationCode.objects.create(
                staff_id=staff_id,
                code_hash=make_password(code),
                expires_at=now + timedelta(minutes=10),
            )
            try:
                send_registration_code.delay(registration_code.pk, code)
            except Exception:
                logger.exception('Could not queue registration email for staff ID %s.', staff_id.pk)
                registration_code.delete()
                form.add_error(
                    None,
                    'We could not send a verification code right now. Please try again later.',
                )
            else:
                request.session['registration_code_id'] = registration_code.pk
                messages.info(
                    request,
                    'A verification code has been sent to the work email HR has on file.',
                )
                return redirect('verify_registration_code')

    return render(request, 'registration/employee_registration.html', {'form': form})


def verify_registration_code(request):
    form = RegistrationCodeForm(request.POST or None)
    code_id = request.session.get('registration_code_id')
    registration_code = RegistrationCode.objects.filter(
        pk=code_id,
        verified_at__isnull=True,
    ).first() if code_id else None

    if request.method == 'POST':
        if not registration_code or registration_code.expires_at <= timezone.now():
            form.add_error(None, 'The code is invalid or expired. Start registration again.')
        elif registration_code.attempts >= 5:
            form.add_error(None, 'Too many attempts. Start registration again.')
        elif form.is_valid():
            registration_code.attempts += 1
            if check_password(form.cleaned_data['code'], registration_code.code_hash):
                registration_code.verified_at = timezone.now()
                registration_code.save(update_fields=['attempts', 'verified_at'])
                request.session['verified_registration_code_id'] = registration_code.pk
                return redirect('complete_employee_registration')
            registration_code.save(update_fields=['attempts'])
            form.add_error('code', 'The code is incorrect.')

    resend_seconds = 0
    if registration_code:
        resend_seconds = max(
            0,
            60 - int((timezone.now() - registration_code.created_at).total_seconds()),
        )
    return render(request, 'registration/verify_registration_code.html', {
        'form': form,
        'resend_seconds': resend_seconds,
    })


@require_POST
def resend_registration_code(request):
    registration_code_id = request.session.get('registration_code_id')
    previous_code = RegistrationCode.objects.select_related('staff_id').filter(
        pk=registration_code_id,
        verified_at__isnull=True,
    ).first() if registration_code_id else None
    if not previous_code or previous_code.staff_id.registered_user_id:
        request.session.pop('registration_code_id', None)
        messages.error(request, 'Please start registration again to request a verification code.')
        return redirect('employee_registration')

    now = timezone.now()
    resend_seconds = max(
        0,
        60 - int((now - previous_code.created_at).total_seconds()),
    )
    if resend_seconds:
        messages.info(request, f'Please wait {resend_seconds} seconds before requesting another code.')
        return redirect('verify_registration_code')

    recent_code_count = RegistrationCode.objects.filter(
        staff_id=previous_code.staff_id,
        created_at__gte=now - timedelta(hours=1),
    ).count()
    if recent_code_count >= 5:
        messages.error(request, 'Too many verification codes were requested. Try again later.')
        return redirect('verify_registration_code')

    code = f'{secrets.randbelow(1_000_000):06d}'
    replacement_code = RegistrationCode.objects.create(
        staff_id=previous_code.staff_id,
        code_hash=make_password(code),
        expires_at=now + timedelta(minutes=10),
    )
    try:
        send_registration_code.delay(replacement_code.pk, code)
    except Exception:
        logger.exception(
            'Could not queue registration email for staff ID %s.',
            previous_code.staff_id_id,
        )
        replacement_code.delete()
        messages.error(request, 'We could not send a verification code right now. Please try again later.')
        return redirect('verify_registration_code')

    RegistrationCode.objects.filter(
        staff_id=previous_code.staff_id,
        verified_at__isnull=True,
    ).exclude(pk=replacement_code.pk).update(expires_at=now)
    request.session['registration_code_id'] = replacement_code.pk
    messages.success(request, 'A new verification code has been sent to your work email.')
    return redirect('verify_registration_code')


def complete_employee_registration(request):
    registration_code_id = request.session.get('verified_registration_code_id')
    registration_code = RegistrationCode.objects.select_related('staff_id').filter(
        pk=registration_code_id,
        verified_at__isnull=False,
        expires_at__gt=timezone.now(),
    ).first() if registration_code_id else None
    if not registration_code or registration_code.staff_id.registered_user_id:
        request.session.pop('verified_registration_code_id', None)
        messages.error(request, 'Please verify your staff ID and work email again.')
        return redirect('employee_registration')

    form = EmployeeRegistrationForm(
        request.POST or None,
        staff_id=registration_code.staff_id.name,
    )
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                staff_id = StaffId.objects.select_for_update().get(
                    pk=registration_code.staff_id_id,
                )
                if staff_id.registered_user_id or User.objects.filter(
                    username__iexact=staff_id.name,
                ).exists():
                    raise ValidationError('This staff ID already has an account.')
                user = User.objects.create_user(
                    username=staff_id.name,
                    email=staff_id.email,
                    password=form.cleaned_data['password'],
                    first_name=form.cleaned_data['first_name'],
                    last_name=form.cleaned_data['last_name'],
                )
                EmployeeProfile.objects.create(
                    user=user,
                    role='Employee',
                    department=form.cleaned_data['department'],
                    title=form.cleaned_data['title'],
                    other_title=form.cleaned_data['other_title'],
                    other_names=form.cleaned_data['other_names'],
                    gender=form.cleaned_data['gender'],
                    job_title=form.cleaned_data['job_title'],
                    appointment_to_present_grade_date=(
                        form.cleaned_data['appointment_to_present_grade_date']
                    ),
                    security_question=form.cleaned_data['security_question'],
                    security_answer_hash=make_password(
                        form.cleaned_data['security_answer'],
                    ),
                )
                staff_id.registered_user = user
                staff_id.save(update_fields=['registered_user'])
        except (IntegrityError, ValidationError):
            form.add_error(None, 'This staff ID already has an account. Contact HR if you need help.')
        else:
            request.session.pop('registration_code_id', None)
            request.session.pop('verified_registration_code_id', None)
            messages.success(request, 'Your account is ready. You can now sign in.')
            return redirect('login')

    return render(request, 'registration/complete_employee_registration.html', {
        'form': form,
        'staff_id': registration_code.staff_id.name,
        'email': registration_code.staff_id.email,
    })


def _issue_password_reset_code(staff_id):
    now = timezone.now()
    recent_code_count = PasswordResetCode.objects.filter(
        staff_id=staff_id,
        created_at__gte=now - timedelta(hours=1),
    ).count()
    if recent_code_count >= 5:
        return None, 'Too many verification codes were requested. Try again later.'

    code = f'{secrets.randbelow(1_000_000):06d}'
    reset_code = PasswordResetCode.objects.create(
        staff_id=staff_id,
        code_hash=make_password(code),
        expires_at=now + timedelta(minutes=10),
    )
    try:
        send_password_reset_code.delay(reset_code.pk, code)
    except Exception:
        reset_code.delete()
        logger.exception(
            'Could not queue password reset email for staff ID %s.',
            staff_id.name,
        )
        return None, 'The verification email could not be queued right now. Please try again later.'

    PasswordResetCode.objects.filter(
        staff_id=staff_id,
        verified_at__isnull=True,
    ).exclude(pk=reset_code.pk).update(expires_at=now)
    return reset_code, None


def request_password_reset(request):
    stage = request.session.get('password_reset_stage', 'staff_id')
    context = {'step': stage, 'form': None}

    if stage == 'staff_id':
        form = PasswordResetRequestForm(request.POST or None)
        if request.method == 'POST' and form.is_valid():
            staff_id = StaffId.objects.select_related(
                'registered_user__profile',
            ).filter(name__iexact=form.cleaned_data['staff_id']).first()
            user = staff_id.registered_user if staff_id else None
            profile = getattr(user, 'profile', None) if user else None
            if (
                not user or not user.is_active or not staff_id.email
                or not profile or not profile.security_question
                or not profile.security_answer_hash
            ):
                form.add_error(None, 'We could not verify that staff ID. Check it or contact HR.')
            else:
                request.session['password_reset_staff_id'] = staff_id.pk
                request.session['password_reset_stage'] = 'answer'
                request.session['password_reset_answer_attempts'] = 0
                stage = 'answer'
                context.update({
                    'step': stage,
                    'welcome_name': user.first_name or user.username,
                    'security_question': profile.get_security_question_display(),
                })
                form = PasswordResetAnswerForm()
        context.setdefault('step', stage)
        context['form'] = form

    elif stage == 'answer':
        staff_id = StaffId.objects.select_related(
            'registered_user__profile',
        ).filter(pk=request.session.get('password_reset_staff_id')).first()
        user = staff_id.registered_user if staff_id else None
        profile = getattr(user, 'profile', None) if user else None
        if not staff_id or not user or not profile:
            request.session.pop('password_reset_stage', None)
            request.session.pop('password_reset_staff_id', None)
            return redirect('password_reset_request')

        form = PasswordResetAnswerForm(request.POST or None)
        context.update({
            'step': stage,
            'form': form,
            'welcome_name': user.first_name or user.username,
            'security_question': profile.get_security_question_display(),
        })
        if request.method == 'POST' and form.is_valid():
            if not check_password(
                form.cleaned_data['security_answer'],
                profile.security_answer_hash,
            ):
                attempts = request.session.get('password_reset_answer_attempts', 0) + 1
                request.session['password_reset_answer_attempts'] = attempts
                form.add_error('security_answer', 'The security answer is incorrect.')
                if attempts >= 5:
                    request.session.pop('password_reset_stage', None)
                    request.session.pop('password_reset_staff_id', None)
                    request.session.pop('password_reset_answer_attempts', None)
                    messages.error(request, 'Too many attempts. Start the reset process again.')
                    return redirect('password_reset_request')
            else:
                reset_code, error = _issue_password_reset_code(staff_id)
                if error:
                    messages.error(request, error)
                else:
                    request.session['password_reset_code_id'] = reset_code.pk
                    request.session['password_reset_stage'] = 'code'
                    request.session.pop('password_reset_answer_attempts', None)
                    return redirect('password_reset_request')

    elif stage == 'code':
        reset_code = PasswordResetCode.objects.select_related(
            'staff_id__registered_user',
        ).filter(
            pk=request.session.get('password_reset_code_id'),
            staff_id_id=request.session.get('password_reset_staff_id'),
            verified_at__isnull=True,
        ).first()
        if not reset_code or reset_code.expires_at <= timezone.now():
            if reset_code:
                reset_code.delete()
            for key in (
                'password_reset_stage', 'password_reset_staff_id',
                'password_reset_code_id',
            ):
                request.session.pop(key, None)
            messages.error(request, 'Your verification code expired. Start the reset process again.')
            return redirect('password_reset_request')

        user = reset_code.staff_id.registered_user
        form = PasswordResetCodeForm(request.POST or None)
        context.update({
            'step': stage,
            'form': form,
            'resend_seconds': max(
                0,
                60 - int((timezone.now() - reset_code.created_at).total_seconds()),
            ),
            'welcome_name': user.first_name or user.username,
            'masked_email': (
                f"{reset_code.staff_id.email[:1]}***@"
                f"{reset_code.staff_id.email.split('@', 1)[1]}"
            ),
        })
        if request.method == 'POST' and form.is_valid():
            if check_password(form.cleaned_data['code'], reset_code.code_hash):
                reset_code.verified_at = timezone.now()
                reset_code.save(update_fields=['verified_at'])
                return redirect('password_reset_confirm')
            reset_code.attempts += 1
            reset_code.save(update_fields=['attempts'])
            form.add_error('code', 'That code is incorrect.')
            if reset_code.attempts >= 5:
                reset_code.delete()
                for key in (
                    'password_reset_stage', 'password_reset_staff_id',
                    'password_reset_code_id',
                ):
                    request.session.pop(key, None)
                messages.error(request, 'Too many attempts. Start the reset process again.')
                return redirect('password_reset_request')
    else:
        request.session.pop('password_reset_stage', None)
        return redirect('password_reset_request')

    return render(request, 'registration/password_reset_request.html', context)


@require_POST
def resend_password_reset_code(request):
    staff_id_id = request.session.get('password_reset_staff_id')
    reset_code = PasswordResetCode.objects.filter(
        pk=request.session.get('password_reset_code_id'),
        staff_id_id=staff_id_id,
        verified_at__isnull=True,
    ).first()
    if (
        request.session.get('password_reset_stage') != 'code'
        or not staff_id_id
        or not reset_code
        or reset_code.expires_at <= timezone.now()
        or reset_code.attempts >= 5
    ):
        for key in (
            'password_reset_stage', 'password_reset_staff_id',
            'password_reset_code_id',
        ):
            request.session.pop(key, None)
        messages.error(request, 'Start the password reset process again to request a new code.')
        return redirect('password_reset_request')

    staff_id = reset_code.staff_id
    resend_seconds = max(
        0,
        60 - int((timezone.now() - reset_code.created_at).total_seconds()),
    )
    if resend_seconds:
        messages.info(request, f'Please wait {resend_seconds} seconds before requesting another code.')
        return redirect('password_reset_request')

    replacement_code, error = _issue_password_reset_code(staff_id)
    if error:
        messages.error(request, error)
        return redirect('password_reset_request')

    request.session['password_reset_code_id'] = replacement_code.pk
    messages.success(request, 'A new verification code has been sent to your work email.')
    return redirect('password_reset_request')


def password_reset_confirm(request):
    reset_code = PasswordResetCode.objects.select_related(
        'staff_id__registered_user',
    ).filter(
        pk=request.session.get('password_reset_code_id'),
        staff_id_id=request.session.get('password_reset_staff_id'),
        verified_at__isnull=False,
        expires_at__gt=timezone.now(),
    ).first()
    user = reset_code.staff_id.registered_user if reset_code else None
    if not user:
        messages.error(request, 'Verify your email code before choosing a new password.')
        return redirect('password_reset_request')

    form = SetPasswordForm(user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        reset_code.delete()
        for key in (
            'password_reset_stage', 'password_reset_staff_id',
            'password_reset_code_id', 'password_reset_answer_attempts',
        ):
            request.session.pop(key, None)
        messages.success(request, 'Your password has been reset successfully. Please sign in.')
        return redirect('login')

    return render(request, 'registration/password_reset_set_password.html', {
        'form': form,
        'welcome_name': user.first_name or user.username,
    })


def home(request):
    if request.user.is_authenticated:
        return redirect('dashboard_router')
    return render(request, 'home.html')


def _return_request_page_url(appraisal, profile):
    if appraisal.employee_id == profile.id:
        return reverse('fill_targets_year', args=[appraisal.cycle.year])
    if appraisal.status == 'pending_hod':
        return reverse('supervisor_view_appraisal', args=[appraisal.id])
    if appraisal.status == 'pending_hr':
        return reverse('hod_view_appraisal', args=[appraisal.id])
    return reverse('dashboard_router')


def support(request):
    return render(request, 'support.html')


@login_required
def dashboard_router(request):
    """Checks the user's role and redirects them to the correct dashboard."""
    if request.user.is_superuser:
        return redirect('admin:index')

    profile = request.user.profile
    if profile.role is None:
        messages.error(request, "Your user profile is not set up correctly. Contact HR.")
        return redirect('/') 
    if profile.role == 'HR':
        return redirect('hr_dashboard')
    elif profile.role == 'HOD':
        return redirect('hod_dashboard')
    elif profile.role == 'Supervisor':
        return redirect('supervisor_dashboard')
    elif DepartmentSupervisor.objects.filter(supervisor=profile).exists():
        return redirect('supervisor_dashboard')

    else:
        return redirect('employee_dashboard')


def _dashboard_url_name_for_profile(profile):
    if profile.role == 'HR':
        return 'hr_dashboard'
    if profile.role == 'HOD':
        return 'hod_dashboard'
    if profile.role == 'Supervisor' or DepartmentSupervisor.objects.filter(
        supervisor=profile,
    ).exists():
        return 'supervisor_dashboard'
    return 'employee_dashboard'


def _previous_appraisal_context(profile):
    previous_appraisals = PreviousAppraisal.objects.filter(
        employee=profile,
    ).select_related('reviewed_by')
    return {
        'previous_appraisal_form': PreviousAppraisalUploadForm(),
        'previous_appraisals': previous_appraisals,
        'approved_previous_appraisals': previous_appraisals.filter(
            status=PreviousAppraisal.Status.APPROVED,
        ),
        'current_year': timezone.localdate().year,
    }


def _get_profile_appraisals_by_cycle(profile, cycles, assigned_supervisor=None):
    """Load a profile's cycle appraisals together, creating only missing rows."""
    cycles = list(cycles)
    if not cycles:
        return {}

    appraisals_by_cycle = {
        appraisal.cycle_id: appraisal
        for appraisal in Appraisal.objects.filter(
            employee=profile,
            cycle_id__in=[cycle.pk for cycle in cycles],
        ).select_related('cycle')
    }

    for cycle in cycles:
        appraisal = appraisals_by_cycle.get(cycle.pk)
        if appraisal is None:
            appraisal, _ = Appraisal.objects.get_or_create(
                employee=profile,
                cycle=cycle,
            )
            appraisals_by_cycle[cycle.pk] = appraisal

        if (
            assigned_supervisor
            and appraisal.status == 'draft'
            and appraisal.supervisor_id != assigned_supervisor.pk
        ):
            appraisal.supervisor = assigned_supervisor
            appraisal.save(update_fields=['supervisor'])

    return appraisals_by_cycle


@login_required
def employee_dashboard(request):
    """Shows the employee's active cycle and appraisal history for every cycle."""
    profile = request.user.profile
    all_cycles = list(AppraisalCycle.objects.order_by('-year'))
    active_cycles = [cycle for cycle in all_cycles if cycle.is_active][:1]

    assigned_supervisor = get_assigned_supervisor(profile)
    appraisals_by_cycle = _get_profile_appraisals_by_cycle(
        profile,
        all_cycles,
        assigned_supervisor,
    )

    active_appraisal_cards = []
    for active_cycle in active_cycles:
        appraisal = appraisals_by_cycle[active_cycle.id]

        supervisor_for_card = appraisal.supervisor or assigned_supervisor
        latest_return = appraisal.returns.first()
        progress_stage, progress_percent = {
            'draft': (1, 0),
            'returned_employee': (1, 0),
            'pending_manager': (2, 20),
            'returned_supervisor': (2, 20),
            'pending_hod': (3, 40),
            'returned_hod': (3, 40),
            'pending_hr': (4, 60),
            'completed': (5, 100),
        }[appraisal.status]
        active_appraisal_cards.append({
            'cycle': active_cycle,
            'appraisal': appraisal,
            'supervisor': supervisor_for_card,
            'latest_return': latest_return,
            'is_editable': appraisal.status in ['draft', 'returned_employee'],
            'is_completed': appraisal.status == 'completed',
            'progress_stage': progress_stage,
            'progress_percent': progress_percent,
        })

    if not active_cycles:
        messages.info(request, "No active appraisal cycle right now.")

    all_appraisals = (
        Appraisal.objects.filter(employee=profile)
        .select_related('cycle', 'supervisor__user', 'employee_department')
        .order_by('-cycle__year', '-pk')
    )
    primary_card = active_appraisal_cards[0] if active_appraisal_cards else None
    appraisal_counts = all_appraisals.aggregate(
        total=Count('pk'),
        incomplete=Count('pk', filter=~Q(status='completed')),
        completed=Count('pk', filter=Q(status='completed')),
    )
    returned_statuses = (
        'returned_employee',
        'returned_supervisor',
        'returned_hod',
    )
    appraisal_trend = list(
        all_appraisals.order_by()
        .values('cycle__year')
        .annotate(
            score=Avg('overall_percentage', filter=Q(status='completed')),
            returned=Count('pk', filter=Q(status__in=returned_statuses)),
            incomplete=Count(
                'pk',
                filter=~Q(status='completed') & ~Q(status__in=returned_statuses),
            ),
        )
        .order_by('cycle__year')
    )
    for year_data in appraisal_trend:
        year_data['year'] = year_data.pop('cycle__year')
        if year_data['score'] is not None:
            year_data['score'] = round(float(year_data['score']), 2)

    return render(request, 'employee/dashboard.html', {
        'active_cycles': active_cycles,
        'active_appraisal_cards': active_appraisal_cards,
        'active_appraisal_count': appraisal_counts['total'],
        'incomplete_appraisal_count': appraisal_counts['incomplete'],
        'completed_appraisal_count': appraisal_counts['completed'],
        'appraisal_trend': appraisal_trend,
        'assigned_supervisor': assigned_supervisor,
        'all_appraisals': all_appraisals,
        **_previous_appraisal_context(profile),
        # Backward compatibility for single-cycle contexts
        'cycle': primary_card['cycle'] if primary_card else None,
        'appraisal': primary_card['appraisal'] if primary_card else None,
        'latest_return': primary_card['latest_return'] if primary_card else None,
    })


@login_required
def upload_previous_appraisal(request):
    if request.method != 'POST':
        return redirect('employee_dashboard')
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    profile = EmployeeProfile.objects.filter(user=request.user).first()
    if profile is None:
        if is_ajax:
            return JsonResponse(
                {'success': False, 'message': 'A staff profile is required to upload an appraisal.'},
                status=400,
            )
        messages.error(request, "A staff profile is required to upload an appraisal.")
        return redirect('dashboard_router')

    existing_rejected = None
    try:
        appraisal_year = int(request.POST.get('appraisal_cycle', ''))
    except (TypeError, ValueError):
        appraisal_year = None
    if appraisal_year is not None:
        existing_rejected = PreviousAppraisal.objects.filter(
            employee=profile,
            appraisal_cycle=appraisal_year,
            status=PreviousAppraisal.Status.REJECTED,
        ).first()

    form = PreviousAppraisalUploadForm(
        request.POST,
        request.FILES,
        instance=existing_rejected,
    )
    if form.is_valid():
        previous_file_name = (
            existing_rejected.appraisal_file.name if existing_rejected else None
        )
        previous_appraisal = form.save(commit=False)
        previous_appraisal.employee = profile
        previous_appraisal.status = PreviousAppraisal.Status.PENDING
        previous_appraisal.reviewed_by = None
        previous_appraisal.reviewed_at = None
        previous_appraisal.save()
        if previous_file_name:
            previous_appraisal.appraisal_file.storage.delete(previous_file_name)
        if is_ajax:
            return JsonResponse({
                'success': True,
                'message': f"Your {previous_appraisal.appraisal_cycle} appraisal was submitted for review.",
                'appraisal_cycle': previous_appraisal.appraisal_cycle,
            })
        messages.success(
            request,
            f"Your {previous_appraisal.appraisal_cycle} appraisal was submitted for review.",
        )
    else:
        errors = [
            str(error)
            for field_errors in form.errors.values()
            for error in field_errors
        ]
        if is_ajax:
            return JsonResponse({
                'success': False,
                'message': errors[0] if errors else 'Check the form and try again.',
                'errors': errors,
            }, status=400)
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, str(error))

    dashboard_url = _dashboard_url_name_for_profile(profile)
    section = 'history' if dashboard_url == 'employee_dashboard' else 'previous'
    return redirect(f"{reverse(dashboard_url)}#{section}")


def _can_review_previous_appraisals(user):
    return user.is_superuser or EmployeeProfile.objects.filter(
        user=user,
        role='HR',
    ).exists()


@login_required
def previous_appraisal_file(request, appraisal_id):
    return _previous_appraisal_file_response(request, appraisal_id, as_attachment=True)


@login_required
def view_previous_appraisal_file(request, appraisal_id):
    return _previous_appraisal_file_response(request, appraisal_id, as_attachment=False)


def _previous_appraisal_file_response(request, appraisal_id, *, as_attachment):
    appraisal = get_object_or_404(
        PreviousAppraisal.objects.select_related('employee__user'),
        pk=appraisal_id,
    )
    is_owner_with_approval = (
        appraisal.employee.user_id == request.user.pk
        and appraisal.status == PreviousAppraisal.Status.APPROVED
    )
    if not is_owner_with_approval and not _can_review_previous_appraisals(request.user):
        raise Http404

    return FileResponse(
        appraisal.appraisal_file.open('rb'),
        as_attachment=as_attachment,
        filename=appraisal.appraisal_file.name.rsplit('/', 1)[-1],
    )


@login_required
def previous_appraisal_reviews(request):
    if not _can_review_previous_appraisals(request.user):
        messages.error(request, "Access denied. Only HR or a superuser can review these files.")
        return redirect('dashboard_router')

    pending_appraisals = PreviousAppraisal.objects.filter(
        status=PreviousAppraisal.Status.PENDING,
    ).select_related('employee__user', 'employee__department').order_by(
        'appraisal_cycle',
        'employee__user__username',
    )
    reviewed_appraisals = PreviousAppraisal.objects.exclude(
        status=PreviousAppraisal.Status.PENDING,
    ).select_related(
        'employee__user',
        'reviewed_by',
    ).order_by('-reviewed_at', '-pk')
    return render(request, 'hr/previous_appraisal_reviews.html', {
        'pending_appraisals': pending_appraisals,
        'reviewed_appraisals': reviewed_appraisals,
    })


@login_required
def review_previous_appraisal(request, appraisal_id):
    if request.method != 'POST':
        return redirect('previous_appraisal_reviews')
    if not _can_review_previous_appraisals(request.user):
        messages.error(request, "Access denied. Only HR or a superuser can review these files.")
        return redirect('dashboard_router')

    appraisal = get_object_or_404(PreviousAppraisal, pk=appraisal_id)
    if appraisal.status != PreviousAppraisal.Status.PENDING:
        messages.warning(request, "This previous appraisal has already been reviewed.")
        return redirect('previous_appraisal_reviews')

    status_by_action = {
        'approve': PreviousAppraisal.Status.APPROVED,
        'reject': PreviousAppraisal.Status.REJECTED,
    }
    new_status = status_by_action.get(request.POST.get('action'))
    if new_status is None:
        messages.error(request, "Choose whether to approve or reject this appraisal.")
        return redirect('previous_appraisal_reviews')

    appraisal.status = new_status
    appraisal.reviewed_by = request.user
    appraisal.reviewed_at = timezone.now()
    appraisal.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
    messages.success(
        request,
        f"The {appraisal.appraisal_cycle} appraisal was {new_status}.",
    )
    return redirect('previous_appraisal_reviews')



@login_required
def fill_targets(request, year=None):
    """Lets employees edit draft goals and view their appraisal after submission."""
    profile = request.user.profile
    if year:
        cycle = get_object_or_404(AppraisalCycle, year=year)
    else:
        cycle = AppraisalCycle.objects.filter(is_active=True).order_by('-year').first()

    if not cycle:
        messages.error(request, "No active appraisal cycle right now.")
        return redirect('employee_dashboard')

    if cycle.is_active:
        appraisal, _ = Appraisal.objects.get_or_create(employee=profile, cycle=cycle)
    else:
        appraisal = get_object_or_404(
            Appraisal,
            employee=profile,
            cycle=cycle,
        )
    cycle_fill_url = reverse('fill_targets_year', args=[cycle.year])

    # Completed or locked appraisals are read-only, but the fill-targets route still renders them.
    if appraisal.status not in ['draft', 'returned_employee']:
        if request.method == 'POST':
            messages.warning(request, "This appraisal is completed and locked from editing.")
            return redirect(cycle_fill_url)
        return _render_read_only_appraisal(
            request,
            appraisal,
            allow_pdf=False,
            dashboard_url_name=_dashboard_url_name_for_profile(profile),
        )

    def save_competency_selections(competency_form):
        selected_statement_values = competency_form.cleaned_data['competency_statements']
        selected_statement_map = {
            statement['value']: (
                group['competency'],
                index,
                statement['text'],
            )
            for group in competency_form.competency_groups
            for index, statement in enumerate(group['statements'])
        }
        selected_statements = [
            selected_statement_map[value]
            for value in selected_statement_values
        ]
        if not selected_statement_values:
            selected_statements = [
                (competency, index, statement)
                for competency in competency_form.cleaned_data['competencies']
                for index, statement in enumerate(competency.description_items)
            ]
        selected_competencies = {
            competency.id: competency
            for competency, _, _ in selected_statements
        }

        selected_ids = list(selected_competencies)
        appraisal.competency_scores.exclude(
            competency_id__in=selected_ids,
        ).delete()
        for competency in selected_competencies.values():
            competency_score, _ = appraisal.competency_scores.get_or_create(
                competency=competency,
            )
            selected_for_competency = [
                (index, statement)
                for selected_competency, index, statement in selected_statements
                if selected_competency.id == competency.id
            ]
            selected_indexes = [index for index, _ in selected_for_competency]
            competency_score.statement_scores.exclude(
                statement_index__in=selected_indexes,
            ).delete()
            for index, statement in selected_for_competency:
                statement_score, created = competency_score.statement_scores.get_or_create(
                    statement_index=index,
                    defaults={'statement': statement},
                )
                if not created and statement_score.statement != statement:
                    statement_score.statement = statement
                    statement_score.end_year_score = None
                    statement_score.save(update_fields=['statement', 'end_year_score'])

    try:
        current_step = int(request.GET.get('step', 0) or 0)
    except (TypeError, ValueError):
        current_step = 0
    if current_step not in [0, 1, 2, 3, 4]:
        current_step = 0

    if request.method == 'POST':
        formset = TargetFormSet(request.POST, instance=appraisal)
        competency_form = EmployeeCompetencySelectionForm(request.POST)
        employee_info_form = AppraisalEmployeeInfoForm(
            request.POST,
            instance=appraisal,
            profile=profile,
            prefix='employee_info',
        )
        training_formset = AppraisalTrainingFormSet(
            request.POST,
            instance=appraisal,
            prefix='trainings',
        )
        action = request.POST.get('action')
        action_steps = {
            'save_personal': 0,
            'save_targets': 1,
            'save_core_competencies': 2,
            'save_competencies': 4,
            'submit': 4,
        }
        current_step = action_steps.get(action, current_step)
        step_for_action = {
            'save_personal': 1,
            'save_targets': 2,
            'save_core_competencies': 3,
            'save_competencies': 4,
        }.get(action)

        if action == 'save_personal':
            if employee_info_form.is_valid() and training_formset.is_valid():
                with transaction.atomic():
                    employee_info_form.save()
                    training_formset.save()
                messages.success(request, "Personal details and training saved.")
                return redirect(f"{cycle_fill_url}?step={step_for_action}")
        elif action == 'save_targets':
            if formset.is_valid():
                formset.save()
                messages.success(request, "Performance planning goals saved.")
                return redirect(f"{cycle_fill_url}?step={step_for_action}")
        elif action in ['save_core_competencies', 'save_competencies']:
            save_all_sections = action == 'save_competencies'
            if (
                competency_form.is_valid()
                and employee_info_form.is_valid()
                and (
                    not save_all_sections
                    or (
                        formset.is_valid()
                        and training_formset.is_valid()
                    )
                )
            ):
                with transaction.atomic():
                    employee_info_form.save()
                    if save_all_sections:
                        formset.save()
                        training_formset.save()
                    save_competency_selections(competency_form)
                messages.success(request, "Competency selections and appraisal comments saved.")
                return redirect(f"{cycle_fill_url}?step={step_for_action}")
        elif (
            formset.is_valid()
            and competency_form.is_valid()
            and employee_info_form.is_valid()
            and training_formset.is_valid()
        ):
            submit_for_review = action == 'submit'
            if submit_for_review and not employee_info_form.cleaned_data.get('employee_comments'):
                employee_info_form.add_error(
                    'employee_comments',
                    'Add your overall assessment comments before submitting.',
                )
            has_selected_statements = bool(
                competency_form.cleaned_data['competency_statements']
                or competency_form.cleaned_data['competencies']
            )
            can_submit = profile.role == 'HOD'

            if submit_for_review and employee_info_form.errors:
                messages.error(request, "Add your overall assessment comments before submitting.")
            elif submit_for_review and not has_selected_statements:
                messages.error(request, "Select at least one competency statement before submitting your appraisal.")
            elif submit_for_review:
                can_submit = (
                    profile.role == 'HOD'
                    or (
                        appraisal.status == 'returned_employee'
                        and appraisal.supervisor_id
                    )
                    or get_assigned_supervisor(profile) is not None
                )
                if not can_submit:
                    messages.error(
                        request,
                        "You cannot submit because no supervisor or HOD has been assigned to you yet. Contact HR.",
                    )

            if (
                not employee_info_form.errors
                and (
                    not submit_for_review
                    or (has_selected_statements and can_submit)
                )
            ):
                with transaction.atomic():
                    employee_info_form.save()
                    formset.save()
                    training_formset.save()
                    save_competency_selections(competency_form)

                    if submit_for_review:
                        destination = _submit_appraisal_to_next_stage(appraisal, profile)

                if submit_for_review:
                    messages.success(
                        request,
                        f"Appraisal submitted to {destination} successfully.",
                    )
                else:
                    messages.success(request, "Targets and competency selections saved successfully.")
                return redirect(
                    'hod_dashboard'
                    if profile.role == 'HOD'
                    else 'employee_dashboard'
                )
    else:
        formset = TargetFormSet(instance=appraisal)
        employee_info_form = AppraisalEmployeeInfoForm(
            instance=appraisal,
            profile=profile,
            prefix='employee_info',
        )
        training_formset = AppraisalTrainingFormSet(
            instance=appraisal,
            prefix='trainings',
        )
        competency_form = EmployeeCompetencySelectionForm(
            initial={
                'competencies': appraisal.competency_scores.values_list(
                    'competency_id',
                    flat=True,
                ),
                'competency_statements': [
                    f'{competency_score.competency_id}:{statement_score.statement_index}'
                    for competency_score in appraisal.competency_scores.prefetch_related(
                        'statement_scores',
                    )
                    for statement_score in competency_score.statement_scores.all()
                ],
            }
        )

    assigned_supervisor = get_assigned_supervisor(profile)
    if not competency_form.is_bound:
        competency_form = EmployeeCompetencySelectionForm(
            initial={
                'competencies': appraisal.competency_scores.values_list(
                    'competency_id',
                    flat=True,
                ),
                'competency_statements': [
                    f'{competency_score.competency_id}:{statement_index}'
                    for competency_score in appraisal.competency_scores.prefetch_related(
                        'statement_scores',
                    )
                    for statement_index in (
                        list(competency_score.statement_scores.values_list(
                            'statement_index',
                            flat=True,
                        ))
                        or list(range(len(competency_score.competency.description_items)))
                    )
                ],
            }
        )
    selected_statement_ids = set(
        competency_form['competency_statements'].value() or []
    )
    if request.method == 'POST' and request.POST.get('action') == 'save_competencies':
        if any(formset.errors) or formset.non_form_errors():
            current_step = 1
        elif training_formset.errors or training_formset.non_form_errors():
            current_step = 0
        elif 'employee_comments' in employee_info_form.errors:
            current_step = 3
        elif employee_info_form.errors:
            current_step = 0
        elif competency_form.errors:
            current_step = 3
    elif request.method == 'POST' and request.POST.get('action') == 'submit':
        if any(formset.errors) or formset.non_form_errors():
            current_step = 1
        elif 'employee_comments' in employee_info_form.errors:
            current_step = 3
        elif (
            employee_info_form.errors
            or training_formset.errors
            or training_formset.non_form_errors()
        ):
            current_step = 0
        else:
            current_step = 4
    return render(request, 'employee/fill_targets.html', {
        'formset': formset,
        'employee_info_form': employee_info_form,
        'training_formset': training_formset,
        'competency_form': competency_form,
        'competency_groups': competency_form.competency_groups,
        'core_competency_groups': [
            group for group in competency_form.competency_groups
            if group['competency'].competency_type == 'core'
        ],
        'non_core_competency_groups': [
            group for group in competency_form.competency_groups
            if group['competency'].competency_type == 'non_core'
        ],
        'selected_statement_ids': selected_statement_ids,
        'score_breakdown': appraisal.get_score_breakdown(),
        'appraisal': appraisal,
        'cycle_fill_url': cycle_fill_url,
        'current_step': current_step,
        'assigned_supervisor': (
            appraisal.supervisor
            if appraisal.status == 'returned_employee' and appraisal.supervisor_id
            else assigned_supervisor or appraisal.supervisor
        ),
        'read_only': False,
        'dashboard_url_name': _dashboard_url_name_for_profile(profile),
        'return_history': appraisal.returns.select_related('reviewer'),
        **_return_request_context_for_user(appraisal, profile),
    })


@login_required
def profile(request):
    employee_profile = request.user.profile
    photo_form = ProfilePhotoForm(instance=employee_profile)
    if request.method == 'POST':
        if request.POST.get('action') == 'remove_profile_photo':
            if employee_profile.profile_photo:
                employee_profile.profile_photo.delete(save=False)
                employee_profile.profile_photo = ''
                employee_profile.save(update_fields=['profile_photo'])
            messages.success(request, "Your profile photo was removed.")
            return redirect('profile')
        if request.POST.get('action') == 'upload_profile_photo':
            photo_form = ProfilePhotoForm(
                request.POST,
                request.FILES,
                instance=employee_profile,
            )
            if photo_form.is_valid():
                photo_form.save()
                messages.success(request, "Your profile photo has been updated.")
                return redirect('profile')
            form = UserProfileForm(
                instance=request.user,
                profile=employee_profile,
            )
        else:
            form = UserProfileForm(
                request.POST,
                instance=request.user,
                profile=employee_profile,
            )
            if form.is_valid():
                form.save()
                messages.success(request, "Your profile has been updated.")
                return redirect('profile')
    else:
        form = UserProfileForm(instance=request.user, profile=employee_profile)

    return render(request, 'account/profile.html', {
        'form': form,
        'photo_form': photo_form,
    })


@login_required
def profile_photo_file(request):
    photo = request.user.profile.profile_photo
    if not photo:
        raise Http404

    with photo.open('rb') as photo_file:
        with Image.open(photo_file) as image:
            content_type = Image.MIME.get(image.format)
    if content_type is None:
        raise Http404

    return FileResponse(photo.open('rb'), content_type=content_type)


@login_required
def notifications(request):
    notification_list = AppNotification.objects.filter(recipient=request.user)
    page = Paginator(notification_list, 20).get_page(request.GET.get('page', 1))
    return render(request, 'account/notifications.html', {
        'notification_page': page,
        'unread_notification_count': get_unread_notification_count(request.user.pk),
    })


@login_required
def notification_status(request):
    if request.method != 'GET':
        return JsonResponse({'error': 'Invalid request method.'}, status=405)

    unread_count = get_unread_notification_count(request.user.pk)
    response = JsonResponse({'unread_count': unread_count})
    response['Cache-Control'] = 'no-store'
    return response


@login_required
def open_notification(request, notification_id):
    if request.method != 'POST':
        return redirect('notifications')

    notification = get_object_or_404(
        AppNotification,
        pk=notification_id,
        recipient=request.user,
    )
    mark_as_read(notification)
    destination = notification.url
    if not destination or not url_has_allowed_host_and_scheme(
        destination,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        destination = reverse('dashboard_router')
    return redirect(destination)

def delete_noti(request,noti_id): 
    get_not=AppNotification.objects.get(id=noti_id)  
    get_not.delete()  
    messages.error(request,'deleted')
    return redirect('notifications')


@login_required
def mark_all_notifications_read(request):
    if request.method == 'POST':
        updated = AppNotification.objects.filter(
            recipient=request.user,
            read_at__isnull=True,
        ).update(read_at=timezone.now())
        if updated:
            invalidate_notification_cache(request.user.pk)
    return redirect('notifications')


@login_required
def change_password(request):
    from django.contrib.auth.forms import PasswordChangeForm

    if request.method == 'POST':
        form = PasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            record_auth_event(user, 'password_changed', request)
            messages.success(request, "Your password has been changed.")
            return redirect('profile')
    else:
        form = PasswordChangeForm(user=request.user)

    return render(request, 'account/password_change.html', {'form': form})


@login_required
def submit_to_supervisor(request, appraisal_id):
    """Changes the status to pending_manager and assigns the supervisor."""
    appraisal = get_object_or_404(Appraisal, id=appraisal_id, employee=request.user.profile)

    if appraisal.status in ['draft', 'returned_employee']:
        if not appraisal.targets.exists():
            messages.error(
                request,
                "Add at least one key result area and target before submitting your appraisal.",
            )
            return redirect('fill_targets_year', year=appraisal.cycle.year)
        if not appraisal.employee_comments:
            messages.error(request, "Add your overall assessment comments before submitting.")
            return redirect('fill_targets_year', year=appraisal.cycle.year)
        if not appraisal.competency_scores.exists():
            messages.error(
                request,
                "No competencies are saved on this appraisal. Return to Edit My Goals and use "
                "'Save and Submit to Supervisor' so your competency selections are saved with the submission.",
            )
            return redirect('fill_targets_year', year=appraisal.cycle.year)

        can_submit = (
            request.user.profile.role == 'HOD'
            or (
                appraisal.status == 'returned_employee'
                and appraisal.supervisor_id
            )
            or get_assigned_supervisor(request.user.profile) is not None
        )
        if not can_submit:
            messages.error(request, "You cannot submit because no supervisor or HOD has been assigned to you yet. Contact HR.")
            return redirect('employee_dashboard')

        destination = _submit_appraisal_to_next_stage(appraisal, request.user.profile)
        if destination is None:
            messages.error(request, "We could not submit your appraisal. Please contact HR for help.")
            return redirect('employee_dashboard')
        messages.success(request, f"Appraisal submitted to {destination} successfully!")
        return redirect('employee_submission_slip', appraisal_id=appraisal.id)
    
    return redirect(
        'hod_dashboard'
        if request.user.profile.role == 'HOD'
        else 'employee_dashboard'
    )



# ==========================================
# SUPERVISOR DASHBOARD (DUAL-HAT)
# ==========================================
@login_required
def supervisor_dashboard(request):
    """Shows the supervisor their own appraisal AND their team's appraisals."""
    profile = request.user.profile

    # Security check
    is_department_reviewer = DepartmentSupervisor.objects.filter(
        supervisor=profile,
    ).exists()
    if profile.role not in ['Supervisor', 'HOD', 'HR'] and not is_department_reviewer:
        messages.error(request, "Access denied.")
        return redirect('dashboard_router')

    appraisal_cycles = list(AppraisalCycle.objects.order_by('-year'))
    cycle = next((item for item in appraisal_cycles if item.is_active), None)
    
    # if not cycle:
    #     messages.error(request, "No active appraisal cycle.")
    #     return redirect('login')

    # A supervisor only has an own appraisal when they are assigned to a supervisor.
    assigned_supervisor = get_assigned_supervisor(profile)
    has_supervisor = assigned_supervisor is not None
    my_appraisal = None
    my_appraisals = []
    if has_supervisor:
        appraisals_by_cycle = _get_profile_appraisals_by_cycle(
            profile,
            appraisal_cycles,
            assigned_supervisor,
        )
        for appraisal_cycle in appraisal_cycles:
            appraisal = appraisals_by_cycle[appraisal_cycle.pk]
            my_appraisals.append(appraisal)
            if appraisal_cycle == cycle:
                my_appraisal = appraisal

    # 2. Get the team's appraisals waiting for review
    pending_reviews = Appraisal.objects.filter(
        supervisor=profile,
        status__in=['pending_manager', 'returned_supervisor']
    ).select_related('employee__user', 'employee__department', 'cycle')
    assigned_employees = EmployeeProfile.objects.filter(
        Q(role='Employee', department__supervisor_assignment__supervisor=profile)
        | Q(role='Supervisor', reporting_line__manager=profile)
    ).exclude(pk=profile.pk).select_related('user', 'department').distinct()
    assigned_appraisals = Appraisal.objects.filter(
        supervisor=profile,
    ).select_related('employee__user', 'employee__department', 'cycle').order_by(
        '-cycle__year',
        'employee__user__username',
    )

    context = {
        'cycle': cycle,
        'my_appraisal': my_appraisal,
        'my_appraisals': my_appraisals,
        **_previous_appraisal_context(profile),
        'has_supervisor': has_supervisor,
        'pending_reviews': pending_reviews,
        'assigned_employees': assigned_employees,
        'assigned_appraisals': assigned_appraisals,
    }

    return render(request, 'supervisor/dashboard.html', context)


@login_required
def supervisor_create_appraisals(request):
    """Creates draft appraisals for assigned employees selected by the supervisor."""
    profile = request.user.profile
    if profile.role != 'Supervisor':
        messages.error(request, "Only supervisors can create team appraisals.")
        return redirect('dashboard_router')

    if request.method != 'POST':
        return redirect('supervisor_dashboard')

    cycle = AppraisalCycle.objects.filter(is_active=True).first()
    if not cycle:
        messages.error(request, "No active appraisal cycle.")
        return redirect('supervisor_dashboard')

    assigned_employees = EmployeeProfile.objects.filter(
        Q(role='Employee', department__supervisor_assignment__supervisor=profile)
        | Q(role='Supervisor', reporting_line__manager=profile)
    ).select_related('user', 'department').distinct()
    selection = request.POST.get('employee_selection', '')

    if selection == 'all':
        employees = list(assigned_employees)
    else:
        try:
            employee_id = int(selection)
        except (TypeError, ValueError):
            messages.error(request, "Select an employee or choose all assigned employees.")
            return redirect('supervisor_dashboard')

        employee = assigned_employees.filter(pk=employee_id).first()
        if employee is None:
            messages.error(request, "You can only create appraisals for employees assigned to you.")
            return redirect('supervisor_dashboard')
        employees = [employee]

    if not employees:
        messages.warning(request, "No employees are assigned to you.")
        return redirect('supervisor_dashboard')

    created_count = 0
    assigned_count = 0
    skipped_count = 0
    with transaction.atomic():
        for employee in employees:
            appraisal, created = Appraisal.objects.get_or_create(
                employee=employee,
                cycle=cycle,
                defaults={'supervisor': profile},
            )
            if created:
                created_count += 1
            elif appraisal.status == 'draft' and appraisal.supervisor_id is None:
                appraisal.supervisor = profile
                appraisal.save(update_fields=['supervisor'])
                assigned_count += 1
            elif appraisal.supervisor_id == profile.id:
                skipped_count += 1
            else:
                skipped_count += 1

    messages.success(
        request,
        f"Created {created_count} appraisal(s), assigned {assigned_count} existing draft(s), "
        f"and left {skipped_count} existing appraisal(s) unchanged."
    )
    return redirect('supervisor_dashboard')


@login_required
def supervisor_review(request, appraisal_id):
    """Allows a supervisor or assigned HOD to score their appraisee."""
    profile = request.user.profile
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'employee_department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        id=appraisal_id,
        supervisor=profile,
    )
    if not _can_review_as_supervisor(profile, appraisal):
        messages.error(request, "You are not the assigned reviewer for this appraisal.")
        return redirect('dashboard_router')
    statement_scores = AppraisalCompetencyStatementScore.objects.filter(
        competency_score__appraisal=appraisal,
    ).select_related('competency_score__competency')

    # SECURITY: If it's not their turn, lock it.
    if appraisal.status not in ['pending_manager', 'returned_supervisor']:
        messages.warning(request, "This appraisal is not currently pending your review.")
        return redirect('hod_dashboard' if profile.role == 'HOD' else 'supervisor_dashboard')

    if request.method == 'POST':
        target_formset = SupervisorTargetFormSet(request.POST, instance=appraisal)
        appraiser_form = AppraiserReviewForm(request.POST, instance=appraisal)
        comp_formset = SupervisorCompetencyFormSet(
            request.POST,
            instance=appraisal,
            queryset=appraisal.competency_scores.select_related('competency'),
        )
        statement_score_formset = SupervisorCompetencyStatementFormSet(
            request.POST,
            queryset=statement_scores,
            prefix='statement_scores',
        )

        if (
            target_formset.is_valid()
            and appraiser_form.is_valid()
            and comp_formset.is_valid()
            and statement_score_formset.is_valid()
        ):
            submit_for_review = request.POST.get('action') == 'submit'
            if submit_for_review and appraisal.return_requests.filter(status='pending').exists():
                messages.error(
                    request,
                    "Decide the pending return request before submitting this appraisal.",
                )
                return redirect('supervisor_review', appraisal_id=appraisal.id)
            if submit_for_review and not appraiser_form.cleaned_data.get('supervisor_comments'):
                appraiser_form.add_error(
                    'supervisor_comments',
                    "Add the appraiser's comments before submitting this appraisal.",
                )
            if submit_for_review and not appraiser_form.cleaned_data.get('assessment_decision'):
                appraiser_form.add_error(
                    'assessment_decision',
                    'Select an assessment decision before submitting this appraisal.',
                )
            if appraiser_form.errors:
                messages.error(request, "Complete the required appraisal comments and decision before submitting.")
            else: 
                with transaction.atomic():
                    target_formset.save()
                    appraiser_form.save()
                    comp_formset.save()
                    statement_score_formset.save()
                if submit_for_review:
                    # messages.success(request, "Review saved. Submitting the appraisal to the HOD.")
                    return redirect('supervisor_submit_to_hod', appraisal_id=appraisal.id)
                messages.success(request, "Scores and appraisal comments saved as draft.")
                return redirect('hod_dashboard' if profile.role == 'HOD' else 'supervisor_dashboard')
        messages.error(request, "Scores were not saved. Correct the highlighted errors and try again.")
    else:
        target_formset = SupervisorTargetFormSet(instance=appraisal)
        appraiser_form = AppraiserReviewForm(instance=appraisal)
        comp_formset = SupervisorCompetencyFormSet(
            instance=appraisal,
            queryset=appraisal.competency_scores.select_related('competency'),
        )
        statement_score_formset = SupervisorCompetencyStatementFormSet(
            queryset=statement_scores,
            prefix='statement_scores',
        )

    statement_forms_by_competency_score = {}
    for statement_form in statement_score_formset.forms:
        statement_forms_by_competency_score.setdefault(
            statement_form.instance.competency_score_id,
            [], 
        ).append(statement_form)
    competency_groups = [
        {
            'form': competency_form,
            'statements': statement_forms_by_competency_score.get(
                competency_form.instance.pk,
                [],
            ),
            'weight': (
                '0.3'
                if competency_form.instance.competency.competency_type == 'core'
                else '0.1'
            ),
            'type': competency_form.instance.competency.competency_type,
        }
        for competency_form in comp_formset.forms
    ]

    return render(request, 'supervisor/review.html', {
        'appraisal': appraisal,
        'is_hod_review': profile.role == 'HOD',
        'target_formset': target_formset,
        'appraiser_form': appraiser_form,
        'comp_formset': comp_formset,
        'statement_score_formset': statement_score_formset,
        'competency_groups': competency_groups,
        'trainings': appraisal.trainings.all(),
        'return_form': AppraisalReturnForm(),
        'return_history': appraisal.returns.select_related('reviewer'),
        **_return_request_context_for_user(appraisal, profile),
    })


@login_required
def supervisor_submit_to_hod(request, appraisal_id):
    """Sends employee appraisals to their HOD and supervisor appraisals to HR."""
    profile = request.user.profile
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'employee_department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        id=appraisal_id,
        supervisor=profile,
    )

    if appraisal.status in ['pending_manager', 'returned_supervisor']:
        if appraisal.return_requests.filter(status='pending').exists():
            messages.error(
                request,
                "Decide the pending return request before submitting this appraisal.",
            )
            return redirect('supervisor_review', appraisal_id=appraisal.id)
        if not _can_review_as_supervisor(profile, appraisal):
            messages.error(request, "You are not authorized to submit this appraisal.")
            return redirect('dashboard_router')
        if not appraisal.supervisor_comments or not appraisal.assessment_decision:
            messages.error(
                request,
                "Complete the appraiser's comments and assessment decision before submitting to the HOD.",
            )
            return redirect('supervisor_review', appraisal_id=appraisal.id)
        try:
            appraisal.validate_final_score_inputs()
        except ValidationError as error:
            messages.error(request, error.messages[0])
            return redirect('supervisor_review', appraisal_id=appraisal.id)

        is_department_lead_appraisal = DepartmentSupervisor.objects.filter(
            supervisor=appraisal.employee,
        ).exists()
        if appraisal.employee.role == 'Supervisor' or is_department_lead_appraisal:
            if profile.role != 'HOD':
                messages.error(request, "Only the assigned HOD can submit this appraisal to HR.")
                return redirect('dashboard_router')
            appraisal.hod = profile
            appraisal.status = 'pending_hr'
            appraisal.supervisor_signed_at = timezone.now()
            appraisal.hod_signed_at = timezone.now()
            appraisal.save(update_fields=[
                'hod',
                'status',
                'supervisor_signed_at',
                'hod_signed_at',
            ])
            _notify_hr_of_submission(appraisal)
            messages.success(request, "Appraisal submitted to HR successfully.")
            return redirect('hod_dashboard')

        from .models import AssignDepartmentHod
        hod = None
        if appraisal.status == 'returned_supervisor' and appraisal.hod_id:
            hod = appraisal.hod
        else:
            hod_assignment = AssignDepartmentHod.objects.filter(
                department=appraisal.employee.department,
            ).select_related('hod__user').first()
            hod = hod_assignment.hod if hod_assignment else None

        if not hod:
            messages.error(request, "Cannot submit: No HOD assigned to this department yet.")
            return redirect('dashboard_router')

        if profile.role == 'HOD' and hod.id == profile.id:
            appraisal.hod = hod
            appraisal.status = 'pending_hr'
            appraisal.supervisor_signed_at = timezone.now()
            appraisal.hod_signed_at = timezone.now()
            appraisal.save(update_fields=[
                'hod',
                'status',
                'supervisor_signed_at',
                'hod_signed_at',
            ])
            _notify_hr_of_submission(appraisal)
            messages.success(request, "Appraisal submitted to HR successfully.")
            return redirect('hod_dashboard')

        appraisal.hod = hod
        appraisal.status = 'pending_hod'
        appraisal.supervisor_signed_at = timezone.now()
        appraisal.save()
        notify_users(
            [hod.user],
            title='Appraisal awaiting your review',
            message=(
                f"{appraisal.employee.user.get_full_name() or appraisal.employee.user.username}'s "
                f"{appraisal.cycle.year} appraisal is ready for your review."
            ),
            url=reverse('hod_review', args=[appraisal.id]),
        )

        messages.success(request, "Appraisal successfully submitted to the HOD.")
    
    return redirect('hod_dashboard' if profile.role == 'HOD' else 'supervisor_dashboard')


@login_required
def request_appraisal_return(request, appraisal_id):
    if request.method != 'POST':
        return redirect('dashboard_router')

    profile = _return_request_actor_profile(request)
    if profile is None:
        return redirect('dashboard_router')
    with transaction.atomic():
        appraisal = get_object_or_404(
            Appraisal.objects.select_related(
                'employee__user',
                'supervisor__user',
                'hod__user',
                'cycle',
            ),
            pk=appraisal_id,
        )
        stage, destination_status = _return_request_stage_and_destination(
            appraisal,
            profile,
        )
        if stage is None:
            messages.error(
                request,
                "You cannot request this appraisal back at its current stage.",
            )
            return redirect('dashboard_router')

        if appraisal.return_requests.filter(status='pending').exists():
            messages.warning(request, "A return request is already awaiting a decision.")
            return redirect(_return_request_page_url(appraisal, profile))

        form = AppraisalReturnRequestForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Enter a reason of at least 5 characters.")
            return redirect(_return_request_page_url(appraisal, profile))

        if appraisal.status == 'pending_manager':
            reviewer_users = [appraisal.supervisor.user] if appraisal.supervisor_id else []
            review_url = reverse('supervisor_review', args=[appraisal.id])
        elif appraisal.status == 'pending_hod':
            reviewer_users = [appraisal.hod.user] if appraisal.hod_id else []
            review_url = reverse('hod_review', args=[appraisal.id])
        else:
            reviewer_users = [
                hr_profile.user
                for hr_profile in EmployeeProfile.objects.filter(
                    role='HR',
                    user__is_active=True,
                ).select_related('user')
            ]
            review_url = reverse('hr_approve', args=[appraisal.id])

        if not reviewer_users:
            messages.error(
                request,
                "No reviewer is assigned to decide this return request. Contact HR.",
            )
            return redirect('dashboard_router')

        AppraisalReturnRequest.objects.create(
            appraisal=appraisal,
            requester=request.user,
            stage=stage,
            from_status=appraisal.status,
            to_status=destination_status,
            reason=form.cleaned_data['reason'].strip(),
        )
        notify_users(
            reviewer_users,
            title='Appraisal return requested',
            message=(
                f"{request.user.get_full_name() or request.user.username} requested "
                f"the {appraisal.cycle.year} appraisal for "
                f"{appraisal.employee.user.get_full_name() or appraisal.employee.user.username} "
                "back for corrections."
            ),
            url=review_url,
        )

    messages.success(request, "Your return request was sent to the reviewer.")
    return redirect('dashboard_router')


@login_required
def decide_appraisal_return_request(request, request_id):
    if request.method != 'POST':
        return redirect('dashboard_router')

    form = AppraisalReturnDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Choose whether to approve or reject the request.")
        return redirect('dashboard_router')

    decision = form.cleaned_data['decision']
    decision_comment = form.cleaned_data['decision_comment'].strip()
    if decision == 'rejected' and len(decision_comment) < 5:
        messages.error(request, "Provide feedback of at least 5 characters when rejecting.")
        return redirect('dashboard_router')

    with transaction.atomic():
        return_request = get_object_or_404(
            AppraisalReturnRequest.objects.select_related(
                'appraisal__cycle',
                'appraisal__employee__user',
                'requester',
            ),
            pk=request_id,
            status='pending',
        )
        appraisal = Appraisal.objects.get(
            pk=return_request.appraisal_id,
        )
        profile = _return_request_actor_profile(request)
        if profile is None:
            return redirect('dashboard_router')
        if not _can_decide_return_request(profile, appraisal):
            messages.error(request, "You are not the current reviewer for this request.")
            return redirect('dashboard_router')

        if appraisal.status != return_request.from_status:
            messages.error(
                request,
                "The appraisal stage has changed. This return request can no longer be decided.",
            )
            return redirect('dashboard_router')

        return_request.reviewer = request.user
        return_request.status = decision
        return_request.decision_comment = decision_comment
        return_request.decided_at = timezone.now()
        return_request.save(update_fields=[
            'reviewer',
            'status',
            'decision_comment',
            'decided_at',
        ])

        if decision == 'approved':
            appraisal.status = return_request.to_status
            appraisal.save(update_fields=['status'])

        if return_request.requester_id:
            if decision == 'approved':
                recipient_url = _return_request_destination_url(
                    appraisal,
                    return_request.to_status,
                    return_request.requester,
                )
                notification_title = 'Appraisal return request approved'
                notification_message = (
                    f"Your request to return the {appraisal.cycle.year} appraisal was approved. "
                    f"It is ready for updates. {decision_comment}"
                )
            else:
                recipient_url = _return_request_destination_url(
                    appraisal,
                    appraisal.status,
                    return_request.requester,
                )
                notification_title = 'Appraisal return request rejected'
                notification_message = (
                    f"Your request to return the {appraisal.cycle.year} appraisal was rejected. "
                    f"Feedback: {decision_comment}"
                )
            notify_users(
                [return_request.requester],
                title=notification_title,
                message=notification_message[:300],
                url=recipient_url,
            )

    messages.success(
        request,
        "Return request approved." if decision == 'approved'
        else "Return request rejected and feedback sent to the requester.",
    )
    return redirect('dashboard_router')


@login_required
def return_appraisal(request, appraisal_id):
    """Returns an appraisal to the previous stage with a required audit comment."""
    if request.method != 'POST':
        return redirect('dashboard_router')

    profile = request.user.profile
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'employee_department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        id=appraisal_id,
    )
    if appraisal.return_requests.filter(status='pending').exists():
        messages.error(
            request,
            "Decide the pending return request before returning this appraisal.",
        )
        return redirect('dashboard_router')

    if (
        appraisal.employee.role == 'Employee'
        and _can_review_as_supervisor(profile, appraisal)
        and appraisal.status in ['pending_manager', 'returned_supervisor']
    ):
        stage = 'supervisor'
        destination_status = 'returned_employee'
    elif (
        profile.role == 'HOD'
        and appraisal.employee.role == 'Supervisor'
        and appraisal.supervisor_id == profile.id
        and appraisal.status in ['pending_manager', 'returned_supervisor']
    ):
        stage = 'hod'
        destination_status = 'returned_employee'
    elif (
        profile.role == 'HOD'
        and appraisal.hod_id == profile.id
        and appraisal.status in ['pending_hod', 'returned_hod']
    ):
        stage = 'hod'
        destination_status = 'returned_supervisor'
    elif profile.role == 'HR' and appraisal.status == 'pending_hr':
        stage = 'hr'
        destination_status = (
            'returned_employee'
            if appraisal.employee.role == 'HOD'
            else 'returned_hod'
        )
    else:
        messages.error(request, "You are not authorized to return this appraisal at its current stage.")
        return redirect('dashboard_router')

    form = AppraisalReturnForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a return comment of at least 5 characters.")
        return redirect({
            'supervisor': 'supervisor_review',
            'hod': 'hod_review',
            'hr': 'hr_approve',
        }[stage], appraisal_id=appraisal.id)

    with transaction.atomic():
        AppraisalReturn.objects.create(
            appraisal=appraisal,
            reviewer=request.user,
            reviewer_name=request.user.get_username(),
            stage=stage,
            from_status=appraisal.status,
            to_status=destination_status,
            comment=form.cleaned_data['comment'].strip(),
        )
        appraisal.status = destination_status
        appraisal.save(update_fields=['status'])

    return_recipients = {
        'returned_employee': appraisal.employee.user,
        'returned_supervisor': appraisal.supervisor.user if appraisal.supervisor_id else None,
        'returned_hod': appraisal.hod.user if appraisal.hod_id else None,
    }
    return_destinations = {
        'returned_employee': (
            reverse('hod_dashboard')
            if appraisal.employee.role == 'HOD'
            else reverse('supervisor_dashboard')
            if appraisal.employee.role == 'Supervisor'
            else reverse('employee_dashboard')
        ),
        'returned_supervisor': reverse('supervisor_dashboard'),
        'returned_hod': reverse('hod_dashboard'),
    }
    notify_users(
        [return_recipients[destination_status]],
        title='Appraisal returned for updates',
        message=(
            f"{appraisal.employee.user.get_full_name() or appraisal.employee.user.username}'s "
            f"{appraisal.cycle.year} appraisal was returned by {request.user.username}."
        ),
        url=return_destinations[destination_status],
    )

    recipient = {
        'returned_employee': (
            {
                'HOD': 'the HOD',
                'Supervisor': 'the supervisor',
            }.get(appraisal.employee.role, 'the employee')
        ),
        'returned_supervisor': 'the supervisor',
        'returned_hod': 'the HOD',
    }[destination_status]
    messages.success(request, f"Appraisal returned to {recipient} with your comment.")
    return redirect({
        'supervisor': 'supervisor_dashboard',
        'hod': 'hod_dashboard',
        'hr': 'hr_dashboard',
    }[stage])


# Add to views.py

@login_required
def hod_dashboard(request):
    """Shows the HOD appraisals pending their review, plus their own appraisal."""
    profile = request.user.profile

    if profile.role != 'HOD':
        messages.error(request, "Access denied. You are not an HOD.")
        return redirect('dashboard_router')

    appraisal_cycles = list(AppraisalCycle.objects.order_by('-year'))
    cycle = next((item for item in appraisal_cycles if item.is_active), None)

    my_appraisals = []
    my_appraisal = None
    appraisals_by_cycle = _get_profile_appraisals_by_cycle(
        profile,
        appraisal_cycles,
    )
    for appraisal_cycle in appraisal_cycles:
        appraisal = appraisals_by_cycle[appraisal_cycle.pk]
        my_appraisals.append(appraisal)
        if appraisal_cycle == cycle:
            my_appraisal = appraisal

    # 2. Get appraisals waiting for HOD review
    pending_reviews = Appraisal.objects.filter(
        Q(hod=profile, status__in=['pending_hod', 'returned_hod'])
        | Q(
            supervisor=profile,
            employee__role__in=['Employee', 'Supervisor'],
            status__in=['pending_manager', 'returned_supervisor'],
        )
    ).select_related(
        'employee__user',
        'employee__department',
        'employee_department',
        'supervisor__user',
        'cycle',
    )

    historical_appraisals = Appraisal.objects.filter(
        hod=profile,
        status__in=['pending_hr', 'completed']
    ).select_related(
        'employee__user',
        'employee_department',
        'supervisor__user',
        'hod__user',
        'cycle',
    ).order_by('-cycle__year', 'employee__user__username')
    context = {
        'cycle': cycle,
        'my_appraisal': my_appraisal,
        'my_appraisals': my_appraisals,
        **_previous_appraisal_context(profile),
        'pending_reviews': pending_reviews,
        'historical_appraisals': historical_appraisals,
    }

    return render(request, 'hod/dashboard.html', context)


def _render_read_only_appraisal(
    request,
    appraisal,
    *,
    allow_pdf=False,
    dashboard_url_name='employee_dashboard',
):
    profile = request.user.profile
    score_context = _score_breakdown_context(appraisal)
    return render(request, 'employee/fill_targets.html', {
        'appraisal': appraisal,
        'read_only': True,
        'print_mode': False,
        'allow_pdf': allow_pdf,
        'dashboard_url_name': dashboard_url_name,
        'trainings': appraisal.trainings.all(),
        'targets': score_context['score_breakdown']['targets'],
        **score_context,
        'return_history': appraisal.returns.select_related('reviewer'),
        **_return_request_context_for_user(appraisal, profile),
        'assigned_supervisor': appraisal.supervisor,
    })


def _score_breakdown_context(appraisal):
    score_breakdown = appraisal.get_score_breakdown()
    groups = score_breakdown['groups']
    return {
        'score_breakdown': score_breakdown,
        'core_score_groups': [group for group in groups if group['type'] == 'core'],
        'non_core_score_groups': [
            group for group in groups if group['type'] == 'non_core'
        ],
    }


@login_required
def supervisor_view_appraisal(request, appraisal_id):
    profile = request.user.profile
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'employee_department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        pk=appraisal_id,
        supervisor=profile,
    )
    if not _can_review_as_supervisor(profile, appraisal):
        messages.error(request, "Access denied. Only the assigned reviewer can view this appraisal.")
        return redirect('dashboard_router')
    return _render_read_only_appraisal(
        request,
        appraisal,
        dashboard_url_name='supervisor_dashboard',
    )


@login_required
def hod_view_appraisal(request, appraisal_id):
    profile = request.user.profile
    if profile.role != 'HOD':
        messages.error(request, "Access denied. Only the assigned HOD can view this appraisal.")
        return redirect('dashboard_router')

    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'employee_department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ).filter(
            Q(hod=profile)
            | Q(
                supervisor=profile,
                employee__role__in=['Employee', 'Supervisor'],
                status__in=['pending_manager', 'returned_supervisor'],
            )
        ),
        pk=appraisal_id,
    )
    return _render_read_only_appraisal(
        request,
        appraisal,
        dashboard_url_name='hod_dashboard',
    )

@login_required
def hod_review(request, appraisal_id):
    """Allows the HOD to view the full appraisal and add their final comments."""
    profile = request.user.profile
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'employee_department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        id=appraisal_id,
        hod=profile,
    )

    # SECURITY: Only review if it is actually pending HOD
    if appraisal.status not in ['pending_hod', 'returned_hod']:
        messages.warning(request, "This appraisal is not pending your review.")
        return redirect('hod_dashboard')

    if request.method == 'POST':
        if appraisal.return_requests.filter(status='pending').exists():
            messages.error(
                request,
                "Decide the pending return request before submitting this appraisal to HR.",
            )
            return redirect('hod_review', appraisal_id=appraisal.id)
        # The HOD only submits comments and pushes to HR
        form = HODCommentForm(request.POST, instance=appraisal)
        if form.is_valid():
            appraisal = form.save(commit=False)
            appraisal.status = 'pending_hr'
            appraisal.hod_signed_at = timezone.now()
            appraisal.save()
            _notify_hr_of_submission(appraisal)
            
            messages.success(request, "Appraisal reviewed and submitted to HR successfully.")
            return redirect('hod_dashboard')
    else:
        form = HODCommentForm(instance=appraisal)

    # We need to fetch the targets and competencies to display them (read-only)
    score_context = _score_breakdown_context(appraisal)
    targets = score_context['score_breakdown']['targets']

    return render(request, 'hod/review.html', {
        'appraisal': appraisal,
        'form': form,
        'targets': targets,
        **score_context,
        'trainings': appraisal.trainings.all(),
        'return_form': AppraisalReturnForm(),
        'return_history': appraisal.returns.select_related('reviewer'),
        **_return_request_context_for_user(appraisal, profile),
    })


# Add to views.py

@login_required
def hr_dashboard(request):
    """Shows HR all appraisals pending final approval."""
    profile = request.user.profile

    if profile.role != 'HR':
        messages.error(request, "Access denied. You are not an HR Manager.")
        return redirect('dashboard_router')

    users = EmployeeProfile.objects.filter(
        role__in=['Employee', 'Supervisor', 'HOD']
    ).select_related('user').order_by('user__username')
    role_choices = [
        choice for choice in EmployeeProfile.ROLE_CHOICES
        if choice[0] != 'HR'
    ]
    reporting_form = SupervisorReportingForm(prefix='reporting')
    hod_assignment_form = HRDepartmentHodAssignmentForm()
    cycle_form = AppraisalCycleForm()
    reporting_confirmation = None

    if request.method == 'POST' and request.POST.get('cycle_action') == 'create':
        cycle_form = AppraisalCycleForm(request.POST)
        if cycle_form.is_valid():
            with transaction.atomic():
                cycle = cycle_form.save(commit=False)
                if cycle.is_active:
                    for existing_cycle in AppraisalCycle.objects.exclude(pk=cycle.pk):
                        if existing_cycle.is_active:
                            existing_cycle.is_active = False
                            existing_cycle.save(update_fields=['is_active'])
                cycle.save()
            messages.success(request, f"Appraisal cycle {cycle.year} was created successfully.")
            return redirect(f"{reverse('hr_dashboard')}#cycles")
    elif request.method == 'POST':
        if request.POST.get('assignment_action') == 'save':
            assignment_form, transfer_confirmation, saved_assignment = (
                _process_hr_supervisor_assignment(request)
            )
            if saved_assignment:
                employee_username, supervisor_username, transferred_count = saved_assignment
                messages.success(
                    request,
                    f"Supervisor for {employee_username} was assigned to {supervisor_username}.",
                )
                if transferred_count:
                    messages.success(
                        request,
                        f"Transferred {transferred_count} in-progress appraisal(s). "
                        "Existing scores, comments, and return history were preserved.",
                    )
                return redirect(f"{reverse('hr_dashboard')}#assignments")
        elif request.POST.get('reporting_action') == 'save':
            reporting_form, reporting_confirmation, saved_reporting = (
                _process_hr_supervisor_reporting(request)
            )
            if saved_reporting:
                supervisor_username, manager_username, transferred_count = saved_reporting
                messages.success(
                    request,
                    f"Reporting assignment from {supervisor_username} to {manager_username} was saved.",
                )
                if transferred_count:
                    messages.success(
                        request,
                        f"Transferred {transferred_count} in-progress appraisal(s). "
                        "Existing scores, comments, and return history were preserved.",
                    )
                return redirect(f"{reverse('hr_dashboard')}#assignments")
        elif request.POST.get('hod_assignment_action') == 'save':
            hod_assignment_form, saved_hod_assignment = (
                _process_hr_department_hod_assignment(request)
            )
            if saved_hod_assignment:
                department_name, hod_username = saved_hod_assignment
                messages.success(
                    request,
                    f"{hod_username} was assigned as HOD for {department_name}.",
                )
                return redirect(f"{reverse('hr_dashboard')}#assignments")
        else:
            assignment_form = HRSupervisorAssignmentForm()
            transfer_confirmation = None

        if request.POST.get('assignment_action') != 'save':
            assignment_form = HRSupervisorAssignmentForm()
            transfer_confirmation = None

        if request.POST.get('assignment_action') != 'save' and not (
            request.POST.get('reporting_action') == 'save'
            or request.POST.get('hod_assignment_action') == 'save'
        ):
            roles_dashboard_url = f"{reverse('hr_dashboard')}#roles"
            submitted_roles = {}
            allowed_profile_ids = {str(user.id) for user in users}
            for key, role in request.POST.items():
                if not key.startswith('role_'):
                    continue
                profile_id = key.removeprefix('role_')
                if profile_id not in allowed_profile_ids or role not in dict(role_choices):
                    messages.error(request, "Choose a valid user and role.")
                    return redirect(roles_dashboard_url)
                submitted_roles[profile_id] = role

            if not submitted_roles:
                messages.error(request, "No user roles were submitted.")
                return redirect(roles_dashboard_url)

            changed_profiles = [
                user_profile
                for user_profile in users
                if submitted_roles.get(str(user_profile.pk))
                and submitted_roles[str(user_profile.pk)] != user_profile.role
            ]
            changed_profile_ids = [profile.pk for profile in changed_profiles]
            pending_supervisor_counts = {
                row['supervisor_id']: row['total']
                for row in Appraisal.objects.filter(
                    supervisor_id__in=changed_profile_ids,
                    status__in=['pending_manager', 'returned_supervisor'],
                ).values('supervisor_id').annotate(total=Count('pk'))
            }
            pending_hod_counts = {
                row['hod_id']: row['total']
                for row in Appraisal.objects.filter(
                    hod_id__in=changed_profile_ids,
                    status__in=['pending_hod', 'returned_hod', 'pending_hr'],
                ).values('hod_id').annotate(total=Count('pk'))
            }
            department_supervisor_ids = set(
                DepartmentSupervisor.objects.filter(
                    supervisor_id__in=changed_profile_ids,
                ).values_list('supervisor_id', flat=True)
            )
            reporting_supervisor_ids = set(
                SupervisorReporting.objects.filter(
                    supervisor_id__in=changed_profile_ids,
                ).values_list('supervisor_id', flat=True)
            )
            reporting_manager_ids = set(
                SupervisorReporting.objects.filter(
                    manager_id__in=changed_profile_ids,
                ).values_list('manager_id', flat=True)
            )
            department_hod_ids = set(
                AssignDepartmentHod.objects.filter(
                    hod_id__in=changed_profile_ids,
                ).values_list('hod_id', flat=True)
            )

            blocked_role_changes = []
            for user_profile in changed_profiles:
                profile_id = user_profile.pk
                pending_as_supervisor = pending_supervisor_counts.get(profile_id, 0)
                pending_as_hod = pending_hod_counts.get(profile_id, 0)
                has_assignments = profile_id in department_supervisor_ids
                if user_profile.role == 'Supervisor':
                    has_assignments = has_assignments or profile_id in reporting_supervisor_ids
                elif user_profile.role == 'HOD':
                    has_assignments = has_assignments or (
                        profile_id in reporting_manager_ids
                        or profile_id in department_hod_ids
                    )

                if pending_as_supervisor or pending_as_hod or has_assignments:
                    reason = []
                    if pending_as_supervisor:
                        reason.append(
                            f'{pending_as_supervisor} appraisal(s) awaiting supervisor review',
                        )
                    if pending_as_hod:
                        reason.append(
                            f'{pending_as_hod} appraisal(s) awaiting HOD review or HR completion',
                        )
                    if has_assignments:
                        reason.append('department or supervisor reporting assignments')
                    blocked_role_changes.append(
                        f"{user_profile.user.username} has " + ' and '.join(reason)
                    )

            if blocked_role_changes:
                messages.error(
                    request,
                    "Reassign the following supervisor workload and reporting assignments "
                    "before changing roles: "
                    + '; '.join(blocked_role_changes) + '.',
                )
                return redirect(roles_dashboard_url)

            changed_count = 0
            with transaction.atomic():
                for user_profile in changed_profiles:
                    new_role = submitted_roles[str(user_profile.pk)]
                    old_role = user_profile.get_role_display()
                    user_profile.role = new_role
                    user_profile.save(update_fields=['role'])
                    notify_users(
                        [user_profile.user],
                        title='Your account role was updated',
                        message=(
                            f"Your role was changed from {old_role} to "
                            f"{user_profile.get_role_display()} by HR."
                        ),
                        url=reverse('dashboard_router'),
                    )
                    changed_count += 1

            if changed_count:
                messages.success(request, f"Updated {changed_count} user role(s).")
            else:
                messages.info(request, "No user roles needed updating.")
            return redirect(roles_dashboard_url)

    # Get all appraisals that the HOD has pushed to HR
    pending_reviews = Appraisal.objects.filter(
        status='pending_hr'
    ).select_related(
        'employee__user',
        'employee__department',
        'employee_department',
        'supervisor__user',
        'hod__user',
        'cycle',
    )

    cycles = AppraisalCycle.objects.annotate(
        appraisal_count=Count('appraisals'),
    ).order_by('-year')
    departments = Department.objects.order_by('name')
    selected_cycle = request.GET.get('cycle', '').strip()
    available_cycle_years = set(cycles.values_list('year', flat=True))
    if selected_cycle:
        try:
            selected_cycle_year = int(selected_cycle)
        except ValueError:
            selected_cycle_year = None
        if selected_cycle_year not in available_cycle_years:
            selected_cycle_year = None
    else:
        selected_cycle_year = AppraisalCycle.objects.filter(
            is_active=True,
        ).values_list('year', flat=True).first()

    selected_department = request.GET.get('department', '').strip()
    if selected_department and not departments.filter(pk=selected_department).exists():
        selected_department = ''

    dashboard_appraisals = Appraisal.objects.all()
    if selected_cycle_year is not None:
        dashboard_appraisals = dashboard_appraisals.filter(
            cycle__year=selected_cycle_year,
        )
    if selected_department:
        dashboard_appraisals = dashboard_appraisals.filter(
            Q(employee_department_id=selected_department)
            | Q(employee_department__isnull=True, employee__department_id=selected_department),
        )

    status_counts = dashboard_appraisals.aggregate(
        total=Count('pk'),
        draft=Count('pk', filter=Q(status='draft')),
        supervisor=Count('pk', filter=Q(status__in=['pending_manager', 'returned_supervisor'])),
        hod=Count('pk', filter=Q(status__in=['pending_hod', 'returned_hod'])),
        hr=Count('pk', filter=Q(status='pending_hr')),
        returned_employee=Count('pk', filter=Q(status='returned_employee')),
        completed=Count('pk', filter=Q(status='completed')),
    )
    department_rows = dashboard_appraisals.annotate(
        department_label=Case(
            When(employee_department__isnull=False, then=F('employee_department__name')),
            When(employee_department_name__gt=Value(''), then=F('employee_department_name')),
            When(employee__department__isnull=False, then=F('employee__department__name')),
            default=Value('Unassigned'),
            output_field=CharField(),
        ),
    ).values('department_label').annotate(
        total=Count('pk'),
        completed=Count('pk', filter=Q(status='completed')),
    ).order_by('department_label')
    department_chart = [
        {
            'name': row['department_label'],
            'completed': row['completed'],
            'in_progress': row['total'] - row['completed'],
        }
        for row in department_rows
    ]

    return render(request, 'hr/dashboard.html', {
        'pending_reviews': pending_reviews,
        'pending_previous_appraisal_count': PreviousAppraisal.objects.filter(
            status=PreviousAppraisal.Status.PENDING,
        ).count(),
        'departments': departments,
        'cycles': cycles,
        'cycle_form': cycle_form,
        'users': users,
        'role_choices': role_choices,
        'supervisor_assignment_form': (
            assignment_form
            if request.method == 'POST' and request.POST.get('assignment_action') == 'save'
            else HRSupervisorAssignmentForm()
        ),
        'transfer_confirmation': (
            transfer_confirmation
            if request.method == 'POST' and request.POST.get('assignment_action') == 'save'
            else None
        ),
        'reporting_form': reporting_form,
        'reporting_confirmation': reporting_confirmation,
        'department_hod_assignment_form': hod_assignment_form,
        'department_hod_assignments': AssignDepartmentHod.objects.select_related(
            'department',
            'hod__user',
        ).order_by('department__name', 'pk'),
        'supervisor_reporting_assignments': SupervisorReporting.objects.select_related(
            'supervisor__user',
            'supervisor__department',
            'manager__user',
        ).order_by('supervisor__user__username'),
        'dashboard_filters': {
            'cycle': selected_cycle_year,
            'department': selected_department,
        },
        'dashboard_counts': status_counts,
        'workflow_chart': [
            {'label': 'Draft', 'count': status_counts['draft']},
            {'label': 'Supervisor review', 'count': status_counts['supervisor']},
            {'label': 'HOD review', 'count': status_counts['hod']},
            {'label': 'HR approval', 'count': status_counts['hr']},
            {'label': 'Returned to employee', 'count': status_counts['returned_employee']},
            {'label': 'Completed', 'count': status_counts['completed']},
        ],
        'department_chart': department_chart,
    })


def _can_manage_backups(user):
    return user.is_superuser or EmployeeProfile.objects.filter(
        user=user,
        role='HR',
    ).exists()


@login_required
def backup_dashboard(request):
    if not _can_manage_backups(request.user):
        return HttpResponseForbidden('Backup access is restricted to HR and superusers.')

    backup_runs = BackupRun.objects.select_related('requested_by')[:50]
    return render(request, 'app/backup_dashboard.html', {
        'backup_runs': backup_runs,
        'backup_retention_days': settings.BACKUP_RETENTION_DAYS,
    })


@login_required
@require_POST
def start_backup(request):
    if not _can_manage_backups(request.user):
        return HttpResponseForbidden('Backup access is restricted to HR and superusers.')

    if BackupRun.objects.filter(status__in=['queued', 'running']).exists():
        messages.warning(request, 'A backup is already queued or running.')
        return redirect('backup_dashboard')

    backup_run = BackupRun.objects.create(
        requested_by=request.user,
        trigger_type='manual',
    )
    AuditLog.objects.create(
        actor=request.user,
        actor_username=request.user.username,
        actor_role='HR' if not request.user.is_superuser else 'Superadmin',
        actor_is_superuser=request.user.is_superuser,
        action='backup_started',
        model_name='BackupRun',
        object_id=str(backup_run.pk),
    )
    try:
        create_backup.delay(backup_run.pk)
    except Exception:
        logger.exception('Could not queue backup run %s.', backup_run.pk)
        backup_run.status = 'failed'
        backup_run.completed_at = timezone.now()
        backup_run.error = 'Could not queue the backup. Check that Redis is running.'
        backup_run.save(update_fields=['status', 'completed_at', 'error'])
        messages.error(request, backup_run.error)
    else:
        messages.success(request, 'Backup queued. Refresh this page to see its status.')
    return redirect('backup_dashboard')


@login_required
def download_backup(request, backup_run_id):
    if not _can_manage_backups(request.user):
        return HttpResponseForbidden('Backup access is restricted to HR and superusers.')

    backup_run = get_object_or_404(BackupRun, pk=backup_run_id, status='succeeded')
    backup_root = Path(settings.BACKUP_ROOT).resolve()
    archive_path = (backup_root / Path(backup_run.archive_name).name).resolve()
    if archive_path.parent != backup_root or not archive_path.is_file():
        raise Http404('Backup file not found.')

    AuditLog.objects.create(
        actor=request.user,
        actor_username=request.user.username,
        actor_role='HR' if not request.user.is_superuser else 'Superadmin',
        actor_is_superuser=request.user.is_superuser,
        action='backup_downloaded',
        model_name='BackupRun',
        object_id=str(backup_run.pk),
        object_label=backup_run.archive_name,
    )
    return FileResponse(
        archive_path.open('rb'),
        as_attachment=True,
        filename=archive_path.name,
        content_type='application/zip',
    )


@login_required
def audit_log(request):
    profile_role = EmployeeProfile.objects.filter(
        user=request.user,
    ).values_list('role', flat=True).first()
    is_superadmin = request.user.is_superuser
    if not is_superadmin and profile_role != 'HR':
        messages.error(request, "Access denied. Audit records are restricted to HR and Superadmin.")
        return redirect('dashboard_router')

    entries = AuditLog.objects.select_related('actor')
    if not is_superadmin:
        entries = entries.filter(actor_is_superuser=False)

    search = request.GET.get('q', '').strip()[:100]
    if search:
        entries = entries.filter(
            Q(actor_username__icontains=search)
            | Q(action__icontains=search)
            | Q(model_name__icontains=search)
            | Q(object_id__icontains=search)
            | Q(object_label__icontains=search)
        )

    action = request.GET.get('action', '').strip()
    if action in dict(AuditLog.ACTION_CHOICES):
        entries = entries.filter(action=action)

    page = Paginator(entries, 50).get_page(request.GET.get('page', 1))
    return render(request, 'hr/audit_log.html', {
        'audit_page': page,
        'search': search,
        'selected_action': action,
        'action_choices': AuditLog.ACTION_CHOICES,
        'is_superadmin': is_superadmin,
    })


@login_required
def hr_search_completed_appraisals(request):
    if request.user.profile.role != 'HR':
        return JsonResponse({'error': 'Forbidden'}, status=403)

    query = request.GET.get('q', '').strip()[:100]
    department_id = request.GET.get('department', '').strip()
    cycle_year = request.GET.get('cycle_year', '').strip()
    appraisals = Appraisal.objects.filter(status='completed').select_related(
        'employee__user',
        'employee__department',
        'supervisor__user',
        'hod__user',
        'cycle',
    )

    if department_id:
        try:
            department_id = int(department_id)
        except ValueError:
            return JsonResponse({'error': 'Invalid department filter.'}, status=400)
        department = get_object_or_404(Department, pk=department_id)
        appraisals = appraisals.filter(employee_department_id=department_id)

    if cycle_year:
        try:
            cycle_year = int(cycle_year)
        except ValueError:
            return JsonResponse({'error': 'Invalid cycle year filter.'}, status=400)
        appraisals = appraisals.filter(cycle__year=cycle_year)

    if query:
        appraisals = appraisals.filter(
            Q(employee__user__username__icontains=query)
            | Q(employee__user__first_name__icontains=query)
            | Q(employee__user__last_name__icontains=query)
            | Q(employee__user__email__icontains=query)
            | Q(employee_department__name__icontains=query)
            | Q(employee_department_name__icontains=query)
            | Q(supervisor__user__username__icontains=query)
            | Q(supervisor__user__first_name__icontains=query)
            | Q(supervisor__user__last_name__icontains=query)
            | Q(hod__user__username__icontains=query)
            | Q(hod__user__first_name__icontains=query)
            | Q(hod__user__last_name__icontains=query)
            | Q(cycle__year__icontains=query)
            | Q(final_rating__icontains=query)
        )

    paginator = Paginator(
        appraisals.order_by('-cycle__year', 'employee__user__username'),
        25,
    )
    page = paginator.get_page(request.GET.get('page', 1))
    results = []
    for appraisal in page.object_list:
        results.append({
            'id': appraisal.id,
            'employee_staff_id': appraisal.employee.user.username,
            'employee_name': appraisal.employee.user.get_full_name()
            or appraisal.employee.user.username,
            'department': appraisal.employee_department_name
            or (appraisal.employee_department.name if appraisal.employee_department else '—'),
            'supervisor': appraisal.supervisor.user.username
            if appraisal.supervisor else '—',
            'hod': appraisal.hod.user.username if appraisal.hod else '—',
            'cycle_year': appraisal.cycle.year,
            'overall_percentage': str(appraisal.overall_percentage)
            if appraisal.overall_percentage is not None else '—',
            'final_rating': appraisal.final_rating or '—',
            'view_url': reverse('hr_completed_appraisal', args=[appraisal.id]),
        })

    return JsonResponse({
        'results': results,
        'page': page.number,
        'pages': paginator.num_pages,
        'total': paginator.count,
        'has_previous': page.has_previous(),
        'has_next': page.has_next(),
    })


@login_required
def hr_completed_appraisal(request, appraisal_id):
    if request.user.profile.role != 'HR':
        messages.error(request, "Access denied. You are not an HR Manager.")
        return redirect('dashboard_router')

    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        pk=appraisal_id,
        status='completed',
    )
    score_context = _score_breakdown_context(appraisal)
    return render(request, 'hr/completed_appraisal.html', {
        'appraisal': appraisal,
        'targets': score_context['score_breakdown']['targets'],
        **score_context,
        'trainings': appraisal.trainings.all(),
        'return_history': appraisal.returns.select_related('reviewer'),
    })


@login_required
def employee_appraisal_detail(request, appraisal_id):
    """Read-only view for an employee to inspect a submitted, past, or completed appraisal."""
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'employee_department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        pk=appraisal_id,
        employee=request.user.profile,
    )
    return _render_read_only_appraisal(request, appraisal, allow_pdf=True)


@login_required
def employee_appraisal_pdf(request, appraisal_id):
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        pk=appraisal_id,
        employee=request.user.profile,
        status='completed',
    ) 

    score_context = _score_breakdown_context(appraisal)
    return render(request, 'employee/fill_targets.html', {
        'appraisal': appraisal,
        'read_only': True,
        'print_mode': True,
        'trainings': appraisal.trainings.all(),
        'targets': score_context['score_breakdown']['targets'],
        **score_context,
        'return_history': appraisal.returns.select_related('reviewer'),
        'assigned_supervisor': appraisal.supervisor,
    })


@login_required
def employee_submission_slip(request, appraisal_id):
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ).prefetch_related(
            'targets',
            'trainings',
            'competency_scores__competency',
        ),
        pk=appraisal_id,
        employee=request.user.profile,
    )
    if not appraisal.reference_number or not appraisal.employee_signed_at:
        messages.error(request, 'A submission slip is available after your appraisal has been submitted.')
        return redirect('employee_dashboard')

    return render(request, 'employee/fill_targets.html', {
        'appraisal': appraisal,
        'read_only': True,
        'print_mode': True,
        'auto_print': True,
        'back_to_dashboard': True,
        'trainings': appraisal.trainings.all(),
        'targets': appraisal.targets.all(),
        'competencies': appraisal.competency_scores.select_related(
            'competency',
        ).prefetch_related('statement_scores'),
        **_score_breakdown_context(appraisal),
        'return_history': appraisal.returns.select_related('reviewer'),
        'assigned_supervisor': appraisal.supervisor,
    })


@login_required
def hr_supervisor_assignments(request):
    profile = request.user.profile
    if profile.role != 'HR':
        messages.error(request, "Access denied. Only HR can manage supervisor assignments.")
        return redirect('dashboard_router')

    if request.method == 'POST':
        if request.POST.get('reporting_action') == 'save':
            reporting_form, transfer_confirmation, saved_assignment = (
                _process_hr_supervisor_reporting(request)
            )
            form = HRSupervisorAssignmentForm()
        else:
            form, transfer_confirmation, saved_assignment = (
                _process_hr_supervisor_assignment(request)
            )
            reporting_form = SupervisorReportingForm(prefix='reporting')
        if transfer_confirmation:
            return render(request, 'hr/supervisor_assignments.html', {
                'form': form,
                'reporting_form': reporting_form,
                'reporting_assignments': SupervisorReporting.objects.select_related(
                    'supervisor__user',
                    'supervisor__department',
                    'manager__user',
                ).order_by('supervisor__user__username'),
                'departments': Department.objects.order_by('name'),
                'transfer_confirmation': transfer_confirmation,
            })
        if saved_assignment:
            employee_username, supervisor_username, transferred_count = saved_assignment
            messages.success(
                request,
                f"Reporting assignment from {employee_username} to {supervisor_username} was saved.",
            )
            if transferred_count:
                messages.success(
                    request,
                    f"Transferred {transferred_count} in-progress appraisal(s). "
                    "Existing scores, comments, and return history were preserved.",
                )
            return redirect('hr_supervisor_assignments')
        return render(request, 'hr/supervisor_assignments.html', {
            'form': form,
            'reporting_form': reporting_form,
            'reporting_assignments': SupervisorReporting.objects.select_related(
                'supervisor__user',
                'supervisor__department',
                'manager__user',
            ).order_by('supervisor__user__username'),
            'departments': Department.objects.order_by('name'),
        })
    else:
        initial = {}
        department_id = request.GET.get('department')
        if department_id:
            assignment = DepartmentSupervisor.objects.filter(
                department_id=department_id,
            ).select_related('department', 'supervisor').first()
            if assignment:
                initial = {
                    'department': assignment.department_id,
                    'supervisor': assignment.supervisor_id,
                }
        form = HRSupervisorAssignmentForm(initial=initial)

    return render(request, 'hr/supervisor_assignments.html', {
        'form': form,
        'reporting_form': SupervisorReportingForm(prefix='reporting'),
        'reporting_assignments': SupervisorReporting.objects.select_related(
            'supervisor__user',
            'supervisor__department',
            'manager__user',
        ).order_by('supervisor__user__username'),
        'departments': Department.objects.order_by('name'),
    })


@login_required
def hr_search_supervisor_assignments(request):
    if request.user.profile.role != 'HR':
        return JsonResponse({'error': 'Forbidden'}, status=403)

    query = request.GET.get('q', '').strip()[:100]
    department_id = request.GET.get('department', '').strip()
    assignments = DepartmentSupervisor.objects.select_related(
        'department',
        'supervisor__user',
    )

    if department_id:
        try:
            department_id = int(department_id)
        except ValueError:
            return JsonResponse({'error': 'Invalid department filter.'}, status=400)
        assignments = assignments.filter(department_id=department_id)

    if query:
        assignments = assignments.filter(
            Q(supervisor__user__username__icontains=query)
            | Q(supervisor__user__first_name__icontains=query)
            | Q(supervisor__user__last_name__icontains=query)
            | Q(department__name__icontains=query)
        )

    paginator = Paginator(
        assignments.order_by('department__name'),
        25,
    )
    page = paginator.get_page(request.GET.get('page', 1))
    results = []
    for assignment in page.object_list:
        supervisor = assignment.supervisor
        results.append({
            'department': assignment.department.name,
            'supervisor_staff_id': supervisor.user.username,
            'supervisor_name': supervisor.user.get_full_name() or supervisor.user.username,
            'department_id': assignment.department_id,
            'supervisor_id': supervisor.id,
            'edit_url': (
                f"{reverse('hr_supervisor_assignments')}?"
                f"department={assignment.department_id}"
            ),
        })

    return JsonResponse({
        'results': results,
        'page': page.number,
        'pages': paginator.num_pages,
        'total': paginator.count,
        'has_previous': page.has_previous(),
        'has_next': page.has_next(),
    })


@login_required
def hr_assignment_options(request):
    if request.user.profile.role != 'HR':
        return JsonResponse({'error': 'Forbidden'}, status=403)

    department_id = request.GET.get('department_id')
    try:
        department_id = int(department_id)
    except (TypeError, ValueError):
        return JsonResponse({'error': 'Select a valid department.'}, status=400)

    if not Department.objects.filter(pk=department_id).exists():
        return JsonResponse({'error': 'Select a valid department.'}, status=400)

    supervisors = EmployeeProfile.objects.filter(
        department_id=department_id,
    ).exclude(role='HR').select_related('user').order_by('user__username')

    current_supervisor_id = DepartmentSupervisor.objects.filter(
        department_id=department_id,
    ).values_list('supervisor_id', flat=True).first()

    return JsonResponse({
        'supervisors': [
            {
                'id': supervisor.id,
                'label': f"{supervisor.user.username} - "
                f"{supervisor.user.get_full_name() or supervisor.user.username}",
            }
            for supervisor in supervisors
        ],
        'current_supervisor_id': current_supervisor_id,
    })


@login_required
def hr_approve(request, appraisal_id):
    """Allows HR to view the full appraisal, add comments, and lock it forever."""
    profile = request.user.profile
    appraisal = get_object_or_404(
        Appraisal.objects.select_related(
            'employee__user',
            'employee__department',
            'employee_department',
            'supervisor__user',
            'hod__user',
            'cycle',
        ),
        id=appraisal_id,
    )

    if profile.role != 'HR' or appraisal.status != 'pending_hr':
        messages.warning(request, "This appraisal is not pending HR approval.")
        return redirect('hr_dashboard')

    is_hod_appraisal = appraisal.employee.role == 'HOD'
    statement_scores = AppraisalCompetencyStatementScore.objects.filter(
        competency_score__appraisal=appraisal,
    ).select_related('competency_score__competency')

    if request.method == 'POST':
        if appraisal.return_requests.filter(status='pending').exists():
            messages.error(
                request,
                "Decide the pending return request before changing this appraisal.",
            )
            return redirect('hr_approve', appraisal_id=appraisal.id)
        if is_hod_appraisal and request.POST.get('action') == 'save_hod_scores':
            target_formset = HODAppraisalTargetScoringFormSet(
                request.POST,
                instance=appraisal,
                prefix='hod_targets',
            )
            statement_score_formset = SupervisorCompetencyStatementFormSet(
                request.POST,
                queryset=statement_scores,
                prefix='hod_statement_scores',
            )
            if target_formset.is_valid() and statement_score_formset.is_valid():
                with transaction.atomic():
                    target_formset.save()
                    statement_score_formset.save()
                messages.success(request, "HOD appraisal scores saved.")
                return redirect('hr_approve', appraisal_id=appraisal.id)
            messages.error(request, "Scores were not saved. Correct the highlighted errors and try again.")
            form = HRCommentForm(instance=appraisal)
        else:
            target_formset = HODAppraisalTargetScoringFormSet(
                instance=appraisal,
                prefix='hod_targets',
            ) if is_hod_appraisal else None
            statement_score_formset = SupervisorCompetencyStatementFormSet(
                queryset=statement_scores,
                prefix='hod_statement_scores',
            ) if is_hod_appraisal else None
            form = HRCommentForm(request.POST, instance=appraisal)
            if form.is_valid():
                appraisal = form.save(commit=False)
                appraisal.status = 'completed'
                appraisal.hr_approved_at = timezone.now()

                try:
                    appraisal.calculate_final_score()
                except ValidationError as error:
                    messages.error(request, error.messages[0])
                    return redirect('hr_approve', appraisal_id=appraisal.id)

                appraisal.save()
                notify_users(
                    [appraisal.employee.user],
                    title='Appraisal completed',
                    message=f"Your {appraisal.cycle.year} appraisal has been approved and completed.",
                    url=reverse('employee_dashboard'),
                )
                messages.success(request, "Appraisal officially approved, scored, and locked.")
                return redirect('hr_dashboard')
    else:
        form = HRCommentForm(instance=appraisal)
        target_formset = HODAppraisalTargetScoringFormSet(
            instance=appraisal,
            prefix='hod_targets',
        ) if is_hod_appraisal else None
        statement_score_formset = SupervisorCompetencyStatementFormSet(
            queryset=statement_scores,
            prefix='hod_statement_scores',
        ) if is_hod_appraisal else None

    statement_forms_by_competency_score = {}
    if statement_score_formset is not None:
        for statement_form in statement_score_formset.forms:
            statement_forms_by_competency_score.setdefault(
                statement_form.instance.competency_score_id,
                [],
            ).append(statement_form)
    score_context = _score_breakdown_context(appraisal)
    competency_scores = sorted(
        (
            group['score']
            for group in score_context['score_breakdown']['groups']
        ),
        key=lambda score: score.competency.name,
    )
    competency_groups = [
        {
            'score': competency_score,
            'statements': statement_forms_by_competency_score.get(
                competency_score.pk,
                [],
            ),
        }
        for competency_score in competency_scores
    ]

    targets = score_context['score_breakdown']['targets']

    return render(request, 'hr/review.html', {
        'appraisal': appraisal,
        'form': form,
        'is_hod_appraisal': is_hod_appraisal,
        'target_formset': target_formset,
        'statement_score_formset': statement_score_formset,
        'competency_groups': competency_groups,
        'targets': targets,
        **score_context,
        'trainings': appraisal.trainings.all(),
        'return_form': AppraisalReturnForm(),
        'return_history': appraisal.returns.select_related('reviewer'),
        **_return_request_context_for_user(appraisal, profile),
    })

@login_required
def newcycle(request):
    if request.user.profile.role != 'HR':
        messages.error(request, "Access denied. Only HR can create appraisal cycles.")
        return redirect('dashboard_router')

    form = AppraisalCycleForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            cycle = form.save(commit=False)
            if cycle.is_active:
                for existing_cycle in AppraisalCycle.objects.exclude(pk=cycle.pk):
                    if existing_cycle.is_active:
                        existing_cycle.is_active = False
                        existing_cycle.save(update_fields=['is_active'])
            cycle.save()
        messages.success(request, f"Appraisal cycle {cycle.year} was created successfully.")
        return redirect('new_app_cycle')

    return render(request, 'hr/new_cycle.html', {
        'form': form,
        'cycles': AppraisalCycle.objects.annotate(
            appraisal_count=Count('appraisals'),
        ),
    })


@login_required
def set_appraisal_cycle_status(request, cycle_id):
    if request.user.profile.role != 'HR':
        messages.error(request, "Access denied. Only HR can manage appraisal cycles.")
        return redirect('dashboard_router')
    if request.method != 'POST':
        messages.error(request, "Use the cycle controls to change its status.")
        return redirect(f"{reverse('hr_dashboard')}#cycles")

    cycle = get_object_or_404(AppraisalCycle, pk=cycle_id)
    activate = request.POST.get('is_active') == 'true'
    with transaction.atomic():
        if activate:
            for existing_cycle in AppraisalCycle.objects.exclude(pk=cycle.pk):
                if existing_cycle.is_active:
                    existing_cycle.is_active = False
                    existing_cycle.save(update_fields=['is_active'])
        cycle.is_active = activate
        cycle.save(update_fields=['is_active'])

    if activate:
        messages.success(request, f"Appraisal cycle {cycle.year} is now active.")
    else:
        messages.success(request, f"Appraisal cycle {cycle.year} is now inactive.")
    return redirect(f"{reverse('hr_dashboard')}#cycles")


@login_required
def delete_appraisal_cycle(request, cycle_id):
    if request.user.profile.role != 'HR':
        messages.error(request, "Access denied. Only HR can manage appraisal cycles.")
        return redirect('dashboard_router')
    if request.method != 'POST':
        messages.error(request, "Use the cycle controls to delete a cycle.")
        return redirect(f"{reverse('hr_dashboard')}#cycles")

    cycle = get_object_or_404(AppraisalCycle, pk=cycle_id)
    if cycle.appraisals.exists():
        messages.error(
            request,
            f"Cycle {cycle.year} cannot be deleted because it has appraisal records. "
            "Deactivate it instead to preserve those records.",
        )
        return redirect(f"{reverse('hr_dashboard')}#cycles")

    year = cycle.year
    cycle.delete()
    messages.success(request, f"Appraisal cycle {year} was deleted.")
    return redirect(f"{reverse('hr_dashboard')}#cycles")


def changer_user_role(request):
    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        new_role = request.POST.get('new_role')

        if not user_id or not new_role:
            return JsonResponse({'error': 'Missing user_id or new_role.'}, status=400)

        try:
            user_profile = EmployeeProfile.objects.get(pk=user_id)
        except EmployeeProfile.DoesNotExist:
            return JsonResponse({'error': 'User not found.'}, status=404)

        if new_role not in dict(EmployeeProfile.ROLE_CHOICES):
            return JsonResponse({'error': 'Invalid role.'}, status=400)

        user_profile.role = new_role
        user_profile.save()

        return JsonResponse({'success': True, 'new_role': new_role})

    return JsonResponse({'error': 'Invalid request method.'}, status=405) 
