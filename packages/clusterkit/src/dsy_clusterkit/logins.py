"""The service logins of an app database, converged from a table of data per service.

The group roles of the app database cannot log in, so each service gets a login in the group role
it writes or reads as. The passwords are generated on first use into the state directory of the
cluster scripts and never committed.
Usage: `render [STORE_CREDENTIALS] | apply DSN | check DSN`, the CLI of each `deploy/*/logins.py`.
`render` prints the Secrets of the service as a JSON list: the DSNs of its logins and, for a service
with store roles, its store identities read from the credentials file of the store.
`apply` creates or converges each login as the superuser of DSN and prints one line per login with
what it did, ending with `changed: <n>`, and `check` exits 1 when a login differs or cannot log in.
"""

import json
import os
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

from dsy_clusterkit import state_dir

DATABASE = "appdb.dsy-infra.svc.cluster.local:5432/postgres"
CREDENTIALS_FILE = "credentials.json"

MEMBER_OF = """
select g.rolname, m.inherit_option, m.set_option, m.admin_option from pg_auth_members m
join pg_roles g on g.oid = m.roleid
join pg_roles l on l.oid = m.member
where l.rolname = %s
order by 1
"""
# A service login logs in, inherits its group's grants and holds no other power or expiry.
ATTRIBUTES = """
select rolcanlogin, rolinherit, not (rolsuper or rolcreaterole or rolcreatedb or rolreplication
    or rolbypassrls), coalesce(rolvaliduntil = 'infinity', true)
from pg_roles where rolname = %s
"""


@dataclass(frozen=True)
class Spec:
    """What differs between services: each login and the one group role it is a member of, keyed
    by the environment variable of its DSN, the namespace of its Secrets, the name its state file
    and Secrets take, and the roles of the store it holds identities for."""

    logins: Mapping[str, tuple[str, str]]
    namespace: str
    state_name: str
    store_roles: tuple[str, ...] = ()

    def state_file(self) -> Path:
        return state_dir() / f"{self.state_name}.logins.json"

    def secrets_of_logins(self) -> dict[str, str]:
        """The password of each login, generated into the state file on first use.

        The first writer wins, so two processes that start at once read the same passwords.
        """
        path = self.state_file()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            generated = {login: secrets.token_hex(24) for login, _ in self.logins.values()}
            tmp = path.with_name(f".{path.name}.{os.getpid()}")
            tmp.touch(mode=0o600)
            tmp.write_text(json.dumps(generated, indent=2) + "\n")
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            finally:
                tmp.unlink()
        return json.loads(path.read_text())

    def secret(self, suffix: str, data: dict[str, str]) -> dict:
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"{self.state_name}-{suffix}", "namespace": self.namespace},
            "type": "Opaque",
            "stringData": data,
        }

    def render(self, store_credentials: str | None = None) -> list[dict]:
        known = self.secrets_of_logins()
        rendered = [
            self.secret(
                "logins",
                {
                    env: f"postgresql://{login}:{known[login]}@{DATABASE}"
                    for env, (login, _) in self.logins.items()
                },
            )
        ]
        if self.store_roles:
            identities = json.loads(Path(store_credentials).read_text())
            held = {role: identities[role] for role in self.store_roles}
            rendered.append(self.secret("store", {CREDENTIALS_FILE: json.dumps(held)}))
        return rendered

    def run(self, mode: str, dsn: str) -> int:
        apply = mode == "apply"
        known = self.secrets_of_logins()
        lines = []
        with psycopg.connect(dsn, autocommit=True) as conn:
            for login, group in self.logins.values():
                current = state(conn, login, group)
                if current == "unchanged" and not logs_in(dsn, login, known[login]):
                    current = "differs: password"
                if apply and current != "unchanged":
                    converge(conn, login, group, known[login])
                    current = "created" if current == "missing" else "updated"
                lines.append((f"login: {login} in {group}", current))
        for item, current in lines:
            print(f"{item} {current}")
        wrong = [item for item, current in lines if current.startswith(("missing", "differs"))]
        if apply:
            print(f"changed: {sum(current in ('created', 'updated') for _, current in lines)}")
        for item in wrong:
            print(f"differs: {item}", file=sys.stderr)
        return 1 if wrong else 0


def state(conn: psycopg.Connection, login: str, group: str) -> str:
    row = conn.execute(ATTRIBUTES, (login,)).fetchone()
    if row is None:
        return "missing"
    wrong = [] if all(row) else ["attributes"]
    groups = conn.execute(MEMBER_OF, (login,)).fetchall()
    if groups != [(group, True, True, False)]:
        wrong.append(f"member of {', '.join(g for g, *_ in groups) or 'nothing'}")
    return f"differs: {'; '.join(wrong)}" if wrong else "unchanged"


def converge(conn: psycopg.Connection, login: str, group: str, phrase: str) -> None:
    """Gives `login` the attributes of ATTRIBUTES, the password `phrase` as a SCRAM verifier, so
    no statement holds it in clear, and the membership of `group` only."""
    role = sql.Identifier(login)
    if conn.execute("select from pg_roles where rolname = %s", (login,)).fetchone() is None:
        conn.execute(sql.SQL("create role {}").format(role))
    verifier = conn.pgconn.encrypt_password(phrase.encode(), login.encode(), b"scram-sha-256")
    conn.execute(
        sql.SQL(
            "alter role {} login inherit nosuperuser nocreaterole nocreatedb noreplication"
            " nobypassrls valid until 'infinity' password {}"
        ).format(role, sql.Literal(verifier.decode()))
    )
    for other, *_ in conn.execute(MEMBER_OF, (login,)).fetchall():
        conn.execute(sql.SQL("revoke {} from {}").format(sql.Identifier(other), role))
    conn.execute(
        sql.SQL("grant {} to {} with inherit true, set true").format(sql.Identifier(group), role)
    )


def logs_in(dsn: str, login: str, phrase: str) -> bool:
    """Whether `login` reaches the database of the superuser `dsn` with the password `phrase`."""
    parts = urlsplit(dsn)
    host = parts.netloc.rsplit("@", 1)[-1]
    try:
        psycopg.connect(
            urlunsplit(parts._replace(netloc=f"{login}:{phrase}@{host}")), connect_timeout=5
        ).close()
    except psycopg.OperationalError:
        return False
    return True


def cli(argv: list[str], spec: Spec) -> int:
    """The CLI of a service's logins: `render [STORE_CREDENTIALS]`, `apply DSN` or `check DSN`, the
    credentials file only for a service with store roles. Returns the exit code, 2 for usage."""
    if argv[:1] == ["render"] and len(argv) == 1 + bool(spec.store_roles):
        print(json.dumps(spec.render(*argv[1:])))
        return 0
    if len(argv) != 2 or argv[0] not in ("apply", "check"):
        render = "render STORE_CREDENTIALS" if spec.store_roles else "render"
        print(f"usage: logins.py {render} | apply DSN | check DSN", file=sys.stderr)
        return 2
    return spec.run(*argv)


def main(
    argv: list[str],
    *,
    logins: Mapping[str, tuple[str, str]],
    namespace: str,
    state_name: str,
    store_roles: tuple[str, ...] = (),
) -> int:
    return cli(argv, Spec(logins, namespace, state_name, store_roles))
