"""Validation for the account and history endpoints (pydantic). The AI endpoints keep their hand-written
parsers (requests.py and friends), whose messages match the TypeScript app word for word.

Every model rejects unknown fields, and validate() turns pydantic's errors into a 422 with one readable
message per field.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .middleware import ApiError

PASSWORD_MIN = 8
PASSWORD_MAX_BYTES = 72  # bcrypt ignores everything after 72 bytes
RECORD_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def normalize_email(raw: str) -> str:
    """Trimmed and lower-cased (the whole address: nobody expects Bob@ and bob@ to be two accounts)."""
    try:
        v = validate_email(raw.strip(), check_deliverability=False)
    except EmailNotValidError:
        raise ValueError("Enter a valid email address.") from None
    email = v.normalized.lower()
    if len(email) > 254:
        raise ValueError("That email address is too long.")
    return email


def check_password(pw: str) -> str:
    if len(pw) < PASSWORD_MIN:
        raise ValueError(f"Use at least {PASSWORD_MIN} characters.")
    if len(pw.encode("utf-8")) > PASSWORD_MAX_BYTES:
        raise ValueError(f"Use at most {PASSWORD_MAX_BYTES} bytes (about {PASSWORD_MAX_BYTES} plain letters).")
    if not pw.strip():
        raise ValueError("The password can't be only spaces.")
    return pw


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Register(_Strict):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return normalize_email(v)

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        return check_password(v)


class Login(_Strict):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return normalize_email(v)


class ChangePassword(_Strict):
    currentPassword: str = Field(min_length=1, max_length=200)
    newPassword: str = Field(max_length=200)

    @field_validator("newPassword")
    @classmethod
    def _password(cls, v: str) -> str:
        return check_password(v)


class DeleteAccount(_Strict):
    password: str = Field(min_length=1, max_length=200)


class Empty(_Strict):
    pass


STATUSES = ("deciding", "asking", "generating", "done", "error")


class Record(BaseModel):
    """An ExplainerRecord (app/src/store/history.ts). Known fields are checked; the rest of the record is
    kept as the browser sent it (forward-compatible), under the size cap enforced by the route."""
    model_config = ConfigDict(extra="allow")

    id: str
    title: str = Field(max_length=200)
    createdAt: int = Field(ge=0, le=10**14)
    updatedAt: int = Field(ge=0, le=10**14)
    status: Literal["deciding", "asking", "generating", "done", "error"]
    prompt: dict[str, Any]
    request: dict[str, Any]
    steps: dict[str, Any]
    progress: dict[str, Any]
    chat: list[Any]
    thread: str | None = None
    spec: dict[str, Any] | None = None

    @field_validator("id", "thread")
    @classmethod
    def _id(cls, v: str | None) -> str | None:
        if v is not None and not RECORD_ID.match(v):
            raise ValueError("Invalid record id.")
        return v


class IdList(_Strict):
    ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        if any(not RECORD_ID.match(i) for i in v):
            raise ValueError("Invalid record id.")
        return list(dict.fromkeys(v))


def validate(model: type[BaseModel], body: Any) -> Any:
    if not isinstance(body, dict):
        raise ApiError(400, "Body must be a JSON object.", "invalid_json")
    try:
        return model.model_validate(body)
    except ValidationError as e:
        fields: dict[str, str] = {}
        for err in e.errors():
            name = ".".join(str(p) for p in err["loc"]) or "body"
            msg = err["msg"].removeprefix("Value error, ")
            if err["type"] == "extra_forbidden":
                msg = "Unknown field."
            elif err["type"] == "missing":
                msg = "This field is required."
            fields.setdefault(name, msg)
        first = next(iter(fields.items()))
        raise ApiError(422, first[1] if len(fields) == 1 else "Please fix the highlighted fields.", "invalid", fields) from None
