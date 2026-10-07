import re
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from .models import EmployeeIDProof, UserProfile
from .services import create_employee


def image_upload(name="photo.jpg", image_format="JPEG", size=(24, 24)):
    stream = BytesIO()
    Image.new("RGB", size, color=(40, 120, 80)).save(stream, format=image_format)
    mime = "image/png" if image_format == "PNG" else "image/jpeg"
    return SimpleUploadedFile(name, stream.getvalue(), content_type=mime)


def pdf_upload(name="proof.pdf"):
    return SimpleUploadedFile(
        name,
        b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF",
        content_type="application/pdf",
    )


def employee_kwargs(restaurant, *, role=UserProfile.Role.OWNER, name="Test Owner", email="owner@example.invalid", **overrides):
    data = {
        "name": name,
        "email": email,
        "contact": "+910000000001",
        "address": "1 Test Street",
        "photo": image_upload(),
        "id_proof_type": EmployeeIDProof.ProofType.PASSPORT,
        "id_proof_file": pdf_upload(),
        "restaurant": restaurant,
        "role": role,
    }
    data.update(overrides)
    return data


def create_test_employee(restaurant, **overrides):
    data = employee_kwargs(restaurant)
    data.update(overrides)
    return create_employee(**data)


def password_from_welcome(message):
    return re.search(r"^Temporary password: (.+)$", message.body, re.MULTILINE).group(1).strip()
