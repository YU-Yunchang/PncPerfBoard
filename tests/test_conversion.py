import errno
import io
import math
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace as NS

from google.protobuf import descriptor_pb2 as pb
from mcap.reader import make_reader
from mcap.writer import Writer, CompressionType
from mcap_protobuf.decoder import DecoderFactory

from record2mcap.cyber import TYPES, SECTION, HEADER_LENGTH, RecordError, RecordReader, Channel, descriptor_set
from record2mcap.converter import convert, validate
from record2mcap.osm_map import _tile_xy
from record2mcap.visualization import Visualizer


def schema():
    dependency = pb.FileDescriptorProto(name='common.proto', package='test', syntax='proto2')
    dependency.message_type.add(name='Value').field.add(name='v', number=1, type=pb.FieldDescriptorProto.TYPE_UINT64,
                                                       label=pb.FieldDescriptorProto.LABEL_OPTIONAL)
    root = pb.FileDescriptorProto(name='sample.proto', package='test', syntax='proto2', dependency=['common.proto'])
    root.message_type.add(name='Sample').field.add(name='value', number=1, type=pb.FieldDescriptorProto.TYPE_MESSAGE,
                                                  type_name='.test.Value', label=pb.FieldDescriptorProto.LABEL_OPTIONAL)
    desc = TYPES['ProtoDesc'](desc=root.SerializeToString())
    desc.dependencies.add(desc=dependency.SerializeToString())
    return desc.SerializeToString()


def fixture(path, compressed=0, complete=True):
    channel = TYPES['Channel'](name='/test', message_type='test.Sample', proto_desc=schema())
    cls = descriptor_set(Channel('/test', 'test.Sample', channel.proto_desc))[1]
    body = TYPES['ChunkBody']()
    for ns, v in [(1_000_000_001, 2**63+7), (1_000_000_000, 42), (1_000_000_001, 0)]:
        msg = cls();msg.value.v=v
        body.messages.add(channel_name='/test', time=ns, content=msg.SerializeToString())
    header = TYPES['Header'](major_version=1, minor_version=0, compress=compressed, is_complete=complete,
                            channel_number=2, chunk_number=1, message_number=3,
                            begin_time=1_000_000_000, end_time=1_000_000_001)
    ch = TYPES['ChunkHeader'](message_number=3, begin_time=header.begin_time, end_time=header.end_time)
    def section(kind, msg):
        data=msg.SerializeToString();return SECTION.pack(kind,len(data))+data
    data=section(0,header)+b'\0'*(HEADER_LENGTH-len(header.SerializeToString()))
    data+=section(4,channel)
    channel.name='/empty';data+=section(4,channel)
    data+=section(1,ch)+section(2,body)
    path.write_bytes(data)


class ConversionTests(unittest.TestCase):
    def test_conversion_on_filesystem_without_hard_links(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'input.record'
            out = Path(directory) / '新加卷' / 'output.mcap'
            fixture(src)
            with patch('record2mcap.converter.os.link', side_effect=OSError(errno.ENOTSUP, 'Operation not supported')):
                report = convert(src, out)
            self.assertEqual(report['original_messages'], 3)
            self.assertTrue(validate(out, src)['original_time_payload_sha256_match'])
            self.assertTrue(out.with_suffix('.mcap.report.json').exists())
            self.assertEqual(list(out.parent.glob('*.partial')), [])

    def test_no_hard_links_fallback_preserves_concurrent_output(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'input.record'
            out = Path(directory) / 'output.mcap'
            fixture(src)
            def concurrent_output(*args):
                out.write_bytes(b'keep concurrent output')
                raise OSError(errno.ENOTSUP, 'Operation not supported')
            with patch('record2mcap.converter.os.link', side_effect=concurrent_output):
                with self.assertRaises(FileExistsError):
                    convert(src, out)
            self.assertEqual(out.read_bytes(), b'keep concurrent output')
            self.assertEqual(list(out.parent.glob('*.partial')), [])
            self.assertFalse(out.with_suffix('.mcap.report.json').exists())

    def test_no_hard_links_copy_failure_removes_incomplete_output(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'input.record'
            out = Path(directory) / 'output.mcap'
            fixture(src)
            def failed_copy(source, destination, **kwargs):
                destination.write(b'incomplete')
                raise OSError(errno.ENOSPC, 'No space left on device')
            with patch('record2mcap.converter.os.link', side_effect=OSError(errno.ENOTSUP, 'Operation not supported')):
                with patch('record2mcap.converter.shutil.copyfileobj', side_effect=failed_copy):
                    with self.assertRaises(OSError) as error:
                        convert(src, out)
            self.assertEqual(error.exception.errno, errno.ENOSPC)
            self.assertFalse(out.exists())
            self.assertFalse(out.with_suffix('.mcap.report.json').exists())
            self.assertEqual(list(out.parent.glob('*.partial')), [])

    def test_publication_io_error_is_preserved_and_temporary_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / 'input.record'
            out = Path(directory) / 'output.mcap'
            fixture(src)
            with patch('record2mcap.converter.os.link', side_effect=OSError(errno.EIO, 'I/O error')):
                with self.assertRaises(OSError) as error:
                    convert(src, out)
            self.assertEqual(error.exception.errno, errno.EIO)
            self.assertFalse(out.exists())
            self.assertEqual(list(out.parent.glob('*.partial')), [])

    def test_lossless_roundtrip_dependencies_empty_topic_and_out_of_order_ns(self):
        with tempfile.TemporaryDirectory() as directory:
            src=Path(directory)/'input.record';out=Path(directory)/'output.mcap'
            fixture(src)
            report=convert(src,out)
            self.assertEqual(report['original_messages'],3)
            self.assertEqual(report['original_channels'],2)
            self.assertTrue(validate(out,src)['original_time_payload_sha256_match'])
            with out.open('rb') as f:
                values=list(make_reader(f,decoder_factories=[DecoderFactory()]).iter_decoded_messages(log_time_order=False))
            self.assertEqual([m.log_time for _,_,m,_ in values],[1000000001,1000000000,1000000001])
            self.assertEqual(values[0][3].value.v,2**63+7)
            self.assertTrue(all(m.publish_time==m.log_time for _,_,m,_ in values))
            with self.assertRaisesRegex(RecordError,'already exists'):convert(src,out)

    def test_truncation_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            src=Path(directory)/'input.record';out=Path(directory)/'output.mcap'
            fixture(src);src.write_bytes(src.read_bytes()[:-1]);out.write_bytes(b'keep me')
            with self.assertRaises(RecordError):convert(src,out,overwrite=True)
            self.assertEqual(out.read_bytes(),b'keep me')
            self.assertEqual(list(Path(directory).glob('*.partial')),[])

    def test_reject_unsupported_compression_and_unfinished_record(self):
        with tempfile.TemporaryDirectory() as directory:
            src=Path(directory)/'input.record'
            fixture(src,compressed=2)
            with self.assertRaisesRegex(RecordError,'Compressed'):RecordReader(src)
            fixture(src,complete=False)
            with self.assertRaisesRegex(RecordError,'incomplete'):RecordReader(src)

    def test_crc_detects_payload_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            src=Path(directory)/'input.record';out=Path(directory)/'output.mcap'
            fixture(src);convert(src,out,compression='none')
            with out.open('rb') as f:
                index=make_reader(f).get_summary().chunk_indexes[0]
            data=bytearray(out.read_bytes())
            # Last byte of the uncompressed chunk is message data, covered by chunk CRC.
            data[index.chunk_start_offset+index.chunk_length-1]^=1
            out.write_bytes(data)
            with self.assertRaises(Exception):validate(out)

    def test_missing_schema_dependency_is_reported(self):
        desc=TYPES['ProtoDesc'].FromString(schema());del desc.dependencies[:]
        with self.assertRaisesRegex(RecordError,'cannot load'):
            descriptor_set(Channel('/test','test.Sample',desc.SerializeToString()))

    def test_colliding_inferred_json_names_are_disambiguated(self):
        root = pb.FileDescriptorProto(name='collision.proto', package='test', syntax='proto2')
        message = root.message_type.add(name='Collision')
        for number, name in enumerate(('ALG_FCW_Main_State', 'ALG_FCW_MainState'), 1):
            message.field.add(name=name, number=number, type=pb.FieldDescriptorProto.TYPE_UINT32,
                              label=pb.FieldDescriptorProto.LABEL_OPTIONAL)
        desc = TYPES['ProtoDesc'](desc=root.SerializeToString())
        encoded, cls = descriptor_set(Channel('/collision', 'test.Collision', desc.SerializeToString()))
        instance = cls()
        setattr(instance, 'ALG_FCW_Main_State', 1)
        setattr(instance, 'ALG_FCW_MainState', 2)
        self.assertEqual(cls.FromString(instance.SerializeToString()).ALG_FCW_MainState, 2)
        repaired = pb.FileDescriptorSet.FromString(encoded).file[0].message_type[0]
        self.assertFalse(repaired.field[0].HasField('json_name'))
        self.assertEqual(repaired.field[1].json_name, 'ALG_FCW_MainState')


class VisualizationTests(unittest.TestCase):
    def render(self, callback, config=None):
        stream=io.BytesIO();writer=Writer(stream,compression=CompressionType.NONE);writer.start()
        v=Visualizer(writer,config);callback(v);writer.finish();stream.seek(0)
        messages=list(make_reader(stream,decoder_factories=[DecoderFactory()]).iter_decoded_messages(log_time_order=False))
        return v,[(channel.topic,decoded) for _,channel,_,decoded in messages]

    def test_pointcloud_sensor_separation_layout_and_extrinsics(self):
        point=lambda x:NS(x=x,y=2.,z=3.,doppler=-4.,SNR=5.)
        msg=NS(frames=[NS(radar_id=1,points=[point(1.),point(float('nan'))]),NS(radar_id=2,points=[point(8.)])])
        config={'radar_extrinsics':{'1':{'translation':[1,2,3],'rotation':[0,0,0,1]}}}
        v,results=self.render(lambda v:v.radar(msg,1_234_567_890),config)
        results=dict(results)
        self.assertEqual(results['/foxglove/tf/radar'].translation.x,1)
        for radar,x in [(1,1.),(2,8.)]:
            cloud=results[f'/foxglove/radar/points/{radar}']
            self.assertEqual(cloud.frame_id,f'radar_{radar}')
            self.assertEqual(cloud.point_stride,20)
            self.assertEqual(struct.unpack('<5f',cloud.data),(x,2.,3.,-4.,5.))
            self.assertEqual(cloud.timestamp.nanos,234567890)
            self.assertEqual([f.offset for f in cloud.fields],[0,4,8,12,16])
        self.assertEqual(v.skipped['nonfinite_radar_points'],1)

    def test_pose_yaw_and_empty_obstacle_deletion(self):
        pose=NS(position_enu=NS(x=10,y=20),heading=math.pi/2,HasField=lambda name:True)
        _,results=self.render(lambda v:(v.pose(pose,1_000_000_000),v.obstacles('/rina/perception/tracked_obstacles',NS(perception_obstacles=[]),1_000_000_000)))
        results=dict(results);tf=results['/foxglove/tf']
        self.assertEqual(tf.translation.z,0)
        self.assertAlmostEqual(tf.rotation.z,math.sqrt(0.5))
        update=results['/foxglove/rina/perception/tracked_obstacles']
        self.assertEqual(update.deletions[0].type,1)
        self.assertEqual(len(update.entities),0)

    def test_bev_map_lane_styles_edges_centers_and_ignores_arrows(self):
        point = lambda x, y, z=0.7: NS(point=NS(x=x, y=y, z=z))
        prop = NS(type=2, color=2, confidence=0.8, HasField=lambda name: name == 'confidence')
        lane = NS(point=[point(0, 1), point(10, 1)], line_piecewice=[prop],
                  marker_width=0., confidence=0.7)
        edge = NS(point=[point(0, 4), point(10, 4)], is_construction=False)
        center = NS(point=[point(0, 0), point(10, 0)], confidence=0.5)
        arrow = NS(point=[point(8, -2), point(4, -2)], confidence=0.9)
        message = NS(lane_line=[lane], road_edge=[edge], center_line=[center],
                     stop_line=[], cross_walk=[], interest_point=[], arrow=[arrow])
        _, results = self.render(lambda v: v.bev_map(message, 1_234_567_890))
        update = dict(results)['/foxglove/perception/bev_maps']
        entity = update.entities[0]
        self.assertEqual(entity.frame_id, 'base_link')
        self.assertEqual(len(entity.lines), 3)
        self.assertEqual(entity.lines[0].type, 2)  # dashed lane becomes LINE_LIST
        self.assertAlmostEqual(entity.lines[0].points[1].x, 6.8)
        self.assertAlmostEqual(entity.lines[0].colors[0].r, 1.0)
        self.assertAlmostEqual(entity.lines[0].colors[0].g, 0.42)
        self.assertEqual(entity.lines[0].colors[0].a, 1.0)
        self.assertAlmostEqual(entity.lines[1].color.r, 0.52)
        self.assertAlmostEqual(entity.lines[1].color.g, 0.52)
        self.assertAlmostEqual(entity.lines[1].color.b, 0.52)
        self.assertAlmostEqual(entity.lines[0].points[0].z, 0.735)
        self.assertAlmostEqual(entity.lines[1].points[0].z, 0.725)
        self.assertAlmostEqual(entity.lines[2].color.b, 0.72)
        self.assertAlmostEqual(entity.lines[2].color.a, 0.35)
        self.assertAlmostEqual(entity.lines[2].thickness, 0.06)
        self.assertEqual(len(entity.arrows), 0)
        metadata = dict((item.key, item.value) for item in entity.metadata)
        self.assertEqual(metadata['lane_lines'], '1')
        self.assertNotIn('arrows', metadata)

    def test_bev_map_frame_is_configurable(self):
        message = NS(lane_line=[], road_edge=[], center_line=[], stop_line=[], cross_walk=[],
                     interest_point=[], arrow=[])
        _, results = self.render(lambda v: v.bev_map(message, 1), {'bev_maps_frame': 'map'})
        self.assertEqual(dict(results)['/foxglove/perception/bev_maps'].entities[0].frame_id, 'map')

    def test_processed_trajectory_is_vehicle_width_ribbon(self):
        path_point = lambda x, y: NS(x=x, y=y)
        message = NS(
            trajectory_point=[NS(path_point=path_point(0, 0)), NS(path_point=path_point(10, 0)),
                              NS(path_point=path_point(10, 10))],
            path_point=[],
        )
        _, results = self.render(lambda v: v.processed_trajectory(message, 1),
                                 {'processed_trajectory_width': 2.0})
        entity = dict(results)['/foxglove/planning/trajectory'].entities[0]
        self.assertEqual(len(entity.triangles), 1)
        self.assertEqual(len(entity.triangles[0].points), 12)
        self.assertAlmostEqual(entity.triangles[0].color.b, 0.92)
        self.assertAlmostEqual(entity.triangles[0].color.a, 0.34)
        self.assertEqual(len(entity.lines), 0)
        self.assertAlmostEqual(entity.triangles[0].points[0].y, 1.0)
        self.assertAlmostEqual(entity.triangles[0].points[1].y, -1.0)
        self.assertAlmostEqual(entity.triangles[0].points[0].z, 0.08)
        self.assertEqual(dict((item.key, item.value) for item in entity.metadata)['width_m'], '2.0')

    def test_processed_trajectory_width_is_validated(self):
        with self.assertRaisesRegex(RecordError, 'processed_trajectory_width'):
            self.render(lambda v: None, {'processed_trajectory_width': 0})

    def test_planner_path_is_a_separate_3d_topic(self):
        points = [NS(point=NS(x=0., y=1.)), NS(point=NS(x=20., y=2.))]
        message = NS(trajectories=[NS(points=points, mode=0), NS(points=points, mode=5)], selected_mode=0,
                     path=NS(points=[NS(x=0., y=1.), NS(x=20., y=2.), NS(x=40., y=4.)]))
        _, results = self.render(lambda v: v.model_trajectories(message, 123))
        topics = dict(results)
        path_entity = topics['/foxglove/planner/trajectory_path'].entities[0]
        self.assertEqual(path_entity.frame_id, 'base_link')
        self.assertEqual(len(path_entity.lines), 1)
        self.assertEqual(len(path_entity.lines[0].points), 3)
        self.assertAlmostEqual(path_entity.lines[0].thickness, 0.22)
        self.assertAlmostEqual(path_entity.lines[0].color.r, 0.22)
        self.assertAlmostEqual(path_entity.lines[0].color.g, 0.22)
        self.assertAlmostEqual(path_entity.lines[0].color.b, 0.22)
        self.assertAlmostEqual(path_entity.lines[0].color.a, 0.75)
        candidates = topics['/foxglove/planning/model_trajectories'].entities[0].lines
        candidate = candidates[0]
        self.assertAlmostEqual(candidate.color.g, 0.95)
        self.assertAlmostEqual(candidate.color.a, 1.0)
        self.assertAlmostEqual(candidates[1].color.r, 1.0)
        self.assertAlmostEqual(candidates[1].color.g, 0.38)
        self.assertAlmostEqual(candidates[1].color.a, 1.0)
        self.assertAlmostEqual(candidate.points[0].z, 0.30)
        self.assertAlmostEqual(path_entity.lines[0].points[0].z, 0.01)
        self.assertEqual(dict((item.key, item.value) for item in path_entity.metadata)['source_field'],
                         '/hirain/planner/trajectories.path')

    def test_bbox_preserves_source_height(self):
        obstacle = NS(id=1, position=NS(x=2., y=3., z=-1.), theta=0.,
                      length=1., width=1., height=0.2, type=0)
        _, results = self.render(lambda v: v.obstacles('/rina/perception/tracked_obstacles',
                                                       NS(perception_obstacles=[obstacle]), 1))
        cube = dict(results)['/foxglove/rina/perception/tracked_obstacles'].entities[0].cubes[0]
        self.assertAlmostEqual(cube.pose.position.z, -1.0)

    def test_reject_bad_extrinsics(self):
        with self.assertRaisesRegex(RecordError,'non-unit'):
            self.render(lambda v:None,{'radar_extrinsics':{'0':{'translation':[0,0,0],'rotation':[0,0,0,0]}}})

    def test_local_osm_tiles_become_translucent_map_aligned_grid(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Outside the GCJ-02 coverage area so the expected XYZ tile is direct WGS84.
            zoom, lon, lat = 17, 10.0, 50.0
            tile_x, tile_y = (math.floor(value) for value in _tile_xy(lon, lat, zoom))
            tile_dir = root / str(zoom) / str(tile_x)
            tile_dir.mkdir(parents=True)
            Image.new('RGBA', (8, 8), (40, 80, 120, 255)).save(tile_dir / f'{tile_y}.png')
            config = {'osm_map': {'source': str(root), 'zoom': zoom, 'padding_tiles': 0,
                                  'opacity': 0.2, 'saturation': 1.0, 'brightness': 1.0}}
            pose = NS(position_enu=NS(x=1000., y=2000.), position_llh=NS(lon=lon, lat=lat, height=3.),
                      heading=0., HasField=lambda name: name in ('position_enu', 'position_llh', 'heading'))
            _, results = self.render(lambda v: (v.pose(pose, 1_234_567_890), v.finalize()), config)
            topics = dict(results)
            grid = topics['/foxglove/map/osm']
            self.assertEqual(grid.frame_id, 'map')
            self.assertEqual((grid.column_count, grid.row_stride, grid.cell_stride), (8, 32, 4))
            self.assertEqual([field.name for field in grid.fields], ['red', 'green', 'blue', 'alpha'])
            self.assertEqual(set(grid.data[3::4]), {51})
            width_m = grid.column_count * grid.cell_size.x
            self.assertLessEqual(grid.pose.position.x, 1000.)
            self.assertGreaterEqual(grid.pose.position.x + width_m, 1000.)
            height_m = (len(grid.data) // grid.row_stride) * grid.cell_size.y
            self.assertLessEqual(grid.pose.position.y, 2000.)
            self.assertGreaterEqual(grid.pose.position.y + height_m, 2000.)
            text = topics['/foxglove/map/osm_attribution'].entities[0].texts[0]
            self.assertEqual(text.text, '© OpenStreetMap contributors')

    def test_osm_map_rejects_remote_tile_sources(self):
        with self.assertRaisesRegex(RecordError, 'must be local'):
            self.render(lambda v: None, {'osm_map': {'source': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'}})

    def test_osm_map_reads_mbtiles_tms_row_order(self):
        from PIL import Image
        from record2mcap.osm_map import OSMMapBuilder
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'map.mbtiles'
            encoded = io.BytesIO()
            Image.new('RGB', (2, 2), (1, 2, 3)).save(encoded, format='PNG')
            with sqlite3.connect(path) as connection:
                connection.execute('create table tiles(zoom_level integer, tile_column integer, tile_row integer, tile_data blob)')
                connection.execute('insert into tiles values(?,?,?,?)', (3, 4, 5, encoded.getvalue()))
            connection.close()
            builder = OSMMapBuilder({'source': str(path)})
            # XYZ y=2 corresponds to TMS row 2^3-1-2=5.
            self.assertEqual(builder._read_tile(3, 4, 2), encoded.getvalue())

    def test_osm_alignment_is_fitted_from_llh_and_map_axes(self):
        from record2mcap.osm_map import OSMMapBuilder
        with tempfile.TemporaryDirectory() as directory:
            builder = OSMMapBuilder({'source': directory})
            def pose(lon, lat, x, y):
                return NS(position_llh=NS(lon=lon, lat=lat), position_enu=NS(x=x, y=y),
                          HasField=lambda name: name in ('position_llh', 'position_enu'))
            # About 44 m east in geographic coordinates maps to +Y in this map frame.
            builder.observe(pose(10.0, 50.0, 100., 200.), 1)
            builder.observe(pose(10.00062, 50.0, 100., 244.3), 2)
            a, b = builder._map_alignment()
            self.assertAlmostEqual(a, 0., delta=0.03)
            self.assertAlmostEqual(b, 1., delta=0.03)


if __name__ == '__main__':
    unittest.main()
