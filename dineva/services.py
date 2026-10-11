import logging
import secrets
import string
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from smtplib import SMTPException

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import BadHeaderError, send_mail
from django.db import DEFAULT_DB_ALIAS, IntegrityError, transaction
from django.utils import timezone
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.text import slugify

from .access import account_can_authenticate
from .models import (
    AssignOrder,
    AssignOrderHistory,
    BillDeliveryAttempt,
    Item,
    OrderItem,
    Payment,
    RESTAURANT_ROLE_PUBLIC_ID_PREFIXES,
    EmployeeIDProof,
    RestaurantTable,
    RestaurantTaxProfile,
    SessionInvoice,
    SessionInvoiceLine,
    SessionInvoiceSequence,
    SessionPayment,
    TableSession,
    User,
    UserProfile,
    UserPublicIdSequence,
)
from .validators import inspect_id_proof, inspect_profile_photo


logger = logging.getLogger(__name__)


class RegistrationEmailError(Exception):
    pass


class DuplicateAccountError(Exception):
    pass


class OrderWorkflowError(Exception):
    pass


class PaymentWorkflowError(Exception):
    pass


class TaxProfileError(Exception):
    pass


class InvoiceWorkflowError(Exception):
    pass


def normalize_username_base(name):
    normalized = unicodedata.normalize("NFKC", name or "").strip().lower()
    base = slugify(normalized, allow_unicode=True).replace("-", ".").strip(".")
    return base or "dineva.user"


def generate_password(length=18):
    if length < 16:
        raise ValueError("Generated passwords must contain at least 16 characters.")
    groups = [
        string.ascii_uppercase,
        string.ascii_lowercase,
        string.digits,
        "!@#$%^&*()-_=+",
    ]
    characters = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    characters.extend(secrets.choice(alphabet) for _ in range(length - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def allocate_public_id(role, using=DEFAULT_DB_ALIAS):
    prefix = RESTAURANT_ROLE_PUBLIC_ID_PREFIXES.get(role)
    if prefix is None:
        raise ValueError("Public IDs are available only for restaurant roles.")
    sequence = (
        UserPublicIdSequence.objects.using(using)
        .select_for_update()
        .get(role=role)
    )
    public_id = f"{prefix}-{sequence.next_value:05d}"
    sequence.next_value += 1
    sequence.save(using=using, update_fields=["next_value"])
    return public_id


def _create_user_with_generated_username(*, name, email, password, using):
    normalized_email = User.objects.normalize_email(email).strip().lower()
    if User.objects.using(using).filter(email__iexact=normalized_email).exists():
        raise DuplicateAccountError("A user with this email already exists.")

    base = normalize_username_base(name)
    for suffix in range(10000):
        suffix_text = "" if suffix == 0 else str(suffix)
        candidate = f"{base[:150 - len(suffix_text)]}{suffix_text}"
        user = User(username=candidate, email=normalized_email, name=name.strip())
        user.set_password(password)
        try:
            with transaction.atomic(using=using):
                user.full_clean(validate_unique=False)
                user.save(using=using, force_insert=True)
        except IntegrityError:
            if User.objects.using(using).filter(username__iexact=candidate).exists():
                continue
            raise DuplicateAccountError("A user with this email already exists.")
        return user
    raise RuntimeError("Unable to allocate a unique username.")


def send_templated_email(*, subject, template_name, context, recipient):
    body = render_to_string(template_name, context)
    try:
        accepted = send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=False,
        )
    except (BadHeaderError, OSError, SMTPException) as exc:
        raise RegistrationEmailError("The Welcome Email could not be sent.") from exc
    if accepted != 1:
        raise RegistrationEmailError("The email server did not accept the message.")
    return accepted


def send_welcome_email(*, user, password):
    profile = user.profile
    public_id_labels = {
        UserProfile.Role.OWNER: "Owner ID",
        UserProfile.Role.WAITER: "Waiter ID",
        UserProfile.Role.KITCHEN_STAFF: "Kitchen Staff ID",
    }
    return send_templated_email(
        subject="Welcome to Dineva",
        template_name="dineva/emails/welcome.txt",
        context={
            "user": user,
            "profile": profile,
            "restaurant": profile.restaurant,
            "role_label": profile.get_role_display(),
            "public_id_label": public_id_labels[profile.role],
            "generated_password": password,
            "login_url": f"{settings.DINEVA_PUBLIC_BASE_URL}{reverse('dineva:login')}",
        },
        recipient=user.email,
    )


def create_employee(
    *,
    name,
    email,
    contact,
    address,
    photo,
    id_proof_type,
    id_proof_file,
    restaurant,
    role,
):
    allowed_roles = {
        UserProfile.Role.OWNER,
        UserProfile.Role.WAITER,
        UserProfile.Role.KITCHEN_STAFF,
    }
    if role not in allowed_roles:
        raise ValueError("Unsupported restaurant role.")
    if not restaurant.is_active:
        raise ValidationError("Employees require an active restaurant.")

    inspect_profile_photo(photo)
    proof_metadata = inspect_id_proof(id_proof_file)
    password = generate_password()
    created_files = []
    using = DEFAULT_DB_ALIAS

    try:
        with transaction.atomic(using=using):
            active_restaurant = type(restaurant).objects.select_for_update().get(
                pk=restaurant.pk,
                is_active=True,
            )
            user = _create_user_with_generated_username(
                name=name,
                email=email,
                password=password,
                using=using,
            )
            profile = UserProfile(
                user=user,
                role=role,
                restaurant=active_restaurant,
                public_id=allocate_public_id(role, using),
                contact=contact.strip(),
                address=address.strip(),
                photo=photo,
            )
            profile.full_clean()
            profile.save(using=using)
            created_files.append((profile.photo.storage, profile.photo.name))

            proof = EmployeeIDProof(
                profile=profile,
                proof_type=id_proof_type,
                file=id_proof_file,
                **proof_metadata,
            )
            proof.full_clean()
            proof.save(using=using)
            created_files.append((proof.file.storage, proof.file.name))

            # SMTP acceptance and the following MySQL commit cannot be atomic.
            # A post-acceptance commit failure remains an unavoidable residual window.
            send_welcome_email(user=user, password=password)
        return user
    except Exception:
        for storage, name in reversed(created_files):
            if name:
                storage.delete(name)
        raise


def replace_private_file(instance, field_name, new_file, metadata=None):
    field = getattr(instance, field_name)
    old_storage = field.storage
    old_name = field.name
    new_name = None
    try:
        with transaction.atomic():
            setattr(instance, field_name, new_file)
            if metadata:
                for key, value in metadata.items():
                    setattr(instance, key, value)
            instance.full_clean()
            instance.save()
            new_field = getattr(instance, field_name)
            new_name = new_field.name
            if old_name and old_name != new_name:
                transaction.on_commit(lambda: old_storage.delete(old_name))
    except Exception:
        if new_name:
            getattr(instance, field_name).storage.delete(new_name)
        raise
    return instance


def update_employee_profile(*, profile, data):
    user = profile.user
    proof = profile.id_proof
    new_photo = data.get("photo")
    new_proof_file = data.get("id_proof_file")
    proof_metadata = inspect_id_proof(new_proof_file) if new_proof_file else None
    old_files = []
    new_files = []

    try:
        with transaction.atomic():
            user.name = data["name"].strip()
            user.email = User.objects.normalize_email(data["email"]).strip().lower()
            user.full_clean()
            user.save(update_fields=["name", "email", "updated_at"])

            profile.contact = data["contact"].strip()
            profile.address = data["address"].strip()
            if new_photo:
                inspect_profile_photo(new_photo)
                old_files.append((profile.photo.storage, profile.photo.name))
                profile.photo = new_photo
            profile.full_clean()
            profile.save(update_fields=["contact", "address", "photo", "updated_at"])
            if new_photo:
                new_files.append((profile.photo.storage, profile.photo.name))

            proof.proof_type = data["id_proof_type"]
            if new_proof_file:
                old_files.append((proof.file.storage, proof.file.name))
                proof.file = new_proof_file
                for key, value in proof_metadata.items():
                    setattr(proof, key, value)
            proof.full_clean()
            proof.save()
            if new_proof_file:
                new_files.append((proof.file.storage, proof.file.name))

            for storage, name in old_files:
                if name:
                    transaction.on_commit(lambda s=storage, n=name: s.delete(n))
    except Exception:
        for storage, name in new_files:
            if name:
                storage.delete(name)
        raise
    return profile


def update_staff_photo(*, profile, photo):
    inspect_profile_photo(photo)
    return replace_private_file(profile, "photo", photo)


def table_has_active_order(table):
    blocking_session_statuses = [
        TableSession.Status.OPEN,
        TableSession.Status.PAYMENT_PENDING,
    ]
    return TableSession.objects.filter(
        restaurant_id=table.restaurant_id,
        table_id=table.pk,
        status__in=blocking_session_statuses,
    ).exists() or AssignOrder.objects.filter(
        restaurant_id=table.restaurant_id,
        table_id=table.pk,
        order_type=AssignOrder.OrderType.DINE_IN,
        status=AssignOrder.Status.ACTIVE,
    ).exists()


def _restaurant_staff_profile(user, role):
    if not account_can_authenticate(user):
        raise OrderWorkflowError("This account is not eligible for restaurant operations.")
    profile = user.profile
    if profile.role != role or user.is_superuser or profile.restaurant_id is None:
        raise OrderWorkflowError("This account is not authorized for this operation.")
    return profile


def create_waiter_order(*, waiter, order_type, table, order_lines, idempotency_key):
    waiter_profile = _restaurant_staff_profile(waiter, UserProfile.Role.WAITER)
    restaurant = waiter_profile.restaurant
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key:
        raise OrderWorkflowError("Order submission could not be verified.")
    if len(idempotency_key) > 64:
        raise OrderWorkflowError("Order submission could not be verified.")
    if order_type not in AssignOrder.OrderType.values:
        raise OrderWorkflowError("Unsupported order type.")
    if not order_lines:
        raise OrderWorkflowError("At least one order item is required.")

    if AssignOrder.objects.filter(idempotency_key=idempotency_key).exists():
        raise OrderWorkflowError("This order submission was already processed.")

    with transaction.atomic():
        locked_table = None
        table_session = None
        if order_type == AssignOrder.OrderType.DINE_IN:
            if table is None:
                raise OrderWorkflowError("Dine-in orders require a table.")
            locked_table = (
                RestaurantTable.objects.select_for_update()
                .filter(
                    pk=table.pk,
                    restaurant=restaurant,
                    is_active=True,
                    status=RestaurantTable.Status.AVAILABLE,
                )
                .first()
            )
            if locked_table is None:
                raise OrderWorkflowError("Select an active available table.")
            if TableSession.objects.select_for_update().filter(
                restaurant=restaurant,
                table=locked_table,
                status__in=[
                    TableSession.Status.OPEN,
                    TableSession.Status.PAYMENT_PENDING,
                ],
            ).exists():
                raise OrderWorkflowError("This table already has an active session.")
            if AssignOrder.objects.select_for_update().filter(
                restaurant=restaurant,
                table=locked_table,
                order_type=AssignOrder.OrderType.DINE_IN,
                status=AssignOrder.Status.ACTIVE,
            ).exists():
                raise OrderWorkflowError("This table already has an active order.")
            table_session = TableSession(
                restaurant=restaurant,
                table=locked_table,
                opened_by=waiter,
            )
            table_session.full_clean()
            table_session.save()
        elif table is not None:
            raise OrderWorkflowError("Takeaway orders cannot be assigned to a table.")

        requested_quantities = {
            line["item"].pk: line["quantity"]
            for line in order_lines
            if line["quantity"] > 0
        }
        locked_items = {
            item.pk: item
            for item in Item.objects.select_for_update().filter(
                pk__in=requested_quantities.keys(),
                restaurant=restaurant,
                is_active=True,
                is_available=True,
            )
        }
        if set(locked_items) != set(requested_quantities):
            raise OrderWorkflowError("One or more selected items are no longer available.")

        assign_order = AssignOrder(
            restaurant=restaurant,
            table=locked_table,
            table_session=table_session,
            waiter=waiter,
            order_type=order_type,
            total_amount=Decimal("0.00"),
            idempotency_key=idempotency_key,
        )
        try:
            assign_order.full_clean()
            assign_order.save()
        except (IntegrityError, ValidationError) as exc:
            if AssignOrder.objects.filter(idempotency_key=idempotency_key).exists():
                raise OrderWorkflowError("This order submission was already processed.") from exc
            raise

        order_items = []
        total_amount = Decimal("0.00")
        for item_id, quantity in requested_quantities.items():
            item = locked_items[item_id]
            line_total = item.price * quantity
            total_amount += line_total
            order_items.append(
                OrderItem(
                    assign_order=assign_order,
                    item=item,
                    quantity=quantity,
                    price=item.price,
                )
            )
        for order_item in order_items:
            order_item.full_clean()
            order_item.save()

        assign_order.total_amount = total_amount
        assign_order.full_clean()
        assign_order.save(update_fields=["total_amount", "updated_at"])

        history_entries = [
            AssignOrderHistory(
                assign_order=assign_order,
                restaurant=restaurant,
                user=waiter,
                action=AssignOrderHistory.Action.ORDER_CREATED,
                description=f"{assign_order.get_order_type_display()} order created.",
            ),
            AssignOrderHistory(
                assign_order=assign_order,
                restaurant=restaurant,
                user=waiter,
                action=AssignOrderHistory.Action.ITEM_ADDED,
                description=f"{len(order_items)} menu line(s) added.",
            ),
        ]
        if locked_table:
            locked_table.status = RestaurantTable.Status.OCCUPIED
            locked_table.save(update_fields=["status", "updated_at"])
            history_entries.append(
                AssignOrderHistory(
                    assign_order=assign_order,
                    restaurant=restaurant,
                    user=waiter,
                    action=AssignOrderHistory.Action.TABLE_OCCUPIED,
                    description=f"Table {locked_table.table_no} occupied.",
                )
            )
        for history_entry in history_entries:
            history_entry.full_clean()
            history_entry.save()
    return assign_order


def _requested_order_items(order_lines):
    return {
        line["item"].pk: line["quantity"]
        for line in order_lines
        if line["quantity"] > 0
    }


def _create_order_for_session(*, restaurant, waiter, table_session, table, order_lines, idempotency_key):
    requested_quantities = _requested_order_items(order_lines)
    locked_items = {
        item.pk: item
        for item in Item.objects.select_for_update().filter(
            pk__in=requested_quantities.keys(),
            restaurant=restaurant,
            is_active=True,
            is_available=True,
        )
    }
    if set(locked_items) != set(requested_quantities):
        raise OrderWorkflowError("One or more selected items are no longer available.")

    assign_order = AssignOrder(
        restaurant=restaurant,
        table=table,
        table_session=table_session,
        waiter=waiter,
        order_type=AssignOrder.OrderType.DINE_IN,
        total_amount=Decimal("0.00"),
        idempotency_key=idempotency_key,
    )
    try:
        assign_order.full_clean()
        assign_order.save()
    except (IntegrityError, ValidationError) as exc:
        if AssignOrder.objects.filter(idempotency_key=idempotency_key).exists():
            raise OrderWorkflowError("This order submission was already processed.") from exc
        raise

    order_items = []
    total_amount = Decimal("0.00")
    for item_id, quantity in requested_quantities.items():
        item = locked_items[item_id]
        line_total = item.price * quantity
        total_amount += line_total
        order_items.append(
            OrderItem(
                assign_order=assign_order,
                item=item,
                quantity=quantity,
                price=item.price,
            )
        )
    for order_item in order_items:
        order_item.full_clean()
        order_item.save()

    assign_order.total_amount = total_amount
    assign_order.full_clean()
    assign_order.save(update_fields=["total_amount", "updated_at"])
    history_entries = [
        AssignOrderHistory(
            assign_order=assign_order,
            restaurant=restaurant,
            user=waiter,
            action=AssignOrderHistory.Action.ORDER_CREATED,
            description="Additional dine-in order round created.",
        ),
        AssignOrderHistory(
            assign_order=assign_order,
            restaurant=restaurant,
            user=waiter,
            action=AssignOrderHistory.Action.ITEM_ADDED,
            description=f"{len(order_items)} menu line(s) added.",
        ),
    ]
    for history_entry in history_entries:
        history_entry.full_clean()
        history_entry.save()
    return assign_order


def add_waiter_session_round(*, waiter, session_id, order_lines, idempotency_key):
    waiter_profile = _restaurant_staff_profile(waiter, UserProfile.Role.WAITER)
    restaurant = waiter_profile.restaurant
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key or len(idempotency_key) > 64:
        raise OrderWorkflowError("Order submission could not be verified.")
    if not order_lines:
        raise OrderWorkflowError("At least one order item is required.")
    if AssignOrder.objects.filter(idempotency_key=idempotency_key).exists():
        raise OrderWorkflowError("This order submission was already processed.")

    with transaction.atomic():
        table_session = (
            TableSession.objects.select_for_update()
            .select_related("table")
            .filter(
                pk=session_id,
                restaurant=restaurant,
                status=TableSession.Status.OPEN,
                table__is_active=True,
            )
            .first()
        )
        if table_session is None or table_session.table_id is None:
            raise OrderWorkflowError("Open table session was not found.")
        if hasattr(table_session, "invoice"):
            raise OrderWorkflowError("This session already has a finalized bill.")
        locked_table = (
            RestaurantTable.objects.select_for_update()
            .filter(
                pk=table_session.table_id,
                restaurant=restaurant,
                status=RestaurantTable.Status.OCCUPIED,
                is_active=True,
            )
            .first()
        )
        if locked_table is None:
            raise OrderWorkflowError("This table is not available for additional rounds.")
        return _create_order_for_session(
            restaurant=restaurant,
            waiter=waiter,
            table_session=table_session,
            table=locked_table,
            order_lines=order_lines,
            idempotency_key=idempotency_key,
        )


def mark_waiter_order_item_served(*, waiter, order_item_id):
    waiter_profile = _restaurant_staff_profile(waiter, UserProfile.Role.WAITER)
    restaurant = waiter_profile.restaurant
    with transaction.atomic():
        order_item = (
            OrderItem.objects.select_for_update()
            .select_related("assign_order", "item")
            .filter(
                pk=order_item_id,
                assign_order__restaurant=restaurant,
                assign_order__status=AssignOrder.Status.ACTIVE,
                status=OrderItem.Status.READY,
            )
            .first()
        )
        if order_item is None:
            raise OrderWorkflowError("Ready order item was not found.")
        if order_item.assign_order.table_session_id:
            session = (
                TableSession.objects.select_for_update()
                .filter(
                    pk=order_item.assign_order.table_session_id,
                    restaurant=restaurant,
                    status__in=[TableSession.Status.OPEN, TableSession.Status.PAYMENT_PENDING],
                )
                .first()
            )
            if session is None:
                raise OrderWorkflowError("This session is not open for serving updates.")
        order_item.status = OrderItem.Status.SERVED
        order_item.full_clean()
        order_item.save(update_fields=["status", "updated_at"])
        history_entry = AssignOrderHistory(
            assign_order=order_item.assign_order,
            restaurant=restaurant,
            user=waiter,
            action=AssignOrderHistory.Action.SERVED,
            description=f"{order_item.item.name} marked served.",
        )
        history_entry.full_clean()
        history_entry.save()
    return order_item


def transition_kitchen_order_item(*, actor, order_item_id, target_status):
    kitchen_profile = _restaurant_staff_profile(actor, UserProfile.Role.KITCHEN_STAFF)
    restaurant = kitchen_profile.restaurant
    allowed_transitions = {
        OrderItem.Status.PENDING: {OrderItem.Status.PREPARING},
        OrderItem.Status.PREPARING: {OrderItem.Status.READY},
    }
    if target_status not in {OrderItem.Status.PREPARING, OrderItem.Status.READY}:
        raise OrderWorkflowError("Unsupported kitchen status.")

    with transaction.atomic():
        order_item = (
            OrderItem.objects.select_for_update()
            .select_related("assign_order", "item")
            .filter(
                pk=order_item_id,
                assign_order__restaurant=restaurant,
                assign_order__status=AssignOrder.Status.ACTIVE,
            )
            .first()
        )
        if order_item is None:
            raise OrderWorkflowError("Order item was not found.")
        valid_targets = allowed_transitions.get(order_item.status, set())
        if target_status not in valid_targets:
            raise OrderWorkflowError("This status change is not allowed.")
        order_item.status = target_status
        order_item.full_clean()
        order_item.save(update_fields=["status", "updated_at"])
        action = (
            AssignOrderHistory.Action.PREPARING
            if target_status == OrderItem.Status.PREPARING
            else AssignOrderHistory.Action.READY
        )
        history_entry = AssignOrderHistory(
            assign_order=order_item.assign_order,
            restaurant=restaurant,
            user=actor,
            action=action,
            description=f"{order_item.item.name} marked {order_item.get_status_display().lower()}.",
        )
        history_entry.full_clean()
        history_entry.save()
    return order_item


def successful_payment_total(assign_order):
    return sum(
        (
            payment.amount
            for payment in assign_order.payments.filter(payment_status=Payment.Status.SUCCESS)
        ),
        Decimal("0.00"),
    )


def _money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _financial_year(for_date):
    year = for_date.year
    if for_date.month < 4:
        start = year - 1
    else:
        start = year
    return f"{start}-{str(start + 1)[-2:]}"


def _next_invoice_number(restaurant, invoice_date):
    financial_year = _financial_year(invoice_date.date())
    try:
        sequence, _ = SessionInvoiceSequence.objects.select_for_update().get_or_create(
            restaurant=restaurant,
            financial_year=financial_year,
            defaults={"next_number": 1},
        )
    except IntegrityError:
        sequence = SessionInvoiceSequence.objects.select_for_update().get(
            restaurant=restaurant,
            financial_year=financial_year,
        )
    number = sequence.next_number
    sequence.next_number += 1
    sequence.full_clean()
    sequence.save(update_fields=["next_number", "updated_at"])
    return financial_year, f"{financial_year}/{number:05d}"


def _verified_tax_profile(restaurant):
    try:
        tax_profile = restaurant.tax_profile
    except RestaurantTaxProfile.DoesNotExist as exc:
        raise TaxProfileError("Restaurant GST profile is missing.") from exc
    if tax_profile.configuration_status != RestaurantTaxProfile.ConfigurationStatus.VERIFIED:
        raise TaxProfileError("Restaurant GST profile is not verified.")
    tax_profile.full_clean()
    return tax_profile


def _tax_rate_for_profile(tax_profile):
    if tax_profile.gst_treatment == RestaurantTaxProfile.Treatment.ORDINARY_RESTAURANT_5_NO_ITC:
        return Decimal("5.00"), SessionInvoice.DocumentType.TAX_INVOICE
    if tax_profile.gst_treatment == RestaurantTaxProfile.Treatment.SPECIFIED_PREMISES_18_WITH_ITC:
        return Decimal("18.00"), SessionInvoice.DocumentType.TAX_INVOICE
    if tax_profile.gst_treatment == RestaurantTaxProfile.Treatment.COMPOSITION_NO_TAX_COLLECTED:
        return Decimal("0.00"), SessionInvoice.DocumentType.BILL_OF_SUPPLY
    if tax_profile.gst_treatment == RestaurantTaxProfile.Treatment.UNREGISTERED_NO_GST:
        return Decimal("0.00"), SessionInvoice.DocumentType.NON_GST_BILL
    raise TaxProfileError("Unsupported GST treatment.")


def calculate_gst_line(*, gross_amount, tax_rate, prices_include_gst, supplier_state_code):
    gross_amount = _money(gross_amount)
    tax_rate = Decimal(tax_rate)
    if tax_rate == Decimal("0.00"):
        return {
            "taxable_value": gross_amount,
            "cgst_amount": Decimal("0.00"),
            "sgst_amount": Decimal("0.00"),
            "igst_amount": Decimal("0.00"),
            "line_total": gross_amount,
        }
    if supplier_state_code != "29":
        raise TaxProfileError("Only Karnataka intra-state GST calculation is configured for this phase.")
    if prices_include_gst:
        taxable_value = _money(gross_amount * Decimal("100.00") / (Decimal("100.00") + tax_rate))
        total_gst = gross_amount - taxable_value
        line_total = gross_amount
    else:
        taxable_value = gross_amount
        total_gst = _money(taxable_value * tax_rate / Decimal("100.00"))
        line_total = taxable_value + total_gst
    cgst_amount = _money(total_gst / Decimal("2.00"))
    sgst_amount = total_gst - cgst_amount
    return {
        "taxable_value": taxable_value,
        "cgst_amount": cgst_amount,
        "sgst_amount": sgst_amount,
        "igst_amount": Decimal("0.00"),
        "line_total": line_total,
    }


def _session_orders_for_update(table_session):
    return (
        AssignOrder.objects.select_for_update()
        .filter(
            table_session=table_session,
            restaurant=table_session.restaurant,
        )
        .prefetch_related("items__item")
        .order_by("created_at")
    )


def _session_payments_total(invoice):
    return sum(
        (
            payment.amount
            for payment in invoice.payments.filter(payment_status=SessionPayment.Status.SUCCESS)
        ),
        Decimal("0.00"),
    )


def finalize_session_invoice(
    *,
    waiter,
    session_id,
    customer_name="",
    customer_email="",
    customer_mobile="",
):
    waiter_profile = _restaurant_staff_profile(waiter, UserProfile.Role.WAITER)
    restaurant = waiter_profile.restaurant
    with transaction.atomic():
        table_session = (
            TableSession.objects.select_for_update()
            .select_related("table", "restaurant")
            .filter(
                pk=session_id,
                restaurant=restaurant,
                status=TableSession.Status.OPEN,
            )
            .first()
        )
        if table_session is None or table_session.table_id is None:
            raise InvoiceWorkflowError("Open table session was not found.")
        if hasattr(table_session, "invoice"):
            return table_session.invoice

        tax_profile = _verified_tax_profile(restaurant)
        tax_rate, document_type = _tax_rate_for_profile(tax_profile)
        orders = list(_session_orders_for_update(table_session))
        if not orders:
            raise InvoiceWorkflowError("This session has no orders to bill.")
        order_items = [
            order_item
            for order in orders
            if order.status != AssignOrder.Status.CANCELLED
            for order_item in order.items.all()
        ]
        unresolved_items = [
            order_item
            for order_item in order_items
            if order_item.status not in {OrderItem.Status.SERVED, OrderItem.Status.CANCELLED}
        ]
        if unresolved_items:
            raise InvoiceWorkflowError("All items must be served or cancelled before checkout.")
        billable_items = [
            order_item for order_item in order_items if order_item.status == OrderItem.Status.SERVED
        ]
        if not billable_items:
            raise InvoiceWorkflowError("There are no served items to bill.")

        now = timezone.now()
        financial_year, invoice_number = _next_invoice_number(restaurant, now)
        line_rows = []
        subtotal = Decimal("0.00")
        taxable_total = Decimal("0.00")
        cgst_total = Decimal("0.00")
        sgst_total = Decimal("0.00")
        igst_total = Decimal("0.00")
        final_total = Decimal("0.00")
        for order_item in billable_items:
            line_subtotal = _money(order_item.price * order_item.quantity)
            tax = calculate_gst_line(
                gross_amount=line_subtotal,
                tax_rate=tax_rate,
                prices_include_gst=tax_profile.prices_include_gst,
                supplier_state_code=tax_profile.state_code,
            )
            subtotal += line_subtotal
            taxable_total += tax["taxable_value"]
            cgst_total += tax["cgst_amount"]
            sgst_total += tax["sgst_amount"]
            igst_total += tax["igst_amount"]
            final_total += tax["line_total"]
            line_rows.append((order_item, line_subtotal, tax))
        subtotal = _money(subtotal)
        taxable_total = _money(taxable_total)
        cgst_total = _money(cgst_total)
        sgst_total = _money(sgst_total)
        igst_total = _money(igst_total)
        total_gst = _money(cgst_total + sgst_total + igst_total)
        final_total = _money(final_total)

        invoice = SessionInvoice(
            restaurant=restaurant,
            table_session=table_session,
            invoice_number=invoice_number,
            financial_year=financial_year,
            invoice_date=now,
            document_type=document_type,
            tax_profile=tax_profile,
            supplier_legal_name=tax_profile.legal_name or restaurant.name,
            supplier_trade_name=tax_profile.trade_name or restaurant.name,
            supplier_address=tax_profile.business_address or restaurant.address,
            supplier_gstin=tax_profile.gstin,
            supplier_state_name=tax_profile.state_name,
            supplier_state_code=tax_profile.state_code,
            tax_treatment=tax_profile.gst_treatment,
            prices_include_gst=tax_profile.prices_include_gst,
            customer_name=(customer_name or "").strip(),
            customer_email=(customer_email or "").strip(),
            customer_mobile=(customer_mobile or "").strip(),
            subtotal=subtotal,
            taxable_value=taxable_total,
            cgst_amount=cgst_total,
            sgst_amount=sgst_total,
            igst_amount=igst_total,
            total_gst=total_gst,
            rounding_adjustment=Decimal("0.00"),
            grand_total=final_total,
            finalized_by=waiter,
            finalized_at=now,
        )
        invoice.full_clean()
        invoice.save()
        for order_item, line_subtotal, tax in line_rows:
            line = SessionInvoiceLine(
                invoice=invoice,
                order_item=order_item,
                description=order_item.item.name,
                quantity=order_item.quantity,
                unit_price=order_item.price,
                line_subtotal=line_subtotal,
                taxable_value=tax["taxable_value"],
                gst_rate=tax_rate,
                cgst_amount=tax["cgst_amount"],
                sgst_amount=tax["sgst_amount"],
                igst_amount=tax["igst_amount"],
                line_total=tax["line_total"],
            )
            line.full_clean()
            line.save()

        table_session.status = TableSession.Status.PAYMENT_PENDING
        table_session.full_clean()
        table_session.save(update_fields=["status", "updated_at"])
        for order in orders:
            history_entry = AssignOrderHistory(
                assign_order=order,
                restaurant=restaurant,
                user=waiter,
                action=AssignOrderHistory.Action.INVOICE_FINALIZED,
                description=f"Session invoice {invoice.invoice_number} finalized.",
            )
            history_entry.full_clean()
            history_entry.save()
    return invoice


def record_waiter_session_payment(*, waiter, invoice_id, payment_method, amount, reference="", idempotency_key=""):
    waiter_profile = _restaurant_staff_profile(waiter, UserProfile.Role.WAITER)
    restaurant = waiter_profile.restaurant
    idempotency_key = (idempotency_key or "").strip()
    if not idempotency_key or len(idempotency_key) > 64:
        raise PaymentWorkflowError("Payment submission could not be verified.")
    if SessionPayment.objects.filter(idempotency_key=idempotency_key).exists():
        raise PaymentWorkflowError("This payment submission was already processed.")
    if payment_method not in SessionPayment.Method.values:
        raise PaymentWorkflowError("Unsupported payment method.")
    amount = _money(amount)
    with transaction.atomic():
        invoice = (
            SessionInvoice.objects.select_for_update()
            .select_related("table_session")
            .filter(pk=invoice_id, restaurant=restaurant, status=SessionInvoice.Status.FINALIZED)
            .first()
        )
        if invoice is None:
            raise PaymentWorkflowError("Finalized invoice was not found.")
        TableSession.objects.select_for_update().get(pk=invoice.table_session_id, restaurant=restaurant)
        pending_payments = SessionPayment.objects.select_for_update().filter(
            invoice=invoice,
            restaurant=restaurant,
            payment_status=SessionPayment.Status.PENDING,
        )
        if pending_payments.exists():
            raise PaymentWorkflowError("This invoice has a pending payment attempt.")
        paid_total = _session_payments_total(invoice)
        if paid_total >= invoice.grand_total:
            raise PaymentWorkflowError("This invoice is already paid.")
        balance = invoice.grand_total - paid_total
        if amount != balance:
            raise PaymentWorkflowError("Payment amount must match the current invoice balance.")
        payment = SessionPayment(
            restaurant=restaurant,
            invoice=invoice,
            amount=amount,
            payment_method=payment_method,
            payment_status=SessionPayment.Status.SUCCESS,
            reference=(reference or "").strip(),
            recorded_by=waiter,
            paid_at=timezone.now(),
            idempotency_key=idempotency_key,
        )
        payment.full_clean()
        payment.save()
    return payment


def _render_invoice_text(invoice):
    lines = [
        f"{invoice.supplier_trade_name or invoice.supplier_legal_name}",
        f"Invoice: {invoice.invoice_number}",
        f"Date: {timezone.localtime(invoice.invoice_date).strftime('%Y-%m-%d %H:%M')}",
        f"Document: {invoice.get_document_type_display()}",
        "",
        "Items:",
    ]
    for line in invoice.lines.select_related("order_item__item").all():
        lines.append(
            f"- {line.description} x {line.quantity}: Rs. {line.line_total}"
        )
    lines.extend(
        [
            "",
            f"Taxable value: Rs. {invoice.taxable_value}",
            f"CGST: Rs. {invoice.cgst_amount}",
            f"SGST: Rs. {invoice.sgst_amount}",
            f"IGST: Rs. {invoice.igst_amount}",
            f"Total GST: Rs. {invoice.total_gst}",
            f"Final payable: Rs. {invoice.grand_total}",
        ]
    )
    return "\n".join(lines)


def send_invoice_email(invoice):
    destination = (invoice.customer_email or "").strip()
    attempt = BillDeliveryAttempt(
        invoice=invoice,
        channel=BillDeliveryAttempt.Channel.EMAIL,
        destination=destination,
    )
    if not destination:
        attempt.status = BillDeliveryAttempt.Status.SKIPPED
        attempt.error_message = "Customer email was not provided."
        attempt.full_clean()
        attempt.save()
        return attempt
    try:
        accepted = send_mail(
            subject=f"{invoice.supplier_trade_name or 'Dineva'} bill {invoice.invoice_number}",
            message=_render_invoice_text(invoice),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[destination],
            fail_silently=False,
        )
    except (BadHeaderError, SMTPException, OSError) as exc:
        attempt.status = BillDeliveryAttempt.Status.FAILED
        attempt.error_message = str(exc)[:1000]
    else:
        attempt.status = BillDeliveryAttempt.Status.SENT if accepted else BillDeliveryAttempt.Status.FAILED
        if not accepted:
            attempt.error_message = "Email backend did not accept the message."
    attempt.full_clean()
    attempt.save()
    return attempt


def send_invoice_email_once(invoice):
    existing_attempt = invoice.delivery_attempts.filter(
        channel=BillDeliveryAttempt.Channel.EMAIL
    ).order_by("-created_at").first()
    if existing_attempt:
        return existing_attempt
    return send_invoice_email(invoice)


def record_whatsapp_unavailable(invoice):
    attempt = BillDeliveryAttempt(
        invoice=invoice,
        channel=BillDeliveryAttempt.Channel.WHATSAPP,
        destination=invoice.customer_mobile,
        status=BillDeliveryAttempt.Status.SKIPPED,
        error_message="WhatsApp provider integration is not configured.",
    )
    attempt.full_clean()
    attempt.save()
    return attempt


def close_waiter_table_session(*, waiter, session_id):
    waiter_profile = _restaurant_staff_profile(waiter, UserProfile.Role.WAITER)
    restaurant = waiter_profile.restaurant
    with transaction.atomic():
        table_session = (
            TableSession.objects.select_for_update()
            .select_related("table")
            .filter(
                pk=session_id,
                restaurant=restaurant,
                status=TableSession.Status.PAYMENT_PENDING,
            )
            .first()
        )
        if table_session is None or table_session.table_id is None:
            closed_session = (
                TableSession.objects.select_for_update()
                .filter(
                    pk=session_id,
                    restaurant=restaurant,
                    status=TableSession.Status.CLOSED,
                )
                .first()
            )
            if closed_session is not None:
                return closed_session
            raise InvoiceWorkflowError("Payment-pending table session was not found.")
        invoice = (
            SessionInvoice.objects.select_for_update()
            .filter(table_session=table_session, restaurant=restaurant, status=SessionInvoice.Status.FINALIZED)
            .first()
        )
        if invoice is None:
            raise InvoiceWorkflowError("Finalized invoice was not found.")
        if SessionPayment.objects.select_for_update().filter(
            invoice=invoice,
            restaurant=restaurant,
            payment_status=SessionPayment.Status.PENDING,
        ).exists():
            raise InvoiceWorkflowError("Pending payment attempts must be resolved before closing.")
        if _session_payments_total(invoice) < invoice.grand_total:
            raise InvoiceWorkflowError("The finalized bill is not fully paid.")
        orders = list(_session_orders_for_update(table_session))
        unresolved_items = OrderItem.objects.select_for_update().filter(
            assign_order__in=orders,
        ).exclude(status__in=[OrderItem.Status.SERVED, OrderItem.Status.CANCELLED])
        if unresolved_items.exists():
            raise InvoiceWorkflowError("All order items must be served or cancelled before closing.")

        locked_table = RestaurantTable.objects.select_for_update().get(
            pk=table_session.table_id,
            restaurant=restaurant,
        )
        table_session.status = TableSession.Status.CLOSED
        table_session.closed_at = timezone.now()
        table_session.closed_by = waiter
        table_session.full_clean()
        table_session.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
        locked_table.status = RestaurantTable.Status.AVAILABLE
        locked_table.save(update_fields=["status", "updated_at"])
        for order in orders:
            order.status = AssignOrder.Status.COMPLETED
            order.full_clean()
            order.save(update_fields=["status", "updated_at"])
            history_entry = AssignOrderHistory(
                assign_order=order,
                restaurant=restaurant,
                user=waiter,
                action=AssignOrderHistory.Action.SESSION_CLOSED,
                description=f"Session closed and table {locked_table.table_no} released.",
            )
            history_entry.full_clean()
            history_entry.save()
    try:
        send_invoice_email_once(invoice)
    except Exception:
        logger.exception("Failed to record or send session invoice email.")
    try:
        record_whatsapp_unavailable(invoice)
    except Exception:
        logger.exception("Failed to record unavailable WhatsApp bill delivery.")
    return table_session


def record_owner_order_payment(*, owner, order_id, payment_method):
    try:
        owner_profile = _restaurant_staff_profile(owner, UserProfile.Role.OWNER)
    except OrderWorkflowError as exc:
        raise PaymentWorkflowError("This account is not eligible to record payments.") from exc
    restaurant = owner_profile.restaurant
    if payment_method not in Payment.Method.values:
        raise PaymentWorkflowError("Unsupported payment method.")

    with transaction.atomic():
        assign_order = (
            AssignOrder.objects.select_for_update()
            .filter(pk=order_id, restaurant=restaurant)
            .first()
        )
        if assign_order is None:
            raise PaymentWorkflowError("Order was not found.")
        if assign_order.status == AssignOrder.Status.CANCELLED:
            raise PaymentWorkflowError("Cancelled orders cannot accept payments.")
        if assign_order.total_amount <= Decimal("0.00"):
            raise PaymentWorkflowError("Only orders with a positive total can be paid.")

        existing_payments = list(
            Payment.objects.select_for_update().filter(
                assign_order=assign_order,
                restaurant=restaurant,
            )
        )
        successful_payments = [
            payment for payment in existing_payments if payment.payment_status == Payment.Status.SUCCESS
        ]
        successful_total = sum((payment.amount for payment in successful_payments), Decimal("0.00"))
        if successful_total >= assign_order.total_amount:
            raise PaymentWorkflowError("This order is already fully paid.")
        if successful_total > Decimal("0.00"):
            raise PaymentWorkflowError(
                "Partial payment completion needs confirmed split-payment rules."
            )

        payment = Payment(
            restaurant=restaurant,
            assign_order=assign_order,
            amount=assign_order.total_amount,
            payment_method=payment_method,
            payment_status=Payment.Status.SUCCESS,
            paid_at=timezone.now(),
        )
        payment.full_clean()
        payment.save()

        history_entry = AssignOrderHistory(
            assign_order=assign_order,
            restaurant=restaurant,
            user=owner,
            action=AssignOrderHistory.Action.PAYMENT_COMPLETED,
            description=(
                f"{payment.get_payment_method_display()} payment recorded for "
                f"Rs. {payment.amount}."
            ),
        )
        history_entry.full_clean()
        history_entry.save()
    return payment


def record_owner_completed_order_correction(*, owner, order_id, reason):
    try:
        owner_profile = _restaurant_staff_profile(owner, UserProfile.Role.OWNER)
    except OrderWorkflowError as exc:
        raise OrderWorkflowError("This account is not eligible to correct completed orders.") from exc

    reason = (reason or "").strip()
    if len(reason) < 10 or len(reason.split()) < 3:
        raise OrderWorkflowError("Provide a meaningful correction reason.")

    restaurant = owner_profile.restaurant
    with transaction.atomic():
        assign_order = (
            AssignOrder.objects.select_for_update()
            .filter(
                pk=order_id,
                restaurant=restaurant,
                status=AssignOrder.Status.COMPLETED,
            )
            .first()
        )
        if assign_order is None:
            raise OrderWorkflowError("Completed order was not found.")

        history_entry = AssignOrderHistory(
            assign_order=assign_order,
            restaurant=restaurant,
            user=owner,
            action=AssignOrderHistory.Action.ORDER_CORRECTED,
            description=f"Owner correction recorded: {reason}",
        )
        history_entry.full_clean()
        history_entry.save()
    return history_entry
