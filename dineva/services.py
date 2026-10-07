import secrets
import string
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.db import DEFAULT_DB_ALIAS, IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.text import slugify

from .models import (
    RESTAURANT_ROLE_PUBLIC_ID_PREFIXES,
    EmployeeIDProof,
    User,
    UserProfile,
    UserPublicIdSequence,
)
from .validators import inspect_id_proof, inspect_profile_photo


class RegistrationEmailError(Exception):
    pass


class DuplicateAccountError(Exception):
    pass


def normalize_username_base(name):
    normalized = unicodedata.normalize("NFKC", name or "").strip().lower()
    base = slugify(normalized, allow_unicode=True).replace("-", ".").strip(".")
    return base or "dineva.user"


def generate_password(length=18):
    if length < 16:
        raise ValueError("Generated passwords must contain at least 16 characters.")
    groups = [
        string.ascii_uppercase,
        string.ascii_lowercase,
        string.digits,
        "!@#$%^&*()-_=+",
    ]
    characters = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    characters.extend(secrets.choice(alphabet) for _ in range(length - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def allocate_public_id(role, using=DEFAULT_DB_ALIAS):
    prefix = RESTAURANT_ROLE_PUBLIC_ID_PREFIXES.get(role)
    if prefix is None:
        raise ValueError("Public IDs are available only for restaurant roles.")
    sequence = (
        UserPublicIdSequence.objects.using(using)
        .select_for_update()
        .get(role=role)
    )
    public_id = f"{prefix}-{sequence.next_value:05d}"
    sequence.next_value += 1
    sequence.save(using=using, update_fields=["next_value"])
    return public_id


def _create_user_with_generated_username(*, name, email, password, using):
    normalized_email = User.objects.normalize_email(email).strip().lower()
    if User.objects.using(using).filter(email__iexact=normalized_email).exists():
        raise DuplicateAccountError("A user with this email already exists.")

    base = normalize_username_base(name)
    for suffix in range(10000):
        suffix_text = "" if suffix == 0 else str(suffix)
        candidate = f"{base[:150 - len(suffix_text)]}{suffix_text}"
        user = User(username=candidate, email=normalized_email, name=name.strip())
        user.set_password(password)
        try:
            with transaction.atomic(using=using):
                user.full_clean(validate_unique=False)
                user.save(using=using, force_insert=True)
        except IntegrityError:
            if User.objects.using(using).filter(username__iexact=candidate).exists():
                continue
            raise DuplicateAccountError("A user with this email already exists.")
        return user
    raise RuntimeError("Unable to allocate a unique username.")


def send_templated_email(*, subject, template_name, context, recipient):
    body = render_to_string(template_name, context)
    accepted = send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
        fail_silently=False,
    )
    if accepted != 1:
        raise RegistrationEmailError("The email server did not accept the message.")
    return accepted


def send_welcome_email(*, user, password):
    profile = user.profile
    public_id_labels = {
        UserProfile.Role.OWNER: "Owner ID",
        UserProfile.Role.WAITER: "Waiter ID",
        UserProfile.Role.KITCHEN_STAFF: "Kitchen Staff ID",
    }
    return send_templated_email(
        subject="Welcome to Dineva",
        template_name="dineva/emails/welcome.txt",
        context={
            "user": user,
            "profile": profile,
            "restaurant": profile.restaurant,
            "role_label": profile.get_role_display(),
            "public_id_label": public_id_labels[profile.role],
            "generated_password": password,
            "login_url": f"{settings.DINEVA_PUBLIC_BASE_URL}{reverse('dineva:login')}",
        },
        recipient=user.email,
    )


def create_employee(
    *,
    name,
    email,
    contact,
    address,
    photo,
    id_proof_type,
    id_proof_file,
    restaurant,
    role,
):
    allowed_roles = {
        UserProfile.Role.OWNER,
        UserProfile.Role.WAITER,
        UserProfile.Role.KITCHEN_STAFF,
    }
    if role not in allowed_roles:
        raise ValueError("Unsupported restaurant role.")
    if not restaurant.is_active:
        raise ValidationError("Employees require an active restaurant.")

    inspect_profile_photo(photo)
    proof_metadata = inspect_id_proof(id_proof_file)
    password = generate_password()
    created_files = []
    using = DEFAULT_DB_ALIAS

    try:
        with transaction.atomic(using=using):
            active_restaurant = type(restaurant).objects.select_for_update().get(
                pk=restaurant.pk,
                is_active=True,
            )
            user = _create_user_with_generated_username(
                name=name,
                email=email,
                password=password,
                using=using,
            )
            profile = UserProfile(
                user=user,
                role=role,
                restaurant=active_restaurant,
                public_id=allocate_public_id(role, using),
                contact=contact.strip(),
                address=address.strip(),
                photo=photo,
            )
            profile.full_clean()
            profile.save(using=using)
            created_files.append((profile.photo.storage, profile.photo.name))

            proof = EmployeeIDProof(
                profile=profile,
                proof_type=id_proof_type,
                file=id_proof_file,
                **proof_metadata,
            )
            proof.full_clean()
            proof.save(using=using)
            created_files.append((proof.file.storage, proof.file.name))

            # SMTP acceptance and the following MySQL commit cannot be atomic.
            # A post-acceptance commit failure remains an unavoidable residual window.
            send_welcome_email(user=user, password=password)
        return user
    except Exception:
        for storage, name in reversed(created_files):
            if name:
                storage.delete(name)
        raise


def replace_private_file(instance, field_name, new_file, metadata=None):
    field = getattr(instance, field_name)
    old_storage = field.storage
    old_name = field.name
    new_name = None
    try:
        with transaction.atomic():
            setattr(instance, field_name, new_file)
            if metadata:
                for key, value in metadata.items():
                    setattr(instance, key, value)
            instance.full_clean()
            instance.save()
            new_field = getattr(instance, field_name)
            new_name = new_field.name
            if old_name and old_name != new_name:
                transaction.on_commit(lambda: old_storage.delete(old_name))
    except Exception:
        if new_name:
            getattr(instance, field_name).storage.delete(new_name)
        raise
    return instance


def update_employee_profile(*, profile, data):
    user = profile.user
    proof = profile.id_proof
    new_photo = data.get("photo")
    new_proof_file = data.get("id_proof_file")
    proof_metadata = inspect_id_proof(new_proof_file) if new_proof_file else None
    old_files = []
    new_files = []

    try:
        with transaction.atomic():
            user.name = data["name"].strip()
            user.email = User.objects.normalize_email(data["email"]).strip().lower()
            user.full_clean()
            user.save(update_fields=["name", "email", "updated_at"])

            profile.contact = data["contact"].strip()
            profile.address = data["address"].strip()
            if new_photo:
                inspect_profile_photo(new_photo)
                old_files.append((profile.photo.storage, profile.photo.name))
                profile.photo = new_photo
            profile.full_clean()
            profile.save(update_fields=["contact", "address", "photo", "updated_at"])
            if new_photo:
                new_files.append((profile.photo.storage, profile.photo.name))

            proof.proof_type = data["id_proof_type"]
            if new_proof_file:
                old_files.append((proof.file.storage, proof.file.name))
                proof.file = new_proof_file
                for key, value in proof_metadata.items():
                    setattr(proof, key, value)
            proof.full_clean()
            proof.save()
            if new_proof_file:
                new_files.append((proof.file.storage, proof.file.name))

            for storage, name in old_files:
                if name:
                    transaction.on_commit(lambda s=storage, n=name: s.delete(n))
    except Exception:
        for storage, name in new_files:
            if name:
                storage.delete(name)
        raise
    return profile


def update_staff_photo(*, profile, photo):
    inspect_profile_photo(photo)
    return replace_private_file(profile, "photo", photo)
