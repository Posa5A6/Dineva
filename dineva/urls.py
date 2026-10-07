from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.urls import path, reverse_lazy

from . import views
from .forms import AccountSetPasswordForm, DinevaPasswordResetForm

app_name = "dineva"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("restaurants/", views.restaurant_list, name="restaurants"),
    path("restaurants/add/", views.restaurant_add, name="restaurant-add"),
    path("restaurants/<int:restaurant_id>/edit/", views.restaurant_edit, name="restaurant-edit"),
    path("restaurants/<int:restaurant_id>/status/", views.restaurant_status, name="restaurant-status"),
    path("owners/add/", views.owner_add, name="owner-add"),
    path("owners/", views.owner_list, name="owner-list"),
    path("staff/add/", views.staff_add, name="staff-add"),
    path("staff/", views.staff_list, name="staff-list"),
    path("employees/<int:user_id>/", views.employee_detail, name="employee-detail"),
    path("employees/<int:user_id>/edit/", views.employee_edit, name="employee-edit"),
    path("employees/<int:user_id>/status/", views.employee_status, name="employee-status"),
    path("employees/<int:user_id>/photo/", views.employee_photo, name="employee-photo"),
    path("employees/<int:user_id>/id-proof/", views.id_proof_download, name="id-proof-download"),
    path("owner/staff/", views.owner_staff_list, name="owner-staff-list"),
    path("owner/staff/add/", views.owner_staff_add, name="owner-staff-add"),
    path("owner/staff/<int:user_id>/", views.owner_staff_detail, name="owner-staff-detail"),
    path("owner/staff/<int:user_id>/photo/", views.owner_staff_photo, name="owner-staff-photo"),
    path("owner/staff/<int:user_id>/status/", views.owner_staff_status, name="owner-staff-status"),
    path(
        "forgot-password/",
        PasswordResetView.as_view(
            form_class=DinevaPasswordResetForm,
            template_name="dineva/password_reset_form.html",
            email_template_name="dineva/emails/password_reset.txt",
            subject_template_name="dineva/emails/password_reset_subject.txt",
            success_url=reverse_lazy("dineva:password-reset-done"),
        ),
        name="password-reset",
    ),
    path(
        "forgot-password/sent/",
        PasswordResetDoneView.as_view(template_name="dineva/password_reset_done.html"),
        name="password-reset-done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        PasswordResetConfirmView.as_view(
            form_class=AccountSetPasswordForm,
            template_name="dineva/password_reset_confirm.html",
            success_url=reverse_lazy("dineva:password-reset-complete"),
        ),
        name="password-reset-confirm",
    ),
    path(
        "reset/complete/",
        PasswordResetCompleteView.as_view(template_name="dineva/password_reset_complete.html"),
        name="password-reset-complete",
    ),
]
