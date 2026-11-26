"""The `db` and `login` fixtures against a real Postgres."""

import psycopg
import pytest
from dsy_migrate.fixtures import logins

pytestmark = pytest.mark.integration


def exists(db, user):
    return db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (user,)).fetchone() is not None


def test_login_connects_as_a_member_of_the_group(db, login):
    db.execute("CREATE ROLE grp_a")
    with psycopg.connect(login("grp_a"), autocommit=True) as conn:
        assert conn.execute("SELECT pg_has_role(current_user, 'grp_a', 'member')").fetchone() == (
            True,
        )
    db.execute("DROP ROLE grp_a")  # fails while the login still belongs to it


def test_leaked_connection_and_owned_objects_do_not_break_teardown(appdb, db):
    db.execute("CREATE ROLE grp_b")
    db.execute("GRANT CREATE ON SCHEMA public TO grp_b")
    with logins(db, appdb) as login:
        dsn = login("grp_b")
        leaked = psycopg.connect(dsn, autocommit=True)  # never closed
        leaked.execute("CREATE TABLE owned_by_login (id int)")
        user = leaked.info.user
        assert exists(db, user)
    assert not exists(db, user)
    with pytest.raises(psycopg.OperationalError):
        leaked.execute("SELECT 1")
    db.execute("DROP OWNED BY grp_b")
    db.execute("DROP ROLE grp_b")
