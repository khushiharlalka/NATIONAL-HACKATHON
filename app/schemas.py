from uuid import UUID
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from typing import Annotated

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
class UserCreate(BaseModel): email: EmailStr; full_name: Name
class TeamCreate(BaseModel): name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
class InviteCreate(BaseModel): user_id: UUID
class TrackCreate(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    capacity: int = Field(ge=0)
    min_team_size: int = Field(default=1, ge=1, le=5)
    max_team_size: int = Field(default=5, ge=1, le=5)
    require_verified_members: bool = False
class RegistrationCreate(BaseModel): team_id: UUID; track_id: UUID
class UserOut(BaseModel): model_config = ConfigDict(from_attributes=True); id: UUID; email: str; full_name: str; verified: bool
class TeamOut(BaseModel): id: UUID; name: str; created_by: UUID
class InviteOut(BaseModel): id: UUID; team_id: UUID; invitee_user_id: UUID; status: str
class RegistrationOut(BaseModel): id: UUID; team_id: UUID; track_id: UUID; status: str
class TrackOut(BaseModel): id: UUID; name: str; capacity: int; reserved_count: int; remaining_capacity: int; min_team_size: int; max_team_size: int; active: bool
