# Passau flood demo: notes

Demo material for the flood scene (see `production/docs/flood-scene.md`). Nothing in this file is
loaded into the system prompt; the chatbot learns the Passau context from the opening message below.

## Opening message (paste as the first chat message)

```
Kontext für dieses Gespräch: Die Datenbank enthält das LoD2-Stadtmodell von Passau
(Altstadt, Innstadt, Ilzstadt). Alle Höhen sind absolute Höhen in m NHN.
Der Pegelnullpunkt des Pegels Passau/Donau liegt bei 286,44 m NHN;
Wasserstand in m NHN = 286,44 + Pegelstand in m.
Meldestufen: 1 = 7,00 m, 2 = 7,40 m, 3 = 7,70 m, 4 = 8,50 m.
Beim Hochwasser im Juni 2013 erreichte der Pegel 12,89 m.
Adressen stehen als „Straße Hausnummer" in address.street.
Bitte bestätige kurz, dann stelle ich meine Fragen.
```

## Storyboard questions and expected answers

Water level 2013 = 286.44 + 12.89 = **299.33 m NHN**. Counts exclude bridges, walls and canopies
(`FLOOD_EXCLUDE_FUNCTIONS`).

| Question | Expected |
|---|---|
| Wie viele Gebäude stehen bei 12,89 m Pegel unter Wasser? | 994 (viewer counter: 994) |
| Und bei Meldestufe 4? | 95 |
| Und 50 cm höher als 2013? | 1,118 |
| Welche kritischen Einrichtungen sind betroffen? | Kläranlage Passau-Haibach (22 buildings) + Pumpwerk = 23 (summarised, not one row each), Rehazentrum Jesuitenschlößl, 17 schools/Kitas, Rathaus |
| Wie viel Fassadenfläche steht unter Wasser? | ≈ 142,984 m² |
| Was bringt ein Objektschutz von 1 m? | 994 affected, 241 protected, 753 still flooded |
| Und wenn es 20 cm höher kommt (mit 1 m Schutz)? | ~50 of the 241 protected buildings flood (at 299.52 m) |
| Ich wohne in der Bräugasse 20 – bin ich betroffen, und wo ist es trocken? | yes, 4.02 m; Jesuitengasse 3, 136 m west (azimuth 278°) |

## Facts about the data

- Bavarian LoD2 open data, CRS EPSG:25832, heights in m NHN (DHHN2016); tiles 828_5388 and 830_5388.
- Base height of a building = `ST_ZMin(feature.envelope)` (its lowest ground point).
- Non-buildings: function `51007_1500` (historic wall), `51009_1610` (canopy), `51009_1700` (wall),
  `53001_1800` (bridge).
- `name` holds facility names, e.g. 'Kläranlage Passau-Haibach', 'Rathaus', 'Gymnasium Leopoldinum Passau'.
- `address.street` holds street and house number, e.g. 'Bräugasse 20' (`house_number` and `zip_code` are empty).
- Gauge zero Passau/Donau: 286.44 m NHN (HND Bayern / WSV master data).

## SQL recipes (reference)

Tested against the Passau data. Useful to check the chatbot's queries, as ground truth for the
benchmark, or to paste into the chat if a local model gets stuck.

1. How many buildings are flooded at water level L (here 299.33):
SELECT count(*) AS betroffen, 299.33 AS flood_level_m
FROM feature f JOIN property fn ON fn.feature_id = f.id AND fn.name = 'function'
WHERE f.objectclass_id = 901 AND ST_ZMin(f.envelope) < 299.33
  AND fn.val_string NOT IN ('51007_1500','51009_1610','51009_1700','53001_1800');

2. Critical facilities that are flooded (schools, Kitas, care, hospitals, fire brigade, town hall, named facilities):
SELECT f.objectid, fn.val_string AS funktion, n.val_string AS name, a.street AS adresse,
       round((299.33 - ST_ZMin(f.envelope))::numeric, 2) AS wassertiefe_m, 299.33 AS flood_level_m
FROM feature f
JOIN property fn ON fn.feature_id = f.id AND fn.name = 'function'
LEFT JOIN property n ON n.feature_id = f.id AND n.name = 'name'
LEFT JOIN property ap ON ap.feature_id = f.id AND ap.name = 'address'
LEFT JOIN address a ON a.id = ap.val_address_id
WHERE f.objectclass_id = 901 AND ST_ZMin(f.envelope) < 299.33
  AND (fn.val_string IN ('31001_3012','31001_3020','31001_3051','31001_3052','31001_3065','31001_3072')
       OR n.val_string ILIKE '%Kläranlage%' OR n.val_string ILIKE '%Pumpwerk%')
ORDER BY funktion, name;

3. Facade area under water at level L (m², true 3D area of each wall times its flooded share):
SELECT round(sum(ST_3DArea(g.geometry) * LEAST(GREATEST(
         (299.33 - ST_ZMin(g.geometry)) / NULLIF(ST_ZMax(g.geometry) - ST_ZMin(g.geometry), 0), 0), 1))::numeric) AS fassade_unter_wasser_m2,
       299.33 AS flood_level_m
FROM feature w JOIN geometry_data g ON g.feature_id = w.id
WHERE w.objectclass_id = 709 AND ST_ZMin(g.geometry) < 299.33;

4. Effect of a property-level barrier of height H (here 1.0 m) at level L:
SELECT count(*) FILTER (WHERE ST_ZMin(f.envelope) < 299.33) AS betroffen,
       count(*) FILTER (WHERE ST_ZMin(f.envelope) < 299.33 AND ST_ZMin(f.envelope) + 1.0 >= 299.33) AS geschuetzt,
       count(*) FILTER (WHERE ST_ZMin(f.envelope) + 1.0 < 299.33) AS trotz_schutz_unter_wasser,
       299.33 AS flood_level_m, 1.0 AS protection_m
FROM feature f JOIN property fn ON fn.feature_id = f.id AND fn.name = 'function'
WHERE f.objectclass_id = 901
  AND fn.val_string NOT IN ('51007_1500','51009_1610','51009_1700','53001_1800');
   For "what if the water rises X cm higher": run the same query with L + X/100 and the same H.

5. Is the building at an address flooded, and which is the nearest dry building:
WITH me AS (
  SELECT f.objectid, f.envelope FROM feature f
  JOIN property p ON p.feature_id = f.id AND p.name = 'address'
  JOIN address a ON a.id = p.val_address_id
  WHERE a.street = 'Bräugasse 20')
SELECT me.objectid, 'Ihr Haus' AS rolle, round(ST_ZMin(me.envelope)::numeric, 2) AS hoehe_m,
       round((299.33 - ST_ZMin(me.envelope))::numeric, 2) AS wassertiefe_m,
       NULL::numeric AS distanz_m, NULL::numeric AS azimut_grad, 299.33 AS flood_level_m
FROM me
UNION ALL
SELECT * FROM (
  SELECT d.objectid, 'nächstes trockenes Gebäude', round(ST_ZMin(d.envelope)::numeric, 2), NULL::numeric,
         round(ST_Distance(d.envelope, me.envelope)::numeric),
         round(degrees(ST_Azimuth(ST_Centroid(me.envelope), ST_Centroid(d.envelope)))::numeric), 299.33
  FROM feature d CROSS JOIN me
  WHERE d.objectclass_id = 901 AND ST_ZMin(d.envelope) > 299.33
  ORDER BY d.envelope <-> me.envelope LIMIT 1) dry;
   Azimuth to compass point: 0 N, 45 NO, 90 O, 135 SO, 180 S, 225 SW, 270 W, 315 NW (nearest).

6. Damage curve (buildings flooded per gauge reading; draw it as a mermaid xychart-beta line chart):
SELECT round((l - 286.439)::numeric, 1) AS pegel_m, count(*) FILTER (WHERE ST_ZMin(f.envelope) < l) AS betroffen
FROM feature f JOIN property fn ON fn.feature_id = f.id AND fn.name = 'function',
     generate_series(293.439, 300.439, 0.5) AS l
WHERE f.objectclass_id = 901
  AND fn.val_string NOT IN ('51007_1500','51009_1610','51009_1700','53001_1800')
GROUP BY l ORDER BY l;
