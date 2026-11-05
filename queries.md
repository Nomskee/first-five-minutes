Q1 · Which SDK went quiet? (P1)

SELECT properties.$lib AS lib,
       max(timestamp) AS last_seen,
       dateDiff('hour', max(timestamp), now()) AS hours_silent
FROM events
WHERE timestamp > now() - INTERVAL 14 DAY
  AND properties.$lib IS NOT NULL
GROUP BY lib
ORDER BY hours_silent DESC

Found: react-native has been silent about 73 hours longer than the others

Q2 · Which SDK versions are in use? (P5)

SELECT properties.$lib AS lib,
       properties.$lib_version AS version,
       count() AS events
FROM events
WHERE timestamp > now() - INTERVAL 7 DAY
  AND properties.$lib IS NOT NULL
GROUP BY lib, version
ORDER BY lib, events DESC

Found: two posthog-js versions: 1.444.4 and the very old 1.36.0

Q3 · Which pageviews have no URL? (P5)

SELECT properties.$lib_version AS version,
       count() AS pageviews,
       countIf(properties.$current_url IS NULL
               OR properties.$current_url = '') AS no_url
FROM events
WHERE event = '$pageview'
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY version

Found: Every pageview without a URL comes from 1.36.0. The blank URLs all come from one site on an old SDK

Q4 · Is anyone being identified? (P3)

SELECT properties.$lib AS lib,
       count(DISTINCT distinct_id) AS distinct_ids,
       countIf(event = '$identify') AS identify_calls
FROM events
WHERE timestamp > now() - INTERVAL 14 DAY
GROUP BY lib
ORDER BY distinct_ids DESC

Found: posthog-js shows thousands of distinct IDs and zero identify calls. Each web visit counts as a new person, which is why "unique users" is far above 60.

Q5 · Can the flag match anyone? (P2)

SELECT count() AS persons,
       countIf(properties.email IS NOT NULL) AS with_email,
       countIf(properties.email LIKE '%@tidewater.io%') AS matching_flag
FROM persons

SELECT properties.$feature_flag AS flag,
       properties.$feature_flag_response AS response,
       count() AS calls
FROM events
WHERE event = '$feature_flag_called'
  AND timestamp > now() - INTERVAL 7 DAY
GROUP BY flag, response

Found: no person matches the new-checkout flag. matching_flag = 0 (with_email = 1, the placeholder only). All 425 $feature_flag_called events in 7 days returned false. Likely cause: users are never identified with their email.

Q6 · Did errors spike, and what are they? (P4)

SELECT JSONExtractString(properties.$exception_list, 1, 'type') AS type,
       JSONExtractString(properties.$exception_list, 1, 'value') AS message,
       properties.app_version AS app_version,
       count() AS last_24h
FROM events
WHERE event = '$exception'
  AND timestamp > now() - INTERVAL 1 DAY
GROUP BY type, message, app_version
ORDER BY last_24h DESC
LIMIT 5

Found: 280 $exception events in the 24 hours before the latest event, against about 5 a day before. All are one TypeError, "Cannot read properties of undefined (reading 'total')", and all on app_version 2.4.0. One new error on one new version points to the customer's 2.4.0 release, not PostHog.

Q7 · Are any events duplicated? (P6)

SELECT event,
       count() AS duplicate_groups
FROM (
  SELECT event, distinct_id, timestamp, count() AS copies
  FROM events
  WHERE timestamp > now() - INTERVAL 7 DAY
  GROUP BY event, distinct_id, timestamp
  HAVING copies > 1
)
GROUP BY event

Found: order_completed shows hundreds of duplicate groups. Tidewater's revenue numbers are double counted, and nobody has reported it


## Checks

| ID | Check | Built from | Fail when | Warn when | Planted problem |
| --- | --- | --- | --- | --- | --- |
| C1 | SDK went quiet | Q1 | One SDK silent 24 h or more longer than the most recently active SDK | Silent 6 to 24 h longer | P1 |
| C2 | SDK version freshness | Q2 + latest version from npm or PyPI | 50 or more minor releases behind latest | 10 to 49 behind | P5 |
| C3 | Missing core properties | Q3 | Over 10% of $pageview have no $current_url | 1% to 10% | P5 |
| C4 | Identification health | Q4 | An SDK has over 500 distinct IDs and no $identify calls | Identify calls under 1% of distinct IDs | P3 |
| C5 | Feature flag can match | Q5 + Feature flags API | Flag inactive, or no person matches its person-property condition | Every $feature_flag_called in 7 days returned false | P2 |
| C6 | Error spike | Q6 + 13-day daily average | Last 24 h over 5x the daily average and over 50 errors | 2x to 5x the average | P4 |
| C7 | Duplicate events | Q7 | Over 10% of an event's rows are exact duplicates | 1% to 10% | P6 |
| C8 | Overall volume drop | Last 24 h vs 7-day daily average | Under 25% of the average | 25% to 50% | None, should pass |
| C9 | Related GitHub issues | GitHub search on PostHog/posthog | Never fails | Always info | All |
| C10 | Docs to read | Fixed map of product area to docs page | Never fails | Always info | All |

All time windows are measured from the anchor (the latest event), not now().

## Expected results per ticket

| Ticket | Should fail | Should also show |
| --- | --- | --- |
| T1 Mobile data gone | C1 | C8 pass |
| T2 Flag shows for nobody | C5 | C4 fail, shared root cause |
| T3 Unique users too high | C4 | C5 fail, same root cause |
| T4 Error spike after release | C6 | Evidence pointing to 2.4.0 |
| T5 Blank page URLs | C2 and C3 | Both point to posthog-js 1.36.0 |
| Every ticket | C7 | The bonus duplicate-orders find |

