import argparse
import json
import sys
from pathlib import Path

from .converter import convert, validate
from .cyber import RecordReader


def main():
    parser = argparse.ArgumentParser(description='Cyber Record → Foxglove MCAP，无需 Apollo / ROS')
    sub = parser.add_subparsers(dest='command', required=True)
    inspection = sub.add_parser('inspect', help='查看 record 文件头和 topic')
    inspection.add_argument('input', type=Path)
    conversion = sub.add_parser('convert', help='转换单文件或目录（递归查找 .record 和 .record.*）')
    conversion.add_argument('input', type=Path)
    conversion.add_argument('-o', '--output', type=Path, required=True)
    conversion.add_argument('--foxglove', action='store_true', help='增加 CX835 / RINA 3D topic')
    conversion.add_argument('--config', type=Path, help='3D 坐标系/雷达外参 JSON')
    conversion.add_argument('--compression', choices=['zstd', 'lz4', 'none'], default='zstd')
    conversion.add_argument('--overwrite', action='store_true')
    verification = sub.add_parser('verify', help='全量解码并检查 MCAP，可与源文件对比')
    verification.add_argument('input', type=Path)
    verification.add_argument('--source', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            reader = RecordReader(args.input)
            print(reader.header)
            for channel in reader.channels().values():
                print(f'{channel.name}\t{channel.message_type}')
        elif args.command == 'verify':
            print(json.dumps(validate(args.input, source=args.source), ensure_ascii=False, indent=2))
        else:
            if args.config and not args.foxglove:
                parser.error('--config requires --foxglove')
            config = json.loads(args.config.read_text()) if args.config else None
            if args.input.is_dir():
                files = sorted(p for p in args.input.rglob('*') if p.is_file() and
                               (p.name.endswith('.record') or '.record.' in p.name))
                if not files:
                    raise ValueError(f'No record files found in {args.input}')
                pairs = [(p, args.output / p.relative_to(args.input).parent / (p.name+'.mcap')) for p in files]
            else:
                pairs = [(args.input, args.output)]
            for source, output in pairs:
                print(f'转换 {source} → {output}', flush=True)
                report = convert(source, output, args.foxglove, config, args.compression, args.overwrite,
                                 progress=lambda n, total: print(f'  {n:,}/{total:,}', flush=True))
                print(f"完成：原始 {report['original_messages']:,} 条，"
                      f"3D {sum(report['derived_channels'].values()):,} 条，{report['output_bytes']/1024**2:.1f} MiB")
                for warning in report['warnings']:
                    print('提示：'+warning)
    except (Exception, KeyboardInterrupt) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
