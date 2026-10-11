from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    SetPasswordForm,
)
from django.core.exceptions import ValidationError

from .models import (
    AssignOrder,
    EmployeeIDProof,
    Item,
    Payment,
    Restaurant,
    RestaurantTaxProfile,
    RestaurantTable,
    SessionPayment,
    TableSession,
    User,
    UserProfile,
)
from .validators import (
    validate_id_proof_file,
    validate_menu_item_image,
    validate_profile_photo,
    validate_restaurant_logo,
)


class DinevaAuthenticationForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "Unable to sign in with the provided credentials.",
        "inactive": "Unable to sign in with the provided credentials.",
    }
    username = forms.CharField(
        label="Username",
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "username",
                "autofocus": True,
                "class": "form-control",
                "placeholder": "your.username",
            }
        ),
    )
    password = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "class": "form-control"}
        ),
    )


class DinevaPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={"autocomplete": "email", "class": "form-control"}
        )
    )


class AccountSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["autocomplete"] = "new-password"


class RestaurantForm(forms.ModelForm):
    logo = forms.ImageField(
        required=False,
        validators=[validate_restaurant_logo],
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png"}
        ),
    )

    class Meta:
        model = Restaurant
        fields = ["name", "address", "contact", "email", "logo", "is_active"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "organization",
                    "placeholder": "Restaurant name",
                }
            ),
            "address": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "autocomplete": "street-address",
                    "placeholder": "Street address, city, and service location",
                }
            ),
            "contact": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "tel",
                    "placeholder": "Contact number",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "email",
                    "placeholder": "restaurant@example.com",
                }
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class OwnerRestaurantSettingsForm(forms.ModelForm):
    logo = forms.ImageField(
        required=False,
        validators=[validate_restaurant_logo],
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png"}
        ),
    )

    class Meta:
        model = Restaurant
        fields = ["name", "address", "contact", "email", "logo"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "organization",
                    "placeholder": "Restaurant name",
                }
            ),
            "address": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "autocomplete": "street-address",
                    "placeholder": "Street address, city, and service location",
                }
            ),
            "contact": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "tel",
                    "placeholder": "Contact number",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "email",
                    "placeholder": "restaurant@example.com",
                }
            ),
        }


class OwnerRestaurantTaxProfileForm(forms.ModelForm):
    class Meta:
        model = RestaurantTaxProfile
        fields = [
            "registration_status",
            "gstin",
            "legal_name",
            "trade_name",
            "business_address",
            "state_name",
            "state_code",
            "gst_treatment",
            "prices_include_gst",
            "effective_from",
        ]
        widgets = {
            "registration_status": forms.Select(attrs={"class": "form-select"}),
            "gstin": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "maxlength": "15",
                    "autocomplete": "off",
                    "placeholder": "29ABCDE1234F1Z5",
                }
            ),
            "legal_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "organization",
                    "placeholder": "Legal business name",
                }
            ),
            "trade_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "organization",
                    "placeholder": "Restaurant display or trade name",
                }
            ),
            "business_address": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "autocomplete": "street-address",
                    "placeholder": "Registered business address",
                }
            ),
            "state_name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Karnataka",
                }
            ),
            "state_code": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "maxlength": "2",
                    "inputmode": "numeric",
                    "placeholder": "29",
                }
            ),
            "gst_treatment": forms.Select(attrs={"class": "form-select"}),
            "prices_include_gst": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "effective_from": forms.DateInput(
                attrs={"class": "form-control", "type": "date"},
                format="%Y-%m-%d",
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["effective_from"].input_formats = ["%Y-%m-%d"]
        self.fields["gstin"].required = False
        self.fields["legal_name"].required = False
        self.fields["trade_name"].required = False
        self.fields["business_address"].required = False
        self.fields["state_name"].required = False
        self.fields["state_code"].required = False
        self.fields["effective_from"].required = False

    def clean_gstin(self):
        return (self.cleaned_data.get("gstin") or "").strip().upper()

    def clean_state_code(self):
        return (self.cleaned_data.get("state_code") or "").strip()

    def clean(self):
        cleaned_data = super().clean()
        registration_status = cleaned_data.get("registration_status")
        gst_treatment = cleaned_data.get("gst_treatment")
        registered_statuses = {
            RestaurantTaxProfile.RegistrationStatus.REGULAR,
            RestaurantTaxProfile.RegistrationStatus.COMPOSITION,
        }
        if registration_status in registered_statuses and not cleaned_data.get("gstin"):
            self.add_error("gstin", "GSTIN is required for registered restaurants.")
        if registration_status in registered_statuses:
            required_fields = {
                "legal_name": "Legal business name is required for registered restaurants.",
                "business_address": "Registered business address is required for registered restaurants.",
                "state_name": "State name is required for registered restaurants.",
                "state_code": "State code is required for registered restaurants.",
            }
            for field_name, message in required_fields.items():
                if not cleaned_data.get(field_name):
                    self.add_error(field_name, message)
        if not cleaned_data.get("effective_from"):
            self.add_error("effective_from", "Effective date is required before review.")
        if (
            registration_status == RestaurantTaxProfile.RegistrationStatus.UNREGISTERED
            and cleaned_data.get("gstin")
        ):
            self.add_error("gstin", "Unregistered restaurants cannot submit a GSTIN.")
        if (
            registration_status == RestaurantTaxProfile.RegistrationStatus.COMPOSITION
            and gst_treatment != RestaurantTaxProfile.Treatment.COMPOSITION_NO_TAX_COLLECTED
        ):
            self.add_error(
                "gst_treatment",
                "Composition suppliers cannot select regular GST collection.",
            )
        if (
            registration_status == RestaurantTaxProfile.RegistrationStatus.UNREGISTERED
            and gst_treatment != RestaurantTaxProfile.Treatment.UNREGISTERED_NO_GST
        ):
            self.add_error(
                "gst_treatment",
                "Unregistered restaurants cannot select GST collection.",
            )
        if (
            registration_status == RestaurantTaxProfile.RegistrationStatus.REGULAR
            and gst_treatment
            not in {
                RestaurantTaxProfile.Treatment.ORDINARY_RESTAURANT_5_NO_ITC,
                RestaurantTaxProfile.Treatment.SPECIFIED_PREMISES_18_WITH_ITC,
            }
        ):
            self.add_error(
                "gst_treatment",
                "Regular GST restaurants must select a regular GST treatment.",
            )
        return cleaned_data


class EmployeeCreateForm(forms.Form):
    name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )
    contact = forms.CharField(
        max_length=20,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    address = forms.CharField(
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
    )
    photo = forms.ImageField(
        validators=[validate_profile_photo],
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png"}
        ),
    )
    id_proof_type = forms.ChoiceField(
        choices=EmployeeIDProof.ProofType.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    id_proof_file = forms.FileField(
        validators=[validate_id_proof_file],
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png,.pdf"}
        ),
    )

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email


class ActiveRestaurantMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["restaurant"].queryset = Restaurant.objects.filter(
            is_active=True
        ).order_by("name")

    def clean_restaurant(self):
        restaurant = self.cleaned_data["restaurant"]
        if not restaurant.is_active:
            raise ValidationError("Select an active restaurant.")
        return restaurant


class OwnerCreateForm(ActiveRestaurantMixin, EmployeeCreateForm):
    restaurant = forms.ModelChoiceField(
        queryset=Restaurant.objects.none(),
        empty_label="Select an active restaurant",
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class StaffCreateForm(ActiveRestaurantMixin, EmployeeCreateForm):
    restaurant = forms.ModelChoiceField(
        queryset=Restaurant.objects.none(),
        empty_label="Select an active restaurant",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    staff_type = forms.ChoiceField(
        choices=[
            (UserProfile.Role.WAITER, "Waiter"),
            (UserProfile.Role.KITCHEN_STAFF, "Kitchen Staff"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class OwnerStaffCreateForm(EmployeeCreateForm):
    staff_type = forms.ChoiceField(
        choices=[
            (UserProfile.Role.WAITER, "Waiter"),
            (UserProfile.Role.KITCHEN_STAFF, "Kitchen Staff"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class OwnerMenuItemForm(forms.ModelForm):
    image = forms.ImageField(
        required=False,
        validators=[validate_menu_item_image],
        widget=forms.ClearableFileInput(
            attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png"}
        ),
    )

    class Meta:
        model = Item
        fields = [
            "category",
            "name",
            "description",
            "price",
            "image",
            "is_available",
            "is_active",
        ]
        widgets = {
            "category": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Starters, mains, beverages...",
                }
            ),
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Menu item name",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Short description shown to staff.",
                }
            ),
            "price": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": "0",
                    "step": "0.01",
                    "inputmode": "decimal",
                }
            ),
            "is_available": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_category(self):
        return self.cleaned_data["category"].strip()

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price < 0:
            raise ValidationError("Price cannot be negative.")
        return price


class OwnerTableForm(forms.ModelForm):
    class Meta:
        model = RestaurantTable
        fields = ["table_no", "capacity"]
        widgets = {
            "table_no": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "off",
                    "placeholder": "A1, Patio 4, Table 12...",
                }
            ),
            "capacity": forms.NumberInput(
                attrs={
                    "class": "form-control",
                    "min": "1",
                    "step": "1",
                    "inputmode": "numeric",
                }
            ),
        }

    def clean_table_no(self):
        table_no = self.cleaned_data["table_no"].strip()
        if not table_no:
            raise ValidationError("Table identifier is required.")
        return table_no

    def clean_capacity(self):
        capacity = self.cleaned_data["capacity"]
        if capacity <= 0:
            raise ValidationError("Capacity must be greater than zero.")
        return capacity


class WaiterOrderForm(forms.Form):
    submission_key = forms.CharField(max_length=64, widget=forms.HiddenInput)
    order_type = forms.ChoiceField(
        choices=AssignOrder.OrderType.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    table = forms.ModelChoiceField(
        queryset=RestaurantTable.objects.none(),
        required=False,
        empty_label="Select an available table",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, restaurant, submission_key=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.restaurant = restaurant
        if submission_key:
            self.fields["submission_key"].initial = submission_key
        self.menu_items = list(
            Item.objects.filter(
                restaurant=restaurant,
                is_active=True,
                is_available=True,
            ).order_by("category", "name")
        )
        open_session_table_ids = TableSession.objects.filter(
            restaurant=restaurant,
            status__in=[
                TableSession.Status.OPEN,
                TableSession.Status.PAYMENT_PENDING,
            ],
            table_id__isnull=False,
        ).values("table_id")
        self.fields["table"].queryset = RestaurantTable.objects.filter(
            restaurant=restaurant,
            is_active=True,
            status=RestaurantTable.Status.AVAILABLE,
        ).exclude(pk__in=open_session_table_ids).order_by("table_no")
        for item in self.menu_items:
            field_name = self.quantity_field_name(item)
            self.fields[field_name] = forms.IntegerField(
                min_value=0,
                required=False,
                initial=0,
                widget=forms.NumberInput(
                    attrs={
                        "class": "form-control",
                        "min": "0",
                        "step": "1",
                        "inputmode": "numeric",
                        "aria-label": f"Quantity for {item.name}",
                    }
                ),
            )
        self.menu_lines = [
            {"item": item, "field": self[self.quantity_field_name(item)]}
            for item in self.menu_items
        ]

    @staticmethod
    def quantity_field_name(item):
        return f"quantity_{item.pk}"

    def clean(self):
        cleaned_data = super().clean()
        order_type = cleaned_data.get("order_type")
        table = cleaned_data.get("table")
        if order_type == AssignOrder.OrderType.DINE_IN and table is None:
            self.add_error("table", "Select a table for dine-in orders.")
        if order_type == AssignOrder.OrderType.TAKEAWAY:
            cleaned_data["table"] = None

        order_lines = []
        for item in self.menu_items:
            quantity = cleaned_data.get(self.quantity_field_name(item)) or 0
            if quantity > 0:
                order_lines.append({"item": item, "quantity": quantity})
        if not order_lines:
            raise ValidationError("Add at least one menu item to create an order.")
        cleaned_data["order_lines"] = order_lines
        return cleaned_data


class WaiterSessionRoundForm(forms.Form):
    submission_key = forms.CharField(max_length=64, widget=forms.HiddenInput)

    def __init__(self, *args, restaurant, submission_key=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.restaurant = restaurant
        if submission_key:
            self.fields["submission_key"].initial = submission_key
        self.menu_items = list(
            Item.objects.filter(
                restaurant=restaurant,
                is_active=True,
                is_available=True,
            ).order_by("category", "name")
        )
        for item in self.menu_items:
            field_name = self.quantity_field_name(item)
            self.fields[field_name] = forms.IntegerField(
                min_value=0,
                required=False,
                initial=0,
                widget=forms.NumberInput(
                    attrs={
                        "class": "form-control",
                        "min": "0",
                        "step": "1",
                        "inputmode": "numeric",
                        "aria-label": f"Quantity for {item.name}",
                    }
                ),
            )
        self.menu_lines = [
            {"item": item, "field": self[self.quantity_field_name(item)]}
            for item in self.menu_items
        ]

    @staticmethod
    def quantity_field_name(item):
        return f"quantity_{item.pk}"

    def clean(self):
        cleaned_data = super().clean()
        order_lines = []
        for item in self.menu_items:
            quantity = cleaned_data.get(self.quantity_field_name(item)) or 0
            if quantity > 0:
                order_lines.append({"item": item, "quantity": quantity})
        if not order_lines:
            raise ValidationError("Add at least one menu item to create an order round.")
        cleaned_data["order_lines"] = order_lines
        return cleaned_data


class WaiterSessionCheckoutForm(forms.Form):
    customer_name = forms.CharField(
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Customer name"}),
    )
    customer_email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-control", "placeholder": "email@example.com"}),
    )
    customer_mobile = forms.CharField(
        max_length=20,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Mobile or WhatsApp number"}),
    )


class WaiterSessionPaymentForm(forms.Form):
    submission_key = forms.CharField(max_length=64, widget=forms.HiddenInput)
    payment_method = forms.ChoiceField(
        choices=[
            (SessionPayment.Method.CASH, "Cash"),
            (SessionPayment.Method.MANUAL_UPI, "Manual Online/UPI confirmation"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "min": "0.01", "step": "0.01", "inputmode": "decimal"}
        ),
    )
    reference = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "Cash note or verified UPI reference"}),
    )

    def __init__(self, *args, submission_key=None, **kwargs):
        super().__init__(*args, **kwargs)
        if submission_key:
            self.fields["submission_key"].initial = submission_key

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise ValidationError("Payment amount must be positive.")
        return amount

    def clean(self):
        cleaned_data = super().clean()
        if (
            cleaned_data.get("payment_method") == SessionPayment.Method.MANUAL_UPI
            and not (cleaned_data.get("reference") or "").strip()
        ):
            self.add_error("reference", "Manual Online/UPI confirmation requires a transaction or reference number.")
        return cleaned_data


class OwnerPaymentRecordForm(forms.Form):
    submission_key = forms.CharField(max_length=64, widget=forms.HiddenInput)
    payment_method = forms.ChoiceField(
        choices=Payment.Method.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, submission_key=None, **kwargs):
        super().__init__(*args, **kwargs)
        if submission_key:
            self.fields["submission_key"].initial = submission_key


class OwnerCompletedOrderCorrectionForm(forms.Form):
    reason = forms.CharField(
        max_length=1000,
        min_length=10,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 4,
                "placeholder": "Describe the completed-order correction and why it is needed.",
            }
        ),
    )

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if len(reason.split()) < 3:
            raise ValidationError("Provide a meaningful correction reason.")
        return reason


class EmployeeEditForm(forms.Form):
    name = forms.CharField(max_length=150, widget=forms.TextInput(attrs={"class": "form-control"}))
    email = forms.EmailField(widget=forms.EmailInput(attrs={"class": "form-control"}))
    contact = forms.CharField(max_length=20, widget=forms.TextInput(attrs={"class": "form-control"}))
    address = forms.CharField(widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))
    photo = forms.ImageField(
        required=False,
        validators=[validate_profile_photo],
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png"}),
    )
    id_proof_type = forms.ChoiceField(
        choices=EmployeeIDProof.ProofType.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    id_proof_file = forms.FileField(
        required=False,
        validators=[validate_id_proof_file],
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png,.pdf"}),
    )

    def __init__(self, *args, profile, **kwargs):
        self.profile = profile
        proof = profile.id_proof
        kwargs.setdefault(
            "initial",
            {
                "name": profile.user.name,
                "email": profile.user.email,
                "contact": profile.contact,
                "address": profile.address,
                "id_proof_type": proof.proof_type,
            },
        )
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.profile.user_id).exists():
            raise ValidationError("A user with this email already exists.")
        return email


class StaffPhotoForm(forms.Form):
    photo = forms.ImageField(
        validators=[validate_profile_photo],
        widget=forms.ClearableFileInput(attrs={"class": "form-control", "accept": ".jpg,.jpeg,.png"}),
    )


class ImpersonationReasonForm(forms.Form):
    reason = forms.CharField(
        label="Reason",
        max_length=1000,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 4,
                "placeholder": "Explain why this impersonation is required.",
            }
        ),
    )

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()
        if not reason:
            raise ValidationError("A reason is required.")
        return reason


class SuperAdminTaxProfileReviewForm(forms.Form):
    decision = forms.ChoiceField(
        choices=[
            ("approve", "Approve"),
            ("reject", "Reject"),
        ],
        widget=forms.HiddenInput,
    )
    review_notes = forms.CharField(
        max_length=1000,
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 4,
                "placeholder": "Review notes or rejection reason.",
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("decision") == "reject" and not (cleaned_data.get("review_notes") or "").strip():
            self.add_error("review_notes", "Rejection requires a review note.")
        return cleaned_data
