#!/usr/bin/env python3
"""Opt-in, host-side Docker Compose updater. Never downloads executable scripts."""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = 'everyframe-studio/everyframe-validator'
IMAGE = 'ghcr.io/' + REPO
API = 'https://api.github.com/repos/' + REPO + '/releases/latest'
POLICIES = {'epoch-start-accounting-v1', 'epoch-start-accounting-burn-v2', 'epoch-start-contest-pool-v3'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('stable_version_required')
    return tuple(map(int, value.removeprefix('v').split('.')))


def image(value):
    if not isinstance(value, str) or not re.fullmatch(re.escape(IMAGE) + r'@sha256:[a-f0-9]{64}', value):
        raise ValueError('official_immutable_image_required')
    return value


def write(path, value):
    fd, name = tempfile.mkstemp(prefix='.update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(name): os.unlink(name)


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private_updater_file_required')
        return json.load(f)


@contextmanager
def locked(root):
    if root.is_symlink(): raise ValueError('updater_directory_symlink')
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private_updater_directory_required')
    fd = os.open(root / 'update.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(fd)


def request(url, limit):
    # Official HTTPS origins only, including GitHub's release-asset redirect.
    allowed = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
    def check(value):
        from urllib.parse import urlsplit
        parts = urlsplit(value)
        if parts.scheme != 'https' or parts.hostname not in allowed or parts.username or parts.password:
            raise ValueError('untrusted_release_url')
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            check(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    check(url)
    req = urllib.request.Request(url, headers={'User-Agent': 'EveryFrame-Validator-Updater', 'Accept': 'application/vnd.github+json'})
    with urllib.request.build_opener(Redirect).open(req, timeout=30) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit: raise ValueError('release_response_too_large')
    return raw


def latest(fetch=request):
    release = json.loads(fetch(API, 2_000_000))
    tag = release['tag_name']; version(tag)
    if release.get('draft') is not False or release.get('prerelease') is not False:
        raise ValueError('published_stable_release_required')
    assets = release['assets']
    def asset(name, limit):
        entries = [a for a in assets if a.get('name') == name]
        if len(entries) != 1: raise ValueError('release_asset_missing_' + name)
        a = entries[0]
        expected = 'https://github.com/' + REPO + '/releases/download/' + tag + '/' + name
        if a.get('browser_download_url') != expected: raise ValueError('unexpected_asset_origin')
        raw = fetch(expected, limit)
        advertised = a.get('digest')
        if advertised is not None and advertised != 'sha256:' + hashlib.sha256(raw).hexdigest():
            raise ValueError('github_asset_digest_mismatch')
        return raw
    sums = asset('SHA256SUMS', 100_000).decode()
    def verified(name):
        raw = asset(name, 100_000)
        matches = [line.split() for line in sums.splitlines() if len(line.split()) == 2 and line.split()[1].lstrip('*') == name]
        if len(matches) != 1 or matches[0][0] != hashlib.sha256(raw).hexdigest():
            raise ValueError('release_checksum_mismatch')
        return raw
    pinned = image(verified('image.txt').decode().strip())
    manifest = json.loads(verified('auto-update.json'))
    if (set(manifest) != {'schema', 'version', 'minimumVersion', 'stateFormat', 'assignmentPolicies'}
            or manifest['schema'] != 1 or manifest['stateFormat'] != 1
            or manifest['version'] != tag.removeprefix('v')
            or not isinstance(manifest['assignmentPolicies'], list)
            or set(manifest['assignmentPolicies']) != POLICIES):
        raise ValueError('unsupported_update_manifest')
    version(manifest['minimumVersion'])
    if version(manifest['minimumVersion']) > version(manifest['version']):
        raise ValueError('invalid_minimum_version')
    return {**manifest, 'image': pinned, 'release': 'https://github.com/' + REPO + '/releases/tag/' + tag}


def command(args, timeout=600):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=timeout).stdout.strip()


def compose(root, config, *args):
    return command(['docker', 'compose', '--project-name', config['project'], '--file',
                    str(root / 'compose.json'), *args])


def set_image(root, snapshot, service, pinned):
    data = deepcopy(snapshot)
    data['services'][service]['image'] = image(pinned)
    data['services'][service]['pull_policy'] = 'never'
    write(root / 'compose.json', data)


def running(root, config):
    ids = compose(root, config, 'ps', '-q', config['service']).splitlines()
    if len(ids) != 1: raise ValueError('one_running_validator_required')
    info = json.loads(command(['docker', 'inspect', ids[0]]))[0]
    if not info['State']['Running']: raise ValueError('validator_not_running')
    return ids[0], info


def configure(root, file, project, service, *, yes=False):
    if not yes: raise ValueError('setup_requires_explicit_yes')
    if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,62}', project): raise ValueError('invalid_compose_project')
    if not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', service): raise ValueError('invalid_service')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(root):
        if (root / 'config.json').exists(): raise ValueError('updater_already_configured')
        snapshot = json.loads(command(['docker', 'compose', '--project-name', project, '--file', str(file.resolve()), 'config', '--format', 'json']))
        selected = snapshot['services'][service]
        args = selected.get('command', [])
        if (not isinstance(args, list) or not args or args[0] != 'run' or args.count('--state-dir') != 1
                or selected.get('build') or selected.get('entrypoint') or selected.get('scale', 1) != 1):
            raise ValueError('standard_validator_run_command_required')
        state = args[args.index('--state-dir') + 1]
        if not state.startswith('/') or '--once' in args: raise ValueError('persistent_absolute_state_required')
        volumes = selected.get('volumes', [])
        if not any(v.get('type') in ('bind', 'volume') and v.get('target') == state
                   and v.get('source') and not v.get('read_only', False) for v in volumes):
            raise ValueError('dedicated_persistent_state_mount_required')
        write(root / 'compose.json', snapshot)
        config = {'schema': 1, 'enabled': True, 'project': project, 'service': service,
                  'stateDir': state, 'signing': '--submit' in args, 'sourceCompose': str(file.resolve())}
        cid, info = running(root, config)
        if info['Config'].get('Labels', {}).get('com.docker.compose.project') != project:
            raise ValueError('running_project_mismatch')
        if info['Config'].get('Cmd') != args:
            raise ValueError('running_command_differs_from_compose')
        hashes = command(['docker', 'compose', '--project-name', project, '--file', str(file.resolve()),
                          'config', '--hash', service]).split()
        if not hashes or hashes[-1] != info['Config']['Labels'].get('com.docker.compose.config-hash'):
            raise ValueError('running_configuration_differs_from_compose')
        health = selected.get('healthcheck', {})
        if health.get('disable') or health.get('test') != ['CMD', 'python', '-m', 'everyframe_validator.healthcheck', state]:
            raise ValueError('standard_validator_healthcheck_required')
        images = json.loads(command(['docker', 'image', 'inspect', info['Image']]))
        digests = [d for d in images[0].get('RepoDigests', []) if d.startswith(IMAGE + '@sha256:')]
        if len(digests) != 1: raise ValueError('running_official_image_digest_required')
        installed = command(['docker', 'exec', cid, 'everyframe-validator', '--version']); version(installed)
        set_image(root, snapshot, service, digests[0])
        write(root / 'snapshot.json', read(root / 'compose.json'))
        write(root / 'current.json', {'version': installed, 'image': digests[0]})
        write(root / 'config.json', config)
        # This reviewed bootstrap remains fixed. Releases update only the image.
        shutil.copyfile(__file__, root / 'auto-update.py'); (root / 'auto-update.py').chmod(0o700)
    return {'state': 'configured', 'signing': config['signing'], 'timerInstalled': False}


def tick(root, *, check=False, get_latest=latest):
    with locked(root):
        config = read(root / 'config.json')
        if not config.get('enabled'): return {'state': 'disabled'}
        if (root / 'pending.json').exists(): raise ValueError('unfinished_update_requires_review')
        current = read(root / 'current.json')
        candidate = get_latest()
        old, new = version(current['version']), version(candidate['version'])
        if new <= old: return {'state': 'up_to_date', 'version': current['version']}
        if new[0] != old[0] or old < version(candidate['minimumVersion']):
            return {'state': 'manual_upgrade_required', 'version': candidate['version']}
        if check: return {'state': 'update_available', **candidate}
        snapshot = read(root / 'snapshot.json')
        cid, info = running(root, config)
        actual = command(['docker', 'exec', cid, 'everyframe-validator', '--version'])
        if actual != current['version']: raise ValueError('running_version_changed')
        runtime_image = json.loads(command(['docker', 'image', 'inspect', current['image']]))[0]['Id']
        if runtime_image != info['Image']: raise ValueError('running_image_changed')
        # Pull and inspect before touching the running service.
        command(['docker', 'pull', candidate['image']])
        reported = command(['docker', 'run', '--rm', '--network', 'none', candidate['image'], '--version'])
        if reported != candidate['version']: raise ValueError('candidate_version_mismatch')
        status = json.loads(command(['docker', 'exec', cid, 'everyframe-validator', 'status', '--state-dir', config['stateDir']]))
        if status.get('state') == 'blocked': return {'state': 'deferred', 'reason': 'journal_requires_reconciliation'}
        write(root / 'pending.json', {'previous': current, 'candidate': candidate, 'stage': 'stopping', 'at': int(time.time())})
        compose(root, config, 'stop', '-t', '330', config['service'])
        set_image(root, snapshot, config['service'], candidate['image'])
        try:
            # The previous process has exited. Offline doctor never signs or rewrites journals.
            result = json.loads(compose(root, config, 'run', '--rm', '--no-deps', config['service'],
                                        'doctor', '--state-dir', config['stateDir'], '--offline'))
            if not result.get('ok') or (config['signing'] and not result.get('signingPrerequisitesOk')):
                raise ValueError('candidate_offline_preflight_failed')
            write(root / 'pending.json', {'previous': current, 'candidate': candidate, 'stage': 'starting', 'at': int(time.time())})
            compose(root, config, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                    '--wait', '--wait-timeout', '480', config['service'])
            newcid, newinfo = running(root, config)
            if newinfo['State'].get('Health', {}).get('Status') != 'healthy': raise ValueError('candidate_not_healthy')
            if command(['docker', 'exec', newcid, 'everyframe-validator', '--version']) != candidate['version']:
                raise ValueError('started_version_mismatch')
            write(root / 'current.json', {'version': candidate['version'], 'image': candidate['image']})
            (root / 'pending.json').unlink()
        except subprocess.TimeoutExpired:
            # Docker may still be handling the request. Do not race it with a rollback.
            raise ValueError('update_timeout_requires_review') from None
        except Exception:
            compose(root, config, 'stop', '-t', '330', config['service'])
            # Keep newest journal and the durable marker. An older reader may not
            # understand a policy accepted by the candidate; never auto-downgrade.
            raise ValueError('update_failed_stopped_state_preserved') from None
        return {'state': 'updated', 'version': candidate['version'], 'image': candidate['image']}


def install_timer(root, *, yes=False):
    if not yes or os.getuid() != 0: raise ValueError('timer_install_requires_root_and_yes')
    if not re.fullmatch(r'/[a-zA-Z0-9_./-]+', str(root)): raise ValueError('simple_absolute_updater_path_required')
    read(root / 'config.json')
    unit = Path('/etc/systemd/system/everyframe-validator-update.service')
    timer = unit.with_suffix('.timer')
    service_text = ('[Unit]\nDescription=EveryFrame validator release updater\nAfter=docker.service network-online.target\n'
                    '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=1800\n'
                    'ExecStart=' + sys.executable + ' ' + str(root / 'auto-update.py') + ' tick --root ' + str(root) + '\n')
    timer_text = ('[Unit]\nDescription=Check EveryFrame validator releases\n[Timer]\nOnBootSec=10m\nOnUnitInactiveSec=1h\n'
                  'RandomizedDelaySec=10m\nPersistent=true\n[Install]\nWantedBy=timers.target\n')
    if unit.exists() or timer.exists(): raise ValueError('existing_update_units_require_review')
    unit.write_text(service_text); timer.write_text(timer_text)
    command(['systemctl', 'daemon-reload']); command(['systemctl', 'enable', '--now', timer.name])
    return {'state': 'timer_enabled', 'interval': '1 hour plus up to 10 minutes jitter'}


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('setup', 'check', 'tick', 'disable', 'enable', 'status', 'install-timer'):
        cmd = sub.add_parser(action); cmd.add_argument('--root', type=Path, required=True)
        if action in ('setup', 'install-timer', 'enable'): cmd.add_argument('--yes', action='store_true')
        if action == 'setup':
            cmd.add_argument('--compose-file', type=Path, required=True)
            cmd.add_argument('--project', required=True); cmd.add_argument('--service', default='validator')
    args = p.parse_args(); root = args.root.absolute()
    try:
        if args.action == 'setup': result = configure(root, args.compose_file, args.project, args.service, yes=args.yes)
        elif args.action in ('check', 'tick'): result = tick(root, check=args.action == 'check')
        elif args.action == 'install-timer': result = install_timer(root, yes=args.yes)
        else:
            with locked(root):
                config = read(root / 'config.json')
                if args.action in ('disable', 'enable'):
                    if args.action == 'enable' and not args.yes: raise ValueError('enable_requires_yes')
                    config['enabled'] = args.action == 'enable'; write(root / 'config.json', config)
                result = {'state': 'enabled' if config['enabled'] else 'disabled',
                          'current': read(root / 'current.json'), 'pending': (root / 'pending.json').exists()}
        write(root / 'last-result.json', {**result, 'at': int(time.time())})
        print(json.dumps(result))
    except BlockingIOError: print(json.dumps({'state': 'busy'}))
    except Exception as exc:
        # Do not print Docker output, compose content, RPC credentials or raw errors.
        code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(json.dumps({'state': 'failed', 'code': code})); raise SystemExit(2)


if __name__ == '__main__': main()
