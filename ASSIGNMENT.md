# Take-home assignment — source of truth

Section 1 is the reviewer's original brief, verbatim. Section 2 is a faithful English translation.
Sections 3–5 are OUR decomposition, interpretation and design choices, not the reviewer's wording.
If the translation and the Hebrew original ever differ, the Hebrew wins.

---

## 1. Original brief (Hebrew, verbatim)

היי, מצרף את התרגיל

המשימה היא לבנות שירות מזג אוויר שממליץ לך האם לצאת לעשות את הפעילות האהובה עליך (לגלוש, לרוץ, לראות את השקיעה, להישאר בבית לשחק במחשב או כל דבר אחר)

יש לבנות אפליקציה שבודקת מה מזג האוויר בחמש ערים שתבחר מ-api חיצוני (openweather או אחר)

לנתונים שקיבלת יש להוסיף המלצת llm האם מזג האוויר מתאים לפעילות שתרצה לבצע
את הllm יש להרים מקומית באמצעות מודל open weights ללא שימוש ב-api חיצוני.

את כל הנתונים שאספת יש לשלוח לשירות תורים, משם להזרים ולאגור בבסיס נתונים.

על כלל רכיבי המערכת להיות containerized ולהיות ניתנים להרצה פשוטה ואחידה.

הגבלה חשובה - על המערכת להיות מסוגלת לרוץ On Prem.
כלומר, ללא גישה מלאה לאינטרנט. עליך לייצר סוכן שיודע לענות בזמן אמת על שאלות מגוונות על מזג האוויר ופעילות ספורט בערים שבחרת, בהתבסס על המידע שאגרת.

בנוסף לשאלות על מזג האוויר, על המערכת לאפשר שאלות תיירות כלליות על הערים שבחרת כמו היסטוריה מקומית,מקומות מעניינים, אירועי ספורט מגניבים בקרבת מקום.

תנגיש את המידע במערכת הטיולים שלך, על בסיסה המשתמש יוכל לקבל המלצות ולהכין מסלול על היעדים שבחרת.

- חשוב שתהיה ויזואליזציה טובה לנתונים.

- חשוב שהתשתית תדע להתמודד עם תקלות זמניות ללא איבוד מידע.

- חשוב שתייצר יכול לעשות update למידע שאגרת עד כה.

חלקים רבים בתרגיל כלליים ומאפשרים לך לקחת אותם לכיוונים או לטכנולוגיות שמתאימות לך!

את התרגיל יש להגיש כלינק ל git repository.
הכולל את כל הקוד, קבצי הקונפיגורציה, הגדרות  ci/cd וקובץ readme.
בקובץ הReadme יש להסביר כיצד מרימים את המערכת, הארכיטקטורה שלה, בחירות טכניות ומה היו השיקולים לבחירה.

בונוסים
- בדיקות מלאות לכל הרכיבים
- llm observability - מטריקות ייעודיות למספר הבקשות, שגיאות, זמנים וכו
- התאוששות אוטומטית מתקלות

דוגמאות לשימוש:

Q: What is the weather tomorrow in Rome?
Q: What activities can I do with my wife this week in London? We like concerts, Shopping and fine dining.

בהצלחה!

---

## 2. English translation (faithful)

Build a weather service that recommends whether you should go out and do your favorite activity (surfing, running, watching the sunset, staying home to play computer games, or anything else).

Build an application that checks the weather in five cities of your choice using an external API (OpenWeather or another provider).

Add an LLM recommendation to the data you received, indicating whether the weather is suitable for the activity you want to do. The LLM must be run locally using an open-weights model, without using an external API.

Send all the data you collected to a message queue service, and from there stream it into a database and store it.

All system components must be containerized and runnable in a simple, uniform way.

Important constraint: the system must be able to run on-prem — that is, without full internet access. You must build an agent that can answer varied questions in real time about the weather and sports activities in the cities you chose, based on the information you stored.

In addition to weather questions, the system must allow general tourism questions about the chosen cities, such as local history, interesting places, and cool sports events nearby.

Make this information available in your travel system, through which the user can get recommendations and build an itinerary for the chosen destinations.

- Good data visualization is important.
- It is important that the infrastructure can handle temporary failures without losing data.
- It is important that you build the ability to update the information stored so far.

Many parts of the assignment are general and let you take them in directions or technologies that suit you.

Submit the assignment as a link to a Git repository containing all the code, configuration files, CI/CD definitions, and a README.
The README should explain how to bring the system up, its architecture, the technical choices, and the considerations behind them.

Bonuses:
- Full tests for all components
- LLM observability — dedicated metrics for number of requests, errors, latency, etc.
- Automatic recovery from failures

Usage examples:
- Q: What is the weather tomorrow in Rome?
- Q: What activities can I do with my wife this week in London? We like concerts, shopping and fine dining.

---

## 3. Requirement checklist (our decomposition — every plan must trace to these IDs)

### Explicit requirements

| ID | Requirement |
|----|-------------|
| M1 | Weather for five chosen cities from an external API (OpenWeather or other) |
| M2 | LLM recommendation added to the weather data: is the weather suitable for the activity |
| M3 | LLM runs locally, open-weights model, no external LLM API |
| M4 | ALL collected data → message queue → streamed into and stored in a database |
| M5 | All components containerized; simple, uniform way to run |
| M6 | Must run on-prem, without full internet access |
| M7 | Agent answering varied questions in real time about weather and sports activities in the selected cities, based on stored data |
| M8 | General tourism questions: local history, interesting places, nearby sports events |
| M9 | Travel system: recommendations + building an itinerary for chosen destinations |
| M10 | Good data visualization |
| M11 | Infrastructure handles temporary failures without data loss |
| M12 | Ability to update information already stored |
| S1 | Git repo with all code, configuration files, CI/CD definitions, README |
| S2 | README: how to start, architecture, technical choices and the reasoning |

### Bonuses (the brief lists these as bonuses, not requirements)

| ID | Requirement |
|----|-------------|
| B1 | Full tests for all components |
| B2 | LLM observability metrics (request count, errors, latency, etc.) |
| B3 | Automatic recovery from failures |

### Illustrative usage examples from the brief

The brief lists these under "דוגמאות לשימוש" (usage examples). They are illustrative usage questions from the brief; the cities are not mandated. Choosing to treat them as acceptance tests is ours (§5).

| ID | Example |
|----|---------|
| E1 | "What is the weather tomorrow in Rome?" |
| E2 | "What activities can I do with my wife this week in London? We like concerts, shopping and fine dining." |

---

## 4. Interpretation notes (ours, not the reviewer's)

These are readings of the brief. Where the brief leaves something open, the note says so and points to §5, where our own decision is recorded.

- **On-prem (M6).** The brief says "ללא גישה מלאה לאינטרנט" — "without FULL internet access", and does not define how much access remains. What follows regardless: new external data can be fetched only while a connection exists, so on-prem every answer comes from the stored data. How strictly we demonstrate this, and what we show about the data's age, is our choice (§5).
- **"להזרים ולאגור" (M4).** Stream and store: the collected data is streamed from the queue service into the database and stored there.
- **"כל הנתונים שאספת" (M4).** "All the data you collected" plainly covers what the system acquires from outside: weather and tourism/place/event records. The brief does not define whether data the system *generates* — the LLM recommendations — or data the user creates or edits counts as collected data that must travel through the queue. Our decisions are in §5.
- **Update (M12).** "שתייצר יכול לעשות update" is a typo for "יכולת" — build the ability to update the data collected so far. The brief prescribes no method; ours are in §5.
- **Temporary failures (M11).** "חשוב שהתשתית תדע להתמודד עם תקלות זמניות ללא איבוד מידע" — the infrastructure must cope with temporary failures without losing data. The brief names no failure modes, no mechanism, and no boundary for the claim. What we guarantee, from which point, and by what means are our choices (§5).
- **"בזמן אמת" (M7).** Real time refers to the agent: it answers varied questions about weather and sports activities in the selected cities interactively, at question time, from the stored data — not through live external lookups.
- **"תנגיש את המידע במערכת הטיולים שלך" (M9).** Expose the information in your travel-planning system: a user-facing app where the user gets recommendations and builds an itinerary for the selected destinations. Good data visualization is a separate requirement (M10); both are needed.
- **"אירועי ספורט מגניבים בקרבת מקום" (M8).** Cool sports events nearby — matches, races, tournaments. Events, not only sports activities.
- **Tourism topics are open-ended (M8).** The brief says "כמו היסטוריה מקומית, מקומות מעניינים, אירועי ספורט מגניבים" — "such as", so the three topics are examples, not an exhaustive list. E2 asks about concerts, shopping and fine dining, which shows the breadth the brief expects.
- **The activity is a parameter (M2).** The brief's own examples span outdoor and indoor ("לגלוש, לרוץ, לראות את השקיעה, להישאר בבית לשחק במחשב או כל דבר אחר"), so the recommendation is made for an activity the user picks, not for one fixed activity.
- **Forecast horizon (M1).** The brief says only "מזג האוויר", but its examples ask about "tomorrow" (E1) and "this week" (E2), so the stored weather has to cover a forward-looking window, not only current conditions.
- **Language.** The example questions are in English. The brief sets no language requirement; which languages we support is our choice (§5).
- **No deadline in the written brief, no mandated technology.** The written brief sets no submission deadline, and leaves the direction open: "חלקים רבים בתרגיל כלליים ומאפשרים לך לקחת אותם לכיוונים או לטכנולוגיות שמתאימות לך."

---

## 5. Design choices (ours, not required by the brief)

The brief does not require any of these. They are our decisions, recorded here so the README and the plan can point at one list. Items marked TBD are not decided yet. The reasoning, alternatives, current working selections, and validation gates are in [TECHNICAL_DECISIONS.md](TECHNICAL_DECISIONS.md).

**Runtime and packaging**

- **Zero-internet runtime.** Stricter than the brief's "without full internet access": after a one-time connected staging step (container images, model weights, dependencies, UI assets and a data snapshot), the whole stack starts and answers questions with no internet at all. We document the staging step and exactly what keeps working offline. Reason: partial access is unspecified and cannot be tested, while zero internet can be demonstrated.
- **Docker Compose is the run path**, one command per mode, identical on Linux, macOS and Windows. Kubernetes/OpenShift appears in the README as a production path, not as code.
- **CPU by default**; the GPU is an optional Compose override file, never required.
- **Local model.** The brief requires only that the model be local and open-weights (M3). Which model and which size is ours to choose — TBD, decided in planning after measuring CPU latency.

**Data flow and integrity**

- **Everything goes through the queue**, and only the consumer writes to the database. That covers the records we acquire (weather, tourism, places, events) and, by our choice, the two cases the brief leaves open (§4): the **LLM recommendations the system generates**, which re-enter the queue rather than being written straight to the database, and **records the user creates or edits**. If we relax either, the exception is documented.
- **When a record counts as accepted.** A record is accepted once it is durably written to the producer's spool on a persistent volume and acknowledged to the caller. Before that — a fetch that never completed, or a write rejected because the spool is full — nothing was accepted, and we claim nothing about it.
- **Guarantee boundary (M11).** Accepted records survive temporary LLM, broker, consumer and database outages and are eventually stored. The means: a durable producer spool/outbox so a broker outage cannot drop an accepted record, publisher confirms, at-least-once delivery, and idempotent writes where the consumer commits to the database before acknowledging. Outside the boundary: destroyed volumes, exhausted disk or a full spool, and external data that was never accepted — weather that was never accepted can be re-fetched while connected. This is at-least-once eventual storage, not exactly-once delivery, and not uninterrupted answers during an outage.
- **Recommendation grounding (M2).** A deterministic, rule-based suitability score per activity is the source of truth; the LLM writes the recommendation grounded on that score. If the LLM is unavailable or slow, the weather record is published with `recommendation_status=pending` and re-enriched later, so an LLM failure never blocks or loses weather data.
- **Update paths (M12).** Connected: re-fetch from the external sources and update the stored records, so newer forecasts revise existing ones. Offline: re-run the local LLM recommendations over stored data, and correct or edit stored records. The last two also work on-prem.

**Activities and the trip planner**

- **The activity catalogue is ours.** The brief names two activities by way of example (running in Rome, staying in to play computer games). We score 18, listed in `data/activities.yml`: running, hiking, an outdoor workout, football, sightseeing, watching the sunset, a farmers' market, an open-air music festival, surfing, swimming, the beach, fishing, a boat ride, museums, an art gallery, stand-up comedy, the mall, and staying in to game. Anything a user types is scored too, against general outdoor comfort, and labelled as such.
- **Some activities are not scored everywhere.** Surfing, swimming, the beach, fishing and a boat ride are scored only for cities marked `coastal`. An inland city gets no row for them at all, and the agent answers "no record — there is no coast there". Reason: a surf score derived from an inland forecast is a number we cannot defend, and the brief's rule against inventing data applies to derived numbers as much as to events.
- **Indoor activities are differentiated, not identical.** Each carries an `indoor_floor` and an `indoor_weight`, so a museum and a games console do not return the same number on the same day. Without them the planner had nothing to choose between the five indoor options.
- **The planner orders a day, it does not re-score it.** The stored score stays the source of truth and is what the heatmap, the API and the agent report. On top of it, the planner adds a small bonus for a stated interest and a small penalty for repeating an activity used on an earlier day, so a warm week is not the same suggestion seven times. Both adjustments are deterministic and small enough that the weather still decides; a UI toggle turns the repeat penalty off so the raw ranking can be compared.
- **Not every score is worded.** Scoring every activity is free; asking a CPU-bound local model to write a sentence about each of ~1,300 rows is hours. The consumer ranks each city-day and queues only the top `ENRICH_TOP_N` (default 6) for wording; the rest are stored `deferred` — scored, charted and answerable, just not written about. Asking about one by name, or the re-word control in the UI, promotes it. This is a cost decision, and the UI states it rather than showing a blank.

**Content and language**

- **Cities.** Current working selection: Rome, London, Lisbon, Tel Aviv and Reykjavík. Rome and London make both illustrative questions work as written; the two coastal cities support marine activity examples, and Reykjavík adds a contrasting daylight/weather case. Verify source coverage before treating the list as final.
- **E1 and E2 as acceptance tests.** The brief offers them as usage examples (§3); adopting them as pass/fail criteria for our own build is our choice.
- **Nothing is invented, and samples say so.** No event, concert or fixture is passed off as real. Every record carries a source and an as-of date, and a question outside the data coverage gets an explicit "no data for that date" answer. Real listings live in `data/events.seed.jsonl`, hand-verified against their own source URLs; there are seven and they are all in London. Because a planner that can only be exercised in one city out of five demonstrates nothing, we also generate `data/events.samples.jsonl`: every row is `is_sample: true`, titled "Sample: …", sourced to a real venue from the places snapshot, and declared as not-a-real-listing in its `source` field. They are labelled in the UI, in the agent's prompt, in its footer, and counted separately in the coverage tab, and the loader drops any row in that file that does not admit to being a sample. Deleting the file leaves the seven verified rows and nothing else.
- **Every answer and chart shows its as-of timestamp** and the coverage window of the data behind it. The brief does not ask for this; stored data goes stale offline, and an answer that hides its age is misleading rather than merely incomplete.
- **English-first.** UI, agent and stored content are in English.

**Security and repository hygiene**

- Secrets live only in a gitignored `.env`; a `.env.example` is committed.
- Pinned image versions (digests where practical), containers run as non-root where the image allows, no published ports beyond the UI, API and Grafana, and an image scan in CI.
