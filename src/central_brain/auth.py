from dataclasses import dataclass
from hmac import compare_digest

from fastapi import Depends, Header, HTTPException, status

from .config import Principal, Settings, get_settings


@dataclass(frozen=True)
class AuthContext:
    principal: Principal

    def require(self, role: str) -> None:
        if role not in self.principal.roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")


def authenticate(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")

    # Iterate so comparison work does not stop at the first partial match.
    match: Principal | None = None
    for candidate, principal in settings.principals().items():
        if compare_digest(candidate, token):
            match = principal
    if match is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")
    return AuthContext(match)
