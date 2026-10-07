from django.contrib.auth import logout

from .access import account_can_authenticate


class AccountEligibilityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not account_can_authenticate(request.user):
            logout(request)
        return self.get_response(request)
