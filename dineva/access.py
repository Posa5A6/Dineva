from django.core.exceptions import ObjectDoesNotExist

from .models import UserProfile


def get_profile(user):
    if not user.is_authenticated:
        return None
    try:
        return user.profile
    except ObjectDoesNotExist:
        return None


def is_superadmin(user):
    profile = get_profile(user)
    return bool(
        user.is_authenticated
        and user.is_active
        and user.is_superuser
        and profile
        and profile.role == UserProfile.Role.SUPERADMIN
        and profile.restaurant_id is None
    )


def is_owner(user):
    profile = get_profile(user)
    return bool(
        user.is_authenticated
        and user.is_active
        and not user.is_superuser
        and profile
        and profile.role == UserProfile.Role.OWNER
        and profile.restaurant_id is not None
        and profile.restaurant.is_active
    )


def account_can_authenticate(user):
    if not user or not user.is_active:
        return False

    profile = get_profile(user)
    if profile is None:
        return False
    if profile.role == UserProfile.Role.SUPERADMIN:
        return is_superadmin(user)
    if user.is_superuser or profile.restaurant_id is None:
        return False
    if not profile.restaurant.is_active:
        return False
    if profile.role == UserProfile.Role.OWNER:
        return True
    if profile.role not in {
        UserProfile.Role.WAITER,
        UserProfile.Role.KITCHEN_STAFF,
    }:
        return False
    return UserProfile.objects.filter(
        restaurant_id=profile.restaurant_id,
        role=UserProfile.Role.OWNER,
        user__is_active=True,
    ).exists()


def owner_can_manage_profile(owner, target_profile):
    owner_profile = get_profile(owner)
    return bool(
        is_owner(owner)
        and target_profile.restaurant_id == owner_profile.restaurant_id
        and target_profile.role
        in {UserProfile.Role.WAITER, UserProfile.Role.KITCHEN_STAFF}
    )
