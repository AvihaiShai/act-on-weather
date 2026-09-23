# Take-home assignment — source of truth

Section 1 is the reviewer's original brief, verbatim. Section 2 is a faithful English translation.
Sections 3–4 are OUR decomposition and interpretation, not the reviewer's wording.
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
| B1 | Bonus: full tests for all components |
| B2 | Bonus: LLM observability metrics (request count, errors, latency, etc.) |
| B3 | Bonus: automatic recovery from failures |
| E1 | Acceptance: "What is the weather tomorrow in Rome?" |
| E2 | Acceptance: "What activities can I do with my wife this week in London? We like concerts, shopping and fine dining." |

---

## 4. Interpretation notes (ours, not the reviewer's)

- On-prem: the brief says explicitly "ללא גישה מלאה לאינטרנט" — "without FULL internet access". Design choice (stricter than the brief): we treat on-prem as zero internet at runtime, because partial access is unspecified and cannot be tested. The system must work fully air-gapped from pre-staged images, model weights, dependencies, UI assets and stored data. The preparation step, and exactly what keeps working offline, are documented. New external data can be fetched only while a connection exists; on-prem, every answer is based on the stored data and shows its as-of timestamp.
- "להזרים ולאגור" = data streaming and storage: the collected data is streamed from the message queue into the database and stored there.
- Update ("שתייצר יכול לעשות update" — typo for "יכולת"): build the ability to update the data collected so far.
  - Connected: re-fetch from the external sources and update the stored records (e.g. newer forecasts revise existing ones).
  - On-prem, no internet: no new external data; answers rely on the stored data. Local updates still work: re-run the local LLM recommendations on stored data, and correct or edit stored records.
- "בזמן אמת" (real time) refers to the agent: it answers varied questions about weather and sports activities in the selected cities interactively, at question time, based on the stored data — not through live external lookups.
- "תנגיש את המידע במערכת הטיולים שלך" = expose the information in your travel-planning system: a user-facing app where the user gets recommendations and builds an itinerary for the selected destinations. Good data visualization is a separate requirement (M10); both are needed.
- "אירועי ספורט מגניבים בקרבת מקום" = cool sports events nearby (matches, races, tournaments) — events, not only sports activities.
- The brief sets no time limit and mandates no specific technology. The example questions are in English, so the system is queried in English.
- Context from the company (verbal, not in the written brief): the task is expected to take about half a day. This is expected effort, not a written deadline.
