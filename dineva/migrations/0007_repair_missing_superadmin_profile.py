from django.db import migrations


def repair_missing_superadmin_profile(apps, schema_editor):
    User = apps.get_model("dineva", "User")
    UserProfile = apps.get_model("dineva", "UserProfile")
    using = schema_editor.connection.alias

    for user in User.objects.using(using).filter(profile__isnull=True).iterator():
        if not user.is_superuser:
            raise RuntimeError(
                "Profile repair aborted: a non-superuser account is missing its "
                "employee profile and cannot be reconstructed safely."
            )
        UserProfile.objects.using(using).create(
            user_id=user.pk,
            role="SUPERADMIN",
            restaurant_id=None,
            public_id=None,
        )


def remove_repaired_superadmin_profiles(apps, schema_editor):
    UserProfile = apps.get_model("dineva", "UserProfile")
    using = schema_editor.connection.alias
    UserProfile.objects.using(using).filter(
        role="SUPERADMIN",
        restaurant_id__isnull=True,
        public_id__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dineva", "0006_userprofile_remove_user_dineva_user_role_scope_valid_and_more"),
    ]

    operations = [
        migrations.RunPython(
            repair_missing_superadmin_profile,
            remove_repaired_superadmin_profiles,
        ),
    ]
