"""Session enumeration across multiple SAP systems.

Field incident (two systems, DE4 and DO1): the picker was completely empty
although two DO1 sessions were open and logged on.  Cause: the whole nested
enumeration loop sat inside a single try/except, so the first connection that
refused a property access aborted the entire listing and returned [].

SAP GUI Scripting is authorised *per system* by the server profile parameter
`sapgui/user_scripting`.  One system answering and the next refusing is the
normal case, not the exception, so enumeration must isolate every item.
"""

import pytest

from core import session as mod
from core.session import SessionEntry, ScriptingDisabledError, enumerate_sessions


class FakeInfo:
    def __init__(self, system, client, user, transaction):
        self.SystemName = system
        self.Client = client
        self.User = user
        self.Transaction = transaction


class FakeSession:
    def __init__(self, info=None, raises=None):
        self._info = info
        self._raises = raises

    @property
    def Info(self):
        if self._raises:
            raise self._raises
        return self._info


class FakeChildren:
    def __init__(self, items, count_raises=None):
        self._items = items
        self._count_raises = count_raises

    @property
    def Count(self):
        if self._count_raises:
            raise self._count_raises
        return len(self._items)


class FakeConnection:
    def __init__(self, sessions, description="", count_raises=None,
                 child_raises=None):
        self._sessions = sessions
        self.Description = description
        self._count_raises = count_raises
        self._child_raises = child_raises

    @property
    def Children(self):
        return FakeChildren(self._sessions, self._count_raises)

    def Children_(self, index):  # pragma: no cover - not used
        raise NotImplementedError

    def __call__(self, index):  # pragma: no cover
        raise NotImplementedError


class _ConnChildren(FakeChildren):
    pass


def _make_connection(sessions, description="", count_raises=None, child_raises=None):
    conn = FakeConnection(sessions, description, count_raises, child_raises)

    def children(index):
        if child_raises:
            raise child_raises
        return sessions[index]

    # GuiConnection.Children is both a collection and callable by index
    holder = _ConnChildren(sessions, count_raises)
    holder.__call__ = children
    conn.Children = holder
    conn._child_fn = children
    return conn


class FakeEngine:
    def __init__(self, connections):
        self._connections = connections

    @property
    def Connections(self):
        return FakeChildren(self._connections)

    def Children(self, index):
        return self._connections[index]


def _install(monkeypatch, engine, error=None):
    monkeypatch.setattr(mod, "_scripting_engine", lambda *a, **k: (engine, error))
    monkeypatch.setattr(mod, "_elevation_hint", lambda: "")


def _conn(sessions, description=""):
    """A connection whose Children works as collection and as indexer."""

    class C:
        Description = description

        class Children:
            pass

    conn = C()

    class Ch:
        @property
        def Count(self):
            return len(sessions)

        def __call__(self, index):
            return sessions[index]

    conn.Children = Ch()
    return conn


def _blocked_conn(exc, description=""):
    class C:
        Description = description

    conn = C()

    class Ch:
        @property
        def Count(self):
            raise exc

        def __call__(self, index):
            raise exc

    conn.Children = Ch()
    return conn


DE4 = FakeSession(FakeInfo("DE4", "300", "FVLDEV", "SE80"))
DO1_A = FakeSession(FakeInfo("DO1", "120", "HVNEODEV01", "SE80"))
DO1_B = FakeSession(FakeInfo("DO1", "120", "HVNEODEV01", "SE38"))


# -- the reported bug -----------------------------------------------------

def test_a_blocked_first_connection_no_longer_hides_the_rest(monkeypatch):
    """The exact failure: connection 0 refuses, so nothing was listed."""
    engine = FakeEngine([
        _blocked_conn(Exception("scripting is disabled"), "ECC QAS QO1"),
        _conn([DO1_A, DO1_B], "ECC DEV DO1"),
    ])
    _install(monkeypatch, engine)

    entries, notes = enumerate_sessions()
    usable = [e for e in entries if e.usable]

    assert len(usable) == 2, "the DO1 sessions must survive a blocked QO1"
    assert {e.system for e in usable} == {"DO1"}
    assert any(not e.usable for e in entries), "the blocked one is still reported"


def test_blocked_connection_is_explained_not_silently_dropped(monkeypatch):
    engine = FakeEngine([_blocked_conn(Exception("scripting is disabled"), "ECC QAS")])
    _install(monkeypatch, engine)

    entries, notes = enumerate_sessions()
    assert len(entries) == 1
    assert not entries[0].usable
    assert "sapgui/user_scripting" in entries[0].error
    assert any("sapgui/user_scripting" in n and "TRUE" in n for n in notes)


def test_all_sessions_of_all_systems_are_listed(monkeypatch):
    engine = FakeEngine([
        _conn([DE4], "ECC DEV DE4"),
        _conn([DO1_A, DO1_B], "ECC DEV DO1"),
    ])
    _install(monkeypatch, engine)

    entries, _ = enumerate_sessions()
    assert [(e.connection_index, e.session_index, e.system) for e in entries] == [
        (0, 0, "DE4"), (1, 0, "DO1"), (1, 1, "DO1"),
    ]


def test_one_bad_session_does_not_hide_its_siblings(monkeypatch):
    bad = FakeSession(raises=Exception("access denied"))
    engine = FakeEngine([_conn([bad, DO1_B], "ECC DEV DO1")])
    _install(monkeypatch, engine)

    entries, _ = enumerate_sessions()
    assert len(entries) == 2
    assert not entries[0].usable
    assert entries[1].usable and entries[1].transaction == "SE38"


# -- labels ---------------------------------------------------------------

def test_label_of_a_good_session():
    entry = SessionEntry(1, 0, "DO1", "120", "HVNEODEV01", "SE80")
    assert entry.label == "1:0  DO1 client 120 user HVNEODEV01 [SE80]"


def test_label_of_a_blocked_connection_names_the_system():
    entry = SessionEntry(0, 0, description="ECC QAS QO1",
                         error="scripting disabled for this system")
    assert "ECC QAS QO1" in entry.label
    assert "scripting disabled" in entry.label


# -- engine-level failures ------------------------------------------------

def test_no_engine_reports_why(monkeypatch):
    monkeypatch.setattr(mod, "_scripting_engine",
                        lambda *a, **k: (None, RuntimeError("ROT lookup failed")))
    monkeypatch.setattr(mod, "_elevation_hint", lambda: "")
    entries, notes = enumerate_sessions()
    assert entries == []
    assert any("ROT lookup failed" in n for n in notes)


def test_elevation_mismatch_is_surfaced(monkeypatch):
    monkeypatch.setattr(mod, "_scripting_engine",
                        lambda *a, **k: (None, RuntimeError("not found")))
    monkeypatch.setattr(mod, "_elevation_hint", lambda: "running as Administrator")
    _entries, notes = enumerate_sessions()
    assert any("Administrator" in n for n in notes)


def test_zero_connections_suggests_logging_on(monkeypatch):
    _install(monkeypatch, FakeEngine([]))
    entries, notes = enumerate_sessions()
    assert entries == []
    assert any("not logged on" in n or "no connections" in n for n in notes)


def test_connection_with_no_session_is_reported(monkeypatch):
    _install(monkeypatch, FakeEngine([_conn([], "ECC DEV DO1")]))
    entries, _ = enumerate_sessions()
    assert len(entries) == 1
    assert "no session visible to scripting" in entries[0].error
    assert entries[0].reason == "no_session"


# -- attach picks a usable session ---------------------------------------

def test_attach_skips_blocked_connections(monkeypatch):
    engine = FakeEngine([
        _blocked_conn(Exception("scripting is disabled"), "ECC QAS QO1"),
        _conn([DO1_A], "ECC DEV DO1"),
    ])
    _install(monkeypatch, engine)

    logged = []
    sap = mod.SapSession.attach(0, 0, log=logged.append)
    assert sap.session is DO1_A
    assert any("using 1:0" in line for line in logged)


def test_attach_honours_an_explicit_choice(monkeypatch):
    engine = FakeEngine([_conn([DE4]), _conn([DO1_A, DO1_B])])
    _install(monkeypatch, engine)
    assert mod.SapSession.attach(1, 1).session is DO1_B


def test_attach_error_lists_what_sap_reported(monkeypatch):
    engine = FakeEngine([_blocked_conn(Exception("scripting is disabled"), "ECC QAS QO1")])
    _install(monkeypatch, engine)

    with pytest.raises(ScriptingDisabledError) as excinfo:
        mod.SapSession.attach()
    message = str(excinfo.value)
    assert "ECC QAS QO1" in message
    assert "sapgui/user_scripting" in message


def test_list_sessions_returns_labels(monkeypatch):
    engine = FakeEngine([_conn([DE4], "ECC DEV DE4"), _conn([DO1_A], "ECC DEV DO1")])
    _install(monkeypatch, engine)
    labels = mod.SapSession.list_sessions()
    assert len(labels) == 2
    assert "DE4" in labels[0] and "DO1" in labels[1]


# -- distinguishing "no session" from "scripting refused" -----------------
#
# Field incident: DO1 had sapgui/user_scripting = TRUE (confirmed in RZ11,
# Current Value TRUE) but the connection exposed zero sessions, because it
# had been opened before the parameter was switched on.  The old summary
# still said "refused scripting access ... needs sapgui/user_scripting =
# TRUE", sending the user to re-check a parameter that was already correct.

def test_connection_with_no_sessions_is_not_called_a_scripting_refusal(monkeypatch):
    _install(monkeypatch, FakeEngine([_conn([DE4], "ECC DEV DE4"),
                                      _conn([], "ECC DEV DO1")]))
    entries, notes = enumerate_sessions()

    stale = [e for e in entries if e.reason == "no_session"]
    assert len(stale) == 1
    joined = "\n".join(notes)
    assert "refused scripting access" not in joined
    assert "sapgui/user_scripting              = TRUE" not in joined


def test_stale_connection_advice_says_log_off_and_on(monkeypatch):
    _install(monkeypatch, FakeEngine([_conn([], "ECC DEV DO1")]))
    _entries, notes = enumerate_sessions()
    joined = "\n".join(notes)
    assert "Log off that system completely" in joined
    assert "Switch on all servers" in joined
    assert "ECC DEV DO1" in joined


def test_real_refusal_still_names_the_parameters(monkeypatch):
    _install(monkeypatch, FakeEngine([
        _blocked_conn(Exception("scripting is disabled"), "ECC QAS QO1")]))
    _entries, notes = enumerate_sessions()
    joined = "\n".join(notes)
    assert "refused scripting access" in joined
    assert "sapgui/user_scripting              = TRUE" in joined
    assert "set_readonly = FALSE" in joined


def test_both_problems_get_their_own_advice(monkeypatch):
    _install(monkeypatch, FakeEngine([
        _blocked_conn(Exception("scripting is disabled"), "ECC QAS QO1"),
        _conn([], "ECC DEV DO1"),
    ]))
    _entries, notes = enumerate_sessions()
    assert len(notes) == 2
    joined = "\n".join(notes)
    assert "ECC QAS QO1" in joined and "ECC DEV DO1" in joined


def test_reason_is_empty_for_healthy_sessions(monkeypatch):
    _install(monkeypatch, FakeEngine([_conn([DE4], "ECC DEV DE4")]))
    entries, notes = enumerate_sessions()
    assert entries[0].usable and entries[0].reason == ""
    assert notes == []
