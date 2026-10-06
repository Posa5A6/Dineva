from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import User


class AccountSetupEmailError(Exception):
    def __init__(self, user, stage):
        super().__init__(
            f"The account was created, but the {stage} email could not be sent."
        )
        self.user = user
        self.stage = stage


def send_templated_email(*, subject, template_name, context, recipient):
    body = render_to_string(template_name, context)
    return send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        fail_silently=False,
    )


def send_welcome_email(*, user):
    public_id_labels = {
        User.Role.OWNER: "Owner ID",
        User.Role.WAITER: "Waiter ID",
        User.Role.KITCHEN_STAFF: "Kitchen Staff ID",
    }
    return send_templated_email(
        subject="Welcome to Dineva",
        template_name="dineva/emails/welcome.txt",
        context={
            "user": user,
            "restaurant": user.restaurant,
            "role_label": user.get_role_display(),
            "public_id_label": public_id_labels[user.role],
        },
        recipient=user.email,
    )


def send_account_setup_email(*, request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    setup_path = reverse(
        "dineva:account-setup",
        kwargs={"uidb64": uid, "token": token},
    )
    setup_url = request.build_absolute_uri(setup_path)
    timeout_days = max(1, settings.PASSWORD_RESET_TIMEOUT // 86400)
    return send_templated_email(
        subject="Set up your Dineva account",
        template_name="dineva/emails/account_setup.txt",
        context={
            "user": user,
            "restaurant": user.restaurant,
            "setup_url": setup_url,
            "timeout_days": timeout_days,
        },
        recipient=user.email,
    )


def create_restaurant_user(*, request, username, name, email, restaurant, role):
    allowed_roles = {
        User.Role.OWNER,
        User.Role.WAITER,
        User.Role.KITCHEN_STAFF,
    }
    if role not in allowed_roles:
        raise ValueError("Unsupported restaurant user role.")
    if not restaurant.is_active:
        raise ValueError("Restaurant users require an active restaurant.")

    with transaction.atomic():
        user = User.objects.create_user(
            username=username,
            email=email,
            name=name,
            password=None,
            role=role,
            restaurant=restaurant,
        )

    for stage, send_email in (
        ("welcome", lambda: send_welcome_email(user=user)),
        ("password setup", lambda: send_account_setup_email(request=request, user=user)),
    ):
        try:
            send_email()
        except Exception as exc:
            raise AccountSetupEmailError(user=user, stage=stage) from exc

    return user
