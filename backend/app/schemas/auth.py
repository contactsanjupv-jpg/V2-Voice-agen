from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    organization_name: str = Field(min_length=1, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)  # same ceiling as signup: no real password is longer, and Argon2 must not see megabytes


class UserOut(BaseModel):
    id: str
    email: str

    model_config = {"from_attributes": True}


class OrganizationOut(BaseModel):
    id: str
    name: str
    role: str
