# Copyright (c) 2013 Ian C. Good
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

from __future__ import absolute_import

import sys
import os
import os.path
import warnings
from functools import wraps
from contextlib import contextmanager

from config import Config, ConfigError, ConfigInputStream
import slimta.system

from .validation import ConfigValidation, ConfigValidationError
from .celery import get_celery_app, get_celery_worker
from .logging import setup_logging


class SlimtaState(object):

    _global_config_files = [os.path.expanduser('~/.slimta/slimta.conf'),
                            '/etc/slimta/slimta.conf']

    def __init__(self, program):
        self.program = program
        self.args = None
        self.cfg = None
        self.edges = {}
        self.queues = {}
        self.relays = {}
        self._celery = None

    @contextmanager
    def _with_chdir(self, new_dir):
        old_dir = os.getcwd()
        os.chdir(new_dir)
        try:
            yield old_dir
        finally:
            os.chdir(old_dir)

    @contextmanager
    def _with_pid_file(self):
        if not self.args.pid_file:
            yield
        else:
            pid_file = os.path.abspath(self.args.pid_file)
            with open(pid_file, 'w') as f:
                f.write('{0}\n'.format(os.getpid()))
            try:
                yield
            finally:
                try:
                    os.unlink(pid_file)
                except OSError:
                    pass

    @contextmanager
    def _with_sighandlers(self):
        from signal import SIGTERM
        from gevent import signal
        def handle_term():
            sys.exit(0)
        old_term = signal(SIGTERM, handle_term)
        try:
            yield
        finally:
            signal(SIGTERM, old_term)

    def _try_configs(self, files):
        for config_file in files:
            config_file = os.path.expanduser(config_file)
            config_dir = os.path.abspath(os.path.dirname(config_file))
            config_base = os.path.basename(config_file)
            with self._with_chdir(config_dir):
                if os.path.exists(config_base):
                    return Config(config_base), config_file
        return None, None

    def load_config(self, argparser, args):
        if self.args:
            return
        self.args = args

        files = self._global_config_files
        if args.config:
            files = [args.config]

        self.cfg, config_file = self._try_configs(files)
        if self.cfg:
            try:
                ConfigValidation.check(self.cfg, self.program)
            except ConfigValidationError as e:
                argparser.error(str(e))
        else:
            argparser.error('No configuration files found!')

    def drop_privileges(self):
        process_options = self.cfg.process.get(self.program)
        user = process_options.get('user')
        group = process_options.get('group')
        if user or group:
            if os.getuid() == 0:
                slimta.system.drop_privileges(user, group)
            else:
                warnings.warn('Only superuser can drop privileges.')

    def redirect_streams(self):
        process_options = self.cfg.process.get(self.program)
        flag = process_options.get('daemon', False)
        if flag and not self.args.attached:
            so = process_options.get('stdout')
            se = process_options.get('stderr')
            si = process_options.get('stdin')
            slimta.system.redirect_stdio(so, se, si)

    def daemonize(self):
        flag = self.cfg.process.get(self.program).get('daemon', False)
        if flag and not self.args.attached:
            slimta.system.daemonize()

    def setup_logging(self):
        settings = self.cfg.process.get(self.program).get('logging')
        setup_logging(settings)

    def _start_relay(self, name, options=None):
        if name in self.relays:
            return self.relays[name]
        if not options:
            options = getattr(self.cfg.relay, name)
        new_relay = None
        if options.type == 'mx':
            from slimta.relay.smtp.mx import MxSmtpRelay
            from .helpers import fill_hostname_template
            kwargs = {}
            kwargs['connect_timeout'] = options.get('connect_timeout', 30)
            kwargs['command_timeout'] = options.get('command_timeout', 30)
            kwargs['data_timeout'] = options.get('data_timeout', 60)
            kwargs['idle_timeout'] = options.get('idle_timeout', 10)
            kwargs['pool_size'] = options.get('concurrent_connections', 5)
            kwargs['ehlo_as'] = fill_hostname_template(options.get('ehlo_as'))
            if 'tls' in options:
                kwargs['tls'] = dict(options.tls)
            new_relay = MxSmtpRelay(**kwargs)
        elif options.type == 'static':
            from slimta.relay.smtp.static import StaticSmtpRelay
            from .helpers import fill_hostname_template
            kwargs = {}
            kwargs['host'] = options.host
            kwargs['port'] = options.get('port', 25)
            kwargs['connect_timeout'] = options.get('connect_timeout', 30)
            kwargs['command_timeout'] = options.get('command_timeout', 30)
            kwargs['data_timeout'] = options.get('data_timeout', 60)
            kwargs['idle_timeout'] = options.get('idle_timeout', 10)
            kwargs['pool_size'] = options.get('concurrent_connections', 5)
            kwargs['ehlo_as'] = fill_hostname_template(options.get('ehlo_as'))
            if 'tls' in options:
                kwargs['tls'] = dict(options.tls)
            new_relay = StaticSmtpRelay(**kwargs)
        elif options.type == 'maildrop':
            from slimta.maildroprelay import MaildropRelay
            executable = options.get('executable')
            new_relay = MaildropRelay(executable=executable)
        else:
            raise ConfigError('relay type does not exist: '+options.type)
        self.relays[name] = new_relay
        return new_relay

    def _start_queue(self, name, options=None):
        if name in self.queues:
            return self.queues[name]
        if not options:
            options = getattr(self.cfg.queue, name)
        from .helpers import add_queue_policies, build_backoff_function
        new_queue = None
        if options.type == 'memory':
            from slimta.queue import Queue
            from slimta.queue.dict import DictStorage
            relay_name = options.get('relay')
            if not relay_name:
                raise ConfigError('queue sections must be given a relay name')
            relay = self._start_relay(relay_name)
            store = DictStorage()
            backoff = build_backoff_function(options.get('retry'))
            new_queue = Queue(store, relay, backoff=backoff)
        elif options.type == 'disk':
            from slimta.queue import Queue
            from slimta.diskstorage import DiskStorage
            relay_name = options.get('relay')
            if not relay_name:
                raise ConfigError('queue sections must be given a relay name')
            relay = self._start_relay(relay_name)
            env_dir = options.envelope_dir
            meta_dir = options.meta_dir
            tmp_dir = options.get('tmp_dir')
            store = DiskStorage(env_dir, meta_dir, tmp_dir)
            backoff = build_backoff_function(options.get('retry'))
            new_queue = Queue(store, relay, backoff=backoff)
        elif options.type == 'proxy':
            from slimta.queue.proxy import ProxyQueue
            relay_name = options.get('relay')
            if not relay_name:
                raise ConfigError('queue sections must be given a relay name')
            relay = self._start_relay(relay_name)
            new_queue = ProxyQueue(relay)
        elif options.type == 'celery':
            from slimta.celeryqueue import CeleryQueue
            relay_name = options.get('relay')
            if not relay_name:
                raise ConfigError('queue sections must be given a relay name')
            relay = self._start_relay(relay_name)
            backoff = build_backoff_function(options.get('retry'))
            new_queue = CeleryQueue(self.celery, relay, name, backoff=backoff)
        else:
            raise ConfigError('queue type does not exist: '+options.type)
        add_queue_policies(new_queue, options.get('policies', []))
        self.queues[name] = new_queue
        return new_queue

    @property
    def celery(self):
        if not self._celery:
            self._celery = get_celery_app(self.cfg)
        return self._celery

    def start_celery_queues(self):
        for name, options in dict(self.cfg.queue).items():
            if options.type == 'celery':
                self._start_queue(name, options)

    def _start_edge(self, name, options=None):
        if name in self.edges:
            return self.edges[name]
        if not options:
            options = getattr(self.cfg.edge, name)
        new_edge = None
        if options.type == 'smtp':
            from slimta.edge.smtp import SmtpEdge
            from .helpers import build_smtpedge_validators, build_smtpedge_auth
            from .helpers import fill_hostname_template
            ip = options.listener.get('interface', '127.0.0.1')
            port = int(options.listener.get('port', 25))
            queue_name = options.queue
            queue = self._start_queue(queue_name)
            kwargs = {}
            if options.get('tls'):
                kwargs['tls'] = dict(options.tls)
            kwargs['tls_immediately'] = options.get('tls_immediately', False)
            kwargs['validator_class'] = build_smtpedge_validators(options)
            kwargs['auth_class'] = build_smtpedge_auth(options)
            kwargs['command_timeout'] = 20.0
            kwargs['data_timeout'] = 30.0
            kwargs['max_size'] = options.get('max_size', 10485760)
            kwargs['hostname'] = fill_hostname_template(options.get('hostname'))
            new_edge = SmtpEdge((ip, port), queue, **kwargs)
            new_edge.start()
        else:
            raise ConfigError('edge type does not exist: '+options.type)
        self.edges[name] = new_edge
        return new_edge

    def start_edges(self):
        for name, options in dict(self.cfg.edge).items():
            self._start_edge(name, options)

    def worker_loop(self):
        try:
            with self._with_sighandlers():
                with self._with_pid_file():
                    get_celery_worker(self.celery).run()
        except (KeyboardInterrupt, SystemExit):
            pass

    def loop(self):
        from gevent.event import Event
        try:
            with self._with_sighandlers():
                with self._with_pid_file():
                    Event().wait()
        except (KeyboardInterrupt, SystemExit):
            pass


# vim:et:fdm=marker:sts=4:sw=4:ts=4
