#!/usr/bin/env python3
"""CLI Entrypoint for Garmin CN <-> Global Full-Fidelity Migrator."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from garmin_migrator.auth import DualRegionAuth
from garmin_migrator.config import MigratorConfig
from garmin_migrator.ledger import MigrationLedger
from garmin_migrator.migrators.activities import ActivityMigrator
from garmin_migrator.migrators.body_health import BodyHealthMigrator
from garmin_migrator.migrators.gear_workouts import GearAndWorkoutsMigrator
from garmin_migrator.migrators.offline_export import OfflineExportImporter
from garmin_migrator.migrators.wellness import WellnessMigrator


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _auto_detect_earliest_activity_date(source_client) -> str:
    """Find the calendar date of the user's oldest activity, or fallback to 365 days ago."""
    try:
        total = source_client.count_activities()
        if total and total > 0:
            oldest_batch = source_client.get_activities(max(0, total - 1), 1)
            if isinstance(oldest_batch, list) and oldest_batch:
                start_local = str(oldest_batch[-1].get("startTimeLocal") or "")
                if len(start_local) >= 10:
                    return start_local[:10]
    except Exception:
        pass
    return (date.today() - timedelta(days=365)).isoformat()


def cmd_check(args: argparse.Namespace, config: MigratorConfig) -> int:
    """Authenticate both accounts, cache tokens, and check target account readiness."""
    auth = DualRegionAuth(config)
    print("\n================ 1. 验证源端与目标端账号登录 ================")
    src = auth.login_region(
        is_cn=(config.source_region == "cn"), force_relogin=args.force_relogin
    )
    tgt = auth.login_region(
        is_cn=(config.target_region == "cn"), force_relogin=args.force_relogin
    )

    src_info = auth.inspect_account_readiness(src)
    tgt_info = auth.inspect_account_readiness(tgt)

    print("\n================ 2. 账号状态与迁移就绪检查 ================")
    for label, info in (
        (f"源账号 ({config.source_region.upper()})", src_info),
        (f"目标账号 ({config.target_region.upper()})", tgt_info),
    ):
        print(f"\n📌 【{label}】 ({info['domain']})")
        print(f"   • 姓名 / DisplayName : {info['full_name']} ({info['display_name']})")
        print(f"   • Profile ID / 时区  : {info['profile_id']} / {info['time_zone']}")
        print(f"   • 历史活动总数量     : {info['activity_count']} 条")
        print(f"   • 已绑定硬件设备数   : {len(info['devices'])} 台")
        for dev in info["devices"]:
            print(
                f"     - {dev.get('productDisplayName')} (DeviceID: {dev.get('deviceId')})"
            )

    if not tgt_info["has_wellness_device"]:
        print(
            "\n⚠️  【重要提示】目标账号当前未检测到已绑定的佳明设备！\n"
            "   • 运动轨迹 (.fit/.tcx/.gpx)、体重体脂、血压、跑鞋装备、训练计划 可以正常迁移。\n"
            "   • 但如果迁移【每日健康监测数据 (步数/睡眠/全天心率/身体电量/HRV)】，佳明服务器会返回 HTTP 419 拒绝接收。\n"
            "   • 建议：先在手机 Garmin Connect App 登录国际区账号，把您的佳明手表配对同步一次，再执行健康数据迁移。"
        )
    else:
        print("\n✅ 目标账号已绑定佳明设备，运动数据与全量健康数据均已就绪，可随时开始迁移！")
    return 0


def cmd_status(args: argparse.Namespace, config: MigratorConfig) -> int:
    """Print SQLite ledger statistics."""
    ledger = MigrationLedger(config.ledger_db_path)
    try:
        summary = ledger.get_summary_stats(config.direction)
        print(f"\n📊 本地迁移账本统计 ({config.ledger_db_path})")
        print(f"   迁移方向: {config.direction}\n")
        labels = {
            "activities": "🏃 运动活动 (Activities)",
            "wellness_days": "💓 每日健康监测 (Wellness Days)",
            "weigh_ins": "⚖️ 体重与体脂 (Weigh-ins)",
            "blood_pressure": "🩺 血压记录 (Blood Pressure)",
            "hydration": "💧 饮水记录 (Hydration)",
            "gear": "👟 运动装备 (Gear)",
            "workouts": "📋 自定义训练 (Workouts)",
            "offline_files": "📦 离线导入文件 (Offline Export)",
        }
        for key, title in labels.items():
            counts = summary.get(key) or {}
            if not counts:
                print(f"   {title:<34}: 暂无记录")
            else:
                parts = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"   {title:<34}: {parts}")
        print()
    finally:
        ledger.close()
    return 0


def cmd_migrate_activities(args: argparse.Namespace, config: MigratorConfig) -> int:
    if args.no_metadata:
        config.sync_activity_metadata = False
    auth = DualRegionAuth(config)
    ledger = MigrationLedger(config.ledger_db_path)
    try:
        migrator = ActivityMigrator(
            auth.source_client, auth.target_client, ledger, config
        )
        migrator.migrate(
            start_offset=args.start_offset,
            max_count=args.limit,
            start_date=args.start_date,
            end_date=args.end_date,
            oldest_first=not args.newest_first,
            dry_run=args.dry_run,
        )
    finally:
        ledger.close()
    return 0


def cmd_migrate_wellness(args: argparse.Namespace, config: MigratorConfig) -> int:
    auth = DualRegionAuth(config)
    ledger = MigrationLedger(config.ledger_db_path)
    try:
        src = auth.source_client
        tgt = auth.target_client
        if args.days and args.days > 0:
            end_d = date.today() - timedelta(days=args.start_days_ago)
            start_d = end_d - timedelta(days=args.days - 1)
            start_date_str = start_d.isoformat()
            end_date_str = end_d.isoformat()
        else:
            end_date_str = args.end_date or date.today().isoformat()
            start_date_str = args.start_date or _auto_detect_earliest_activity_date(src)

        migrator = WellnessMigrator(src, tgt, ledger, config)
        migrator.migrate_date_range(
            start_date=start_date_str,
            end_date=end_date_str,
            oldest_first=not args.newest_first,
            dry_run=args.dry_run,
        )
    finally:
        ledger.close()
    return 0


def cmd_migrate_body(args: argparse.Namespace, config: MigratorConfig) -> int:
    auth = DualRegionAuth(config)
    ledger = MigrationLedger(config.ledger_db_path)
    try:
        migrator = BodyHealthMigrator(
            auth.source_client, auth.target_client, ledger, config
        )
        migrator.migrate_weight_and_body_composition(
            start_date=args.start_date,
            end_date=args.end_date,
            dry_run=args.dry_run,
        )
        migrator.migrate_blood_pressure(
            start_date=args.start_date or "2018-01-01",
            end_date=args.end_date,
            dry_run=args.dry_run,
        )
    finally:
        ledger.close()
    return 0


def cmd_migrate_gear(args: argparse.Namespace, config: MigratorConfig) -> int:
    auth = DualRegionAuth(config)
    ledger = MigrationLedger(config.ledger_db_path)
    try:
        migrator = GearAndWorkoutsMigrator(
            auth.source_client, auth.target_client, ledger, config
        )
        migrator.migrate_gear(
            relink_activities=not args.no_relink,
            dry_run=args.dry_run,
        )
    finally:
        ledger.close()
    return 0


def cmd_migrate_workouts(args: argparse.Namespace, config: MigratorConfig) -> int:
    auth = DualRegionAuth(config)
    ledger = MigrationLedger(config.ledger_db_path)
    try:
        migrator = GearAndWorkoutsMigrator(
            auth.source_client, auth.target_client, ledger, config
        )
        migrator.migrate_workouts(dry_run=args.dry_run)
    finally:
        ledger.close()
    return 0


def cmd_migrate_all(args: argparse.Namespace, config: MigratorConfig) -> int:
    """Execute full end-to-end migration across all sport and health domains."""
    auth = DualRegionAuth(config)
    ledger = MigrationLedger(config.ledger_db_path)
    try:
        src = auth.source_client
        tgt = auth.target_client

        # 1. Activities
        print("\n================ [阶段 1/4] 迁移所有运动记录与自定义元数据 ================")
        act_migrator = ActivityMigrator(src, tgt, ledger, config)
        act_migrator.migrate(
            max_count=args.limit,
            start_date=args.start_date,
            end_date=args.end_date,
            dry_run=args.dry_run,
        )

        # 2. Gear + Activity Relinking + Workouts
        print("\n================ [阶段 2/4] 迁移运动装备(跑鞋/单车)与自定义训练计划 ================")
        gw_migrator = GearAndWorkoutsMigrator(src, tgt, ledger, config)
        gw_migrator.migrate_gear(relink_activities=True, dry_run=args.dry_run)
        gw_migrator.migrate_workouts(dry_run=args.dry_run)

        # 3. Weight / Body Composition & Blood Pressure
        print("\n================ [阶段 3/4] 迁移体重、12项体脂成分与血压数据 ================")
        body_migrator = BodyHealthMigrator(src, tgt, ledger, config)
        body_migrator.migrate_weight_and_body_composition(
            start_date=args.start_date,
            end_date=args.end_date,
            dry_run=args.dry_run,
        )
        body_migrator.migrate_blood_pressure(
            start_date=args.start_date or "2018-01-01",
            end_date=args.end_date,
            dry_run=args.dry_run,
        )

        # 4. Daily Wellness (.fit monitoring archives)
        if not args.skip_wellness:
            print("\n================ [阶段 4/4] 迁移每日全天候健康监测数据 (步数/睡眠/心率/身体电量/HRV) ================")
            w_start = args.start_date or _auto_detect_earliest_activity_date(src)
            w_end = args.end_date or date.today().isoformat()
            well_migrator = WellnessMigrator(src, tgt, ledger, config)
            well_migrator.migrate_date_range(
                start_date=w_start,
                end_date=w_end,
                dry_run=args.dry_run,
            )
    finally:
        ledger.close()
    return 0


def cmd_import_offline(args: argparse.Namespace, config: MigratorConfig) -> int:
    auth = DualRegionAuth(config)
    ledger = MigrationLedger(config.ledger_db_path)
    try:
        importer = OfflineExportImporter(auth.target_client, ledger, config)
        importer.import_from_path(
            args.path,
            max_files=args.limit,
            dry_run=args.dry_run,
        )
    finally:
        ledger.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate.py",
        description="佳明中国区 (garmin.cn) <-> 佳明国际区 (garmin.com) 全量运动与健康信息迁移工具",
    )
    parser.add_argument(
        "--direction",
        choices=["cn_to_global", "global_to_cn"],
        default=None,
        help="覆盖迁移方向 (默认读取 .env 中的 MIGRATE_DIRECTION，即 cn_to_global)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="覆盖每次上传之间的间隔秒数 (默认 1.5s)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="输出详细的调试日志"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # 1. check
    p_check = sub.add_parser("check", help="登录双区账号、缓存 Token 并检查目标账号设备与权限就绪状态")
    p_check.add_argument("--force-relogin", action="store_true", help="强制忽略缓存重新输入密码/验证码登录")
    p_check.set_defaults(func=cmd_check)

    # 2. status
    p_status = sub.add_parser("status", help="查看本地 SQLite 账本记录的各模块迁移进度统计")
    p_status.set_defaults(func=cmd_status)

    # 3. migrate-all
    p_all = sub.add_parser("migrate-all", help="一键全量迁移：运动轨迹+元数据+跑鞋装备+训练计划+体重体脂+血压+每日健康监测")
    p_all.add_argument("--start-date", help="起始日期 YYYY-MM-DD (可选)")
    p_all.add_argument("--end-date", help="结束日期 YYYY-MM-DD (可选)")
    p_all.add_argument("--limit", type=int, default=None, help="最多迁移多少条运动 (默认全量)")
    p_all.add_argument("--skip-wellness", action="store_true", help="跳过按天下载的每日健康监测 .fit 包")
    p_all.add_argument("--dry-run", action="store_true", help="仅预览将要迁移的数据，不实际写入目标账号")
    p_all.set_defaults(func=cmd_migrate_all)

    # 4. migrate-activities
    p_act = sub.add_parser("migrate-activities", help="迁移所有运动记录 (.fit/.tcx/.gpx/手动记录) 并还原自定义名称、备注与力量训练组数")
    p_act.add_argument("--start-offset", type=int, default=0, help="从第几条活动开始偏移 (默认 0)")
    p_act.add_argument("--limit", type=int, default=None, help="最多迁移多少条活动 (默认全量)")
    p_act.add_argument("--start-date", help="按起始日期筛选 YYYY-MM-DD")
    p_act.add_argument("--end-date", help="按结束日期筛选 YYYY-MM-DD")
    p_act.add_argument("--newest-first", action="store_true", help="从最新向最旧迁移 (默认从最旧向最新正序迁移)")
    p_act.add_argument("--no-metadata", action="store_true", help="不额外同步活动自定义名称、备注与力量训练组数")
    p_act.add_argument("--dry-run", action="store_true", help="仅预览不实际上传")
    p_act.set_defaults(func=cmd_migrate_activities)

    # 5. migrate-wellness
    p_well = sub.add_parser("migrate-wellness", help="迁移每日全天候健康监测数据 (步数、睡眠、心率、压力、身体电量、HRV)")
    p_well.add_argument("--start-date", help="起始日期 YYYY-MM-DD (不填则自动检测最早一条运动日期)")
    p_well.add_argument("--end-date", help="结束日期 YYYY-MM-DD (默认今天)")
    p_well.add_argument("--days", type=int, default=None, help="或者指定迁移最近 N 天的数据")
    p_well.add_argument("--start-days-ago", type=int, default=0, help="配合 --days 使用，从几天前开始往回推")
    p_well.add_argument("--newest-first", action="store_true", help="从最近日期向过去迁移")
    p_well.add_argument("--dry-run", action="store_true", help="仅预览不实际上传")
    p_well.set_defaults(func=cmd_migrate_wellness)

    # 6. migrate-body
    p_body = sub.add_parser("migrate-body", help="迁移体重、12项身体成分 (体脂/骨骼肌/水分/代谢年龄等 FIT 编码) 与血压记录")
    p_body.add_argument("--start-date", help="起始日期 YYYY-MM-DD (默认自动探测最早体重记录日期)")
    p_body.add_argument("--end-date", help="结束日期 YYYY-MM-DD (默认今天)")
    p_body.add_argument("--dry-run", action="store_true", help="仅预览不实际上传")
    p_body.set_defaults(func=cmd_migrate_body)

    # 7. migrate-gear
    p_gear = sub.add_parser("migrate-gear", help="迁移跑鞋/自行车等装备并在目标端重新关联已迁移的运动记录")
    p_gear.add_argument("--no-relink", action="store_true", help="仅创建装备，不自动关联活动")
    p_gear.add_argument("--dry-run", action="store_true", help="仅预览不实际创建")
    p_gear.set_defaults(func=cmd_migrate_gear)

    # 8. migrate-workouts
    p_work = sub.add_parser("migrate-workouts", help="迁移自定义训练计划模板 (间歇跑、力量训练计划等)")
    p_work.add_argument("--dry-run", action="store_true", help="仅预览不实际上传")
    p_work.set_defaults(func=cmd_migrate_workouts)

    # 9. import-offline
    p_off = sub.add_parser(
        "import-offline",
        help="从佳明官方导出的全量数据包 (DI_CONNECT 文件夹或 .zip 压缩包) 批量离线导入目标账号",
    )
    p_off.add_argument("path", help="本地全量导出包 .zip 文件路径或解压后的文件夹路径")
    p_off.add_argument("--limit", type=int, default=None, help="限制本次最多导入的文件数量")
    p_off.add_argument("--dry-run", action="store_true", help="仅扫描统计不实际上传")
    p_off.set_defaults(func=cmd_import_offline)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    config = MigratorConfig.from_env()
    if args.direction:
        config.direction = args.direction
    if args.delay is not None:
        config.upload_delay_seconds = max(0.0, args.delay)

    try:
        return args.func(args, config)
    except KeyboardInterrupt:
        print("\n⏹ 用户中断运行。由于已启用 SQLite 断点续传账本，下次运行将自动从断点继续。")
        return 130
    except Exception as exc:
        logging.getLogger(__name__).error("❌ 运行终止: %s", exc)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
