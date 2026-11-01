# EDSpec Plugin for EDMarketConnector

Plugin for [EDMarketConnector](https://github.com/EDCD/EDMarketConnector) that syncs Elite Dangerous commander data to the [EDSpec](https://edspecbot.com) Discord bot service.

**Version**: 1.1.0 
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

### Step 3: Install Plugin Files

**From a release zip (recommended):**

1. Download `EDSpec-1.1.0.zip` from GitHub Releases
2. Extract so you have an `EDSpec` folder
3. Move that folder into the EDMarketConnector `plugins` directory

**From source:**

1. Create the `EDSpec` folder in the plugins directory
2. Copy at least `load.py` and `LICENSE` into it
3. Verify the file structure:
   ```
   plugins/
     └── EDSpec/
         ├── load.py
         └── LICENSE
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

1. Get an API key from https://edspecbot.com (Discord login required)
2. Open EDMarketConnector Settings, then the EDSpec tab
3. Turn the plugin on, choose what to share, and enter the API key
4. Use Test connection

## Status Indicators

- `Not configured` (gray) - No API key
- `Disabled` (orange) - Plugin disabled
- `Please join a game mode` (orange) - API key set, but Elite Dangerous is not running or no commander is loaded
- `Disconnected` (red) - Configured and enabled, but not yet connected to EDSpec
- `In Sync` (green) - Connected and syncing. May include route or fuel details, for example `In Sync · Sol (2/5)` or `In Sync · 85% fuel`
- `API Key invalid` (red) - Authentication failed
- `Connection failed` (red) - Could not reach EDSpec
- `Error` (red) - Status display error (check EDMC log)

## Data Events

The plugin sends data only while you are in an active game session (after `LoadGame`).

### Commander location (ingest API)

Primary journal events: `FSDJump`, `CarrierJump`, `Location`, `Docked`, `Undocked`, `Loadout`, `Embark`, `Disembark`

Additional events:

- **Fuel**: live tank level while you are in your ship, plus `Refuel`, `RefuelAll`, and `FuelScoop`
- **Cargo**: `Cargo` (optional)
- **Fleet**: `StoredShips` (optional)
- **Frontier CAPI**: commander data when available

**Always shared** while in game:

- Commander name, current system, station when docked (cleared on `Undocked`)
- Fuel level while you fly, scoop, or jump
- Docked, undocked, or on-foot status on location updates (`FSDJump`, `CarrierJump`, `Location`, `Docked`, `Undocked`, `Embark`, `Disembark`)

**Optional** (Share ship, credits, Merc coins, cargo and fleet):

- Active ship name, credits, Merc coins, cargo, fleet ships, and loadout details

Fuel warnings, route display, and map audio are configured in the galaxy map Settings menu on the EDSpec website, not in ED Market Connector.

### In-game route (active-route API)

Synced from journal events: `NavRoute`, `Route`, `FSDTarget`

Cleared on `NavRouteClear` or when the route is completed.

### FSS scans (ingest API)

Body scans, mapped bodies, discovery progress, and a finished system scan (`FSSAllBodiesFound`) are sent. Expiring FSS signals are not shared.

## Requirements

- EDMarketConnector **6.1.0+** (required for the EDMC Plugin Browser and this plugin)
- EDSpec API key from https://edspecbot.com (Discord account)

## License

This plugin is free software under the [GNU General Public License v3.0](LICENSE) (GPL-3.0).

You may redistribute and/or modify it under the terms of the GPL as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
