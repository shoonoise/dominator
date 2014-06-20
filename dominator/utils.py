import functools
import itertools
import difflib
import inspect
import string
import pprint
import logging
import colorlog
import docker

from .settings import settings


class PartialFormatter(string.Formatter):
    def __init__(self):
        self._retrieved = []

    def get_field(self, field_name, args, kwargs):
        try:
            val = super().get_field(field_name, args, kwargs)
        except (KeyError, AttributeError):
            val = '', field_name
        kwargs['_extra'].pop(field_name.split('.')[0], None)
        return val


class PartialLoggingFormatter(colorlog.ColoredFormatter):
    def formatMessage(self, record):
        return PartialFormatter().format(self._style._fmt, **vars(record))

    def formatException(self, exc_info):
        max_vars_lines = 30
        max_line_length = 100
        # First get the original formatted exception.
        exc_text = super().formatException(exc_info)
        # Now we're going to format and add the locals information.
        output_lines = [exc_text, '\n']
        tb = exc_info[2]  # This is the outermost frame of the traceback.
        while tb.tb_next:
            tb = tb.tb_next  # Zoom to the innermost frame.
        output_lines.append('Locals at innermost frame:\n')
        locals_text = pprint.pformat(tb.tb_frame.f_locals, indent=2)
        locals_lines = locals_text.split('\n')
        if len(locals_lines) > max_vars_lines:
            locals_lines = locals_lines[:max_vars_lines]
            locals_lines[-1] = '...'
        output_lines.extend(
            line[:max_line_length - 3] + '...' if len(line) > max_line_length else line
            for line in locals_lines)
        output_lines.append('\n')
        return '\n'.join(output_lines)


def getlogger(name='dominator', bindto=1, **kwargs):
    for frame, _, _, _, _, _ in inspect.stack()[1:]:
        if '__logger_context' in frame.f_locals:
            context = frame.f_locals['__logger_context'].copy()
            break
    else:
        context = {}

    context.update(kwargs)
    inspect.stack()[bindto][0].f_locals['__logger_context'] = context

    return BoundLogger(logging.getLogger(name), context=context)


class BoundLogger(logging.Logger):
    def __init__(self, logger, context):
        self.context = context
        self.logger = logger
        self.level = logger.level
        self.parent = logger.parent

    def _log(self, level, msg, args, exc_info=None, stack_info=False, **kwargs):
        extra = kwargs.copy()
        extra.update(self.context)

        class PrettyDict(dict):
            def __format__(self, _):
                return ' '.join('{}={}'.format(k, v) for k, v in self.items())
        extra['_extra'] = PrettyDict(extra)

        self.logger._log(level, msg, args, exc_info, extra)


def cached(fun):
    return functools.lru_cache(100)(fun)


def groupby(objects, key):
    return itertools.groupby(sorted(objects, key=key), key=key)


def _as(agg):
    aggfun = agg

    def decorator(fun):
        @functools.wraps(fun)
        def wrapper(*args, **kwargs):
            return aggfun(fun(*args, **kwargs))
        return wrapper
    return decorator

asdict = _as(dict)
aslist = _as(list)


@cached
def getdocker(url=None):
    url = url or settings.get('dockerurl')
    getlogger(url=url).debug('creating docker client')
    return docker.Client(url)


def getrepo(repo):
    if '/' not in repo and 'docker-namespace' in settings:
        repo = '{}/{}'.format(settings['docker-namespace'], repo)
    return repo


@aslist
def compare_env(expected: dict, actual: dict):
    getlogger().debug('comparing environment')
    for name, value in actual.items():
        if name not in expected:
            yield name, '', value or '""'
        elif expected[name] != value:
            yield name, expected[name], value or '""'

    for name, value in expected.items():
        if name not in actual:
            yield name, value or '""', ''


@aslist
def compare_ports(cont, actual: dict):
    getlogger().debug('comparing ports')
    for name, port_expected in cont.ports.items():
        extport_expected = cont.extports.get(name, port_expected)
        proto_expected = cont.portproto.get(name, 'tcp')

        matched_actual = [info for name, info in actual.items()
                          if name == '{}/{}'.format(port_expected, proto_expected)]

        if len(matched_actual) == 0:
            yield name, [('int', port_expected, ''),
                         ('ext', extport_expected, ''),
                         ('proto', proto_expected, '')]
        else:
            extport_actual = int(matched_actual[0][0]['HostPort'])
            if extport_actual != extport_expected:
                yield name, [('ext', extport_expected, extport_actual)]

    for portname, portinfo in actual.items():
        matched_expected = [name for name, port in cont.ports.items()
                            if portname == '{}/{}'.format(port, cont.portproto.get(name, 'tcp'))]
        if len(matched_expected) == 0:
            port, proto = portname.split('/')
            yield port, [('int', '', port),
                         ('ext', '', portinfo[0]['HostPort']),
                         ('proto', '', proto)]


@aslist
def compare_volumes(cont, cinfo):
    getlogger().debug('comparing volumes')
    for dest, path in cinfo['Volumes'].items():
        ro = not cinfo['VolumesRW'][dest]
        matched_expected = [volume for volume in cont.volumes if volume.dest == dest]
        if len(matched_expected) == 0:
            if not path.startswith('/var/lib/docker/vfs/dir'):
                yield dest, [('path', '', path),
                             ('ro', '', ro)]
        else:
            volume = matched_expected[0]
            getlogger(volume=volume).debug('comparing volume')
            diffs = []

            if volume.getpath(cont) != path:
                diffs.append(('path', volume.getpath(cont), path))
            elif hasattr(volume, 'files'):
                configdiff = compare_files(cont, volume)
                if len(configdiff) > 0:
                    diffs.append(('files', configdiff))

            if volume.ro != ro:
                diffs.append(('ro', volume.ro, ro))

            if len(diffs) > 0:
                yield dest, diffs

    for volume in cont.volumes:
        matched_actual_path = [path for dest, path in cinfo['Volumes'].items() if dest == volume.dest]
        if len(matched_actual_path) == 0:
            yield volume.dest, [('path', volume.getpath(cont), ''),
                                ('ro', volume.ro, '')]


@aslist
def compare_files(container, volume):
    getlogger().debug('comparing files')
    for file in volume.files:
        try:
            actual = file.load(container, volume)
        except FileNotFoundError:
            actual = ''
        expected = file.data(container)
        if actual != expected:
            diff = difflib.Differ().compare(actual.split('\n'), expected.split('\n'))
            yield file.name, [line for line in diff if line[:2] != '  ']


@aslist
def compare_container(cont, cinfo):
    getlogger().debug('comparing container')
    imagerepo, imageid = cinfo['Config']['Image'].split(':')

    for key, expected, actual in [
        ('name', cont.name, cinfo['Name'][1:]),
        ('image.repo', cont.image.repository, imagerepo),
        ('image.id', cont.image.id, imageid),
        ('command', cont.command or cont.image.command, ' '.join(cinfo['Config']['Cmd'])),
        ('memory', cont.memory, cinfo['Config']['Memory']),
    ]:
        if expected != actual:
            yield key, expected, actual

    for key, subkeys in [('env', compare_env(dict(list(cont.image.env.items()) + list(cont.env.items())),
                                             dict(var.split('=', 1) for var in cinfo['Config']['Env']))),
                         ('ports', compare_ports(cont, cinfo['NetworkSettings']['Ports'])),
                         ('volumes', compare_volumes(cont, cinfo))]:
        if len(subkeys) > 0:
            yield key, subkeys


@aslist
def ships_from_conductor(name):
    import conductor_client
    from .entities import Ship
    getlogger(group=name).debug('retrieving ships from Conductor')
    for host in conductor_client.Group(name=name).hosts:
        yield Ship(
            fqdn=host.fqdn,
            datacenter=host.datacenter.name,
            name=host.fqdn.split('.')[0],
        )


def _nova_client(cluster):
    from novaclient.v1_1 import client
    novaconfig = settings['nova'][cluster]['client']
    getlogger().debug('creating nova client', **novaconfig)
    return client.Client(**novaconfig)


@aslist
def ships_from_nova(cluster, metadata):
    from .entities import Ship
    getlogger(cluster=cluster, **metadata).debug('retrieving ships from Openstack')
    nova = _nova_client(cluster)
    for server in nova.servers.findall():
        for k, v in metadata.items():
            if server.metadata.get(k) != v:
                break
        else:
            yield Ship(
                name=server.name,
                fqdn='{}.{}'.format(server.name, settings['nova'][cluster]['domain']),
                novacluster=cluster,
            )


def datacenter_from_racktables(hostname):
    import requests
    import pyquery
    getlogger().debug('retrieving datacenter from Racktables')
    r = requests.get(
        url=settings['racktables']['url'],
        auth=(settings['racktables']['user'], settings['racktables']['password']),
        params={'page': 'search', 'q': hostname},
    )
    return pyquery.PyQuery(r.text)('a.tag-279').text()


def ship_memory_from_bot(fqdn):
    import requests
    # BOT response format: 'ok:64GB'
    r = requests.get('http://bot.yandex-team.ru/api/ram-summary.php?name={}'.format(fqdn))
    if r.status_code != 200 or r.text[:2] != 'ok':
        raise RuntimeError('failed to get RAM for {} from BOT'.format(fqdn))
    return int(r.text.split(':')[1][:-3]) * 1024**3


def ship_memory_from_nova(ship):
    c = _nova_client(ship.novacluster)
    return c.flavors.get(c.servers.find(name=ship.name).flavor['id']).ram * 1024**2


def docker_lines(records):
    buf = ''
    for record in records:
        buf += record.decode()
        while '\n' in buf:
            line, buf = buf.split('\n', 1)
            yield line
