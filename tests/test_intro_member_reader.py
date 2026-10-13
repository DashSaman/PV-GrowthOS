from pv_growth.mirza_adapter.mysql_reader import MirzaMySQLReader


class Cursor:
    def __init__(self, step="checkout", status="Active", agent="f"):
        self.step, self.status, self.agent = step, status, agent
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, args=None):
        self.queries.append((sql, args))

    def fetchone(self):
        return {"id": 12345, "User_Status": self.status, "agent": self.agent, "step": self.step}

    def fetchall(self):
        return [{"id": 12345}] if self.queries[-1][0].startswith("SELECT id FROM user") else []


class Conn:
    def __init__(self, cursor):
        self.c = cursor
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        return self.c

    def rollback(self):
        self.rolled_back = True


def reader(monkeypatch, settings, **kw):
    r = MirzaMySQLReader(settings)
    c = Cursor(**kw)
    conn = Conn(c)
    monkeypatch.setattr(r, "connect", lambda: conn)
    return r, c, conn


def test_contact_census_all_steps_is_opt_in_and_stays_readonly(settings, monkeypatch):
    r, c, conn = reader(monkeypatch, settings)
    assert r.list_private_invite_contacts(require_home=False) == [12345]
    assert "step='home'" not in c.queries[1][0]
    assert "User_Status='Active' AND agent='f'" in c.queries[1][0]
    assert c.queries[0][0] == "START TRANSACTION READ ONLY" and conn.rolled_back
    c.queries.clear()
    r.list_private_invite_contacts()
    assert "step='home'" in c.queries[1][0]


def test_full_history_outside_home_is_opt_in(settings, monkeypatch):
    r, c, conn = reader(monkeypatch, settings)
    assert r.fetch_private_invite_history(12345) is None
    c.queries.clear()
    assert r.fetch_private_invite_history(12345, require_home=False) == ([], [], [])
    assert len(c.queries) == 5 and conn.rolled_back
    assert c.queries[0][0] == "START TRANSACTION READ ONLY"


def test_inactive_or_agent_user_is_still_rejected(settings, monkeypatch):
    for kw in ({"status": "Blocked"}, {"agent": "t"}):
        r, c, conn = reader(monkeypatch, settings, **kw)
        assert r.fetch_private_invite_history(12345, require_home=False) is None
        assert len(c.queries) == 2 and conn.rolled_back
