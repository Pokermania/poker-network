# -*- coding: utf-8 *-*

from collections import namedtuple
from reflogging import root_logger
from reflogging.handlers import BaseHandler

Message = namedtuple('Message', ['severity', 'path', 'refs', 'message', 'args', 'formated'])

root_logger.set_level(10)

class TestLoggingHandler(BaseHandler):

    def __init__(self):
        BaseHandler.__init__(self)
        self.set_level(10)

    def record(self, severity, name, refs, format, *a, **kw):
        log_history.output.append(Message(
            severity = severity,
            path = name,
            refs = str(refs),
            message = format,
            args = a,
            formated = format % a if a else format
        ))

class Log(object):

    def __init__(self):
        self.reset()

    def reset(self):
        self.output = []

    def get_all(self):
        return [m.formated for m in self.output]

    def search(self, needle):
        for m in self.output:
            if needle in m.formated:
                return True
        return False

log_history = Log()
root_logger.add_handler(TestLoggingHandler())




