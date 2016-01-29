#!/usr/bin/python2

## CABS_Server.py
# This is the webserver that is at the center of the CABS system.
# It is asynchronous, and as such the callbacks and function flow can be a bit confusing
# The basic idea is that the HandleAgentFactory and HandleClienFactory make new HandleAgents and Handle Clients
# There is one per connection, and it processes all communication on that connection, without blocking

from twisted import internet
from twisted.internet.protocol import Factory, Protocol
from twisted.internet import reactor, endpoints, defer, task
from twisted.protocols.basic import LineOnlyReceiver
from twisted.protocols.policies import TimeoutMixin
from twisted.enterprise import adbapi
from twisted.names import client
from twisted.python import log

from MySQLdb import IntegrityError
import ldap
import sys
import logging
import random
import os
import socket
import ssl

from time import sleep

blacklist = set()
logger=logging.getLogger()

random.seed()

settings = {"Max_Clients":'62',
            "Max_Agents":'120',
            "Client_Port":'18181',
            "Agent_Port":'18182',
            "Command_Port":'18183',
            "Agent_Command_Port":'18185',
            "Use_Agents":'True',
            "Database_Addr":"127.0.0.1",
            "Database_Port":3306,
            "Database_Usr":"CABS",
            "Database_Pass":"BACS",
            "Database_Name":"test",
            "Reserve_Time":'360',
            "Timeout_Time":'540',
            "Use_Blacklist":'False',
            "Auto_Blacklist":'False',
            "Auto_Max":'300',
            "Auth_Server":'None',
            "Auth_Prefix":'',
            "Auth_Postfix":'',
            "Auth_Base":'None',
            "Auth_Usr_Attr":'None',
            "Auth_Grp_Attr":'None',
            "Auth_Secure":'False',
            "Cert_Dir":"/usr/share/cabsbroker/",
            "Auth_Cert":None,
            "Broker_Priv_Key":None,
            "Broker_Cert":None,
            "Agent_Cert":None,
            "RGS_Ver_Min":'False',
            "Verbose_Out":'False',
            "Log_Amount":'4',
            "Log_Keep":'500',
            "Log_Time":'1800',
            "One_Connection":'True',
            "Trusted_Clients":None }

## Handles each Agent connection
class HandleAgent(LineOnlyReceiver, TimeoutMixin):
    def __init__(self, factory):
        self.factory = factory
        #timeout after 9 seconds
        self.setTimeout(9)

    def connectionMade(self):
        self.agentAddr = self.transport.getPeer()
        self.factory.numConnections = self.factory.numConnections + 1

    def connectionLost(self, reason):
        self.factory.numConnections = self.factory.numConnections - 1

    def lineLengthExceeded(self, line):
        self.transport.abortConnection()

    def lineReceived(self, line):
        #types of reports = status report (sr) and status process report (spr)
        report = line.split(':')
        if report[0] == 'sr' or report[0] == 'spr':
            status = None
            if report[0] == 'spr':
                status = report.pop(1)
                if status.endswith('-1'):
                    status = status.rstrip('-1') + ' : Unknown'
                elif status.endswith('0'):
                    status = status.rstrip('0') + ' : Not Found'
                elif status.endswith('1'):
                    status = status.rstrip('1') + ' : Not Running'
                elif status.endswith('2'):
                    status = status.rstrip('2') + ' : Not Connected'
                elif status.endswith('3'):
                    status = status.rstrip('3') + ' : Okay'

            #Mark the machine as active, and update timestamp
            querystring = "UPDATE machines SET active = True, last_heartbeat = NOW(), " + \
                    "status = %s WHERE machine = %s"
            r1 = dbpool.runQuery(querystring, (status, report[1]))
            #confirm any users that reserved the machine if they are there, or unconfirm them if they are not
            #For now we don't support assigning multiple users per machine, so only one should be on at a time
            #but, if we do have multiple, let it be so
            #Try to write an entry under the first listed users name, if
            #duplicate machine update the old entry to confirmed
            users = ''
            if len(report) > 2:
                for item in range(2, len(report)):
                    users += report[item] + ', '
                users = users[0:-2]
                logger.debug("Machine {0} reports user {1}".format(report[1],users))
                regexstr = ''
                for item in range(2, len(report)):
                    regexstr += '(^'
                    regexstr += report[item]
                    regexstr += '$)|'
                regexstr = regexstr[0:-1]
                if settings.get("One_Connection") == 'True' or settings.get("One_Connection") == True:
                    querystring = "INSERT INTO current VALUES (%s, NULL, %s, True, NOW()) ON DUPLICATE " + \
                                    "KEY UPDATE confirmed = True, connecttime = Now(), user = %s"
                    r2 = dbpool.runQuery(querystring,(report[2],report[1],users))
                querystring = "UPDATE current SET confirmed = True, connecttime = Now() " + \
                                "WHERE (machine = %s AND user REGEXP %s)"
                r2 = dbpool.runQuery(querystring,(report[1],regexstr))
            else:
                querystring = "DELETE FROM current WHERE machine = %s"
                r2 = dbpool.runQuery(querystring, (report[1],))

## Creates a HandleAgent for each connection
class HandleAgentFactory(Factory):
    def __init__(self):
        self.numConnections = 0

    def buildProtocol(self, addr):
        #Blacklist check here
        if addr.host in blacklist:
            logger.debug("Blacklisted address {0} tried to connect".format(addr.host))
            protocol = DoNothing()
            protocol.factory = self
            return protocol

        #limit connection number here
        if (settings.get("Max_Agents") is not None and settings.get("Max_Agents") != 'None') and (int(self.numConnections) >= int(settings.get("Max_Agents"))):
            logger.warning("Reached maximum Agent connections")
            protocol = DoNothing()
            protocol.factory = self
            return protocol
        return HandleAgent(self)

## Handles each Client Connection
class HandleClient(LineOnlyReceiver, TimeoutMixin):
    def __init__(self, factory):
        self.factory = factory
        self.setTimeout(9)#set timeout of 9 seconds

    def connectionMade(self):
        #if auto then add to blacklist
        self.clientAddr = self.transport.getPeer()
        self.factory.numConnections = self.factory.numConnections + 1

    def connectionLost(self, reason):
        self.factory.numConnections = self.factory.numConnections - 1

    def lineLengthExceeded(self, line):
        self.transport.abortConnection()

    def lineReceived(self, line):
        #We can receieve 2 types of lines from a client, pool request (pr), machine request(mr)
        request = line.split(':')
        user = request[1]
        password = request[2]
        try:
            self.groups = self.user_groups(user, password)
        except ldap.INVALID_CREDENTIALS:
            self.send_error("Invalid credentials")
            return
        except Exception as e:
            logger.error(e)
            self.send_error("Unknown error")
            return
        if request[0].startswith('pr'):
            self.pool_request(*request[1:])
        elif request[0] == 'mr':
            self.machine_request(*request[1:])

    @defer.inlineCallbacks
    def pool_request(self, user, password, version=None):
        if version is not None and settings.get('RGS_Ver_Min') != 'False':
            #check version
            if version < settings.get('RGS_Ver_Min'):
                self.transport.write("Err:Sorry, your RGS reciever is out of date, " + \
                        "it should be at least {0}".format(settings.get('RGS_Ver_Min')))
                self.transport.loseConnection()
                return
        pools = yield self.available_pools(user, password)
        self.writePools(pools)

    @defer.inlineCallbacks
    def machine_request(self, user, password, pool):
        pools = yield self.available_pools(user, password)
        self.userPools = [p for p, description in pools]

        if pool not in self.userPools:
            self.send_error("{0} does not have access to pool \"{1}\"".format(user, pool))
            raise StopIteration
        prevMachine = yield self.prev_machine(user, pool)
        if prevMachine is not None:
            logger.info("Restored {0} to {1}".format(prevMachine, user))
            self.send_machine(prevMachine)
        else:
            openMachines = yield self.open_machines(pool)
            if len(openMachines) == 0:
                self.send_error("Sorry, There are no open machines in {0}.".format(pool))
                raise StopIteration
            machine = yield self.tryReserve(user, pool, openMachines)
            if machine is None:
                # The chances of this happening are extremely small (see tryReserve).
                self.send_error("RETRY")
                raise StopIteration
            logger.info("Gave {0} to {1}".format(machine, user))
            self.send_machine(machine)

    def send_error(self, message):
        self.sendLine("Err:" + message)
        self.transport.loseConnection()

    def send_machine(self, machine):
        self.sendLine(machine)
        self.transport.loseConnection()

    @defer.inlineCallbacks
    def prev_machine(self, user, requestedpool):
        #only find a previous machine if it had been in the same pool, and confirmed
        querystring = "SELECT machine FROM current WHERE (user = %s AND name = %s AND confirmed = True)"
        prevMachine = yield dbpool.runQuery(querystring, (user, requestedpool))
        if len(prevMachine) == 0:
            defer.returnValue(None)
        else:
            defer.returnValue(prevMachine[0][0])



    ## Sends the SQL request to reserve the machine for the user
    def reserveMachine(self, user, pool, machine):
        opstring = "INSERT INTO current VALUES (%s, %s, %s, False, CURRENT_TIMESTAMP)"
        return dbpool.runQuery(opstring, (user, pool, machine))

    @defer.inlineCallbacks
    def tryReserve(self, user, pool, machines):
        # If a user reserves a machine but that machine sends a heartbeat before the user logs in, the
        # machine will be unreserved. If another user requests a machine before the first user logs in,
        # the broker might reserve the same machine for the second user. Trying to reserve machines in
        # random order lessens the chance that this will happen.
        random.shuffle(machines)
        for machine in machines:
            try:
                yield self.reserveMachine(user, pool, machine)
            except IntegrityError:
                continue
            defer.returnValue(machine)
        defer.returnValue(None)

    ## Sends the list of pools accesable to the user
    def writePools(self, listpools):
        for item in listpools:
            self.transport.write(str(item))
            self.transport.write("\n")
        self.transport.loseConnection()

    def user_groups(self, user, password):
        groups = []

        Server = settings.get("Auth_Server")
        if Server.startswith("AUTO"):
            raise ReferenceError("No AUTO Authentication Server yet")
        if not Server.startswith("ldap"):
            Server = "ldap://" + Server
        DN = settings.get("Auth_Prefix") + user + settings.get("Auth_Postfix")
        Base = settings.get("Auth_Base")
        Scope = ldap.SCOPE_SUBTREE
        Attrs = [ settings.get("Auth_Grp_Attr") ]
        UsrAttr = settings.get("Auth_Usr_Attr")

        if settings.get("Auth_Secure") == 'True':
            if settings.get("Auth_Cert") != 'None' and settings.get("Auth_Cert") is not None:
                ldap.set_option(ldap.OPT_X_TLS_CACERTFILE, settings.get("Auth_Cert"))
                ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
            else:
                ldap.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
        l = ldap.initialize(Server)
        l.set_option(ldap.OPT_REFERRALS,0)
        l.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
        if settings.get("Auth_Secure") == 'True':
            l.start_tls_s()
        l.bind_s(DN, password, ldap.AUTH_SIMPLE)
        r = l.search(Base, Scope, UsrAttr + '=' + user, Attrs)
        result = l.result(r,9)
        # TODO: remove or improve this try block. unbind() will throw ldap.LDAPError if it's called twice;
        # maybe that's why it's here. Would that ever happen here?
        try:
            l.unbind()
        except:
            pass

        #get user groups
        AttrsDict = result[1][0][1]
        for key in AttrsDict:
            for x in AttrsDict[key]:
                #take only the substring after the first =, and before the comma
                groups.append(x[x.find('=')+1:x.find(',')])

        return groups
    
    def available_pools(self, user, password):
        query = "SELECT name, description FROM pools WHERE deactivated = False AND (groups IS NULL"
        if len(self.groups) > 0:
            query += " OR groups REGEXP %s)"
            regexstring = "|".join("(.*{0}.*)".format(group) for group in self.groups)
            return dbpool.runQuery(query, (regexstring,))
        else:
            query += ")"
            return dbpool.runQuery(query)

    ## Runs the SQL to get machines the user could use
    @defer.inlineCallbacks
    def open_machines(self, requestedPool):
        poolRegex = lambda pools : "|".join("(^{}$)".format(pool) for pool in pools)
        query = "SELECT machine FROM machines WHERE name REGEXP %s AND machine NOT IN " + \
                "(SELECT machine FROM current) AND active = True AND status LIKE '%%Okay' AND " + \
                "deactivated = False"
        result = yield dbpool.runQuery(query, [poolRegex([requestedPool])])
        if len(result) == 0:
            secondaries = yield dbpool.runQuery("SELECT secondary FROM pools WHERE name = %s",
                                                [requestedPool])
            if secondaries[0][0] is not None:
                pools = [requestedPool] + [pool for pool in secondaries[0][0].split(',')
                                           if pool in self.userPools]
                result = yield dbpool.runQuery(query, [poolRegex(pools)])
        machines = [record[0] for record in result]
        defer.returnValue(machines)



## A place to direct blacklisted addresses, or if we have too many connections at once
class DoNothing(Protocol):
    def makeConnection(self, transport):
        transport.abortConnection()

## creates a HandleClient for each Client connection
class HandleClientFactory(Factory):
    def __init__(self):
        self.numConnections = 0
        self.transports = []

    def buildProtocol(self, addr):
        #Blacklist check here
        if addr.host in blacklist:
            logger.debug("Blacklisted address {0} tried to connect".format(addr.host))
            protocol = DoNothing()
            protocol.factory = self
            return protocol

        #limit connection number here
        if (settings.get("Max_Clients") is not None and settings.get("Max_Clients") != 'None') and (int(self.numConnections) >= int(settings.get("Max_Clients"))):
            logger.warning("Reached maximum Client connections")
            protocol = DoNothing()
            protocol.factory = self
            return protocol
        return HandleClient(self)

class CommandHandler(LineOnlyReceiver):
    """Recognized commands:
    query[:verbose]
    tell_agent:(restart|reboot):<hostname>
    autoit:(enable|disable):<hostname>
    """
    def lineReceived(self, line):
        segments = line.split(':')
        command = segments[0]
        args = segments[1:]
        print "received command: " + command
        if command == "query":
            verbose = len(args) >= 1 and args[0] == 'verbose'
            response = dbpool.runQuery(
                    "SELECT machines.name, machines.machine, status, user, deactivated, reason " + \
                    "FROM machines LEFT OUTER JOIN current ON machines.machine = current.machine")
            response.addCallback(self.respond_query, verbose)
        elif command == "tell_agent":
            if len(args) != 2:
                self.bad_command(line)
                return
            self.tell_agent(*args)
        elif command == "autoit":
            if len(args) != 2:
                self.bad_command(line)
                return
            self.autoit(*args)

    @defer.inlineCallbacks
    def autoit(self, action, hostname):
        result = yield dbpool.runQuery("SELECT deactivated,reason FROM machines WHERE machine = %s", (hostname,))
        if len(result) == 0:
            self.transport.write("unknown machine: " + hostname)
            self.tranport.loseConnection()
            raise StopIteration
        deactivated, reason = result[0]
        if action == "enable" and reason == "autoit":
            dbpool.runQuery("UPDATE machines SET deactivated = FALSE, reason = NULL WHERE machine = %s",
                            (hostname,))
        elif action == "disable" and not deactivated:
            dbpool.runQuery("UPDATE machines SET deactivated = TRUE, reason = 'autoit' WHERE machine = %s",
                            (hostname,))
        else:
            self.bad_command(":".join(("autoit", action, hostname)))

    def bad_command(self, command):
        self.transport.write("unrecognized command: " + command)
        self.transport.loseConnection()

    def tell_agent(self, command, hostname):
        port = int(settings["Agent_Command_Port"])
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        print "sending a command to {} on port {}".format(hostname, port)
        try:
            s.connect((hostname, port))
        except socket.error:
            print "couldn't send command to agent"
            self.transport.write("Couldn't connect to {} on port {}.".format(hostname, port))
            self.transport.loseConnection()
            return
        if settings.get("Broker_Cert") is None:
            s_wrapped = s 
        else:
            ssl_cert = settings.get("Agent_Cert")
            s_wrapped = ssl.wrap_socket(s, certfile=settings["Broker_Cert"],
                    keyfile=settings["Broker_Priv_Key"], cert_reqs=ssl.CERT_REQUIRED,
                    ca_certs=ssl_cert, ssl_version=ssl.PROTOCOL_SSLv23)
        s_wrapped.sendall(command + "\r\n")
        print "Told " + hostname + ": " + command

    def respond_query(self, response, verbose):
        print "sending response: " + repr(response)
        str_response = ""
        if verbose:
            str_response += "pool, machine, status, has users, deactivated, reason\n"
        for line in response:
            str_response += ','.join([str(x) for x in line[:3]])
            str_response += ',1,' if line[3] is not None else ',0,'
            str_response += ','.join([str(x) for x in line[4:]])
            str_response += '\n'
        str_response += 'END'
        self.transport.write(str_response)
        self.transport.loseConnection()

## Checks the machines table for inactive machines, and sets them as so
# Called periodically
def checkMachines():
    #check for inactive machines
    if (settings.get("Timeout_time") is not None) or (settings.get("Timeout_time") != 'None'):
        querystring = "UPDATE machines SET active = False, status = NULL WHERE last_heartbeat " + \
                "< DATE_SUB(NOW(), INTERVAL %s SECOND)"
        r1 = dbpool.runQuery(querystring, (settings.get("Timeout_Time"),))

    #check for reserved machines without confirmation
    #querystring = "DELETE FROM current WHERE (confirmed = False AND connecttime < DATE_SUB(NOW(), INTERVAL %s SECOND))"
    querystring = "DELETE FROM current WHERE (connecttime < DATE_SUB(NOW(), INTERVAL %s SECOND))"
    r2 = dbpool.runQuery(querystring, (settings.get("Reserve_Time"),))

## Gets the blacklist from the database, and updates is
def cacheBlacklist():
    querystring = "SELECT blacklist.address FROM blacklist LEFT JOIN whitelist ON blacklist.address = whitelist.address WHERE (banned = True AND whitelist.address IS NULL)"
    r = dbpool.runQuery(querystring)
    r.addBoth(setBlacklist)

## Sets the blacklist variable, given data from the Database
def setBlacklist(data):
    global blacklist
    blacklist = set()
    for item in data:
        blacklist.add(item[0])

## Chooses a LDAP/Active Directory server
def setAuthServer(results):
    results = results[0]
    if not results[0].payload.target:
        logger.error("Could not find LDAP server from AUTO")
    else:
        settings["Auth_Server"] = str(results[0].payload.target)

## Does a DNS service request for an LDAP service
def getAuthServer():
    domain = settings.get("Auth_Auto").replace('AUTO', '', 1)
    domain = '_ldap._tcp' + domain
    resolver = client.Resolver('/etc/resolv.conf')
    d = resolver.lookupService(domain)
    d.addCallback(setAuthServer)

## Reads the configuration file
def readConfigFile():
    #open the .conf file and return the variables as a dictionary
    for filelocation in ["/etc/cabsbroker.conf", "/usr/local/share/cabsbroker.conf",
                         os.path.dirname(os.path.abspath(__file__)) + "/cabsbroker.conf"]:
        if os.path.isfile(filelocation):
            break
    with open(filelocation, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('#') or not line:
                continue
            try:
                key, val = [word.strip() for word in line.split(':', 1)]
            except ValueError:
                print "Warning: unrecognized setting: " + line
                continue
            if key not in settings:
                print "Warning: unrecognized setting: " + line
                continue
            if val == "None":
                val == None
            settings[key] = val
        f.close()

    for key in ("Broker_Cert", "Agent_Cert", "Trusted_Clients"):
        if settings[key] is not None and not os.path.isabs(settings[key]):
            settings[key] = os.path.join(settings["Cert_Dir"], settings[key])
    if settings["Broker_Priv_Key"] is None:
        settings["Broker_Priv_Key"] = settings["Broker_Cert"]

## gets additional settings overrides from the Database
def readDatabaseSettings():
    #This needs to be a "blocked" call to ensure order, it can't be asynchronous.
    querystring = "SELECT * FROM settings"
    con = dbpool.connect()
    cursor = con.cursor()
    cursor.execute(querystring)
    data = cursor.fetchall()

    global settings
    for rule in data:
        settings[str(rule[0])] = rule[1]

    cursor.close()
    dbpool.disconnect(con)

    con = dbpool.connect()
    cursor = con.cursor()
    querystring = "UPDATE settings SET applied = True"
    cursor.execute(querystring)
    data = cursor.fetchall()
    cursor.close()
    con.commit()
    dbpool.disconnect(con)

## See if we need to restart because Database settings have changed
def checkSettingsChanged():
    querystring = "select COUNT(*) FROM settings WHERE (applied = False OR applied IS NULL)"
    r = dbpool.runQuery(querystring)
    r.addBoth(getSettingsChanged)

## Sees the result from checkSettingsChanged, and acts accordingly
def getSettingsChanged(data):
    if int(data[0][0]) > 0:
        reactor.stop()
        #should probably sleep here for a few seconds?
        os.execv(__file__, sys.argv)

## A logging.Handler class for writing out log to the Database
class MySQLHandler(logging.Handler):
    #This is our logger to the Database, it will handle out logger calls
    def __init__(self):
        logging.Handler.__init__(self)

    def emit(self, record):
        querystring = "INSERT INTO log VALUES(NOW(), %s, %s, DEFAULT)"
        r = dbpool.runQuery(querystring, (str(record.getMessage()), record.levelname))

## Makes sure the log doesn't grow too big, removes execess
def pruneLog():
    querystring = "DELETE FROM log WHERE id <= (SELECT id FROM (SELECT id FROM log ORDER BY id DESC LIMIT 1 OFFSET %s)foo )"
    r = dbpool.runQuery(querystring, (int(settings.get("Log_Keep")),))

## Starts the logging
def setLogging():
    global logger
    loglevel = int(settings.get("Log_Amount"))
    if loglevel <= 0:
        loglevel = logging.CRITICAL
        #For our purposes, CRITICAL means no logging
    elif loglevel == 1:
        loglevel = logging.ERROR
    elif loglevel == 2:
        loglevel = logging.WARNING
    elif loglevel == 3:
        loglevel = logging.INFO
    elif loglevel >= 4:
        loglevel = logging.DEBUG
    logger.setLevel(loglevel)

    if settings.get("Verbose_Out") == 'True':
        logger.addHandler(logging.StreamHandler(sys.stdout))

    logger.addHandler(MySQLHandler())

def start_ssl_cmd_server():
    with open(settings["Broker_Cert"], 'r') as certfile:
        certdata = certfile.read()
    if settings["Broker_Priv_Key"] != settings["Broker_Cert"]:
        with open(settings.get("Broker_Priv_Key"), 'r') as keyfile:
            certdata += keyfile.read()
    with open(settings["Trusted_Clients"], 'r') as f:
        authdata = f.read()
    certificate = internet.ssl.PrivateCertificate.loadPEM(certdata)
    authority = internet.ssl.Certificate.loadPEM(authdata)
    factory = Factory.forProtocol(CommandHandler)
    reactor.listenSSL(int(settings.get("Command_Port")), factory, certificate.options(authority))

if __name__ == "__main__":
    #Read the settings
    readConfigFile()

    #create database pool
    global dbpool
    dbpool = adbapi.ConnectionPool(
            "MySQLdb",
            db=settings.get("Database_Name"),
            port=int(settings.get("Database_Port")),
            user=settings.get("Database_Usr"),
            passwd=settings.get("Database_Pass"),
            host=settings.get("Database_Addr"),
            cp_reconnect=True
        )

    #get override settings from the database, then start logger
    readDatabaseSettings()
    #SetLogging
    setLogging()

    if settings.get("Broker_Priv_Key") is None or settings.get("Broker_Cert") is None:
        #Create Client Listening Server
        serverstring = "tcp:" + str(settings.get("Client_Port"))
        endpoints.serverFromString(reactor, serverstring).listen(HandleClientFactory())

        #Create external command Listening Server
        endpoint = endpoints.TCP4ServerEndpoint(reactor, int(settings.get("Command_Port")))
        endpoint.listen(Factory.forProtocol(CommandHandler))
    else:
        serverstring = "ssl:" + str(settings.get("Client_Port")) + ":privateKey=" + \
                settings.get("Broker_Priv_Key") + ":certKey=" + settings.get("Broker_Cert")
        endpoints.serverFromString(reactor, serverstring).listen(HandleClientFactory())
        start_ssl_cmd_server()

    #Set up Agents listening
    if (settings.get("Use_Agents") == 'True'):
        #Use Agents, so start the listening server
        if settings.get("Broker_Priv_Key") is None or settings.get("Broker_Cert") is None:
            agentserverstring = "tcp:" + str(settings.get("Agent_Port"))
            endpoints.serverFromString(reactor, agentserverstring).listen(HandleAgentFactory())
        else:
            agentserverstring = "ssl:" + str(settings.get("Agent_Port")) + ":privateKey=" + \
                    settings.get("Broker_Priv_Key") + ":certKey=" + settings.get("Broker_Cert")
            endpoints.serverFromString(reactor, agentserverstring).listen(HandleAgentFactory())

        #Check Machine status every 1/2 Reserve_Time
        checkup = task.LoopingCall(checkMachines)
        checkup.start( int(settings.get("Reserve_Time"))/2 )
    else:
        #this to do so things kinda work without agents
        querystring = "UPDATE machines SET active = True, status = 'Okay'"
        r1 = dbpool.runQuery(querystring)

    #resolve LDAP server
    if settings.get("Auth_Server").startswith("AUTO"):
        settings["Auth_Auto"] = settings.get("Auth_Server")
        resolveldap = task.LoopingCall(getAuthServer)
        #Get the LDAP server every 2 hours
        resolveldap.start(7200)

    #Start Blacklist cacheing
    if settings.get("Use_Blacklist") == 'True':
        getblacklist = task.LoopingCall(cacheBlacklist)
        #refresh blacklist every 15 minutes
        getblacklist.start(900)
    #Start Pruning log
    if int(settings.get("Log_Amount")) != 0:
        prune = task.LoopingCall(pruneLog)
        prune.start(int(settings.get("Log_Time")))

    #Check the online settings every 9 minutes, and restart if they changed
    checksettingschange = task.LoopingCall(checkSettingsChanged)
    checksettingschange.start(540)

    #Start Everything
    reactor.run()
