from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .access import is_superadmin
from .models import EmployeeIDProof, Restaurant, User, UserProfile, UserPublicIdSequence
from .services import RegistrationEmailError, create_employee
from .test_utils import employee_kwargs, image_upload, password_from_welcome, pdf_upload


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


def form_data(restaurant=None, **overrides):
    data = {
        "name": "Form Employee",
        "email": "form@example.invalid",
        "contact": "+910000000010",
        "address": "10 Form Street",
        "photo": image_upload(),
        "id_proof_type": EmployeeIDProof.ProofType.AADHAAR,
        "id_proof_file": pdf_upload(),
    }
    if restaurant:
        data["restaurant"] = getattr(restaurant, "pk", restaurant)
    data.update(overrides)
    return data


class SuperAdminManagementTests(PrivateMediaMixin, TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username="platform.admin",
            email="admin@example.invalid",
            name="Platform Admin",
            password="Admin-secure-123!",
        )
        self.restaurant = Restaurant.objects.create(
            name="Active Restaurant",
            address="1 Active Street",
            contact="+910000000011",
            email="active@example.invalid",
        )
        self.inactive_restaurant = Restaurant.objects.create(
            name="Inactive Restaurant",
            address="2 Inactive Street",
            contact="+910000000012",
            email="inactive@example.invalid",
            is_active=False,
        )
        self.client.force_login(self.admin)

    def create_employee(self, **overrides):
        data = employee_kwargs(self.restaurant)
        data.update(overrides)
        return create_employee(**data)

    def test_superadmin_guard_requires_complete_platform_identity(self):
        self.assertTrue(is_superadmin(self.admin))
        self.admin.is_superuser = False
        self.assertFalse(is_superadmin(self.admin))

    def test_superadmin_sees_common_shell_and_menu(self):
        response = self.client.get(reverse("dineva:dashboard"))
        self.assertContains(response, 'class="app-topbar"', html=False)
        self.assertContains(response, 'id="appSidebar"', html=False)
        self.assertContains(response, "Platform Admin")
        self.assertContains(response, "Dashboard")
        self.assertContains(response, "Restaurants")
        self.assertContains(response, "Add Restaurant")
        self.assertContains(response, "Owners")
        self.assertContains(response, "Staff")
        self.assertContains(response, "Home")
        self.assertContains(response, "Logout")
        self.assertContains(response, "csrfmiddlewaretoken")

    def test_superadmin_can_create_and_edit_restaurant(self):
        response = self.client.post(
            reverse("dineva:restaurant-add"),
            {"name": "New Restaurant", "address": "3 Street", "contact": "+910000000013", "email": "new@example.invalid", "is_active": "on"},
        )
        restaurant = Restaurant.objects.get(name="New Restaurant")
        self.assertRedirects(response, reverse("dineva:restaurants"))
        response = self.client.post(
            reverse("dineva:restaurant-edit", args=[restaurant.pk]),
            {"name": "Updated Restaurant", "address": restaurant.address, "contact": restaurant.contact, "email": restaurant.email, "is_active": "on"},
        )
        self.assertRedirects(response, reverse("dineva:restaurants"))
        restaurant.refresh_from_db()
        self.assertEqual(restaurant.name, "Updated Restaurant")

    def test_restaurant_status_changes_without_deletion(self):
        self.client.post(reverse("dineva:restaurant-status", args=[self.restaurant.pk]))
        self.restaurant.refresh_from_db()
        self.assertFalse(self.restaurant.is_active)
        self.assertTrue(Restaurant.objects.filter(pk=self.restaurant.pk).exists())

    def test_owner_creation_generates_identity_and_one_email(self):
        response = self.client.post(reverse("dineva:owner-add"), form_data(self.restaurant))
        user = User.objects.get(email="form@example.invalid")
        self.assertRedirects(response, reverse("dineva:owner-list"))
        self.assertEqual(user.username, "form.employee")
        self.assertEqual(user.profile.role, UserProfile.Role.OWNER)
        self.assertEqual(user.profile.public_id, "OWN-00001")
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(user.check_password(password_from_welcome(mail.outbox[0])))

    def test_waiter_and_kitchen_staff_get_role_ids(self):
        for index, (role, prefix) in enumerate(
            [(UserProfile.Role.WAITER, "WTR-"), (UserProfile.Role.KITCHEN_STAFF, "KST-")]
        ):
            response = self.client.post(
                reverse("dineva:staff-add"),
                form_data(
                    self.restaurant,
                    name=f"Staff {index}",
                    email=f"staff{index}@example.invalid",
                    staff_type=role,
                ),
            )
            self.assertEqual(response.status_code, 302)
            profile = User.objects.get(email=f"staff{index}@example.invalid").profile
            self.assertTrue(profile.public_id.startswith(prefix))

    def test_username_role_and_public_id_post_injection_is_ignored(self):
        self.client.post(
            reverse("dineva:owner-add"),
            form_data(
                self.restaurant,
                username="attacker",
                role=UserProfile.Role.SUPERADMIN,
                public_id="WTR-99999",
                is_superuser="on",
            ),
        )
        user = User.objects.get(email="form@example.invalid")
        self.assertEqual(user.username, "form.employee")
        self.assertEqual(user.profile.role, UserProfile.Role.OWNER)
        self.assertEqual(user.profile.public_id, "OWN-00001")
        self.assertFalse(user.is_superuser)

    def test_inactive_restaurant_cannot_receive_employee(self):
        response = self.client.post(reverse("dineva:owner-add"), form_data(self.inactive_restaurant))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="form@example.invalid").exists())

    def test_all_mandatory_employee_fields_are_required(self):
        response = self.client.post(reverse("dineva:owner-add"), {"restaurant": self.restaurant.pk})
        self.assertEqual(response.status_code, 200)
        for field in ("name", "email", "contact", "address", "photo", "id_proof_type", "id_proof_file"):
            self.assertIn(field, response.context["form"].errors)

    def test_welcome_email_contains_credentials_and_no_forbidden_data(self):
        user = self.create_employee()
        message = mail.outbox[-1]
        password = password_from_welcome(message)
        for value in (user.name, user.username, user.email, self.restaurant.name, "Role: Owner", user.profile.public_id, password, "https://dineva.example.invalid/login/"):
            self.assertIn(value, message.body)
        for forbidden in (user.password, str(user.pk), "/account/setup/", "smtp-password", "employee_id_proofs"):
            self.assertNotIn(forbidden, message.body)

    @patch("dineva.services.generate_password", return_value="Secure&Password123!")
    def test_plain_text_email_preserves_special_password_characters(self, _generate):
        user = self.create_employee()
        emailed_password = password_from_welcome(mail.outbox[-1])
        self.assertEqual(emailed_password, "Secure&Password123!")
        self.assertTrue(user.check_password(emailed_password))

    @patch("dineva.services.send_mail", return_value=0)
    def test_clear_email_failure_rolls_back_database_and_private_files(self, _send):
        with self.assertRaises(RegistrationEmailError):
            self.create_employee()
        self.assertFalse(User.objects.filter(email="owner@example.invalid").exists())
        self.assertEqual(UserProfile.objects.filter(role=UserProfile.Role.OWNER).count(), 0)
        self.assertEqual(EmployeeIDProof.objects.count(), 0)
        self.assertEqual(UserPublicIdSequence.objects.get(role=UserProfile.Role.OWNER).next_value, 1)
        self.assertEqual(list(Path(self.private_media.name).rglob("*.*")), [])

    def test_retry_uses_fresh_password_and_leaves_no_duplicate_identity(self):
        passwords = []

        def fail_email(**kwargs):
            passwords.append(kwargs["context"]["generated_password"])
            raise RegistrationEmailError("failed")

        with patch("dineva.services.send_templated_email", side_effect=fail_email):
            for _ in range(2):
                with self.assertRaises(RegistrationEmailError):
                    self.create_employee(photo=image_upload(), id_proof_file=pdf_upload())
        self.assertEqual(len(set(passwords)), 2)
        self.assertFalse(User.objects.filter(email="owner@example.invalid").exists())

    def test_superadmin_can_view_proof_with_private_headers(self):
        user = self.create_employee()
        response = self.client.get(reverse("dineva:id-proof-download", args=[user.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        with self.assertRaises(ValueError):
            user.profile.id_proof.file.url

    def test_superadmin_edit_replaces_proof_and_deletes_old_file(self):
        user = self.create_employee()
        old_name = user.profile.id_proof.file.name
        data = form_data(
            name="Updated Owner",
            email=user.email,
            contact="+910000000099",
            address="Updated Address",
            photo=image_upload("new-photo.png", "PNG"),
            id_proof_type=EmployeeIDProof.ProofType.PAN,
            id_proof_file=pdf_upload("new-proof.pdf"),
        )
        data.pop("restaurant", None)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("dineva:employee-edit", args=[user.pk]), data)
        self.assertRedirects(response, reverse("dineva:employee-detail", args=[user.pk]))
        user.profile.id_proof.refresh_from_db()
        self.assertNotEqual(user.profile.id_proof.file.name, old_name)
        self.assertFalse(user.profile.id_proof.file.storage.exists(old_name))


class OwnerTenantManagementTests(PrivateMediaMixin, TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Owner Restaurant", address="1 Owner Street", contact="+910000000020", email="owner-restaurant@example.invalid")
        self.other_restaurant = Restaurant.objects.create(name="Other Restaurant", address="2 Other Street", contact="+910000000021", email="other@example.invalid")
        self.owner = create_employee(**employee_kwargs(self.restaurant))
        self.owner_password = password_from_welcome(mail.outbox[-1])
        self.staff = create_employee(**employee_kwargs(self.restaurant, role=UserProfile.Role.WAITER, name="Own Waiter", email="own-waiter@example.invalid")).profile
        self.other_staff = create_employee(**employee_kwargs(self.other_restaurant, role=UserProfile.Role.WAITER, name="Other Waiter", email="other-waiter@example.invalid")).profile
        self.client.force_login(self.owner)

    def test_owner_staff_creation_derives_restaurant(self):
        response = self.client.post(
            reverse("dineva:owner-staff-add"),
            form_data(name="New Waiter", email="new-waiter@example.invalid", staff_type=UserProfile.Role.WAITER),
        )
        self.assertRedirects(response, reverse("dineva:owner-staff-list"))
        self.assertEqual(User.objects.get(email="new-waiter@example.invalid").profile.restaurant, self.restaurant)

    def test_owner_restaurant_injection_is_blocked(self):
        response = self.client.post(
            reverse("dineva:owner-staff-add"),
            form_data(name="Injected", email="injected@example.invalid", staff_type=UserProfile.Role.WAITER, restaurant=self.other_restaurant.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="injected@example.invalid").exists())

    def test_owner_cannot_create_owner(self):
        response = self.client.post(
            reverse("dineva:owner-staff-add"),
            form_data(name="Bad Owner", email="bad-owner@example.invalid", staff_type=UserProfile.Role.OWNER),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="bad-owner@example.invalid").exists())

    def test_owner_staff_list_is_tenant_scoped_and_hides_id_proof(self):
        response = self.client.get(reverse("dineva:owner-staff-list"))
        self.assertContains(response, self.staff.user.name)
        self.assertNotContains(response, self.other_staff.user.name)
        self.assertNotContains(response, "ID Proof")

    def test_owner_does_not_see_superadmin_sidebar(self):
        response = self.client.get(reverse("dineva:owner-staff-list"))
        self.assertContains(response, 'id="appSidebar"', html=False)
        self.assertNotContains(response, "Platform Admin")
        self.assertNotContains(response, "Add Restaurant")
        self.assertNotContains(response, "Owners")

    def test_owner_cross_tenant_details_return_404(self):
        response = self.client.get(reverse("dineva:owner-staff-detail", args=[self.other_staff.user_id]))
        self.assertEqual(response.status_code, 404)

    def test_owner_cannot_access_id_proof(self):
        self.assertEqual(self.client.get(reverse("dineva:id-proof-download", args=[self.staff.user_id])).status_code, 403)

    def test_staff_cannot_access_id_proof_or_management(self):
        self.client.force_login(self.staff.user)
        self.assertEqual(self.client.get(reverse("dineva:id-proof-download", args=[self.staff.user_id])).status_code, 403)
        self.assertEqual(self.client.get(reverse("dineva:owner-staff-list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("dineva:dashboard")).status_code, 403)
        response = self.client.get(reverse("dineva:dashboard"))
        self.assertNotContains(response, "Platform Admin", status_code=403)

    def test_owner_can_update_only_staff_photo(self):
        original_name = self.staff.user.name
        response = self.client.post(
            reverse("dineva:owner-staff-photo", args=[self.staff.user_id]),
            {"photo": image_upload("updated.png", "PNG"), "name": "Manipulated", "contact": "bad"},
        )
        self.assertRedirects(response, reverse("dineva:owner-staff-detail", args=[self.staff.user_id]))
        self.staff.user.refresh_from_db()
        self.assertEqual(self.staff.user.name, original_name)

    def test_owner_can_toggle_own_staff_without_deleting(self):
        self.client.post(reverse("dineva:owner-staff-status", args=[self.staff.user_id]))
        self.staff.user.refresh_from_db()
        self.assertFalse(self.staff.user.is_active)
        self.assertTrue(User.objects.filter(pk=self.staff.user_id).exists())

    def test_owner_cannot_access_superadmin_endpoints(self):
        endpoints = ["dashboard", "restaurants", "restaurant-add", "owner-list", "owner-add", "staff-list", "staff-add"]
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.client.get(reverse(f"dineva:{endpoint}")).status_code, 403)

    def test_waiter_and_kitchen_staff_cannot_access_superadmin_endpoints(self):
        kitchen = create_employee(
            **employee_kwargs(
                self.restaurant,
                role=UserProfile.Role.KITCHEN_STAFF,
                name="Own Kitchen",
                email="own-kitchen@example.invalid",
            )
        )
        endpoints = ["dashboard", "restaurants", "restaurant-add", "owner-list", "owner-add", "staff-list", "staff-add"]
        for user in (self.staff.user, kitchen):
            self.client.force_login(user)
            for endpoint in endpoints:
                with self.subTest(user=user.email, endpoint=endpoint):
                    self.assertEqual(self.client.get(reverse(f"dineva:{endpoint}")).status_code, 403)


class UploadValidationTests(PrivateMediaMixin, TestCase):
    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Upload Restaurant", address="1 Upload Street", contact="+910000000030", email="upload@example.invalid")

    def test_jpeg_png_and_pdf_are_accepted(self):
        owner = create_employee(**employee_kwargs(self.restaurant, photo=image_upload("photo.jpeg"), id_proof_file=pdf_upload()))
        waiter = create_employee(**employee_kwargs(self.restaurant, role=UserProfile.Role.WAITER, name="PNG Waiter", email="png@example.invalid", photo=image_upload("photo.png", "PNG"), id_proof_file=image_upload("proof.jpg")))
        self.assertEqual(owner.profile.id_proof.mime_type, "application/pdf")
        self.assertEqual(waiter.profile.id_proof.mime_type, "image/jpeg")

    def test_invalid_extension_and_mime_are_rejected(self):
        with self.assertRaises(ValidationError):
            create_employee(**employee_kwargs(self.restaurant, id_proof_file=pdf_upload("proof.exe")))
        bad_mime = pdf_upload()
        bad_mime.content_type = "text/plain"
        with self.assertRaises(ValidationError):
            create_employee(**employee_kwargs(self.restaurant, id_proof_file=bad_mime))

    def test_oversized_photo_and_proof_are_rejected(self):
        photo = image_upload()
        photo.size = 5 * 1024 * 1024 + 1
        with self.assertRaises(ValidationError):
            create_employee(**employee_kwargs(self.restaurant, photo=photo))
        proof = pdf_upload()
        proof.size = 20 * 1024 * 1024 + 1
        with self.assertRaises(ValidationError):
            create_employee(**employee_kwargs(self.restaurant, id_proof_file=proof))


class IdentityConcurrencyTests(PrivateMediaMixin, TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.restaurant = Restaurant.objects.create(name="Concurrency Restaurant", address="1 Concurrent Street", contact="+910000000040", email="concurrent@example.invalid")

    @patch("dineva.services.send_welcome_email", return_value=1)
    def test_concurrent_creation_allocates_unique_usernames_and_public_ids(self, _send):
        def create(index):
            close_old_connections()
            try:
                restaurant = Restaurant.objects.get(pk=self.restaurant.pk)
                user = create_employee(**employee_kwargs(restaurant, name="Same Name", email=f"same{index}@example.invalid", photo=image_upload(f"photo{index}.jpg"), id_proof_file=pdf_upload(f"proof{index}.pdf")))
                return user.username, user.profile.public_id
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            identities = list(executor.map(create, [1, 2]))
        self.assertEqual({name for name, _ in identities}, {"same.name", "same.name1"})
        self.assertEqual({public_id for _, public_id in identities}, {"OWN-00001", "OWN-00002"})
