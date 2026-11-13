"""Apollo Cyber Record v1 reader without an Apollo installation.

Wire field numbers follow Apollo's cyber/proto/record.proto and proto_desc.proto.
"""
from dataclasses import dataclass
from pathlib import Path
import struct

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from google.protobuf.message import DecodeError

SECTION = struct.Struct('<i4xq')
HEADER_LENGTH = 2048


class RecordError(ValueError):
    pass


def _make_types():
    fd = descriptor_pb2.FileDescriptorProto(name='record2mcap_cyber.proto',
                                          package='record2mcap.cyber', syntax='proto2')
    F = descriptor_pb2.FieldDescriptorProto
    definitions = {
        'Header': [('major_version', 1, F.TYPE_UINT32), ('minor_version', 2, F.TYPE_UINT32),
                   ('compress', 3, F.TYPE_UINT32), ('index_position', 6, F.TYPE_UINT64),
                   ('chunk_number', 7, F.TYPE_UINT64), ('channel_number', 8, F.TYPE_UINT64),
                   ('begin_time', 9, F.TYPE_UINT64), ('end_time', 10, F.TYPE_UINT64),
                   ('message_number', 11, F.TYPE_UINT64), ('size', 12, F.TYPE_UINT64),
                   ('is_complete', 13, F.TYPE_BOOL)],
        'Channel': [('name', 1, F.TYPE_STRING), ('message_type', 2, F.TYPE_STRING),
                    ('proto_desc', 3, F.TYPE_BYTES)],
        'SingleMessage': [('channel_name', 1, F.TYPE_STRING), ('time', 2, F.TYPE_UINT64),
                          ('content', 3, F.TYPE_BYTES)],
        'ChunkHeader': [('begin_time', 1, F.TYPE_UINT64), ('end_time', 2, F.TYPE_UINT64),
                        ('message_number', 3, F.TYPE_UINT64), ('raw_size', 4, F.TYPE_UINT64)],
        'ChunkBody': [('messages', 1, '.record2mcap.cyber.SingleMessage')],
        'ProtoDesc': [('desc', 1, F.TYPE_BYTES), ('dependencies', 2, '.record2mcap.cyber.ProtoDesc')],
    }
    for name, fields in definitions.items():
        msg = fd.message_type.add(name=name)
        for field_name, number, kind in fields:
            field = msg.field.add(name=field_name, number=number, label=F.LABEL_OPTIONAL)
            if isinstance(kind, str):
                field.type = F.TYPE_MESSAGE
                field.type_name = kind
                field.label = F.LABEL_REPEATED
            else:
                field.type = kind
    pool = descriptor_pool.DescriptorPool()
    pool.Add(fd)
    return {name: message_factory.GetMessageClass(pool.FindMessageTypeByName('record2mcap.cyber.' + name))
            for name in definitions}


TYPES = _make_types()


def parse(name, payload):
    try:
        return TYPES[name].FromString(payload)
    except DecodeError as exc:
        raise RecordError(f'Invalid {name} protobuf: {exc}') from exc


@dataclass
class Channel:
    name: str
    message_type: str
    descriptor: bytes


class RecordReader:
    def __init__(self, path):
        self.path = Path(path)
        self.size = self.path.stat().st_size
        with self.path.open('rb') as stream:
            kind, size = self._section(stream)
            if kind != 0 or size > HEADER_LENGTH:
                raise RecordError(f'{self.path}: not a supported Cyber Record header')
            self.header = parse('Header', stream.read(size))
        if self.header.major_version != 1 or self.header.minor_version != 0:
            raise RecordError('Only Cyber Record version 1.0 is supported')
        if self.header.compress != 0:
            raise RecordError(f'Compressed Cyber Record input is unsupported (compress={self.header.compress})')
        if not self.header.is_complete:
            raise RecordError('Record is incomplete; refusing to silently produce partial output')
        if self.size < SECTION.size + HEADER_LENGTH:
            raise RecordError('Truncated header padding')

    def _section(self, stream):
        offset = stream.tell()
        raw = stream.read(SECTION.size)
        if len(raw) != SECTION.size:
            raise RecordError(f'Truncated section header at byte {offset}')
        kind, size = SECTION.unpack(raw)
        if kind not in range(5) or size < 0 or size > self.size - stream.tell():
            raise RecordError(f'Invalid section at byte {offset}: type={kind}, size={size}')
        return kind, size

    def sections(self, wanted):
        with self.path.open('rb') as stream:
            stream.seek(SECTION.size + HEADER_LENGTH)
            while stream.tell() < self.size:
                offset = stream.tell()
                kind, size = self._section(stream)
                if kind == 0:
                    raise RecordError(f'Unexpected extra file header at byte {offset}')
                if kind in wanted:
                    yield kind, stream.read(size)
                else:
                    stream.seek(size, 1)

    def channels(self):
        channels = {}
        for _, data in self.sections({4}):
            msg = parse('Channel', data)
            if not msg.name or not msg.message_type or not msg.proto_desc:
                raise RecordError('Channel is missing name, message type or embedded schema')
            channel = Channel(msg.name, msg.message_type, msg.proto_desc)
            if msg.name in channels and channels[msg.name] != channel:
                raise RecordError(f'Conflicting channel definitions: {msg.name}')
            channels[msg.name] = channel
        if len(channels) != self.header.channel_number:
            raise RecordError(f'Channel count mismatch: {len(channels)} != {self.header.channel_number}')
        return channels

    def messages(self):
        count = chunks = 0
        begin = end = None
        pending = None
        for kind, data in self.sections({1, 2}):
            if kind == 1:
                if pending is not None:
                    raise RecordError('Chunk header without body')
                pending = parse('ChunkHeader', data)
                continue
            if pending is None:
                raise RecordError('Chunk body without header')
            body = parse('ChunkBody', data)
            if len(body.messages) != pending.message_number:
                raise RecordError('Chunk message count mismatch')
            chunk_begin = chunk_end = None
            for msg in body.messages:
                if not msg.channel_name or not msg.HasField('time') or not msg.HasField('content'):
                    raise RecordError('Message is missing channel, timestamp or content')
                chunk_begin = msg.time if chunk_begin is None else min(chunk_begin, msg.time)
                chunk_end = msg.time if chunk_end is None else max(chunk_end, msg.time)
                begin = msg.time if begin is None else min(begin, msg.time)
                end = msg.time if end is None else max(end, msg.time)
                count += 1
                yield msg
            if body.messages and (chunk_begin != pending.begin_time or chunk_end != pending.end_time):
                raise RecordError('Chunk timestamp bounds mismatch')
            chunks += 1
            pending = None
        if pending is not None:
            raise RecordError('Missing last chunk body')
        if count != self.header.message_number or chunks != self.header.chunk_number:
            raise RecordError(f'Record count mismatch: read {count} messages / {chunks} chunks')
        if count and (begin != self.header.begin_time or end != self.header.end_time):
            raise RecordError('Record timestamp bounds mismatch')


def descriptor_set(channel):
    """Flatten recursive Cyber ProtoDesc into a complete, dependency-first FDS."""
    files = {}

    def default_json_name(name):
        result = []
        uppercase_next = False
        for char in name:
            if char == '_':
                uppercase_next = True
            elif uppercase_next:
                result.append(char.upper())
                uppercase_next = False
            else:
                result.append(char)
        return ''.join(result)

    def make_json_names_unique(message):
        # Some generated vehicle schemas contain distinct protobuf field names
        # whose inferred lowerCamelCase JSON names collide. Binary protobuf is
        # unambiguous; assign an explicit JSON name only to later collisions so
        # modern descriptor pools can still load and decode the embedded schema.
        used = set()
        for field in message.field:
            effective = field.json_name if field.HasField('json_name') else default_json_name(field.name)
            if effective in used:
                candidate = field.name
                if candidate in used:
                    candidate = f'{field.name}_field_{field.number}'
                    while candidate in used:
                        candidate += '_'
                field.json_name = candidate
                effective = candidate
            used.add(effective)
        for nested in message.nested_type:
            make_json_names_unique(nested)

    def visit(node):
        if not node.desc:
            raise RecordError(f'{channel.name}: empty embedded descriptor')
        fd = descriptor_pb2.FileDescriptorProto.FromString(node.desc)
        if not fd.name:
            raise RecordError(f'{channel.name}: descriptor has no filename')
        for message in fd.message_type:
            make_json_names_unique(message)
        if fd.name in files:
            if files[fd.name] != fd:
                raise RecordError(f'{channel.name}: conflicting descriptor {fd.name}')
            return
        for dep in node.dependencies:
            visit(dep)
        files[fd.name] = fd

    visit(parse('ProtoDesc', channel.descriptor))
    pool = descriptor_pool.DescriptorPool()
    result = descriptor_pb2.FileDescriptorSet()
    # Dependencies normally arrive in order; sort explicitly to handle shared imports.
    pending = dict(files)
    while pending:
        ready = [name for name, fd in pending.items() if all(dep not in pending for dep in fd.dependency)]
        if not ready:
            raise RecordError(f'{channel.name}: cyclic descriptor dependencies')
        for name in ready:
            fd = pending.pop(name)
            try:
                pool.Add(fd)
            except Exception as exc:
                raise RecordError(f'{channel.name}: cannot load {name}: {exc}') from exc
            result.file.add().CopyFrom(fd)
    try:
        desc = pool.FindMessageTypeByName(channel.message_type)
    except KeyError as exc:
        raise RecordError(f'{channel.name}: message type absent from embedded schema') from exc
    return result.SerializeToString(), message_factory.GetMessageClass(desc)
