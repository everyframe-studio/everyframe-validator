"""Both supported chain schedulers retain reveal/application observations."""
import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from everyframe_validator import application
from everyframe_validator.state import journal_rows


@pytest.mark.parametrize('cadence', ['chain-epoch-v2', 'chain-epoch-v3'])
@pytest.mark.parametrize('state', ['commit_finalized', 'finalized', 'pending_reveal'])
def test_observe_both_chain_schedulers(profile, monkeypatch, cadence, state):
    root, _ = profile
    record = {'cadence': cadence, 'block': 100}
    result = {'extrinsic_id': '101-0', 'broadcast': {'hash': 'fixture'}}
    with sqlite3.connect(root / 'journal.db') as db:
        db.execute('INSERT INTO weight_runs VALUES(?,?,?,?)',
                   ('fixture', state, json.dumps(record), json.dumps(result)))
    evidence = {'state': 'application_observed', 'terminal': True,
                'revealConfirmed': True, 'applicationConfirmed': True,
                'alphaSettlementConfirmed': False, 'mechanismBlock': 200}
    inspect = AsyncMock(return_value=evidence)
    monkeypatch.setattr(application, 'inspect', inspect)
    reports = asyncio.run(application.observe(None, root, journal_rows(root), 210, []))
    inspect.assert_awaited_once()
    assert reports == [{'runId': 'fixture', **evidence}]
    saved = journal_rows(root)[0]
    assert saved['record'] == record
    assert saved['state'] == 'application_observed'
    assert saved['result']['broadcast'] == result['broadcast']
    assert saved['result']['revealConfirmed'] is True
    assert saved['result']['applicationConfirmed'] is True
    assert saved['result']['alphaSettlementConfirmed'] is False
    # The same immutable transaction must not be observed again once terminal.
    assert asyncio.run(application.observe(None, root, journal_rows(root), 211, [])) == []
    assert inspect.await_count == 1


def test_observe_ignores_unsupported_or_unsubmitted_records(profile, monkeypatch):
    root, _ = profile
    with sqlite3.connect(root / 'journal.db') as db:
        for name, cadence, state in [('old', 'block-paced-v1', 'commit_finalized'),
                                     ('unsigned', 'chain-epoch-v3', 'not_submitted'),
                                     ('uncertain', 'chain-epoch-v3', 'unknown')]:
            db.execute('INSERT INTO weight_runs VALUES(?,?,?,?)',
                       (name, state, json.dumps({'cadence': cadence, 'block': 100}), '{}'))
    inspect = AsyncMock()
    monkeypatch.setattr(application, 'inspect', inspect)
    before = journal_rows(root)
    assert asyncio.run(application.observe(None, root, before, 210, [])) == []
    inspect.assert_not_called()
    assert journal_rows(root) == before
