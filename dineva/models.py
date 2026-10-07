from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, models, transaction
from django.db.models import Q
from django.utils import timezone

from .storage import (
    employee_id_proof_upload_to,
    employee_photo_upload_to,
    get_private_storage,
)
from .validators import validate_id_proof_file, validate_profile_photo


RESTAURANT_ROLE_PUBLIC_ID_PREFIXES = {
    "OWNER": "OWN",
    "WAITER": "WTR",
    "KITCHEN_STAFF": "KST",
}


class UserPublicIdSequence(models.Model):
    role = models.CharField(
        max_length=20,
        primary_key=True,
        choices=[
            ("OWNER", "Owner"),
            ("WAITER", "Waiter"),
            ("KITCHEN_STAFF", "Kitchen Staff"),
        ],
    )
    next_value = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "user_public_id_sequences"

    def __str__(self):
        return f"{self.role}: {self.next_value}"


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, username, email, name, password, **extra_fields):
        if not username:
            raise ValueError("Users must have a username.")
        if not email:
            raise ValueError("Users must have an email address.")
        if not name or not name.strip():
            raise ValueError("Users must have a name.")

        username = self.model.normalize_username(username).strip()
        self.model.username_validator(username)
        email = self.normalize_email(email).strip().lower()
        using = self._db or DEFAULT_DB_ALIAS
        user = self.model(
            username=username,
            email=email,
            name=name.strip(),
            **extra_fields,
        )
        user.set_password(password)
        user.clean()
        user.save(using=using)
        return user

    def create_user(self, username, email, name, password=None, **extra_fields):
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_superuser", False)

        if extra_fields["is_superuser"]:
            raise ValueError("Regular users cannot have superuser privileges.")

        return self._create_user(username, email, name, password, **extra_fields)

    def create_superuser(self, username, email, name, password=None, **extra_fields):
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields["is_superuser"] is not True:
            raise ValueError("A superuser must have is_superuser=True.")

        using = self._db or DEFAULT_DB_ALIAS
        with transaction.atomic(using=using):
            user = self._create_user(username, email, name, password, **extra_fields)
            UserProfile.objects.using(using).create(
                user=user,
                role=UserProfile.Role.SUPERADMIN,
            )
        return user


class Restaurant(models.Model):
    name = models.CharField(max_length=150)
    address = models.TextField()
    contact = models.CharField(max_length=20)
    email = models.EmailField()
    logo = models.ImageField(upload_to="restaurants/logos/", blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "restaurants"

    def __str__(self):
        return self.name


class User(AbstractBaseUser, PermissionsMixin):
    username_validator = UnicodeUsernameValidator()

    name = models.CharField(max_length=150)
    username = models.CharField(
        max_length=150,
        unique=True,
        validators=[username_validator],
    )
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "username"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["email", "name"]

    class Meta:
        db_table = "users"

    def clean(self):
        super().clean()
        self.email = (
            self.__class__.objects.normalize_email(self.email or "").strip().lower()
        )

    def save(self, *args, **kwargs):
        if self.pk:
            using = kwargs.get("using") or self._state.db or DEFAULT_DB_ALIAS
            stored_identity = (
                type(self).objects.using(using)
                .filter(pk=self.pk)
                .values_list("username", flat=True)
                .first()
            )
            if stored_identity is not None:
                if stored_identity != self.username:
                    raise ValidationError(
                        {"username": "A user's username cannot be changed."}
                    )
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class UserProfile(models.Model):
    class Role(models.TextChoices):
        SUPERADMIN = "SUPERADMIN", "Super Admin"
        OWNER = "OWNER", "Owner"
        KITCHEN_STAFF = "KITCHEN_STAFF", "Kitchen Staff"
        WAITER = "WAITER", "Waiter"

    user = models.OneToOneField(
        User,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="profile",
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="user_profiles",
    )
    public_id = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )
    contact = models.CharField(max_length=20, null=True, blank=True)
    address = models.TextField(null=True, blank=True)
    photo = models.ImageField(
        upload_to=employee_photo_upload_to,
        storage=get_private_storage,
        validators=[validate_profile_photo],
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "user_profiles"
        indexes = [
            models.Index(fields=["restaurant", "role"], name="profiles_rest_role_idx")
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        role="SUPERADMIN",
                        restaurant_id__isnull=True,
                        public_id__isnull=True,
                    )
                    | Q(
                        role__in=["OWNER", "KITCHEN_STAFF", "WAITER"],
                        restaurant_id__isnull=False,
                        public_id__isnull=False,
                        contact__isnull=False,
                        address__isnull=False,
                        photo__isnull=False,
                    )
                ),
                name="profiles_role_scope_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(role="SUPERADMIN", public_id__isnull=True)
                    | Q(role="OWNER", public_id__startswith="OWN-")
                    | Q(role="WAITER", public_id__startswith="WTR-")
                    | Q(role="KITCHEN_STAFF", public_id__startswith="KST-")
                ),
                name="profiles_public_id_matches_role",
            ),
        ]

    def clean(self):
        super().clean()
        if self.role == self.Role.SUPERADMIN:
            if self.restaurant_id is not None or self.public_id is not None:
                raise ValidationError("A SUPERADMIN must remain platform-level.")
            if not self.user.is_superuser:
                raise ValidationError({"role": "A SUPERADMIN must be a superuser."})
            return

        expected_prefix = RESTAURANT_ROLE_PUBLIC_ID_PREFIXES.get(self.role)
        errors = {}
        if self.user.is_superuser:
            errors["role"] = "Restaurant users cannot have superuser privileges."
        if self.restaurant_id is None:
            errors["restaurant"] = "Restaurant users require a restaurant."
        if not self.public_id or not self.public_id.startswith(f"{expected_prefix}-"):
            errors["public_id"] = "The public ID must match the user's role."
        for field in ("contact", "address", "photo"):
            if not getattr(self, field):
                errors[field] = "This field is required for restaurant users."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            using = kwargs.get("using") or self._state.db or DEFAULT_DB_ALIAS
            stored = (
                type(self).objects.using(using)
                .filter(pk=self.pk)
                .values("role", "restaurant_id", "public_id")
                .first()
            )
            if stored:
                immutable = {
                    field: "This profile identity field cannot be changed."
                    for field in ("role", "restaurant_id", "public_id")
                    if stored[field] != getattr(self, field)
                }
                if immutable:
                    raise ValidationError(immutable)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class EmployeeIDProof(models.Model):
    class ProofType(models.TextChoices):
        AADHAAR = "AADHAAR", "Aadhaar"
        PAN = "PAN", "PAN"
        DRIVING_LICENSE = "DRIVING_LICENSE", "Driving License"
        PASSPORT = "PASSPORT", "Passport"
        VOTER_ID = "VOTER_ID", "Voter ID"
        OTHER = "OTHER_GOVERNMENT_ID", "Other Government ID"

    profile = models.OneToOneField(
        UserProfile,
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="id_proof",
    )
    proof_type = models.CharField(max_length=24, choices=ProofType.choices)
    file = models.FileField(
        upload_to=employee_id_proof_upload_to,
        storage=get_private_storage,
        validators=[validate_id_proof_file],
    )
    mime_type = models.CharField(max_length=50, editable=False)
    size_bytes = models.PositiveIntegerField(editable=False)
    sha256 = models.CharField(max_length=64, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "employee_id_proofs"

    def __str__(self):
        return f"ID proof for {self.profile.user.username}"


class RestaurantTable(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        OCCUPIED = "OCCUPIED", "Occupied"
        RESERVED = "RESERVED", "Reserved"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="dining_tables",
    )
    table_no = models.CharField(max_length=20)
    capacity = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.AVAILABLE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tables"
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "table_no"],
                name="tables_restaurant_table_no_unique",
            ),
            models.CheckConstraint(
                condition=Q(capacity__gt=0),
                name="tables_capacity_positive",
            ),
        ]

    def __str__(self):
        return f"{self.restaurant.name} - Table {self.table_no}"


class Item(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="items",
    )
    category = models.CharField(max_length=100)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    image = models.ImageField(upload_to="items/", blank=True, null=True)
    is_available = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "items"
        constraints = [
            models.CheckConstraint(
                condition=Q(price__gte=0),
                name="items_price_non_negative",
            )
        ]

    def __str__(self):
        return self.name


class Customer(models.Model):
    name = models.CharField(max_length=150)
    mobile = models.CharField(max_length=20, unique=True)
    email = models.EmailField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customers"

    def __str__(self):
        return f"{self.name} ({self.mobile})"


class AssignOrder(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="assign_orders",
    )
    table = models.ForeignKey(
        RestaurantTable,
        on_delete=models.PROTECT,
        related_name="assign_orders",
    )
    waiter = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="waiter_orders",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assign_orders",
    )
    order_date = models.DateTimeField(default=timezone.now)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "assign_orders"

    def __str__(self):
        return f"Order {self.pk or 'new'} - Table {self.table.table_no}"


class AssignOrderHistory(models.Model):
    class Action(models.TextChoices):
        TABLE_OCCUPIED = "TABLE_OCCUPIED", "Table Occupied"
        ITEM_ADDED = "ITEM_ADDED", "Item Added"
        ITEM_CANCELLED = "ITEM_CANCELLED", "Item Cancelled"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        SERVED = "SERVED", "Served"
        CUSTOMER_ADDED = "CUSTOMER_ADDED", "Customer Added"
        PAYMENT_INITIATED = "PAYMENT_INITIATED", "Payment Initiated"
        PAYMENT_COMPLETED = "PAYMENT_COMPLETED", "Payment Completed"
        ORDER_COMPLETED = "ORDER_COMPLETED", "Order Completed"

    assign_order = models.ForeignKey(
        AssignOrder,
        on_delete=models.CASCADE,
        related_name="history_entries",
    )
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="order_histories",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="order_history_entries",
    )
    action = models.CharField(max_length=30, choices=Action.choices)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "assign_order_histories"

    def __str__(self):
        return f"{self.assign_order_id} - {self.action}"


class OrderItem(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PREPARING = "PREPARING", "Preparing"
        READY = "READY", "Ready"
        SERVED = "SERVED", "Served"
        CANCELLED = "CANCELLED", "Cancelled"

    assign_order = models.ForeignKey(
        AssignOrder,
        on_delete=models.CASCADE,
        related_name="items",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.PROTECT,
        related_name="order_items",
    )
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "order_items"
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="order_items_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(price__gte=0),
                name="order_items_price_non_negative",
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.item.name}"


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        ONLINE = "ONLINE", "Online"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"
        REFUNDED = "REFUNDED", "Refunded"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    assign_order = models.ForeignKey(
        AssignOrder,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=10, choices=Method.choices)
    payment_status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    transaction_id = models.CharField(max_length=255, blank=True, null=True)
    paid_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "payments"
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="payments_amount_positive",
            )
        ]

    def __str__(self):
        return f"Payment {self.pk or 'new'} - {self.payment_status}"
