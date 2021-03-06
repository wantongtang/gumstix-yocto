import ast
import codegen
import logging
import os.path
import bb.utils, bb.data
from itertools import chain
from pysh import pyshyacc, pyshlex, sherrors


logger = logging.getLogger('BitBake.CodeParser')
PARSERCACHE_VERSION = 2

try:
    import cPickle as pickle
except ImportError:
    import pickle
    logger.info('Importing cPickle failed.  Falling back to a very slow implementation.')


def check_indent(codestr):
    """If the code is indented, add a top level piece of code to 'remove' the indentation"""

    i = 0
    while codestr[i] in ["\n", "\t", " "]:
        i = i + 1

    if i == 0:
        return codestr

    if codestr[i-1] == "\t" or codestr[i-1] == " ":
        return "if 1:\n" + codestr

    return codestr

pythonparsecache = {}
shellparsecache = {}
pythonparsecacheextras = {}
shellparsecacheextras = {}


def parser_cachefile(d):
    cachedir = (d.getVar("PERSISTENT_DIR", True) or
                d.getVar("CACHE", True))
    if cachedir in [None, '']:
        return None
    bb.utils.mkdirhier(cachedir)
    cachefile = os.path.join(cachedir, "bb_codeparser.dat")
    logger.debug(1, "Using cache in '%s' for codeparser cache", cachefile)
    return cachefile

def parser_cache_init(d):
    global pythonparsecache
    global shellparsecache

    cachefile = parser_cachefile(d)
    if not cachefile:
        return

    try:
        p = pickle.Unpickler(file(cachefile, "rb"))
        data, version = p.load()
    except:
        return

    if version != PARSERCACHE_VERSION:
        return

    pythonparsecache = data[0]
    shellparsecache = data[1]

def parser_cache_save(d):
    cachefile = parser_cachefile(d)
    if not cachefile:
        return

    glf = bb.utils.lockfile(cachefile + ".lock", shared=True)

    i = os.getpid()
    lf = None
    while not lf:
        shellcache = {}
        pythoncache = {}

        lf = bb.utils.lockfile(cachefile + ".lock." + str(i), retry=False)
        if not lf or os.path.exists(cachefile + "-" + str(i)):
            if lf:
               bb.utils.unlockfile(lf) 
               lf = None
            i = i + 1
            continue

        shellcache = shellparsecacheextras
        pythoncache = pythonparsecacheextras

        p = pickle.Pickler(file(cachefile + "-" + str(i), "wb"), -1)
        p.dump([[pythoncache, shellcache], PARSERCACHE_VERSION])

    bb.utils.unlockfile(lf)
    bb.utils.unlockfile(glf)

def internSet(items):
    new = set()
    for i in items:
        new.add(intern(i))
    return new

def parser_cache_savemerge(d):
    cachefile = parser_cachefile(d)
    if not cachefile:
        return

    glf = bb.utils.lockfile(cachefile + ".lock")

    try:
        p = pickle.Unpickler(file(cachefile, "rb"))
        data, version = p.load()
    except (IOError, EOFError):
        data, version = None, None

    if version != PARSERCACHE_VERSION:
        data = [{}, {}]

    for f in [y for y in os.listdir(os.path.dirname(cachefile)) if y.startswith(os.path.basename(cachefile) + '-')]:
        f = os.path.join(os.path.dirname(cachefile), f)
        try:
            p = pickle.Unpickler(file(f, "rb"))
            extradata, version = p.load()
        except (IOError, EOFError):
            extradata, version = [{}, {}], None
        
        if version != PARSERCACHE_VERSION:
            continue

        for h in extradata[0]:
            if h not in data[0]:
                data[0][h] = extradata[0][h]
        for h in extradata[1]:
            if h not in data[1]:
                data[1][h] = extradata[1][h]
        os.unlink(f)

    # When the dicts are originally created, python calls intern() on the set keys
    # which significantly improves memory usage. Sadly the pickle/unpickle process 
    # doesn't call intern() on the keys and results in the same strings being duplicated
    # in memory. This also means pickle will save the same string multiple times in 
    # the cache file. By interning the data here, the cache file shrinks dramatically
    # meaning faster load times and the reloaded cache files also consume much less 
    # memory. This is worth any performance hit from this loops and the use of the 
    # intern() data storage.
    # Python 3.x may behave better in this area
    for h in data[0]:
        data[0][h]["refs"] = internSet(data[0][h]["refs"])
        data[0][h]["execs"] = internSet(data[0][h]["execs"])
    for h in data[1]:
        data[1][h]["execs"] = internSet(data[1][h]["execs"])

    p = pickle.Pickler(file(cachefile, "wb"), -1)
    p.dump([data, PARSERCACHE_VERSION])

    bb.utils.unlockfile(glf)


Logger = logging.getLoggerClass()
class BufferedLogger(Logger):
    def __init__(self, name, level=0, target=None):
        Logger.__init__(self, name)
        self.setLevel(level)
        self.buffer = []
        self.target = target

    def handle(self, record):
        self.buffer.append(record)

    def flush(self):
        for record in self.buffer:
            self.target.handle(record)
        self.buffer = []

class PythonParser():
    getvars = ("d.getVar", "bb.data.getVar", "data.getVar")
    execfuncs = ("bb.build.exec_func", "bb.build.exec_task")

    def warn(self, func, arg):
        """Warn about calls of bitbake APIs which pass a non-literal
        argument for the variable name, as we're not able to track such
        a reference.
        """

        try:
            funcstr = codegen.to_source(func)
            argstr = codegen.to_source(arg)
        except TypeError:
            self.log.debug(2, 'Failed to convert function and argument to source form')
        else:
            self.log.debug(1, self.unhandled_message % (funcstr, argstr))

    def visit_Call(self, node):
        name = self.called_node_name(node.func)
        if name in self.getvars:
            if isinstance(node.args[0], ast.Str):
                self.var_references.add(node.args[0].s)
            else:
                self.warn(node.func, node.args[0])
        elif name in self.execfuncs:
            if isinstance(node.args[0], ast.Str):
                self.var_execs.add(node.args[0].s)
            else:
                self.warn(node.func, node.args[0])
        elif name and isinstance(node.func, (ast.Name, ast.Attribute)):
            self.execs.add(name)

    def called_node_name(self, node):
        """Given a called node, return its original string form"""
        components = []
        while node:
            if isinstance(node, ast.Attribute):
                components.append(node.attr)
                node = node.value
            elif isinstance(node, ast.Name):
                components.append(node.id)
                return '.'.join(reversed(components))
            else:
                break

    def __init__(self, name, log):
        self.var_references = set()
        self.var_execs = set()
        self.execs = set()
        self.references = set()
        self.log = BufferedLogger('BitBake.Data.%s' % name, logging.DEBUG, log)

        self.unhandled_message = "in call of %s, argument '%s' is not a string literal"
        self.unhandled_message = "while parsing %s, %s" % (name, self.unhandled_message)

    def parse_python(self, node):
        h = hash(str(node))

        if h in pythonparsecache:
            self.references = pythonparsecache[h]["refs"]
            self.execs = pythonparsecache[h]["execs"]
            return

        if h in pythonparsecacheextras:
            self.references = pythonparsecacheextras[h]["refs"]
            self.execs = pythonparsecacheextras[h]["execs"]
            return


        code = compile(check_indent(str(node)), "<string>", "exec",
                       ast.PyCF_ONLY_AST)

        for n in ast.walk(code):
            if n.__class__.__name__ == "Call":
                self.visit_Call(n)

        self.references.update(self.var_references)
        self.references.update(self.var_execs)

        pythonparsecacheextras[h] = {}
        pythonparsecacheextras[h]["refs"] = self.references
        pythonparsecacheextras[h]["execs"] = self.execs

class ShellParser():
    def __init__(self, name, log):
        self.funcdefs = set()
        self.allexecs = set()
        self.execs = set()
        self.log = BufferedLogger('BitBake.Data.%s' % name, logging.DEBUG, log)
        self.unhandled_template = "unable to handle non-literal command '%s'"
        self.unhandled_template = "while parsing %s, %s" % (name, self.unhandled_template)

    def parse_shell(self, value):
        """Parse the supplied shell code in a string, returning the external
        commands it executes.
        """

        h = hash(str(value))

        if h in shellparsecache:
            self.execs = shellparsecache[h]["execs"]
            return self.execs

        if h in shellparsecacheextras:
            self.execs = shellparsecacheextras[h]["execs"]
            return self.execs

        try:
            tokens, _ = pyshyacc.parse(value, eof=True, debug=False)
        except pyshlex.NeedMore:
            raise sherrors.ShellSyntaxError("Unexpected EOF")

        for token in tokens:
            self.process_tokens(token)
        self.execs = set(cmd for cmd in self.allexecs if cmd not in self.funcdefs)

        shellparsecacheextras[h] = {}
        shellparsecacheextras[h]["execs"] = self.execs

        return self.execs

    def process_tokens(self, tokens):
        """Process a supplied portion of the syntax tree as returned by
        pyshyacc.parse.
        """

        def function_definition(value):
            self.funcdefs.add(value.name)
            return [value.body], None

        def case_clause(value):
            # Element 0 of each item in the case is the list of patterns, and
            # Element 1 of each item in the case is the list of commands to be
            # executed when that pattern matches.
            words = chain(*[item[0] for item in value.items])
            cmds  = chain(*[item[1] for item in value.items])
            return cmds, words

        def if_clause(value):
            main = chain(value.cond, value.if_cmds)
            rest = value.else_cmds
            if isinstance(rest, tuple) and rest[0] == "elif":
                return chain(main, if_clause(rest[1]))
            else:
                return chain(main, rest)

        def simple_command(value):
            return None, chain(value.words, (assign[1] for assign in value.assigns))

        token_handlers = {
            "and_or": lambda x: ((x.left, x.right), None),
            "async": lambda x: ([x], None),
            "brace_group": lambda x: (x.cmds, None),
            "for_clause": lambda x: (x.cmds, x.items),
            "function_definition": function_definition,
            "if_clause": lambda x: (if_clause(x), None),
            "pipeline": lambda x: (x.commands, None),
            "redirect_list": lambda x: ([x.cmd], None),
            "subshell": lambda x: (x.cmds, None),
            "while_clause": lambda x: (chain(x.condition, x.cmds), None),
            "until_clause": lambda x: (chain(x.condition, x.cmds), None),
            "simple_command": simple_command,
            "case_clause": case_clause,
        }

        for token in tokens:
            name, value = token
            try:
                more_tokens, words = token_handlers[name](value)
            except KeyError:
                raise NotImplementedError("Unsupported token type " + name)

            if more_tokens:
                self.process_tokens(more_tokens)

            if words:
                self.process_words(words)

    def process_words(self, words):
        """Process a set of 'words' in pyshyacc parlance, which includes
        extraction of executed commands from $() blocks, as well as grabbing
        the command name argument.
        """

        words = list(words)
        for word in list(words):
            wtree = pyshlex.make_wordtree(word[1])
            for part in wtree:
                if not isinstance(part, list):
                    continue

                if part[0] in ('`', '$('):
                    command = pyshlex.wordtree_as_string(part[1:-1])
                    self.parse_shell(command)

                    if word[0] in ("cmd_name", "cmd_word"):
                        if word in words:
                            words.remove(word)

        usetoken = False
        for word in words:
            if word[0] in ("cmd_name", "cmd_word") or \
               (usetoken and word[0] == "TOKEN"):
                if "=" in word[1]:
                    usetoken = True
                    continue

                cmd = word[1]
                if cmd.startswith("$"):
                    self.log.debug(1, self.unhandled_template % cmd)
                elif cmd == "eval":
                    command = " ".join(word for _, word in words[1:])
                    self.parse_shell(command)
                else:
                    self.allexecs.add(cmd)
                break
