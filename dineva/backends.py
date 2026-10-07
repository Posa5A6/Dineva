from django.contrib.auth.backends import ModelBackend

from .access import account_can_authenticate


class DinevaModelBackend(ModelBackend):
    def user_can_authenticate(self, user):
        return super().user_can_authenticate(user) and account_can_authenticate(user)
