import hashlib
import warnings
from pathlib import Path

from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError


PHOTO_MAX_BYTES = 5 * 1024 * 1024
ID_PROOF_MAX_BYTES = 20 * 1024 * 1024
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
IMAGE_MIME_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png"}


def _rewind(upload):
    try:
        upload.seek(0)
    except (AttributeError, OSError):
        pass


def inspect_profile_photo(upload):
    return _inspect_upload(upload, PHOTO_MAX_BYTES, allow_pdf=False)


def inspect_id_proof(upload):
    return _inspect_upload(upload, ID_PROOF_MAX_BYTES, allow_pdf=True)


def validate_profile_photo(upload):
    inspect_profile_photo(upload)


def validate_id_proof_file(upload):
    inspect_id_proof(upload)


def _inspect_upload(upload, max_bytes, *, allow_pdf):
    extension = Path(upload.name).suffix.lower()
    allowed_extensions = IMAGE_EXTENSIONS | ({".pdf"} if allow_pdf else set())
    if extension not in allowed_extensions:
        raise ValidationError("Unsupported file extension.")
    if upload.size > max_bytes:
        raise ValidationError(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.")

    declared_type = (getattr(upload, "content_type", "") or "").lower()
    sha256 = hashlib.sha256()
    _rewind(upload)
    for chunk in upload.chunks():
        sha256.update(chunk)
    _rewind(upload)

    if extension == ".pdf":
        header = upload.read(5)
        try:
            upload.seek(max(0, upload.size - 1024))
        except (AttributeError, OSError):
            upload.seek(0)
        trailer = upload.read()
        _rewind(upload)
        if header != b"%PDF-" or b"%%EOF" not in trailer:
            raise ValidationError("The uploaded file is not a valid PDF document.")
        if declared_type and declared_type != "application/pdf":
            raise ValidationError("The declared MIME type does not match the PDF file.")
        mime_type = "application/pdf"
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                image = Image.open(upload)
                if image.width > 8000 or image.height > 8000:
                    raise ValidationError("Image dimensions are too large.")
                image.verify()
                image_format = image.format
        except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombWarning) as exc:
            raise ValidationError("The uploaded file is not a valid image.") from exc
        finally:
            _rewind(upload)
        mime_type = IMAGE_MIME_TYPES.get(image_format)
        if mime_type is None:
            raise ValidationError("Only JPG, JPEG, and PNG images are allowed.")
        expected_extensions = {".jpg", ".jpeg"} if image_format == "JPEG" else {".png"}
        if extension not in expected_extensions:
            raise ValidationError("The file extension does not match its content.")
        if declared_type and declared_type != mime_type:
            raise ValidationError("The declared MIME type does not match the image.")

    return {
        "mime_type": mime_type,
        "size_bytes": upload.size,
        "sha256": sha256.hexdigest(),
    }
