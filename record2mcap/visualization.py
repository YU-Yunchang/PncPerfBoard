"""Explicit adapters for the RINA schemas embedded in the CX835 sample.

map/base_link are a planar driving view: ENU x/y, heading in radians, z=0.
Radar clouds retain independent sensor frames unless calibrated extrinsics are supplied.
"""
import math
import struct
from collections import Counter

from foxglove_schemas_protobuf.SceneUpdate_pb2 import SceneUpdate
from foxglove_schemas_protobuf.FrameTransform_pb2 import FrameTransform
from foxglove_schemas_protobuf.PointCloud_pb2 import PointCloud
from foxglove_schemas_protobuf.PackedElementField_pb2 import PackedElementField
from mcap_protobuf.schema import build_file_descriptor_set

from .cyber import RecordError

POSE_TOPIC = '/hirain/localization/global_pose'
PLAN_TOPIC = '/hirain/planning/processed_trajectory'
MODEL_TOPIC = '/hirain/planner/trajectories'
RADAR_TOPIC = '/hirain/radar4d/raw_pcds'
BEV_MAP_TOPIC = '/hirain/perception/bev_maps'
OBSTACLE_TOPICS = {
    '/rina/perception/tracked_obstacles', '/rina/perception/vision_nn_obstacles',
    '/rina/perception/vision_nn_obstacles_pilot', '/rina/perception/pure_vision_nn_obstacles',
    '/rina/perception/radar_obstacles', '/rina/perception/srr_obstacles',
    '/hirain/perception/bev_obstacles', '/hirain/perception/radar4d_obstacles',
}
EXPECTED = {POSE_TOPIC: 'rina.localization.GlobalPose', PLAN_TOPIC: 'rina.planning.ADCTrajectory',
            MODEL_TOPIC: 'rina.planner.PlannerModelResult', RADAR_TOPIC: 'rina.radar4d.Radar4dPointClouds2',
            BEV_MAP_TOPIC: 'rina.perception.PerceptionRoad',
            **{topic: 'rina.perception.PerceptionObstacles' for topic in OBSTACLE_TOPICS}}
DEFAULT_CONFIG = {'obstacles_frame': 'base_link', 'trajectory_frame': 'map',
                  'model_frame': 'base_link', 'bev_maps_frame': 'base_link',
                  'processed_trajectory_width': 2.2,
                  'radar_extrinsics': {}, 'osm_map': None}

LANE_COLORS = {
    # High-contrast display palette for Foxglove's white/light-gray 3D canvas.
    # Keys retain the source LPColor enum; colors intentionally are not literal.
    0: (0.12, 0.14, 0.18),  # unknown -> charcoal
    1: (0.04, 0.18, 0.58),  # white -> deep blue
    2: (1.0, 0.42, 0.02),  # yellow -> vivid orange
    3: (0.42, 0.12, 0.62),  # blue -> violet
    4: (0.72, 0.03, 0.03),  # orange -> deep red
    5: (0.02, 0.38, 0.18),  # green -> forest green
    6: (0.58, 0.03, 0.38),  # yellow / white -> magenta
    7: (0.0, 0.30, 0.55),  # white / yellow -> cyan-blue
}

ROAD_EDGE_COLOR = (0.52, 0.52, 0.52, 1.0)
TRAJECTORY_MODE_GENERAL = 5  # rina.planner.TrajectoryMode in the embedded record schema


def stamp(target, ns):
    target.seconds, target.nanos = divmod(ns, 1_000_000_000)


def xyz(target, x, y, z=0):
    target.x, target.y, target.z = x, y, z


def yaw(target, theta):
    target.z, target.w = math.sin(theta / 2), math.cos(theta / 2)


def color(target, rgba):
    target.r, target.g, target.b, target.a = rgba


def finite(*values):
    return all(math.isfinite(v) for v in values)


class Visualizer:
    def __init__(self, writer, config=None):
        self.writer = writer
        self.config = dict(DEFAULT_CONFIG)
        if config:
            unknown = set(config) - set(DEFAULT_CONFIG)
            if unknown:
                raise RecordError(f'Unknown visualization config keys: {sorted(unknown)}')
            self.config.update(config)
        for key in ('obstacles_frame', 'trajectory_frame', 'model_frame', 'bev_maps_frame'):
            if self.config[key] not in ('map', 'base_link'):
                raise RecordError(f'{key} must be map or base_link')
        width = self.config['processed_trajectory_width']
        if not isinstance(width, (int, float)) or not finite(width) or not 0.2 <= width <= 10:
            raise RecordError('processed_trajectory_width must be a finite number from 0.2 to 10 meters')
        for radar, extrinsic in self.config['radar_extrinsics'].items():
            if set(extrinsic) != {'translation', 'rotation'}:
                raise RecordError(f'Radar {radar}: require translation [x,y,z], rotation [x,y,z,w]')
            p, q = extrinsic['translation'], extrinsic['rotation']
            if len(p) != 3 or len(q) != 4 or not finite(*p, *q) or abs(sum(v*v for v in q)-1) > 1e-3:
                raise RecordError(f'Radar {radar}: invalid extrinsic or non-unit quaternion')
        self.channels = {}
        self.schemas = {}
        self.counts = Counter()
        self.skipped = Counter()
        self.track = []
        self.pose_count = 0
        self.warnings = []
        self.osm_map = None
        if self.config['osm_map'] is not None:
            from .osm_map import OSMMapBuilder
            self.osm_map = OSMMapBuilder(self.config['osm_map'])
            self.config['osm_map'] = dict(self.osm_map.config)

    def emit(self, topic, message, ns):
        if topic not in self.channels:
            name = message.DESCRIPTOR.full_name
            if name not in self.schemas:
                data = build_file_descriptor_set(type(message)).SerializeToString()
                self.schemas[name] = self.writer.register_schema(name=name, encoding='protobuf', data=data)
            self.channels[topic] = self.writer.register_channel(topic=topic, message_encoding='protobuf',
                                                               schema_id=self.schemas[name])
        self.writer.add_message(self.channels[topic], ns, message.SerializeToString(), ns)
        self.counts[topic] += 1

    def scene(self, ns, frame, name):
        update = SceneUpdate()
        deletion = update.deletions.add(type=1)  # ALL, prevents stale entities after empty frames.
        stamp(deletion.timestamp, ns)
        entity = update.entities.add(id=name, frame_id=frame, frame_locked=True)
        stamp(entity.timestamp, ns)
        entity.lifetime.nanos = 500_000_000
        return update, entity

    def line(self, entity, points, rgba, width=0.15):
        line = entity.lines.add(type=0, thickness=width, scale_invariant=False)  # LINE_STRIP
        line.pose.orientation.w = 1
        color(line.color, rgba)
        for x, y, z in points:
            if finite(x, y, z):
                xyz(line.points.add(), x, y, z)
            else:
                raise RecordError('Non-finite trajectory point')

    def process(self, topic, message, ns):
        if topic not in EXPECTED:
            return
        if message.DESCRIPTOR.full_name != EXPECTED[topic]:
            raise RecordError(f'{topic}: unsupported type for CX835 visualization')
        if topic == POSE_TOPIC:
            self.pose(message, ns)
        elif topic == PLAN_TOPIC:
            self.processed_trajectory(message, ns)
        elif topic == MODEL_TOPIC:
            self.model_trajectories(message, ns)
        elif topic in OBSTACLE_TOPICS:
            self.obstacles(topic, message, ns)
        elif topic == BEV_MAP_TOPIC:
            self.bev_map(message, ns)
        elif topic == RADAR_TOPIC:
            self.radar(message, ns)

    @staticmethod
    def _ribbon_edges(points, width):
        """Return left/right vertices for a constant-width planar trajectory."""
        clean = []
        for point in points:
            if not finite(point.x, point.y):
                continue
            xy = (point.x, point.y)
            if not clean or math.hypot(xy[0] - clean[-1][0], xy[1] - clean[-1][1]) > 1e-6:
                clean.append(xy)
        if len(clean) < 2:
            return []

        result = []
        half_width = width / 2
        for index, (x, y) in enumerate(clean):
            if index == 0:
                dx, dy = clean[1][0] - x, clean[1][1] - y
            elif index == len(clean) - 1:
                dx, dy = x - clean[-2][0], y - clean[-2][1]
            else:
                dx, dy = clean[index + 1][0] - clean[index - 1][0], clean[index + 1][1] - clean[index - 1][1]
            length = math.hypot(dx, dy)
            nx, ny = -dy / length * half_width, dx / length * half_width
            result.append(((x + nx, y + ny, 0.08), (x - nx, y - ny, 0.08)))
        return result

    def processed_trajectory(self, message, ns):
        """Render the processed trajectory as a translucent, vehicle-width ribbon."""
        update, entity = self.scene(ns, self.config['trajectory_frame'], 'planned_trajectory')
        points = [p.path_point for p in message.trajectory_point] or list(message.path_point)
        edges = self._ribbon_edges(points, self.config['processed_trajectory_width'])
        if len(edges) < 2:
            self.skipped['short_processed_trajectory'] += 1
            self.emit('/foxglove/planning/trajectory', update, ns)
            return

        triangle = entity.triangles.add()
        triangle.pose.orientation.w = 1
        color(triangle.color, (0.08, 0.42, 0.92, 0.34))
        for (left_a, right_a), (left_b, right_b) in zip(edges, edges[1:]):
            for point in (left_a, right_a, left_b, right_a, right_b, left_b):
                xyz(triangle.points.add(), *point)

        entity.metadata.add(key='source_topic', value=PLAN_TOPIC)
        entity.metadata.add(key='width_m', value=str(self.config['processed_trajectory_width']))
        self.emit('/foxglove/planning/trajectory', update, ns)

    def model_trajectories(self, message, ns):
        update, entity = self.scene(ns, self.config['model_frame'], 'model_trajectories')
        for trajectory in message.trajectories:
            rgba = ((1.0, 0.38, 0.0, 1.0) if trajectory.mode == TRAJECTORY_MODE_GENERAL
                    else (0.30, 0.95, 0.16, 1.0))
            self.line(entity, ((p.point.x, p.point.y, 0.30) for p in trajectory.points), rgba)
        self.emit('/foxglove/planning/model_trajectories', update, ns)

        path_update, path_entity = self.scene(ns, self.config['model_frame'], 'trajectory_path')
        path_points = [(point.x, point.y, 0.01) for point in message.path.points
                       if finite(point.x, point.y)]
        if len(path_points) >= 2:
            self.line(path_entity, path_points, (0.22, 0.22, 0.22, 0.75), 0.22)
            path_entity.metadata.add(key='source_field', value=f'{MODEL_TOPIC}.path')
        else:
            self.skipped['short_trajectory_path'] += 1
        self.emit('/foxglove/planner/trajectory_path', path_update, ns)

    def pose(self, message, ns):
        if not message.HasField('position_enu') or not message.HasField('heading'):
            raise RecordError('GlobalPose is missing position_enu or heading')
        p = message.position_enu
        if not finite(p.x, p.y, message.heading):
            raise RecordError('Non-finite ego pose')
        tf = FrameTransform(parent_frame_id='map', child_frame_id='base_link')
        stamp(tf.timestamp, ns)
        xyz(tf.translation, p.x, p.y)
        yaw(tf.rotation, message.heading)
        self.emit('/foxglove/tf', tf, ns)
        if self.osm_map is not None:
            self.osm_map.observe(message, ns)
        self.pose_count += 1
        if self.pose_count % 10 != 1:
            return
        update, entity = self.scene(ns, 'base_link', 'ego_vehicle')
        cube = entity.cubes.add()
        cube.pose.orientation.w = 1
        cube.pose.position.z = 0.8
        xyz(cube.size, 4.8, 2.0, 1.6)  # illustrative, not measured CX835 dimensions
        color(cube.color, (0.1, 0.6, 1, 0.8))
        self.emit('/foxglove/ego', update, ns)
        self.track.append((p.x, p.y, 0.05))
        self.track = self.track[-3000:]
        update, entity = self.scene(ns, 'map', 'driven_path')
        self.line(entity, self.track, (0.1, 0.65, 1, 1))
        self.emit('/foxglove/localization/trail', update, ns)

    def finalize(self):
        """Emit file-level assets after all pose messages established their bounds."""
        if self.osm_map is None:
            return
        grid = self.osm_map.build()
        if grid is None:
            self.warnings.append('OSM map skipped because no synchronized LLH/ENU pose was available.')
            return
        ns = grid.timestamp.seconds * 1_000_000_000 + grid.timestamp.nanos
        self.emit('/foxglove/map/osm', grid, ns)
        if self.osm_map.missing_tiles:
            self.warnings.append(f'OSM map has {self.osm_map.missing_tiles} missing transparent tiles.')

        attribution = SceneUpdate()
        entity = attribution.entities.add(id='osm_attribution', frame_id='base_link', frame_locked=True)
        stamp(entity.timestamp, ns)
        text = entity.texts.add(billboard=True, font_size=14, scale_invariant=True,
                                text=self.osm_map.config['attribution'])
        text.pose.position.x, text.pose.position.y, text.pose.position.z = -8, -4, 1
        text.pose.orientation.w = 1
        color(text.color, (0.18, 0.20, 0.24, 0.75))
        self.emit('/foxglove/map/osm_attribution', attribution, ns)

    def obstacles(self, topic, message, ns):
        update = SceneUpdate()
        stamp(update.deletions.add(type=1).timestamp, ns)
        for obstacle in message.perception_obstacles:
            p = obstacle.position
            values = (p.x, p.y, p.z, obstacle.theta, obstacle.length, obstacle.width, obstacle.height)
            if not finite(*values) or min(obstacle.length, obstacle.width, obstacle.height) <= 0:
                self.skipped['invalid_obstacle_geometry'] += 1
                continue
            entity = update.entities.add(id=str(obstacle.id), frame_id=self.config['obstacles_frame'],
                                         frame_locked=True)
            stamp(entity.timestamp, ns)
            entity.lifetime.nanos = 500_000_000
            cube = entity.cubes.add()
            xyz(cube.pose.position, p.x, p.y, p.z)
            yaw(cube.pose.orientation, obstacle.theta)
            xyz(cube.size, obstacle.length, obstacle.width, obstacle.height)
            color(cube.color, (1, 0.45, 0.12, 0.6))
            entity.metadata.add(key='source_topic', value=topic)
            entity.metadata.add(key='type', value=str(obstacle.type))
        self.emit('/foxglove' + topic, update, ns)

    @staticmethod
    def _road_points(values, z_offset):
        points = []
        for value in values:
            point = value.point if hasattr(value, 'point') else value
            if finite(point.x, point.y, point.z):
                points.append((point.x, point.y, point.z + z_offset))
        return points

    def _road_line(self, entity, points, rgba, width, line_type=0):
        if len(points) < 2:
            self.skipped['short_bev_map_geometry'] += 1
            return None
        line = entity.lines.add(type=line_type, thickness=width, scale_invariant=False)
        line.pose.orientation.w = 1
        color(line.color, rgba)
        for point in points:
            xyz(line.points.add(), *point)
        return line

    def _lane_line(self, entity, lane):
        points = self._road_points(lane.point, 0.035)
        if len(points) < 2:
            self.skipped['short_bev_map_geometry'] += 1
            return
        properties = list(lane.line_piecewice)
        line = entity.lines.add(type=2, thickness=max(lane.marker_width, 0.16),
                                scale_invariant=False)  # LINE_LIST
        line.pose.orientation.w = 1
        for index, (start, end) in enumerate(zip(points, points[1:])):
            prop = properties[min(index, len(properties) - 1)] if properties else None
            line_type = prop.type if prop else 0
            # Foxglove has no dashed-line primitive. Shorten each measured segment
            # so the source's solid/dash classification remains visually distinct.
            if line_type in (2, 3, 4, 5):
                ratio = 0.68
                end = tuple(a + (b - a) * ratio for a, b in zip(start, end))
            rgb = LANE_COLORS.get(prop.color if prop else 0, LANE_COLORS[0])
            # Keep geometry opaque on the light canvas. Confidence remains available
            # in the original topic and must not make low-confidence lines invisible.
            rgba = (*rgb, 1.0)
            for point in (start, end):
                xyz(line.points.add(), *point)
                color(line.colors.add(), rgba)

    def bev_map(self, message, ns):
        """Render vector BEV road semantics in the vehicle-relative frame."""
        update, entity = self.scene(ns, self.config['bev_maps_frame'], 'bev_map')
        entity.lifetime.nanos = 300_000_000

        for lane in message.lane_line:
            self._lane_line(entity, lane)
        for edge in message.road_edge:
            self._road_line(entity, self._road_points(edge.point, 0.025),
                            ROAD_EDGE_COLOR, 0.18)
        for center in message.center_line:
            self._road_line(entity, self._road_points(center.point, 0.02),
                            (0.20, 0.55, 0.72, 0.35), 0.06)
        for stop in message.stop_line:
            self._road_line(entity, self._road_points(stop.point, 0.045),
                            (1.0, 0.1, 0.1, 0.95), 0.32)
        for crossing in message.cross_walk:
            points = self._road_points(crossing.point, 0.025)
            if not points and crossing.HasField('ploygon'):
                points = self._road_points(crossing.ploygon.point, 0.025)
            self._road_line(entity, points, (0.20, 0.23, 0.28, 0.9), 0.12, line_type=1)
        for interest in message.interest_point:
            p = interest.point
            if not finite(p.x, p.y, p.z):
                self.skipped['invalid_bev_map_point'] += 1
                continue
            sphere = entity.spheres.add()
            xyz(sphere.pose.position, p.x, p.y, p.z + 0.12)
            sphere.pose.orientation.w = 1
            xyz(sphere.size, 0.3, 0.3, 0.3)
            color(sphere.color, (0.42, 0.12, 0.62, 1.0))
        for key, values in (
                ('lane_lines', message.lane_line), ('road_edges', message.road_edge),
                ('center_lines', message.center_line), ('stop_lines', message.stop_line),
                ('crosswalks', message.cross_walk), ('interest_points', message.interest_point)):
            entity.metadata.add(key=key, value=str(len(values)))
        self.emit('/foxglove/perception/bev_maps', update, ns)

    def radar(self, message, ns):
        # Frames may belong to different radar sensors; never concatenate across radar IDs.
        grouped = {}
        for frame in message.frames:
            grouped.setdefault(frame.radar_id, []).extend(frame.points)
        for radar_id, points in grouped.items():
            frame_id = f'radar_{radar_id}'
            extrinsic = self.config['radar_extrinsics'].get(str(radar_id))
            if extrinsic:
                tf = FrameTransform(parent_frame_id='base_link', child_frame_id=frame_id)
                stamp(tf.timestamp, ns)
                xyz(tf.translation, *extrinsic['translation'])
                tf.rotation.x, tf.rotation.y, tf.rotation.z, tf.rotation.w = extrinsic['rotation']
                self.emit('/foxglove/tf/radar', tf, ns)
            cloud = PointCloud(frame_id=frame_id, point_stride=20)
            stamp(cloud.timestamp, ns)
            cloud.pose.orientation.w = 1
            for i, name in enumerate(('x', 'y', 'z', 'doppler', 'snr')):
                cloud.fields.add(name=name, offset=i*4, type=PackedElementField.FLOAT32)
            packed = bytearray()
            for point in points:
                values = (point.x, point.y, point.z, point.doppler, point.SNR)
                if finite(*values):
                    packed.extend(struct.pack('<5f', *values))
                else:
                    self.skipped['nonfinite_radar_points'] += 1
            cloud.data = bytes(packed)
            self.emit(f'/foxglove/radar/points/{radar_id}', cloud, ns)
