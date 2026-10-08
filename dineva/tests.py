from decimal import Decimal
from tempfile import TemporaryDirectory

from django.contrib.auth import SESSION_KEY, authenticate
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .models import (
    AssignOrder,
    Customer,
    Item,
    OrderItem,
    Payment,
    Restaurant,
    RestaurantTable,
    User,
    UserProfile,
)
from .test_utils import create_test_employee, password_from_welcome


class PrivateMediaMixin:
    @classmethod
    def setUpClass(cls):
        cls.private_media = TemporaryDirectory()
        cls.settings_override = override_settings(
            PRIVATE_MEDIA_ROOT=cls.private_media.name,
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
            DINEVA_PUBLIC_BASE_URL="https://dineva.example.invalid",
        )
        cls.settings_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.settings_override.disable()
        cls.private_media.cleanup()


class AccountArchitectureTests(PrivateMediaMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.restaurant = Restaurant.objects.create(
            name="Test Kitchen",
            address="1 Test Street",
            contact="+910000000000",
            email="restaurant@example.invalid",
        )

    def test_superuser_creation_creates_platform_profile(self):
        admin = User.objects.create_superuser(
            username="platform.admin",
            email="admin@example.invalid",
            name="Platform Admin",
            password="Admin-secure-123!",
        )
        self.assertEqual(admin.profile.role, UserProfile.Role.SUPERADMIN)
        self.assertIsNone(admin.profile.restaurant_id)
        self.assertIsNone(admin.profile.public_id)
        self.assertTrue(admin.is_superuser)

    def test_employee_fields_are_not_on_user(self):
        field_names = {field.name for field in User._meta.get_fields()}
        self.assertFalse({"role", "restaurant", "public_id"} & field_names)

    def test_generated_username_and_password_hash(self):
        user = create_test_employee(self.restaurant, name="Rahul Sharma")
        password = password_from_welcome(mail.outbox[-1])
        self.assertEqual(user.username, "rahul.sharma")
        self.assertTrue(user.check_password(password))
        self.assertNotEqual(user.password, password)

    def test_username_collision_uses_numeric_suffix(self):
        first = create_test_employee(self.restaurant, name="Rahul Sharma")
        second = create_test_employee(
            self.restaurant,
            name="Rahul Sharma",
            email="rahul2@example.invalid",
        )
        self.assertEqual(first.username, "rahul.sharma")
        self.assertEqual(second.username, "rahul.sharma1")

    def test_username_is_immutable(self):
        user = create_test_employee(self.restaurant)
        user.username = "changed.username"
        with self.assertRaises(ValidationError):
            user.save()

    def test_role_restaurant_and_public_id_are_immutable(self):
        user = create_test_employee(self.restaurant)
        profile = user.profile
        for field, value in (
            ("role", UserProfile.Role.WAITER),
            ("public_id", "OWN-99999"),
            ("restaurant_id", None),
        ):
            profile.refresh_from_db()
            setattr(profile, field, value)
            with self.assertRaises(ValidationError):
                profile.save()

    def test_email_remains_unique(self):
        create_test_employee(self.restaurant)
        with self.assertRaises(Exception):
            create_test_employee(self.restaurant, name="Other", email="OWNER@example.invalid")


class AuthenticationLifecycleTests(PrivateMediaMixin, TestCase):
    password = "Admin-secure-123!"

    def setUp(self):
        cache.clear()
        mail.outbox.clear()
        self.admin = User.objects.create_superuser(
            username="platform.admin",
            email="admin@example.invalid",
            name="Platform Admin",
            password=self.password,
        )
        self.restaurant = Restaurant.objects.create(
            name="Auth Kitchen",
            address="2 Test Street",
            contact="+910000000002",
            email="auth@example.invalid",
        )
        self.owner = create_test_employee(self.restaurant)
        self.owner_password = password_from_welcome(mail.outbox[-1])
        self.waiter = create_test_employee(
            self.restaurant,
            role=UserProfile.Role.WAITER,
            name="Test Waiter",
            email="waiter@example.invalid",
        )
        self.waiter_password = password_from_welcome(mail.outbox[-1])

    def test_shared_login_uses_username_and_csrf(self):
        response = self.client.get(reverse("dineva:login"))
        self.assertContains(response, "Username")
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_username_login_succeeds_and_email_login_fails(self):
        self.assertEqual(authenticate(username=self.owner.username, password=self.owner_password), self.owner)
        self.assertIsNone(authenticate(username=self.owner.email, password=self.owner_password))

    def test_owner_login_redirects_to_tenant_staff_list(self):
        response = self.client.post(
            reverse("dineva:login"),
            {"username": self.owner.username, "password": self.owner_password},
        )
        self.assertRedirects(response, reverse("dineva:owner-staff-list"))
        self.assertEqual(self.client.session[SESSION_KEY], str(self.owner.pk))

    def test_inactive_user_cannot_authenticate(self):
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertIsNone(authenticate(username=self.owner.username, password=self.owner_password))

    def test_inactive_restaurant_blocks_owner_and_staff(self):
        self.restaurant.is_active = False
        self.restaurant.save(update_fields=["is_active"])
        self.assertIsNone(authenticate(username=self.owner.username, password=self.owner_password))
        self.assertIsNone(authenticate(username=self.waiter.username, password=self.waiter_password))

    def test_all_owners_inactive_blocks_staff_without_mutating_staff(self):
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertIsNone(authenticate(username=self.waiter.username, password=self.waiter_password))
        self.waiter.refresh_from_db()
        self.assertTrue(self.waiter.is_active)

    def test_one_active_owner_allows_staff_and_reactivation_restores_access(self):
        second_owner = create_test_employee(
            self.restaurant,
            name="Second Owner",
            email="owner2@example.invalid",
        )
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertEqual(authenticate(username=self.waiter.username, password=self.waiter_password), self.waiter)
        second_owner.is_active = False
        second_owner.save(update_fields=["is_active"])
        self.assertIsNone(authenticate(username=self.waiter.username, password=self.waiter_password))
        self.owner.is_active = True
        self.owner.save(update_fields=["is_active"])
        self.assertEqual(authenticate(username=self.waiter.username, password=self.waiter_password), self.waiter)

    def test_existing_session_is_revoked_when_restaurant_deactivates(self):
        self.client.force_login(self.owner)
        self.restaurant.is_active = False
        self.restaurant.save(update_fields=["is_active"])
        response = self.client.get(reverse("dineva:owner-staff-list"))
        self.assertRedirects(response, reverse("dineva:login"))

    def test_password_recovery_is_generic_and_preserves_username(self):
        original_username = self.owner.username
        known = self.client.post(reverse("dineva:password-reset"), {"email": self.owner.email})
        unknown = self.client.post(reverse("dineva:password-reset"), {"email": "unknown@example.invalid"})
        self.assertRedirects(known, reverse("dineva:password-reset-done"))
        self.assertRedirects(unknown, reverse("dineva:password-reset-done"))
        self.assertEqual(len(mail.outbox), 3)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.username, original_username)

    def test_obsolete_account_setup_route_is_retired(self):
        response = self.client.get("/account/setup/unused/unused/")
        self.assertEqual(response.status_code, 404)

    def test_login_throttle_keeps_generic_error(self):
        for _ in range(6):
            response = self.client.post(
                reverse("dineva:login"),
                {"username": self.owner.username, "password": "wrong"},
            )
        self.assertContains(response, "Unable to sign in with the provided credentials.")


class FoundationModelTests(PrivateMediaMixin, TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(
            name="Foundation Kitchen",
            address="3 Test Street",
            contact="+910000000003",
            email="foundation@example.invalid",
        )
        self.owner = create_test_employee(self.restaurant)
        self.waiter = create_test_employee(
            self.restaurant,
            role=UserProfile.Role.WAITER,
            name="Foundation Waiter",
            email="foundation-waiter@example.invalid",
        )
        self.table = RestaurantTable.objects.create(
            restaurant=self.restaurant,
            table_no="T1",
            capacity=4,
        )

    def test_customer_and_table_uniqueness(self):
        Customer.objects.create(name="First", mobile="9999999999")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Customer.objects.create(name="Second", mobile="9999999999")
        with self.assertRaises(IntegrityError), transaction.atomic():
            RestaurantTable.objects.create(restaurant=self.restaurant, table_no="T1", capacity=2)

    def test_order_item_keeps_historical_price(self):
        item = Item.objects.create(restaurant=self.restaurant, category="Main", name="Dish", price=Decimal("100.00"))
        order = AssignOrder.objects.create(restaurant=self.restaurant, table=self.table, waiter=self.waiter)
        order_item = OrderItem.objects.create(assign_order=order, item=item, quantity=1, price=item.price)
        item.price = Decimal("125.00")
        item.save(update_fields=["price"])
        order_item.refresh_from_db()
        self.assertEqual(order_item.price, Decimal("100.00"))

    def test_payment_uses_decimal(self):
        order = AssignOrder.objects.create(restaurant=self.restaurant, table=self.table, waiter=self.waiter)
        payment = Payment.objects.create(restaurant=self.restaurant, assign_order=order, amount=Decimal("249.50"), payment_method=Payment.Method.CASH)
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("249.50"))

    def test_home_page_renders(self):
        self.assertContains(self.client.get(reverse("dineva:home")), "Dineva")

    def test_logout_is_post_only(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("dineva:logout")).status_code, 405)

    def test_logout_requires_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(reverse("dineva:logout")).status_code, 403)
