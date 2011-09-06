"""voldemort_client is a synchronous Python client for Voldemort

How to start a Voldemort server for testing::

    $ ./bin/voldemort-server.sh config/single_node_cluster/


TCP Usage::

    v = Voldemort('tcp://localhost:6666')
    st = v.get_store('test')
    st.put('foo', bar')
    value, version = st.get('foo')
    assert value == 'bar'

HTTP Usage::

    v = Voldemort('http://localhost:8081')
    st = v.get_store('test')
    st.put('foo', bar')
    value, version = st.get('foo')
    assert value == 'bar'

"""
from __future__ import with_statement

import time
import array
import struct
import base64
import socket
import httplib
import urlparse
import binascii
from contextlib import closing
from xml.etree import ElementTree as ET

import random
import logging
from os import mkdir
from os.path import exists, abspath
from urllib import pathname2url
from rdflib import URIRef
from rdflib.store import Store
from rdflib.store import VALID_STORE

log = _logger = logging.getLogger(__name__)

__version__ = '0.1'

VERSION_HEADER = 'X-vldmt-version'
CLUSTER_KEY = 'cluster.xml'
STORES_KEY = 'stores.xml'
MAX_NUMBER_OF_VERSIONS = 0x7fff

class InconsistentDataError(ValueError):
    pass


class ObsoleteVersionError(ValueError):
    pass


def b2i_uint(arr, offset, num_bytes):
    rval = 0
    for i in xrange(offset , offset + num_bytes):
        rval = (rval << 8) | arr[i]
    return rval

def i2b_uint_len(n):
    for i in xrange(1, 8 + 1):
        if n < (1 << (8 * i)):
            return i
    raise ValueError("%r will not fit in 64-bits" % (n,))


def i2b_uint(value, num_bytes):
    """
    >>> i2b_uint(256, 2)
    [1, 0]
    >>> i2b_uint(255, 2)
    [0, 255]

    """
    return [(value >> i) & 0xff for i in xrange(8 * (num_bytes - 1), -1, -8)]


def compare_vector_clocks(v1, v2):
    """Returns:

    * ``0`` if concurrent
    * ``-1`` if before; equal (arbitrarily) or v2 is a successor
    * ``1`` if after; v1 is a successor

    """
    v1_bigger = False
    v2_bigger = False
    ver1 = v1.versions
    ver2 = v2.versions
    p1 = 0
    p2 = 0
    while p1 < len(ver1) and p2 < len(ver2):
        v1_node, v1_version = ver1[p1]
        v2_node, v2_version = ver2[p2]
        if v1_node == v2_node:
            if v1_version > v2_version:
                v1_bigger = True
            elif v2_version > v1_version:
                v2_bigger = True
            p1 += 1
            p2 += 1
        elif v1_node > v2_node:
            # v1 is missing a version that v2 has
            v2_bigger = True
            p2 += 1
        else:
            # v2 is missing a version that v1 has
            v1_bigger = True
            p1 += 1

    # check for leftover versions
    if p1 < len(ver1):
        v1_bigger = True
    elif p2 < len(ver2):
        v2_bigger = True

    if not (v1_bigger or v2_bigger):
        # return -1 arbitrarily for equal clocks
        return -1
    elif v1_bigger and v2_bigger:
        # parallel clocks return 0
        return 0
    elif v1_bigger:
        # v1 is a successor to v2, return 1
        return 1
    else:
        # v2 is a successor to v1, return -1
        return -1


class VectorClock(object):
    """
    >>> v = VectorClock.from_base64('AAEBAAABAAABH030x/Y=')
    >>> v.versions == [(0, 1)]
    True
    >>> v.timestamp == 1233963501558L
    True
    >>> v.to_base64() == 'AAEBAAABAAABH030x/Y='
    True

    """
    def __init__(self, versions=None, timestamp=None):
        if timestamp is None:
            timestamp = int(time.time() * 1000)
        if versions is None:
            versions = []
        self.timestamp = timestamp
        self.versions = versions

    @classmethod
    def from_bytes(cls, bytes):
        num_entries, version_size = struct.unpack('>HB', bytes[:3])
        entry_size = 2 + version_size
        min_bytes = 2 + 1 + (num_entries * entry_size) + 8
        if len(bytes) < min_bytes:
            raise ValueError(
                "Too few bytes: expected at least %d but found only %d" % (
                    min_bytes, len(bytes)))
        a = array.array('B', bytes[3:3 + min_bytes])
        index = 0
        entries = []
        for _ in xrange(num_entries):
            node_id = (a[index] << 8) | a[index + 1]
            version = b2i_uint(a, index + 2, version_size)
            entries.append((node_id, version))
            index += entry_size
        timestamp = struct.unpack('>Q', bytes[index + 3:index + 3 + 8])[0]
        return cls(entries, timestamp)

    @classmethod
    def from_base64(cls, s):
        return cls.from_bytes(base64.standard_b64decode(s))

    def to_bytes(self):
        max_version = (max(version for (node_id, version) in self.versions)
                       if self.versions else 0)
        version_size = i2b_uint_len(max_version)
        num_entries = len(self.versions)
        a = array.array('B', [
            (num_entries >> 8) & 0xff, num_entries & 0xff,
            version_size,
        ])
        for (node_id, version) in self.versions:
            a.extend([(node_id >> 8) & 0xff, node_id & 0xff] +
                     i2b_uint(version, version_size))
        return a.tostring() + struct.pack('>Q', self.timestamp)

    def to_base64(self):
        return base64.standard_b64encode(self.to_bytes())

    @property
    def size_in_bytes(self):
        max_version = (max(version for (node_id, version) in self.versions)
                       if self.versions else 0)
        version_size = i2b_uint_len(max_version)
        return 2 + 1 + (len(self.versions) * (2 + version_size)) + 8

    def __repr__(self):
        return '%s(%r, %r)' % (
            type(self).__name__, self.versions, self.timestamp)

    def __hash__(self):
        return hash(self.versions)

    def __eq__(self, other):
        if not isinstance(other, VectorClock):
            raise TypeError("VectorClock can only be compared to VectorClock")
        # NOTE: the timestamp on the VectorClock is not used for equality
        return (self is other) or (self.versions == other.versions)

    def incremented(self, node_id, timestamp=None):
        versions = list(self.versions)
        if 0 > node_id > 0x7fff:
            raise ValueError(
                "%r is outside of the acceptable range of node ids" % (
                    node_id,))
        for i, (v_node_id, version) in enumerate(self.versions):
            if v_node_id == node_id:
                versions[i] = (v_node_id, 1 + version)
                break
        else:
            if len(versions) > MAX_NUMBER_OF_VERSIONS:
                raise ValueError("Vector clock is full!")
            versions.append((node_id, 1))
        return type(self)(versions, timestamp)


def utf8_str(s):
    """``s.encode('utf-8') if isinstance(s, unicode) else str(s)``

    >>> isinstance(utf8_str(u''), str)
    True
    >>> utf8_str(1) == '1'
    True
    """
    return s.encode('utf-8') if isinstance(s, unicode) else str(s)


def fnv_hash(bytes):
    hash = 0x811c9dc5
    fnv_prime = 0x1000193
    for c in array.array('B', bytes):
        hash = 0xffffffff & ((hash ^ c) * fnv_prime)
    if hash & 0x80000000:
        return hash - 0x100000000
    else:
        return hash


class StringSerializer(object):
    def __init__(self, schema_map={}, has_version=False):
        assert schema_map == {}
        assert has_version == False
        self.schema_map = schema_map
        self.has_version = has_version
        self.newest_version = max(schema_map) if schema_map else 0

    def to_bytes(self, s):
        return utf8_str(s)

    def from_bytes(self, bytes):
        return bytes.decode('utf-8')

    def __repr__(self):
        ATTRS = 'schema_map', 'has_version'
        return '<%s %s>' % (
            type(self).__name__,
            ' '.join('%s=%r' % (k, getattr(self, k)) for k in ATTRS))


class NotJSONSerializer(object):
    def __init__(self, schema_map, has_version):
        assert schema_map == {0: '"string"'}
        assert has_version == True
        self.schema_map = schema_map
        self.has_version = has_version
        self.newest_version = max(schema_map) if schema_map else 0

    def to_bytes(self, s):
        if self.has_version:
            v = chr(self.newest_version)
        else:
            v = ''
        if s is None:
            return v + struct.pack('>h', -1)
        s = utf8_str(s)
        return v + struct.pack('>h', len(s)) + s

    def from_bytes(self, bytes):
        offset = 0
        if self.has_version:
            v = ord(bytes[0])
            offset += 1
        else:
            v = 0
        assert self.schema_map[v] == '"string"'
        string_len = struct.unpack('>h', bytes[offset:offset + 2])[0]
        offset += 2
        if string_len == -1:
            return None
        return bytes[offset:offset + string_len].decode('utf-8')

    def __repr__(self):
        ATTRS = 'schema_map', 'has_version'
        return '<%s %s>' % (
            type(self).__name__,
            ' '.join('%s=%r' % (k, getattr(self, k)) for k in ATTRS))


def serializer(name, schema_map, has_version):
    SERIALIZERS = {
        'json': NotJSONSerializer,
        'string': StringSerializer,
    }
    return SERIALIZERS[name](schema_map, has_version)


def serializer_from_xml(x):
    schema_map = {}
    has_version = True
    for info in x.findall('schema-info'):
        v = x.get('version')
        if v is None:
            version = 0
        elif v == 'none':
            version = 0
            has_version = False
        else:
            version = int(v)
        schema_map[version] = info.text
    return serializer(
        name=x.findtext('type'),
        schema_map=schema_map,
        has_version=has_version,
    )


def socksend(sock, lst):
    for chunk in lst:
        sock.sendall(chunk)


def sockrecv(sock, bytes):
    d = ''
    while len(d) < bytes:
        d += sock.recv(min(8192, bytes - len(d)))
    return d


class VoldemortTCP(object):
    OP_CODE = dict(GET=1, PUT=2, DELETE=3)

    def __init__(self, host, socket_port):
        self.host = host
        self.socket_port = socket_port

    def get_connection(self):
        sock = socket.socket()
        sock.connect((self.host, self.socket_port))
        sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        return sock

    def send_cmd(self, conn, op, store_name, packed_key, extra):
        store_name = utf8_str(store_name)
        iolist = [
            chr(self.OP_CODE[op]),
            struct.pack('>h', len(store_name)),
            store_name,
            struct.pack('>i', len(packed_key)),
            packed_key,
        ] + extra
        socksend(conn, iolist)
        err_code = struct.unpack('>h', sockrecv(conn, 2))[0]
        if err_code == 0:
            return
        err_msg_len = struct.unpack('>h', sockrecv(conn, 2))[0]
        err_msg = sockrecv(conn, err_msg_len)
        if err_code == 4:
            raise ObsoleteVersionError(err_msg)
        elif err_code == 8:
            raise InconsistentDataError(err_msg)
        else:
            raise ValueError("Unknown Voldemort error code %d: %s" %
                             (err_code, err_msg))

    def get_raw(self, store_name, packed_key):
        res = []
        with closing(self.get_connection()) as conn:
            self.send_cmd(conn, 'GET', store_name, packed_key, [])
            num_results = struct.unpack('>i', sockrecv(conn, 4))[0]
            for i in xrange(num_results):
                chunk_len = struct.unpack('>i', sockrecv(conn, 4))[0]
                chunk = sockrecv(conn, chunk_len)
                clock = VectorClock.from_bytes(chunk)
                res.append((chunk[clock.size_in_bytes:], clock))
        return res

    def put_raw(self, store_name, packed_key, packed_value, version):
        packed_version = version.to_bytes()
        chunk = packed_version + packed_value
        with closing(self.get_connection()) as conn:
            self.send_cmd(conn, 'PUT', store_name, packed_key, [
                struct.pack('>i', len(chunk)),
                chunk,
            ])

    def delete_raw(self, store_name, packed_key, version):
        packed_version = version.to_bytes()
        with closing(self.get_connection()) as conn:
            self.send_cmd(conn, 'DELETE', store_name, packed_key, [
                struct.pack('>h', len(packed_version)),
                packed_version,
            ])
            return sockrecv(conn, 1) == '\x01'


class VoldemortHTTP(object):
    def __init__(self, host, http_port):
        self.host = host
        self.http_port = http_port

    def get_connection(self):
        """Get the raw :class:`HTTPConnection` to Voldemort"""
        return httplib.HTTPConnection('%s:%s' % (self.host, self.http_port))

    def store_path(self, store_name, packed_key):
        return '/%s/%s' % (
            store_name,
            binascii.b2a_hex(packed_key))

    def http(self, conn, method, path, data=None, version=None):
        """Make a HTTP request to Voldemort, return the response body.

        * *method* must be ``'GET'``, ``'PUT'`` or ``'DELETE'``
        """
        headers = {}
        if not path[:1] == '/':
            path = '/' + path
        if method == 'GET':
            if data is not None:
                raise TypeError("data must be None for GET")
        elif method == 'DELETE':
            if data is not None:
                raise TypeError("data must be None for DELETE")
            if version is None:
                raise TypeError("version is required for DELETE")
            headers[VERSION_HEADER] = version.to_base64()
        elif method == 'PUT':
            if data is None:
                raise TypeError("data must not be None for PUT")
            if version is None:
                raise TypeError("version is required for DELETE")
            headers[VERSION_HEADER] = version.to_base64()
            headers['Content-length'] = str(len(data))
        else:
            raise ValueError("Voldemort does not support method %r" % (
                    method,))

        conn.request(method, path, data, headers)
        response = conn.getresponse()
        status, body = response.status, response.read()
        if 200 <= status < 300:
            return body
        elif status == 409:
            junk = body[body.index('<pre>'):body.rindex('</pre>') + 6]
            message = ET.XML(ET.XML(junk).text).findtext('message')
            raise ObsoleteVersionError(message)
        else:
            raise ValueError("Voldemort response failure for %s %s %s" % (
                    path, response.status, response.reason))


    def get_raw(self, store_name, packed_key):
        path = self.store_path(store_name, packed_key)
        with closing(self.get_connection()) as conn:
            bytes = self.http(conn, 'GET', path)
        index = 0
        res = []
        while index < len(bytes):
            size = struct.unpack('>i', bytes[index:index + 4])[0]
            index += 4
            chunk = bytes[index:index + size]
            index += size
            clock = VectorClock.from_bytes(chunk)
            res.append((chunk[clock.size_in_bytes:], clock))
        return res

    def put_raw(self, store_name, packed_key, packed_value, version):
        path = self.store_path(store_name, packed_key)
        with closing(self.get_connection()) as conn:
            self.http(conn,
                      'PUT',
                      path,
                      packed_value,
                      version)

    def delete_raw(self, store_name, packed_key, version):
        path = self.store_path(store_name, packed_key)
        with closing(self.get_connection()) as conn:
            self.http(conn,
                      'DELETE',
                      path,
                      None,
                      version)


class Node(object):
    def __init__(self, id, host, http_port, socket_port, partitions):
        self.id = id
        self.host = host
        self.http_port = http_port
        try:
            socket_port = int(socket_port)
        except (ValueError, TypeError):
            socket_port = None
        self.socket_port = socket_port
        self.partitions = partitions
        if socket_port:
            t = VoldemortTCP(host, socket_port)
        else:
            t = VoldemortHTTP(host, http_port)
        self.transport = t
        self.get_raw = t.get_raw
        self.delete_raw = t.delete_raw
        self.put_raw = t.put_raw

    @classmethod
    def from_xml(cls, server):
        return Node(
            id=int(server.findtext('id')),
            host=server.findtext('host'),
            http_port=server.findtext('http-port'),
            socket_port=server.findtext('socket-port'),
            partitions=[
                int(s.strip())
                for s in server.findtext('partitions').split(',')
                if s.strip()
            ],
        )

    @classmethod
    def from_url(cls, url):
        scheme, netloc, _path, _query, _frag = urlparse.urlsplit(url)
        socket_port = None
        http_port = None
        if scheme == 'http':
            host, _, http_port = netloc.partition(':')
            if not http_port:
                http_port = '8081'
        elif scheme == 'tcp':
            host, _, socket_port = netloc.partition(':')
            if not socket_port:
                socket_port = '6666'
        else:
            raise ValueError("Invalid URL scheme for Voldemort %r" % (url,))
        return cls(
            id=None,
            host=host,
            http_port=http_port,
            socket_port=socket_port,
            partitions=None)

    def __eq__(self, other):
        return self.id == other.id

    def __hash__(self):
        return hash(self.id)

    def __repr__(self):
        ATTRS = 'id', 'host', 'http_port', 'socket_port', 'partitions'
        return '<%s %s>' % (
            type(self).__name__,
            ' '.join('%s=%r' % (k, getattr(self, k)) for k in ATTRS))


class VStore(object):
    def __init__(self, name, persistence, routing, replication_factor,
                 required_reads, required_writes, preferred_reads,
                 preferred_writes, retention_days, key_serializer,
                 value_serializer):
        self.name = name
        self.persistence = persistence
        self.routing = routing
        self.replication_factor = replication_factor
        self.required_reads = required_reads
        self.required_writes = required_writes
        self.preferred_reads = preferred_reads
        self.preferred_writes = preferred_writes
        self.retention_days = retention_days
        self.key_serializer = key_serializer
        self.value_serializer = value_serializer

    @classmethod
    def from_xml(cls, store):
        def int_optional(x, k):
            s = x.findtext(k)
            return int(s) if s else None

        return cls(
            name=store.findtext('name'),
            persistence=store.findtext('persistence'),
            routing=store.findtext('routing'),
            replication_factor=int(store.findtext('replication-factor')),
            required_reads=int(store.findtext('required-reads')),
            required_writes=int(store.findtext('required-writes')),
            preferred_reads=int_optional(store, 'preferred-reads'),
            preferred_writes=int_optional(store, 'preferred-reads'),
            retention_days=int_optional(store, 'retention-days'),
            key_serializer=serializer_from_xml(store.find('key-serializer')),
            value_serializer=serializer_from_xml(
                store.find('value-serializer')),
        )

    def __repr__(self):
        ATTRS = ('name', 'persistence', 'routing', 'replication_factor',
                 'required_reads', 'required_writes', 'preferred_reads',
                 'preferred_writes', 'retention_days', 'key_serializer',
                 'value_serializer')
        return '<%s %s>' % (
            type(self).__name__,
            ' '.join('%s=%r' % (k, getattr(self, k)) for k in ATTRS))


class ConsistentRouter(object):
    def __init__(self, store, nodes):
        self.store = store
        self.nodes = nodes
        self.num_replicas = store.replication_factor
        pmap = {}
        for node in nodes:
            for partition in node.partitions:
                if partition in pmap:
                    raise ValueError(
                        "Duplicate partition id %s in cluster configuration" %
                        (partition,))
                pmap[partition] = node
        plist = []
        for i in xrange(len(pmap)):
            try:
                plist.append(pmap[i])
            except KeyError:
                raise ValueError("Missing tag %s" % (i,))
        self.partitions = plist

    def route_request(self, key):
        p = self.partitions
        num_results = self.num_replicas
        res = []
        index = abs(fnv_hash(key)) % len(p)
        for _i in xrange(len(p)):
            node = p[index]
            if node not in res:
                res.append(node)
            if len(res) >= num_results:
                # we have enough results, go home
                return res
            index = (index + 1) % len(p)
        # we don't have enough results, but that might be ok
        return res

    def resolve_conflicts(self, pairs):
        # VectorClock based inconsistency resolver
        if len(pairs) <= 1:
            return pairs
        pairs.sort(cmp=lambda a,b: compare_vector_clocks(a[1], b[1]))
        last_data, last_clock = pairs.pop()
        concurrent = [(last_data, last_clock)]
        for (data, clock) in reversed(pairs):
            if compare_vector_clocks(clock, last_clock) == 0:
                concurrent.append((data, clock))
            else:
                break
        pairs = concurrent
        # timestamp based inconsistency resolver
        if len(pairs) <= 1:
            return pairs
        max_timestamp = None
        max_pair = None
        for pair in pairs:
            if max_timestamp is None or pair[1].timestamp > max_timestamp:
                max_pair = pair
        return [max_pair]

    def read_repair(self, store_name, packed_key, retrieved):
        if len(retrieved) <= 1:
            return retrieved

        # TODO: implement read repair
        return retrieved

    def get_raw(self, store_name, packed_key):
        results = []
        for node in self.route_request(packed_key):
            res = node.get_raw(store_name, packed_key)
            results.extend((node, v) for v in res)
        results = self.read_repair(store_name, packed_key, results)
        return self.resolve_conflicts([v for (node, v) in results])

    def delete_raw(self, store_name, packed_key, version):
        for node in self.route_request(packed_key):
            node.delete_raw(store_name, packed_key, version)

    def put_raw(self, store_name, packed_key, packed_value, version):
        nodes = self.route_request(packed_key)
        master = nodes[0]
        master_v = version.incremented(master.id)
        master.put_raw(store_name, packed_key, packed_value, master_v)
        for node in nodes[1:]:
            node.put_raw(store_name, packed_key, packed_value, master_v)
        return version.incremented(master.id)


class VoldemortStore(object):
    def __init__(self, connection, store_name,
                 key_serializer, value_serializer):
        self.connection = connection
        self.store_name = store_name
        self.key_serializer = key_serializer
        self.value_serializer = value_serializer

    def get(self, key, default=None):
        packed_key = self.key_serializer.to_bytes(key)
        results = self.connection.get_raw(self.store_name, packed_key)
        if not results:
            return default, None
        elif len(results) == 1:
            chunk, clock = results[0]
            return self.value_serializer.from_bytes(chunk), clock
        else:
            raise InconsistentDataError(
                "Unresolved versions for key %r" % (key,))

    def get_value(self, key, default=None):
        return self.get(key, default)[0]

    def locate(self, key):
        packed_key = self.key_serializer.to_bytes(key)
        return self.connection.route_request(packed_key)

    def put(self, key, value, version=None):
        if version is None:
            version = self.get(key)[1] or VectorClock()
        packed_key = self.key_serializer.to_bytes(key)
        packed_value = self.value_serializer.to_bytes(value)
        return self.connection.put_raw(self.store_name,
                                       packed_key, packed_value, version)

    def delete(self, key):
        version = self.get(key)[1]
        if version is None:
            return False
        packed_key = self.key_serializer.to_bytes(key)
        self.connection.delete_raw(self.store_name, packed_key, version)
        return True


class Voldemort(object):
    """Voldemort HTTP connection
    """

    def __init__(self, bootstrap_url):
        """Create a Voldemort connection

        """
        self.bootstrap_url = bootstrap_url
        # name, nodes, stores, routing come from bootstrap info
        self.name = None
        self.nodes = None
        self.stores = None
        self.bootstrap()

    def __repr__(self):
        return '<%s name=%r bootstrap_url=%r>' % (
            type(self).__name__, self.name, self.bootstrap_url)


    def get_store(self, store_name):
        store = self.stores[store_name]
        return VoldemortStore(ConsistentRouter(store, self.nodes),
                              store_name,
                              store.key_serializer,
                              store.value_serializer)

    def bootstrap(self):
        store = VoldemortStore(Node.from_url(self.bootstrap_url),
                               'metadata',
                               StringSerializer(),
                               StringSerializer())
        cluster_xml = store.get(CLUSTER_KEY)[0]
        stores_xml = store.get(STORES_KEY)[0]
        if cluster_xml is None or stores_xml is None:
            raise ValueError(
                "Couldn't bootstrap cluster.xml and/or stores.xml")

        cx = ET.XML(cluster_xml)
        self.name = cx.findtext('name')
        self.nodes = map(Node.from_xml, cx.findall('server'))
        sx = ET.XML(stores_xml)
        self.stores = dict(
            (store.name, store)
            for store in map(VStore.from_xml, sx.findall('store')))

class NoopMethods(object):
    def __getattr__(self, methodName):
        return lambda *args: None
    
class VoldemortRDF(Store):
    context_aware = True
    formula_aware = True
    transaction_aware = False

    def __init__(self, configuration=None, identifier=None):
        self.__open = False
        self.__identifier = identifier
        super(VoldemortRDF, self).__init__(configuration)
        self.configuration = configuration
        self._loads = self.node_pickler.loads
        self._dumps = self.node_pickler.dumps
    
    def __get_identifier(self):
        return self.__identifier
    identifier = property(__get_identifier)
    
    def is_open(self):
        return self.__open

    def open(self, path, create=True):
        self.create = create
        self.path = homeDir = path
        if not exists(homeDir):
            if create:
                mkdir(homeDir)
            else:
                raise ValueError("graph path %r does not exist" % homeDir)
        if self.identifier is None:
            self.__identifier = URIRef(pathname2url(abspath(homeDir)))
        self.voldemortconn = Voldemort('tcp://localhost:6666')

        
        def dbOpen(name):
            db = self.voldemortconn.get_store(name)
            return db
                
        # create and open the DBs
        self.__indices = [None,] * 3
        self.__indices_info = [None,] * 3
        for i in xrange(0, 3):
            index_name = to_key_func(i)(("s", "p", "o"), "c")
            index = dbOpen(index_name)
            self.__indices[i] = index
            self.__indices_info[i] = (index, to_key_func(i), from_key_func(i))
        
        lookup = {}
        for i in xrange(0, 8):
            results = []
            for start in xrange(0, 3):
                score = 1
                len = 0
                for j in xrange(start, start+3):
                    if i & (1<<(j%3)):
                        score = score << 1
                        len += 1
                    else:
                        break
                tie_break = 2-start
                results.append(((score, tie_break), start, len))
            
            results.sort()
            score, start, len = results[-1]
            
            def get_prefix_func(start, end):
                def get_prefix(triple, context):
                    if context is None:
                        yield ""
                    else:
                        yield context
                    i = start
                    while i<end:
                        yield triple[i%3]
                        i += 1
                    yield ""
                return get_prefix
            
            lookup[i] = (self.__indices[start], 
                            get_prefix_func(start, start + len), 
                            from_key_func(start), 
                            results_from_key_func(start, self._from_string))

        self.__lookup_dict = lookup

        # these 3 were btree mode in sleepycat, but currently i'm using tc hash
        self.__contexts = dbOpen("contexts")
        self.__namespace = dbOpen("namespace")
        self.__prefix = dbOpen("prefix")
        self.__k2i = dbOpen("k2i")
        self.__i2k = dbOpen("i2k") # was DB_RECNO mode
        self.__journal = NoopMethods() # was DB_RECNO mode
 
        self.__needs_sync = False
        # t = Thread(target=self.__sync_run)
        # t.setDaemon(True)
        # t.start()
        # self.__sync_thread = t
        self.__sync_thread = NoopMethods()
        # self.synchronize()
        self.__open = True

        return VALID_STORE

    
    def close(self, commit_pending_transaction=False):
        log.debug("Closing store")
        # self.__sync_thread.join()
        for i in self.__indices:
            i.close()
        self.__contexts.close()
        self.__namespace.close()
        self.__prefix.close()
        self.__i2k.close()
        self.__k2i.close()
        # self.db_env.close()
        self.__open = False


    def destroy(self, configuration=''):
        import os
        path = configuration or self.homeDir
        if os.path.exists(path):
            for f in os.listdir(path): 
                os.unlink(path+'/'+f)
            os.rmdir(path)

    def __len__(self, context=None):
        assert self.__open, "The Store must be open."
        if context is not None:
            if context == self:
                context = None
        
        if context is None:
            prefix = "^"
        else:
            prefix = "%s^" % self._to_string(context)
        
        return len([key for key in self.__indices[0] 
                            if key.startswith(prefix)])

    def add(self, (subject, predicate, object), context, quoted=False):
        """\
        Add a triple to the store of triples.
        """
        assert self.__open, "The Store must be open."
        assert context != self, "Can not add triple directly to store"
        # Add the triple to the Store, triggering TripleAdded events
        Store.add(self, (subject, predicate, object), context, quoted)
        
        _to_string = self._to_string
        
        s = _to_string(subject)
        p = _to_string(predicate)
        o = _to_string(object)
        c = _to_string(context)
        
        cspo, cpos, cosp = self.__indices
        
        value = cspo.get("%s^%s^%s^%s^" % (c, s, p, o))
        if value is None:
            self.__contexts.set(c, "")
            
            contexts_value = cspo.get("%s^%s^%s^%s^" % ("", s, p, o)) or ""
            contexts = set(contexts_value.split("^"))
            contexts.add(c)
            contexts_value = "^".join(contexts)
            assert contexts_value!=None
            
            cspo.set("%s^%s^%s^%s^" % (c, s, p, o), "")
            cpos.set("%s^%s^%s^%s^" % (c, p, o, s), "")
            cosp.set("%s^%s^%s^%s^" % (c, o, s, p), "")
            if not quoted:
                cspo.set("%s^%s^%s^%s^" % ("", s, p, o), contexts_value)
                cpos.set("%s^%s^%s^%s^" % ("", p, o, s), contexts_value)
                cosp.set("%s^%s^%s^%s^" % ("", o, s, p), contexts_value)
            
            self.__needs_sync = True
            self.__contexts.synchronize()
            for dbindex in self.__indices:
                dbindex.synchronize()
            # self.synchronize()

    def __remove(self, (s, p, o), c, quoted=False):
        cspo, cpos, cosp = self.__indices
        contexts_value = cspo.get("^".join(("", s, p, o, ""))) or ""
        contexts = set(contexts_value.split("^"))
        contexts.discard(c)
        contexts_value = "^".join(contexts)
        for i, _to_key, _from_key in self.__indices_info:
            i.remove(_to_key((s, p, o), c))
        if not quoted:
            if contexts_value:
                for i, _to_key, _from_key in self.__indices_info:
                    i.set(_to_key((s, p, o), ""), contexts_value)
            else:
                for i, _to_key, _from_key in self.__indices_info:
                    try:
                        i.remove(_to_key((s, p, o), ""))
                    except self.db.DBNotFoundError, e:
                        log.debug("__remove failed with %s" % e)
                        pass # TODO: is it okay to ignore these?

    def remove(self, (subject, predicate, object), context):
        assert self.__open, "The Store must be open."
        Store.remove(self, (subject, predicate, object), context)
        _to_string = self._to_string
        
        if context is not None:
            if context == self:
                context = None
        
        if subject is not None \
                and predicate is not None \
                and object is not None \
                and context is not None:
            s = _to_string(subject)
            p = _to_string(predicate)
            o = _to_string(object)
            c = _to_string(context)
            value = self.__indices[0].get("%s^%s^%s^%s^" % (c, s, p, o))
            if value is not None:
                self.__remove((s, p, o), c)
                self.__needs_sync = True
        else:
            cspo, cpos, cosp = self.__indices
            index, prefix, from_key, results_from_key = self.__lookup(
                                    (subject, predicate, object), context)
            
            needs_sync = False
            for key in index.match_prefix(prefix):
                c, s, p, o = from_key(key)
                if context is None:
                    contexts_value = index.get(key) or ""
                    # remove triple from all non quoted contexts
                    contexts = set(contexts_value.split("^"))
                    contexts.add("") # and from the conjunctive index
                    for c in contexts:
                        for i, _to_key, _ in self.__indices_info:
                            i.remove(_to_key((s, p, o), c))
                else:
                    self.__remove((s, p, o), c)
                needs_sync = True
            if context is not None:
                if subject is None and predicate is None and object is None:
                    # TODO: also if context becomes empty and not just on 
                    # remove((None, None, None), c)
                    try:
                        self.__contexts.remove(_to_string(context))
                    # except db.DBNotFoundError, e:
                    #     pass
                    except Exception, e:
                        print("%s, Failed to delete %s" % (e, context))
                        pass
            
            self.__needs_sync = needs_sync
            # self.synchronize()

    def _from_string(self, i):
        """rdflib term from index number (as a string)"""
        k = self.__i2k.get(i)
        return self._loads(k)
    
    def _to_string(self, term, txn=None):
        """index number (as a string) from rdflib term"""
        k = self._dumps(term)
        i = self.__k2i.get(k)
        if i is None: # (from BdbApi)
            i = "%s" % random.random() # sleepycat used a serial number
            self.__k2i.set(k, i)
            self.__i2k.set(i, k)
            self.__k2i.synchronize()
            self.__i2k.synchronize()
        assert i is not None
        return i

    def __lookup(self, (subject, predicate, object), context):
        _to_string = self._to_string
        if context is not None:
            context = _to_string(context)
        i = 0
        if subject is not None:
            i += 1
            subject = _to_string(subject)
        if predicate is not None:
            i += 2
            predicate = _to_string(predicate)
        if object is not None:
            i += 4
            object = _to_string(object)
        index, prefix_func, from_key, results_from_key = self.__lookup_dict[i]
        prefix = "^".join(prefix_func((subject, predicate, object), context))
        return index, prefix, from_key, results_from_key
    
    def play_journal(self, graph=None):
        raise NotImplementedError
    

    def triples(self, (subject, predicate, object), context=None):
        """A generator over all the triples matching """
        assert self.__open, "The Store must be open."
        
        if context is not None:
            if context == self:
                context = None
        # _from_string = self._from_string

        index, prefix, from_key, results_from_key = self.__lookup(
                                    (subject, predicate, object), context)

        for key in index.match_prefix(prefix):
            yield results_from_key(
                key, subject, predicate, object, index[key])

    def bind(self, prefix, namespace):
        prefix = prefix.encode("utf-8")
        namespace = namespace.encode("utf-8")
        bound_prefix = self.__prefix.get(namespace)
        if bound_prefix:
            self.__namespace.remove(bound_prefix)
        self.__prefix[namespace] = prefix
        self.__namespace[prefix] = namespace
    
    def namespace(self, prefix):
        prefix = prefix.encode("utf-8")
        p = self.__namespace.get(prefix)
        return p if p else None
    
    def prefix(self, namespace):
        namespace = namespace.encode("utf-8")
        p = self.__prefix.get(namespace)
        return p if p else None

    def namespaces(self):
        for prefix in self.__namespace:
            yield prefix, URIRef(self.__namespace[prefix])

    def contexts(self, triple=None):
        _from_string = self._from_string
        _to_string = self._to_string
        
        if triple:
            s, p, o = triple
            s = _to_string(s)
            p = _to_string(p)
            o = _to_string(o)
            contexts = self.__indices[0].get("%s^%s^%s^%s^" % ("", s, p, o))
            if contexts:
                for c in contexts.split("^"):
                    if c:
                        yield _from_string(c)
        else:
            for key in self.__contexts:
                yield _from_string(key)

def results_from_key_func(i, from_string):
    def from_key(key, subject, predicate, object, contexts_value):
        "Takes a key and subject, predicate, object; returns tuple for yield"
        parts = key.split("^")
        if subject is None:
            # TODO: i & 1: # dis assemble and/or measure to see which is 
            # faster
            # subject is None or i & 1
            s = from_string(parts[(3-i+0)%3+1])
        else:
            s = subject
        if predicate is None:#i & 2:
            p = from_string(parts[(3-i+1)%3+1])
        else:
            p = predicate
        if object is None:#i & 4:
            o = from_string(parts[(3-i+2)%3+1])
        else:
            o = object
        return (s, p, o), (from_string(c) 
                                for c in contexts_value.split("^") if c)
    
    return from_key

def to_key_func(i):
    def to_key(triple, context):
        "Takes a string; returns key"
        return "^".join((context, triple[i%3], 
                         triple[(i+1)%3], triple[(i+2)%3], "")
                         ) # "" to tac on the trailing ^
    
    return to_key

def from_key_func(i):
    def from_key(key):
        "Takes a key; returns string"
        parts = key.split("^")
        return parts[0], parts[(3-i+0)%3+1], \
                    parts[(3-i+1)%3+1], parts[(3-i+2)%3+1]
    
    return from_key

def readable_index(i):
    s, p, o = "?" * 3
    if i & 1: s = "s"
    if i & 2: p = "p"
    if i & 4: o = "o"
    return "%s,%s,%s" % (s, p, o)
