"""Updater crash/failure tests with a Docker-command simulator; no wallet use."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('auto_update', Path(__file__).resolve().parents[1] / 'deploy/auto-update.py')
update = importlib.util.module_from_spec(spec); spec.loader.exec_module(update)
OLD = update.IMAGE + '@sha256:' + 'a'*64
NEW = update.IMAGE + '@sha256:' + 'b'*64


def release():
    return {'schema': 1, 'version': '0.7.0', 'minimumVersion': '0.6.0', 'stateFormat': 1,
            'assignmentPolicies': sorted(update.POLICIES), 'image': NEW, 'release': 'official'}


def release_fetch(change=None):
    manifest = {k:v for k,v in release().items() if k not in ('image', 'release')}
    data = {'image.txt': (NEW+'\n').encode(), 'auto-update.json': json.dumps(manifest).encode()}
    data['SHA256SUMS'] = ''.join(hashlib.sha256(v).hexdigest()+'  '+k+'\n' for k,v in data.items()).encode()
    body = {'tag_name': 'v0.7.0', 'draft': False, 'prerelease': False, 'assets': []}
    for k,v in data.items():
        body['assets'].append({'name': k, 'browser_download_url': f'https://github.com/{update.REPO}/releases/download/v0.7.0/{k}',
                               'digest': 'sha256:'+hashlib.sha256(v).hexdigest()})
    if change: change(body, data)
    def fetch(url, limit):
        return json.dumps(body).encode() if url == update.API else data[url.rsplit('/',1)[-1]]
    return fetch


def test_official_asset_and_checksum_verification():
    assert update.latest(release_fetch())['image'] == NEW


@pytest.mark.parametrize('change', [
    lambda b,d: b.update(prerelease=True),
    lambda b,d: b.update(tag_name='v0.7.0-rc1'),
    lambda b,d: b['assets'][0].update(browser_download_url='https://evil.example/image.txt'),
    lambda b,d: d.update({'image.txt': b'bad'}),
    lambda b,d: b['assets'].append(b['assets'][0]),
    lambda b,d: b.update(assets=b['assets'][:-1]),
])
def test_untrusted_or_incomplete_release_is_rejected(change):
    with pytest.raises(ValueError): update.latest(release_fetch(change))


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    root = tmp_path / 'updater'; root.mkdir(mode=0o700)
    config = {'schema': 1, 'enabled': True, 'project': 'validator', 'service': 'validator', 'stateDir': '/state', 'signing': True}
    snapshot = {'services': {'validator': {'image': OLD, 'command': ['run', '--submit', '--state-dir', '/state'],
                 'volumes': [{'type':'volume', 'source':'state', 'target':'/state'}, {'type':'bind', 'source':'/wallet', 'target':'/wallet','read_only':True}]}}}
    update.write(root/'config.json',config); update.write(root/'snapshot.json',snapshot)
    update.write(root/'compose.json',snapshot); update.write(root/'current.json',{'version':'0.6.0','image':OLD})
    untouched = tmp_path/'validator-state'; untouched.mkdir()
    for name in ('journal.db', 'config.json', 'authorization.json'): (untouched/name).write_bytes(b'original-private-state')
    calls = []; runtime = {'started': False, 'blocked':False, 'fail':None}
    def command(args, timeout=600):
        calls.append(args)
        if args[:3] == ['docker','image','inspect']: return json.dumps([{'Id':'old-id'}])
        if args[:2] == ['docker','inspect']:
            return json.dumps([{'Image':'old-id','State':{'Running':True,'Health':{'Status':'healthy'}}}])
        if args[:2] == ['docker','exec']:
            if args[-1] == '--version': return '0.7.0' if runtime['started'] else '0.6.0'
            return json.dumps({'state':'blocked' if runtime['blocked'] else 'idle'})
        if args[:2] == ['docker','run']: return '0.7.0'
        if args[:2] == ['docker','pull']: return ''
        if args[:2] == ['docker','compose']:
            op = args[6]
            if op == 'ps': return 'container'
            if op == 'run': return json.dumps({'ok': runtime['fail'] != 'preflight', 'signingPrerequisitesOk':None,
                    'checks':[{'name':'wallet','ok':True},{'name':'authorization','ok':True,'remainingSubmissions':10}]})
            if op == 'up':
                runtime['started'] = True
                if runtime['fail'] == 'start': raise RuntimeError('provider failed')
                if runtime['fail'] == 'timeout': raise subprocess.TimeoutExpired(args,timeout)
            return ''
        raise AssertionError(args)
    monkeypatch.setattr(update, 'command', command)
    return root, snapshot, calls, runtime, untouched


def test_upgrade_keeps_signing_configuration_and_state(deployment):
    root, snapshot, calls, runtime, state = deployment
    assert update.tick(root, get_latest=release)['state'] == 'updated'
    updated = update.read(root/'compose.json')
    assert updated['services']['validator']['command'] == snapshot['services']['validator']['command']
    assert updated['services']['validator']['volumes'] == snapshot['services']['validator']['volumes']
    assert all(p.read_bytes() == b'original-private-state' for p in state.iterdir())
    assert not (root/'pending.json').exists()
    operations = [c[6] for c in calls if c[:2] == ['docker','compose']]
    assert operations.index('stop') < operations.index('run') < operations.index('up')


@pytest.mark.parametrize('failure', ['start','preflight','timeout'])
def test_failed_update_keeps_marker_and_never_restores_state(deployment,failure):
    root, _, calls, runtime, state = deployment
    runtime['fail']=failure
    with pytest.raises(ValueError): update.tick(root,get_latest=release)
    assert (root/'pending.json').exists()
    assert update.read(root/'current.json')['version'] == '0.6.0'
    assert all(p.read_bytes() == b'original-private-state' for p in state.iterdir())
    before = deepcopy(calls)
    with pytest.raises(ValueError,match='unfinished'): update.tick(root,get_latest=release)
    assert calls == before


def test_unknown_journal_defers_before_stop(deployment):
    root,_,calls,runtime,_ = deployment; runtime['blocked']=True
    assert update.tick(root,get_latest=release)['state'] == 'deferred'
    assert not (root/'pending.json').exists()
    assert not any('stop' in c for c in calls)


def test_disabled_and_checks_never_change_containers(deployment):
    root,_,calls,_,_ = deployment
    assert update.tick(root,check=True,get_latest=release)['state']=='update_available'
    assert not calls
    config=update.read(root/'config.json'); config['enabled']=False; update.write(root/'config.json',config)
    assert update.tick(root,get_latest=lambda: pytest.fail('must not fetch'))['state']=='disabled'


@pytest.mark.parametrize('number,state', [('0.6.0','up_to_date'), ('0.5.0','up_to_date'), ('1.0.0','manual_upgrade_required')])
def test_no_downgrade_or_automatic_major_upgrade(deployment,number,state):
    root,_,calls,_,_ = deployment
    assert update.tick(root,get_latest=lambda:{**release(),'version':number})['state']==state
    assert not calls


def test_lock_blocks_concurrent_updater(deployment):
    root=deployment[0]
    with update.locked(root):
        with pytest.raises(BlockingIOError): update.tick(root,get_latest=release)


def test_symlink_config_is_not_followed(deployment,tmp_path):
    root=deployment[0]
    (root/'config.json').unlink(); (root/'config.json').symlink_to(tmp_path/'elsewhere')
    with pytest.raises(OSError): update.tick(root,get_latest=release)


@pytest.mark.parametrize('mismatch', [None, 'command', 'hash', 'health', 'state_mount'])
def test_setup_binds_existing_running_configuration(tmp_path,monkeypatch,mismatch):
    cmd=['run','--submit','--state-dir','/state']
    service={'image':OLD,'command':cmd,'healthcheck':{'test':['CMD','python','-m','everyframe_validator.healthcheck','/state']},
             'volumes':[{'type':'volume','source':'validator-state','target':'/state'}]}
    snapshot={'services':{'validator':service}}
    if mismatch=='health': service['healthcheck']['test']=['CMD','true']
    if mismatch=='state_mount': service['volumes']=[]
    def command(args,timeout=600):
        if '--format' in args: return json.dumps(snapshot)
        if '--hash' in args: return 'validator correct-hash'
        if 'ps' in args: return 'id'
        if args[:2]==['docker','inspect']:
            return json.dumps([{'Image':'image-id','State':{'Running':True},'Config':{'Cmd':cmd if mismatch!='command' else ['run'],
                 'Labels':{'com.docker.compose.project':'validator','com.docker.compose.config-hash':'wrong' if mismatch=='hash' else 'correct-hash'}}}])
        if args[:3]==['docker','image','inspect']: return json.dumps([{'RepoDigests':[OLD]}])
        if args[:2]==['docker','exec']: return '0.6.0'
        raise AssertionError(args)
    monkeypatch.setattr(update,'command',command)
    root=tmp_path/'updater'
    if mismatch:
        with pytest.raises(ValueError): update.configure(root,tmp_path/'compose.yaml','validator','validator',yes=True)
        assert not (root/'config.json').exists()
    else:
        result=update.configure(root,tmp_path/'compose.yaml','validator','validator',yes=True)
        assert result['signing'] is True and result['timerInstalled'] is False
        assert update.read(root/'snapshot.json')['services']['validator']['command']==cmd
        assert (root/'auto-update.py').exists()
