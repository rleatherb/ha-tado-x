


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