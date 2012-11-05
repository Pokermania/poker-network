#
# -*- py-indent-offset: 4; coding: utf-8; mode: python -*-
#
# Copyright (C) 2006, 2007, 2008, 2009 Loic Dachary <loic@dachary.org>
# Copyright (C)       2008, 2009 Bradley M. Kuhn <bkuhn@ebb.org>
# Copyright (C)             2009 Johan Euphrosine <proppy@aminche.com>
# Copyright (C) 2004, 2005, 2006 Mekensleep <licensing@mekensleep.com>
#                                24 rue vieille du temple 75004 Paris
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
# Authors:
#  Loic Dachary <loic@dachary.org>
#  Bradley M. Kuhn <bkuhn@ebb.org> (2008-)
#  Henry Precheur <henry@precheur.org> (2004)
#  Cedric Pinson <cpinson@freesheep.org> (2004-2006)

from os.path import exists
from types import *
import re
import locale
import gettext
import libxml2

try: from collections import OrderedDict
except ImportError: from pokernetwork.ordereddict import OrderedDict

from pokernetwork import log as network_log
log = network_log.get_child('pokerservice')

from twisted.application import service
from twisted.internet import protocol, reactor, defer
from lockcheck import LockChecks
from twisted.python.runtime import seconds
from twisted.web import client

# disable noisy on HTTPClientFactory
client.HTTPClientFactory.noisy = False

try:
    from OpenSSL import SSL
    HAS_OPENSSL=True
except :
    log.inform("OpenSSL not available.")
    HAS_OPENSSL=False

from zope.interface import Interface
from zope.interface import implements

from MySQLdb.cursors import DictCursor

from twisted.python import components

from pokerengine.pokertournament import *
from pokerengine.pokercards import PokerCards
from pokerengine import pokerprizes

from pokernetwork.protocol import UGAMEProtocol
from pokernetwork.server import PokerServerProtocol
from pokernetwork.user import checkName, checkPassword
from pokernetwork.pokerdatabase import PokerDatabase
from pokerpackets.packets import packets2maps
from pokerpackets.networkpackets import *
from pokernetwork.pokersite import PokerTourneyStartResource, PokerImageUpload, PokerAvatarResource, PokerResource, args2packets
from pokernetwork.pokertable import PokerTable, PokerAvatarCollection
from pokernetwork import pokeravatar
from pokernetwork.user import User
from pokernetwork import pokercashier
from pokernetwork import pokernetworkconfig
from pokernetwork import pokermemcache
from pokerauth import get_auth_instance
from datetime import date

UPDATE_TOURNEYS_SCHEDULE_DELAY = 2 * 60
CHECK_TOURNEYS_SCHEDULE_DELAY = 60
DELETE_OLD_TOURNEYS_DELAY = 1 * 60 * 60

def _import(path):
    module = __import__(path)
    for i in path.split(".")[1:]:
        module = getattr(module, i)
    return module

class IPokerService(Interface):

    def createAvatar(self):
        """ """

    def destroyAvatar(self, avatar):
        """ """

class IPokerFactory(Interface):

    def createAvatar(self):
        """ """

    def destroyAvatar(self, avatar):
        """ """

    def buildProtocol(self, addr):
        """ """

class PokerFactoryFromPokerService(protocol.ServerFactory):

    implements(IPokerFactory)

    protocol = PokerServerProtocol

    def __init__(self, service):
        self.service = service

    def createAvatar(self):
        """ """
        return self.service.createAvatar()

    def destroyAvatar(self, avatar):
        """ """
        return self.service.destroyAvatar(avatar)

components.registerAdapter(PokerFactoryFromPokerService, IPokerService, IPokerFactory)

class PokerService(service.Service):

    implements(IPokerService)
    _spawnTourney_currency_from_date_format_re = re.compile('(%[dHIjmMSUwWyY])+')

    STATE_OFFLINE = 0
    STATE_ONLINE = 1
    STATE_SHUTTING_DOWN = 2

    log = log.get_child('PokerService')
    
    def __init__(self, settings):
        if type(settings) is StringType:
            settings_object = pokernetworkconfig.Config(['.'])
            settings_object.doc = libxml2.parseMemory(settings, len(settings))
            settings_object.header = settings_object.doc.xpathNewContext()
            settings = settings_object
        self.settings = settings
        
        self.joined_max = self.settings.headerGetInt("/server/@max_joined")
        if self.joined_max <= 0: self.joined_max = 4000
        
        self.sng_timeout = self.settings.headerGetInt("/server/@sng_timeout")
        if self.sng_timeout <= 0: self.sng_timeout = 3600
        
        self.missed_round_max = self.settings.headerGetInt("/server/@max_missed_round")
        if self.missed_round_max <= 0: self.missed_round_max = 10
        
        self.client_queued_packet_max = self.settings.headerGetInt("/server/@max_queued_client_packets")
        if self.client_queued_packet_max <= 0: self.client_queued_packet_max = 500
        
        self.throttle = settings.headerGet('/server/@throttle') == 'yes'
        self.delays = settings.headerGetProperties("/server/delays")[0]
        
        refill = settings.headerGetProperties("/server/refill")
        self.refill = refill[0] if len(refill) > 0 else None
        self.db = None
        self.memcache = None
        self.cashier = None
        self.poker_auth = None
        self.timer = {}
        self.down = True
        self.shutdown_deferred = None
        self.resthost_serial = 0
        self.has_ladder = None
        self.monitor_plugins = [
            _import(path.content).handle_event
            for path in settings.header.xpathEval("/server/monitor")
        ]
        self.chat_filter = None
        self.remove_completed = settings.headerGetInt("/server/@remove_completed")
        self.getPage = client.getPage
        self.long_poll_timeout = settings.headerGetInt("/server/@long_poll_timeout")
        if self.long_poll_timeout <= 0: self.long_poll_timeout = 20
        #
        #
        self.temporary_users_cleanup = self.settings.headerGet("/server/@cleanup") == "yes" 
        self.temporary_users_pattern = '^'+settings.headerGet("/server/users/@temporary")+'$'
        self.temporary_serial_min = settings.headerGetInt("/server/users/@temporary_serial_min")
        self.temporary_serial_max = settings.headerGetInt("/server/users/@temporary_serial_max")

        #
        #badwords list
        chat_filter_filepath = settings.headerGet("/server/badwordschatfilter/@file")
        if chat_filter_filepath:
            self.setupChatFilter(chat_filter_filepath)
        #
        # tourney lock check
        self._lock_check_locked = False
        self._lock_check_running = None
        self._lock_check_break = None
        #
        # hand cache
        self.hand_cache = OrderedDict()

        self.timer_remove_player = {}

    def setupLadder(self):
        cursor = self.db.cursor()
        cursor.execute("SHOW TABLES LIKE 'rank'")
        self.has_ladder = cursor.rowcount == 1
        cursor.close()
        return self.has_ladder

    def getLadder(self, game_id, currency_serial, user_serial):
        cursor = self.db.cursor()
        cursor.execute("SELECT rank,percentile FROM rank WHERE currency_serial = %s AND user_serial = %s", ( currency_serial, user_serial ))
        if cursor.rowcount == 1:
            row = cursor.fetchone()
            packet = PacketPokerPlayerStats(
                currency_serial = currency_serial,
                serial = user_serial,
                rank = row[0],
                percentile = row[1]
            )
        else:
            packet = PacketPokerError(
                serial = user_serial,
                other_type = PACKET_POKER_PLAYER_STATS,
                code = PacketPokerPlayerStats.NOT_FOUND,
                message = "no ladder entry for player %d and currency %d" % ( user_serial, currency_serial )
            )
        if game_id:
            packet.game_id = game_id
        else:
            packet.game_id = 0
        cursor.close()
        return packet
        
    def setupTourneySelectInfo(self):
        #
        # load module that provides additional tourney information
        #
        self.tourney_select_info = None
        settings = self.settings
        for path in settings.header.xpathEval("/server/tourney_select_info"):
            self.log.inform("trying to load '%s'", path.content)
            module = _import(path.content)
            path = settings.headerGet("/server/tourney_select_info/@settings")
            if path:
                s = pokernetworkconfig.Config(settings.dirs)
                s.load(path)
            else:
                s = None
            self.tourney_select_info = module.Handle(self, s)
            getattr(self.tourney_select_info, '__call__')

    def setupChatFilter(self,chat_filter_filepath):
        regExp = "(%s)" % "|".join(i.strip() for i in open(chat_filter_filepath,'r'))
        self.chat_filter = re.compile(regExp,re.IGNORECASE)
        
    def startService(self):
        self.monitors = []
        self.db = PokerDatabase(self.settings)
        memcache_address = self.settings.headerGet("/server/@memcached")
        if memcache_address:
            self.memcache = pokermemcache.memcache.Client([memcache_address])
            pokermemcache.checkMemcacheServers(self.memcache)
        else:
            self.memcache = pokermemcache.MemcacheMockup.Client([])
        self.setupTourneySelectInfo()
        self.setupLadder()
        self.setupResthost()
        self.cleanupCrashedTables()
        
        if self.temporary_users_cleanup: self.cleanUpTemporaryUsers()
        
        self.cashier = pokercashier.PokerCashier(self.settings)
        self.cashier.setDb(self.db)
        self.poker_auth = get_auth_instance(self.db, self.memcache, self.settings)
        self.dirs = self.settings.headerGet("/server/path").split()
        self.avatar_collection = PokerAvatarCollection("service")
        self.avatars = []
        self.tables = {}
        self.joined_count = 0
        self.tourney_table_serial = 1
        self.shutting_down = False
        self.simultaneous = self.settings.headerGetInt("/server/@simultaneous")
        self._ping_delay = self.settings.headerGetInt("/server/@ping")
        self.chat = self.settings.headerGet("/server/@chat") == "yes"

        # gettextFuncs is a dict that is indexed by full locale strings,
        # such as fr_FR.UTF-8, and returns a translation function.  If you
        # wanted to apply it directly, you'd do something like:
        # but usually, you will do something like this to change your locale on the fly:
        #   global _
        #   _ = self.gettextFuncs{'fr_FR.UTF-8'}
        #   _("Hello!  I am speaking in French now.")
        #   _ = self.gettextFuncs{'en_US.UTF-8'}
        #   _("Hello!  I am speaking in US-English now.")

        self.gettextFuncs = {}
        langsSupported = self.settings.headerGetProperties("/server/language")
        if (len(langsSupported) > 0):
            # Note, after calling _lookupTranslationFunc() a bunch of
            # times, we must restore the *actual* locale being used by the
            # server itself for strings locally on its machine.  That's
            # why we save it here.
            localLocale = locale.getlocale(locale.LC_ALL)

            for lang in langsSupported:
                self.gettextFuncs[lang['value']] = self._lookupTranslationFunc(lang['value'])
            try:
                locale.setlocale(locale.LC_ALL, localLocale)
            except locale.Error, le:
                self.log.error('Unable to restore original locale: %s', le)

        for description in self.settings.headerGetProperties("/server/table"):
            self.createTable(0, description)
        self.cleanupTourneys()
        self.updateTourneysSchedule()
        self.messageCheck()
        self.poker_auth.SetLevel(PACKET_POKER_SEAT, User.REGULAR)
        self.poker_auth.SetLevel(PACKET_POKER_GET_USER_INFO, User.REGULAR)
        self.poker_auth.SetLevel(PACKET_POKER_GET_PERSONAL_INFO, User.REGULAR)
        self.poker_auth.SetLevel(PACKET_POKER_PLAYER_INFO, User.REGULAR)
        self.poker_auth.SetLevel(PACKET_POKER_TOURNEY_REGISTER, User.REGULAR)
        self.poker_auth.SetLevel(PACKET_POKER_HAND_SELECT_ALL, User.ADMIN)
        self.poker_auth.SetLevel(PACKET_POKER_CREATE_TOURNEY, User.ADMIN)
        self.poker_auth.SetLevel(PACKET_POKER_TABLE, User.ADMIN)
        service.Service.startService(self)
        self.down = False

        # Setup Lock Check
        self._lock_check_running = LockChecks(5 * 60 * 60, self._warnLock)
        player_timeout = max(t.playerTimeout for t in self.tables.itervalues()) if self.tables else 20
        max_players = max(t.game.max_players for t in self.tables.itervalues()) if self.tables else 9
        len_rounds = (max(len(t.game.round_info) for t in self.tables.itervalues()) + 3) if self.tables else 8
        self._lock_check_break = LockChecks(
            player_timeout * max_players * len_rounds,
            self._warnLock
        )

    def stopServiceFinish(self):
        self.monitors = []
        if self.cashier: self.cashier.close()
        if self.db:
            self.cleanupCrashedTables()
            self.abortRunningTourneys()
            if self.resthost_serial: self.cleanupResthost()
            self.db.close()
            self.db = None
        if self.poker_auth: self.poker_auth.db = None
        service.Service.stopService(self)

    def disconnectAll(self):
        reactor.disconnectAll()

    def stopService(self):
        deferred = self.shutdown()
        deferred.addCallback(lambda x: self.disconnectAll())
        deferred.addCallback(lambda x: self.stopServiceFinish())
        return deferred

    def cancelTimer(self, key):
        if key in self.timer:
            self.log.debug("cancelTimer %s", key)
            timer = self.timer[key]
            if timer.active():
                timer.cancel()
            del self.timer[key]

    def cancelTimers(self, what):
        for key in self.timer.keys():
            if what in key:
                self.cancelTimer(key)

    def joinedCountReachedMax(self):
        """Returns True iff. the number of joins to tables has exceeded
        the maximum allowed by the server configuration"""
        return self.joined_count >= self.joined_max

    def joinedCountIncrease(self, num = 1):
        """Increases the number of currently joins to tables by num, which
        defaults to 1."""
        self.joined_count += num
        return self.joined_count

    def joinedCountDecrease(self, num = 1):
        """Decreases the number of currently joins to tables by num, which
        defaults to 1."""
        self.joined_count -= num
        return self.joined_count

    def getMissedRoundMax(self):
        return self.missed_round_max

    def getClientQueuedPacketMax(self):
        return self.client_queued_packet_max

    def _separateCodesetFromLocale(self, lang_with_codeset):
        lang = lang_with_codeset
        codeset = ""
        dotLoc = lang.find('.')
        if dotLoc > 0:
            lang = lang_with_codeset[:dotLoc]
            codeset = lang_with_codeset[dotLoc+1:]

        if len(codeset) <= 0:
            self.log.error('Unable to find codeset string in language value: %s', lang_with_codeset)
        if len(lang) <= 0:
            self.log.error('Unable to find locale string in language value: %s', lang_with_codeset)
        return (lang, codeset)

    def _lookupTranslationFunc(self, lang_with_codeset):
        # Start by defaulting to just returning the string...
        myGetTextFunc = lambda text:text

        (lang, codeset) = self._separateCodesetFromLocale(lang_with_codeset)

# I now believe that changing the locale in this way for each language is
# completely uneeded given the set of features we are looking for.
# Ultimately, we aren't currently doing localization operations other than
# gettext() string lookup, so the need to actually switch locales does not
# exist.  Long term, we may want to format numbers properly for remote
# users, and then we'll need more involved locale changes, probably
# handled by avatar and stored in the server object.  In the meantime,
# this can be commented out and makes testing easier.  --bkuhn, 2008-11-28

#         try:
#             locale.setlocale(locale.LC_ALL, lang)
#         except locale.Error, le:
#             self.error('Unable to support locale, "%s", due to locale error: %s'
#                        % (lang_with_codeset, le))
#             return myGetTextFunc

        outputStr = "Aces"
        try:
            # I am not completely sure poker-engine should be hardcoded here like this...
            transObj = gettext.translation('poker-engine', 
                                                languages=[lang], codeset=codeset)
            transObj.install()
            myGetTextFunc = transObj.gettext
            # This test call of the function *must* be a string in the
            # poker-engine domain.  The idea is to force a throw of
            # LookupError, which will be thrown if the codeset doesn't
            # exist.  Unfortunately, gettext doesn't throw it until you
            # call it with a string that it can translate (gibberish
            # doesn't work!).  We want to fail to support this
            # language/encoding pair here so the server can send the error
            # early and still support clients with this codec, albeit by
            # sending untranslated strings.
            outputStr = myGetTextFunc("Aces")
        except IOError, e:
            self.log.error("No translation for language %s for %s in "
                "poker-engine; locale ignored: %s",
                lang,
                lang_with_codeset,
                e
            )
            myGetTextFunc = lambda text:text
        except LookupError, l:
            self.log.error("Unsupported codeset %s for %s in poker-engine; locale ignored: %s",
                codeset,
                lang_with_codeset,
                l
            )
            myGetTextFunc = lambda text:text

        if outputStr == "Aces" and lang[0:2] != "en":
            self.log.error("Translation setup for %s failed.  Strings for clients "
                "requesting %s will likely always be in English",
                lang_with_codeset,
                lang
            )
        return myGetTextFunc

    def locale2translationFunc(self, locale, codeset = ""):
        if len(codeset) > 0:
            locale += "." + codeset
        if locale in self.gettextFuncs:
            return self.gettextFuncs[locale]
        else:
            self.log.warn("Locale, '%s' not available. %s must not have been "
                "provide via <language/> tag in settings, or errors occured during loading.",
                locale,
                locale
            )
            return None

    def shutdownLockChecks(self):
        if self._lock_check_break:
            self._lock_check_break.stopall()
        if self._lock_check_running:
            self._lock_check_running.stopall()
        
    def shutdownGames(self):
        #
        # happens when the service is not started and to accomodate tests 
        if not hasattr(self, "tables"):
            return
        
        tables = (t for t in self.tables.itervalues() if not t.game.isEndOrNull())
        for table in tables:
            table.broadcast(PacketPokerStateInformation(
                game_id = table.game.id,
                code = PacketPokerStateInformation.SHUTTING_DOWN,
                message = "shutting down"
            ))
            for serial,avatars in table.avatar_collection.serial2avatars.items():
                for avatar in avatars:
                    #
                    # if the avatar uses a non-persistent connection, disconnect
                    # it, since it is impossible establish new connections while
                    # shutting down
                    if avatar._queue_packets:
                        table.quitPlayer(avatar,serial)
                        
    
    def shutdown(self):
        self.shutting_down = True
        self.cancelTimer('checkTourney')
        self.cancelTimer('updateTourney')
        self.cancelTimer('messages')
        self.cancelTimers('tourney_breaks')
        self.cancelTimers('tourney_delete_route')

        if self.resthost_serial: self.setResthostOnShuttingDown()
        self.shutdownGames()
        self.shutdown_deferred = defer.Deferred()
        self.shutdown_deferred.addCallback(lambda res: self.shutdownLockChecks())
        reactor.callLater(0.01, self.shutdownCheck)
        return self.shutdown_deferred

    def shutdownCheck(self):
        if self.down:
            if self.shutdown_deferred:
                self.shutdown_deferred.callback(True)
            return

        playing = sum(1 for table in self.tables.itervalues() if not table.game.isEndOrNull())
        if playing > 0:
            self.log.warn('Shutting down, waiting for %d games to finish', playing)
        if playing <= 0:
            self.log.warn("Shutdown immediately")
            self.down = True
            self.shutdown_deferred.callback(True)
            self.shutdown_deferred = False
        else:
            reactor.callLater(2.0, self.shutdownCheck)

    def isShuttingDown(self):
        return self.shutting_down

    def stopFactory(self):
        pass

    def monitor(self, avatar):
        if avatar not in self.monitors:
            self.monitors.append(avatar)
        return PacketAck()

    def databaseEvent(self, **kwargs):
        event = PacketPokerMonitorEvent(**kwargs)
        for avatar in self.monitors:
            if hasattr(avatar, "protocol") and avatar.protocol:
                avatar.sendPacketVerbose(event)
        for plugin in self.monitor_plugins:
            plugin(self, event)

    def stats(self, query):
        cursor = self.db.cursor()
        cursor.execute("SELECT MAX(serial) FROM hands")
        (hands,) = cursor.fetchone()
        cursor.close()
        return PacketPokerStats(
            players = len(self.avatars),
            hands = 0 if hands is None else int(hands),
            bytesin = UGAMEProtocol._stats_read,
            bytesout = UGAMEProtocol._stats_write,
        )

    def createAvatar(self):
        avatar = pokeravatar.PokerAvatar(self)
        self.avatars.append(avatar)
        return avatar

    def forceAvatarDestroy(self, avatar):
#        self.destroyAvatar(avatar)
        reactor.callLater(0.1, self.destroyAvatar, avatar)

    def destroyAvatar(self, avatar):
        if avatar in self.avatars:
            self.avatars.remove(avatar)
        # if serial is 0 this avatar is already obsolete and may have been 
        # already removed from self.avatars in a distributed scenario
        elif avatar.getSerial() != 0: 
            self.log.warn("avatar %s is not in the list of known avatars", avatar)
        if avatar in self.monitors:
            self.monitors.remove(avatar)
        avatar.connectionLost("disconnected")

    def sessionStart(self, serial, ip):
        self.log.debug("sessionStart(%d, %s)", serial, ip)
        cursor = self.db.cursor()
        sql = "REPLACE INTO session ( user_serial, started, ip ) VALUES ( %d, %d, '%s')" % ( serial, seconds(), ip )
        cursor.execute(sql)
        if not (1 <= cursor.rowcount <= 2):
            self.log.error("modified %d rows (expected 1 or 2): %s", cursor.rowcount, cursor._executed)
        cursor.close()
        return True

    def sessionEnd(self, serial):
        self.log.debug("sessionEnd(%d)", serial)
        cursor = self.db.cursor()
        sql = "INSERT INTO session_history ( user_serial, started, ended, ip ) SELECT user_serial, started, %d, ip FROM session WHERE user_serial = %d" % ( seconds(), serial )
        cursor.execute(sql)
        if cursor.rowcount != 1:
            self.log.error("a) modified %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
        sql = "DELETE FROM session where user_serial = %d" % serial
        cursor.execute(sql)
        if cursor.rowcount != 1:
            self.log.error("b) modified %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
        cursor.close()
        return True

    def auth(self, auth_type, auth_args, roles):
        ( info, reason ) = self.poker_auth.auth(auth_type,auth_args)
        if info:
            self.autorefill(info[0])
        return ( info, reason )

    def autorefill(self, serial):
        if not self.refill:
            return
        user_info = self.getUserInfo(serial)
        if int(self.refill['serial']) in user_info.money:
            money = user_info.money[int(self.refill['serial'])]
            missing = int(self.refill['amount']) - ( int(money[0]) + int(money[1]) )
            if missing > 0:
                refill = int(money[0]) + missing
            else:
                refill = 0
        else:
            refill = int(self.refill['amount'])
        if refill > 0:
            self.db.db.query("REPLACE INTO user2money (user_serial, currency_serial, amount) values (%d, %s, %s)" % ( serial, self.refill['serial'], refill))
            self.databaseEvent(event = PacketPokerMonitorEvent.REFILL, param1 = serial, param2 = int(self.refill['serial']), param3 = refill)

        return refill

    def updateTourneysSchedule(self):
        self.log.debug("updateTourneysSchedule. (%s)" % self.resthost_serial)
        cursor = self.db.cursor(DictCursor)
        try:
            sql = \
                "SELECT * FROM tourneys_schedule " \
                "WHERE resthost_serial = %s " \
                "AND active = 'y' " \
                "AND (respawn = 'y' OR register_time < %s)"
            params = (self.resthost_serial,seconds())
            cursor.execute(sql,params)
            self.tourneys_schedule = dict((schedule['serial'],schedule) for schedule in cursor.fetchall())
            self.checkTourneysSchedule()
            self.cancelTimer('updateTourney')
            self.timer['updateTourney'] = reactor.callLater(UPDATE_TOURNEYS_SCHEDULE_DELAY, self.updateTourneysSchedule)
        finally:
            cursor.close()

    def checkTourneysSchedule(self):
        self.log.debug("checkTourneysSchedule")
        now = seconds()
        #
        # Cancel sng that stayed in registering state for too long
        #
        for tourney in filter(lambda tourney: tourney.sit_n_go == 'y', self.tourneys.values()):
            if tourney.state == TOURNAMENT_STATE_REGISTERING and now - tourney.register_time > self.sng_timeout:
                tourney.changeState(TOURNAMENT_STATE_CANCELED)
        #
        # Respawning sit'n'go tournaments
        #
        for schedule in filter(lambda schedule: schedule['respawn'] == 'y' and schedule['sit_n_go'] == 'y', self.tourneys_schedule.values()):
            schedule_serial = schedule['serial']
            if (
                schedule_serial not in self.schedule2tourneys or
                not filter(lambda tourney: tourney.state == TOURNAMENT_STATE_REGISTERING, self.schedule2tourneys[schedule_serial])
            ):
                self.spawnTourney(schedule)
        #
        # One time tournaments
        #
        one_time = []
        for schedule in filter(lambda schedule: schedule['respawn'] == 'n' and int(schedule['register_time']) < now,self.tourneys_schedule.values()):
            one_time.append(schedule)
            del self.tourneys_schedule[schedule['serial']]
        for schedule in one_time:
            self.spawnTourney(schedule)
        
        #
        # Respawning regular tournaments
        #
        for schedule in filter(
            lambda schedule: schedule['respawn'] == 'y' and int(schedule['respawn_interval']) > 0 and schedule['sit_n_go'] == 'n',
            self.tourneys_schedule.values()
        ):
            schedule_serial = schedule['serial']
            schedule = schedule.copy()
            if schedule['start_time'] < now:
                start_time = int(schedule['start_time'])
                respawn_interval = int(schedule['respawn_interval'])
                intervals = max(0, int(1+(now-start_time)/respawn_interval))
                schedule['start_time'] += schedule['respawn_interval']*intervals
                schedule['register_time'] += schedule['respawn_interval']*intervals
            if schedule['register_time'] < now and (
                schedule_serial not in self.schedule2tourneys or
                not filter(
                    lambda tourney: tourney.start_time >= schedule['start_time'] 
                    ,self.schedule2tourneys[schedule_serial]
                )
            ):
                self.spawnTourney(schedule)
            
        #
        # Update tournaments with time clock
        #
        for tourney in filter(lambda tourney: tourney.sit_n_go == 'n', self.tourneys.values()):
            tourney.updateRunning()
        #
        # Forget about old tournaments
        #
        for tourney in filter(lambda tourney: tourney.state in ( TOURNAMENT_STATE_COMPLETE,  TOURNAMENT_STATE_CANCELED ), self.tourneys.values()):
            if now - tourney.finish_time > DELETE_OLD_TOURNEYS_DELAY:
                self.deleteTourney(tourney)
                self.tourneyDeleteRoute(tourney)

        self.cancelTimer('checkTourney')
        self.timer['checkTourney'] = reactor.callLater(CHECK_TOURNEYS_SCHEDULE_DELAY, self.checkTourneysSchedule)

    def today(self):
        return date.today()
    
    def spawnTourney(self, schedule):
        cursor = self.db.cursor()
        try:
            #
            # buy-in currency
            #
            currency_serial = schedule['currency_serial']
            currency_serial_from_date_format = schedule['currency_serial_from_date_format']
            if currency_serial_from_date_format:
                if not self._spawnTourney_currency_from_date_format_re.match(currency_serial_from_date_format):
                    raise UserWarning, "tourney_schedule.currency_serial_from_date_format format string %s does not match %s" % ( currency_serial_from_date_format, self._spawnTourney_currency_from_date_format_re.pattern )
                currency_serial = long(self.today().strftime(currency_serial_from_date_format))
            #
            # prize pool currency
            #
            prize_currency = schedule['prize_currency']
            prize_currency_from_date_format = schedule['prize_currency_from_date_format']
            if prize_currency_from_date_format:
                if not self._spawnTourney_currency_from_date_format_re.match(prize_currency_from_date_format):
                    raise UserWarning, "tourney_schedule.prize_currency_from_date_format format string %s does not match %s" % ( prize_currency_from_date_format, self._spawnTourney_currency_from_date_format_re.pattern )
                prize_currency = long(self.today().strftime(prize_currency_from_date_format))
            cursor.execute("INSERT INTO tourneys SET " + ", ".join("%s = %s" % (key, self.db.literal(val)) for key, val in {
                'resthost_serial': schedule['resthost_serial'],
                'schedule_serial': schedule['serial'],
                'name': schedule['name'],
                'description_short': schedule['description_short'],
                'description_long': schedule['description_long'],
                'players_quota': schedule['players_quota'],
                'players_min': schedule['players_min'],
                'variant': schedule['variant'],
                'betting_structure': schedule['betting_structure'],
                'skin': schedule['skin'],
                'seats_per_game': schedule['seats_per_game'],
                'player_timeout': schedule['player_timeout'],
                'currency_serial': currency_serial,
                'prize_currency': prize_currency,
                'prize_min': schedule['prize_min'],
                'bailor_serial': schedule['bailor_serial'],
                'buy_in': schedule['buy_in'],
                'rake': schedule['rake'],
                'sit_n_go': schedule['sit_n_go'],
                'breaks_first': schedule['breaks_first'],
                'breaks_interval': schedule['breaks_interval'],
                'breaks_duration': schedule['breaks_duration'],
                'rebuy_delay': schedule['rebuy_delay'],
                'add_on': schedule['add_on'],
                'add_on_delay': schedule['add_on_delay'],
                'start_time': schedule['start_time'],
                'via_satellite': schedule['via_satellite'],
                'satellite_of': schedule['satellite_of'],
                'satellite_player_count': schedule['satellite_player_count']
            }.iteritems()))
            self.log.debug("spawnTourney: %s", schedule)
            #
            # Accomodate with MySQLdb versions < 1.1
            #
            tourney_serial = cursor.lastrowid
            if schedule['respawn'] == 'n':
                cursor.execute("UPDATE tourneys_schedule SET active = 'n' WHERE serial = %s", (int(schedule['serial']),))
            cursor.execute("REPLACE INTO route VALUES (0,%s,%s,%s)", ( tourney_serial, int(seconds()), self.resthost_serial))
            self.spawnTourneyInCore(schedule, tourney_serial, schedule['serial'], currency_serial, prize_currency)
        finally:
            cursor.close()

    def spawnTourneyInCore(self, tourney_map, tourney_serial, schedule_serial, currency_serial, prize_currency):
        tourney_map['start_time'] = int(tourney_map['start_time'])
        if tourney_map['sit_n_go'] == 'y':
            tourney_map['register_time'] = int(seconds()) - 1
        else:
            tourney_map['register_time'] = int(tourney_map.get('register_time', 0))
        tourney = PokerTournament(dirs = self.dirs, **tourney_map)
        tourney.serial = tourney_serial
        tourney.schedule_serial = schedule_serial
        tourney.currency_serial = currency_serial
        tourney.prize_currency = prize_currency
        tourney.bailor_serial = tourney_map['bailor_serial']
        tourney.player_timeout = int(tourney_map['player_timeout'])
        tourney.via_satellite = int(tourney_map['via_satellite'])
        tourney.satellite_of = int(tourney_map['satellite_of'])
        tourney.satellite_of = self.tourneySatelliteLookup(tourney)[0]
        tourney.satellite_player_count = int(tourney_map['satellite_player_count'])
        tourney.satellite_registrations = []
        tourney.callback_new_state = self.tourneyNewState
        tourney.callback_create_game = self.tourneyCreateTable
        tourney.callback_game_filled = self.tourneyGameFilled
        tourney.callback_destroy_game = self.tourneyDestroyGame
        tourney.callback_move_player = self.tourneyMovePlayer
        tourney.callback_remove_player = self.tourneyRemovePlayerLater
        tourney.callback_cancel = self.tourneyCancel
        tourney.callback_reenter_game = self.tourneyReenterGame
        if schedule_serial not in self.schedule2tourneys:
            self.schedule2tourneys[schedule_serial] = []
        self.schedule2tourneys[schedule_serial].append(tourney)
        self.tourneys[tourney.serial] = tourney
        return tourney

    def deleteTourney(self, tourney):
        self.log.debug("deleteTourney: %d", tourney.serial)
        self.schedule2tourneys[tourney.schedule_serial].remove(tourney)
        if len(self.schedule2tourneys[tourney.schedule_serial]) <= 0:
            del self.schedule2tourneys[tourney.schedule_serial]
        del self.tourneys[tourney.serial]

    def tourneyResumeAndDeal(self, tourney):
        self.tourneyBreakResume(tourney)
        self.tourneyDeal(tourney)

    def _warnLock(self, tourney_serial):
        self._lock_check_locked = True
        self.log.warn("Tournament is locked! tourney_serial: %s", tourney_serial)

    def isLocked(self):
        return self._lock_check_locked

    def tourneyNewState(self, tourney, old_state, new_state):
        # Lock Check
        if self._lock_check_running:
            if new_state == TOURNAMENT_STATE_RUNNING:
                self._lock_check_running.start(tourney.serial)
            elif new_state == TOURNAMENT_STATE_COMPLETE:
                self._lock_check_running.stop(tourney.serial)
        if self._lock_check_break:
            if new_state == TOURNAMENT_STATE_BREAK_WAIT:
                self._lock_check_break.start(tourney.serial)
            elif new_state == TOURNAMENT_STATE_BREAK:
                self._lock_check_break.stop(tourney.serial)

        cursor = self.db.cursor()
        updates = []

        updates.append("state = %s" % self.db.literal(new_state))
        if old_state != TOURNAMENT_STATE_BREAK and new_state == TOURNAMENT_STATE_RUNNING:
            updates.append("start_time = %s" % self.db.literal(tourney.start_time))

        sql = "UPDATE tourneys SET %s WHERE serial = %s" % (
            ", ".join(updates),
            self.db.literal(tourney.serial)
        )
        self.log.debug("tourneyNewState: %s", sql)
        cursor.execute(sql)
        if cursor.rowcount != 1:
            self.log.error("modified %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
        cursor.close()
        if new_state == TOURNAMENT_STATE_BREAK:
            # When we are entering BREAK state for the first time, which
            # should only occur here in the state change operation, we
            # send the PacketPokerTableTourneyBreakBegin.  Note that this
            # code is here and not in tourneyBreakCheck() because that
            # function is called over and over again, until the break
            # finishes.  Note that tourneyBreakCheck() also sends a
            # PacketPokerGameMessage() with the time remaining, too.
            secsLeft = tourney.remainingBreakSeconds()
            if secsLeft == None:
                # eek, should I really be digging down into tourney's
                # member variables in this next assignment?
                secsLeft = tourney.breaks_duration
            resumeTime = seconds() + secsLeft
            for gameId in map(lambda game: game.id, tourney.games):
                table = self.getTable(gameId)
                table.broadcast(PacketPokerTableTourneyBreakBegin(game_id = gameId, resume_time = resumeTime))
            self.tourneyBreakCheck(tourney)
        elif old_state == TOURNAMENT_STATE_BREAK and new_state == TOURNAMENT_STATE_RUNNING:
            wait = int(self.delays.get('extra_wait_tourney_break', 0))
            if wait > 0:
                reactor.callLater(wait, self.tourneyResumeAndDeal, tourney)
            else:
                self.tourneyResumeAndDeal(tourney)
        elif old_state == TOURNAMENT_STATE_REGISTERING and new_state == TOURNAMENT_STATE_RUNNING:
            self.databaseEvent(event = PacketPokerMonitorEvent.TOURNEY_START, param1 = tourney.serial)            
            reactor.callLater(0.01, self.tourneyBroadcastStart, tourney.serial)
            #
            # Only obey extra_wait_tourney_start if we had been registering and are now running,
            # since we only want this behavior before the first deal.
            wait_type = 'tourney' if tourney.sit_n_go != 'y' else 'sng'
            wait = int(self.delays.get('extra_wait_%s_start' % wait_type, 0))
            wait_msg_interval = 20
            if wait > 0:
                for remaining in range(wait-int(wait_msg_interval/2),0,-wait_msg_interval):
                    reactor.callLater(remaining,self.tourneyStartingMessage,tourney,wait-remaining)
                reactor.callLater(wait, self.tourneyDeal, tourney)
            else:
                self.tourneyDeal(tourney)
        elif new_state == TOURNAMENT_STATE_RUNNING:
            self.tourneyDeal(tourney)
        elif new_state == TOURNAMENT_STATE_BREAK_WAIT:
            self.tourneyBreakWait(tourney)
    
    def tourneyStartingMessage(self,tourney,remaining):
        for game_id in tourney.id2game.keys():
            table = self.getTable(game_id)
            table.broadcastMessage(PacketPokerGameMessage, "Waiting for players.\nNext hand will be dealt shortly.\n(maximum %d seconds)" % remaining)
            
    def tourneyBreakCheck(self, tourney):
        key = 'tourney_breaks_%d' % id(tourney)
        self.cancelTimer(key)
        tourney.updateBreak()
        if tourney.state == TOURNAMENT_STATE_BREAK:
            self.timer[key] = reactor.callLater(int(self.delays.get('breaks_check', 30)), self.tourneyBreakCheck, tourney)

    def tourneyDeal(self, tourney):
        for game_id in tourney.id2game.keys():
            table = self.getTable(game_id)
            table.autodeal = self.getTableAutoDeal()
            table.scheduleAutoDeal()

    def tourneyBreakWait(self, tourney):
        for game_id in map(lambda game: game.id, tourney.games):
            table = self.getTable(game_id)
            if table.game.isRunning():
                table.broadcastMessage(PacketPokerGameMessage, "Tournament break at the end of the hand")
            else:
                table.broadcastMessage(PacketPokerGameMessage, "Tournament break will start when the other tables finish their hand")

    def tourneyBreakResume(self, tourney):
        for gameId in map(lambda game: game.id, tourney.games):
            table = self.getTable(gameId)
            table.broadcast(PacketPokerTableTourneyBreakDone(game_id = gameId))

    def tourneyEndTurn(self, tourney, game_id):
        tourney.endTurn(game_id)
        self.tourneyFinishHandler(tourney, game_id)

    def tourneyUpdateStats(self,tourney,game_id):
        tourney.stats.update(game_id)

    def tourneyFinishHandler(self, tourney, game_id):
        if not tourney.tourneyEnd(game_id):
            self.tourneyFinished(tourney)
            self.tourneySatelliteWaitingList(tourney)

    def tourneyFinished(self, tourney):
        prizes = tourney.prizes()
        winners = tourney.winners[:len(prizes)]
        cursor = self.db.cursor()
        #
        # If prize_currency is non zero, use it instead of currency_serial
        #
        if tourney.prize_currency > 0:
            prize_currency = tourney.prize_currency
        else:
            prize_currency = tourney.currency_serial
        #
        # Guaranteed prize pool is withdrawn from a given account if and only if
        # the buy in of the players is not enough.
        #
        bail = tourney.prize_min - ( tourney.buy_in * tourney.registered )
        if bail > 0 and tourney.bailor_serial > 0:
            sql = "UPDATE user2money SET amount = amount - %s WHERE user_serial = %s AND currency_serial = %s AND amount >= %s"
            params = (bail,tourney.bailor_serial,prize_currency,bail)
            cursor.execute(sql,params)
            self.log.debug("tourneyFinished: bailor pays %s", cursor._executed)
            if cursor.rowcount != 1:
                self.log.error("tourneyFinished: bailor failed to provide "
                    "requested money modified %d rows (expected 1): %s",
                    cursor.rowcount,
                    cursor._executed
                )
                cursor.close()
                return False

        while prizes:
            prize = prizes.pop(0)
            serial = winners.pop(0)
            if prize <= 0:
                continue
            sql = "UPDATE user2money SET amount = amount + %s WHERE user_serial = %s AND currency_serial = %s"
            params = (prize,serial,prize_currency)
            cursor.execute(sql,params)
            self.log.debug("tourneyFinished: %s", cursor._executed)
            if cursor.rowcount == 0:
                sql = "INSERT INTO user2money (user_serial, currency_serial, amount) VALUES (%s, %s, %s)"
                params = (serial, prize_currency, prize)
                cursor.execute(sql,params)
                self.log.debug("tourneyFinished: %s", cursor._executed)
            self.databaseEvent(event = PacketPokerMonitorEvent.PRIZE, param1 = serial, param2 = prize_currency, param3 = prize)

        #added the following so that it wont break tests where the tournament mockup doesn't contain a finish_time
        if not hasattr(tourney, "finish_time"):
            tourney.finish_time = seconds()
        cursor.execute("UPDATE tourneys SET finish_time = %s WHERE serial = %s", (tourney.finish_time, int(tourney.serial)))
        cursor.close()
        self.databaseEvent(event = PacketPokerMonitorEvent.TOURNEY, param1 = tourney.serial)
        self.tourneyDeleteRoute(tourney)
        return True

    def tourneyDeleteRoute(self, tourney):
        key = 'tourney_delete_route_%d' % tourney.serial
        if key in self.timer: return
        wait = int(self.delays.get('extra_wait_tourney_finish', 0))
        def doTourneyDeleteRoute():
            self.cancelTimer(key)
            for serial in tourney.players:
                for player in self.avatar_collection.get(serial):
                    if tourney.serial in player.tourneys:
                        player.tourneys.remove(tourney.serial)
            self.tourneyDeleteRouteActual(tourney.serial)
        self.timer[key] = reactor.callLater(max(self._ping_delay*2, wait*2), doTourneyDeleteRoute)
        
    def tourneyDeleteRouteActual(self, tourney_serial):
        cursor = self.db.cursor()
        cursor.execute("DELETE FROM route WHERE tourney_serial = %s", tourney_serial)
        cursor.close()
    
    def tourneyGameFilled(self, tourney, game):
        table = self.getTable(game.id)
        cursor = self.db.cursor()
        for player in game.playersAll():
            serial = player.serial
            player.setUserData(pokeravatar.DEFAULT_PLAYER_USER_DATA.copy())
            avatars = self.avatar_collection.get(serial)
            if avatars:
                self.log.debug("tourneyGameFilled: player %d connected", serial)
                table.avatar_collection.set(serial, avatars)
            else:
                self.log.debug("tourneyGameFilled: player %d disconnected", serial)
            self.seatPlayer(serial, game.id, game.buyIn())

            for avatar in avatars:
                # First, force a count increase, since this player will
                # now be at the table, but table.joinPlayer() was never
                # called (which is where the increase usually happens).
                self.joinedCountIncrease()
                avatar.join(table, reason = PacketPokerTable.REASON_TOURNEY_START)
            sql = "update user2tourney set table_serial = %d where user_serial = %d and tourney_serial = %d" % ( game.id, serial, tourney.serial )
            self.log.debug("tourneyGameFilled: %s", sql)
            cursor.execute(sql)
            if cursor.rowcount != 1:
                self.log.error("modified %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
        cursor.close()
        table.update()

    def tourneyCreateTable(self, tourney):
        table = self.createTable(0, {
            'name': "%s (%s)" % (
                tourney.name,
                str(self.tourney_table_serial)
            ),
            'variant': tourney.variant,
            'betting_structure': tourney.betting_structure,
            'skin': tourney.skin,
            'seats': tourney.seats_per_game,
            'currency_serial': 0,
            'player_timeout': tourney.player_timeout,
            'transient': True,
            'tourney': tourney
        })
        self.tourney_table_serial += 1
        table.timeout_policy = "fold"
        table.autodeal = False
        return table.game

    def tourneyDestroyGameActual(self, game):
        table = self.getTable(game.id)
        table.destroy()

    def tourneyDestroyGame(self, tourney, game):
        wait = int(self.delays.get('extra_wait_tourney_finish', 0))
        if wait > 0:
            reactor.callLater(wait, self.tourneyDestroyGameActual, game)
        else:
            self.tourneyDestroyGameActual(game)

    def tourneyMovePlayer(self, tourney, from_game_id, to_game_id, serial):
        cursor = self.db.cursor()
        try:
            from_table = self.getTable(from_game_id)
            from_table.movePlayer(
                from_table.avatar_collection.get(serial),
                serial,
                to_game_id,
                reason = PacketPokerTable.REASON_TOURNEY_MOVE
            )
            cursor.execute(
                "UPDATE user2tourney SET table_serial = %s " \
                "WHERE user_serial = %s " \
                "AND tourney_serial = %s",
                (to_game_id, serial, tourney.serial)
            )
            self.log.debug("tourneyMovePlayer: %s", cursor._executed)
            if cursor.rowcount != 1:
                self.log.error("modified %d row (expected 1): %s", cursor.rowcount, cursor._executed)
                return False
            return True
        finally:
            cursor.close()

    def tourneyReenterGame(self, tourney_serial, serial):
        self.log.debug('tourneyReenterGame tourney_serial(%d) serial(%d)', tourney_serial, serial)
        timeout_key = "%s_%s" % (tourney_serial,serial)
        timer = self.timer_remove_player[timeout_key]
        if timer.active(): timer.cancel()
        del self.timer_remove_player[timeout_key]

    def tourneyRemovePlayerLater(self, tourney, game_id, serial, now=False):
        table = self.getTable(game_id)
        avatars = self.avatar_collection.get(serial)
        timeout_key = "%s_%s" % (tourney.serial,serial)
        for avatar in avatars:
            table.sitOutPlayer(avatar, serial)
        if not now:
            if timeout_key not in self.timer_remove_player:
                delay = int(self.delays.get('tourney_kick', 20))
                self.timer_remove_player[timeout_key] = reactor.callLater(delay, self.tourneyRemovePlayer, tourney, serial)
        else:
            if timeout_key in self.timer_remove_player:
                if self.timer_remove_player[timeout_key].active():
                    self.timer_remove_player[timeout_key].cancel()
                del self.timer_remove_player[timeout_key]
            self.tourneyRemovePlayer(tourney, serial)


    def tourneyRemovePlayer(self, tourney, serial):
        self.log.debug('remove now tourney(%d) serial(%d)', tourney.serial, serial)
        # the following line causes an IndexError if the player is not in any game. this is a good thing. 
        table = [t for t in self.tables.itervalues() if t.tourney is tourney and serial in t.game.serial2player][0]
        table.kickPlayer(serial)
        tourney.finallyRemovePlayer(serial)
        
        cursor = self.db.cursor()
        try:
            prizes = tourney.prizes()
            rank = tourney.getRank(serial)
            players = len(tourney.players)
            money = 0
            if 0 <= rank-1 < len(prizes):
                money = prizes[rank-1]
            avatars = self.avatar_collection.get(serial)
            if avatars:
                packet = PacketPokerTourneyRank(
                    serial = tourney.serial,
                    game_id = table.game.id,
                    players = players,
                    rank = rank,
                    money = money
                )
                for avatar in avatars:
                    avatar.sendPacketVerbose(packet)
            cursor.execute(
                "UPDATE user2tourney " \
                "SET rank = %s, table_serial = -1 " \
                "WHERE user_serial = %s " \
                "AND tourney_serial = %s",
                (rank, serial, tourney.serial)
            )
            self.log.debug("tourneyRemovePlayer: %s", cursor._executed)
            if cursor.rowcount != 1:
                self.log.error("modified %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
            self.tourneySatelliteSelectPlayer(tourney, serial, rank)
        finally:
            cursor.close()

        self.tourneyEndTurn(tourney, table.game.id)
        tourney.balanceGames()

    def tourneySatelliteLookup(self, tourney):
        if tourney.satellite_of == 0:
            return (0, None)
        found = None
        for candidate in self.tourneys.values():
            if candidate.schedule_serial == tourney.satellite_of:
                found = candidate
                break
        if found:
            if found.state != TOURNAMENT_STATE_REGISTERING:
                self.log.error(
                   "tourney %d is a satellite of %d but %d is in state %s instead of the expected state %s", 
                   tourney.serial,
                   found.schedule_serial,
                   found.schedule_serial,
                   found.state,
                   TOURNAMENT_STATE_REGISTERING 
                )
                return (0, TOURNAMENT_STATE_REGISTERING)
            return (found.serial, None)
        else:
            return (0, False)
                
    def tourneySatelliteSelectPlayer(self, tourney, serial, rank):
        if tourney.satellite_of == 0:
            return False
        if rank <= tourney.satellite_player_count:
            packet = PacketPokerTourneyRegister(serial = serial, tourney_serial = tourney.satellite_of)
            if self.tourneyRegister(packet = packet, via_satellite = True):
                tourney.satellite_registrations.append(serial)
        return True

    def tourneySatelliteWaitingList(self, tourney):
        """If the satellite did not register enough players, presumably because of a registration error
         for some of the winners (for instance if they were already registered), register the remaining
         players with winners that are not in the top satellite_player_count."""
        if tourney.satellite_of == 0:
            return False
        registrations = tourney.satellite_player_count - len(tourney.satellite_registrations)
        if registrations <= 0:
            return False
        serials = (serial for serial in tourney.winners if serial not in tourney.satellite_registrations)
        for serial in serials:
            packet = PacketPokerTourneyRegister(serial = serial, tourney_serial = tourney.satellite_of)
            if self.tourneyRegister(packet = packet, via_satellite = True):
                tourney.satellite_registrations.append(serial)
                registrations -= 1
                if registrations <= 0:
                    break
        return True

    def tourneyCreate(self, packet):
        cursor = self.db.cursor()
        sql = \
            "INSERT INTO tourneys_schedule " \
            "(resthost_serial, name, description_short, description_long, players_quota, variant, betting_structure, skin, seats_per_game, player_timeout, currency_serial, prize_currency, prize_min, bailor_serial, buy_in, rake, sit_n_go, start_time)" \
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        params = (
            self.resthost_serial,
            packet.name,
            packet.description_short,
            packet.description_long,
            packet.players_quota if packet.players_quota > len(packet.players) else len(packet.players),
            packet.variant,
            packet.betting_structure,
            packet.skin,
            packet.seats_per_game,
            packet.player_timeout,
            packet.currency_serial,
            packet.prize_currency,
            packet.prize_min,
            packet.bailor_serial,
            packet.buy_in,
            packet.rake,
            packet.sit_n_go,
            packet.start_time
        )
        cursor.execute(sql,params)
        schedule_serial = cursor.lastrowid
        cursor.close()
        self.updateTourneysSchedule()
        #
        # There can be only one tourney for this tourney_schedule because they are not respawning 
        tourney = self.schedule2tourneys[schedule_serial][0]
        register_packet = PacketPokerTourneyRegister(tourney_serial = tourney.serial)
        serial_failed = []
        for serial in packet.players:
            register_packet.serial = serial
            if not self.tourneyRegister(register_packet):
                serial_failed.append(serial)
        if len(serial_failed) > 0:
            self.tourneyCancel(tourney)
            return PacketPokerError(
                game_id = schedule_serial,
                serial = tourney.serial,
                other_type = PACKET_POKER_CREATE_TOURNEY,
                code = PacketPokerCreateTourney.REGISTRATION_FAILED,
                message = "registration failed for players %s in tourney %d" % (serial_failed, tourney.serial)
            )
        else:
            return PacketPokerTourney(**tourney.__dict__)

    def tourneyBroadcastStart(self, tourney_serial):
        cursor = self.db.cursor()
        cursor.execute("SELECT host,port FROM resthost WHERE state = %s",(self.STATE_ONLINE))
        for host,port in cursor.fetchall():
            self.getPage('http://%s:%d/TOURNEY_START?tourney_serial=%d' % (host,long(port),tourney_serial))
        cursor.close()
        
    def tourneyNotifyStart(self, tourney_serial):
        manager = self.tourneyManager(tourney_serial)
        if manager.type != PACKET_POKER_TOURNEY_MANAGER:
            raise UserWarning, str(manager)
        user2properties = manager.user2properties
        
        calls = []
        def send(avatar_serial,table_serial):
            # get all avatars that are logged in and having an explain instance
            avatars = (a for a in self.avatar_collection.get(avatar_serial) if a.isLogged() and a.explain)
            for avatar in avatars:
                avatar.sendPacket(PacketPokerTourneyStart(tourney_serial=tourney_serial,table_serial=table_serial))
                
        for avatar_serial,properties in user2properties.iteritems():
            avatar_serial = long(avatar_serial)
            table_serial = properties['table_serial']
            calls.append(reactor.callLater(0.1,send,avatar_serial,table_serial))
            
        return calls
    
    def tourneyManager(self, tourney_serial):
        packet = PacketPokerTourneyManager()
        packet.tourney_serial = tourney_serial
        cursor = self.db.cursor(DictCursor)
        cursor.execute("SELECT user_serial, table_serial, rank FROM user2tourney WHERE tourney_serial = %d" % tourney_serial)
        user2tourney = cursor.fetchall()

        table2serials = {}
        for row in user2tourney:
            table_serial = row['table_serial']
            if table_serial == None or table_serial == -1:
                continue
            if table_serial not in table2serials:
                table2serials[table_serial] = []
            table2serials[table_serial].append(row['user_serial'])
        packet.table2serials = table2serials
        user2money = {}
        if len(table2serials) > 0:
            cursor.execute("SELECT user_serial, money FROM user2table WHERE table_serial IN ( " + ",".join(map(lambda x: str(x), table2serials.keys())) + " )")
            for row in cursor.fetchall():
                user2money[row['user_serial']] = row['money']

        cursor.execute("SELECT user_serial, name FROM user2tourney, users WHERE user2tourney.tourney_serial = " + str(tourney_serial) + " AND user2tourney.user_serial = users.serial")
        user2name = dict((entry["user_serial"], entry["name"]) for entry in cursor.fetchall())

        cursor.execute("SELECT * FROM tourneys WHERE serial = %s",(tourney_serial,));
        if cursor.rowcount > 1:
            # This would be a bizarre case; unlikely to happen, but worth
            # logging if it happens.
            self.log.error("tourneyManager: tourney_serial(%d) has more than one "
                "row in tourneys table, using first row returned",
                tourney_serial
            )
        elif cursor.rowcount <= 0:
            # More likely to happen, so don't log it unless some verbosity
            # is requested.
            self.log.debug("tourneyManager: tourney_serial(%d) requested not "
                "found in database, returning error packet",
                tourney_serial
            )
            # Construct and return an error packet at this point.  I
            # considered whether it made more sense to return "None"
            # here and have avatar construct the Error packet, but it
            # seems other methods in pokerservice also construct error
            # packets already, so it seemed somewhat fitting.
            return PacketError(
                other_type = PACKET_POKER_GET_TOURNEY_MANAGER,
                code = PacketPokerGetTourneyManager.DOES_NOT_EXIST,
                message = "Tournament %d does not exist" % tourney_serial
            )
        # Now we know we can proceed with taking the first row returned in
        # the cursor; there is at least one there.
        packet.tourney = cursor.fetchone()
        packet.tourney["registered"] = len(user2tourney)
        packet.tourney["rank2prize"] = None
        if tourney_serial in self.tourneys:
            packet.tourney["rank2prize"] = self.tourneys[tourney_serial].prizes()
        else:
            player_count = packet.tourney["players_quota"] \
                if packet.tourney["sit_n_go"] == 'y' \
                else packet.tourney["registered"]
            packet.tourney["rank2prize"] = pokerprizes.PokerPrizesTable(
                buy_in_amount = packet.tourney['buy_in'],
                guarantee_amount = packet.tourney['prize_min'],
                player_count = player_count,
                config_dirs = self.dirs
            ).getPrizes()
        cursor.close()

        user2properties = {}
        for row in user2tourney:
            user_serial = row["user_serial"]
            money = user_serial in user2money and user2money[user_serial] or -1
            user2properties[str(user_serial)] = {
                "name": user2name[user_serial],
                "money": money,
                "rank": row["rank"],
                "table_serial": row["table_serial"]
            }
        packet.user2properties = user2properties

        return packet

    def tourneyPlayersList(self, tourney_serial):
        if tourney_serial not in self.tourneys:
            return PacketError(
                other_type = PACKET_POKER_TOURNEY_REQUEST_PLAYERS_LIST,
                code = PacketPokerTourneyRegister.DOES_NOT_EXIST,
                message = "Tournament %d does not exist" % tourney_serial
            )
        tourney = self.tourneys[tourney_serial]
        players = [(self.getName(serial),-1,0) for serial in tourney.players]
        return PacketPokerTourneyPlayersList(tourney_serial = tourney_serial, players = players)

    def tourneyStats(self):
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) FROM tourneys WHERE state in ( %s, %s )", ( TOURNAMENT_STATE_RUNNING, TOURNAMENT_STATE_REGISTERING ))
        tourneys = int(cursor.fetchone()[0])
        cursor.execute("SELECT COUNT(*) FROM user2tourney WHERE rank = -1")
        players = int(cursor.fetchone()[0])
        cursor.close()
        return ( players, tourneys )
    
    def tourneyPlayerStats(self, tourney_serial, user_serial):
        tourney = self.tourneys.get(tourney_serial,None)
        if tourney is None:
            return PacketError(
                other_type = PACKET_POKER_GET_TOURNEY_PLAYER_STATS,
                code = PacketPokerGetTourneyPlayerStats.DOES_NOT_EXIST,
                message = "Tournament %d does not exist" % tourney_serial
            )
        elif user_serial not in tourney.players:
            return PacketError(
                other_type = PACKET_POKER_GET_TOURNEY_PLAYER_STATS,
                code = PacketPokerGetTourneyPlayerStats.NOT_PARTICIPATING,
                message = "User %d not participating in tourney %d." % (user_serial,tourney_serial)
            )
        stats = tourney.stats(user_serial)
        return PacketPokerTourneyPlayerStats(**stats)
    
    def tourneySelect(self, query_string):
        cursor = self.db.cursor(DictCursor)
        criterion = query_string.split("\t")
        tourney_sql = \
            "SELECT tourneys.*,COUNT(user2tourney.user_serial) AS registered FROM tourneys " \
            "LEFT JOIN user2tourney ON (tourneys.serial = user2tourney.tourney_serial) " \
            "WHERE (state != 'complete' OR (state = 'complete' AND finish_time > UNIX_TIMESTAMP(NOW() - INTERVAL %d HOUR))) " % self.remove_completed
        schedule_sql = "SELECT * FROM tourneys_schedule AS tourneys WHERE respawn = 'n' AND active = 'y'"
        sql = ''
        if len(criterion) > 1:
            ( currency_serial, tourney_type ) = criterion
            sit_n_go = 'y' if tourney_type == 'sit_n_go' else 'n'
            if currency_serial:
                sql += " AND tourneys.currency_serial = %s AND sit_n_go = '%s'" % (currency_serial, sit_n_go)
            else:
                sql += " AND sit_n_go = '%s'" % sit_n_go
        elif query_string != '':
            sql = " AND name = '%s'" % query_string
        tourney_sql += sql
        schedule_sql += sql
        tourney_sql += " GROUP BY tourneys.serial"
        cursor.execute(tourney_sql)
        result = cursor.fetchall()
        cursor.execute(schedule_sql)
        result += cursor.fetchall()
        cursor.close()
        return result

    def tourneySelectInfo(self, packet, tourneys):
        if self.tourney_select_info:
            return self.tourney_select_info(self, packet, tourneys)
        else:
            return None
    
    def tourneyRegister(self, packet, via_satellite=False):
        serial = packet.serial
        tourney_serial = packet.tourney_serial
        avatars = self.avatar_collection.get(serial)
        tourney = self.tourneys.get(tourney_serial,None)
        if tourney is None:
            error = PacketError(
                other_type = PACKET_POKER_TOURNEY_REGISTER,
                code = PacketPokerTourneyRegister.DOES_NOT_EXIST,
                message = "Tournament %d does not exist" % tourney_serial
            )
            if not via_satellite:
                self.log.error("%s", error)
            for avatar in avatars:
                avatar.sendPacketVerbose(error)
            return False
        
        if tourney.via_satellite and not via_satellite:
            error = PacketError(
                other_type = PACKET_POKER_TOURNEY_REGISTER,
                code = PacketPokerTourneyRegister.VIA_SATELLITE,
                message = "Player %d must register to %d via a satellite" % ( serial, tourney_serial ) 
            )
            self.log.error("%s", error)
            for avatar in avatars:
                avatar.sendPacketVerbose(error)
            return False
            
        if tourney.isRegistered(serial):
            error = PacketError(
                other_type = PACKET_POKER_TOURNEY_REGISTER,
                code = PacketPokerTourneyRegister.ALREADY_REGISTERED,
                message = "Player %d already registered in tournament %d " % ( serial, tourney_serial )
            )
            self.log.error("%s", error)
            for avatar in avatars:
                avatar.sendPacketVerbose(error)
            return False

        if not tourney.canRegister(serial):
            error = PacketError(
                other_type = PACKET_POKER_TOURNEY_REGISTER,
                code = PacketPokerTourneyRegister.REGISTRATION_REFUSED,
                message = "Registration refused in tournament %d " % tourney_serial
            )
            self.log.error("%s", error)
            for avatar in avatars:
                avatar.sendPacketVerbose(error)
            return False

        cursor = self.db.cursor()
        #
        # Buy in
        #
        currency_serial = tourney.currency_serial or 0
        withdraw = tourney.buy_in + tourney.rake
        if withdraw > 0:
            sql = \
                "UPDATE user2money SET amount = amount - %s " \
                "WHERE user_serial = %s " \
                "AND currency_serial = %s " \
                "AND amount >= %s"
            params = (withdraw,serial,currency_serial,withdraw)
            cursor.execute(sql,params)
            self.log.debug("tourneyRegister: %s" % cursor._executed)
            if cursor.rowcount == 0:
                error = PacketError(
                    other_type = PACKET_POKER_TOURNEY_REGISTER,
                    code = PacketPokerTourneyRegister.NOT_ENOUGH_MONEY,
                    message = "Not enough money to enter the tournament %d" % tourney_serial
                )
                for avatar in avatars:
                    avatar.sendPacketVerbose(error)
                self.log.error("%s", error)
                return False
            if cursor.rowcount != 1:
                self.log.error("modified %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
                for avatar in avatars:
                    avatar.sendPacketVerbose(PacketError(
                        other_type = PACKET_POKER_TOURNEY_REGISTER,
                        code = PacketPokerTourneyRegister.SERVER_ERROR,
                        message = "Server error"
                    ))
                return False
        self.databaseEvent(event = PacketPokerMonitorEvent.REGISTER, param1 = serial, param2 = currency_serial, param3 = withdraw)
        #
        # Register
        #
        sql = "INSERT INTO user2tourney (user_serial, currency_serial, tourney_serial) VALUES (%s, %s, %s)"
        params = (serial, currency_serial, tourney_serial)
        cursor.execute(sql,params)
        self.log.debug("tourneyRegister: %s", cursor._executed)
        if cursor.rowcount != 1:
            self.log.error("insert %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
            cursor.close()
            for avatar in avatars:
                avatar.sendPacketVerbose(PacketError(
                    other_type = PACKET_POKER_TOURNEY_REGISTER,
                    code = PacketPokerTourneyRegister.SERVER_ERROR,
                    message = "Server error"
                ))
            return False
        cursor.close()

        # notify success
        for avatar in avatars:
            avatar.sendPacketVerbose(packet)
        tourney.register(serial,self.getName(serial))
        return True

    def tourneyUnregister(self, packet):
        serial = packet.serial
        tourney_serial = packet.tourney_serial
        if tourney_serial not in self.tourneys:
            return PacketError(
                other_type = PACKET_POKER_TOURNEY_UNREGISTER,
                code = PacketPokerTourneyUnregister.DOES_NOT_EXIST,
                message = "Tournament %d does not exist" % tourney_serial
            )
        tourney = self.tourneys[tourney_serial]

        if not tourney.isRegistered(serial):
            return PacketError(
                other_type = PACKET_POKER_TOURNEY_UNREGISTER,
                code = PacketPokerTourneyUnregister.NOT_REGISTERED,
                message = "Player %d is not registered in tournament %d " % ( serial, tourney_serial ) 
            )

        if not tourney.canUnregister(serial):
            return PacketError(
                other_type = PACKET_POKER_TOURNEY_UNREGISTER,
                code = PacketPokerTourneyUnregister.TOO_LATE,
                message = "It is too late to unregister player %d from tournament %d " % ( serial, tourney_serial ) 
            )

        cursor = self.db.cursor()
        #
        # Refund registration fees
        #
        currency_serial = tourney.currency_serial
        withdraw = tourney.buy_in + tourney.rake
        if withdraw > 0:
            cursor.execute(
                "UPDATE user2money SET amount = amount + %s " \
                "WHERE user_serial = %s " \
                "AND currency_serial = %s",
                (withdraw,serial,currency_serial)
            )
            self.log.debug("tourneyUnregister: %s", cursor._executed)
            if cursor.rowcount != 1:
                self.log.error("modified no rows (expected 1): %s", cursor._executed)
                return PacketError(
                    other_type = PACKET_POKER_TOURNEY_UNREGISTER,
                    code = PacketPokerTourneyUnregister.SERVER_ERROR,
                    message = "Server error : user_serial = %d and currency_serial = %d was not in user2money" % (serial,currency_serial)
                )
            self.databaseEvent(event = PacketPokerMonitorEvent.UNREGISTER, param1 = serial, param2 = currency_serial, param3 = withdraw)
        #
        # unregister
        cursor.execute("DELETE FROM user2tourney WHERE user_serial = %s AND tourney_serial = %s",(serial,tourney_serial))
        self.log.debug("tourneyUnregister: %s", cursor._executed)
        if cursor.rowcount != 1:
            self.log.error("delete no rows (expected 1): %s", cursor._executed)
            cursor.close()
            return PacketError(
                other_type = PACKET_POKER_TOURNEY_UNREGISTER,
                code = PacketPokerTourneyUnregister.SERVER_ERROR,
                message = "Server error : user_serial = %d and tourney_serial = %d was not in user2tourney" % ( serial, tourney_serial )
            )
        cursor.close()

        tourney.unregister(serial)

        return packet

    def tourneyStart(self, tourney):
        '''start a registering tourney immediately.
        
        if more than one player is registered, players_min and quota is set to the
        amount of the currently registered players.
        '''
        now = seconds()
        cursor = self.db.cursor()
        sql = 'UPDATE tourneys SET start_time=%s, players_min=%s, players_quota=%s WHERE serial=%s'
        cursor.execute(sql, (now,tourney.registered,tourney.registered,tourney.serial))
        tourney.start_time = now
        tourney.players_min = tourney.players_quota = tourney.registered
        tourney.updateRunning()
        
        return PacketAck()

    def tourneyCancel(self, tourney):
        players = list(tourney.players.iterkeys())
        self.log.debug("tourneyCancel: %s", players)
        self.databaseEvent(event = PacketPokerMonitorEvent.TOURNEY_CANCELED, param1 = tourney.serial)
        for serial in players:
            avatars = self.avatar_collection.get(serial)
            packet = self.tourneyUnregister(PacketPokerTourneyUnregister(
                tourney_serial = tourney.serial,
                serial = serial
            ))
            if packet.type == PACKET_ERROR:
                self.log.debug("tourneyCancel: %s", packet)
            for avatar in avatars:
                avatar.sendPacketVerbose(packet)

    def getHandSerial(self):
        cursor = self.db.cursor()
        cursor.execute("INSERT INTO hands (description) VALUES ('[]')")
        serial = cursor.lastrowid
        cursor.close()
        return int(serial)

    def getHandHistory(self, hand_serial, serial):
        history = self.loadHand(hand_serial)

        if not history:
            return PacketPokerError(
                game_id = hand_serial,
                serial = serial,
                other_type = PACKET_POKER_HAND_HISTORY,
                code = PacketPokerHandHistory.NOT_FOUND,
                message = "Hand %d was not found in history of player %d" % ( hand_serial, serial ) 
            )

        (type, level, hand_serial, hands_count, time, variant, betting_structure, player_list, dealer, serial2chips) = history[0]

        if serial not in player_list:
            return PacketPokerError(
                game_id = hand_serial,
                serial = serial,
                other_type = PACKET_POKER_HAND_HISTORY,
                code = PacketPokerHandHistory.FORBIDDEN,
                message = "Player %d did not participate in hand %d" % ( serial, hand_serial ) 
            )

        serial2name = {}
        for player_serial in player_list:
            serial2name[player_serial] = self.getName(player_serial)
        #
        # Filter out the pocket cards that do not belong to player "serial"
        #
        for event in history:
            if event[0] == "round":
                (type, name, board, pockets) = event
                if pockets:
                    for (player_serial, pocket) in pockets.iteritems():
                        if player_serial != serial:
                            pocket.loseNotVisible()
            elif event[0] == "showdown":
                (type, board, pockets) = event
                if pockets:
                    for (player_serial, pocket) in pockets.iteritems():
                        if player_serial != serial:
                            pocket.loseNotVisible()

        return PacketPokerHandHistory(
            game_id = hand_serial,
            serial = serial,
            history = str(history),
            serial2name = str(serial2name)
        )

    def loadHand(self, hand_serial, load_from_cache=True):
        #
        # load from hand_cache if needed and available
        if load_from_cache and hand_serial in self.hand_cache:
            return self.hand_cache[hand_serial]
        #
        # else fetch the hand from the database
        cursor = self.db.cursor()
        sql = "SELECT description FROM hands WHERE serial = %s"
        cursor.execute(sql,(hand_serial,))
        if cursor.rowcount != 1:
            self.log.error("loadHand(%d) expected one row got %d", hand_serial, cursor.rowcount)
            cursor.close()
            return None
        (description,) = cursor.fetchone()
        cursor.close()
        history = None
        try:
            history = eval(description.replace("\r",""), {'PokerCards':PokerCards})
        except Exception:
            self.log.error("loadHand(%d) eval failed for %s", hand_serial, description, exc_info=1)
        return history

    def saveHand(self, description, hand_serial, save_to_cache=True):
        (hand_type, level, hand_serial, hands_count, time, variant, betting_structure, player_list, dealer, serial2chips) = description[0] #@UnusedVariable
        #
        # save the value to the hand_cache if needed
        if save_to_cache:
            for obsolete_hand_serial in self.hand_cache.keys()[:-3]:
                del self.hand_cache[obsolete_hand_serial]
            self.hand_cache[hand_serial] = description
        
        cursor = self.db.cursor()
        sql = "UPDATE hands SET description = %s WHERE serial = %s"
        params = (str(description), hand_serial)
        cursor.execute(sql, params)
        self.log.debug("saveHand: %s" , cursor._executed)
        if cursor.rowcount not in (1,0):
            self.log.error("modified %d rows (expected 1 or 0): %s", cursor.rowcount, cursor._executed)
            cursor.close()
            return
        sql = "INSERT INTO user2hand VALUES "
        sql += ", ".join("(%d, %d)" % (player_serial, hand_serial) for player_serial in player_list)
        cursor.execute(sql)
        self.log.debug("saveHand: %s", sql)
        if cursor.rowcount != len(player_list):
            self.log.error("inserted %d rows (expected exactly %d): %s", cursor.rowcount, len(player_list), cursor._executed)
        cursor.close()

    def listHands(self, sql_list, sql_total):
        cursor = self.db.cursor()
        self.log.debug("listHands: %s %s", sql_list, sql_total)
        cursor.execute(sql_list)
        hands = cursor.fetchall()
        cursor.execute(sql_total)
        total = cursor.fetchone()[0]
        cursor.close()
        return (total, [x[0] for x in hands])

    def eventTable(self, table):
        self.log.debug("eventTable: %s" % {
            'game_id': table.game.id ,
            'tourney_serial': table.tourney.serial if table.tourney else 0
        })

    def statsTables(self):
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) FROM pokertables")
        tables = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM user2table")
        players = cursor.fetchone()[0]
        cursor.close()
        return ( players, tables )

    def listTables(self, query_string, serial):
        """listTables() takes two arguments:

                 query_string : which is the ad-hoc query string for the tables
                                sought (described in detail below), and
                 serial       : which is the user serial, used only when query_string == "my"

           The ad-hoc format of the query string deserves special
           documentation.  It works as follows:

               0. If query_string is the empty string, or exactly 'all', then
                  all tables in the system are returned.

               1. If query_string is 'marked', then all tables in the system are
                  returned, and the table objects contain a player_seated
                  attribute that is set to 1 if the player is currently
                  seated at that table (otherwise the attribute is set 
                  to 0).
                  
               2. If query_string is 'my', then all tables that player identified
                  by the argument, 'serial', has joined are returned.

               3. If query_string (a) contains *no* TAB (\t) characters AND (b)
                  contains any non-numeric characters (aka is a string of
                  letters, optionally with numbers, with no tabs), then it
                  assumed to be a specific table name, and only table(s)
                  with the specific name exactly equal to the string are
                  returned.

               4. Otherwise, query_string is interpreted as a tab-separated
                  group of criteria for selecting which tables to be
                  returned, which mimics the 'string' input given in a
                  PacketPokerTableSelect() (this method was written
                  primarily to service that packet).  Two rules to keep in
                  mind when constructing the query_string:

                     (a) If any field is the empty string (i.e., nothing
                         between the tab characters for that field), then
                         the given criterion is not used for table
                         selection.

                     (b) If the second field is left completely off (by
                         simply having fewer than the maximum tab
                         characters) are treated as if they were present
                         but empty strings (i.e., they are not used as a
                         criterion for table selection).

                     (c) Any additional fields are ignored, but an error
                         message is generated.

                  The tab-separated query string currently accepts two fields:
                         "currency_serial\tvariant"
           """
        # It appears to me that the original motivation for this \t
        # seperated format for query_string was that the string would front-load
        # with more commonly used criteria, and put less frequently used
        # ones further to the back.  Thus, the query string can be
        # effeciently constructed by callers.  During implementation of
        # the table-picker feature, I wrote the documentation above and I
        # heavily extended this method to account for additional criteria
        # that table-picker wanted to use.  dachary and I discussed a bit
        # whether it was better not to expand this method, but I felt it
        # was close enough that it was.  Our discussion happened on IRC
        # circa 2009-06-20 and following. -- bkuhn, 2009-06-20

        #  However, after additional debate and looking at the final
        #  implementation and tests, we discovered this whole approach was
        #  a mess, yielding the refactoring out of listTables() into its
        #  helper function, searchTables() -- bkuhn, 2009-07-03

        orderBy = " ORDER BY players desc, serial"
        
        criteria = query_string.split("\t")
        cursor = self.db.cursor(DictCursor)
        if query_string == '' or query_string == 'all':
            cursor.execute("SELECT * FROM pokertables" + orderBy)
        elif query_string == 'my':
            cursor.execute("SELECT pokertables.* FROM pokertables,user2table WHERE pokertables.serial = user2table.table_serial AND user2table.user_serial = %s" + orderBy, serial)
        elif query_string == 'marked':
            cursor.execute("SELECT pokertables.*, IF(user2table.user_serial IS NULL,0,1) player_seated FROM pokertables LEFT JOIN user2table on (pokertables.serial = user2table.table_serial AND user2table.user_serial = %s)" + orderBy, serial)
        elif re.match("^[0-9]+$", query_string):
            cursor.execute("SELECT * FROM pokertables WHERE currency_serial = %s" + orderBy, query_string)
        elif len(criteria) > 1:
            # Next, unpack the various possibilities in the tab-separated
            # criteria, starting with everything set to None.  This is to
            # setup the defaults to be None when we call the helper
            # function.
            whereValues = { 'currency_serial' : None, 'variant' : None }
            if len(criteria) == 2:
                ( whereValues['currency_serial'], whereValues['variant'] ) = criteria
            else:
                self.log.error("Following listTables() criteria query_string "
                    "has more parameters than expected, ignoring third one and beyond in: %s",
                    query_string
                )
                ( whereValues['currency_serial'], whereValues['variant'] ) = criteria[:2]
            # Next, do some minor format verification for those values that are
            # supposed to be integers.
            if whereValues['currency_serial'] != None and whereValues['currency_serial'] != '':
                if not re.match("^[0-9]+$", whereValues['currency_serial']):
                    self.log.error(
                        "listTables(): currency_serial parameter must be an integer, instead was: %s"
                        % whereValues['currency_serial']
                    )
                    cursor.close()
                    return []
                else:
                    whereValues['currency_serial'] = int(whereValues['currency_serial'])
            cursor.close()
            return self.searchTables(whereValues['currency_serial'], whereValues['variant'], None, None)
        else:
            cursor.execute("SELECT * FROM pokertables WHERE name = %s", query_string)

        result = cursor.fetchall()
        cursor.close()
        return result

    def searchTables(self, currency_serial = None, variant = None, betting_structure = None, min_players = 0):
        """searchTables() returns a list of tables that match the criteria
        specified in the parameters.  Parameter requirements are:
            currency_serial:    must be a positive integer or None
            variant:            must be a string or None
            betting_structure:  must be a string or None
            min_players:        must be a non-negative integer

        Note that the 'min_players' criterion is a >= setting.  The rest
        are exactly = to setting.

        Note further that min_players and currency_serial *must be*
        integer values in decimal greater than 0.  (Also, if sent in equal
        to 0, no error will be generated, but it will be as if you didn't
        send them at all).

        Finally, the query is sorted such that tables with the most
        players are at the top of the list.  Note that other methods rely
        on this, so don't change it.  The secondary sorting key is the
        ascending table serial.
        """
        orderBy = " ORDER BY players desc, serial"
        whereValues = { 'currency_serial' : currency_serial, 'variant' : variant,
                        'betting_structure' : betting_structure, 'min_players' : min_players }
        cursor = self.db.cursor(DictCursor)
        # Now build the SQL statement we need.
        sql = "SELECT * FROM pokertables"
        sqlQuestionMarkParameterList = []
        startLen = len(sql)
        for (kk, vv) in whereValues.iteritems():
            if vv == None or vv == '' or (kk == 'currency_serial' and int(vv) == 0):
                # We skip any value that is was not given to us (is still
                # None), was entirely empty when it came in, or, in the
                # case of currency_serial, is 0, since a 0 currency_serial
                # is not valid.
                continue
            # Next, if we have an sql statement already from previous
            # iteration of this loop, add an "AND", otherwise, initialze
            # the sql string with the beginning of the SELECT statement.
            if len(sql) > startLen:
                sql += " AND "
            else:
                sql += " WHERE "
            # Next, we handle the fact that min_players is a >= parameter,
            # unlike the others which are = parameters.  Also, note here
            # that currency_serial and min_players are integer values.
            if kk == 'min_players':
                sql += " players >= " + "%s"
            else:
                sql += kk + " = " + "%s"
            sqlQuestionMarkParameterList.append(vv)

        sql += orderBy
        cursor.execute(sql, sqlQuestionMarkParameterList)
        result = cursor.fetchall()
        cursor.close()
        return result

    def setupResthost(self):
        resthost = self.settings.headerGetProperties("/server/resthost")
        if resthost:
            resthost = resthost[0]
            cursor = self.db.cursor()
            values = ( resthost['host'], resthost['port'], resthost['path'])
            name = resthost.get('name', '')
            cursor.execute("SELECT serial FROM resthost WHERE host = %s AND port = %s AND path = %s", values)
            if cursor.rowcount > 0:
                self.resthost_serial = cursor.fetchone()[0]
                cursor.execute("UPDATE resthost SET state = %s WHERE serial = %s", (self.STATE_ONLINE,self.resthost_serial))
            else:
                if not name:
                    cursor.execute("INSERT INTO resthost (name, host, port, path, state) VALUES ('', %s, %s, %s, %s)", values + (self.STATE_ONLINE,))
                else:
                    cursor.execute("INSERT INTO resthost (name, host, port, path, state) VALUES (%s, %s, %s, %s, %s)", (name,) + values + (self.STATE_ONLINE,))
                self.resthost_serial = cursor.lastrowid
            cursor.execute("DELETE FROM route WHERE resthost_serial = %s", self.resthost_serial)
            cursor.close()
    
    def setResthostOnShuttingDown(self):
        if self.resthost_serial:
            cursor = self.db.cursor()
            cursor.execute("UPDATE resthost SET state = %s WHERE serial = %s", (self.STATE_SHUTTING_DOWN,self.resthost_serial))
            cursor.close()
            
    def cleanupResthost(self):
        if self.resthost_serial:
            cursor = self.db.cursor()
            cursor.execute("DELETE FROM route WHERE resthost_serial = %s", self.resthost_serial)
            cursor.execute("UPDATE resthost SET state = %s WHERE serial = %s", (self.STATE_OFFLINE,self.resthost_serial))
            cursor.close()

    def packet2resthost(self, packet):
        #
        # game_id is only set for packets related to a table and not for
        # packets that are delegated but related to tournaments.
        #
        game_id = None
        result = None
        where = ""
        cursor = self.db.cursor()
        if packet.type == PACKET_POKER_CREATE_TOURNEY:
            #
            # Look for the server with the less routes going to it
            #
            sql = \
                "SELECT rh.serial, host, port, path " \
                "FROM resthost rh " \
                "JOIN route r ON (rh.serial=r.resthost_serial) " \
                "GROUP BY rh.serial " \
                "ORDER BY count(rh.serial) " \
                "LIMIT 1"
            self.log.debug("packet2resthost: create tourney %s", sql)
            cursor.execute(sql)
            result = None
            if cursor.rowcount > 0:
                (resthost_serial, host, port, path) = cursor.fetchone()
                if resthost_serial != self.resthost_serial:
                    result = (host,port,path)
        else:
            if packet.type in ( PACKET_POKER_TOURNEY_REQUEST_PLAYERS_LIST, PACKET_POKER_TOURNEY_REGISTER, PACKET_POKER_TOURNEY_UNREGISTER ):
                where = "tourney_serial = %d" % packet.tourney_serial
            elif packet.type in ( PACKET_POKER_GET_TOURNEY_MANAGER, ):
                where = "tourney_serial = " + str(packet.tourney_serial)
            elif getattr(packet, "game_id",0) > 0 and packet.game_id in self.tables.iterkeys():
                game_id = packet.game_id
            elif getattr(packet, "game_id",0) > 0:
                where = "table_serial = %d" % packet.game_id
                game_id = packet.game_id
                
            if where:
                cursor.execute(
                   "SELECT host, port, path FROM route,resthost WHERE route.resthost_serial = resthost.serial " \
                   "AND resthost.serial != %d AND %s" % (self.resthost_serial,where)
                )
                result = cursor.fetchone() if cursor.rowcount > 0 else None
        cursor.close()
        return ( result, game_id )

    def cleanUpTemporaryUsers(self):
        cursor = self.db.cursor()
        params = (self.temporary_serial_min,self.temporary_serial_max,self.temporary_users_pattern)
        sql = "DELETE session_history FROM session_history, users WHERE session_history.user_serial = users.serial AND (users.serial BETWEEN %s AND %s OR users.name RLIKE %s)"
        cursor.execute(sql,params)
        sql = "DELETE session FROM session, users WHERE session.user_serial = users.serial AND (users.serial BETWEEN %s AND %s OR users.name RLIKE %s)"
        cursor.execute(sql,params)
        sql = "DELETE user2tourney FROM user2tourney, users WHERE (users.serial BETWEEN %s AND %s OR users.name RLIKE %s) AND users.serial = user2tourney.user_serial"
        cursor.execute(sql,params)
        sql = "DELETE FROM users WHERE serial BETWEEN %s AND %s OR name RLIKE %s"
        cursor.execute(sql,params)

        sql = "INSERT INTO session_history ( user_serial, started, ended, ip ) SELECT user_serial, started, %s, ip FROM session"
        cursor.execute(sql,(seconds(),))
        sql = "DELETE FROM session"
        cursor.execute(sql)
        cursor.close()

    def abortRunningTourneys(self):
        cursor = self.db.cursor()
        try:
            cursor.execute("SELECT serial FROM tourneys WHERE state IN ('running', 'break', 'breakwait')")
            if cursor.rowcount:
                for (tourney_serial,) in cursor.fetchall():
                    self.databaseEvent(event = PacketPokerMonitorEvent.TOURNEY_CANCELED, param1 = tourney_serial)

                cursor.execute(
                    "UPDATE tourneys AS t " \
                        "LEFT JOIN user2tourney AS u2t ON u2t.tourney_serial = t.serial " \
                        "LEFT JOIN user2money AS u2m ON u2m.user_serial = u2t.user_serial " \
                    "SET u2m.amount = u2m.amount + t.buy_in + t.rake, t.state = 'aborted' " \
                    "WHERE " \
                        "t.resthost_serial = %s AND " \
                        "t.state IN ('running', 'break', 'breakwait')",
                    (self.resthost_serial,)
                )
                
                self.log.debug("cleanupTourneys: %s", cursor._executed)

        finally:
            cursor.close()

    def cleanupTourneys(self):
        self.tourneys = {}
        self.schedule2tourneys = {}
        self.tourneys_schedule = {}
        cursor = self.db.cursor(DictCursor)
        try:
            # abort still running tourneys and refund buyin
            self.abortRunningTourneys()
            
            # trash tourneys and their user2tourney data which are either sit'n'go and aborted
            # or in registering state with a starttime in the past+60s
            where = \
                "WHERE t.resthost_serial = %s " \
                "AND ( " \
                    "(t.sit_n_go = 'y' AND t.state IN ('aborted', 'registering')) " \
                    "OR (t.state = 'registering' AND t.start_time < %s) " \
                ")"
            _seconds = seconds()
            cursor.execute(
                "UPDATE tourneys AS t " \
                    "LEFT JOIN user2tourney AS u2t ON u2t.tourney_serial = t.serial " \
                    "LEFT JOIN user2money AS u2m ON u2m.user_serial = u2t.user_serial " \
                "SET u2m.amount = u2m.amount + t.buy_in + t.rake, t.state = 'aborted' " \
                + where,
                (self.resthost_serial, _seconds)
            )
            if cursor.rowcount:
                self.log.debug("cleanupTourneys: rows: %d, sql: %s", cursor.rowcount, cursor._executed)
            cursor.execute(
                "DELETE u2t FROM user2tourney AS u2t LEFT JOIN tourneys AS t ON t.serial = u2t.tourney_serial " + where,
                (self.resthost_serial, _seconds)
            )
            if cursor.rowcount:
                self.log.debug("cleanupTourneys: %s", cursor._executed)
            cursor.execute("DELETE t FROM tourneys AS t " + where,
                (self.resthost_serial, _seconds)
            )
            if cursor.rowcount:
                self.log.debug("cleanupTourneys: %s", cursor._executed)
            
            # restore registering tourneys
            cursor.execute(
                "SELECT * FROM tourneys " \
                "WHERE resthost_serial = %s " \
                "AND state = 'registering' " \
                "AND start_time >= %s",
                (self.resthost_serial, _seconds)
            )
            self.log.debug("cleanupTourneys: %s", cursor._executed)
            for row in cursor.fetchall():
                tourney = self.spawnTourneyInCore(row, row['serial'], row['schedule_serial'], row['currency_serial'], row['prize_currency'])
                cursor.execute(
                    "SELECT u.serial, u.name FROM users AS u " \
                    "JOIN user2tourney AS u2t " \
                    "ON (u.serial=u2t.user_serial AND u2t.tourney_serial=%s)",
                    (row['serial'],)
                )
                self.log.debug("cleanupTourneys: %s", cursor._executed)
                for user in cursor.fetchall():
                    tourney.register(user['serial'],user['name'])
                cursor.execute(
                    "REPLACE INTO route VALUES (0, %s, %s, %s)",
                    (row['serial'], _seconds, self.resthost_serial)
                )
        finally:
            cursor.close()

    def getMoney(self, serial, currency_serial):
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT amount FROM user2money " \
            "WHERE user_serial = %s " \
            "AND currency_serial = %s",
            (serial,currency_serial)
        )
        self.log.debug("%s", cursor._executed)

        if cursor.rowcount > 1:
            self.log.error("getMoney(%d) expected one row got %d", serial, cursor.rowcount)
            cursor.close()
            return 0
        elif cursor.rowcount == 1:
            (money,) = cursor.fetchone()
        else:
            money = 0
        cursor.close()
        return money

    def cashIn(self, packet):
        return self.cashier.cashIn(packet)

    def cashOut(self, packet):
        return self.cashier.cashOut(packet)

    def cashQuery(self, packet):
        return self.cashier.cashQuery(packet)

    def cashOutCommit(self, packet):
        count = self.cashier.cashOutCommit(packet)
        if count in (0, 1):
            return PacketAck()
        else:
            return PacketError(
                code = PacketPokerCashOutCommit.INVALID_TRANSACTION,
                message = "transaction " + packet.transaction_id + " affected " + str(count) + " rows instead of zero or one",
                other_type = PACKET_POKER_CASH_OUT_COMMIT
            )

    def getPlayerInfo(self, serial):
        placeholder = PacketPokerPlayerInfo(
            serial = serial,
            name = "anonymous",
            url= "",
            outfit = "",
            locale = "en_US"
        )
        if serial == 0:
            return placeholder

        cursor = self.db.cursor()
        
        cursor.execute(
            "SELECT locale,name,skin_url,skin_outfit FROM users WHERE serial = %s",
            (serial,)
        )
        if cursor.rowcount != 1:
            self.log.error("getPlayerInfo(%d) expected one row got %d", serial, cursor.rowcount)
            return placeholder
        (locale,name,skin_url,skin_outfit) = cursor.fetchone()
        if skin_outfit == None: skin_outfit = ""
        cursor.close()
        packet = PacketPokerPlayerInfo(
            serial = serial,
            name = name,
            url = skin_url,
            outfit = skin_outfit
        )
        # pokerservice generally provides playerInfo() internally to
        # methods like pokeravatar.(re)?login.  Since this is the central
        # internal location where the query occurs, we hack in the locale
        # returned from the DB.
        packet.locale = locale
        return packet

    def getPlayerPlaces(self, serial):
        cursor = self.db.cursor()
        cursor.execute("SELECT table_serial FROM user2table WHERE user_serial = %s", serial)
        tables = map(lambda x: x[0], cursor.fetchall())
        cursor.execute("SELECT user2tourney.tourney_serial FROM user2tourney,tourneys WHERE user2tourney.user_serial = %s AND user2tourney.tourney_serial = tourneys.serial AND (tourneys.state = 'registering' OR tourneys.state = 'running' OR tourneys.state = 'break' OR  tourneys.state = 'breakwait')", serial)
        tourneys = map(lambda x: x[0], cursor.fetchall())
        cursor.close()
        return PacketPokerPlayerPlaces(
            serial = serial,
            tables = tables,
            tourneys = tourneys
        )

    def getPlayerPlacesByName(self, name):
        cursor = self.db.cursor()
        cursor.execute("SELECT serial FROM users WHERE name = %s", name)
        serial = cursor.fetchone()
        if serial == None:
            return PacketError(other_type = PACKET_POKER_PLAYER_PLACES)
        else:
            serial = serial[0]
        return self.getPlayerPlaces(serial)

    def isTemporaryUser(self,serial):
        return bool(
            self.temporary_serial_min <= serial <= self.temporary_serial_max or 
            re.match(self.temporary_users_pattern,self.getName(serial))
        )
        
    def getUserInfo(self, serial):
        cursor = self.db.cursor(DictCursor)

        sql = "SELECT rating,affiliate,email,name FROM users WHERE serial = %s"
        cursor.execute(sql,(serial,))
        if cursor.rowcount != 1:
            self.log.error("getUserInfo(%d) expected one row got %d", serial, cursor.rowcount)
            return PacketPokerUserInfo(serial = serial)
        row = cursor.fetchone()
        if row['email'] == None: row['email'] = ""

        packet = PacketPokerUserInfo(
            serial = serial,
            name = row['name'],
            email = row['email'],
            rating = row['rating'],
            affiliate = row['affiliate']
        )
        sql = \
            "SELECT user2money.currency_serial,user2money.amount,user2money.points,CAST(SUM(user2table.bet) + SUM(user2table.money) AS UNSIGNED) AS in_game " \
            "FROM user2money " \
            "LEFT JOIN (pokertables,user2table) ON ( " \
                "user2table.user_serial = user2money.user_serial " \
                "AND user2table.table_serial = pokertables.serial " \
                "AND user2money.currency_serial = pokertables.currency_serial " \
            ") " \
            "WHERE user2money.user_serial = %s GROUP BY user2money.currency_serial"
        cursor.execute(sql,(serial,))
        self.log.debug("getUserInfo: %s", cursor._executed)
        for row in cursor:
            if not row['in_game']: row['in_game'] = 0
            if not row['points']: row['points'] = 0
            packet.money[row['currency_serial']] = ( row['amount'], row['in_game'], row['points'] )
        self.log.debug("getUserInfo: %s", packet)
        return packet

    def getPersonalInfo(self, serial):
        user_info = self.getUserInfo(serial)
        self.log.debug("getPersonalInfo %s", user_info)
        packet = PacketPokerPersonalInfo(
            serial = user_info.serial,
            name = user_info.name,
            email = user_info.email,
            rating = user_info.rating,
            affiliate = user_info.affiliate,
            money = user_info.money
        )
        cursor = self.db.cursor()
        sql = "SELECT firstname,lastname,addr_street,addr_street2,addr_zip,addr_town,addr_state,addr_country,phone,gender,birthdate FROM users_private WHERE serial = %s"
        cursor.execute(sql,(serial,))
        if cursor.rowcount != 1:
            self.log.error("getPersonalInfo(%d) expected one row got %d", serial, cursor.rowcount)
            return PacketPokerPersonalInfo(serial = serial)
        (packet.firstname, packet.lastname, packet.addr_street, packet.addr_street2, packet.addr_zip, packet.addr_town, packet.addr_state, packet.addr_country, packet.phone, packet.gender, packet.birthdate) = cursor.fetchone()
        cursor.close()
        if not packet.gender: packet.gender = ''
        if not packet.birthdate: packet.birthdate = ''
        packet.birthdate = str(packet.birthdate)
        return packet

    def setPersonalInfo(self, personal_info):
        cursor = self.db.cursor()
        sql = \
            "UPDATE users_private " \
            "SET firstname = %s, lastname = %s, addr_street = %s, addr_street2 = %s, addr_zip = %s, " \
                "addr_town = %s, addr_state = %s, addr_country = %s, phone = %s, gender = %s, birthdate = %s " \
            "WHERE serial = %s"
        params = (
            personal_info.firstname, personal_info.lastname, personal_info.addr_street, personal_info.addr_street2, personal_info.addr_zip,
            personal_info.addr_town, personal_info.addr_state, personal_info.addr_country, personal_info.phone, personal_info.gender, personal_info.birthdate,
            personal_info.serial
        )
        cursor.execute(sql,params)
        self.log.debug("setPersonalInfo: %s", cursor._executed)
        if cursor.rowcount != 1 and cursor.rowcount != 0:
            self.log.error("setPersonalInfo: modified %d rows (expected 1 or 0): %s", cursor.rowcount, sql)
            return False
        else:
            return True

    def setAccount(self, packet):
        #
        # name constraints check
        status = checkName(packet.name)
        if not status[0]:
            return PacketError(
                code = status[1],
                message = status[2],
                other_type = packet.type
            )
        #
        # look for user
        cursor = self.db.cursor()
        cursor.execute("SELECT serial FROM users WHERE name = %s", (packet.name,))
        numrows = int(cursor.rowcount)
        #
        # password constraints check
        if ( numrows == 0 or ( numrows > 0 and packet.password != "" )):
            status = checkPassword(packet.password)
            if not status[0]:
                return PacketError(
                    code = status[1],
                    message = status[2],
                    other_type = packet.type
                )
        #
        # email constraints check
        email_regexp = ".*.@.*\..*$"
        if not re.match(email_regexp, packet.email):
            return PacketError(
                code = PacketPokerSetAccount.INVALID_EMAIL,
                message = "email %s does not match %s " % ( packet.email, email_regexp ),
                other_type = packet.type
            )
        if numrows == 0:
            cursor.execute("SELECT serial FROM users WHERE email = %s", (packet.email,))
            numrows = int(cursor.rowcount)
            if numrows > 0:
                return PacketError(
                    code = PacketPokerSetAccount.EMAIL_ALREADY_EXISTS,
                    message = "there already is another account with the email %s" % packet.email,
                    other_type = packet.type
                )
            #
            # user does not exists, create it
            sql = "INSERT INTO users (created, name, password, email, affiliate) values (%s, %s, %s, %s, %s)"
            params = (seconds(), packet.name, packet.password, packet.email, str(packet.affiliate))
            cursor.execute(sql,params)
            if cursor.rowcount != 1:
                #
                # impossible except for a sudden database corruption, because of the
                # above SQL statements
                self.log.error("setAccount: insert %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
                return PacketError(
                    code = PacketPokerSetAccount.SERVER_ERROR,
                    message = "inserted %d rows (expected 1)" % cursor.rowcount,
                    other_type = packet.type
                )
            packet.serial = cursor.lastrowid
            cursor.execute("INSERT INTO users_private (serial) VALUES (%s)", (packet.serial,))
        else:
            #
            # user exists, update name, password and email
            (serial,) = cursor.fetchone()
            if serial != packet.serial:
                return PacketError(
                    code = PacketPokerSetAccount.NAME_ALREADY_EXISTS,
                    message = "user name %s already exists" % packet.name,
                    other_type = packet.type
                )
            cursor.execute("SELECT serial FROM users WHERE email = %s and serial != %s", ( packet.email, serial ))
            numrows = int(cursor.rowcount)
            if numrows > 0:
                return PacketError(
                    code = PacketPokerSetAccount.EMAIL_ALREADY_EXISTS,
                    message = "there already is another account with the email %s" % packet.email,
                    other_type = packet.type
                )
            set_password = ", password = %s " % self.db.literal(packet.password) if packet.password else ""
            sql = "UPDATE users SET name = %s, email = %s " + set_password + "WHERE serial = %s"
            params = (packet.name,packet.email,packet.serial)
            cursor.execute(sql,params)
            self.log.debug("setAccount: %s", sql)
            if cursor.rowcount != 1 and cursor.rowcount != 0:
                self.log.error("setAccount: modified %d rows (expected 1 or 0): %s", cursor.rowcount, sql)
                return PacketError(
                    code = PacketPokerSetAccount.SERVER_ERROR,
                    message = "modified %d rows (expected 1 or 0)" % cursor.rowcount,
                    other_type = packet.type
                )
        #
        # set personal information
        if not self.setPersonalInfo(packet):
                return PacketError(
                    code = PacketPokerSetAccount.SERVER_ERROR,
                    message = "unable to set personal information",
                    other_type = packet.type
                )
        return self.getPersonalInfo(packet.serial)

    def setPlayerInfo(self, player_info):
        cursor = self.db.cursor()
        sql = "UPDATE users SET name = %s, skin_url = %s, skin_outfit = %s WHERE serial = %s" 
        params = (player_info.name,player_info.url,player_info.outfit,player_info.serial)
        cursor.execute(sql,params)
        self.log.debug("setPlayerInfo: %s", cursor._executed)
        if cursor.rowcount != 1 and cursor.rowcount != 0:
            self.log.error("setPlayerInfo: modified %d rows (expected 1 or 0): %s", cursor.rowcount, sql)
            return False
        return True


    def getPlayerImage(self, serial):
        placeholder = PacketPokerPlayerImage(serial = serial)

        if serial == 0:
            return placeholder

        cursor = self.db.cursor()
        cursor.execute("SELECT skin_image,skin_image_type from users where serial = %s", (serial,))
        if cursor.rowcount != 1:
            self.log.error("getPlayerImage(%d) expected one row got %d", serial, cursor.rowcount)
            return placeholder
        (skin_image, skin_image_type) = cursor.fetchone()
        if skin_image == None:
            skin_image = ""
        cursor.close()
        return PacketPokerPlayerImage(
            serial = serial,
            image = skin_image,
            image_type = skin_image_type
        )

    def setPlayerImage(self, player_image):
        cursor = self.db.cursor()
        sql = "UPDATE users SET skin_image = %s, skin_image_type = %s WHERE serial = %s" 
        params = (player_image.image,player_image.image_type,player_image.serial)
        cursor.execute(sql,params)
        self.log.debug("setPlayerInfo: %s", cursor._executed)
        if cursor.rowcount != 1 and cursor.rowcount != 0:
            self.log.error("setPlayerImage: modified %d rows (expected 1 or 0): %s", cursor.rowcount, sql)
            return False
        return True

    def getName(self, serial):
        if serial == 0:
            return "anonymous"

        cursor = self.db.cursor()
        sql = ( "SELECT name FROM users WHERE serial = " + str(serial) )
        cursor.execute(sql)
        if cursor.rowcount != 1:
            self.log.error("getName(%d) expected one row got %d", serial, cursor.rowcount)
            return "UNKNOWN"
        (name,) = cursor.fetchone()
        cursor.close()
        return name
    
    def getNames(self,serials):
        cursor = self.db.cursor()
        sql = "SELECT serial,name FROM users WHERE serial IN (%s)"
        params = ", ".join("%d" % serial for serial in set(serials) if serial > 0)
        cursor.execute(sql % params)
        ret = cursor.fetchall()
        cursor.close()
        return ret
    
    def getTableAutoDeal(self):
        return self.settings.headerGet("/server/@autodeal") == "yes"
    
    def buyInPlayer(self, serial, table_id, currency_serial, amount):
        if amount == None:
            self.log.error("called buyInPlayer with None amount (expected > 0); denying buyin")
            return 0
        # unaccounted money is delivered regardless
        if not currency_serial: return amount

        withdraw = min(self.getMoney(serial, currency_serial), amount)
        cursor = self.db.cursor()
        cursor.execute(
           "UPDATE user2money,user2table SET " \
           "user2table.money = user2table.money + %s, " \
           "user2money.amount = user2money.amount - %s " \
           "WHERE user2money.user_serial = %s " \
           "AND user2money.currency_serial = %s " \
           "AND user2table.user_serial = %s " \
           "AND user2table.table_serial = %s",
            (withdraw,withdraw,serial,currency_serial,serial,table_id)
        )
        self.log.debug("buyInPlayer: %s", cursor._executed)
        if cursor.rowcount != 0 and cursor.rowcount != 2:
            self.log.error("modified %d rows (expected 0 or 2): %s", cursor.rowcount, cursor._executed)
        self.databaseEvent(event = PacketPokerMonitorEvent.BUY_IN, param1 = serial, param2 = table_id, param3 = withdraw)
        return withdraw

    def seatPlayer(self, serial, table_id, amount, minimum_amount = None):
        status = True
        cursor = self.db.cursor()
        if minimum_amount:
            cursor.execute(
                "SELECT COUNT(*) FROM user2money " \
                "WHERE user_serial = %s " \
                "AND currency_serial = %s " \
                "AND amount >= %s",
                ((serial,)+minimum_amount)
            )
            status = (cursor.fetchone()[0] >= 1)
            cursor.close()
        if status:
            cursor = self.db.cursor()
            cursor.execute(
                "INSERT INTO user2table ( user_serial, table_serial, money) " \
                "VALUES (%s, %s, %s)",
                (serial,table_id,amount)
            )
            self.log.debug("seatPlayer: %s", cursor._executed)
            if cursor.rowcount != 1:
                self.log.error("inserted %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
                status = False
            cursor.close()
            self.databaseEvent(event = PacketPokerMonitorEvent.SEAT, param1 = serial, param2 = table_id)
        return status

    def movePlayer(self, serial, from_table_id, to_table_id):
        cursor = self.db.cursor()
        try:
            cursor.execute(
                "SELECT money FROM user2table " \
                "WHERE user_serial = %s " \
                "AND table_serial = %s",
                (serial,from_table_id)
            )
            if cursor.rowcount != 1:
                self.log.error("movePlayer(%d) expected one row got %d", serial, cursor.rowcount)
            (money,) = cursor.fetchone()

            if money > 0:
                sql = "UPDATE user2table " \
                    "SET table_serial = %s " \
                    "WHERE user_serial = %s " \
                    "AND table_serial = %s"
                params = (to_table_id,serial,from_table_id)
                
                for error_cnt in xrange(3):
                    try:
                        cursor.execute(sql, params)
                        break
                    except:
                        self.log.warn("ERROR: couldn't execute %r with params %r for %s times" % (sql, params,error_cnt))
                        if error_cnt >= 3:
                            raise

                self.log.debug("movePlayer: %s", cursor._executed)
                if cursor.rowcount != 1:
                    self.log.error("modified %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
                    money = -1

        finally:
            cursor.close()
        return money

    def leavePlayer(self, serial, table_id, currency_serial):
        status = True
        cursor = self.db.cursor()
        if currency_serial != '':
            sql = \
                "UPDATE user2money,user2table,pokertables " \
                "SET user2money.amount = user2money.amount + user2table.money + user2table.bet " \
                "WHERE user2money.user_serial = user2table.user_serial " \
                "AND user2money.currency_serial = pokertables.currency_serial " \
                "AND pokertables.serial = %s " \
                "AND user2table.table_serial = %s " \
                "AND user2table.user_serial = %s" 
            params = (table_id,table_id,serial)
            cursor.execute(sql,params)
            self.log.debug("leavePlayer %s" % cursor._executed)
            if cursor.rowcount > 1:
                self.log.error("modified %d rows (expected 0 or 1): %s", cursor.rowcount, sql)
                status = False
        
        sql = "DELETE FROM user2table WHERE user_serial = %s AND table_serial = %s"
        cursor.execute(sql,(serial,table_id))
        self.log.debug("leavePlayer %s", cursor._executed)
        if cursor.rowcount != 1:
            self.log.error("modified %d rows (expected 1): %s", cursor.rowcount, sql)
            status = False
        cursor.close()
        self.databaseEvent(event = PacketPokerMonitorEvent.LEAVE, param1 = serial, param2 = table_id)
        return status

    def updatePlayerRake(self, currency_serial, serial, amount):
        if amount == 0 or currency_serial == 0:
            return True
        status = True
        cursor = self.db.cursor()
        sql = "UPDATE user2money SET rake = rake + %s, points = points + %s WHERE user_serial = %s AND currency_serial = %s"
        params = (amount,amount,serial,currency_serial)
        cursor.execute(sql,params)
        self.log.debug("updatePlayerRake: %s", cursor._executed)
        if cursor.rowcount != 1:
            self.log.error("modified %d rows (expected 1): %s", cursor.rowcount, sql)
            status = False
        cursor.close()
        return status

    def updatePlayerMoney(self, serial, table_id, amount):
        if amount == 0:
            return True
        status = True
        cursor = self.db.cursor()
        sql = "UPDATE user2table SET money = money + %s, bet = bet - %s WHERE user_serial = %s AND table_serial = %s"
        params = (amount,amount,serial,table_id)
        cursor.execute(sql,params)
        self.log.debug("updatePlayerMoney: %s", cursor._executed)
        if cursor.rowcount != 1:
            self.log.error("modified %d rows (expected 1): %s", cursor.rowcount, sql)
            status = False
        cursor.close()

#         # HACK CHECK
#         cursor = self.db.cursor()
#         sql = ( "select sum(money), sum(bet) from user2table" )
#         cursor.execute(sql)
#         (money,bet) = cursor.fetchone()
#         if money + bet != 120000:
#             self.log.warn("BUG(4) %d", money + bet)
#             os.abort()
#         cursor.close()

#         cursor = self.db.cursor()
#         sql = ( "select user_serial,table_serial,money from user2table where money < 0" )
#         cursor.execute(sql)
#         if cursor.rowcount >= 1:
#             (user_serial, table_serial, money) = cursor.fetchone()
#             self.log.warn("BUG(11) %d/%d/%d", user_serial, table_serial, money)
#             os.abort()
#         cursor.close()
#         # END HACK CHECK

        return status

    def updateTableStats(self, game, observers, waiting):
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE pokertables " \
            "SET average_pot = %s, hands_per_hour = %s, percent_flop = %s, players = %s, observers = %s, waiting = %s " \
            "WHERE serial = %s ", (
                game.stats['average_pot'],
                game.stats['hands_per_hour'],
                game.stats['percent_flop'],
                game.allCount(),
                observers,
                waiting,
                game.id
            )
        )
        cursor.close()

    def destroyTable(self, table_id):

#         # HACK CHECK
#         cursor = self.db.cursor()
#         sql = ( "select * from user2table where money != 0 and bet != 0 and table_serial = " + str(table_id) )
#         cursor.execute(sql)
#         if cursor.rowcount != 0:
#             self.log.warn("BUG(10)")
#             os.abort()
#         cursor.close()
#         # END HACK CHECK

        cursor = self.db.cursor()
        cursor.execute("DELETE FROM user2table WHERE table_serial = %s", (table_id,))
        self.log.debug("destroy: %s", cursor._executed)
        cursor.execute("DELETE FROM route WHERE table_serial = %s", table_id)

#     def setRating(self, winners, serials):
#         url = self.settings.headerGet("/server/@rating")
#         if url == "":
#             return

#         params = []
#         for first in range(0, len(serials) - 1):
#             for second in range(first + 1, len(serials)):
#                 first_wins = serials[first] in winners
#                 second_wins = serials[second] in winners
#                 if first_wins or second_wins:
#                     param = "a=%d&b=%d&c=" % ( serials[first], serials[second] )
#                     if first_wins and second_wins:
#                         param += "2"
#                     elif first_wins:
#                         param += "0"
#                     else:
#                         param += "1"
#                     params.append(param)

#         params = join(params, '&')
#         self.log.debug("setRating: url = %s", url + params)
#         content = loadURL(url + params)
#         self.log.debug("setRating: %s", content)

    def resetBet(self, table_id):
        cursor = self.db.cursor()
        try:
            cursor.execute("UPDATE user2table SET bet = 0 WHERE table_serial = %s", (table_id,))
            self.log.debug("resetBet: %s", cursor._executed)
        finally:
            cursor.close()
        return True

    def getTable(self, game_id):
        return self.tables.get(game_id, False)

    def getTableBestByCriteria(self, serial, currency_serial = None, variant = None,
                               betting_structure = None, min_players = 0):
        """Return a PokerTable object optimal table based on the given
        criteria in list_table_query_str plus the amount of currency the
        user represented by serial has.  The caller is assured that the
        user represented by serial can afford at least the minimum buy-in
        for the table returned (if one is returned).

        Arguments in order:

             serial:            a user_serial for the user who
                                wants a good table.
            currency_serial:    must be a positive integer or None
            variant:            must be a string or None
            betting_structure:  must be a string or None
            min_players:        must be a positive integer or None

        General algorithm used by this method:

          First, a list of tables is requested from
          self.searchTables(currency_serial, variant, betting_structure,
          min_players).  If an empty list is returned by searchTables(),
          then None is returned here.

          Second, this method iterates across the tables returned from
          searchTables() and eliminates any tables for which the user
          represented by serial has less than the minimum buy-in, and
          those that have too many users sitting out such that the
          criteria from the user is effectively not met.

          If there are multiple tables found, the first one from the list
          coming from searchTables() that the user can buy into is
          returned.

          Methods of interest used by this method:

            self.getMoney() :       used to find out how much money the
                                    serial has.

            table.game.sitCount() : used to find out how many users are
                                    sitting out.

            table.game.all() :      used to shortcut away from tables that
                                    are full and should not be considered.
        """
        bestTable = None

        # money_results dict is used to store lookups made to self.getMoney()
        money_results = {}

        # A bit of a cheat, listTables() caches the value for min_players
        # when it does parsing so that we can use it here.
        for rr in self.searchTables(currency_serial, variant, betting_structure, min_players):
            table = self.getTable(rr['serial'])
            # Skip considering table entirely if it is full.
            if table.game.full():
                continue

            # Check to see that the players sitting out don't effecitvely
            # cause the user to fail to meet the number of players
            # criteria.
            if table.game.sitCount() < min_players:
                continue

            buy_in = table.game.buyIn()
            currency_serial = rr['currency_serial']
            if currency_serial not in money_results:
                money_results[currency_serial] = self.getMoney(serial, currency_serial)

            if money_results[currency_serial] > buy_in:
                # If the user can afford the buy_in, we've found a table for them!
                bestTable = table
                break

        return bestTable

    def createTable(self, owner, description):
        tourney_serial = description['tourney'].serial if 'tourney' in description else 0

        cursor = self.db.cursor()
        sql = "INSERT INTO pokertables ( resthost_serial, seats, player_timeout, muck_timeout, currency_serial, name, variant, betting_structure, skin, tourney_serial ) VALUES ( %s, %s, %s, %s, %s, %s, %s, %s, %s, %s )"
        params = (
            self.resthost_serial,
            description['seats'],
            description.get('player_timeout', 60),
            description.get('muck_timeout', 5),
            description['currency_serial'],
            description['name'],
            description['variant'],
            description['betting_structure'],
            description.get('skin', 'default'),
            tourney_serial
        )
        cursor.execute(sql,params)
        self.log.debug("createTable: %s", cursor._executed)
        if cursor.rowcount != 1:
            self.log.error("inserted %d rows (expected 1): %s", cursor.rowcount, cursor._executed)
            return None
            
        insert_id = cursor.lastrowid
        cursor.execute("REPLACE INTO route VALUES (%s,%s,%s,%s)", ( insert_id, tourney_serial, int(seconds()), self.resthost_serial))
        cursor.close()

        table = PokerTable(self, insert_id, description)
        table.owner = owner

        self.tables[insert_id] = table

        self.log.debug("table created : %s", table.game.name)

        return table

    def cleanupCrashedTables(self):
        for description in self.settings.headerGetProperties("/server/table"):
            self.cleanupCrashedTable("pokertables.name = %s" % self.db.literal(description['name']))
        self.cleanupCrashedTable("pokertables.resthost_serial = %s" % self.db.literal(self.resthost_serial))

    def cleanupCrashedTable(self, pokertables_where):
        cursor = self.db.cursor()

        sql = (
            "SELECT user_serial,table_serial,currency_serial " \
            "FROM pokertables,user2table " \
            "WHERE user2table.table_serial = pokertables.serial " \
            "AND " + pokertables_where
        )
        cursor.execute(sql)
        
        if cursor.rowcount > 0:
            self.log.debug(
                "cleanupCrashedTable found %d players on table %s",
                cursor.rowcount,
                pokertables_where
            )

            for i in xrange(cursor.rowcount):
                (user_serial, table_serial, currency_serial) = cursor.fetchone()
                self.leavePlayer(user_serial, table_serial, currency_serial)
                
        cursor.execute("DELETE FROM pokertables WHERE " + pokertables_where)

        cursor.close()

    def deleteTable(self, table):
        self.log.debug("table %s/%d removed from server", table.game.name, table.game.id)
        del self.tables[table.game.id]
        cursor = self.db.cursor()
        sql = ( "DELETE FROM pokertables where serial = " + str(table.game.id) )
        self.log.debug("deleteTable: %s", sql)
        cursor.execute(sql)
        if cursor.rowcount != 1:
            self.log.error("deleted %d rows (expected 1): %s", cursor.rowcount, sql)
        cursor.close()

    def broadcast(self, packet):
        for avatar in self.avatars:
            if hasattr(avatar, "protocol") and avatar.protocol:
                avatar.sendPacketVerbose(packet)
            else:
                self.log.debug("broadcast: avatar %s excluded" % str(avatar))

    def messageCheck(self):
        cursor = self.db.cursor()
        cursor.execute("SELECT serial,message FROM messages WHERE " +
                       "       sent = 'n' AND send_date < FROM_UNIXTIME(" + str(int(seconds())) + ")")
        rows = cursor.fetchall()
        for (serial, message) in rows:
            self.broadcast(PacketMessage(string = message))
            cursor.execute("UPDATE messages SET sent = 'y' WHERE serial = %d" % serial)
        cursor.close()
        self.cancelTimer('messages')
        delay = int(self.delays.get('messages', 60))
        self.timer['messages'] = reactor.callLater(delay, self.messageCheck)

    def chatMessageArchive(self, player_serial, game_id, message):
        cursor = self.db.cursor()
        cursor.execute("INSERT INTO chat_messages (player_serial, game_id, message) VALUES (%s, %s, %s)", (player_serial, game_id, message))

if HAS_OPENSSL:
    from twisted.internet.ssl import DefaultOpenSSLContextFactory
    
    class SSLContextFactory(DefaultOpenSSLContextFactory):
        def __init__(self, settings):
            self.pem_file = None
            for path in settings.headerGet("/server/path").split():
                if exists(path + "/poker.pem"):
                    self.pem_file = path + "/poker.pem"
            if self.pem_file is None:
                raise Exception("no poker.pem found in the setting's server path")
            DefaultOpenSSLContextFactory.__init__(self, self.pem_file, self.pem_file)
            

from twisted.web import resource, server

class PokerTree(resource.Resource):

    _log = log.get_child('PokerTree')

    def __init__(self, service):
        resource.Resource.__init__(self)
        self.service = service
        self.putChild("RPC2", PokerXMLRPC(self.service))
        try:
            self.putChild("SOAP", PokerSOAP(self.service))
        except:
            self._log.error("SOAP service not available")
        self.putChild("", self)

    def render_GET(self, request):
        return "Use /RPC2 or /SOAP"

components.registerAdapter(PokerTree, IPokerService, resource.IResource)

class PokerRestTree(resource.Resource):

    def __init__(self, service):
        resource.Resource.__init__(self)
        self.service = service
        self.putChild("POKER_REST", PokerResource(self.service))
        self.putChild("UPLOAD", PokerImageUpload(self.service))
        self.putChild("TOURNEY_START", PokerTourneyStartResource(self.service))
        self.putChild("AVATAR", PokerAvatarResource(self.service))
        self.putChild("", self)

    def render_GET(self, request):
        return "Use /POKER_REST or /UPLOAD or /AVATAR or /TOURNEY_START"

def _getRequestCookie(request):
    if request.cookies:
        return request.cookies[0]
    else:
        return request.getCookie('_'.join(['TWISTED_SESSION'] + request.sitepath))

#
# When connecting to the poker server with REST, SOAP or XMLRPC
# the client must chose to use sessions or not. If using session,
# the server will issue a cookie and keep track of it during
# (X == default twisted timeout) minutes.
#
# The session cookie is returned as a regular HTTP cookie and
# the library of the client in charge of the HTTP dialog should
# handle it transparently. To help the developer using a library
# that does a poor job at handling the cookies, it is also sent
# back as the "cookie" field of the PacketSerial packet in response
# to a successfull authentication request. This cookie may then
# be used to manually set the cookie header, for instance:
#
# Cookie: TWISTED_SESSION=a0bb35083c1ed3bef068d39bd29fad52; Path=/
#
# Because this cookie is only sent back in the SERIAL packet following
# an authentication request, it will not help clients observing the
# tables. These clients will have to find a way to properly handle the
# HTTP headers sent by the server.
#
# When the client sends a packet to the server using sessions, it must
# be prepared to receive the backlog of packets accumulated since the
# last request. For instance,
#
#   A client connects in REST session mode
#   The client sends POKER_TABLE_JOIN and the server replies with
#   packets describing the state of the table.
#   A player sitting at the table sends POKER_FOLD.
#   The server broadcasts the action to all players and observers.
#   Because the client does not maintain a persistent connection
#    and is in session mode, the server keeps the POKER_FOLD packet
#    for later.
#   The client sends PING to tell the server that it is still alive.
#   In response the server sends it the cached POKER_FOLD packet and
#    the client is informed of the action.
#
# The tests/test-webservice.py.in tests contain code that will help
# understand the usage of the REST, SOAP and XMLRPC protocols.
#
class PokerXML(resource.Resource):

    encoding = "UTF-8"

    log = log.get_child('PokerXML')

    def __init__(self, service):
        resource.Resource.__init__(self)
        self.service = service

    def sessionExpires(self, session):
        self.service.destroyAvatar(session.avatar)
        del session.avatar

    def render(self, request):
        self.log.debug("render %s", request.content.read())
        request.content.seek(0, 0)
        if self.encoding is not None:
            mimeType = 'text/xml; charset="%s"' % self.encoding
        else:
            mimeType = "text/xml"
        request.setHeader("Content-type", mimeType)
        args = self.getArguments(request)
        self.log.debug("args = %s", args)
        session = None
        use_sessions = args[0]
        args = args[1:]
        if use_sessions == "use sessions":
            self.log.debug("receive session cookie %s", _getRequestCookie(request))
            session = request.getSession()
            if not hasattr(session, "avatar"):
                session.avatar = self.service.createAvatar()
                session.notifyOnExpire(lambda: self.sessionExpires(session))
            avatar = session.avatar
        else:
            avatar = self.service.createAvatar()

        logout = False
        result_packets = []
        for packet in args2packets(args):
            if isinstance(packet, PacketError):
                result_packets.append(packet)
                break
            else:
                results = avatar.handlePacket(packet)
                if use_sessions == "use sessions" and len(results) > 1:
                    for result in results:
                        if isinstance(result, PacketSerial):
                            result.cookie = _getRequestCookie(request)
                            self.log.debug("send session cookie %s", result.cookie)
                            break
                result_packets.extend(results)
                if isinstance(packet, PacketLogout):
                    logout = True

        #
        # If the result is a single packet, it means the requested
        # action is using sessions (non session packet streams all
        # start with an auth packet). It may be a Deferred but may never
        # be a logout (because logout is not supposed to return a deferred).
        #
        if len(result_packets) == 1 and isinstance(result_packets[0], defer.Deferred):
            def renderLater(packet):
                result_maps = packets2maps([packet])
                result_string = self.maps2result(request, result_maps)
                request.setHeader("Content-length", str(len(result_string)))
                request.write(result_string)
                request.finish()
                return
            d = result_packets[0]
            d.addCallback(renderLater)
            return server.NOT_DONE_YET
        else:
            if use_sessions != "use sessions":
                self.service.destroyAvatar(avatar)
            elif use_sessions == "use sessions":
                if logout:
                    session.expire()
                    session.logout()
                else:
                    avatar.queuePackets()
            result_maps = packets2maps(result_packets)
            result_string = self.maps2result(request, result_maps)
            self.log.debug("result_string %s", result_string)
            request.setHeader("Content-length", str(len(result_string)))
            return result_string

    def getArguments(self, request):
        raise NotImplementedError("PokerXML is pure base class, subclass an implement getArguments")

    def maps2result(self, request, maps):
        raise NotImplementedError("PokerXML is pure base class, subclass an implement maps2result")

import xmlrpclib

class PokerXMLRPC(PokerXML):

    def getArguments(self, request):
        args = xmlrpclib.loads(request.content.read())[0]
        return args

    def maps2result(self, request, maps):
        return xmlrpclib.dumps((maps, ), methodresponse = 1)


try:
    import SOAPpy
    class PokerSOAP(PokerXML):
        def getArguments(self, request):
            data = request.content.read()
            p, _header, _body, _attrs = SOAPpy.parseSOAPRPC(data, 1, 1, 1)
            _methodName, args, kwargs, _ns = p._name, p._aslist, p._asdict, p._ns
            # deal with changes in SOAPpy 0.11
            if callable(args):
                args = args()
            if callable(kwargs):
                kwargs = kwargs()
            return SOAPpy.simplify(args)

        def maps2result(self, request, maps):
            return SOAPpy.buildSOAP(
                kw = {'Result': maps},
                method = 'returnPacket',
                encoding = self.encoding
            )
except:
    log.error("Python SOAP module not available")
