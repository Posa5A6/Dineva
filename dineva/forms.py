from django import forms
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordResetForm,
    SetPasswordForm,
)
from django.core.exceptions import ValidationError

from .models import EmployeeIDProof, Restaurant, User, UserProfile
from .validators import validate_id_proof_file, validate_profile_photo


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
    class Meta:
        model = Restaurant
        fields = ["name", "address", "contact", "email", "logo", "is_active"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "contact": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "logo": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


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
