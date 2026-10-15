# Updated

The core integration by exabird works well for single home installations, but there are some underlying problems with multiple Tado homes. As this integration no longer seems to be actively developed, and pull requests are not acted on, this branch is my attempt to create a working version for my own use. At present I only intend this to maintained as a separate integration, but any changes that are made will be documented here.

# Changes

## Show device serial number
The device serial number is now shown for each Tado device. Originally implemented by LeroyK https://github.com/exabird/ha-tado-x/pull/52 (thank you!)

## Corrected a depreciated device-linking method that gives a warning it will stop working in Home Assistant 2027.8.0
"'tado_x' calls device_registry.async_get_or_create with a deprecated via_device parameter; use via_device_id..."

- Fixed

## Open window processing
The Tado app shows the minutes remaining and time open window will end when open window is detected. These are now added as attributes to the climate device associated with the room

Adds Attributes
- open_window_ends_at
- open_window_remaining_minutes

## Hot water control
This builds on https://github.com/exabird/ha-tado-x/pull/53 by donalfenwick (Thank you!) to add hot water control. However, there are a couple of issues relating to use with multiple homes and the hot water mode display is not updated correctly if in Away mode. These are fixed.

Adds Controls:
- Boost Hot Water button
- Hot Water Mode select
- Resume Hot Water Schedule button
- Turn Off Hot Water button
Adds Sensors
- Hot water boost ends
- Hot water next schedule change


## Improved temperature control setting that works with multiple Tado homes

- Fixes set_climate_timer (and set_temperature_offset, add_meter_reading and set_eiq_tariff) so that they now work correctly if multiple homes are present. Previously they only functioned for the first home that is added.

- Adds termination_type to set_climate_timer to allow temperature changes that are either until you resume schedule, until the next time block, or for a defined period of time.

- Ensures that temperature changes introduced by standard Home Assistant thermostat controls use the Tado per-room manual control termination type setting and so work in the same way as when changing temperature via Tado devices. (No more “temperature change is always set for 30 min”.)

- Adds read/write access to the Tado per-room manual control termination type setting (needed to achieve the correct thermostat control)

### What has changed
There were several issues surrounding setting and controlling the temperature of each room via the current integration.
`set_climate_timer(…)`
In the original implementation, this will set the temperature but always sets this for a 30 min period. This is because the termination_type setting, which can be MANUAL, TIMER or NEXT_TIME_BLOCK is hard-coded to always be a 30 min TIMER. User xiic published an update that allows full control, and I have taken this code https://github.com/exabird/ha-tado-x/pull/54 (thank you!) and incorporated it as part of this update. In addition, the existing code limited the temperature range to 5-25C. I have expanded this to 5-30C.
This then allows setting Duration and Termination type for each call.

<img width="400" height="385" alt="image" src="https://github.com/user-attachments/assets/cc266a2a-aafe-48d1-ac6f-c039268ff594" />

However, there is a fundamental flaw in the original implementation, which is still present in the modification published by xiic https://github.com/exabird/ha-tado-x/pull/54 that means the call only works with either a single home, or just the first home in a multi-home environment. This is because the provided services are implemented in a way that means they are associated with just one coordinator, which is a local variable from the initial async_setup_entry call. What this means this that the Tado X specific calls only work with the first home added and not with a second or subsequent one.

I have therefore updated things so that the call works when more than one home is present, irrespective of which room in which home is selected, by ensuring that the correct coordinator is used.
Although not strictly to do with temperature setting, this limitation to a single home was, for the same reason, also found to affect the following routines:

- set_temperature_offset
- add_meter_reading
- set_eiq_tariff

I have therefore also corrected these to ensure that they too work when multiple homes are present.

The next issue with setting temperatures is that when standard HA controls for thermostats are used with the Tado X rooms, any change is made in such a way that the new temperature is always set as a TIMER for 30 mins. After that time, the room will revert to schedule. This is unlike the way that temperature changes are handled directly via Tado X radiator or thermostat controls, where the app defines a per room setting that determines whether any manual temperature change is for a fixed time, lasts until you resume schedule, or lasts until the next time block. The current Tado X implementation completely ignores these settings and hard-codes a 30 min TIMER. I have therefore modified the code to read the Tado settings, so that when the temperature is changed in HA via thermostat controls, the temperature is set according to the individual Tado room settings—the behaviour is now the same whether the temperature is altered via a Tado device or via Home Assistant.

Finally, as the state of the manual control termination type, which is visible via the app, was not being reported and could not be set from HA, I have added additional controls that can be read/written from HA. These show up on each Tado room when viewed in Devices:
<img width="322" height="219" alt="image" src="https://github.com/user-attachments/assets/0b74e885-c7f4-4279-9933-0c33b27a2199" />

These correspond directly to the settings shown in the Tado app:

<img width="360" height="251" alt="image" src="https://github.com/user-attachments/assets/15b5f6b2-f07d-40e1-a6f4-18499f954f16" />

The Termination Type is a select option, by default:
`select.##ROOM##_termination_type`

The Timer Duration is a number, by default:
`number.##ROOM##_timer_duration`

Each of these can be read or written within scripts/automations as required. It is therefore possible to update this setting from HA as appropriate.

Timer Duration is only relevant for the type ‘Timer’. When setting a timer it is recommended first to set the Timer Duration and then set the Termination Type.

As an aside, the Tado app does not seem to refresh the display of these values when they change, but reloading the page will show when they have been updated.

### Example use: setting an unused room to a low temperature
My own main use case for these changes is so that it is more easily possible to set a spare bedroom to a low temperature when not in use, without editing the normal schedule, then restore normal operation when guests are present. Simply turning the room off is not an appropriate solution as that sets the room to frost protection (5C), which is too cold to provide suitable protection against damp.

To set SPARE_ROOM to a background temperature:

1. Call set_climate_timer for the required room with Termination Type set to Manual and a low temperature selected. I generally use 14C to ensure that the room does not become damp. 

2. Set select.SPARE_ROOM_termination_timer to Manual to ensure that even if the temperature setting is altered manually, the room will not revert to schedule.

When you need to restore the default schedule for that room:

1. Restore select.SPARE_ROOM_termination_timer to your normal usage (for example, Next Time Block or Timer for 30 min)

2. Resume auto function for the room by ‘Set thermostat HVAC mode’ to Auto

### Example use: lower fallback temperature during Summer
I also use the same route as above to set a lower background temperature for rooms that applies over Summer, which again allows my normal temperature schedule to remain in place. What works for me is to set rooms at 17.5C overnight and 18.5C during the day, meaning that any really cold spells get heat supplied, but this is a rare fallback. Importantly, it means that I do not have to continually edit the schedule in order to back off the heating but can easily restore the schedule when the weather gets colder. Setting the room-specific termination timer to Manual while the low temperature mode is active is important as otherwise any manual change to temperature via a Tado device will revert things back to schedule once the timer is up or the next schedule change begins. Restoring the setting when you cancel the low temperature mode will then resume normal activity. I use the same settings for each room during normal scheduling, but if you use specific ones then you will need to store the old values before setting the termination type to Manual, then restore these when you resume normal scheduling if you do not wish to hard-code this.



# Tado X Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/exabird/ha-tado-x)](https://github.com/exabird/ha-tado-x/releases)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy%20Me%20A%20Coffee-Support-yellow?style=flat&logo=buy-me-a-coffee)](https://buymeacoffee.com/exabird)

A Home Assistant custom integration for **Tado X** devices (the new generation of Tado smart thermostats and radiator valves).

> **Note:** This integration is specifically designed for Tado X devices. For older Tado devices (V3+ and earlier), use the [official Tado integration](https://www.home-assistant.io/integrations/tado/).

---

## Quick Installation (HACS)

### Step 1: Add the repository
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=exabird&repository=ha-tado-x&category=integration)

**Or manually:**
1. Open HACS in Home Assistant
2. Click **⋮** (top right) → **Custom repositories**
3. Add `https://github.com/exabird/ha-tado-x` as **Integration**

### Step 2: Install & Restart
1. Search "Tado X" in HACS → **Download**
2. **Restart Home Assistant**

### Step 3: Configure
1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search "Tado X" and follow the authentication flow

---

## Features

| Entity Type | Features |
|-------------|----------|
| **Climate** | Temperature control, HVAC modes (Heat/Off/Auto), Preset modes (Schedule/Boost/Home/Away/Auto) |
| **Sensors** | Temperature, Humidity, Heating power, Battery status, **API usage monitoring**, **Weather** (outdoor temp, solar intensity), **Air comfort** (freshness, comfort level), **Heating time today** |
| **Binary Sensors** | Window open, Heating active, Manual control, Connectivity, Low battery |
| **Switches** | **Child lock** (per device), **Open window** control (per room) |
| **Buttons** | **Boost All**, **Turn Off All**, **Resume Schedules** (quick actions) |
| **Device Tracker** | **Mobile devices** for geofencing (home/away status) |
| **Services** | Temperature offset, Meter reading, **Energy tariff** (Energy IQ) |

### Climate Presets

- **Schedule**: Follow the smart schedule
- **Boost**: Quick temperature boost
- **Home**: Manually set presence to Home (override geofencing)
- **Away**: Manually set presence to Away (override geofencing)
- **Auto (Geofencing)**: Enable automatic presence detection

### Quick Actions (Buttons)

Home-level controls for managing all zones at once:
- **Boost All**: Activate boost mode on all heating zones simultaneously
- **Turn Off All**: Disable heating in all zones (useful when leaving)
- **Resume Schedules**: Cancel all manual overrides and return to smart schedules

### Services

- **set_temperature_offset**: Calibrate device temperature readings (-9.9°C to +9.9°C)
- **add_meter_reading**: Upload meter readings to Tado Energy IQ
- **set_eiq_tariff**: Set energy tariffs for cost calculations in Energy IQ
  - Supports both m³ (gas) and kWh (electricity) units
  - Define tariff periods with start/end dates
  - Enables accurate cost tracking in Tado's Energy IQ dashboard

### API Usage Monitoring

Five sensors help you track API quota usage (values read directly from Tado API headers when available):
- **API calls today**: Number of API requests since midnight
- **API quota remaining**: Remaining requests
- **API quota limit**: Your daily quota (auto-detected from API: 100 free, 20,000 with Auto-Assist)
- **API usage**: Percentage of daily quota used
- **API reset time**: When the quota resets

These sensors use **real values from Tado API response headers** when available, providing accurate quota tracking.

## Supported Devices

| Model | Device |
|-------|--------|
| VA04 | Radiator Valve X |
| SU04 | Temperature Sensor X |
| TR04 | Thermostat X |
| IB02 | Bridge X |

## API Rate Limits & Smart Polling

Tado enforces daily API limits. This integration **automatically adapts** its polling interval based on your subscription:

| Subscription | Daily Limit | Auto Polling Interval | Actual Usage |
|--------------|-------------|----------------------|--------------|
| Without Auto-Assist | 5000 requests/day | Every 45 minutes | ~96 req/day ✅ |
| With Auto-Assist | 20,000 requests/day | Every 30 seconds | ~2,880 req/day ✅ |

### Configuration Options

Go to **Settings** → **Devices & Services** → **Tado X** → **⚙️ Configure** to:

- **Enable Auto-Assist**: Toggle if you have an Auto-Assist subscription
- **Custom polling interval**: Override the automatic interval (30s - 3600s)
- **Enable weather sensors**: Outdoor temperature, solar intensity, weather state (+1 API call)
- **Enable mobile device tracking**: Device tracker for geofencing (+1 API call)
- **Enable air comfort sensors**: Air freshness and comfort level per room (+1 API call)
- **Enable heating time sensors**: Daily heating runtime per room (+1 API call)

**Note:** Optional features are disabled by default for free tier users to optimize API usage. Auto-Assist users have all features enabled by default.

### API Counter Persistence

The API call counter is **persisted across restarts** - your quota tracking continues accurately even after Home Assistant reboots.

## Troubleshooting

**Authentication issues:** Go to Settings → Devices & Services → Tado X → **⋮** → Reconfigure

**Rate limiting:** If entities become unavailable, you may have exceeded the daily limit. Wait until the next day or get Auto-Assist subscription.

## Manual Installation

1. Download `custom_components/tado_x` from this repository
2. Copy to your `config/custom_components/` directory
3. Restart Home Assistant

---

## Contributing

This is a community-maintained integration for Tado X devices. Contributions are welcome!

### Reporting Issues

Found a bug or have a feature request? Please use our issue templates:
- [Report a Bug](https://github.com/exabird/ha-tado-x/issues/new?template=bug_report.md)
- [Request a Feature](https://github.com/exabird/ha-tado-x/issues/new?template=feature_request.md)

### Development

Want to contribute code? Great! Here's how to get started:

1. Fork this repository
2. Create a branch for your feature: `git checkout -b feature/my-feature`
3. Make your changes
4. Test thoroughly with your Tado X setup
5. Submit a pull request

### Roadmap

**Recently Implemented:**
- [x] Away preset (geofencing) - [Issue #2](https://github.com/exabird/ha-tado-x/issues/2) ✅ v1.1.0
- [x] Temperature offset adjustment service - [Issue #3](https://github.com/exabird/ha-tado-x/issues/3) ✅ v1.1.0
- [x] API usage monitoring sensors - [Issue #4](https://github.com/exabird/ha-tado-x/issues/4) ✅ v1.3.0
- [x] Meter reading service (Energy IQ) - [Issue #5](https://github.com/exabird/ha-tado-x/issues/5) ✅ v1.2.0
- [x] Smart polling based on subscription tier - [Issue #4](https://github.com/exabird/ha-tado-x/issues/4) ✅ v1.3.2
- [x] API counter persistence across restarts ✅ v1.3.2
- [x] Configurable polling interval ✅ v1.3.2
- [x] Real API quota values from response headers ✅ v1.3.3
- [x] Fix meter reading API endpoint - [Issue #7](https://github.com/exabird/ha-tado-x/issues/7) ✅ v1.3.4
- [x] Open window detection toggle ✅ v1.4.0
- [x] Child lock control ✅ v1.4.0
- [x] Quick actions (Boost All, Turn Off All, Resume Schedules) ✅ v1.5.0
- [x] Energy IQ tariff management ✅ v1.5.0
- [x] Weather sensors (outdoor temp, solar intensity, weather state) ✅ v1.6.0
- [x] Air comfort sensors (freshness, comfort level per room) ✅ v1.6.0
- [x] Mobile device tracking (geofencing home/away) ✅ v1.6.0
- [x] Heating time sensors (daily runtime per room) ✅ v1.6.0
- [x] Configurable feature toggles to optimize API usage ✅ v1.6.0

**Planned features:**

| Feature | Description | Priority |
|---------|-------------|----------|
| **Historic Data** | Historical temperature, humidity, and heating data | Medium |
| **Schedule Management** | Read and modify heating schedules from Home Assistant | Medium |
| **Flow Temperature Optimization** | Boiler flow temperature control for energy savings | Low |
| **Away Radius Configuration** | Configure geofencing radius for presence detection | Low |

See [all feature requests](https://github.com/exabird/ha-tado-x/issues?q=is%3Aissue+is%3Aopen+label%3Aenhancement) for the full list and to vote on priorities.

## Why This Integration Exists

Tado X devices use a different API than previous Tado generations. While Tado recommends using Matter integration, many users experience significant issues with Matter setup:

- **Greyed-out pairing buttons** preventing Matter device linking
- **Failed pairing attempts** with cryptic error messages
- **Tado's official solution** requires a [full device reset](https://help.tado.com/en/articles/10264695-how-can-i-fix-matter-device-linking-errors) - impractical for remote installations (vacation homes, rental properties, elderly parents' homes)

### Why Not an Official Home Assistant Integration?

The Home Assistant team has chosen not to add Tado X support to the official Tado integration. Based on community discussions, this decision was made at Tado's request - they don't want third-party integrations using their cloud API because "it costs them money".

But here's the thing: **users pay for this API access**. We buy Tado devices knowing an API exists. Many of us pay an additional Auto-Assist subscription specifically for extended API access. So why restrict how we use what we're already paying for?

The only official alternative is Matter, which is extremely limited compared to Tado's own app:
- No boost mode or presets
- No temperature offset calibration
- No Energy IQ or meter readings
- No detailed heating statistics
- No child lock controls

And unlike most smart home manufacturers, Tado provides **no local API** - neither on their classic devices nor on the new Tado X line. This leaves cloud API as the only way to get full functionality.

**This is why ha-tado-x exists.** This integration uses Tado's official cloud API to provide what paying customers deserve: full Home Assistant integration for Tado X users who:
- Cannot get Matter working (greyed-out buttons, failed pairing)
- Have remote installations where device resets aren't feasible
- Need features that Matter simply doesn't support
- Want to actually use the API they're paying for

## Support the Project

If you find this integration useful and it saved you time or solved your Tado X / Home Assistant integration challenges, consider supporting its continued development!

<a href="https://buymeacoffee.com/exabird" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="50">
</a>

Your support helps me:
- Dedicate time to maintaining and improving this integration
- Respond quickly to bug reports and feature requests
- Keep the project compatible with Home Assistant updates
- Add new features from the roadmap

Every coffee fuels another contribution to the community!

## Support

- **Community Forum:** Discuss on [Home Assistant Community](https://community.home-assistant.io/)
- **Issues:** Report bugs or request features on [GitHub Issues](https://github.com/exabird/ha-tado-x/issues)
- **Hardware Support:** For Tado device issues, contact [Tado Support](https://support.tado.com/)

## License

MIT License - see [LICENSE](LICENSE) file for details.
