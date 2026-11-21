import hashlib, json
from datetime import datetime, timedelta, timezone
from uuid import UUID
from fastapi import FastAPI, Depends, Header, HTTPException, status
from sqlalchemy import select, update, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .database import get_db
from . import models as m
from .schemas import UserCreate, UserOut, TeamCreate, TeamOut, InviteCreate, InviteOut, TrackCreate, TrackOut, RegistrationCreate, RegistrationOut
from .security import current_user, require_admin

app = FastAPI(title="National Hackathon Registration API", version="1.0.0")

def fail_integrity(exc):
    raise HTTPException(409, "A conflicting or invalid state transition occurred") from exc

def audit(db, actor_id, action, kind, resource_id, details=None):
    db.add(m.AuditLog(actor_id=actor_id, action=action, resource_type=kind, resource_id=str(resource_id), details=details or {}))

def get_user(db, user_id):
    user = db.get(m.User, user_id)
    if not user: raise HTTPException(404, "User not found")
    return user

def active_member_count(db, team_id):
    return db.scalar(select(func.count()).select_from(m.Membership).where(m.Membership.team_id == team_id, m.Membership.active.is_(True))) or 0

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    normalized = body.email.strip().casefold()
    row = m.User(email=normalized, full_name=body.full_name.strip())
    db.add(row)
    try: db.commit(); db.refresh(row)
    except IntegrityError as e: db.rollback(); raise HTTPException(409, "Email already exists") from e
    return row

@app.post("/teams", response_model=TeamOut, status_code=201)
def create_team(body: TeamCreate, db: Session = Depends(get_db), actor=Depends(current_user)):
    get_user(db, actor["id"])
    team = m.Team(name=body.name, created_by=actor["id"])
    db.add(team); db.flush()
    db.add(m.Membership(team_id=team.id, user_id=actor["id"], role="owner", active=True))
    audit(db, actor["id"], "team.create", "team", team.id)
    try: db.commit(); db.refresh(team)
    except IntegrityError as e: db.rollback(); fail_integrity(e)
    return TeamOut(id=team.id, name=team.name, created_by=team.created_by)

@app.post("/teams/{team_id}/invites", response_model=InviteOut, status_code=201)
def invite_user(team_id: UUID, body: InviteCreate, db: Session = Depends(get_db), actor=Depends(current_user)):
    try:
        team = db.execute(select(m.Team).where(m.Team.id == team_id).with_for_update()).scalar_one_or_none()
        if not team: raise HTTPException(404, "Team not found")
        owner = db.scalar(select(m.Membership).where(m.Membership.team_id == team_id, m.Membership.user_id == actor["id"], m.Membership.active.is_(True)))
        if not owner or owner.role != "owner": raise HTTPException(403, "Only an active team owner may invite")
        invitee = get_user(db, body.user_id)
        if db.scalar(select(m.Membership.id).where(m.Membership.user_id == invitee.id, m.Membership.active.is_(True))): raise HTTPException(409, "Invitee already belongs to an active team")
        if active_member_count(db, team_id) >= 5: raise HTTPException(409, "Team is already full")
        row = m.Invitation(team_id=team_id, invitee_user_id=invitee.id, invited_by=actor["id"])
        db.add(row); audit(db, actor["id"], "invite.create", "invitation", row.id, {"invitee_user_id": str(invitee.id)})
        db.commit(); db.refresh(row)
        return InviteOut(id=row.id, team_id=row.team_id, invitee_user_id=row.invitee_user_id, status=row.status)
    except HTTPException: db.rollback(); raise
    except IntegrityError as e: db.rollback(); fail_integrity(e)

@app.post("/invites/{invite_id}/accept", response_model=InviteOut)
def accept_invite(invite_id: UUID, db: Session = Depends(get_db), actor=Depends(current_user)):
    try:
        invite = db.execute(select(m.Invitation).where(m.Invitation.id == invite_id).with_for_update()).scalar_one_or_none()
        if not invite: raise HTTPException(404, "Invitation not found")
        if invite.invitee_user_id != actor["id"]: raise HTTPException(403, "Invitation belongs to another user")
        if invite.status != "pending": raise HTTPException(409, "Invitation is no longer pending")
        team = db.execute(select(m.Team).where(m.Team.id == invite.team_id).with_for_update()).scalar_one_or_none()
        if not team: raise HTTPException(404, "Team not found")
        if db.scalar(select(m.Membership.id).where(m.Membership.user_id == actor["id"], m.Membership.active.is_(True))): raise HTTPException(409, "User already belongs to an active team")
        if active_member_count(db, team.id) >= 5: raise HTTPException(409, "Team is full")
        invite.status = "accepted"; invite.accepted_at = datetime.now(timezone.utc)
        db.add(m.Membership(team_id=team.id, user_id=actor["id"], role="member", active=True))
        audit(db, actor["id"], "invite.accept", "invitation", invite.id)
        db.commit(); db.refresh(invite)
        return InviteOut(id=invite.id, team_id=invite.team_id, invitee_user_id=invite.invitee_user_id, status=invite.status)
    except HTTPException: db.rollback(); raise
    except IntegrityError as e: db.rollback(); fail_integrity(e)

@app.post("/admin/tracks", response_model=TrackOut, status_code=201)
def create_track(body: TrackCreate, db: Session = Depends(get_db), actor=Depends(current_user)):
    require_admin(actor)
    if body.min_team_size > body.max_team_size: raise HTTPException(422, "min_team_size cannot exceed max_team_size")
    row = m.Track(**body.model_dump())
    db.add(row); db.flush(); audit(db, actor["id"], "track.create", "track", row.id, body.model_dump())
    try: db.commit(); db.refresh(row)
    except IntegrityError as e: db.rollback(); fail_integrity(e)
    return TrackOut(id=row.id, name=row.name, capacity=row.capacity, reserved_count=row.reserved_count, remaining_capacity=row.capacity-row.reserved_count, min_team_size=row.min_team_size, max_team_size=row.max_team_size, active=row.active)

@app.get("/tracks", response_model=list[TrackOut])
def list_tracks(db: Session = Depends(get_db)):
    rows = db.scalars(select(m.Track).where(m.Track.active.is_(True)).order_by(m.Track.name)).all()
    return [TrackOut(id=x.id, name=x.name, capacity=x.capacity, reserved_count=x.reserved_count, remaining_capacity=x.capacity-x.reserved_count, min_team_size=x.min_team_size, max_team_size=x.max_team_size, active=x.active) for x in rows]

@app.post("/registrations", response_model=RegistrationOut, status_code=201)
def register(body: RegistrationCreate, idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200), db: Session = Depends(get_db), actor=Depends(current_user)):
    payload_hash = hashlib.sha256(json.dumps({"team_id": str(body.team_id), "track_id": str(body.track_id)}, sort_keys=True).encode()).hexdigest()
    try:
        # Insert key in the same transaction as capacity reservation + registration.
        db.execute(pg_insert(m.IdempotencyRecord).values(actor_id=actor["id"], key=idempotency_key, request_hash=payload_hash, expires_at=datetime.now(timezone.utc)+timedelta(days=30)).on_conflict_do_nothing(index_elements=["actor_id", "key"]))
        idem = db.execute(select(m.IdempotencyRecord).where(m.IdempotencyRecord.actor_id == actor["id"], m.IdempotencyRecord.key == idempotency_key).with_for_update()).scalar_one()
        if idem.request_hash != payload_hash: raise HTTPException(409, "Idempotency-Key was already used with a different payload")
        if idem.registration_id:
            existing = db.get(m.Registration, idem.registration_id)
            if existing: db.commit(); return RegistrationOut(id=existing.id, team_id=existing.team_id, track_id=existing.track_id, status=existing.status)
        team = db.execute(select(m.Team).where(m.Team.id == body.team_id).with_for_update()).scalar_one_or_none()
        if not team: raise HTTPException(404, "Team not found")
        owner = db.scalar(select(m.Membership.id).where(m.Membership.team_id == team.id, m.Membership.user_id == actor["id"], m.Membership.role == "owner", m.Membership.active.is_(True)))
        if not owner: raise HTTPException(403, "Only an active team owner may register the team")
        track = db.get(m.Track, body.track_id)
        if not track or not track.active: raise HTTPException(404, "Active track not found")
        members = db.scalars(select(m.Membership).where(m.Membership.team_id == team.id, m.Membership.active.is_(True))).all()
        count = len(members)
        if count > 5 or count < track.min_team_size or count > track.max_team_size: raise HTTPException(422, "Team does not meet track size eligibility")
        if track.require_verified_members and any(not get_user(db, x.user_id).verified for x in members): raise HTTPException(422, "All active team members must be verified")
        # Atomic conditional update is the capacity gate across all app instances.
        reserved = db.execute(update(m.Track).where(m.Track.id == track.id, m.Track.active.is_(True), m.Track.reserved_count < m.Track.capacity).values(reserved_count=m.Track.reserved_count+1).returning(m.Track.id)).scalar_one_or_none()
        if reserved is None: raise HTTPException(409, "Track is full")
        row = m.Registration(team_id=team.id, track_id=track.id, status="active")
        db.add(row); db.flush(); idem.registration_id = row.id
        db.add(m.OutboxEvent(event_type="registration.created", payload={"registration_id": str(row.id), "team_id": str(team.id), "track_id": str(track.id)}))
        audit(db, actor["id"], "registration.create", "registration", row.id, {"team_id": str(team.id), "track_id": str(track.id)})
        db.commit(); db.refresh(row)
        return RegistrationOut(id=row.id, team_id=row.team_id, track_id=row.track_id, status=row.status)
    except HTTPException: db.rollback(); raise
    except IntegrityError as e: db.rollback(); fail_integrity(e)

@app.get("/registrations/{registration_id}", response_model=RegistrationOut)
def get_registration(registration_id: UUID, db: Session = Depends(get_db), actor=Depends(current_user)):
    row = db.get(m.Registration, registration_id)
    if not row: raise HTTPException(404, "Registration not found")
    allowed = db.scalar(select(m.Membership.id).where(m.Membership.team_id == row.team_id, m.Membership.user_id == actor["id"], m.Membership.active.is_(True)))
    if actor["role"] != "admin" and not allowed: raise HTTPException(403, "Not authorised to view this registration")
    return RegistrationOut(id=row.id, team_id=row.team_id, track_id=row.track_id, status=row.status)

@app.delete("/registrations/{registration_id}", response_model=RegistrationOut)
def cancel_registration(registration_id: UUID, db: Session = Depends(get_db), actor=Depends(current_user)):
    try:
        row = db.execute(select(m.Registration).where(m.Registration.id == registration_id).with_for_update()).scalar_one_or_none()
        if not row: raise HTTPException(404, "Registration not found")
        allowed = db.scalar(select(m.Membership.id).where(m.Membership.team_id == row.team_id, m.Membership.user_id == actor["id"], m.Membership.role == "owner", m.Membership.active.is_(True)))
        if actor["role"] != "admin" and not allowed: raise HTTPException(403, "Only the team owner or admin may cancel")
        if row.status == "active":
            changed = db.execute(update(m.Registration).where(m.Registration.id == row.id, m.Registration.status == "active").values(status="cancelled", cancelled_at=datetime.now(timezone.utc)).returning(m.Registration.id)).scalar_one_or_none()
            if changed:
                db.execute(update(m.Track).where(m.Track.id == row.track_id, m.Track.reserved_count > 0).values(reserved_count=m.Track.reserved_count-1))
                db.add(m.OutboxEvent(event_type="registration.cancelled", payload={"registration_id": str(row.id), "team_id": str(row.team_id), "track_id": str(row.track_id)}))
                audit(db, actor["id"], "registration.cancel", "registration", row.id)
        db.commit(); db.refresh(row)
        return RegistrationOut(id=row.id, team_id=row.team_id, track_id=row.track_id, status=row.status)
    except HTTPException: db.rollback(); raise
    except IntegrityError as e: db.rollback(); fail_integrity(e)
