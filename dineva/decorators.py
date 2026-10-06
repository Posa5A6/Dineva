from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .models import User


def is_superadmin(user):
    return (
        user.is_authenticated
        and user.is_active
        and user.role == User.Role.SUPERADMIN
        and user.is_superuser
    )


def superadmin_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("dineva:login")
        if not is_superadmin(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped
