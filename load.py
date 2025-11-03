"""
EDSpec Plugin for EDMarketConnector

This plugin connects EDMarketConnector with the EDSpec service to share
commander data including ships, credits, current location, and status.

Requires an API key from EDSpec to authenticate requests.

Version: 1.0.0
Developer: sashathemiot
Website: https://edspecbot.com
"""
import logging
import os
import threading
import queue
import time
import webbrowser
from typing import Optional, Tuple

from config import appname, config
import timeout_session
import tkinter as tk
import tkinter.messagebox as messagebox
import myNotebook as nb

# Set up logging
plugin_name = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(f'{appname}.{plugin_name}')

PLUGIN_VERSION = '1.0.0'
GITHUB_REPO = 'sashathemiot/edspec-ed-market-connector'

# Configuration
API_KEY_SETTING = f'{plugin_name}.api_key'
ENABLED_SETTING = f'{plugin_name}.enabled'
SEND_SHIP_INFO_SETTING = f'{plugin_name}.send_ship_info'
CHECK_UPDATES_SETTING = f'{plugin_name}.check_updates'

# Default values
DEFAULT_API_URL = 'https://edspecbot.com/api/edmcConnector'

# Global state
status_label: Optional[tk.Label] = None
send_queue: Optional[queue.Queue] = None
worker_thread: Optional[threading.Thread] = None
ping_thread: Optional[threading.Thread] = None
update_check_thread: Optional[threading.Thread] = None
stop_event = threading.Event()
ping_event = threading.Event()
update_check_event = threading.Event()
update_check_performed = False
prefs_frame: Optional[object] = None
last_connection_status = 'disconnected'
last_connection_message = 'Active'
current_cmdr = ''
countdown_seconds = 10


def plugin_start3(plugin_dir: str) -> str:
    """
    Initialize the plugin when EDMarketConnector starts.
    
    Args:
        plugin_dir: The directory containing this plugin
        
    Returns:
        The plugin's internal name
    """
    global send_queue, worker_thread, ping_thread, stop_event, ping_event, update_check_performed
    
    logger.info(f'EDSpec plugin starting from {plugin_dir}')
    
    send_queue = queue.Queue()
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


def plugin_prefs(parent: nb.Notebook, cmdr: str, is_beta: bool) -> nb.Frame:
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
    
    frame = nb.Frame(parent)
    frame.columnconfigure(1, weight=1)
    
    # Title - EDSpec styled
    title_label = nb.Label(frame, text='EDSpec', font=('Helvetica', 18, 'bold'))
    title_label.grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(15, 8), padx=(20, 5))
    
    # Subtitle
    nb.Label(frame, text='A Discord bot for Elite Dangerous!', font=('Helvetica', 10)).grid(
        row=1, column=0, columnspan=2, sticky=tk.W, pady=(0, 25), padx=(20, 5)
    )
    
    # Description section
    desc_text = (
        'Connect your Elite Dangerous game data to the EDSpec Discord bot.\n\n'
        'This integration automatically shares your commander data including:\n'
        '• Your current location and system\n'
        '• Station information when docked\n'
        '• Active ship details\n'
        '• Credit balance\n'
        '• On-foot, docked, or undocked status\n\n'
        'Your data is sent in real-time when game events occur like FSD jumps,\n'
        'docking, or loadout changes.'
    )
    nb.Label(frame, text=desc_text, justify=tk.LEFT, wraplength=550, font=('Helvetica', 9)).grid(
        row=2, column=0, columnspan=2, sticky=tk.W, pady=(0, 20), padx=(20, 5)
    )
    
    # Enable checkbox
    enabled_var = tk.BooleanVar(value=config.get(ENABLED_SETTING) if config.get(ENABLED_SETTING) is not None else True)
    nb.Checkbutton(frame, text='Enable EDSpec integration', variable=enabled_var).grid(
        row=3, column=0, columnspan=2, sticky=tk.W, pady=10, padx=(20, 5)
    )
    
    # Privacy options section
    nb.Label(frame, text='Privacy Options:', font=('Helvetica', 10)).grid(
        row=4, column=0, columnspan=2, sticky=tk.W, pady=(20, 5), padx=(20, 5)
    )
    
    send_ship_info_var = tk.BooleanVar(value=config.get(SEND_SHIP_INFO_SETTING) if config.get(SEND_SHIP_INFO_SETTING) is not None else True)
    nb.Checkbutton(frame, text='Share additional data (ship, credits, on-foot status)', variable=send_ship_info_var).grid(
        row=5, column=0, columnspan=2, sticky=tk.W, pady=5, padx=(40, 5)
    )
    
    nb.Label(frame, text='Note: System and station information is always shared', 
             justify=tk.LEFT, wraplength=520, font=('Helvetica', 8), foreground='gray').grid(
        row=6, column=0, columnspan=2, sticky=tk.W, pady=(0, 15), padx=(40, 5)
    )
    
    # Update check section
    nb.Label(frame, text='Updates:', font=('Helvetica', 10)).grid(
        row=7, column=0, columnspan=2, sticky=tk.W, pady=(20, 5), padx=(20, 5)
    )
    
    check_updates_var = tk.BooleanVar(value=config.get(CHECK_UPDATES_SETTING) if config.get(CHECK_UPDATES_SETTING) is not None else True)
    nb.Checkbutton(frame, text='Check for updates on startup', variable=check_updates_var).grid(
        row=8, column=0, columnspan=2, sticky=tk.W, pady=5, padx=(40, 5)
    )
    
    current_version_label = nb.Label(frame, text=f'Current version: {PLUGIN_VERSION}', 
                                     font=('Helvetica', 8), foreground='gray')
    current_version_label.grid(row=9, column=0, columnspan=2, sticky=tk.W, pady=(0, 15), padx=(40, 5))
    
    # API Key section
    api_key_label = nb.Label(frame, text='API Key:')
    api_key_label.grid(row=10, column=0, sticky=tk.W, pady=8, padx=(20, 5))
    api_key_var = tk.StringVar(value=config.get(API_KEY_SETTING) if config.get(API_KEY_SETTING) else '')
    api_key_entry = nb.Entry(frame, textvariable=api_key_var, width=50, show='*', font=('Helvetica', 9))
    api_key_entry.grid(row=10, column=1, sticky=tk.W+tk.E, pady=8, padx=5)
    
    # Get API Key help text
    get_key_text = 'Get your API key from https://edspecbot.com'
    get_key_label = nb.Label(frame, text=get_key_text, cursor='hand2', underline=18)
    get_key_label.grid(
        row=11, column=0, columnspan=2, sticky=tk.W, pady=(0, 10), padx=(20, 5)
    )
    # Configure blue color and make the link clickable
    get_key_label.config(foreground='#0000FF')  # Explicit blue hex color
    get_key_label.bind('<Button-1>', lambda e: webbrowser.open('https://edspecbot.com'))
    
    # Connection test section
    test_result_var = tk.StringVar(value='')
    test_result_label = nb.Label(frame, textvariable=test_result_var, wraplength=500, font=('Helvetica', 9))
    test_result_label.grid(row=12, column=0, columnspan=2, sticky=tk.W, pady=5, padx=(20, 5))
    
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
        
        # Get the current value from the UI
        current_api_key = api_key_var.get()
        
        def do_test():
            try:
                api_url = DEFAULT_API_URL
                api_key = current_api_key
                
                if not api_key:
                    test_result_var.set('❌ No API key configured')
                    return
                
                session = timeout_session.new_session()
                headers = {
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                    'User-Agent': f'{appname}/{plugin_name}'
                }
                
                data = {'connected': True, 'test': True}
                response = session.post(api_url, json=data, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    test_result_var.set('✅ Connection successful!')
                elif response.status_code == 401:
                    test_result_var.set('❌ Authentication failed - check your API key')
                else:
                    test_result_var.set(f'❌ Unexpected response: {response.status_code}')
                    
            except Exception as e:
                error_msg = str(e)
                if 'getaddrinfo failed' in error_msg or 'NameResolutionError' in error_msg:
                    test_result_var.set('❌ Failed to connect - check if the server is running')
                elif 'Connection refused' in error_msg:
                    test_result_var.set('❌ Connection refused - server may be down')
                else:
                    test_result_var.set(f'❌ Error: {error_msg[:60]}')
        
        # Run test in a thread to avoid blocking UI
        threading.Thread(target=do_test, daemon=True).start()
    
    # Set the command after function is defined
    test_button.config(command=test_connection)
    test_button.grid(row=13, column=0, columnspan=2, sticky=tk.W, pady=5, padx=(20, 5))
    
    # Store references for prefs_changed
    frame.enabled_var = enabled_var
    frame.api_key_var = api_key_var
    frame.send_ship_info_var = send_ship_info_var
    frame.check_updates_var = check_updates_var
    
    # Store the frame globally so prefs_changed can access it
    prefs_frame = frame
    
    return frame


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
    global status_label, last_connection_status, last_connection_message, current_cmdr
    
    if not status_label:
        return
    
    try:
        api_key = config.get(API_KEY_SETTING) if config.get(API_KEY_SETTING) else ''
        enabled = config.get(ENABLED_SETTING) if config.get(ENABLED_SETTING) is not None else True
        
        if not api_key:
            status_label['text'] = 'Not configured'
            status_label['foreground'] = 'gray'
        elif not enabled:
            status_label['text'] = 'Disabled'
            status_label['foreground'] = 'orange'
        else:
            # Use last known connection status
            if last_connection_status == 'success':
                status_label['text'] = 'In Sync'
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
            else:  # disconnected or unknown
                global countdown_seconds
                if countdown_seconds > 0:
                    status_label['text'] = f'Disconnected ({countdown_seconds}s)'
                else:
                    status_label['text'] = 'Disconnected'
                status_label['foreground'] = 'red'
            
    except Exception as e:
        logger.exception('Error updating status')
        status_label['text'] = 'Error'
        status_label['foreground'] = 'red'


def worker_thread_loop() -> None:
    """
    Worker thread that sends data to the EDSpec API.
    This thread runs in the background and processes items from the queue.
    """
    session = timeout_session.new_session()
    
    while not stop_event.is_set():
        try:
            # Try to get data from queue (with timeout)
            try:
                data = send_queue.get(timeout=1)
            except queue.Empty:
                continue
            
            # Check if we're enabled
            enabled = config.get(ENABLED_SETTING) if config.get(ENABLED_SETTING) is not None else True
            if not enabled:
                logger.debug('Plugin disabled, skipping send')
                continue
            
            # Get configuration
            api_url = DEFAULT_API_URL
            api_key = config.get(API_KEY_SETTING) if config.get(API_KEY_SETTING) else ''
            
            if not api_key:
                logger.warning('No API key configured')
                continue
            
            # Prepare headers
            headers = {
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'User-Agent': f'{appname}/{plugin_name}'
            }
            
            # Send data to API
            try:
                response = session.post(api_url, json=data, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    logger.debug('Successfully sent data to EDSpec')
                    update_status_with_color('green')
                elif response.status_code == 401:
                    logger.warning('Authentication failed - check your API key')
                    update_status_with_color('red', auth_failed=True)
                else:
                    logger.warning(f'Unexpected response from EDSpec: {response.status_code}')
                    update_status_with_color('orange')
                    
            except Exception as e:
                logger.error(f'Failed to send data to EDSpec: {e}')
                update_status_with_color('red')
            
            # Mark task as done
            send_queue.task_done()
            
        except Exception as e:
            logger.exception('Error in worker thread loop')
            time.sleep(1)


def update_status_with_color(color: str, auth_failed: bool = False) -> None:
    """
    Update status based on connection result from worker thread.
    
    Args:
        color: Color result ('green', 'red', 'orange', etc.)
        auth_failed: Whether the failure was due to authentication
    """
    global last_connection_status
    
    if color == 'green':
        last_connection_status = 'success'
    elif auth_failed:
        last_connection_status = 'auth_failed'
    else:
        last_connection_status = 'failed'
    
    # Trigger UI update on main thread
    update_status()


def send_connection_ping(connected: bool = True) -> None:
    """
    Send a connection status ping to the API.
    
    Args:
        connected: True for connected, False for disconnected
    """
    try:
        enabled = config.get(ENABLED_SETTING) if config.get(ENABLED_SETTING) is not None else True
        if not enabled:
            return
        
        api_url = DEFAULT_API_URL
        api_key = config.get(API_KEY_SETTING) if config.get(API_KEY_SETTING) else ''
        
        if not api_key:
            return
        
        session = timeout_session.new_session()
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'User-Agent': f'{appname}/{plugin_name}'
        }
        
        data = {'connected': connected}
        
        response = session.post(api_url, json=data, headers=headers, timeout=10)
        
        if response.status_code == 200:
            logger.debug(f'Sent connection status to EDSpec: connected={connected}')
            if connected:
                update_status_with_color('green')
        elif response.status_code == 401:
            logger.warning(f'Failed to send connection status: authentication failed')
            update_status_with_color('red', auth_failed=True)
        else:
            logger.warning(f'Failed to send connection status: {response.status_code}')
            
    except Exception as e:
        logger.debug(f'Failed to send connection status: {e}')
        update_status_with_color('red')


def send_disconnect_message() -> None:
    """
    Send a disconnect message to the API when shutting down.
    """
    send_connection_ping(connected=False)
    logger.info('Sent disconnect message to EDSpec')


def ping_thread_loop() -> None:
    """
    Background thread that sends periodic ping messages to keep the connection alive.
    Sends initial connection status and then pings every 30 seconds.
    """
    global last_connection_status, countdown_seconds, status_label
    
    # Show countdown timer from 10 to 1 seconds
    for remaining in range(10, 0, -1):
        if status_label:
            try:
                # Get root window to schedule updates on main thread
                root = status_label.winfo_toplevel()
                countdown_seconds = remaining
                root.after(0, update_status)
            except:
                pass
        time.sleep(1)
    
    # Reset countdown
    countdown_seconds = 10
    
    # Update status to "Connecting..." before attempting connection
    last_connection_status = 'connecting'
    if status_label:
        try:
            root = status_label.winfo_toplevel()
            root.after(0, update_status)
        except:
            update_status()
    else:
        update_status()
    
    # Send initial connection status
    send_connection_ping(connected=True)
    
    # Now ping every 30 seconds
    while not ping_event.wait(30):
        send_connection_ping(connected=True)


def check_for_updates_delayed() -> None:
    # Check for updates after startup, only runs once
    global update_check_performed
    
    if update_check_performed:
        return
    
    logger.info('Starting update check...')
    time.sleep(5)  # Give UI time to load
    
    check_updates = config.get(CHECK_UPDATES_SETTING) if config.get(CHECK_UPDATES_SETTING) is not None else True
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
        
        if is_newer_version(latest_version, PLUGIN_VERSION):
            logger.info(f'Update available: {PLUGIN_VERSION} -> {latest_version}')
            if status_label:
                try:
                    root = status_label.winfo_toplevel()
                    root.after(0, lambda: show_update_dialog(latest_version))
                except Exception as e:
                    logger.warning(f'Failed to schedule dialog: {e}')
                    show_update_dialog(latest_version)
            else:
                show_update_dialog(latest_version)
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
    # Show update notification dialog
    try:
        root = None
        if status_label:
            try:
                root = status_label.winfo_toplevel()
            except:
                pass
        
        if not root:
            try:
                root = tk._default_root
            except:
                pass
        
        if not root:
            return
        
        message = (
            f'A new version of the EDSpec plugin is available!\n\n'
            f'Current version: {PLUGIN_VERSION}\n'
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
    global current_cmdr
    
    try:
        # Update commander name
        if cmdr:
            current_cmdr = cmdr
        
        # Only send on specific events to avoid spam
        events_to_send_on = ['FSDJump', 'Location', 'Docked', 'Undocked', 'Loadout', 'Embark', 'Disembark']
        
        if entry.get('event') not in events_to_send_on:
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
        
        # Get configuration
        send_ship_info = config.get(SEND_SHIP_INFO_SETTING) if config.get(SEND_SHIP_INFO_SETTING) is not None else True
        
        # Prepare simplified data to send (location is always sent)
        data = {
            'cmdr': cmdr,
            'system': system,
            'station': station
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
            data['credits'] = state.get('Credits', 0)
            data['status'] = status
        
        # Queue data for sending
        if send_queue and data:
            send_queue.put(data)
            logger.debug(f'Queued data for send: {entry.get("event")} - Ship info: {send_ship_info}')
        
    except Exception as e:
        logger.exception('Error in journal_entry')
        return f'EDSpec error: {str(e)}'
    
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
        # Get configuration
        send_ship_info = config.get(SEND_SHIP_INFO_SETTING) if config.get(SEND_SHIP_INFO_SETTING) is not None else True
        
        # Prepare simplified data to send
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
                
                # Get ship info if available
                if 'ships' in data and data['ships']:
                    # Find current ship
                    current_ship_id = data.get('currentShipId')
                    if current_ship_id and isinstance(data['ships'], list):
                        for ship in data['ships']:
                            if ship.get('id') == current_ship_id:
                                capi_data['ship'] = ship.get('name', 'Unknown')
                                break
        
        # Queue data for sending
        if send_queue and capi_data:
            send_queue.put(capi_data)
            logger.debug(f'Queued CAPI data for send - Ship info: {send_ship_info}')
        
    except Exception as e:
        logger.exception('Error in cmdr_data')

