# EDSpec Plugin for EDMarketConnector

Plugin for [EDMarketConnector](https://github.com/EDCD/EDMarketConnector) that syncs Elite Dangerous commander data to the [EDSpec](https://edspecbot.com) Discord bot service.

**Version**: 1.0.2 
**Developer**: sashathemiot

## Installation

### Step 1: Locate EDMarketConnector Plugins Directory

**Windows:**
- Press `Win + R` to open Run dialog
- Type: `%LOCALAPPDATA%\EDMarketConnector\plugins`
- Press Enter

**Mac:**
- Open Finder
- Press `Cmd + Shift + G` (or Go menu → Go to Folder)
- Type: `~/Library/Application Support/EDMarketConnector/plugins`
- Press Enter

**Linux:**
- Open terminal
- Run: `mkdir -p ~/.local/share/EDMarketConnector/plugins`
- Or if `$XDG_DATA_HOME` is set: `$XDG_DATA_HOME/EDMarketConnector/plugins`
- Navigate with: `cd ~/.local/share/EDMarketConnector/plugins`

### Step 2: Create Plugin Directory

1. In the plugins directory, create a new folder
2. Name it exactly: `EDSpec` (case-sensitive)
3. The full path should be:
   - Windows: `%LOCALAPPDATA%\EDMarketConnector\plugins\EDSpec\`
   - Mac: `~/Library/Application Support/EDMarketConnector/plugins/EDSpec/`
   - Linux: `~/.local/share/EDMarketConnector/plugins/EDSpec/`

### Step 3: Copy Plugin File

1. Copy the `load.py` file from this repository
2. Paste it into the `EDSpec` folder you just created
3. Verify the file structure:
   ```
   plugins/
     └── EDSpec/
         └── load.py
   ```

### Step 4: Restart EDMarketConnector

1. **Completely close** EDMarketConnector (not just minimize)
   - Check system tray/notification area if running
   - End process if needed
2. Launch EDMarketConnector again
3. The plugin should appear in the main window with status "Not configured"

### Step 5: Verify Installation

- Look for "EDSpec:" label in the main EDMarketConnector window
- Status should show "Not configured" (gray text)
- Check Settings → Plugins tab to confirm EDSpec is listed

## Configuration

1. Get API key from https://edspecbot.com (Discord login required)
2. Open EDMarketConnector Settings → EDSpec tab
3. Enter API key and configure privacy options
4. Use "Test Connection" button (10s cooldown)

## Status Indicators

- `Not configured` (gray) - No API key
- `Disabled` (orange) - Plugin disabled
- `PLEASE START GAME` (orange) - API key set, but Elite Dangerous is not running or no commander is loaded
- `Disconnected` (red) - Configured and enabled, but not yet connected to EDSpec
- `In Sync` (green) - Connected and syncing. May include route or fuel details, for example `In Sync · Sol (2/5)` or `In Sync · 85% fuel`
- `API Key invalid` (red) - Authentication failed
- `Connection failed` (red) - Could not reach EDSpec
- `Error` (red) - Status display error (check EDMC log)

## Data Events

The plugin sends data only while you are in an active game session (after `LoadGame`).

### Commander location (ingest API)

Primary journal events: `FSDJump`, `Location`, `Docked`, `Undocked`, `Loadout`, `Embark`, `Disembark`

Additional events:

- **Fuel**: `Refuel`, `RefuelAll`, `FuelScoop` (fuel updates; optional ship/cargo/credits when privacy is on)
- **Cargo**: `Cargo` (optional, privacy setting)
- **Fleet**: `StoredShips` (optional, privacy setting)
- **Frontier CAPI**: commander data when available

**Always shared** while in game:

- Commander name, current system, station when docked (cleared on `Undocked`)
- Fuel level on jump, refuel, and fuel scoop events
- Docked, undocked, or on-foot status on `FSDJump`, `Docked`, and `Undocked` (even when optional data is off)

**Optional** (Settings → Share additional data):

- Active ship name, credits, cargo, fleet ships, loadout details
- On-foot, docked, or undocked status on all primary location events

Fuel warnings, route display, and map audio are configured in the galaxy map Settings menu on the EDSpec website, not in ED Market Connector.

### In-game route (active-route API)

Synced from journal events: `NavRoute`, `Route`, `FSDTarget`

Cleared on `NavRouteClear` or when the route is completed.

### FSS signals (ingest API)

From `FSSSignalDiscovered` for expiring signals only (`TimeRemaining` > 0). Signals are batched briefly and sent before the next jump.

## Requirements

- EDMarketConnector 4.0.0+
- EDSpec API key (Discord account)

## License

Provided as-is for use with EDMarketConnector and EDSpec.
