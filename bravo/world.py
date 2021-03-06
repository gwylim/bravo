from functools import wraps
from itertools import product
import random
import sys
import weakref

from numpy import fromstring

from twisted.internet.defer import (inlineCallbacks, maybeDeferred,
                                    returnValue, succeed)
from twisted.internet.task import coiterate, LoopingCall
from twisted.python import log

from bravo.chunk import Chunk
from bravo.config import configuration
from bravo.entity import Player
from bravo.errors import ChunkNotLoaded, SerializerReadException
from bravo.ibravo import ISerializer, ISerializerFactory
from bravo.plugin import (retrieve_named_plugins, verify_plugin,
    PluginException)
from bravo.utilities.coords import split_coords
from bravo.utilities.temporal import PendingEvent

def coords_to_chunk(f):
    """
    Automatically look up the chunk for the coordinates, and convert world
    coordinates to chunk coordinates.
    """

    @wraps(f)
    def decorated(self, coords, *args, **kwargs):
        x, y, z = coords

        bigx, smallx, bigz, smallz = split_coords(x, z)
        d = self.request_chunk(bigx, bigz)

        @d.addCallback
        def cb(chunk):
            return f(self, chunk, (smallx, y, smallz), *args, **kwargs)

        return d

    return decorated

def sync_coords_to_chunk(f):
    """
    Either get a chunk for the coordinates, or raise an exception.
    """

    @wraps(f)
    def decorated(self, coords, *args, **kwargs):
        x, y, z = coords

        bigx, smallx, bigz, smallz = split_coords(x, z)
        bigcoords = bigx, bigz
        if bigcoords in self.chunk_cache:
            chunk = self.chunk_cache[bigcoords]
        elif bigcoords in self.dirty_chunk_cache:
            chunk = self.dirty_chunk_cache[bigcoords]
        else:
            raise ChunkNotLoaded("Chunk (%d, %d) isn't loaded" % bigcoords)

        return f(self, chunk, (smallx, y, smallz), *args, **kwargs)

    return decorated

class World(object):
    """
    Object representing a world on disk.

    Worlds are composed of levels and chunks, each of which corresponds to
    exactly one file on disk. Worlds also contain saved player data.
    """

    season = None
    """
    The current `ISeason`.
    """

    saving = True
    """
    Whether objects belonging to this world may be written out to disk.
    """

    async = False
    """
    Whether this world is using multiprocessing methods to generate geometry.
    """

    dimension = "earth"
    """
    The world dimension. Valid values are earth, sky, and nether.
    """

    permanent_cache = None
    """
    A permanent cache of chunks which are never evicted from memory.

    This cache is used to speed up logins near the spawn point.
    """

    spawn = (0, 0, 0)
    """
    The spawn point.
    """

    time = 0
    """
    The current time.

    This does not reflect the actual in-game time, just the time currently
    saved in the level data.
    """

    def __init__(self, name):
        """
        :Parameters:
            name : str
                The configuration key to use to look up configuration data.
        """

        self.config_name = "world %s" % name

        self.chunk_cache = weakref.WeakValueDictionary()
        self.dirty_chunk_cache = dict()

        self._pending_chunks = dict()

    def start(self):
        """
        Load a world from disk.
        """

        world_url = configuration.get(self.config_name, "url")
        world_sf_name = configuration.get(self.config_name, "serializer")

        try:
            sf = retrieve_named_plugins(ISerializerFactory, [world_sf_name])[0]
            self.serializer = verify_plugin(ISerializer, sf(world_url))
        except PluginException, pe:
            log.msg(pe)
            raise RuntimeError("Fatal error: Couldn't set up serializer!")

        self.seed = random.randint(0, sys.maxint)

        # Check if we should offload chunk requests to ampoule.
        if configuration.getbooleandefault("bravo", "ampoule", False):
            try:
                import ampoule
                if ampoule:
                    self.async = True
            except ImportError:
                pass

        # First, try loading the level, to see if there's any data out there
        # which we can use. If not, don't worry about it.
        d = maybeDeferred(self.serializer.load_level, self)
        d.addCallback(lambda chaff: log.msg("Loaded level data!"))
        @d.addErrback
        def sre(failure):
            failure.trap(SerializerReadException)
            log.msg("Had issues loading level data...")
            log.msg(failure)

        # And now save our level.
        if self.saving:
            self.serializer.save_level(self)

        self.chunk_management_loop = LoopingCall(self.sort_chunks)
        self.chunk_management_loop.start(1)

        log.msg("World started on %s, using serializer %s" %
            (world_url, self.serializer.name))
        log.msg("Using Ampoule: %s" % self.async)

    def stop(self):
        """
        Stop managing the world.

        This can be a time-consuming, blocking operation, while the world's
        data is serialized.

        Note to callers: If you want the world time to be accurate, don't
        forget to write it back before calling this method!
        """

        self.chunk_management_loop.stop()

        # Flush all dirty chunks to disk.
        for chunk in self.dirty_chunk_cache.itervalues():
            self.save_chunk(chunk)

        # Evict all chunks.
        self.chunk_cache.clear()
        self.dirty_chunk_cache.clear()

        # Save the level data.
        self.serializer.save_level(self)

    def enable_cache(self, size):
        """
        Set the permanent cache size.

        Changing the size of the cache sets off a series of events which will
        empty or fill the cache to make it the proper size.

        For reference, 3 is a large-enough size to completely satisfy the
        Notchian client's login demands. 10 is enough to completely fill the
        Notchian client's chunk buffer.

        :param int size: The taxicab radius of the cache, in chunks
        """

        log.msg("Setting cache size to %d..." % size)

        self.permanent_cache = set()
        def assign(chunk):
            self.permanent_cache.add(chunk)

        x = self.spawn[0] // 16
        z = self.spawn[2] // 16

        rx = xrange(x - size, x + size)
        rz = xrange(z - size, z + size)
        d = coiterate(self.request_chunk(x, z).addCallback(assign)
            for x, z in product(rx, rz))
        d.addCallback(lambda chaff: log.msg("Cache size is now %d" % size))

    def sort_chunks(self):
        """
        Sort out the internal caches.

        This method will always block when there are dirty chunks.
        """

        first = True

        all_chunks = dict(self.dirty_chunk_cache)
        all_chunks.update(self.chunk_cache)
        self.chunk_cache.clear()
        self.dirty_chunk_cache.clear()
        for coords, chunk in all_chunks.iteritems():
            if chunk.dirty:
                if first:
                    first = False
                    self.save_chunk(chunk)
                    self.chunk_cache[coords] = chunk
                else:
                    self.dirty_chunk_cache[coords] = chunk
            else:
                self.chunk_cache[coords] = chunk

    def save_off(self):
        """
        Disable saving to disk.

        This is useful for accessing the world on disk without Bravo
        interfering, for backing up the world.
        """

        if not self.saving:
            return

        d = dict(self.chunk_cache)
        self.chunk_cache = d
        self.saving = False

    def save_on(self):
        """
        Enable saving to disk.
        """

        if self.saving:
            return

        d = weakref.WeakValueDictionary(self.chunk_cache)
        self.chunk_cache = d
        self.saving = True

    def postprocess_chunk(self, chunk):
        """
        Do a series of final steps to bring a chunk into the world.

        This method might be called multiple times on a chunk, but it should
        not be harmful to do so.
        """

        # Apply the current season to the chunk.
        if self.season:
            self.season.transform(chunk)

        # Since this chunk hasn't been given to any player yet, there's no
        # conceivable way that any meaningful damage has been accumulated;
        # anybody loading any part of this chunk will want the entire thing.
        # Thus, it should start out undamaged.
        chunk.clear_damage()

        # Register the chunk's entities with our parent factory.
        for entity in chunk.entities:
            self.factory.register_entity(entity)

        # Return the chunk, in case we are in a Deferred chain.
        return chunk

    @inlineCallbacks
    def request_chunk(self, x, z):
        """
        Request a ``Chunk`` to be delivered later.

        :returns: ``Deferred`` that will be called with the ``Chunk``
        """

        if (x, z) in self.chunk_cache:
            returnValue(self.chunk_cache[x, z])
        elif (x, z) in self.dirty_chunk_cache:
            returnValue(self.dirty_chunk_cache[x, z])
        elif (x, z) in self._pending_chunks:
            # Rig up another Deferred and wrap it up in a to-go box.
            retval = yield self._pending_chunks[x, z].deferred()
            returnValue(retval)

        chunk = Chunk(x, z)
        yield maybeDeferred(self.serializer.load_chunk, chunk)

        if chunk.populated:
            self.chunk_cache[x, z] = chunk
            self.postprocess_chunk(chunk)
            self.factory.scan_chunk(chunk)
            returnValue(chunk)

        if self.async:
            from ampoule import deferToAMPProcess
            from bravo.remote import MakeChunk

            d = deferToAMPProcess(MakeChunk,
                x=x,
                z=z,
                seed=self.seed,
                generators=configuration.getlist(self.config_name, "generators")
            )

            # Get chunk data into our chunk object.
            def fill_chunk(kwargs):
                chunk.blocks = fromstring(kwargs["blocks"],
                    dtype="uint8").reshape(chunk.blocks.shape)
                chunk.heightmap = fromstring(kwargs["heightmap"],
                    dtype="uint8").reshape(chunk.heightmap.shape)
                chunk.metadata = fromstring(kwargs["metadata"],
                    dtype="uint8").reshape(chunk.metadata.shape)
                chunk.skylight = fromstring(kwargs["skylight"],
                    dtype="uint8").reshape(chunk.skylight.shape)
                chunk.blocklight = fromstring(kwargs["blocklight"],
                    dtype="uint8").reshape(chunk.blocklight.shape)

                return chunk
            d.addCallback(fill_chunk)
        else:
            # Populate the chunk the slow way. :c
            for stage in self.pipeline:
                stage.populate(chunk, self.seed)

            chunk.regenerate()
            d = succeed(chunk)

        # Set up our event and generate our return-value Deferred. It has to
        # be done early becaues PendingEvents only fire exactly once and it
        # might fire immediately in certain cases.
        pe = PendingEvent()
        # This one is for our return value.
        retval = pe.deferred()
        # This one is for scanning the chunk for automatons.
        pe.deferred().addCallback(self.factory.scan_chunk)
        self._pending_chunks[x, z] = pe

        def pp(chunk):
            chunk.populated = True
            chunk.dirty = True

            self.postprocess_chunk(chunk)

            self.dirty_chunk_cache[x, z] = chunk
            del self._pending_chunks[x, z]

            return chunk

        # Set up callbacks.
        d.addCallback(pp)
        d.chainDeferred(pe)

        # Because multiple people might be attached to this callback, we're
        # going to do something magical here. We will yield a forked version
        # of our Deferred. This means that we will wait right here, for a
        # long, long time, before actually returning with the chunk, *but*,
        # when we actually finish, we'll be ready to return the chunk
        # immediately. Our caller cannot possibly care because they only see a
        # Deferred either way.
        retval = yield retval
        returnValue(retval)

    def save_chunk(self, chunk):

        if not chunk.dirty or not self.saving:
            return

        self.serializer.save_chunk(chunk)

        chunk.dirty = False

    def load_player(self, username):
        """
        Retrieve player data.

        :returns: a ``Deferred`` that will be fired with a ``Player``
        """

        player = Player(username=username)
        player.location.x = self.spawn[0]
        player.location.y = self.spawn[1]
        player.location.stance = self.spawn[1]
        player.location.z = self.spawn[2]

        d = maybeDeferred(self.serializer.load_player, player)
        d.addCallback(lambda none: player)
        return d

    def save_player(self, username, player):
        if self.saving:
            self.serializer.save_player(player)

    # World-level geometry access.
    # These methods let external API users refrain from going through the
    # standard motions of looking up and loading chunk information.

    @coords_to_chunk
    def get_block(self, chunk, coords):
        """
        Get a block from an unknown chunk.

        :returns: a ``Deferred`` with the requested value
        """

        return chunk.get_block(coords)

    @coords_to_chunk
    def set_block(self, chunk, coords, value):
        """
        Set a block in an unknown chunk.

        :returns: a ``Deferred`` that will fire on completion
        """

        chunk.set_block(coords, value)

    @coords_to_chunk
    def get_metadata(self, chunk, coords):
        """
        Get a block's metadata from an unknown chunk.

        :returns: a ``Deferred`` with the requested value
        """

        return chunk.get_metadata(coords)

    @coords_to_chunk
    def set_metadata(self, chunk, coords, value):
        """
        Set a block's metadata in an unknown chunk.

        :returns: a ``Deferred`` that will fire on completion
        """

        chunk.set_metadata(coords, value)

    @coords_to_chunk
    def destroy(self, chunk, coords):
        """
        Destroy a block in an unknown chunk.

        :returns: a ``Deferred`` that will fire on completion
        """

        chunk.destroy(coords)

    @coords_to_chunk
    def mark_dirty(self, chunk, coords):
        """
        Mark an unknown chunk dirty.

        :returns: a ``Deferred`` that will fire on completion
        """

        chunk.dirty = True

    @sync_coords_to_chunk
    def sync_get_block(self, chunk, coords):
        """
        Get a block from an unknown chunk.

        :returns: a ``Deferred`` with the requested value
        """

        return chunk.get_block(coords)

    @sync_coords_to_chunk
    def sync_set_block(self, chunk, coords, value):
        """
        Set a block in an unknown chunk.

        :returns: a ``Deferred`` that will fire on completion
        """

        chunk.set_block(coords, value)

    @sync_coords_to_chunk
    def sync_get_metadata(self, chunk, coords):
        """
        Get a block's metadata from an unknown chunk.

        :returns: a ``Deferred`` with the requested value
        """

        return chunk.get_metadata(coords)

    @sync_coords_to_chunk
    def sync_set_metadata(self, chunk, coords, value):
        """
        Set a block's metadata in an unknown chunk.

        :returns: a ``Deferred`` that will fire on completion
        """

        chunk.set_metadata(coords, value)

    @sync_coords_to_chunk
    def sync_destroy(self, chunk, coords):
        """
        Destroy a block in an unknown chunk.

        :returns: a ``Deferred`` that will fire on completion
        """

        chunk.destroy(coords)

    @sync_coords_to_chunk
    def sync_mark_dirty(self, chunk, coords):
        """
        Mark an unknown chunk dirty.

        :returns: a ``Deferred`` that will fire on completion
        """

        chunk.dirty = True
