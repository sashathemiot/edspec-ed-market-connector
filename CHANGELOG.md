# Changelog

## [1.0.2] - 2026-07-02

### Added

- In-game navigation route sync to EDSpec (`NavRoute`, `Route`, `FSDTarget`; cleared on `NavRouteClear` or when the route is completed)
- Route progress in the status bar, for example `In Sync · Sol (2/5)`
- FSS expiring signal forwarding from `FSSSignalDiscovered` (batched before jumps)
- Fuel level tracking and status display on `Refuel`, `RefuelAll`, `FuelScoop`, and jump events
- Cargo inventory sync from `Cargo` journal events
- Fleet ship tracking from `StoredShips` and Frontier CAPI data
- Ship loadout details from `Loadout` (modules, engineering, jump range, and related stats)
- Game session detection so data is sent only while Elite Dangerous is running
- `PLEASE START GAME` status when the plugin is configured but no commander session is active
- Configurable API URL in settings (default `https://api.edspecbot.com/edmc/ingest`)
- Route hop index included with location updates while following an active route
- `cmdr_data_legacy` hook for legacy Galaxy CAPI compatibility
- EDMC config API compatibility shims for versions before 5.0

### Changed

- Default API endpoint moved to `https://api.edspecbot.com/edmc/ingest`
- Privacy model updated: commander, system, station, fuel, and basic docked/undocked/on-foot status are always shared in game: ship, credits, cargo, fleet, and loadout remain optional
- Settings panel copy updated for galaxy map integration and clearer privacy notes
- Status indicators updated: removed startup countdown; `In Sync` can show route destination and fuel percentage
- Background HTTP reworked into a single worker for commander data, routes, pings, and FSS signals
- Thread-safe UI updates via the EDMC main thread
- README updated for current status indicators and data events

---

## [1.0.1] - 2025-11-03

### Fixed

- Fixed EDSpec settings tab not appearing in EDMC settings dialog
- Fixed API key entry field compatibility issue (`nb.Entry` → `ttk.Entry`)
- Added `prefs_cmdr_changed` hook for EDMC compatibility
- Improved error handling to ensure settings tab always appears

---

## [1.0.0] - Initial Release

### Added

- Initial release of EDSpec plugin for EDMarketConnector
- Real-time commander data sharing with EDSpec Discord bot
- Settings panel for API key configuration
- Privacy options for controlling data sharing
- Connection testing functionality
- Automatic update checking
