# Daily Health shortcut

Posts one day of Apple Health numbers to the BayOS gateway. The gateway stores an inbox note. It does not call Gemini.

RISE writes sleep into Apple Health. This shortcut reads Apple Health. It does not talk to RISE.

The gateway key is saved inside the shortcut. Anyone who can open and edit the shortcut can read that key.

## What gets stored

`POST https://YOUR-GATEWAY/api/v1/health/daily`

Header: `X-BayOS-Key` = the BayOS gateway key.

JSON body. Only `date` is required. Omit a field when the phone has no sample.

```json
{
  "date": "2026-10-12",
  "sleep_hours": 7.5,
  "sleep_start": "22:40",
  "sleep_end": "06:10",
  "resting_hr": 62,
  "steps": 8400,
  "spo2_min": 94,
  "spo2_avg": 97,
  "notes": ""
}
```

The inbox item uses agent `shortcut-001`, tags `health` and `sleep`, and the title `Daily health YYYY-MM-DD`. Priority is high when `spo2_min` is below 90 or `sleep_hours` is below 5. A minimum of 90 or a sleep of 5 hours stays normal.

Use the gateway host `deploy.ps1` printed.

## Build the shortcut

1. Open the **Shortcuts** app.
2. Tap **+**, then name the shortcut `BayOS Daily Health`.
3. Add **Find Health Samples**.
   - Type: **Sleep**.
   - If iOS shows **Sleep Analysis**, choose that.
   - Start Date: **is in the last** `1` **day**.
   - Sort by **Start Date**, order **Descending**.
   - Limit on, **Get** `20` samples.
4. Add **Repeat with Each** over those sleep samples. Inside the loop, keep samples whose value is an asleep state (`Asleep`, `Asleep Core`, `Asleep Deep`, `Asleep REM`, or `Asleep Unspecified`). Skip `In Bed` and `Awake` so the total is not doubled.
5. Add **Calculate Statistics** on the asleep samples, operation **Sum** of **Duration**.
6. Check the unit once. If the sum is in hours, use it as `sleep_hours`. If it is in seconds, divide by `3600`. If it is in minutes, divide by `60`. A first run that shows `27000` is seconds, not hours.
7. From the earliest asleep sample, format the start time as `HH:mm` for `sleep_start`. From the latest, format the end time for `sleep_end`.
8. Add **Find Health Samples** for **Resting Heart Rate**, last `1` day, sort **Start Date** descending, limit `1`. Format that sample as a number for `resting_hr`. If the list is empty, leave `resting_hr` out of the JSON.
9. Add **Find Health Samples** for **Steps**, last `1` day. **Calculate Statistics**, **Sum** of the value, for `steps`.
10. Add **Find Health Samples** for **Blood Oxygen**, last `1` day. **Calculate Statistics**: **Minimum** for `spo2_min` and **Average** for `spo2_avg`. Blood oxygen is a percent from 0 to 100. If the shortcut shows `0.94`, multiply by `100` before posting.
11. Add **Date**, then **Format Date** with format `yyyy-MM-dd` for `date`.
12. Add **Dictionary** and set the keys that have values: `date`, `sleep_hours`, `sleep_start`, `sleep_end`, `resting_hr`, `steps`, `spo2_min`, `spo2_avg`. Add `notes` only when you want a line of text.
13. Add **Get Contents of URL**.
    - URL: `https://YOUR-GATEWAY/api/v1/health/daily`
    - Method: **POST**
    - Headers: `X-BayOS-Key` = the gateway key, and `Content-Type` = `application/json`
    - Request Body: **JSON**, and set it to the Dictionary from the previous step.
14. Add **Get Dictionary from Input** on the URL result. Show `title` and `priority` in a quick look or notification so a high-priority morning is visible.

Run it once by hand. Open the tablet page and confirm a note titled `Daily health` plus today's date. If the priority is high on a normal night, the sleep unit or the oxygen scale is wrong. Fix that before automating.

## Run it every morning

1. Open Shortcuts, then the **Automation** tab.
2. Tap **+** (or **New Automation**), then **Time of Day**.
3. Set the time to **9:00 AM**, repeat **Daily**.
4. Choose the shortcut `BayOS Daily Health`.
5. Turn off **Ask Before Running** if iOS shows that switch.
6. If iOS still asks you to tap Run, that is the system confirmation for a shortcut that sends data off the phone. The gateway cannot turn that prompt off.

The shortcut needs permission for Health the first time it runs. Allow Sleep, Resting Heart Rate, Steps, and Blood Oxygen.
