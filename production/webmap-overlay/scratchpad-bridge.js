// Spatial scratchpad in the 3D viewer: the user's saved items (see
// production/docs/scratchpad.md). The parent page sends the complete list of
// the user's items ({type: "scratchpad", items, focus}) after every chat turn
// and on page load; this layer is then replaced, so the viewer always mirrors
// the database.
//
// - featureset: buildings coloured per feature (like flood-bridge.js). Order:
//   selection > featureset; while an answer's highlight is shown the sets
//   pause, so the highlight stands out. During a flood scene the sets are
//   drawn on top of the flood colours by flood-bridge.js (also not while a
//   highlight is shown).
// - geometry: Cesium entities in their own data source. Coordinates keep the
//   data's raw heights, the same convention the 3D tiles and the water use.
// - A small list (name, colour, show/hide, delete, fly to). Clicks go back to
//   the parent page, which applies them for this user and sends the new list.
(function () {
    "use strict";

    var ORIGIN = window.location.origin;
    var ID_PROPERTIES = ["objectid", "id", "GMLID", "gmlid"];
    var DE = (navigator.language || "").toLowerCase().indexOf("de") === 0;
    var T = DE ? { title: "Meine Objekte", empty: "Noch nichts gespeichert", hide: "Ausblenden",
                   show: "Einblenden", del: "Löschen", go: "Hinfliegen", objects: "Objekte",
                   collapse: "Liste einklappen", expand: "Liste ausklappen" }
               : { title: "My items", empty: "Nothing saved yet", hide: "Hide",
                   show: "Show", del: "Delete", go: "Fly to", objects: "objects",
                   collapse: "Collapse list", expand: "Expand list" };
    var HIGHLIGHT = Cesium.Color.fromCssColorString("#ff5a00");
    var SELECTION = Cesium.Color.fromCssColorString("#a855f7");
    var OTHER = Cesium.Color.fromCssColorString("#e5e7eb");
    var RECOLOR_FRAMES_AFTER_STYLE = 5;  // see recolor(): a removed style resets colours late

    var items = [];
    var colorById = new Map();   // building objectid -> Cesium.Color of its featureset
    var contents = new Map();    // tile content -> {idx: building ids per feature, stamp}
    var hooked = new WeakSet();
    var stamp = 0;
    var layer = null, panel = null, listEl = null;

    function bridge() { return window._citydbBridge || {}; }
    function tilesets() { return bridge().getTilesets ? bridge().getTilesets() : []; }
    function floodActive() { return !!(window._citydbFlood && window._citydbFlood.active()); }
    // An answer's highlight (orange) must stand out: while one is shown, the
    // saved sets pause and come back with the next answer without highlight.
    function highlightActive() {
        var b = bridge();
        return !!(b.getHighlightIds && b.getHighlightIds().length);
    }
    function active() { return colorById.size > 0 && !floodActive() && !highlightActive(); }
    var painted = false;  // feature colours set by this bridge are on the tiles

    // ── featuresets: per-feature colours ─────────────────────────────────────
    function featureId(feature) {
        for (var i = 0; i < ID_PROPERTIES.length; i++) {
            var v = feature.getProperty(ID_PROPERTIES[i]);
            if (v !== undefined && v !== null) return String(v);
        }
        return null;
    }

    // Tile features are boundary surfaces "<building objectid>_<suffix>".
    function buildingOf(id) {
        if (id === null) return null;
        if (colorById.has(id)) return id;
        var s = id;
        for (var k = 0; k < 3; k++) {
            var cut = s.lastIndexOf("_");
            if (cut <= 0) break;
            s = s.slice(0, cut);
            if (colorById.has(s)) return s;
        }
        return null;
    }

    function indexContent(content) {
        var n = content.featuresLength || 0;
        var idx = new Array(n);
        for (var i = 0; i < n; i++) idx[i] = buildingOf(featureId(content.getFeature(i)));
        return { idx: idx, stamp: -1 };
    }

    function colorContent(content, entry, hl, sel) {
        for (var i = 0; i < entry.idx.length; i++) {
            var f = content.getFeature(i);
            var c = null;
            if (sel.size || hl.size) {
                var id = featureId(f);
                if (sel.has(id)) c = SELECTION;
                else if (hl.has(id)) c = HIGHLIGHT;
            }
            var b = entry.idx[i];
            f.color = c || (b !== null ? colorById.get(b) : OTHER);
        }
        entry.stamp = stamp;
    }

    function idSets() {
        var b = bridge();
        return { hl: new Set(b.getHighlightIds ? b.getHighlightIds() : []),
                 sel: new Set(b.getSelectionIds ? b.getSelectionIds() : []) };
    }

    function recolor() {
        if (!active()) return;
        painted = true;
        stamp++;
        var s = idSets();
        var hadStyle = false;
        tilesets().forEach(function (ts) {
            if (ts.style) hadStyle = true;
            ts.style = undefined;
            ts.colorBlendMode = Cesium.Cesium3DTileColorBlendMode.REPLACE;
        });
        if (hadStyle) {
            // Removing a style (e.g. "Clear" on a highlight) takes effect a frame
            // or two later, when Cesium resets every feature to white, over the
            // colours set below. Colour again after each of the next frames.
            var scene = cesiumViewer.scene, frames = 0;
            var off = scene.postRender.addEventListener(function () {
                if (++frames >= RECOLOR_FRAMES_AFTER_STYLE) off();
                if (active()) colorVisible();
            });
            scene.requestRender();
        }
        colorVisible(s);
    }

    function colorVisible(s) {
        s = s || idSets();
        contents.forEach(function (entry, content) {
            if (content.isDestroyed && content.isDestroyed()) { contents.delete(content); return; }
            colorContent(content, entry, s.hl, s.sel);
        });
    }

    function hookTilesets() {
        tilesets().forEach(function (ts) {
            if (hooked.has(ts)) return;
            hooked.add(ts);
            ts.tileVisible.addEventListener(function (tile) {
                var content = tile.content;
                if (!content || !content.featuresLength) return;
                var entry = contents.get(content);
                if (!entry) { entry = indexContent(content); contents.set(content, entry); }
                if (active() && entry.stamp !== stamp) {
                    var s = idSets();
                    colorContent(content, entry, s.hl, s.sel);
                }
            });
            ts.tileUnload.addEventListener(function (tile) {
                if (tile.content) contents.delete(tile.content);
            });
        });
    }

    // Called by highlight-bridge.js before it applies its own style.
    function release() {
        if (!painted) return;
        painted = false;
        resetFeatureColors();
    }

    function resetFeatureColors() {
        contents.forEach(function (entry, content) {
            if (content.isDestroyed && content.isDestroyed()) return;
            for (var i = 0; i < entry.idx.length; i++) content.getFeature(i).color = Cesium.Color.WHITE;
        });
        tilesets().forEach(function (ts) {
            ts.colorBlendMode = Cesium.Cesium3DTileColorBlendMode.HIGHLIGHT;
        });
    }

    function applyFeaturesets() {
        var wasActive = colorById.size > 0;
        colorById = new Map();
        items.forEach(function (it) {
            if (it.kind !== "featureset" || !it.visible) return;
            var c = Cesium.Color.fromCssColorString(it.color || "#ff5a00");
            (it.objectids || []).forEach(function (oid) { colorById.set(String(oid), c); });
        });
        hookTilesets();
        // Building ids per feature depend on the sets: index again.
        contents.forEach(function (entry, content) { contents.set(content, indexContent(content)); });
        if (floodActive()) {
            // The flood scene draws the sets on top of its colours (colorFor).
            window._citydbFlood.recolor();
        } else if (bridge().applyCombinedStyle) {
            // highlight-bridge decides: sets (recolor), or its highlight/selection
            // style after release().
            bridge().applyCombinedStyle();
        } else if (colorById.size) {
            recolor();
        } else if (wasActive) {
            resetFeatureColors();
        }
    }

    // ── geometry items: entities ─────────────────────────────────────────────
    function ensureLayer() {
        if (!layer) {
            layer = new Cesium.CustomDataSource("scratchpad");
            cesiumViewer.dataSources.add(layer);
        }
        return layer;
    }

    function positions(coords, clamp) {
        var flat = [];
        coords.forEach(function (c) {
            flat.push(c[0], c[1]);
            if (!clamp) flat.push(c.length > 2 ? c[2] : 0);
        });
        return clamp ? Cesium.Cartesian3.fromDegreesArray(flat)
                     : Cesium.Cartesian3.fromDegreesArrayHeights(flat);
    }

    function hasZ(coords) { return coords.length && coords[0].length > 2; }

    // Cross-section of a pipe (LineString3D), radius in metres.
    function circle(r) {
        var pts = [];
        for (var i = 0; i < 16; i++) {
            var a = (i / 16) * Math.PI * 2;
            pts.push(new Cesium.Cartesian2(r * Math.cos(a), r * Math.sin(a)));
        }
        return pts;
    }

    function addPart(it, part, color, fill) {
        var L = ensureLayer().entities;
        var t = part.type, c = part.coordinates;
        var label = it.label || it.name;
        var cls = it["class"] || "";
        if (t === "Point" && cls === "Point3D") {
            // A true 3D sphere, radius from size_m (original design: Point3D).
            // With a height it is centred there; a point without one (e.g. a
            // GeoJSON point) gets a sphere resting on the terrain.
            var r = it.size_m || 1;
            var z3 = c.length > 2 && !it.clamp;
            L.add({ position: Cesium.Cartesian3.fromDegrees(c[0], c[1], z3 ? c[2] : r),
                    ellipsoid: { radii: new Cesium.Cartesian3(r, r, r), material: fill,
                                 heightReference: z3 ? Cesium.HeightReference.NONE
                                                     : Cesium.HeightReference.RELATIVE_TO_GROUND } });
            return;
        }
        if (t === "Point" && cls === "Point2D") {
            // A plain filled dot on the ground, no pin.
            L.add({ position: Cesium.Cartesian3.fromDegrees(c[0], c[1], 0),
                    point: { pixelSize: 10, color: color,
                             heightReference: Cesium.HeightReference.CLAMP_TO_GROUND,
                             disableDepthTestDistance: Number.POSITIVE_INFINITY } });
            return;
        }
        if (t === "LineString" && /LineString3D$/.test(cls) && hasZ(c) && !it.clamp) {
            // A pipe along the line, radius from size_m (original design: LineString3D).
            L.add({ polylineVolume: { positions: positions(c, false), shape: circle(it.size_m || 0.5),
                                      cornerType: Cesium.CornerType.ROUNDED, material: fill } });
            return;
        }
        if (t === "Point") {
            var clampP = it.clamp || c.length < 3;
            L.add({
                position: Cesium.Cartesian3.fromDegrees(c[0], c[1], clampP ? 0 : c[2]),
                point: { pixelSize: 14, color: color, outlineColor: Cesium.Color.WHITE, outlineWidth: 2,
                         heightReference: clampP ? Cesium.HeightReference.CLAMP_TO_GROUND
                                                 : Cesium.HeightReference.NONE,
                         disableDepthTestDistance: Number.POSITIVE_INFINITY },
                label: { text: label, font: "600 14px sans-serif", fillColor: Cesium.Color.WHITE,
                         outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
                         style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                         pixelOffset: new Cesium.Cartesian2(0, -22),
                         heightReference: clampP ? Cesium.HeightReference.CLAMP_TO_GROUND
                                                 : Cesium.HeightReference.NONE,
                         disableDepthTestDistance: Number.POSITIVE_INFINITY },
            });
            if (!clampP && cls === "3Dmarker") addStem(c, color);
        } else if (t === "LineString") {
            var clampL = it.clamp || !hasZ(c);
            L.add({ polyline: { positions: positions(c, clampL), width: 4, material: color,
                                clampToGround: clampL } });
        } else if (t === "Polygon") {
            var ring = c[0];
            var clampA = it.clamp || !hasZ(ring);
            var holes = c.slice(1).map(function (h) { return new Cesium.PolygonHierarchy(positions(h, clampA)); });
            var poly = { hierarchy: new Cesium.PolygonHierarchy(positions(ring, clampA), holes), material: fill };
            if (clampA) poly.classificationType = Cesium.ClassificationType.BOTH;
            else { poly.perPositionHeight = true; poly.outline = true; poly.outlineColor = color; }
            L.add({ polygon: poly });
        } else if (t && t.indexOf("Multi") === 0) {
            var single = t.slice(5);
            c.forEach(function (cc) { addPart(it, { type: single, coordinates: cc }, color, fill); });
        }
    }

    // A floating 3Dmarker: a thin line from the pin down to the terrain, so it
    // is clear where on the ground it stands (original design: "connected to
    // the ground"). The terrain height is sampled once; without terrain it is 0.
    function addStem(c, color) {
        var top = Cesium.Cartesian3.fromDegrees(c[0], c[1], c[2]);
        var stem = ensureLayer().entities.add({
            polyline: { positions: [top, Cesium.Cartesian3.fromDegrees(c[0], c[1], 0)], width: 2,
                        material: color.withAlpha(0.9), arcType: Cesium.ArcType.NONE },
        });
        var carto = [Cesium.Cartographic.fromDegrees(c[0], c[1])];
        var provider = cesiumViewer.terrainProvider;
        var sample = provider && provider.availability
            ? Cesium.sampleTerrainMostDetailed(provider, carto) : Promise.resolve(carto);
        sample.then(function (res) {
            var h = res && res[0] && isFinite(res[0].height) ? res[0].height : 0;
            stem.polyline.positions = [top, Cesium.Cartesian3.fromDegrees(c[0], c[1], Math.min(h, c[2]))];
        }).catch(function () { /* keep the line down to 0 */ });
    }

    function addLabel(it) {
        // Markers carry their own label; other geometries get one above their box.
        if (!it.bbox || /marker$/.test(it["class"] || "")) return;
        var b = it.bbox;
        // Above the item: a sphere's top, else its box; items without heights
        // (2D, draped) relative to the terrain, which is not at height 0.
        var sphere = it["class"] === "Point3D" ? 2 * (it.size_m || 1) : 0;
        var ground = onGround(it);
        var h = ground ? sphere + 3 : b[5] + (sphere ? sphere / 2 : 0) + 3;
        ensureLayer().entities.add({
            position: Cesium.Cartesian3.fromDegrees((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, h),
            label: { text: it.label || it.name, font: "600 14px sans-serif",
                     fillColor: Cesium.Color.WHITE, outlineColor: Cesium.Color.BLACK, outlineWidth: 3,
                     style: Cesium.LabelStyle.FILL_AND_OUTLINE,
                     heightReference: ground ? Cesium.HeightReference.RELATIVE_TO_GROUND
                                             : Cesium.HeightReference.NONE,
                     disableDepthTestDistance: Number.POSITIVE_INFINITY },
        });
    }

    function applyGeometries() {
        ensureLayer().entities.removeAll();
        items.forEach(function (it) {
            if (it.kind !== "geometry" || !it.visible) return;
            var color = Cesium.Color.fromCssColorString(it.color || "#ff5a00");
            var fill = color.withAlpha(it.alpha === undefined ? 0.8 : it.alpha);
            (it.parts || []).forEach(function (p) {
                try { addPart(it, p, color, fill); }
                catch (err) { console.warn("[scratchpad-bridge] could not draw a part of", it.name, err); }
            });
            addLabel(it);
        });
    }

    // ── camera ───────────────────────────────────────────────────────────────
    // Items without heights (2D, draped: e.g. a GeoJSON point) sit at height 0
    // in their box, while the terrain is at its real height (Passau: ~300 m
    // NHN). Flying to height 0 put the camera underground, so for those the
    // terrain height at the spot is sampled first.
    function onGround(it) {
        var b = it.bbox;
        return it.clamp || /2D(marker)?$/.test(it["class"] || "") || b[4] === null || b[5] === null ||
               (!b[4] && !b[5]);
    }

    function terrainHeight(lon, lat) {
        var carto = [Cesium.Cartographic.fromDegrees(lon, lat)];
        var provider = cesiumViewer.terrainProvider;
        if (!provider || !provider.availability) {
            var h = cesiumViewer.scene.globe.getHeight(carto[0]);
            return Promise.resolve(isFinite(h) ? h : 0);
        }
        return Cesium.sampleTerrainMostDetailed(provider, carto).then(function (res) {
            var hh = res && res[0] ? res[0].height : 0;
            return isFinite(hh) ? hh : 0;
        }).catch(function () { return 0; });
    }

    function flyTo(names) {
        var targets = items.filter(function (it) { return names.indexOf(it.name) >= 0 && it.bbox; });
        if (!targets.length) return;
        var boxes = targets.map(function (it) { return it.bbox; });
        var w = Math.min.apply(null, boxes.map(function (b) { return b[0]; }));
        var s = Math.min.apply(null, boxes.map(function (b) { return b[1]; }));
        var e = Math.max.apply(null, boxes.map(function (b) { return b[2]; }));
        var n = Math.max.apply(null, boxes.map(function (b) { return b[3]; }));
        var lon = (w + e) / 2, lat = (s + n) / 2;
        var withZ = targets.filter(function (it) { return !onGround(it); }).map(function (it) { return it.bbox; });
        var ground = withZ.length < targets.length ? terrainHeight(lon, lat) : Promise.resolve(null);
        ground.then(function (g) {
            var zs = [];
            withZ.forEach(function (b) { zs.push(b[4], b[5]); });
            if (g !== null) zs.push(g, g + 10);  // 2D items: on the terrain
            var zmin = Math.min.apply(null, zs), zmax = Math.max.apply(null, zs);
            var center = Cesium.Cartesian3.fromDegrees(lon, lat, (zmin + zmax) / 2);
            var corner = Cesium.Cartesian3.fromDegrees(e, n, zmax);
            // At least ~80 m around a single point, so its surroundings stay in
            // view, and more than a sphere/pipe's own size, so the camera is outside it.
            var size = Math.max.apply(null, targets.map(function (it) {
                return /^(Point3D|LineString3D|MultiLineString3D)$/.test(it["class"] || "") ? (it.size_m || 1) : 0;
            }));
            var radius = Math.max(Cesium.Cartesian3.distance(center, corner), 80, size * 2);
            cesiumViewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(center, radius), {
                duration: 1.5,
                offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-35), radius * 2.6),
            });
        });
    }

    // ── list panel ───────────────────────────────────────────────────────────
    function post(action, name) {
        window.parent.postMessage({ type: "scratchpad-action", action: action, name: name, t: Date.now() },
                                  ORIGIN);
    }

    // The header stays put and only the list scrolls, at most LIST_MAX_PX high
    // (less in a small viewer), so many items never cover the map.
    var LIST_MAX_PX = 220;
    var COLLAPSED_KEY = "citydb_scratchpad_collapsed";
    var collapsed = false;
    try { collapsed = localStorage.getItem(COLLAPSED_KEY) === "1"; } catch (e) { /* no storage */ }

    function ensurePanel() {
        if (panel) return;
        panel = document.createElement("div");
        panel.id = "citydb-scratchpad";
        panel.style.cssText = "position:absolute;right:8px;bottom:34px;z-index:20;width:230px;" +
            "display:flex;flex-direction:column;background:rgba(15,23,42,0.88);color:#f8fafc;" +
            "font:12px/1.35 sans-serif;border-radius:6px;padding:6px 8px;box-shadow:0 2px 8px rgba(0,0,0,.35)";
        panel.innerHTML =
            '<style>#citydb-scratchpad [data-list]{scrollbar-width:thin;scrollbar-color:#64748b transparent}' +
            '#citydb-scratchpad [data-list]::-webkit-scrollbar{width:6px}' +
            '#citydb-scratchpad [data-list]::-webkit-scrollbar-thumb{background:#64748b;border-radius:3px}</style>' +
            '<div data-head style="display:flex;align-items:center;gap:6px;flex:none;cursor:pointer;user-select:none">' +
            '<span style="font-weight:600;flex:1">' + T.title + ' <span data-count style="font-weight:400;color:#94a3b8"></span></span>' +
            '<span data-toggle style="color:#cbd5e1;font-size:11px"></span></div>' +
            '<div data-list style="flex:1 1 auto;min-height:0;overflow-y:auto;overscroll-behavior:contain;' +
            'margin-top:4px;padding-right:2px;max-height:min(' + LIST_MAX_PX + 'px, calc(45vh - 40px))"></div>';
        listEl = panel.querySelector("[data-list]");
        panel.querySelector("[data-head]").onclick = function () {
            collapsed = !collapsed;
            try { localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0"); } catch (e) { /* no storage */ }
            updateCollapsed();
        };
        updateCollapsed();
        (document.getElementById("cesiumContainer") || document.body).appendChild(panel);
    }

    function updateCollapsed() {
        listEl.style.display = collapsed ? "none" : "block";
        panel.querySelector("[data-toggle]").textContent = collapsed ? "▸" : "▾";
        panel.querySelector("[data-head]").title = collapsed ? T.expand : T.collapse;
    }

    function button(text, title, onclick) {
        var b = document.createElement("button");
        b.textContent = text;
        b.title = title;
        b.style.cssText = "background:none;border:0;color:#cbd5e1;cursor:pointer;padding:0 3px;font-size:13px";
        b.onclick = function (ev) { ev.stopPropagation(); onclick(); };
        return b;
    }

    function renderPanel(focus) {
        ensurePanel();
        panel.style.display = items.length ? "flex" : "none";
        panel.querySelector("[data-count]").textContent = "(" + items.length + ")";
        var scrollTop = listEl.scrollTop;  // a refresh rebuilds the rows; keep the place
        var focusRow = null;
        listEl.innerHTML = "";
        items.forEach(function (it) {
            var row = document.createElement("div");
            if (focus && focus.indexOf(it.name) >= 0) focusRow = row;
            row.style.cssText = "display:flex;align-items:center;gap:4px;padding:2px 0;" +
                (it.visible ? "" : "opacity:.5");
            var sw = document.createElement("span");
            sw.style.cssText = "width:10px;height:10px;border-radius:2px;flex:none;background:" + it.color;
            var name = document.createElement("span");
            name.style.cssText = "flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer";
            name.textContent = it.label || it.name;
            name.title = it.name + " · " + (it["class"] || it.type || "") +
                (it.kind === "featureset" ? " · " + (it.count || 0) + " " + T.objects : "");
            name.onclick = function () { if (it.visible) flyTo([it.name]); else post("goto", it.name); };
            row.appendChild(sw);
            row.appendChild(name);
            row.appendChild(button(it.visible ? "👁" : "◌", it.visible ? T.hide : T.show,
                                   function () { post(it.visible ? "hide" : "show", it.name); }));
            row.appendChild(button("🗑", T.del, function () { post("delete", it.name); }));
            listEl.appendChild(row);
        });
        listEl.scrollTop = scrollTop;
        // a new or flown-to item: scroll the list (only the list, not the page) to it
        if (focusRow && !collapsed) {
            var top = focusRow.offsetTop - listEl.offsetTop;
            if (top < listEl.scrollTop || top + focusRow.offsetHeight > listEl.scrollTop + listEl.clientHeight) {
                listEl.scrollTop = Math.max(0, top - listEl.clientHeight / 2);
            }
        }
    }

    // ── messages ─────────────────────────────────────────────────────────────
    function apply(msg) {
        if (typeof cesiumViewer === "undefined" || !cesiumViewer.scene || !tilesets().length) {
            setTimeout(function () { apply(msg); }, 500);
            return;
        }
        items = (msg.items || []).filter(function (it) { return it && it.name; });
        applyFeaturesets();
        applyGeometries();
        var focus = (msg.focus || []).filter(function (n) {
            return items.some(function (it) { return it.name === n && it.visible; });
        });
        renderPanel(focus);
        if (focus.length) flyTo(focus);
        console.log("[scratchpad-bridge] items:", items.length, "featureset buildings:", colorById.size,
                    "focus:", focus);
    }

    window.addEventListener("message", function (event) {
        if (event.origin !== ORIGIN || event.source !== window.parent) return;
        var data = event.data;
        if (!data || typeof data !== "object" || data.type !== "scratchpad") return;
        try { apply(data); }
        catch (err) { console.error("[scratchpad-bridge] could not show the scratchpad:", err); }
    });

    // Used by highlight-bridge.js (while featuresets are shown they own the
    // colours) and by flood-bridge.js (sets drawn on top of the flood).
    window._citydbScratchpad = {
        active: active,
        recolor: recolor,
        release: release,
        debug: function () {
            return { sets: colorById.size, visibleTiles: contents.size, painted: painted, active: active(),
                     highlight: highlightActive(), flood: floodActive() };
        },
        hasSets: function () { return colorById.size > 0; },
        colorFor: function (id) { var b = buildingOf(id); return b !== null ? colorById.get(b) : null; },
    };

    // The page may have sent the list before this frame was ready: ask again.
    if (window.parent !== window) window.parent.postMessage({ type: "scratchpad-ready" }, ORIGIN);
})();
