import json
import logging
import re
from pathlib import Path

from app.models import AppConfig

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/data/config.json")

DEFAULT_CONFIG = AppConfig()

# Regex to match old-style drivetemp IDs that contain a block device letter:
_OLD_DRIVETEMP_RE = re.compile(
    r'^(drivetemp-wwid-[^/]+)/sd[a-z]+ \u2014 (.+)$'
)

# Regex to match hwmon IDs with dynamic index (e.g., nct6798-hwmon10/SYSTIN)
_HWMON_ID_RE = re.compile(r'^([a-zA-Z0-9_]+)-hwmon\d+/(.+)$')


def _normalize_hwmon_id(sensor_id: str) -> str:
    """Removes the volatile hwmon index from sensor IDs for comparison (e.g. nct6798-hwmon10/SYSTIN -> nct6798/SYSTIN)."""
    return _HWMON_ID_RE.sub(r'\1/\2', sensor_id)


def _migrate_sensor_id(old_id: str, active_sensor_ids: list[str] | None = None) -> str:
    """
    If old_id matches the old drivetemp format or an outdated hwmon index,
    return the updated format.
    """
    # 1. Drivetemp Migration
    m_drive = _OLD_DRIVETEMP_RE.match(old_id)
    if m_drive:
        return f"{m_drive.group(1)}/{m_drive.group(2)}"

    # 2. Hwmon Migration: If sensor_id has an old hwmon index not in active_sensor_ids, map it
    if active_sensor_ids and old_id not in active_sensor_ids:
        target_norm = _normalize_hwmon_id(old_id)
        for active_id in active_sensor_ids:
            if _normalize_hwmon_id(active_id) == target_norm:
                return active_id

    return old_id


def migrate_sensor_ids(config: AppConfig, active_sensor_ids: list[str] | None = None) -> tuple[AppConfig, int]:
    """
    Rewrite old-style drivetemp or outdated hwmon sensor IDs to the current active sensor format.
    """
    count = 0

    # sensor_aliases
    new_aliases: dict[str, str] = {}
    for sid, alias in config.sensor_aliases.items():
        new_sid = _migrate_sensor_id(sid, active_sensor_ids)
        if new_sid != sid:
            count += 1
            logger.info("Migrated alias key: %s -> %s", sid, new_sid)
        new_aliases[new_sid] = alias
    config.sensor_aliases = new_aliases

    # virtual_sensors
    for vs in config.virtual_sensors:
        new_sources = []
        for sid in vs.source_sensor_ids:
            new_sid = _migrate_sensor_id(sid, active_sensor_ids)
            if new_sid != sid:
                count += 1
                logger.info("Migrated virtual sensor '%s' source: %s -> %s", vs.id, sid, new_sid)
            new_sources.append(new_sid)
        vs.source_sensor_ids = new_sources

    # fan_configs
    for fc in config.fan_configs:
        new_sid = _migrate_sensor_id(fc.sensor_id, active_sensor_ids)
        if new_sid != fc.sensor_id:
            count += 1
            logger.info("Migrated fan config '%s' sensor: %s -> %s", fc.fan_id, fc.sensor_id, new_sid)
            fc.sensor_id = new_sid

    # dashboard_groups
    for grp in config.dashboard_groups:
        new_items = []
        for sid in grp.item_ids:
            new_sid = _migrate_sensor_id(sid, active_sensor_ids)
            if new_sid != sid:
                count += 1
                logger.info("Migrated group '%s' item: %s -> %s", grp.name, sid, new_sid)
            new_items.append(new_sid)
        grp.item_ids = new_items

    # card_colors
    new_colors: dict[str, str] = {}
    for sid, color in config.card_colors.items():
        new_sid = _migrate_sensor_id(sid, active_sensor_ids)
        if new_sid != sid:
            count += 1
            logger.info("Migrated card color key: %s -> %s", sid, new_sid)
        new_colors[new_sid] = color
    config.card_colors = new_colors

    return config, count


def load_config(active_sensor_ids: list[str] | None = None) -> AppConfig:
    """
    Load config from CONFIG_PATH.
    If the file doesn't exist, write defaults and return them.
    """
    if not CONFIG_PATH.exists():
        logger.info("No config file found at %s, writing defaults", CONFIG_PATH)
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.model_copy(deep=True)

    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        config = AppConfig.model_validate(data)
        logger.info("Loaded config from %s", CONFIG_PATH)
    except json.JSONDecodeError as e:
        raise ValueError(f"Config file is not valid JSON: {e}") from e
    except Exception as e:
        raise ValueError(f"Config file failed validation: {e}") from e

    # Fix backend for hwmon-pwm
    backend_fixed = 0
    for fc in config.fan_configs:
        if fc.fan_id.startswith("hwmon-pwm-") and fc.backend != "hwmon-pwm":
            logger.warning("Fixing backend for '%s': %s -> hwmon-pwm", fc.fan_id, fc.backend)
            fc.backend = "hwmon-pwm"
            backend_fixed += 1
    if backend_fixed:
        save_config(config)

    # Migrate IDs (drivetemp and dynamic hwmon)
    config, migrated = migrate_sensor_ids(config, active_sensor_ids)
    if migrated > 0:
        logger.warning("Migrated %d sensor ID(s) in config", migrated)
        save_config(config)

    return config


def save_config(config: AppConfig) -> None:
    """Write config to CONFIG_PATH as pretty-printed JSON."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(config.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_path.replace(CONFIG_PATH)
        logger.info("Saved config to %s", CONFIG_PATH)
    except OSError as e:
        logger.error("Failed to save config: %s", e)
        raise


VALID_CARD_COLORS = {"teal", "blue", "purple", "pink", "amber", "orange", "red", "slate"}


def _is_sensor_known(sensor_id: str, known_sensor_ids: set[str]) -> bool:
    """Checks direct match or match via normalized hwmon ID."""
    if sensor_id in known_sensor_ids:
        return True
    
    target_norm = _normalize_hwmon_id(sensor_id)
    for known_id in known_sensor_ids:
        if _normalize_hwmon_id(known_id) == target_norm:
            return True
            
    return False


def validate_config(config: AppConfig, known_sensor_ids: list[str], known_fan_ids: list[str]) -> list[str]:
    """Validate config against currently detected devices."""
    errors = []
    curve_names = {c.name for c in config.curves}

    virtual_sensor_ids = {vs.id for vs in config.virtual_sensors}
    known_sensors_set = set(known_sensor_ids)
    all_sensor_ids = known_sensors_set | virtual_sensor_ids

    # Validate virtual sensors
    for vs in config.virtual_sensors:
        if not vs.id:
            errors.append("Virtual sensor has empty ID")
        if vs.aggregation not in ("avg", "min", "max"):
            errors.append(
                f"Virtual sensor '{vs.id}' has invalid aggregation '{vs.aggregation}' (must be avg, min, or max)"
            )
        if len(vs.source_sensor_ids) < 2:
            errors.append(
                f"Virtual sensor '{vs.id}' must reference at least 2 source sensors"
            )
        for src_id in vs.source_sensor_ids:
            if not _is_sensor_known(src_id, known_sensors_set):
                errors.append(
                    f"Virtual sensor '{vs.id}' references unknown sensor '{src_id}'"
                )
            if src_id in virtual_sensor_ids:
                errors.append(
                    f"Virtual sensor '{vs.id}' cannot reference another virtual sensor '{src_id}'"
                )

    seen_vs_ids = set()
    for vs in config.virtual_sensors:
        if vs.id in seen_vs_ids:
            errors.append(f"Duplicate virtual sensor ID '{vs.id}'")
        seen_vs_ids.add(vs.id)

    # Validate fan configs
    for fan_cfg in config.fan_configs:
        if fan_cfg.backend not in ("liquidctl", "hwmon-pwm"):
            errors.append(
                f"Fan '{fan_cfg.fan_id}' has invalid backend '{fan_cfg.backend}' (must be liquidctl or hwmon-pwm)"
            )
        if fan_cfg.curve_name not in curve_names:
            errors.append(
                f"Fan '{fan_cfg.fan_id}' references unknown curve '{fan_cfg.curve_name}'"
            )
        if not _is_sensor_known(fan_cfg.sensor_id, all_sensor_ids):
            errors.append(
                f"Fan '{fan_cfg.fan_id}' references unknown sensor '{fan_cfg.sensor_id}'"
            )
        if fan_cfg.fan_id not in known_fan_ids:
            errors.append(
                f"Fan config references unknown fan '{fan_cfg.fan_id}'"
            )

    for curve in config.curves:
        if len(curve.points) < 2:
            errors.append(
                f"Curve '{curve.name}' must have at least 2 points"
            )
        else:
            temps = [p.temp for p in curve.points]
            if temps != sorted(temps):
                errors.append(
                    f"Curve '{curve.name}' points must be in ascending temperature order"
                )

    # Validate dashboard groups
    seen_group_ids = set()
    for grp in config.dashboard_groups:
        if grp.id in seen_group_ids:
            errors.append(f"Duplicate dashboard group ID '{grp.id}'")
        seen_group_ids.add(grp.id)
        if grp.type not in ("sensor", "fan"):
            errors.append(
                f"Dashboard group '{grp.name}' has invalid type '{grp.type}' (must be sensor or fan)"
            )

    # Validate card colors
    for item_id, color in config.card_colors.items():
        if color not in VALID_CARD_COLORS:
            errors.append(
                f"Card color '{color}' for '{item_id}' is not valid (must be one of {VALID_CARD_COLORS})"
            )

    return errors