# Solar-surplus control for the pool heat pump

Switches the Zodiac Z550iQ on when the house has real PV surplus and off when it
starts importing from the grid or draining the battery. It replaces the habit of
switching the heater on because it looks sunny, which over the 15 Jul – 15 Aug
baseline left about a third of the heater's 12.9 kWh/day running with no solar
behind it.

It runs as a scheduled GitHub Actions workflow in this repository. There is no
hardware at the house to maintain and nothing to keep running on a laptop.

```
Solar Manager cloud  ──►  decide  ──►  iAquaLink cloud  ──►  Z550iQ
   (every 5 min)                        (only when needed)
```

## How it decides

Each cycle reads the house from Solar Manager and computes

```
surplus = grid_export + battery_charging
```

Grid export is hard surplus. Power going into the battery is soft surplus: it is
available to the heater at the cost of charging the battery more slowly.

**Using soft surplus never imports from the grid**, and that is not a hope — it
follows from one comparison. If the battery is charging at 7 kW and the heater
draws 2 kW, the battery charges at 5 kW instead; nothing is drawn from the grid.
Import can only happen if the heater consumes more than the surplus that was
there, so `ON_THRESHOLD` must stay at or above `HEATER_DRAW_W`. Configuring it
lower is refused at startup rather than discovered on the meter.

* **Start** when the surplus has held above `ON_THRESHOLD` for `ON_DELAY`.
* **Stop** when the house is importing above `IMPORT_THRESHOLD`, or the battery
  is discharging above `DISCHARGE_THRESHOLD` while below `SOC_FLOOR`, for
  `OFF_DELAY` — but never before `MIN_RUN` has elapsed.

On top of that sit rails that no reading can talk it out of:

| Rail | What it does |
|---|---|
| Season | Outside `SEASON_START`–`SEASON_END` no ON command exists at all |
| Hard-off window | Never runs between `HARD_OFF_START` and `HARD_OFF_END` |
| Switching budget | At most `MAX_SWITCHES_PER_DAY` starts; the closing OFF is always allowed |
| Compressor minimum | No run shorter than `MIN_RUN`, no restart within `MIN_OFF` |
| Start verification | A start that does not take is switched off and alerted, not left commanded-on |
| Fail-safe | If either API is unreachable, send OFF once and alert |
| Car priority | While the Easee pulls over `CAR_ACTIVE_W`, the start threshold rises |

Two rails could contradict each other at the end of the day: the 20:00 ceiling
wants the heater off, the compressor minimum wants a run finished. Rather than
picking a winner, the logic refuses to *start* a run the ceiling would have to
cut short, so the conflict never arises.

## Setting it up

### 1. Credentials

**Solar Manager.** Log in to <https://web.solar-manager.ch/> and create a Cloud
API key under *Profile → Cloud API key* (*Profil → Cloud-API-Schlüssel*), with at
least the `read` scope. Nothing here writes to Solar Manager — the heater is
controlled through iAquaLink — so `read` is enough. You also need your SM ID, the
gateway id shown in the portal.

Reach the key from your **account** menu, not from the gateway page. The portal
has API settings in both places and they are different things: the account one
offers scope checkboxes and issues a key for `cloud.solar-manager.ch`, while the
gateway one configures the *local* API served on your home network and mentions
letting "any device" make requests to it. A local key is useless here, because a
GitHub Actions runner cannot reach your home network.

If your profile has no Cloud API key entry at all, your account predates them.
Use `SOLAR_MANAGER_EMAIL` and `SOLAR_MANAGER_PASSWORD` instead — that login works
until 30 June 2027 and this project supports either. Emailing
<support@solar-manager.ch> is the way to get a key issued if you want one.

**iAquaLink.** The same email and password you use in the app. You also need the
heat pump's serial number — leave `ZODIAC_SERIAL` unset and run the probe below,
and it will list the devices on your account.

**Telegram.** Message [@BotFather](https://t.me/botfather), send `/newbot`, and
keep the token. Then message your new bot once and open
`https://api.telegram.org/bot<TOKEN>/getUpdates` to find your numeric chat id.

### 2. Repository secrets

*Settings → Secrets and variables → Actions → Secrets*:

| Secret | Needed |
|---|---|
| `SOLAR_MANAGER_API_KEY` | yes (or the email/password pair below) |
| `SOLAR_MANAGER_EMAIL`, `SOLAR_MANAGER_PASSWORD` | only without an API key |
| `SOLAR_MANAGER_SM_ID` | yes |
| `ZODIAC_EMAIL`, `ZODIAC_PASSWORD`, `ZODIAC_SERIAL` | yes |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | for notifications |

### 3. Prove the two APIs work

Run the **Pool heater probe** workflow from the Actions tab.

* `target: solar` prints the live figures, the parsed surplus, and every device
  with its power — find the Easee there and set `SOLAR_MANAGER_CAR_DEVICE_ID`.
  It also prints **which authentication method your account accepted**; note it
  down, because that is the one fact about this API we could not establish
  without your credentials.
* `target: zodiac` prints the raw equipment block and the parsed state. With
  `ZODIAC_SERIAL` unset it lists your devices instead.
* `target: zodiac` with `send: on` / `off` / `boost` sends a real command, so you
  can confirm the app reflects it. This is the one place the probe touches
  hardware.

**Check the mode mapping while you are there.** `st: 0` is Boost and `st: 1` is
Silent across this API family, but the Z550iQ's third mode (Smart) is a guess.
Set the heater to each mode in the app, re-run the probe, and correct
`ZODIAC_MODE_BOOST` / `ZODIAC_MODE_ECOSILENCE` / `ZODIAC_MODE_SMART` if the `st`
values differ. Boost and off are all v1 needs, so this only matters if you turn
on the EcoSilence refinement.

### 4. Dry run for two or three days

`DRY_RUN` defaults to **true**, so enabling the schedule changes nothing at the
house. The workflow logs every decision it would have made and Telegram messages
are prefixed `[dry run]`. Compare a few days against Solar Manager's consumption
graphs, and check the switch-on lands in the export window.

Also verify `ON_THRESHOLD` here: 3000 W is the assumed electrical draw, but the
first real Boost run is what tells you the true figure. Watch consumption jump in
Solar Manager and set the threshold a little above it.

### 5. Go live

Set the repository variable `DRY_RUN` to `false`. To try a single live cycle
first, run the workflow manually with `mode: live`.

## Tuning

*Settings → Secrets and variables → Actions → Variables*. Leave a variable unset
and the default applies; the defaults live in one place, `src/pool_heater/config.py`.

| Variable | Default | Notes |
|---|---|---|
| `ON_THRESHOLD` | `3000` | W of surplus needed to start; must be >= `HEATER_DRAW_W` |
| `HEATER_DRAW_W` | `2000` | what the heater actually consumes; set from a measured Boost run |
| `IMPORT_THRESHOLD` | `300` | W of grid import that counts as paying for it |
| `DISCHARGE_THRESHOLD` | `500` | W of battery discharge that counts |
| `SOC_FLOOR` | `90` | % below which discharge stops the heater |
| `ON_DELAY` / `OFF_DELAY` | `10` | minutes the condition must hold |
| `MIN_RUN` / `MIN_OFF` | `30` | minutes minimum run and minimum rest |
| `START_GRACE` | `10` | minutes before a start is checked; the unit stages up slowly |
| `MAX_FAILED_STARTS_PER_DAY` | `2` | failed starts that refund their switching cycle |
| `MAX_SWITCHES_PER_DAY` | `3` | starts per day |
| `HARD_OFF_START` / `HARD_OFF_END` | `20:00` / `10:00` | the run window |
| `SEASON_START` / `SEASON_END` | `01 May` / `30 Sep` | inclusive |
| `OFF_SEASON_MODE` | `monitor` | or `dormant` |
| `FORCE_OFF_SEASON` | `false` | close the pool early without editing dates |
| `CAR_PRIORITY` | `true` | raise the bar while the car charges |
| `CAR_ACTIVE_W` | `3000` | W above which the car counts as charging |
| `CAR_PRIORITY_MARGIN_W` | `1000` | extra W demanded while it is |
| `ECOSILENCE_ENABLED` | `false` | the modulation refinement, off in v1 |
| `SETPOINT_C` | unset | send a target water temperature with each start |
| `DRY_RUN` | `true` | **set to `false` to control the heater** |

**If a partly cloudy day spends the budget before mid-afternoon**, raise
`ON_DELAY` to 15 or 20 minutes. The simulation in `tests/test_day_simulation.py`
shows the trade-off: a day of sun and cloud alternating every ten minutes spends
all three starts by 14:00 at the default settings, and the switching budget is
what stops it there. Responding within fifteen minutes and surviving a flickering
day pull in opposite directions, and `ON_DELAY` is the dial between them.

## Off-season

Outside the season the automation never sends an ON command. In `monitor` mode it
checks the heater about once an hour and, if it finds it running, switches it off
and messages you — the exact mistake this project exists to prevent. In `dormant`
mode it makes no API calls at all, which is right if the unit is winterised and
powered down.

The first cycle of a new season sends a notification rather than silently
starting to switch hardware.

## A manual override, if you want one later

Solar Manager supports a **virtual switch** — a device that controls nothing but
can be read back through the API. Creating one and having this loop treat it as a
veto would give you an override you can flip from the Solar Manager app, without
touching repository variables. Not built, but it is the natural place to put one.

## Operating it

* **Logs** — Actions tab → *Pool heater*. Every cycle prints the reading and the
  decision with its reason.
* **State** — kept on the `pool-heater-state` branch as a single file, replaced
  by one parentless commit each cycle so it never grows a history. Nothing
  secret is written there; this repository is public and the tests enforce it.
* **Stop it now** — set `DRY_RUN` to `true`, or disable the *Pool heater*
  workflow in the Actions tab. Neither touches the heater; use the app for that.
* **Tests** — `pip install -r requirements-dev.txt && python -m pytest` from the
  repository root. `pytest.ini` is what puts `src/` on the path.

## Things worth knowing

**Solar Manager's rate limits are 500 requests per hour per endpoint**, except
`/v3/auth/refresh`, which allows only 50 and whose docs say the access token must
therefore be cached. This job runs 12 times an hour and reads one or two
endpoints per cycle, so it sits far inside the general limit — but it has nowhere
safe to cache a token, since the repository is public. That is why it prefers the
`X-API-KEY` header method: no exchange, no token lifecycle, and the rate-limited
endpoint is never touched. The exchange remains as a fallback, and `probe-solar`
reports which method your account accepted.

**Do not tick "Erneuerung erlauben" (allow renewal) on the API key.** That makes
it a *rotating* refresh token: every exchange issues a new key and invalidates
the old one, so an integration that cannot persist secrets between runs — this
one — would authenticate once and then be locked out. Leave it off and the key
stays static.

**Solar Manager offer no customer support for their API**, in their own words.
Their article is the documentation, and there is nobody to ask about it.

**Neither API is official.** Solar Manager's is documented and supported;
iAquaLink's is reverse-engineered by the Home Assistant community and can change
without warning. The fail-safe path exists because of this: an unreachable API
sends one OFF and one alert, rather than leaving the heater running or spamming
you every five minutes.

**iAquaLink rate-limits.** The heat pump is therefore *not* polled every cycle.
Its state is read before any command, on a 30-minute reconcile interval, and on
the hourly out-of-hours check — roughly 35 reads a day rather than 288. If a
command and the device's own state ever disagree, the device wins and the
disagreement is logged.

**GitHub's cron is approximate.** Scheduled runs can be delayed several minutes
under load. The debounce therefore counts elapsed time as well as samples, so a
burst of bunched-up runs cannot pass for ten minutes of steady surplus, and a
long gap resets the streak rather than pretending the condition held through it.
Note also that GitHub disables scheduled workflows in a repository with no
activity for 60 days, and emails you when it does.

**Grid import and export are derived when they are not reported.** The cloud
stream carries `iW` and `eW` directly, but the documented `/v2/point` schema
lists only their watt-hour forms, accumulated over an interval whose length is
not published. So when the watt pair is missing the client works them out from
the energy balance — production plus battery discharge, less consumption and
battery charging — rather than guessing at an interval. Likewise, if the stream
has no top-level state of charge, it looks for the battery in the device list,
taking care never to read the *car's* charge level as the house battery's. An
unknown state of charge counts as below `SOC_FLOOR`, which is the cautious
direction: it stops the heater rather than letting it drain the battery blind.

**The car-priority margin is headroom, not arithmetic.** The surplus figure
already nets the Easee out — power going into the car is neither exported nor
stored. The margin exists so the heater does not claim the last watt of surplus
and leave the charger to ramp into the grid a minute later.

**What has and has not met real hardware.** The control logic, state handling and
both clients are covered by 147 tests including a full simulated day, but tests
only prove the code does what it was written to do. Against the real thing:

*Verified.* Solar Manager authenticates by `X-API-KEY` header on a live account,
no token exchange involved. The cloud stream carries `pW`, `cW`, `iW`, `eW`,
`bcW`, `bdW` and `soc` exactly as mapped, at a ten-second interval (`iv`), so the
energy-balance derivation is a genuine fallback rather than the normal path.
Devices come back with readable types — `Inverter`, `Smart Meter`, `Battery`,
`Car Charging` — and the charger is found by type without needing
`SOLAR_MANAGER_CAR_DEVICE_ID` set.

iAquaLink is verified too, against a TD5 running firmware 8.3.0. Login works;
the shadow reads from `/devices/v1/…` (v2 answers "missing signature"); and both
power commands take effect — a power-on was seen reaching `status 2`, the unit's
own code for actively heating, and a power-off returned it to `status 0`.

The unit stages up over **minutes**, not seconds. It reports `state 1, status 0`
with a changing `reason` while it checks flow and eases the compressor in, which
is why nothing here judges a start until `START_GRACE` has passed.

*Still unverified.* The `st` code for Smart mode. Boost is 0 and EcoSilence is 1;
2 is a guess that only matters with `ECOSILENCE_ENABLED` turned on.

*Assumed.* `ON_THRESHOLD = 3000 W`. The shadow reports the unit as "5 kW", and a
reading taken while it was heating suggests an electrical draw nearer 1.5–2 kW —
so the default likely demands far more surplus than the heater needs. Watch the
consumption step in Solar Manager during a Boost run and set the threshold just
above it.
