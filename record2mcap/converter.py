"""Lossless conversion and independent full-file validation."""
import errno
import hashlib
import json
import os
import shutil
import struct
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from mcap.reader import make_reader
from mcap.writer import Writer, CompressionType
from mcap_protobuf.decoder import DecoderFactory

from .cyber import RecordReader, RecordError, descriptor_set


def digest_message(digest, timestamp, data):
    digest.update(struct.pack('<QQ', timestamp, len(data)))
    digest.update(data)


def _publish_no_replace(temporary, output):
    """Save a validated file without overwriting another output."""
    try:
        os.link(temporary, output)
    except OSError as exc:
        # exFAT may support neither hard links nor exclusive rename. Exclusive
        # creation still protects concurrent outputs. The fallback destination
        # is visible during copying; callers mark it ready only after returning.
        if exc.errno not in (errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        destination = output.open('xb')  # Keep outside cleanup: EEXIST belongs to another writer.
        try:
            with destination, temporary.open('rb') as source:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    temporary.unlink()


def convert(source, output, foxglove=False, config=None, compression='zstd', overwrite=False, progress=None,
            observer=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output:
        raise RecordError('Input and output must be different files')
    if output.exists() and not overwrite:
        raise RecordError(f'Output already exists: {output}; use --overwrite to replace it')
    reader = RecordReader(source)
    channels = reader.channels()
    schemas = {name: descriptor_set(channel) for name, channel in channels.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    counts = Counter()
    hashes = {name: hashlib.sha256() for name in channels}
    try:
        with tempfile.NamedTemporaryFile(mode='w+b', dir=output.parent, prefix='.'+output.name+'.',
                                         suffix='.partial', delete=False) as stream:
            temporary = Path(stream.name)
            writer = Writer(stream, compression={'zstd': CompressionType.ZSTD, 'lz4': CompressionType.LZ4,
                                                'none': CompressionType.NONE}[compression],
                            enable_data_crcs=True)
            writer.start(profile='', library='record2mcap/0.2.0')
            writer.add_metadata('record2mcap', {'source': str(source), 'timestamp_policy': 'record nanoseconds',
                                               'original_payloads': 'unchanged', 'visualization': str(foxglove)})
            schema_ids, channel_ids = {}, {}
            for name, channel in channels.items():
                data, _ = schemas[name]
                key = (channel.message_type, data)
                if key not in schema_ids:
                    schema_ids[key] = writer.register_schema(channel.message_type, 'protobuf', data)
                channel_ids[name] = writer.register_channel(name, 'protobuf', schema_ids[key])
            visualizer = None
            if foxglove:
                from .visualization import Visualizer, EXPECTED
                if any(name.startswith('/foxglove/') for name in channels):
                    raise RecordError('Input already contains /foxglove/ topics; output topic collision')
                visualizer = Visualizer(writer, config)
                writer.add_metadata('record2mcap.visualization',
                                    {'config': json.dumps(visualizer.config), 'coordinates': 'planar map/base_link',
                                     'derived_timestamp': 'record log time; no sensor latency correction'})
            for index, message in enumerate(reader.messages(), 1):
                name = message.channel_name
                if name not in channel_ids:
                    raise RecordError(f'Message references undefined topic {name}')
                writer.add_message(channel_ids[name], message.time, message.content, message.time)
                counts[name] += 1
                digest_message(hashes[name], message.time, message.content)
                observer_topics = observer.topics if observer is not None else ()
                decoded = None
                if (visualizer and name in EXPECTED) or name in observer_topics:
                    decoded = schemas[name][1].FromString(message.content)
                if visualizer and name in EXPECTED:
                    visualizer.process(name, decoded, message.time)
                if observer is not None and name in observer_topics:
                    observer.process(name, decoded, message.time)
                if progress and index % 10000 == 0:
                    progress(index, reader.header.message_number)
            if visualizer:
                visualizer.finalize()
            writer.finish()
            stream.flush()
            os.fsync(stream.fileno())
        # Validate CRCs, every message's schema and per-topic losslessness before publishing output.
        validation = validate(temporary, expected={n: (counts[n], hashes[n].hexdigest()) for n in channels})
        if overwrite:
            os.replace(temporary, output)
        else:
            _publish_no_replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    warnings = []
    if foxglove:
        from .visualization import RADAR_TOPIC
        if counts[RADAR_TOPIC] == 0:
            warnings.append('输入雷达点云 topic 消息数为 0，当前输出没有真实点云。')
        warnings.append('3D 使用平面驾驶视图：ENU x/y、heading，车体 z=0；不做传感器延迟补偿。')
        if visualizer.skipped:
            warnings.append(f'可视化跳过无效几何，原始数据保留：{dict(visualizer.skipped)}')
        warnings.extend(visualizer.warnings)
    report = {
        'source': str(source), 'output': str(output), 'output_bytes': output.stat().st_size,
        'source_bytes': source.stat().st_size, 'original_channels': len(channels),
        'original_messages': sum(counts.values()), 'start_time_ns': reader.header.begin_time,
        'end_time_ns': reader.header.end_time,
        'start_time_utc': datetime.fromtimestamp(reader.header.begin_time / 1e9, timezone.utc).isoformat(),
        'duration_seconds': (reader.header.end_time-reader.header.begin_time)/1e9,
        'channels': [{'topic': n, 'type': c.message_type, 'messages': counts[n],
                      'sha256_time_and_payload': hashes[n].hexdigest()} for n, c in channels.items()],
        'derived_channels': dict(visualizer.counts) if visualizer else {},
        'visualization_config': visualizer.config if visualizer else None,
        'warnings': warnings, 'validation': validation,
    }
    output.with_suffix(output.suffix+'.report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding="utf-8")
    return report


def validate(path, source=None, expected=None):
    counts, hashes = Counter(), {}
    if source:
        source_reader = RecordReader(source)
        source_channels = source_reader.channels()
        source_counts = Counter()
        source_hashes = {n: hashlib.sha256() for n in source_channels}
        for message in source_reader.messages():
            if message.channel_name not in source_hashes:
                raise RecordError(f'Undefined source topic {message.channel_name}')
            source_counts[message.channel_name] += 1
            digest_message(source_hashes[message.channel_name], message.time, message.content)
        expected = {n: (source_counts[n], source_hashes[n].hexdigest()) for n in source_channels}
    with Path(path).open('rb') as stream:
        reader = make_reader(stream, validate_crcs=True, decoder_factories=[DecoderFactory()])
        summary = reader.get_summary()
        if summary is None or summary.statistics is None:
            raise RecordError('MCAP is missing summary/statistics')
        for channel in summary.channels.values():
            counts[channel.topic] = 0
            hashes[channel.topic] = hashlib.sha256()
        # Physical order preserves the source per-topic sequence, including out-of-order timestamps.
        for schema, channel, message, decoded in reader.iter_decoded_messages(log_time_order=False):
            if decoded is None or schema is None:
                raise RecordError(f'Cannot decode {channel.topic}')
            if not decoded.IsInitialized():
                raise RecordError(f'Uninitialized required protobuf fields on {channel.topic}')
            counts[channel.topic] += 1
            digest_message(hashes[channel.topic], message.log_time, message.data)
        if sum(counts.values()) != summary.statistics.message_count:
            raise RecordError('MCAP statistics message count mismatch')
        for channel_id, count in summary.statistics.channel_message_counts.items():
            if counts[summary.channels[channel_id].topic] != count:
                raise RecordError('MCAP per-channel statistics mismatch')
    if expected:
        for name, (count, digest) in expected.items():
            if name not in hashes or counts[name] != count or hashes[name].hexdigest() != digest:
                raise RecordError(f'Lossless verification failed on {name}')
    return {'all_messages_decoded': True, 'chunk_crcs_checked': True,
            'original_time_payload_sha256_match': bool(expected),
            'total_messages': sum(counts.values()), 'total_channels': len(counts)}
