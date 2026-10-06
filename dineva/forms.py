from django import forms
from django.contrib.auth.forms import AuthenticationForm, SetPasswordForm
from django.core.exceptions import ValidationError

from .models import Restaurant, User


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
            attrs={
                "autocomplete": "current-password",
                "class": "form-control",
            }
        ),
    )

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


class RestaurantUserCreateForm(forms.Form):
    name = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(
            attrs={"class": "form-control", "autocomplete": "username"}
        ),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )
    restaurant = forms.ModelChoiceField(
        queryset=Restaurant.objects.none(),
        empty_label="Select an active restaurant",
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["restaurant"].queryset = Restaurant.objects.filter(
            is_active=True
        ).order_by("name")

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("A user with this email already exists.")
        return email

    def clean_username(self):
        username = User.normalize_username(self.cleaned_data["username"]).strip()
        User.username_validator(username)
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("A user with this username already exists.")
        return username

    def clean_restaurant(self):
        restaurant = self.cleaned_data["restaurant"]
        if not restaurant.is_active:
            raise ValidationError("Select an active restaurant.")
        return restaurant


class OwnerCreateForm(RestaurantUserCreateForm):
    pass


class StaffCreateForm(RestaurantUserCreateForm):
    staff_type = forms.ChoiceField(
        choices=[
            (User.Role.WAITER, "Waiter"),
            (User.Role.KITCHEN_STAFF, "Kitchen Staff"),
        ],
        widget=forms.Select(attrs={"class": "form-select"}),
    )


class AccountSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["autocomplete"] = "new-password"
