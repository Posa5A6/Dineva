from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .access import is_owner, is_superadmin


def superadmin_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("dineva:login")
        if not is_superadmin(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped


def owner_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("dineva:login")
        if not is_owner(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped
