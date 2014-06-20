import tempfile
import logging
import re
import pytest
from vcr import VCR
from colorama import Fore

from dominator.entities import LocalShip, Container, Image
from dominator.actions import dump, run, status, load_yaml
from dominator.settings import settings as _settings


vcr = VCR(cassette_library_dir='test/fixtures/vcr_cassettes')


@pytest.yield_fixture(autouse=True)
def settings():
    with tempfile.TemporaryDirectory() as configdir:
        _settings['configvolumedir'] = configdir
        # FIXME: use https endpoint because vcrpy doesn't handle UnixHTTPConnection
        _settings['dockerurl'] = 'http://localhost:4243'
        yield _settings


@pytest.fixture(autouse=True)
def logs():
    logging.basicConfig(level=logging.DEBUG)


@pytest.fixture
def ships():
    return [LocalShip()]


@pytest.fixture
@vcr.use_cassette('images.yaml')
def containers():
    return [Container(name='testcont',
                      ship=ship,
                      image=Image('busybox'),
                      command='sleep 10') for ship in ships()]


@pytest.fixture
def docker():
    import docker
    return docker.Client()


@vcr.use_cassette('run.yaml')
def test_run(capsys, containers):
    run(containers)
    _, _ = capsys.readouterr()
    status(containers)
    out, _ = capsys.readouterr()
    assert re.match(r'  testcont[ \t]+{color}[a-f0-9]{{7}}[ \t]+Up Less than a second'.format(
        color=re.escape(Fore.GREEN)), out.split('\n')[-2])


def test_dump(capsys, containers):
    dump(containers)
    dump1, _ = capsys.readouterr()
    assert dump1 != ''
    with tempfile.NamedTemporaryFile(mode='w') as tmp:
        tmp.write(dump1)
        tmp.flush()
        dump(load_yaml(tmp.name))
    dump2, _ = capsys.readouterr()
    assert dump1 == dump2