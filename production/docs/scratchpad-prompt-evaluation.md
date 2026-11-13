# Scratchpad prompt evaluation

This evaluation measures how local models handle requests to create, draw, group or colour items in the scratchpad: the old prompt (baseline), two fix rounds, and a final round in English and German, then evening rounds over the native tool-call API with more changes (see "Evening rounds"). It ran on 2026-11-13 against the Harthof data. Each cell is a single run, so read the results as indications, not statistics. The first attempt was set up for the wrong database (`citydb`, LDBV data), but nothing ran there; every run below used `harthof`.

## Setup

- Database: `harthof` (3DCityDB v5, schema `citydb`, SRID 25832, 331 buildings) in the
  `production-postgres-1` container (PostGIS 3.6.4, SFCGAL 2.3 at run time but PostGIS compiled
  against SFCGAL 2.0, so `CG_3DAlphaWrapping` fails). All runs used `harthof` (`CITYDB_NAME=harthof`
  set only in the run environment). The stack's default database `citydb` (LDBV data) was not used
  and has no scratchpad rows.
- Client: `python -m webui.cli chat --provider ollama --show-reasoning --scratchpad-user <uuid>` on
  the host (PYTHONPATH = `src;production`), questions piped via stdin. Q1 to Q8 plus two control
  questions (C1, C2) ran in **one** session per model, because Q5 builds on Q4 and Q7 on Q6.
  Local ReAct backend, num_ctx 131072, `AGENT_MAX_ITERATIONS` 20.
- Ollama at the shared server (OLLAMA_BASE_URL in production/.env). Models: `gpt-oss:20b`,
  `qwen3.6:27b-q4_K_M`; `gpt-oss:120b` in the final round (English only).
- Baseline = commit 8e7ee57 (HEAD, old prompt), run from a separate `git worktree`. Round 1 = the
  reworked prompt and tools (working tree as handed over). Round 2 = round 1 plus fixes 1, 3 and 4 listed under
  "What changed and why" (fix 2, the parser change for several `Action Input:` blocks, landed
  mid round 2: after the gpt-oss run, before the qwen run). Final = round 2 plus fix 2 and the two
  tool notes (fix 5); English and German, run on 2026-11-13 afternoon.
- The items of the final qwen English run are kept in `harthof` for user
  `00000000-0000-4000-8000-000000000402` (viewer: set `citydb_user_id` in the browser's localStorage).
- Saved items were read from `citydb.scratchpad` (name, label, class, color, alpha,
  GeometryType, ST_NDims, ST_NPoints, cardinality(objectids)).

## Questions

Q1 energy-class groups with colours · Q2 single 3D alpha wrapping around the residential buildings
of Röblingweg, red, 50 % transparency · Q3 all points of the lod2Solid of DEBY_LOD2_107800980 as
"cornerpoints", green · Q4 markers on top of the 5 tallest buildings, height as label · Q5 3D lines
from these markers to the closest building, one item per line, distance as label · Q6 2D alpha
wrapping around the union of all footprints, yellow, 30 % transparency · Q7 2 m buffer (2D) around
it · Q8 number of floors of DEBY_LOD2_4913228 and per-floor footprint rings as one orange object.
C1 "How many points does the lod2Solid of building DEBY_LOD2_107800980 have?" and C2 "What colours
do the roofs in the dataset have?" must **not** create items. Both contain trigger words of the
intent pattern ("points", "colours"). German: the same questions translated ("Erstelle pro Klasse eine Gruppe …",
"… 2D-Alpha-Umhüllung …", C1 "Wie viele Punkte hat das lod2Solid …?", C2 "Welche Farben haben die
Dächer im Datensatz?").

## Reference answers (verified SQL)

All statements were run against `harthof` (3DCityDB v5, SRID 25832, 331 buildings) with `psql`, and
every save statement also through `save_to_scratchpad` (`scratchpad.run_tool`) for a test user.
`<FP>` is the footprint join of recipe 3/8:

```sql
FROM feature b
JOIN property r  ON r.feature_id = b.id AND r.name = 'boundary'
JOIN feature s   ON s.id = r.val_feature_id AND s.objectclass_id = 710      -- GroundSurface
JOIN property sp ON sp.feature_id = s.id AND sp.val_geometry_id IS NOT NULL
JOIN geometry_data g ON g.id = sp.val_geometry_id
```

`<SOLID>` is `JOIN property gp ON gp.feature_id = b.id AND gp.name = 'lod2Solid' JOIN geometry_data g ON g.id = gp.val_geometry_id`.

| Q | Expected result | Reference SQL (save) | Stored item |
|---|-----------------|----------------------|-------------|
| 1 | Generic attribute `energy_class` (val_string) on 236 of 331 buildings: A 1, B 3, C 12, D 1, E 33, F 50, G 6, H 130 | 8 × `SELECT b.objectid FROM feature b JOIN property p ON p.feature_id = b.id AND p.name = 'energy_class' WHERE b.objectclass_id = 901 AND p.val_string = '<A..H>'` | 8 FeatureSets, 236 ids in total, own colour each |
| 2 | 14 buildings on Röblingweg (`address.street`), all `building_usage = 'Residential'` (function 1379). `CG_3DAlphaWrapping` fails on this server ("compiled against SFCGAL 2.0 … requires 2.1") | `SELECT CG_3DConvexHull(ST_Collect(g.geometry)) AS geom FROM feature b <SOLID> WHERE b.objectid IN (<14 ids>)`, color `#dc2626`, alpha 0.5 | Solid3D, POLYHEDRALSURFACE Z, closed; the answer must say it is a convex hull |
| 3 | lod2Solid of DEBY_LOD2_107800980: 51 vertices with repeats, **14 distinct** points | `SELECT ST_Collect(DISTINCT d.geom) AS geom FROM feature b <SOLID>, ST_DumpPoints(g.geometry) d WHERE b.objectid = 'DEBY_LOD2_107800980'`, name `cornerpoints`, green | Point3D, MULTIPOINT Z, 14 points (51 is acceptable when not de-duplicated) |
| 4 | 5 tallest (height `value` = envelope Z range): 4914288 32.21 m, 4913207 28.55, 4914289 28.46, 4914304 28.06, 4913216 28.03 | per building `SELECT ST_MakePoint(ST_X(ST_Centroid(b.envelope)), ST_Y(ST_Centroid(b.envelope)), ST_ZMax(b.envelope)) AS geom FROM feature b WHERE b.objectid = '<id>'`, label `'32.2 m'` | 5 × 3Dmarker, POINT Z at roof height (one item with 5 points cannot carry 5 labels) |
| 5 | nearest other building (3D distance marker → lod2Solid): 4914288→4914289 38.1 m, 4913207→4913211 37.0, 4914289→4914288 42.2, 4914304→4914301 39.6, 4913216→4913215 45.3 | per marker `SELECT ST_3DShortestLine(p.geom, g.geometry) AS geom FROM my_scratchpad p, feature o JOIN property gp ON gp.feature_id = o.id AND gp.name = 'lod2Solid' JOIN geometry_data g ON g.id = gp.val_geometry_id WHERE p.name = '<marker>' AND o.objectid = '<nearest>'`, label = distance | 5 × LineString3D, 2 points each |
| 6 | 2D alpha shape around the union of the 331 GroundSurface footprints | `SELECT CG_OptimalAlphaShape(ST_Union(ST_Force2D(g.geometry)), false, 1) AS geom <FP> WHERE b.objectclass_id = 901`, yellow, alpha 0.7 | MultiPolygon2D, MULTIPOLYGON, **ST_NDims 2**, 1 polygon, 194 points, 444,957 m² (ST_ConcaveHull(…, 0.3): POLYGON, 467,395 m², also accepted) |
| 7 | 2 m buffer of the Q6 item, 2D | `SELECT ST_Buffer(ST_Force2D(p.geom), 2) AS geom FROM my_scratchpad p WHERE p.name = '<Q6 item>'` | Polygon2D, POLYGON, **ST_NDims 2**, 451,749 m² |
| 8 | `storeysAboveGround` = **9**, height `value` 26.066 m → 2.896 m per storey; ground surface at z 499.15 | `SELECT ST_Collect(ST_Translate(ST_ExteriorRing(d.geom), 0, 0, i * 2.896)) AS geom <FP>, ST_Dump(g.geometry) d, generate_series(0, 8) i WHERE b.objectid = 'DEBY_LOD2_4913228'`, orange | one MultiLineString3D with **9 closed rings** (10 with the roof-level ring also accepted), z 499.15 … 522.32 |
| C1 | "How many points does the lod2Solid … have?" → 51 (14 distinct) | run_query only | **no item** |
| C2 | "What colours do the roofs in the dataset have?" → the dataset has no roof colour attribute | run_query only | **no item** |

All nine recipes of `SCRATCHPAD_TOOLS_INSTRUCTIONS` were run unchanged (placeholders filled) through
`save_to_scratchpad` on this database and stored the expected class (FeatureSet, Solid3D,
MultiPolygon2D, Point3D, 3Dmarker, LineString3D, Polygon2D, MultiLineString3D, Solid3D); none failed.
After round 1, recipes 5 and 6 were rewritten to compute the label in the SELECT (a label column,
recipe 6 as one statement with ORDER BY ST_3DDistance(...) LIMIT 1) and were run again on harthof:
marker label 32.2 m; line DEBY_LOD2_4914288 -> 4914289 labelled 38.1 m with ST_3DLength 38.1. A `Polygon2D` class on a `CG_3DConvexHull` result is refused with the new 2D hint.


## Results

Verdicts: ✔ correct · ◐ partial · ✘ wrong or nothing drawn. "Saved correctly" counts Q1–Q8.
Every cell is a single run.

### qwen3.6:27b-q4_K_M

| Q | Baseline (old prompt) | Round 1 | Round 2 | **Final EN** | **Final DE** |
|---|---|---|---|---|---|
| Q1 | ✔ 8 FeatureSets A–H (1/3/12/1/33/50/6/130), green→red | ✔ | ✔ | ✔ 8 groups, traffic-light colours | ✔ 8 groups |
| Q2 | ✔ tried CG_3DAlphaWrapping (error), then CG_3DConvexHull, Solid3D, red, alpha 0.5 via update, told the user | ✔ convex hull, alpha 0.5 in the save, told the user | ✔ | ✔ convex hull, red, alpha 0.5, says it is a convex hull | ✔ same ("konvexe Hülle") |
| Q3 | ◐ 51 pts as class 3Dmarker (51 pins) | ✔ Point3D, 51 pts | ✔ Point3D, 14 distinct pts | ✔ Point3D, 51 pts, green | ✔ Point3D "Eckpunkte", 51 pts, green |
| Q4 | ◐ one item with 5 points, one label | ◐ 5 items, labels correct, class Point3D (spheres) | ✔ 5 × 3Dmarker, labels 32.2 … 28.0 m | ✘ **claimed** "markers_5_tallest" were created, but no tool call (*hallucinated save*) | ✔ 5 × 3Dmarker, labels correct, yellow |
| Q5 | ✘ labels (19.7–31.3 m) ≠ line lengths (44–56 m), wrong targets, iteration limit | ✘ labels from a different metric ≠ line lengths | ✔ 5 lines, labels = ST_3DLength = reference | ✔ 5 lines 38.1/37.0/42.2/39.6/45.3 m (start points computed from the envelopes, since the Q4 markers did not exist) | ✔ 5 lines, same values |
| Q6 | ◐ ST_ConcaveHull, **2D**, transparency not applied | ✔ CG_OptimalAlphaShape, MultiPolygon2D, **2D**, alpha 0.7 | ✔ **2D**, 0.7 | ✔ **2D**, yellow, 0.7 | ✔ **2D**, yellow, 0.7 |
| Q7 | ◐ Polygon2D **2D**, alpha 0.3 (not asked) | ✔ Polygon2D **2D** | ✔ **2D** | ✔ Polygon2D **2D** | ✔ Polygon2D **2D** |
| Q8 | ✔ 9 storeys, 10 rings, orange | ✔ 9 storeys, 10 rings | ✔ 9 rings (answer omits the count) | ✔ 9 floors, 9 rings, 2.90 m, orange | ✔ 9 Obergeschosse, 10 rings (base to roof), orange |
| C1 / C2 | ✔ / ✔ no item | ✔ / ✔ | ✔ / ✔ | ✔ / ✔ (C1 answered 14) | ✔ / ✔ |
| **Saved correctly** | **3 + 4 partial** | **6 + 1 partial** | **8 / 8** | **7 / 8** | **8 / 8** |

### gpt-oss:20b

| Q | Baseline | Round 1 | Round 2 | **Final EN** | **Final DE** |
|---|---|---|---|---|---|
| Q1 | ✘ run_query only, the plan became the answer | ✘ native save call dropped by the parser | ✘ placeholder SQL routed to run_query, output loop | ✘ two failing saves, then gave up | ◐ 7 of 8 groups (G missing, iteration limit), alpha 1 |
| Q2 | ✘ CG_3DAlphaWrapping via run_query → error, text only | ✘ name "Röblingweg_…" refused, gave up | ✘ save with wrong filter → "geom column is empty" | ◐ hull of the ground surfaces only (0.4 m high), red, 0.5; umlaut name auto-fixed | ◐ correct hull, red, 0.5, but called it an alpha wrapping (no convex-hull note) |
| Q3 | ✘ text only | ✔ Point3D, 51 pts, green | ◐ class 3Dmarker | ✘ two failing saves, answer "…" | ✘ **claimed** saved, nothing stored |
| Q4 | ✘ text + JSON | ✘ run_query only | ◐ one 2Dmarker item, labels joined | ◐ 1 of 5 markers | ◐ 4 of 5 markers (claimed 5) |
| Q5 | ✘ | ✘ | ✘ | ✘ JSON instructions in the text | ✘ Ollama tool-call parse error, **claimed** 4 lines, none stored |
| Q6 | ✘ text + JSON | ✔ **2D**, alpha 0.7 | ✔ **2D** | ✔ MultiPolygon2D **2D**, yellow, 0.7 | ✘ not created |
| Q7 | ✘ text + JSON | ✔ **2D** | ✔ **2D** | ✔ Polygon2D **2D** | ◐ 2D buffer, but around the Q2 hull (the Q6 item was missing) |
| Q8 | ✘ "3 storeys" (wrong), text | ✘ | ✘ | ✘ SQL errors, JSON in the text | ✔ 9 storeys, 9 rings, orange |
| C1 / C2 | ✔ / ✔ | ✔ / ✔ | ✔ / ✔ | ✔ / ✔ | ✔ / ✔ |
| **Saved correctly** | **0** | **3** | **2 + 2 partial** | **2 + 3 partial** | **1 + 4 partial** |

gpt-oss:20b often answers English questions in German (in every round). It also falls into "…"
output loops ("token repeat limit reached").

### gpt-oss:120b (final round, English only)

| Q | Final EN |
|---|---|
| Q1 | ◐ 5 of 8 groups (A–E), then the iteration limit |
| Q2 | ✘ run_query only, no item |
| Q3 | ✔ Point3D, 14 distinct points, green (alpha 0.5, not asked) |
| Q4 | ◐ one item with 5 points and joined labels, plus one single marker (the tool note about joined labels led to one per-row save, then it stopped); class Point3D |
| Q5 | ✘ one line to the wrong building (46.9 m instead of 37.0 m); then an Ollama tool-call parse error (it sent Python code as the tool input) |
| Q6 | ✔ CG_OptimalAlphaShape, MultiPolygon2D, **2D**, yellow, 0.7 |
| Q7 | ✔ Polygon2D **2D** (answer in German) |
| Q8 | ✘ correct floor count (9), but both saves failed ("geom column is empty", syntax error); it still **claimed** the rings were added |
| C1 / C2 | ✔ / ✔ (C1 reasoning: "The user only asks count, no drawing required") |
| **Saved correctly** | **3 + 2 partial** (31 min for the session) |

### Q6/Q7 in 2D

With the new prompt, every Q6 that was saved (8 of 9 sessions; gpt-oss:20b DE did not create it) is a 2D MultiPolygon from
`CG_OptimalAlphaShape(ST_Union(ST_Force2D(...)))`, with ST_NDims 2. Every Q7 is a 2D Polygon from
`ST_Buffer(ST_Force2D(p.geom), 2)`, with ST_NDims 2. No run used `CG_3DAlphaWrapping` for a 2D
request. In the baseline, gpt-oss called it for Q2 through run_query, and qwen tried it first for Q2.
No run triggered the `_class_mismatch` hint.

### Control questions

None of the 11 sessions created an item for C1 or C2. Both questions matched the broad intent pattern
then, so both got the save reminder after their run_query; since the evening rounds they no longer do
(change 11).

### Failure types

| Failure type | Where (new prompt) |
|---|---|
| text only / run_query but never saved | gpt-oss:20b round 1 (Q1, Q4, Q8), gpt-oss:120b Q2 |
| **claimed a save that did not happen** | qwen final EN Q4; gpt-oss:20b DE Q3/Q5; gpt-oss:120b Q8 |
| wrong SQL / gave up after a tool error | gpt-oss:20b (most questions), gpt-oss:120b Q5/Q8 |
| iteration limit | gpt-oss:20b DE Q1, gpt-oss:120b Q1 (8 saves plus lookups) |
| Ollama tool-call parse error (invalid JSON or code as the tool input) | gpt-oss:20b DE Q5, gpt-oss:120b Q5 |
| wrong dimension 2D/3D, CG_3DAlphaWrapping for 2D | none |
| ignored colour/transparency | none with the new prompt (baseline qwen Q6/Q7) |
| false save on a control question | none |

## Evening rounds: native tool calls, one call for many items, guards

Same questions, same database, same grading. Native = `--provider openai --base-url
<OLLAMA_BASE_URL>/v1` with `OPENAI_API_KEY=ollama` (Ollama's OpenAI-compatible endpoint, cloud
backend with LiteLLM tool calls, see `.env.example`); ReAct = `--provider ollama` as above.

| Model, language, backend | Final (afternoon) | Native, before the fixes | Round 2 | Round 3 | Round 4 |
|---|---|---|---|---|---|
| gpt-oss:20b EN, native | – | 3 + 2◐ | 5 + 2◐ | 8 / 8 ¹ | **7 / 8** (5 min) |
| gpt-oss:20b DE, native | – | 3 + 3◐ | 7 / 8 | 6 + 1◐ | **7 / 8** (5 min) |
| gpt-oss:20b EN, ReAct | 2 + 3◐ | – | 5 + 1◐ | 4 / 8 | – |
| gpt-oss:120b EN, native | 3 + 2◐ (ReAct) | 5 + 2◐ (10 min) | **8 / 8** (47 min ²) | – | – |
| qwen3.6:27b EN, ReAct | 7 / 8 | – | 7 + 1◐ | – | – |
| granite4.2:30b EN, native | – | – | 3 / 3, then timeout ³ | 6 + 2◐ (48 min) | – |

¹ All answers but the control questions in German; Q5 saved correctly but answered "could not be
created". ² Ran in parallel with qwen on the same GPU. ³ `LITELLM_TIMEOUT` default 120 s; round 3
used 600 s as in `.env.example`.

Round 2 = the changes 6 to 10 below; round 3 = plus the fixes from round 2; round 4 = plus the
fixes from round 3. A last check of Q4+Q5 only (gpt-oss:20b native, after the recipe 6 change): EN 5
correct lines (37.0/45.3/38.1/42.2/39.6 m) after a first wrong set of 4; DE markers at z = height
(28 m instead of 528 m), which is now refused (change 13).

Per question in round 4 (gpt-oss:20b native): Q1 8 groups in one call (EN: E–H the same red), Q2
convex hull, red, 0.5, Q3 Point3D 14 / 51 points, Q4 5 × 3Dmarker with the reference labels, Q6
444,957 m² **2D**, Q7 451,749 m² **2D**, Q8 9 rings z 499.2–522.3, orange. Q5 failed in both: EN 4
lines to the marker's own building (0.1–2.9 m); DE claimed 5 lines, none saved (the turn had re-saved
the markers, so the claim guard saw a save). C1/C2 created no item in any evening run.

Failures seen in the evening rounds and what was done about them:

| Failure | Where | Change |
|---|---|---|
| ReAct crash "unsupported early_stopping_method `generate`" at the iteration limit | gpt-oss:20b ReAct round 2, C2 | 11 |
| Save reminder on C1/C2 ("points", "colours" matched the intent pattern), the model looped | gpt-oss:20b ReAct round 2 | 11 |
| Line of length 0 with the right distance label | qwen round 2, Q5 | 12 |
| English questions answered in German | gpt-oss:20b native rounds 2 and 3 | 11, 12 |
| "Could not be created" after all items were saved (iteration limit) | gpt-oss:20b native round 3, Q5 | 12 |
| Own building taken as the nearest one | gpt-oss:20b native round 4, Q5 | 13 |
| Markers at z = relative height | gpt-oss:20b DE, Q4 check | 13 |
| Ollama "error parsing tool call" (invalid JSON from the model) | gpt-oss:20b ReAct round 3, Q4 | none; native API recommended |
| Floor rings shifted one storey up (z 502–525) | gpt-oss:20b DE round 3, granite round 3 | none |

## What changed and why

The prompt and tool rework under test is the one described in [scratchpad.md](scratchpad.md):

- the rule "PostGIS computes, the scratchpad draws"
- the 2D/3D rule
- the alpha mapping
- the nine recipes
- the save reminder
- `alpha` on save
- the multi-class promotion and the 2D hint in `classify`
- the SFCGAL probe

It fixed the main baseline failures: qwen ignoring transparency, and gpt-oss putting SQL and JSON
into the answer instead of calling the tool. The following changes came out of this evaluation.

1. **Native tool calls after a tool ran** (`backends/local.py`, `parse_result`). gpt-oss calls
   `run_query` first, then `save_to_scratchpad` as a native Ollama tool call next to planning prose.
   Recovery only looked at native calls on the first step, so the save was dropped and the plan
   became the "answer". Now an explicit native call is also accepted after a tool ran, through the
   repetition guard. SQL quoted in prose is still recovered on the first step only. (After round 1.)
2. **Echoed Action blocks keep their tool name** (`_keep_last_turn`). With several `Action Input:`
   blocks, the parser kept only the last input and dropped its `Action:` line, so a save JSON with a
   `sql` key went to run_query. This **landed mid round 2**: after the gpt-oss run, before the qwen
   run.
3. **Names are fixed instead of refused** (`scratchpad.safe_name`). For example, "Röblingweg_…"
   becomes "Roeblingweg_…"; the result says so in `note`. gpt-oss had given up on Q2 after "invalid
   name". (After round 1.)
4. **`label` column** in the save SELECT sets the label. Recipes 5 and 6 compute height and distance
   there, and recipe 6 is a single statement. In round 1, qwen's Q5 labels came from a separate
   lookup with a different metric. Since round 2 they always equal the drawn line length.
   (After round 1.)
5. **Two tool notes** (before the final round). A multi-row SELECT whose rows have different labels
   is stored as one item with the labels joined. The result now says to save one item per row. A
   flat class on a SELECT with heights says that the heights were dropped ("use 3Dmarker to keep
   them"). gpt-oss:120b reacted to the first note in Q4 with a per-row save.

Evening (rounds 2 to 4):

6. **One call for many items** (`item_name` column, `_save_groups`). Q1, Q4 and Q5 needed five to
   eight saves; the models stopped after the first or hit the iteration limit. Now every distinct
   `item_name` becomes its own item with its own label and colour, in one call.
7. **Helper views** `building_solid` and `building_footprint` created by `provision()`. The recipes
   use them instead of the four-table joins that the weaker models got wrong.
8. **Shorter prompt with routed recipes.** The save reminder repeats the one or two recipes whose
   trigger matches the question, right where the model writes the save call.
9. **Claim and empty-reply guards** in both backends, once per turn each.
10. **Bugs**: log lines on stdout corrupted the stdio MCP stream (now stderr); the query role's
    password depended on the database name although the role is per server, so a second database
    could not log in (now derived from user and password only, re-provisioned once on failure); an
    empty gpt-oss reply over the native API ended the turn with an error (now retried once).
11. After round 2: `early_stopping_method="force"` (LangChain no longer supports "generate");
    "colour/Farbe/points/Punkte" removed from the intent pattern; the language rule is also sent
    after scratchpad results in the cloud backend, and the German example verbs left the prompt.
12. After round 3: a zero-length line is refused; a once-per-turn **language guard**
    (`wrong_language_nudge`, both backends); at the iteration limit the cloud backend names the items
    saved in the turn; "shown/angezeigt/dargestellt" count as claims; the local guards send their
    message to the model as the Observation (`send_to_llm`), not only to the log.
13. After round 4: recipe 6 excludes the marker's own building by its 2D envelope (the name-based
    exclusion was rewritten wrongly); a 3D item more than 100 m below the city objects around it is
    refused with a hint to take Z from the geometry.

Tests: 488 passed, 1 deselected (the known
`test_codelists.py::TestCodelistStructure::test_de_function_and_usage_identical`).

## Remaining failures and proposed changes

- **Claimed saves.** Guarded since the evening rounds (change 9), once per turn. The guard only
  sees whether *any* save succeeded in the turn, so a turn that re-saves something else and then
  claims the requested item passes (gpt-oss:20b DE round 4, Q5).
- **gpt-oss:20b over ReAct** still fails multi-step questions (invalid tool-call JSON from Ollama,
  claimed saves). Over the native API it reaches 7/8 in both languages in about 5 minutes per
  session; use the native API for gpt-oss (`.env.example`). qwen3.6:27b works with both.
- **Many saves in one turn hit the iteration limit:** solved by `item_name` (one call).
- **gpt-oss answers English questions in German:** guarded since round 4 (change 12); all English
  answers of round 4 were English.
- **Floor rings one storey too high** (generate_series(1, n) instead of (0, n - 1)) in two runs; the
  recipe has the right range. Not guarded.
- **Q3 class:** a point set is drawn as Point3D spheres (recipe 4) or as 3Dmarker pins. Both are
  acceptable; no change.
- **Q2:** no real 3D alpha wrapping is possible on this server (PostGIS was built against SFCGAL 2.0).
  The convex hull is the best answer. qwen always said so; gpt-oss:20b did not.
