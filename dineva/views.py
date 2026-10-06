from django.contrib import messages
from django.contrib.auth import login, logout
from django.db import IntegrityError
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .decorators import is_superadmin, superadmin_required
from .forms import (
    DinevaAuthenticationForm,
    OwnerCreateForm,
    RestaurantForm,
    StaffCreateForm,
)
from .models import Restaurant, User
from .services import AccountSetupEmailError, create_restaurant_user


def home(request):
    return render(request, "dineva/home.html")


@never_cache
def account_setup_success(request):
    return render(request, "dineva/account_setup_success.html")


@never_cache
def login_view(request):
    if request.user.is_authenticated:
        return _post_login_redirect(request.user)

    form = DinevaAuthenticationForm(request=request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return _post_login_redirect(form.get_user())

    return render(request, "dineva/login.html", {"form": form})


@never_cache
@superadmin_required
def dashboard(request):
    return render(request, "dineva/dashboard.html", _dashboard_context())


@never_cache
@superadmin_required
def restaurant_list(request):
    return render(request, "dineva/dashboard.html", _dashboard_context())


@never_cache
@superadmin_required
def restaurant_add(request):
    form = RestaurantForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Restaurant created successfully.")
        return redirect("dineva:restaurants")

    return render(request, "dineva/restaurant_form.html", {"form": form})


@never_cache
@superadmin_required
def owner_add(request):
    form = OwnerCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            create_restaurant_user(
                request=request,
                username=form.cleaned_data["username"],
                name=form.cleaned_data["name"],
                email=form.cleaned_data["email"],
                restaurant=form.cleaned_data["restaurant"],
                role=User.Role.OWNER,
            )
        except IntegrityError:
            form.add_error(
                None,
                "A user with this username or email already exists.",
            )
        except AccountSetupEmailError as exc:
            messages.warning(
                request,
                f"Owner created, but the {exc.stage} email could not be sent. "
                "The account can be recovered through a future resend workflow.",
            )
            return redirect("dineva:dashboard")
        else:
            messages.success(request, "Owner created and setup email sent.")
            return redirect("dineva:dashboard")

    return render(request, "dineva/owner_form.html", {"form": form})


@never_cache
@superadmin_required
def staff_add(request):
    form = StaffCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            create_restaurant_user(
                request=request,
                username=form.cleaned_data["username"],
                name=form.cleaned_data["name"],
                email=form.cleaned_data["email"],
                restaurant=form.cleaned_data["restaurant"],
                role=form.cleaned_data["staff_type"],
            )
        except IntegrityError:
            form.add_error(
                None,
                "A user with this username or email already exists.",
            )
        except AccountSetupEmailError as exc:
            messages.warning(
                request,
                f"Staff member created, but the {exc.stage} email could not be sent. "
                "The account can be recovered through a future resend workflow.",
            )
            return redirect("dineva:dashboard")
        else:
            messages.success(request, "Staff member created and setup email sent.")
            return redirect("dineva:dashboard")

    return render(request, "dineva/staff_form.html", {"form": form})


def _dashboard_context():
    restaurants = Restaurant.objects.annotate(
        owner_count=Count(
            "users",
            filter=Q(users__role=User.Role.OWNER),
            distinct=True,
        ),
        staff_count=Count(
            "users",
            filter=Q(
                users__role__in=[User.Role.WAITER, User.Role.KITCHEN_STAFF]
            ),
            distinct=True,
        ),
    ).order_by("name")

    return {
        "restaurants": restaurants,
        "restaurant_count": Restaurant.objects.count(),
        "owner_count": User.objects.filter(role=User.Role.OWNER).count(),
        "waiter_count": User.objects.filter(role=User.Role.WAITER).count(),
        "kitchen_count": User.objects.filter(role=User.Role.KITCHEN_STAFF).count(),
    }


def _post_login_redirect(user):
    if is_superadmin(user):
        return redirect("dineva:dashboard")
    return redirect("dineva:home")


@never_cache
@require_POST
def logout_view(request):
    logout(request)
    return redirect("dineva:login")
