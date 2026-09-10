from dataclasses import dataclass
from hmac import compare_digest

import jwt
from fastapi import Header, HTTPException, Request

from .config import Principal, Settings


@dataclass(frozen=True)
class AuthContext:
    principal: Principal

    def require(self, role: str) -> None:
        if role not in self.principal.roles:
            raise HTTPException(403, "insufficient role")


class TokenAuthenticator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jwks = (jwt.PyJWKClient(settings.oauth_issuer.rstrip("/") + "/.well-known/jwks.json")
                     if settings.environment == "production" else None)

    def verify(self, token: str) -> AuthContext:
        if self.settings.environment == "local":
            for candidate, principal in self.settings.principals().items():
                if compare_digest(candidate, token):
                    return AuthContext(principal)
            raise HTTPException(401, "invalid bearer token")
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], issuer=self.settings.oauth_issuer,
                audience=self.settings.oauth_resource,
                options={"require": ["exp", "iat", "iss", "sub", "aud", "client_id", "token_use"]},
            )
            if claims["token_use"] != "access":
                raise ValueError("Expected an access token")
            if claims["client_id"] not in self.settings.oauth_client_ids:
                raise ValueError("Unknown OAuth client")
            principal = self.settings.oauth_principals()[claims["sub"]]
            scope_roles = {
                "read": "reader", "propose": "writer", "review": "reviewer", "admin": "admin"
            }
            granted = {
                role for scope, role in scope_roles.items()
                if f"{self.settings.oauth_scope_prefix}/{scope}" in claims.get("scope", "").split()
            }
            if claims["client_id"] != self.settings.oauth_web_client_id:
                granted &= {"reader", "writer"}
            return AuthContext(principal.model_copy(update={"roles": principal.roles & granted}))
        except (jwt.PyJWTError, ValueError, KeyError) as exc:
            raise HTTPException(401, "invalid access token") from exc


def authenticate(request: Request, authorization: str | None = Header(default=None)) -> AuthContext:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "missing bearer token")
    return request.app.state.authenticator.verify(token)
