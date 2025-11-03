# EDSpec Plugin for EDMarketConnector

Plugin for [EDMarketConnector](https://github.com/EDCD/EDMarketConnector) that syncs Elite Dangerous commander data to the [EDSpec](https://edspecbot.com) Discord bot service.

**Version**: 1.0.0  
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
- `Disconnected (Xs)` (red) - Startup countdown (10s)
- `Connecting...` (orange) - Connection attempt
- `In Sync` (green) - Connected and syncing
- `API Key invalid` (red) - Auth failed
- `Connection failed` (red) - Connection error

## Data Events

Sends data on journal events: `FSDJump`, `Location`, `Docked`, `Undocked`, `Loadout`, `Embark`, `Disembark`

**Always sent**: Commander name, system, station  
**Optional** (privacy setting): Ship, credits, status

## Requirements

- EDMarketConnector 4.0.0+
- EDSpec API key (Discord account)

## License

Provided as-is for use with EDMarketConnector and EDSpec.
