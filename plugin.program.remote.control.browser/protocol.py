import abc
import collections

Volume = collections.namedtuple('Volume', ['muted', 'volume'])

class VolumeStrategy:
    __metaclass__ = abc.ABCMeta

    @abc.abstractmethod
    def execute(self):
        """Perform an operation on the volume"""
        pass

class IncrementVolumeStrategy(VolumeStrategy):
    def execute(self):
        
