import hashlib
import logging
import mimetypes
import secrets
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .access import get_profile, is_owner, is_superadmin, owner_can_manage_profile
from .decorators import kitchen_required, owner_required, superadmin_required, waiter_required
from .forms import (
    DinevaAuthenticationForm,
    EmployeeEditForm,
    ImpersonationReasonForm,
    OwnerCompletedOrderCorrectionForm,
    OwnerCreateForm,
    OwnerMenuItemForm,
    OwnerPaymentRecordForm,
    OwnerRestaurantSettingsForm,
    OwnerRestaurantTaxProfileForm,
    OwnerStaffCreateForm,
    OwnerTableForm,
    RestaurantForm,
    StaffCreateForm,
    StaffPhotoForm,
    WaiterOrderForm,
    WaiterSessionCheckoutForm,
    WaiterSessionPaymentForm,
    WaiterSessionRoundForm,
    SuperAdminTaxProfileReviewForm,
)
from .impersonation import (
    close_impersonation_without_restore,
    is_impersonating,
    start_impersonation,
    stop_impersonation,
    validate_impersonation_target,
)
from .models import (
    AssignOrder,
    ImpersonationAudit,
    Item,
    OrderItem,
    Payment,
    Restaurant,
    RestaurantTaxProfile,
    RestaurantTaxProfileReview,
    RestaurantTable,
    SessionInvoice,
    SessionPayment,
    TableSession,
    User,
    UserProfile,
)
from .services import (
    DuplicateAccountError,
    OrderWorkflowError,
    PaymentWorkflowError,
    RegistrationEmailError,
    TaxProfileError,
    InvoiceWorkflowError,
    add_waiter_session_round,
    close_waiter_table_session,
    create_employee,
    create_waiter_order,
    finalize_session_invoice,
    mark_waiter_order_item_served,
    record_owner_completed_order_correction,
    record_owner_order_payment,
    record_waiter_session_payment,
    send_invoice_email_once,
    table_has_active_order,
    transition_kitchen_order_item,
    update_employee_profile,
    update_staff_photo,
)


logger = logging.getLogger(__name__)
ORDER_SUBMISSION_SESSION_KEY = "dineva_waiter_order_submission_keys"
PAYMENT_SUBMISSION_SESSION_KEY = "dineva_owner_payment_submission_keys"
SESSION_PAYMENT_SUBMISSION_SESSION_KEY = "dineva_waiter_session_payment_submission_keys"


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
    return render(request, "dineva/superadmin/dashboard.html", _dashboard_context(request.user))


@never_cache
@owner_required
def owner_dashboard(request):
    return render(request, "dineva/owner/dashboard.html", _owner_dashboard_context(request.user))


@never_cache
@superadmin_required
def restaurant_list(request):
    status = request.GET.get("status", "all").strip().lower()
    if status not in {"all", "active", "inactive"}:
        status = "all"

    search_query = request.GET.get("q", "").strip()
    restaurants = _restaurant_queryset()
    if search_query:
        restaurants = restaurants.filter(
            Q(name__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(contact__icontains=search_query)
        )
    if status == "active":
        restaurants = restaurants.filter(is_active=True)
    elif status == "inactive":
        restaurants = restaurants.filter(is_active=False)

    paginator = Paginator(restaurants, 10)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    summary = Restaurant.objects.aggregate(
        total_count=Count("id"),
        active_count=Count("id", filter=Q(is_active=True)),
        inactive_count=Count("id", filter=Q(is_active=False)),
    )
    return render(
        request,
        "dineva/superadmin/restaurants/list.html",
        {
            "page_obj": page_obj,
            "restaurants": page_obj.object_list,
            "search_query": search_query,
            "status_filter": status,
            "query_params": query_params.urlencode(),
            "has_active_filters": bool(search_query or status != "all"),
            **summary,
        },
    )


@never_cache
@superadmin_required
def restaurant_add(request):
    form = RestaurantForm(
        request.POST or None,
        request.FILES or None,
        initial=None if request.method == "POST" else {"is_active": True},
    )
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Restaurant created successfully.")
        return redirect("dineva:restaurants")
    return render(
        request,
        "dineva/superadmin/restaurants/form.html",
        {"form": form, "mode": "add"},
    )


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
    return render(
        request,
        "dineva/superadmin/restaurants/form.html",
        {"form": form, "mode": "edit", "restaurant": restaurant},
    )


@require_POST
@superadmin_required
def restaurant_status(request, restaurant_id):
    restaurant = get_object_or_404(Restaurant, pk=restaurant_id)
    restaurant.is_active = not restaurant.is_active
    restaurant.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Restaurant status updated.")
    redirect_params = {}
    search_query = request.POST.get("q", "").strip()
    status = request.POST.get("status", "all").strip().lower()
    page = request.POST.get("page", "").strip()
    if search_query:
        redirect_params["q"] = search_query
    if status in {"active", "inactive"}:
        redirect_params["status"] = status
    if page.isdigit() and int(page) > 1:
        redirect_params["page"] = page
    redirect_url = reverse("dineva:restaurants")
    if redirect_params:
        return redirect(f"{redirect_url}?{urlencode(redirect_params)}")
    return redirect(redirect_url)


@never_cache
@superadmin_required
def tax_profile_list(request):
    status = request.GET.get("status", RestaurantTaxProfile.ConfigurationStatus.PENDING_REVIEW).strip()
    valid_statuses = set(RestaurantTaxProfile.ConfigurationStatus.values) | {"all"}
    if status not in valid_statuses:
        status = RestaurantTaxProfile.ConfigurationStatus.PENDING_REVIEW
    search_query = request.GET.get("q", "").strip()
    profiles = RestaurantTaxProfile.objects.select_related(
        "restaurant",
        "reviewed_by",
        "verified_by",
    ).order_by("-submitted_at", "restaurant__name")
    if status != "all":
        profiles = profiles.filter(configuration_status=status)
    if search_query:
        profiles = profiles.filter(
            Q(restaurant__name__icontains=search_query)
            | Q(gstin__icontains=search_query)
            | Q(legal_name__icontains=search_query)
            | Q(trade_name__icontains=search_query)
        )
    paginator = Paginator(profiles, 15)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dineva/superadmin/tax_profiles/list.html",
        {
            "page_obj": page_obj,
            "tax_profiles": page_obj.object_list,
            "search_query": search_query,
            "status_filter": status,
            "status_choices": RestaurantTaxProfile.ConfigurationStatus.choices,
            "query_params": query_params.urlencode(),
            "has_active_filters": bool(
                search_query
                or status != RestaurantTaxProfile.ConfigurationStatus.PENDING_REVIEW
            ),
        },
    )


@never_cache
@superadmin_required
def tax_profile_review(request, profile_id):
    tax_profile = get_object_or_404(
        RestaurantTaxProfile.objects.select_related("restaurant", "reviewed_by", "verified_by"),
        pk=profile_id,
    )
    can_review = tax_profile.configuration_status == RestaurantTaxProfile.ConfigurationStatus.PENDING_REVIEW
    form = SuperAdminTaxProfileReviewForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        decision = form.cleaned_data["decision"]
        notes = form.cleaned_data["review_notes"].strip()
        try:
            with transaction.atomic():
                tax_profile = RestaurantTaxProfile.objects.select_for_update().select_related(
                    "restaurant"
                ).get(pk=profile_id)
                previous_status = tax_profile.configuration_status
                if previous_status != RestaurantTaxProfile.ConfigurationStatus.PENDING_REVIEW:
                    raise ValidationError("Only profiles pending review can be approved or rejected.")
                now = timezone.now()
                tax_profile.reviewed_by = request.user
                tax_profile.reviewed_at = now
                tax_profile.review_notes = notes
                if decision == "approve":
                    tax_profile.configuration_status = RestaurantTaxProfile.ConfigurationStatus.VERIFIED
                    tax_profile.verified_by = request.user
                    tax_profile.verified_at = now
                    review_decision = RestaurantTaxProfileReview.Decision.APPROVED
                else:
                    tax_profile.configuration_status = RestaurantTaxProfile.ConfigurationStatus.REJECTED
                    tax_profile.verified_by = None
                    tax_profile.verified_at = None
                    review_decision = RestaurantTaxProfileReview.Decision.REJECTED
                tax_profile.full_clean()
                tax_profile.save(
                    update_fields=[
                        "configuration_status",
                        "verified_by",
                        "verified_at",
                        "reviewed_by",
                        "reviewed_at",
                        "review_notes",
                        "updated_at",
                    ]
                )
                review = RestaurantTaxProfileReview(
                    tax_profile=tax_profile,
                    reviewer=request.user,
                    decision=review_decision,
                    previous_status=previous_status,
                    resulting_status=tax_profile.configuration_status,
                    notes=notes,
                )
                review.full_clean()
                review.save()
        except ValidationError as exc:
            if hasattr(exc, "message_dict"):
                for errors in exc.message_dict.values():
                    form.add_error(None, errors)
            else:
                form.add_error(None, exc)
        except RestaurantTaxProfile.DoesNotExist:
            raise PermissionDenied
        else:
            if decision == "approve":
                messages.success(request, "GST profile approved. Server-side GST billing can now use this verified profile.")
            else:
                messages.success(request, "GST profile rejected and the rejection reason was recorded.")
            return redirect("dineva:tax-profile-review", profile_id=tax_profile.id)
    return render(
        request,
        "dineva/superadmin/tax_profiles/review.html",
        {
            "form": form,
            "tax_profile": tax_profile,
            "can_review": can_review,
            "reviews": tax_profile.reviews.select_related("reviewer").order_by("-created_at"),
        },
    )


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
    active_restaurants_available = form.fields["restaurant"].queryset.exists()
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
    return render(
        request,
        "dineva/superadmin/owners/form.html",
        {
            "form": form,
            "title": "Add Owner",
            "active_restaurants_available": active_restaurants_available,
        },
    )


@never_cache
@superadmin_required
def staff_add(request):
    form = StaffCreateForm(request.POST or None, request.FILES or None)
    active_restaurants_available = form.fields["restaurant"].queryset.exists()
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
    return render(
        request,
        "dineva/superadmin/staff/form.html",
        _staff_form_context(
            form=form,
            staff_list_url=reverse("dineva:staff-list"),
            staff_form_eyebrow="Staff Management",
            staff_form_title="Add Staff",
            staff_form_description="Create a Waiter or Kitchen Staff account for an active restaurant.",
            staff_form_warning=(
                "No active restaurants are available. Create or activate a restaurant before adding Staff."
                if not active_restaurants_available
                else ""
            ),
            staff_form_warning_url=reverse("dineva:restaurant-add"),
            staff_form_warning_label="Add Restaurant",
            staff_submit_disabled=not active_restaurants_available,
        ),
    )


def _profile_queryset():
    return UserProfile.objects.select_related("user", "restaurant")


@never_cache
@superadmin_required
def owner_list(request):
    status = request.GET.get("status", "all").strip().lower()
    if status not in {"all", "active", "inactive"}:
        status = "all"

    search_query = request.GET.get("q", "").strip()
    profiles = _profile_queryset().filter(role=UserProfile.Role.OWNER)
    if search_query:
        profiles = profiles.filter(
            Q(user__name__icontains=search_query)
            | Q(user__username__icontains=search_query)
            | Q(public_id__icontains=search_query)
            | Q(user__email__icontains=search_query)
            | Q(contact__icontains=search_query)
            | Q(restaurant__name__icontains=search_query)
        )
    if status == "active":
        profiles = profiles.filter(user__is_active=True)
    elif status == "inactive":
        profiles = profiles.filter(user__is_active=False)

    paginator = Paginator(profiles.order_by("user__name"), 10)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dineva/superadmin/owners/list.html",
        {
            "page_obj": page_obj,
            "owners": page_obj.object_list,
            "search_query": search_query,
            "status_filter": status,
            "query_params": query_params.urlencode(),
            "has_active_filters": bool(search_query or status != "all"),
        },
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


def _staff_list_context(
    *,
    request,
    profiles,
    staff_page_eyebrow,
    staff_page_title,
    staff_page_description,
    staff_empty_message,
    staff_list_url,
    staff_add_url,
    staff_detail_route,
    staff_status_route,
    staff_show_restaurant,
    staff_search_placeholder,
    staff_edit_route="",
    staff_impersonate_route="",
):
    status = request.GET.get("status", "all").strip().lower()
    if status not in {"all", "active", "inactive"}:
        status = "all"

    role = request.GET.get("role", "all").strip()
    allowed_staff_roles = {UserProfile.Role.WAITER, UserProfile.Role.KITCHEN_STAFF}
    if role not in allowed_staff_roles:
        role = "all"

    search_query = request.GET.get("q", "").strip()
    if search_query:
        search_filter = (
            Q(user__name__icontains=search_query)
            | Q(user__username__icontains=search_query)
            | Q(public_id__icontains=search_query)
            | Q(user__email__icontains=search_query)
            | Q(contact__icontains=search_query)
        )
        if staff_show_restaurant:
            search_filter |= Q(restaurant__name__icontains=search_query)
        profiles = profiles.filter(search_filter)
    if role != "all":
        profiles = profiles.filter(role=role)
    if status == "active":
        profiles = profiles.filter(user__is_active=True)
    elif status == "inactive":
        profiles = profiles.filter(user__is_active=False)

    paginator = Paginator(profiles.order_by("user__name"), 10)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return {
        "page_obj": page_obj,
        "staff_members": page_obj.object_list,
        "search_query": search_query,
        "status_filter": status,
        "role_filter": role,
        "query_params": query_params.urlencode(),
        "has_active_filters": bool(search_query or status != "all" or role != "all"),
        "waiter_role": UserProfile.Role.WAITER,
        "kitchen_staff_role": UserProfile.Role.KITCHEN_STAFF,
        "staff_page_eyebrow": staff_page_eyebrow,
        "staff_page_title": staff_page_title,
        "staff_page_description": staff_page_description,
        "staff_empty_message": staff_empty_message,
        "staff_list_url": staff_list_url,
        "staff_add_url": staff_add_url,
        "staff_detail_route": staff_detail_route,
        "staff_edit_route": staff_edit_route,
        "staff_impersonate_route": staff_impersonate_route,
        "staff_status_route": staff_status_route,
        "staff_show_restaurant": staff_show_restaurant,
        "staff_search_placeholder": staff_search_placeholder,
    }


def _staff_form_context(
    *,
    form,
    staff_list_url,
    staff_form_eyebrow,
    staff_form_title,
    staff_form_description,
    staff_form_warning="",
    staff_form_warning_url="",
    staff_form_warning_label="",
    staff_submit_disabled=False,
):
    return {
        "form": form,
        "staff_list_url": staff_list_url,
        "staff_form_eyebrow": staff_form_eyebrow,
        "staff_form_title": staff_form_title,
        "staff_form_description": staff_form_description,
        "staff_form_warning": staff_form_warning,
        "staff_form_warning_url": staff_form_warning_url,
        "staff_form_warning_label": staff_form_warning_label,
        "staff_submit_disabled": staff_submit_disabled,
    }


@never_cache
@superadmin_required
def staff_list(request):
    allowed_staff_roles = {UserProfile.Role.WAITER, UserProfile.Role.KITCHEN_STAFF}
    profiles = _profile_queryset().filter(role__in=allowed_staff_roles)
    return render(
        request,
        "dineva/superadmin/staff/list.html",
        _staff_list_context(
            request=request,
            profiles=profiles,
            staff_page_eyebrow="User Management",
            staff_page_title="Staff Management",
            staff_page_description="Manage Waiters and Kitchen Staff across Dineva restaurants.",
            staff_empty_message="Create Waiter and Kitchen Staff accounts to begin managing restaurant employees.",
            staff_list_url=reverse("dineva:staff-list"),
            staff_add_url=reverse("dineva:staff-add"),
            staff_detail_route="dineva:employee-detail",
            staff_edit_route="dineva:employee-edit",
            staff_impersonate_route="dineva:impersonation-confirm",
            staff_status_route="dineva:employee-status",
            staff_show_restaurant=True,
            staff_search_placeholder="Search staff by name, username, public ID, email, contact, or restaurant...",
        ),
    )


@never_cache
@superadmin_required
def impersonation_confirm(request, user_id):
    if is_impersonating(request):
        raise PermissionDenied

    profile = get_object_or_404(_profile_queryset(), user_id=user_id)
    if profile.role == UserProfile.Role.SUPERADMIN:
        raise PermissionDenied

    try:
        validate_impersonation_target(profile.user)
    except ValidationError as exc:
        raise PermissionDenied from exc

    form = ImpersonationReasonForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            start_impersonation(
                request,
                target_user=profile.user,
                reason=form.cleaned_data["reason"],
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        except PermissionDenied:
            raise
        else:
            messages.warning(
                request,
                f"You are now impersonating {profile.user.name} ({profile.get_role_display()}).",
            )
            return _post_login_redirect(profile.user)

    return render(
        request,
        "dineva/impersonation_confirm.html",
        {"form": form, "target": profile},
    )


@never_cache
@require_POST
def impersonation_stop(request):
    if not request.user.is_authenticated:
        return redirect("dineva:login")
    restored = stop_impersonation(request)
    if restored:
        messages.success(request, "Impersonation stopped. Super Admin session restored.")
        return redirect("dineva:dashboard")
    messages.warning(request, "Impersonation stopped. Please sign in again.")
    return redirect("dineva:login")


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
    next_url = request.POST.get("next", "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("dineva:employee-detail", user_id=profile.user_id)


@never_cache
@owner_required
def owner_staff_list(request):
    owner_profile = request.user.profile
    profiles = _profile_queryset().filter(
        restaurant_id=owner_profile.restaurant_id,
        role__in=[UserProfile.Role.WAITER, UserProfile.Role.KITCHEN_STAFF],
    )
    return render(
        request,
        "dineva/owner/staff/list.html",
        _staff_list_context(
            request=request,
            profiles=profiles,
            staff_page_eyebrow="Staff Management",
            staff_page_title="Manage Staff",
            staff_page_description=f"Manage Waiters and Kitchen Staff for {owner_profile.restaurant.name}.",
            staff_empty_message="Create Waiter and Kitchen Staff accounts for your restaurant team.",
            staff_list_url=reverse("dineva:owner-staff-list"),
            staff_add_url=reverse("dineva:owner-staff-add"),
            staff_detail_route="dineva:owner-staff-detail",
            staff_status_route="dineva:owner-staff-status",
            staff_show_restaurant=False,
            staff_search_placeholder="Search staff by name, username, public ID, email, or contact...",
        ),
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
    return render(
        request,
        "dineva/owner/staff/form.html",
        _staff_form_context(
            form=form,
            staff_list_url=reverse("dineva:owner-staff-list"),
            staff_form_eyebrow="Staff Management",
            staff_form_title="Add Staff",
            staff_form_description=(
                f"Create a Waiter or Kitchen Staff account for {request.user.profile.restaurant.name}."
            ),
        ),
    )


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
    next_url = request.POST.get("next", "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("dineva:owner-staff-detail", user_id=user_id)


def _owner_restaurant(user):
    return user.profile.restaurant


def _save_owner_restaurant_settings(form, *, restaurant):
    old_logo_name = restaurant.logo.name if restaurant.logo else ""
    instance = form.save(commit=False)
    try:
        with transaction.atomic():
            instance.full_clean()
            instance.save(
                update_fields=[
                    "name",
                    "address",
                    "contact",
                    "email",
                    "logo",
                    "updated_at",
                ]
            )
    except Exception:
        failed_logo_name = instance.logo.name if instance.logo else ""
        if failed_logo_name and failed_logo_name != old_logo_name:
            try:
                instance.logo.storage.delete(failed_logo_name)
            except Exception:
                logger.exception("Failed to clean up restaurant logo upload after save failure.")
        raise
    return instance


def _owner_tax_profile_instance(restaurant):
    try:
        return restaurant.tax_profile
    except RestaurantTaxProfile.DoesNotExist:
        return RestaurantTaxProfile(
            restaurant=restaurant,
            legal_name=restaurant.name,
            trade_name=restaurant.name,
            business_address=restaurant.address,
            state_name="Karnataka",
            state_code="29",
        )


@never_cache
@owner_required
def owner_restaurant_settings(request):
    restaurant = _owner_restaurant(request.user)
    form = OwnerRestaurantSettingsForm(
        request.POST or None,
        request.FILES or None,
        instance=restaurant,
    )
    if request.method == "POST" and form.is_valid():
        try:
            _save_owner_restaurant_settings(form, restaurant=restaurant)
        except ValidationError as exc:
            if hasattr(exc, "error_dict"):
                for field, errors in exc.error_dict.items():
                    form.add_error(field if field in form.fields else None, errors)
            else:
                form.add_error(None, exc)
        except Exception:
            logger.exception("Owner restaurant settings update failed.")
            form.add_error(
                None,
                "Unable to save restaurant settings right now. Please try again.",
            )
        else:
            messages.success(request, "Restaurant profile updated successfully.")
            return redirect("dineva:owner-restaurant-settings")
    return render(
        request,
        "dineva/owner/restaurant_settings.html",
        {"form": form, "restaurant": restaurant},
    )


@never_cache
@owner_required
def owner_restaurant_tax_settings(request):
    restaurant = _owner_restaurant(request.user)
    tax_profile = _owner_tax_profile_instance(restaurant)
    form = OwnerRestaurantTaxProfileForm(
        request.POST or None,
        instance=tax_profile,
    )
    if request.method == "POST" and form.is_valid():
        tax_profile = form.save(commit=False)
        tax_profile.restaurant = restaurant
        tax_profile.configuration_status = RestaurantTaxProfile.ConfigurationStatus.PENDING_REVIEW
        tax_profile.submitted_at = timezone.now()
        tax_profile.verified_by = None
        tax_profile.verified_at = None
        tax_profile.reviewed_by = None
        tax_profile.reviewed_at = None
        tax_profile.review_notes = ""
        try:
            with transaction.atomic():
                tax_profile.full_clean()
                tax_profile.save()
        except ValidationError as exc:
            if hasattr(exc, "error_dict"):
                for field, errors in exc.error_dict.items():
                    form.add_error(field if field in form.fields else None, errors)
            else:
                form.add_error(None, exc)
        except Exception:
            logger.exception("Owner restaurant tax settings update failed.")
            form.add_error(
                None,
                "Unable to submit GST settings right now. Please try again.",
            )
        else:
            messages.success(
                request,
                "GST settings submitted for Dineva review. GST calculations are not active yet.",
            )
            return redirect("dineva:owner-restaurant-tax-settings")
    return render(
        request,
        "dineva/owner/restaurant_tax_settings.html",
        {
            "form": form,
            "restaurant": restaurant,
            "tax_profile": tax_profile,
            "has_saved_tax_profile": tax_profile.pk is not None,
        },
    )


def _owner_menu_queryset(owner):
    return Item.objects.filter(restaurant=_owner_restaurant(owner)).order_by(
        "category",
        "name",
    )


def _owner_menu_item_or_404(owner, item_id):
    return get_object_or_404(
        Item,
        pk=item_id,
        restaurant=_owner_restaurant(owner),
    )


def _safe_next_url(request, fallback):
    next_url = request.POST.get("next", "").strip()
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url
    return fallback


@never_cache
@owner_required
def owner_menu_list(request):
    search_query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "all").strip()
    status = request.GET.get("status", "all").strip().lower()
    if status not in {"all", "active", "inactive", "available", "unavailable"}:
        status = "all"

    base_queryset = _owner_menu_queryset(request.user)
    category_options = list(
        base_queryset.exclude(category="")
        .order_by("category")
        .values_list("category", flat=True)
        .distinct()
    )
    if category != "all" and category not in category_options:
        category = "all"

    items = base_queryset
    if search_query:
        items = items.filter(
            Q(name__icontains=search_query)
            | Q(category__icontains=search_query)
            | Q(description__icontains=search_query)
        )
    if category != "all":
        items = items.filter(category=category)
    if status == "active":
        items = items.filter(is_active=True)
    elif status == "inactive":
        items = items.filter(is_active=False)
    elif status == "available":
        items = items.filter(is_available=True)
    elif status == "unavailable":
        items = items.filter(is_available=False)

    paginator = Paginator(items, 12)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dineva/owner/menu/list.html",
        {
            "page_obj": page_obj,
            "items": page_obj.object_list,
            "search_query": search_query,
            "category_filter": category,
            "status_filter": status,
            "category_options": category_options,
            "query_params": query_params.urlencode(),
            "has_active_filters": bool(search_query or category != "all" or status != "all"),
            "restaurant": _owner_restaurant(request.user),
        },
    )


def _save_owner_menu_item(form, *, restaurant, instance=None):
    old_image = instance.image if instance and instance.pk else None
    old_storage = old_image.storage if old_image and old_image.name else None
    old_name = old_image.name if old_image and old_image.name else ""
    item = form.save(commit=False)
    item.restaurant = restaurant
    new_name = ""
    try:
        with transaction.atomic():
            item.full_clean()
            item.save()
            new_name = item.image.name if item.image else ""
            form.save_m2m()
            if old_name and old_name != new_name and old_storage:
                transaction.on_commit(lambda: old_storage.delete(old_name))
    except Exception:
        failed_name = new_name or (item.image.name if item.image else "")
        if failed_name and failed_name != old_name:
            try:
                item.image.storage.delete(failed_name)
            except Exception:
                logger.exception("Failed to delete menu item image after a save error.")
        raise
    return item


@never_cache
@owner_required
def owner_menu_add(request):
    form = OwnerMenuItemForm(request.POST or None, request.FILES or None)
    if "restaurant" in request.POST:
        form.add_error(None, "Restaurant cannot be supplied for menu items.")
    if request.method == "POST" and form.is_valid():
        _save_owner_menu_item(
            form,
            restaurant=_owner_restaurant(request.user),
        )
        messages.success(request, "Menu item created.")
        return redirect("dineva:owner-menu-list")
    return render(
        request,
        "dineva/owner/menu/form.html",
        {
            "form": form,
            "mode": "add",
            "title": "Add Menu Item",
        },
    )


@never_cache
@owner_required
def owner_menu_edit(request, item_id):
    item = _owner_menu_item_or_404(request.user, item_id)
    form = OwnerMenuItemForm(
        request.POST or None,
        request.FILES or None,
        instance=item,
    )
    if "restaurant" in request.POST:
        form.add_error(None, "Restaurant cannot be changed for menu items.")
    if request.method == "POST" and form.is_valid():
        _save_owner_menu_item(
            form,
            restaurant=_owner_restaurant(request.user),
            instance=item,
        )
        messages.success(request, "Menu item updated.")
        return redirect("dineva:owner-menu-list")
    return render(
        request,
        "dineva/owner/menu/form.html",
        {
            "form": form,
            "mode": "edit",
            "title": "Edit Menu Item",
            "item": item,
        },
    )


@require_POST
@owner_required
def owner_menu_availability(request, item_id):
    item = _owner_menu_item_or_404(request.user, item_id)
    item.is_available = not item.is_available
    item.save(update_fields=["is_available", "updated_at"])
    messages.success(
        request,
        "Menu item availability updated.",
    )
    return redirect(_safe_next_url(request, reverse("dineva:owner-menu-list")))


@require_POST
@owner_required
def owner_menu_status(request, item_id):
    item = _owner_menu_item_or_404(request.user, item_id)
    item.is_active = not item.is_active
    item.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Menu item status updated.")
    return redirect(_safe_next_url(request, reverse("dineva:owner-menu-list")))


def _owner_table_queryset(owner):
    return RestaurantTable.objects.filter(restaurant=_owner_restaurant(owner)).order_by("table_no")


def _owner_table_or_404(owner, table_id):
    return get_object_or_404(
        RestaurantTable,
        pk=table_id,
        restaurant=_owner_restaurant(owner),
    )


def _save_owner_table(form, *, restaurant, instance=None):
    table = form.save(commit=False)
    table.restaurant = restaurant
    if instance and instance.pk and table_has_active_order(instance):
        raise ValidationError("Tables with an active order cannot be modified.")
    table.full_clean()
    table.save()
    return table


@never_cache
@owner_required
def owner_table_list(request):
    search_query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all").strip().lower()
    if status not in {"all", "active", "inactive", "available", "occupied", "reserved"}:
        status = "all"

    tables = _owner_table_queryset(request.user)
    if search_query:
        tables = tables.filter(table_no__icontains=search_query)
    if status == "active":
        tables = tables.filter(is_active=True)
    elif status == "inactive":
        tables = tables.filter(is_active=False)
    elif status in {"available", "occupied", "reserved"}:
        tables = tables.filter(status=status.upper())

    paginator = Paginator(tables, 12)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dineva/owner/tables/list.html",
        {
            "page_obj": page_obj,
            "tables": page_obj.object_list,
            "search_query": search_query,
            "status_filter": status,
            "query_params": query_params.urlencode(),
            "has_active_filters": bool(search_query or status != "all"),
            "restaurant": _owner_restaurant(request.user),
        },
    )


@never_cache
@owner_required
def owner_table_add(request):
    form = OwnerTableForm(request.POST or None)
    if "restaurant" in request.POST:
        form.add_error(None, "Restaurant cannot be supplied for tables.")
    if request.method == "POST" and form.is_valid():
        try:
            _save_owner_table(form, restaurant=_owner_restaurant(request.user))
        except (IntegrityError, ValidationError):
            form.add_error("table_no", "A table with this identifier already exists for your restaurant.")
        else:
            messages.success(request, "Table created.")
            return redirect("dineva:owner-table-list")
    return render(
        request,
        "dineva/owner/tables/form.html",
        {"form": form, "mode": "add", "title": "Add Table"},
    )


@never_cache
@owner_required
def owner_table_edit(request, table_id):
    table = _owner_table_or_404(request.user, table_id)
    form = OwnerTableForm(request.POST or None, instance=table)
    if "restaurant" in request.POST:
        form.add_error(None, "Restaurant cannot be changed for tables.")
    if request.method == "POST" and form.is_valid():
        try:
            _save_owner_table(
                form,
                restaurant=_owner_restaurant(request.user),
                instance=table,
            )
        except IntegrityError:
            form.add_error("table_no", "A table with this identifier already exists for your restaurant.")
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, "Table updated.")
            return redirect("dineva:owner-table-list")
    return render(
        request,
        "dineva/owner/tables/form.html",
        {"form": form, "mode": "edit", "title": "Edit Table", "table": table},
    )


@require_POST
@owner_required
def owner_table_status(request, table_id):
    table = _owner_table_or_404(request.user, table_id)
    if table.is_active and table_has_active_order(table):
        messages.error(request, "Tables with an active order cannot be deactivated.")
        return redirect(_safe_next_url(request, reverse("dineva:owner-table-list")))
    table.is_active = not table.is_active
    table.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Table status updated.")
    return redirect(_safe_next_url(request, reverse("dineva:owner-table-list")))


def _restaurant_order_queryset(user):
    return (
        AssignOrder.objects.filter(restaurant=user.profile.restaurant)
        .select_related("table", "waiter")
        .prefetch_related("items__item")
        .order_by("-created_at")
    )


def _owner_order_or_404(owner, order_id):
    return get_object_or_404(
        AssignOrder.objects.filter(restaurant=_owner_restaurant(owner))
        .select_related("table", "waiter", "customer")
        .prefetch_related(
            "items__item",
            "history_entries__user",
            "payments",
        ),
        pk=order_id,
    )


def _order_submission_keys(request):
    return list(request.session.get(ORDER_SUBMISSION_SESSION_KEY, []))


def _new_order_submission_key(request):
    keys = _order_submission_keys(request)
    key = secrets.token_urlsafe(32)
    keys.append(key)
    request.session[ORDER_SUBMISSION_SESSION_KEY] = keys[-10:]
    request.session.modified = True
    return key


def _order_submission_key_is_valid(request, key):
    return bool(key and key in _order_submission_keys(request))


def _consume_order_submission_key(request, key):
    keys = [existing for existing in _order_submission_keys(request) if existing != key]
    request.session[ORDER_SUBMISSION_SESSION_KEY] = keys
    request.session.modified = True


def _payment_submission_keys(request):
    return list(request.session.get(PAYMENT_SUBMISSION_SESSION_KEY, []))


def _new_payment_submission_key(request):
    keys = _payment_submission_keys(request)
    key = secrets.token_urlsafe(32)
    keys.append(key)
    request.session[PAYMENT_SUBMISSION_SESSION_KEY] = keys[-10:]
    request.session.modified = True
    return key


def _payment_submission_key_is_valid(request, key):
    return bool(key and key in _payment_submission_keys(request))


def _consume_payment_submission_key(request, key):
    keys = [existing for existing in _payment_submission_keys(request) if existing != key]
    request.session[PAYMENT_SUBMISSION_SESSION_KEY] = keys
    request.session.modified = True


def _session_payment_submission_keys(request):
    return list(request.session.get(SESSION_PAYMENT_SUBMISSION_SESSION_KEY, []))


def _new_session_payment_submission_key(request):
    keys = _session_payment_submission_keys(request)
    key = secrets.token_urlsafe(32)
    keys.append(key)
    request.session[SESSION_PAYMENT_SUBMISSION_SESSION_KEY] = keys[-10:]
    request.session.modified = True
    return key


def _session_payment_submission_key_is_valid(request, key):
    return bool(key and key in _session_payment_submission_keys(request))


def _consume_session_payment_submission_key(request, key):
    keys = [existing for existing in _session_payment_submission_keys(request) if existing != key]
    request.session[SESSION_PAYMENT_SUBMISSION_SESSION_KEY] = keys
    request.session.modified = True


def _owner_billing_queryset(owner):
    successful_payment_total = (
        Payment.objects.filter(
            assign_order=OuterRef("pk"),
            payment_status=Payment.Status.SUCCESS,
        )
        .values("assign_order")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    return (
        AssignOrder.objects.filter(
            restaurant=_owner_restaurant(owner),
            table_session__isnull=True,
        )
        .select_related("table", "waiter", "customer")
        .prefetch_related("items__item", "payments")
        .annotate(
            successful_payment_total=Coalesce(
                Subquery(successful_payment_total),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .order_by("-created_at")
    )


def _attach_billing_state(order):
    paid_total = getattr(order, "successful_payment_total", None)
    if paid_total is None:
        paid_total = sum(
            (
                payment.amount
                for payment in order.payments.all()
                if payment.payment_status == Payment.Status.SUCCESS
            ),
            Decimal("0.00"),
        )
    balance_due = order.total_amount - paid_total
    if balance_due < Decimal("0.00"):
        balance_due = Decimal("0.00")
    order.billing_paid_total = paid_total
    order.billing_balance_due = balance_due
    order.billing_is_paid = order.total_amount > Decimal("0.00") and paid_total >= order.total_amount
    order.billing_can_record_payment = (
        order.status != AssignOrder.Status.CANCELLED
        and order.total_amount > Decimal("0.00")
        and paid_total == Decimal("0.00")
    )
    if order.status == AssignOrder.Status.CANCELLED:
        order.billing_blocker = "Cancelled orders cannot accept payments."
    elif order.total_amount <= Decimal("0.00"):
        order.billing_blocker = "Only orders with a positive total can be paid."
    elif paid_total >= order.total_amount:
        order.billing_blocker = "This order is fully paid."
    elif paid_total > Decimal("0.00"):
        order.billing_blocker = "Partial payment completion needs confirmed split-payment rules."
    else:
        order.billing_blocker = ""
    return order


def _owner_billing_order_or_404(owner, order_id):
    order = get_object_or_404(
        _owner_billing_queryset(owner).prefetch_related("history_entries__user"),
        pk=order_id,
    )
    return _attach_billing_state(order)


def _session_invoice_payment_total_subquery():
    return (
        SessionPayment.objects.filter(
            invoice=OuterRef("pk"),
            payment_status=SessionPayment.Status.SUCCESS,
        )
        .values("invoice")
        .annotate(total=Sum("amount"))
        .values("total")
    )


def _owner_session_invoice_queryset(owner):
    paid_total = _session_invoice_payment_total_subquery()
    return (
        SessionInvoice.objects.filter(restaurant=_owner_restaurant(owner))
        .select_related("table_session__table", "finalized_by")
        .prefetch_related("payments", "lines__order_item__item", "table_session__orders")
        .annotate(
            successful_session_payment_total=Coalesce(
                Subquery(paid_total),
                Value(Decimal("0.00")),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .order_by("-invoice_date")
    )


def _owner_session_invoice_or_404(owner, invoice_id):
    return get_object_or_404(
        _owner_session_invoice_queryset(owner).prefetch_related(
            "delivery_attempts",
            "table_session__orders__items__item",
            "table_session__orders__history_entries__user",
        ),
        pk=invoice_id,
    )


def _attach_session_invoice_billing_state(invoice):
    paid_total = getattr(invoice, "successful_session_payment_total", None)
    if paid_total is None:
        paid_total = sum(
            (
                payment.amount
                for payment in invoice.payments.all()
                if payment.payment_status == SessionPayment.Status.SUCCESS
            ),
            Decimal("0.00"),
        )
    balance_due = invoice.grand_total - paid_total
    if balance_due < Decimal("0.00"):
        balance_due = Decimal("0.00")
    invoice.billing_paid_total = paid_total
    invoice.billing_balance_due = balance_due
    invoice.billing_is_paid = invoice.grand_total > Decimal("0.00") and paid_total >= invoice.grand_total
    return invoice


def _legacy_billing_row(order):
    _attach_billing_state(order)
    return {
        "kind": "legacy_order",
        "label": f"Order #{order.id}",
        "date": order.created_at,
        "order_type": order.get_order_type_display(),
        "table": order.table.table_no if order.table else "Takeaway",
        "waiter": order.waiter.name or order.waiter.username,
        "total_amount": order.total_amount,
        "paid_total": order.billing_paid_total,
        "balance_due": order.billing_balance_due,
        "is_paid": order.billing_is_paid,
        "detail_url": reverse("dineva:owner-billing-detail", args=[order.id]),
    }


def _session_invoice_billing_row(invoice):
    _attach_session_invoice_billing_state(invoice)
    table = invoice.table_session.table if invoice.table_session_id else None
    return {
        "kind": "session_invoice",
        "label": f"Invoice {invoice.invoice_number}",
        "date": invoice.invoice_date,
        "order_type": "Dine-in session",
        "table": table.table_no if table else "Session",
        "waiter": invoice.finalized_by.name or invoice.finalized_by.username,
        "total_amount": invoice.grand_total,
        "paid_total": invoice.billing_paid_total,
        "balance_due": invoice.billing_balance_due,
        "is_paid": invoice.billing_is_paid,
        "detail_url": reverse("dineva:owner-billing-invoice-detail", args=[invoice.id]),
    }


def _request_wants_json(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )


def _form_error_payload(form):
    return {
        "non_field_errors": [str(error) for error in form.non_field_errors()],
        "fields": {
            field: [str(error) for error in errors]
            for field, errors in form.errors.items()
            if field != "__all__"
        },
    }


def _order_kitchen_summary(order):
    items = list(order.items.all())
    if not items:
        return {
            "label": "No items",
            "code": "EMPTY",
            "ready_count": 0,
            "total_count": 0,
        }
    total_count = len(items)
    ready_count = sum(1 for item in items if item.status == OrderItem.Status.READY)
    served_count = sum(1 for item in items if item.status == OrderItem.Status.SERVED)
    preparing_count = sum(
        1 for item in items if item.status == OrderItem.Status.PREPARING
    )
    cancelled_count = sum(
        1 for item in items if item.status == OrderItem.Status.CANCELLED
    )
    if ready_count == total_count:
        label = "Ready"
        code = "READY"
    elif served_count == total_count:
        label = "Served"
        code = "SERVED"
    elif cancelled_count == total_count:
        label = "Cancelled"
        code = "CANCELLED"
    elif preparing_count or ready_count:
        label = "In preparation"
        code = "PREPARING"
    else:
        label = "Pending"
        code = "PENDING"
    return {
        "label": label,
        "code": code,
        "ready_count": ready_count,
        "served_count": served_count,
        "total_count": total_count,
    }


def _waiter_order_queryset(user):
    return (
        AssignOrder.objects.filter(restaurant=user.profile.restaurant)
        .select_related("table", "waiter")
        .prefetch_related("items__item")
        .order_by("-created_at")
    )


def _waiter_order_or_404(waiter, order_id):
    return get_object_or_404(
        _waiter_order_queryset(waiter).prefetch_related("history_entries__user"),
        pk=order_id,
    )


def _waiter_order_payload(order):
    kitchen_summary = _order_kitchen_summary(order)
    session_url = ""
    if order.table_session_id:
        session_url = reverse("dineva:waiter-session-detail", args=[order.table_session_id])
    return {
        "id": order.id,
        "detail_url": reverse("dineva:waiter-order-detail", args=[order.id]),
        "session_url": session_url,
        "order_type": order.order_type,
        "order_type_display": order.get_order_type_display(),
        "table": order.table.table_no if order.table else "Takeaway",
        "status": order.status,
        "status_display": order.get_status_display(),
        "total_amount": str(order.total_amount),
        "updated_at": order.updated_at.isoformat(),
        "kitchen_status": kitchen_summary,
        "items": [
            {
                "id": order_item.id,
                "name": order_item.item.name,
                "quantity": order_item.quantity,
                "status": order_item.status,
                "status_display": order_item.get_status_display(),
                "served_url": (
                    reverse("dineva:waiter-order-item-served", args=[order_item.id])
                    if order_item.status == OrderItem.Status.READY
                    else ""
                ),
            }
            for order_item in order.items.all()
        ],
    }


def _waiter_available_table_rows(restaurant):
    open_session_table_ids = set(
        TableSession.objects.filter(
            restaurant=restaurant,
            status__in=[
                TableSession.Status.OPEN,
                TableSession.Status.PAYMENT_PENDING,
            ],
            table_id__isnull=False,
        ).values_list("table_id", flat=True)
    )
    tables = RestaurantTable.objects.filter(restaurant=restaurant).order_by("table_no")
    return [
        {
            "id": table.id,
            "table_no": table.table_no,
            "is_available": (
                table.is_active
                and table.status == RestaurantTable.Status.AVAILABLE
                and table.id not in open_session_table_ids
            ),
            "status": table.status,
        }
        for table in tables
    ]


@never_cache
@waiter_required
def waiter_dashboard(request):
    restaurant = request.user.profile.restaurant
    orders = _waiter_order_queryset(request.user)
    active_orders = orders.filter(status=AssignOrder.Status.ACTIVE)
    active_session_table_ids = TableSession.objects.filter(
        restaurant=restaurant,
        status__in=[
            TableSession.Status.OPEN,
            TableSession.Status.PAYMENT_PENDING,
        ],
        table_id__isnull=False,
    ).values("table_id")
    recent_orders = list(orders[:8])
    for order in recent_orders:
        order.kitchen_summary = _order_kitchen_summary(order)
    return render(
        request,
        "dineva/waiter/dashboard.html",
        {
            "restaurant": restaurant,
            "active_order_count": active_orders.count(),
            "today_order_count": orders.filter(order_date__date=timezone.localdate()).count(),
            "available_table_count": RestaurantTable.objects.filter(
                restaurant=restaurant,
                is_active=True,
                status=RestaurantTable.Status.AVAILABLE,
            ).exclude(pk__in=active_session_table_ids).count(),
            "available_item_count": Item.objects.filter(
                restaurant=restaurant,
                is_active=True,
                is_available=True,
            ).count(),
            "recent_orders": recent_orders,
        },
    )


@never_cache
@waiter_required
def waiter_order_list(request):
    search_query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active").strip().lower()
    if status not in {"all", "active", "completed", "cancelled"}:
        status = "active"
    order_type = request.GET.get("type", "all").strip().upper()
    allowed_types = {AssignOrder.OrderType.DINE_IN, AssignOrder.OrderType.TAKEAWAY}
    if order_type not in allowed_types:
        order_type = "all"
    orders = _waiter_order_queryset(request.user)
    if search_query:
        search_filter = (
            Q(table__table_no__icontains=search_query)
            | Q(waiter__name__icontains=search_query)
            | Q(waiter__username__icontains=search_query)
            | Q(items__item__name__icontains=search_query)
        )
        if search_query.isdigit():
            search_filter |= Q(id=int(search_query))
        orders = orders.filter(search_filter).distinct()
    if status != "all":
        orders = orders.filter(status=status.upper())
    if order_type != "all":
        orders = orders.filter(order_type=order_type)
    paginator = Paginator(orders, 15)
    page_obj = paginator.get_page(request.GET.get("page"))
    for order in page_obj.object_list:
        order.kitchen_summary = _order_kitchen_summary(order)
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dineva/waiter/orders/list.html",
        {
            "page_obj": page_obj,
            "orders": page_obj.object_list,
            "search_query": search_query,
            "status_filter": status,
            "type_filter": order_type,
            "query_params": query_params.urlencode(),
            "has_active_filters": bool(search_query or status != "active" or order_type != "all"),
            "restaurant": request.user.profile.restaurant,
            "dine_in_type": AssignOrder.OrderType.DINE_IN,
            "takeaway_type": AssignOrder.OrderType.TAKEAWAY,
        },
    )


def _waiter_legacy_order_state(order):
    successful_payments = Payment.objects.filter(
        assign_order=order,
        restaurant=order.restaurant,
        payment_status=Payment.Status.SUCCESS,
    )
    paid_total = sum(
        (payment.amount for payment in successful_payments),
        Decimal("0.00"),
    )
    unresolved_count = order.items.exclude(
        status__in=[OrderItem.Status.SERVED, OrderItem.Status.CANCELLED]
    ).count()
    if paid_total:
        next_message = (
            "This order has a legacy payment recorded and is not linked to a table session. "
            "Do not create a duplicate checkout or invoice from this page."
        )
    elif unresolved_count:
        next_message = "This legacy order still has items pending preparation or service."
    else:
        next_message = (
            "This order is not linked to a table session. Add-more and consolidated checkout "
            "are available only for session-backed table visits."
        )
    return {
        "paid_total": paid_total,
        "unresolved_count": unresolved_count,
        "next_message": next_message,
        "has_success_payment": paid_total > Decimal("0.00"),
    }


@never_cache
@waiter_required
def waiter_order_detail(request, order_id):
    order = _waiter_order_or_404(request.user, order_id)
    session_state = None
    legacy_order_state = None
    if order.table_session_id:
        table_session = _waiter_session_or_404(request.user, order.table_session_id)
        session_state = _waiter_session_payload(table_session)
    else:
        legacy_order_state = _waiter_legacy_order_state(order)
    order_item_rows = [
        {
            "order_item": order_item,
            "line_total": order_item.price * order_item.quantity,
        }
        for order_item in order.items.all()
    ]
    order.kitchen_summary = _order_kitchen_summary(order)
    ready_item_count = sum(
        1 for row in order_item_rows if row["order_item"].status == OrderItem.Status.READY
    )
    return render(
        request,
        "dineva/waiter/orders/detail.html",
        {
            "order": order,
            "order_item_rows": order_item_rows,
            "history_entries": order.history_entries.all().order_by("-created_at"),
            "restaurant": request.user.profile.restaurant,
            "ready_item_count": ready_item_count,
            "session_state": session_state,
            "legacy_order_state": legacy_order_state,
        },
    )


@never_cache
@require_GET
@waiter_required
def waiter_order_statuses(request):
    ids_param = request.GET.get("ids", "")
    raw_ids = [
        value.strip()
        for value in ids_param.replace(",", " ").split()
        if value.strip()
    ]
    order_ids = [int(value) for value in raw_ids if value.isdigit()]
    orders = _waiter_order_queryset(request.user)
    if order_ids:
        orders = orders.filter(pk__in=order_ids)
    elif ids_param:
        orders = orders.none()
    else:
        orders = orders.filter(status=AssignOrder.Status.ACTIVE)[:25]
    return JsonResponse(
        {
            "orders": [_waiter_order_payload(order) for order in orders],
            "poll_interval_ms": 7000,
        }
    )


@never_cache
@require_GET
@waiter_required
def waiter_table_availability(request):
    restaurant = request.user.profile.restaurant
    return JsonResponse(
        {
            "tables": _waiter_available_table_rows(restaurant),
            "poll_interval_ms": 10000,
        }
    )


def _waiter_session_queryset(user):
    return (
        TableSession.objects.filter(restaurant=user.profile.restaurant)
        .select_related("table", "opened_by", "closed_by")
        .prefetch_related("orders__items__item", "invoice__payments", "invoice__lines")
        .order_by("-opened_at")
    )


def _waiter_session_or_404(waiter, session_id):
    return get_object_or_404(_waiter_session_queryset(waiter), pk=session_id)


def _session_bill_state(table_session):
    try:
        invoice = table_session.invoice
    except SessionInvoice.DoesNotExist:
        return {
            "invoice": None,
            "paid_total": Decimal("0.00"),
            "balance_due": Decimal("0.00"),
            "is_paid": False,
        }
    paid_total = sum(
        (
            payment.amount
            for payment in invoice.payments.all()
            if payment.payment_status == SessionPayment.Status.SUCCESS
        ),
        Decimal("0.00"),
    )
    balance_due = invoice.grand_total - paid_total
    if balance_due < Decimal("0.00"):
        balance_due = Decimal("0.00")
    return {
        "invoice": invoice,
        "paid_total": paid_total,
        "balance_due": balance_due,
        "is_paid": invoice.grand_total > Decimal("0.00") and paid_total >= invoice.grand_total,
    }


def _waiter_session_payload(table_session):
    orders = list(table_session.orders.all().order_by("created_at"))
    for order in orders:
        order.kitchen_summary = _order_kitchen_summary(order)
    bill_state = _session_bill_state(table_session)
    invoice = bill_state["invoice"]
    session_items = [
        order_item
        for order in orders
        if order.status != AssignOrder.Status.CANCELLED
        for order_item in order.items.all()
    ]
    unresolved_count = sum(
        1
        for order_item in session_items
        if order_item.status not in {OrderItem.Status.SERVED, OrderItem.Status.CANCELLED}
    )
    served_count = sum(1 for order_item in session_items if order_item.status == OrderItem.Status.SERVED)
    ready_count = sum(1 for order_item in session_items if order_item.status == OrderItem.Status.READY)
    billable_count = served_count
    next_step_ready = (
        table_session.status == TableSession.Status.OPEN
        and invoice is None
        and billable_count > 0
        and unresolved_count == 0
    )
    can_add_round = next_step_ready
    can_checkout = next_step_ready
    if invoice is not None:
        next_message = "This session has a finalized bill. Additional rounds are blocked; continue with payment."
    elif can_checkout:
        next_message = "Current items are served. Ask whether the customer wants more or is ready for checkout."
    elif unresolved_count:
        next_message = "Some items are still pending, preparing, or ready. Serve ready items before checkout."
    elif not orders:
        next_message = "No order rounds have been added to this session yet."
    else:
        next_message = "This session is not ready for checkout."
    return {
        "id": table_session.id,
        "status": table_session.status,
        "status_display": table_session.get_status_display(),
        "table": table_session.table.table_no if table_session.table_id else "",
        "orders": [_waiter_order_payload(order) for order in orders],
        "ready_count": ready_count,
        "served_count": served_count,
        "unresolved_count": unresolved_count,
        "billable_count": billable_count,
        "can_add_round": can_add_round,
        "can_checkout": can_checkout,
        "next_message": next_message,
        "add_round_url": (
            reverse("dineva:waiter-session-add-round", args=[table_session.id])
            if can_add_round
            else ""
        ),
        "checkout_url": (
            reverse("dineva:waiter-session-checkout", args=[table_session.id])
            if table_session.status == TableSession.Status.OPEN
            else ""
        ),
        "payment_url": (
            reverse("dineva:waiter-session-payment", args=[table_session.id])
            if invoice is not None
            else ""
        ),
        "bill": {
            "invoice_number": invoice.invoice_number if invoice else "",
            "paid_total": str(bill_state["paid_total"]),
            "balance_due": str(bill_state["balance_due"]),
            "is_paid": bill_state["is_paid"],
        },
    }


@never_cache
@waiter_required
def waiter_session_detail(request, session_id):
    table_session = _waiter_session_or_404(request.user, session_id)
    orders = list(table_session.orders.all().order_by("created_at"))
    for order in orders:
        order.kitchen_summary = _order_kitchen_summary(order)
    bill_state = _session_bill_state(table_session)
    session_state = _waiter_session_payload(table_session)
    return render(
        request,
        "dineva/waiter/sessions/detail.html",
        {
            "session": table_session,
            "orders": orders,
            "bill_state": bill_state,
            "session_state": session_state,
            "restaurant": request.user.profile.restaurant,
        },
    )


@never_cache
@waiter_required
def waiter_session_add_round(request, session_id):
    table_session = _waiter_session_or_404(request.user, session_id)
    wants_json = _request_wants_json(request)
    if table_session.status != TableSession.Status.OPEN:
        if wants_json:
            return JsonResponse(
                {"ok": False, "message": "Additional rounds can be added only to open sessions."},
                status=400,
            )
        messages.error(request, "Additional rounds can be added only to open sessions.")
        return redirect("dineva:waiter-session-detail", session_id=session_id)
    if hasattr(table_session, "invoice"):
        if wants_json:
            return JsonResponse(
                {"ok": False, "message": "This session already has a finalized bill."},
                status=400,
            )
        messages.error(request, "This session already has a finalized bill.")
        return redirect("dineva:waiter-session-detail", session_id=session_id)
    if request.method == "POST":
        submission_key = request.POST.get("submission_key", "")
        if not _order_submission_key_is_valid(request, submission_key):
            if wants_json:
                return JsonResponse(
                    {
                        "ok": False,
                        "message": "This order-round submission has expired or was already processed.",
                        "errors": {
                            "non_field_errors": [
                                "This order-round submission has expired or was already processed."
                            ],
                            "fields": {},
                        },
                        "new_submission_key": _new_order_submission_key(request),
                    },
                    status=409,
                )
            messages.error(request, "This order-round submission has expired or was already processed.")
            return redirect("dineva:waiter-session-add-round", session_id=session_id)
    else:
        submission_key = _new_order_submission_key(request)
    form = WaiterSessionRoundForm(
        request.POST or None,
        restaurant=request.user.profile.restaurant,
        submission_key=submission_key,
    )
    if request.method == "POST" and form.is_valid():
        submission_key = form.cleaned_data["submission_key"]
        try:
            order = add_waiter_session_round(
                waiter=request.user,
                session_id=session_id,
                order_lines=form.cleaned_data["order_lines"],
                idempotency_key=submission_key,
            )
        except OrderWorkflowError as exc:
            form.add_error(None, exc)
        else:
            _consume_order_submission_key(request, submission_key)
            if wants_json:
                order = _waiter_order_or_404(request.user, order.id)
                table_session = _waiter_session_or_404(request.user, session_id)
                return JsonResponse(
                    {
                        "ok": True,
                        "message": "Order round added to this table session.",
                        "order": _waiter_order_payload(order),
                        "session": _waiter_session_payload(table_session),
                        "new_submission_key": _new_order_submission_key(request),
                    },
                    status=201,
                )
            messages.success(request, "Order round added to this table session.")
            return redirect("dineva:waiter-session-detail", session_id=session_id)
    if request.method == "POST" and wants_json:
        return JsonResponse(
            {
                "ok": False,
                "message": "Review the highlighted fields and try again.",
                "errors": _form_error_payload(form),
            },
            status=400,
        )
    return render(
        request,
        "dineva/waiter/sessions/add_round.html",
        {
            "form": form,
            "session": table_session,
            "restaurant": request.user.profile.restaurant,
        },
    )


@require_POST
@waiter_required
def waiter_order_item_served(request, order_item_id):
    next_url = _safe_next_url(request, reverse("dineva:waiter-order-list"))
    wants_json = _request_wants_json(request)
    try:
        order_item = mark_waiter_order_item_served(waiter=request.user, order_item_id=order_item_id)
    except OrderWorkflowError as exc:
        if wants_json:
            return JsonResponse({"ok": False, "message": str(exc)}, status=400)
        messages.error(request, str(exc))
    else:
        if wants_json:
            order = _waiter_order_or_404(request.user, order_item.assign_order_id)
            payload = {
                "ok": True,
                "message": "Item marked served.",
                "order": _waiter_order_payload(order),
            }
            if order.table_session_id:
                table_session = _waiter_session_or_404(request.user, order.table_session_id)
                payload["session"] = _waiter_session_payload(table_session)
            else:
                payload["legacy_order"] = _waiter_legacy_order_state(order)
            return JsonResponse(payload)
        messages.success(request, "Item marked served.")
    return redirect(next_url)


@never_cache
@waiter_required
def waiter_session_checkout(request, session_id):
    table_session = _waiter_session_or_404(request.user, session_id)
    try:
        invoice = table_session.invoice
    except SessionInvoice.DoesNotExist:
        invoice = None
    if invoice:
        return redirect("dineva:waiter-session-payment", session_id=session_id)
    form = WaiterSessionCheckoutForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            invoice = finalize_session_invoice(
                waiter=request.user,
                session_id=session_id,
                customer_name=form.cleaned_data["customer_name"],
                customer_email=form.cleaned_data["customer_email"],
                customer_mobile=form.cleaned_data["customer_mobile"],
            )
        except (InvoiceWorkflowError, TaxProfileError, ValidationError) as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, f"Bill {invoice.invoice_number} finalized.")
            return redirect("dineva:waiter-session-payment", session_id=session_id)
    return render(
        request,
        "dineva/waiter/sessions/checkout.html",
        {
            "form": form,
            "session": table_session,
            "restaurant": request.user.profile.restaurant,
        },
    )


@never_cache
@waiter_required
def waiter_session_payment(request, session_id):
    table_session = _waiter_session_or_404(request.user, session_id)
    try:
        invoice = table_session.invoice
    except SessionInvoice.DoesNotExist:
        messages.error(request, "Finalize the bill before recording payment.")
        return redirect("dineva:waiter-session-checkout", session_id=session_id)
    bill_state = _session_bill_state(table_session)
    if request.method == "POST":
        submission_key = request.POST.get("submission_key", "")
        if not _session_payment_submission_key_is_valid(request, submission_key):
            messages.error(request, "This payment submission has expired or was already processed.")
            return redirect("dineva:waiter-session-payment", session_id=session_id)
    else:
        submission_key = _new_session_payment_submission_key(request)
    form = WaiterSessionPaymentForm(
        request.POST or None,
        submission_key=submission_key,
        initial={"amount": bill_state["balance_due"]},
    )
    if request.method == "POST" and form.is_valid():
        submission_key = form.cleaned_data["submission_key"]
        try:
            record_waiter_session_payment(
                waiter=request.user,
                invoice_id=invoice.id,
                payment_method=form.cleaned_data["payment_method"],
                amount=form.cleaned_data["amount"],
                reference=form.cleaned_data["reference"],
                idempotency_key=submission_key,
            )
        except PaymentWorkflowError as exc:
            form.add_error(None, exc)
        else:
            _consume_session_payment_submission_key(request, submission_key)
            try:
                delivery_attempt = send_invoice_email_once(invoice)
            except Exception:
                logger.exception("Failed to record or send session invoice email after payment.")
                messages.warning(
                    request,
                    "Payment recorded, but Dineva could not record the bill email delivery attempt.",
                )
            else:
                if delivery_attempt.status == "SENT":
                    messages.success(request, "Payment recorded and bill email sent.")
                elif delivery_attempt.status == "SKIPPED":
                    messages.info(
                        request,
                        "Payment recorded. Bill email was skipped because no customer email was provided.",
                    )
                else:
                    messages.warning(
                        request,
                        "Payment recorded, but bill email delivery was not successful.",
                    )
            return redirect("dineva:waiter-session-payment", session_id=session_id)
    delivery_attempts = invoice.delivery_attempts.filter(channel="EMAIL").order_by("-created_at")
    return render(
        request,
        "dineva/waiter/sessions/payment.html",
        {
            "form": form,
            "session": table_session,
            "invoice": invoice,
            "bill_state": bill_state,
            "delivery_attempts": delivery_attempts,
            "restaurant": request.user.profile.restaurant,
        },
    )


@require_POST
@waiter_required
def waiter_session_close(request, session_id):
    try:
        close_waiter_table_session(waiter=request.user, session_id=session_id)
    except (InvoiceWorkflowError, PaymentWorkflowError, ValidationError) as exc:
        messages.error(request, str(exc))
        return redirect("dineva:waiter-session-payment", session_id=session_id)
    messages.success(request, "Session closed and table released.")
    return redirect("dineva:waiter-order-list")


@never_cache
@owner_required
def owner_order_list(request):
    search_query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "active").strip().lower()
    if status not in {"all", "active", "completed", "cancelled"}:
        status = "active"
    order_type = request.GET.get("type", "all").strip().upper()
    allowed_types = {AssignOrder.OrderType.DINE_IN, AssignOrder.OrderType.TAKEAWAY}
    if order_type not in allowed_types:
        order_type = "all"
    orders = _restaurant_order_queryset(request.user)
    if search_query:
        search_filter = (
            Q(table__table_no__icontains=search_query)
            | Q(waiter__name__icontains=search_query)
            | Q(waiter__username__icontains=search_query)
            | Q(items__item__name__icontains=search_query)
        )
        if search_query.isdigit():
            search_filter |= Q(id=int(search_query))
        orders = orders.filter(search_filter).distinct()
    if status != "all":
        orders = orders.filter(status=status.upper())
    if order_type != "all":
        orders = orders.filter(order_type=order_type)
    paginator = Paginator(orders, 15)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dineva/owner/orders/list.html",
        {
            "page_obj": page_obj,
            "orders": page_obj.object_list,
            "search_query": search_query,
            "status_filter": status,
            "type_filter": order_type,
            "query_params": query_params.urlencode(),
            "has_active_filters": bool(search_query or status != "active" or order_type != "all"),
            "restaurant": request.user.profile.restaurant,
        },
    )


@never_cache
@owner_required
def owner_order_detail(request, order_id):
    order = _owner_order_or_404(request.user, order_id)
    session_invoice = None
    if order.table_session_id:
        try:
            session_invoice = _attach_session_invoice_billing_state(order.table_session.invoice)
        except SessionInvoice.DoesNotExist:
            session_invoice = None
    order_item_rows = [
        {
            "order_item": order_item,
            "line_total": order_item.price * order_item.quantity,
        }
        for order_item in order.items.all()
    ]
    return render(
        request,
        "dineva/owner/orders/detail.html",
        {
            "order": order,
            "order_item_rows": order_item_rows,
            "history_entries": order.history_entries.all().order_by("-created_at"),
            "payments": order.payments.all().order_by("-created_at"),
            "session_invoice": session_invoice,
            "correction_form": OwnerCompletedOrderCorrectionForm(),
            "can_record_correction": order.status == AssignOrder.Status.COMPLETED,
            "restaurant": request.user.profile.restaurant,
        },
    )


@require_POST
@owner_required
def owner_order_correction(request, order_id):
    form = OwnerCompletedOrderCorrectionForm(request.POST)
    if form.is_valid():
        try:
            record_owner_completed_order_correction(
                owner=request.user,
                order_id=order_id,
                reason=form.cleaned_data["reason"],
            )
        except OrderWorkflowError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Completed-order correction recorded.")
    else:
        messages.error(request, "Provide a meaningful correction reason.")
    return redirect("dineva:owner-order-detail", order_id=order_id)


@never_cache
@owner_required
def owner_billing_list(request):
    search_query = request.GET.get("q", "").strip()
    payment_filter = request.GET.get("payment", "due").strip().lower()
    if payment_filter not in {"all", "due", "paid"}:
        payment_filter = "due"
    order_status = request.GET.get("status", "all").strip().lower()
    if order_status not in {"all", "active", "completed", "cancelled"}:
        order_status = "all"

    orders = _owner_billing_queryset(request.user)
    session_invoices = _owner_session_invoice_queryset(request.user)
    if search_query:
        search_filter = (
            Q(table__table_no__icontains=search_query)
            | Q(waiter__name__icontains=search_query)
            | Q(waiter__username__icontains=search_query)
            | Q(items__item__name__icontains=search_query)
        )
        if search_query.isdigit():
            search_filter |= Q(id=int(search_query))
        orders = orders.filter(search_filter).distinct()
        session_filter = (
            Q(invoice_number__icontains=search_query)
            | Q(table_session__table__table_no__icontains=search_query)
            | Q(finalized_by__name__icontains=search_query)
            | Q(finalized_by__username__icontains=search_query)
            | Q(lines__description__icontains=search_query)
        )
        if search_query.isdigit():
            session_filter |= Q(id=int(search_query))
        session_invoices = session_invoices.filter(session_filter).distinct()
    if order_status != "all":
        orders = orders.filter(status=order_status.upper())
        session_invoices = session_invoices.filter(
            table_session__orders__status=order_status.upper()
        ).distinct()
    if payment_filter == "due":
        orders = orders.exclude(status=AssignOrder.Status.CANCELLED).filter(
            total_amount__gt=F("successful_payment_total")
        )
        session_invoices = session_invoices.filter(
            status=SessionInvoice.Status.FINALIZED,
            grand_total__gt=F("successful_session_payment_total"),
        )
    elif payment_filter == "paid":
        orders = orders.filter(
            total_amount__gt=Decimal("0.00"),
            successful_payment_total__gte=F("total_amount"),
        )
        session_invoices = session_invoices.filter(
            grand_total__gt=Decimal("0.00"),
            successful_session_payment_total__gte=F("grand_total"),
        )

    billing_records = [_legacy_billing_row(order) for order in orders]
    billing_records.extend(_session_invoice_billing_row(invoice) for invoice in session_invoices)
    billing_records.sort(key=lambda row: row["date"], reverse=True)
    paginator = Paginator(billing_records, 15)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dineva/owner/billing/list.html",
        {
            "page_obj": page_obj,
            "billing_records": page_obj.object_list,
            "search_query": search_query,
            "payment_filter": payment_filter,
            "status_filter": order_status,
            "query_params": query_params.urlencode(),
            "has_active_filters": bool(
                search_query or payment_filter != "due" or order_status != "all"
            ),
            "restaurant": request.user.profile.restaurant,
        },
    )


@never_cache
@owner_required
def owner_billing_detail(request, order_id):
    order = _owner_billing_order_or_404(request.user, order_id)
    if request.method == "POST":
        submission_key = request.POST.get("submission_key", "")
        if not _payment_submission_key_is_valid(request, submission_key):
            messages.error(request, "This payment submission has expired or was already processed.")
            return redirect("dineva:owner-billing-detail", order_id=order_id)
    else:
        submission_key = _new_payment_submission_key(request)

    form = OwnerPaymentRecordForm(
        request.POST or None,
        submission_key=submission_key,
    )
    if request.method == "POST" and form.is_valid():
        submission_key = form.cleaned_data["submission_key"]
        try:
            record_owner_order_payment(
                owner=request.user,
                order_id=order_id,
                payment_method=form.cleaned_data["payment_method"],
            )
        except PaymentWorkflowError as exc:
            form.add_error(None, exc)
        else:
            _consume_payment_submission_key(request, submission_key)
            messages.success(request, "Payment recorded.")
            return redirect("dineva:owner-billing-detail", order_id=order_id)

    order_item_rows = [
        {
            "order_item": order_item,
            "line_total": order_item.price * order_item.quantity,
        }
        for order_item in order.items.all()
    ]
    return render(
        request,
        "dineva/owner/billing/detail.html",
        {
            "order": order,
            "order_item_rows": order_item_rows,
            "payments": order.payments.all().order_by("-created_at"),
            "history_entries": order.history_entries.all().order_by("-created_at"),
            "form": form,
            "restaurant": request.user.profile.restaurant,
        },
    )


@never_cache
@owner_required
def owner_billing_invoice_detail(request, invoice_id):
    invoice = _attach_session_invoice_billing_state(
        _owner_session_invoice_or_404(request.user, invoice_id)
    )
    orders = list(invoice.table_session.orders.all().order_by("created_at"))
    history_entries = AssignOrderHistory.objects.filter(
        restaurant=request.user.profile.restaurant,
        assign_order__in=orders,
    ).select_related("user").order_by("-created_at")
    return render(
        request,
        "dineva/owner/billing/invoice_detail.html",
        {
            "invoice": invoice,
            "orders": orders,
            "payments": invoice.payments.all().order_by("-created_at"),
            "delivery_attempts": invoice.delivery_attempts.all().order_by("-created_at"),
            "history_entries": history_entries,
            "restaurant": request.user.profile.restaurant,
        },
    )


def _decimal_zero():
    return Decimal("0.00")


def _money(value):
    return value if value is not None else _decimal_zero()


def _successful_payment_total_subquery():
    return (
        Payment.objects.filter(
            assign_order=OuterRef("pk"),
            payment_status=Payment.Status.SUCCESS,
        )
        .values("assign_order")
        .annotate(total=Sum("amount"))
        .values("total")
    )


def _parse_owner_report_filters(request):
    today = timezone.localdate()
    default_start = today - timedelta(days=29)
    raw_start = request.GET.get("start_date", "").strip()
    raw_end = request.GET.get("end_date", "").strip()
    start_date = parse_date(raw_start) if raw_start else default_start
    end_date = parse_date(raw_end) if raw_end else today
    errors = []
    if raw_start and start_date is None:
        start_date = default_start
        errors.append("The start date was invalid, so the default range is shown.")
    if raw_end and end_date is None:
        end_date = today
        errors.append("The end date was invalid, so the default range is shown.")
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        errors.append("The date range was reversed, so it has been normalized.")
    if (end_date - start_date).days > 366:
        start_date = end_date - timedelta(days=366)
        errors.append("Date ranges are limited to 367 days for responsive reporting.")

    order_type = request.GET.get("type", "all").strip().upper()
    if order_type not in {AssignOrder.OrderType.DINE_IN, AssignOrder.OrderType.TAKEAWAY}:
        order_type = "all"
    status = request.GET.get("status", "all").strip().lower()
    if status not in {"all", "active", "completed", "cancelled"}:
        status = "all"

    current_tz = timezone.get_current_timezone()
    start_at = timezone.make_aware(datetime.combine(start_date, time.min), current_tz)
    end_at = timezone.make_aware(datetime.combine(end_date, time.max), current_tz)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "start_at": start_at,
        "end_at": end_at,
        "order_type": order_type,
        "status": status,
        "errors": errors,
    }


def _report_order_queryset(restaurant, filters):
    orders = AssignOrder.objects.filter(
        restaurant=restaurant,
        table_session__isnull=True,
        order_date__gte=filters["start_at"],
        order_date__lte=filters["end_at"],
    )
    if filters["order_type"] != "all":
        orders = orders.filter(order_type=filters["order_type"])
    if filters["status"] != "all":
        orders = orders.filter(status=filters["status"].upper())
    return orders


def _report_session_invoice_queryset(restaurant, filters):
    invoices = SessionInvoice.objects.filter(
        restaurant=restaurant,
        invoice_date__gte=filters["start_at"],
        invoice_date__lte=filters["end_at"],
    )
    if filters["order_type"] == AssignOrder.OrderType.TAKEAWAY:
        return invoices.none()
    if filters["status"] != "all":
        invoices = invoices.filter(table_session__orders__status=filters["status"].upper()).distinct()
    return invoices


def _report_payment_queryset(restaurant, filters):
    return Payment.objects.filter(
        restaurant=restaurant,
        payment_status=Payment.Status.SUCCESS,
    ).filter(
        Q(paid_at__gte=filters["start_at"], paid_at__lte=filters["end_at"])
        | Q(
            paid_at__isnull=True,
            created_at__gte=filters["start_at"],
            created_at__lte=filters["end_at"],
        )
    )


def _report_session_payment_queryset(restaurant, filters):
    return SessionPayment.objects.filter(
        restaurant=restaurant,
        payment_status=SessionPayment.Status.SUCCESS,
    ).filter(
        Q(paid_at__gte=filters["start_at"], paid_at__lte=filters["end_at"])
        | Q(
            paid_at__isnull=True,
            created_at__gte=filters["start_at"],
            created_at__lte=filters["end_at"],
        )
    )


def _aggregate_count_total(queryset, amount_field):
    return queryset.aggregate(
        count=Count("id"),
        total=Coalesce(
            Sum(amount_field),
            Value(_decimal_zero()),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )


def _merge_money_rows(rows, key_name, count_name, total_name):
    merged = {}
    for row in rows:
        key = row[key_name]
        if key not in merged:
            merged[key] = {key_name: key, count_name: 0, total_name: _decimal_zero()}
        merged[key][count_name] += row[count_name]
        merged[key][total_name] += _money(row[total_name])
    return list(merged.values())


@never_cache
@owner_required
def owner_reports(request):
    restaurant = _owner_restaurant(request.user)
    filters = _parse_owner_report_filters(request)
    legacy_orders = _report_order_queryset(restaurant, filters)
    session_invoices = _report_session_invoice_queryset(restaurant, filters)
    legacy_sales_orders = legacy_orders.exclude(status=AssignOrder.Status.CANCELLED)
    legacy_cancelled_orders = legacy_orders.filter(status=AssignOrder.Status.CANCELLED)
    finalized_invoices = session_invoices.filter(status=SessionInvoice.Status.FINALIZED)
    cancelled_invoices = session_invoices.filter(status=SessionInvoice.Status.CANCELLED)
    legacy_payments = _report_payment_queryset(restaurant, filters)
    session_payments = _report_session_payment_queryset(restaurant, filters)

    legacy_sales = _aggregate_count_total(legacy_sales_orders, "total_amount")
    invoice_sales = finalized_invoices.aggregate(
        count=Count("id"),
        total=Coalesce(
            Sum("grand_total"),
            Value(_decimal_zero()),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
        taxable_total=Coalesce(
            Sum("taxable_value"),
            Value(_decimal_zero()),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
        gst_total=Coalesce(
            Sum("total_gst"),
            Value(_decimal_zero()),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )
    cancelled_legacy = _aggregate_count_total(legacy_cancelled_orders, "total_amount")
    cancelled_session = _aggregate_count_total(cancelled_invoices, "grand_total")
    legacy_payment_summary = _aggregate_count_total(legacy_payments, "amount")
    session_payment_summary = _aggregate_count_total(session_payments, "amount")

    sales_summary = {
        "bill_count": legacy_sales["count"] + invoice_sales["count"],
        "legacy_order_count": legacy_sales["count"],
        "session_invoice_count": invoice_sales["count"],
        "order_total": _money(legacy_sales["total"]) + _money(invoice_sales["total"]),
        "taxable_total": _money(invoice_sales["taxable_total"]),
        "gst_total": _money(invoice_sales["gst_total"]),
    }
    cancelled_summary = {
        "order_count": cancelled_legacy["count"] + cancelled_session["count"],
        "order_total": _money(cancelled_legacy["total"]) + _money(cancelled_session["total"]),
    }
    payment_summary = {
        "payment_count": legacy_payment_summary["count"] + session_payment_summary["count"],
        "payment_total": _money(legacy_payment_summary["total"]) + _money(session_payment_summary["total"]),
    }

    order_status_labels = dict(AssignOrder.Status.choices)
    order_type_labels = dict(AssignOrder.OrderType.choices)
    payment_method_labels = {**dict(Payment.Method.choices), **dict(SessionPayment.Method.choices)}

    status_summary = [
        {
            "label": order_status_labels.get(row["status"], row["status"]),
            "order_count": row["order_count"],
            "order_total": _money(row["order_total"]),
        }
        for row in legacy_orders.values("status")
        .annotate(
            order_count=Count("id"),
            order_total=Coalesce(
                Sum("total_amount"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("status")
    ]
    invoice_status_summary = [
        {
            "label": f"Invoice {dict(SessionInvoice.Status.choices).get(row['status'], row['status'])}",
            "invoice_count": row["invoice_count"],
            "invoice_total": _money(row["invoice_total"]),
        }
        for row in session_invoices.values("status")
        .annotate(
            invoice_count=Count("id"),
            invoice_total=Coalesce(
                Sum("grand_total"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("status")
    ]
    legacy_type_rows = [
        {
            "label": order_type_labels.get(row["order_type"], row["order_type"]),
            "order_count": row["order_count"],
            "order_total": _money(row["order_total"]),
        }
        for row in legacy_orders.values("order_type")
        .annotate(
            order_count=Count("id"),
            order_total=Coalesce(
                Sum("total_amount"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("order_type")
    ]
    session_type_total = finalized_invoices.aggregate(
        order_count=Count("id"),
        order_total=Coalesce(
            Sum("grand_total"),
            Value(_decimal_zero()),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )
    type_summary = _merge_money_rows(
        legacy_type_rows
        + [
            {
                "label": "Dine-in",
                "order_count": session_type_total["order_count"],
                "order_total": _money(session_type_total["order_total"]),
            }
        ],
        "label",
        "order_count",
        "order_total",
    )
    legacy_payment_method_rows = [
        {
            "label": payment_method_labels.get(row["payment_method"], row["payment_method"]),
            "payment_count": row["payment_count"],
            "payment_total": _money(row["payment_total"]),
        }
        for row in legacy_payments.values("payment_method")
        .annotate(
            payment_count=Count("id"),
            payment_total=Coalesce(
                Sum("amount"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("payment_method")
    ]
    session_payment_method_rows = [
        {
            "label": payment_method_labels.get(row["payment_method"], row["payment_method"]),
            "payment_count": row["payment_count"],
            "payment_total": _money(row["payment_total"]),
        }
        for row in session_payments.values("payment_method")
        .annotate(
            payment_count=Count("id"),
            payment_total=Coalesce(
                Sum("amount"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("payment_method")
    ]
    payment_method_summary = _merge_money_rows(
        legacy_payment_method_rows + session_payment_method_rows,
        "label",
        "payment_count",
        "payment_total",
    )

    paid_total_subquery = _successful_payment_total_subquery()
    outstanding_legacy_orders = (
        legacy_orders.exclude(status=AssignOrder.Status.CANCELLED)
        .annotate(
            successful_payment_total=Coalesce(
                Subquery(paid_total_subquery),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .filter(total_amount__gt=F("successful_payment_total"))
        .select_related("table")
        .order_by("-order_date")
    )
    outstanding_invoices = (
        session_invoices.filter(status=SessionInvoice.Status.FINALIZED)
        .annotate(
            successful_session_payment_total=Coalesce(
                Subquery(_session_invoice_payment_total_subquery()),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            )
        )
        .filter(grand_total__gt=F("successful_session_payment_total"))
        .select_related("table_session__table")
        .order_by("-invoice_date")
    )
    outstanding_rows = [
        {
            "label": f"Order #{order.id}",
            "date": order.order_date,
            "type": order.get_order_type_display(),
            "table": order.table.table_no if order.table else "Takeaway",
            "total_amount": order.total_amount,
            "paid_total": order.successful_payment_total,
            "balance_due": order.total_amount - order.successful_payment_total,
            "detail_url": reverse("dineva:owner-billing-detail", args=[order.id]),
        }
        for order in outstanding_legacy_orders
    ]
    outstanding_rows.extend(
        {
            "label": f"Invoice {invoice.invoice_number}",
            "date": invoice.invoice_date,
            "type": "Dine-in session",
            "table": invoice.table_session.table.table_no if invoice.table_session.table_id else "Session",
            "total_amount": invoice.grand_total,
            "paid_total": invoice.successful_session_payment_total,
            "balance_due": invoice.grand_total - invoice.successful_session_payment_total,
            "detail_url": reverse("dineva:owner-billing-invoice-detail", args=[invoice.id]),
        }
        for invoice in outstanding_invoices
    )
    outstanding_rows.sort(key=lambda row: row["date"], reverse=True)
    outstanding_summary = {
        "order_count": len(outstanding_rows),
        "balance_total": sum((row["balance_due"] for row in outstanding_rows), _decimal_zero()),
    }
    outstanding_rows = outstanding_rows[:10]

    current_tz = timezone.get_current_timezone()
    order_daily = {
        row["day"]: row
        for row in legacy_sales_orders.annotate(day=TruncDate("order_date", tzinfo=current_tz))
        .values("day")
        .annotate(
            order_count=Count("id"),
            order_total=Coalesce(
                Sum("total_amount"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("day")
    }
    invoice_daily = {
        row["day"]: row
        for row in finalized_invoices.annotate(day=TruncDate("invoice_date", tzinfo=current_tz))
        .values("day")
        .annotate(
            invoice_count=Count("id"),
            invoice_total=Coalesce(
                Sum("grand_total"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("day")
    }
    payment_daily = {
        row["day"]: row
        for row in legacy_payments.annotate(
            day=TruncDate(Coalesce("paid_at", "created_at"), tzinfo=current_tz)
        )
        .values("day")
        .annotate(
            payment_count=Count("id"),
            payment_total=Coalesce(
                Sum("amount"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("day")
    }
    session_payment_daily = {
        row["day"]: row
        for row in session_payments.annotate(
            day=TruncDate(Coalesce("paid_at", "created_at"), tzinfo=current_tz)
        )
        .values("day")
        .annotate(
            payment_count=Count("id"),
            payment_total=Coalesce(
                Sum("amount"),
                Value(_decimal_zero()),
                output_field=DecimalField(max_digits=12, decimal_places=2),
            ),
        )
        .order_by("day")
    }
    daily_breakdown = []
    cursor = filters["start_date"]
    while cursor <= filters["end_date"]:
        order_row = order_daily.get(cursor, {})
        invoice_row = invoice_daily.get(cursor, {})
        payment_row = payment_daily.get(cursor, {})
        session_payment_row = session_payment_daily.get(cursor, {})
        daily_breakdown.append(
            {
                "day": cursor,
                "order_count": order_row.get("order_count", 0) + invoice_row.get("invoice_count", 0),
                "order_total": _money(order_row.get("order_total")) + _money(invoice_row.get("invoice_total")),
                "payment_count": payment_row.get("payment_count", 0) + session_payment_row.get("payment_count", 0),
                "payment_total": _money(payment_row.get("payment_total")) + _money(session_payment_row.get("payment_total")),
            }
        )
        cursor += timedelta(days=1)

    query_params = request.GET.copy()
    return render(
        request,
        "dineva/owner/reports.html",
        {
            "restaurant": restaurant,
            "filters": filters,
            "query_params": query_params.urlencode(),
            "sales_summary": sales_summary,
            "cancelled_summary": cancelled_summary,
            "payment_summary": payment_summary,
            "status_summary": status_summary,
            "invoice_status_summary": invoice_status_summary,
            "type_summary": type_summary,
            "payment_method_summary": payment_method_summary,
            "outstanding_summary": outstanding_summary,
            "outstanding_rows": outstanding_rows,
            "daily_breakdown": daily_breakdown,
            "has_report_data": bool(
                sales_summary["bill_count"]
                or cancelled_summary["order_count"]
                or payment_summary["payment_count"]
            ),
            "dine_in_type": AssignOrder.OrderType.DINE_IN,
            "takeaway_type": AssignOrder.OrderType.TAKEAWAY,
        },
    )


@never_cache
@waiter_required
def waiter_order_add(request):
    restaurant = request.user.profile.restaurant
    wants_json = _request_wants_json(request)
    if request.method == "POST":
        submission_key = request.POST.get("submission_key", "")
        if not _order_submission_key_is_valid(request, submission_key):
            if wants_json:
                return JsonResponse(
                    {
                        "ok": False,
                        "message": "This order submission has expired or was already processed.",
                        "errors": {
                            "non_field_errors": [
                                "This order submission has expired or was already processed."
                            ],
                            "fields": {},
                        },
                        "new_submission_key": _new_order_submission_key(request),
                    },
                    status=409,
                )
            messages.error(request, "This order submission has expired or was already processed.")
            return redirect("dineva:waiter-order-add")
    else:
        submission_key = _new_order_submission_key(request)
    form = WaiterOrderForm(
        request.POST or None,
        restaurant=restaurant,
        submission_key=submission_key,
    )
    if request.method == "POST" and form.is_valid():
        submission_key = form.cleaned_data["submission_key"]
        try:
            order = create_waiter_order(
                waiter=request.user,
                order_type=form.cleaned_data["order_type"],
                table=form.cleaned_data["table"],
                order_lines=form.cleaned_data["order_lines"],
                idempotency_key=submission_key,
            )
        except OrderWorkflowError as exc:
            form.add_error(None, exc)
        else:
            _consume_order_submission_key(request, submission_key)
            if wants_json:
                order = _waiter_order_or_404(request.user, order.id)
                return JsonResponse(
                    {
                        "ok": True,
                        "message": "Order created.",
                        "order": _waiter_order_payload(order),
                        "list_url": reverse("dineva:waiter-order-list"),
                        "new_submission_key": _new_order_submission_key(request),
                    },
                    status=201,
                )
            messages.success(request, "Order created.")
            return redirect("dineva:waiter-order-list")
    if request.method == "POST" and wants_json:
        return JsonResponse(
            {
                "ok": False,
                "message": "Review the highlighted fields and try again.",
                "errors": _form_error_payload(form),
            },
            status=400,
        )
    return render(
        request,
        "dineva/waiter/orders/form.html",
        {"form": form, "restaurant": restaurant},
    )


@never_cache
@kitchen_required
def kitchen_order_queue(request):
    status = request.GET.get("status", "pending").strip().lower()
    if status not in {"all", "pending", "preparing", "ready"}:
        status = "pending"
    order_items = (
        OrderItem.objects.filter(
            assign_order__restaurant=request.user.profile.restaurant,
            assign_order__status=AssignOrder.Status.ACTIVE,
        )
        .select_related("assign_order", "assign_order__table", "item")
        .order_by("created_at")
    )
    if status != "all":
        order_items = order_items.filter(status=status.upper())
    paginator = Paginator(order_items, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dineva/kitchen/queue.html",
        {
            "page_obj": page_obj,
            "order_items": page_obj.object_list,
            "status_filter": status,
            "query_params": query_params.urlencode(),
            "preparing_status": OrderItem.Status.PREPARING,
            "ready_status": OrderItem.Status.READY,
            "restaurant": request.user.profile.restaurant,
        },
    )


@require_POST
@kitchen_required
def kitchen_order_item_status(request, order_item_id, status):
    target_status = status.strip().upper()
    try:
        transition_kitchen_order_item(
            actor=request.user,
            order_item_id=order_item_id,
            target_status=target_status,
        )
    except OrderWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Kitchen item status updated.")
    return redirect(_safe_next_url(request, reverse("dineva:kitchen-order-queue")))


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


def _dashboard_context(user):
    restaurants = list(_restaurant_queryset())
    employee_profiles = list(
        _profile_queryset()
        .filter(
            role__in=[
                UserProfile.Role.OWNER,
                UserProfile.Role.WAITER,
                UserProfile.Role.KITCHEN_STAFF,
            ]
        )
        .order_by("user__name")
    )
    owners = [profile for profile in employee_profiles if profile.role == UserProfile.Role.OWNER]
    waiters = [profile for profile in employee_profiles if profile.role == UserProfile.Role.WAITER]
    kitchen_staff = [
        profile for profile in employee_profiles if profile.role == UserProfile.Role.KITCHEN_STAFF
    ]
    return {
        "restaurants": restaurants,
        "owners": owners,
        "waiters": waiters,
        "kitchen_staff": kitchen_staff,
        "superadmin_profile": get_profile(user),
        "restaurant_count": len(restaurants),
        "owner_count": len(owners),
        "waiter_count": len(waiters),
        "kitchen_count": len(kitchen_staff),
        "pending_tax_profile_count": RestaurantTaxProfile.objects.filter(
            configuration_status=RestaurantTaxProfile.ConfigurationStatus.PENDING_REVIEW
        ).count(),
    }


def _owner_dashboard_context(user):
    owner_profile = get_profile(user)
    restaurant = owner_profile.restaurant
    today = timezone.localdate()
    staff_profiles = UserProfile.objects.filter(restaurant=restaurant)
    active_waiter_count = staff_profiles.filter(
        role=UserProfile.Role.WAITER,
        user__is_active=True,
    ).count()
    active_kitchen_count = staff_profiles.filter(
        role=UserProfile.Role.KITCHEN_STAFF,
        user__is_active=True,
    ).count()
    menu_item_count = Item.objects.filter(
        restaurant=restaurant,
        is_active=True,
    ).count()
    table_count = RestaurantTable.objects.filter(restaurant=restaurant).count()
    today_invoice_summary = SessionInvoice.objects.filter(
        restaurant=restaurant,
        status=SessionInvoice.Status.FINALIZED,
        invoice_date__date=today,
    ).aggregate(
        count=Count("id"),
        total=Coalesce(
            Sum("grand_total"),
            Value(_decimal_zero()),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )
    today_legacy_payment_summary = Payment.objects.filter(
        restaurant=restaurant,
        payment_status=Payment.Status.SUCCESS,
    ).filter(Q(paid_at__date=today) | Q(paid_at__isnull=True, created_at__date=today)).aggregate(
        count=Count("id"),
        total=Coalesce(
            Sum("amount"),
            Value(_decimal_zero()),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )
    today_session_payment_summary = SessionPayment.objects.filter(
        restaurant=restaurant,
        payment_status=SessionPayment.Status.SUCCESS,
    ).filter(Q(paid_at__date=today) | Q(paid_at__isnull=True, created_at__date=today)).aggregate(
        count=Count("id"),
        total=Coalesce(
            Sum("amount"),
            Value(_decimal_zero()),
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
    )
    setup_steps = [
        {
            "label": "Staff team",
            "complete": active_waiter_count > 0 and active_kitchen_count > 0,
            "detail": "Add at least one active waiter and one active kitchen staff member.",
        },
        {
            "label": "Menu",
            "complete": menu_item_count > 0,
            "detail": "Add active menu items so waiters can use them in later order workflows.",
        },
        {
            "label": "Tables",
            "complete": table_count > 0,
            "detail": "Add active dining tables for dine-in order assignment.",
        },
    ]
    return {
        "owner_profile": owner_profile,
        "restaurant": restaurant,
        "active_waiter_count": active_waiter_count,
        "active_kitchen_count": active_kitchen_count,
        "menu_item_count": menu_item_count,
        "table_count": table_count,
        "today_invoice_count": today_invoice_summary["count"],
        "today_invoice_total": _money(today_invoice_summary["total"]),
        "today_payment_count": (
            today_legacy_payment_summary["count"] + today_session_payment_summary["count"]
        ),
        "today_payment_total": (
            _money(today_legacy_payment_summary["total"])
            + _money(today_session_payment_summary["total"])
        ),
        "setup_steps": setup_steps,
    }


def _restaurant_queryset():
    return Restaurant.objects.annotate(
        owner_count=Count(
            "user_profiles",
            filter=Q(user_profiles__role=UserProfile.Role.OWNER),
            distinct=True,
        ),
        waiter_count=Count(
            "user_profiles",
            filter=Q(user_profiles__role=UserProfile.Role.WAITER),
            distinct=True,
        ),
        kitchen_staff_count=Count(
            "user_profiles",
            filter=Q(user_profiles__role=UserProfile.Role.KITCHEN_STAFF),
            distinct=True,
        ),
    ).order_by("name")


def _post_login_redirect(user):
    profile = get_profile(user)
    if profile and profile.role == UserProfile.Role.SUPERADMIN:
        return redirect("dineva:dashboard")
    if profile and profile.role == UserProfile.Role.OWNER:
        return redirect("dineva:owner-dashboard")
    if profile and profile.role == UserProfile.Role.WAITER:
        return redirect("dineva:waiter-dashboard")
    if profile and profile.role == UserProfile.Role.KITCHEN_STAFF:
        return redirect("dineva:kitchen-order-queue")
    return redirect("dineva:home")


@never_cache
@require_POST
def logout_view(request):
    close_impersonation_without_restore(
        request,
        ImpersonationAudit.EndReason.LOGOUT,
    )
    logout(request)
    return redirect("dineva:login")
