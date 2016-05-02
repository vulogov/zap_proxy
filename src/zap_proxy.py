#!/usr/bin/env python

##
## Zabbix Application Proxy (ZAP) main executable and source file
##
##

__author__ = 'Vladimir Ulogov'
__version__ = 'ZAP 0.0.1'

import logging
import argparse
import clips
import sys
import os
import time
import signal
import daemonize
import SocketServer
import Queue
import socket
import multiprocessing
from multiprocessing.reduction import reduce_handle
from multiprocessing.reduction import rebuild_handle
import setproctitle


logger = logging.getLogger("ZAP")
ARGS = None
ENV  = None

########################################################################################################################
## Service fuctions
########################################################################################################################
def check_file(fname, mode):
    fname = os.path.expandvars(fname)
    if os.path.exists(fname) and os.path.isfile(fname) and os.access(fname, mode):
        return True
    return False

def check_directory(dname):
    dname = os.path.expandvars(dname)
    if os.path.exists(dname) and os.path.isdir(dname) and os.access(dname, os.R_OK):
        return True
    return False

def check_file_read(fname):
    return check_file(fname, os.R_OK)

def Is_Process_Running():
    global ARGS, logger

    try:
        pid = int(open(ARGS.pid).read())
    except:
        logger.error("Can not detect ZAP process ID from %s" % ARGS.pid)
        return None
    if not os.path.exists('/proc/%d' % pid):
        logger.info("ZAP process with PID=%d isn't running. Removing stale PID file" % pid)
        os.unlink(args.pid)
        return None
    return pid

def check_module(fname):
    if not check_file_read(fname):
        return False
    if os.path.getsize(fname) > 0:
        return True
    return False

def check_file_write(fname):
    return check_file(fname, os.W_OK)

def check_file_exec(fname):
    return check_file(fname, os.X_OK)

def get_dir_content(dname):
    if not check_directory(dname):
        return []
    ret = []
    for f in os.listdir(dname):
        if not check_file_read("%s/%s"%(dname, f)):
            continue
        ret.append((f, "%s/%s"%(dname, f), os.path.splitext(f)))
    return ret

def rchop(thestring, ending):
  if thestring.endswith(ending):
    return thestring[:-len(ending)]
  return thestring


class Object(object):
    def Object__set_attr(self, key, argv):
        if argv.has_key(key):
            setattr(self, key, argv[key])


########################################################################################################################
## Clips and Clips
########################################################################################################################

class FACT:
    def facts(self, fname):
        self.clips.LoadFacts(fname)
    def load_facts(self, **args):
        return self._load(self.clips.LoadFacts, self.clips.LoadFactsFromString, args)

class LOADER:
    def _load(self, lf_file, lf_string, args):
        if args.has_key("file") and lf_file:
            if not check_module(args["file"]):
                raise IOError, "File %s not found or not accessible"%args["file"]
            return apply(lf_file, (args["file"],))
        elif args.has_key("data") and len(args["data"]) and lf_string:
            return apply(lf_string, (args["data"],))
        else:
            raise ValueError, "Loader requested to load not from file, nether from string"

class CLPEXEC:
    def load(self, **args):
        return self._load(self.clips.Load, self.clips.Eval, args)
    def execute(self, **args):
        return self._load(self.clips.BatchStar, self.clips.Eval, args)

class CLP(Object, LOADER, FACT, CLPEXEC):
    def __init__(self, **argv):
        self.argv = argv
        self.clips = clips.Environment()
        self.clear()
    def clear(self):
        self.clips.Clear()
        self.clips.Reset()
    def current(self):
        self.clips.SetCurrent()


########################################################################################################################
## Python and Python
########################################################################################################################

class PYLOADER:
    def __init__(self):
        self.mods = {}
    def module_loaded(self, mod, fun):
        pass
    def mod_exec(self, _mod):
        if type(_mod) == types.StringType:
            ## Passing the name
            _mod = self.find_the_mod(_mod)
            if _mod == None:
                return []
        elif type(_mod) == types.ModuleType:
            _mod = _mod
        else:
            return []
        out = []
        for f in dir(_mod):
            if type(getattr(_mod, f)) != types.FunctionType:
                continue
            out.append(f)
        return out
    def find_the_mod(self, mod_name):
        for p in self.mods.keys():
            if self.mods[p].has_key(mod_name):
                return self.mods[p][mod_name]
        return None
    def reload_mods(self, path=None):
        if not path:
            _path = self.path
        else:
            _path = path
        for p in _path:
            if not self.mods.has_key(p):
                self.mods[p] = {}
            dir = get_dir_content(p)
            for m in dir:
                file, full_path, mod = m
                modname, ext = mod
                if ext not in [".py",] or self.find_the_mod(modname) != None:
                    continue
                try:
                    _mod = imp.load_source(modname, full_path)
                except:
                    continue
                self.mods[p][modname] = _mod
                f_list = self.mod_exec(_mod)
                for f in f_list:
                    self.module_loaded(modname, f)
        for p in self.mods.keys():
            if p not in self.path:
                del self.mods[p]

class PYEXEC:
    def __call__(self, modname, *args, **kw):
        parse = modname.split(".")
        if len(parse) == 1:
            _mod     = modname
            _fun     = "main"
        elif len(parse) >= 2:
            _mod = parse[0]
            _fun = parse[1]
        else:
            raise ValueError, "Bad function name %s"%modname
        mod = self.find_the_mod(_mod)
        if mod == None:
            raise ValueError, "Module %s not found"%modname
        try:
            fun = getattr(mod, _fun)
        except:
            raise ValueError, "Function %s.%s not exists"%(_mod, _fun)
        try:
            return apply(fun, args, kw)
        except:
            raise ValueError, "Error in %s.%s"%(_mod, _fun)
    def execute(self, _fun, *args, **kw):
        out = {}
        for p in self.mods.keys():
            for m in self.mods[p].keys():
                mod = self.mods[p][m]
                try:
                    fun = getattr(mod, _fun)
                except:
                    continue
                try:
                    ret = apply(fun, args, kw)
                    out[m] = ret
                except:
                    continue
        return out

class PY(Object, PYLOADER, PYEXEC):
    def __init__(self, *path):
        self.path = []
        for d in list(path):
            if check_directory(d):
                self.path.append(d)
        PYLOADER.__init__(self)
        self.reload_mods()
    def __add__(self, path):
        if not check_directory(path) or path in self.path:
            return self
        self.path.append(path)
        return self
    def __sub__(self, path):
        if path in self.path:
           self.path.remove(path)
        return self

class PYCLP(PY,CLP):
    def __init__(self, **argv):
        self.argv = argv
        self.path = []
        self.Object__set_attr("path", self.argv)
        apply(PY.__init__, tuple([self,] + [self.path,]))
        apply(CLP.__init__, (self,), argv)
    def load_pyclp_module(self, name):
        import fnmatch
        mod = self.find_the_mod(name)
        if mod == None:
            raise ValueError, "PYCLP modulе %s not found"%name
        c = 0
        for e in dir(mod):
            if fnmatch.fnmatch(e, "*_clips"):
                fun_name = rchop(e,"_clips")
                try:
                    fun = getattr(mod, fun_name)
                except:
                    continue
                clips.RegisterPythonFunction(fun)
                self.clips.Build(getattr(mod, e))
                c += 1
        return c



class ZAPEnv:
    def __init__(self, args):
        global logger
        self.args = args
        self.logger = logger
        self.logger.info("Initializing environment")
        self.py = PY("%s/zap_modules"%self.args.config)





########################################################################################################################
## Network-related classes
########################################################################################################################



class ZAPConnectionWorker(multiprocessing.Process):
    def __init__(self, sq):

        self.SLEEP_INTERVAL = 1  # base class initialization
        multiprocessing.Process.__init__(self)
        self.socket_queue = sq
        self.kill_received = False

    def run(self):
        while not self.kill_received:
            try:
                h = self.socket_queue.get_nowait()
                fd = rebuild_handle(h)
                client_socket = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
                received = client_socket.recv(1024)
                print "Recieved on client: ", received
                client_socket.close()
            except Queue.Empty:
                pass
            time.sleep(self.SLEEP_INTERVAL)


class ZAPTCPHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        h = reduce_handle(self.request.fileno())
        socket_queue.put(h)



def build_argparser():
    global ARGS
    parser = argparse.ArgumentParser(description='ZAP_proxy - Zabbix Application Proxy')
    parser.add_argument('--config', '-c', nargs='?', default=".", action="store",
                        help='Path to the configuration directory')
    parser.add_argument('--log', '-l', nargs='?', default="/tmp/zap_proxy.log", action="store",
                        help='Path to the Log file')
    parser.add_argument('--verbose', '-v', default=0, action="count", help='Verbosity level')
    parser.add_argument('--cmd', '-C', default="help", action="store",
                        help="Execute specific command. Possibilities are: [start|stop|restart|help]")
    parser.add_argument('--daemonize', '-d', default=False, action="store_true", help='Run ZAP as a Unix Daemon')
    parser.add_argument('--pid', '-P', default="/tmp/zap_proxy.pid", action="store", help='Path to the PID file')
    parser.add_argument('--user', '-U', default="zabbix", action="store", help='Run ZAP as User')
    parser.add_argument('--group', '-G', default="zabbix", action="store", help='Set Group privileges for a ZAP')
    parser.add_argument('--bootstrap', '-b', default="bootstrap.clp", action="store", help='Name of the ZAP bootstrap file')
    parser.add_argument('--configuration', '-f', default="configuration.clp", action="store", help='Name of the ZAP configuration file')
    ARGS = parser.parse_args()
    return parser, ARGS


def set_logging(args):
    global logger

    fmt = logging.Formatter("%(asctime)s  %(message)s", "%m-%d-%Y %H:%M:%S")

    if args.log == '-':
        f = logging.StreamHandler()
    else:
        f = logging.FileHandler(args.log)
    f.setFormatter(fmt)
    logger.addHandler(f)
    if args.verbose == 1:
        logger.setLevel(logging.CRITICAL)
    elif args.verbose == 2:
        logger.setLevel(logging.ERROR)
    elif args.verbose == 3:
        logger.setLevel(logging.WARNING)
    elif args.verbose == 4:
        logger.setLevel(logging.INFO)
    elif args.verbose == 5:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)


def Loop():
    global logger, ARGS
    ENV = ZAPEnv(ARGS)
    logger.info("Entering loop...")
    while True:
        pass


def Start(args, parser):
    global logger

    import pwd, grp

    try:
        u = pwd.getpwnam(args.user)
        uid = u.pw_uid
        home = u.pw_dir
        gid = grp.getgrnam(args.group).gr_gid
    except KeyError:
        logger.error("User %(user)s or Group %(group)s does not exists" % args)
        return None
    daemon = daemonize.Daemonize(app="ZAP", pid=args.pid, action=Loop, chdir=home, user=args.user, group=args.group,
                                 logger=logger, foreground=not args.daemonize)
    logger.info("Executing ZAP as %s/%s in %s" % (args.user, args.group, home))
    daemon.start()
    return daemon





def Stop(args, parser):
    global logger

    logger.info("Trying to stop ZAP daemon")
    pid = Is_Process_Running()
    if not pid:
        logger.info("ZAP isn't running. Nothing to stop")
        return True
    for i in range(10):
        logger.info("Trying to TERM ZAP daemon. Appempt #%d" % i)
        os.kill(pid, signal.SIGTERM)
        time.sleep(1)
        if not Is_Process_Running():
            logger.info("ZAP is Gone!")
            return True
    if Is_Process_Running():
        logger.error("ZAP process is still there. Killing it")
    for i in range(10):
        logger.info("Trying to TERM ZAP daemon. Appempt #%d" % i)
        os.kill(pid, signal.SIGHUP)
        time.sleep(5)
        if not Is_Process_Running():
            logger.info("ZAP is Gone!")
            return True
    if Is_Process_Running():
        logger.error("ZAP process is still there. Nothing is I can do. Please contact System Administrator.")
    return False


def Main(args, parser):
    global logger
    if args.cmd.lower() == 'help':
        parser.print_help()
    elif args.cmd.lower() == "start":
        Start(args, parser)
    elif args.cmd.lower() == "stop":
        Stop(args, parser)
    elif args.cmd.lower() == "restart":
        if not Stop(args, parser):
            logger.error("Can not stop ZAP process. Restart is failed")
            return
        Start(args, parser)
    else:
        parser.print_help()


def main():
    global logger, ENV
    parser, args = build_argparser()
    set_logging(args)
    logger.critical("Zabbix Application Proxy ver %s" % __version__)
    print args
    Main(args, parser)


if __name__ == '__main__':
    main()


