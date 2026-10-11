from django.contrib.auth import logout

from .access import account_can_authenticate
from .impersonation import close_impersonation_without_restore, validate_impersonation_state
from .models import ImpersonationAudit


class AccountEligibilityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            if not validate_impersonation_state(request):
                logout(request)
            elif not account_can_authenticate(request.user):
                close_impersonation_without_restore(
                    request,
                    ImpersonationAudit.EndReason.TARGET_INELIGIBLE,
                )
                logout(request)
        return self.get_response(request)
