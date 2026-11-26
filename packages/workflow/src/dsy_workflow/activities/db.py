"""The database connection of a control activity and the typed errors it raises."""

from contextlib import contextmanager

import psycopg
from dsy_artifactclient.commit import CommitRefused
from dsy_artifactclient.errors import ArtifactError
from dsy_artifactindex import InvalidReference
from dsy_servicekit import pg

from ..testing.control import control_error

LOCK_TIMEOUT = "10s"


def connect(dsn: str, autocommit: bool = False) -> psycopg.Connection:
    """A connection whose lock waits are bounded, so a lock held elsewhere fails the attempt, which
    the activity's retry sends again."""
    return pg.connect(dsn, autocommit=autocommit, lock_timeout=LOCK_TIMEOUT)


@contextmanager
def typed_errors():
    """Raises an artifact error as the ApplicationError of its code, and a reference that cannot
    be committed as CommitRefused."""
    try:
        yield
    except ArtifactError as error:
        raise control_error(error.code, str(error)) from error
    except (CommitRefused, InvalidReference) as error:
        raise control_error("CommitRefused", str(error)) from error
