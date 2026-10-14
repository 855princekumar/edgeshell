"""Blocked commands: destructive forms are refused, everyday commands are not."""
import pytest

from app import create_app, db
from app.models import Settings
from app.services import command_policy

BAD = ['rm -rf /', 'rm  -rf   / ', 'sudo rm -rf /*', 'rm -fr /', 'rm -r -f /', 'rm -rf --no-preserve-root /',
       'mkfs.ext4 /dev/sda1', 'sudo mkfs -t ext4 /dev/sdb', 'ls; mkfs.vfat /dev/sdc1',
       'dd if=/dev/zero of=/dev/sda bs=1M', 'cat x > /dev/mmcblk0', ':(){ :|:& };:', 'sudo chmod -R 777 /',
       'rm -rf / ; echo done', 'RM -RF /']
GOOD = ['rm -rf /tmp/build', 'rm -rf /home/pi/app', 'chown -R pi:pi /home/pi/app', 'chmod -R 755 /opt/app',
        'ls -la /', 'df -h /', 'dd if=/dev/zero of=/tmp/test bs=1M count=1', 'echo mkfs-docs', 'man mkfs',
        'uptime', 'sudo systemctl restart nodered', 'docker ps -a']


@pytest.fixture
def app():
    app = create_app('testing')
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app


def _set(value):
    row = Settings.query.filter_by(key='blocked_commands').first()
    if row:
        row.value = value
    else:
        db.session.add(Settings(key='blocked_commands', value=value))
    db.session.commit()


@pytest.mark.parametrize('cmd', BAD)
def test_destructive_commands_are_blocked(app, cmd):
    assert command_policy.blocked(cmd)


@pytest.mark.parametrize('cmd', GOOD)
def test_everyday_commands_are_allowed(app, cmd):
    assert command_policy.blocked(cmd) is None


def test_the_administrators_list_is_enforced(app):
    db.session.add(Settings(key='blocked_commands', value='reboot\nshutdown -h'))
    db.session.commit()
    assert command_policy.blocked('sudo  REBOOT') == 'reboot'
    assert command_policy.blocked('sudo shutdown -h now') == 'shutdown -h'
    assert command_policy.blocked('uptime') is None


def test_the_fleet_terminal_enforces_it(app):
    from app.models.node import Node
    from app.models.user import User
    from app import _seed_defaults
    _seed_defaults()
    a = User.query.filter_by(username='admin').first()
    a.must_change_password, a.email = False, 'o@fleet.test'
    db.session.add(Node(name='n1', ip_address='10.0.0.1', username='pi', status='online'))
    db.session.commit()
    _set('reboot')
    c = app.test_client()
    c.post('/login', data={'username': 'admin', 'password': 'admin123'})
    nid = Node.query.first().id
    for cmd in ('sudo reboot', 'mkfs.ext4 /dev/sda1'):
        r = c.post('/terminal/execute', json={'command': cmd, 'node_ids': [nid]})
        assert r.status_code == 400 and 'blocked' in r.get_json()['error'].lower(), cmd
