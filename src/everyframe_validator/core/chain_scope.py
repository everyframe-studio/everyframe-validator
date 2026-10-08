"""Pinned chain domains. Never infer mainnet from a wallet, endpoint or netuid."""
import json
from pathlib import Path
import sqlite3
import time

ROOT = Path(__file__).resolve().parent
PROFILES = {
    name: json.loads((ROOT / 'config' / filename).read_text())
    for name, filename in [('test', 'testnet.json'), ('finney', 'mainnet.json')]
}


def read_network(domain):
    """Use a pinned archive as the PRIMARY for historical mainnet reads.

    No transparent cross-endpoint fallback: callers check this connection's
    genesis before using any data. Lite nodes prune before a full reward window.
    """
    return 'wss://archive.chain.opentensor.ai:443' if domain['network'] == 'finney' else 'test'


def scope(network='test', netuid=None):
    if network not in PROFILES:
        raise ValueError('unsupported network')
    p = PROFILES[network]
    if netuid is not None and (type(netuid) is not int or netuid != p['netuid']):
        raise ValueError('network/netuid mismatch')
    return {'network': network, 'netuid': p['netuid'], 'genesis': p['genesisHash']}


def add_network(parser):
    parser.add_argument('--network', choices=tuple(PROFILES), default='test',
                        help='Explicit chain: test (SN566) or finney (mainnet SN117)')


def check_record(record, domain):
    if any(record.get(key) != value for key, value in domain.items()):
        raise ValueError('cross-network record rejected')


def bind_database(db, domain, *, legacy_test=False):
    """Bind every artifact/journal/accounting DB to one chain, before any write.

    Mainnet refuses populated unscoped databases, including copied test fixtures.
    Old testnet files can be adopted explicitly without rewriting their records.
    """
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='chain_scope'").fetchone()
    if exists:
        rows = db.execute('SELECT network,netuid,genesis FROM chain_scope').fetchall()
        if rows != [(domain['network'], domain['netuid'], domain['genesis'])]:
            raise ValueError('database belongs to another chain')
        return
    populated = False
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
        safe = name.replace('"', '""')
        if db.execute(f'SELECT 1 FROM "{safe}" LIMIT 1').fetchone():
            populated = True
            break
    if populated and not (legacy_test and domain == scope('test')):
        raise ValueError('refusing populated unscoped database')
    db.execute('CREATE TABLE chain_scope(network TEXT NOT NULL,netuid INTEGER NOT NULL,genesis TEXT NOT NULL)')
    db.execute('INSERT INTO chain_scope VALUES(?,?,?)', tuple(domain[k] for k in ('network','netuid','genesis')))
    db.commit()


def check_database(path, domain):
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute('SELECT network,netuid,genesis FROM chain_scope').fetchall()
        if rows != [(domain['network'], domain['netuid'], domain['genesis'])]:
            raise ValueError('database chain scope mismatch')


def check_authorization(path, domain, hotkey, version_key, now=None):
    """Local validator-operator consent, never permission from the subnet owner."""
    if not path:
        raise ValueError('local signing authorization file required')
    p = Path(path)
    if p.stat().st_mode & 0o077:
        raise ValueError('mainnet authorization must be owner-only')
    a = json.loads(p.read_text())
    check_record(a, domain)
    now = int(time.time()*1000) if now is None else now
    if (a.get('action') != 'submit-reward-weights' or a.get('validatorHotkey') != hotkey
            or a.get('versionKey') != version_key or type(a.get('expiresAt')) is not int
            or not now < a['expiresAt'] <= now + 366 * 86400000
            or type(a.get('maxSubmissions')) is not int or not 1 <= a['maxSubmissions'] <= 1000000
            or not isinstance(a.get('id'), str) or not a['id']):
        raise ValueError('invalid or expired mainnet authorization')
    return a
