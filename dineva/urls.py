from django.contrib.auth.views import PasswordResetConfirmView
from django.urls import path, reverse_lazy

from . import views
from .forms import AccountSetPasswordForm

app_name = "dineva"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("restaurants/", views.restaurant_list, name="restaurants"),
    path("restaurants/add/", views.restaurant_add, name="restaurant-add"),
    path("owners/add/", views.owner_add, name="owner-add"),
    path("staff/add/", views.staff_add, name="staff-add"),
    path(
        "account/setup/<uidb64>/<token>/",
        PasswordResetConfirmView.as_view(
            form_class=AccountSetPasswordForm,
            template_name="dineva/account_setup.html",
            success_url=reverse_lazy("dineva:account-setup-success"),
        ),
        name="account-setup",
    ),
    path(
        "account/setup/success/",
        views.account_setup_success,
        name="account-setup-success",
    ),
]
