from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

def auth(uid, role='user'):
    return {'X-User-ID': uid, 'X-Role': role}

def setup_team(client, make_user, size=1):
    owner = make_user(f'{uuid4().hex}@example.com')
    team = client.post('/teams', json={'name':'Team'}, headers=auth(owner)).json()
    for _ in range(size-1):
        member = make_user(f'{uuid4().hex}@example.com')
        inv = client.post(f"/teams/{team['id']}/invites", json={'user_id':member}, headers=auth(owner)).json()
        assert client.post(f"/invites/{inv['id']}/accept", headers=auth(member)).status_code == 200
    return owner, team['id']

def make_track(client, capacity=1):
    r=client.post('/admin/tracks',json={'name':f'Track-{uuid4().hex[:8]}','capacity':capacity},headers=auth(str(uuid4()),'admin'))
    # Admin actor must exist because audit actor_id is intentionally not FK-constrained,
    # so a random UUID is valid in this mock-auth fixture.
    assert r.status_code == 201, r.text
    return r.json()['id']

def test_duplicate_email_and_successful_registration(client, make_user):
    owner, team = setup_team(client, make_user)
    email=f'{uuid4().hex}@example.com'
    assert client.post('/users',json={'email':email,'full_name':'A'}).status_code==201
    assert client.post('/users',json={'email':email.upper(),'full_name':'B'}).status_code==409
    track=make_track(client)
    r=client.post('/registrations',json={'team_id':team,'track_id':track},headers={**auth(owner),'Idempotency-Key':'registration-key-001'})
    assert r.status_code==201
    assert client.get('/tracks').json()[0]['remaining_capacity']==0

def test_same_key_replay_and_payload_conflict(client, make_user):
    owner, team=setup_team(client,make_user); track=make_track(client,2)
    headers={**auth(owner),'Idempotency-Key':'retry-key-0001'}
    first=client.post('/registrations',json={'team_id':team,'track_id':track},headers=headers)
    second=client.post('/registrations',json={'team_id':team,'track_id':track},headers=headers)
    assert first.status_code==second.status_code==201
    assert first.json()['id']==second.json()['id']
    other_track=make_track(client,2)
    conflict=client.post('/registrations',json={'team_id':team,'track_id':other_track},headers=headers)
    assert conflict.status_code==409

def test_cancel_is_idempotent_and_releases_once(client, make_user):
    owner,team=setup_team(client,make_user); track=make_track(client,1)
    headers={**auth(owner),'Idempotency-Key':'cancel-reg-key-01'}
    reg_response=client.post('/registrations',json={'team_id':team,'track_id':track},headers=headers)
    reg=reg_response.json()
    for _ in range(2): assert client.delete(f"/registrations/{reg['id']}",headers=auth(owner)).json()['status']=='cancelled'
    # A stale POST retry with the original key returns the cancelled record; it cannot reactivate it.
    replay=client.post('/registrations',json={'team_id':team,'track_id':track},headers=headers)
    assert replay.status_code==201 and replay.json()['status']=='cancelled'
    t=next(x for x in client.get('/tracks').json() if x['id']==track)
    assert t['reserved_count']==0 and t['remaining_capacity']==1

def test_wrong_invitee_and_duplicate_acceptance(client,make_user):
    owner,team=setup_team(client,make_user)
    intended=make_user(f'{uuid4().hex}@example.com'); wrong=make_user(f'{uuid4().hex}@example.com')
    invite=client.post(f'/teams/{team}/invites',json={'user_id':intended},headers=auth(owner)).json()
    assert client.post(f"/invites/{invite['id']}/accept",headers=auth(wrong)).status_code==403
    assert client.post(f"/invites/{invite['id']}/accept",headers=auth(intended)).status_code==200
    assert client.post(f"/invites/{invite['id']}/accept",headers=auth(intended)).status_code==409

def test_final_seat_race(client,make_user):
    owner1,team1=setup_team(client,make_user); owner2,team2=setup_team(client,make_user); track=make_track(client,1)
    def submit(args):
        owner,team,key=args
        return client.post('/registrations',json={'team_id':team,'track_id':track},headers={**auth(owner),'Idempotency-Key':key}).status_code
    # Each request has a distinct idempotency key and team; DB conditional UPDATE is the arbiter.
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(submit,[(owner1,team1,'race-key-0001'),(owner2,team2,'race-key-0002')]))
    assert results.count(201)==1
    assert sum(x['reserved_count'] for x in client.get('/tracks').json() if x['id']==track)==1

def test_invalid_team_size_unknown_registration_and_admin_auth(client,make_user):
    owner,team=setup_team(client,make_user)
    track=client.post('/admin/tracks',json={'name':'NeedsTwo','capacity':3,'min_team_size':2},headers=auth(str(uuid4()),'admin')).json()['id']
    assert client.post('/registrations',json={'team_id':team,'track_id':track},headers={**auth(owner),'Idempotency-Key':'size-key-0001'}).status_code==422
    assert client.get(f'/registrations/{uuid4()}',headers=auth(owner)).status_code==404
    assert client.post('/admin/tracks',json={'name':'NoAdmin','capacity':2},headers=auth(owner)).status_code==403

def test_malformed_json_and_notification_outbox(client,make_user):
    owner,team=setup_team(client,make_user); track=make_track(client,1)
    bad=client.post('/registrations',content='{bad',headers={**auth(owner),'Idempotency-Key':'malformed-key-01','Content-Type':'application/json'})
    assert bad.status_code==422
    reg=client.post('/registrations',json={'team_id':team,'track_id':track},headers={**auth(owner),'Idempotency-Key':'notify-key-0001'})
    assert reg.status_code==201
    # Outbox row is committed with the registration; provider failure cannot roll it back.
    from app.models import OutboxEvent
    import os
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    engine=create_engine(os.environ['TEST_DATABASE_URL'])
    with Session(engine) as db:
        assert db.query(OutboxEvent).filter_by(event_type='registration.created').count()==1
    engine.dispose()


def test_notification_provider_outage_does_not_undo_registration(client,make_user,monkeypatch):
    owner,team=setup_team(client,make_user); track=make_track(client,1)
    response=client.post('/registrations',json={'team_id':team,'track_id':track},headers={**auth(owner),'Idempotency-Key':'outage-key-0001'})
    assert response.status_code==201
    import os
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import OutboxEvent
    import app.outbox_worker as worker
    engine=create_engine(os.environ['TEST_DATABASE_URL'])
    TestSession=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(worker,'SessionLocal',TestSession)
    def outage(event): raise RuntimeError('simulated mail provider outage')
    monkeypatch.setattr(worker,'send_notification',outage)
    worker.dispatch_batch()
    with TestSession() as db:
        event=db.query(OutboxEvent).filter_by(event_type='registration.created').one()
        assert event.status=='pending'
    # The business transaction remains committed and visible despite delivery failure.
    assert client.get(f"/registrations/{response.json()['id']}",headers=auth(owner)).status_code==200
    engine.dispose()
