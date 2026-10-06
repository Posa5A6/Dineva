import django.contrib.auth.validators
from django.db import migrations, models
from django.utils.text import slugify


ROLE_SUFFIXES = {
    "OWNER": "owner",
    "WAITER": "waiter",
    "KITCHEN_STAFF": "kitchen",
}


def backfill_usernames(apps, schema_editor):
    User = apps.get_model("dineva", "User")
    using = schema_editor.connection.alias

    for user in User.objects.using(using).filter(username__isnull=True).order_by(
        "created_at", "pk"
    ):
        name_slug = slugify(user.name).replace("-", ".") or "dineva.user"
        suffix = ROLE_SUFFIXES.get(user.role)
        base = name_slug if suffix is None else f"{name_slug}.{suffix}"
        base = base[:150]
        candidate = base
        collision_number = 2

        while User.objects.using(using).filter(
            username__iexact=candidate
        ).exists():
            marker = f".{collision_number}"
            candidate = f"{base[:150 - len(marker)]}{marker}"
            collision_number += 1

        user.username = candidate
        user.save(using=using, update_fields=["username"])


def clear_usernames(apps, schema_editor):
    User = apps.get_model("dineva", "User")
    User.objects.using(schema_editor.connection.alias).update(username=None)


class Migration(migrations.Migration):
    dependencies = [
        ("dineva", "0004_userpublicidsequence_user_public_id_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="username",
            field=models.CharField(
                max_length=150,
                null=True,
                unique=True,
                validators=[
                    django.contrib.auth.validators.UnicodeUsernameValidator()
                ],
            ),
        ),
        migrations.RunPython(backfill_usernames, clear_usernames),
        migrations.AlterField(
            model_name="user",
            name="username",
            field=models.CharField(
                max_length=150,
                unique=True,
                validators=[
                    django.contrib.auth.validators.UnicodeUsernameValidator()
                ],
            ),
        ),
    ]
