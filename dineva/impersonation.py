import hashlib
import logging

from django.conf import settings
from django.contrib.auth import login, logout
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_ipv46_address
from django.db import DatabaseError, transaction
from django.utils import timezone

from .access import account_can_authenticate, get_profile, is_superadmin
from .models import ImpersonationAudit, User, UserProfile


logger = logging.getLogger(__name__)

SESSION_AUDIT_ID = "dineva_impersonation_audit_id"
SESSION_ORIGINAL_USER_ID = "dineva_impersonation_original_user_id"
SESSION_ORIGINAL_USER_NAME = "dineva_impersonation_original_user_name"
SESSION_TARGET_USER_ID = "dineva_impersonation_target_user_id"


def is_impersonating(request):
    return bool(request.session.get(SESSION_AUDIT_ID))


def _hash_session_key(session_key):
    if not session_key:
        return ""
    return hashlib.sha256(
        f"{settings.SECRET_KEY}|{session_key}".encode("utf-8")
    ).hexdigest()


def _client_ip(request):
    value = request.META.get("REMOTE_ADDR") or ""
    try:
        validate_ipv46_address(value)
    except ValidationError:
        return None
    return value


def _user_agent(request):
    return (request.META.get("HTTP_USER_AGENT") or "")[:512]


def _auth_backend():
    return settings.AUTHENTICATION_BACKENDS[0]


def _session_int(request, key):
    value = request.session.get(key)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clear_impersonation_session(request):
    for key in (
        SESSION_AUDIT_ID,
        SESSION_ORIGINAL_USER_ID,
        SESSION_ORIGINAL_USER_NAME,
        SESSION_TARGET_USER_ID,
    ):
        request.session.pop(key, None)
    request.session.modified = True


def validate_impersonation_target(target_user):
    profile = get_profile(target_user)
    if profile is None:
        raise ValidationError("The selected account does not have a valid profile.")
    if profile.role == UserProfile.Role.SUPERADMIN:
        raise ValidationError("Super Admin accounts cannot be impersonated.")
    if profile.role not in {
        UserProfile.Role.OWNER,
        UserProfile.Role.WAITER,
        UserProfile.Role.KITCHEN_STAFF,
    }:
        raise ValidationError("The selected account is not an impersonation target.")
    if profile.restaurant_id is None:
        raise ValidationError("The selected account is not associated with a restaurant.")
    if not account_can_authenticate(target_user):
        raise ValidationError("The selected account is not currently eligible to sign in.")
    return profile


def _allowed_target_roles():
    return {
        UserProfile.Role.OWNER,
        UserProfile.Role.WAITER,
        UserProfile.Role.KITCHEN_STAFF,
    }


def start_impersonation(request, *, target_user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError("A reason is required.")
    if is_impersonating(request):
        raise PermissionDenied("Nested impersonation is not allowed.")

    original_user = User.objects.select_related("profile").get(pk=request.user.pk)
    if not is_superadmin(original_user) or not account_can_authenticate(original_user):
        raise PermissionDenied("Only an active platform Super Admin can impersonate users.")

    target_user = User.objects.select_related("profile", "profile__restaurant").get(
        pk=target_user.pk
    )
    target_profile = validate_impersonation_target(target_user)

    audit = None
    try:
        login(request, target_user, backend=_auth_backend())
        request.session.save()
        audit = ImpersonationAudit.objects.create(
            superadmin=original_user,
            target_user=target_user,
            target_role=target_profile.role,
            target_restaurant=target_profile.restaurant,
            reason=reason,
            session_key_hash=_hash_session_key(request.session.session_key),
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
        request.session[SESSION_AUDIT_ID] = audit.pk
        request.session[SESSION_ORIGINAL_USER_ID] = original_user.pk
        request.session[SESSION_ORIGINAL_USER_NAME] = (
            original_user.name or original_user.username
        )
        request.session[SESSION_TARGET_USER_ID] = target_user.pk
        request.session.save()
    except Exception:
        if audit is not None:
            try:
                _end_audit(audit, ImpersonationAudit.EndReason.SESSION_INIT_FAILED)
            except DatabaseError:
                logger.exception("Failed to record impersonation session initialization failure.")
        logout(request)
        raise

    return audit


def _audit_queryset(*, lock=False):
    queryset = ImpersonationAudit.objects.select_related(
        "superadmin",
        "superadmin__profile",
        "target_user",
        "target_user__profile",
        "target_restaurant",
    )
    if lock:
        queryset = queryset.select_for_update()
    return queryset


def _session_audit_identity(request):
    audit_id = _session_int(request, SESSION_AUDIT_ID)
    original_user_id = _session_int(request, SESSION_ORIGINAL_USER_ID)
    target_user_id = _session_int(request, SESSION_TARGET_USER_ID)
    if not audit_id or not original_user_id or not target_user_id:
        return None, None, None
    return audit_id, original_user_id, target_user_id


def _session_hash_matches(request, audit):
    if not audit.session_key_hash:
        return False
    return audit.session_key_hash == _hash_session_key(request.session.session_key)


def _current_state_end_reason(audit):
    if not is_superadmin(audit.superadmin) or not account_can_authenticate(
        audit.superadmin
    ):
        return ImpersonationAudit.EndReason.RESTORE_FAILED

    target_profile = get_profile(audit.target_user)
    if target_profile is None:
        return ImpersonationAudit.EndReason.TARGET_INELIGIBLE
    if (
        target_profile.role != audit.target_role
        or target_profile.restaurant_id != audit.target_restaurant_id
    ):
        return ImpersonationAudit.EndReason.INVALID_STATE
    if target_profile.role not in _allowed_target_roles():
        return ImpersonationAudit.EndReason.TARGET_INELIGIBLE
    try:
        validate_impersonation_target(audit.target_user)
    except ValidationError:
        return ImpersonationAudit.EndReason.TARGET_INELIGIBLE
    return None


def _get_active_audit_from_session(request, *, lock=False):
    audit_id, original_user_id, target_user_id = _session_audit_identity(request)
    if not audit_id:
        return None

    try:
        audit = _audit_queryset(lock=lock).get(pk=audit_id)
    except ImpersonationAudit.DoesNotExist:
        return None

    if (
        audit.superadmin_id != original_user_id
        or audit.target_user_id != target_user_id
        or audit.ended_at is not None
        or not _session_hash_matches(request, audit)
    ):
        return None
    if request.user.is_authenticated and request.user.pk != target_user_id:
        return None
    if _current_state_end_reason(audit) is not None:
        return None
    return audit


def _end_safely_identified_audit_from_session(request, reason):
    audit_id, original_user_id, target_user_id = _session_audit_identity(request)
    if not audit_id:
        return False

    try:
        audit = _audit_queryset(lock=True).get(pk=audit_id)
    except ImpersonationAudit.DoesNotExist:
        return False

    if (
        audit.superadmin_id != original_user_id
        or audit.target_user_id != target_user_id
        or audit.ended_at is not None
        or not _session_hash_matches(request, audit)
    ):
        return False

    if request.user.is_authenticated and request.user.pk != audit.target_user_id:
        return False

    _end_audit(audit, _current_state_end_reason(audit) or reason)
    return True


def _end_known_audit(audit_id, *, original_user_id, target_user_id, reason):
    audit = _audit_queryset(lock=True).get(pk=audit_id)
    if (
        audit.superadmin_id != original_user_id
        or audit.target_user_id != target_user_id
        or audit.ended_at is not None
    ):
        return False
    _end_audit(audit, reason)
    return True


def validate_impersonation_state(request):
    if not is_impersonating(request):
        return True
    try:
        audit = _get_active_audit_from_session(request)
    except DatabaseError:
        logger.exception("Failed to validate impersonation audit state.")
        _clear_impersonation_session(request)
        return False
    if audit is None:
        close_impersonation_without_restore(
            request,
            ImpersonationAudit.EndReason.INVALID_STATE,
        )
        return False
    return True


def _end_audit(audit, reason):
    if audit.ended_at is not None:
        return audit
    audit.ended_at = timezone.now()
    audit.end_reason = reason
    audit.save(update_fields=["ended_at", "end_reason", "updated_at"])
    return audit


def close_impersonation_without_restore(request, reason):
    if not is_impersonating(request):
        return False
    try:
        with transaction.atomic():
            audit = _get_active_audit_from_session(request, lock=True)
            if audit is not None:
                _end_audit(audit, reason)
            else:
                _end_safely_identified_audit_from_session(request, reason)
        return True
    except DatabaseError:
        logger.exception("Failed to close impersonation audit.")
        return False
    finally:
        _clear_impersonation_session(request)


def stop_impersonation(request):
    if not is_impersonating(request):
        return False

    try:
        with transaction.atomic():
            audit = _get_active_audit_from_session(request, lock=True)
            if audit is None:
                close_impersonation_without_restore(
                    request,
                    ImpersonationAudit.EndReason.INVALID_STATE,
                )
                logout(request)
                return False
            audit_id = audit.pk
            original_user_id = audit.superadmin_id
            target_user_id = audit.target_user_id
    except DatabaseError:
        logger.exception("Failed to read impersonation audit for restoration.")
        _clear_impersonation_session(request)
        logout(request)
        return False

    try:
        original_user = User.objects.select_related("profile").get(pk=original_user_id)
    except User.DoesNotExist:
        try:
            with transaction.atomic():
                _end_known_audit(
                    audit_id,
                    original_user_id=original_user_id,
                    target_user_id=target_user_id,
                    reason=ImpersonationAudit.EndReason.RESTORE_FAILED,
                )
        except DatabaseError:
            logger.exception("Failed to record impersonation restore failure.")
        _clear_impersonation_session(request)
        logout(request)
        return False

    if not is_superadmin(original_user) or not account_can_authenticate(original_user):
        try:
            with transaction.atomic():
                _end_known_audit(
                    audit_id,
                    original_user_id=original_user_id,
                    target_user_id=target_user_id,
                    reason=ImpersonationAudit.EndReason.RESTORE_FAILED,
                )
        except DatabaseError:
            logger.exception("Failed to record impersonation restore failure.")
        _clear_impersonation_session(request)
        logout(request)
        return False

    try:
        login(request, original_user, backend=_auth_backend())
    except Exception:
        logger.exception("Failed to restore Super Admin impersonation session.")
        try:
            with transaction.atomic():
                _end_known_audit(
                    audit_id,
                    original_user_id=original_user_id,
                    target_user_id=target_user_id,
                    reason=ImpersonationAudit.EndReason.RESTORE_FAILED,
                )
        except DatabaseError:
            logger.exception("Failed to record impersonation restore failure.")
        logout(request)
        return False

    try:
        with transaction.atomic():
            stopped = _end_known_audit(
                audit_id,
                original_user_id=original_user_id,
                target_user_id=target_user_id,
                reason=ImpersonationAudit.EndReason.STOPPED,
            )
    except DatabaseError:
        logger.exception("Failed to record impersonation stop.")
        logout(request)
        return False

    if not stopped:
        logout(request)
        return False
    return True
