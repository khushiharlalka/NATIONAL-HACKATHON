from uuid import UUID
from fastapi import Header, HTTPException

def current_user(x_user_id: str | None = Header(default=None), x_role: str = Header(default="user")):
    if not x_user_id:
        raise HTTPException(401, "X-User-ID is required in mock-auth mode")
    try: actor = UUID(x_user_id)
    except ValueError: raise HTTPException(401, "Invalid X-User-ID")
    if x_role not in {"user", "admin"}: raise HTTPException(403, "Unknown role")
    return {"id": actor, "role": x_role}

def require_admin(actor):
    if actor["role"] != "admin": raise HTTPException(403, "Administrator role required")
