from decimal import Decimal

from django.contrib.auth import SESSION_KEY
from django.db import IntegrityError, transaction
from django.test import TestCase
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
)


class FoundationModelTests(TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(
            name="Dineva Test Kitchen",
            address="1 Test Street",
            contact="+910000000000",
            email="restaurant@example.com",
        )
        self.waiter = User.objects.create_user(
            username="test.waiter",
            email="waiter@example.com",
            name="Test Waiter",
            password="test-password-only",
            role=User.Role.WAITER,
            restaurant=self.restaurant,
        )
        self.table = RestaurantTable.objects.create(
            restaurant=self.restaurant,
            table_no="T1",
            capacity=4,
        )

    def test_user_password_hashing_and_restaurant_relationship(self):
        self.assertTrue(self.waiter.check_password("test-password-only"))
        self.assertNotEqual(self.waiter.password, "test-password-only")
        self.assertEqual(self.waiter.restaurant, self.restaurant)

    def test_superadmin_has_no_restaurant(self):
        superadmin = User.objects.create_superuser(
            username="platform.admin",
            email="admin@example.com",
            name="Platform Admin",
            password="test-password-only",
        )
        self.assertIsNone(superadmin.restaurant)
        self.assertEqual(superadmin.role, User.Role.SUPERADMIN)
        self.assertIsNone(superadmin.public_id)

    def test_email_is_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(
                username="duplicate.waiter",
                email="waiter@example.com",
                name="Duplicate Waiter",
                password="test-password-only",
                role=User.Role.WAITER,
                restaurant=self.restaurant,
            )

    def test_username_is_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(
                username=self.waiter.username,
                email="different@example.com",
                name="Duplicate Username",
                password="test-password-only",
                role=User.Role.WAITER,
                restaurant=self.restaurant,
            )

    def test_customer_mobile_is_unique(self):
        Customer.objects.create(name="First Customer", mobile="9999999999")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Customer.objects.create(name="Second Customer", mobile="9999999999")

    def test_table_number_is_unique_per_restaurant(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            RestaurantTable.objects.create(
                restaurant=self.restaurant,
                table_no="T1",
                capacity=2,
            )

    def test_item_and_payment_use_decimal_values(self):
        item = Item.objects.create(
            restaurant=self.restaurant,
            category="Main",
            name="Test Dish",
            price=Decimal("249.50"),
        )
        order = AssignOrder.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            waiter=self.waiter,
        )
        payment = Payment.objects.create(
            restaurant=self.restaurant,
            assign_order=order,
            amount=Decimal("249.50"),
            payment_method=Payment.Method.CASH,
        )
        item.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(item.price, Decimal("249.50"))
        self.assertEqual(payment.amount, Decimal("249.50"))

    def test_order_item_keeps_historical_price(self):
        item = Item.objects.create(
            restaurant=self.restaurant,
            category="Main",
            name="Historical Dish",
            price=Decimal("100.00"),
        )
        order = AssignOrder.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            waiter=self.waiter,
        )
        order_item = OrderItem.objects.create(
            assign_order=order,
            item=item,
            quantity=1,
            price=item.price,
        )
        item.price = Decimal("125.00")
        item.save(update_fields=["price"])
        order_item.refresh_from_db()
        self.assertEqual(order_item.price, Decimal("100.00"))

    def test_assign_order_relationships(self):
        customer = Customer.objects.create(
            name="Order Customer",
            mobile="8888888888",
        )
        order = AssignOrder.objects.create(
            restaurant=self.restaurant,
            table=self.table,
            waiter=self.waiter,
            customer=customer,
        )
        self.assertEqual(order.restaurant, self.restaurant)
        self.assertEqual(order.table, self.table)
        self.assertEqual(order.waiter, self.waiter)
        self.assertEqual(order.customer, customer)

    def test_home_page_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dineva")


class SuperAdminAuthenticationTests(TestCase):
    password = "Test-admin-password-123"
    generic_error = "Unable to sign in with the provided credentials."

    def setUp(self):
        self.superadmin = User.objects.create_superuser(
            username="platform.admin",
            email="admin@example.com",
            name="Platform Admin",
            password=self.password,
        )
        self.restaurant = Restaurant.objects.create(
            name="Authentication Test Restaurant",
            address="2 Test Street",
            contact="+910000000001",
            email="auth-restaurant@example.com",
        )

    def login(self, username=None, password=None):
        return self.client.post(
            reverse("dineva:login"),
            {
                "username": username or self.superadmin.username,
                "password": password or self.password,
            },
        )

    def create_restaurant_user(self, role):
        return User.objects.create_user(
            username=f"test.{role.lower()}",
            email=f"{role.lower()}@example.com",
            name=f"Test {role}",
            password=self.password,
            role=role,
            restaurant=self.restaurant,
        )

    def assert_role_can_login_but_cannot_access_dashboard(self, role):
        user = self.create_restaurant_user(role)
        response = self.client.post(
            reverse("dineva:login"),
            {"username": user.username, "password": self.password},
        )
        self.assertRedirects(response, reverse("dineva:home"))
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))

        response = self.client.get(reverse("dineva:dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_login_with_correct_credentials(self):
        response = self.login()
        self.assertRedirects(response, reverse("dineva:dashboard"))

    def test_wrong_password_is_rejected(self):
        response = self.login(password="incorrect-password")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.generic_error)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_email_is_not_a_login_identifier(self):
        response = self.login(username=self.superadmin.email)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.generic_error)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_inactive_user_cannot_login(self):
        self.superadmin.is_active = False
        self.superadmin.save(update_fields=["is_active"])
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.generic_error)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_owner_cannot_access_dashboard(self):
        self.assert_role_can_login_but_cannot_access_dashboard(User.Role.OWNER)

    def test_waiter_cannot_access_dashboard(self):
        self.assert_role_can_login_but_cannot_access_dashboard(User.Role.WAITER)

    def test_kitchen_staff_cannot_access_dashboard(self):
        self.assert_role_can_login_but_cannot_access_dashboard(
            User.Role.KITCHEN_STAFF
        )

    def test_unauthenticated_user_cannot_access_dashboard(self):
        response = self.client.get(reverse("dineva:dashboard"))
        self.assertRedirects(response, reverse("dineva:login"))

    def test_successful_login_creates_session(self):
        self.login()
        self.assertEqual(
            self.client.session[SESSION_KEY],
            str(self.superadmin.pk),
        )

    def test_logout_destroys_authenticated_session(self):
        self.login()
        response = self.client.post(reverse("dineva:logout"))
        self.assertRedirects(response, reverse("dineva:login"))
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_logout_rejects_get_requests(self):
        self.client.force_login(self.superadmin)
        response = self.client.get(reverse("dineva:logout"))
        self.assertEqual(response.status_code, 405)
        self.assertIn(SESSION_KEY, self.client.session)

    def test_login_error_does_not_enumerate_accounts(self):
        known_response = self.login(password="incorrect-password")
        unknown_response = self.login(
            username="unknown.user",
            password="incorrect-password",
        )
        self.assertContains(known_response, self.generic_error)
        self.assertContains(unknown_response, self.generic_error)
        self.assertEqual(
            known_response.context["form"].non_field_errors(),
            unknown_response.context["form"].non_field_errors(),
        )

    def test_shared_login_page_uses_username_and_csrf(self):
        response = self.client.get(reverse("dineva:login"))
        self.assertContains(response, "Username")
        self.assertContains(response, "csrfmiddlewaretoken")
        self.assertNotContains(response, "Super Admin account")
