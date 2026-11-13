"""Build a self-contained Foxglove Grid from locally supplied OSM tiles."""
from __future__ import annotations

import io
import math
import sqlite3
from pathlib import Path

from foxglove_schemas_protobuf.Grid_pb2 import Grid
from foxglove_schemas_protobuf.PackedElementField_pb2 import PackedElementField

from .cyber import RecordError
from .perfboard_analysis import gcj02_to_wgs84


EARTH_RADIUS_M = 6_378_137.0
DEFAULTS = {
    "zoom": 17,
    "opacity": 0.18,
    "saturation": 0.22,
    "brightness": 1.18,
    "padding_tiles": 1,
    "max_tiles": 64,
    "z_offset": -0.02,
    "attribution": "© OpenStreetMap contributors",
}


def _tile_xy(lon, lat, zoom):
    count = 1 << zoom
    lat = max(-85.05112878, min(85.05112878, lat))
    x = (lon + 180.0) / 360.0 * count
    radians = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * count
    return x, y


def _tile_lon(x, zoom):
    return x / (1 << zoom) * 360.0 - 180.0


def _tile_lat(y, zoom):
    mercator = math.pi * (1.0 - 2.0 * y / (1 << zoom))
    return math.degrees(math.atan(math.sinh(mercator)))


def _mercator(lon, lat):
    lat = max(-85.05112878, min(85.05112878, lat))
    return (EARTH_RADIUS_M * math.radians(lon),
            EARTH_RADIUS_M * math.asinh(math.tan(math.radians(lat))))


class OSMMapBuilder:
    """Collect pose bounds and turn XYZ or MBTiles raster tiles into one RGBA grid."""

    def __init__(self, config):
        if not isinstance(config, dict):
            raise RecordError("osm_map must be an object")
        unknown = set(config) - ({"source"} | set(DEFAULTS))
        if unknown:
            raise RecordError(f"Unknown osm_map keys: {sorted(unknown)}")
        if not config.get("source"):
            raise RecordError("osm_map.source is required")
        self.config = {**DEFAULTS, **config}
        source = str(self.config["source"])
        if "://" in source:
            raise RecordError("osm_map.source must be local; remote tile downloads are not supported")
        self.source = Path(source).expanduser().resolve()
        if not self.source.exists() and not any(token in source for token in ("{z}", "{x}", "{y}")):
            raise RecordError(f"OSM tile source does not exist: {self.source}")
        zoom = self.config["zoom"]
        if not isinstance(zoom, int) or not 0 <= zoom <= 22:
            raise RecordError("osm_map.zoom must be an integer from 0 to 22")
        for name in ("opacity", "saturation"):
            value = self.config[name]
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise RecordError(f"osm_map.{name} must be a finite number from 0 to 1")
        brightness = self.config["brightness"]
        if not isinstance(brightness, (int, float)) or not math.isfinite(brightness) or brightness <= 0:
            raise RecordError("osm_map.brightness must be a positive finite number")
        for name, upper in (("padding_tiles", 8), ("max_tiles", 256)):
            value = self.config[name]
            if not isinstance(value, int) or not 0 <= value <= upper:
                raise RecordError(f"osm_map.{name} must be an integer from 0 to {upper}")
        if self.config["max_tiles"] < 1:
            raise RecordError("osm_map.max_tiles must be at least 1")
        z_offset = self.config["z_offset"]
        if not isinstance(z_offset, (int, float)) or not math.isfinite(z_offset):
            raise RecordError("osm_map.z_offset must be finite")
        self.anchor = None
        self.bounds = None
        self.samples = []
        self.missing_tiles = 0

    def observe(self, message, ns):
        if not message.HasField("position_llh") or not message.HasField("position_enu"):
            return
        llh, enu = message.position_llh, message.position_enu
        values = (llh.lon, llh.lat, enu.x, enu.y)
        if not all(math.isfinite(value) for value in values):
            return
        if not -180 <= llh.lon <= 180 or not -90 <= llh.lat <= 90:
            return
        lon, lat = gcj02_to_wgs84(llh.lon, llh.lat)
        if self.anchor is None:
            self.anchor = (lon, lat, enu.x, enu.y, ns)
            self.bounds = [lon, lat, lon, lat]
        else:
            self.bounds[0] = min(self.bounds[0], lon)
            self.bounds[1] = min(self.bounds[1], lat)
            self.bounds[2] = max(self.bounds[2], lon)
            self.bounds[3] = max(self.bounds[3], lat)
        self.samples.append((lon, lat, enu.x, enu.y))

    def _map_alignment(self):
        """Fit a similarity transform from local east/north to the record's map x/y."""
        anchor_lon, anchor_lat, anchor_x, anchor_y, _ = self.anchor
        anchor_mx, anchor_my = _mercator(anchor_lon, anchor_lat)
        ground_scale = math.cos(math.radians(anchor_lat))
        denominator = coefficient_a = coefficient_b = 0.0
        for lon, lat, map_x, map_y in self.samples:
            mx, my = _mercator(lon, lat)
            east = (mx - anchor_mx) * ground_scale
            north = (my - anchor_my) * ground_scale
            dx, dy = map_x - anchor_x, map_y - anchor_y
            denominator += east * east + north * north
            coefficient_a += dx * east + dy * north
            coefficient_b += dy * east - dx * north
        if denominator < 25.0:  # Less than five metres of useful baseline.
            return 1.0, 0.0
        a, b = coefficient_a / denominator, coefficient_b / denominator
        scale = math.hypot(a, b)
        if not math.isfinite(scale) or not 0.5 <= scale <= 2.0:
            raise RecordError(f"Cannot align OSM map to position_enu (fitted scale {scale:.3f})")
        return a, b

    def _read_tile(self, zoom, x, y):
        if self.source.suffix.lower() == ".mbtiles":
            uri = self.source.as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                row = connection.execute(
                    "select tile_data from tiles where zoom_level=? and tile_column=? and tile_row=?",
                    (zoom, x, (1 << zoom) - 1 - y),
                ).fetchone()
            return bytes(row[0]) if row else None

        source_text = str(self.source)
        if any(token in source_text for token in ("{z}", "{x}", "{y}")):
            path = Path(source_text.format(z=zoom, x=x, y=y))
            return path.read_bytes() if path.is_file() else None
        base = self.source / str(zoom) / str(x)
        for suffix in (".png", ".jpg", ".jpeg", ".webp"):
            path = base / f"{y}{suffix}"
            if path.is_file():
                return path.read_bytes()
        return None

    def build(self):
        if self.anchor is None:
            return None
        try:
            from PIL import Image, ImageEnhance
        except ImportError as exc:
            raise RecordError("Pillow is required when osm_map is enabled") from exc

        zoom = self.config["zoom"]
        west, south, east, north = self.bounds
        x0f, y0f = _tile_xy(west, north, zoom)
        x1f, y1f = _tile_xy(east, south, zoom)
        padding = self.config["padding_tiles"]
        limit = (1 << zoom) - 1
        x0 = max(0, math.floor(x0f) - padding)
        y0 = max(0, math.floor(y0f) - padding)
        x1 = min(limit, math.floor(x1f) + padding)
        y1 = min(limit, math.floor(y1f) + padding)
        tile_count = (x1 - x0 + 1) * (y1 - y0 + 1)
        if tile_count > self.config["max_tiles"]:
            raise RecordError(
                f"OSM map needs {tile_count} tiles, exceeding osm_map.max_tiles={self.config['max_tiles']}"
            )

        mosaic = None
        tile_size = None
        for tile_y in range(y0, y1 + 1):
            for tile_x in range(x0, x1 + 1):
                encoded = self._read_tile(zoom, tile_x, tile_y)
                if encoded is None:
                    self.missing_tiles += 1
                    continue
                try:
                    with Image.open(io.BytesIO(encoded)) as image:
                        tile = image.convert("RGBA")
                except Exception as exc:
                    raise RecordError(f"Cannot decode OSM tile z={zoom} x={tile_x} y={tile_y}: {exc}") from exc
                if tile_size is None:
                    if tile.width != tile.height or tile.width < 1:
                        raise RecordError("OSM tiles must be non-empty squares")
                    tile_size = tile.width
                    mosaic = Image.new("RGBA", ((x1 - x0 + 1) * tile_size,
                                                (y1 - y0 + 1) * tile_size), (255, 255, 255, 0))
                if tile.size != (tile_size, tile_size):
                    raise RecordError("All OSM tiles must have the same dimensions")
                mosaic.paste(tile, ((tile_x - x0) * tile_size, (tile_y - y0) * tile_size))
        if mosaic is None:
            raise RecordError("No OSM tiles were found for the record's GPS bounds")

        rgb = mosaic.convert("RGB")
        rgb = ImageEnhance.Color(rgb).enhance(self.config["saturation"])
        rgb = ImageEnhance.Brightness(rgb).enhance(self.config["brightness"])
        alpha = mosaic.getchannel("A").point(lambda value: round(value * self.config["opacity"]))
        mosaic = Image.merge("RGBA", (*rgb.split(), alpha))
        # Slippy-map images are north-to-south. Foxglove Grid rows grow in +Y,
        # so row zero must be the southern edge.
        transpose = getattr(Image, "Transpose", Image)
        mosaic = mosaic.transpose(transpose.FLIP_TOP_BOTTOM)

        anchor_lon, anchor_lat, anchor_x, anchor_y, timestamp = self.anchor
        anchor_mx, anchor_my = _mercator(anchor_lon, anchor_lat)
        west_mx, south_my = _mercator(_tile_lon(x0, zoom), _tile_lat(y1 + 1, zoom))
        local_scale = math.cos(math.radians(anchor_lat))
        east = (west_mx - anchor_mx) * local_scale
        north = (south_my - anchor_my) * local_scale
        alignment_a, alignment_b = self._map_alignment()
        origin_x = anchor_x + alignment_a * east - alignment_b * north
        origin_y = anchor_y + alignment_b * east + alignment_a * north
        alignment_scale = math.hypot(alignment_a, alignment_b)
        meters_per_pixel = (2 * math.pi * EARTH_RADIUS_M * local_scale * alignment_scale /
                            ((1 << zoom) * tile_size))

        grid = Grid(frame_id="map", column_count=mosaic.width,
                    row_stride=mosaic.width * 4, cell_stride=4, data=mosaic.tobytes())
        grid.timestamp.seconds, grid.timestamp.nanos = divmod(timestamp, 1_000_000_000)
        grid.pose.position.x = origin_x
        grid.pose.position.y = origin_y
        grid.pose.position.z = self.config["z_offset"]
        alignment_yaw = math.atan2(alignment_b, alignment_a)
        grid.pose.orientation.z = math.sin(alignment_yaw / 2)
        grid.pose.orientation.w = math.cos(alignment_yaw / 2)
        grid.cell_size.x = meters_per_pixel
        grid.cell_size.y = meters_per_pixel
        for offset, name in enumerate(("red", "green", "blue", "alpha")):
            grid.fields.add(name=name, offset=offset, type=PackedElementField.UINT8)
        return grid
