# Seed sources

Spot-checked 2026-10-07 from command-center1 with `curl`, the way the job's
date check fetches (one GET, `User-Agent: Mozilla/5.0 (compatible; outings/0.1; ...)`).
"Dates in HTML" means the event dates are in the server response, so the
date-on-page check can confirm them without running JavaScript.

| Source | URL | Static fetch | Dates in HTML | Notes |
|---|---|---|---|---|
| Exploratorium After Dark | https://www.exploratorium.edu/visit/calendar/after-dark | 200 | yes, `Thu, Oct 15 2026 • 6 - 10pm` | Returns 403 to curl's default UA; the job's UA passes. Theme pages under `/visit/calendar/after-dark-*`. |
| Cal Academy NightLife | https://www.calacademy.org/nightlife | 200 | yes, `October 8 at 6:00 pm` (inside a `document.write`) | Written into script text; raw-HTML search still finds it. |
| Chabot Space & Science Center | https://chabotspace.org/events/ | 200 | yes, JSON-LD `startDate` | Structured evidence. |
| Long Now talks | https://longnow.org/talks/ | 200 | yes, `Oct 22` | Years are five digits (`02025`); month-day still matches. |
| Asian Art Museum | https://asianart.org/ | 200 | yes, on the home page | `/events/` is 404; event pages live elsewhere. |
| Japantown | https://www.sfjapantown.org/ | 200 | yes | `/events/` is 404. |
| JCCCNC | https://www.jccnc.org/events/ | 200 after redirect | no | Akamai captcha page. Search-led only. |
| SFJAZZ | https://www.sfjazz.org/calendar/ | 403 | no | Cloudflare challenge for any non-browser client. Search-led; an SFJAZZ event survives only if a source page (Eventbrite, a listing) shows the date. |
| Luma SF | https://luma.com/sf | 200 | yes, JSON-LD `startDate` | The index's visible dates are JS-rendered, but the JSON-LD is server-side. |
| Meetup | https://www.meetup.com/find/?location=us--ca--san-francisco | 200 | yes, JSON-LD | |
| Eventbrite | https://www.eventbrite.com/d/ca--san-francisco/japanese/ | 200 | yes, JSON-LD | |

What the check proves: the event's local date appears on the page the
model linked (or one of its source pages). On a listing page that shows many
dates, that is a sanity check, not proof that this event is on that date. The
prompt therefore asks for the event's own page, and the page labels the
evidence ("Date checked" for an ISO date in the page data, "Date on page" for
a written date).

# Collected sources (collect.py)

Measured 2026-10-18 from command-center1 with the job's User-Agent. A live
collection took about a minute and gave 133 events in a 15-day window.

| Source | Kind | What it gives | Notes |
|---|---|---|---|
| chabotspace.org/events/ | JSON-LD | start and end with offset, no location | Each event appears in two ld+json blocks. |
| exploratorium.edu After Dark | HTML cards | `<time datetime>` start and end | One card per theme, next Thursday shown. |
| calacademy.org/nightlife | HTML rows | "October 22 at 6:00 pm", no year | Year inferred: nearest date after yesterday. |
| luma.com/sf, /sf/tech, /ai-sf | JSON-LD | start, end, offers | Venue often hidden ("San Francisco, CA"); kept as "Venue on the event page". |
| meetup.com find, keywords japanese / astronomy | JSON-LD | start in UTC, venue, online flag | Keyword search is loose; the model filters. |
| eventbrite.com SF categories (japanese, science-and-tech, astronomy, jazz, cooking-class) | JSON-LD | date only, venue, region | Times come from each event page. Geo is unreliable, region is not. |
| events.stanford.edu API | JSON (Localist) | start and end with offset, venue, free flag | About 500 events in 15 days; titles must match a listed interest. |
| events.berkeley.edu feed | JSON (LiveWhale) | start and end with offset, location | About 1000 events in 15 days over 11 pages; explicit date range required. |
