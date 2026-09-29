"""
Argon2id password hashing via argon2-cffi — an established library, not
homemade crypto. Parameters are the argon2-cffi defaults (already tuned
for interactive login use) unless overridden.
"""
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False


def needs_rehash(hashed: str) -> bool:
    """Call after a successful verify; rehash+resave if this returns True
    (parameters were upgraded since the hash was created)."""
    return _hasher.check_needs_rehash(hashed)
