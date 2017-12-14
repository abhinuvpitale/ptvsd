# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root for license information.

from __future__ import print_function, with_statement, absolute_import

__author__ = "Microsoft Corporation <ptvshelp@microsoft.com>"
__version__ = "4.0.0a1"

import io
import os
import socket
import sys
import time
import threading
import traceback
import untangle 

try:
    import urllib
    urllib.unquote
except:
    import urllib.parse as urllib

import _pydevd_bundle.pydevd_comm as pydevd_comm
from _pydevd_bundle.pydevd_comm import pydevd_log

import ptvsd.ipcjson as ipcjson
import ptvsd.futures as futures


#def ipcjson_trace(s):
#    print(s)
#ipcjson._TRACE = ipcjson_trace

def unquote(s):
    if s is None:
        return None
    return urllib.unquote(s)

# Generates VSCode entity IDs, and maps them to corresponding pydevd entity IDs.
#
# For VSCode, IDs are always integers, and uniquely identify the entity among all other
# entities of the same type - e.g. all frames across all threads have unique IDs.
#
# For pydevd, IDs can be integer or strings, and are usually specific to some scope -
# for example, a frame ID is only unique within a given thread. To produce a truly unique
# ID, the IDs of all the outer scopes have to be combined into a tuple. Thus, for example,
# a pydevd frame ID is (thread_id, frame_id).
#
# Variables (evaluation results) technically don't have IDs in pydevd, as it doesn't have
# evaluation persistence. However, for a given frame, any child can be identified by the
# path one needs to walk from the root of the frame to get to that child - and that path,
# represented as a sequence of its consituent components, is used by pydevd commands to
# identify the variable. So we use the tuple representation of the same as its pydevd ID.
# For example, for something like foo[1].bar, its ID is:
#   (thread_id, frame_id, 'FRAME', 'foo', 1, 'bar')
#
# For pydevd breakpoints, the ID has to be specified by the caller when creating, so we
# can just reuse the ID that was generated for VSC. However, when referencing the pydevd
# breakpoint later (e.g. to remove it), its ID must be specified together with path to
# file in which that breakpoint is set - i.e. pydevd treats those IDs as scoped to a file.
# So, even though breakpoint IDs are unique across files, use (path, bp_id) as pydevd ID.
class IDMap(object):
    def __init__(self):
        self._vscode_to_pydevd = {}
        self._pydevd_to_vscode = {}
        self._next_id = 1
        self._lock = threading.Lock()

    def pairs(self):
        with self._lock:
            return list(self._pydevd_to_vscode.items())
    
    def add(self, pydevd_id):
        with self._lock:
            vscode_id = self._next_id
            if callable(pydevd_id):
                pydevd_id = pydevd_id(vscode_id)
            self._next_id += 1
            self._vscode_to_pydevd[vscode_id] = pydevd_id
            self._pydevd_to_vscode[pydevd_id] = vscode_id
        return vscode_id

    def remove(self, pydevd_id=None, vscode_id=None):
        with self._lock:
            if pydevd_id is None:
                pydevd_id = self._vscode_to_pydevd[vscode_id]
            elif vscode_id is None:
                vscode_id = self._pydevd_to_vscode[pydevd_id]
            del self._vscode_to_pydevd[vscode_id]
            del self._pydevd_to_vscode[pydevd_id]
    
    def to_pydevd(self, vscode_id):
        return self._vscode_to_pydevd[vscode_id]

    def to_vscode(self, pydevd_id, autogen=True):
        try:
            return self._pydevd_to_vscode[pydevd_id]
        except KeyError:
            if autogen:
                return self.add(pydevd_id)
            else:
                raise

    def pydevd_ids(self):
        with self._lock:
            ids = list(self._pydevd_to_vscode.keys())
        return ids

    def vscode_ids(self):
        with self._lock:
            ids = list(self._vscode_to_pydevd.keys())
        return ids

class ExceptionInfo(object):
    def __init__(self, name, description):
        self.name = name
        self.description = description

# A dummy socket-like object that is given to pydevd in lieu of the real thing.
# It parses pydevd messages and redirects them to the provided handler callback.
# It also provides an interface to send notifications and requests to pydevd;
# for requests, the reply can be asynchronously awaited.
class PydevdSocket(object):
    def __init__(self, event_handler):
        #self.log = open('pydevd.log', 'w')
        self.event_handler = event_handler
        self.lock = threading.Lock()
        self.seq = 1000000000
        self.pipe_r, self.pipe_w = os.pipe()
        self.requests = {}

    def close(self):
        pass

    def shutdown(self, mode):
        pass

    def recv(self, count):
        data = os.read(self.pipe_r, count)
        #self.log.write('>>>[' + data.decode('utf8') + ']\n\n')
        #self.log.flush()
        return data

    def send(self, data):
        result = len(data)
        data = unquote(data.decode('utf8'))
        #self.log.write('<<<[' + data + ']\n\n')
        #self.log.flush()
        cmd_id, seq, args = data.split('\t', 2)
        cmd_id = int(cmd_id)
        seq = int(seq)
        with self.lock:
            loop, fut = self.requests.pop(seq, (None, None))
        if fut is None:
            self.event_handler(cmd_id, seq, args)
        else:
            loop.call_soon_threadsafe(fut.set_result, (cmd_id, seq, args))
        return result

    def make_packet(self, cmd_id, args):
        with self.lock:
            seq = self.seq
            self.seq += 1
        s = "%s\t%s\t%s\n" % (cmd_id, seq, args)
        return seq, s

    def pydevd_notify(self, cmd_id, args):
        seq, s = self.make_packet(cmd_id, args)
        os.write(self.pipe_w, s.encode('utf8'))

    def pydevd_request(self, loop, cmd_id, args):
        seq, s = self.make_packet(cmd_id, args)
        fut = loop.create_future()
        with self.lock:
            self.requests[seq] = loop, fut
            os.write(self.pipe_w, s.encode('utf8'))
        return fut

# IPC JSON message processor for VSC debugger protocol, mapping it to pydevd protocol.
class VSCodeMessageProcessor(ipcjson.SocketIO, ipcjson.IpcChannel):
    def __init__(self, socket, pydevd, logfile=None):
        super(VSCodeMessageProcessor, self).__init__(socket=socket, own_socket=False, logfile=logfile)
        self.socket = socket
        self.pydevd = pydevd
        self.stack_traces = {}
        self.stack_traces_lock = threading.Lock()
        self.active_exceptions = {}
        self.active_exceptions_lock = threading.Lock()
        self.thread_map = IDMap()
        self.frame_map = IDMap()
        self.var_map = IDMap()
        self.bp_map = IDMap()
        self.next_var_ref = 0
        self.loop = futures.EventLoop()
        threading.Thread(target = self.loop.run_forever, name = 'ptvsd.EventLoop').start()

    def close(self):
        if self.socket:
            self.socket.close()

    def pydevd_notify(self, cmd_id, args):
        try:
            return self.pydevd.pydevd_notify(cmd_id, args)
        except:
            traceback.print_exc(file=sys.__stderr__)
            raise

    def pydevd_request(self, cmd_id, args):
        return self.pydevd.pydevd_request(self.loop, cmd_id, args)

    # Instances of this class provide decorators to mark methods as handlers for various
    # pydevd messages - a decorated method is added to the map with the corresponding
    # message ID, and is looked up there by pydevd event handler below.
    class EventHandlers(dict):
        def handler(self, cmd_id):
            def decorate(f):
                self[cmd_id] = f
                return f
            return decorate

    pydevd_events = EventHandlers()

    def on_pydevd_event(self, cmd_id, seq, args):
        try:
            f = self.pydevd_events[cmd_id]
        except KeyError:
            raise Exception('Unsupported pydevd command ' + str(cmd_id))
        return f(self, seq, args)

    def async_handler(m):
        m = futures.async(m)
        def f(self, *args, **kwargs):
            fut = m(self, self.loop, *args, **kwargs)
            def done(fut):
                try:
                    fut.result()
                except:
                    traceback.print_exc(file=sys.__stderr__)
            fut.add_done_callback(done)
        return f

    @async_handler
    def on_initialize(self, request, args):
        yield self.pydevd_request(pydevd_comm.CMD_VERSION, '1.1\tWINDOWS\tID')
        self.send_response(request,
            supportsExceptionInfoRequest=True,
            supportsConfigurationDoneRequest=True,
            exceptionBreakpointFilters=[
                {'filter': 'raised', 'label': 'Raised Exceptions'},
                {'filter': 'uncaught', 'label': 'Uncaught Exceptions'},
            ]
        )
        self.send_event('initialized')

    def on_configurationDone(self, request, args):
        self.send_response(request)

    def on_disconnect(self, request, args):
        self.send_response(request)

    @async_handler
    def on_attach(self, request, args):
        self.send_response(request)
        yield self.pydevd_request(pydevd_comm.CMD_RUN, '')
        self.send_process_event('attach')

    @async_handler
    def on_launch(self, request, args):
        self.send_response(request)
        yield self.pydevd_request(pydevd_comm.CMD_RUN, '')
        self.send_process_event('launch')

    def send_process_event(self, start_method):
        evt = {
            'name': sys.argv[0],
            'systemProcessId': os.getpid(),
            'isLocalProcess': True,
            'startMethod': start_method,
        }
        self.send_event('process', **evt)

    @async_handler
    def on_threads(self, request, args):
        _, _, args = yield  self.pydevd_request(pydevd_comm.CMD_LIST_THREADS, '')
        xml = untangle.parse(args).xml
        try:
            xthreads = xml.thread
        except AttributeError:
            xthreads = []

        threads = []
        for xthread in xthreads:
            tid = self.thread_map.to_vscode(xthread['id'])
            try:
                name = unquote(xthread['name'])
            except KeyError:
                name = None                
            if not (name and name.startswith('pydevd.')):
                threads.append({'id': tid, 'name': name})
            
        self.send_response(request, threads=threads)

    @async_handler
    def on_stackTrace(self, request, args):
        tid = int(args['threadId'])
        startFrame = int(args['startFrame'])
        levels = int(args['levels'])

        tid = self.thread_map.to_pydevd(tid)
        with self.stack_traces_lock:
            xframes = self.stack_traces[tid]
        totalFrames = len(xframes)

        if levels == 0:
            levels = totalFrames

        stackFrames = []
        for xframe in xframes:
            if startFrame > 0:
                startFrame -= 1
                continue
            if levels <= 0:
                break
            levels -= 1
            fid = self.frame_map.to_vscode((tid, int(xframe['id'])))
            name = unquote(xframe['name'])
            file = unquote(xframe['file'])
            line = int(xframe['line'])
            stackFrames.append({'id': fid, 'name': name, 'source': {'path': file}, 'line': line, 'column': 0})

        self.send_response(request, stackFrames=stackFrames, totalFrames=totalFrames)

    @async_handler
    def on_scopes(self, request, args):
        vsc_fid = int(args['frameId'])
        pyd_tid, pyd_fid = self.frame_map.to_pydevd(vsc_fid)
        pyd_var = (pyd_tid, pyd_fid, 'FRAME')
        vsc_var = self.var_map.to_vscode(pyd_var)
        scope = {'name': 'Locals', 'expensive': False, 'variablesReference': vsc_var}
        self.send_response(request, scopes=[scope])

    @async_handler
    def on_variables(self, request, args):
        vsc_var = int(args['variablesReference'])
        pyd_var = self.var_map.to_pydevd(vsc_var)

        if len(pyd_var) == 3:
            cmd = pydevd_comm.CMD_GET_FRAME
        else:
            cmd = pydevd_comm.CMD_GET_VARIABLE

        _, _, args = yield self.pydevd_request(cmd, '\t'.join(str(s) for s in pyd_var))
        xml = untangle.parse(args).xml
        try:
            xvars = xml.var
        except AttributeError:
            xvars = []

        vars = []
        for xvar in xvars:
            var = {
                'name': unquote(xvar['name']),
                'type': unquote(xvar['type']),
                'value': unquote(xvar['value']),
            }
            if bool(xvar['isContainer']):
                pyd_child = pyd_var + (var['name'],)
                var['variablesReference'] = self.var_map.to_vscode(pyd_child)
            vars.append(var)

        self.send_response(request, variables=vars)

    @async_handler
    def on_pause(self, request, args):
        vsc_tid = int(args['threadId'])
        if vsc_tid == 0: # VS does this to mean "stop all threads":
            for pyd_tid in self.thread_map.pydevd_ids():
                self.pydevd_notify(pydevd_comm.CMD_THREAD_SUSPEND, pyd_tid)
        else:
            pyd_tid = self.thread_map.to_pydevd(vsc_tid)
            self.pydevd_notify(pydevd_comm.CMD_THREAD_SUSPEND, pyd_tid)
        self.send_response(request)

    @async_handler
    def on_continue(self, request, args):
        tid = self.thread_map.to_pydevd(int(args['threadId']))
        self.pydevd_notify(pydevd_comm.CMD_THREAD_RUN, tid)
        self.send_response(request)

    @async_handler
    def on_next(self, request, args):
        tid = self.thread_map.to_pydevd(int(args['threadId']))
        self.pydevd_notify(pydevd_comm.CMD_STEP_OVER, tid)
        self.send_response(request)

    @async_handler
    def on_stepIn(self, request, args):
        tid = self.thread_map.to_pydevd(int(args['threadId']))
        self.pydevd_notify(pydevd_comm.CMD_STEP_INTO, tid)
        self.send_response(request)

    @async_handler
    def on_stepOut(self, request, args):
        tid = self.thread_map.to_pydevd(int(args['threadId']))
        self.pydevd_notify(pydevd_comm.CMD_STEP_RETURN, tid)
        self.send_response(request)

    @async_handler
    def on_setBreakpoints(self, request, args):
        bps = []
        path = args['source']['path']
        src_bps = args.get('breakpoints', [])

        # First, we must delete all existing breakpoints in that source.
        for pyd_bpid, vsc_bpid in self.bp_map.pairs():
            self.pydevd_notify(pydevd_comm.CMD_REMOVE_BREAK, 'python-line\t%s\t%s' % (path, vsc_bpid))
            self.bp_map.remove(pyd_bpid, vsc_bpid)

        for src_bp in src_bps:
            line = src_bp['line']
            vsc_bpid = self.bp_map.add(lambda vsc_bpid: (path, vsc_bpid))
            self.pydevd_notify(pydevd_comm.CMD_SET_BREAK, '%s\tpython-line\t%s\t%s\tNone\tNone\tNone' %
                (vsc_bpid, path, line))
            bps.append({ 'id': vsc_bpid, 'verified': True, 'line': line })

        self.send_response(request, breakpoints=bps)

    @async_handler
    def on_setExceptionBreakpoints(self, request, args):
        self.pydevd_notify(pydevd_comm.CMD_REMOVE_EXCEPTION_BREAK, 'python-BaseException') 
        filters = args['filters']
        break_raised = 'raised' in filters
        break_uncaught = 'uncaught' in filters
        if break_raised or break_uncaught:
            self.pydevd_notify(pydevd_comm.CMD_ADD_EXCEPTION_BREAK, 'python-BaseException\t%s\t%s\t%s' % (
                2 if break_raised else 0, 1 if break_uncaught else 0, 0))
        self.send_response(request)

    @async_handler
    def on_exceptionInfo(self, request, args):
        tid = self.thread_map.to_pydevd(args['threadId'])
        with self.active_exceptions_lock:
            exc = self.active_exceptions[tid]
        self.send_response(request, exceptionId=exc.name, description=exc.description, breakMode='unhandled', details={
            'typeName': exc.name,
            'message': exc.description,
        })            

    @pydevd_events.handler(pydevd_comm.CMD_THREAD_CREATE)
    def on_pydevd_thread_create(self, seq, args):
        xml = untangle.parse(args).xml
        tid = self.thread_map.to_vscode(xml.thread['id'])
        self.send_event('thread', reason='started', threadId=tid)

    @pydevd_events.handler(pydevd_comm.CMD_THREAD_KILL)
    def on_pydevd_thread_kill(self, seq, args):
        try:
            tid = self.thread_map.to_vscode(args, autogen=False)
        except KeyError:
            pass
        else:
            self.send_event('thread', reason='exited', threadId=tid)

    @pydevd_events.handler(pydevd_comm.CMD_THREAD_SUSPEND)
    def on_pydevd_thread_suspend(self, seq, args):
        xml = untangle.parse(args).xml
        tid = xml.thread['id']
        reason = int(xml.thread['stop_reason'])
        if reason in (pydevd_comm.CMD_STEP_INTO, pydevd_comm.CMD_STEP_OVER, pydevd_comm.CMD_STEP_RETURN):
            reason = 'step'
        elif reason == pydevd_comm.CMD_STEP_CAUGHT_EXCEPTION:
            reason = 'exception'
        elif reason == pydevd_comm.CMD_SET_BREAK:
            reason = 'breakpoint'
        else:
            reason = 'pause'
        with self.stack_traces_lock:            
            self.stack_traces[tid] = xml.thread.frame
        tid = self.thread_map.to_vscode(tid)
        self.send_event('stopped', reason=reason, threadId=tid)

    @pydevd_events.handler(pydevd_comm.CMD_THREAD_RUN)
    def on_pydevd_thread_run(self, seq, args):
        pyd_tid, reason = args.split('\t')
        vsc_tid = self.thread_map.to_vscode(pyd_tid)

        # Stack trace, and all frames and variables for this thread are now invalid; clear their IDs.
        with self.stack_traces_lock:
            del self.stack_traces[pyd_tid]

        for pyd_fid, vsc_fid in self.frame_map.pairs():
            if pyd_fid[0] == pyd_tid:
                self.frame_map.remove(pyd_fid, vsc_fid)

        for pyd_var, vsc_var in self.var_map.pairs():
            if pyd_var[0] == pyd_tid:
                self.var_map.remove(pyd_var, vsc_var)

        self.send_event('continued', threadId=vsc_tid)

    @pydevd_events.handler(pydevd_comm.CMD_SEND_CURR_EXCEPTION_TRACE)
    def on_pydevd_send_curr_exception_trace(self, seq, args):
        _, name, description, xml = args.split('\t')
        xml = untangle.parse(xml).xml
        pyd_tid = xml.thread['id']
        with self.active_exceptions_lock:
            self.active_exceptions[pyd_tid] = ExceptionInfo(name, description)

    @pydevd_events.handler(pydevd_comm.CMD_SEND_CURR_EXCEPTION_TRACE_PROCEEDED)
    def on_pydevd_send_curr_exception_trace_proceeded(self, seq, args):
        pass


def start_server(port):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', port))
    server.listen(1)
    client, addr = server.accept()

    pydevd = PydevdSocket(lambda *args: proc.on_pydevd_event(*args))
    proc = VSCodeMessageProcessor(client, pydevd)

    server_thread = threading.Thread(target = proc.process_messages, name = 'ptvsd.Server')
    server_thread.start()

    return pydevd

def start_client(host, port):
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    client.connect((host, port))

    pydevd = PydevdSocket(lambda *args: proc.on_pydevd_event(*args))
    proc = VSCodeMessageProcessor(client, pydevd)

    server_thread = threading.Thread(target = proc.process_messages, name = 'ptvsd.Client')
    server_thread.start()

    return pydevd
  
# These are the functions pydevd invokes to get a socket to the client.
pydevd_comm.start_server = start_server
pydevd_comm.start_client = start_client