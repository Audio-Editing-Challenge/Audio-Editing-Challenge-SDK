import os
from pathlib import Path
from typing import Any

import httpx

from aec_upload.validation import (
    UploadPolicy,
    ValidationError,
    build_submission_tar,
    validate_submission,
    validate_tar_stream,
)


class UploadError(RuntimeError):
    """Raised when the Upload service rejects a request."""


class UploadClient:
    """Client for the standalone Audio Editing Challenge Upload service."""

    def __init__(
        self,
        base_url: str,
        secret_key: str,
        competition_id: str,
        *,
        timeout: float = 3600,
    ) -> None:
        self._secret_key = secret_key
        self._competition_id = competition_id
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def __enter__(self) -> "UploadClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def policy(self) -> UploadPolicy:
        response = self._client.get(
            "/upload/policy", params={"competition_id": self._competition_id},
        )
        policy = UploadPolicy.from_dict(self._result(response))
        if policy.competition_id != self._competition_id:
            raise ValidationError("Server policy belongs to a different competition")
        return policy

    def validate(self, submission_dir: str | Path) -> dict[str, Any]:
        return validate_submission(submission_dir, self.policy())

    def pack(self, submission_dir: str | Path, tar_path: str | Path) -> Path:
        return build_submission_tar(submission_dir, tar_path, self.policy())

    def upload(self, tar_path: str | Path, *, name: str) -> dict[str, Any]:
        path = Path(tar_path)
        policy = self.policy()
        with path.open("rb") as stream:
            validate_tar_stream(stream, policy)
            stream.seek(0)
            response = self._client.post(
                "/upload/uploads",
                params={
                    "competition_id": self._competition_id,
                    "name": name,
                    "filename": path.name,
                },
                headers={
                    "Authorization": f"Bearer {self._secret_key}",
                    "Content-Type": "application/x-tar",
                    "Content-Length": str(os.fstat(stream.fileno()).st_size),
                },
                content=iter(lambda: stream.read(64 * 1024), b""),
            )
        return self._result(response)

    def list_uploads(self) -> dict[str, Any]:
        response = self._client.get(
            "/upload/uploads",
            params={
                "secret_key": self._secret_key,
                "competition_id": self._competition_id,
            },
        )
        return self._result(response)

    @staticmethod
    def _result(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise UploadError(f"Upload service returned HTTP {response.status_code}") from exc
        if not response.is_success:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise UploadError(str(detail or f"Upload service returned HTTP {response.status_code}"))
        if not isinstance(payload, dict):
            raise UploadError("Upload service returned a JSON response that is not an object")
        return payload
