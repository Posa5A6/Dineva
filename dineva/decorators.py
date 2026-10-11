from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .access import is_kitchen_staff, is_owner, is_superadmin, is_waiter
from .impersonation import is_impersonating


def superadmin_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("dineva:login")
        if is_impersonating(request):
            raise PermissionDenied
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


def waiter_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("dineva:login")
        if not is_waiter(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped


def kitchen_required(view_func):
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("dineva:login")
        if not is_kitchen_staff(request.user):
            raise PermissionDenied
        return view_func(request, *args, **kwargs)

    return wrapped
