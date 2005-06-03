#
# Copyright (C) 2004, 2005 Mekensleep
#
# Mekensleep
# 24 rue vieille du temple
# 75004 Paris
#       licensing@mekensleep.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#  Loic Dachary <loic@gnu.org>
#  Henry Precheur <henry@precheur.org>
#
#
from twisted.internet.protocol import Protocol, Factory
from twisted.internet import reactor
from twisted.python import dispatch
from string import split, join, rstrip
from time import time, strftime

INTERFACE_READY = "//event/poker3d/pokerinterface/ready"
INTERFACE_GONE = "//event/poker3d/pokerinterface/gone"

INTERFACE_LOGIN = "//event/poker3d/pokerinterface/login"
INTERFACE_LOBBY = "//event/poker3d/pokerinterface/lobby"
INTERFACE_HANDS = "//event/poker3d/pokerinterface/hands"
INTERFACE_SHOW_OUTFITS = "//event/poker3d/pokerinterface/show_outfits"
INTERFACE_OUTFITS_SEX = "//event/poker3d/pokerinterface/outfits_sex"
INTERFACE_OUTFITS_SLOT_TYPE = "//event/poker3d/pokerinterface/outfits_slot_type"
INTERFACE_OUTFITS_SLOT = "//event/poker3d/pokerinterface/outfits_slot"
INTERFACE_OUTFITS_PARAMETER = "//event/poker3d/pokerinterface/outfits_parameter"
INTERFACE_OUTFITS_RANDOM = "//event/poker3d/pokerinterface/outfits_random"
INTERFACE_OUTFITS = "//event/poker3d/pokerinterface/outfits"
INTERFACE_YESNO = "//event/poker3d/pokerinterface/yesno"
INTERFACE_MESSAGE_BOX = "//event/poker3d/pokerinterface/message_box"
INTERFACE_CHOOSER = "//event/poker3d/pokerinterface/chooser"
INTERFACE_POST_BLIND = "//event/poker3d/pokerinterface/post_blind"
INTERFACE_AUTO_BLIND = "//event/poker3d/pokerinterface/auto_blind"
INTERFACE_SIT_OUT = "//event/poker3d/pokerinterface/sit_out"
INTERFACE_BUY_IN = "//event/poker3d/pokerinterface/buy_in"
INTERFACE_CASHIER = "//event/poker3d/pokerinterface/cashier"
INTERFACE_CHAT_HISTORY = "//event/poker3d/pokerinterface/chat_history"
INTERFACE_CHAT_LINE = "//event/poker3d/pokerinterface/chat_line"
INTERFACE_MENU = "//event/poker3d/pokerinterface/menu"
INTERFACE_TOURNAMENTS = "//event/poker3d/pokerinterface/tournaments"

class PokerInterface(dispatch.EventDispatcher):
    def __init__(self):
        dispatch.EventDispatcher.__init__(self)

    def event(self, *data):
        if self.verbose > 2: print "PokerInterface::event: " + str(data)
        while data:
            type = data[0]
            if type == "login":
                data = self.handleLogin(data[1:])
            elif type == "lobby":
                data = self.handleLobby(data[1:])
            elif type == "yesno":
                data = self.handleYesNo(data[1:])
            elif type == "hand_history":
                data = self.handleHands(data[1:])
            elif type == "blind":
                data = self.handleBlind(data[1:])
            elif type == "sit_actions":
                data = self.handleSitActions(data[1:])
            elif type == "message_box":
                data = self.handleMessageBox(data[1:])
            elif type == "chooser":
                data = self.handleChooser(data[1:])
            elif type == "buy_in":
                data = self.handleBuyIn(data[1:])
            elif type == "chat":
                data = self.handleChat(data[1:])
            elif type == "outfit":
                data = self.handleOutfit(data[1:])
            elif type == "cashier":
                data = self.handleCashier(data[1:])
            elif type == "tournaments":
                data = self.handleTournaments(data[1:])
            elif type == "menu":
                data = self.handleMenu(data[1:])
            else:
                print "PokerInterfaceProtocol: unexpected type %s " % type
                data = data[1:]

    def command(self, *args):
        print "*ERROR* command not implemented"
        
    def handleLogin(self, data):
        (ok_or_cancel, name, password, remember) = data[:4]
        remember = remember == "1"
        if self.verbose > 1:
            print "PokerInterfaceProtocol: login %s, password %s, remember %s\n" % (name, password, remember)
        self.publishEvent(INTERFACE_LOGIN, ok_or_cancel, name, password, remember)
        self.clearCallbacks(INTERFACE_LOGIN)
        return data[4:]

    def requestLogin(self, name, password, remember):
        remember = remember and "1" or "0"
        packet = ("login", name, password, remember)
        if self.verbose > 1:
            print "PokerInterfaceProtocol:requestLogin" + str(packet)
        self.command(*packet)

    def hideLogin(self):
        self.command("login", "hide")

    def handleTournaments(self, data):
        self.publishEvent(INTERFACE_TOURNAMENTS, data[:3])
        return data[3:]
    
    def updateTournamentsPlayersList(self, can_register, players):
        packet = [ "tournaments", "players", (can_register and "1" or "0"), str(len(players)) ]
        if len(players) > 0:
            packet.extend(map(lambda player: player[0], players))
        self.command(*packet)

    def updateTournaments(self, tournaments = []):
        sit_n_go = filter(lambda tournament: tournament.sit_n_go == 'y', tournaments)
        if sit_n_go:
            packet = ['tournaments', 'sit_n_go', str(len(sit_n_go)) ]
            for tournament in sit_n_go:
                players = str(tournament.players_quota)
                packet.extend((str(tournament.serial), tournament.description_short, tournament.state, players))
            self.command(*packet)
            
        regular = filter(lambda tournament: tournament.sit_n_go == 'n', tournaments)
        if regular:
            packet = ['tournaments', 'sit_n_go', str(len(regular)) ]
            for tournament in regular:
                players = str(tournament.players_quota)
                packet.extend((str(tournament.serial), strftime("%Y/%m/%d %H:%M", tournament.start_time), tournament.description_short, tournament.state, players))
            self.command(*packet)
            
    def showTournaments(self, page, real_money):
        self.command("tournaments", "show", page, real_money)
                
    def hideTournaments(self):
        self.command("tournaments", "hide")
        
    def handleLobby(self, data):
        self.publishEvent(INTERFACE_LOBBY, data[:2])
        return data[2:]
    
    def updateLobbyPlayersList(self, players):
        packet = [ "lobby", "players", str(len(players)) ]
        if len(players) > 0:
            map(lambda player: packet.extend(map(lambda value: str(value), player)), players)
        self.command(*packet)

    def updateLobby(self, file2name, tables = []):
        variant2tables = {}
        for table in tables:
            if not variant2tables.has_key(table.variant):
                variant2tables[table.variant] = []
            info = ( str(table.id),
                     table.name,
                     file2name(table.betting_structure),
                     str(table.seats),
                     str(table.average_pot),
                     str(table.hands_per_hour),
                     str(table.percent_flop),
                     str(table.players),
                     str(table.observers),
                     str(table.waiting),
                     str(table.timeout) )
            variant2tables[table.variant].append(info)

        for (variant, tables) in variant2tables.iteritems():
            packet = ['lobby', variant, str(len(tables)) ]
            for table in tables:
                packet.extend(table)
            self.command(*packet)
            
    def showLobby(self, page, real_money):
        self.command("lobby", "show", page, real_money)
                
    def hideLobby(self):
        self.command("lobby", "hide")
        
    def hideOutfits(self):
        self.command("outfit", "hide")
        
    def showOutfits(self, sex, slot_number, slot_type, slot, outfit):
        self.command("outfit", "show")
        packet = [ "outfit", "set", sex, str(slot_number) ]
        slot_value_index = slot.keys().index(outfit['NAME'])
        packet.extend((slot_type, "0" ,str(len(slot)), str(slot_value_index)))
        nparams = min(len(outfit['VALUES']), 4)

        key = 'global_skin_hue/hue_set'
        if slot_type != 'head' and outfit['VALUES'].has_key(key) :
            nparams = nparams - 1

        packet.append(str(nparams))
        count = 0
        for (xpath, value) in outfit['VALUES'].iteritems():
            if slot_type != 'head' and xpath == key:
                continue

            if count >= nparams: break
            count += 1
            definition = outfit['DEFINITIONS'][xpath]
            print "vlaue %s " % value['value']
            print "definition %s" % definition['ids']
            print "slot_type %s" % slot_type
            index = definition['ids'].index(value['value'])
            packet.extend((definition['text'], xpath, "0", str(len(definition['ids'])), str(index)))
            packet.append(definition['preview_type'])
            packet.append(str(len(definition['preview'])))
            packet.extend(definition['preview'])
        if self.verbose > 1:
            print "PokerInterfaceProtocol:showOutfits " + str(packet)
        self.command(*packet)

    def handleOutfit(self, data):
        what = data[0]
        if what == "ok":
            self.publishEvent(INTERFACE_OUTFITS, "ok")
            return data[1:]
        elif what == "sex":
            self.publishEvent(INTERFACE_OUTFITS_SEX, data[1])
            return data[2:]
        elif what == "slot_type":
            self.publishEvent(INTERFACE_OUTFITS_SLOT_TYPE, data[1], data[2])
            return data[3:]
        elif what == "slot":
            self.publishEvent(INTERFACE_OUTFITS_SLOT, int(data[1]))
            return data[2:]
        elif what == "parameter":
            self.publishEvent(INTERFACE_OUTFITS_PARAMETER, data[1], data[2])
            return data[3:]
        elif what == "random":
            self.publishEvent(INTERFACE_OUTFITS_RANDOM)
            return data[1:]
        else:
            print "*CRITICAL* unknown outfit message type %s" % what

    def showHands(self, hands, start, count, total):
        self.hands = hands
        packet = [ "hand_history", "show", str(start), str(count), str(total), str(len(hands)) ]
        for hand in hands:
            packet.append("#%d" % hand)
        if self.verbose > 1:
            print "PokerInterfaceProtocol:showHands " + str(packet)
        self.command(*packet)

    def showHandMessages(self, hand_serial, messages):
        subject = messages[0]
        messages = messages[1]
        self.command("hand_history", "messages", str(hand_serial), subject + "\n" + "\n".join(messages))
        
    def hideHands(self):
        self.command("hand_history", "hide")

    def handleHands(self, data):
        tag = data[0]
        if tag == "quit" or tag == "next" or tag == "previous":
            self.publishEvent(INTERFACE_HANDS, tag)
            return data[1:]
        elif tag == "show":
            self.publishEvent(INTERFACE_HANDS, "show", int(data[1][1:]))
            return data[2:]
        else:
            print "*CRITICAL* unknown tag " + tag
            return data[1:]

    def chooser(self, title, alternatives):
        packet = [ "chooser", title, str(len(alternatives)) ]
        packet.extend(alternatives)
        if self.verbose > 1:
            print "PokerInterfaceProtocol:chooser %s : %s" % ( str(alternatives), packet )
        self.command(*packet)

    def handleChooser(self, data):
        (alternative,) = data[:1]
        if self.verbose > 1:
            print "PokerInterfaceProtocol:chooser"
        if self.callbacks.has_key(INTERFACE_CHOOSER):
            self.publishEvent(INTERFACE_CHOOSER, alternative)
            self.clearCallbacks(INTERFACE_CHOOSER)
        return data[1:]
        
    def messageBox(self, message):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:messageBox %s" % message
        self.command("message_box", message)

    def handleMessageBox(self, data):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:handleMessageBox"
        if self.callbacks.has_key(INTERFACE_MESSAGE_BOX):
            self.publishEvent(INTERFACE_MESSAGE_BOX)
            self.clearCallbacks(INTERFACE_MESSAGE_BOX)
            
    def blindShow(self):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:blind show"
        self.command("blind", "show")

    def blindHide(self):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:blind hide"
        self.command("blind", "hide")

    def blindMessage(self, message, wait_blind):
        self.blindShow()
        packet = [ "blind", "blind message", message, wait_blind ]
        if self.verbose > 1:
            print "PokerInterfaceProtocol:blindMessage " + str(packet)
        self.command(*packet)

    def handleBlind(self, data):
        if data[0] == "post":
            (tag, answer) = data[:2]
            self.publishEvent(INTERFACE_POST_BLIND, answer)
            self.clearCallbacks(INTERFACE_POST_BLIND)
            data = data[2:]
        else:
            raise Exception("bad packet received from blind")
        return data

    def sitActionsShow(self):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:sitActions show"
        self.command("sit_actions", "show")

    def sitActionsHide(self):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:sitActions hide"
        self.command("sit_actions", "hide")

    def sitActionsAuto(self, auto):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:sitActions auto"
        self.command("sit_actions", "auto", str(auto))

    def sitActionsSitOut(self, status, message):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:sitActions sit_out"
        self.command("sit_actions", "sit_out", status, message)

    def handleSitActions(self, data):
        if data[0] == "auto":
            answer = data[1]
            self.publishEvent(INTERFACE_AUTO_BLIND, answer == "yes")
        elif data[0] == "sit_out":
            answer = data[1]
            self.publishEvent(INTERFACE_SIT_OUT, answer == "yes")
        else:
            raise Exception("bad packet received from sit_actions")
        return data[2:]

    def yesnoBox(self, message):
        if self.verbose > 1:
            print "PokerInterfaceProtocol:yesnoBox %s" % message
        self.clearCallbacks(INTERFACE_YESNO)
        self.command("yesno", message)

    def handleYesNo(self, data):
        response = data[0]
        if response == "yes":
            result = True
        elif response == "no":
            result = False
        else:
            raise Exception("bad packet recieved from lobby")
        self.publishEvent(INTERFACE_YESNO, result)
        self.clearCallbacks(INTERFACE_YESNO)
        return data[1:]

    def chatShow(self):
        self.command("chat", "show")
        
    def chatHide(self):
        self.command("chat", "hide")

    def chatHistory(self, message):
        self.command("chat", "line", message)
        
    def handleChat(self, data):
        if data[0] == "history":
            self.publishEvent(INTERFACE_CHAT_HISTORY, data[1])
        elif data[0] == "line":
            self.publishEvent(INTERFACE_CHAT_LINE, data[1])
        return data[2:]

    def buyInShow(self):
        self.command("buy_in", "show")
        
    def buyInHide(self):
        self.command("buy_in", "hide")
        
    def buyInParams(self, minimum, maximum, legend, max_label):
        packet = [ "buy_in", "params", str(minimum), str(maximum), legend, max_label ]
        if self.verbose > 1:
            print "PokerInterfaceProtocol:requestBuyIn " + str(packet)
        self.command(*packet)
        
    def handleBuyIn(self, data):
        print "handleBuyIn: " + str(data)
        value = data[0]
        self.publishEvent(INTERFACE_BUY_IN, value)
        self.clearCallbacks(INTERFACE_BUY_IN)
        return data[1:]

    def updateCashier(self, *messages):
        packet = [ "cashier", "update", str(len(messages)) ]
        packet.extend(messages)
        if self.verbose > 1:
            print "PokerInterfaceProtocol:updateCashier " + str(packet)
        self.command(*packet)
 
    def handleCashier(self, data):
        self.publishEvent(INTERFACE_CASHIER, data[0])
        return data[1:]

    def showCashier(self):
        self.command("cashier", "show", "0")
        
    def hideCashier(self):
        self.command("cashier", "hide", "0")
        
    def updateMenu(self, settings):
        screen = settings.headerGetProperties("/settings/screen")[0]
        sound = settings.headerGet("/settings/sound")
        shadow = settings.headerGet("/settings/shadow")
        shaders = settings.headerGet("/settings/vprogram")
        if shadow == "yes" and shaders == "yes":
            graphics = "yes"
        else:
            graphics = "no"
        auto_post = settings.headerGet("/settings/auto_post")
        remember_me = settings.headerGet("/settings/remember")
        muck = settings.headerGet("/settings/muck")
        display = settings.headerGet("/settings/@display3d") == "yes" and "3d" or "2d"
        packet = []
        packet.extend(("menu", "set", "resolution", screen["width"] + "x" + screen["height"]))
        packet.extend(("menu", "set", "graphics", graphics))
        packet.extend(("menu", "set", "sound", sound))
        packet.extend(("menu", "set", "display", display))
        packet.extend(("menu", "set", "fullscreen", screen["fullscreen"]))
        packet.extend(("menu", "set", "auto_post", auto_post))
        packet.extend(("menu", "set", "remember_me", remember_me))
        packet.extend(("menu", "set", "muck", muck))
        print "updateMenu: " + str(packet)
        self.command(*packet)
        
    def showMenu(self):
        self.command("menu", "show")
        
    def hideMenu(self):
        self.command("menu", "hide")
        
    def handleMenu(self, data):
        self.publishEvent(INTERFACE_MENU, data[0], data[1])
        return data[2:]

    def clearCallbacks(self, *events):
        for event in events:
            if self.callbacks.has_key(event):
                del self.callbacks[event]

class PokerInterfaceProtocol(Protocol, PokerInterface):
    def __init__(self):
        PokerInterface.__init__(self)

    def connectionMade(self):
        self.factory.publishEvent(INTERFACE_READY, self, self.factory)
        self.factory.clearCallbacks(INTERFACE_READY)

    def connectionLost(self, reason):
        self.factory.publishEvent(INTERFACE_GONE, self, self.factory)        
        self.factory.clearCallbacks(INTERFACE_GONE)
        
    def dataReceived(self, data):
        if self.verbose > 1:
            print "PokerInterfaceProtocol: dataReceived %s " % data
        args = split(rstrip(data, "\0"), "\0")
        self.event(*args)

    def command(self, *args):
        if self.factory.verbose > 2: print "PokerInterfaceProtocol.command " + str(args)
        self.transport.write("\000".join(args) + "\000")

class PokerInterfaceFactory(Factory, dispatch.EventDispatcher):

    protocol = PokerInterfaceProtocol
    
    def __init__(self, *args, **kwargs):
        dispatch.EventDispatcher.__init__(self)
        self.verbose = kwargs["verbose"]
        
    def buildProtocol(self, addr):
        protocol = Factory.buildProtocol(self, addr)
        protocol.verbose = self.verbose
        return protocol

    def clearCallbacks(self, *events):
        for event in events:
            if self.callbacks.has_key(event):
                del self.callbacks[event]
