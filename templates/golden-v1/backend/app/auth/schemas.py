"""Request/response models for the auth endpoints."""

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    """POST /api/auth/register body."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    """POST /api/auth/login body."""

    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    """A JWT access token."""

    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """A user's public fields."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
