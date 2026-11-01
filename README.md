# Amazon job recorder

Requires Python 3.10 or newer. No packages need to be installed.
Automatic session renewal requires `curl`, which is built into Windows.
The recorder uses the `curl` program when available (including Windows' built-in
`curl.exe`), with Python's HTTP client as a fallback. Use `--transport curl` or
`--transport urllib` to select one explicitly. Request headers are sent to curl
through standard input and are not included in command-line arguments.

From this project folder, run:

```powershell
python amazon_jobs.py
```

The complete browser flow and five-second job-found alarm are enabled for the
normal command.
Restart an already-running recorder to apply this change.

Errors and browser progress are shown by default. For troubleshooting, run
`python amazon_jobs.py --verbose` to show requests, job counts, and renewals.

After the existing distance and search filters, jobs trigger the alarm and
browser automation if **`"jobType": "Full-time"` OR the city is Rochdale or Bolton**.
Rochdale and Bolton jobs qualify regardless of job type, but must still match
the configured distance. City names ignore letter case and surrounding spaces.
All returned jobs are still saved to `record.json`. If no jobs qualify,
searching continues without starting the alarm or application steps.

When the results have been saved successfully, the recorder opens the **first
eligible job in that response** in your regular Google Chrome profile. It uses
that job's ID to open its detail page directly, waits for it to
load, clicks **Apply**, then clicks **Next** on the application introduction.
With the current settings it then clicks **Start Application**. If Amazon shows
the **You have an active job application** warning, it clicks **Continue** in
that modal, confirming replacement of the previous active application as you
requested. It then clicks **I Agree** on the application integrity notice,
chooses the available shift with the **highest weekly hours, at least
30 hours**, and clicks **Select this job**. It stops at the **contingent-offer
page** for you to review the offer. The job order
comes from the current API response, including your configured search filters;
the existing order of historical jobs in `record.json` does not affect it.
Once Chrome opens the first matching job, **job fetching stops for the rest of
that run**. The application steps continue silently. The command stays open until
you close it or press **Ctrl+C**, including after the
application steps finish or time out; it does not resume job searches. No more
API session renewals, counters, or sighting timestamps are recorded during this
phase. Restarting `python amazon_jobs.py` starts searching again and repeats the
same workflow, while preserving existing records. Chrome stays open after you
stop the recorder.

For example, shifts offering **30, 30, 30, 40, and 20 hours** select the
**40-hour shift**. Equal maximum hours select the first listed shift. Shifts
below 30 hours are never selected; exactly 30 hours qualifies. If none qualify, the helper waits for an
eligible shift until its timeout. All listed cards, including those below the
visible area, are considered. Disabled cards and unreadable hours are skipped.

For speed, the helper reads the listed hours together using cached accessibility
properties, sorts the eligible shifts by weekly hours, and opens the highest.
It keeps the tracked Chrome window in the desktop accessibility tree so the
application iframe remains readable, and adds no deliberate pauses between
ready steps. Missing controls are checked every 100 milliseconds. The selected
hours are checked again before **Select this job** is clicked. Dates, schedules,
pay, and locations are not compared as part of shift selection.
Chrome can briefly keep a card marked off-screen after scrolling. The helper
invokes that card directly instead of aborting on the delayed visibility flag.
Page loading and Amazon's response time still affect the total duration.
If Amazon resumes an existing application directly at shift selection, the
helper attaches to that matching application and continues the same selection
rule. An older offer window is ignored when one active application window for
the same job is available. Multiple active windows for the same job are left
untouched rather than choosing a window arbitrarily.

These application steps are saved in the script and run with the normal command:

```powershell
python amazon_jobs.py
```

On Windows, a hidden PowerShell helper uses the same accessibility controls as
the earlier interactive clicks. It verifies the Amazon domain, job ID, and page
step before clicking. Next is only clicked on the introduction, not on later
application steps. The helper continues after job fetching stops, without
further job-search requests or record updates. The terminal prints `Browser automation: clicked
Apply`, `clicked Next`, `clicked Start Application`, `clicked I Agree`, the
chosen weekly hours, `clicked Select this job`, and `reached the contingent-offer page`
as it progresses. Continue is clicked only inside the active-application modal
after checking its title and withdrawal message. Ctrl+C stops the
helper and recorder while leaving Chrome open.

The initial browser configuration selects **Profile 7**, the last-used Chrome
profile found on this computer. Check that this is the profile where you are
logged in to Amazon:

```powershell
python amazon_jobs.py --test-browser
```

This makes one job search using the current distance, coordinates, and payload
filters and the Full-time or Rochdale/Bolton check, then opens the **first eligible job's detail page** in Chrome and runs
the same configured application steps. The command waits for the browser steps to finish and
reports a failure if they cannot complete. It does not change `record.json` or
play audio. If no jobs match, it prints a message
and does not open a page. The normal automatic session renewal may update
`headers.json` before the search. Chrome uses its existing cookies and login
storage. If Amazon asks you to sign in again, sign in in that regular Chrome
profile.

To change profiles, open `chrome://version` in the Chrome window where you are
logged in. The last folder under **Profile Path** (for example, `Profile 2`) is
the value to put in `browser_settings.json`:

```json
{
    "enabled": true,
    "profile_directory": "Profile 7",
    "chrome_path": null,
    "user_data_dir": null,
    "auto_apply": true,
    "auto_next": true,
    "auto_start_application": true,
    "auto_withdraw_previous_application": true,
    "auto_agree_integrity_notice": true,
    "auto_select_shift": true,
    "automation_timeout_seconds": 90
}
```

You can also override it for a run with
`python amazon_jobs.py --chrome-profile "Profile 2"`. Setting
`profile_directory` to `null` selects Chrome's last-used profile. Leave
`user_data_dir` as `null` for Chrome's normal user data folder, or set it to the
parent folder containing your profile directories. Leave `chrome_path` as
`null` to detect Chrome automatically, or supply the full path to `chrome.exe`.
Relative paths are resolved next to the browser settings file. The profile
must already exist; a misspelled profile name produces an error rather than
creating a new profile without your login. Browser settings are read again
before each opening attempt. A launch failure leaves job recording running and
retries browser opening after 60 seconds.

`auto_apply: false` opens the job without running the application steps.
`auto_next: false` clicks Apply and stops at the introduction.
`auto_start_application: false` stops at consent. With Start Application enabled,
`auto_withdraw_previous_application: false` leaves the active-application warning
for you to handle manually. `auto_agree_integrity_notice: false` stops at the
integrity notice, and `auto_select_shift: false` stops at shift selection.
Start Application, previous-application replacement, integrity agreement, and
shift selection default to false when omitted from a settings file; they are
explicitly enabled in your current configuration for the requested workflow.
The browser steps
wait up to `automation_timeout_seconds` (an integer from 1 to 300; default 90).
Keep the job window open while the helper is working. If Amazon needs you to
sign in, it prints a message and waits for you to finish in Chrome; it can then
resume the application steps within that time limit. Timeouts or browser automation errors
are printed and leave the command running, with job fetching stopped.
Restart the recorder or use
`--test-browser` to try the browser steps again.
Each helper run saves its progress and errors to
`.diagnostics/browser-automation-<time>-<process>.log`; its path is printed when
the helper starts. Amazon's timeout page is reported as a failed run.

Use `--no-browser` or set `enabled` to `false` to disable browser opening.
With browser opening disabled, fetching and recording continue at the configured
interval. If Chrome cannot open, fetching also continues until an opening
succeeds. Once it opens, fetching stays stopped until you restart the command.
This uses Chrome's normal profile launch options and Windows UI Automation;
no Selenium package or browser driver is needed. Automatic application steps require
Windows, Windows PowerShell, and the included `browser_steps.ps1` file.
Chrome documents its profile directories and launch options
at <https://www.chromium.org/developers/creating-and-using-profiles/>.

The normal command records jobs, starts the job-found alarm, and runs the
browser application steps:

```powershell
python amazon_jobs.py
```

The five-second full-volume alarm is enabled by default; `--alert` also enables it
explicitly. On Windows, as soon as a successful response contains eligible jobs and they have been
saved to `record.json`, a two-tone alarm starts. It sets your default
speaker/headphone volume to **100%** and unmutes it. Jobs already present in
`record.json` also trigger the alarm when the API returns them again and they qualify; historical
records alone do not trigger it. The alarm plays **once per run for 5 seconds**,
then stops and restores the previous volume and mute setting automatically.
Browser automation continues while the alarm plays. Repeated job results do not
restart the alarm. **Ctrl+C** also stops it early and restores the previous volume.
Force-killing the process stops
its audio, but cannot restore the previous volume.

To try the five-second alarm without making any API requests:

```powershell
python amazon_jobs.py --test-alert
```

Use `python amazon_jobs.py --no-alert` to explicitly disable sound. Restart any
already-running recorder once to enable the restored alarm. `--once` saves one
response and exits without opening Chrome or playing an alarm. Sound alerts use Windows' built-in
audio support and need no extra packages. If audio or volume control fails, the
error is printed and job recording continues.

Anonymous session renewal is automatic. The recorder uses the same session
request as the website, renews the token five minutes before its declared
expiry, and saves the result to `headers.json`. New session cookies returned
by that request are saved too. A rejected job request (401/403) triggers a
renewal attempt on the next poll. Renewal attempts are at least 60 seconds
apart, and network errors respect the normal backoff and `Retry-After` handling.

Your existing browser headers and cookies are still used. If Amazon refuses
the renewal request or requires browser verification, importing a fresh browser
request may still be necessary. This does not complete browser challenges
automatically. To turn off renewal, use `python amazon_jobs.py --no-auto-session`.

The first request runs immediately. Successful polls start every 3 seconds when
requests take less than 3 seconds. Requests never overlap; a slower request
finishes before the next one starts. Stop with **Ctrl+C**.

To change the search radius, open `settings.json`. Distance options from 20 to
100 miles are listed, with **30 miles active** and the other options commented:

```jsonc
{
    // "distance_miles": 20,
    "distance_miles": 30,
    // "distance_miles": 40,
    // "distance_miles": 50,
    "latitude": 53.56553690674,
    "longitude": -2.225683211454
}
```

To select 40 miles, add `//` before the 30-mile line and remove `//` from the
40-mile line. Keep exactly one `distance_miles` line active. You can also edit
the active number to any positive radius. All choices use the same latitude
and longitude as their centre. The default is 30 miles around your original
coordinates. The settings reader supports full-line `//` comments; other JSON
files remain standard JSON. VS Code is configured to recognise comments in
`settings.json`.

After updating the script to support comments, restart it once. Thereafter,
save the file: the running script
reads it before each poll, so the new radius applies to the next request without
restarting. With `--verbose`, the terminal shows the radius used for every request. The latitude
and longitude define the centre of the search.

You can also set a radius when starting the script:

```powershell
python amazon_jobs.py --distance 40
```

`--distance` overrides the file's distance for that run. Invalid settings skip
the request and leave saved counters unchanged until the file is corrected.
Existing jobs in `record.json` are retained as history when the radius changes.

`record.json` contains one object per unique `jobId`, with the requested job
details, an integer `Counter`, and a `JobPostedTime` array. New jobs start at `1`;
each job seen in a successful response increases its counter by `1` and appends
that poll's date and time to `JobPostedTime`. Timestamps use ISO 8601 in UTC with
an explicit `+00:00` offset. The normal interval is three seconds. Duplicate IDs
within the same response count once and append one timestamp. Updated job
details replace the previous details while preserving the timestamp history.
Jobs that disappear remain in the file; empty responses and failed requests do
not append timestamps or increment counters. Restarting continues both histories.
Counters and timestamps update while fetching is active. Once the browser
opens a matching job, fetching and record updates stop for the rest of that run.
Use `--no-browser` to keep fetching and recording without browser automation.
Run one instance at a time for each output file.

`JobPostedTime` records when this script **observed** a job in an API result;
it does not establish Amazon's original publication time. Existing records that
do not yet have the array start with an empty history and retain their counters.
Earlier dates cannot be reconstructed, so those jobs may have a larger counter
than the number of saved timestamps. Restart the recorder after this update to
begin saving timestamps on subsequent successful polls.

Example output after the same job appears in three successful responses:

```json
[
    {
        "jobId": "JOB-UK-0000000660",
        "jobUuid": null,
        "jobTitle": "Delivery Station Warehouse Associate",
        "jobType": "Reduced",
        "employmentType": "Seasonal",
        "city": "Garden City",
        "state": "Wales",
        "postalCode": "CH5 2JF",
        "locationName": "Garden City Area",
        "totalPayRateMin": 15.1,
        "totalPayRateMax": 15.1,
        "distance": 39.97481,
        "geoClusterDescription": "Garden City Area",
        "Counter": 3,
        "JobPostedTime": [
            "2026-10-20T12:00:00+00:00",
            "2026-10-20T12:00:03+00:00",
            "2026-10-20T12:00:06+00:00"
        ]
    }
]
```

The latest browser GraphQL payload is in `payload.json`. It searches across the
United Kingdom, includes both `isPrivateSchedule` filter values, sorts by maximum
pay descending, and uses `pageSize: 100`. Before each request the script adds
`geoQueryClause` from `settings.json` to filter by distance in miles. Edit
`payload.json` to change other search filters. The
script records every job in the returned `jobCards` array. In verbose mode it warns if the API
returns a `nextToken`; it does not automatically request additional pages.

For a single request, or a different interval:

```powershell
python amazon_jobs.py --once
python amazon_jobs.py --interval 10
```

For continuous polling, use `python amazon_jobs.py` without `--once`. The terminal
prints errors and browser progress by default.
Add `--verbose` to see each request and its job count.
Each request has a 15-second overall timeout, including DNS and connection
attempts; use `--timeout 5` for a shorter limit. `record.json` stays empty until
a successful response contains at least one job. An empty search result and a
failed request produce different terminal messages in verbose mode; by default
only the failed request is printed.

The default payload and output paths are next to the script, even when it is
launched from another folder. Use `--records another-file.json` to change the
output file or `--payload another-payload.json` for a different request.

Network failures, non-JSON responses, GraphQL errors, and invalid job cards leave
existing records unchanged. Repeated failures increase the retry delay up to
60 seconds (or longer when the server sends `Retry-After`). Successful requests
restore the normal 3-second interval. A malformed existing `record.json` or a
write failure stops the script to protect the saved counters. Writes replace
the output file atomically.

If Amazon requires authentication or additional request headers, save the headers
from your own working request as a JSON object in `headers.json`. That file is
loaded automatically when it exists next to the script. A different file can
be selected explicitly:

```powershell
python amazon_jobs.py --headers-file headers.json
```

An `HTTP 401` or `HTTP 403` message means access was refused. In your browser's
Developer Tools, open **Network**, run the Amazon job search, and select the
successful `graphql` request. Copy its required request headers into your local
`headers.json` file as string key/value pairs. Only copy request headers; omit
HTTP/2 pseudo-headers beginning with `:`. A 403 can have other causes, so extra
headers do not guarantee access.

Browser cookies can still expire or become invalid. If automatic session
renewal keeps returning 401 or 403, refresh the local header file from a
successful browser request and restart the script. The script also refreshes
the supplied `x-hvh-time` timestamp and request ID for each poll.

To import a new browser session without manually editing long header values:

1. Open the Amazon job search in your browser and reload it. Confirm that a job
   search succeeds.
2. In **F12 > Network**, select the successful `graphql` request and choose
   **Copy > Copy as cURL (bash)**.
3. Save the copied text as `request.curl` in this project folder.
4. Run `python import_session.py`. It extracts the request headers and updates
   `headers.json`. It rejects a token whose declared expiry is already past.
5. Stop the old recorder with **Ctrl+C**, then run `python amazon_jobs.py`.

The importer reads the copied command as text and does not execute it. It
imports session headers only; your search settings and saved counters are
preserved. Importing a session does not extend that token's lifetime; the
running recorder renews it automatically before expiry. You need a fresh
successful browser request when browser cookies or verification need updating.

If the response comes from CloudFront and says `Request blocked`, it does not
identify which header or access condition caused the refusal. First check that
you can search for jobs at <https://www.jobsatamazon.co.uk/> in your browser on
the same network. If the search also fails there, the script cannot fetch job
data until that access problem is resolved.

If the browser search succeeds, open **Developer Tools > Network**, select the
successful `graphql` request, and choose **Copy > Copy as cURL (bash)**. Save the
copied text locally as `request.curl`, then run `python import_session.py` to
configure the script with that request's headers. The polling script does not
import this file automatically. It can contain cookies or authentication values, so keep it out
of chat. Both `request.curl` and local `.diagnostics` files are excluded by
`.gitignore`.

Do not share credentials. `headers.json` is excluded by `.gitignore`. The
recorder renews anonymous job-search tokens only; it does not sign in to an
Amazon account or renew account login credentials.

Run the local tests with:

```powershell
python -m unittest discover -s tests -v
```
