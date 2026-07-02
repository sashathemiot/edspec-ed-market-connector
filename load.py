"""
EDSpec Plugin for EDMarketConnector

This plugin connects EDMarketConnector with the EDSpec service to share
commander data including ships, credits, current location, and status.

Requires an API key from EDSpec to authenticate requests.

Version: 1.0.2
Developer: sashathemiot
Website: https://edspecbot.com
"""
import logging
import os
import queue
import threading
import time
import webbrowser
from calendar import timegm
from time import strptime
from typing import Callable, Optional, Tuple

from config import appname, config
import timeout_session
import tkinter as tk
import tkinter.messagebox as messagebox
import tkinter.ttk as ttk
import myNotebook as nb

# EDMC < 5.0 config API compatibility (see PLUGINS.md)
if not hasattr(config, 'get_int'):
    config.get_int = config.getint
if not hasattr(config, 'get_str'):
    config.get_str = config.get
if not hasattr(config, 'get_bool'):
    config.get_bool = lambda key: bool(config.getint(key))
if not hasattr(config, 'get_list'):
    config.get_list = config.get

plugin_name = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(f'{appname}.{plugin_name}')
if not logger.hasHandlers():
    logger.setLevel(logging.INFO)
    _log_handler = logging.StreamHandler()
    _log_handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d:%(funcName)s: %(message)s',
    ))
    logger.addHandler(_log_handler)

VERSION = '1.0.2'
PLUGIN_VERSION = VERSION
GITHUB_REPO = 'sashathemiot/edspec-ed-market-connector'

API_KEY_SETTING = f'{plugin_name}.api_key'
API_URL_SETTING = f'{plugin_name}.api_url'
ENABLED_SETTING = f'{plugin_name}.enabled'
SEND_SHIP_INFO_SETTING = f'{plugin_name}.send_ship_info'
CHECK_UPDATES_SETTING = f'{plugin_name}.check_updates'

DEFAULT_API_URL = 'https://api.edspecbot.com/edmc/ingest'
JOURNAL_ERROR_MESSAGE = 'EDSpec could not send data. Check the log for details.'

JOB_COMMANDER = 'commander'
JOB_PING = 'ping'
JOB_ROUTE_SET = 'route_set'
JOB_ROUTE_CLEAR = 'route_clear'
JOB_ROUTE_STATE_RETRY = 'route_state_retry'
JOB_FSS_SIGNALS = 'fss_signals'

FSS_FLUSH_DEBOUNCE_SEC = 2
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
latest_journal_state: dict = {}
game_session_active = False
active_route = None
active_route_hop_index = -1
route_trust_state_nav_route = False
suppressed_fsd_target: Optional[str] = None
game_session_start_ts: Optional[int] = None
last_enqueued_route_signature: Optional[str] = None
route_completion_timer: Optional[threading.Timer] = None
ROUTE_COMPLETE_CLEAR_SEC = 30
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
pending_fss_system_address: Optional[int] = None
pending_fss_timestamp: Optional[str] = None
pending_fss_signals: list = []
fss_flush_timer: Optional[threading.Timer] = None
status_label: Optional[tk.Label] = None


def user_agent() -> str:
    base = getattr(config, 'user_agent', None) or f'{appname}/{plugin_name}'
    return f'{base} (EDSpec/{VERSION})'


def is_plugin_enabled() -> bool:
    if config.get(ENABLED_SETTING) is None:
        return True
    return bool(config.get_bool(ENABLED_SETTING))


def get_api_key() -> str:
    return (config.get_str(API_KEY_SETTING) or '').strip()


def send_ship_info_enabled() -> bool:
    if config.get(SEND_SHIP_INFO_SETTING) is None:
        return True
    return bool(config.get_bool(SEND_SHIP_INFO_SETTING))


def check_updates_enabled() -> bool:
    if config.get(CHECK_UPDATES_SETTING) is None:
        return True
    return bool(config.get_bool(CHECK_UPDATES_SETTING))


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


def enqueue_commander(data: dict) -> None:
    if not game_session_active:
        logger.debug('Skipping commander payload: commander not in game')
        return
    enqueue_job(JOB_COMMANDER, data)




def get_active_route_api_url() -> str:
    api_url = get_api_url().rstrip('/')
    for ingest_path in (
        '/edmc/ingest',
        '/api/edmc/ingest',
        '/api/edmcConnector',
    ):
        if api_url.endswith(ingest_path):
            return f'{api_url[:-len(ingest_path)]}/edmc/active-route'
    from urllib.parse import urlparse
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
    active_route = payload
    active_route_hop_index = -1
    suppressed_fsd_target = None
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
    if not game_session_active:
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


def route_steps_from_edmc(entry: dict, state: dict) -> list:
    route_steps = entry.get('Route')
    if isinstance(route_steps, list) and route_steps:
        return route_steps

    if not route_trust_state_nav_route:
        return []

    nav_route = state.get('NavRoute')
    if isinstance(nav_route, dict) and not is_nav_route_clear(nav_route):
        nav_steps = nav_route.get('Route')
        if isinstance(nav_steps, list) and nav_steps:
            return nav_steps

    return []


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
        global route_trust_state_nav_route
        route_trust_state_nav_route = True
        enqueue_route_sync(route_steps, game_state)


def request_route_clear_from_game() -> None:
    """Schedule route clear after ROUTE_COMPLETE_CLEAR_SEC (Elite often spams NavRouteClear in hyperspace)."""
    cancel_route_completion_clear()
    if active_route and not is_route_complete():
        logger.debug('Ignoring route clear while hops remain')
        return
    schedule_route_completion_clear()


def handle_journal_route_event(entry: dict, state: dict) -> None:
    global route_trust_state_nav_route

    if is_nav_route_clear(entry):
        cancel_route_completion_clear()
        if active_route and not is_route_complete():
            logger.debug('Ignoring NavRouteClear mid-route (hyperspace)')
            return
        logger.info('In-game nav route cleared; removing EDSpec route plot')
        request_route_clear_from_game()
        return

    route_trust_state_nav_route = True
    steps = route_steps_from_edmc(entry, state)
    if steps:
        enqueue_route_sync(steps, state)
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

    if fsd_target_is_suppressed(target_name):
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
    global route_trust_state_nav_route, suppressed_fsd_target, last_enqueued_route_signature

    clear_dest = active_route.get('to') if isinstance(active_route, dict) else None
    suppressed_fsd_target = str(clear_dest).strip() if clear_dest else None

    route_trust_state_nav_route = False
    last_enqueued_route_signature = None
    cancel_nav_route_state_retry()


def is_ingame_active_route() -> bool:
    return isinstance(active_route, dict) and active_route.get('source') == 'ingame'


def fsd_target_is_suppressed(target_name: Optional[str]) -> bool:
    if not suppressed_fsd_target or not target_name:
        return False
    return _system_key(target_name) == _system_key(suppressed_fsd_target)


def enqueue_route_clear() -> None:
    mark_route_cleared()
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


def enqueue_fss_signals(payload: dict) -> None:
    enqueue_job(JOB_FSS_SIGNALS, payload)


def clear_pending_fss_batch() -> None:
    global pending_fss_system_address, pending_fss_timestamp, pending_fss_signals, fss_flush_timer
    pending_fss_system_address = None
    pending_fss_timestamp = None
    pending_fss_signals = []
    if fss_flush_timer is not None:
        fss_flush_timer.cancel()
        fss_flush_timer = None


def flush_pending_fss_signals() -> None:
    """Send batched expiring FSS signals to EDSpec."""
    global pending_fss_system_address, pending_fss_timestamp, pending_fss_signals, fss_flush_timer

    if fss_flush_timer is not None:
        fss_flush_timer.cancel()
        fss_flush_timer = None

    if not pending_fss_signals or pending_fss_system_address is None:
        clear_pending_fss_batch()
        return

    payload = {
        'fssSignals': {
            'systemAddress': pending_fss_system_address,
            'timestamp': pending_fss_timestamp or time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'signals': list(pending_fss_signals),
        }
    }
    clear_pending_fss_batch()
    enqueue_fss_signals(payload)


def schedule_fss_flush() -> None:
    global fss_flush_timer

    if fss_flush_timer is not None:
        fss_flush_timer.cancel()

    fss_flush_timer = threading.Timer(FSS_FLUSH_DEBOUNCE_SEC, flush_pending_fss_signals)
    fss_flush_timer.daemon = True
    fss_flush_timer.start()


def fss_signal_from_journal_entry(entry: dict) -> Optional[dict]:
    """Build one EDDN-shaped signal row; only expiring signals (TimeRemaining > 0)."""
    time_remaining = entry.get('TimeRemaining')
    if time_remaining is None:
        return None

    try:
        remaining = int(time_remaining)
    except (TypeError, ValueError):
        return None

    if remaining <= 0:
        return None

    signal_name = entry.get('SignalName')
    if not signal_name:
        return None

    return {
        'SignalName': signal_name,
        'SignalType': entry.get('SignalType'),
        'IsStation': entry.get('IsStation'),
        'USSType': entry.get('USSType'),
        'TimeRemaining': remaining,
        'SpawningState': entry.get('SpawningState'),
        'SpawningFaction': entry.get('SpawningFaction'),
        'SpawningPower': entry.get('SpawningPower'),
        'OpposingPower': entry.get('OpposingPower'),
        'ThreatLevel': entry.get('ThreatLevel'),
        'timestamp': entry.get('timestamp'),
    }


def queue_fss_signal_from_entry(entry: dict) -> None:
    global pending_fss_system_address, pending_fss_timestamp, pending_fss_signals

    signal = fss_signal_from_journal_entry(entry)
    if not signal:
        return

    system_address = entry.get('SystemAddress')
    try:
        system_address = int(system_address)
    except (TypeError, ValueError):
        return

    if pending_fss_system_address not in (None, system_address):
        flush_pending_fss_signals()

    pending_fss_system_address = system_address
    pending_fss_timestamp = entry.get('timestamp') or pending_fss_timestamp
    pending_fss_signals.append(signal)
    schedule_fss_flush()


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
    return record


def merge_ships_into_cache(ships: list) -> None:
    global commander_ships_cache
    for ship in ships:
        if not isinstance(ship, dict):
            continue
        ship_id = normalize_ship_id(ship.get('shipId'))
        if ship_id is None:
            continue
        previous = commander_ships_cache.get(ship_id, {})
        merged = {**previous, **ship, 'shipId': ship_id}
        commander_ships_cache[ship_id] = merged


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


def handle_stored_ships_event(entry: dict, system: str) -> None:
    star_system = entry.get('StarSystem') or system
    batch = []

    for ship in entry.get('ShipsHere') or []:
        if isinstance(ship, dict):
            record = ship_record_from_stored(ship, star_system=star_system, in_transit=False)
            if record:
                batch.append(record)

    for ship in entry.get('ShipsRemote') or []:
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

    if batch:
        merge_ships_into_cache(batch)


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

    payload = {
        'cmdr': cmdr,
        'system': system,
        'station': station,
    }
    payload.update(build_ships_payload(
        active_ship_id=ship_id,
        fuel_level=fuel_level,
        fuel_capacity=fuel_capacity,
    ))
    if send_ship_info_enabled():
        attach_cargo_payload(payload)
        attach_credits_payload(payload, state, entry)
    enqueue_commander(payload)


def attach_ship_payload(data: dict, state: dict, entry: dict) -> None:
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


def _require_api_credentials() -> Optional[tuple[str, str]]:
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
    global work_queue, worker_thread, ping_thread, stop_event, ping_event, update_check_performed
    
    logger.info(f'EDSpec plugin starting from {plugin_dir}')
    
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
    global stop_event, ping_event, worker_thread, ping_thread
    
    logger.info('EDSpec plugin stopping')

    flush_pending_fss_signals()
    cancel_nav_route_state_retry()
    cancel_route_completion_clear()
    
    # Send disconnect message before shutting down
    send_disconnect_message()
    
    # Signal threads to stop
    if stop_event:
        stop_event.set()
    if ping_event:
        ping_event.set()
    if update_check_event:
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
    
    logger.debug(f'plugin_prefs called: cmdr={cmdr}, is_beta={is_beta}')
    
    # Create the frame - this must succeed
    try:
        frame = nb.Frame(parent)
        frame.columnconfigure(1, weight=1)
    except Exception as e:
        logger.exception(f'Error creating preferences frame: {e}')
        # Last resort - create minimal frame
        frame = nb.Frame(parent)
        nb.Label(frame, text='Error: Could not create EDSpec preferences frame').pack()
        return frame
    
    # Wrap all widget creation in try-except to ensure frame is always returned
    try:
        # Title - EDSpec styled
        try:
            title_label = nb.Label(frame, text='EDSpec', font=('Helvetica', 18, 'bold'))
            title_label.grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(15, 8), padx=(20, 5))
        except Exception as e:
            logger.exception(f'Error creating title label: {e}')
            # Fallback to simple label without font
            title_label = nb.Label(frame, text='EDSpec')
            title_label.grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(15, 8), padx=(20, 5))
        # Subtitle
        nb.Label(frame, text='A Discord bot for Elite Dangerous!', font=('Helvetica', 10)).grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=(0, 25), padx=(20, 5)
        )
        
        # Description section
        desc_text = (
            'Connect your Elite Dangerous game data to EDSpec for the Discord bot and galaxy map.\n\n'
            'Always shared while you are in game:\n'
            '• Commander name, current system, and station when docked\n'
            '• Fuel level (jumps, refuel, and fuel scoop events)\n\n'
            'Optional (privacy setting below):\n'
            '• Active ship name, credits, cargo, fleet, and loadout details\n'
            '• On-foot, docked, or undocked status\n\n'
            'Fuel warnings, route display, and map audio are configured in the galaxy map '
            'Settings menu on the EDSpec website, not in ED Market Connector.'
        )
        nb.Label(frame, text=desc_text, justify=tk.LEFT, wraplength=550, font=('Helvetica', 9)).grid(
            row=2, column=0, columnspan=2, sticky=tk.W, pady=(0, 20), padx=(20, 5)
        )
        
        # Enable checkbox
        enabled_var = tk.BooleanVar(value=is_plugin_enabled())
        nb.Checkbutton(frame, text='Enable EDSpec integration', variable=enabled_var).grid(
            row=3, column=0, columnspan=2, sticky=tk.W, pady=10, padx=(20, 5)
        )
        
        # Privacy options section
        nb.Label(frame, text='Privacy Options:', font=('Helvetica', 10)).grid(
            row=4, column=0, columnspan=2, sticky=tk.W, pady=(20, 5), padx=(20, 5)
        )
        
        send_ship_info_var = tk.BooleanVar(value=send_ship_info_enabled())
        nb.Checkbutton(
            frame,
            text='Share additional data (ship, credits, cargo, fleet, loadout, on-foot status)',
            variable=send_ship_info_var,
        ).grid(
            row=5, column=0, columnspan=2, sticky=tk.W, pady=5, padx=(40, 5)
        )
        
        nb.Label(
            frame,
            text='Note: System, station, and fuel are always shared while Elite is running.',
            justify=tk.LEFT,
            wraplength=520,
            font=('Helvetica', 8),
            foreground='gray',
        ).grid(
            row=6, column=0, columnspan=2, sticky=tk.W, pady=(0, 15), padx=(40, 5)
        )
        
        # Update check section
        nb.Label(frame, text='Updates:', font=('Helvetica', 10)).grid(
            row=7, column=0, columnspan=2, sticky=tk.W, pady=(20, 5), padx=(20, 5)
        )
        
        check_updates_var = tk.BooleanVar(value=check_updates_enabled())
        nb.Checkbutton(frame, text='Check for updates on startup', variable=check_updates_var).grid(
            row=8, column=0, columnspan=2, sticky=tk.W, pady=5, padx=(40, 5)
        )
        
        current_version_label = nb.Label(frame, text=f'Current version: {VERSION}',
                                         font=('Helvetica', 8), foreground='gray')
        current_version_label.grid(row=9, column=0, columnspan=2, sticky=tk.W, pady=(0, 15), padx=(40, 5))
        
        api_url_label = nb.Label(frame, text='API URL:')
        api_url_label.grid(row=10, column=0, sticky=tk.W, pady=8, padx=(20, 5))
        api_url_var = tk.StringVar(
            value=(config.get_str(API_URL_SETTING) or '').strip() or DEFAULT_API_URL,
        )
        api_url_entry = ttk.Entry(frame, textvariable=api_url_var, width=50)
        api_url_entry.grid(row=10, column=1, sticky=tk.W+tk.E, pady=8, padx=5)

        nb.Label(
            frame,
            text='Local dev: http://localhost:4000/edmc/ingest',
            font=('Helvetica', 8),
            foreground='gray',
        ).grid(row=11, column=0, columnspan=2, sticky=tk.W, pady=(0, 10), padx=(40, 5))
        
        # API Key section
        api_key_label = nb.Label(frame, text='API Key:')
        api_key_label.grid(row=12, column=0, sticky=tk.W, pady=8, padx=(20, 5))
        api_key_var = tk.StringVar(value=config.get_str(API_KEY_SETTING) or '')
        # Use ttk.Entry for password field (EntryMenu may not support show parameter)
        api_key_entry = ttk.Entry(frame, textvariable=api_key_var, width=50, show='*')
        api_key_entry.grid(row=12, column=1, sticky=tk.W+tk.E, pady=8, padx=5)
        
        # Get API Key help text
        get_key_text = 'Get your API key from https://edspecbot.com'
        get_key_label = nb.Label(frame, text=get_key_text, cursor='hand2', underline=18)
        get_key_label.grid(
            row=13, column=0, columnspan=2, sticky=tk.W, pady=(0, 10), padx=(20, 5)
        )
        # Configure blue color and make the link clickable
        get_key_label.config(foreground='#0000FF')  # Explicit blue hex color
        get_key_label.bind('<Button-1>', lambda e: webbrowser.open('https://edspecbot.com'))
        get_key_label.bind('<Enter>', lambda e: get_key_label.config(foreground='#fb923c'))
        get_key_label.bind('<Leave>', lambda e: get_key_label.config(foreground='#0000FF'))
        
        # Connection test section
        test_result_var = tk.StringVar(value='')
        test_result_label = nb.Label(frame, textvariable=test_result_var, wraplength=500, font=('Helvetica', 9))
        test_result_label.grid(row=14, column=0, columnspan=2, sticky=tk.W, pady=5, padx=(20, 5))
        
        # Cooldown state for test button
        test_button_cooldown_active = {'value': False}
        
        # Create button first so it can be referenced in the function
        test_button = tk.Button(frame, text='Test Connection')
        
        def test_connection():
            """Test the connection to the EDSpec API"""
            # Check if cooldown is active
            if test_button_cooldown_active['value']:
                return
            
            # Activate cooldown
            test_button_cooldown_active['value'] = True
            test_button.config(state='disabled')
            original_text = test_button['text']
            
            def reenable_button():
                test_button_cooldown_active['value'] = False
                test_button.config(state='normal', text=original_text)
            
            def update_cooldown_text(seconds_left):
                if seconds_left > 0:
                    test_button.config(text=f'Test Connection (cooldown: {seconds_left}s)')
                    frame.after(1000, lambda: update_cooldown_text(seconds_left - 1))
                else:
                    reenable_button()
            
            # Start cooldown countdown
            update_cooldown_text(10)
            
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
                schedule_on_main_thread(lambda: test_result_var.set(message))
            
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
                        set_test_result(f'Unexpected response: {response.status_code}')
                        
                except Exception as e:
                    error_msg = str(e)
                    if 'getaddrinfo failed' in error_msg or 'NameResolutionError' in error_msg:
                        set_test_result('Could not reach the server. Check the URL.')
                    elif 'Connection refused' in error_msg:
                        set_test_result('Connection refused. The server may be down.')
                    else:
                        set_test_result('Connection test failed. Check the log for details.')
                    logger.debug('Connection test failed: %s', e)
            
            # Run test in a thread to avoid blocking UI
            threading.Thread(target=do_test, daemon=True).start()
        
        # Set the command after function is defined
        test_button.config(command=test_connection)
        test_button.grid(row=15, column=0, columnspan=2, sticky=tk.W, pady=5, padx=(20, 5))
        
        # Store references for prefs_changed and prefs_cmdr_changed
        frame.enabled_var = enabled_var
        frame.api_url_var = api_url_var
        frame.api_key_var = api_key_var
        frame.send_ship_info_var = send_ship_info_var
        frame.check_updates_var = check_updates_var
        frame.enabled_checkbutton = None  # Will be set if we need to reference it
        
        # Store the frame globally so prefs_changed can access it
        prefs_frame = frame
        
        # Initialize UI based on current commander (if needed)
        try:
            prefs_cmdr_changed(cmdr, is_beta)
        except Exception as e:
            logger.exception(f'Error in prefs_cmdr_changed: {e}')
        
        logger.debug('plugin_prefs returning frame')
    except Exception as e:
        logger.exception(f'Error in plugin_prefs widget creation: {e}')
        # Clear frame and add error message
        for widget in frame.winfo_children():
            widget.destroy()
        nb.Label(frame, text=f'Error loading EDSpec preferences: {str(e)[:100]}').pack()
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
                status_label['text'] = 'PLEASE START GAME'
                status_label['foreground'] = 'orange'
            else:  # disconnected or unknown
                status_label['text'] = 'Disconnected'
                status_label['foreground'] = 'red'
            
    except Exception as e:
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
            _set_connection_status('orange')
    except Exception as exc:
        logger.error('Failed to send data to EDSpec: %s', exc)
        _set_connection_status('red')


def _worker_send_fss_signals(session, data: dict) -> None:
    creds = _require_api_credentials()
    if not creds:
        logger.debug('Plugin disabled or no API key, skipping FSS send')
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
            signal_count = len((data.get('fssSignals') or {}).get('signals') or [])
            logger.debug('Sent %s expiring FSS signal(s) to EDSpec', signal_count)
            _set_connection_status('green')
        elif response.status_code == 401:
            logger.warning('Authentication failed - check your API key')
            _set_connection_status('red', auth_failed=True)
        else:
            logger.warning('Unexpected FSS response from EDSpec: %s', response.status_code)
            _set_connection_status('orange')
    except Exception as exc:
        logger.error('Failed to send FSS signals to EDSpec: %s', exc)
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
    """Background worker: all HTTP to EDSpec (commander, route, ping, FSS)."""
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
            elif kind == JOB_FSS_SIGNALS:
                _worker_send_fss_signals(session, payload)

            work_queue.task_done()
        except Exception:
            logger.exception('Error in worker thread loop')
        time.sleep(1)
    

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
            logger.info(f'Update available: {VERSION} -> {latest_version}')
            schedule_on_main_thread(lambda: show_update_dialog(latest_version))
    except Exception as e:
        logger.exception(f'Update check failed: {e}')
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
                import re
                version_match = re.search(r'(?:v)?(\d+\.\d+(?:\.\d+)?)', release_name, re.IGNORECASE)
                if version_match:
                    version_str = version_match.group(1)
            
            if not version_str:
                logger.warning(f'No version found in tag "{tag_name}" or name "{release_name}"')
                return None
            
            return version_str
        elif response.status_code == 404:
            logger.warning(f'Repo not found: {GITHUB_REPO}')
            return None
        else:
            logger.warning(f'GitHub API error: {response.status_code}')
            return None
    except Exception as e:
        logger.warning(f'Failed to fetch version: {e}')
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
    except Exception as e:
        logger.debug(f'Version comparison failed: {e}')
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
            
    except Exception as e:
        logger.exception('Error showing update dialog')


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
    global current_cmdr, current_system, latest_journal_state, game_session_start_ts
    global last_enqueued_route_signature, route_trust_state_nav_route, suppressed_fsd_target
    
    try:
        latest_journal_state = state if isinstance(state, dict) else {}
        # Update commander name
        if cmdr:
            current_cmdr = cmdr
        if system:
            current_system = system

        event_name = entry.get('event')

        if event_name in ('Shutdown', 'ShutDown'):
            flush_pending_fss_signals()
            game_session_start_ts = None
            last_enqueued_route_signature = None
            set_game_session_active(False)
            return None

        if event_name == 'LoadGame':
            game_session_start_ts = journal_entry_unix_ts(entry)
            last_enqueued_route_signature = None
            route_trust_state_nav_route = False
            suppressed_fsd_target = None
            set_game_session_active(True)

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

        if event_name == 'NavRouteClear':
            cancel_route_completion_clear()
            if active_route and not is_route_complete():
                logger.debug('Ignoring NavRouteClear mid-route (hyperspace)')
                return None
            logger.info('In-game nav route cleared; removing EDSpec route plot')
            request_route_clear_from_game()
            return None

        if event_name in ('NavRoute', 'Route'):
            handle_journal_route_event(entry, state)
            return None

        if event_name == 'FSDTarget':
            handle_fsd_target(entry, state)
            return None

        if event_name == 'FSSSignalDiscovered':
            queue_fss_signal_from_entry(entry)
            return None

        if event_name == 'FSDJump':
            flush_pending_fss_signals()

        if event_name == 'Cargo':
            update_cargo_cache(entry, state)
            if send_ship_info_enabled() and work_queue:
                payload = {
                    'cmdr': cmdr,
                    'system': system,
                    'station': station,
                }
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

        if event_name == 'StoredShips':
            handle_stored_ships_event(entry, system)
            if send_ship_info_enabled() and work_queue and commander_ships_cache:
                payload = {
                    'cmdr': cmdr,
                    'system': system,
                    'station': station,
                }
                attach_credits_payload(payload, state, entry)
                payload.update(build_ships_payload())
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
                    if loadout:
                        ship_record['loadout'] = loadout
                    merge_ships_into_cache([ship_record])
                if ship_id is not None and work_queue:
                    payload = {
                        'cmdr': cmdr,
                        'system': system,
                        'station': station,
                        'ship': ship_name or ship_type,
                        **build_ships_payload(
                            active_ship_id=ship_id,
                            fuel_level=fuel_level,
                            fuel_capacity=fuel_capacity,
                        ),
                    }
                    attach_cargo_payload(payload)
                    attach_credits_payload(payload, state, entry)
                    enqueue_commander(payload)
            if event_name == 'Loadout':
                return None

        if event_name in ('Refuel', 'RefuelAll', 'FuelScoop'):
            enqueue_fuel_update(cmdr, system, station, state, entry)
            return None
        
        # Only send on specific events to avoid spam
        events_to_send_on = ['FSDJump', 'Location', 'Docked', 'Undocked', 'Loadout', 'Embark', 'Disembark']
        
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
        
        # Prepare simplified data to send (location is always sent)
        data = {
            'cmdr': cmdr,
            'system': system,
            'station': report_station,
        }
        
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
            data['status'] = status
            attach_ship_payload(data, state, entry)
        elif event_name in ('Undocked', 'Docked', 'FSDJump'):
            data['status'] = status
            attach_ship_payload(data, state, entry)
        
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
                        ships_batch.append({
                            'shipId': ship_id,
                            'shipType': ship.get('shipType') or ship.get('ship_type') or 'unknown',
                            'name': ship.get('name'),
                            'value': ship.get('value'),
                            'active': current_ship_id is not None and ship_id == current_ship_id,
                        })
                        if current_ship_id is not None and ship_id == current_ship_id:
                            capi_data['ship'] = ship.get('name', 'Unknown')

                if ships_batch:
                    merge_ships_into_cache(ships_batch)
                    capi_data.update(build_ships_payload(active_ship_id=current_ship_id))
        
        # Queue data for sending
        if work_queue and capi_data:
            enqueue_commander(capi_data)
            logger.debug('Queued CAPI data for send - Ship info: %s', send_ship_info)
        
    except Exception:
        logger.exception('Error in cmdr_data')


def cmdr_data_legacy(data: dict, is_beta: bool) -> None:
    """Legacy galaxy CAPI uses a separate callback; same payload handling as Live."""
    cmdr_data(data, is_beta)
