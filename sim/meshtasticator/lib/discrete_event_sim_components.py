from simpy import Environment as SimpyEnvironment

from lib.config import Config
from lib.discrete_event import BroadcastPipe

# This is where we put components of DiscreteEventSim that need to be referenced
# by other classes, to avoid circular imports

class Counter:
    """simple shared counter, so we have a more ergonomic way to get
    a strictly increasing integer for message IDs than manually handling an
    integer. Can add locking later if necessary (very likely won't be).
    """
    def __init__(self, start: int=0):
        """Create a counter

        Arguments:
        start -- integer to start counter at. First value from 'get' is start+1. Default 0.
        """
        self.counter = start

    def get(self):
        """increment the counter, returning its current value after the increment.
        """
        self.counter += 1
        return self.counter

    def peek(self):
        """return the current value of the counter. Do not modify the counter
        """
        return self.counter

class SimulationState:
    """Class to hold all global mutated state of a simulation, not including
    node-specific state such as the position of a moving node.
    """
    def __init__(self, conf: Config, env: SimpyEnvironment):
        """Constructor

        Arguments:
        conf -- Config object of global sim constants. Only used for NR_NODES.
        env -- SimPy Environment for simulation. Required for internal BroadcastPipe.
        """
        self.env = env
        self.bc_pipe = BroadcastPipe(self.env)
        self.packets = [] # used mostly for data tracking, but also for state
        self.packetsAtN = [[] for _ in range(conf.NR_NODES)]
        self.messageSeq = Counter()
        self.nodes = []
        self.connectivity_map = {} # node_id -> set of nodes which can hear it. (list would be faster for lookup)
        self.baseline_pathloss_matrix = [[None for _ in range(conf.NR_NODES)] for _ in range(conf.NR_NODES)] # cache for later lookup

class SimulationDataTracking:
    """Class to hold data used to monitor a simulation which has no
    impact on the state or progress of the simulation
    """
    def __init__(self):
        self.messages = []
        self.delays = []
        self.totalPairs = 0
        self.totalLinks = 0
        self.noLinks = 0
