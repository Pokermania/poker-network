#
# Copyright (C) 2009 Loic Dachary <loic@dachary.org>
# Copyright (C) 2009 Johan Euphrosine <proppy@aminche.com>
#
# This software's license gives you freedom; you can copy, convey,
# propagate, redistribute and/or modify this program under the terms of
# the GNU Affero General Public License (AGPL) as published by the Free
# Software Foundation (FSF), either version 3 of the License, or (at your
# option) any later version of the AGPL published by the FSF.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program in a file in the toplevel directory called
# "AGPLv3".  If not, see <http://www.gnu.org/licenses/>.
#
import simplejson as json
from twisted.internet import defer, protocol, reactor, error
from twisted.internet.defer import CancelledError
from twisted.web import http, client
from twisted.python.util import InsensitiveDict
from twisted.python.runtime import seconds

from pokernetwork.pokerpackets import *
from pokernetwork import pokersite
from pokernetwork import log as network_log
log = network_log.getChild('pokerrestclient')


class RestClientFactory(protocol.ClientFactory):

    protocol = client.HTTPPageGetter
    noisy = False
    
    def __init__(self, host, port, path, data, timeout = 60):
        self.log = log.getChild('ClientFactory')
        self.timeout = timeout
        self.agent = "RestClient"
        self.headers = InsensitiveDict()
        self.headers.setdefault('Content-Length', len(data))
        self.headers.setdefault("connection", "close")
        self.method = 'POST'
        self.url = 'http://' + host + ':' + str(port) + path
        self.scheme = 'http'
        self.postdata = data
        self.host = host
        self.port = port
        self.path = path
        self.waiting = 1
        self.deferred = defer.Deferred()
        self.response_headers = None
        self.cookies = {}
        self._disconnectedDeferred = defer.Deferred()

    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.url)

    def buildProtocol(self, addr):
        p = protocol.ClientFactory.buildProtocol(self, addr)
        if self.timeout:
            timeoutCall = reactor.callLater(self.timeout, p.timeout)
            self.deferred.addBoth(self._cancelTimeout, timeoutCall)
        return p

    def _cancelTimeout(self, result, timeoutCall):
        if timeoutCall.active():
            timeoutCall.cancel()
        return result

    def gotHeaders(self, headers):
        self.response_headers = headers
        
    def gotStatus(self, version, status, message):
        self.version, self.status = version, status

    def page(self, page):
        if self.waiting:
            self.waiting = 0
            self.deferred.callback(page)

    def noPage(self, reason):
        if self.waiting:
            self.waiting = 0
            self.deferred.errback(reason)

    def clientConnectionFailed(self, _, reason):
        if self.waiting:
            self.waiting = 0
            self.deferred.errback(reason)

class PokerRestClient:
    DEFAULT_LONG_POLL_FREQUENCY = 0.1
    
    def __init__(self, host, port, path, longPollCallback, timeout = 60):
        self.log = log.getChild(self.__class__.__name__)
        self.queue = defer.succeed(True)
        self.pendingLongPoll = False
        self.minLongPollFrequency = 0.01
        self.sentTime = 0
        self.host = host
        self.port = port
        self.path = path
        self.timer = None
        self.timeout = timeout
        self.longPollCallback = longPollCallback
        if longPollCallback:
            self.longPollFrequency = PokerRestClient.DEFAULT_LONG_POLL_FREQUENCY
            self.scheduleLongPoll(0)
        else:
            self.longPollFrequency = -1

    def sendPacket(self, packet, data):
        if self.pendingLongPoll:
            self.sendPacketData('{ "type": "PacketPokerLongPollReturn" }')
        d = defer.Deferred()
        d.addCallbacks(self.receivePacket, self.receiveError)
        self.queue.addCallback(lambda status: self.sendPacketData(data))
        self.queue.chainDeferred(d)
        if packet.type == PACKET_POKER_LONG_POLL:
            self.pendingLongPoll = True
        return d

    def receivePacket(self, data):
        self.log.debug("receivePacket %s", data)
            
        if self.pendingLongPoll:
            self.scheduleLongPoll(0)
        self.pendingLongPoll = False
        args = json.loads(data)
        args = pokersite.fromutf8(args)
        packets = list(pokersite.args2packets(args))
        return packets

    def receiveError(self, data):
        return [ PacketError(message = str(data)) ]
    
    def sendPacketData(self, data):
        self.log.debug("sendPacketData %s", data)
        factory = RestClientFactory(self.host, self.port, self.path, data, self.timeout)
        reactor.connectTCP(self.host, self.port, factory)
        self.sentTime = seconds()
        return factory.deferred

    def clearTimeout(self):
        if self.timer and self.timer.active():
            self.timer.cancel()
        self.timer = None
        
    def scheduleLongPoll(self, delta):
        if self.longPollFrequency > 0:        
            self.clearTimeout()
            self.timer = reactor.callLater(max(self.minLongPollFrequency, self.longPollFrequency - delta), self.longPoll)

    def longPoll(self):
        if self.longPollFrequency > 0:
            delta = seconds() - self.sentTime
            in_line = len(self.queue.callbacks)
            if in_line <= 0 and delta > self.longPollFrequency:
                self.clearTimeout()
                d = self.sendPacket(PacketPokerLongPoll(),'{ "type": "PacketPokerLongPoll" }')
                d.addCallback(self.longPollCallback)
            else:
                self.scheduleLongPoll(delta)
                
    def cancel(self):
        self.clearTimeout()
        self.longPollFrequency = -1
        handle_cancel = lambda fail: True if fail.check(CancelledError) else fail
        old_queue, self.queue.callbacks = self.queue.callbacks, []     
        self.queue.addErrback(handle_cancel)
#        self.queue.callbacks.extend(old_queue)
        self.queue.cancel()
        
class PokerProxyClient(http.HTTPClient):
    """
    Used by PokerProxyClientFactory to implement a simple web proxy.
    """

    def __init__(self, command, rest, version, headers, data, father):
        self.log = log.getChild(self.__class__.__name__)
        self.father = father
        self.command = command
        self.rest = rest
        if "proxy-connection" in headers:
            del headers["proxy-connection"]
        headers["connection"] = "close"
        self.headers = headers
        self.data = data

    def connectionMade(self):
        self.sendCommand(self.command, self.rest)
        for header, value in self.headers.items():
            self.sendHeader(header, value)
        self.endHeaders()
        self.transport.write(self.data)

    def handleStatus(self, version, code, message):
        self.father.setResponseCode(int(code), message)

    def handleHeader(self, key, value):
        self.father.setHeader(key, value)

    def handleResponse(self, buffer):
        self.father.write(buffer)
        
    def connectionLost(self, reason):
        self.father.finish()

class PokerProxyClientFactory(protocol.ClientFactory):

    serial = 0
    noisy = False
    protocol = PokerProxyClient

    def __init__(self, command, rest, version, headers, data, father, destination):
        self.log = log.getChild(self.__class__.__name__)
        self.father = father
        self.command = command
        self.rest = rest
        self.headers = headers
        self.data = data
        self.version = version
        self.deferred = defer.Deferred()
        self.destination = destination
        PokerProxyClientFactory.serial += 1
        self.serial = PokerProxyClientFactory.serial

    def doStart(self):
        self.log.debug("START %s => %s", self.data, self.destination)
        protocol.ClientFactory.doStart(self)

    def doStop(self):
        self.log.debug("STOP")
        protocol.ClientFactory.doStop(self)

    def buildProtocol(self, addr):
        return self.protocol(self.command, self.rest, self.version,
                             self.headers, self.data, self.father)

    def clientConnectionFailed(self, connector, reason):
        if not self.deferred.called:
            self.deferred.errback(reason)

    def clientConnectionLost(self, connector, reason):
        if not self.deferred.called:
            if reason.check(error.ConnectionDone):
                self.deferred.callback(True)
            else:
                self.deferred.errback(reason)
