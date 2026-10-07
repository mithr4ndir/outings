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
