import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .models import Restaurant, User, UserPublicIdSequence
from .services import AccountSetupEmailError, create_restaurant_user


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="noreply@example.invalid",
    EMAIL_HOST_USER="smtp-user-secret@example.invalid",
    EMAIL_HOST_PASSWORD="smtp-password-secret",
)
class SuperAdminManagementTests(TestCase):
    password = "Test-admin-password-123"

    def setUp(self):
        self.superadmin = User.objects.create_superuser(
            username="platform.admin",
            email="admin@example.com",
            name="Platform Admin",
            password=self.password,
        )
        self.restaurant = Restaurant.objects.create(
            name="Active Restaurant",
            address="1 Active Street",
            contact="+910000000010",
            email="active@example.com",
        )
        self.inactive_restaurant = Restaurant.objects.create(
            name="Inactive Restaurant",
            address="2 Inactive Street",
            contact="+910000000011",
            email="inactive@example.com",
            is_active=False,
        )
        self.client.force_login(self.superadmin)

    def owner_data(self, **overrides):
        data = {
            "name": "Test Owner",
            "username": "test.owner",
            "email": "owner@example.invalid",
            "restaurant": self.restaurant.pk,
        }
        data.update(overrides)
        return data

    def staff_data(self, **overrides):
        data = {
            "name": "Test Waiter",
            "username": "test.waiter",
            "email": "waiter@example.invalid",
            "restaurant": self.restaurant.pk,
            "staff_type": User.Role.WAITER,
        }
        data.update(overrides)
        return data

    def test_superadmin_can_create_restaurant(self):
        response = self.client.post(
            reverse("dineva:restaurant-add"),
            {
                "name": "New Restaurant",
                "address": "3 New Street",
                "contact": "+910000000012",
                "email": "new@example.com",
                "is_active": "on",
            },
        )
        self.assertRedirects(response, reverse("dineva:restaurants"))
        self.assertTrue(Restaurant.objects.filter(name="New Restaurant").exists())

    def test_invalid_restaurant_data_is_rejected(self):
        response = self.client.post(
            reverse("dineva:restaurant-add"),
            {"name": "", "address": "", "contact": "", "email": "invalid"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Restaurant.objects.filter(email="invalid").exists())
        self.assertTrue(response.context["form"].errors)

    def test_csrf_is_required_for_restaurant_creation(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.superadmin)
        response = client.post(
            reverse("dineva:restaurant-add"),
            {
                "name": "No CSRF Restaurant",
                "address": "4 Test Street",
                "contact": "+910000000013",
                "email": "no-csrf@example.com",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_superadmin_can_create_owner(self):
        response = self.client.post(
            reverse("dineva:owner-add"),
            self.owner_data(),
        )
        self.assertRedirects(response, reverse("dineva:dashboard"))
        owner = User.objects.get(email="owner@example.invalid")
        self.assertEqual(owner.role, User.Role.OWNER)
        self.assertEqual(owner.restaurant, self.restaurant)
        self.assertFalse(owner.is_superuser)
        self.assertFalse(owner.has_usable_password())
        self.assertEqual(owner.public_id, "OWN-00001")
        self.assertEqual(owner.username, "test.owner")
        self.assertNotEqual(owner.username, owner.public_id)
        self.assertEqual(len(mail.outbox), 2)

    def test_owner_role_cannot_be_manipulated(self):
        self.client.post(
            reverse("dineva:owner-add"),
            self.owner_data(
                role=User.Role.SUPERADMIN,
                is_superuser="on",
                public_id="WTR-99999",
            ),
        )
        owner = User.objects.get(email="owner@example.invalid")
        self.assertEqual(owner.role, User.Role.OWNER)
        self.assertFalse(owner.is_superuser)
        self.assertEqual(owner.public_id, "OWN-00001")

    def test_duplicate_owner_email_is_rejected_without_reassignment(self):
        existing = User.objects.create_user(
            username="existing.waiter",
            email="owner@example.invalid",
            name="Existing Waiter",
            password=self.password,
            role=User.Role.WAITER,
            restaurant=self.restaurant,
        )
        response = self.client.post(
            reverse("dineva:owner-add"),
            self.owner_data(),
        )
        self.assertEqual(response.status_code, 200)
        existing.refresh_from_db()
        self.assertEqual(existing.role, User.Role.WAITER)
        self.assertContains(response, "A user with this email already exists.")
        self.assertEqual(len(mail.outbox), 0)

    def test_duplicate_username_is_rejected_without_creating_an_account(self):
        User.objects.create_user(
            username="test.owner",
            email="existing@example.invalid",
            name="Existing Owner",
            password=self.password,
            role=User.Role.OWNER,
            restaurant=self.restaurant,
        )
        response = self.client.post(
            reverse("dineva:owner-add"),
            self.owner_data(email="new-owner@example.invalid"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A user with this username already exists.")
        self.assertFalse(User.objects.filter(email="new-owner@example.invalid").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_inactive_restaurant_cannot_be_selected_for_owner(self):
        response = self.client.post(
            reverse("dineva:owner-add"),
            self.owner_data(restaurant=self.inactive_restaurant.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="owner@example.invalid").exists())

    def test_superadmin_can_create_waiter(self):
        self.client.post(reverse("dineva:staff-add"), self.staff_data())
        waiter = User.objects.get(email="waiter@example.invalid")
        self.assertEqual(waiter.role, User.Role.WAITER)
        self.assertEqual(waiter.restaurant, self.restaurant)
        self.assertFalse(waiter.has_usable_password())
        self.assertEqual(waiter.public_id, "WTR-00001")

    def test_superadmin_can_create_kitchen_staff(self):
        self.client.post(
            reverse("dineva:staff-add"),
            self.staff_data(
                name="Test Kitchen Staff",
                email="kitchen@example.invalid",
                staff_type=User.Role.KITCHEN_STAFF,
            ),
        )
        user = User.objects.get(email="kitchen@example.invalid")
        self.assertEqual(user.role, User.Role.KITCHEN_STAFF)
        self.assertEqual(user.restaurant, self.restaurant)
        self.assertEqual(user.public_id, "KST-00001")

    def test_public_ids_are_unique_stable_and_database_constrained(self):
        first = User.objects.create_user(
            username="first.owner",
            email="first-owner@example.invalid",
            name="First Owner",
            password=None,
            role=User.Role.OWNER,
            restaurant=self.restaurant,
        )
        second = User.objects.create_user(
            username="second.owner",
            email="second-owner@example.invalid",
            name="Second Owner",
            password=None,
            role=User.Role.OWNER,
            restaurant=self.inactive_restaurant,
        )
        self.assertEqual(first.public_id, "OWN-00001")
        self.assertEqual(second.public_id, "OWN-00002")
        self.assertNotEqual(first.public_id, second.public_id)
        original_public_id = first.public_id

        first.name = "Renamed Owner"
        first.save(update_fields=["name"])
        first.refresh_from_db()
        self.assertEqual(first.public_id, "OWN-00001")

        first.public_id = "OWN-99999"
        with self.assertRaises(ValidationError):
            first.save(update_fields=["public_id"])
        first.refresh_from_db()

        first.username = "changed.owner"
        with self.assertRaises(ValidationError):
            first.save(update_fields=["username"])
        first.refresh_from_db()
        self.assertEqual(first.username, "first.owner")

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=second.pk).update(public_id=original_public_id)

    def test_public_id_cannot_be_supplied_to_user_manager(self):
        with self.assertRaisesMessage(ValueError, "generated by Dineva"):
            User.objects.create_user(
                username="injected.owner",
                email="injected@example.invalid",
                name="Injected ID",
                password=None,
                role=User.Role.OWNER,
                restaurant=self.restaurant,
                public_id="OWN-99999",
            )

    def test_duplicate_staff_email_is_rejected(self):
        User.objects.create_user(
            username="existing.owner",
            email="waiter@example.invalid",
            name="Existing Owner",
            password=self.password,
            role=User.Role.OWNER,
            restaurant=self.restaurant,
        )
        response = self.client.post(
            reverse("dineva:staff-add"),
            self.staff_data(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A user with this email already exists.")
        self.assertEqual(User.objects.filter(email="waiter@example.invalid").count(), 1)

    def test_inactive_restaurant_cannot_be_selected_for_staff(self):
        response = self.client.post(
            reverse("dineva:staff-add"),
            self.staff_data(restaurant=self.inactive_restaurant.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="waiter@example.invalid").exists())

    def test_arbitrary_staff_role_is_rejected(self):
        response = self.client.post(
            reverse("dineva:staff-add"),
            self.staff_data(staff_type=User.Role.SUPERADMIN, role=User.Role.SUPERADMIN),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="waiter@example.invalid").exists())

    def test_unknown_restaurant_id_is_rejected(self):
        response = self.client.post(
            reverse("dineva:staff-add"),
            self.staff_data(restaurant=999999),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="waiter@example.invalid").exists())

    def test_restaurant_users_cannot_access_management_endpoints(self):
        endpoints = [
            "dineva:dashboard",
            "dineva:restaurants",
            "dineva:restaurant-add",
            "dineva:owner-add",
            "dineva:staff-add",
        ]
        for role in [User.Role.OWNER, User.Role.WAITER, User.Role.KITCHEN_STAFF]:
            user = User.objects.create_user(
                username=f"blocked.{role.lower()}",
                email=f"{role.lower()}@example.invalid",
                name=f"Test {role}",
                password=self.password,
                role=role,
                restaurant=self.restaurant,
            )
            self.client.force_login(user)
            for endpoint in endpoints:
                with self.subTest(role=role, endpoint=endpoint):
                    self.assertEqual(self.client.get(reverse(endpoint)).status_code, 403)

    def test_dashboard_counts_and_restaurant_list_are_correct(self):
        User.objects.create_user(
            username="dashboard.owner",
            email="owner@example.invalid",
            name="Owner",
            password=None,
            role=User.Role.OWNER,
            restaurant=self.restaurant,
        )
        User.objects.create_user(
            username="dashboard.waiter",
            email="waiter@example.invalid",
            name="Waiter",
            password=None,
            role=User.Role.WAITER,
            restaurant=self.restaurant,
        )
        response = self.client.get(reverse("dineva:dashboard"))
        self.assertEqual(response.context["restaurant_count"], 2)
        self.assertEqual(response.context["owner_count"], 1)
        self.assertEqual(response.context["waiter_count"], 1)
        active = next(r for r in response.context["restaurants"] if r == self.restaurant)
        self.assertEqual(active.owner_count, 1)
        self.assertEqual(active.staff_count, 1)
        self.assertContains(response, self.restaurant.name)

    def test_unauthenticated_management_access_redirects_to_login(self):
        self.client.logout()
        for endpoint in [
            "dineva:dashboard",
            "dineva:restaurants",
            "dineva:restaurant-add",
            "dineva:owner-add",
            "dineva:staff-add",
        ]:
            with self.subTest(endpoint=endpoint):
                self.assertRedirects(
                    self.client.get(reverse(endpoint)),
                    reverse("dineva:login"),
                )

    def test_setup_email_contains_safe_account_context_and_link(self):
        self.client.post(reverse("dineva:owner-add"), self.owner_data())
        self.assertEqual(len(mail.outbox), 2)
        welcome_email, setup_email = mail.outbox
        self.assertEqual(welcome_email.subject, "Welcome to Dineva")
        self.assertNotIn("/account/setup/", welcome_email.body)
        email = setup_email
        self.assertIn("Dineva", email.body)
        self.assertIn("Test Owner", email.body)
        self.assertIn(self.restaurant.name, email.body)
        self.assertRegex(email.body, r"http://testserver/account/setup/[^\s]+")
        self.assertIn("expires", email.body)
        self.assertNotIn(self.password, email.body)

    def test_each_restaurant_role_receives_safe_separate_welcome_email(self):
        request = self.client.get(reverse("dineva:owner-add")).wsgi_request
        cases = [
            (User.Role.OWNER, "Owner", "Owner ID", "OWN-"),
            (User.Role.WAITER, "Waiter", "Waiter ID", "WTR-"),
            (
                User.Role.KITCHEN_STAFF,
                "Kitchen Staff",
                "Kitchen Staff ID",
                "KST-",
            ),
        ]

        for index, (role, role_label, id_label, prefix) in enumerate(cases):
            with self.subTest(role=role):
                mail.outbox.clear()
                user = create_restaurant_user(
                    request=request,
                    username=f"welcome.{index}",
                    name=f"Welcome {role_label}",
                    email=f"welcome-{index}@example.invalid",
                    restaurant=self.restaurant,
                    role=role,
                )
                self.assertFalse(user.has_usable_password())
                self.assertTrue(user.public_id.startswith(prefix))
                self.assertEqual(len(mail.outbox), 2)

                welcome_email, setup_email = mail.outbox
                self.assertEqual(welcome_email.subject, "Welcome to Dineva")
                self.assertIn(user.name, welcome_email.body)
                self.assertIn(user.email, welcome_email.body)
                self.assertIn(f"Username: {user.username}", welcome_email.body)
                self.assertIn(self.restaurant.name, welcome_email.body)
                self.assertIn(f"Role: {role_label}", welcome_email.body)
                self.assertIn(f"{id_label}: {user.public_id}", welcome_email.body)
                self.assertIn("successfully created", welcome_email.body)
                self.assertNotIn("/account/setup/", welcome_email.body)
                self.assertNotIn(self.password, welcome_email.body)
                self.assertNotIn("smtp-user-secret", welcome_email.body)
                self.assertNotIn("smtp-password-secret", welcome_email.body)

                self.assertEqual(
                    setup_email.subject,
                    "Set up your Dineva account",
                )
                self.assertRegex(
                    setup_email.body,
                    r"http://testserver/account/setup/[^\s]+",
                )

    def test_user_can_set_password_and_setup_link_cannot_be_reused(self):
        self.client.post(reverse("dineva:owner-add"), self.owner_data())
        owner = User.objects.get(email="owner@example.invalid")
        original_url = re.search(
            r"http://testserver(?P<path>/account/setup/[^\s]+)",
            mail.outbox[1].body,
        ).group("path")

        response = self.client.get(original_url)
        self.assertEqual(response.status_code, 302)
        setup_form_url = response.url
        new_password = "Owner-secure-password-456"
        response = self.client.post(
            setup_form_url,
            {"new_password1": new_password, "new_password2": new_password},
        )
        self.assertRedirects(response, reverse("dineva:account-setup-success"))

        owner.refresh_from_db()
        self.assertTrue(owner.check_password(new_password))
        self.assertNotEqual(owner.password, new_password)
        self.assertEqual(
            authenticate(username=owner.username, password=new_password),
            owner,
        )
        self.assertIsNone(authenticate(email=owner.email, password=new_password))

        reused = self.client.get(original_url)
        self.assertEqual(reused.status_code, 200)
        self.assertContains(reused, "invalid or has already been used")

    @patch("dineva.services.send_account_setup_email", side_effect=RuntimeError)
    def test_setup_email_failure_occurs_after_committed_account_creation(self, _send):
        request = self.client.get(reverse("dineva:owner-add")).wsgi_request
        with self.assertRaises(AccountSetupEmailError) as raised:
            create_restaurant_user(
                request=request,
                username="email.failure",
                name="Email Failure Owner",
                email="email-failure@example.invalid",
                restaurant=self.restaurant,
                role=User.Role.OWNER,
            )
        self.assertTrue(
            User.objects.filter(email="email-failure@example.invalid").exists()
        )
        user = User.objects.get(email="email-failure@example.invalid")
        self.assertEqual(user.public_id, "OWN-00001")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(raised.exception.stage, "password setup")
        self.assertEqual(len(mail.outbox), 1)

    @patch("dineva.services.send_welcome_email", side_effect=RuntimeError)
    def test_welcome_email_failure_occurs_after_committed_account_creation(
        self, _send
    ):
        request = self.client.get(reverse("dineva:owner-add")).wsgi_request
        with self.assertRaises(AccountSetupEmailError) as raised:
            create_restaurant_user(
                request=request,
                username="welcome.failure",
                name="Welcome Failure Owner",
                email="welcome-failure@example.invalid",
                restaurant=self.restaurant,
                role=User.Role.OWNER,
            )
        user = User.objects.get(email="welcome-failure@example.invalid")
        self.assertEqual(user.public_id, "OWN-00001")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(raised.exception.stage, "welcome")
        self.assertEqual(len(mail.outbox), 0)


class PublicIdConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        for role in [User.Role.OWNER, User.Role.WAITER, User.Role.KITCHEN_STAFF]:
            UserPublicIdSequence.objects.update_or_create(
                role=role,
                defaults={"next_value": 1},
            )
        self.restaurant = Restaurant.objects.create(
            name="Concurrency Restaurant",
            address="3 Concurrent Street",
            contact="+910000000020",
            email="concurrency@example.invalid",
        )

    def test_concurrent_owner_creation_allocates_unique_public_ids(self):
        barrier = Barrier(2)

        def create_owner(index):
            close_old_connections()
            try:
                restaurant = Restaurant.objects.get(pk=self.restaurant.pk)
                barrier.wait(timeout=10)
                return User.objects.create_user(
                    username=f"concurrent.owner.{index}",
                    email=f"concurrent-{index}@example.invalid",
                    name=f"Concurrent Owner {index}",
                    password=None,
                    role=User.Role.OWNER,
                    restaurant=restaurant,
                ).public_id
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            public_ids = list(executor.map(create_owner, [1, 2]))

        self.assertEqual(set(public_ids), {"OWN-00001", "OWN-00002"})
