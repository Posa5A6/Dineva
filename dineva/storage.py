from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage, storages


class PrivateFileSystemStorage(FileSystemStorage):
    def __init__(self, **kwargs):
        kwargs.setdefault("location", settings.PRIVATE_MEDIA_ROOT)
        super().__init__(**kwargs)

    def url(self, name):
        raise ValueError("Private files do not have public URLs.")


def get_private_storage():
    return storages["private"]


def private_file_path(prefix, filename):
    suffix = Path(filename).suffix.lower()
    from uuid import uuid4

    return f"{prefix}/{uuid4().hex}{suffix}"


def employee_photo_upload_to(instance, filename):
    return private_file_path("employee_photos", filename)


def employee_id_proof_upload_to(instance, filename):
    return private_file_path("employee_id_proofs", filename)
