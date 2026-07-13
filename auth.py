import os
import jwt
from fastapi import Header, HTTPException

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

if not SUPABASE_JWT_SECRET:
    raise RuntimeError("FATAL: Missing SUPABASE_JWT_SECRET. Refusing to start.")


def get_current_user_id(authorization: str = Header(...)) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = authorization.removeprefix("Bearer ").strip()

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    return payload["sub"]
