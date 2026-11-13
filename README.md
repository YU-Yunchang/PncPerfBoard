# PerfBoard

本地路测数据分析工具：导入 Apollo Cyber Record，后台转换 Foxglove MCAP，查看 GPS 路线、闭环状态、接管事件和人工标注。无需 Apollo、ROS、Node.js、GPU 或 Whisper 即可使用核心功能。

## 最快启动

安装 [Python](https://www.python.org/downloads/) 3.9 或更高版本（推荐 3.11；Windows 安装时勾选 **Add Python to PATH**），下载本仓库 ZIP 并解压，或执行：

```bash
git clone https://github.com/YU-Yunchang/PncPerfBoard.git
cd PncPerfBoard
```

- **Windows**：双击 `start.bat`，或运行 `py -3 start.py`。
- **macOS**：终端运行 `python3 start.py`，也可双击 `start.command`；系统阻止时用终端启动。
- **Linux**：终端运行 `python3 start.py`；如提示缺少 venv，先安装系统的 `python3-venv`。

首次启动会自动创建 `.venv`、联网安装依赖并打开 <http://127.0.0.1:8766>；后续启动复用环境。关闭服务按 Ctrl+C。不需要安装 Whisper，也不会下载语音模型。

```bash
python3 start.py --port 8767          # 端口冲突时
python3 start.py --no-browser        # 不自动打开浏览器
python3 start.py --setup-only        # 只安装依赖
```

在网页点击 **Import Run**，选择本机 record 所在目录，填写名称和版本标签并导入。等待后台转换后打开 **Details** 查看路线、闭环与事件。输入支持完整、未压缩的 Cyber Record 1.0；3D 与闭环分析字段针对 CX835 / RINA schema。

数据库保存在 `data/perfboard.sqlite3`，MCAP 写入输入数据旁的 `mcap/` 目录，请确保输入目录可写。仓库不包含路测数据、数据库、录音或模型；同伴需要使用自己的 record。

## 可选功能

- **Foxglove**：查看 3D MCAP 时安装 Foxglove 桌面版，并关联 `.mcap` 文件。未安装也可使用 PerfBoard 的路线、状态与事件页面；亦可在 Foxglove 中手动打开生成的 MCAP。Windows/macOS/Linux 均有本机打开文件入口（Linux 需要 `xdg-open`）。
- **地图底图**：浏览器通过 OpenStreetMap 加载底图，需要网络；record 转换和数据分析在本机进行。
- **语音转写**：默认不安装。当前扩展基于 Apple Silicon + MLX 和预先缓存的 Whisper 模型，详见 [语音指南](docs/voice-events.md)。未配置时页面会提示并禁用上传/转写，核心功能可直接使用。

服务默认仅监听本机 `127.0.0.1`，可以访问本机文件，不应作为公共网络服务部署。

## Cyber Record 转 Foxglove MCAP

这个工具将 Apollo Cyber Record 1.0 文件转换成 Foxglove 可直接打开的 MCAP。转换过程保留原始 topic、Protobuf 消息、纳秒时间戳和嵌入式 schema；`--foxglove` 还会生成 Foxglove 原生 3D 消息。

当前已针对 CX835 / RINA 数据适配：

- 车辆位姿与 `map -> base_link` 变换
- 已行驶轨迹
- 半透明带状规划轨迹、模型候选轨迹与独立的 planner path
- 感知障碍物立方体
- BEV 道路语义：车道线、道路边缘、中心线、停止线、斑马线和兴趣点
- 4D 雷达点云；每个雷达使用独立坐标系
- 可选的离线 OSM 浅色地图贴图（本地 MBTiles 或 XYZ 瓦片目录）

## PerfBoard 网页

本项目包含本地 PerfBoard，用于导入 run、后台转换每分钟 MCAP、查看转换状态、GPS 路线和 Closed-loop Active 时间序列：

```bash
.venv/bin/perfboard
```

打开 `http://127.0.0.1:8766`。数据库默认写入 `data/perfboard.sqlite3`；原始 record 保留在原目录，MCAP 写入每分钟数据目录的 `mcap` 文件夹。Details 页面可逐分钟调用本地 Foxglove 打开转换后的 MCAP。

## 安装

需要 Python 3.9 或更高版本。在项目目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

## 转换

转换单个文件并添加 3D topic：

```bash
.venv/bin/record2mcap convert \
  /path/to/input.record \
  -o /path/to/output.mcap \
  --foxglove
```

递归转换目录：

```bash
.venv/bin/record2mcap convert \
  /path/to/record-directory \
  -o /path/to/mcap-directory \
  --foxglove
```

已有输出不会被覆盖。确认要替换时增加 `--overwrite`。转换先写入临时文件，完成全量解码、CRC 和原始消息哈希校验后再发布最终文件；失败不会留下看似完整的 MCAP。

## 在 Foxglove 中查看

1. 选择“打开本地文件”，打开生成的 `.mcap`。
2. 添加或选中 3D 面板，将固定参考系和显示参考系设为 `base_link`。
3. 在 3D 面板的“主题”中启用 `/foxglove/ego`、`/foxglove/localization/trail`、`/foxglove/planning/trajectory`、`/foxglove/planner/trajectory_path`、`/foxglove/perception/bev_maps` 和需要的障碍物 topic。
4. 点击播放。原始 topic 也可直接用于 Raw Messages、Plot 和现有自定义面板。

### OSM 地图贴图

OSM 地图贴图默认关闭，不会访问瓦片数据源，也不会生成地图 topic。需要时在 `--config` JSON 中显式设置 `osm_map`；`source` 可以是本地 `.mbtiles`，或者本地 XYZ 目录（`<source>/<z>/<x>/<y>.png`）。转换器使用同一帧定位中的 LLH/ENU 对齐地图，并生成自包含的 `/foxglove/map/osm` `foxglove.Grid` topic。在 3D 面板将该 topic 的 Color mode 设为 `RGBA (separate fields)`，并开启 `Draw behind`。

```json
{
  "osm_map": {
    "source": "/absolute/path/to/map.mbtiles",
    "zoom": 17,
    "opacity": 0.18,
    "saturation": 0.22,
    "brightness": 1.18
  }
}
```

为保持 MCAP 离线可回放，转换器不接受远程瓦片 URL。请使用允许离线使用的瓦片数据源或自建服务；不要批量下载 `tile.openstreetmap.org`。地图层会同时输出 `/foxglove/map/osm_attribution` 归属说明。

示例文件没有 `/hirain/radar4d/raw_pcds` 消息，所以该文件不会显示真实点云。其他 record 只要该 topic 有消息，转换器就会生成 `/foxglove/radar/points/<radar_id>`。

## 雷达外参

雷达点云默认保留在独立的 `radar_<id>` 坐标系中。要叠加到车辆坐标系，复制并修改 [foxglove.example.json](foxglove.example.json)，填写实车标定的平移 `[x, y, z]` 和四元数 `[x, y, z, w]`：

```bash
.venv/bin/record2mcap convert input.record -o output.mcap \
  --foxglove --config foxglove.json
```

不要直接使用示例中的零外参作为实车标定值。

## 检查与验证

查看 record 头信息和 topic：

```bash
.venv/bin/record2mcap inspect input.record
```

全量检查 MCAP，并与源 record 做时间戳和 payload 哈希比对：

```bash
.venv/bin/record2mcap verify output.mcap --source input.record
```

转换报告写在 `output.mcap.report.json`，包含消息数、topic 类型、时间范围、哈希和警告。

输出目录可以位于 exFAT 移动硬盘上。不支持硬链接的文件系统会在临时 MCAP 完成全量校验后，以排他创建方式复制到最终路径，避免覆盖已有文件；复制失败时清理未完成的输出。此兼容路径需要额外一次复制，复制期间最终路径可见，PerfBoard 在保存完成后才标记文件可用。若进程被强制终止或硬盘断开，最终路径可能留下不完整文件；重新导入前应删除该失败的输出（保留源 record）。

## 坐标与时间约定

- `map` 使用定位消息中的 ENU `x/y` 和 heading，按平面驾驶场景处理，车辆 `z=0`。
- 感知障碍物和模型轨迹使用 `base_link`；处理后的全局规划轨迹使用 `map`。
- 处理后的规划轨迹显示为半透明蓝色轨迹带，默认宽度为 `2.2 m`；可通过 `processed_trajectory_width` 调整。
- BEV 道路语义默认使用 `base_link`，可通过配置文件中的 `bev_maps_frame` 修改。
- BEV 使用适配 Foxglove 浅色 3D 画布的高对比配色；原始白线显示为深蓝，原始黄线显示为橘色，路沿显示为中灰。
- 模型候选轨迹中 `mode = TRAJECTORY_MODE_GENERAL` 的轨迹显示为不透明荧光橙，其他轨迹显示为不透明荧光绿；不使用 `selected_mode` 决定高亮。planner path 使用独立 topic，以 75% 不透明度的深灰显示，便于单独开关。
- BEV centerline 使用较细的半透明浅蓝线，作为辅助参考，避免盖过车道边界。
- 派生 3D 几何保留原有坐标高度；BEV 道路点仅增加很小的显示偏移，障碍物 bbox 保持源消息高度。Foxglove 原生 3D 面板的 SceneUpdate 没有图层顺序字段，因此不同 topic 的遮挡由面板的深度渲染决定，不能仅靠 MCAP 消息指定固定前后顺序。
- Foxglove 派生消息采用 record 的记录时间，不额外推断或补偿传感器延迟。
- 原始消息不经过重新序列化，始终完整保留。

## 已知输入限制

- 支持完整、未压缩的 Apollo Cyber Record 1.0。
- 输入 record 的 chunk 压缩目前不支持，工具会明确报错而不会生成部分结果。
- 3D 字段适配针对当前 CX835 / RINA schema；其他车型仍可无损转换原始 topic，但需要另加 3D adapter。
# Voice event workflow

PerfBoard supports offline recording transcription, structured voice event imports,
takeover association and audio evidence playback. See [the voice event guide](docs/voice-events.md).

## 开发与验证

```bash
python -m unittest discover -s tests
# 可选：安装 Node.js 后运行前端回归测试（运行工具本身不需要 Node.js）
node --test tests/test_perfboard_frontend.cjs
```
