""" Unittest helper methods """
import os
import random
import logging
import time
import datetime
import base64
from collections import defaultdict
from minimock import mock
import google.appengine.api.apiproxy_stub_map as apiproxy_stub_map
import fantasm
from fantasm import config
from fantasm.fsm import FSM
from google.appengine.ext import webapp
from google.appengine.api.taskqueue.taskqueue import TaskAlreadyExistsError

# pylint: disable-msg=C0111, C0103, W0613, W0612
# - docstrings not reqd in unit tests
# - mock interfaces need to inherit args with '_' in them

os.environ['APPLICATION_ID'] = 'fantasm'
APP_ID = os.environ['APPLICATION_ID']

class TaskDouble(object):
    """ TaskDouble is a mock for google.appengine.api.taskqueue.Task """
    def __init__(self, url, params=None, name=None, transactional=False, method='POST', countdown=0):
        """ Initialize MockTask """
        self.url = url
        self.params = params
        self.name = name or 'task-%s' % random.randint(100000000, 999999999)
        self.transactional = transactional
        self.method = method
        self.countdown = countdown

    def add(self, queue_name='default', transactional=False):
        """Adds this Task to a queue. See Queue.add."""
        return TaskQueueDouble(queue_name).add(self, transactional=transactional)

class TaskQueueDouble(object):
    """ TaskQueueDouble is a mock for google.appengine.api.lab.taskqueue.Queue """

    def __init__(self, name='default'):
        """ Initialize TaskQueueDouble object """
        self.tasknames = set([])
        self.tasks = []
        self.name = name
        
    def add(self, task_or_tasks, transactional=False):
        """ mock for google.appengine.api.taskqueue.add """
        if isinstance(task_or_tasks, list):
            tasks = task_or_tasks
            for task in tasks:
                if task.name in self.tasknames:
                    raise TaskAlreadyExistsError()
                self.tasknames.add(task.name)
            self.tasks.extend([(task, transactional) for task in tasks])
        else:
            task = task_or_tasks
            if task.name in self.tasknames:
                raise TaskAlreadyExistsError()
            self.tasknames.add(task.name)
            self.tasks.append((task, transactional))

    def purge(self):
        """ purge all tasks in queue """
        self.tasks = []

class LoggingDouble(object):

    def __init__(self):
        self.count = defaultdict(int)
        self.messages = defaultdict(list)

    def debug(self, message, *args, **kwargs):
        self.count['debug'] += 1
        self.messages['debug'].append(message % args)

    def info(self, message, *args, **kwargs):
        self.count['info'] += 1
        self.messages['info'].append(message % args)

    def warning(self, message, *args, **kwargs):
        self.count['warning'] += 1
        self.messages['warning'].append(message % args)

    def error(self, message, *args, **kwargs):
        self.count['error'] += 1
        self.messages['error'].append(message % args)
        
    def critical(self, message, *args, **kwargs):
        self.count['critical'] += 1
        self.messages['critical'].append(message % args)

def getLoggingDouble():
    """ Creates a logging double and wires it up with minimock. 
    
    You are responsible for restoring the minimock environment (ususally in your tearDown).
    
    @param printMessages A list of 'debug', 'info', 'warning', 'error', 'critical' indicating which
                         error levels to dump to STDERR.
    """
    loggingDouble = LoggingDouble()
    mock(name='logging.debug', returns_func=loggingDouble.debug, tracker=None)
    mock(name='logging.info', returns_func=loggingDouble.info, tracker=None)
    mock(name='logging.warning', returns_func=loggingDouble.warning, tracker=None)
    mock(name='logging.error', returns_func=loggingDouble.error, tracker=None)
    mock(name='logging.critical', returns_func=loggingDouble.critical, tracker=None)
    return loggingDouble

def runQueuedTasks(queueName='default'):
    """ Ability to run Tasks from unit/integration tests """
    # pylint: disable-msg=W0212
    #         allow access to protected member _IsValidQueue
    tq = apiproxy_stub_map.apiproxy.GetStub('taskqueue')
    assert tq._IsValidQueue(queueName, APP_ID)
    assert tq.GetTasks(queueName)
    
    retries = {}
    runList = []
    alreadyRun = []
    runAgain = True
    while runAgain:
        
        runAgain = False
        tasks = tq.GetTasks(queueName)
        lastRunList = list(runList)
            
        for task in tasks:
            
            if task['name'] in alreadyRun:
                continue
            
            if task.has_key('eta'):
                now = datetime.datetime.utcfromtimestamp(time.time())
                eta = datetime.datetime.strptime(task['eta'], "%Y/%m/%d %H:%M:%S")
                if runList == lastRunList:
                    # nothing ran list loop around, just force this task to speedup the tests
                    pass
                elif eta > now:
                    runAgain = True
                    continue
                
            record = True
            if task['url'] == '/fantasm/log/':
                record = False
                handler = fantasm.handlers.FSMLogHandler()
            else:
                handler = fantasm.handlers.FSMHandler()
            parts = task['url'].split('?')
            assert 1 <= len(parts) <= 2
            
            environ = {'PATH_INFO': parts[0]}
            if len(parts) == 2:
                environ['QUERY_STRING'] = parts[1]
            if task['method'] == 'POST':
                environ['CONTENT_TYPE'] = 'application/x-www-form-urlencoded'
            environ['REQUEST_METHOD'] = task['method']
            
            handler.request = webapp.Request(environ)
            
            if task['method'] == 'POST':
                handler.request.body = base64.decodestring(task['body'])
            
            handler.request.headers[random.choice(['X-AppEngine-TaskName', 
                                                   'X-Appengine-Taskname'])] = task['name']
            if retries.get(task['name']):
                handler.request.headers[random.choice(['X-AppEngine-TaskRetryCount', 
                                                       'X-Appengine-Taskretrycount'])] = retries[task['name']]
            
            try:
                {'GET': handler.get, 'POST': handler.post}[task['method']]() # call the correct dispatch
                runAgain = True
                alreadyRun.append(task['name'])
                if record:
                    runList.append(task['name'])
                
            except Exception:
                logging.debug("Error running Task. This would be a 500 error.", exc_info=1)
                runAgain = True
                if record:
                    runList.append(task['name'])
                retries[task['name']] = retries.get(task['name'], 0) + 1
            
    return runList

class ConfigurationMock(object):
    """ A mock object that looks like a config._Configuration instance """
    def __init__(self, machines):
        self.machines = dict([(m.name, m) for m in machines])
        
def getFSMFactoryByFilename(filename):
    """ Returns an FSM instance 
    
    @param filename: a filename like 'test-Foo.yaml'
    @return: an FSM instance 
    """
    machineName = getMachineNameByFilename(filename)
    filename = os.path.join(os.path.dirname(__file__), 'yaml', filename)
    currentConfig = config.loadYaml(filename=filename)
    factory = FSM(currentConfig=currentConfig)
    return factory
    
def getMachineNameByFilename(filename):
    """ Returns a fsm name based on the input filename 
    
    @param filename: a filename like 'test-Foo.yaml'
    @return: a machine name like 'Foo'
    """
    return filename.replace('test-', '').replace('.yaml', '')
    
def setUpByFilename(obj, filename, machineName=None, instanceName=None, maxRetriesOverrides=None, method='GET'):
    """ Configures obj (a unittest.TestCase instance) with obj.context 
    
    @param obj: a unittest.TestCase instance
    @param filename: a filename like 'test-Foo.yaml'
    @param machineName: a machine name define in filename
    @param instanceName: an fsm instance name   
    @param maxRetriesOverrides: a dict of {'transitionName' : maxRetries} use to override values in .yaml 
    """
    obj.machineName = machineName or filename.replace('test-', '').replace('.yaml', '')
    filename = os.path.join(os.path.dirname(__file__), 'yaml', filename)
    obj.currentConfig = config.loadYaml(filename=filename)
    obj.machineConfig = obj.currentConfig.machines[obj.machineName]
    if maxRetriesOverrides:
        overrideMaxRetries(obj.machineConfig, maxRetriesOverrides)
    obj.factory = FSM(currentConfig=obj.currentConfig)
    obj.context = obj.factory.createFSMInstance(obj.machineConfig.name, instanceName=instanceName, method=method)
    obj.initialState = obj.context.initialState

class ZeroCountMock(object):
    count = 0
    fcount = 0
    ccount = 0
    
def getCounts(machineConfig):
    """ Returns the count values from all the states' entry/action/exit FSMActions 
    
    @param machineConfig: a config._MachineConfig instance
    @return: a dict of { 'stateName' : {'entry' : count, 'action' : count, 'exit' : count } } 
    
    NOTE: relies on the config._StateConfig and FSMState instances sharing the FSMActions
    """
    counts = {}
    for stateName, state in machineConfig.states.items():
        if state.continuation:
            counts[state.name] = {'entry': (state.entry or ZeroCountMock).count, 
                                  'continuation': (state.action or ZeroCountMock).ccount,
                                  'action': (state.action or ZeroCountMock).count, 
                                  'exit': (state.exit or ZeroCountMock).count}
            
        elif state.fanInPeriod > 0:
            counts[state.name] = {'entry': (state.entry or ZeroCountMock).count, 
                                  'action': (state.action or ZeroCountMock).count, 
                                  'exit': (state.exit or ZeroCountMock).count,
                                  'fan-in-entry': (state.entry or ZeroCountMock).fcount, 
                                  'fan-in-action': (state.action or ZeroCountMock).fcount, 
                                  'fan-in-exit': (state.exit or ZeroCountMock).fcount}
            
        else:
            counts[state.name] = {'entry': (state.entry or ZeroCountMock).count, 
                                  'action': (state.action or ZeroCountMock).count, 
                                  'exit': (state.exit or ZeroCountMock).count}
                                  
    for transition in machineConfig.transitions.values():
        counts['%s--%s' % (transition.fromState.name, transition.event)] = {
            'action': (transition.action or ZeroCountMock).count
        }
        
    return counts

def overrideFails(machineConfig, fails, tranFails):
    """ Configures all the .fail parameters on the actions defined in fails
    
    @param machineConfig: a config._MachineConfig instance
    @param fails: a list of ('stateName', 'actionName', #failures)
        ie. [('state-initial', 'entry', 2), ('state-final', 'entry', 2)] 
    @param tranFails: a list of ('transitionName', #failures)
        ie. [('state-event', 1), ('state2-event2', 3)]
    
    NOTE: relies on the config._StateConfig and FSMState instances sharing the FSMActions
    """
    for (stateName, action, fail) in fails:
        state = machineConfig.states[stateName]
        if isinstance(fail, tuple):
            getattr(state, action).fails = fail[0]
            getattr(state, action).failat = fail[1]
            getattr(state, action).cfailat = fail[2]
        else:
            getattr(state, action).fails = fail
            
    for (tranName, fail) in tranFails:
        transition = machineConfig.transitions[tranName]
        transition.action.fails = fail
            
        
def overrideMaxRetries(machineConfig, overrides):
    """ Configures all the .maxRetries parameters on all the Transitions 
    
    @param machineConfig: a config._MachineConfig instance
    @param overrides: a dict of {'transitionName' : max_retries} to override
    """
    for (transitionName, maxRetries) in overrides.items():
        transition = machineConfig.transitions[transitionName]
        transition.maxRetries = maxRetries
