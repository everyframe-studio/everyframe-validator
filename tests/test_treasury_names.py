"""New operator names must preserve existing consent and publisher policy."""
import argparse
import json
import sys

from everyframe_validator import cli, assignment_publisher
from everyframe_validator.core import prize_pool
from conftest import hot


def test_treasury_and_legacy_publisher_options_have_identical_destinations(tmp_path):
    parser = argparse.ArgumentParser()
    assignment_publisher.add_treasury_arguments(parser)
    new = parser.parse_args(['--treasury-wallet-from-chain-epoch', '42', '--treasury-wallet-coldkey', hot(16)])
    old = parser.parse_args(['--contest-pool-from-chain-epoch', '42', '--contest-pool-coldkey', hot(16)])
    assert vars(new) == vars(old)
    assert assignment_publisher.contest_activation(new, tmp_path) == assignment_publisher.contest_activation(old, tmp_path)


def test_treasury_and_legacy_acceptance_preserve_consent_and_journal(profile, monkeypatch, capsys):
    root, _ = profile
    before = {name: (root/name).read_bytes() for name in ('config.json', 'identity.json', 'journal.db')}
    consent = None
    for command in ('accept-treasury-wallet', 'accept-contest-pool'):
        monkeypatch.setattr(sys, 'argv', ['everyframe-validator', command, '--state-dir', str(root), '--coldkey', hot(16), '--yes'])
        cli.main()
        result = json.loads(capsys.readouterr().out)
        assert result['policy'] == prize_pool.POLICY
        assert result['submittedTransaction'] is False
        assert result['message'].startswith('Treasury wallet accepted.')
        saved = (root/'prize-pool-consent.json').read_bytes()
        if consent is not None:
            assert saved == consent
        consent = saved
    assert {name: (root/name).read_bytes() for name in before} == before
    assert not (root/'authorization.json').exists()


def test_help_prefers_treasury_name_and_retains_legacy_command():
    text = cli.parser().format_help()
    assert 'accept-treasury-wallet' in text and 'accept-contest-pool' in text

    assert text.index('accept-treasury-wallet') < text.index('accept-contest-pool')
