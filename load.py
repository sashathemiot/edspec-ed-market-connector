"""
EDSpec Plugin for EDMarketConnector

This plugin connects EDMarketConnector with the EDSpec service to share
commander data including ships, credits, current location, and status.

Requires an API key from EDSpec to authenticate requests.

Version: 1.1.0
Developer: sashathemiot
Website: https://edspecbot.com

Copyright (C) 2025-2026 sashathemiot

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""
import base64
import logging
import os
import queue
import re
import threading
import time
import webbrowser
from calendar import timegm
from time import strptime
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

from config import appname, config
import timeout_session
import tkinter as tk
import tkinter.messagebox as messagebox
import myNotebook as nb

# Requires EDMarketConnector 6.1.0+ (Plugin Browser and modern config API).

plugin_name = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(f'{appname}.{plugin_name}')
if not logger.hasHandlers():
    logger.setLevel(logging.INFO)
    _log_handler = logging.StreamHandler()
    _log_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d:%(funcName)s: %(message)s',
    ))
    logger.addHandler(_log_handler)

VERSION = '1.1.0'
GITHUB_REPO = 'sashathemiot/edspec-ed-market-connector'

API_KEY_SETTING = f'{plugin_name}.api_key'
API_URL_SETTING = f'{plugin_name}.api_url'
ENABLED_SETTING = f'{plugin_name}.enabled'
SEND_SHIP_INFO_SETTING = f'{plugin_name}.send_ship_info'
CHECK_UPDATES_SETTING = f'{plugin_name}.check_updates'

DEFAULT_API_URL = 'https://api.edspecbot.com/edmc/ingest'
JOURNAL_ERROR_MESSAGE = 'EDSpec could not send data. Check the log for details.'

# Same tokens as the website: zinc background, orange accent.
ED_BG = '#18181b'
ED_TEXT = '#ffffff'
ED_MUTED = '#a1a1aa'
ED_AMBER = '#f97316'
ED_AMBER_HOVER = '#fb923c'
ED_FIELD = '#27272a'
ED_LINE = '#3f3f46'
ED_FONT = ('Segoe UI', 9)

JOB_COMMANDER = 'commander'
JOB_PING = 'ping'
JOB_ROUTE_SET = 'route_set'
JOB_ROUTE_CLEAR = 'route_clear'
JOB_ROUTE_STATE_RETRY = 'route_state_retry'
work_queue: Optional[queue.Queue] = None
worker_thread: Optional[threading.Thread] = None
ping_thread: Optional[threading.Thread] = None
update_check_thread: Optional[threading.Thread] = None
stop_event = threading.Event()
ping_event = threading.Event()
update_check_event = threading.Event()
update_check_performed = False
prefs_frame: Optional[object] = None
last_connection_status = 'disconnected'
current_cmdr = ''
current_system = ''
current_station = ''
latest_journal_state: dict = {}
game_session_active = False
active_route = None
active_route_hop_index = -1
route_trust_state_nav_route = False
suppressed_fsd_target: Optional[str] = None
game_session_start_ts: Optional[int] = None
last_enqueued_route_signature: Optional[str] = None
route_completion_timer: Optional[threading.Timer] = None
route_cancel_timer: Optional[threading.Timer] = None
route_clear_pending = False
route_clear_requested_at = 0.0
route_jump_hold = False
route_jump_released_at = 0.0
suppressed_route_hops: set = set()
suppress_route_hops_until = 0.0
ROUTE_COMPLETE_CLEAR_SEC = 30
ROUTE_CANCEL_CONFIRM_SEC = 2
ROUTE_CANCEL_MAX_SEC = 20
FSD_CHARGING_FLAG = 1 << 17
FSD_JUMP_FLAG = 1 << 30
IN_FIGHTER_FLAG = 1 << 25
IN_SRV_FLAG = 1 << 26
FUEL_LIVE_INTERVAL_SEC = 2.0
last_live_fuel_sent_at = 0.0
last_live_fuel_level: Optional[float] = None
live_fuel_pending: Optional[float] = None
live_fuel_timer: Optional[threading.Timer] = None
nav_route_state_retry_timer: Optional[threading.Timer] = None
nav_route_state_retry_gen = 0
NAV_ROUTE_STATE_RETRY_DELAYS_SEC = (0.1, 0.25, 0.5, 1.0, 2.0)
commander_ships_cache: dict = {}
active_ship_id_cache: Optional[int] = None
active_fuel_level_cache: Optional[float] = None
active_fuel_capacity_cache: Optional[float] = None
cargo_cache: list = []
cargo_count_cache: int = 0
cargo_vessel_cache: str = 'Ship'
merc_coins_cache: Optional[int] = None
status_label: Optional[tk.Label] = None


def user_agent() -> str:
    base = getattr(config, 'user_agent', None) or f'{appname}/{plugin_name}'
    return f'{base} (EDSpec/{VERSION})'


def config_bool(setting: str, default: bool = True) -> bool:
    if config.get(setting) is None:
        return default
    return bool(config.get_bool(setting))


def is_plugin_enabled() -> bool:
    return config_bool(ENABLED_SETTING)


def get_api_key() -> str:
    return (config.get_str(API_KEY_SETTING) or '').strip()


def send_ship_info_enabled() -> bool:
    return config_bool(SEND_SHIP_INFO_SETTING)


def check_updates_enabled() -> bool:
    return config_bool(CHECK_UPDATES_SETTING)


def get_api_url() -> str:
    url = (config.get_str(API_URL_SETTING) or '').strip()
    return url if url else DEFAULT_API_URL


def _ui_root() -> Optional[tk.Misc]:
    if not status_label:
        return None
    try:
        return status_label.winfo_toplevel()
    except tk.TclError:
        return None


def schedule_on_main_thread(callback: Callable[[], None]) -> None:
    """Run on the EDMC Tk main loop. Skipped while the app is shutting down."""
    if config.shutting_down:
        return
    root = _ui_root()
    if root:
        try:
            root.after(0, callback)
            return
        except tk.TclError:
            pass
    if threading.current_thread() is threading.main_thread():
        callback()


def enqueue_job(kind: str, payload=None) -> None:
    if work_queue is not None:
        work_queue.put((kind, payload))


def commander_in_game(cmdr: Optional[str], system: Optional[str], state: dict) -> bool:
    if not cmdr:
        return False
    if system and str(system).strip():
        return True
    star_system = state.get('StarSystem')
    if isinstance(star_system, str) and star_system.strip():
        return True
    return False


def set_game_session_active(active: bool) -> None:
    global game_session_active
    if game_session_active == active:
        return
    game_session_active = active
    logger.info('Game session active: %s', active)
    schedule_on_main_thread(update_status)
    enqueue_ping(active)
    if not active:
        cancel_route_completion_clear()

def sync_game_session_from_journal(cmdr: Optional[str], system: Optional[str], state: dict) -> None:
    set_game_session_active(commander_in_game(cmdr, system, state))


def _optional_journal_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _optional_journal_bool(value):
    if isinstance(value, bool):
        return value
    return None


def enqueue_fss_progress(cmdr: str, system: str, entry: dict) -> None:
    system_name = entry.get('SystemName') or entry.get('StarSystem') or system
    if not isinstance(system_name, str) or not system_name.strip():
        return
    body_count = entry.get('BodyCount')
    progress = entry.get('Progress')
    enqueue_commander({
        'cmdr': cmdr,
        'system': system_name.strip(),
        'fssProgress': {
            'systemName': system_name.strip(),
            'systemAddress': entry.get('SystemAddress'),
            'bodyCount': body_count if isinstance(body_count, int) and not isinstance(body_count, bool) else None,
            'progress': progress if isinstance(progress, (int, float)) and not isinstance(progress, bool) else None,
            'timestamp': entry.get('timestamp'),
        },
    })


def enqueue_fss_scan_complete(cmdr: str, system: str, entry: dict) -> None:
    system_name = entry.get('SystemName') or entry.get('StarSystem') or system
    if not isinstance(system_name, str) or not system_name.strip():
        return
    count = entry.get('Count')
    enqueue_commander({
        'cmdr': cmdr,
        'system': system_name.strip(),
        'fssScanComplete': {
            'systemName': system_name.strip(),
            'systemAddress': entry.get('SystemAddress'),
            'bodyCount': count if isinstance(count, int) and not isinstance(count, bool) else None,
            'timestamp': entry.get('timestamp'),
        },
    })


def enqueue_fss_mapped(cmdr: str, system: str, entry: dict) -> None:
    body_name = entry.get('BodyName')
    if not isinstance(body_name, str) or not body_name.strip():
        return
    body_id = entry.get('BodyID')
    enqueue_commander({
        'cmdr': cmdr,
        'system': entry.get('StarSystem') or system,
        'fssMapped': {
            'bodyName': body_name.strip(),
            'bodyId': body_id if isinstance(body_id, int) and not isinstance(body_id, bool) else None,
            'systemAddress': entry.get('SystemAddress'),
            'timestamp': entry.get('timestamp'),
        },
    })


def enqueue_fss_scan(cmdr: str, system: str, entry: dict) -> None:
    body_name = entry.get('BodyName')
    if not isinstance(body_name, str) or not body_name.strip():
        return
    planet_class = entry.get('PlanetClass')
    atmosphere = entry.get('Atmosphere')
    enqueue_commander({
        'cmdr': cmdr,
        'system': entry.get('StarSystem') or system,
        'fssPlanet': {
            'bodyName': body_name.strip(),
            'planetClass': planet_class.strip() if isinstance(planet_class, str) else '',
            'terraformable': entry.get('TerraformState') == 'Terraformable',
            'wasDiscovered': _optional_journal_bool(entry.get('WasDiscovered')),
            'wasMapped': _optional_journal_bool(entry.get('WasMapped')),
            'distanceFromArrivalLs': _optional_journal_number(entry.get('DistanceFromArrivalLS')),
            'landable': _optional_journal_bool(entry.get('Landable')),
            'atmosphere': atmosphere.strip() if isinstance(atmosphere, str) else None,
            'surfaceTemperature': _optional_journal_number(entry.get('SurfaceTemperature')),
            'massEarth': _optional_journal_number(entry.get('MassEM')),
            'systemName': entry.get('StarSystem') or system,
            'systemAddress': entry.get('SystemAddress'),
            'bodyId': entry.get('BodyID'),
            'timestamp': entry.get('timestamp'),
        },
    })


def enqueue_commander(data: dict) -> None:
    if not game_session_active:
        logger.debug('Skipping commander payload: commander not in game')
        return
    enqueue_job(JOB_COMMANDER, data)


def location_payload(cmdr: Optional[str], system: Optional[str], station: Optional[str]) -> dict:
    return {'cmdr': cmdr, 'system': system, 'station': station}


def get_active_route_api_url() -> str:
    api_url = get_api_url().rstrip('/')
    for ingest_path in (
        '/edmc/ingest',
        '/api/edmc/ingest',
        '/api/edmcConnector',
    ):
        if api_url.endswith(ingest_path):
            return f'{api_url[:-len(ingest_path)]}/edmc/active-route'
    parsed = urlparse(api_url)
    return f'{parsed.scheme}://{parsed.netloc}/edmc/active-route'


def cancel_nav_route_state_retry() -> None:
    global nav_route_state_retry_timer, nav_route_state_retry_gen
    nav_route_state_retry_gen += 1
    if nav_route_state_retry_timer is not None:
        nav_route_state_retry_timer.cancel()
        nav_route_state_retry_timer = None


def build_nav_route_payload(route_steps: list) -> Optional[dict]:
    if not route_steps:
        return None

    hops = []
    waypoints = []
    for step in route_steps:
        if not isinstance(step, dict):
            continue
        system_name = step.get('StarSystem')
        if not system_name:
            continue
        star_pos = step.get('StarPos') or [0, 0, 0]
        if not isinstance(star_pos, (list, tuple)) or len(star_pos) < 3:
            star_pos = [0, 0, 0]
        hops.append(str(system_name))
        waypoints.append({
            'systemName': str(system_name),
            'x': float(star_pos[0]),
            'y': float(star_pos[1]),
            'z': float(star_pos[2]),
        })

    if not hops:
        return None

    return {
        'source': 'ingame',
        'from': hops[0],
        'to': hops[-1],
        'jumpRange': 0,
        'routeComplete': True,
        'hops': hops,
        'waypoints': waypoints,
    }


def _apply_route_cleared_state() -> None:
    global active_route, active_route_hop_index
    mark_route_cleared()
    active_route = None
    active_route_hop_index = -1
    refresh_route_status_ui()


def _apply_route_posted_state(payload: dict) -> None:
    global active_route, active_route_hop_index, suppressed_fsd_target
    global suppressed_route_hops, suppress_route_hops_until
    active_route = payload
    active_route_hop_index = -1
    suppressed_fsd_target = None
    suppressed_route_hops = set()
    suppress_route_hops_until = 0.0
    cancel_route_completion_clear()
    apply_system_to_route_progress(current_system)


def _worker_post_route(session, payload: dict) -> None:
    creds = _require_api_credentials()
    if not creds:
        return
    _, api_key = creds
    try:
        response = session.post(
            get_active_route_api_url(),
            json=payload,
            headers=_api_headers(api_key, json_body=True),
            timeout=10,
        )
        if response.status_code == 200:
            _apply_route_posted_state(payload)
            logger.info('Synced in-game nav route to EDSpec')
            return
        logger.warning('Failed to post active route: %s', response.status_code)
    except Exception as exc:
        logger.debug('Failed to post active route: %s', exc)


def _worker_delete_route(session) -> None:
    cancel_route_completion_clear()
    creds = _require_api_credentials()
    if not creds:
        _apply_route_cleared_state()
        return
    _, api_key = creds
    try:
        response = session.delete(
            get_active_route_api_url(),
            headers=_api_headers(api_key),
            timeout=10,
        )
        if response.status_code in (200, 404):
            _apply_route_cleared_state()
            logger.info('Cleared in-game nav route from EDSpec')
        else:
            logger.warning('Failed to delete active route: %s', response.status_code)
    except Exception as exc:
        logger.debug('Failed to delete active route: %s', exc)


def _worker_sync_route(session, route_steps: Optional[list]) -> None:
    payload = build_nav_route_payload(route_steps or [])
    if payload:
        _worker_post_route(session, payload)
    else:
        _worker_delete_route(session)


def _worker_route_state_retry(attempt: int) -> None:
    if not game_session_active or route_clear_pending:
        return
    nav_route = latest_journal_state.get('NavRoute')
    if isinstance(nav_route, dict) and nav_route.get('Route'):
        handle_nav_route_state(nav_route, latest_journal_state)
        return
    schedule_nav_route_state_retry(attempt)


def _system_key(name: Optional[str]) -> str:
    return (name or '').strip().lower()


def star_pos_from_state(state: dict) -> list:
    star_pos = state.get('StarPos')
    if isinstance(star_pos, (list, tuple)) and len(star_pos) >= 3:
        return [float(star_pos[0]), float(star_pos[1]), float(star_pos[2])]
    return [0.0, 0.0, 0.0]


def prepend_current_system(route_steps: list, state: dict) -> list:
    system_name = (state.get('SystemName') or current_system or '').strip()
    if not system_name or not route_steps:
        return route_steps

    first = route_steps[0]
    if isinstance(first, dict):
        first_name = (first.get('StarSystem') or '').strip()
        if _system_key(first_name) == _system_key(system_name):
            return route_steps

    origin = {
        'StarSystem': system_name,
        'SystemAddress': state.get('SystemAddress'),
        'StarPos': star_pos_from_state(state),
    }
    return [origin, *route_steps]


def schedule_nav_route_state_retry(attempt: int = 0) -> None:
    global nav_route_state_retry_timer, nav_route_state_retry_gen
    if attempt >= len(NAV_ROUTE_STATE_RETRY_DELAYS_SEC):
        return

    if nav_route_state_retry_timer is not None:
        nav_route_state_retry_timer.cancel()
        nav_route_state_retry_timer = None

    gen = nav_route_state_retry_gen
    delay = NAV_ROUTE_STATE_RETRY_DELAYS_SEC[attempt]

    def _fire() -> None:
        global nav_route_state_retry_timer
        nav_route_state_retry_timer = None
        if gen != nav_route_state_retry_gen:
            return
        enqueue_job(JOB_ROUTE_STATE_RETRY, attempt + 1)

    nav_route_state_retry_timer = threading.Timer(delay, _fire)
    nav_route_state_retry_timer.daemon = True
    nav_route_state_retry_timer.start()


def build_fsd_target_route_steps(entry: dict, state: dict) -> list:
    target_name = entry.get('Name') or entry.get('StarSystem')
    if not isinstance(target_name, str) or not target_name.strip():
        return []

    current_name = (state.get('SystemName') or current_system or '').strip()
    if not current_name or _system_key(target_name) == _system_key(current_name):
        return []

    return [
        {
            'StarSystem': current_name,
            'SystemAddress': state.get('SystemAddress'),
            'StarPos': star_pos_from_state(state),
        },
        {
            'StarSystem': target_name.strip(),
            'SystemAddress': entry.get('SystemAddress'),
            'StarPos': [0.0, 0.0, 0.0],
        },
    ]


def is_nav_route_clear(nav_route: Optional[dict]) -> bool:
    if not nav_route or not isinstance(nav_route, dict):
        return False
    event = nav_route.get('event')
    return isinstance(event, str) and event.lower() == 'navrouteclear'


def handle_nav_route_state(nav_route: Optional[dict], state: Optional[dict] = None) -> None:
    game_state = state if isinstance(state, dict) else latest_journal_state

    if not nav_route or not isinstance(nav_route, dict):
        return

    if is_nav_route_clear(nav_route):
        if not nav_route_entry_is_current_session(nav_route):
            logger.debug('Ignoring stale NavRouteClear in state NavRoute from a prior session')
            return
        logger.info('NavRoute state is cleared; removing EDSpec route plot')
        enqueue_route_clear()
        return

    if not nav_route_entry_is_current_session(nav_route):
        logger.debug('Ignoring stale NavRoute in state from a prior session')
        return

    route_steps = nav_route.get('Route')
    if route_steps:
        if route_clear_pending:
            return
        global route_trust_state_nav_route
        route_trust_state_nav_route = True
        enqueue_route_sync(route_steps, game_state)


def status_flags(state: Optional[dict]) -> int:
    if not isinstance(state, dict):
        return 0
    try:
        return int(state.get('Flags') or 0)
    except (TypeError, ValueError):
        return 0


def hyperspace_route_hold(state: Optional[dict] = None) -> bool:
    game_state = state if isinstance(state, dict) else latest_journal_state
    flags = status_flags(game_state)
    if flags & (FSD_CHARGING_FLAG | FSD_JUMP_FLAG):
        return True
    return route_jump_hold


def cancel_route_cancel_confirm() -> None:
    global route_cancel_timer, route_clear_pending
    route_clear_pending = False
    if route_cancel_timer is not None:
        route_cancel_timer.cancel()
        route_cancel_timer = None


def schedule_route_cancel_confirm() -> None:
    global route_cancel_timer, route_clear_pending, route_clear_requested_at
    if not route_clear_pending:
        route_clear_requested_at = time.time()
    route_clear_pending = True
    if route_cancel_timer is not None:
        route_cancel_timer.cancel()
        route_cancel_timer = None

    def _fire() -> None:
        global route_cancel_timer
        route_cancel_timer = None
        confirm_route_removed()

    route_cancel_timer = threading.Timer(ROUTE_CANCEL_CONFIRM_SEC, _fire)
    route_cancel_timer.daemon = True
    route_cancel_timer.start()


def confirm_route_removed() -> None:
    """Clear the plot once NavRouteClear sticks. A newer NavRoute event cancels this."""
    global route_clear_pending
    if not route_clear_pending:
        return
    # Only a jump we saw start should delay the delete. Status flags can stay set
    # after the last journal line and would otherwise block the clear forever.
    jump_hold = route_jump_hold or (
        route_jump_released_at and time.time() - route_jump_released_at < ROUTE_CANCEL_CONFIRM_SEC
    )
    if jump_hold and time.time() - route_clear_requested_at < ROUTE_CANCEL_MAX_SEC:
        schedule_route_cancel_confirm()
        return
    if is_route_complete() and not jump_hold:
        route_clear_pending = False
        schedule_route_completion_clear()
        return

    route_clear_pending = False
    logger.info('In-game nav route removed; resetting EDSpec route plot')
    enqueue_route_clear()


def remember_cleared_route_hops() -> None:
    global suppressed_fsd_target, suppressed_route_hops, suppress_route_hops_until
    if not isinstance(active_route, dict):
        return
    hops = active_route.get('hops') or []
    clear_dest = active_route.get('to')
    suppressed_fsd_target = str(clear_dest).strip() if clear_dest else None
    suppressed_route_hops = {_system_key(str(hop)) for hop in hops if hop}
    if suppressed_fsd_target:
        suppressed_route_hops.add(_system_key(suppressed_fsd_target))
    suppress_route_hops_until = time.time() + 15


def request_route_clear_from_game(state: Optional[dict] = None) -> None:
    """Reset the plot when the player removes it. Hyperspace also emits NavRouteClear, then a new NavRoute."""
    global route_trust_state_nav_route
    cancel_route_completion_clear()
    cancel_nav_route_state_retry()
    # Do not let a stale NavRoute still sitting in EDMC state recreate the plot.
    route_trust_state_nav_route = False
    if active_route and is_route_complete() and not hyperspace_route_hold(state):
        logger.info('In-game nav route cleared after arrival; removing EDSpec route plot')
        schedule_route_completion_clear()
        return
    remember_cleared_route_hops()
    if hyperspace_route_hold(state):
        logger.info('NavRouteClear during a jump; route resets if it stays empty')
    else:
        logger.info('NavRouteClear received; route resets if it stays empty')
    schedule_route_cancel_confirm()


def handle_journal_route_event(entry: dict, state: dict) -> None:
    global route_trust_state_nav_route

    if is_nav_route_clear(entry):
        request_route_clear_from_game(state)
        return

    steps = entry.get('Route')
    if isinstance(steps, list) and steps:
        route_trust_state_nav_route = True
        enqueue_route_sync(steps, state)
        return
    if route_clear_pending:
        return
    schedule_nav_route_state_retry(0)


def handle_fsd_target(entry: dict, state: dict) -> None:
    global suppressed_fsd_target

    remaining = entry.get('RemainingJumpsInRoute')
    if isinstance(remaining, int) and remaining > 1:
        return

    if isinstance(remaining, int) and remaining == 1:
        nav_route = state.get('NavRoute')
        if isinstance(nav_route, dict) and nav_route.get('Route'):
            return

    target_name = entry.get('Name') or entry.get('StarSystem')
    if not isinstance(target_name, str) or not target_name.strip():
        if is_ingame_active_route():
            logger.info('FSD target cleared; removing EDSpec route plot')
            enqueue_route_clear()
        return

    if route_clear_pending or fsd_target_is_suppressed(target_name):
        logger.debug('Ignoring suppressed FSD target after route clear: %s', target_name.strip())
        return

    steps = build_fsd_target_route_steps(entry, state)
    if steps:
        suppressed_fsd_target = None
        enqueue_route_sync(steps, state)
        return

    hops = (active_route.get('hops') or []) if isinstance(active_route, dict) else []
    if is_ingame_active_route() and len(hops) <= 2:
        logger.info('FSD target set to current system; removing EDSpec route plot')
        enqueue_route_clear()


def enqueue_route_sync(route_steps: Optional[list], state: Optional[dict] = None) -> None:
    global last_enqueued_route_signature
    if not game_session_active:
        return
    cancel_route_completion_clear()
    cancel_route_cancel_confirm()
    cancel_nav_route_state_retry()
    game_state = state if isinstance(state, dict) else latest_journal_state
    steps = route_steps if route_steps is not None else []
    if steps:
        steps = prepend_current_system(steps, game_state)
    payload = build_nav_route_payload(steps)
    if payload:
        signature = route_sync_signature(payload)
        if signature and signature == last_enqueued_route_signature:
            return
        last_enqueued_route_signature = signature
        enqueue_job(JOB_ROUTE_SET, steps)
    else:
        last_enqueued_route_signature = None
        enqueue_job(JOB_ROUTE_SET, [])


def journal_entry_unix_ts(entry: Optional[dict]) -> Optional[int]:
    if not entry or not isinstance(entry, dict):
        return None
    timestamp = entry.get('timestamp')
    if not isinstance(timestamp, str):
        return None
    try:
        return timegm(strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ'))
    except (ValueError, OverflowError):
        return None


def nav_route_entry_is_current_session(nav_route: dict) -> bool:
    """Ignore NavRoute.json snapshots from before this LoadGame (EDMC PLUGINS.md)."""
    if game_session_start_ts is None:
        return False
    entry_ts = journal_entry_unix_ts(nav_route)
    if entry_ts is None:
        return False
    return entry_ts >= game_session_start_ts - 2


def sync_game_session_start_from_edmc() -> None:
    """Use monitor.started from catch-up LoadGame (plugins do not receive that event)."""
    global game_session_start_ts
    try:
        from monitor import monitor as edmc_monitor
        if edmc_monitor.started is not None:
            game_session_start_ts = edmc_monitor.started
    except ImportError:
        pass


def route_sync_signature(payload: dict) -> str:
    hops = payload.get('hops') or []
    return f"{payload.get('to')}|{'|'.join(hops)}"


def mark_route_cleared() -> None:
    global route_trust_state_nav_route, last_enqueued_route_signature

    remember_cleared_route_hops()
    route_trust_state_nav_route = False
    last_enqueued_route_signature = None
    cancel_nav_route_state_retry()


def is_ingame_active_route() -> bool:
    return isinstance(active_route, dict) and active_route.get('source') == 'ingame'


def fsd_target_is_suppressed(target_name: Optional[str]) -> bool:
    if not target_name:
        return False
    key = _system_key(target_name)
    if suppressed_fsd_target and key == _system_key(suppressed_fsd_target):
        return True
    return time.time() < suppress_route_hops_until and key in suppressed_route_hops


def enqueue_route_clear() -> None:
    mark_route_cleared()
    _apply_route_cleared_state()
    enqueue_job(JOB_ROUTE_CLEAR, None)


def hop_index_for_system(system_name: str) -> int:
    global active_route
    if not active_route or not system_name:
        return -1
    hops = active_route.get('hops') or []
    key = system_name.strip().lower()
    for index in range(len(hops) - 1, -1, -1):
        hop = str(hops[index]).strip().lower()
        if hop == key:
            return index
    return -1


def effective_hop_index() -> int:
    global active_route, active_route_hop_index, current_system
    if not active_route:
        return -1
    hops = active_route.get('hops') or []
    if not hops:
        return -1

    idx = active_route_hop_index
    if not current_system:
        return idx

    match = hop_index_for_system(current_system)
    if match >= 0:
        idx = max(idx, match)

    dest = active_route.get('to') or hops[-1]
    if current_system.strip().lower() == str(dest).strip().lower():
        idx = max(idx, len(hops) - 1)

    return idx


def is_route_complete() -> bool:
    global active_route
    if not active_route:
        return False
    hops = active_route.get('hops') or []
    if not hops:
        return False
    return effective_hop_index() >= len(hops) - 1


def refresh_route_status_ui() -> None:
    schedule_on_main_thread(update_status)


def cancel_route_completion_clear() -> None:
    global route_completion_timer
    if route_completion_timer:
        route_completion_timer.cancel()
        route_completion_timer = None


def schedule_route_completion_clear() -> None:
    global route_completion_timer
    if route_completion_timer is not None:
        return
    route_completion_timer = threading.Timer(ROUTE_COMPLETE_CLEAR_SEC, enqueue_route_clear)
    route_completion_timer.daemon = True
    route_completion_timer.start()


def apply_system_to_route_progress(system_name: str) -> None:
    global active_route_hop_index
    if not active_route or not system_name:
        return

    hops = active_route.get('hops') or []
    match = hop_index_for_system(system_name)
    if match >= 0:
        active_route_hop_index = max(active_route_hop_index, match)

    dest = active_route.get('to') or (hops[-1] if hops else '')
    if dest and system_name.strip().lower() == str(dest).strip().lower() and hops:
        active_route_hop_index = max(active_route_hop_index, len(hops) - 1)

    refresh_route_status_ui()

    if is_route_complete():
        schedule_route_completion_clear()
    else:
        cancel_route_completion_clear()


def route_status_suffix() -> str:
    global active_route
    if not active_route:
        return ''
    hops = active_route.get('hops') or []
    if not hops:
        return ''
    idx = effective_hop_index()
    total_jumps = max(len(hops) - 1, 1)
    completed = max(idx, 0) if idx >= 0 else 0
    destination = active_route.get('to') or hops[-1]
    return f' · {destination} ({completed}/{total_jumps})'


def fuel_percent() -> Optional[int]:
    level = active_fuel_level_cache
    capacity = active_fuel_capacity_cache
    if level is None or capacity is None or capacity <= 0:
        return None
    return min(100, round((level / capacity) * 100))


def fuel_status_suffix() -> str:
    percent = fuel_percent()
    if percent is None:
        return ''
    return f' · {percent}% fuel'


def in_sync_status_suffix() -> str:
    return route_status_suffix() + fuel_status_suffix()

def enqueue_ping(connected: bool = True) -> None:
    enqueue_job(JOB_PING, connected)


def normalize_ship_id(ship_id) -> Optional[int]:
    try:
        value = int(ship_id)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def parse_fuel_level(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_fuel_capacity(raw) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return parse_fuel_level(raw.get('Main'))
    return parse_fuel_level(raw)


def _cached_ship_fuel_level(state: dict, entry: dict) -> Optional[float]:
    ship_id = normalize_ship_id(state.get('ShipID') or entry.get('ShipID'))
    if ship_id is None:
        return None
    return parse_fuel_level(commander_ships_cache.get(ship_id, {}).get('fuelLevel'))


def fuel_from_state_and_entry(state: dict, entry: dict) -> Tuple[Optional[float], Optional[float]]:
    global active_fuel_level_cache, active_fuel_capacity_cache

    event_name = entry.get('event')
    fuel_level = entry.get('FuelLevel')

    if fuel_level is None and event_name in ('Refuel', 'FuelScoop'):
        fuel_level = entry.get('Total')

    if fuel_level is None and event_name == 'RefuelAll':
        amount = parse_fuel_level(entry.get('Amount'))
        if amount is not None:
            current = parse_fuel_level(state.get('FuelLevel'))
            if current is None:
                current = _cached_ship_fuel_level(state, entry)
            if current is None:
                current = active_fuel_level_cache
            if current is not None:
                capacity = parse_fuel_capacity(state.get('FuelCapacity') or entry.get('FuelCapacity'))
                fuel_level = current + amount
                if capacity is not None:
                    fuel_level = min(fuel_level, capacity)

    if fuel_level is None:
        fuel_level = state.get('FuelLevel')

    fuel_capacity = state.get('FuelCapacity')
    if fuel_capacity is None:
        fuel_capacity = entry.get('FuelCapacity')

    level = parse_fuel_level(fuel_level)
    capacity = parse_fuel_capacity(fuel_capacity)

    if level is None:
        level = _cached_ship_fuel_level(state, entry)
    if level is None:
        level = active_fuel_level_cache

    if capacity is None:
        capacity = active_fuel_capacity_cache

    if level is not None:
        active_fuel_level_cache = level
    if capacity is not None:
        active_fuel_capacity_cache = capacity

    return level, capacity


def active_ship_from_state_and_entry(state: dict, entry: dict) -> Tuple[Optional[int], str, Optional[str], Optional[str]]:
    ship_id = normalize_ship_id(state.get('ShipID') or entry.get('ShipID'))
    ship_type = state.get('ShipType') or entry.get('Ship') or entry.get('ShipType') or 'unknown'
    ship_name = state.get('ShipName') or entry.get('ShipName')
    ship_ident = state.get('ShipIdent') or entry.get('ShipIdent')
    return ship_id, str(ship_type), ship_name, ship_ident


def credits_from_state_and_entry(state: dict, entry: dict) -> Optional[int]:
    raw = entry.get('Credits')
    if raw is None:
        raw = state.get('Credits')
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def attach_credits_payload(data: dict, state: dict, entry: dict) -> None:
    credits = credits_from_state_and_entry(state, entry)
    if credits is not None:
        data['credits'] = credits


def merc_coins_from_statistics(entry: dict) -> Optional[int]:
    bank = entry.get('Bank_Account')
    if not isinstance(bank, dict):
        return None
    raw = bank.get('MercCoins_Current')
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def attach_merc_coins_payload(data: dict) -> None:
    if merc_coins_cache is not None:
        data['mercCoins'] = merc_coins_cache


def ship_record_from_stored(
    ship: dict,
    *,
    star_system: Optional[str] = None,
    in_transit: bool = False,
) -> Optional[dict]:
    ship_id = normalize_ship_id(ship.get('ShipID'))
    if ship_id is None:
        return None

    record = {
        'shipId': ship_id,
        'shipType': ship.get('ShipType') or 'unknown',
        'name': ship.get('Name'),
        'value': ship.get('Value'),
        'hot': bool(ship.get('Hot')),
        'inTransit': in_transit,
        'starSystem': star_system or ship.get('StarSystem'),
    }
    ship_ident = ship.get('ShipIdent') or ship.get('UserShipId')
    if isinstance(ship_ident, str) and ship_ident.strip():
        record['ident'] = ship_ident.strip()
    return record


def _merge_ship_record(ship: dict) -> Optional[tuple]:
    if not isinstance(ship, dict):
        return None
    ship_id = normalize_ship_id(ship.get('shipId'))
    if ship_id is None:
        return None
    previous = commander_ships_cache.get(ship_id, {})
    return ship_id, {**previous, **ship, 'shipId': ship_id}


def merge_ships_into_cache(ships: list) -> None:
    global commander_ships_cache
    for ship in ships:
        merged = _merge_ship_record(ship)
        if merged is None:
            continue
        ship_id, record = merged
        commander_ships_cache[ship_id] = record


def replace_ships_cache(ships: list) -> None:
    """Replace the cache with this inventory. Keep stored details for ships that remain."""
    global commander_ships_cache
    incoming = {}
    for ship in ships:
        merged = _merge_ship_record(ship)
        if merged is None:
            continue
        ship_id, record = merged
        incoming[ship_id] = record
    commander_ships_cache = incoming


def prune_ships_cache(keep_ids: set) -> None:
    global commander_ships_cache
    commander_ships_cache = {
        ship_id: ship
        for ship_id, ship in commander_ships_cache.items()
        if ship_id in keep_ids
    }


def remove_ship_from_cache(ship_id: int) -> None:
    commander_ships_cache.pop(ship_id, None)


def build_ships_payload(
    *,
    active_ship_id: Optional[int] = None,
    fuel_level: Optional[float] = None,
    fuel_capacity: Optional[float] = None,
) -> dict:
    global active_ship_id_cache
    if active_ship_id is not None:
        active_ship_id_cache = active_ship_id
    active_id = active_ship_id if active_ship_id is not None else active_ship_id_cache

    ships = []
    for ship_id, ship in commander_ships_cache.items():
        entry = dict(ship)
        entry['active'] = active_id is not None and ship_id == active_id
        if entry['active']:
            if fuel_level is not None:
                entry['fuelLevel'] = fuel_level
            if fuel_capacity is not None:
                entry['fuelCapacity'] = fuel_capacity
            if cargo_cache is not None:
                entry['cargo'] = cargo_cache
                entry['cargoCount'] = cargo_count_cache
        ships.append(entry)

    payload: dict = {'ships': ships}
    if active_id is not None:
        payload['activeShipId'] = active_id
    if fuel_level is not None:
        payload['fuelLevel'] = fuel_level
    if fuel_capacity is not None:
        payload['fuelCapacity'] = fuel_capacity
    return payload


def handle_stored_ships_event(entry: dict, system: str, active_ship_id: Optional[int] = None) -> bool:
    """StoredShips lists every ship in storage. The ship being flown is kept too."""
    ships_here = entry.get('ShipsHere')
    ships_remote = entry.get('ShipsRemote')
    if not isinstance(ships_here, list) and not isinstance(ships_remote, list):
        return False

    star_system = entry.get('StarSystem') or system
    batch = []

    for ship in ships_here or []:
        if isinstance(ship, dict):
            record = ship_record_from_stored(ship, star_system=star_system, in_transit=False)
            if record:
                batch.append(record)

    for ship in ships_remote or []:
        if not isinstance(ship, dict):
            continue
        in_transit = bool(ship.get('InTransit'))
        record = ship_record_from_stored(
            ship,
            star_system=ship.get('StarSystem'),
            in_transit=in_transit,
        )
        if record:
            batch.append(record)

    keep_ids = {record['shipId'] for record in batch}
    active_id = active_ship_id if active_ship_id is not None else active_ship_id_cache
    if active_id is not None:
        keep_ids.add(active_id)
    if not keep_ids:
        return False

    if batch:
        merge_ships_into_cache(batch)
    prune_ships_cache(keep_ids)
    return True


def normalize_engineering(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None

    modifications = []
    for mod in raw.get('Modifications') or []:
        if not isinstance(mod, dict):
            continue
        modifications.append({
            'label': mod.get('Label'),
            'value': mod.get('Value'),
            'originalValue': mod.get('OriginalValue'),
            'lessIsGood': mod.get('LessIsGood'),
        })

    engineering = {
        'engineer': raw.get('Engineer'),
        'blueprintName': raw.get('BlueprintName'),
        'level': raw.get('Level'),
        'quality': raw.get('Quality'),
        'experimentalEffect': raw.get('ExperimentalEffect'),
        'modifications': modifications,
    }
    return engineering if any(v is not None for v in engineering.values()) else None


def normalize_module(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None

    module = {
        'slot': raw.get('Slot'),
        'item': raw.get('Item'),
        'on': raw.get('On'),
        'priority': raw.get('Priority'),
        'health': raw.get('Health'),
        'value': raw.get('Value'),
    }
    if raw.get('AmmoInClip') is not None:
        module['ammoInClip'] = raw.get('AmmoInClip')
    if raw.get('AmmoInHopper') is not None:
        module['ammoInHopper'] = raw.get('AmmoInHopper')

    engineering = normalize_engineering(raw.get('Engineering'))
    if engineering:
        module['engineering'] = engineering

    return module if module.get('slot') or module.get('item') else None


def loadout_from_entry(entry: dict) -> Optional[dict]:
    if entry.get('event') != 'Loadout':
        return None

    modules = []
    for raw in entry.get('Modules') or []:
        module = normalize_module(raw)
        if module:
            modules.append(module)

    fuel_capacity = entry.get('FuelCapacity')
    if not isinstance(fuel_capacity, dict):
        fuel_capacity = None

    loadout = {
        'hullHealth': entry.get('HullHealth'),
        'hullValue': entry.get('HullValue'),
        'modulesValue': entry.get('ModulesValue'),
        'unladenMass': entry.get('UnladenMass'),
        'cargoCapacity': entry.get('CargoCapacity'),
        'maxJumpRange': entry.get('MaxJumpRange'),
        'rebuy': entry.get('Rebuy'),
        'fuelCapacity': fuel_capacity,
        'modules': modules,
    }

    if not modules and all(v is None for k, v in loadout.items() if k != 'modules'):
        return None
    return loadout


def normalize_cargo_item(raw) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None

    name = raw.get('Name') or raw.get('name')
    if not name:
        return None

    try:
        count = int(raw.get('Count', raw.get('count', 0)))
    except (TypeError, ValueError):
        count = 0
    if count <= 0:
        return None

    item = {
        'name': str(name),
        'count': count,
        'stolen': bool(raw.get('Stolen') or raw.get('stolen')),
    }

    localised = raw.get('Name_Localised') or raw.get('nameLocalised')
    if localised:
        item['nameLocalised'] = str(localised)

    mission_id = raw.get('MissionID', raw.get('missionId'))
    if mission_id is not None:
        try:
            item['missionId'] = int(mission_id)
        except (TypeError, ValueError):
            pass

    return item


def cargo_from_entry_and_state(entry: dict, state: dict) -> dict:
    vessel = entry.get('Vessel') or cargo_vessel_cache or 'Ship'
    inventory = entry.get('Inventory')
    if inventory is None:
        inventory = state.get('Cargo')

    items = []
    total = 0
    if isinstance(inventory, list):
        for raw in inventory:
            item = normalize_cargo_item(raw)
            if item:
                items.append(item)
                total += item['count']

    return {
        'vessel': vessel,
        'count': total,
        'items': items,
    }


def update_cargo_cache(entry: dict, state: dict) -> None:
    global cargo_cache, cargo_count_cache, cargo_vessel_cache
    cargo = cargo_from_entry_and_state(entry, state)
    cargo_cache = cargo['items']
    cargo_count_cache = cargo['count']
    cargo_vessel_cache = cargo['vessel']


def build_cargo_payload() -> dict:
    return {
        'cargoVessel': cargo_vessel_cache,
        'cargoCount': cargo_count_cache,
        'cargo': cargo_cache,
    }


def attach_cargo_payload(data: dict) -> None:
    data.update(build_cargo_payload())


def enqueue_fuel_update(
    cmdr: Optional[str],
    system: Optional[str],
    station: Optional[str],
    state: dict,
    entry: dict,
) -> None:
    if not work_queue:
        return

    fuel_level, fuel_capacity = fuel_from_state_and_entry(state, entry)
    if fuel_level is None:
        return

    ship_id, ship_type, ship_name, ship_ident = active_ship_from_state_and_entry(state, entry)
    if ship_id is not None:
        merge_ships_into_cache([{
            'shipId': ship_id,
            'shipType': ship_type,
            'name': ship_name,
            'ident': ship_ident,
            'fuelLevel': fuel_level,
            'fuelCapacity': fuel_capacity,
            'active': True,
        }])

    payload = location_payload(cmdr, system, station)
    payload['fuelLevel'] = fuel_level
    if fuel_capacity is not None:
        payload['fuelCapacity'] = fuel_capacity

    if send_ship_info_enabled():
        payload.update(build_ships_payload(
            active_ship_id=ship_id,
            fuel_level=fuel_level,
            fuel_capacity=fuel_capacity,
        ))
        attach_cargo_payload(payload)
        attach_credits_payload(payload, state, entry)

    enqueue_commander(payload)
    schedule_on_main_thread(update_status)


def cancel_live_fuel_timer() -> None:
    global live_fuel_timer
    if live_fuel_timer is not None:
        live_fuel_timer.cancel()
        live_fuel_timer = None


def fuel_main_from_status(entry: dict) -> Optional[float]:
    fuel = entry.get('Fuel')
    if isinstance(fuel, dict):
        return parse_fuel_level(fuel.get('FuelMain'))
    return None


def send_live_fuel(level: float) -> None:
    global last_live_fuel_sent_at, last_live_fuel_level, live_fuel_pending
    last_live_fuel_sent_at = time.time()
    last_live_fuel_level = level
    live_fuel_pending = None
    state = latest_journal_state if isinstance(latest_journal_state, dict) else {}
    enqueue_fuel_update(
        current_cmdr,
        current_system or None,
        current_station or None,
        state,
        {'event': 'Status', 'FuelLevel': level},
    )


def flush_live_fuel() -> None:
    global live_fuel_timer
    live_fuel_timer = None
    level = live_fuel_pending
    if level is None:
        return
    if last_live_fuel_level is not None and abs(level - last_live_fuel_level) < 0.01:
        return
    send_live_fuel(level)


def schedule_live_fuel(level: float) -> None:
    global live_fuel_timer, live_fuel_pending
    compared = last_live_fuel_level if last_live_fuel_level is not None else active_fuel_level_cache
    if compared is not None and abs(level - compared) < 0.01:
        return

    wait = FUEL_LIVE_INTERVAL_SEC - (time.time() - last_live_fuel_sent_at)
    if wait <= 0:
        cancel_live_fuel_timer()
        send_live_fuel(level)
        return

    live_fuel_pending = level
    if live_fuel_timer is not None:
        return
    live_fuel_timer = threading.Timer(wait, flush_live_fuel)
    live_fuel_timer.daemon = True
    live_fuel_timer.start()


def enqueue_ship_cache(
    cmdr: Optional[str],
    system: Optional[str],
    station: Optional[str],
    state: dict,
    entry: dict,
    *,
    active_ship_id: Optional[int] = None,
) -> None:
    if not send_ship_info_enabled() or not work_queue or not commander_ships_cache:
        return
    payload = location_payload(cmdr, system, station)
    attach_credits_payload(payload, state, entry)
    payload.update(build_ships_payload(active_ship_id=active_ship_id))
    enqueue_commander(payload)


def _loadout_modules(ship: dict) -> list:
    loadout = ship.get('loadout')
    if not isinstance(loadout, dict):
        loadout = {}
        ship['loadout'] = loadout
    modules = loadout.get('modules')
    if not isinstance(modules, list):
        modules = []
        loadout['modules'] = modules
    return modules


def _find_module(modules: list, slot: Optional[str]):
    if not slot:
        return None
    for module in modules:
        if isinstance(module, dict) and module.get('slot') == slot:
            return module
    return None


def apply_module_event(entry: dict) -> bool:
    """Update the fitted module. Jump range, hull, and rebuy stay until the next Loadout."""
    ship_id = normalize_ship_id(entry.get('ShipID'))
    if ship_id is None:
        return False

    ship = commander_ships_cache.get(ship_id)
    if ship is None:
        ship = {
            'shipId': ship_id,
            'shipType': entry.get('Ship') or entry.get('ShipType') or 'unknown',
        }
        commander_ships_cache[ship_id] = ship

    modules = _loadout_modules(ship)
    slot = entry.get('Slot')
    event_name = entry.get('event')

    if event_name == 'ModuleSell':
        ship['loadout']['modules'] = [
            module for module in modules
            if not (isinstance(module, dict) and slot and module.get('slot') == slot)
        ]
        return True

    module = _find_module(modules, slot)
    if module is None:
        module = {'slot': slot}
        modules.append(module)

    if event_name == 'ModuleBuy':
        module['item'] = entry.get('BuyItem')
        module.pop('engineering', None)
    elif event_name == 'EngineerCraft':
        if entry.get('Module'):
            module['item'] = entry.get('Module')
        module['engineering'] = {
            'engineer': entry.get('Engineer'),
            'blueprintName': entry.get('BlueprintName'),
            'level': entry.get('Level'),
            'quality': entry.get('Quality'),
            'experimentalEffect': entry.get('ApplyExperimentalEffect'),
        }
    return True


def handle_shipyard_buy(entry: dict, system: Optional[str], state: dict) -> Optional[int]:
    stored_id = normalize_ship_id(entry.get('StoreShipID'))
    new_id = normalize_ship_id(entry.get('NewShipID') or entry.get('ShipID'))
    if new_id is None:
        state_id = normalize_ship_id(state.get('ShipID'))
        if state_id is not None and state_id != stored_id:
            new_id = state_id

    records = []
    if stored_id is not None:
        records.append({
            'shipId': stored_id,
            'shipType': entry.get('StoreOldShip') or 'unknown',
            'starSystem': system,
            'inTransit': False,
            'active': False,
        })
    if new_id is not None:
        records.append({
            'shipId': new_id,
            'shipType': entry.get('ShipType') or state.get('ShipType') or 'unknown',
            'value': entry.get('ShipPrice'),
            'starSystem': system,
            'inTransit': False,
            'active': True,
        })
    if records:
        merge_ships_into_cache(records)
    return new_id


def handle_shipyard_swap(entry: dict, system: Optional[str]) -> Optional[int]:
    new_id = normalize_ship_id(entry.get('ShipID'))
    stored_id = normalize_ship_id(entry.get('StoreShipID'))
    records = []
    if stored_id is not None:
        records.append({
            'shipId': stored_id,
            'shipType': entry.get('StoreOldShip') or 'unknown',
            'starSystem': system,
            'inTransit': False,
            'active': False,
        })
    if new_id is not None:
        records.append({
            'shipId': new_id,
            'shipType': entry.get('ShipType') or 'unknown',
            'starSystem': system,
            'inTransit': False,
            'active': True,
        })
    if records:
        merge_ships_into_cache(records)
    return new_id


def handle_shipyard_transfer(entry: dict, system: Optional[str]) -> Optional[int]:
    ship_id = normalize_ship_id(entry.get('ShipID'))
    if ship_id is None:
        return None
    destination = system or entry.get('System')
    merge_ships_into_cache([{
        'shipId': ship_id,
        'shipType': entry.get('ShipType') or 'unknown',
        'starSystem': destination,
        'inTransit': True,
        'active': False,
    }])
    return ship_id


def handle_set_user_ship_name(entry: dict) -> Optional[int]:
    ship_id = normalize_ship_id(entry.get('ShipID'))
    if ship_id is None:
        return None
    merge_ships_into_cache([{
        'shipId': ship_id,
        'shipType': entry.get('Ship') or 'unknown',
        'name': entry.get('UserShipName'),
        'ident': entry.get('UserShipId'),
    }])
    return ship_id


def attach_ship_payload(data: dict, state: dict, entry: dict, star_system: Optional[str] = None) -> None:
    ship_id, ship_type, ship_name, ship_ident = active_ship_from_state_and_entry(state, entry)
    fuel_level, fuel_capacity = fuel_from_state_and_entry(state, entry)
    loadout = loadout_from_entry(entry)

    if ship_id is not None:
        ship_record = {
            'shipId': ship_id,
            'shipType': ship_type,
            'name': ship_name,
            'ident': ship_ident,
            'fuelLevel': fuel_level,
            'fuelCapacity': fuel_capacity,
            'active': True,
        }
        if star_system:
            ship_record['starSystem'] = star_system
            ship_record['inTransit'] = False
        if loadout:
            ship_record['loadout'] = loadout
        merge_ships_into_cache([ship_record])

    data.update(build_ships_payload(
        active_ship_id=ship_id,
        fuel_level=fuel_level,
        fuel_capacity=fuel_capacity,
    ))
    attach_cargo_payload(data)


def _api_headers(api_key: str, *, json_body: bool = False) -> dict:
    headers = {
        'Authorization': f'Bearer {api_key}',
        'User-Agent': user_agent(),
    }
    if json_body:
        headers['Content-Type'] = 'application/json'
    return headers


def _require_api_credentials() -> Optional[Tuple[str, str]]:
    if not is_plugin_enabled():
        return None
    api_key = get_api_key()
    if not api_key:
        return None
    return get_api_url(), api_key


def plugin_start3(plugin_dir: str) -> str:
    """
    Initialize the plugin when EDMarketConnector starts.
    
    Args:
        plugin_dir: The directory containing this plugin
        
    Returns:
        The plugin's internal name
    """
    global work_queue, worker_thread, ping_thread, update_check_thread
    global stop_event, ping_event, update_check_performed
    
    logger.info('EDSpec plugin starting from %s', plugin_dir)
    
    work_queue = queue.Queue()
    update_check_performed = False
    
    stop_event.clear()
    ping_event.clear()
    worker_thread = threading.Thread(target=worker_thread_loop, daemon=True)
    worker_thread.start()
    
    ping_thread = threading.Thread(target=ping_thread_loop, daemon=True)
    ping_thread.start()
    
    update_check_event.clear()
    update_check_thread = threading.Thread(target=check_for_updates_delayed, daemon=True)
    update_check_thread.start()
    
    logger.info('EDSpec plugin started successfully')
    return 'EDSpec'


def plugin_stop() -> None:
    """
    Cleanup when EDMarketConnector shuts down.
    """
    global stop_event, ping_event, worker_thread, ping_thread, update_check_thread
    
    logger.info('EDSpec plugin stopping')

    cancel_nav_route_state_retry()
    cancel_route_completion_clear()
    cancel_live_fuel_timer()
    
    # Send disconnect message before shutting down
    send_disconnect_message()
    
    # Signal threads to stop
    stop_event.set()
    ping_event.set()
    update_check_event.set()
    
    # Wait for threads to finish (max 5 seconds each)
    if worker_thread:
        worker_thread.join(timeout=5)
        if worker_thread.is_alive():
            logger.warning('Worker thread did not stop within timeout')
    
    if ping_thread:
        ping_thread.join(timeout=5)
        if ping_thread.is_alive():
            logger.warning('Ping thread did not stop within timeout')
    
    if update_check_thread:
        update_check_thread.join(timeout=5)
        if update_check_thread.is_alive():
            logger.warning('Update check thread did not stop within timeout')
    
    logger.info('EDSpec plugin stopped')


def plugin_app(parent: tk.Frame) -> Tuple[tk.Label, tk.Label]:
    """
    Create UI widgets for the main EDMarketConnector window.
    
    Args:
        parent: The parent frame
        
    Returns:
        Tuple of (label_widget, status_widget)
    """
    global status_label
    
    label = tk.Label(parent, text='EDSpec:')
    status_label = tk.Label(parent, text='Not configured', foreground='gray')
    
    # Update status based on configuration
    update_status()
    
    return label, status_label


def edmc_api_root() -> str:
    url = get_api_url().rstrip('/')
    suffix = '/edmc/ingest'
    if url.endswith(suffix):
        return url[:-len(suffix)]
    parsed = urlparse(url)
    return f'{parsed.scheme}://{parsed.netloc}'


def fit_photo(image: tk.PhotoImage, size: int) -> tk.PhotoImage:
    edge = max(image.width(), image.height(), 1)
    factor = edge // size
    if factor > 1:
        return image.subsample(factor, factor)
    return image


def photo_from_b64(data: str, size: int) -> Optional[tk.PhotoImage]:
    if not data:
        return None
    try:
        return fit_photo(tk.PhotoImage(data=data), size)
    except tk.TclError:
        logger.debug('Could not read an embedded logo')
        return None


def photo_from_bytes(raw: bytes, size: int) -> Optional[tk.PhotoImage]:
    return photo_from_b64(base64.b64encode(raw).decode('ascii'), size)


def _api_get(session, path: str):
    return session.get(f'{edmc_api_root()}{path}', timeout=8)


def fetch_season_brand(session) -> Optional[dict]:
    try:
        response = _api_get(session, '/season')
        if getattr(response, 'status_code', 0) != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        logger.debug('Could not load the season from the API')
        return None


def png_bytes(raw: bytes) -> Optional[bytes]:
    if raw.startswith(b'\x89PNG'):
        return raw
    return None


def fetch_api_png(session, path: str) -> Optional[bytes]:
    try:
        response = _api_get(session, path)
        raw = getattr(response, 'content', b'') or b''
        if getattr(response, 'status_code', 0) == 200:
            return png_bytes(raw)
    except Exception:
        logger.debug('Could not download the season image')
    return None


def logo_bytes_from_brand(brand: Optional[dict]) -> Optional[bytes]:
    if not isinstance(brand, dict):
        return None
    encoded = brand.get('logo')
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        return png_bytes(base64.b64decode(encoded, validate=True))
    except Exception:
        logger.debug('Could not read the season logo from the API')
        return None


def fetch_png_url(session, url: Optional[str]) -> Optional[bytes]:
    if not isinstance(url, str) or not url.startswith('https://'):
        return None
    try:
        response = session.get(
            url,
            timeout=8,
            headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'image/png,image/*,*/*'},
        )
        raw = getattr(response, 'content', b'') or b''
        if getattr(response, 'status_code', 0) == 200:
            return png_bytes(raw)
    except Exception:
        logger.debug('Could not download the season image')
    return None


def refresh_season_images(
    logo_label: tk.Label,
    greeting: tk.Frame,
    greeting_label: tk.Label,
    pumpkin_label: tk.Label,
) -> None:
    def work() -> None:
        try:
            session = timeout_session.new_session()
        except Exception:
            logger.debug('Could not open a session for the season logo')
            return
        brand = fetch_season_brand(session)
        logo_raw = logo_bytes_from_brand(brand) or fetch_api_png(session, '/season/logo')
        pumpkin_raw = None
        if isinstance(brand, dict) and brand.get('season') == 'halloween':
            pumpkin_raw = fetch_png_url(session, 'https://edspecbot.com/pumpkin.png')

        def apply() -> None:
            try:
                if logo_raw:
                    photo = photo_from_bytes(logo_raw, 48)
                    if photo is not None:
                        logo_label.configure(image=photo)
                        logo_label.image = photo
                greeting_text = brand.get('message') if isinstance(brand, dict) else None
                if isinstance(greeting_text, str) and greeting_text.strip():
                    greeting_label.configure(text=greeting_text.strip())
                    if pumpkin_raw:
                        photo = photo_from_bytes(pumpkin_raw, 20)
                        if photo is not None:
                            pumpkin_label.configure(image=photo)
                            pumpkin_label.image = photo
                            pumpkin_label.pack(side=tk.LEFT, padx=(6, 0))
                    if not greeting.winfo_ismapped():
                        greeting.pack(anchor=tk.W, pady=(2, 0))
            except tk.TclError:
                return

        schedule_on_main_thread(apply)

    threading.Thread(target=work, daemon=True).start()


def plugin_prefs(parent: nb.Notebook, cmdr: str | None, is_beta: bool) -> nb.Frame:
    """
    Create the preferences panel in EDMarketConnector settings.
    
    Args:
        parent: The parent notebook widget
        cmdr: Current commander name
        is_beta: Whether this is a beta game
        
    Returns:
        Frame containing the preferences UI
    """
    global prefs_frame
    
    logger.debug('plugin_prefs called: cmdr=%s, is_beta=%s', cmdr, is_beta)
    
    try:
        frame = nb.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
    except Exception:
        logger.exception('Error creating preferences frame')
        frame = nb.Frame(parent)
        nb.Label(frame, text='EDSpec settings could not be opened. Please try again.').pack()
        return frame
    
    try:
        shell = tk.Frame(frame, bg=ED_BG)
        shell.grid(row=0, column=0, sticky=tk.NW, padx=6, pady=6)
        brackets = tk.Canvas(shell, bg=ED_BG, highlightthickness=0, bd=0)
        brackets.place(x=0, y=0, relwidth=1, relheight=1)
        body = tk.Frame(shell, bg=ED_BG)
        body.pack(padx=18, pady=16)
        width_lock = tk.Frame(body, width=456, height=1, bg=ED_BG, bd=0, highlightthickness=0)
        width_lock.pack(fill=tk.X)
        width_lock.pack_propagate(False)

        def draw_brackets(_event=None) -> None:
            brackets.delete('frame')
            width = shell.winfo_width()
            height = shell.winfo_height()
            if width < 8 or height < 8:
                return
            arm = 11
            x0, y0, x1, y1 = 1, 1, width - 2, height - 2
            for coords in (
                (x0, y0, x0 + arm, y0),
                (x0, y0, x0, y0 + arm),
                (x1, y0, x1 - arm, y0),
                (x1, y0, x1, y0 + arm),
                (x0, y1, x0 + arm, y1),
                (x0, y1, x0, y1 - arm),
                (x1, y1, x1 - arm, y1),
                (x1, y1, x1, y1 - arm),
            ):
                brackets.create_line(*coords, fill=ED_LINE, tags='frame')

        shell.bind('<Configure>', draw_brackets)

        def option_row(text: str, variable: tk.BooleanVar, note: Optional[str] = None) -> None:
            holder = tk.Frame(body, bg=ED_BG, cursor='hand2')
            holder.pack(fill=tk.X, pady=(10, 0))
            mark = tk.Canvas(holder, width=12, height=12, bg=ED_BG, highlightthickness=0, bd=0, cursor='hand2')
            mark.pack(side=tk.LEFT, padx=(0, 8), pady=(2, 0))
            words = tk.Frame(holder, bg=ED_BG, cursor='hand2')
            words.pack(side=tk.LEFT, fill=tk.X)
            caption = tk.Label(
                words,
                text=text,
                bg=ED_BG,
                fg=ED_TEXT,
                font=ED_FONT,
                anchor=tk.W,
                justify=tk.LEFT,
                wraplength=430,
                cursor='hand2',
            )
            caption.pack(anchor=tk.W)
            note_label = None
            if note:
                note_label = tk.Label(
                    words,
                    text=note,
                    bg=ED_BG,
                    fg=ED_MUTED,
                    font=('Segoe UI', 8),
                    anchor=tk.W,
                    justify=tk.LEFT,
                    wraplength=430,
                    cursor='hand2',
                )
                note_label.pack(anchor=tk.W, pady=(1, 0))

            def paint() -> None:
                mark.delete('box')
                mark.create_rectangle(0, 0, 12, 12, outline=ED_BG, fill=ED_BG, tags='box')
                if variable.get():
                    mark.create_rectangle(1, 1, 11, 11, outline=ED_AMBER, fill=ED_AMBER, tags='box')
                else:
                    mark.create_rectangle(1, 1, 11, 11, outline=ED_MUTED, fill=ED_BG, tags='box')

            def toggle(_event=None) -> None:
                variable.set(not variable.get())

            for widget in (holder, mark, words, caption, note_label):
                if widget is not None:
                    widget.bind('<Button-1>', toggle)
            variable.trace_add('write', lambda *_args: paint())
            paint()

        def field_block(
            caption: str,
            variable: tk.StringVar,
            show: Optional[str] = None,
            link_text: Optional[str] = None,
            link_url: Optional[str] = None,
        ) -> None:
            block = tk.Frame(body, bg=ED_BG)
            block.pack(fill=tk.X, pady=(14, 0))
            heading = tk.Frame(block, bg=ED_BG)
            heading.pack(fill=tk.X)
            tk.Label(heading, text=caption, bg=ED_BG, fg=ED_MUTED, font=('Segoe UI', 8)).pack(side=tk.LEFT)
            if link_text and link_url:
                link = tk.Label(
                    heading,
                    text=link_text,
                    bg=ED_BG,
                    fg=ED_AMBER,
                    font=('Segoe UI', 8),
                    cursor='hand2',
                )
                link.pack(side=tk.RIGHT)
                link.bind('<Button-1>', lambda _event: webbrowser.open(link_url))
                link.bind('<Enter>', lambda _event: link.config(fg=ED_AMBER_HOVER))
                link.bind('<Leave>', lambda _event: link.config(fg=ED_AMBER))
            tk.Entry(
                block,
                textvariable=variable,
                show=show,
                width=48,
                bg=ED_FIELD,
                fg=ED_TEXT,
                insertbackground=ED_AMBER,
                relief=tk.FLAT,
                highlightthickness=1,
                highlightbackground=ED_LINE,
                highlightcolor=ED_AMBER,
                font=ED_FONT,
            ).pack(fill=tk.X, pady=(3, 0), ipady=4)

        head = tk.Frame(body, bg=ED_BG)
        head.pack(fill=tk.X)
        tk.Label(head, text=VERSION, bg=ED_BG, fg=ED_MUTED, font=('Segoe UI', 8)).pack(side=tk.RIGHT, pady=(14, 0))
        logo_label = tk.Label(head, bg=ED_BG, bd=0, highlightthickness=0)
        logo_label.pack(side=tk.LEFT, padx=(0, 10))
        names = tk.Frame(head, bg=ED_BG)
        names.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tk.Label(names, text='EDSpec', bg=ED_BG, fg=ED_AMBER, font=('Segoe UI', 12)).pack(anchor=tk.W)
        greeting = tk.Frame(names, bg=ED_BG)
        greeting_label = tk.Label(greeting, text='', bg=ED_BG, fg=ED_AMBER, font=('Segoe UI', 8))
        greeting_label.pack(side=tk.LEFT)
        pumpkin_label = tk.Label(greeting, bg=ED_BG, bd=0, highlightthickness=0)
        refresh_season_images(logo_label, greeting, greeting_label, pumpkin_label)
        rule = tk.Frame(body, bg=ED_LINE, height=1, bd=0, highlightthickness=0)
        rule.pack(fill=tk.X, pady=(8, 8))
        rule.pack_propagate(False)
        tk.Label(
            body,
            text='Commander location for the Discord bot and the galaxy map.',
            bg=ED_BG,
            fg=ED_TEXT,
            font=ED_FONT,
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=430,
        ).pack(anchor=tk.W)

        enabled_var = tk.BooleanVar(value=is_plugin_enabled())
        option_row('Enabled', enabled_var)

        send_ship_info_var = tk.BooleanVar(value=send_ship_info_enabled())
        option_row(
            'Share ship, credits, Merc coins, cargo and fleet',
            send_ship_info_var,
            'System, station and fuel are always sent.',
        )
        
        check_updates_var = tk.BooleanVar(value=check_updates_enabled())
        option_row('Check for updates', check_updates_var)

        api_url_var = tk.StringVar(
            value=(config.get_str(API_URL_SETTING) or '').strip() or DEFAULT_API_URL,
        )
        api_key_var = tk.StringVar(value=config.get_str(API_KEY_SETTING) or '')
        field_block('API URL', api_url_var)
        field_block(
            'API key',
            api_key_var,
            show='*',
            link_text='Get an API key here',
            link_url='https://edspecbot.com',
        )

        actions = tk.Frame(body, bg=ED_BG)
        actions.pack(fill=tk.X, pady=(16, 2))
        test_button_cooldown_active = {'value': False}
        test_button = tk.Label(
            actions,
            text='Test connection',
            bg=ED_BG,
            fg=ED_AMBER,
            font=ED_FONT,
            padx=10,
            pady=3,
            highlightthickness=1,
            highlightbackground=ED_AMBER,
            highlightcolor=ED_AMBER,
            cursor='hand2',
        )
        test_button.pack(side=tk.LEFT)
        test_result_var = tk.StringVar(value='')
        tk.Label(
            actions,
            textvariable=test_result_var,
            bg=ED_BG,
            fg=ED_MUTED,
            font=ED_FONT,
            anchor=tk.W,
            wraplength=280,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(12, 0))
        tk.Label(
            body,
            text='Route lines, fuel warnings and map audio are set on the galaxy map.',
            bg=ED_BG,
            fg=ED_MUTED,
            font=('Segoe UI', 8),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=430,
        ).pack(anchor=tk.W, pady=(14, 0))
        
        def test_connection():
            """Test the connection to the EDSpec API"""
            # Check if cooldown is active
            if test_button_cooldown_active['value']:
                return
            
            test_button_cooldown_active['value'] = True
            test_button.config(fg=ED_MUTED, highlightbackground=ED_LINE, highlightcolor=ED_LINE, cursor='arrow')
            
            def reenable_button():
                test_button_cooldown_active['value'] = False
                test_button.config(
                    fg=ED_AMBER,
                    highlightbackground=ED_AMBER,
                    highlightcolor=ED_AMBER,
                    cursor='hand2',
                )
            
            test_result_var.set('Testing connection...')
            frame.update_idletasks()
            
            # Save the current API key to config before testing
            if prefs_frame and hasattr(prefs_frame, 'api_key_var'):
                config.set(API_KEY_SETTING, prefs_frame.api_key_var.get())
            if prefs_frame and hasattr(prefs_frame, 'api_url_var'):
                config.set(API_URL_SETTING, prefs_frame.api_url_var.get().strip())
            
            # Get the current value from the UI
            current_api_key = api_key_var.get()
            current_api_url = api_url_var.get().strip() or DEFAULT_API_URL
            
            def set_test_result(message: str) -> None:
                def apply() -> None:
                    test_result_var.set(message)
                    reenable_button()

                schedule_on_main_thread(apply)
            
            def do_test():
                try:
                    api_url = current_api_url
                    api_key = current_api_key
                    
                    if not api_key:
                        set_test_result('No API key configured')
                        return
                    
                    session = timeout_session.new_session()
                    response = session.post(
                        api_url,
                        json={'connected': True, 'test': True},
                        headers=_api_headers(api_key, json_body=True),
                        timeout=10,
                    )
                    
                    if response.status_code == 200:
                        set_test_result('Connection successful')
                    elif response.status_code == 401:
                        set_test_result('Authentication failed. Check your API key.')
                    else:
                        set_test_result('Unexpected response from the server. Please try again.')
                        
                except Exception as exc:
                    error_msg = str(exc)
                    if 'getaddrinfo failed' in error_msg or 'NameResolutionError' in error_msg:
                        set_test_result('Could not reach the server. Check the URL.')
                    elif 'Connection refused' in error_msg:
                        set_test_result('Could not connect. Please try again.')
                    else:
                        set_test_result('Connection test failed. Please try again.')
                    logger.debug('Connection test failed: %s', exc)
            
            # Run test in a thread to avoid blocking UI
            threading.Thread(target=do_test, daemon=True).start()
        
        test_button.bind('<Button-1>', lambda _event: test_connection())
        test_button.bind('<Enter>', lambda _event: test_button.config(fg=ED_AMBER_HOVER) if not test_button_cooldown_active['value'] else None)
        test_button.bind('<Leave>', lambda _event: test_button.config(fg=ED_AMBER) if not test_button_cooldown_active['value'] else None)

        body.lift()
        frame.enabled_var = enabled_var
        frame.api_url_var = api_url_var
        frame.api_key_var = api_key_var
        frame.send_ship_info_var = send_ship_info_var
        frame.check_updates_var = check_updates_var
        
        # Store the frame globally so prefs_changed can access it
        prefs_frame = frame
        
        # Initialize UI based on current commander (if needed)
        try:
            prefs_cmdr_changed(cmdr, is_beta)
        except Exception:
            logger.exception('Error in prefs_cmdr_changed')
        
        logger.debug('plugin_prefs returning frame')
    except Exception:
        logger.exception('Error in plugin_prefs widget creation')
        # Clear frame and add error message
        for widget in frame.winfo_children():
            widget.destroy()
        nb.Label(frame, text='EDSpec settings could not be loaded. Please try again.').pack()
        # Still return the frame so the tab appears
        prefs_frame = frame
    
    logger.debug('plugin_prefs returning frame (final)')
    return frame


def prefs_cmdr_changed(cmdr: str | None, is_beta: bool) -> None:
    """
    Handle the Commander name changing whilst Settings was open.
    
    This function is called by EDMC when the commander changes while the
    settings dialog is open. Since EDSpec uses global settings (not per-commander),
    this mainly handles enabling/disabling UI elements based on beta status.
    
    Args:
        cmdr: The new current Commander name (or None if no commander).
        is_beta: Whether game beta was detected.
    """
    global prefs_frame
    
    if not prefs_frame:
        return
    
    # EDSpec uses global settings, so we don't need to update per-commander data
    # However, we could disable the plugin in beta if desired (similar to EDSM)
    # For now, we'll just ensure the function exists for EDMC compatibility
    pass


def prefs_changed(cmdr: str, is_beta: bool) -> None:
    """
    Save preferences when the user closes the settings dialog.
    
    Args:
        cmdr: Current commander name
        is_beta: Whether this is a beta game
    """
    global prefs_frame
    
    logger.info('Saving EDSpec preferences')
    
    # Access the stored frame and its variables
    if prefs_frame and hasattr(prefs_frame, 'enabled_var'):
        # Save enabled state
        config.set(ENABLED_SETTING, int(prefs_frame.enabled_var.get()))
        
        config.set(API_URL_SETTING, prefs_frame.api_url_var.get().strip())
        
        # Save API Key
        config.set(API_KEY_SETTING, prefs_frame.api_key_var.get())
        
        # Save privacy preferences
        config.set(SEND_SHIP_INFO_SETTING, int(prefs_frame.send_ship_info_var.get()))
        
        # Save update check preference
        config.set(CHECK_UPDATES_SETTING, int(prefs_frame.check_updates_var.get()))
        
        logger.info('EDSpec preferences saved successfully')
    else:
        logger.warning('Preferences frame not available')
    
    update_status()


def update_status() -> None:
    """
    Update the status label based on current configuration.
    """
    global status_label, last_connection_status, current_cmdr
    
    if not status_label:
        return
    
    try:
        api_key = get_api_key()
        enabled = is_plugin_enabled()
        
        if not api_key:
            status_label['text'] = 'Not configured'
            status_label['foreground'] = 'gray'
        elif not enabled:
            status_label['text'] = 'Disabled'
            status_label['foreground'] = 'orange'
        else:
            # Use last known connection status
            if last_connection_status == 'success':
                route_suffix = in_sync_status_suffix()
                status_label['text'] = f'In Sync{route_suffix}' if route_suffix else 'In Sync'
                status_label['foreground'] = 'green'
            elif last_connection_status == 'connecting':
                status_label['text'] = 'Connecting...'
                status_label['foreground'] = 'orange'
            elif last_connection_status == 'auth_failed':
                status_label['text'] = 'API Key invalid'
                status_label['foreground'] = 'red'
            elif last_connection_status == 'failed':
                status_label['text'] = 'Connection failed'
                status_label['foreground'] = 'red'
            elif not game_session_active:
                status_label['text'] = 'Please join a game mode'
                status_label['foreground'] = 'orange'
            else:  # disconnected or unknown
                status_label['text'] = 'Disconnected'
                status_label['foreground'] = 'red'
            
    except Exception:
        logger.exception('Error updating status')
        status_label['text'] = 'Error'
        status_label['foreground'] = 'red'


def _set_connection_status(color: str, *, auth_failed: bool = False) -> None:
    global last_connection_status
    if color == 'green':
        last_connection_status = 'success'
    elif auth_failed:
        last_connection_status = 'auth_failed'
    else:
        last_connection_status = 'failed'
    schedule_on_main_thread(update_status)


def _worker_send_commander(session, data: dict) -> None:
    creds = _require_api_credentials()
    if not creds:
        logger.debug('Plugin disabled or no API key, skipping send')
        return
    api_url, api_key = creds
    try:
        response = session.post(
            api_url,
            json=data,
            headers=_api_headers(api_key, json_body=True),
            timeout=10,
        )
        if response.status_code == 200:
            logger.debug('Successfully sent data to EDSpec')
            _set_connection_status('green')
        elif response.status_code == 401:
            logger.warning('Authentication failed - check your API key')
            _set_connection_status('red', auth_failed=True)
        else:
            logger.warning('Unexpected response from EDSpec: %s', response.status_code)
            _set_connection_status('red')
    except Exception as exc:
        logger.error('Failed to send data to EDSpec: %s', exc)
        _set_connection_status('red')


def _worker_ping(session, connected: bool) -> None:
    creds = _require_api_credentials()
    if not creds:
        return
    api_url, api_key = creds
    try:
        response = session.post(
            api_url,
            json={'connected': connected},
            headers=_api_headers(api_key, json_body=True),
            timeout=10,
        )
        if response.status_code == 200:
            logger.debug('Sent connection status to EDSpec: connected=%s', connected)
            if connected:
                _set_connection_status('green')
            else:
                global last_connection_status
                last_connection_status = 'disconnected'
                schedule_on_main_thread(update_status)
        elif response.status_code == 401:
            logger.warning('Failed to send connection status: authentication failed')
            _set_connection_status('red', auth_failed=True)
        else:
            logger.warning('Failed to send connection status: %s', response.status_code)
    except Exception as exc:
        logger.debug('Failed to send connection status: %s', exc)
        if connected:
            _set_connection_status('red')


def worker_thread_loop() -> None:
    """Background worker: all HTTP to EDSpec (commander, route, ping)."""
    session = timeout_session.new_session()

    while not stop_event.is_set():
        try:
            try:
                kind, payload = work_queue.get(timeout=1)
            except queue.Empty:
                continue

            if kind == JOB_COMMANDER:
                _worker_send_commander(session, payload)
            elif kind == JOB_PING:
                _worker_ping(session, bool(payload))
            elif kind == JOB_ROUTE_SET:
                _worker_sync_route(session, payload)
            elif kind == JOB_ROUTE_CLEAR:
                _worker_delete_route(session)
            elif kind == JOB_ROUTE_STATE_RETRY:
                _worker_route_state_retry(int(payload or 0))

            work_queue.task_done()
        except Exception:
            logger.exception('Error in worker thread loop')


def send_disconnect_message() -> None:
    """Synchronous disconnect ping during plugin_stop (no UI updates)."""
    creds = _require_api_credentials()
    if not creds:
        return
    api_url, api_key = creds
    try:
        session = timeout_session.new_session()
        session.post(
            api_url,
            json={'connected': False},
            headers=_api_headers(api_key, json_body=True),
            timeout=10,
        )
        logger.info('Sent disconnect message to EDSpec')
    except Exception as exc:
        logger.debug('Failed to send disconnect message: %s', exc)


def ping_thread_loop() -> None:
    """Enqueue keep-alive pings; never touch tkinter directly."""
    if game_session_active:
        enqueue_ping(True)
    else:
        enqueue_ping(False)

    while not ping_event.wait(30):
        if stop_event.is_set():
            break
        if game_session_active:
            enqueue_ping(True)
        else:
            enqueue_ping(False)


def check_for_updates_delayed() -> None:
    # Check for updates after startup, only runs once
    global update_check_performed
    
    if update_check_performed:
        return
    
    logger.info('Starting update check...')
    time.sleep(5)  # Give UI time to load
    
    check_updates = check_updates_enabled()
    if not check_updates:
        update_check_performed = True
        return
    
    if update_check_event.is_set():
        return
    
    try:
        latest_version = get_latest_version()
        
        if not latest_version:
            update_check_performed = True
            return
        
        if is_newer_version(latest_version, VERSION):
            logger.info('Update available: %s -> %s', VERSION, latest_version)
            schedule_on_main_thread(lambda: show_update_dialog(latest_version))
    except Exception:
        logger.exception('Update check failed')
    finally:
        update_check_performed = True


def get_latest_version() -> Optional[str]:
    # Fetch latest release version from GitHub
    try:
        api_url = f'https://api.github.com/repos/{GITHUB_REPO}/releases/latest'
        session = timeout_session.new_session()
        response = session.get(api_url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            tag_name = data.get('tag_name', '')
            release_name = data.get('name', '')
            
            version_str = None
            
            if tag_name:
                cleaned_tag = tag_name.lstrip('v').strip()
                if cleaned_tag and any(c.isdigit() for c in cleaned_tag) and '.' in cleaned_tag:
                    version_str = cleaned_tag
            
            if not version_str and release_name:
                version_match = re.search(r'(?:v)?(\d+\.\d+(?:\.\d+)?)', release_name, re.IGNORECASE)
                if version_match:
                    version_str = version_match.group(1)
            
            if not version_str:
                logger.warning('No version found in tag "%s" or name "%s"', tag_name, release_name)
                return None
            
            return version_str
        elif response.status_code == 404:
            logger.warning('Repo not found: %s', GITHUB_REPO)
            return None
        else:
            logger.warning('GitHub API error: %s', response.status_code)
            return None
    except Exception as exc:
        logger.warning('Failed to fetch version: %s', exc)
        return None


def is_newer_version(latest: str, current: str) -> bool:
    # Simple version comparison (1.0.0 format)
    try:
        latest_parts = [int(x) for x in latest.split('.')]
        current_parts = [int(x) for x in current.split('.')]
        
        max_len = max(len(latest_parts), len(current_parts))
        latest_parts += [0] * (max_len - len(latest_parts))
        current_parts += [0] * (max_len - len(current_parts))
        
        for i in range(max_len):
            if latest_parts[i] > current_parts[i]:
                return True
            elif latest_parts[i] < current_parts[i]:
                return False
        
        return False
    except Exception as exc:
        logger.debug('Version comparison failed: %s', exc)
        return False


def show_update_dialog(latest_version: str) -> None:
    if config.shutting_down:
        return
    try:
        root = _ui_root()
        if not root:
            return
        
        message = (
            f'A new version of the EDSpec plugin is available!\n\n'
            f'Current version: {VERSION}\n'
            f'Latest version: {latest_version}\n\n'
            f'Would you like to visit the GitHub releases page?'
        )
        
        result = messagebox.askyesno(
            'EDSpec Plugin Update Available',
            message,
            parent=root
        )
        
        if result:
            releases_url = f'https://github.com/{GITHUB_REPO}/releases/latest'
            webbrowser.open(releases_url)
            
    except Exception:
        logger.exception('Error showing update dialog')


def journal_id_string(value: object) -> Optional[str]:
    """Elite ids are 64-bit. Send them as strings so JSON does not round them."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        return str(int(text))
    if isinstance(value, int):
        if value < 0:
            return None
        return str(value)
    return None


def flight_log_from_journal(entry: dict) -> Optional[dict]:
    """Story event for flight logs. Names are not included."""
    event_name = entry.get('event')
    if event_name not in ('FSDJump', 'CarrierJump', 'Location', 'Docked', 'Undocked'):
        return None
    system_address = journal_id_string(entry.get('SystemAddress'))
    if not system_address:
        return None
    timestamp = entry.get('timestamp')
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None
    return {
        'systemAddress': system_address,
        'marketId': journal_id_string(entry.get('MarketID')),
        'event': event_name,
        'eventAt': timestamp.strip(),
    }


def journal_entry(cmdr: str, is_beta: bool, system: str, station: str, entry: dict, state: dict) -> Optional[str]:
    """
    Handle journal entries from Elite Dangerous.
    
    Args:
        cmdr: Commander name
        is_beta: Whether this is a beta version
        system: Current star system
        station: Current station (if docked)
        entry: Journal entry data
        state: Current game state
        
    Returns:
        None or error message string
    """
    global current_cmdr, current_system, current_station, latest_journal_state, game_session_start_ts
    global last_enqueued_route_signature, route_trust_state_nav_route, suppressed_fsd_target
    global merc_coins_cache, route_jump_hold, route_jump_released_at
    
    try:
        latest_journal_state = state if isinstance(state, dict) else {}
        # Update commander name
        if cmdr:
            current_cmdr = cmdr
        if system:
            current_system = system
        event_name = entry.get('event')
        if event_name == 'Undocked':
            current_station = ''
        elif isinstance(station, str):
            current_station = station

        if event_name in ('Shutdown', 'ShutDown'):
            game_session_start_ts = None
            last_enqueued_route_signature = None
            set_game_session_active(False)
            return None

        if event_name == 'LoadGame':
            game_session_start_ts = journal_entry_unix_ts(entry)
            last_enqueued_route_signature = None
            route_trust_state_nav_route = False
            suppressed_fsd_target = None
            route_jump_hold = False
            cancel_route_cancel_confirm()
            set_game_session_active(True)

        if event_name == 'Statistics':
            coins = merc_coins_from_statistics(entry)
            if coins is not None:
                merc_coins_cache = coins
                if game_session_active and send_ship_info_enabled() and work_queue:
                    enqueue_commander({
                        **location_payload(cmdr, system, station),
                        'mercCoins': coins,
                    })
            return None

        if event_name == 'StartUp':
            sync_game_session_from_journal(cmdr, system, state)
            sync_game_session_start_from_edmc()
            if game_session_active:
                nav_route = state.get('NavRoute')
                if (
                    nav_route
                    and isinstance(nav_route, dict)
                    and not is_nav_route_clear(nav_route)
                ):
                    handle_nav_route_state(nav_route, state)
            return None

        if not game_session_active:
            return None

        if event_name == 'StartJump' and entry.get('JumpType') == 'Hyperspace':
            route_jump_hold = True

        if event_name == 'NavRouteClear':
            request_route_clear_from_game(state)
            return None

        if event_name in ('NavRoute', 'Route'):
            handle_journal_route_event(entry, state)
            return None

        if event_name == 'FSDTarget':
            handle_fsd_target(entry, state)
            return None

        if event_name == 'Cargo':
            update_cargo_cache(entry, state)
            if send_ship_info_enabled() and work_queue:
                payload = location_payload(cmdr, system, station)
                ship_id, _, ship_name, _ = active_ship_from_state_and_entry(state, entry)
                if ship_name:
                    payload['ship'] = ship_name
                if ship_id is not None:
                    payload['activeShipId'] = ship_id
                payload.update(build_ships_payload(active_ship_id=ship_id))
                attach_cargo_payload(payload)
                attach_credits_payload(payload, state, entry)
                enqueue_commander(payload)
            return None

        if event_name == 'ShipyardBuy':
            new_id = handle_shipyard_buy(entry, system, state)
            enqueue_ship_cache(cmdr, system, station, state, entry, active_ship_id=new_id)
            return None

        if event_name == 'ShipyardSwap':
            new_id = handle_shipyard_swap(entry, system)
            enqueue_ship_cache(cmdr, system, station, state, entry, active_ship_id=new_id)
            return None

        if event_name == 'ShipyardTransfer':
            handle_shipyard_transfer(entry, system)
            enqueue_ship_cache(cmdr, system, station, state, entry)
            return None

        if event_name == 'SetUserShipName':
            handle_set_user_ship_name(entry)
            enqueue_ship_cache(cmdr, system, station, state, entry)
            return None

        if event_name in ('ModuleBuy', 'ModuleSell', 'EngineerCraft'):
            if apply_module_event(entry):
                enqueue_ship_cache(cmdr, system, station, state, entry)
            return None

        if event_name == 'ShipyardSell':
            sold_id = normalize_ship_id(entry.get('SellShipID'))
            if sold_id is not None and send_ship_info_enabled() and work_queue:
                remove_ship_from_cache(sold_id)
                payload = location_payload(cmdr, system, station)
                payload['removedShipIds'] = [sold_id]
                attach_credits_payload(payload, state, entry)
                payload.update(build_ships_payload())
                enqueue_commander(payload)
            return None

        if event_name == 'StoredShips':
            active_id, _, _, _ = active_ship_from_state_and_entry(state, entry)
            inventory_complete = handle_stored_ships_event(entry, system, active_ship_id=active_id)
            if send_ship_info_enabled() and work_queue and commander_ships_cache:
                payload = location_payload(cmdr, system, station)
                attach_credits_payload(payload, state, entry)
                payload.update(build_ships_payload())
                if inventory_complete:
                    payload['shipsComplete'] = True
                enqueue_commander(payload)
            return None

        if event_name in ('LoadGame', 'Loadout'):
            if send_ship_info_enabled():
                if event_name == 'LoadGame':
                    update_cargo_cache(entry, state)
                ship_id, ship_type, ship_name, ship_ident = active_ship_from_state_and_entry(state, entry)
                fuel_level, fuel_capacity = fuel_from_state_and_entry(state, entry)
                loadout = loadout_from_entry(entry) if event_name == 'Loadout' else None
                if ship_id is not None:
                    ship_record = {
                        'shipId': ship_id,
                        'shipType': ship_type,
                        'name': ship_name,
                        'ident': ship_ident,
                        'fuelLevel': fuel_level,
                        'fuelCapacity': fuel_capacity,
                        'active': True,
                    }
                    if system:
                        ship_record['starSystem'] = system
                        ship_record['inTransit'] = False
                    if loadout:
                        ship_record['loadout'] = loadout
                    merge_ships_into_cache([ship_record])
                if ship_id is not None and work_queue:
                    payload = location_payload(cmdr, system, station)
                    payload['ship'] = ship_name or ship_type
                    payload.update(build_ships_payload(
                            active_ship_id=ship_id,
                            fuel_level=fuel_level,
                            fuel_capacity=fuel_capacity,
                    ))
                    attach_cargo_payload(payload)
                    attach_credits_payload(payload, state, entry)
                    enqueue_commander(payload)
            if event_name == 'Loadout':
                return None

        if event_name in ('Refuel', 'RefuelAll', 'FuelScoop'):
            enqueue_fuel_update(cmdr, system, station, state, entry)
            return None

        if event_name == 'Scan':
            enqueue_fss_scan(cmdr, system, entry)
            return None

        if event_name == 'SAAScanComplete':
            enqueue_fss_mapped(cmdr, system, entry)
            return None

        if event_name == 'FSSDiscoveryScan':
            enqueue_fss_progress(cmdr, system, entry)
            return None

        if event_name == 'FSSAllBodiesFound':
            enqueue_fss_scan_complete(cmdr, system, entry)
            return None

        # Only send on specific events to avoid spam
        events_to_send_on = [
            'FSDJump',
            'CarrierJump',
            'Location',
            'Docked',
            'Undocked',
            'Loadout',
            'Embark',
            'Disembark',
        ]
        
        if event_name not in events_to_send_on:
            return None
        
        # Determine status based on state and event
        status = 'unknown'
        if state.get('OnFoot', False):
            status = 'onfoot'
        elif state.get('IsDocked', False):
            status = 'docked'
        elif state.get('Role'):  # If in multicrew
            status = 'docked'  # or could be 'multicrew'
        else:
            status = 'undocked'
        
        send_ship_info = send_ship_info_enabled()
        report_station = None if event_name == 'Undocked' else station

        data = location_payload(cmdr, system, report_station)
        
        # Add ship info if enabled
        if send_ship_info:
            ship_name = 'Unknown'
            if state.get('ShipName'):
                ship_name = state.get('ShipName')
            elif 'Ship' in state and isinstance(state['Ship'], dict):
                ship_name = state['Ship'].get('name', 'Unknown')
            elif 'ShipType' in state and state['ShipType']:
                ship_name = state.get('ShipType')
            
            data['ship'] = ship_name
            credits = credits_from_state_and_entry(state, entry)
            if credits is not None:
                data['credits'] = credits
            attach_merc_coins_payload(data)
            data['status'] = status
            attach_ship_payload(data, state, entry, star_system=system)
        else:
            data['status'] = status
            fuel_level, fuel_capacity = fuel_from_state_and_entry(state, entry)
            if fuel_level is not None:
                data['fuelLevel'] = fuel_level
            if fuel_capacity is not None:
                data['fuelCapacity'] = fuel_capacity
        
        flight_log = flight_log_from_journal(entry)
        if flight_log:
            data['flightLog'] = flight_log

        if event_name == 'FSDJump':
            route_jump_hold = False
            route_jump_released_at = time.time()

        apply_system_to_route_progress(system)
        hop_idx = effective_hop_index()
        if hop_idx >= 0:
            data['routeHopIndex'] = hop_idx

        # Queue data for sending
        if work_queue and data:
            enqueue_commander(data)
            logger.debug('Queued data for send: %s - Ship info: %s', entry.get('event'), send_ship_info)
        
    except Exception:
        logger.exception('Error in journal_entry')
        return JOURNAL_ERROR_MESSAGE
    
    return None


def dashboard_entry(cmdr: str, is_beta: bool, entry: dict) -> None:
    """Push the ship's main tank from status.json while fuel is burning or scooping."""
    global current_cmdr
    try:
        if not isinstance(entry, dict) or not is_plugin_enabled() or not game_session_active:
            return
        if not current_system:
            return
        flags = entry.get('Flags')
        if isinstance(flags, int) and flags & (IN_SRV_FLAG | IN_FIGHTER_FLAG):
            return
        level = fuel_main_from_status(entry)
        if level is None:
            return
        if cmdr:
            current_cmdr = cmdr
        schedule_live_fuel(level)
    except Exception:
        logger.exception('Error in dashboard_entry')


def cmdr_data(data: dict, is_beta: bool) -> None:
    """
    Handle commander data from Frontier's CAPI.
    
    Args:
        data: Commander data from CAPI
        is_beta: Whether this is a beta version
    """
    global current_cmdr
    
    try:
        if not game_session_active:
            return
        
        send_ship_info = send_ship_info_enabled()
        
        capi_data = {}
        
        if 'commander' in data and data['commander']:
            commander = data['commander']
            cmdr_name = commander.get('name', '')
            capi_data = {'cmdr': cmdr_name}
            
            # Update commander name
            if cmdr_name:
                current_cmdr = cmdr_name
            
            # Add location data (always sent)
            # Get system info if available
            if 'lastSystem' in data and data['lastSystem']:
                capi_data['system'] = data['lastSystem'].get('name', '')
            
            # Get station info if available
            if 'lastStarport' in data and data['lastStarport']:
                last_station = data['lastStarport']
                capi_data['station'] = last_station.get('name', '')
            
            # Add ship info if enabled
            if send_ship_info:
                capi_data['credits'] = commander.get('credits', 0)
                
                ships_batch = []
                current_ship_id = normalize_ship_id(data.get('currentShipId'))
                if 'ships' in data and isinstance(data['ships'], list):
                    for ship in data['ships']:
                        if not isinstance(ship, dict):
                            continue
                        ship_id = normalize_ship_id(ship.get('id'))
                        if ship_id is None:
                            continue
                        ship_record = {
                            'shipId': ship_id,
                            'shipType': ship.get('shipType') or ship.get('ship_type') or 'unknown',
                            'name': ship.get('name'),
                            'value': ship.get('value'),
                            'active': current_ship_id is not None and ship_id == current_ship_id,
                        }
                        ship_ident = ship.get('ident') or ship.get('shipIdent')
                        if isinstance(ship_ident, str) and ship_ident.strip():
                            ship_record['ident'] = ship_ident.strip()
                        ships_batch.append(ship_record)
                        if current_ship_id is not None and ship_id == current_ship_id:
                            capi_data['ship'] = ship.get('name', 'Unknown')

                if ships_batch:
                    replace_ships_cache(ships_batch)
                    capi_data.update(build_ships_payload(active_ship_id=current_ship_id))
                    capi_data['shipsComplete'] = True
        
        # Queue data for sending
        if work_queue and capi_data:
            enqueue_commander(capi_data)
            logger.debug('Queued CAPI data for send - Ship info: %s', send_ship_info)
        
    except Exception:
        logger.exception('Error in cmdr_data')

