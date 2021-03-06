from twisted.web import xmlrpc, server, static, http
from twisted.internet import defer, reactor
import cPickle, time, os, getopt, sys, base64, re, thread, ConfigParser, random, shutil
import atexit, json
import smtplib
from email.mime.text import MIMEText

from db_sqlite import DBSQLite
from db_mysql import DBMySQL

GErr=0
GOk=0

# Go to the script directory
global installDir, dataDir
if sys.platform=="win32":
	import _winreg
	# under windows, uses the registry setup by the installer
	try:
		hKey = _winreg.OpenKey (_winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\Mercenaries Engineering\\Coalition", 0, _winreg.KEY_READ)
		installDir, _type = _winreg.QueryValueEx (hKey, "Installdir")
		dataDir, _type = _winreg.QueryValueEx (hKey, "Datadir")
	except OSError:
		installDir = "."
		dataDir = "."
else:
	installDir = "."
	dataDir = "."
os.chdir (installDir)

# Create the logs/ directory
try:
	os.mkdir (dataDir + "/logs", 0755);
except OSError:
	pass

global TimeOut, port, verbose, config
config = ConfigParser.SafeConfigParser()
config.read ("coalition.ini")

def cfgInt (name, defvalue):
	global config
	if config.has_option('server', name):
		try:
			return int (config.get('server', name))
		except:
			pass
	return defvalue

def cfgBool (name, defvalue):
	global config
	if config.has_option('server', name):
		try:
			return int (config.get('server', name)) != 0
		except:
			pass
	return defvalue

def cfgStr (name, defvalue):
	global config
	if config.has_option('server', name):
		try:
			return str (config.get('server', name))
		except:
			pass
	return defvalue

port = cfgInt ('port', 19211)
TimeOut = cfgInt ('timeout', 60)
verbose = cfgBool ('verbose', False)
service = cfgBool ('service', True)
notifyafter = cfgInt ('notifyafter', 10)
decreasepriorityafter = cfgInt ('decreasepriorityafter', 10)
smtpsender = cfgStr ('smtpsender', "")
smtphost = cfgStr ('smtphost', "")
smtpport = cfgInt ('smtpport', 587)
smtptls = cfgBool ('smtptls', True)
smtplogin = cfgStr ('smtplogin', "")
smtppasswd = cfgStr ('smtppasswd', "")

LDAPServer = cfgStr ('ldaphost', "")
LDAPTemplate = cfgStr ('ldaptemplate', "")

_TrustedUsers = cfgStr ('trustedusers', "")

TrustedUsers = {}
for line in _TrustedUsers.splitlines (False):
	TrustedUsers[line] = True

_CmdWhiteList = cfgStr ('commandwhitelist', "")

GlobalCmdWhiteList = None
UserCmdWhiteList = {}
UserCmdWhiteListUser = None
for line in _CmdWhiteList.splitlines (False):
	_re = re.match ("^@(.*)", line)
	if _re:
		UserCmdWhiteListUser = _re.group(1)
		if not UserCmdWhiteListUser in UserCmdWhiteList:
			UserCmdWhiteList[UserCmdWhiteListUser] = []
	else:
		if UserCmdWhiteListUser:
			UserCmdWhiteList[UserCmdWhiteListUser].append (line)			
		else:
			if not GlobalCmdWhiteList:
				GlobalCmdWhiteList = []
			GlobalCmdWhiteList.append (line)

DefaultLocalProgressPattern = "PROGRESS:%percent"
DefaultGlobalProgressPattern = None

def usage():
	print ("Usage: server [OPTIONS]")
	print ("Start a Coalition server.\n")
	print ("Options:")
	print ("  -h, --help\t\tShow this help")
	print ("  -p, --port=PORT\tPort used by the server (default: "+str(port)+")")
	print ("  -v, --verbose\t\tIncrease verbosity")
	print ("  --reset\t\tReset the database (warning: all previous data are lost)")
	print ("  --test\t\tPerform the database unit tests")
	if sys.platform == "win32":	
		print ("  -c, --console=\t\tRun as a windows console application")
		print ("  -s, --service=\t\tRun as a windows service")
	print ("\nExample : server -p 1234")

# Service only on Windows
service = service and sys.platform == "win32"

resetDb = False
testDb = False

# Parse the options
try:
	opts, args = getopt.getopt(sys.argv[1:], "hp:vcs", ["help", "port=", "verbose", "reset", "test"])
	if len(args) != 0:
		usage()
		sys.exit(2)
except getopt.GetoptError, err:
	# print help information and exit:
	print str(err) # will print something like "option -a not recognized"
	usage()
	sys.exit(2)
for o, a in opts:
	if o in ("-h", "--help"):
		usage ()
		sys.exit(2)
	elif o in ("-v", "--verbose"):
		verbose = True
	elif o in ("-p", "--port"):
		port = int(a)
	elif o in ("--reset"):
		resetDb = True
	elif o in ("--test"):
		testDb = True
	else:
		assert False, "unhandled option " + o

	if LDAPServer != "":
		import ldap

if not verbose or service:
	try:
		outfile = open(dataDir + '/server.log', 'a')
		sys.stdout = outfile
		sys.stderr = outfile
		def exit ():
			outfile.close ()
		atexit.register (exit)
	except:
		pass


# Log function
def vprint (str):
	if verbose:
		print (str)
		sys.stdout.flush()

vprint ("[Init] --- Start ------------------------------------------------------------")
print ("[Init] "+time.strftime("%a, %d %b %Y %H:%M:%S", time.localtime(time.time ())))

# Init the good database
if cfgStr ('db_type', 'sqlite') == "mysql":
	vprint ("[Init] Use mysql")
	db = DBMySQL (cfgStr ('db_mysql_host', "127.0.0.1"), cfgStr ('db_mysql_user', ""), cfgStr ('db_mysql_password', ""), cfgStr ('db_mysql_base', "base"))
else:
	vprint ("[Init] Use sqlite")
	db = DBSQLite (cfgStr ('db_sqlite_file', "coalition.db"))

if service:
	vprint ("[Init] Running service")
else:
	vprint ("[Init] Running standard console")

def getLogFilename (jobId):
	global dataDir
	return dataDir + "/logs/" + str(jobId) + ".log"

# strip all 
def strToInt (s):
	try:
		return int(s)
	except:
		return 0

class LogFilter:
	"""A log filter object. The log pattern must include a '%percent' or a '%one' key word."""
	
	def __init__ (self, pattern):
		# 0~100 or 0~1 ?
		self.IsPercent = re.match (".*%percent.*", pattern) != None
		
		# Build the final pattern for the RE
		if self.IsPercent:
			pattern = re.sub ("%percent", "([0-9.]+)", pattern)
		else:
			pattern = re.sub ("%one", "([0-9.]+)", pattern)
			
		# Final progress filter
		self.RE = re.compile(pattern)
		
		# Put it in the cache
		global LogFilterCache
		LogFilterCache[pattern] = self

	def filterLogs (self, log):
		"""Return the filtered log and the last progress, if any"""
		progress = None
		for m in self.RE.finditer (log):
			capture = m.group(1)
			try:
				progress = float(capture) / (self.IsPercent and 100.0 or 1.0)
			except ValueError:
				pass
		#return self.RE.sub ("", log), progress
		return log, progress
		
LogFilterCache = {}

def getLogFilter (pattern):
	"""Get the pattern filter from the cache or add one"""
	global LogFilterCache
	try:	
		filter = LogFilterCache[pattern]
	except KeyError:
		filter = LogFilter (pattern)
		LogFilterCache[pattern] = filter
	return filter

def writeJobLog (jobId, log):
	logFile = open (getLogFilename (jobId), "a")
	logFile.write (log)
	logFile.close ()	



# Authenticate the user
def authenticate (request):
	if LDAPServer != "":
		username = request.getUser ()
		password = request.getPassword ()
		if username in TrustedUsers:
			vprint (username + " in the clearance list")
			vprint ("Authentication OK")
			return True
		if username != "" or password != "":
			l = ldap.open(LDAPServer)
			vprint ("Authenticate "+username+" with LDAP")
			username = LDAPTemplate.replace ("__login__", username)
			try:
				if l.bind_s(username, password, ldap.AUTH_SIMPLE):
					vprint ("Authentication OK")
					return True
			except ldap.LDAPError:
				vprint ("Authentication Failed")
				pass
		else:
			vprint ("Authentication Required")
		request.setHeader ("WWW-Authenticate", "Basic realm=\"Coalition Login\"")
		request.setResponseCode(http.UNAUTHORIZED)
		return False
	return True

# Check if the user can add this command
def grantAddJob (user, cmd):

	def checkWhiteList (wl):
		for pattern in wl:
			if (re.match (pattern, cmd)):
				return True
		else:
			vprint ("user '" + user + "' is not allowed to run the command '" + cmd + "'")
		return False

	# user defined white list ?		
	if user in UserCmdWhiteList:
		wl = UserCmdWhiteList[user]
		if checkWhiteList (wl):
			return True

		# If in the global command white list
		if GlobalCmdWhiteList:
			if checkWhiteList (GlobalCmdWhiteList):
				return True
		return False

	else:
		# If in the global command white list
		if GlobalCmdWhiteList:
			if not checkWhiteList (GlobalCmdWhiteList):
				return False
	
	# Cleared
	return True

class Root (static.File):
	def __init__ (self, path, defaultType='text/html', ignoredExts=(), registry=None, allowExt=0):
		static.File.__init__(self, path, defaultType, ignoredExts, registry, allowExt)

	def render (self, request):
		if authenticate (request):
			return static.File.render (self, request)
		return 'Authorization required!'

class Master (xmlrpc.XMLRPC):
	"""    """

	user = ""

	def render (self, request):
		with db:
			vprint ("[" + request.method + "] "+request.path)
			if authenticate (request):
				# If not autenticated, user == ""
				self.user = request.getUser ()
				# Addjob

				def getArg (name, default):
					value = request.args.get (name, [default])
					return value[0]

				# The legacy method for compatibility
				if request.path == "/xmlrpc/addjob":

					parent = getArg ("parent", "0")
					title = getArg ("title", "New job")
					cmd = getArg ("cmd", getArg ("command", ""))
					dir = getArg ("dir", ".")
					environment = getArg ("env", None)
					if environment == "":
						environment = None
					priority = getArg ("priority", "1000")
					timeout = getArg ("timeout", "0")
					affinity = getArg ("affinity", "")
					dependencies = getArg ("dependencies", "")
					progress_pattern = getArg ("localprogress", "")
					url = getArg ("url", "")
					user = getArg ("user", "")
					state = getArg ("state", "WAITING")
					paused = getArg ("paused", "0")
					if self.user != "":
						user = self.user

					if grantAddJob (self.user, cmd):
						vprint ("Add job : " + cmd)
						# try as an int
						parent = int (parent)
						if type(dependencies) is str:
							# Parse the dependencies string
							dependencies = re.findall ('(\d+)', dependencies)
						for i, dep in enumerate (dependencies) :
							dependencies[i] = int (dep)

						job = db.newJob (parent, str (title), str (cmd), str (dir), str (environment),
									str (state), int (paused), int (timeout), int (priority), str (affinity),
									str (user), str (url), str (progress_pattern))
						if job is not None:
							db.setJobDependencies (job['id'], dependencies)
							return str(job['id'])

					return "-1"

				else:
					value = request.content.getvalue()
					if request.method != "GET":
						data = value and json.loads(request.content.getvalue()) or {}
						if verbose:
							vprint ("[Content] "+repr(data))
					else:
						if verbose:
							vprint ("[Content] "+repr(request.args))

					def getArg (name, default):
						if request.method == "GET":
							# GET params
							value = request.args.get (name, [default])[0]
							value = type(default)(default if value == None else value)
							assert (value != None)
							return value
						else:
							# JSON params
							value = data.get (name)
							value = type(default)(default if value == None else value)
							assert (value != None)
							return value

					# REST api
					def api_rest ():
						if request.method == "PUT":
							if request.path == "/api/jobs":
								job = db.newJob ((getArg ("parent",0)),
												 (getArg("title","")),
												 (getArg("command","")),
												 (getArg("dir","")),
												 (getArg("environment","")), 
												 (getArg("state","WAITING")),
												 (getArg("paused",0)),
												 (getArg("timeout",1000)),
												 (getArg("priority",1000)),
												 (getArg("affinity", "")), 
												 (getArg("user", "")),
												 (getArg("url", "")),
												 (getArg("progress_pattern", "")),
												 (getArg("dependencies", [])))
								return job['id']

						elif request.method == "GET":
							m = re.match(r"^/api/jobs/(\d+)$", request.path)
							if m:
								return db.getJob (int(m.group (1)))

							m = re.match(r"^/api/jobs/(\d+)/children$", request.path)
							if m:
								return db.getJobChildren (int(m.group (1)), {})

							m = re.match(r"^/api/jobs/(\d+)/dependencies$", request.path)
							if m:
								return db.getJobDependencies (int(m.group (1)))

							m = re.match(r"^/api/jobs/(\d+)/childrendependencies$", request.path)
							if m:
								return db.getChildrenDependencyIds (int(m.group (1)))

							m = re.match(r"^/api/jobs/(\d+)/log$", request.path)
							if m:
								return self.getLog (int(m.group (1)))

							if request.path == "/api/jobs":
								return db.getJobChildren (0, {})

							if request.path == "/api/workers":
								return db.getWorkers ()

							if request.path == "/api/events":
								return db.getEvents (getArg ("job", -1), getArg ("worker", ""), getArg ("howlong", -1))

							if request.path == "/api/affinities":
								return db.getAffinities ()

						elif request.method == "POST":
							if request.path == "/api/jobs":
								db.editJobs (data)
								return 1

							if request.path == "/api/workers":
								db.editWorkers (data)
								return 1

							m = re.match(r"^/api/jobs/(\d+)/dependencies$", request.path)
							if m:
								db.setJobDependencies (int(m.group (1)), data)
								return 1

							if request.path == "/api/resetjobs":
								for jobId in data:
									db.resetJob (int(jobId))
								return 1

							if request.path == "/api/reseterrorjobs":
								for jobId in data:
									db.resetErrorJob (int(jobId))
								return 1

							if request.path == "/api/startjobs":
								for jobId in data:
									db.startJob (int(jobId))
								return 1

							if request.path == "/api/pausejobs":
								for jobId in data:
									db.pauseJob (int(jobId))
								return 1

							if request.path == "/api/stopworkers":
								for name in data:
									db.stopWorker (name)
								return 1

							if request.path == "/api/startworkers":
								for name in data:
									db.startWorker (name)
								return 1

							if request.path == "/api/affinities":
								db.setAffinities (data)
								return 1

						elif request.method == "DELETE":

							if request.path == "/api/jobs":
								for jobId in data:
									deletedJobs = []
									db.deleteJob (int(jobId), deletedJobs)
									for deleteJobId in deletedJobs:
										self.deleteLog (deleteJobId)
								return 1

							if request.path == "/api/workers":
								for name in data:
									db.deleteWorker (name)
								return 1


					result = api_rest ()
					if result != None:
						# Only JSON right now
						return json.dumps (result)
					else:
						# return server.NOT_DONE_YET
						request.setResponseCode(404)
						return "Web service not found"
			return 'Authorization required!'

	def getLog (self, jobId):
		# Look for the job
		log = ""
		try:
			logFile = open (getLogFilename (jobId), "r")
			while (1):
				# Read some lines of logs
				line = logFile.readline()
				# "" means EOF
				if line == "":
					break
				log = log + line
			logFile.close ()
		except IOError:
			pass
		return log

	def deleteLog (self, jobId):
		# Look for the job
		try:
			os.remove (getLogFilename (jobId))
		except OSError:
			pass

# Unauthenticated connection for workers
class Workers(xmlrpc.XMLRPC):
	"""    """

	def render (self, request):
		with db:
			vprint ("[" + request.method + "] "+request.path)
			def getArg (name, default):
				value = request.args.get (name, [default])
				return value[0]

			if request.path == "/workers/heartbeat":
				return self.json_heartbeat (getArg ('hostname', ''), getArg ('jobId', '-1'), getArg ('log', ''), getArg ('load', '[0]'), getArg ('free_memory', '0'), getArg ('total_memory', '0'), request.getClientIP ())
			elif request.path == "/workers/pickjob":
				return self.json_pickjob (getArg ('hostname', ''), getArg ('load', '[0]'), getArg ('free_memory', '0'), getArg ('total_memory', '0'), request.getClientIP ())
			elif request.path == "/workers/endjob":
				return self.json_endjob (getArg ('hostname', ''), getArg ('jobId', '1'), getArg ('errorCode', '0'), request.getClientIP ())
			else:
				# return server.NOT_DONE_YET
				return xmlrpc.XMLRPC.render (self, request)

	def json_heartbeat (self, hostname, jobId, log, load, free_memory, total_memory, ip):
		result = db.heartbeat (hostname, int(jobId), load, int(free_memory), int(total_memory), str(ip))
		if log != "" :
			try:
				logFile = open (getLogFilename (jobId), "a")
				log = base64.decodestring(log)
				
				# Filter the log progression message
				progress = None
				job = db.getJob (int (jobId))
				progress_pattern = getattr (job, "progress_pattern", DefaultLocalProgressPattern)
				if progress_pattern != "":
					vprint ("progressPattern : \n" + str(progress_pattern))
					lp = None
					gp = None
					lFilter = getLogFilter (progress_pattern)
					log, lp = lFilter.filterLogs (log)
					if lp != None:
						vprint ("lp : "+ str(lp)+"\n")
						if lp != job['progress']:
							db.setJobProgress (int (jobId), lp)				
				logFile.write (log)
				if not result:
					logFile.write ("KillJob: server required worker to kill job.\n")
				logFile.close ()
			except IOError:
				vprint ("Error in logs")
		return result and "true" or "false"

	def json_pickjob (self, hostname, load, free_memory, total_memory, ip):
		return str (db.pickJob (hostname, load, int(free_memory), int(total_memory), str(ip)))

	def json_endjob (self, hostname, jobId, errorCode, ip):
		return str (db.endJob (hostname, int(jobId), int(errorCode), str(ip)))

# Listen to an UDP socket to respond to the workers broadcast
def listenUDP():
	from socket import SOL_SOCKET, SO_BROADCAST
	from socket import socket, AF_INET, SOCK_DGRAM, error
	s = socket (AF_INET, SOCK_DGRAM)
	s.bind (('0.0.0.0', port))
	while 1:
		try:
			data, addr = s.recvfrom (1024)
			s.sendto ("roxor", addr)
		except:
			pass

def main():
	# Start the UDP server used for the broadcast
	thread.start_new_thread (listenUDP, ())

	from twisted.internet import reactor
	from twisted.web import server
	root = Root("public_html")
	webService = Master()
	workers = Workers()
	root.putChild('xmlrpc', webService)
	root.putChild('api', webService)
	root.putChild('workers', workers)
	vprint ("[Init] Listen on port " + str (port))
	reactor.listenTCP(port, server.Site(root))
	reactor.run()

def sendEmail (to, message) :
	if to != "" :
		vprint ("Send email to " + to + " : " + message)
		if smtphost != "" :
			# Create a text/plain message
			msg = MIMEText(message)

			# me == the sender's email address
			# you == the recipient's email address
			msg['Subject'] = message
			msg['From'] = smtpsender
			msg['To'] = to

			# Send the message via our own SMTP server, but don't include the
			# envelope header.
			try:
				s = smtplib.SMTP(smtphost, smtpport)
				if smtptls:
					s.ehlo()
					s.starttls()
					s.ehlo() 
				if smtplogin != '' or smtppasswd != '':
					s.login(smtplogin, smtppasswd)
				s.sendmail (smtpsender, [to], msg.as_string())
				s.quit()
			except Exception as inst:
				vprint (inst)
				pass

def notifyError (job):
	if job['user'] :
		sendEmail (job['user'], 'ERRORS in job ' + job['title'] + ' (' + str(job['id']) + ').')

def notifyFinished (job):
	if job['user'] :
		sendEmail (job['user'], 'The job ' + job['title'] + ' (' + str(job['id']) + ') is FINISHED.')

def notifyFirstFinished (job):
	if job['user'] :
		sendEmail (job['user'], 'The job ' + job['title'] + ' (' + str(job['id']) + ') has finished ' + str(notifyafter) + ' jobs.')

db.NotifyError = notifyError
db.NotifyFinished = notifyFinished
db.Verbose = verbose

with db:
	if resetDb:
		db.reset ()
	if testDb:
		db.test ()

if sys.platform=="win32" and service:

	# Windows Service
	import win32serviceutil
	import win32service
	import win32event

	class WindowsService(win32serviceutil.ServiceFramework):
		_svc_name_ = "CoalitionServer"
		_svc_display_name_ = "Coalition Server"

		def __init__(self, args):
			vprint ("[Init] Service init")
			win32serviceutil.ServiceFramework.__init__(self, args)
			self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

		def SvcStop(self):
			vprint ("[Stop] Service stop")
			self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
			win32event.SetEvent(self.hWaitStop)

		def SvcDoRun(self):
			vprint ("[Run] Service running")
			import servicemanager
			self.CheckForQuit()
			main()
			vprint ("Service quitting")

		def CheckForQuit(self):
			vprint ("[Stop] Checking for quit...")
			retval = win32event.WaitForSingleObject(self.hWaitStop, 10)
			if not retval == win32event.WAIT_TIMEOUT:
				# Received Quit from Win32
				reactor.stop()

			reactor.callLater(1.0, self.CheckForQuit)

	if __name__=='__main__':
		win32serviceutil.HandleCommandLine(WindowsService)
else:

	# Simple server
	if __name__ == '__main__':
		main()

