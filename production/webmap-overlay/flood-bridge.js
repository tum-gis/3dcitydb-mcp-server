// Flood scene for the 3DCityDB Web Map Client (loaded after highlight-bridge.js).
//
// Parent -> viewer message (same origin only, sent by window._sendHighlight in app.py):
//   {type: "flood", level, protection, from_level, gauge, extent,
//    buildings: [[objectid, base_m], ...], extra_ids: {objectid: [tile_id]},
//    focus: [objectid], centroid}
//
// Draws a water plane at `level` over the dataset extent and colours every
// building: red = the water reaches it, green = reached but held back by a
// property-level barrier of height `protection`, light grey = dry. Heights are
// the raw z values of the data, the same convention the tiles are placed with,
// so water, buildings and a terrain in the same height system line up.
//
// Tile features are boundary surfaces; a surface is mapped to its building by
// objectid prefix ("<building>_<suffix>") or through `extra_ids`. Colours are
// set per feature (feature.color) rather than through a Cesium3DTileStyle, so
// thousands of buildings can be recoloured while the water rises. While the
// scene is active, highlight-bridge.js hands its style over to this file
// (window._citydbFlood), and highlights/selections are drawn on top.
(function () {
    "use strict";

    var ORIGIN = window.location.origin;
    var ID_PROPERTIES = ["objectid", "id", "GMLID", "gmlid"];
    var DE = (navigator.language || "").toLowerCase().indexOf("de") === 0;
    var T = DE ? {
        affected: "Gebäude betroffen", flooded: "unter Wasser", protectedBy: "geschützt",
        level: "Wasserstand", gauge: "Pegel", stage: "Meldestufe", clear: "Flut ausblenden",
        dry: "Alle Gebäude trocken", barrier: "Objektschutz",
        dryLabel: "trocken", straight: "Luftlinie",
    } : {
        affected: "buildings affected", flooded: "flooded", protectedBy: "protected",
        level: "Water level", gauge: "Gauge", stage: "Warning level", clear: "Hide flood",
        dry: "All buildings dry", barrier: "Barrier",
        dryLabel: "dry", straight: "straight line",
    };

    var C = {
        flooded: Cesium.Color.fromCssColorString("#dc2626"),
        protectedB: Cesium.Color.fromCssColorString("#16a34a"),
        dry: Cesium.Color.fromCssColorString("#e5e7eb"),
        // Route view (a few focus buildings): the affected one, e.g. "my house",
        // and the dry ones nearby.
        focusFlooded: Cesium.Color.fromCssColorString("#b91c1c"),
        focusDry: Cesium.Color.fromCssColorString("#15803d"),
        highlight: Cesium.Color.fromCssColorString("#ff5a00"),
        selection: Cesium.Color.fromCssColorString("#a855f7"),
        // Faded variants: used for everything else while some buildings are
        // highlighted or in focus, so those stand out against the flood.
        floodedFaint: Cesium.Color.fromCssColorString("#f5b5b5"),
        protectedFaint: Cesium.Color.fromCssColorString("#b7e0c3"),
        dimmed: Cesium.Color.fromCssColorString("#f1f2f4"),
    };

    var RECOLOR_INTERVAL_MS = 100;
    var ROUTE_MAX_FOCUS = 6;  // up to this many focus buildings: route view (camera, lines)

    // ── scene state ──────────────────────────────────────────────────────────
    var scene = null;          // last message
    var byId = new Map();      // building objectid -> index
    var extraById = new Map(); // tile id -> index (surfaces breaking the prefix rule)
    var bases = new Float64Array(0);
    var focusIdx = new Set();
    var level = null;          // currently shown water level
    var stamp = 0;             // bumped on every recolour; contents remember theirs
    var contents = new Map();  // tile content -> Int32Array building index per feature
    var hooked = new WeakSet();
    var water = null, waterCenter = null, waterUp = null;
    var anim = null, lastRecolor = 0;
    var savedDepthTest = null;
    var everShown = false;
    var routeLayer = null;     // dashed lines + distance labels of the route view

    function bridge() { return window._citydbBridge || {}; }
    function tilesets() { return bridge().getTilesets ? bridge().getTilesets() : []; }

    // ── feature -> building ──────────────────────────────────────────────────
    function featureId(feature) {
        for (var i = 0; i < ID_PROPERTIES.length; i++) {
            var v = feature.getProperty(ID_PROPERTIES[i]);
            if (v !== undefined && v !== null) return String(v);
        }
        return null;
    }

    function buildingIndex(id) {
        if (id === null) return -1;
        if (byId.has(id)) return byId.get(id);
        if (extraById.has(id)) return extraById.get(id);
        var s = id;
        for (var k = 0; k < 3; k++) {
            var cut = s.lastIndexOf("_");
            if (cut <= 0) break;
            s = s.slice(0, cut);
            if (byId.has(s)) return byId.get(s);
        }
        return -1;
    }

    function indexContent(content) {
        var n = content.featuresLength || 0;
        var idx = new Int32Array(n);
        for (var i = 0; i < n; i++) idx[i] = buildingIndex(featureId(content.getFeature(i)));
        return { idx: idx, stamp: -1 };
    }

    // ── colouring ────────────────────────────────────────────────────────────
    // Focus buildings: in the route view (a few buildings, e.g. an address and
    // the nearest dry ones) dark red when flooded and dark green when dry; in a
    // longer list orange like a highlight. While anything is emphasised, all
    // other buildings fade, so the emphasised ones are found at a glance.
    function routeView() { return focusIdx.size > 0 && focusIdx.size <= ROUTE_MAX_FOCUS; }

    function statusColor(b, emphasis) {
        if (b < 0) return emphasis ? C.dimmed : C.dry;
        var base = bases[b];
        var p = scene.protection || 0;
        var flooded = base < level;
        if (focusIdx.has(b)) {
            if (routeView()) return flooded ? C.focusFlooded : C.focusDry;
            return flooded ? C.highlight : C.focusDry;
        }
        if (flooded) {
            if (base + p >= level) return emphasis ? C.protectedFaint : C.protectedB;
            return emphasis ? C.floodedFaint : C.flooded;
        }
        return emphasis ? C.dimmed : C.dry;
    }

    // Saved building sets (scratchpad-bridge.js) are drawn on top of the flood
    // in their own colours; the flood keeps its full colours around them.
    function scratchSets() {
        var sp = window._citydbScratchpad;
        return sp && sp.hasSets && sp.hasSets() ? sp : null;
    }

    function colorContent(content, entry, hl, sel) {
        var n = entry.idx.length;
        // While an answer's highlight is shown, the saved sets pause here too.
        var sets = hl.size ? null : scratchSets();
        var emphasis = sel.size > 0 || hl.size > 0 || focusIdx.size > 0;
        for (var i = 0; i < n; i++) {
            var f = content.getFeature(i);
            var c;
            var b = entry.idx[i];
            if (sel.size || hl.size || sets) {
                var id = featureId(f);
                if (sel.has(id)) c = C.selection;
                // Focus buildings are highlighted too; their focus colour wins.
                else if (hl.has(id) && !focusIdx.has(b)) c = C.highlight;
                else if (sets && !focusIdx.has(b)) c = sets.colorFor(id);
            }
            f.color = c || statusColor(b, emphasis);
        }
        entry.stamp = stamp;
    }

    function idSets() {
        var b = bridge();
        return {
            hl: new Set(b.getHighlightIds ? b.getHighlightIds() : []),
            sel: new Set(b.getSelectionIds ? b.getSelectionIds() : []),
        };
    }

    function recolor() {
        if (!scene) return;
        stamp++;
        var s = idSets();
        contents.forEach(function (entry, content) {
            if (content.isDestroyed && content.isDestroyed()) { contents.delete(content); return; }
            colorContent(content, entry, s.hl, s.sel);
        });
        lastRecolor = performance.now();
        updateHud();
    }

    function hookTilesets() {
        tilesets().forEach(function (ts) {
            if (hooked.has(ts)) return;
            hooked.add(ts);
            ts.tileVisible.addEventListener(function (tile) {
                if (!scene) return;
                var content = tile.content;
                if (!content || !content.featuresLength) return;
                var entry = contents.get(content);
                if (!entry) { entry = indexContent(content); contents.set(content, entry); }
                if (entry.stamp !== stamp) {
                    var s = idSets();
                    colorContent(content, entry, s.hl, s.sel);
                }
            });
            ts.tileUnload.addEventListener(function (tile) {
                if (tile.content) contents.delete(tile.content);
            });
        });
    }

    function takeOverStyle() {
        tilesets().forEach(function (ts) {
            ts.style = undefined;
            ts.colorBlendMode = Cesium.Cesium3DTileColorBlendMode.REPLACE;
        });
    }

    // ── water ────────────────────────────────────────────────────────────────
    function waterMaterial() {
        try {
            return Cesium.Material.fromType("Water", {
                baseWaterColor: new Cesium.Color(0.12, 0.36, 0.62, 0.72),
                blendColor: new Cesium.Color(0.2, 0.5, 0.75, 0.72),
                normalMap: Cesium.buildModuleUrl("Assets/Textures/waterNormals.jpg"),
                frequency: 2000.0,
                animationSpeed: 0.02,
                amplitude: 4.0,
                specularIntensity: 0.6,
            });
        } catch (e) {
            console.warn("[flood-bridge] water material unavailable, using plain colour:", e);
            return Cesium.Material.fromType("Color", { color: new Cesium.Color(0.15, 0.4, 0.7, 0.6) });
        }
    }

    function ensureWater(extent) {
        if (water || !extent) return;
        // A little margin so the water reaches beyond the outermost buildings.
        var mx = (extent.east - extent.west) * 0.05, my = (extent.north - extent.south) * 0.05;
        var w = extent.west - mx, e = extent.east + mx, s = extent.south - my, n = extent.north + my;
        waterCenter = Cesium.Cartesian3.fromDegrees((w + e) / 2, (s + n) / 2, 0);
        waterUp = Cesium.Ellipsoid.WGS84.geodeticSurfaceNormal(waterCenter, new Cesium.Cartesian3());
        water = cesiumViewer.scene.primitives.add(new Cesium.Primitive({
            geometryInstances: new Cesium.GeometryInstance({
                geometry: new Cesium.PolygonGeometry({
                    polygonHierarchy: new Cesium.PolygonHierarchy(
                        Cesium.Cartesian3.fromDegreesArray([w, s, e, s, e, n, w, n])),
                    height: 0,
                    vertexFormat: Cesium.EllipsoidSurfaceAppearance.VERTEX_FORMAT,
                }),
            }),
            appearance: new Cesium.EllipsoidSurfaceAppearance({ aboveGround: true, material: waterMaterial() }),
            asynchronous: false,
        }));
        // Terrain in front of the water hides it, so only the flooded area shows.
        savedDepthTest = cesiumViewer.scene.globe.depthTestAgainstTerrain;
        cesiumViewer.scene.globe.depthTestAgainstTerrain = true;
    }

    function setWaterHeight(h) {
        if (!water) return;
        water.modelMatrix = Cesium.Matrix4.fromTranslation(
            Cesium.Cartesian3.multiplyByScalar(waterUp, h, new Cesium.Cartesian3()));
    }

    function removeWater() {
        if (water) cesiumViewer.scene.primitives.remove(water);
        water = null;
        if (savedDepthTest !== null) cesiumViewer.scene.globe.depthTestAgainstTerrain = savedDepthTest;
        savedDepthTest = null;
    }

    // ── level + animation ────────────────────────────────────────────────────
    function gaugeLevels() {
        var g = scene && scene.gauge;
        if (!g || g.zero_m == null) return [];
        return (g.stages_cm || []).map(function (cm, i) {
            return { level: g.zero_m + cm / 100, label: T.stage + " " + (i + 1) };
        });
    }

    function setLevel(h) {
        level = h;
        setWaterHeight(h);
        if (performance.now() - lastRecolor >= RECOLOR_INTERVAL_MS) recolor();
        else updateHud();
    }

    // Rise from `from` to `to`, holding briefly at each warning level in between.
    function animate(from, to) {
        if (anim) cancelAnimationFrame(anim.frame);
        var dist = Math.abs(to - from);
        if (dist < 0.005) { setLevel(to); recolor(); return; }
        var duration = Math.min(Math.max(dist * 600, 1200), 6000); // ms
        var holds = gaugeLevels().filter(function (s) {
            return (s.level - from) * (s.level - to) < 0;
        });
        if (to < from) holds.reverse();
        var HOLD = 700, start = performance.now(), stageLabel = null;
        var schedule = [];  // [t0, t1, l0, l1, label]
        var t = 0, l = from, rate = (to - from) / duration;
        holds.forEach(function (s) {
            var dt = (s.level - l) / rate;
            schedule.push([t, t + dt, l, s.level, null]);
            t += dt;
            schedule.push([t, t + HOLD, s.level, s.level, s.label]);
            t += HOLD;
            l = s.level;
        });
        schedule.push([t, t + (to - l) / rate, l, to, null]);
        var total = t + (to - l) / rate;
        var me = anim = {};
        function step(now) {
            if (anim !== me) return;  // cancelled or replaced
            var e = now - start;
            if (e >= total) { anim = null; stage(null); setLevel(to); recolor(); return; }
            for (var i = 0; i < schedule.length; i++) {
                var seg = schedule[i];
                if (e < seg[1]) {
                    var f = seg[1] > seg[0] ? (e - seg[0]) / (seg[1] - seg[0]) : 1;
                    if (seg[4] !== stageLabel) { stageLabel = seg[4]; stage(stageLabel); }
                    setLevel(seg[2] + (seg[3] - seg[2]) * f);
                    break;
                }
            }
            me.frame = requestAnimationFrame(step);
        }
        me.frame = requestAnimationFrame(step);
    }

    // ── counts ───────────────────────────────────────────────────────────────
    function counts() {
        var p = (scene && scene.protection) || 0, wet = 0, prot = 0;
        for (var i = 0; i < bases.length; i++) {
            if (bases[i] < level) { wet++; if (bases[i] + p >= level) prot++; }
        }
        return { affected: wet, protectedN: prot, flooded: wet - prot };
    }

    // ── HUD ──────────────────────────────────────────────────────────────────
    var hud = null, el = {};

    function fmt(n, d) {
        return n.toLocaleString(DE ? "de-DE" : "en-US",
            { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
    }

    function ensureHud() {
        if (hud) return;
        hud = document.createElement("div");
        // Compact: about 90 px high, so it covers little of the map.
        hud.style.cssText =
            "position:absolute;top:44px;left:8px;z-index:9999;display:flex;gap:9px;" +
            "background:rgba(15,23,42,.85);color:#f8fafc;padding:7px 28px 7px 8px;border-radius:8px;" +
            "font:12px/1.3 system-ui,sans-serif;box-shadow:0 3px 12px rgba(0,0,0,.3);";
        hud.innerHTML =
            '<div data-k="gauge" style="position:relative;width:16px;height:76px;' +
            'background:#f8fafc;border-radius:2px;overflow:hidden;flex:none;align-self:center"></div>' +
            '<div style="display:flex;flex-direction:column;gap:2px;min-width:190px">' +
            '<div><span data-k="count" style="font-size:22px;font-weight:700"></span> ' +
            '<span data-k="countLabel" style="opacity:.85"></span></div>' +
            '<div data-k="split" style="font-size:11px"></div>' +
            '<div><span data-k="level" style="font-weight:600"></span> ' +
            '<span data-k="stage" style="color:#fbbf24;font-weight:600"></span></div>' +
            '<input data-k="slider" type="range" style="width:100%;margin:2px 0 0;height:14px">' +
            '</div>' +
            '<button data-k="clear" style="position:absolute;top:4px;right:5px;cursor:pointer;' +
            'background:none;border:none;color:#cbd5e1;font:15px/1 sans-serif;padding:2px"></button>';
        document.body.appendChild(hud);
        hud.querySelectorAll("[data-k]").forEach(function (n) { el[n.getAttribute("data-k")] = n; });
        el.clear.textContent = "✕";
        el.clear.title = T.clear;
        el.clear.onclick = clearScene;
        el.slider.oninput = function () {
            if (anim) { cancelAnimationFrame(anim.frame); anim = null; stage(null); }
            setLevel(parseFloat(el.slider.value));
        };
        el.slider.onchange = function () { recolor(); };
    }

    // Directly below the client's "Show / Hide Toolbox" button, or below the
    // toolbox itself while it is open, so neither covers the other.
    function placeHud() {
        if (!hud) return;
        var anchor = null;
        var box = document.getElementById("citydb_toolbox");
        if (box && box.offsetParent !== null) anchor = box;
        else {
            var menu = document.getElementById("uiMenu");
            anchor = menu && menu.firstElementChild;
        }
        if (!anchor) return;
        var r = anchor.getBoundingClientRect();
        if (!r.width && !r.height) return;
        hud.style.top = Math.round(r.bottom + 6) + "px";
        hud.style.left = Math.round(r.left) + "px";
    }

    var menuHooked = false;
    function hookMenu() {
        if (menuHooked) return;
        var menu = document.getElementById("uiMenu");
        if (!menu) return;
        menuHooked = true;
        // showToolbox() runs on click; re-place after it has toggled the box.
        menu.addEventListener("click", function () { setTimeout(placeHud, 0); });
        window.addEventListener("resize", placeHud);
    }

    function stage(label) { if (el.stage) el.stage.textContent = label || ""; }

    function gaugeReading(h) {
        var g = scene && scene.gauge;
        return g && g.zero_m != null ? (h - g.zero_m) : null;
    }

    function drawGauge() {
        var g = scene && scene.gauge, box = el.gauge;
        if (!box) return;
        if (!g || g.zero_m == null) { box.style.display = "none"; return; }
        box.style.display = "";
        var maxM = Math.max(14, Math.ceil((gaugeReading(level) || 0) + 1));
        function y(m) { return (1 - Math.min(Math.max(m / maxM, 0), 1)) * 100; }
        var html = "";
        for (var m = 0; m <= maxM; m += 2) {  // 2-metre ticks, like a gauge board
            html += '<div style="position:absolute;left:0;width:' + (m % 4 ? 5 : 9) + 'px;top:' +
                y(m) + '%;border-top:1px solid #0f172a"></div>';
        }
        var r = gaugeReading(level) || 0;
        html += '<div style="position:absolute;left:0;right:0;bottom:0;height:' + (100 - y(r)) +
            '%;background:rgba(37,99,235,.75)"></div>';
        (g.stages_cm || []).forEach(function (cm) {
            html += '<div style="position:absolute;left:0;right:0;top:' + y(cm / 100) +
                '%;border-top:2px solid #f59e0b"></div>';
        });
        if (g.reference_cm != null) {
            html += '<div style="position:absolute;left:0;right:0;top:' + y(g.reference_cm / 100) +
                '%;border-top:3px solid #dc2626"></div>';
        }
        box.innerHTML = html;
    }

    function updateHud() {
        if (!scene || !hud) return;
        var c = counts();
        el.count.textContent = c.affected ? fmt(c.affected) : "0";
        el.countLabel.textContent = c.affected ? T.affected : T.dry;
        el.split.innerHTML = (scene.protection > 0 && c.affected)
            ? '<span style="color:#f87171">■</span> ' + fmt(c.flooded) + " " + T.flooded +
              ' &nbsp;<span style="color:#4ade80">■</span> ' + fmt(c.protectedN) + " " + T.protectedBy +
              " (" + fmt(scene.protection, 1) + " m)"
            : "";
        var r = gaugeReading(level);
        el.level.textContent = T.level + " " + fmt(level, 2) + " m" +
            (r !== null ? " · " + T.gauge + " " + fmt(r, 2) + " m" : "");
        if (document.activeElement !== el.slider) el.slider.value = String(level);
        drawGauge();
    }

    function configureSlider() {
        var lo = scene.from_level != null ? scene.from_level : level - 5;
        var hi = Math.max(scene.level + 2, lo + 1);
        el.slider.min = String(Math.floor(lo * 10) / 10);
        el.slider.max = String(Math.ceil(hi * 10) / 10);
        el.slider.step = "0.01";
    }

    // ── camera ───────────────────────────────────────────────────────────────
    function flyOverview(extent) {
        var w = extent.west, e = extent.east, s = extent.south, n = extent.north;
        var center = Cesium.Cartesian3.fromDegrees((w + e) / 2, (s + n) / 2, scene.level);
        var radius = Cesium.Cartesian3.distance(
            Cesium.Cartesian3.fromDegrees(w, s, 0), Cesium.Cartesian3.fromDegrees(e, n, 0)) / 2;
        cesiumViewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(center, radius * 0.8), {
            offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-38), radius * 1.6),
            duration: 2.5,
        });
    }

    // Route view: look at the anchor building (e.g. "my house") from the side
    // facing away from the dry buildings, so they lie behind it in the picture.
    function flyRoute(msg) {
        var pos = msg.focus_positions || {};
        var home = msg.anchor && pos[msg.anchor];
        if (!home) return false;
        var target = Cesium.Cartesian3.fromDegrees(home.lon, home.lat,
            Math.max(msg.level, home.zmin || msg.level));
        var heading = cesiumViewer.camera.heading;
        var others = Object.keys(pos).filter(function (o) { return o !== msg.anchor; });
        if (others.length) {
            var to = pos[others[0]];
            var dx = (to.lon - home.lon) * Math.cos(Cesium.Math.toRadians(home.lat));
            var dy = to.lat - home.lat;
            if (dx || dy) heading = Math.atan2(dx, dy);
        }
        var far = others.reduce(function (m, o) { return Math.max(m, pos[o].distance_m || 0); }, 0);
        cesiumViewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(target, 15), {
            offset: new Cesium.HeadingPitchRange(heading, Cesium.Math.toRadians(-35),
                Math.min(Math.max(far * 1.1, 120), 450)),
            duration: 2.5,
        });
        return true;
    }

    // Dashed line from the anchor to each dry focus building, with its distance.
    function drawRoute(msg) {
        clearRoute();
        var pos = msg.focus_positions || {};
        var home = msg.anchor && pos[msg.anchor];
        if (!home) return;
        routeLayer = new Cesium.CustomDataSource("flood-route");
        cesiumViewer.dataSources.add(routeLayer);
        var lift = 2.0;  // just above the water
        Object.keys(pos).forEach(function (oid) {
            if (oid === msg.anchor) return;
            var p = pos[oid], i = byId.get(oid);
            if (i === undefined || bases[i] < msg.level) return;  // only dry buildings
            var h = Math.max(msg.level, p.zmin || msg.level) + lift;
            routeLayer.entities.add({
                polyline: {
                    positions: Cesium.Cartesian3.fromDegreesArrayHeights([
                        home.lon, home.lat, Math.max(msg.level, home.zmin || msg.level) + lift,
                        p.lon, p.lat, h]),
                    width: 4,
                    material: new Cesium.PolylineDashMaterialProperty({
                        color: C.focusDry, gapColor: Cesium.Color.WHITE.withAlpha(0.6), dashLength: 14,
                    }),
                },
            });
            routeLayer.entities.add({
                position: Cesium.Cartesian3.fromDegrees(p.lon, p.lat, (p.zmax || h) + 6),
                label: {
                    text: T.dryLabel + (p.distance_m != null ? " · " + fmt(p.distance_m, 0) + " m " + T.straight : ""),
                    font: "bold 15px system-ui, sans-serif",
                    fillColor: Cesium.Color.WHITE,
                    outlineColor: Cesium.Color.fromCssColorString("#14532d"),
                    outlineWidth: 4,
                    style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                    showBackground: true,
                    backgroundColor: Cesium.Color.fromCssColorString("#15803d").withAlpha(0.85),
                    verticalOrigin: Cesium.VerticalOrigin.BOTTOM,
                    disableDepthTestDistance: Number.POSITIVE_INFINITY,
                },
            });
        });
    }

    function clearRoute() {
        if (routeLayer) cesiumViewer.dataSources.remove(routeLayer, true);
        routeLayer = null;
    }

    // Low, oblique view just above the water surface onto the focus building(s).
    function flyFocus(c) {
        if (!c || c.lat == null) return;
        var radius = Math.max(parseFloat(c.radius_m) || 20, 15);
        var center = Cesium.Cartesian3.fromDegrees(parseFloat(c.long), parseFloat(c.lat),
            Math.max(scene.level, c.height_m || scene.level));
        cesiumViewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(center, radius), {
            offset: new Cesium.HeadingPitchRange(cesiumViewer.camera.heading,
                Cesium.Math.toRadians(-12), Math.max(radius * 3.2, 70)),
            duration: 2.5,
        });
    }

    // ── actions ──────────────────────────────────────────────────────────────
    function applyScene(msg) {
        if (!tilesets().length) {  // tiles still loading: retry shortly
            console.log("[flood-bridge] waiting for the tileset …");
            setTimeout(function () { applyScene(msg); }, 500);
            return;
        }
        console.log("[flood-bridge] scene: level", msg.level, "protection", msg.protection,
            "buildings", (msg.buildings || []).length, "focus", (msg.focus || []).length);
        var previous = level;
        scene = msg;
        byId = new Map(); extraById = new Map();
        bases = new Float64Array(msg.buildings.length);
        msg.buildings.forEach(function (b, i) { byId.set(String(b[0]), i); bases[i] = b[1]; });
        Object.keys(msg.extra_ids || {}).forEach(function (oid) {
            var i = byId.get(oid);
            if (i !== undefined) msg.extra_ids[oid].forEach(function (t) { extraById.set(String(t), i); });
        });
        focusIdx = new Set((msg.focus || []).map(function (o) { return byId.get(String(o)); })
            .filter(function (i) { return i !== undefined; }));
        contents.forEach(function (entry, content) {  // ids may differ after a re-import
            contents.set(content, indexContent(content));
        });

        hookTilesets();
        takeOverStyle();
        ensureWater(msg.extent);
        ensureHud();
        hud.style.display = "flex";
        hookMenu();
        placeHud();
        var start = previous !== null ? previous : (msg.from_level != null ? msg.from_level : msg.level);
        level = start;
        configureSlider();
        setWaterHeight(start);
        recolor();

        // Route view (an address and the nearest dry buildings): camera on the
        // address, lines to the dry ones. For a longer list the highlight bridge
        // has already framed all of them, so the camera is left alone.
        clearRoute();
        if (routeView()) {
            drawRoute(msg);
            if (!flyRoute(msg) && msg.centroid) flyFocus(msg.centroid);
        } else if (!focusIdx.size && !everShown && msg.extent) {
            flyOverview(msg.extent);
        }
        everShown = true;
        // First scene: let the camera arrive before the water starts rising.
        setTimeout(function () { animate(start, msg.level); }, previous === null ? 2200 : 0);
        setTimeout(function () {
            var mapped = 0, total = 0;
            contents.forEach(function (entry) {
                total += entry.idx.length;
                for (var i = 0; i < entry.idx.length; i++) if (entry.idx[i] >= 0) mapped++;
            });
            console.log("[flood-bridge] tile features mapped to buildings:", mapped, "of", total,
                "in", contents.size, "visible tiles");
        }, 4000);
    }

    function clearScene() {
        if (anim) cancelAnimationFrame(anim.frame);
        anim = null;
        scene = null; level = null; focusIdx = new Set();
        removeWater();
        clearRoute();
        contents.forEach(function (entry, content) {
            for (var i = 0; i < entry.idx.length; i++) content.getFeature(i).color = Cesium.Color.WHITE;
        });
        contents = new Map();
        tilesets().forEach(function (ts) {
            ts.colorBlendMode = Cesium.Cesium3DTileColorBlendMode.HIGHLIGHT;
        });
        if (hud) hud.style.display = "none";
        if (bridge().applyCombinedStyle) bridge().applyCombinedStyle();
    }

    window.addEventListener("message", function (event) {
        if (event.origin !== ORIGIN || event.source !== window.parent) return;
        var data = event.data;
        if (!data || typeof data !== "object" || data.type !== "flood") return;
        console.log("[flood-bridge] flood message received");
        try {
            applyScene(data);
        } catch (err) {
            console.error("[flood-bridge] could not show the flood scene:", err);
        }
    });

    // Used by highlight-bridge.js: while a scene is active it owns the colours.
    // Console diagnostics: the water's state next to the terrain and the camera.
    function debug() {
        var out = { active: scene !== null, level: level, target: scene && scene.level,
                    water: !!water, visibleTiles: contents.size,
                    depthTestAgainstTerrain: cesiumViewer.scene.globe.depthTestAgainstTerrain };
        if (water) {
            out.waterShow = water.show;
            out.waterReady = water.ready;
            out.waterHeightOffset = Cesium.Matrix4.getTranslation(water.modelMatrix, new Cesium.Cartesian3());
            out.waterHeightOffset = Cesium.Cartesian3.magnitude(out.waterHeightOffset);
            var c = Cesium.Cartographic.fromCartesian(waterCenter);
            out.terrainHeightAtCenter = cesiumViewer.scene.globe.getHeight(c);
        }
        out.cameraHeight = cesiumViewer.camera.positionCartographic.height;
        var ts = tilesets()[0];
        if (ts) out.tilesetBottom = Cesium.Cartographic.fromCartesian(ts.boundingSphere.center).height -
            ts.boundingSphere.radius;
        return out;
    }

    window._citydbFlood = {
        active: function () { return scene !== null; },
        recolor: recolor,
        clear: clearScene,
        debug: debug,
    };
})();
