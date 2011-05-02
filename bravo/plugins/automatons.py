from random import choice

from twisted.internet.defer import inlineCallbacks
from twisted.internet.task import LoopingCall
from zope.interface import implements

from bravo.blocks import blocks
from bravo.ibravo import IAutomaton
from bravo.terrain.trees import NormalTree

class Trees(object):
    """
    Turn saplings into trees.
    """

    implements(IAutomaton)

    blocks = (blocks["sapling"].slot,)
    step = 2

    def __init__(self):
        self.saplings = set()
        self.loop = LoopingCall(self.process)

    def feed(self, factory, coords):
        self.saplings.add((factory, coords))
        if not self.loop.running:
            self.loop.start(self.step)

    @inlineCallbacks
    def process(self):
        factory, coords = choice(list(self.saplings))
        metadata = yield factory.world.get_metadata(coords)
        # Is this sapling ready to grow into a big tree? We use a bit-trick to
        # check.
        if metadata >= 12:
            # Tree time!
            tree = NormalTree(pos=coords)
            tree.make_trunk(factory.world)
            tree.make_foliage(factory.world)
            self.saplings.discard((factory, coords))
        else:
            # Increment metadata.
            metadata += 4
            factory.world.set_metadata(coords, metadata)
        if not self.saplings and self.loop.running:
            self.loop.stop()

    name = "trees"

trees = Trees()