from __future__ import division

import math
from functools import wraps
from time import time

# Coord handling.

def split_coords(x, z):
    """
    Split a pair of coordinates into chunk and subchunk coordinates.

    :param int x: the X coordinate
    :param int z: the Z coordinate

    :returns: a tuple of the X chunk, X subchunk, Z chunk, and Z subchunk
    """

    first, second = divmod(int(x), 16)
    third, fourth = divmod(int(z), 16)

    return first, second, third, fourth

def triplet_to_index(coords):
    """
    Calculate the index for a set of subchunk coordinates.

    :param tuple coords: X, Y, Z coordinates

    :returns: integer index into chunk data
    :raises Exception: the coordinates are out-of-bounds
    """

    x, y, z = coords

    retval = (x * 16 + z) * 128 + y

    if not 0 <= retval < 16 * 128 * 16:
        raise Exception("%d, %d, %d causes OOB index %d" % (x, y, z, retval))

    return retval

def index_to_triplet(index):
    """
    Calculate the subchunk coordinates for an index.

    :param int index: index

    :returns: tuple of subchunk coordinates
    :raises Exception: the index is out-of-bounds
    """

    if not 0 <= index < 16 * 128 * 16:
        raise Exception("%d is an OOB index" % index)

    xz, y = divmod(index, 128)
    x, z = divmod(xz, 16)

    return (x, y, z)

# Bit twiddling.

def unpack_nibbles(l):
    """
    Unpack bytes into pairs of nibbles.

    Nibbles are half-byte quantities. The nibbles unpacked by this function
    are returned as unsigned numeric values.

    >>> unpack_nibbles(["a"])
    [6, 1]
    >>> unpack_nibbles("nibbles")
    [6, 14, 6, 9, 6, 2, 6, 2, 6, 12, 6, 5, 7, 3]

    :param list l: bytes

    :returns: list of nibbles
    """

    retval = []
    for i in l:
        i = ord(i)
        retval.append(i >> 4)
        retval.append(i & 15)
    return retval

def pack_nibbles(l):
    """
    Pack pairs of nibbles into bytes.

    Bytes are returned as characters.

    :param list l: nibbles to pack

    :returns: list of bytes
    """

    it = iter(l)
    return [chr(i << 4 | j) for i, j in zip(it, it)]

# Trig.

def rotated_cosine(x, y, theta, lambd):
    r"""
    Evaluate a rotated 3D sinusoidal wave at a given point, angle, and
    wavelength.

    The function used is:

    .. math::

       f(x, y) = -\cos((x \cos\theta - y \sin\theta) / \lambda) / 2 + 1

    This function has a handful of useful properties; it has a local minimum
    at f(0, 0) and oscillates infinitely betwen 0 and 1.

    :param float x: X coordinate
    :param float y: Y coordinate
    :param float theta: angle of rotation
    :param float lambda: wavelength

    :returns: float of f(x, y)
    """

    return -math.cos((x * math.cos(theta) - y * math.sin(theta)) / lambd) / 2 + 1

# Decorators.

timers = {}

def timed(f):
    timers[f] = (0, 0)
    @wraps(f)
    def deco(*args, **kwargs):
        before = time()
        retval = f(*args, **kwargs)
        after = time()
        count, average = timers[f]
        # MMA
        average = (9 * average + after - before) / 10
        count += 1
        if not count % 10:
            print "Average time for %s: %dms" % (f, average * 1000)
        timers[f] = (count, average)
        return retval
    return deco
