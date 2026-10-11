from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import DEFAULT_DB_ALIAS, models, transaction
from django.db.models import Q
from django.utils import timezone

from .storage import (
    employee_id_proof_upload_to,
    employee_photo_upload_to,
    get_private_storage,
)
from .validators import (
    validate_id_proof_file,
    validate_menu_item_image,
    validate_profile_photo,
)


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


gstin_validator = RegexValidator(
    regex=r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$",
    message="Enter a valid 15-character GSTIN.",
)


class RestaurantTaxProfile(models.Model):
    class RegistrationStatus(models.TextChoices):
        REGULAR = "REGULAR", "Regular GST registration"
        COMPOSITION = "COMPOSITION", "Composition scheme"
        UNREGISTERED = "UNREGISTERED", "Unregistered"

    class Treatment(models.TextChoices):
        ORDINARY_RESTAURANT_5_NO_ITC = (
            "ORDINARY_RESTAURANT_5_NO_ITC",
            "Ordinary restaurant service - 5% without ITC",
        )
        SPECIFIED_PREMISES_18_WITH_ITC = (
            "SPECIFIED_PREMISES_18_WITH_ITC",
            "Specified-premises restaurant service - 18% with ITC",
        )
        COMPOSITION_NO_TAX_COLLECTED = (
            "COMPOSITION_NO_TAX_COLLECTED",
            "Composition supplier - no separate GST collection",
        )
        UNREGISTERED_NO_GST = (
            "UNREGISTERED_NO_GST",
            "Unregistered supplier - no GST collection",
        )

    class ConfigurationStatus(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING_REVIEW = "PENDING_REVIEW", "Pending review"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"

    restaurant = models.OneToOneField(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="tax_profile",
    )
    registration_status = models.CharField(
        max_length=20,
        choices=RegistrationStatus.choices,
        default=RegistrationStatus.UNREGISTERED,
    )
    gstin = models.CharField(
        max_length=15,
        blank=True,
        validators=[gstin_validator],
    )
    legal_name = models.CharField(max_length=200, blank=True)
    trade_name = models.CharField(max_length=200, blank=True)
    business_address = models.TextField(blank=True)
    state_name = models.CharField(max_length=100, blank=True)
    state_code = models.CharField(
        max_length=2,
        blank=True,
        validators=[
            RegexValidator(regex=r"^[0-9]{2}$", message="Enter a valid 2-digit GST state code.")
        ],
    )
    gst_treatment = models.CharField(
        max_length=40,
        choices=Treatment.choices,
        default=Treatment.UNREGISTERED_NO_GST,
    )
    prices_include_gst = models.BooleanField(default=True)
    effective_from = models.DateField(blank=True, null=True)
    configuration_status = models.CharField(
        max_length=20,
        choices=ConfigurationStatus.choices,
        default=ConfigurationStatus.DRAFT,
    )
    submitted_at = models.DateTimeField(blank=True, null=True)
    verified_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="verified_restaurant_tax_profiles",
    )
    verified_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="reviewed_restaurant_tax_profiles",
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    review_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "restaurant_tax_profiles"
        indexes = [
            models.Index(
                fields=["registration_status", "configuration_status"],
                name="tax_profiles_status_idx",
            ),
            models.Index(fields=["gst_treatment"], name="tax_profiles_treatment_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        registered_statuses = {
            self.RegistrationStatus.REGULAR,
            self.RegistrationStatus.COMPOSITION,
        }
        tax_collecting_treatments = {
            self.Treatment.ORDINARY_RESTAURANT_5_NO_ITC,
            self.Treatment.SPECIFIED_PREMISES_18_WITH_ITC,
        }
        gstin = (self.gstin or "").strip().upper()
        self.gstin = gstin
        for field in ("legal_name", "trade_name", "state_name", "state_code"):
            value = getattr(self, field)
            if value:
                setattr(self, field, value.strip())

        if self.registration_status in registered_statuses and not gstin:
            errors["gstin"] = "GSTIN is required for registered restaurants."
        if self.registration_status in registered_statuses:
            for field_name, label in (
                ("legal_name", "Legal business name"),
                ("business_address", "Registered business address"),
                ("state_name", "State name"),
                ("state_code", "State code"),
            ):
                if not getattr(self, field_name):
                    errors[field_name] = f"{label} is required for registered restaurants."
        if gstin and self.registration_status == self.RegistrationStatus.UNREGISTERED:
            errors["gstin"] = "Unregistered restaurants cannot have a GSTIN in this profile."
        if gstin and self.state_code and gstin[:2] != self.state_code:
            errors["state_code"] = "State code must match the first two digits of the GSTIN."

        if self.registration_status == self.RegistrationStatus.REGULAR:
            if self.gst_treatment not in tax_collecting_treatments:
                errors["gst_treatment"] = "Regular GST restaurants must use a regular GST treatment."
        elif self.registration_status == self.RegistrationStatus.COMPOSITION:
            if self.gst_treatment != self.Treatment.COMPOSITION_NO_TAX_COLLECTED:
                errors["gst_treatment"] = "Composition suppliers cannot select regular GST collection."
        elif self.registration_status == self.RegistrationStatus.UNREGISTERED:
            if self.gst_treatment != self.Treatment.UNREGISTERED_NO_GST:
                errors["gst_treatment"] = "Unregistered restaurants cannot select GST collection."

        if self.configuration_status == self.ConfigurationStatus.VERIFIED:
            if self.verified_by_id is None or self.verified_at is None:
                errors["configuration_status"] = "Verified profiles require reviewer metadata."
        elif self.verified_by_id is not None or self.verified_at is not None:
            errors["configuration_status"] = "Only verified profiles may store verification metadata."
        if self.configuration_status in {
            self.ConfigurationStatus.VERIFIED,
            self.ConfigurationStatus.REJECTED,
        }:
            if self.reviewed_by_id is None or self.reviewed_at is None:
                errors["configuration_status"] = "Reviewed profiles require review metadata."
        elif self.reviewed_by_id is not None or self.reviewed_at is not None:
            errors["configuration_status"] = "Only reviewed profiles may store review metadata."
        if self.configuration_status in {
            self.ConfigurationStatus.PENDING_REVIEW,
            self.ConfigurationStatus.VERIFIED,
        } and self.effective_from is None:
            errors["effective_from"] = "Effective date is required before review."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"Tax profile for {self.restaurant.name}"


class RestaurantTaxProfileReview(models.Model):
    class Decision(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    tax_profile = models.ForeignKey(
        RestaurantTaxProfile,
        on_delete=models.PROTECT,
        related_name="reviews",
    )
    reviewer = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="restaurant_tax_profile_reviews",
    )
    decision = models.CharField(max_length=10, choices=Decision.choices)
    previous_status = models.CharField(
        max_length=20,
        choices=RestaurantTaxProfile.ConfigurationStatus.choices,
    )
    resulting_status = models.CharField(
        max_length=20,
        choices=RestaurantTaxProfile.ConfigurationStatus.choices,
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "restaurant_tax_profile_reviews"
        indexes = [
            models.Index(fields=["tax_profile", "created_at"], name="tax_profile_review_idx"),
            models.Index(fields=["reviewer", "created_at"], name="tax_review_reviewer_idx"),
        ]

    def __str__(self):
        return f"{self.tax_profile_id} - {self.decision}"


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


class ImpersonationAudit(models.Model):
    class EndReason(models.TextChoices):
        STOPPED = "STOPPED", "Stopped by Super Admin"
        LOGOUT = "LOGOUT", "Logged out"
        TARGET_INELIGIBLE = "TARGET_INELIGIBLE", "Target became ineligible"
        INVALID_STATE = "INVALID_STATE", "Invalid impersonation state"
        RESTORE_FAILED = "RESTORE_FAILED", "Super Admin restore failed"
        SESSION_INIT_FAILED = "SESSION_INIT_FAILED", "Session initialization failed"

    superadmin = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="impersonations_started",
    )
    target_user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="impersonations_received",
    )
    target_role = models.CharField(max_length=20, choices=UserProfile.Role.choices)
    target_restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="impersonation_audits",
    )
    reason = models.TextField()
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(
        max_length=32,
        choices=EndReason.choices,
        blank=True,
    )
    session_key_hash = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "impersonation_audits"
        indexes = [
            models.Index(fields=["superadmin", "started_at"], name="imp_audit_admin_started_idx"),
            models.Index(fields=["target_user", "started_at"], name="imp_audit_target_started_idx"),
            models.Index(fields=["ended_at"], name="imp_audit_ended_idx"),
        ]

    def __str__(self):
        return f"{self.superadmin_id} impersonated {self.target_user_id}"


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
    is_active = models.BooleanField(default=True)
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
    image = models.ImageField(
        upload_to="items/",
        blank=True,
        null=True,
        validators=[validate_menu_item_image],
    )
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


class TableSession(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        PAYMENT_PENDING = "PAYMENT_PENDING", "Payment Pending"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="table_sessions",
    )
    table = models.ForeignKey(
        RestaurantTable,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="table_sessions",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="table_sessions",
    )
    opened_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="opened_table_sessions",
    )
    closed_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closed_table_sessions",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
    )
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "table_sessions"
        indexes = [
            models.Index(
                fields=["restaurant", "table", "status"],
                name="table_sessions_lookup_idx",
            ),
            models.Index(
                fields=["restaurant", "status", "opened_at"],
                name="table_sessions_status_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if self.table_id and self.table.restaurant_id != self.restaurant_id:
            raise ValidationError("The table must belong to the same restaurant.")
        if self.status == self.Status.CLOSED and (self.closed_at is None or self.closed_by_id is None):
            raise ValidationError("Closed sessions require closure timestamp and actor.")

    def __str__(self):
        if self.table_id:
            return f"Session {self.pk or 'new'} - Table {self.table.table_no}"
        return f"Session {self.pk or 'new'}"


class AssignOrder(models.Model):
    class OrderType(models.TextChoices):
        DINE_IN = "DINE_IN", "Dine-in"
        TAKEAWAY = "TAKEAWAY", "Takeaway"

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
        null=True,
        blank=True,
        related_name="assign_orders",
    )
    table_session = models.ForeignKey(
        TableSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
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
    order_type = models.CharField(
        max_length=10,
        choices=OrderType.choices,
        default=OrderType.DINE_IN,
    )
    order_date = models.DateTimeField(default=timezone.now)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    idempotency_key = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "assign_orders"
        constraints = [
            models.CheckConstraint(
                condition=Q(order_type="TAKEAWAY") | Q(table__isnull=False),
                name="assign_orders_dine_in_requires_table",
            ),
            models.CheckConstraint(
                condition=Q(order_type="DINE_IN") | Q(table__isnull=True),
                name="assign_orders_takeaway_has_no_table",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gte=0),
                name="assign_orders_total_non_negative",
            ),
        ]

    def __str__(self):
        if self.table_id:
            return f"Order {self.pk or 'new'} - Table {self.table.table_no}"
        return f"Order {self.pk or 'new'} - Takeaway"


class AssignOrderHistory(models.Model):
    class Action(models.TextChoices):
        ORDER_CREATED = "ORDER_CREATED", "Order Created"
        ORDER_CANCELLED = "ORDER_CANCELLED", "Order Cancelled"
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
        ORDER_CORRECTED = "ORDER_CORRECTED", "Order Corrected"
        INVOICE_FINALIZED = "INVOICE_FINALIZED", "Invoice Finalized"
        SESSION_CLOSED = "SESSION_CLOSED", "Session Closed"
        BILL_DELIVERY_ATTEMPTED = "BILL_DELIVERY_ATTEMPTED", "Bill Delivery Attempted"

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


class SessionInvoiceSequence(models.Model):
    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="invoice_sequences",
    )
    financial_year = models.CharField(max_length=9)
    next_number = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_invoice_sequences"
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "financial_year"],
                name="invoice_sequences_rest_year_unique",
            ),
            models.CheckConstraint(
                condition=Q(next_number__gt=0),
                name="invoice_sequences_next_positive",
            ),
        ]


class SessionInvoice(models.Model):
    class DocumentType(models.TextChoices):
        TAX_INVOICE = "TAX_INVOICE", "Tax Invoice"
        BILL_OF_SUPPLY = "BILL_OF_SUPPLY", "Bill of Supply"
        NON_GST_BILL = "NON_GST_BILL", "Non-GST Bill"

    class Status(models.TextChoices):
        FINALIZED = "FINALIZED", "Finalized"
        CANCELLED = "CANCELLED", "Cancelled"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="session_invoices",
    )
    table_session = models.OneToOneField(
        TableSession,
        on_delete=models.PROTECT,
        related_name="invoice",
    )
    invoice_number = models.CharField(max_length=40)
    financial_year = models.CharField(max_length=9)
    invoice_date = models.DateTimeField(default=timezone.now)
    document_type = models.CharField(max_length=20, choices=DocumentType.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.FINALIZED)
    tax_profile = models.ForeignKey(
        RestaurantTaxProfile,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="session_invoices",
    )
    supplier_legal_name = models.CharField(max_length=200)
    supplier_trade_name = models.CharField(max_length=200, blank=True)
    supplier_address = models.TextField()
    supplier_gstin = models.CharField(max_length=15, blank=True)
    supplier_state_name = models.CharField(max_length=100, blank=True)
    supplier_state_code = models.CharField(max_length=2, blank=True)
    tax_treatment = models.CharField(max_length=40)
    prices_include_gst = models.BooleanField(default=True)
    customer_name = models.CharField(max_length=150, blank=True)
    customer_email = models.EmailField(blank=True)
    customer_mobile = models.CharField(max_length=20, blank=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    taxable_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_gst = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    rounding_adjustment = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2)
    finalized_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="finalized_session_invoices",
    )
    finalized_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_invoices"
        constraints = [
            models.UniqueConstraint(
                fields=["restaurant", "invoice_number"],
                name="session_invoices_rest_number_unique",
            ),
            models.CheckConstraint(
                condition=Q(grand_total__gte=0),
                name="session_invoices_total_non_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["restaurant", "invoice_date"], name="session_invoice_date_idx"),
            models.Index(fields=["restaurant", "status"], name="session_invoice_status_idx"),
        ]


class SessionInvoiceLine(models.Model):
    invoice = models.ForeignKey(
        SessionInvoice,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    order_item = models.ForeignKey(
        OrderItem,
        on_delete=models.PROTECT,
        related_name="invoice_lines",
    )
    description = models.CharField(max_length=200)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    taxable_value = models.DecimalField(max_digits=12, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    cgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sgst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    igst_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "session_invoice_lines"
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="session_invoice_lines_quantity_positive",
            ),
            models.CheckConstraint(
                condition=Q(line_total__gte=0),
                name="session_invoice_lines_total_non_negative",
            ),
        ]


class SessionPayment(models.Model):
    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        MANUAL_UPI = "MANUAL_UPI", "Manual UPI Attestation"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    restaurant = models.ForeignKey(
        Restaurant,
        on_delete=models.PROTECT,
        related_name="session_payments",
    )
    invoice = models.ForeignKey(
        SessionInvoice,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=Method.choices)
    payment_status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )
    reference = models.CharField(max_length=255, blank=True)
    recorded_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="recorded_session_payments",
    )
    paid_at = models.DateTimeField(blank=True, null=True)
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "session_payments"
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="session_payments_amount_positive",
            )
        ]


class BillDeliveryAttempt(models.Model):
    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        WHATSAPP = "WHATSAPP", "WhatsApp"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SENT = "SENT", "Sent"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    invoice = models.ForeignKey(
        SessionInvoice,
        on_delete=models.PROTECT,
        related_name="delivery_attempts",
    )
    channel = models.CharField(max_length=10, choices=Channel.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    destination = models.CharField(max_length=255, blank=True)
    provider_reference = models.CharField(max_length=255, blank=True)
    error_message = models.TextField(blank=True)
    attempted_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bill_delivery_attempts"
        indexes = [
            models.Index(fields=["invoice", "channel", "created_at"], name="bill_delivery_lookup_idx")
        ]
