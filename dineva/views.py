import hashlib
import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .access import get_profile, is_owner, is_superadmin, owner_can_manage_profile
from .decorators import owner_required, superadmin_required
from .forms import (
    DinevaAuthenticationForm,
    EmployeeEditForm,
    OwnerCreateForm,
    OwnerStaffCreateForm,
    RestaurantForm,
    StaffCreateForm,
    StaffPhotoForm,
)
from .models import Restaurant, User, UserProfile
from .services import (
    DuplicateAccountError,
    RegistrationEmailError,
    create_employee,
    update_employee_profile,
    update_staff_photo,
)


def home(request):
    return render(request, "dineva/home.html")


def _login_throttle_key(request, username):
    source = f"{request.META.get('REMOTE_ADDR', '')}|{username.strip().lower()}"
    digest = hashlib.sha256(
        f"{settings.SECRET_KEY}|{source}".encode("utf-8")
    ).hexdigest()
    return f"dineva:login:{digest}"


@never_cache
def login_view(request):
    if request.user.is_authenticated:
        return _post_login_redirect(request.user)

    form = DinevaAuthenticationForm(request=request, data=request.POST or None)
    throttle_key = None
    if request.method == "POST":
        throttle_key = _login_throttle_key(request, request.POST.get("username", ""))
        attempts = cache.get(throttle_key, 0)
        if attempts >= 5:
            form.add_error(None, "Unable to sign in with the provided credentials.")
        elif form.is_valid():
            cache.delete(throttle_key)
            login(request, form.get_user())
            return _post_login_redirect(form.get_user())
        else:
            cache.set(throttle_key, attempts + 1, timeout=900)

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
    return render(request, "dineva/restaurant_form.html", {"form": form, "mode": "add"})


@never_cache
@superadmin_required
def restaurant_edit(request, restaurant_id):
    restaurant = get_object_or_404(Restaurant, pk=restaurant_id)
    form = RestaurantForm(
        request.POST or None,
        request.FILES or None,
        instance=restaurant,
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Restaurant updated successfully.")
        return redirect("dineva:restaurants")
    return render(request, "dineva/restaurant_form.html", {"form": form, "mode": "edit"})


@require_POST
@superadmin_required
def restaurant_status(request, restaurant_id):
    restaurant = get_object_or_404(Restaurant, pk=restaurant_id)
    restaurant.is_active = not restaurant.is_active
    restaurant.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Restaurant status updated.")
    return redirect("dineva:restaurants")


def _employee_create_response(request, form, *, role, restaurant, success_message, redirect_name):
    try:
        create_employee(
            name=form.cleaned_data["name"],
            email=form.cleaned_data["email"],
            contact=form.cleaned_data["contact"],
            address=form.cleaned_data["address"],
            photo=form.cleaned_data["photo"],
            id_proof_type=form.cleaned_data["id_proof_type"],
            id_proof_file=form.cleaned_data["id_proof_file"],
            restaurant=restaurant,
            role=role,
        )
    except (DuplicateAccountError, IntegrityError) as exc:
        form.add_error("email", str(exc))
    except RegistrationEmailError:
        form.add_error(None, "Registration failed because the Welcome Email was not accepted. No account was created.")
    except (ValidationError, Restaurant.DoesNotExist) as exc:
        form.add_error(None, exc)
    else:
        messages.success(request, success_message)
        return redirect(redirect_name)
    return None


@never_cache
@superadmin_required
def owner_add(request):
    form = OwnerCreateForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        response = _employee_create_response(
            request,
            form,
            role=UserProfile.Role.OWNER,
            restaurant=form.cleaned_data["restaurant"],
            success_message="Owner created and Welcome Email sent.",
            redirect_name="dineva:owner-list",
        )
        if response:
            return response
    return render(request, "dineva/employee_form.html", {"form": form, "title": "Add Owner"})


@never_cache
@superadmin_required
def staff_add(request):
    form = StaffCreateForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        response = _employee_create_response(
            request,
            form,
            role=form.cleaned_data["staff_type"],
            restaurant=form.cleaned_data["restaurant"],
            success_message="Staff member created and Welcome Email sent.",
            redirect_name="dineva:staff-list",
        )
        if response:
            return response
    return render(request, "dineva/employee_form.html", {"form": form, "title": "Add Staff"})


def _profile_queryset():
    return UserProfile.objects.select_related("user", "restaurant")


@never_cache
@superadmin_required
def owner_list(request):
    profiles = _profile_queryset().filter(role=UserProfile.Role.OWNER).order_by("user__name")
    return render(
        request,
        "dineva/employee_list.html",
        {"page_obj": Paginator(profiles, 25).get_page(request.GET.get("page")), "list_kind": "owners"},
    )


def _filtered_staff(profiles, request):
    status = request.GET.get("status", "all")
    role = request.GET.get("role", "all")
    if status == "active":
        profiles = profiles.filter(user__is_active=True)
    elif status == "inactive":
        profiles = profiles.filter(user__is_active=False)
    if role in {UserProfile.Role.WAITER, UserProfile.Role.KITCHEN_STAFF}:
        profiles = profiles.filter(role=role)
    return profiles, status, role


@never_cache
@superadmin_required
def staff_list(request):
    profiles, status, role = _filtered_staff(
        _profile_queryset().filter(
            role__in=[UserProfile.Role.WAITER, UserProfile.Role.KITCHEN_STAFF]
        ),
        request,
    )
    return render(
        request,
        "dineva/employee_list.html",
        {
            "page_obj": Paginator(profiles.order_by("user__name"), 25).get_page(request.GET.get("page")),
            "list_kind": "staff",
            "status_filter": status,
            "role_filter": role,
        },
    )


@never_cache
@superadmin_required
def employee_detail(request, user_id):
    profile = get_object_or_404(_profile_queryset(), user_id=user_id)
    if profile.role == UserProfile.Role.SUPERADMIN:
        raise PermissionDenied
    return render(request, "dineva/employee_detail.html", {"employee": profile, "can_view_proof": True})


@never_cache
@superadmin_required
def employee_edit(request, user_id):
    profile = get_object_or_404(
        _profile_queryset().select_related("id_proof"),
        user_id=user_id,
    )
    if profile.role == UserProfile.Role.SUPERADMIN:
        raise PermissionDenied
    form = EmployeeEditForm(request.POST or None, request.FILES or None, profile=profile)
    if request.method == "POST" and form.is_valid():
        update_employee_profile(profile=profile, data=form.cleaned_data)
        messages.success(request, "Employee details updated.")
        return redirect("dineva:employee-detail", user_id=profile.user_id)
    return render(request, "dineva/employee_form.html", {"form": form, "title": "Edit Employee"})


@require_POST
@superadmin_required
def employee_status(request, user_id):
    profile = get_object_or_404(_profile_queryset(), user_id=user_id)
    if profile.role == UserProfile.Role.SUPERADMIN:
        raise PermissionDenied
    profile.user.is_active = not profile.user.is_active
    profile.user.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Employee status updated.")
    return redirect("dineva:employee-detail", user_id=profile.user_id)


@never_cache
@owner_required
def owner_staff_list(request):
    owner_profile = request.user.profile
    profiles, status, role = _filtered_staff(
        _profile_queryset().filter(
            restaurant_id=owner_profile.restaurant_id,
            role__in=[UserProfile.Role.WAITER, UserProfile.Role.KITCHEN_STAFF],
        ),
        request,
    )
    return render(
        request,
        "dineva/employee_list.html",
        {
            "page_obj": Paginator(profiles.order_by("user__name"), 25).get_page(request.GET.get("page")),
            "list_kind": "owner_staff",
            "status_filter": status,
            "role_filter": role,
        },
    )


@never_cache
@owner_required
def owner_staff_add(request):
    form = OwnerStaffCreateForm(request.POST or None, request.FILES or None)
    if "restaurant" in request.POST:
        form.add_error(None, "Restaurant cannot be supplied for Owner-created staff.")
    if request.method == "POST" and form.is_valid():
        response = _employee_create_response(
            request,
            form,
            role=form.cleaned_data["staff_type"],
            restaurant=request.user.profile.restaurant,
            success_message="Staff member created and Welcome Email sent.",
            redirect_name="dineva:owner-staff-list",
        )
        if response:
            return response
    return render(request, "dineva/employee_form.html", {"form": form, "title": "Add Staff"})


def _owner_staff_or_404(owner, user_id):
    profile = get_object_or_404(
        _profile_queryset(),
        user_id=user_id,
        restaurant_id=owner.profile.restaurant_id,
        role__in=[UserProfile.Role.WAITER, UserProfile.Role.KITCHEN_STAFF],
    )
    if not owner_can_manage_profile(owner, profile):
        raise PermissionDenied
    return profile


@never_cache
@owner_required
def owner_staff_detail(request, user_id):
    profile = _owner_staff_or_404(request.user, user_id)
    return render(request, "dineva/employee_detail.html", {"employee": profile, "can_view_proof": False})


@never_cache
@owner_required
def owner_staff_photo(request, user_id):
    profile = _owner_staff_or_404(request.user, user_id)
    form = StaffPhotoForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        update_staff_photo(profile=profile, photo=form.cleaned_data["photo"])
        messages.success(request, "Staff photo updated.")
        return redirect("dineva:owner-staff-detail", user_id=user_id)
    return render(request, "dineva/employee_form.html", {"form": form, "title": "Update Staff Photo"})


@require_POST
@owner_required
def owner_staff_status(request, user_id):
    profile = _owner_staff_or_404(request.user, user_id)
    profile.user.is_active = not profile.user.is_active
    profile.user.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Staff status updated.")
    return redirect("dineva:owner-staff-detail", user_id=user_id)


@never_cache
def employee_photo(request, user_id):
    if not request.user.is_authenticated:
        return redirect("dineva:login")
    profile = get_object_or_404(_profile_queryset(), user_id=user_id)
    allowed = is_superadmin(request.user) or request.user.pk == profile.user_id
    if is_owner(request.user) and owner_can_manage_profile(request.user, profile):
        allowed = True
    if not allowed:
        raise PermissionDenied
    profile.photo.open("rb")
    content_type = mimetypes.guess_type(profile.photo.name)[0] or "application/octet-stream"
    response = FileResponse(profile.photo.file, content_type=content_type)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@superadmin_required
def id_proof_download(request, user_id):
    profile = get_object_or_404(
        _profile_queryset().select_related("id_proof"),
        user_id=user_id,
    )
    proof = profile.id_proof
    proof.file.open("rb")
    extension = Path(proof.file.name).suffix.lower()
    response = FileResponse(
        proof.file.file,
        content_type=proof.mime_type,
        as_attachment=True,
        filename=f"identity-proof{extension}",
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _dashboard_context():
    restaurants = Restaurant.objects.annotate(
        owner_count=Count(
            "user_profiles",
            filter=Q(user_profiles__role=UserProfile.Role.OWNER),
            distinct=True,
        ),
        staff_count=Count(
            "user_profiles",
            filter=Q(
                user_profiles__role__in=[
                    UserProfile.Role.WAITER,
                    UserProfile.Role.KITCHEN_STAFF,
                ]
            ),
            distinct=True,
        ),
    ).order_by("name")
    role_counts = UserProfile.objects.aggregate(
        owner_count=Count("user_id", filter=Q(role=UserProfile.Role.OWNER)),
        waiter_count=Count("user_id", filter=Q(role=UserProfile.Role.WAITER)),
        kitchen_count=Count("user_id", filter=Q(role=UserProfile.Role.KITCHEN_STAFF)),
    )
    return {
        "restaurants": restaurants,
        "restaurant_count": Restaurant.objects.count(),
        **role_counts,
    }


def _post_login_redirect(user):
    profile = get_profile(user)
    if profile and profile.role == UserProfile.Role.SUPERADMIN:
        return redirect("dineva:dashboard")
    if profile and profile.role == UserProfile.Role.OWNER:
        return redirect("dineva:owner-staff-list")
    return redirect("dineva:home")


@never_cache
@require_POST
def logout_view(request):
    logout(request)
    return redirect("dineva:login")
