"""Initial relational schema"""
from alembic import op
from app.models import Base
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    Base.metadata.create_all(bind=op.get_bind())
    # Defense in depth: active team membership cannot exceed five, even for
    # future code paths that forget to lock/check the team in application code.
    op.execute("""
    CREATE OR REPLACE FUNCTION enforce_team_active_member_limit() RETURNS trigger AS $$
    DECLARE active_count integer;
    BEGIN
      IF NEW.active THEN
        PERFORM 1 FROM teams WHERE id = NEW.team_id FOR UPDATE;
        SELECT count(*) INTO active_count FROM memberships
          WHERE team_id = NEW.team_id AND active = true
            AND (TG_OP = 'INSERT' OR id <> NEW.id);
        IF active_count >= 5 THEN
          RAISE EXCEPTION 'team active member limit exceeded' USING ERRCODE = '23514';
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER trg_team_active_member_limit BEFORE INSERT OR UPDATE OF active, team_id ON memberships FOR EACH ROW EXECUTE FUNCTION enforce_team_active_member_limit()")

def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_team_active_member_limit ON memberships")
    op.execute("DROP FUNCTION IF EXISTS enforce_team_active_member_limit()")
    Base.metadata.drop_all(bind=op.get_bind())
